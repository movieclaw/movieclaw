"""重复文件：检测、分堆、建议保留与清理（docs/design/library-duplicate-files.md §3）。

同一**单元**（电影 = 条目，剧集 = 某季某集）下有两个以上在位文件，就是这里的
对象。设计只有一条分界线、两种决定：

- **一模一样**（``identical``）：单元内所有文件两两满足"同一 inode"或"尺寸相等且
  实测时长相等"——多出来的没多任何东西，一键清；
- **不同版本**（``versions``）：其余全部——同档不同组、1080p 与 2160p、HDR 与
  SDR、国配与原声、片源未知比不了……有区别，区别值不值得留只有用户知道，
  每个单元回答一次：**留哪个 / 都留着**。

机器只负责把"确定没区别"的和"有区别"的分开，再给每个单元贴一个「建议保留」
标签（复用洗版的档位阶梯，比不出来就按实测码率），不做任何自动清理。

剧集按季折叠：一季各集的文件按**版本签名**（质量标签 + 来源）分组，各集的签名
集合一致时（同构）只列版本行，动作是「整季留这个」；不同构的季直接列各集。

不列出的单元：所有文件都带「都留着」标记（``kept_at``）的；订阅规则组开了
「保留共存」的条目；洗版验证在途的单元（与验证打架）。

清理只调 ``recycle_file``：进回收站、7 天可恢复。执行前逐文件重验——列表是
几分钟前算的，中间可能跑了扫描或洗版，不按过期结论删。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Literal

from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.library.origin import derive_origins, origin_of
from movieclaw_api.services.library.recycle import recycle_file
from movieclaw_db.models import FileState, LibraryFile, MediaItem, RuleSet, Subscription, utcnow
from movieclaw_db.models.library import Library
from movieclaw_db.models.subscription import DownloadAttemptStatus, SubscriptionDownloadAttempt
from movieclaw_matcher.decision import compare_ladder, ladder_vector
from movieclaw_matcher.models import RuleSetSpec

logger = logging.getLogger("movieclaw_api.library.duplicates")

Bucket = Literal["identical", "versions"]

REASON_DUPLICATE = "duplicate_cleanup"  # trash_context.reason 词表新词：重复清理

# 同内容副本的时长容差：与扫描改名归并（scan._try_relink）同一指纹
_DURATION_TOLERANCE = 2
# 洗版在途的投递状态（与 upgrade.run_upgrade 的 in_flight 口径一致）
_IN_FLIGHT = (
    DownloadAttemptStatus.ACTIVE,
    DownloadAttemptStatus.REPLACEMENT_PENDING,
    DownloadAttemptStatus.TRIAL,
    DownloadAttemptStatus.CLEANUP_PENDING,
    DownloadAttemptStatus.COMPLETED,
)
# 整理器的多版本退让名：``片名 (2020) - 1080p.mkv``——占着标准名的那个更像"正主"
_VERSION_SUFFIX = re.compile(
    r" - (?:\d{3,4}p|V\d+|WEB-?DL|WEBRip|Blu-?ray|BluRay|HDTV|Remux|Disc)$", re.I
)
# 来源可追溯性：本系统入库的有台账证据链，扫描发现的是"不知道谁放的"
_ORIGIN_PRIORITY = {"subscription": 3, "manual_download": 3, "watch_import": 2, "scan": 1}
_NEUTRAL_SPEC = RuleSetSpec()


# ---------------------------------------------------------------------------
# 结果结构（路由层直接映射成视图）
# ---------------------------------------------------------------------------


@dataclass
class DupFile:
    row: LibraryFile
    quality_label: str
    origin: dict[str, Any]
    version_key: str
    suggested: bool = False
    suggest_reason: str | None = None

    @property
    def kept(self) -> bool:
        return self.row.kept_at is not None

    @property
    def file_name(self) -> str:
        return PurePath(self.row.file_path).name


@dataclass
class DupUnit:
    season_number: int
    episode_number: int
    bucket: Bucket
    files: list[DupFile]

    @property
    def suggested(self) -> DupFile:
        return next(f for f in self.files if f.suggested)

    @property
    def extras(self) -> list[DupFile]:
        """会被清掉的文件：既不是建议保留者、也没被用户「都留着」过的。"""
        return [f for f in self.files if not f.suggested and not f.kept]


@dataclass
class DupVersion:
    key: str
    quality_label: str
    origin_label: str
    episodes: list[int]
    bytes: int
    suggested: bool


@dataclass
class DupSeason:
    season_number: int
    bucket: Bucket
    uniform: bool
    versions: list[DupVersion]
    units: list[DupUnit]

    @property
    def extras(self) -> list[DupFile]:
        return [f for u in self.units for f in u.extras]


@dataclass
class DupItem:
    library: Library
    item: MediaItem
    seasons: list[DupSeason]


@dataclass
class BucketStats:
    units: int = 0
    files: int = 0
    bytes: int = 0


@dataclass
class DuplicateReport:
    identical: BucketStats = field(default_factory=BucketStats)
    versions: BucketStats = field(default_factory=BucketStats)
    upgrading_units: int = 0
    keep_old_items: int = 0
    total_items: int = 0
    items: list[DupItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------


def quality_label_of(row: LibraryFile) -> str:
    """版本签名里的质量标签：「分辨率 片源」，HDR 有值时追加——HDR 与 SDR 是不同版本。"""
    parts = [p for p in (row.resolution, row.media_source) if p]
    label = " ".join(parts) or "未知规格"
    return f"{label} {row.hdr}" if row.hdr else label


def _same_content(a: LibraryFile, b: LibraryFile, inode: dict[int, tuple[int, int] | None]) -> bool:
    ia, ib = inode.get(a.id or -1), inode.get(b.id or -1)
    if ia is not None and ia == ib:
        return True
    if a.size_bytes != b.size_bytes or not a.size_bytes:
        return False
    if a.duration_seconds is None or b.duration_seconds is None:
        return False
    return abs(a.duration_seconds - b.duration_seconds) <= _DURATION_TOLERANCE


def _bucket_of(rows: list[LibraryFile], inode: dict[int, tuple[int, int] | None]) -> Bucket:
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if not _same_content(a, b, inode):
                return "versions"
    return "identical"


def _stat_many(paths: list[str]) -> dict[str, tuple[int, int] | None]:
    out: dict[str, tuple[int, int] | None] = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = (st.st_dev, st.st_ino)
        except OSError:
            out[p] = None
    return out


def _rank(f: DupFile, spec: RuleSetSpec, vectors: dict[int, tuple[int | None, ...]]) -> tuple:
    """建议保留的排序键（大者优先）：档位阶梯 > 实测码率 > 来源可追溯 > 命名规范 > id。
    阶梯里比不出来的位（None）只影响排序不影响判定，按最低处理。"""
    vec = tuple(-1 if v is None else v for v in vectors[f.row.id or -1])
    return (
        vec,
        f.row.bit_rate or 0,
        _ORIGIN_PRIORITY.get(str(f.origin.get("kind")), 0),
        0 if _VERSION_SUFFIX.search(PurePath(f.row.file_path).stem) else 1,
        f.row.id or 0,
    )


def _suggest(files: list[DupFile], spec: RuleSetSpec) -> None:
    """给单元贴「建议保留」并说明依据。它只是建议：用户点哪个「留这个」就留哪个。"""
    from movieclaw_api.services.subscription.upgrade import snapshot_from_file

    vectors = {f.row.id or -1: ladder_vector(snapshot_from_file(f.row, None), spec) for f in files}
    ordered = sorted(files, key=lambda f: _rank(f, spec, vectors), reverse=True)
    best = ordered[0]
    best_vec = vectors[best.row.id or -1]
    verdicts = [compare_ladder(best_vec, vectors[o.row.id or -1]) for o in ordered[1:]]
    if any(v is None for v in verdicts):
        reason = "档位无法比较，按实测码率建议"
    elif all(v == 1 for v in verdicts):
        reason = "档位最高"
    else:
        tied = [o for o, v in zip(ordered[1:], verdicts, strict=True) if v == 0]
        if all((best.row.bit_rate or 0) > (o.row.bit_rate or 0) for o in tied):
            reason = "同档，实测码率更高"
        elif all(
            _ORIGIN_PRIORITY.get(str(best.origin.get("kind")), 0)
            > _ORIGIN_PRIORITY.get(str(o.origin.get("kind")), 0)
            for o in tied
        ):
            reason = "同档，来源可追溯"
        elif not _VERSION_SUFFIX.search(PurePath(best.row.file_path).stem) and all(
            _VERSION_SUFFIX.search(PurePath(o.row.file_path).stem) for o in tied
        ):
            reason = "同档，占着标准文件名"
        else:
            reason = "同档，最近入库"
    for f in files:
        f.suggested = f is best
        f.suggest_reason = reason if f is best else None


def _seasons_of(units: list[DupUnit], is_tv: bool) -> list[DupSeason]:
    """按季折叠：同构（各集版本签名集合一致，允许某版本缺几集）的季出版本行。

    归堆按集：一季各集都一模一样才整季在「一模一样」堆；两种都有时两堆各出现
    一次，各带自己的集。电影恰好一季一集，永远不同构（直接列文件）。
    """
    by_key: dict[tuple[int, Bucket], list[DupUnit]] = defaultdict(list)
    for u in units:
        by_key[(u.season_number, u.bucket)].append(u)
    seasons: list[DupSeason] = []
    for (season_number, bucket), members in sorted(by_key.items()):
        members.sort(key=lambda u: u.episode_number)
        versions: list[DupVersion] = []
        uniform = False
        if is_tv and len(members) >= 2:
            groups: dict[str, list[tuple[DupUnit, DupFile]]] = defaultdict(list)
            for u in members:
                for f in u.files:
                    if not f.kept:
                        groups[f.version_key].append((u, f))
            # 同构判据：至少两个版本，且每个版本都覆盖这一季至少一半的重复集
            if len(groups) >= 2 and all(
                len({u.episode_number for u, _ in g}) * 2 >= len(members) for g in groups.values()
            ):
                uniform = True
                best_key = max(groups, key=lambda k: sum(1 for _, f in groups[k] if f.suggested))
                for key, pairs in groups.items():
                    sample = pairs[0][1]
                    versions.append(
                        DupVersion(
                            key=key,
                            quality_label=sample.quality_label,
                            origin_label=str(sample.origin.get("label") or ""),
                            episodes=sorted({u.episode_number for u, _ in pairs}),
                            bytes=sum(f.row.size_bytes for _, f in pairs),
                            suggested=key == best_key,
                        )
                    )
                versions.sort(key=lambda v: (not v.suggested, v.quality_label))
        seasons.append(
            DupSeason(
                season_number=season_number,
                bucket=bucket,
                uniform=uniform,
                versions=versions,
                units=members,
            )
        )
    return seasons


async def _rule_specs(
    session: AsyncSession, item_ids: set[int]
) -> dict[int, tuple[RuleSetSpec, bool]]:
    """{media_item_id: (阶梯 spec, 是否「保留共存」)}——只有订阅了的条目有。"""
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(Subscription.media_item_id, RuleSet.spec)
            .join(RuleSet, RuleSet.id == Subscription.rule_set_id)  # type: ignore[arg-type]
            .where(Subscription.media_item_id.in_(item_ids))  # type: ignore[union-attr]
        )
    ).all()
    out: dict[int, tuple[RuleSetSpec, bool]] = {}
    for item_id, spec_json in rows:
        try:
            spec = RuleSetSpec.model_validate(spec_json or {})
        except ValueError:
            spec = _NEUTRAL_SPEC
        out[int(item_id)] = (spec, bool(spec.upgrade_keep_old))
    return out


async def _in_flight_units(session: AsyncSession, item_ids: set[int]) -> set[tuple[int, int, int]]:
    """洗版验证在途的 (media_item_id, season, episode)：新版本刚入库、验证还没裁决。"""
    if not item_ids:
        return set()
    rows = (
        await session.execute(
            select(Subscription.media_item_id, SubscriptionDownloadAttempt.units)
            .join(
                Subscription,
                Subscription.id == SubscriptionDownloadAttempt.subscription_id,  # type: ignore[arg-type]
            )
            .where(
                Subscription.media_item_id.in_(item_ids),  # type: ignore[union-attr]
                SubscriptionDownloadAttempt.purpose == "upgrade",
                SubscriptionDownloadAttempt.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
            )
        )
    ).all()
    out: set[tuple[int, int, int]] = set()
    for item_id, units in rows:
        for u in units or []:
            if isinstance(u, list) and len(u) == 2:
                out.add((int(item_id), int(u[0]), int(u[1])))
    return out


def _candidate_rows_stmt():
    return select(LibraryFile).where(
        LibraryFile.state == FileState.IN_PLACE,
        LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
        LibraryFile.unidentified_code.is_(None),  # type: ignore[union-attr]
        LibraryFile.ignored_at.is_(None),  # type: ignore[union-attr]
    )


async def detect_duplicates(
    session: AsyncSession,
    *,
    library_id: int | None = None,
    media_item_id: int | None = None,
    q: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    limit: int | None = 20,
    offset: int = 0,
) -> DuplicateReport:
    """圈出全部多文件单元，分堆、贴建议、按季折叠；按条目分页。

    ``limit=None`` 取全部（批量清理用）。摘要计数（两堆的单元 / 文件 / 字节）
    永远按全部筛选算，与页面上的批量按钮是同一份数字。
    """
    # 1. 多文件单元：一条聚合查询
    unit_stmt = (
        select(
            LibraryFile.media_item_id,
            LibraryFile.season_number,
            LibraryFile.episode_number,
        )
        .where(
            LibraryFile.state == FileState.IN_PLACE,
            LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
            LibraryFile.unidentified_code.is_(None),  # type: ignore[union-attr]
            LibraryFile.ignored_at.is_(None),  # type: ignore[union-attr]
        )
        .group_by(LibraryFile.media_item_id, LibraryFile.season_number, LibraryFile.episode_number)
        .having(func.count(LibraryFile.id) >= 2)
    )
    if library_id is not None:
        unit_stmt = unit_stmt.where(LibraryFile.library_id == library_id)
    if media_item_id is not None:
        unit_stmt = unit_stmt.where(LibraryFile.media_item_id == media_item_id)
    if season_number is not None:
        unit_stmt = unit_stmt.where(LibraryFile.season_number == season_number)
    if episode_number is not None:
        unit_stmt = unit_stmt.where(LibraryFile.episode_number == episode_number)
    if q:
        needle = f"%{q.strip()}%"
        unit_stmt = unit_stmt.join(MediaItem, MediaItem.id == LibraryFile.media_item_id).where(
            or_(
                MediaItem.title.ilike(needle),  # type: ignore[union-attr]
                MediaItem.original_title.ilike(needle),  # type: ignore[union-attr]
                LibraryFile.file_path.ilike(needle),  # type: ignore[union-attr]
            )
        )
    unit_keys = {(int(m), int(s), int(e)) for m, s, e in (await session.execute(unit_stmt)).all()}
    report = DuplicateReport()
    if not unit_keys:
        return report
    item_ids = {k[0] for k in unit_keys}

    # 2. 这些单元的全部在位行（一次取，按单元分桶）
    rows_stmt = _candidate_rows_stmt().where(LibraryFile.media_item_id.in_(item_ids))  # type: ignore[union-attr]
    if library_id is not None:
        rows_stmt = rows_stmt.where(LibraryFile.library_id == library_id)
    rows = list((await session.execute(rows_stmt)).scalars())
    by_unit: dict[tuple[int, int, int], list[LibraryFile]] = defaultdict(list)
    for r in rows:
        key = (int(r.media_item_id or 0), r.season_number, r.episode_number)
        if key in unit_keys:
            by_unit[key].append(r)

    # 3. 放行：保留共存 / 洗版在途 / 全部「都留着」
    specs = await _rule_specs(session, item_ids)
    keep_old_items = {i for i, (_, keep_old) in specs.items() if keep_old}
    in_flight = await _in_flight_units(session, item_ids)
    report.keep_old_items = len(keep_old_items & item_ids)
    listed: dict[tuple[int, int, int], list[LibraryFile]] = {}
    for key, unit_rows in by_unit.items():
        if key[0] in keep_old_items:
            continue
        if key in in_flight or (key[1], key[2]) == (0, 0) and (key[0], 0, 0) in in_flight:
            report.upgrading_units += 1
            continue
        if all(r.kept_at is not None for r in unit_rows):
            continue
        listed[key] = sorted(unit_rows, key=lambda r: r.id or 0)
    if not listed:
        return report

    # 4. 物理指纹（只对这些文件 stat 一次）+ 来源快照 + 建议保留
    flat = [r for unit_rows in listed.values() for r in unit_rows]
    stats = await asyncio.to_thread(_stat_many, [r.file_path for r in flat])
    inode = {r.id or -1: stats.get(r.file_path) for r in flat}
    origins = await derive_origins(session, flat)
    units_by_item: dict[int, list[DupUnit]] = defaultdict(list)
    for key, unit_rows in listed.items():
        files = [
            DupFile(
                row=r,
                quality_label=quality_label_of(r),
                origin=origin_of(r, origins),
                version_key="",
            )
            for r in unit_rows
        ]
        for f in files:
            f.version_key = f"{f.quality_label}|{f.origin.get('label') or ''}"
        spec = specs.get(key[0], (_NEUTRAL_SPEC, False))[0]
        _suggest(files, spec)
        units_by_item[key[0]].append(
            DupUnit(
                season_number=key[1],
                episode_number=key[2],
                bucket=_bucket_of(unit_rows, inode),
                files=files,
            )
        )

    # 5. 条目 + 库，按季折叠，分页
    items = {
        i.id: i
        for i in (
            await session.execute(select(MediaItem).where(MediaItem.id.in_(units_by_item)))  # type: ignore[union-attr]
        ).scalars()
    }
    lib_ids = {r.library_id for r in flat}
    libraries = {
        lib.id: lib
        for lib in (
            await session.execute(select(Library).where(Library.id.in_(lib_ids)))  # type: ignore[union-attr]
        ).scalars()
    }
    dup_items: list[DupItem] = []
    for item_id, units in units_by_item.items():
        item = items.get(item_id)
        if item is None:
            continue
        seasons = _seasons_of(units, item.kind == "tv")
        # 一个条目的文件理论上同库；跨库重叠配置下取第一份即可（展示用）
        library = libraries.get(units[0].files[0].row.library_id)
        if library is None:
            continue
        dup_items.append(DupItem(library=library, item=item, seasons=seasons))
        for s in seasons:
            stats_ = report.identical if s.bucket == "identical" else report.versions
            stats_.units += len(s.units)
            extras = s.extras
            stats_.files += len(extras)
            stats_.bytes += sum(f.row.size_bytes for f in extras)
    dup_items.sort(key=lambda d: (d.library.name, d.item.title, d.item.id or 0))
    report.total_items = len(dup_items)
    report.items = dup_items[offset : offset + limit] if limit is not None else dup_items
    return report


# ---------------------------------------------------------------------------
# 清理：留这个 / 整季留这个版本 / 都留着 / 整堆按建议
# ---------------------------------------------------------------------------


@dataclass
class ResolveOutcome:
    done: int = 0
    failed: list[tuple[int, str, str]] = field(default_factory=list)  # (id, file_name, error)
    library_ids: set[int] = field(default_factory=set)
    remaining: int = 0


def _note(keep: DupFile, gone: DupFile, bucket: Bucket) -> str:
    if bucket == "identical":
        return f"重复清理：与「{keep.file_name}」一模一样，已保留后者"
    return (
        f"重复清理：留下「{keep.file_name}」（{keep.quality_label}），"
        f"移除本版本（{gone.quality_label}）"
    )


async def _recycle_extras(
    session: AsyncSession,
    unit: DupUnit,
    keep: DupFile,
    trigger: dict[str, Any],
    outcome: ResolveOutcome,
    *,
    include_kept: bool,
) -> None:
    for f in unit.files:
        if f is keep or (f.kept and not include_kept):
            continue
        row = f.row
        outcome.library_ids.add(row.library_id)
        try:
            result = await recycle_file(
                session,
                row,
                reason=REASON_DUPLICATE,
                trigger=trigger,
                note=_note(keep, f, unit.bucket),
            )
            if result == "already_gone":
                await session.delete(row)
            await session.commit()
            outcome.done += 1
        except Exception as exc:  # noqa: BLE001 -- 单个失败不回滚已成功的
            await session.rollback()
            logger.warning("重复清理移入回收站失败：%s", row.file_path, exc_info=True)
            outcome.failed.append((row.id or 0, f.file_name, f"移入回收站失败：{exc}"))


async def resolve_unit(
    session: AsyncSession,
    *,
    media_item_id: int,
    season_number: int,
    episode_number: int | None,
    keep: int | str,
    trigger: dict[str, Any],
) -> ResolveOutcome:
    """一个单元 / 一季的决定。

    ``keep``：文件 id = 这一集留这个（其余进回收站，含此前「都留着」过的——用户
    这次说了只留一个）；版本 key = 整季留这个版本（缺该版本的集留建议保留者）；
    ``"all"`` = 都留着（盖 ``kept_at``，不动文件）。

    决定前**重新检测**这一季：列表是几分钟前算的，中间可能跑了扫描或洗版——
    文件集合变了，指定的文件 / 版本找不到就整体拒绝，不按过期结论删。
    """
    report = await detect_duplicates(
        session,
        media_item_id=media_item_id,
        season_number=season_number,
        episode_number=episode_number,
        limit=None,
    )
    outcome = ResolveOutcome()
    units = [u for d in report.items for s in d.seasons for u in s.units]
    if not units:
        raise LookupError("这个单元已经没有重复文件（可能刚被扫描或洗版改动过），请刷新列表")

    if keep == "all":
        now = utcnow()
        for u in units:
            for f in u.files:
                f.row.kept_at = now
                outcome.library_ids.add(f.row.library_id)
                outcome.done += 1
        await session.commit()
        return outcome

    if isinstance(keep, int):
        if episode_number is None and len(units) != 1:
            raise ValueError("按文件「留这个」必须指定到集")
        unit = units[0]
        target = next((f for f in unit.files if f.row.id == keep), None)
        if target is None:
            raise LookupError("要保留的文件已不在这个单元里（文件集合已变化），请刷新列表")
        await _recycle_extras(session, unit, target, trigger, outcome, include_kept=True)
        return outcome

    # 整季留某个版本
    if not any(f.version_key == keep for u in units for f in u.files):
        raise LookupError("这一季里已经没有这个版本（文件集合已变化），请刷新列表")
    for u in units:
        target = next((f for f in u.files if f.version_key == keep), None) or u.suggested
        await _recycle_extras(session, u, target, trigger, outcome, include_kept=False)
    return outcome


async def resolve_all(
    session: AsyncSession,
    *,
    bucket: Bucket,
    library_id: int | None,
    trigger: dict[str, Any],
    batch_limit: int,
) -> ResolveOutcome:
    """整堆按「建议保留」清理，一次最多 ``batch_limit`` 个文件，超出的返回 remaining。"""
    report = await detect_duplicates(session, library_id=library_id, limit=None)
    outcome = ResolveOutcome()
    todo: list[DupUnit] = [
        u for d in report.items for s in d.seasons if s.bucket == bucket for u in s.units
    ]
    budget = batch_limit
    for u in todo:
        extras = u.extras
        if not extras:
            continue
        if budget <= 0:
            outcome.remaining += len(extras)
            continue
        if len(extras) > budget:
            outcome.remaining += len(extras) - budget
            # 单元内部分清理会让它下次以更少的文件再出现，语义仍然正确
            trimmed = DupUnit(
                u.season_number, u.episode_number, u.bucket, [u.suggested, *extras[:budget]]
            )
            await _recycle_extras(
                session, trimmed, u.suggested, trigger, outcome, include_kept=False
            )
            budget = 0
            continue
        await _recycle_extras(session, u, u.suggested, trigger, outcome, include_kept=False)
        budget -= len(extras)
    return outcome
