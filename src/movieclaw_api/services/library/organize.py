"""整理器（存量规范化）：把库里已识别的文件按刮削结果批量重命名归位。

存量扫描（library_scan）只识别落账、绝不动磁盘，因此部署前就存在的文件
会一直保持原来杂乱的名字。本模块补上"让库变规整"的主动能力：

  台账在位文件 → 计算规范目标路径（与入库管线同一套命名模板）
    ``{所在根}/{标题 (年份)}[/Season NN]/{标题 (年份)[ - SxxEyy][ - 版本标签]}.ext``
  → 用户在前端预览确认 → 逐文件改名 + 台账路径随迁 → 清理搬空的目录

设计决策：
- **预览与执行分离**：``build_organize_plan`` 是纯计算（只读磁盘不写），
  预览接口直接返回完整清单；执行时**重新计算**计划——预览到确认之间
  磁盘可能已变化，执行永远以最新状态为准；
- **防覆盖改名**：``os.rename`` 会静默覆盖已存在的目标，这里用
  ``fsops.rename_no_replace``（renameat2 RENAME_NOREPLACE）——目标已存在
  会原子失败（EEXIST），堵死与入库管线并发写入同一规范名的覆盖窗口；
  且 rename 会被极空间等 FUSE 存储的索引感知（旧实现的 os.link 不会，
  改名后文件会从极影视里消失，见 fsops 模块头）；
- **逐文件收口**：每改名成功一个立即随迁台账（repo.relocate），中途失败
  不会留下"账实不符"的批量烂摊子，单文件失败记入 errors 不断整轮；
- **只清理自己搬空的目录**：改名后仅对被搬走文件的原目录（及其空祖先）
  尝试 rmdir——非空即停，绝不触碰与本次整理无关的目录，绝不删除文件；
- **条目目录改名时镜像资产一起搬**：``poster.jpg`` / ``fanart.jpg`` /
  ``seasonNN-poster.jpg`` / ``movie.nfo`` / ``tvshow.nfo`` 这些镜像产物不以
  主文件名开头，附属文件规则认不出它们。不搬的话旧目录永远非空、清不掉，
  用户每调一次命名模板就多留一层只剩图片的空壳目录——这与"用户会反复调
  模板试效果"的产品预期直接冲突。只在**旧条目目录会被彻底搬空**时搬（还留
  着别的在位视频就得把图留给它们），且只认镜像自己写死的那几个文件名，
  用户放进目录的东西一概不碰。唯一的删除动作：目标已存在且**内容逐字节
  相同**时删掉源头那份重复副本（那是本程序自己写出的、随时可由 data/ 下
  的资产重建的镜像产物，不是用户数据）；内容不同则原样保留并告警；
- **多版本按播放器规范命名**：同条目多个版本（1080p 与 2160p 并存）落
  同一条目目录，文件名加 `` - 版本标签`` 后缀（如 ``标题 (年份) - 2160p.ext``）
  ——Emby / Plex / Jellyfin 都按此约定把它们归组为同一影片的不同版本。
  标签优先取分辨率，撞车时逐级追加片源/发布组，全部探测不到退回按文件
  大小编号（V1/V2…）；标签推导是确定性的，重跑整理不会来回改名。

与其他任务的互斥（仔细评估的结论）：
- **与扫描双向互斥**：扫描的改名归并（_try_relink）用"旧路径消失 + 新路径
  出现"做指纹匹配，与整理的批量改名并发会竞态——扫描可能把整理刚搬走的
  旧路径标 missing、把新路径当新文件重走识别链（人工认领可能丢失）。
  扫描的三个入口（手动路由 / watchdog 去抖 / 6 小时对账）都收敛到
  ``scan_library``，在那里统一用 ``is_organizing`` 挡下；整理开始前同样
  检查 ``is_scanning``。整理产生的 rename 事件会触发 watchdog 的去抖扫描
  并被该守卫挡下——台账在整理中已同步更新，无需扫描补账，漏掉的事件
  由 6 小时对账兜底。
- **与入库管线（下载完成硬链）不加锁**：入库只新建规范命名的文件，与
  整理的冲突面仅剩"同一单元恰好同名"——执行时目标已存在即跳过（防覆盖
  改名保证不清掉入库产物）；为此把下载轮询与整理跨任务加锁，复杂度
  不成比例。
"""

