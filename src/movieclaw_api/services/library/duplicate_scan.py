"""重复扫描：算一轮、落一份结论、页面只读它（docs/design/library-duplicate-files.md §9）。

第一版的重复文件页是**打开即现算**：每次 ``GET`` 都跑一遍全库检测，页面还每 30 秒
轮询一次，管理页没打开这个标签也每 120 秒为了标签上那个数字算一遍。检测整轮要给
每个候选文件各 ``stat`` 一次（网络盘上就是几千次往返）、各跑一遍发布名解析才排得出
「建议保留」——万级媒体库上这是几十秒的活，放在请求线上就是"打开页面特别慢"。

现在它和「扫描媒体库」是同一种东西：**用户按一次「开始扫描」，任务在后台跑一会儿，
结论落 ``library_duplicate_unit``，页面只读那张表**（媒体库扫描作业结束后也自动排
一轮，所以通常打开就已经有新鲜结果）。

第二件事是**分档**。扫出几千条重复，两堆（一模一样 / 不同版本）仍然是"一屏文件"，
用户不知道从哪下手。所以按"要用户花多少心思"分三档，页面先给三张卡：

- ``safe``「可以放心清理」——机器确认过没区别（同一 inode，或尺寸与时长都相等），
  清掉不丢任何东西，一个按钮；
- ``suggested``「建议清理」——不同版本，但机器真的比出了高下（档位阶梯分得出，
  ``suggest_basis == "ladder"``），其余是它的低配版；清理前逐条列出清单；
- ``review``「需要你决定」——其余全部。机器比不出来的东西按**取舍类型**再分组
  （分辨率不同 / HDR 与 SDR / 规格不全 / 同档不同版本），同一种取舍的单元聚成一组：
  用户对"我到底要 4K 还是要 1080p 的小体积"只需回答一次，而不是回答四百次。

分档只是**排序与分组**，不改变任何判定：每个单元原来的两个动作（留哪个 / 都留着）
一个没少，三档的任何一个批量按钮清掉的文件也都只是进回收站、7 天可恢复。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services import jobs
from movieclaw_api.services.library.duplicates import (
    DupFile,
    DupItem,
    DupUnit,
    ResolveOutcome,
    detect_duplicates,
    fold_seasons,
    quality_label_of,
    recycle_extras,
)
from movieclaw_api.services.library.origin import derive_origins, origin_of
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    FileState,
    LibraryDuplicateUnit,
    LibraryFile,
    MediaItem,
    utcnow,
)
from movieclaw_db.models.library import Library

logger = logging.getLogger("movieclaw_api.library.duplicate_scan")

Tier = Literal["safe", "suggested", "review"]
ReviewKind = Literal["resolution", "hdr", "unknown", "same_tier"]

JOB_TYPE = "library.duplicate-scan"

#: 三档的名字与一句话说明。文案放后端：CLI、Web 与将来的通知说的是同一句话
TIER_LABELS: dict[str, str] = {
    "safe": "可以放心清理",
    "suggested": "建议清理",
    "review": "需要你决定",
}
TIER_HINTS: dict[str, str] = {
    "safe": "机器确认过没有区别：同一个文件的多份记录，或尺寸与时长完全一致。清掉不丢任何东西。",
    "suggested": "每组里有一个档位明显更高，其余是它的低配版。清理前可以逐条看一眼清单。",
    "review": "各有各的好，机器不替你决定。同一种取舍的放在一组，一次回答一批。",
}
#: ``review`` 档的取舍类型：用户面对它时真正在犹豫的那件事
REVIEW_KIND_LABELS: dict[str, str] = {
    "resolution": "分辨率不同，档位分不出高下",
    "hdr": "HDR 与 SDR 并存",
    "unknown": "规格不全，比不出来",
    "same_tier": "同档不同版本（不同发布组 / 不同码率）",
}
REVIEW_KIND_HINTS: dict[str, str] = {
    "resolution": "一个分辨率高、一个片源更好，阶梯上各占一头。要画质还是要体积，只有你知道。",
    "hdr": "HDR 版本在不支持的设备上颜色会发灰，很多人两个都留着。",
    "unknown": "这些文件的分辨率或片源没探测到，机器没有比较的依据。",
    "same_tier": "规格完全同档，差的是发布组、字幕或压制。挑一个熟悉的组，或者都留着。",
}
#: 分档在页面上的先后：先做没风险的，再做要花心思的
TIER_ORDER: tuple[Tier, ...] = ("safe", "suggested", "review")
#: ``review`` 组的先后：大的取舍排前面
REVIEW_KIND_ORDER: tuple[ReviewKind, ...] = ("resolution", "hdr", "same_tier", "unknown")

#: 扫描任务的阶段（进度条按序号走）
_PHASES = ("units", "fingerprint", "suggest", "persist")
#: 落账时一次插入多少行
_INSERT_CHUNK = 500


# ---------------------------------------------------------------------------
# 分档
# ---------------------------------------------------------------------------


def classify(unit: DupUnit) -> tuple[Tier, str | None]:
    """一个单元该放进哪一档，以及（``review`` 档）它是哪一种取舍。

    判据只有一条线：**机器有没有把握**。「一模一样」是物理上确认过的没区别；
    「档位最高」是阶梯真的分出了高下；其余都是同档里按次级信号（码率、来源、
    命名）挑了一个——那种"建议"不足以支撑批量清理，必须由人看一眼。
    """
    if unit.bucket == "identical":
        return "safe", None
    if unit.suggested.suggest_basis == "ladder":
        return "suggested", None
    return "review", _review_kind(unit.files)


def _review_kind(files: list[DupFile]) -> ReviewKind:
    """这一组文件之间到底在犹豫什么。顺序即优先级：先看大的取舍。

    规格不全排在最前：分辨率是 ``None`` 时"分辨率不同"是句假话——那不是两个
    分辨率之间的取舍，是机器压根没探到规格。
    """
    if any(not f.row.resolution or not f.row.media_source for f in files):
        return "unknown"
    if len({f.row.resolution for f in files}) > 1:
        return "resolution"
    if len({f.row.hdr or "" for f in files}) > 1:
        return "hdr"
    return "same_tier"


# ---------------------------------------------------------------------------
# 扫描：算一轮、整表重建
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """一轮扫描的产出，同时是任务结论里存的那几个数。"""

    units: int = 0
    files: int = 0
    bytes: int = 0
    upgrading_units: int = 0
    keep_old_items: int = 0
    scanned_at: datetime = field(default_factory=utcnow)


async def scan_duplicates(
    session: AsyncSession, *, report_progress: Any | None = None
) -> ScanResult:
    """全库算一轮，把结论整表重建进 ``library_duplicate_unit``。

    整表重建而不是增量维护：重复关系会因为入库、洗版、扫描、手工删文件而变，
    维护一份随时可能失真的增量索引比重算一遍更容易出错，而重算的成本本来就
    只在这一个后台任务里付一次。
    """
    report = await detect_duplicates(session, report_progress=report_progress)
    if report_progress is not None:
        await report_progress("persist", "记录结论", None, None)
    result = ScanResult(
        upgrading_units=report.upgrading_units, keep_old_items=report.keep_old_items
    )
    rows: list[LibraryDuplicateUnit] = []
    for item in report.items:
        for season in item.seasons:
            for unit in season.units:
                tier, review_kind = classify(unit)
                extras = unit.extras
                rows.append(
                    LibraryDuplicateUnit(
                        library_id=item.library.id or 0,
                        media_item_id=item.item.id or 0,
                        season_number=unit.season_number,
                        episode_number=unit.episode_number,
                        bucket=unit.bucket,
                        tier=tier,
                        review_kind=review_kind,
                        file_ids=sorted(f.row.id or 0 for f in unit.files),
                        suggested_file_id=unit.suggested.row.id or 0,
                        suggest_reason=unit.suggested.suggest_reason,
                        extra_files=len(extras),
                        extra_bytes=sum(f.row.size_bytes for f in extras),
                        scanned_at=result.scanned_at,
                    )
                )
                result.units += 1
                result.files += len(extras)
                result.bytes += sum(f.row.size_bytes for f in extras)
    await session.execute(delete(LibraryDuplicateUnit))
    for start in range(0, len(rows), _INSERT_CHUNK):
        session.add_all(rows[start : start + _INSERT_CHUNK])
        await session.flush()
    await session.commit()
    logger.info(
        "重复扫描完成：%d 个单元（可清理 %d 个文件），洗版在途 %d 个单元不计",
        result.units,
        result.files,
        result.upgrading_units,
    )
    return result


async def forget_units(
    session: AsyncSession,
    *,
    media_item_id: int,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> None:
    """删掉已经做完决定的单元的结论行，让摘要数字立刻变小。

    做完决定不重扫整库（那是几十秒的活），只把这几行删掉：结论表本来就只是
    "上一轮扫出来还没处理的"，处理掉一个就少一个。真正的修正等下一轮扫描。
    """
    stmt = delete(LibraryDuplicateUnit).where(LibraryDuplicateUnit.media_item_id == media_item_id)
    if season_number is not None:
        stmt = stmt.where(LibraryDuplicateUnit.season_number == season_number)
    if episode_number is not None:
        stmt = stmt.where(LibraryDuplicateUnit.episode_number == episode_number)
    await session.execute(stmt)
    await session.commit()


# ---------------------------------------------------------------------------
# 读：摘要与列表都只查结论表
# ---------------------------------------------------------------------------


@dataclass
class GroupStats:
    """一档（或 ``review`` 档里的一组）有多少活要干。"""

    key: str
    label: str
    hint: str
    units: int = 0
    files: int = 0
    bytes: int = 0


@dataclass
class DuplicateSummary:
    tiers: list[GroupStats] = field(default_factory=list)
    review_groups: list[GroupStats] = field(default_factory=list)
    total_units: int = 0
    total_files: int = 0
    total_bytes: int = 0


async def summarize(session: AsyncSession, *, library_id: int | None = None) -> DuplicateSummary:
    """三档 + ``review`` 各组的计数。一条聚合查询，与页面上的批量按钮同一份数字。"""
    stmt = select(
        LibraryDuplicateUnit.tier,
        LibraryDuplicateUnit.review_kind,
        func.count(LibraryDuplicateUnit.id),
        func.coalesce(func.sum(LibraryDuplicateUnit.extra_files), 0),
        func.coalesce(func.sum(LibraryDuplicateUnit.extra_bytes), 0),
    ).group_by(LibraryDuplicateUnit.tier, LibraryDuplicateUnit.review_kind)
    if library_id is not None:
        stmt = stmt.where(LibraryDuplicateUnit.library_id == library_id)
    summary = DuplicateSummary()
    tiers = {
        tier: GroupStats(key=tier, label=TIER_LABELS[tier], hint=TIER_HINTS[tier])
        for tier in TIER_ORDER
    }
    groups = {
        kind: GroupStats(key=kind, label=REVIEW_KIND_LABELS[kind], hint=REVIEW_KIND_HINTS[kind])
        for kind in REVIEW_KIND_ORDER
    }
    for tier, review_kind, units, files, size in (await session.execute(stmt)).all():
        bucket = tiers.get(str(tier))
        if bucket is None:
            continue
        bucket.units += int(units)
        bucket.files += int(files)
        bucket.bytes += int(size)
        group = groups.get(str(review_kind))
        if group is not None:
            group.units += int(units)
            group.files += int(files)
            group.bytes += int(size)
        summary.total_units += int(units)
        summary.total_files += int(files)
        summary.total_bytes += int(size)
    summary.tiers = [tiers[tier] for tier in TIER_ORDER]
    summary.review_groups = [groups[kind] for kind in REVIEW_KIND_ORDER if groups[kind].units]
    return summary


def _scope(stmt, *, tier, review_kind, library_id, media_item_id):
    if tier is not None:
        stmt = stmt.where(LibraryDuplicateUnit.tier == tier)
    if review_kind is not None:
        stmt = stmt.where(LibraryDuplicateUnit.review_kind == review_kind)
    if library_id is not None:
        stmt = stmt.where(LibraryDuplicateUnit.library_id == library_id)
    if media_item_id is not None:
        stmt = stmt.where(LibraryDuplicateUnit.media_item_id == media_item_id)
    return stmt


async def _hydrate(
    session: AsyncSession, rows: list[LibraryDuplicateUnit]
) -> tuple[dict[int, DupUnit], list[int]]:
    """把结论行还原成可展示 / 可执行的单元；文件集合变过的算过期。

    结论里只存"哪些文件、哪一堆、建议留哪个"，展示要用的规格标签与来源文案
    现拼——它们只在翻到的那一页才需要，几十行的成本可以忽略，存下来反而会在
    改名、重新探测、订阅改名之后变成**过时的假话**。

    过期判据是集合相等：少了文件（被删被移）或多了文件（新入库、洗版落地）
    都算，两种都意味着"当时那个决定说的已经不是现在这回事"。过期的单元不显示
    也不清理，等下一轮扫描修正。
    """
    if not rows:
        return {}, []
    keys = {(r.media_item_id, r.season_number, r.episode_number): r for r in rows}
    live = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.media_item_id.in_({r.media_item_id for r in rows}),  # type: ignore[union-attr]
                    LibraryFile.state == FileState.IN_PLACE,
                    LibraryFile.unidentified_code.is_(None),  # type: ignore[union-attr]
                    LibraryFile.ignored_at.is_(None),  # type: ignore[union-attr]
                )
            )
        ).scalars()
    )
    by_key: dict[tuple[int, int, int], list[LibraryFile]] = defaultdict(list)
    for f in live:
        key = (int(f.media_item_id or 0), f.season_number, f.episode_number)
        if key in keys:
            by_key[key].append(f)
    fresh: dict[int, list[LibraryFile]] = {}
    stale: list[int] = []
    for key, row in keys.items():
        files = sorted(by_key.get(key, []), key=lambda f: f.id or 0)
        if [f.id for f in files] != list(row.file_ids):
            stale.append(row.id or 0)
            continue
        fresh[row.id or 0] = files
    origins = await derive_origins(session, [f for files in fresh.values() for f in files])
    units: dict[int, DupUnit] = {}
    for row in rows:
        files = fresh.get(row.id or 0)
        if files is None:
            continue
        dup_files = []
        for f in files:
            dup = DupFile(
                row=f,
                quality_label=quality_label_of(f),
                origin=origin_of(f, origins),
                version_key="",
                suggested=f.id == row.suggested_file_id,
                suggest_reason=row.suggest_reason if f.id == row.suggested_file_id else None,
            )
            dup.version_key = f"{dup.quality_label}|{dup.origin.get('label') or ''}"
            dup_files.append(dup)
        units[row.id or 0] = DupUnit(
            season_number=row.season_number,
            episode_number=row.episode_number,
            bucket=row.bucket,  # type: ignore[arg-type]
            files=dup_files,
        )
    if stale:
        logger.debug("重复结论过期 %d 个单元，等下一轮扫描修正", len(stale))
    return units, stale


async def list_duplicate_items(
    session: AsyncSession,
    *,
    tier: str | None = None,
    review_kind: str | None = None,
    library_id: int | None = None,
    media_item_id: int | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[int, list[DupItem]]:
    """按条目分页地读结论表。``limit=0`` 只要总数（页面落地先看摘要，不拉明细）。

    分页单位仍是条目（一部剧一块），所以先用一条聚合查询按「库名 + 片名」排出
    本页的条目 id，再只为这几个条目还原明细。
    """
    keyed = (
        select(
            LibraryDuplicateUnit.media_item_id,
            func.min(Library.name).label("library_name"),
            func.min(MediaItem.title).label("title"),
        )
        .join_from(
            LibraryDuplicateUnit,
            MediaItem,
            MediaItem.id == LibraryDuplicateUnit.media_item_id,  # type: ignore[arg-type]
        )
        .join(
            Library,
            Library.id == LibraryDuplicateUnit.library_id,  # type: ignore[arg-type]
        )
    )
    keyed = _scope(
        keyed,
        tier=tier,
        review_kind=review_kind,
        library_id=library_id,
        media_item_id=media_item_id,
    )
    if q:
        needle = f"%{q.strip()}%"
        keyed = keyed.where(
            or_(
                MediaItem.title.ilike(needle),  # type: ignore[union-attr]
                MediaItem.original_title.ilike(needle),  # type: ignore[union-attr]
            )
        )
    keyed = keyed.group_by(LibraryDuplicateUnit.media_item_id)
    total = int(
        (
            await session.execute(select(func.count()).select_from(keyed.order_by(None).subquery()))
        ).scalar_one()
    )
    if limit <= 0 or total == 0:
        return total, []
    page = (
        await session.execute(
            keyed.order_by("library_name", "title", LibraryDuplicateUnit.media_item_id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    item_ids = [int(row[0]) for row in page]
    if not item_ids:
        return total, []
    rows_stmt = _scope(
        select(LibraryDuplicateUnit).where(LibraryDuplicateUnit.media_item_id.in_(item_ids)),  # type: ignore[union-attr]
        tier=tier,
        review_kind=review_kind,
        library_id=library_id,
        media_item_id=media_item_id,
    )
    rows = list((await session.execute(rows_stmt)).scalars())
    units, _stale = await _hydrate(session, rows)
    by_item: dict[int, list[DupUnit]] = defaultdict(list)
    library_of: dict[int, int] = {}
    for row in rows:
        unit = units.get(row.id or 0)
        if unit is None:
            continue
        by_item[row.media_item_id].append(unit)
        library_of.setdefault(row.media_item_id, row.library_id)
    items = {
        i.id: i
        for i in (
            await session.execute(select(MediaItem).where(MediaItem.id.in_(by_item)))  # type: ignore[union-attr]
        ).scalars()
    }
    libraries = {
        lib.id: lib
        for lib in (
            await session.execute(
                select(Library).where(Library.id.in_(set(library_of.values())))  # type: ignore[union-attr]
            )
        ).scalars()
    }
    out: list[DupItem] = []
    for item_id in item_ids:
        item = items.get(item_id)
        library = libraries.get(library_of.get(item_id, -1))
        unit_list = by_item.get(item_id)
        if item is None or library is None or not unit_list:
            continue
        out.append(
            DupItem(
                library=library,
                item=item,
                seasons=fold_seasons(unit_list, item.kind == "tv"),
            )
        )
    return total, out


# ---------------------------------------------------------------------------
# 一组一起决定
# ---------------------------------------------------------------------------


async def resolve_group(
    session: AsyncSession,
    *,
    tier: str,
    review_kind: str | None,
    library_id: int | None,
    keep_all: bool,
    trigger: dict[str, Any],
    batch_limit: int,
) -> ResolveOutcome:
    """一整档（或 ``review`` 里的一组）一起决定：都按建议清理，或都留着。

    这是"一次回答一批"的那个动作：同一种取舍的几百个单元，用户其实只有一个
    答案。清理仍是逐单元重验后走回收站，一次最多 ``batch_limit`` 个文件，超出
    的返回 ``remaining``——「都留着」只盖标记不动文件，没有这个上限。
    """
    stmt = _scope(
        select(LibraryDuplicateUnit).order_by(LibraryDuplicateUnit.extra_bytes.desc()),  # type: ignore[union-attr]
        tier=tier,
        review_kind=review_kind,
        library_id=library_id,
        media_item_id=None,
    )
    rows = list((await session.execute(stmt)).scalars())
    outcome = ResolveOutcome()
    if not rows:
        return outcome

    if keep_all:
        units, _stale = await _hydrate(session, rows)
        now = utcnow()
        for unit in units.values():
            for f in unit.files:
                f.row.kept_at = now
                outcome.library_ids.add(f.row.library_id)
                outcome.done += 1
        await session.commit()
        await session.execute(
            delete(LibraryDuplicateUnit).where(
                LibraryDuplicateUnit.id.in_([r.id for r in rows if r.id in units])  # type: ignore[union-attr]
            )
        )
        await session.commit()
        return outcome

    # 按落账时的 extra_files 先切出够一批的行，只为这些行还原明细
    todo: list[LibraryDuplicateUnit] = []
    budget = batch_limit
    for row in rows:
        if budget <= 0:
            outcome.remaining += row.extra_files
            continue
        todo.append(row)
        budget -= row.extra_files
    units, _stale = await _hydrate(session, todo)
    budget = batch_limit
    resolved: list[int] = []
    for row in todo:
        unit = units.get(row.id or 0)
        if unit is None:  # 结论过期：不按过期结论删，等下一轮扫描
            continue
        extras = unit.extras
        if not extras:
            resolved.append(row.id or 0)
            continue
        if len(extras) > budget:
            outcome.remaining += len(extras)
            continue
        done_before = outcome.done
        await recycle_extras(session, unit, unit.suggested, trigger, outcome, include_kept=False)
        budget -= len(extras)
        # 一个都没清成的单元留着结论行：它的文件原封不动，还是"待处理"。
        # 删了只会让它从页面和摘要里一起消失，用户下次看到的是更小的数字
        # 和一样多的重复文件
        if outcome.done > done_before:
            resolved.append(row.id or 0)
    if resolved:
        await session.execute(
            delete(LibraryDuplicateUnit).where(LibraryDuplicateUnit.id.in_(resolved))  # type: ignore[union-attr]
        )
        await session.commit()
    return outcome


# ---------------------------------------------------------------------------
# 任务：手动触发，媒体库扫描结束也自动排一轮
# ---------------------------------------------------------------------------


@dataclass
class ScanState:
    """页面头部那一行：扫过没有、上次什么时候、现在是不是正在跑。"""

    status: str | None = None
    job_id: str | None = None
    message: str | None = None
    percent: float | None = None
    scanned_at: datetime | None = None
    upgrading_units: int = 0
    keep_old_items: int = 0


async def scan_state(session: AsyncSession) -> ScanState:
    """最近一次扫描的状态。事实源是 Job 表，不另存一份会和它对不上的状态。

    页头那一行要分清三件事：**从未扫过**（``status`` 为 None，页面上只有一个
    「开始扫描」）、**正在跑**（带进度）、**上次跑完于何时**。失败也要说出来——
    静默退回"还没有扫描过"，用户会一直按按钮却不知道为什么没结果。
    """
    recent = await jobs.list_jobs(session, job_type=JOB_TYPE, limit=10)
    state = ScanState()
    failure: str | None = None
    for job in recent:  # 按创建时间倒序
        status = str(job.status)
        if status in jobs.ACTIVE_STATUS_VALUES and state.job_id is None:
            progress = job.progress or {}
            state.status = status
            state.job_id = job.id
            state.message = progress.get("message")
            state.percent = progress.get("percent")
        elif status in ("failed", "cancelled") and failure is None and state.job_id is None:
            failure = status
            state.message = (job.error or {}).get("message")
        if status == "succeeded" and state.scanned_at is None:
            result = job.result or {}
            state.scanned_at = job.finished_at
            state.upgrading_units = int(result.get("upgrading_units") or 0)
            state.keep_old_items = int(result.get("keep_old_items") or 0)
    # Job 历史有保留期；结论行还在就说明扫过，时间以行上的为准
    if state.scanned_at is None:
        state.scanned_at = (
            await session.execute(select(func.max(LibraryDuplicateUnit.scanned_at)))
        ).scalar_one_or_none()
    if state.status is None:
        # 最近一次失败了但更早跑成功过：仍然有结论可看，只是这一次没跑成
        state.status = failure or ("succeeded" if state.scanned_at else None)
    return state


async def enqueue_duplicate_scan_job(
    session: AsyncSession,
    *,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "system",
) -> jobs.CreateJobResult:
    """排一轮重复扫描；同时最多一份（``dedupe_key`` 是常量）。

    不挂任何库资源：它跨全部媒体库、只读台账与文件元信息，占谁的租约都会平白
    挡住那个库的扫描/整理。低优先级——用户等着看结果的是扫描与刷新，这一轮
    晚几分钟没关系。
    """
    return await jobs.create_job(
        session,
        job_type=JOB_TYPE,
        subject="重复文件",
        input_data={},
        dedupe_key=JOB_TYPE,
        conflict_policy="return_existing",
        handler_revision=f"{JOB_TYPE}.v1",
        max_attempts=2,
        priority=-10,
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress=jobs.default_progress("等待扫描重复文件"),
    )


@jobs.register_job_handler(JOB_TYPE)
async def _run_duplicate_scan_job(
    context: jobs.JobContext, input_data: dict[str, Any]
) -> dict[str, Any]:
    """重复扫描处理器：算一轮、整表重建。中断了重跑一轮即可，没有断点要存。

    每报一次进度顺带查一次取消/租约——这是本任务唯一的安全边界。少了它有两个
    后果：用户在任务中心点「取消」按不动（要等整轮算完），以及**租约被接管后
    这一份还在跑**——它照样会把 ``library_duplicate_unit`` 整表删掉重建一遍，
    与接管者的写并发打架；框架事后那句「丢弃本次执行结果」只丢作业结论，
    删表重建的副作用早已落库。
    """

    async def report(phase: str, message: str, current: int | None, total: int | None) -> None:
        determinate = current is not None and total
        await context.update_progress(
            mode="determinate" if determinate else "indeterminate",
            phase=phase,
            message=message,
            current=current,
            total=total,
            percent=round(current * 100 / total, 1) if determinate else None,  # type: ignore[operator]
            phase_index=_PHASES.index(phase) + 1,
            phase_count=len(_PHASES),
        )
        # 放在 update_progress 之后：它已经顺路发现过租约失效，这时判定不花查询
        await context.raise_if_cancelled()

    db = get_database()
    async with db.session() as session:
        result = await scan_duplicates(session, report_progress=report)
    message = (
        f"重复扫描完成：{result.units} 个单元有重复，按建议清理可腾出 {result.files} 个文件"
        if result.units
        else "重复扫描完成：没有发现重复文件"
    )
    return {
        "message": message,
        "units": result.units,
        "files": result.files,
        "bytes": result.bytes,
        "upgrading_units": result.upgrading_units,
        "keep_old_items": result.keep_old_items,
    }


async def enqueue_after_library_change(library_name: str) -> None:
    """媒体库扫描 / 整理结束后自动排一轮：用户打开页面时通常已经有新鲜结果。

    失败只记日志不打断调用方——它挂在别人的作业收尾处，重复扫描排不上队
    不该让那个作业变成失败。
    """
    try:
        db = get_database()
        async with db.session() as session:
            await enqueue_duplicate_scan_job(session, origin="system")
    except Exception:  # noqa: BLE001 -- 附带动作，失败不影响主作业
        logger.warning("「%s」变更后排重复扫描失败，可在页面手动扫描", library_name, exc_info=True)


__all__ = [
    "JOB_TYPE",
    "REVIEW_KIND_LABELS",
    "TIER_LABELS",
    "DuplicateSummary",
    "GroupStats",
    "ScanResult",
    "ScanState",
    "classify",
    "enqueue_after_library_change",
    "enqueue_duplicate_scan_job",
    "forget_units",
    "list_duplicate_items",
    "resolve_group",
    "scan_duplicates",
    "scan_state",
    "summarize",
]
