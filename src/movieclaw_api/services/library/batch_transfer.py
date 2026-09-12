"""批量条目转移：把一批作品一次搬到另一个媒体库。

为什么要有它：单条目转移一次只搬一部，合并两个库（593 部电影 + 238 部剧）
就是 800 次点击、800 个作业、800 次确认——整件事里最大的低效与不稳定源。
本模块把「集合」这一层补上，而**搬运引擎完全复用 transfer.py**：同一套条目
目录识别、混目录降级逐文件、跨盘续传、空目录清理、身份锚重定位。批量只负责
成员的编排、检查点与失败策略。

三条与单条目不同的语义（docs/design/library-bulk-relocate.md §5、§8）：

- **集合冻结，路径重算**：成员集合在预览时定下、写进作业输入，执行期不再
  变（用户确认的是"这 593 部"，而筛选结果会随扫描与刮削漂移）；每个成员的
  路径计划则在轮到它时按磁盘现场**现算**——既以最新状态为准，也避免把几十
  万条路径塞进 SQLite 的作业输入。
- **冲突逐条跳过，失败分三级**：目标已有同名目录只是这一条跳过，其余照搬；
  而执行期的意外按「这个错对下一个成员还会不会发生」分级——环境性故障
  （盘满/只读/掉线）整轮退避重试，单条问题记下继续。
- **连续失败熔断**：连着 5 个成员失败一定是系统性问题，停到 ``blocked``
  （不是 ``failed``）等用户处理——blocked 的恢复是原地入队、保留检查点，
  用户腾出空间点一下继续，从断点接着搬剩下的，不必重新预览。

「一个作业」而不是「593 个子作业」：子作业方案会往 job_resource 写上千行、
把按资源聚合的媒体墙冲垮，还要引入父子租约与取消传播；失败隔离自己做的
代价远小于此，而"只重试失败的那几部"用「把失败子集重新提交」满足即可。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlmodel import select

from movieclaw_api.services import jobs

# 与 transfer.py 的内部件共用：批量是单条目的泛化，不是第二套搬运实现。
# 同一段语义两处各写一份迟早分叉，而搬运的每一处克制都是安全底线。
from movieclaw_api.services.library.transfer import (
    TransferState,
    TransferSummary,
    _MoveHalt,
    _transfer,
    _transfer_tasks,
    build_transfer_plan,
    notify_media_server,
)
from movieclaw_db.engine import get_database
from movieclaw_db.models import Library, LibraryFile, MediaItem, utcnow
from movieclaw_db.repositories.library_repo import LibraryRepository

logger = logging.getLogger("movieclaw_api.library_batch_transfer")

JOB_TYPE = "library.transfer-batch"
# 库内根路径归并：把若干个根下的条目并到一个根。它与批量转移是**同一个引擎的
# 两种投影**——跨库转移的目标根是目标库主根、台账改归属；库内归并的目标根是
# 指定的那个根、台账只改路径、收尾还要更新 root_paths。所以共用同一个处理器，
# 只用两个作业类型把它们在任务中心区分开。
CONSOLIDATE_JOB_TYPE = "library.consolidate-roots"

# 连续失败到这个数就熔断。用「连续」而不是「累计失败率」：593 部里零星几部
# 搬不动（个别文件权限）不该停，连着 5 部失败则一定是系统性的；累计率要等
# 跑完一半才有统计意义，太晚了。阈值写死——这是安全网，不是策略旋钮。
MAX_CONSECUTIVE_FAILURES = 5

# 冲突策略（§7.6）。缺省 skip 的理由是**可逆性不对称**：批量场景下用户看不见
# 每一条，跳过的损失是「这几部没搬、得手工处理」，合并的损失可能是「文件名被
# 改了」而用户根本没细看预检。可逆性不对称时，缺省取可逆的那一侧。
ON_CONFLICT_SKIP = "skip"
ON_CONFLICT_MERGE = "merge"
ON_CONFLICT_FAIL = "fail"


@dataclass
class BatchMember:
    """冻结进作业输入的一个成员。标题一并存下，作业结论里不必回查条目表。"""

    media_item_id: int
    title: str


@dataclass
class BatchOutcome:
    """批量转移的结论。

    ``skips`` 与 ``failures`` 刻意是**两个**数组：跳过是预检里用户已经看过并
    确认的策略性结果（不计熔断、不影响退出码），失败是执行时才发生的意外。
    混成一个会让熔断在合库场景下误触发——200 部同名连着跳过，第 5 部就停了。
    """

    moved: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_moved: int = 0
    files_relocated: int = 0
    removed_dirs: int = 0
    subscriptions_moved: int = 0
    skips: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    # 已处理成员的检查点：重启后据此跳过，不重搬也不重复计数
    done: list[int] = field(default_factory=list)


async def resolve_members(
    session,
    library_id: int,
    *,
    media_item_ids: list[int] | None,
    all_items: bool,
) -> list[BatchMember]:
    """把「选择集」解析成冻结的成员清单。

    选择集只有两种形态：显式 id 列表，或整库。**刻意不接受筛选表达式**——
    筛选面已经在 library.items.list 上，在这里复制一份必然分叉（面板说
    593 部、实际搬了 586 部，而且没人说得清差在哪）。
    """
    stmt = (
        select(MediaItem.id, MediaItem.title)
        .join(LibraryFile, LibraryFile.media_item_id == MediaItem.id)  # type: ignore[arg-type]
        .where(LibraryFile.library_id == library_id)
        .distinct()
    )
    if not all_items:
        ids = list(dict.fromkeys(media_item_ids or []))
        if not ids:
            return []
        stmt = stmt.where(MediaItem.id.in_(ids))  # type: ignore[union-attr]
    rows = (await session.execute(stmt)).all()
    found = {int(mid): str(title) for mid, title in rows}
    if all_items:
        return [BatchMember(media_item_id=mid, title=title) for mid, title in found.items()]
    # 保持调用方给的顺序：用户在墙上的选择顺序就是他心里的顺序
    return [
        BatchMember(media_item_id=mid, title=found[mid])
        for mid in dict.fromkeys(media_item_ids or [])
        if mid in found
    ]


async def resolve_members_under_roots(
    session,
    library_id: int,
    roots: list[str],
) -> list[BatchMember]:
    """选出「文件落在这些根下」的全部条目——库内归并的选择集。

    归并选的是**根**不是条目：用户说的是"把这两个旧目录并到新目录去"，
    而不是逐个挑片子。
    """
    prefixes = [r.rstrip("/") + "/" for r in roots if r.strip()]
    if not prefixes:
        return []
    stmt = (
        select(MediaItem.id, MediaItem.title)
        .join(LibraryFile, LibraryFile.media_item_id == MediaItem.id)  # type: ignore[arg-type]
        .where(LibraryFile.library_id == library_id)
        .distinct()
    )
    rows = (await session.execute(stmt)).all()
    found = {int(mid): str(title) for mid, title in rows}
    paths = (
        await session.execute(
            select(LibraryFile.media_item_id, LibraryFile.file_path).where(
                LibraryFile.library_id == library_id,
                LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
            )
        )
    ).all()
    under: dict[int, None] = {}
    for media_item_id, file_path in paths:
        if any(str(file_path).startswith(prefix) for prefix in prefixes):
            under.setdefault(int(media_item_id), None)
    return [BatchMember(media_item_id=mid, title=found[mid]) for mid in under if mid in found]


async def enqueue_consolidate_job(
    session,
    *,
    library: Library,
    into: str,
    drop_roots: list[str],
    members: list[BatchMember],
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "web",
) -> jobs.CreateJobResult:
    """投递库内根路径归并作业（与批量转移共用处理器，见 CONSOLIDATE_JOB_TYPE）。"""
    assert library.id is not None
    total = len(members)
    return await jobs.create_job(
        session,
        job_type=CONSOLIDATE_JOB_TYPE,
        subject=f"{library.name}：{total} 个条目并入 {into}",
        input_data={
            "source_library_id": library.id,
            "target_library_id": library.id,
            "target_library_name": library.name,
            "target_root": into,
            "drop_roots": drop_roots,
            "on_conflict": ON_CONFLICT_SKIP,
            "members": [asdict(m) for m in members],
            "checkpoint_id": uuid.uuid4().hex,
        },
        resources=[jobs.ResourceRef("library", library.id)],
        dedupe_key=f"{CONSOLIDATE_JOB_TYPE}:{library.id}:{into}",
        conflict_policy="return_existing",
        handler_revision=f"{CONSOLIDATE_JOB_TYPE}.v1",
        max_attempts=3,
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress={
            **jobs.default_progress(f"等待归并 {total} 个条目"),
            "total": total,
            "details": {
                "source_library_id": library.id,
                "target_library_id": library.id,
                "total_items": total,
                "done_items": 0,
                "done": [],
            },
        },
    )


async def enqueue_batch_transfer_job(
    session,
    *,
    source: Library,
    target: Library,
    members: list[BatchMember],
    on_conflict: str,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "web",
) -> jobs.CreateJobResult:
    """保存确认后的成员集合并投递作业。

    去重键含成员集合的指纹：网络重试提交同一批不会搬两次，而换一批成员是
    另一件事。真正的互斥由库级任务位承担（同一个库同时只能有一次搬运）。
    """
    assert source.id is not None and target.id is not None
    fingerprint = ",".join(str(m.media_item_id) for m in members)
    checkpoint_id = uuid.uuid4().hex
    total = len(members)
    return await jobs.create_job(
        session,
        job_type=JOB_TYPE,
        subject=f"{total} 个条目 → {target.name}",
        input_data={
            "source_library_id": source.id,
            "target_library_id": target.id,
            "target_library_name": target.name,
            "on_conflict": on_conflict,
            "members": [asdict(m) for m in members],
            "checkpoint_id": checkpoint_id,
        },
        resources=[
            jobs.ResourceRef("library", source.id),
            jobs.ResourceRef("library", target.id),
        ],
        dedupe_key=f"{JOB_TYPE}:{source.id}:{target.id}:{hash(fingerprint) & 0xFFFFFFFF:08x}",
        conflict_policy="return_existing",
        handler_revision=f"{JOB_TYPE}.v1",
        max_attempts=3,
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress={
            **jobs.default_progress(f"等待转移 {total} 个条目"),
            "total": total,
            "details": {
                "source_library_id": source.id,
                "target_library_id": target.id,
                "total_items": total,
                "done_items": 0,
                "done": [],
            },
        },
    )


@jobs.register_job_handler(JOB_TYPE)
@jobs.register_job_handler(CONSOLIDATE_JOB_TYPE)
async def _run_batch_transfer_job(
    context: jobs.JobContext, input_data: dict[str, object]
) -> dict[str, object]:
    """批量转移处理器：逐成员现算计划、逐成员提交、逐成员落检查点。

    原子性边界刻意落在**成员**上而不是整批：593 部一个事务等于把库锁住几个
    小时，而且回滚也回滚不了磁盘。每个成员搬完即提交并写进检查点，任何时刻
    崩溃，磁盘与台账的偏差最多是一个成员的一次搬运，而那个窗口由续传发布
    标记兜住。
    """
    source_id = int(input_data["source_library_id"])  # type: ignore[arg-type]
    target_id = int(input_data["target_library_id"])  # type: ignore[arg-type]
    on_conflict = str(input_data.get("on_conflict") or ON_CONFLICT_SKIP)
    landing = Path(str(input_data["target_root"])) if input_data.get("target_root") else None
    # 库内归并才有：并完之后要从库配置里摘掉的那些源根
    drop_roots = [str(r) for r in (input_data.get("drop_roots") or [])]
    checkpoint_id = str(input_data.get("checkpoint_id") or context.job_id)
    raw_members = input_data.get("members") or []
    members = [BatchMember(**m) for m in raw_members]  # type: ignore[arg-type]

    # 从已持久化的进度里恢复检查点：重启/退避重试后不重搬已完成的成员
    details = (await context.current_progress()).get("details") or {}
    outcome = BatchOutcome(done=[int(i) for i in (details.get("done") or [])])
    done = set(outcome.done)

    state = TransferState(
        source_library_id=source_id,
        target_library_id=target_id,
        media_item_id=0,
        title=f"{len(members)} 个条目",
        total=len(members),
    )
    # 两侧库各占一个任务位，整轮只取一次：扫描/整理/重识别一律挡下。
    # 库内归并时两个 id 相同，只占一次（重复登记会被任务位判成"已在跑"）
    if not _transfer_tasks.try_start(source_id, state):
        raise jobs.JobRetry("源媒体库仍有搬运任务在收尾", delay_seconds=5)
    same_library = source_id == target_id
    if not same_library and not _transfer_tasks.try_start(target_id, state):
        _transfer_tasks.finish(source_id)
        raise jobs.JobRetry("目标媒体库仍有搬运任务在收尾", delay_seconds=5)

    # **先加后删**：归并到一个当前不在 root_paths 里的新根时，必须在开始搬
    # 之前就把它加进配置。顺序反了会出事——文件先落到库根之外，下一次扫描
    # （或崩溃后的恢复扫描）会把它们全标 missing。代价是中途失败时配置里会
    # 多一个根、处于"两个根都有内容"的半搬完状态，但那是可观察、可续跑的，
    # 比"文件在根外"的静默损坏好得多。
    if landing is not None:
        await _ensure_root_registered(source_id, landing)

    consecutive = 0
    try:
        for index, member in enumerate(members, start=1):
            await context.raise_if_cancelled()
            if member.media_item_id in done:
                continue
            state.media_item_id = member.media_item_id
            state.title = member.title
            state.processed = index

            verdict = await _transfer_one(
                member,
                source_id=source_id,
                target_id=target_id,
                landing=landing,
                context=context,
                checkpoint_id=checkpoint_id,
                outcome=outcome,
                on_conflict=on_conflict,
            )
            if verdict == "failed":
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    await _save_progress(
                        context,
                        outcome,
                        index,
                        len(members),
                        source_id=source_id,
                        target_id=target_id,
                        force=True,
                    )
                    raise jobs.JobBlocked(
                        f"连续 {consecutive} 个条目搬运失败，已暂停以免继续扩大影响；"
                        f"已完成 {outcome.moved} 个。请检查目标盘与源目录后恢复本任务，"
                        "恢复会从断点继续，不会重搬已完成的部分",
                        code="LIBRARY_TRANSFER_TOO_MANY_FAILURES",
                    )
            else:
                consecutive = 0

            done.add(member.media_item_id)
            outcome.done = sorted(done)
            await _save_progress(
                context,
                outcome,
                index,
                len(members),
                source_id=source_id,
                target_id=target_id,
                force=index == len(members),
            )
    finally:
        _transfer_tasks.finish(source_id)
        if not same_library:
            _transfer_tasks.finish(target_id)
        await _refresh_batch_stats(source_id, target_id)

    # 只有真正搬干净了才摘掉源根：还有跳过或失败就说明那些根下仍有内容，
    # 摘掉会让它们的台账瞬间指到库根之外、下次扫描全标 missing
    if drop_roots and not outcome.failed and not outcome.skipped:
        await _drop_roots(source_id, drop_roots)

    await notify_media_server()
    return {"message": _summary_message(outcome, len(members)), **asdict(outcome)}


async def _transfer_one(
    member: BatchMember,
    *,
    source_id: int,
    target_id: int,
    landing: Path | None,
    context: jobs.JobContext,
    checkpoint_id: str,
    outcome: BatchOutcome,
    on_conflict: str,
) -> str:
    """搬一个成员；返回 moved / skipped / failed。

    每个成员独立开会话：提交边界与检查点边界对齐，一个成员出问题不会把
    前面已经提交的台账拖下水。
    """
    db = get_database()
    async with db.session() as session:
        source = await session.get(Library, source_id)
        target = await session.get(Library, target_id)
        item = await session.get(MediaItem, member.media_item_id)
        if source is None or target is None:
            # 前提被推翻：继续做只会扩大损害
            raise jobs.JobFailed(
                "源或目标媒体库已不存在（可能在搬运期间被删除），已停止",
                code="LIBRARY_GONE",
            )
        if item is None:
            _skip(outcome, member, "条目已不存在（可能已被删除）")
            return "skipped"
        rows = list(
            (
                await session.execute(
                    select(LibraryFile).where(
                        LibraryFile.library_id == source_id,
                        LibraryFile.media_item_id == member.media_item_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            _skip(outcome, member, "这部作品已经不在源媒体库里了")
            return "skipped"
        # 预检到轮到它可能已经过了几小时：以此刻的磁盘现场为准重算
        plan = await build_transfer_plan(
            session,
            source,
            target,
            item,
            rows,
            target_root=landing,
            merge_same_anchor=on_conflict == ON_CONFLICT_MERGE,
        )

    if plan.blocked:
        reason = "；".join(plan.blocked)
        if on_conflict == ON_CONFLICT_FAIL:
            raise jobs.JobFailed(
                f"「{member.title}」{reason}——已按 fail 策略停止整批",
                code="LIBRARY_TRANSFER_CONFLICT",
            )
        _skip(outcome, member, reason)
        return "skipped"
    if not plan.moves and not plan.missing_file_ids:
        _skip(outcome, member, "；".join(s.reason for s in plan.skips) or "没有可搬运的内容")
        return "skipped"

    summary = TransferSummary(
        source_library_id=source_id,
        target_library_id=target_id,
        media_item_id=member.media_item_id,
        title=member.title,
    )
    state = TransferState(
        source_library_id=source_id,
        target_library_id=target_id,
        media_item_id=member.media_item_id,
        title=member.title,
        total=len(plan.moves),
    )
    try:
        await _transfer(
            plan,
            state,
            summary,
            context=context,
            checkpoint_id=checkpoint_id,
            notify_downstream=False,
        )
    except _MoveHalt as exc:
        # 环境性故障：整轮退避重试，已完成的保持已完成。继续试下一个成员
        # 只会把同一个错误重复几百遍，并在目标盘留下一地半截续传文件。
        raise jobs.JobRetry(
            f"{exc}——已完成 {outcome.moved} 个条目，稍后自动重试；请确认目标盘的剩余空间与挂载状态",
            delay_seconds=60,
        ) from exc
    except jobs.JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 -- 单条失败不该带走整批
        logger.exception("条目 #%s「%s」批量转移失败", member.media_item_id, member.title)
        _fail(outcome, member, f"搬运时发生错误：{exc}")
        return "failed"

    if summary.errors:
        # 搬了一部分但有文件没搬成：这是失败不是成功，否则用户会以为全好了
        _fail(outcome, member, "；".join(summary.errors))
        _accumulate(outcome, summary)
        return "failed"

    outcome.moved += 1
    _accumulate(outcome, summary)
    return "moved"


def _accumulate(outcome: BatchOutcome, summary: TransferSummary) -> None:
    outcome.bytes_moved += summary.bytes_moved
    outcome.files_relocated += summary.files_relocated
    outcome.removed_dirs += summary.removed_dirs
    if summary.subscription_moved:
        outcome.subscriptions_moved += 1


def _skip(outcome: BatchOutcome, member: BatchMember, reason: str) -> None:
    outcome.skipped += 1
    outcome.skips.append(
        {"media_item_id": member.media_item_id, "title": member.title, "reason": reason}
    )


def _fail(outcome: BatchOutcome, member: BatchMember, reason: str) -> None:
    outcome.failed += 1
    outcome.failures.append(
        {"media_item_id": member.media_item_id, "title": member.title, "reason": reason}
    )


async def _save_progress(
    context: jobs.JobContext,
    outcome: BatchOutcome,
    index: int,
    total: int,
    *,
    source_id: int,
    target_id: int,
    force: bool = False,
) -> None:
    """把进度与**检查点**一起落库；节流，但最后一条与熔断前必写。"""
    if not force and not context.progress_due():
        return
    await context.update_progress(
        mode="determinate",
        phase="transferring",
        message=f"已处理 {index} / {total} 个条目（成功 {outcome.moved}、"
        f"跳过 {outcome.skipped}、失败 {outcome.failed}）",
        current=index,
        total=total,
        percent=(index / total * 100) if total else 100.0,
        details={
            "source_library_id": source_id,
            "target_library_id": target_id,
            "total_items": total,
            "done_items": index,
            "moved": outcome.moved,
            "skipped": outcome.skipped,
            "failed": outcome.failed,
            "done": outcome.done,
        },
    )


async def _ensure_root_registered(library_id: int, root: Path) -> None:
    """确保归并目标根已在库配置里（先加后删的"先加"）。

    直接写 ``Library.root_paths``，**不走 library.update**：那条路径在根变化时
    会自动投递一次补扫，而归并任务正持着这个库的任务位，等于跟自己抢锁。
    归并的台账是逐条精确随迁的，也不需要靠扫描重建。
    """
    db = get_database()
    async with db.session() as session:
        library = await session.get(Library, library_id)
        if library is None:
            return
        target = str(root).rstrip("/")
        roots = [r.rstrip("/") for r in library.root_paths]
        if target in roots:
            return
        library.root_paths = [*roots, target]
        library.updated_at = utcnow()
        session.add(library)
        await session.commit()
        logger.info("媒体库 #%s 的归并目标根已加入配置：%s", library_id, target)


async def _drop_roots(library_id: int, roots: list[str]) -> None:
    """归并完成后摘掉已经搬空的源根（先加后删的"后删"）。

    只在整轮没有跳过也没有失败时调用：还剩内容就摘根，那些台账会瞬间指到
    库根之外，下一次扫描把它们全标 missing。
    """
    dropped = {r.rstrip("/") for r in roots}
    db = get_database()
    async with db.session() as session:
        library = await session.get(Library, library_id)
        if library is None:
            return
        remaining = [r for r in library.root_paths if r.rstrip("/") not in dropped]
        if not remaining or remaining == list(library.root_paths):
            return
        library.root_paths = remaining
        library.updated_at = utcnow()
        session.add(library)
        await session.commit()
        logger.info("媒体库 #%s 已摘掉归并完成的源根：%s", library_id, "、".join(sorted(dropped)))


async def _refresh_batch_stats(source_id: int, target_id: int) -> None:
    """整轮收尾刷新两库统计（593 次逐条刷新纯属浪费，一次即可）。

    失败只记日志：统计对不上下次扫描会自愈，绝不能因此回滚已完成的磁盘搬运。
    """
    try:
        db = get_database()
        async with db.session() as session:
            await LibraryRepository(session).refresh_stats([source_id, target_id])
    except Exception:  # noqa: BLE001
        logger.exception("批量转移收尾刷新媒体库统计失败（下次扫描会自愈）")


def _summary_message(outcome: BatchOutcome, total: int) -> str:
    parts = [f"已转移 {outcome.moved} / {total} 个条目"]
    if outcome.skipped:
        parts.append(f"跳过 {outcome.skipped} 个")
    if outcome.failed:
        parts.append(f"失败 {outcome.failed} 个")
    return "，".join(parts)
