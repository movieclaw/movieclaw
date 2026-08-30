"""媒体库配置服务：库的增删改查、默认库不变量与入库路径推导（L1）。

媒体库是"我拥有哪些影视内容、放在哪里"的权威定义（docs/design/library.md）。
L1 阶段它的唯一消费者是投递：订阅/手动下载按"入库到哪个库"确定 save_path
（``derive_save_path``：主根 + 规范条目目录名）。入库管线、扫描等能力
在 L2/L3 接入。

系统不预置任何默认库：首次部署库表为空，由前端空态引导用户按自己的
目录结构创建（NAS 用户的媒体盘路径千差万别，预置库只会造成误导）。
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import BadRequestException, ConflictException, NotFoundException
from movieclaw_db.models.library import Library
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.library_config")

# 后台监听重建任务的存活引用：create_task 若不持有引用可能被 GC 中途取消
_refresh_tasks: set[asyncio.Task] = set()

# 条目目录名里的文件系统保留字符（跨 ext4/NTFS/APFS 的并集），统一替换为空格。
# Plex/Emby 对目录名的解析只依赖 "标题 (年份)" 结构，替换不影响识别。
_FORBIDDEN_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_folder_name(name: str) -> str:
    """把标题清洗成安全的目录名：替换保留字符、折叠空白、去首尾点与空格。"""
    cleaned = _FORBIDDEN_CHARS.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "未命名"


def derive_entry_dir(
    root: str,
    *,
    title: str,
    year: int | None,
    item: object | None = None,
    library: object | None = None,
) -> str:
    """由根路径推导条目目录（目录名走命名模板，默认 ``{title} ({year})``）。

    库入库与监听导入的自定义目录目标共用同一命名规范——命名同源是
    "整理输出 → 外部流转 → 库根回流"链路的唯一衔接机制
    （docs/design/strm-workflow.md）；模板化后这个"同一"由
    ``library/naming.py`` 独家保证，本函数不再自己拼名字。
    路径用 POSIX 分隔符拼接。
    """
    from movieclaw_api.services.library.naming import entry_dir_name, item_context

    # 给了条目就用完整上下文：模板里的 {original_title}/{tmdb_id} 只有拿到
    # 条目才渲染得出来。手动下载等"还没有 TMDB 身份"的场景只有 title/year，
    # 那些占位符渲染为空并被收缩——文件落盘后由扫描识别、整理给出规范名
    extra = item_context(item) if item is not None else {}
    # title/year 以显式入参为准（调用方拿到的就是权威值），条目只补其余占位符
    extra.pop("title", None)
    extra.pop("year", None)
    return posixpath.join(
        root.rstrip("/"), entry_dir_name(title=title, year=year, library=library, **extra)
    )


def derive_save_path(
    library: Library, *, title: str, year: int | None, item: object | None = None
) -> str | None:
    """由库推导入库保存路径：``{主根}/{title} ({year})``。

    电影与剧集同构（剧集的 Season 子目录是 L2 整理器的职责，投递阶段
    下载器只需要落到条目目录）。库没有根路径时返回 None（调用方回落
    到下载器默认目录）。路径用 POSIX 分隔符拼接——save_path 是给
    下载器所在环境用的，movieclaw 部署面向 Linux/NAS/Docker。
    """
    root = library.primary_root
    if not root:
        return None
    # 库自己就是覆盖来源：命名模板可按库覆盖（动漫库与电影库各用一套）
    return derive_entry_dir(root, title=title, year=year, item=item, library=library)


class LibraryConfigService:
    """媒体库配置的业务服务。绑定一个数据库会话。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = LibraryRepository(session)

    # -- 查询 --------------------------------------------------------------

    async def list_all(self, *, kind: str | None = None) -> list[Library]:
        """返回全部库（可按类型过滤）。"""
        return await self._repo.list_all(kind=kind)

    async def get(self, library_id: int) -> Library:
        """按 id 获取；不存在抛 404。"""
        row = await self._repo.get(library_id)
        if row is None:
            raise NotFoundException(f"媒体库不存在：id={library_id}")
        return row

    async def resolve_for_subscription(
        self, library_id: int | None, kind: str, *, item=None
    ) -> Library | None:
        """解析订阅/投递实际使用的库：显式指定 → 收藏范围路由 → 该类型默认库。

        ``library_id`` 为 NULL（老订阅/定格库已被删除）且给了 ``item`` 时按
        收藏范围路由（route 内含默认库兜底）；不给 item 维持默认库语义。
        类型没有任何库时返回 None——调用方回落到下载器默认目录，不阻断投递。
        """
        if library_id is not None:
            row = await self._repo.get(library_id)
            if row is not None:
                return row
        if item is not None:
            from movieclaw_api.services.library.routing import route_for_item

            return (await route_for_item(self._session, kind, item)).library
        return await self._repo.get_default(kind)

    # -- 写入 --------------------------------------------------------------

    def _validate(self, *, name: str, root_paths: list[str]) -> list[str]:
        """公共校验：名称非空、根路径非空且均为绝对路径。返回清洗后的根列表。"""
        if not name.strip():
            raise BadRequestException("库名称不能为空")
        cleaned = [p.strip() for p in root_paths if p.strip()]
        if not cleaned:
            raise BadRequestException("至少需要一个根路径（第一个为主根，新入库落在这里）")
        for path in cleaned:
            if not path.startswith("/"):
                raise BadRequestException(f"根路径必须是绝对路径：{path}")
        if len(set(cleaned)) != len(cleaned):
            raise BadRequestException("根路径存在重复项")
        return cleaned

    @staticmethod
    def _validate_overrides(raw: dict | None) -> dict | None:
        """库级刮削覆盖的校验：只收可覆盖字段，值按全局设置的同一套规则把关。

        不合法就整体拒绝（400 中文文案）而不是静默丢弃——用户在库设置里
        写了个坏模板却"保存成功"，比报错难查得多。None = 不改动。
        """
        if raw is None:
            return None
        from movieclaw_api.services.scrape_config import (
            LIBRARY_OVERRIDABLE,
            sanitize_overrides,
        )
        from movieclaw_api.settings import MetadataScrapeSetting

        unknown = sorted(set(raw) - LIBRARY_OVERRIDABLE)
        if unknown:
            raise BadRequestException(
                f"这些设置不支持按库覆盖：{'、'.join(unknown)}（不是刮削设置里的字段）"
            )
        overrides = sanitize_overrides(raw)
        if not overrides:
            return {}
        try:
            MetadataScrapeSetting.model_validate(
                {**MetadataScrapeSetting().model_dump(), **overrides}
            )
        except Exception as exc:  # noqa: BLE001 -- 转成用户可读的 400
            detail = str(exc).split("\n")[-2].strip() if "\n" in str(exc) else str(exc)
            raise BadRequestException(f"库级刮削设置不合法：{detail}") from exc
        return overrides

    async def _assert_roots_clear_of_import_watch(self, roots: list[str]) -> None:
        """根路径不得与任何监听导入规则的源目录或自定义目录前缀重叠。

        源目录在库根之下会被扫描当存量原地入账，库根在源目录之下会把
        整库当"下载完成的条目"搬运，两个方向都是双头管理的灾难；
        自定义目录（target_path）里是等待外部流转的"过客"文件，被库
        扫描收编等于绕过了流转语义。监听导入侧建规则时做同样的反向
        校验（import_watch_config）。
        """
        from sqlmodel import select

        from movieclaw_db.models import ImportWatch

        rows = list((await self._session.execute(select(ImportWatch))).scalars().all())
        for root in roots:
            r = root.rstrip("/")
            for rule in rows:
                s = rule.source_path.rstrip("/")
                if r == s or r.startswith(s + "/") or s.startswith(r + "/"):
                    raise BadRequestException(
                        f"根路径与监听导入的源目录重叠：{root} ↔ {rule.source_path}；"
                        "请先调整「监听导入」配置"
                    )
                t = (rule.target_path or "").rstrip("/")
                if t and (r == t or r.startswith(t + "/") or t.startswith(r + "/")):
                    raise BadRequestException(
                        f"根路径与监听导入的自定义目录重叠：{root} ↔ {rule.target_path}；"
                        "请先调整「监听导入」配置"
                    )

    async def _assert_roots_clear_of_other_libraries(
        self, roots: list[str], *, exclude_id: int | None = None
    ) -> None:
        """根路径不得与**其他库**的根路径相同或嵌套（双向）。

        台账 ``library_file.file_path`` 是全局唯一键——一个文件属于且仅属于
        一个库。两个库盖住同一片目录没有合理语义：谁后扫描谁就把台账行抢走，
        文件在两个库之间反复横跳，扫描还会撞唯一键整轮失败。这种配置必须在
        保存时拒绝，而不是等扫描时炸出天书报错。同一个库自己的嵌套根路径
        不受此限（扫描按 seen_paths 去重，明确支持）。
        """
        for other in await self._repo.list_all():
            if other.id == exclude_id:
                continue
            for root in roots:
                r = root.rstrip("/")
                for other_root in other.root_paths:
                    s = other_root.rstrip("/")
                    if r == s or r.startswith(s + "/") or s.startswith(r + "/"):
                        raise BadRequestException(
                            f"根路径与媒体库「{other.name}」重叠：{root} ↔ {other_root}；"
                            "一个目录只能归属一个库，请调整其中一方的根路径"
                        )

    async def _assert_name_available(self, name: str, *, exclude_id: int | None = None) -> None:
        existing = await self._repo.get_by_name(name)
        if existing is not None and existing.id != exclude_id:
            raise ConflictException(f"名称「{name}」已被使用，请换一个")

    @staticmethod
    def _refresh_watcher() -> None:
        """库/根路径/监听目录变更后在后台重建实时监听（监听器未启动时为 no-op）。

        重建对大库（recursive 建 watch）/网络挂载可达数秒到数十秒，不能拖住
        保存请求——这里只调度任务立即返回，弹窗秒关；重建期间的新文件由
        定期对账兜底，不存在漏网。
        """

        async def _do() -> None:
            from movieclaw_api.services.library.ingest import get_ingest_watcher
            from movieclaw_api.services.library.watch import get_library_watcher

            try:
                watcher = get_library_watcher()
                if watcher is not None:
                    await watcher.refresh_watches()
                ingest_watcher = get_ingest_watcher()
                if ingest_watcher is not None:
                    await ingest_watcher.refresh_watches()
            except Exception:  # noqa: BLE001 -- 后台任务无人 await，异常必须就地落日志
                logger.exception("重建媒体库实时监听失败（定期对账仍会兜底发现新文件）")

        task = asyncio.create_task(_do())
        _refresh_tasks.add(task)
        task.add_done_callback(_refresh_tasks.discard)

    async def create(
        self,
        *,
        name: str,
        kind: MediaKind,
        root_paths: list[str],
        match_rules: list | None = None,
        auto_clear_missing: bool | None = None,
        realtime_watch: bool | None = None,
        scrape_overrides: dict | None = None,
    ) -> Library:
        """新增一个库。该类型尚无默认库时自动成为默认。"""
        from movieclaw_api.services.library.routing import validate_match_rules

        roots = self._validate(name=name, root_paths=root_paths)
        rules = validate_match_rules(match_rules)
        overrides = self._validate_overrides(scrape_overrides)
        await self._assert_roots_clear_of_import_watch(roots)
        await self._assert_roots_clear_of_other_libraries(roots)
        await self._assert_name_available(name)
        row = await self._repo.create(
            name=name.strip(),
            kind=kind.value,
            root_paths=roots,
            match_rules=rules,
            auto_clear_missing=bool(auto_clear_missing),
            # 实时监控默认开（与 Emby/Plex 一致）；不传按默认
            realtime_watch=True if realtime_watch is None else bool(realtime_watch),
            scrape_overrides=overrides,
        )
        self._refresh_watcher()
        return row

    async def update(
        self,
        library_id: int,
        *,
        name: str,
        root_paths: list[str],
        match_rules: list | None = None,
        auto_clear_missing: bool | None = None,
        realtime_watch: bool | None = None,
        scrape_overrides: dict | None = None,
    ) -> Library:
        """更新名称/根路径/收藏范围。kind 创建后不可改（订阅按类型挂库）。

        ``auto_clear_missing`` / ``realtime_watch`` 为 None 时保持原值
        （见 LibraryRepository.update）。
        """
        await self.get(library_id)
        from movieclaw_api.services.library.routing import validate_match_rules

        roots = self._validate(name=name, root_paths=root_paths)
        rules = validate_match_rules(match_rules)
        overrides = self._validate_overrides(scrape_overrides)
        await self._assert_roots_clear_of_import_watch(roots)
        await self._assert_roots_clear_of_other_libraries(roots, exclude_id=library_id)
        await self._assert_name_available(name, exclude_id=library_id)
        updated = await self._repo.update(
            library_id,
            name=name.strip(),
            root_paths=roots,
            match_rules=rules,
            auto_clear_missing=auto_clear_missing,
            realtime_watch=realtime_watch,
            scrape_overrides=overrides,
        )
        assert updated is not None  # get() 已确认存在
        # 开关切换也走同一次后台差量重建：关掉的库拆监听、打开的库建监听
        self._refresh_watcher()
        return updated

    async def reorder(self, ordered_ids: list[int]) -> list[Library]:
        """按给定 id 顺序重排全部库的展示顺序（决定媒体库首页卡片与
        「最近添加」分区的排列）。必须一次给全：漏库/多库/重复都拒绝——
        部分排序没有明确语义（漏掉的库排哪里说不清），宁可让前端重拉列表。"""
        existing = {row.id for row in await self._repo.list_all()}
        if len(ordered_ids) != len(set(ordered_ids)):
            raise BadRequestException("排序列表存在重复的库 id")
        if set(ordered_ids) != existing:
            raise BadRequestException(
                "排序列表必须包含且仅包含全部媒体库的 id（库列表可能已变化，请刷新后重试）"
            )
        await self._repo.reorder(ordered_ids)
        return await self._repo.list_all()

    async def set_default(self, library_id: int) -> Library:
        """设为该类型的默认库（订阅/手动下载不选库时用它）。"""
        ok = await self._repo.set_default(library_id)
        if not ok:
            raise NotFoundException(f"媒体库不存在：id={library_id}")
        return await self.get(library_id)

    async def delete(self, library_id: int) -> None:
        """删除库。挂在它上面的订阅回落到该类型默认库（外键 SET NULL）。"""
        row = await self.get(library_id)
        await self._repo.delete(library_id)
        self._refresh_watcher()
        logger.info("媒体库「%s」已删除，其订阅将回落到该类型的默认库", row.name)
