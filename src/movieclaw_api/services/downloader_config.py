"""下载器配置服务：用户接入的下载软件的增删改查与连接验证。

与站点配置（site_config + verification）完全同构：
- 写入前做业务校验（名称唯一），落库后状态置 PENDING；
- 真正的连通性验证交给后台任务 ``verify_downloader``（测一次连接）；
- VERIFYING 期间拒绝更新/删除/重复验证（409 并发守卫，原理见 SiteConfigService）。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import ConflictException, NotFoundException
from movieclaw_api.services.download_health import adopt_landing_groups
from movieclaw_api.services.downloader_paths import PathProbe, probe_mappings, summarize
from movieclaw_db.engine import get_database
from movieclaw_db.models.downloader_client import ClientType, DownloaderClient
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader import DownloaderConfig, DownloaderException, create_downloader

logger = logging.getLogger("movieclaw_api.downloader_config")

# 连接测试用较短超时：测试只为回答"通不通"，没必要等默认 30 秒
_TEST_TIMEOUT = 10.0


class DownloaderConfigService:
    """下载器配置的业务服务。绑定一个数据库会话。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = DownloaderRepository(session)

    @staticmethod
    def _assert_not_verifying(row: DownloaderClient) -> None:
        """若下载器正在测试连接，拒绝当前操作（409）。"""
        if row.status == ConfigStatus.VERIFYING:
            raise ConflictException(f"下载器「{row.name}」正在测试连接，请等待完成后再操作")

    # -- 查询 --------------------------------------------------------------

    async def list_all(self) -> list[DownloaderClient]:
        """返回所有已配置的下载器（含各自验证状态）。"""
        return await self._repo.list_all()

    async def get(self, downloader_id: int) -> DownloaderClient:
        """按 id 获取；不存在抛 404。"""
        row = await self._repo.get(downloader_id)
        if row is None:
            raise NotFoundException(f"下载器不存在：id={downloader_id}")
        return row

    # -- 写入 --------------------------------------------------------------

    async def _assert_name_available(self, name: str, *, exclude_id: int | None = None) -> None:
        """名称唯一性校验（更新时排除自身）。"""
        existing = await self._repo.get_by_name(name)
        if existing is not None and existing.id != exclude_id:
            raise ConflictException(f"名称「{name}」已被使用，请换一个")

    async def create(
        self,
        *,
        name: str,
        client_type: ClientType,
        url: str,
        username: str | None,
        password: str | None,
        save_path: str | None,
        path_mappings: list[dict[str, str]] | None = None,
        enabled: bool = True,
    ) -> DownloaderClient:
        """新增下载器配置（状态置 PENDING，等待异步测试连接）。"""
        await self._assert_name_available(name)
        return await self._repo.create(
            name=name,
            client_type=client_type,
            url=url,
            username=username,
            password=password,
            save_path=save_path,
            path_mappings=path_mappings,
            enabled=enabled,
        )

    async def update(
        self,
        downloader_id: int,
        *,
        name: str,
        client_type: ClientType,
        url: str,
        username: str | None,
        password: str | None,
        save_path: str | None,
        path_mappings: list[dict[str, str]] | None = None,
        enabled: bool,
    ) -> DownloaderClient:
        """整体更新下载器配置；不存在抛 404，正在验证中抛 409。"""
        row = await self.get(downloader_id)
        self._assert_not_verifying(row)
        await self._assert_name_available(name, exclude_id=downloader_id)
        updated = await self._repo.update(
            downloader_id,
            name=name,
            client_type=client_type,
            url=url,
            username=username,
            password=password,
            save_path=save_path,
            path_mappings=path_mappings,
            enabled=enabled,
        )
        assert updated is not None  # get() 已确认存在
        return updated

    async def start_verification(self, downloader_id: int) -> DownloaderClient:
        """同步占位为 VERIFYING 并返回，随后由调用方排队后台测试任务。

        并发守卫原理见 SiteConfigService.start_verification。
        """
        row = await self.get(downloader_id)
        self._assert_not_verifying(row)
        await self._repo.update_status(downloader_id, ConfigStatus.VERIFYING)
        return await self.get(downloader_id)

    async def set_enabled(self, downloader_id: int, enabled: bool) -> DownloaderClient:
        """启用/停用；与验证状态正交，不受 VERIFYING 守卫限制。"""
        ok = await self._repo.set_enabled(downloader_id, enabled)
        if not ok:
            raise NotFoundException(f"下载器不存在：id={downloader_id}")
        return await self.get(downloader_id)

    async def set_default(self, downloader_id: int) -> DownloaderClient:
        """设为默认下载器；与验证状态正交，不受 VERIFYING 守卫限制。

        会同时清掉其他台的默认标记（有且只有一个默认的不变量由 repo 维护）。
        """
        ok = await self._repo.set_default(downloader_id)
        if not ok:
            raise NotFoundException(f"下载器不存在：id={downloader_id}")
        return await self.get(downloader_id)

    async def delete(self, downloader_id: int) -> None:
        """删除下载器配置；不存在抛 404，正在验证中抛 409。"""
        row = await self.get(downloader_id)
        self._assert_not_verifying(row)
        await self._repo.delete(downloader_id)
        # 配置已不存在，它的连接失败告警（若有）随之作废
        from movieclaw_api.services.system_notice import resolve_notices

        await resolve_notices(self._session, dedupe_key=f"downloader:{downloader_id}")