from __future__ import annotations

import asyncio
import errno
import filecmp
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlmodel import select

from movieclaw_api.services import jobs
from movieclaw_api.services.library.config import sanitize_folder_name
from movieclaw_api.services.library.fsops import rename_no_replace
from movieclaw_api.services.library.layout import entry_dir_of
from movieclaw_api.services.library.naming import (
    entry_dir_name_of,
    episode_file_name,
    movie_file_name,
    season_dir_name,
)
from movieclaw_api.services.library.sidecar import SIDECAR_SKIP_EXTS, find_sidecars
from movieclaw_api.services.task_state import TaskState
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileState, Library, LibraryFile, MediaItem, utcnow
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.library_organize")

# 视频类文件（含 strm 占位与原盘 iso）：判断旧条目目录是否已被搬空。
# 与附属文件判定共用同一集合——``library.sidecar`` 是唯一定义处，那边把
# 这些扩展名当"独立版本"排除，这边把它们当"还没搬完"的证据，同一份语义
_VIDEO_LIKE_EXTS = SIDECAR_SKIP_EXTS

# 条目目录级镜像资产的固定文件名（media_scrape.mirror_media_dir_assets 写出）。
# 它们不以主文件名开头，附属文件规则（"主文件名."前缀）认不出来，条目目录
# 改名时必须单独搬——见模块头"条目目录改名时镜像资产一起搬"
_ENTRY_ASSET_NAMES = frozenset({"poster.jpg", "fanart.jpg", "movie.nfo", "tvshow.nfo"})
_SEASON_POSTER_RE = re.compile(r"^season(?:\d{2}|-specials)-poster\.jpg$")


def _is_entry_asset(name: str) -> bool:
    """是不是镜像写出的条目级资产（白名单，用户自己的文件一律不算）。"""
    return name in _ENTRY_ASSET_NAMES or _SEASON_POSTER_RE.match(name) is not None

# 每库单飞互斥 + 实时进度 (已完成, 总数) + 最近一次结论，容器统一为 TaskState
_organize_tasks: TaskState[tuple[int, int]] = TaskState()


def is_organizing(library_id: int) -> bool:
    return _organize_tasks.running(library_id)


def organize_progress(library_id: int) -> tuple[int, int] | None:
    """进行中整理的 (已完成, 总数)；没有整理在跑则为 None。"""
    return _organize_tasks.state_of(library_id)


