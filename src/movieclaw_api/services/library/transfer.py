"""条目转移：把一部作品连同**整个磁盘目录**从一个库搬到另一个库。

为什么需要它：入库时的选库要么靠收藏范围路由（library_routing）、要么靠
用户在订阅弹窗里手选，两条路都可能选错——最典型的是韩剧被路由进了
「大陆华语剧」（元数据里 origin_countries 缺失或不准）。此前对这种错分
**没有任何补救手段**：改库的收藏范围只影响后续入库，重新识别只改身份锚
不动归属，唯一能"挪窝"的办法是手工移动目录再两边重扫——对普通用户
不可接受。本模块补上这条通道。

语义定线（三条，与删除/整理同一套克制哲学）：

- **搬的是目录不是台账**：转移 = 磁盘上的条目目录整体搬到目标库主根下 +
  台账行随迁（library_id 与 file_path 一起改）。只改台账不动文件会立刻
  被下一次扫描打回原形（文件还在旧根下，旧库重新入账、新库标缺失）；
- **目录名原样保留**：搬过去仍叫 ``风筝 (2017)``，不趁机规范化命名——
  规范化是「整理文件名」的职责，一次操作只做一件事，用户才说得清
  "刚才那一下到底改了什么"；
- **绝不覆盖、绝不合并**：目标已存在同名目录即判为冲突并中止该单元
  （多半是目标库里已经有这部片子），宁可让用户自己决断。

跨设备（源根与目标根不在同一块盘）是本模块唯一的重活：``os.rename`` 会以
EXDEV 失败，此时退化为"完整复制 → 复制成功才删源"。两个后果必须在预览
里说清楚：耗时按体积走（几十 GB 以分钟计），且**与做种目录的硬链接关系
会断开**（复制产生新 inode，下载器仍在对旧文件做种，磁盘占用翻倍）。

并发：转移期间在**源库与目标库两侧**都占住库级任务位（TaskState），扫描、
整理、重识别、改库配置一律挡下——搬运中途被扫描介入会把搬走的旧路径标
missing、把新路径当新文件重走识别链，人工认领会丢。
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlmodel import select

from movieclaw_api.exceptions import BadRequestException, ConflictException
from movieclaw_api.services import jobs
from movieclaw_api.services.library.fsops import rename_no_replace
from movieclaw_api.services.library.layout import entry_dir_of

# 复用整理器的"只清理自己搬空的目录"实现（非空即停、绝不删文件）——同一
# 语义两处各写一份迟早分叉，而这段克制正是搬运类操作的安全底线
from movieclaw_api.services.library.organize import _prune_emptied_dirs
from movieclaw_api.services.library.sidecar import find_sidecars
from movieclaw_api.services.task_state import TaskState
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileState, Library, LibraryFile, MediaItem, Subscription, utcnow
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_db.repositories.library_repo import LibraryRepository

logger = logging.getLogger("movieclaw_api.library_transfer")

# 跨盘复制按块落入隐藏临时路径。每块独立交还事件循环，使取消和应用更新
# 最多只损失当前块；下次执行按临时文件现有长度续传，不重新复制几十 GB。
_COPY_CHUNK_BYTES = 8 * 1024 * 1024


@dataclass
class TransferState:
    """进行中转移的实时状态（前端进度条 + 冲突提示的数据源）。"""

    source_library_id: int
    target_library_id: int
    media_item_id: int
    title: str
    processed: int = 0
    total: int = 0


# 每库单飞：源库与目标库**两个键**都登记同一个 state 对象，任何一侧的
# 扫描/整理/编辑都能据此挡下（值同一个对象，进度更新一处生效两处可见）
_transfer_tasks: TaskState[TransferState] = TaskState()
# 后台任务的存活引用：create_task 不持引用可能被 GC 中途取消
_running: set[asyncio.Task] = set()


def is_transferring(library_id: int) -> bool:
    """该库是否正被某次条目转移占用（作为源或目标皆算）。"""
    return _transfer_tasks.running(library_id)


def transfer_state(library_id: int) -> TransferState | None:
    """进行中转移的实时状态；没在转移返回 None。"""
    return _transfer_tasks.state_of(library_id)


def last_transfer(library_id: int) -> tuple | None:
    """最近一次转移的 (完成时间, TransferSummary)；从未转移过则为 None。"""
    return _transfer_tasks.last(library_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 计划：纯计算（只读磁盘与台账），预览接口与执行共用
# ---------------------------------------------------------------------------


@dataclass
class TransferMove:
    """一个搬运单元：整个条目目录，或（目录里混着别的条目时）单个文件。"""

    source_path: str
    target_path: str
    is_dir: bool
    size_bytes: int  # 本单元涉及的台账体积（展示用）
    file_ids: list[int] = field(default_factory=list)  # 随迁的台账行
    # 单文件模式下跟随主文件搬的字幕/NFO 等（(源, 目标) 对）
    sidecars: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class TransferSkip:
    """不参与转移的路径与中文原因（预览逐条展示，用户心里有数）。"""

    file_path: str
    reason: str


@dataclass
class TransferPlan:
    """一次转移的完整计划。``blocked`` 非空时执行接口直接拒绝。"""

    source_library_id: int
    target_library_id: int
    media_item_id: int
    title: str
    moves: list[TransferMove] = field(default_factory=list)
    skips: list[TransferSkip] = field(default_factory=list)
    # 逻辑随迁的缺失台账行（磁盘上没有实体，只改 library_id 与路径投影）
    missing_file_ids: list[int] = field(default_factory=list)
    total_bytes: int = 0
    # 目标主根与源目录不在同一块盘 → 退化为完整复制（耗时 + 断硬链）
    cross_device: bool = False
    # 阻断性问题（目标已存在同名目录等）：有值就不给执行
    blocked: list[str] = field(default_factory=list)


async def build_transfer_plan(
    session,
    source: Library,
    target: Library,
    item: MediaItem,
    files: list[LibraryFile],
) -> TransferPlan:
    """计算转移计划。只读磁盘与台账，不做任何写入。

    调用前的合法性校验（同类型、非同库、目标有主根）由 ``assert_transferable``
    统一负责——预览与执行都要走那一道，不在这里重复。
    """
    assert source.id is not None and target.id is not None and item.id is not None
    # 源库里**其他条目**占用的路径：条目目录里混着别人时不能整目录搬
    foreign = list(
        (
            await session.execute(
                select(LibraryFile.file_path).where(
                    LibraryFile.library_id == source.id,
                    LibraryFile.media_item_id != item.id,
                )
            )
        )
        .scalars()
        .all()
    )
    roots = [Path(p) for p in source.root_paths]
    target_root = Path(target.primary_root or "")
    # 磁盘检查（exists/stat/附属文件枚举）放线程池：网络挂载上一次 stat 也要毫秒级
    return await asyncio.to_thread(
        _build_plan_sync,
        source.id,
        target.id,
        item.id,
        item.title,
        roots,
        target_root,
        files,
        [Path(p) for p in foreign],
    )


def _build_plan_sync(
    source_library_id: int,
    target_library_id: int,
    media_item_id: int,
    title: str,
    roots: list[Path],
    target_root: Path,
    files: list[LibraryFile],
    foreign_paths: list[Path],
) -> TransferPlan:
    plan = TransferPlan(
        source_library_id=source_library_id,
        target_library_id=target_library_id,
        media_item_id=media_item_id,
        title=title,
    )
    if not target_root.is_dir():
        plan.blocked.append(f"目标库的主根路径不可访问：{target_root}（盘未挂载？）")
        return plan

    # 条目目录 → 该目录下本条目的台账行；保序（第一个目录决定跨设备判定）
    by_entry: dict[Path, list[LibraryFile]] = {}
    loose: list[LibraryFile] = []  # 找不到条目目录、直接躺在库根下的裸文件

    for row in files:
        path = Path(row.file_path)
        if row.state == FileState.MISSING or (
            row.state == FileState.TRASHED and row.trash_original_path is not None
        ):
            # 缺失行没有磁盘实体；移入回收站形态的待回收行实体在源库
            # .movieclaw-trash 里、不跟条目目录走——都只做逻辑随迁。
            # **原地待回收行（做种保护形态）不在此列**：它的实体就在条目
            # 目录里，必须随目录物理搬运并改写路径，否则搬运后行路径陈旧、
            # 清理任务删行收敛，新库里的无台账文件会被扫描重新收编——
            # 证伪版本借转移复活（relocate 的 revive 守卫保证状态保持待回收）
            assert row.id is not None
            plan.missing_file_ids.append(row.id)
            continue
        entry = entry_dir_of(roots, path)
        if entry is None and row.container in ("bluray", "dvd"):
            entry = path  # 直接躺在根下的原盘目录：目录本身就是条目
        if entry is not None and _inside_roots(entry, roots):
            by_entry.setdefault(entry, []).append(row)
        elif _inside_roots(path, roots):
            loose.append(row)
        else:
            plan.skips.append(
                TransferSkip(row.file_path, "不在源库的根路径之内（根路径可能已变更），已跳过")
            )

    taken: set[str] = set()  # 本次计划已占用的目标路径（同名条目目录撞车检测）

    for entry, rows in by_entry.items():
        dst = target_root / entry.name
        if str(dst) in taken or dst.exists():
            plan.blocked.append(
                f"目标库里已存在同名目录「{dst}」，为避免覆盖/合并已中止；"
                "请先处理目标库里的同名内容，或改用「重新识别」修正身份"
            )
            continue
        mixed = [p for p in foreign_paths if p == entry or entry in p.parents]
        if mixed:
            # 目录里混着其他条目：退化为逐文件搬（保留相对结构，Season 层跟着走）
            for row in rows:
                src = Path(row.file_path)
                rel = src.relative_to(entry)
                _add_file_move(plan, row, src, dst / rel, taken)
            plan.skips.append(
                TransferSkip(
                    str(entry),
                    f"目录里还有其他条目的 {len(mixed)} 个文件，未整目录搬运——"
                    "只搬本片的文件及其字幕/NFO",
                )
            )
            continue
        size = sum(r.size_bytes for r in rows)
        taken.add(str(dst))
        plan.moves.append(
            TransferMove(
                source_path=str(entry),
                target_path=str(dst),
                is_dir=True,
                size_bytes=size,
                file_ids=[r.id for r in rows if r.id is not None],
            )
        )

    for row in loose:
        src = Path(row.file_path)
        _add_file_move(plan, row, src, target_root / src.name, taken)

    plan.total_bytes = sum(m.size_bytes for m in plan.moves)
    if plan.moves:
        plan.cross_device = _cross_device(Path(plan.moves[0].source_path), target_root)
    return plan


def _add_file_move(
    plan: TransferPlan, row: LibraryFile, src: Path, dst: Path, taken: set[str]
) -> None:
    """登记一个单文件搬运单元（含字幕/NFO 等附属文件）。"""
    if str(dst) in taken or dst.exists():
        plan.skips.append(TransferSkip(str(src), f"目标路径已存在同名文件，跳过以免覆盖：{dst}"))
        return
    taken.add(str(dst))
    assert row.id is not None
    plan.moves.append(
        TransferMove(
            source_path=str(src),
            target_path=str(dst),
            is_dir=src.is_dir(),
            size_bytes=row.size_bytes,
            file_ids=[row.id],
            sidecars=_find_sidecars(src, dst),
        )
    )


def _find_sidecars(src: Path, dst: Path) -> list[tuple[str, str]]:
    """主文件的附属文件，判定口径见 ``library.sidecar``（整理/转移/回收共用）。"""
    return [(str(entry), str(dst.parent / (dst.stem + tail))) for entry, tail in find_sidecars(src)]


def _inside_roots(path: Path, roots: list[Path]) -> bool:
    """路径必须严格位于某个库根之内（不等于根本身）——搬运的硬边界。"""
    return any(root in path.parents for root in roots)


def _cross_device(source: Path, target_root: Path) -> bool:
    """源与目标是否分处两块盘（决定 rename 还是完整复制）。探测失败保守判 True。"""
    try:
        return os.stat(source).st_dev != os.stat(target_root).st_dev
    except OSError:
        return True


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


@dataclass
class TransferSummary:
    """一次转移的结论（日志与接口响应共用）。"""

    source_library_id: int
    target_library_id: int
    media_item_id: int
    title: str = ""
    target_library_name: str = ""
    moved_paths: list[str] = field(default_factory=list)  # 实际搬到的目标路径
    files_relocated: int = 0  # 随迁的台账行数
    bytes_moved: int = 0
    removed_dirs: int = 0  # 搬空后清理掉的源目录数
    subscription_moved: bool = False  # 该片的订阅是否一并改挂目标库
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def assert_transferable(source: Library, target: Library) -> None:
    """转移前的合法性校验（预览与执行共用；不合法抛中文报错）。"""
    if source.id == target.id:
        raise BadRequestException("目标库与当前库相同，无需转移")
    if source.kind != target.kind:
        raise BadRequestException(
            "只能转移到同类型的媒体库（电影 ↔ 电影、剧集 ↔ 剧集）；"
            "如果是类型判错，请用「重新识别」或在待识别清单里人工认领"
        )
    if not target.primary_root:
        raise BadRequestException(f"目标库「{target.name}」没有配置根路径，无法接收转移")
    for library in (source, target):
        assert library.id is not None
        _assert_idle(library)


def _assert_idle(library: Library) -> None:
    """库必须空闲：扫描/重识别/整理/另一次转移在跑时一律不给转。"""
    from movieclaw_api.services.library.organize import is_organizing
    from movieclaw_api.services.library.scan import PHASE_LABELS, busy_phase

    assert library.id is not None
    phase = busy_phase(library.id)
    if phase is not None:
        raise ConflictException(
            f"「{library.name}」{PHASE_LABELS[phase]}，请等当前任务完成后再转移"
        )
    if is_organizing(library.id):
        raise ConflictException(f"「{library.name}」正在整理文件名，请等整理完成后再转移")
    state = _transfer_tasks.state_of(library.id)
    if state is not None:
        raise ConflictException(f"「{library.name}」正在转移「{state.title}」，请等待完成")


def start_transfer(plan: TransferPlan) -> TransferState:
    """占住两侧库级任务位并在后台执行转移；同步返回初始状态。

    锁在**返回响应前**就立起来（而不是等后台任务跑起来才立），前端紧接着
    的第一次进度轮询必然看得到"正在转移"——否则会闪一下"没有任务在跑"，
    看起来像点了没反应。
    """
    state = TransferState(
        source_library_id=plan.source_library_id,
        target_library_id=plan.target_library_id,
        media_item_id=plan.media_item_id,
        title=plan.title,
        total=len(plan.moves),
    )
    _transfer_tasks.try_start(plan.source_library_id, state)
    _transfer_tasks.try_start(plan.target_library_id, state)

    task = asyncio.create_task(_run(plan, state))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return state


async def _run(plan: TransferPlan, state: TransferState) -> None:
    summary = TransferSummary(
        source_library_id=plan.source_library_id,
        target_library_id=plan.target_library_id,
        media_item_id=plan.media_item_id,
        title=plan.title,
    )
    try:
        await _transfer(plan, state, summary)
    except Exception:  # noqa: BLE001 -- 后台任务无人 await，异常必须就地落日志
        logger.exception(
            "条目 #%s 从库 #%s 转移到库 #%s 时发生未知错误",
            plan.media_item_id,
            plan.source_library_id,
            plan.target_library_id,
        )
        summary.errors.append("转移中断：发生未知错误（详见后端日志）")
    finally:
        await _refresh_stats_after_transfer(plan, summary)
        finished = (utcnow(), summary)
        _transfer_tasks.finish(plan.source_library_id, result=finished)
        _transfer_tasks.finish(plan.target_library_id, result=finished)


async def _transfer(
    plan: TransferPlan,
    state: TransferState,
    summary: TransferSummary,
    *,
    context: jobs.JobContext | None = None,
    checkpoint_id: str | None = None,
) -> None:
    db = get_database()
    async with db.session() as session:
        target = await session.get(Library, plan.target_library_id)
        if target is None:
            summary.errors.append("目标媒体库不存在（可能已被删除）")
            return
        summary.target_library_name = target.name
        repo = LibraryFileRepository(session)
        dirty_parents: set[Path] = set()

        for done, move in enumerate(plan.moves, start=1):
            if context is not None:
                await context.raise_if_cancelled()
            src, dst = Path(move.source_path), Path(move.target_path)
            try:
                if context is not None and checkpoint_id is not None:
                    await _move_resumable(context, src, dst, checkpoint_id)
                else:
                    await asyncio.to_thread(_move, src, dst, plan.cross_device)
            except _MoveError as exc:
                summary.errors.append(str(exc))
                state.processed = done
                continue
            if move.target_path not in summary.moved_paths:
                summary.moved_paths.append(move.target_path)
            summary.bytes_moved += move.size_bytes
            dirty_parents.add(src.parent)

            for sidecar_src, sidecar_dst in move.sidecars:
                try:
                    if context is not None and checkpoint_id is not None:
                        await _move_resumable(
                            context, Path(sidecar_src), Path(sidecar_dst), checkpoint_id
                        )
                    else:
                        await asyncio.to_thread(_move, Path(sidecar_src), Path(sidecar_dst), False)
                except _MoveError as exc:
                    summary.errors.append(f"附属文件搬运失败：{exc}")

            # 搬运成功立即随迁台账：中途失败不会留下"账在新库、文件还在旧库"
            for file_id in move.file_ids:
                row = await session.get(LibraryFile, file_id)
                if row is None:
                    continue
                row_path = Path(row.file_path)
                if (
                    move.is_dir
                    and row.library_id == plan.target_library_id
                    and (row_path == dst or dst in row_path.parents)
                ):
                    new_path = row.file_path
                else:
                    new_path = str(dst / row_path.relative_to(src)) if move.is_dir else str(dst)
                if row.library_id == plan.target_library_id and row.file_path == new_path:
                    # 上次执行已提交该行；重启后不重复计数或改写。
                    summary.files_relocated += 1
                    continue
                await repo.relocate_to_library(
                    file_id, library_id=plan.target_library_id, file_path=new_path
                )
                summary.files_relocated += 1
            state.processed = done
            # 按秒节流（最后一条必写）：逐路径写进度会触发“事件 → SSE →
            # 客户端拉列表”的放大链；实时读数由内存态 _transfer_tasks 提供
            if context is not None and (done == len(plan.moves) or context.progress_due()):
                await context.update_progress(
                    mode="determinate",
                    phase="transferring",
                    message=f"已转移 {done} / {len(plan.moves)} 个路径",
                    current=done,
                    total=len(plan.moves),
                    percent=(done / len(plan.moves) * 100) if plan.moves else 100.0,
                    details={
                        "media_item_id": plan.media_item_id,
                        "source_library_id": plan.source_library_id,
                        "target_library_id": plan.target_library_id,
                        "bytes_moved": summary.bytes_moved,
                        "errors": len(summary.errors),
                    },
                )

        # 缺失行没有磁盘实体，只改归属：留在旧库会变成"旧库里一个永远补不回来
        # 的缺失条目"，跟着条目走才对得上用户认知
        for file_id in plan.missing_file_ids:
            if context is not None:
                await context.raise_if_cancelled()
            row = await session.get(LibraryFile, file_id)
            if row is None:
                continue
            if row.library_id == plan.target_library_id:
                summary.files_relocated += 1
                continue
            await repo.relocate_to_library(
                file_id,
                library_id=plan.target_library_id,
                file_path=row.file_path,
                keep_missing=True,
            )
            summary.files_relocated += 1

        # 只清理被本次转移搬空的源目录（及其变空的祖先）：非空即停、绝不删文件
        source = await session.get(Library, plan.source_library_id)
        if source is not None and dirty_parents:
            summary.removed_dirs = await asyncio.to_thread(
                _prune_emptied_dirs, dirty_parents, [r.rstrip("/") for r in source.root_paths]
            )

        # 订阅一并改挂目标库：不然下一集下载完又按旧库投递，用户刚搬完就被打回原形
        if summary.files_relocated:
            subscription = (
                await session.execute(
                    select(Subscription).where(Subscription.media_item_id == plan.media_item_id)
                )
            ).scalar_one_or_none()
            if subscription is not None and subscription.library_id != plan.target_library_id:
                subscription.library_id = plan.target_library_id
                subscription.updated_at = utcnow()
                await session.commit()
                summary.subscription_moved = True
    from movieclaw_api.services.media_server_notify import notify_media_server_refresh

    try:
        await notify_media_server_refresh()
    except Exception:  # noqa: BLE001 -- 下游刷新失败不该影响转移结论
        logger.warning("转移完成后通知媒体服务器刷新失败（不影响本地库存）", exc_info=True)

    logger.info(
        "条目「%s」已从库 #%s 转移到「%s」：搬运 %d 个路径、随迁 %d 条台账（约 %.1f GB），"
        "清理空目录 %d，问题 %d",
        summary.title,
        summary.source_library_id,
        summary.target_library_name,
        len(summary.moved_paths),
        summary.files_relocated,
        summary.bytes_moved / 1024**3,
        summary.removed_dirs,
        len(summary.errors),
    )


async def _refresh_stats_after_transfer(plan: TransferPlan, summary: TransferSummary) -> None:
    """转移收尾时一次刷新源/目标库；部分失败已提交的随迁行同样要纳入。"""
    if not summary.files_relocated:
        return
    try:
        db = get_database()
        async with db.session() as session:
            await LibraryRepository(session).refresh_stats(
                [plan.source_library_id, plan.target_library_id]
            )
    except Exception:  # noqa: BLE001 -- 统计失败不回滚已完成的磁盘搬运
        logger.exception(
            "条目 #%s 转移后的媒体库统计刷新失败", plan.media_item_id
        )
        summary.errors.append("媒体库统计刷新失败，将在下次扫描时重试")


async def enqueue_transfer_job(
    session,
    plan: TransferPlan,
    *,
    target_library_name: str,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "web",
) -> jobs.CreateJobResult:
    """保存确认后的转移计划；checkpoint_id 在人工重试时保持不变以复用副本。"""
    checkpoint_id = uuid.uuid4().hex
    return await jobs.create_job(
        session,
        job_type="library.transfer",
        subject=f"{plan.title} → {target_library_name}",
        input_data={
            "plan": asdict(plan),
            "target_library_name": target_library_name,
            "checkpoint_id": checkpoint_id,
        },
        resources=[
            jobs.ResourceRef("library", plan.source_library_id),
            jobs.ResourceRef("library", plan.target_library_id),
            jobs.ResourceRef("media_item", plan.media_item_id),
        ],
        dedupe_key=f"library.transfer:{plan.media_item_id}",
        conflict_policy="return_existing",
        handler_revision="library.transfer.v1",
        max_attempts=3,
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress={
            **jobs.default_progress("等待转移媒体文件"),
            "total": len(plan.moves),
            "details": {
                "source_library_id": plan.source_library_id,
                "target_library_id": plan.target_library_id,
                "media_item_id": plan.media_item_id,
                "cross_device": plan.cross_device,
                "total_bytes": plan.total_bytes,
            },
        },
    )


def _plan_from_job(value: object) -> TransferPlan:
    """恢复持久化转移计划；不在重启后重新猜测已经变化的源/目标路径。"""
    if not isinstance(value, dict):
        raise jobs.JobFailed("转移任务定义损坏，请重新预览后发起", code="JOB_INPUT_INVALID")
    return TransferPlan(
        source_library_id=int(value["source_library_id"]),
        target_library_id=int(value["target_library_id"]),
        media_item_id=int(value["media_item_id"]),
        title=str(value.get("title") or "未知条目"),
        moves=[TransferMove(**item) for item in value.get("moves", [])],
        skips=[TransferSkip(**item) for item in value.get("skips", [])],
        missing_file_ids=[int(item) for item in value.get("missing_file_ids", [])],
        total_bytes=int(value.get("total_bytes") or 0),
        cross_device=bool(value.get("cross_device")),
        blocked=[str(item) for item in value.get("blocked", [])],
    )


@jobs.register_job_handler("library.transfer")
async def _run_transfer_job(
    context: jobs.JobContext, input_data: dict[str, object]
) -> dict[str, object]:
    """条目转移处理器：路径发布、台账迁移和订阅改挂均可跨进程恢复。"""
    plan = _plan_from_job(input_data.get("plan"))
    checkpoint_id = str(input_data.get("checkpoint_id") or context.job_id)
    state = TransferState(
        source_library_id=plan.source_library_id,
        target_library_id=plan.target_library_id,
        media_item_id=plan.media_item_id,
        title=plan.title,
        total=len(plan.moves),
    )
    if not _transfer_tasks.try_start(plan.source_library_id, state):
        raise jobs.JobRetry("源媒体库仍有转移任务在收尾", delay_seconds=5)
    if not _transfer_tasks.try_start(plan.target_library_id, state):
        _transfer_tasks.finish(plan.source_library_id)
        raise jobs.JobRetry("目标媒体库仍有转移任务在收尾", delay_seconds=5)

    summary = TransferSummary(
        source_library_id=plan.source_library_id,
        target_library_id=plan.target_library_id,
        media_item_id=plan.media_item_id,
        title=plan.title,
        target_library_name=str(input_data.get("target_library_name") or ""),
    )
    try:
        await context.update_progress(
            mode="determinate",
            phase="transferring",
            message="正在核对上次保存的转移进度",
            current=0,
            total=len(plan.moves),
            percent=0.0 if plan.moves else 100.0,
            details={
                "source_library_id": plan.source_library_id,
                "target_library_id": plan.target_library_id,
                "media_item_id": plan.media_item_id,
                "cross_device": plan.cross_device,
                "total_bytes": plan.total_bytes,
            },
        )
        await _transfer(
            plan,
            state,
            summary,
            context=context,
            checkpoint_id=checkpoint_id,
        )
        message = f"已把「{plan.title}」转移到「{summary.target_library_name}」"
        if summary.errors:
            message += f"，{len(summary.errors)} 个问题已跳过"
        return {"message": message, **asdict(summary)}
    finally:
        await _refresh_stats_after_transfer(plan, summary)
        finished = (utcnow(), summary)
        _transfer_tasks.finish(plan.source_library_id, result=finished)
        _transfer_tasks.finish(plan.target_library_id, result=finished)


class _MoveError(Exception):
    """单个单元搬运失败。message 是完整中文句子，直接进 errors。"""


def _checkpoint_paths(dst: Path, checkpoint_id: str) -> tuple[Path, Path]:
    """返回跨盘续传临时路径与发布标记；二者都与最终目标同目录。"""
    safe = "".join(ch for ch in checkpoint_id if ch.isalnum())[:40]
    return (
        dst.with_name(f".{dst.name}.movieclaw-{safe}.partial"),
        dst.with_name(f".{dst.name}.movieclaw-{safe}.transfer"),
    )


async def _move_resumable(
    context: jobs.JobContext,
    src: Path,
    dst: Path,
    checkpoint_id: str,
) -> None:
    """可跨重启恢复的搬运原语。

    同盘仍是一次原子 rename。跨盘先复制到目标旁的隐藏路径，文件按现有
    长度续传；全部复制完成才原子发布为最终路径。发布标记写在发布前，若
    更新恰好发生在“目标已发布、源尚未删完”之间，下次可确认归属并继续
    删除源，而不会把目标误判为外部冲突。
    """
    partial, marker = _checkpoint_paths(dst, checkpoint_id)
    dst_exists = os.path.lexists(dst)
    src_exists = os.path.lexists(src)
    if dst_exists:
        if marker.exists():
            if src_exists:
                await asyncio.to_thread(_remove_source, src)
            marker.unlink(missing_ok=True)
            return
        if not src_exists:
            # 同盘 rename 或跨盘发布完成后，进程停在台账提交之前。
            return
        raise _MoveError(f"源与目标同时存在且没有本任务续传标记，跳过以免覆盖：{dst}")
    if not src_exists:
        raise _MoveError(f"源路径已不在原位，且没有可恢复的目标：{src}")

    try:
        await asyncio.to_thread(_rename_no_replace_with_parent, src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            if isinstance(exc, FileExistsError):
                raise _MoveError(f"目标路径已被占用，跳过以免覆盖：{dst}") from exc
            raise _MoveError(f"搬运失败（{exc.strerror}）：{src} → {dst}") from exc

    try:
        await _copy_path_resumable(context, src, partial)
        await context.raise_if_cancelled()
        await asyncio.to_thread(_write_transfer_marker, marker)
        try:
            await asyncio.to_thread(rename_no_replace, partial, dst)
        except FileExistsError as exc:
            raise _MoveError(f"目标路径在发布前被占用，已保留续传副本：{dst}") from exc
        await asyncio.to_thread(_remove_source, src)
        marker.unlink(missing_ok=True)
    except jobs.JobCancelled:
        raise
    except asyncio.CancelledError:
        raise
    except _MoveError:
        raise
    except (OSError, shutil.Error) as exc:
        # 临时副本刻意保留：网络盘瞬断或应用更新后，下次从已有字节继续。
        raise _MoveError(f"跨盘复制暂未完成（{exc}）：{src} → {dst}") from exc


def _rename_no_replace_with_parent(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    rename_no_replace(src, dst)


def _write_transfer_marker(marker: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)


def _remove_source(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


async def _copy_path_resumable(context: jobs.JobContext, source: Path, partial: Path) -> None:
    """把文件或目录复制到隐藏路径；每个文件按字节偏移续传。"""
    if source.is_symlink():
        await asyncio.to_thread(_copy_symlink_once, source, partial)
        return
    if source.is_file():
        await _copy_file_resumable(context, source, partial)
        return
    if not source.is_dir():
        raise OSError(f"不支持的源路径类型：{source}")

    partial.mkdir(parents=True, exist_ok=True)
    entries = await asyncio.to_thread(lambda: sorted(source.rglob("*")))
    for entry in entries:
        await context.raise_if_cancelled()
        target = partial / entry.relative_to(source)
        if entry.is_symlink():
            await asyncio.to_thread(_copy_symlink_once, entry, target)
        elif entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif entry.is_file():
            await _copy_file_resumable(context, entry, target)
    await asyncio.to_thread(shutil.copystat, source, partial, follow_symlinks=False)


def _copy_symlink_once(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target):
        if target.is_symlink() and os.readlink(target) == os.readlink(source):
            return
        raise FileExistsError(f"续传路径已存在不同内容：{target}")
    target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())


async def _copy_file_resumable(context: jobs.JobContext, source: Path, partial: Path) -> None:
    partial.parent.mkdir(parents=True, exist_ok=True)
    while True:
        await context.raise_if_cancelled()
        copied, total = await asyncio.to_thread(_copy_file_chunk, source, partial)
        if copied >= total:
            await asyncio.to_thread(shutil.copystat, source, partial, follow_symlinks=False)
            return


def _copy_file_chunk(source: Path, partial: Path) -> tuple[int, int]:
    """复制至多一个块并关闭文件句柄；返回（已落盘字节，总字节）。"""
    total = source.stat().st_size
    copied = partial.stat().st_size if partial.exists() else 0
    if copied > total:
        partial.unlink()
        copied = 0
    if copied == total:
        partial.touch(exist_ok=True)
        return copied, total
    with source.open("rb") as source_file:
        source_file.seek(copied)
        chunk = source_file.read(_COPY_CHUNK_BYTES)
    mode = "ab" if copied else "wb"
    with partial.open(mode) as target_file:
        target_file.write(chunk)
        target_file.flush()
    return copied + len(chunk), total


def _move(src: Path, dst: Path, cross_device: bool) -> None:
    """把一个文件或目录搬到新位置（同步，放线程池）。

    同盘走 ``os.rename``（瞬时、保留硬链接与 inode）；跨盘 rename 会以
    EXDEV 失败，退化为"完整复制 → 复制成功才删源"。复制中途失败会清掉
    半成品目标再报错，绝不留下一个看起来像搬完了的残缺目录。

    ``cross_device`` 只是计划阶段的**预判**（用于文案），真正的分支仍以
    rename 的 errno 为准——预判失误不会导致错误行为。
    """
    if not src.exists():
        raise _MoveError(f"源路径已不在原位，跳过：{src}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _MoveError(f"创建目标目录失败（{exc.strerror}）：{dst.parent}") from exc
    if dst.exists():
        raise _MoveError(f"目标路径已被占用，跳过以免覆盖：{dst}")
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise _MoveError(f"搬运失败（{exc.strerror}）：{src} → {dst}") from exc
    # 跨设备：复制成功才删源
    try:
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    except (OSError, shutil.Error) as exc:
        shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink(missing_ok=True)
        raise _MoveError(f"跨盘复制失败（{exc}）：{src} → {dst}") from exc
    try:
        shutil.rmtree(src) if src.is_dir() else src.unlink()
    except OSError as exc:
        raise _MoveError(
            f"已复制到目标，但删除源路径失败（{exc.strerror}）：{src}——"
            "请手工确认后删除，否则会占用双份空间"
        ) from exc