# ---------------------------------------------------------------------------
# 后台连接测试（与 verification.verify_site 同构的背景任务）
# ---------------------------------------------------------------------------


async def verify_downloader(downloader_id: int) -> None:
    """异步测试某下载器的连通性与凭证，并把结论写回状态字段。

    验证判据：真实调用一次 ``test_connection()``，能拿到版本号即证明
    地址、凭证均有效。

    前置约定：调用前状态已被 start_verification 置为 VERIFYING。
    作为背景任务：自开独立数据库会话，绝不向外抛异常 ——
    任何失败都转成 FAILED + last_error。
    """
    async with get_database().session() as session:
        repo = DownloaderRepository(session)
        row = await repo.get(downloader_id)
        if row is None:
            logger.warning("测试连接时下载器已被删除：id=%s", downloader_id)
            return

        config = DownloaderConfig(
            type=row.client_type.value,
            url=row.url,
            username=row.username,
            password=repo.decrypted_password(row),
            timeout=_TEST_TIMEOUT,
        )
        downloader = create_downloader(config)
        try:
            info = await downloader.test_connection()
        except DownloaderException as exc:
            # Downloader* 异常的 message 本身已是清晰中文，直接展示
            logger.info("下载器连接测试失败：%s（%s）", row.name, exc.message)
            await repo.update_status(downloader_id, ConfigStatus.FAILED, last_error=exc.message)
            await _notify_failed(session, downloader_id, row.name, exc.message)
            return
        except Exception as exc:  # noqa: BLE001 -- 背景任务兜底，绝不外抛
            logger.exception("下载器连接测试发生未知错误：%s", row.name)
            error = f"测试时发生未知错误（{type(exc).__name__}）：{exc}"
            await repo.update_status(downloader_id, ConfigStatus.FAILED, last_error=error)
            await _notify_failed(session, downloader_id, row.name, error)
            return
        finally:
            await downloader.close()

        logger.info("下载器连接测试通过：%s（%s %s）", row.name, info.type.value, info.version)
        # API 通了，再体检路径映射：两者是独立的故障面，绿灯必须两者都过才算数。
        # 只在连接成功后做——连接都不通时路径结论没有意义，也别让两种失败互相遮盖
        probes = probe_mappings(row.path_mappings)
        await repo.update_status(
            downloader_id,
            ConfigStatus.ACTIVE,
            version=info.version,
            path_health=[p.as_dict() for p in probes] or None,
        )
        # 连接恢复，红灯熄灭
        from movieclaw_api.services.system_notice import resolve_notices

        await resolve_notices(session, dedupe_key=f"downloader:{downloader_id}")
        await _notify_paths(session, downloader_id, row.name, probes)


async def _notify_paths(
    session: AsyncSession, downloader_id: int, name: str, probes: list[PathProbe]
) -> None:
    """路径体检结论 → 全局红灯（或熄灯）。

    与连接失败的红灯分开 key：连接失败是"下载器不可达"，路径异常是"下载器
    好好的、movieclaw 看不到它放的文件"。两者的修法完全不同，混成一盏灯会把
    用户引去重启下载器，而真正要重启的是 movieclaw 自己。
    """
    from movieclaw_api.services.system_notice import resolve_notices, upsert_notice
    from movieclaw_db.models import NoticeSeverity

    key = f"downloader.paths:{downloader_id}"
    worst = summarize(probes)
    if worst is None:
        await resolve_notices(session, dedupe_key=key)
        return
    state, detail = worst
    broken = [p for p in probes if not p.healthy]
    logger.warning(
        "下载器「%s」路径体检发现 %d 条映射异常（最严重：%s）", name, len(broken), state.value
    )
    await upsert_notice(
        session,
        dedupe_key=key,
        severity=NoticeSeverity.ERROR,
        source="downloader",
        title=f"下载器「{name}」的下载目录 movieclaw 看不到",
        message=(
            f"连接正常，但 {len(broken)} 条路径映射的本地侧不可用，"
            f"下载完成的内容将无法入库。{detail}"
        ),
        payload={
            "group_key": key,
            "downloader_id": downloader_id,
            "paths": [p.as_dict() for p in broken],
        },
    )
    # 同一挂载故障可能已经被完成种子的落点核验撞上、亮了目录级红灯：收编到
    # 这盏更上游的灯下面，告警中心只显示一条
    await adopt_landing_groups(session, downloader_id)


async def _notify_failed(session: AsyncSession, downloader_id: int, name: str, error: str) -> None:
    """下载器验证失败 → 全局红灯：下载器坏了，订阅投递与完成检测全部停摆。"""
    from movieclaw_api.services.system_notice import upsert_notice
    from movieclaw_db.models import NoticeSeverity

    await upsert_notice(
        session,
        dedupe_key=f"downloader:{downloader_id}",
        severity=NoticeSeverity.ERROR,
        source="downloader",
        title=f"下载器「{name}」连接失败",
        message=f"下载器「{name}」连接测试失败：{error}",
        payload={"downloader_id": downloader_id},
    )