def last_organize(library_id: int) -> tuple | None:
    """最近一次整理的 (完成时间, OrganizeSummary)；从未整理过则为 None。"""
    return _organize_tasks.last(library_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 计划：纯计算，预览接口与执行共用
# ---------------------------------------------------------------------------


@dataclass
class SidecarMove:
    """跟随主文件改名的附属文件（字幕等）。"""

    source_path: str
    target_path: str


@dataclass
class RenameAction:
    """一个待改名文件的完整计划。"""

    file_id: int
    media_item_id: int
    title: str
    year: int | None
    source_path: str
    target_path: str
    # 相对所在根的路径（前端展示用，绝对路径太长）
    source_rel: str
    target_rel: str
    size_bytes: int
    # 版本标签素材（多版本同名时用来生成 " - 2160p" 这类后缀）
    resolution: str | None = None
    media_source: str | None = None
    release_group: str | None = None
    sidecars: list[SidecarMove] = field(default_factory=list)


@dataclass
class EntryAssetMove:
    """条目目录改名时跟着搬的镜像资产（海报/背景/季海报/条目 NFO）。"""

    source_path: str
    target_path: str


@dataclass
class SkipEntry:
    """不参与整理的文件与中文原因（预览里逐条展示，用户心里有数）。"""

    file_path: str
    reason: str


@dataclass
class OrganizePlan:
    """一次整理的完整计划。"""

    library_id: int
    total: int = 0  # 台账在位文件总数（= 改名 + 已规范 + 跳过）
    already_ok: int = 0  # 已符合规范命名，无需动作
    renames: list[RenameAction] = field(default_factory=list)
    skips: list[SkipEntry] = field(default_factory=list)
    entry_assets: list[EntryAssetMove] = field(default_factory=list)


async def build_organize_plan(session, library: Library) -> OrganizePlan:
    """计算整理计划：只读磁盘与台账，不做任何写入。

    目标路径在文件**当前所在的根**下生成（不跨根移动——扩展根常在
    另一块盘上，跨根改名等于跨盘复制，不是本功能的职责）。
    """
    assert library.id is not None
    result = await session.execute(
        select(LibraryFile, MediaItem)
        .join(MediaItem, LibraryFile.media_item_id == MediaItem.id, isouter=True)  # type: ignore[arg-type]
        .where(LibraryFile.library_id == library.id)
        .order_by(LibraryFile.file_path)
    )
    rows = [(f, item) for f, item in result.all()]
    kind = MediaKind(library.kind)
    roots = [r.rstrip("/") for r in library.root_paths]
    # 磁盘检查（exists/is_dir/附属文件枚举）放线程池：大库上千次 stat 不该阻塞事件循环
    # 带上库本身：命名模板可按库覆盖（library.scrape_overrides）
    return await asyncio.to_thread(_build_plan_sync, library, kind, roots, rows)


def _build_plan_sync(
    library: Library,
    kind: MediaKind,
    roots: list[str],
    rows: list[tuple[LibraryFile, MediaItem | None]],
) -> OrganizePlan:
    plan = OrganizePlan(library_id=library.id)
    candidates: list[RenameAction] = []
    for row, item in rows:
        if row.state != FileState.IN_PLACE:
            continue  # 缺失文件不计入总数也不展示——它不在磁盘上，无从整理
        plan.total += 1
        src = Path(row.file_path)
        if item is None:
            plan.skips.append(SkipEntry(row.file_path, "尚未识别身份，请先在「待识别」里认领"))
            continue
        root = _root_of(roots, row.file_path)
        if root is None:
            plan.skips.append(
                SkipEntry(row.file_path, "不在库的任何根路径下（根路径可能已变更），请重新扫描")
            )
            continue
        if src.is_dir():
            plan.skips.append(SkipEntry(row.file_path, "原盘目录（BDMV/VIDEO_TS）整体保持原结构"))
            continue
        if kind is MediaKind.TV and row.episode_number == 0:
            plan.skips.append(
                SkipEntry(row.file_path, "解析不出集号，无法生成规范文件名（可在待识别里修正季集）")
            )
            continue
        # 目录名与文件名各走各的模板（默认两者相同，即模板化之前的行为）
        entry_dir = entry_dir_name_of(item, library=library)
        ext = src.suffix.lower()
        # 文件属性来自台账行：命名模板可以用 {resolution}/{media_source}/
        # {release_group}，与入库侧喂的是同一组值（命名同源）
        attrs = {
            "resolution": row.resolution,
            "media_source": row.media_source,
            "release_group": row.release_group,
        }
        if kind is MediaKind.MOVIE:
            target = (
                Path(root) / entry_dir / f"{movie_file_name(item, library=library, **attrs)}{ext}"
            )
        else:
            season = row.season_number
            stem = episode_file_name(item, season, row.episode_number, library=library, **attrs)
            target = (
                Path(root)
                / entry_dir
                / season_dir_name(season, item, library=library)
                / f"{stem}{ext}"
            )
        if str(target) == row.file_path:
            plan.already_ok += 1
            continue
        assert row.id is not None and item.id is not None
        candidates.append(
            RenameAction(
                file_id=row.id,
                media_item_id=item.id,
                title=item.title,
                year=item.year,
                source_path=row.file_path,
                target_path=str(target),
                source_rel=row.file_path[len(root) + 1 :],
                target_rel=str(target)[len(root) + 1 :],
                size_bytes=row.size_bytes,
                resolution=row.resolution,
                media_source=row.media_source,
                release_group=row.release_group,
            )
        )

    # 同名处理：多个文件算出同一规范名 = 同条目多版本，按 Emby/Plex 的
    # 多版本约定加 " - 版本标签" 后缀落同一目录归组；规范名被同条目的
    # 在位文件占用时，本文件同样作为附加版本加标签。加标签后仍撞车、
    # 或目标被无关文件占用则跳过——宁可留乱，绝不覆盖
    in_place_item: dict[str, int] = {
        row.file_path: item.id
        for row, item in rows
        if item is not None and item.id is not None and row.state == FileState.IN_PLACE
    }
    by_target: dict[str, list[RenameAction]] = {}
    for action in candidates:
        by_target.setdefault(action.target_path, []).append(action)
    taken: set[str] = set()
    for target, actions in by_target.items():
        if len(actions) > 1:
            for action, label in zip(actions, _version_labels(actions), strict=True):
                _apply_version_label(action, label)
        elif in_place_item.get(target) == actions[0].media_item_id:
            # 规范名的占用者是同条目的在位文件（already_ok 的那份保持无标签，
            # Emby/Plex 按相同基础名照样归组）→ 本文件作为附加版本
            _apply_version_label(actions[0], _attr_label(actions[0]) or "V2")
        for action in actions:
            if action.target_path == action.source_path:
                plan.already_ok += 1  # 上一轮整理已加过标签的版本文件（幂等）
                continue
            if action.target_path in taken or Path(action.target_path).exists():
                plan.skips.append(
                    SkipEntry(action.source_path, "目标路径已存在同名文件，跳过以免覆盖")
                )
                continue
            taken.add(action.target_path)
            action.sidecars = _find_sidecars(action)
            plan.renames.append(action)
    plan.renames.sort(key=lambda a: a.target_path)
    plan.entry_assets = _plan_entry_assets(plan.renames, roots)
    return plan


def _plan_entry_assets(renames: list[RenameAction], roots: list[str]) -> list[EntryAssetMove]:
    """条目目录变了 → 把该目录里的镜像资产（海报/背景/季海报/NFO）一起搬走。

    两道守门（都是"宁可留着也不搬错"）：
    - 旧目录里还留着**不在本次计划里**的视频 → 不搬，图要留给那些文件；
    - 同一个旧目录的文件被搬向多个新条目目录（目录里混着两部作品）→ 歧义，
      不搬。
    条目目录的判定复用 ``layout.entry_dir_of``——与镜像写出时用的是同一个
    函数，不会出现"镜像写到 A、整理去 B 找"的错位。
    """
    root_paths = [Path(r) for r in roots]
    planned_sources = {a.source_path for a in renames}
    # 旧条目目录 → 新条目目录；值为 None 表示歧义（一个旧目录搬向多个新目录）
    mapping: dict[Path, Path | None] = {}
    for action in renames:
        old = entry_dir_of(root_paths, Path(action.source_path))
        new = entry_dir_of(root_paths, Path(action.target_path))
        if old is None or new is None or old == new:
            continue
        if old in mapping and mapping[old] != new:
            mapping[old] = None
        else:
            mapping[old] = new
    moves: list[EntryAssetMove] = []
    for old_dir, new_dir in sorted(mapping.items()):
        if new_dir is None or _has_unplanned_video(old_dir, planned_sources):
            continue
        try:
            entries = sorted(old_dir.iterdir())
        except OSError:
            continue
        moves += [
            EntryAssetMove(str(e), str(new_dir / e.name))
            for e in entries
            if _is_entry_asset(e.name) and e.is_file()
        ]
    return moves


def _has_unplanned_video(entry_dir: Path, planned_sources: set[str]) -> bool:
    """条目目录里还有视频文件吗（含季目录下的分集），``planned_sources`` 里的不算。

    计划阶段传本次要搬走的源路径（那些等会儿就不在了）；执行阶段传空集合
    ——那时该走的都走了，还剩视频就说明改名没成，资产必须原地不动。
    """
    try:
        for path in entry_dir.rglob("*"):
            if (
                path.suffix.lower() in _VIDEO_LIKE_EXTS
                and str(path) not in planned_sources
                and path.is_file()
            ):
                return True
    except OSError:
        return True  # 读不了目录就当有东西在，保守不搬
    return False


def _version_labels(actions: list[RenameAction]) -> list[str]:
    """为同名多版本生成互不相同的版本标签。

    逐级增加信息量直到组内唯一：分辨率 → +片源 → +发布组；三级都无法
    区分（探测/解析信息缺失）退回按文件大小从大到小编号（V1/V2…）。
    每一级都是确定性的，重跑整理时标签不变、不会来回改名。
    """
    picks = (
        lambda a: [a.resolution],
        lambda a: [a.resolution, a.media_source],
        lambda a: [a.resolution, a.media_source, a.release_group],
    )
    for pick in picks:
        raw = [" ".join(p for p in pick(a) if p) for a in actions]
        if all(raw) and len(set(raw)) == len(raw):
            return [sanitize_folder_name(label) for label in raw]
    order = sorted(
        range(len(actions)), key=lambda i: (-actions[i].size_bytes, actions[i].source_path)
    )
    labels = [""] * len(actions)
    for rank, index in enumerate(order, start=1):
        labels[index] = f"V{rank}"
    return labels


def _attr_label(action: RenameAction) -> str | None:
    """单个附加版本的标签：分辨率优先，缺失退片源/发布组；全缺返回 None。"""
    raw = action.resolution or action.media_source or action.release_group
    return sanitize_folder_name(raw) if raw else None


def _apply_version_label(action: RenameAction, label: str) -> None:
    """把版本标签织入目标文件名：``…/标题 (年份)[ - SxxEyy] - 标签.ext``。"""
    dst = Path(action.target_path)
    named = dst.with_name(f"{dst.stem} - {label}{dst.suffix}")
    root_len = len(action.target_path) - len(action.target_rel)
    action.target_path = str(named)
    action.target_rel = str(named)[root_len:]


def _root_of(roots: list[str], file_path: str) -> str | None:
    """文件所在的库根（最长前缀优先）；不在任何根下返回 None。"""
    best = None
    for root in roots:
        if file_path.startswith(root + "/") and (best is None or len(root) > len(best)):
            best = root
    return best


def _find_sidecars(action: RenameAction) -> list[SidecarMove]:
    """主文件的附属文件，判定口径见 ``library.sidecar``（整理/转移/回收共用）。"""
    src = Path(action.source_path)
    dst = Path(action.target_path)
    return [
        SidecarMove(str(entry), str(dst.parent / (dst.stem + tail)))
        for entry, tail in find_sidecars(src)
    ]


# ---------------------------------------------------------------------------
# 执行：后台任务入口
# ---------------------------------------------------------------------------


@dataclass
class OrganizeSummary:
    """一次整理的结论（日志与接口响应共用）。"""

    library_id: int
    renamed: int = 0  # 成功改名归位的主文件数
    sidecars_renamed: int = 0  # 跟随改名的附属文件数
    entry_assets_moved: int = 0  # 跟随条目目录改名的镜像资产数（海报/NFO 等）
    already_ok: int = 0  # 本就符合规范
    skipped: int = 0  # 计划阶段跳过（原因见预览）
    removed_dirs: int = 0  # 搬空后清理掉的目录数
    errors: list[str] = field(default_factory=list)


class _MoveError(Exception):
    """单个文件改名失败。message 是完整中文句子，直接进 errors。"""


async def organize_library(
    library_id: int,
    *,
    plan: OrganizePlan | None = None,
    context: jobs.JobContext | None = None,
    raise_unexpected: bool = False,
) -> OrganizeSummary:
    """整理一个库（后台任务入口；自开会话，不向外抛异常）。

    执行时重新计算计划（不信任预览快照），逐文件"改名 → 台账随迁"收口。
    """
    from movieclaw_api.services.library.scan import is_scanning
    from movieclaw_api.services.library.transfer import is_transferring

    summary = OrganizeSummary(library_id=library_id)
    if is_organizing(library_id):
        summary.errors.append("该库已有整理在进行中")
        return summary
    if is_scanning(library_id):
        summary.errors.append("该库正在扫描中，请等待扫描完成后再整理")
        return summary
    if is_transferring(library_id):
        summary.errors.append("该库正在转移条目，请等待转移完成后再整理")
        return summary
    _organize_tasks.try_start(library_id, (0, 0))
    try:
        return await _organize(library_id, summary, plan=plan, context=context)
    except jobs.JobCancelled:
        raise
    except Exception:  # noqa: BLE001 -- 后台任务兜底
        logger.exception("媒体库 #%s 整理时发生未知错误", library_id)
        if raise_unexpected:
            raise
        summary.errors.append("整理中断：发生未知错误（详见后端日志）")
        return summary
    finally:
        _organize_tasks.finish(library_id, result=(utcnow(), summary))


async def _organize(
    library_id: int,
    summary: OrganizeSummary,
    *,
    plan: OrganizePlan | None = None,
    context: jobs.JobContext | None = None,
) -> OrganizeSummary:
    db = get_database()
    async with db.session() as session:
        library = await session.get(Library, library_id)
        if library is None:
            summary.errors.append("媒体库不存在（可能已被删除）")
            return summary
        plan = plan or await build_organize_plan(session, library)
        summary.already_ok = plan.already_ok
        summary.skipped = len(plan.skips)
        repo = LibraryFileRepository(session)
        roots = [r.rstrip("/") for r in library.root_paths]

        _organize_tasks.update(library_id, (0, len(plan.renames)))
        if context is not None:
            await context.update_progress(
                mode="determinate",
                phase="organizing",
                message=f"准备整理 {len(plan.renames)} 个文件",
                current=0,
                total=len(plan.renames),
                percent=0.0 if plan.renames else 100.0,
                details={"errors": 0},
            )
        # 一次性把本库台账行拉进会话身份映射：随后逐文件的 session.get
        # 全部命中内存，不再每个文件都打一次数据库
        await session.execute(select(LibraryFile).where(LibraryFile.library_id == library_id))
        dirty_parents: set[Path] = set()
        total = len(plan.renames)
        for done, action in enumerate(plan.renames, start=1):
            if context is not None:
                await context.raise_if_cancelled()
            row = await session.get(LibraryFile, action.file_id)
            if row is None:
                summary.errors.append(f"台账文件已不存在，跳过：{action.source_path}")
                _organize_tasks.update(library_id, (done, total))
                continue
            src = Path(action.source_path)
            dst = Path(action.target_path)
            # 台账已指向目标 = 上次执行连台账都提交过了，确认磁盘在位即可
            ledger_done = row.file_path == action.target_path
            try:
                if ledger_done:
                    if not await asyncio.to_thread(dst.exists):
                        raise _MoveError(f"台账路径已被其他操作修改，跳过：{row.file_path}")
                elif row.file_path != action.source_path:
                    raise _MoveError(f"台账路径已被其他操作修改，跳过：{row.file_path}")
                else:
                    await asyncio.to_thread(
                        _resolve_and_move,
                        src,
                        dst,
                        missing_message=f"源与目标都不存在，无法继续整理：{src}",
                    )
            except _MoveError as exc:
                summary.errors.append(str(exc))
                _organize_tasks.update(library_id, (done, total))
                continue
            # 改名成功立即随迁台账：中途失败不会留下账实不符的批量烂摊子
            if not ledger_done:
                container = dst.suffix.lstrip(".").lower() or None
                await repo.relocate(
                    action.file_id, file_path=action.target_path, container=container
                )
            summary.renamed += 1
            dirty_parents.add(src.parent)
            for sidecar in action.sidecars:
                sidecar_src = Path(sidecar.source_path)
                sidecar_dst = Path(sidecar.target_path)
                try:
                    await asyncio.to_thread(
                        _resolve_and_move,
                        sidecar_src,
                        sidecar_dst,
                        missing_message=f"附属文件已不存在：{sidecar_src}",
                    )
                    summary.sidecars_renamed += 1
                except _MoveError as exc:
                    summary.errors.append(f"附属文件改名失败：{exc}")
            _organize_tasks.update(library_id, (done, total))
            # 进度持久化按秒节流（最后一个文件必写）：逐文件写会触发
            # “事件 → SSE → 所有任务中心客户端各拉两次列表”的放大链，
            # 实测能把整轮整理拖慢一个数量级。实时进度由上面的内存态
            # _organize_tasks 提供，节流不影响接口里的进行中读数
            if context is not None and (done == total or context.progress_due()):
                await context.update_progress(
                    mode="determinate",
                    phase="organizing",
                    message=f"已整理 {done} / {total} 个文件",
                    current=done,
                    total=total,
                    percent=(done / total * 100) if total else 100.0,
                    details={"errors": len(summary.errors)},
                )

        # 条目目录级镜像资产跟着搬：必须在清理空目录之前——它们不搬走，
        # 旧条目目录就永远非空，下面的 rmdir 一个也清不掉
        if plan.entry_assets:
            moved, asset_errors = await asyncio.to_thread(_move_entry_assets, plan.entry_assets)
            summary.entry_assets_moved = moved
            summary.errors.extend(asset_errors)

        # 只清理被本次整理搬空的目录（及其变空的祖先）：非空即停、绝不删文件，
        # 与整理无关的空目录一概不碰
        summary.removed_dirs = await asyncio.to_thread(_prune_emptied_dirs, dirty_parents, roots)

    logger.info(
        "媒体库 #%s 整理完成：改名归位 %d（附属文件 %d，条目资产 %d），已规范 %d，"
        "跳过 %d，清理空目录 %d，问题 %d",
        library_id,
        summary.renamed,
        summary.sidecars_renamed,
        summary.entry_assets_moved,
        summary.already_ok,
        summary.skipped,
        summary.removed_dirs,
        len(summary.errors),
    )
    return summary


async def enqueue_organize_job(
    session,
    library: Library,
    plan: OrganizePlan,
    *,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "web",
) -> jobs.CreateJobResult:
    """保存本次确认后的整理计划；重启时据此修复“磁盘已改、台账未改”。"""
    assert library.id is not None
    return await jobs.create_job(
        session,
        job_type="library.organize",
        subject=library.name,
        input_data={"library_id": library.id, "plan": asdict(plan)},
        resources=[jobs.ResourceRef("library", library.id)],
        dedupe_key=f"library.organize:{library.id}",
        conflict_policy="return_existing",
        handler_revision="library.organize.v1",
        max_attempts=3,
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress={
            **jobs.default_progress("等待整理媒体库文件"),
            "total": len(plan.renames),
            "details": {"errors": 0},
        },
    )


def _plan_from_job(value: object) -> OrganizePlan:
    """从版本化 Job 输入恢复计划；字段只接受创建端写入的稳定最小集合。"""
    if not isinstance(value, dict):
        raise jobs.JobFailed("整理任务定义损坏，请重新预览后发起", code="JOB_INPUT_INVALID")
    renames = []
    for raw in value.get("renames", []):
        sidecars = [SidecarMove(**item) for item in raw.get("sidecars", [])]
        renames.append(RenameAction(**{**raw, "sidecars": sidecars}))
    return OrganizePlan(
        library_id=int(value["library_id"]),
        total=int(value.get("total") or 0),
        already_ok=int(value.get("already_ok") or 0),
        renames=renames,
        skips=[SkipEntry(**item) for item in value.get("skips", [])],
        entry_assets=[EntryAssetMove(**item) for item in value.get("entry_assets", [])],
    )


@jobs.register_job_handler("library.organize")
async def _run_organize_job(
    context: jobs.JobContext, input_data: dict[str, object]
) -> dict[str, object]:
    """整理 Job 处理器：逐文件物理改名并提交台账，每一步都可幂等恢复。"""
    library_id = int(input_data["library_id"])
    from movieclaw_api.services.library.scan import is_scanning
    from movieclaw_api.services.library.transfer import is_transferring

    if is_organizing(library_id):
        raise jobs.JobRetry("媒体库已有整理任务在收尾", delay_seconds=5)
    if is_scanning(library_id):
        raise jobs.JobRetry("媒体库正在扫描，整理作业稍后自动继续", delay_seconds=10)
    if is_transferring(library_id):
        raise jobs.JobRetry("媒体库正在转移条目，整理作业稍后自动继续", delay_seconds=10)
    summary = await organize_library(
        library_id,
        plan=_plan_from_job(input_data.get("plan")),
        context=context,
        raise_unexpected=True,
    )
    message = f"整理完成：改名 {summary.renamed} 个文件"
    if summary.errors:
        message += f"，{len(summary.errors)} 个问题已跳过"
    return {"message": message, **asdict(summary)}


def _move_entry_assets(moves: list[EntryAssetMove]) -> tuple[int, list[str]]:
    """搬运条目目录级镜像资产（线程池内运行），返回 (成功数, 中文问题列表)。

    **执行前按源目录复核一次**：目录里还留着视频，说明这一组的改名没有真的
    成功（权限不足、并发占用，或计划到执行之间目录又进了新文件）——此时搬
    资产会把图从还在原地的视频身边抽走、丢进一个空目录，必须整组跳过。
    计划阶段的同名守门管的是预览准不准，这一道管的是执行对不对。

    目标已存在时**不覆盖**，按内容分流：
    - 逐字节相同 → 删掉源头那份重复副本。这是本模块唯一的删除动作，只针对
      白名单里的镜像产物（本程序自己写出、随时可由 data/ 下的资产重建），
      不是用户数据；不删它，旧目录就永远非空、清不掉；
    - 内容不同 → 原样保留并告警。那可能是用户自己换过的图，绝不静默处置。
    """
    moved = 0
    errors: list[str] = []
    checked: dict[Path, bool] = {}  # 源目录 → 是否可搬（每组只复核一次）
    for move in moves:
        src, dst = Path(move.source_path), Path(move.target_path)
        movable = checked.get(src.parent)
        if movable is None:
            if not src.parent.is_dir():
                # 目录已经不在了：作业重跑、上一轮就搬完并清掉了。无事可做，
                # 但这不是问题——不能报"仍有视频文件"污染结论
                movable = False
            else:
                movable = not _has_unplanned_video(src.parent, set())
                if not movable:
                    errors.append(f"条目目录里仍有视频文件，资产保持原位：{src.parent}")
            checked[src.parent] = movable
        if not movable:
            continue
        try:
            if not src.is_file():
                continue  # 计划到执行之间被别的操作动过，跳过即可
            if dst.exists():
                if filecmp.cmp(src, dst, shallow=False):
                    src.unlink()
                    moved += 1
                else:
                    errors.append(f"条目资产目标已存在且内容不同，保留原文件：{src} → {dst}")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            _move_no_clobber(src, dst)
            moved += 1
        except (OSError, _MoveError) as exc:
            errors.append(f"条目资产搬运失败：{src} → {dst}（{exc}）")
    return moved, errors


def _resolve_and_move(src: Path, dst: Path, *, missing_message: str) -> None:
    """（线程池内运行）看一眼现场，把该做的改名做掉。

    exists 判定与改名收进同一次线程跳转：省去逐文件三四次跨线程 stat 的
    调度开销，更重要的是网络挂载上这些都是可能阻塞的调用，一律不允许
    落在事件循环里（一次挂住会冻住全站请求）。

    源在目标不在 → 改名；源不在目标在 → 无事（服务可能停在“磁盘改名
    成功、台账提交前”，持久化计划能证明目标属于本任务，调用方只补台账）；
    其余现场说明磁盘被并发修改，抛 _MoveError 逐条跳过。
    """
    src_exists = src.exists()
    dst_exists = dst.exists()
    if src_exists and not dst_exists:
        _move_no_clobber(src, dst)
        return
    if not src_exists and dst_exists:
        return
    if src_exists and dst_exists:
        raise _MoveError(f"源与目标同时存在，跳过以免覆盖：{src} → {dst}")
    raise _MoveError(missing_message)


def _move_no_clobber(src: Path, dst: Path) -> None:
    """同文件系统内的防覆盖改名（线程池内运行）。

    ``fsops.rename_no_replace`` 一步到位：目标已存在会原子失败（EEXIST），
    杜绝"计划检查后、执行前恰有同名文件落地（如入库管线并发落位）"的
    覆盖窗口；rename 同时保证极空间等 FUSE 存储的索引能跟上改名。
    """
    if not src.exists():
        raise _MoveError(f"源文件已不在原位，跳过：{src}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _MoveError(f"创建目标目录失败（{exc.strerror}）：{dst.parent}") from exc
    try:
        rename_no_replace(src, dst)
    except FileExistsError as exc:
        raise _MoveError(f"目标路径已被占用，跳过以免覆盖：{dst}") from exc
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise _MoveError(f"目标与源不在同一文件系统，无法改名归位：{src} → {dst}") from exc
        raise _MoveError(f"改名失败（{exc.strerror}）：{src} → {dst}") from exc


def _prune_emptied_dirs(dirty_parents: set[Path], roots: list[str]) -> int:
    """从被搬空的目录向上清理：目录空则 rmdir 并继续看父级，非空/到根即停。"""
    removed = 0
    root_paths = {Path(r) for r in roots}
    for parent in dirty_parents:
        current = parent
        while current not in root_paths and any(current.is_relative_to(r) for r in root_paths):
            try:
                current.rmdir()  # 非空目录会抛 OSError——这正是"绝不删文件"的保证
            except OSError:
                break
            removed += 1
            current = current.parent
    return removed
