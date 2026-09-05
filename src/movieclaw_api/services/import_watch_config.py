"""监听导入规则的配置服务：CRUD 与校验（媒体库之上的独立功能）。

规则 = 源目录 → 目标 的搬运声明（策略：硬链接/复制），由
``library_ingest`` 引擎消费。目标三态（docs/design/library-routing.md 2.3、
docs/design/strm-workflow.md）：

- **指定库**（``library_id`` 非空）：内容固定进该库；
- **自动路由**（``library_id`` 与 ``target_path`` 均空 + ``kind`` 必填）：
  识别出作品后按各库的收藏范围声明选库。kind 仍须先验（识别链按
  movie/tv 分叉）；每 kind 至多一条 auto 规则——多条会让订阅投递的
  落点歧义；
- **自定义目录**（``target_path`` 非空 + ``kind`` 必填）：movie/tv 识别改名后落
  该目录、video 不识别不改名原样落该目录，
  该目录、不进入任何媒体库——"整理结果需外部流转再进库"场景的落点。
  每 kind 至多一条（投递查找链同 auto 理由）。

校验要点：

- ``library_id`` 与 ``target_path`` 互斥；
- 源目录不得与**任何**库的根路径前缀重叠（双头管理必乱；库侧改根路径
  时做反向校验，见 LibraryConfigService）；自定义目录同理，且不得与
  任何监听源目录重叠——整理输出落回监听区会被再次消费成环；
- 源目录全局唯一（数据库唯一索引兜底，这里给可读中文报错）；
- 策略选硬链接时做**同盘检测**（源目录与落点的 st_dev 比对）——
  把"跨文件系统无法硬链"从第一次搬运失败前置到保存配置时；auto 规则
  对该 kind **全部可能目标库**（声明收藏范围的库 + 默认库）逐一检测，
  列出不同盘的库名；自定义目录对 ``target_path`` 检测；任一目录尚不
  存在（挂载未就绪）时跳过检测，搬运失败的中文引导兜底。
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.services.library.profile import KIND_LABELS
from movieclaw_db.models import ImportWatch, Library
from movieclaw_db.models.base import utcnow

logger = logging.getLogger("movieclaw_api.import_watch_config")

# 后台监听重建任务的存活引用：create_task 若不持有引用可能被 GC 中途取消
_refresh_tasks: set[asyncio.Task] = set()

STRATEGIES = ("hardlink", "copy")
# 自动路由 / 自定义目录可声明的形态：movie/tv 走识别链（自动路由按收藏范围选库、
# 自定义目录识别改名后落该目录）；video 不识别不改名——自动路由落该形态默认库，
# 自定义目录原样落该目录
_KINDS = ("movie", "tv", "video")
_KIND_LABELS = KIND_LABELS


def _routable_cause(lib: Library) -> str:
    """库进入自动路由候选的成因（同盘检测报错文案用）：默认库 / 声明了收藏范围。"""
    causes = []
    if lib.is_default:
        causes.append(f"{_KIND_LABELS.get(lib.kind, lib.kind)}默认库")
    if lib.match_rules:
        causes.append("声明了收藏范围")
    return "、".join(causes)


def rule_target_label(rule: ImportWatch, library_name: str | None) -> str:
    """规则目标的展示名：库名 /「自动路由（电影/剧集）」/「自定义目录 …」
    （日志与接口共用）。"""
    if rule.library_id is not None:
        return f"「{library_name or '?'}」"
    if rule.target_path:
        kind_label = _KIND_LABELS.get(rule.kind or "", rule.kind or "?")
        return f"自定义目录（{kind_label} · {rule.target_path}）"
    return f"自动路由（{_KIND_LABELS.get(rule.kind or '', rule.kind or '?')}）"


class ImportWatchConfigService:
    """监听导入规则的业务服务。绑定一个数据库会话。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> list[ImportWatch]:
        result = await self._session.execute(select(ImportWatch).order_by(ImportWatch.id))
        return list(result.scalars().all())

    async def get(self, rule_id: int) -> ImportWatch:
        row = await self._session.get(ImportWatch, rule_id)
        if row is None:
            raise NotFoundException(f"监听导入规则不存在：id={rule_id}")
        return row

    async def create(
        self,
        *,
        source_path: str,
        strategy: str,
        library_id: int | None,
        kind: str | None = None,
        target_path: str | None = None,
        process_existing: bool = True,
    ) -> ImportWatch:
        source, kind, target = await self._validate(
            source_path=source_path,
            strategy=strategy,
            library_id=library_id,
            kind=kind,
            target_path=target_path,
        )
        row = ImportWatch(
            source_path=source,
            strategy=strategy,
            library_id=library_id,
            kind=kind,
            target_path=target,
            process_existing=process_existing,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        _refresh_watcher()
        library = await self._session.get(Library, library_id) if library_id else None
        logger.info(
            "已创建监听导入规则：%s → %s（%s）",
            source,
            rule_target_label(row, library.name if library else None),
            "硬链接" if strategy == "hardlink" else "复制",
        )
        return row

    async def update(
        self,
        rule_id: int,
        *,
        source_path: str,
        strategy: str,
        library_id: int | None,
        kind: str | None = None,
        target_path: str | None = None,
        process_existing: bool = True,
    ) -> ImportWatch:
        row = await self.get(rule_id)
        source, kind, target = await self._validate(
            source_path=source_path,
            strategy=strategy,
            library_id=library_id,
            kind=kind,
            target_path=target_path,
            exclude_id=rule_id,
        )
        # 换了源目录或改了存量选项 → 复位基线：下轮巡检对（新）目录重新
        # 盖章。「跳过存量」的"存量"始终指**该设置生效那一刻**目录里已有的
        if source != row.source_path or process_existing != row.process_existing:
            row.baseline_done = False
        row.source_path = source
        row.strategy = strategy
        row.library_id = library_id
        row.kind = kind
        row.target_path = target
        row.process_existing = process_existing
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        _refresh_watcher()
        return row

    async def delete(self, rule_id: int) -> None:
        row = await self.get(rule_id)
        await self._session.delete(row)
        await self._session.commit()
        _refresh_watcher()
        logger.info("已删除监听导入规则：%s", row.source_path)

    # -- 校验 --------------------------------------------------------------

    async def _validate(
        self,
        *,
        source_path: str,
        strategy: str,
        library_id: int | None,
        kind: str | None,
        target_path: str | None,
        exclude_id: int | None = None,
    ) -> tuple[str, str | None, str | None]:
        source = source_path.strip().rstrip("/")
        if not source or not source.startswith("/"):
            raise BadRequestException("源目录必须是绝对路径")
        if strategy not in STRATEGIES:
            raise BadRequestException("搬运策略必须是硬链接（hardlink）或复制（copy）")
        if library_id is not None and target_path:
            raise BadRequestException(
                "目标只能选一种：指定媒体库、自动路由或自定义目录"
                "（library_id 与 target_path 不能同时提供）"
            )

        # 目标解析：指定库（kind 由库推导、存 NULL）/ 自定义目录（kind 必填）
        # / auto（kind 必填）
        target: str | None = None
        hardlink_targets: list[Library] = []
        if target_path:
            target = target_path.strip().rstrip("/")
            if not target or not target.startswith("/"):
                raise BadRequestException("自定义目录必须是绝对路径")
            if kind not in _KINDS:
                raise BadRequestException("自定义目录规则必须指定媒体类型（movie / tv / video）")
            # 每 kind 至多一条：投递查找链（resolve_dispatch_rule）的落点不能歧义
            existing_target = (
                await self._session.execute(
                    select(ImportWatch).where(
                        ImportWatch.target_path.is_not(None),  # type: ignore[union-attr]
                        ImportWatch.kind == kind,
                    )
                )
            ).scalar_one_or_none()
            if existing_target is not None and existing_target.id != exclude_id:
                raise BadRequestException(
                    f"{_KIND_LABELS[kind]}已有自定义目录规则（{existing_target.source_path}）——"
                    "每个类型至多一条，请编辑既有规则"
                )
        elif library_id is not None:
            library = await self._session.get(Library, library_id)
            if library is None:
                raise NotFoundException(f"目标媒体库不存在：id={library_id}")
            if not library.primary_root:
                raise BadRequestException(
                    f"媒体库「{library.name}」没有配置根路径，无法作为导入目标"
                )
            kind = None
            hardlink_targets = [library]
        else:
            if kind not in _KINDS:
                raise BadRequestException("自动路由规则必须指定媒体类型（movie / tv / video）")
            # 每 kind 至多一条 auto 规则：多条会让订阅投递的落点歧义。
            # 自定义目录规则的 library_id 同为 NULL，须按 target_path 区分
            existing_auto = (
                await self._session.execute(
                    select(ImportWatch).where(
                        ImportWatch.library_id.is_(None),  # type: ignore[union-attr]
                        ImportWatch.target_path.is_(None),  # type: ignore[union-attr]
                        ImportWatch.kind == kind,
                    )
                )
            ).scalar_one_or_none()
            if existing_auto is not None and existing_auto.id != exclude_id:
                raise BadRequestException(
                    f"{_KIND_LABELS[kind]}已有自动路由规则（{existing_auto.source_path}）——"
                    "每个类型至多一条，请编辑既有规则"
                )
            candidates = list(
                (await self._session.execute(select(Library).where(Library.kind == kind)))
                .scalars()
                .all()
            )
            # 硬链同盘检测只覆盖**路由可达**的库（声明收藏范围的库 + 默认库，
            # 与 route() 及设计文档 2.3 同一口径）：无声明且非默认的库（典型如
            # 跨盘的 strm 归档库）路由永远不会选中，不该有资格否决规则创建
            # （issue #79）。已知漏网：订阅可手选任意同类型库定格、事后跨盘——
            # 该场景由订阅链路体检（transfer_disk 按定格库检测）与搬运时的
            # EXDEV 中文报错兜底，勿再扩回全量检测重新引入误伤
            routable = [
                lib
                for lib in candidates
                if (lib.match_rules or lib.is_default) and lib.primary_root
            ]
            if not routable:
                raise BadRequestException(
                    f"当前没有任何可路由的{_KIND_LABELS[kind]}库（需要有根路径的默认库"
                    "或声明了收藏范围的库），请先到「媒体库」创建"
                )
            hardlink_targets = routable

        def _overlaps(a: str, b: str) -> bool:
            return a == b or a.startswith(b + "/") or b.startswith(a + "/")

        # 与所有库的根路径不重叠（不只是目标库：落在任何库根下都会被扫描双头
        # 管理）；自定义目录同理——整理输出落进库根等于绕过了"外部流转"语义
        libraries = list((await self._session.execute(select(Library))).scalars().all())
        for lib in libraries:
            for root in lib.root_paths:
                r = root.rstrip("/")
                if _overlaps(source, r):
                    raise BadRequestException(
                        f"源目录与媒体库「{lib.name}」的根路径重叠：{source} ↔ {root}"
                    )
                if target is not None and _overlaps(target, r):
                    raise BadRequestException(
                        f"自定义目录与媒体库「{lib.name}」的根路径重叠：{target} ↔ {root}"
                    )

        # 自定义目录与监听源互不重叠（含本条规则自身）：整理输出落回监听区
        # 会被再次消费成环
        rules = list((await self._session.execute(select(ImportWatch))).scalars().all())
        others = [r for r in rules if r.id != exclude_id]
        if target is not None:
            for candidate in [source, *(r.source_path for r in others)]:
                if _overlaps(target, candidate):
                    raise BadRequestException(f"自定义目录与监听源目录重叠：{target} ↔ {candidate}")
        for r in others:
            if r.target_path and _overlaps(source, r.target_path):
                raise BadRequestException(
                    f"源目录与另一条规则的自定义目录重叠：{source} ↔ {r.target_path}"
                )

        # 源目录全局唯一
        existing = next((r for r in others if r.source_path == source), None)
        if existing is not None:
            raise BadRequestException(f"该源目录已有监听导入规则：{source}")

        # 硬链接的同盘检测：跨盘当场提示，不留到第一次搬运失败。auto 规则
        # 逐库检测——路由到哪个库都可能，任何一个不同盘将来都会搬运失败；
        # 自定义目录规则对 target 检测
        if strategy == "hardlink":
            offenders: list[Library] = []
            try:
                source_dev = os.stat(source).st_dev
            except OSError:
                source_dev = None  # 目录未就绪（挂载中）：跳过检测，搬运失败的中文引导兜底
            if source_dev is not None:
                for lib in hardlink_targets:
                    try:
                        root_dev = os.stat(lib.primary_root).st_dev  # type: ignore[arg-type]
                    except OSError:
                        continue
                    if root_dev != source_dev:
                        offenders.append(lib)
                if target is not None:
                    try:
                        if os.stat(target).st_dev != source_dev:
                            raise BadRequestException(
                                f"源目录与自定义目录不在同一文件系统（{source} ↔ {target}），"
                                "硬链接无法工作；请把策略改为「复制」，或把它们放到同一存储卷"
                            )
                    except OSError:
                        pass  # 目录未就绪：同上跳过
            if offenders and library_id is not None:
                names = "」「".join(lib.name for lib in offenders)
                raise BadRequestException(
                    f"源目录与媒体库「{names}」的主根不在同一文件系统，"
                    "硬链接无法工作；请把策略改为「复制」，或把它们放到同一存储卷"
                )
            if offenders:
                # auto 规则：报错讲明每个库**为什么在候选里**——用户仍会撞上的
                # 场景是"跨盘归档库恰好是默认库/声明了收藏范围"，只有讲清成因
                # 他才知道出路是调整该库的设置，而不只是换复制策略
                labeled = "".join(f"「{lib.name}」（{_routable_cause(lib)}）" for lib in offenders)
                raise BadRequestException(
                    f"源目录与媒体库{labeled}的主根不在同一文件系统——"
                    "自动路由可能选中这些库，硬链接会失败；请把策略改为「复制」、"
                    "调整存储布局，或调整这些库的收藏范围/默认库设置"
                )
        return source, kind, target


async def resolve_dispatch_rule(
    session: AsyncSession, library_id: int | None, *, kind: str | None = None
) -> ImportWatch | None:
    """投递会命中的监听导入规则：
    目标库的专属规则 → 同 kind 的自动路由规则 → 同 kind 的自定义目录规则 → None。

    订阅/手动下载止于投递——把种子投到会被监听导入接管的目录（分离布局），
    或不指定目录退下载器默认。auto 规则兜底让"一个混合下载目录服务所有库"
    成立：完成后按 info_hash 认领回订阅身份，目标库=订阅定格的库，与投递
    预检的结论必然一致（docs/design/library-routing.md 2.3）。自定义目录
    规则再兜一格：命中它意味着完成后整理到规则声明的目录、不直接进库
    （docs/design/strm-workflow.md——外部流转后由库根扫描收尾）。
    多条规则指向同一库时取最早创建的一条。
    投递方（dispatch/预检）只关心源目录，链路体检还要读策略——故返回规则本体。
    """
    if library_id is not None:
        rule = (
            (
                await session.execute(
                    select(ImportWatch)
                    .where(ImportWatch.library_id == library_id)
                    .order_by(ImportWatch.id)
                )
            )
            .scalars()
            .first()
        )
        if rule is not None:
            return rule
    if kind is not None:
        auto_rule = (
            (
                await session.execute(
                    select(ImportWatch)
                    .where(
                        ImportWatch.library_id.is_(None),  # type: ignore[union-attr]
                        ImportWatch.target_path.is_(None),  # type: ignore[union-attr]
                        ImportWatch.kind == kind,
                    )
                    .order_by(ImportWatch.id)
                )
            )
            .scalars()
            .first()
        )
        if auto_rule is not None:
            return auto_rule
        return (
            (
                await session.execute(
                    select(ImportWatch)
                    .where(
                        ImportWatch.target_path.is_not(None),  # type: ignore[union-attr]
                        ImportWatch.kind == kind,
                    )
                    .order_by(ImportWatch.id)
                )
            )
            .scalars()
            .first()
        )
    return None


def _refresh_watcher() -> None:
    """规则变更后在后台重建监听（监听器未启动时为 no-op）。

    重建的 recursive 建 watch 在大目录/网络挂载上可达数秒到数十秒，不能
    拖住保存请求——只调度任务立即返回；重建期间完成的下载由兜底巡检接住。
    """

    async def _do() -> None:
        from movieclaw_api.services.library.ingest import get_ingest_watcher

        try:
            watcher = get_ingest_watcher()
            if watcher is not None:
                await watcher.refresh_watches()
        except Exception:  # noqa: BLE001 -- 后台任务无人 await，异常必须就地落日志
            logger.exception("重建监听导入的目录监听失败（兜底巡检仍会发现完成的下载）")

    task = asyncio.create_task(_do())
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)
