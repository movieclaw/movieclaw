from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
    UpstreamServiceException,
)
from movieclaw_api.services.auth_factory import missing_required_fields
from movieclaw_api.services.site_access import invalidate_site_access
from movieclaw_api.services.site_catalog import SiteCatalogService
from movieclaw_db.models.site_credential import AuthType, ConfigStatus, SiteCredential
from movieclaw_db.models.site_user_profile import SiteUserProfile
from movieclaw_db.repositories.cookie_repo import CookieRepository
from movieclaw_db.repositories.credential_repo import CredentialRepository
from movieclaw_db.repositories.profile_repo import ProfileRepository
from movieclaw_db.repositories.torrent_repo import TorrentRepository


class SiteConfigService:
    """已配置站点服务：用户"可用站点"的增删改查。

    职责边界：
    - 写入前做**业务校验**（站点是否存在于目录、授权类型是否被支持、必填字段是否齐全）。
    - 校验通过后落库（状态置 PENDING），真正的有效性验证交给异步验证流程。
    - 删除配置时**连带清理该站点的 cookie 缓存**，避免残留旧会话。

    并发状态守卫（关键设计）：
    当某站点正处于 VERIFYING（验证进行中）时，一切"会改动凭据或重复触发验证"的
    操作——更新、删除、再次验证——都被拒绝（抛 409 冲突）。配套手段是"同步占位"：
    触发验证时在**当前请求内**就把状态改为 VERIFYING（见 start_verification），
    而非等后台任务再改，从而关闭并发窗口，让前端立刻看到"验证中"并禁用相关按钮。
    （启用/停用与验证正交，不受此守卫限制。）

    本服务绑定一个数据库会话；目录校验委托给无状态的 SiteCatalogService。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._credentials = CredentialRepository(session)
        self._cookies = CookieRepository(session)
        self._torrents = TorrentRepository(session)
        self._profiles = ProfileRepository(session)
        self._catalog = SiteCatalogService()

    @staticmethod
    def _assert_not_verifying(row: SiteCredential) -> None:
        """若站点正在验证中，拒绝当前操作（409）。"""
        if row.status == ConfigStatus.VERIFYING:
            raise ConflictException(f"站点 {row.site_id} 正在验证中，请等待验证完成后再操作")

    # -- 查询 --------------------------------------------------------------

    async def list_configured(self) -> list[SiteCredential]:
        """返回用户已配置的所有站点（含各自验证状态）。"""
        return await self._credentials.list_all()

    async def get_configured(self, site_id: str) -> SiteCredential:
        """获取单个已配置站点；未配置时抛 404。"""
        row = await self._credentials.get_by_site(site_id)
        if row is None:
            raise NotFoundException(f"站点尚未配置：{site_id}")
        return row

    async def profile_of(self, site_id: str) -> SiteUserProfile | None:
        """获取某站点的用户资料快照；从未验证成功过则返回 None。

        供路由层把资料嵌进 ConfiguredSite 视图（见 schemas.site.SiteUserProfileView）。
        """
        return await self._profiles.get_by_site(site_id)

    async def profiles_of(self, site_ids: list[str]) -> dict[str, SiteUserProfile]:
        """批量获取多个站点的资料快照（site_id → 快照），列表页一次查完避免 N+1。"""
        return await self._profiles.get_many(site_ids)

    # -- 写入 --------------------------------------------------------------

    async def configure(
        self,
        *,
        site_id: str,
        auth_type: AuthType,
        cookie: str | None = None,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        enabled: bool = True,
    ) -> SiteCredential:
        """新增或更新一个站点配置。

        校验顺序：
        0. 若站点已存在且正在验证中 → 409（不允许边验证边改凭据）。
        1. 站点必须存在于目录且支持该授权类型（否则 400/404）。
        2. 该授权类型要求的字段必须填全（否则 400）。
        校验通过后写库，状态自动重置为 PENDING，等待异步验证。
        """
        # 0. 并发守卫：验证进行中不允许修改凭据
        existing = await self._credentials.get_by_site(site_id)
        if existing is not None:
            self._assert_not_verifying(existing)

        # 1. 目录校验：站点存在 + 支持该授权类型
        self._catalog.assert_auth_type_supported(site_id, auth_type)

        # 2. 必填字段校验
        values = {
            "cookie": cookie,
            "api_key": api_key,
            "username": username,
            "password": password,
        }
        missing = missing_required_fields(auth_type, values)
        if missing:
            raise BadRequestException(
                f"授权类型 '{auth_type.value}' 缺少必填字段：{', '.join(missing)}"
            )

        # 3. 落库（状态在 repo 内重置为 PENDING）
        credential = await self._credentials.upsert(
            site_id=site_id,
            auth_type=auth_type,
            cookie=cookie,
            api_key=api_key,
            username=username,
            password=password,
            enabled=enabled,
        )
        # 4. 建立同步游标：以「此刻」为跟踪起点 t0，next_sync_at 保持 NULL（立即到期），
        #    使站点通过验证后，下一个 tick 即触发首刷。幂等——已存在则不改动既有 t0。
        await self._torrents.ensure_cursor(site_id)
        # 5. 凭据已变更，作废共享客户端缓存，确保下次访问用新授权重建
        await invalidate_site_access(site_id)
        return credential

    async def start_verification(self, site_id: str) -> SiteCredential:
        """同步占位：把站点状态改为 VERIFYING 并返回，随后由调用方排队后台任务。

        这是并发守卫的核心：在请求内就抢占 VERIFYING 状态，使得在后台验证跑完前，
        任何更新/删除/再次验证都会因看到 VERIFYING 而被拒绝，关闭并发窗口。

        - 未配置 → 404。
        - 已在验证中 → 409（避免重复触发）。
        """
        row = await self.get_configured(site_id)  # 未配置抛 404
        self._assert_not_verifying(row)  # 已在验证中抛 409
        await self._credentials.update_status(site_id, ConfigStatus.VERIFYING)
        return await self.get_configured(site_id)

    async def set_enabled(self, site_id: str, enabled: bool) -> SiteCredential:
        """启用/停用站点；未配置时抛 404。返回更新后的记录。

        注意：启用/停用与验证状态正交，**刻意不受 VERIFYING 守卫限制** ——
        用户随时可以停用一个正在验证的站点，二者互不影响。
        """
        ok = await self._credentials.set_enabled(site_id, enabled)
        if not ok:
            raise NotFoundException(f"站点尚未配置：{site_id}")
        # 启停影响可访问性，作废共享缓存（停用后再取会被拒、启用后按新状态重建）
        await invalidate_site_access(site_id)
        return await self.get_configured(site_id)

    async def set_protected(self, site_id: str, protected: bool) -> SiteCredential:
        """打开/关闭站点保护；未配置时抛 404。返回更新后的记录。

        保护开关只影响订阅链路的选站（搜索扇出与评估投递两道闸都在读库时
        取最新值），不涉及站点访问凭据，无需作废共享客户端缓存。
        """
        ok = await self._credentials.set_protected(site_id, protected)
        if not ok:
            raise NotFoundException(f"站点尚未配置：{site_id}")
        return await self.get_configured(site_id)

    async def set_ratio_boost(
        self,
        site_id: str,
        *,
        enabled: bool,
        budget_bytes: int | None = None,
        hold_days: int | None = None,
    ) -> SiteCredential:
        """设置自动刷分享率开关、预算与汰换保留期；未配置时抛 404。

        预算调小不会立刻删种——刷流引擎在下一个 tick 按汰换规则（保留期 +
        效率下限）逐步收敛到新预算，绝不为了腾空间违反保留期。
        保留期 0 = 站点无 H&R 考核、不设保护（判定成熟度仍由引擎测量窗保证）。
        """
        if budget_bytes is not None and budget_bytes < 1024**3:
            raise BadRequestException("刷流存储预算不能小于 1 GiB")
        if hold_days is not None and not (0 <= hold_days <= 30):
            raise BadRequestException("刷流汰换保留期须在 0～30 天之间（0=不保护）")
        row = await self._credentials.get_by_site(site_id)
        if row is None:
            raise NotFoundException(f"站点尚未配置：{site_id}")
        was_paused = row.boost_paused
        await self._credentials.set_ratio_boost(
            site_id, enabled=enabled, budget_bytes=budget_bytes, hold_days=hold_days
        )
        if not enabled and was_paused:
            # 关闭刷流会连带清掉暂停态（见 credential_repo），做种上的暂停
            # 限速也要一并解除——尽力而为：下载器不可达时只告警，任务保留
            # 做种但仍被限速，用户可在下载器里自行解除
            from movieclaw_api.services.ratio_boost import apply_boost_pause

            await apply_boost_pause(self._session, site_id, False)
        if enabled:
            # 立刻生效：游标标记到期，下一个 tick（≤2 分钟）即同步一次索引，
            # 之后同步节奏被钉在最快档（见 torrent_sync._adapt_interval 的 pinned），
            # 免费新种的发现延迟收敛到 5 分钟级，而不是等冷站的旧排期走完
            await self._torrents.expire_cursor(site_id)
        return await self.get_configured(site_id)

    async def set_boost_paused(self, site_id: str, paused: bool) -> SiteCredential:
        """暂停 / 恢复某站点的刷流；未配置时抛 404，未开启刷流时抛 400。

        暂停是"临时给前台流量让路"：在池做种批量压到极低上传限速，引擎
        停止汰换与拉新种（任务与数据全部保留）；恢复则解除限速、回到正常
        节奏。两个方向的落地策略刻意不对称：

        - **暂停**：先落库再下发限速——即使此刻下载器不可达，引擎每个 tick
          都会重新下发（幂等自愈），暂停意图不能因一次网络抖动而丢失；
        - **恢复**：先解除限速、全部成功才落库——若有下载器没解除成功就
          报错保持暂停态，绝不能出现"界面显示运行中、做种却停在 1 KiB/s"
          的静默失速。
        """
        row = await self.get_configured(site_id)  # 未配置抛 404
        if not row.boost_enabled:
            raise BadRequestException(f"站点 {site_id} 未开启刷流，无法暂停/恢复")
        from movieclaw_api.services.ratio_boost import apply_boost_pause

        if paused:
            await self._credentials.set_boost_paused(site_id, True)
            await apply_boost_pause(self._session, site_id, True)  # 失败由 tick 重试
        else:
            failed = await apply_boost_pause(self._session, site_id, False)
            if failed:
                raise UpstreamServiceException(
                    "部分下载器不可达，做种的限速尚未解除，刷流保持暂停状态；"
                    "请检查下载器连接后重试恢复"
                )
            await self._credentials.set_boost_paused(site_id, False)
        return await self.get_configured(site_id)

    async def delete(self, site_id: str) -> None:
        """删除站点配置，并连带清理其 cookie 缓存。

        - 未配置 → 404。
        - 正在验证中 → 409（不允许删除正在被后台任务使用的配置）。
        """
        row = await self.get_configured(site_id)  # 未配置抛 404
        self._assert_not_verifying(row)  # 验证中抛 409
        if row.boost_paused:
            # 暂停中删除站点：转出的任务会继续做种，先尽力解除暂停限速，
            # 避免它们永远停在 1 KiB/s（失败只告警，用户可在下载器里解除）
            from movieclaw_api.services.ratio_boost import apply_boost_pause

            await apply_boost_pause(self._session, site_id, False)
        await self._credentials.delete(site_id)
        # 凭据已删，遗留的 cookie 会话也一并清掉
        await self._cookies.delete(site_id)
        # 用户资料快照是凭据的派生缓存，随配置一起删除
        await self._profiles.delete(site_id)
        # 配置已不存在，它的验证失败告警（若有）随之作废
        from movieclaw_api.services.system_notice import resolve_notices

        await resolve_notices(self._session, dedupe_key=f"site:{site_id}")
        # 连带清理该站的本地种子快照与同步游标，避免重新添加时命中过期高水位
        await self._torrents.delete_site_data(site_id)
        # 该站在池的刷流任务转出管理：预算主体已不存在，任务与数据保留做种
        from movieclaw_api.services.ratio_boost import release_site_tasks

        await release_site_tasks(self._session, site_id)
        # 作废共享客户端缓存并释放其连接
        await invalidate_site_access(site_id)
