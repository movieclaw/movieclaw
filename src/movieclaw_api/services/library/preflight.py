"""批量搬运的执行前预检：一次把「将要发生什么」全部摆出来。

为什么单条目的那份预览不够用：搬一部片子出问题，用户当场看到、当场处理；
搬 593 部要跑几个小时，用户不在现场。**搬到一半才发现盘不够是灾难**，所以
正式开始前必须一次算清四件事，而且要快。

四件事（docs/design/library-bulk-relocate.md §4.1）：

1. **目标盘空间够不够——只按跨盘部分算**。同盘搬运是一次 rename，不占任何
   新空间；把总体积拿去和剩余空间比会把一次本来零风险的同盘归并吓停。
2. **硬链接的代价，分同盘/跨盘说**。PT 用户的库大量是「下载目录 + 媒体库
   硬链接」形态：同盘 rename 不碰 inode 与链接数，硬链接完整保留、零额外
   空间；跨盘则必断，复制出新 inode 之后下载目录仍指着旧的，所以「删源」
   只删掉一个链接、**源盘一字节都不释放**，而目标盘要吃下全量。做种本身
   不受影响（下载目录路径没动），代价纯粹是空间翻倍。
3. **同名冲突逐条列出，且不阻断整批**。这是从单条目泛化到集合时的语义变化
   点：单条目的「目标已有同名目录」等于整个操作失败，批量里只是这一条跳过。
   而「同名」本身还要再分三类，判据是**台账的锚**而不是目录名（§7.1）。
4. **同盘还是跨盘，必须用真实 rename 探针判定**（见 ``probe_same_mount``）。

性能定线：预检是 **O(成员数)，不是 O(文件数)**。593 部 × 每部几十个文件
≈ 几万次 stat，网络挂载上就是几分钟，同步请求扛不住。因此这里只做「一次
台账查询 + 每个条目目录一次 exists + 每对根一次 rename 探针 + 一次
disk_usage + 跨盘成员的视频主文件各一次 stat」，逐文件的精确计划留到执行时
**逐成员现算**（那本来就是执行侧的设计）。
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import select

from movieclaw_api.services.library.layout import VIDEO_EXTS, entry_dir_of
from movieclaw_api.services.library.profile import profile_of
from movieclaw_db.models import FileState, Library, LibraryFile

logger = logging.getLogger("movieclaw_api.library_preflight")

# 目标盘要留的余量：搬运期间下载器仍在往盘里写，卡着底线开工必然中途 ENOSPC。
# 按比例 + 固定下限取大者——纯比例对小库太松（100GB 的库只留 5GB），
# 纯固定值对大库又太紧。
_HEADROOM_RATIO = 0.05
_HEADROOM_FLOOR_BYTES = 10 * 1024**3

# 一次显式提交的成员上限：再多就该用「整库」，否则请求体与预检时间都失控。
# 放在这里（而不是批量服务里）是为了让请求体校验与执行侧共用同一个数——
# 两处各写一个 2000，迟早只改一处。
MAX_SELECTION = 2000

# 冲突分类（§7.1）：判据是目标目录里的台账行挂在哪个条目上，不是目录名。
CONFLICT_SAME_ANCHOR = "same_anchor"  # 同一部作品的其他版本 → 允许合并
CONFLICT_DIFFERENT_ANCHOR = "different_anchor"  # 目录撞名，不是同一部片 → 只能跳过
CONFLICT_UNKNOWN = "unknown"  # 目标目录没有台账行，身份不明 → 只能跳过


# 冲突分类 → 给人看的一句话。枚举名是给机器的，不能直接抛到用户面前。
CONFLICT_LABELS = {
    CONFLICT_SAME_ANCHOR: "目标位置已有这部作品的其他版本",
    CONFLICT_DIFFERENT_ANCHOR: "目标位置被另一部作品占着（只是目录重名）",
    CONFLICT_UNKNOWN: "目标位置已有内容，但媒体库里没有它的记录（手工放入或正在下载？）",
}


@dataclass
class MemberPreflight:
    """单个成员的预检结论。"""

    media_item_id: int
    title: str
    # 该成员会落到目标根下的哪些路径（只到条目目录/文件这一层，不逐文件展开）
    target_paths: list[str] = field(default_factory=list)
    size_bytes: int = 0
    cross_device: bool = False
    # 同名冲突的分类；None 表示不冲突
    conflict: str | None = None
    conflict_path: str = ""
    # 跨盘后会断开、且不释放源盘空间的字节数（同盘搬运恒为 0）
    hardlinked_bytes: int = 0
    # 下载器里有同名落盘根——多半是「下载器直接做种库内路径」的部署，
    # 搬走会让做种任务找不到文件（同盘 rename 也一样，路径变了）
    seeding_in_place: bool = False
    # 台账里没有可搬内容（全是缺失行/全在库根之外）
    reason: str = ""


@dataclass
class Preflight:
    """一次批量搬运的完整预检。``blocked`` 非空时执行接口直接拒绝。"""

    target_root: str
    selected: int = 0
    movable: int = 0
    total_bytes: int = 0
    # 逐成员明细（前端与 CLI 都从这里渲染，不另算一遍）
    members: list[MemberPreflight] = field(default_factory=list)

    # --- 空间（只算跨盘部分）---
    cross_device_items: int = 0
    cross_device_bytes: int = 0
    target_free_bytes: int = 0
    target_required_bytes: int = 0
    source_reclaimable_bytes: int = 0

    # --- 硬链接（只统计跨盘成员）---
    hardlinked_items: int = 0
    hardlinked_bytes: int = 0

    # --- 做种检测；None = 下载器不可达，无法确认（不阻断）---
    seeding_in_place_items: int | None = 0

    # --- 冲突分类计数 ---
    conflicts: dict[str, int] = field(default_factory=dict)

    # 整批阻断：目标根不可访问、空间不足、库正忙
    blocked: list[str] = field(default_factory=list)

    @property
    def mergeable_conflicts(self) -> int:
        """可以靠 --on-conflict merge 并进去的冲突数（其余任何策略都跳过）。"""
        return self.conflicts.get(CONFLICT_SAME_ANCHOR, 0)


def probe_same_mount(source_dir: Path, target_dir: Path) -> bool:
    """两个目录之间能否一次 rename 完成——用**真实探针**判，不看 st_dev。

    为什么不能只比 st_dev（这是本模块最容易被写错的一处）：Linux 的
    ``rename(2)`` 在**挂载点不同**时就返回 EXDEV（``do_renameat2`` 里
    ``old_path.mnt != new_path.mnt`` 的显式判断），而 **bind mount 共用同一个
    superblock、st_dev 完全相同**。一库一个挂载恰恰是合库场景的主流部署：

        -v /mnt/disk1/movies:/media/movies      # 库 A
        -v /mnt/disk1/tv-shows:/media/tv        # 库 B  ← 同一块物理盘

    只比 st_dev 会告诉用户「同盘、秒完成、不需要额外空间」，实际 rename 会
    EXDEV 失败、退化成复制 30T——预检的全部价值就在于不撒谎。

    探针顺带把「目标根可写吗、是不是只读挂载」一起验了：建不出临时文件就
    判不同挂载（保守），执行时真正的分支仍以 rename 的 errno 为准。
    """
    token = uuid.uuid4().hex[:12]
    probe = source_dir / f".movieclaw-probe-{token}"
    landed = target_dir / f".movieclaw-probe-{token}"
    try:
        probe.touch()
    except OSError:
        logger.debug("同挂载探针无法在源目录建临时文件，保守判为跨盘：%s", source_dir)
        return False
    try:
        os.rename(probe, landed)
    except OSError as exc:
        probe.unlink(missing_ok=True)
        if exc.errno != errno.EXDEV:
            logger.debug("同挂载探针失败（%s），保守判为跨盘：%s", exc.strerror, target_dir)
        return False
    finally:
        landed.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)
    return True


def headroom_for(cross_device_bytes: int) -> int:
    """跨盘复制需要目标盘留出的总空间（含余量）。"""
    if cross_device_bytes <= 0:
        return 0
    headroom = max(int(cross_device_bytes * _HEADROOM_RATIO), _HEADROOM_FLOOR_BYTES)
    return cross_device_bytes + headroom


async def build_preflight(
    session,
    source: Library,
    target_root: Path,
    members: list[tuple[int, str]],
    *,
    seeding_names: set[str] | None = None,
    on_conflict: str = "skip",
) -> Preflight:
    """算出一次批量搬运的预检。只读磁盘与台账，不做任何写入。

    ``members`` 是**已经冻结**的成员集合（条目 id + 标题）；筛选与全选在调用
    方完成，这里不认识筛选这回事。``seeding_names`` 是下载器当前的落盘根名
    集合，``None`` 表示下载器不可达——那一栏如实报「无法确认」而不是报 0。

    ``on_conflict`` 必须与执行时传的是同一个值：预检按该策略算出的影响面，
    就是执行会做的事。``merge`` 下同锚冲突（同一部作品的其他版本）算作可搬，
    ``skip``/``fail`` 下算作跳过——同一份成员清单在两种策略下的 ``movable``
    本来就不一样，糊成一个数会让调用方按错的数去判断要不要执行。
    """
    assert source.id is not None
    result = Preflight(target_root=str(target_root), selected=len(members))
    if not target_root.is_dir():
        result.blocked.append(f"目标根路径不可访问：{target_root}（盘未挂载？）")
        return result

    member_ids = [mid for mid, _ in members]
    titles = dict(members)
    roots = [Path(p) for p in source.root_paths]
    file_entries = not profile_of(source).scraped

    # 一次查完所有成员的在位台账行；缺失/待回收的行没有磁盘实体，不参与
    # 空间与冲突计算（它们只做逻辑随迁）
    rows = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.library_id == source.id,
                    LibraryFile.media_item_id.in_(member_ids),  # type: ignore[union-attr]
                    LibraryFile.state == FileState.IN_PLACE,
                )
            )
        )
        .scalars()
        .all()
    )

    # 目标库/目标根下已有内容的锚归属：conflict 分类靠它（§7.1）
    owners = await _target_anchor_owners(session, target_root)

    return await asyncio.to_thread(
        _build_sync,
        result,
        rows,
        titles,
        roots,
        target_root,
        file_entries,
        owners,
        seeding_names,
        on_conflict,
    )


async def _target_anchor_owners(session, target_root: Path) -> dict[str, set[int]]:
    """目标根下每个条目目录/文件路径 → 占着它的条目 id 集合。

    这是把「同名」升级成「同锚」判据的全部数据来源，成本是一次查询：不查它
    就只能按目录名判冲突，而目录名分不清「同一部片的另一个版本」和「碰巧
    重名的另一部片」，这两者的正确处理完全相反。
    """
    rows = (
        await session.execute(
            select(LibraryFile.file_path, LibraryFile.media_item_id).where(
                LibraryFile.file_path.startswith(str(target_root)),  # type: ignore[union-attr]
                LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
            )
        )
    ).all()
    owners: dict[str, set[int]] = {}
    for file_path, media_item_id in rows:
        path = Path(file_path)
        entry = entry_dir_of([target_root], path)
        for key in {str(entry) if entry else None, str(path)}:
            if key:
                owners.setdefault(key, set()).add(int(media_item_id))
    return owners


def _build_sync(
    result: Preflight,
    rows: list[LibraryFile],
    titles: dict[int, str],
    roots: list[Path],
    target_root: Path,
    file_entries: bool,
    owners: dict[str, set[int]],
    seeding_names: set[str] | None,
    on_conflict: str,
) -> Preflight:
    by_member: dict[int, list[LibraryFile]] = {}
    for row in rows:
        if row.media_item_id is not None:
            by_member.setdefault(int(row.media_item_id), []).append(row)

    # 每对（源根，目标根）只探一次：探针有文件系统开销，不能按成员做
    same_mount: dict[Path, bool] = {}

    def _is_cross_device(source_dir: Path) -> bool:
        root = next((r for r in roots if source_dir == r or r in source_dir.parents), None)
        key = root or source_dir
        if key not in same_mount:
            # 源目录不可用时保守判跨盘：拿目标根自己探自己必然为真，
            # 会把「其实要复制 30T」说成「同盘秒完成」
            same_mount[key] = key.is_dir() and probe_same_mount(key, target_root)
        return not same_mount[key]

    taken: set[str] = set()
    for media_item_id, title in titles.items():
        member = MemberPreflight(media_item_id=media_item_id, title=title)
        result.members.append(member)
        member_rows = by_member.get(media_item_id, [])
        if not member_rows:
            member.reason = "台账里没有在位文件（可能全部缺失或已入回收站），只随迁归属"
            continue

        units = _target_units(member_rows, roots, target_root, file_entries)
        if not units:
            member.reason = "文件不在源库的根路径之内（根路径可能已变更）"
            continue

        member.size_bytes = sum(r.size_bytes for r in member_rows)
        member.cross_device = _is_cross_device(Path(member_rows[0].file_path).parent)

        for target_path in units:
            member.target_paths.append(str(target_path))
            key = str(target_path)
            if key in taken or target_path.exists():
                member.conflict = _classify_conflict(key, media_item_id, owners)
                member.conflict_path = key
                break
            taken.add(key)

        if member.cross_device:
            member.hardlinked_bytes = _hardlinked_bytes(member_rows)

        if seeding_names is not None:
            member.seeding_in_place = any(
                Path(p).name in seeding_names for p in member.target_paths
            )

    _summarize(result, target_root, seeding_names, on_conflict)
    return result


def _target_units(
    rows: list[LibraryFile],
    roots: list[Path],
    target_root: Path,
    file_entries: bool,
) -> list[Path]:
    """成员会落到目标根下的哪些路径，只到「搬运单元」这一层。

    与执行侧 ``build_transfer_plan`` 的落点规则保持一致：影视库搬条目目录，
    一文件一条目的本地内容库搬文件并保留相对库根的结构。这里刻意**不**逐文件
    展开目录——预检要的是 O(成员数)，精确计划在执行时现算。
    """
    units: list[Path] = []
    seen: set[Path] = set()
    for row in rows:
        path = Path(row.file_path)
        root = next((r for r in roots if path == r or r in path.parents), None)
        if root is None:
            continue
        if file_entries:
            units.append(target_root / path.relative_to(root))
            continue
        entry = entry_dir_of(roots, path)
        if entry is None:
            # 直接躺在库根下的裸文件（含原盘目录本身就是条目的形态）
            entry = path
        if entry in seen:
            continue
        seen.add(entry)
        units.append(target_root / entry.name)
    return units


def _classify_conflict(target_path: str, media_item_id: int, owners: dict[str, set[int]]) -> str:
    """把一次「目标已被占用」分成三类——处理方式完全不同（§7.1）。"""
    holders = owners.get(target_path)
    if not holders:
        # 目标位置有东西，但库里没有它的台账行：用户手放的、正在下载的、
        # 还没扫过的。身份不明，任何策略下都不碰。
        return CONFLICT_UNKNOWN
    if holders == {media_item_id}:
        return CONFLICT_SAME_ANCHOR
    return CONFLICT_DIFFERENT_ANCHOR


def _hardlinked_bytes(rows: list[LibraryFile]) -> int:
    """跨盘搬运后会断开、且不会让源盘释放空间的字节数。

    只看视频主文件：字幕/NFO 的链接数对空间账毫无意义，而它们的数量是视频的
    好几倍——逐个 stat 会把预检的成本从 O(成员数) 拖成 O(文件数)。
    """
    total = 0
    for row in rows:
        path = Path(row.file_path)
        if path.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            if path.stat().st_nlink > 1:
                total += row.size_bytes
        except OSError:
            continue
    return total


def _summarize(
    result: Preflight,
    target_root: Path,
    seeding_names: set[str] | None,
    on_conflict: str,
) -> None:
    """把逐成员明细汇总成用户一眼要看的那几个数。"""
    seeding = 0
    merging = on_conflict == "merge"
    for member in result.members:
        if member.conflict is not None:
            result.conflicts[member.conflict] = result.conflicts.get(member.conflict, 0) + 1
            # merge 下同锚冲突是「并进去」，仍然算可搬；异锚与无主任何策略下都跳过
            if not (merging and member.conflict == CONFLICT_SAME_ANCHOR):
                continue
        elif member.reason:
            continue
        result.movable += 1
        result.total_bytes += member.size_bytes
        if member.cross_device:
            result.cross_device_items += 1
            result.cross_device_bytes += member.size_bytes
            result.hardlinked_bytes += member.hardlinked_bytes
            if member.hardlinked_bytes:
                result.hardlinked_items += 1
        if member.seeding_in_place:
            seeding += 1

    result.seeding_in_place_items = None if seeding_names is None else seeding
    # 源盘预计释放：同盘搬运不涉及释放（就是改个名），跨盘则要扣掉硬链文件
    # ——那部分下载目录还引用着，删掉库里这一个链接不会让盘腾出一个字节。
    # 全硬链接库跨盘搬时这个数是 0，正是这个字段存在的理由。
    result.source_reclaimable_bytes = max(0, result.cross_device_bytes - result.hardlinked_bytes)
    result.target_required_bytes = headroom_for(result.cross_device_bytes)
    try:
        result.target_free_bytes = shutil.disk_usage(target_root).free
    except OSError:
        result.target_free_bytes = 0
    if result.target_required_bytes > result.target_free_bytes:
        result.blocked.append(
            f"目标盘剩余空间不足：需要 {_gib(result.target_required_bytes)}"
            f"（含余量），实际剩余 {_gib(result.target_free_bytes)}"
        )


def _gib(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"
