"""媒体库条目详情装配与真实删除（条目详情页的后端）。

三块职责：

1. **详情装配** ``build_item_detail``：把一个条目在库内的全部信息拼成一页——
   基本信息（MediaItem）、本地刮削元数据（NFO：简介/评分/片长/演职员）、
   本地美术图（poster/fanart 优先于 TMDB 图床）、逐文件的真实介质规格
   （ffprobe：分辨率/音轨/内封字幕）与外挂字幕。旧台账没探测过音轨的，
   详情页按需补探后回填（懒回填，不强迫用户整库重扫）。
2. **本地美术图定位** ``find_local_artwork``：条目目录下按 Kodi/Emby 惯例
   命名的图片文件；找到即由 /libraries/.../artwork 接口直接回吐，
   没有时前端退回 TMDB 图床。
3. **真实删除** ``delete_item_files``：条目详情页「删除」的语义是**从磁盘
   彻底删除**（与其他一切"只删台账"的接口截然相反）——整个条目目录
   （视频+NFO+海报+字幕）一起清掉，不留刮削残渣。唯一的克制：目录里
   混有**其他条目**的文件时退化为只删本条目的文件及其同名附属文件。
   ``delete_single_file`` 是它的文件级姊妹：只删一个版本/一集的文件及
   同名附属（多版本洗版、删某集重下），最后一个文件时升级为整条目删除。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, NamedTuple

from sqlalchemy import and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.schemas.library import LibraryItemView, derive_air_status
from movieclaw_api.services.library.layout import STRM_EXT, entry_dir_of
from movieclaw_api.services.library.nfo import (
    EntryMetadata,
    NfoActor,
    read_entry_metadata,
    read_episode_metadata,
)
from movieclaw_api.services.library.sort_key import title_initial, title_sort_key
from movieclaw_api.services.media_probe import probe_media
from movieclaw_api.services.media_scrape import asset_version, file_version
from movieclaw_db.models import Library, LibraryFile, MediaItem, utcnow
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.library_items")

# TMDB 展示信息（条目简介/演职员、分季集名/剧照）的持久缓存：详情页
# 每次访问都实时打 TMDB 是首屏慢的主因之一。展示信息变化极慢——新鲜期
# 内直接命中（毫秒级），过期后先回旧值、后台静默刷新（SWR），跨重启不丢
_DISPLAY_CACHE_FRESH_TTL = 24 * 3600.0
_DISPLAY_CACHE_STALE_TTL = 30 * 24 * 3600.0

_display_cache = None


def _get_display_cache():
    global _display_cache
    if _display_cache is None:
        from movieclaw_cache import SwrCache
        from movieclaw_db.stores import SqlCacheStore

        _display_cache = SwrCache(SqlCacheStore(), "library_display")
    return _display_cache


# ---------------------------------------------------------------------------
# 条目目录与本地美术图
# ---------------------------------------------------------------------------

# Kodi/Emby/TMM 生态的条目级美术图命名惯例（按优先级排列）
_POSTER_NAMES = ("poster", "folder", "cover", "movie")
_FANART_NAMES = ("fanart", "backdrop", "background")
_ART_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# 外挂字幕扩展名（同名或"同名.语言"命名，整理器的 sidecar 同一口径）
_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".sup", ".vtt"}

# 详情页单次请求最多补探的文件数：电影条目几个版本毫秒级完成；上百集的
# 剧集首次打开只补一批，翻几次页自然补齐——不让一次请求卡几十秒
_DETAIL_PROBE_LIMIT = 16

# 补探过程中每探完这么多个文件提交一次：整库补探是小时级的活，不能攒成
# 一个大事务（中途中断就全白探了），也不必一个一个提交
_PROBE_COMMIT_EVERY = 32


def find_local_artwork(entry_dir: Path, kind: str) -> Path | None:
    """条目目录下的本地美术图；``kind``: poster / fanart。找不到返回 None。"""
    names = _POSTER_NAMES if kind == "poster" else _FANART_NAMES
    for name in names:
        for ext in _ART_EXTS:
            candidate = entry_dir / f"{name}{ext}"
            if candidate.is_file():
                return candidate
    # 裸文件条目的 "<文件名>-poster.jpg" 惯例（TMM 对散装电影的写法）
    suffix = f"-{kind}"
    try:
        for candidate in sorted(entry_dir.iterdir()):
            if (
                candidate.is_file()
                and candidate.suffix.lower() in _ART_EXTS
                and candidate.stem.lower().endswith(suffix)
            ):
                return candidate
    except OSError:
        return None
    return None


def _external_subtitles(video: Path) -> list[str]:
    """视频旁的外挂字幕文件名（同名前缀匹配："片名.chs.srt" 算"片名.mkv"的）。"""
    stem = video.stem.lower()
    found: list[str] = []
    try:
        entries = sorted(video.parent.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in _SUBTITLE_EXTS:
            continue
        if entry.stem.lower() == stem or entry.stem.lower().startswith(stem + "."):
            found.append(entry.name)
    return found


# ---------------------------------------------------------------------------
# 详情装配
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 海报墙聚合（单库条目列表）
# ---------------------------------------------------------------------------


class _FileFacts(NamedTuple):
    """海报墙聚合用到的七个台账字段（顺序与查询列一致）。

    代码读起来与整行取时一模一样，只是不再拖着四十列（含三列 JSON）走。"""

    season_number: int
    episode_number: int
    size_bytes: int
    resolution: str | None
    missing_since: datetime | None
    created_at: datetime
    # 尚未探出介质规格（audio_streams IS NULL 在 SQL 里算好，不取 JSON 本体）
    unprobed: bool


WallSort = Literal["title", "added_at", "probing"]


async def _titles_sorted(session: AsyncSession, library_id: int) -> list[tuple[int, str]]:
    """本库全部条目的 (id, 标题)，按拼音序排好。

    按标题排序不走 SQL：SQLite 对中文按码点排，出来的顺序对用户没有意义
    （详见 sort_key 模块）。这里只取一列标题排序，本页的聚合仍只算 limit
    个条目——贵的是聚合，不是排一列标题。索引条（A-Z 分档）与按标题分页
    共用这份排好的名单，口径天然一致。
    """
    rows = (
        await session.execute(
            select(LibraryFile.media_item_id, MediaItem.title)
            .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .where(
                LibraryFile.library_id == library_id,
                LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
            )
            .distinct()
        )
    ).all()
    # id 收尾：拼音串相同（同名不同条目）时顺序必须稳定，否则翻页会重复或漏项
    return sorted(
        ((i, t) for i, t in rows if i is not None), key=lambda r: (title_sort_key(r[1]), r[0])
    )


async def build_library_index(session: AsyncSession, library_id: int) -> list[tuple[str, int, int]]:
    """按标题排序下的首字母分档：[(首字母, 条目数, 起始 offset)]，只回非空档。

    起始 offset 就是海报墙 ``?sort=title&offset=`` 的取值——前端点一下
    字母即可跳到该档第一格。
    """
    ordered = await _titles_sorted(session, library_id)
    buckets: list[tuple[str, int, int]] = []
    for index, (_, title) in enumerate(ordered):
        initial = title_initial(title)
        if buckets and buckets[-1][0] == initial:
            head, count, start = buckets[-1]
            buckets[-1] = (head, count + 1, start)
        else:
            buckets.append((initial, 1, index))
    return buckets


async def _wall_page_ids(
    session: AsyncSession,
    library_id: int,
    sort: WallSort,
    limit: int | None,
    offset: int,
) -> list[int]:
    """按 sort 排好序的本页条目 id（无 limit 时是全库）。

    先把「这一页是哪些条目」定下来，后面的聚合才能只算这几十个条目。
    每个排序都以 media_item_id 收尾——排序键相等时顺序必须稳定，
    否则翻页会出现某条目重复出现、另一条目永远刷不到的漏项。
    """
    if sort == "title":
        ids = [i for i, _ in await _titles_sorted(session, library_id)]
        return ids if limit is None else ids[offset : offset + limit]

    if sort == "added_at":
        # 「最近添加」：条目的入账时间取它名下最新的一次文件入账
        query = (
            select(LibraryFile.media_item_id)
            .where(
                LibraryFile.library_id == library_id,
                LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
            )
            .group_by(LibraryFile.media_item_id)  # type: ignore[arg-type]
            .order_by(
                func.max(LibraryFile.created_at).desc(),
                LibraryFile.media_item_id.desc(),  # type: ignore[union-attr]
            )
        )
        if limit is not None:
            query = query.limit(limit).offset(offset)
        return [i for i in (await session.execute(query)).scalars().all() if i is not None]

    # probing：扫描补探阶段把「还有文件没读出规格」的条目提到墙最前面，
    # 用户能看见"在处理哪几部"；两段各自保持拼音序（sorted 稳定排序）。
    # strm 占位文件不算"没读出"——它永远探不出规格，算进来会让网盘库
    # 每轮扫描都全墙置顶、永不落位
    ordered = await _titles_sorted(session, library_id)
    unprobed = {
        i
        for i in (
            await session.execute(
                select(LibraryFile.media_item_id)
                .where(
                    LibraryFile.library_id == library_id,
                    LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
                    LibraryFile.audio_streams.is_(None),  # type: ignore[union-attr]
                    LibraryFile.missing_since.is_(None),  # type: ignore[union-attr]
                    LibraryFile.file_path.not_like(f"%{STRM_EXT}"),  # type: ignore[union-attr]
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    }
    ids = [i for i, _ in sorted(ordered, key=lambda row: row[0] not in unprobed)]
    return ids if limit is None else ids[offset : offset + limit]


async def build_library_wall(
    session: AsyncSession,
    library_id: int,
    *,
    sort: WallSort = "title",
    limit: int | None = None,
    offset: int = 0,
) -> list[LibraryItemView]:
    """库内媒体条目的库存聚合（单库海报墙数据源）。

    ``limit`` 给定时只聚合这一页的条目——首页「最近添加」只要 20 格，
    海报墙滚动加载一次要一屏，都不该为此把整库的台账行捞出来算一遍。
    不给 limit 则是全库（保留给一次性拿完整库存的调用方）。

    调用方需自行完成库存在性检查（404）。
    """
    page_ids = await _wall_page_ids(session, library_id, sort, limit, offset)
    if not page_ids:
        return []
    # 分页时按 id 列表收窄（一页几十个，绑定变量绰绰有余）；全库时走子查询
    # ——SQLite 的绑定变量数有上限，几千个 id 塞进 IN 列表会撞
    in_page = (
        select(LibraryFile.media_item_id).where(
            LibraryFile.library_id == library_id,
            LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
        )
        if limit is None
        else page_ids
    )
    return await _aggregate_wall_views(session, library_id, in_page, page_ids)


async def _aggregate_wall_views(
    session: AsyncSession,
    library_id: int,
    in_page,
    ordered_ids: list[int],
) -> list[LibraryItemView]:
    """把一批条目在某个库内的台账行聚合成海报墙视图，按 ``ordered_ids`` 排列。

    海报墙分页与媒体库搜索共用这份聚合（口径必须一致：库存概况、缺集数、
    海报的本地资产优先级）。``in_page`` 是条目 id 列表或等价子查询。
    """
    from movieclaw_api.core.config import get_settings
    from movieclaw_db.models import MediaMetadata
    from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
    from movieclaw_db.repositories.media_repo import MediaItemRepository

    # 只取聚合真正用得上的六列，不整行取 ORM 对象：台账行有四十来列、其中
    # 三列是 JSON（音轨/字幕/候选），整行取意味着一部三万集的库要反序列化
    # 九万段 JSON——实测 2000 部剧的库因此要跑四秒多，而这些列一个都用不上
    file_rows = (
        await session.execute(
            select(
                LibraryFile.media_item_id,
                LibraryFile.season_number,
                LibraryFile.episode_number,
                LibraryFile.size_bytes,
                LibraryFile.resolution,
                LibraryFile.missing_since,
                LibraryFile.created_at,
                # strm 占位文件永远探不出规格，不算「待补探」
                and_(
                    LibraryFile.audio_streams.is_(None),  # type: ignore[union-attr]
                    LibraryFile.file_path.not_like(f"%{STRM_EXT}"),  # type: ignore[union-attr]
                ),
            ).where(
                LibraryFile.library_id == library_id,
                LibraryFile.media_item_id.in_(in_page),  # type: ignore[union-attr]
            )
        )
    ).all()
    files_by_item: dict[int, list[_FileFacts]] = {}
    for media_item_id, *facts in file_rows:
        files_by_item.setdefault(media_item_id, []).append(_FileFacts(*facts))
    # 条目行整取——展示要用到标题/年份/状态等大部分列
    items = (
        (await session.execute(select(MediaItem).where(MediaItem.id.in_(in_page))))  # type: ignore[attr-defined]
        .scalars()
        .all()
    )
    grouped: dict[int, tuple[MediaItem, list[_FileFacts]]] = {
        item.id: (item, files_by_item[item.id])  # type: ignore[index]
        for item in items
        if item.id is not None
    }

    # 剧集的已播单元集合：海报悬浮操作（订阅追新/补齐缺集）的判断依据。
    # 集数据在条目建档时已落库（media_episode），批量查询本地即得，不打 TMDB。
    # 已播/在位口径走仓储层唯一实现（与订阅预检、工单生成同源）：
    # 缺集 = 已播（特别季除外）− 跨库在位，「补齐缺集」建订阅后恰好只为这些集生成工单
    tv_item_ids = [i for i, (item, _) in grouped.items() if item.kind == "tv"]
    aired_by_item = await MediaItemRepository(session).aired_units_many(tv_item_ids)
    owned_by_item = await LibraryFileRepository(session).owned_units_many(tv_item_ids)

    base = get_settings().tmdb_image_base_url.rstrip("/")
    # 海报优先本地刮削资产（断网可用），没有资产的回落 TMDB 图床
    poster_assets: dict[int, str] = {
        item_id: poster_file
        for item_id, poster_file in (
            await session.execute(
                select(MediaMetadata.media_item_id, MediaMetadata.poster_file).where(
                    MediaMetadata.media_item_id.in_(grouped.keys()),  # type: ignore[attr-defined]
                    MediaMetadata.poster_file.is_not(None),  # type: ignore[union-attr]
                )
            )
        ).all()
    }
    by_id: dict[int, LibraryItemView] = {}
    for item, files in grouped.values():
        units = {(f.season_number, f.episode_number) for f in files}
        if item.kind == "tv":
            missing_episodes = len(
                aired_by_item.get(item.id, set()) - owned_by_item.get(item.id, set())  # type: ignore[arg-type]
            )
        else:
            missing_episodes = 0
        if item.id in poster_assets:
            # ?v=<mtime>：换图原地覆盖同一路径，不带版本海报墙会一直显示旧图
            rel = poster_assets[item.id]
            poster_url = f"/images/assets/{rel}?v={asset_version(rel)}"
        else:
            poster_url = f"{base}/w500{item.poster_path}" if item.poster_path else None
        by_id[item.id] = LibraryItemView(  # type: ignore[index]
            media_item_id=item.id,  # type: ignore[arg-type]
            kind=MediaKind(item.kind),
            tmdb_id=item.tmdb_id,
            title=item.title,
            year=item.year,
            poster_url=poster_url,
            file_count=len(files),
            total_size_bytes=sum(f.size_bytes for f in files),
            seasons=sorted({s for s, _ in units if item.kind == "tv"}),
            episode_count=len(units) if item.kind == "tv" else 0,
            resolutions=sorted({f.resolution for f in files if f.resolution}, reverse=True),
            missing_count=sum(1 for f in files if f.missing_since is not None),
            air_status=derive_air_status(item.status) if item.kind == "tv" else None,
            missing_episode_count=missing_episodes,
            added_at=max(f.created_at for f in files),
            # 缺失文件不算「待补探」：文件都不在了，探不是「还没轮到」而是「探不了」
            probe_pending_count=sum(1 for f in files if f.unprobed and f.missing_since is None),
        )
    # 顺序以调用方给定的 ordered_ids 为准，不在这里二次排序
    return [by_id[i] for i in ordered_ids if i in by_id]


async def search_library_items(
    session: AsyncSession, keyword: str
) -> dict[int, list[LibraryItemView]]:
    """按关键词搜索全部媒体库的已识别条目：library_id -> 命中条目视图。

    搜索弹窗「媒体库」垂直的数据源。标题/原名子串匹配（忽略英文大小写），
    只搜已识别入库的条目——待识别文件没有可靠的标题可匹配，去待识别清单
    处理更合适。组内按标题拼音排序，与海报墙同一套排序规则。
    """
    pattern = f"%{keyword.strip().lower()}%"
    rows = (
        await session.execute(
            select(LibraryFile.library_id, LibraryFile.media_item_id, MediaItem.title)
            .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
            .where(
                LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
                or_(
                    func.lower(MediaItem.title).like(pattern),
                    func.lower(MediaItem.original_title).like(pattern),
                ),
            )
            .distinct()
        )
    ).all()
    matched: dict[int, list[tuple[int, str]]] = {}
    for library_id, item_id, title in rows:
        if item_id is not None:
            matched.setdefault(library_id, []).append((item_id, title))

    result: dict[int, list[LibraryItemView]] = {}
    for library_id, pairs in matched.items():
        ordered = [i for i, _ in sorted(pairs, key=lambda p: (title_sort_key(p[1]), p[0]))]
        result[library_id] = await _aggregate_wall_views(session, library_id, ordered, ordered)
    return result


@dataclass
class ItemDetailBundle:
    """详情页所需的全部原料（路由层映射为响应 schema）。"""

    item: MediaItem
    files: list[LibraryFile]
    entry_dirs: list[str]  # 条目在磁盘上的目录（删除确认展示；可能多根多个）
    local_meta: EntryMetadata | None  # NFO 解出的展示元数据；无 NFO 为 None
    has_local_poster: bool
    has_local_fanart: bool
    # 条目目录美术图的版本戳（mtime）：图片被替换后 URL 随之变化，
    # 否则浏览器拿缓存里的旧图显示，用户看到"换了没生效"
    local_poster_version: int
    local_fanart_version: int
    external_subtitles: dict[int, list[str]]  # file_id -> 外挂字幕文件名
    # 待补探音轨的台账行（响应后由后台任务补探回填——ffprobe 在网络挂载上
    # 一个文件就要秒级，放在请求路径里会把首屏拖成十几秒）
    pending_probe_ids: list[int] = field(default_factory=list)


def resolve_entry_dirs(roots: list[Path], files: list[LibraryFile]) -> list[Path]:
    """条目目录集合：多根/多版本可能给出多个，去重保序（第一个用于 NFO 与美术图）。"""
    entry_dirs: list[Path] = []
    for row in files:
        entry = entry_dir_of(roots, Path(row.file_path))
        # 原盘条目 file_path 本身就是目录（BDMV/VIDEO_TS），直接在根下时以它为条目目录
        if entry is None and row.container in ("bluray", "dvd"):
            entry = Path(row.file_path)
        if entry is not None and entry not in entry_dirs:
            entry_dirs.append(entry)
    return entry_dirs


async def layered_item_meta(
    session: AsyncSession,
    item: MediaItem,
    entry_dirs: list[Path],
    files: list[LibraryFile],
    kind: MediaKind,
) -> EntryMetadata | None:
    """条目展示元数据的**唯一读口径**（docs/design/metadata.md 第 5 节），
    Web 详情页与 Jellyfin 兼容层共用——分层策略只在这里维护一份：
    本地 NFO 最优先（尊重既有刮削成果）→ 库内刮削档案（media_metadata，
    绝大多数条目的日常路径，断网可用）→ TMDB 实时兜底（条目还没刮过，
    顺带触发后台刮削自愈）。
    """
    local_meta = await asyncio.to_thread(_read_meta, entry_dirs, files, kind)
    if local_meta is not None:
        await _fill_actor_thumbs(session, item, local_meta)
    if local_meta is None:
        local_meta = await _db_meta(session, item)
    if local_meta is None:
        local_meta = await _tmdb_fallback_meta(item)
        if item.id is not None:
            from movieclaw_api.services.media_scrape import scrape_media_item

            asyncio.get_running_loop().create_task(scrape_media_item(item.id))
    return local_meta


def local_item_artwork(roots: list[Path], files: list[LibraryFile], kind: str) -> Path | None:
    """条目目录里的本地美术图（逐个条目目录找，第一张命中即用）。

    Web 的 artwork 接口与 Jellyfin 图片接口共用；找不到时两端各自退回
    刮削资产 / TMDB 图床。同步磁盘 IO——调用方自行决定是否进线程池。
    """
    for entry in resolve_entry_dirs(roots, files):
        if not entry.is_dir():
            continue
        art = find_local_artwork(entry, kind)
        if art is not None:
            return art
    return None


async def build_item_detail(
    session: AsyncSession, library: Library, item: MediaItem, files: list[LibraryFile]
) -> ItemDetailBundle:
    """装配条目详情：NFO 元数据 + 本地美术图 + 介质规格懒回填 + 外挂字幕。

    磁盘 IO（NFO 解析、目录列举、ffprobe 补探）全部放线程池；库根不可达时
    各环节自然落空，页面仍能靠台账字段渲染。
    """
    roots = [Path(p) for p in library.root_paths]
    kind = MediaKind(library.kind)

    entry_dirs = resolve_entry_dirs(roots, files)
    local_meta = await layered_item_meta(session, item, entry_dirs, files, kind)

    primary_dir = next((d for d in entry_dirs if d.is_dir()), None)
    poster_art = fanart_art = None
    if primary_dir is not None:
        poster_art = await asyncio.to_thread(find_local_artwork, primary_dir, "poster")
        fanart_art = await asyncio.to_thread(find_local_artwork, primary_dir, "fanart")

    external: dict[int, list[str]] = {}
    for row in files:
        if row.missing_since is not None or row.container in ("bluray", "dvd"):
            continue
        assert row.id is not None
        external[row.id] = await asyncio.to_thread(_external_subtitles, Path(row.file_path))

    return ItemDetailBundle(
        item=item,
        files=files,
        entry_dirs=[str(d) for d in entry_dirs],
        local_meta=local_meta,
        has_local_poster=poster_art is not None,
        has_local_fanart=fanart_art is not None,
        local_poster_version=file_version(poster_art),
        local_fanart_version=file_version(fanart_art),
        external_subtitles=external,
        # strm 占位文件不排后台补探——探了必失败，还会让详情页每次打开都
        # 白排一次任务
        pending_probe_ids=[
            row.id
            for row in files
            if row.id is not None
            and row.audio_streams is None
            and row.missing_since is None
            and not row.file_path.lower().endswith(STRM_EXT)
        ][:_DETAIL_PROBE_LIMIT],
    )


def _read_meta(
    entry_dirs: list[Path], files: list[LibraryFile], kind: MediaKind
) -> EntryMetadata | None:
    """找到并解析条目 NFO（同步，放线程池）。

    候选顺序：条目目录的 movie.nfo/tvshow.nfo → 各视频文件的同名 .nfo
    （散装电影惯例）。取第一份解出实质内容的；全是最小身份 NFO 时返回 None
    ——前端据此展示"本地未刮削"而不是一堆空栏。
    """
    candidates: list[Path] = []
    entry_name = "movie.nfo" if kind is MediaKind.MOVIE else "tvshow.nfo"
    for directory in entry_dirs:
        candidates.append(directory / entry_name)
    for row in files:
        path = Path(row.file_path)
        if path.suffix:  # 原盘目录没有同名 NFO 一说
            candidates.append(path.with_suffix(".nfo"))
    for nfo in candidates:
        if not nfo.is_file():
            continue
        meta = read_entry_metadata(nfo)
        if meta is not None and meta.has_content():
            return meta
    return None


# ---------------------------------------------------------------------------
# 剧集分集（播放器式分集区的数据源）
# ---------------------------------------------------------------------------


@dataclass
class EpisodeInfo:
    """一集的展示信息：季集结构（media_season）+ 本地分集 NFO/缩略图 +
    TMDB 分季兜底 三源合并的结果。"""

    episode_number: int
    name: str | None = None
    overview: str | None = None
    air_date: str | None = None
    # 剧照：本地 "<视频名>-thumb.jpg" 走 /libraries/files/{id}/thumb 相对路径，
    # 否则 TMDB still 图床绝对地址；都没有为 None（前端给占位）
    still_url: str | None = None
    owned: bool = False  # 该集有在位文件
    file_ids: list[int] = field(default_factory=list)  # 该集的台账行（含 missing）


def find_episode_thumb(video: Path) -> Path | None:
    """分集本地缩略图：Kodi 惯例 "<视频文件名>-thumb.jpg"。"""
    for ext in _ART_EXTS:
        candidate = video.with_name(f"{video.stem}-thumb{ext}")
        if candidate.is_file():
            return candidate
    return None


async def build_season_episodes(
    session: AsyncSession, item: MediaItem, files: list[LibraryFile], season_number: int
) -> list[EpisodeInfo]:
    """装配一季的分集清单：并集"元数据里的集 ∪ 库里实际拥有的集"。

    元数据有而库里没有的集照样列出（owned=False，前端置灰）——用户一眼
    看到缺口；库里有而元数据没有的集也不丢（TMDB 元数据可能滞后）。
    读路径分层（docs/design/metadata.md 第 5 节）：分集 NFO/本地缩略图
    最优先（尊重既有刮削成果）→ media_episode 表（刮削落库的主体）→
    条目还没刮过时拉一次 TMDB 分季实时兜底。
    """
    from sqlmodel import select

    from movieclaw_db.models import MediaEpisode

    season_rows = [
        row for row in files if row.season_number == season_number and row.episode_number > 0
    ]
    by_episode: dict[int, list[LibraryFile]] = {}
    for row in season_rows:
        by_episode.setdefault(row.episode_number, []).append(row)

    meta_by_number: dict[int, MediaEpisode] = {
        e.episode_number: e
        for e in (
            await session.execute(
                select(MediaEpisode).where(
                    MediaEpisode.media_item_id == item.id,
                    MediaEpisode.season_number == season_number,
                )
            )
        )
        .scalars()
        .all()
    }

    image_base = None
    infos: list[EpisodeInfo] = []
    for number in sorted(set(meta_by_number) | set(by_episode)):
        meta = meta_by_number.get(number)
        rows = by_episode.get(number, [])
        info = EpisodeInfo(
            episode_number=number,
            name=(meta.name if meta else "").strip() or None,
            overview=meta.overview if meta else None,
            air_date=meta.air_date.isoformat() if meta and meta.air_date else None,
            owned=any(row.missing_since is None for row in rows),
            file_ids=[row.id for row in rows if row.id is not None],
        )
        # 剧照：本地资产 → TMDB 图床（经前端缓存代理）
        if meta is not None:
            if meta.still_file:
                # ?v=<mtime>：剧照原地覆盖同一路径，不带版本浏览器会用旧图
                version = asset_version(meta.still_file)
                info.still_url = f"/images/assets/{meta.still_file}?v={version}"
            elif meta.still_path:
                if image_base is None:
                    from movieclaw_api.core.config import get_settings

                    image_base = get_settings().tmdb_image_base_url.rstrip("/")
                info.still_url = f"{image_base}/w300{meta.still_path}"
        # 本地优先：分集 NFO 的标题/简介、同名 -thumb 缩略图（取首个在位文件）
        for row in rows:
            if row.missing_since is not None:
                continue
            video = Path(row.file_path)
            nfo = await asyncio.to_thread(read_episode_metadata, video.with_suffix(".nfo"))
            if nfo is not None:
                info.name = nfo.title or info.name
                info.overview = nfo.plot or info.overview
                info.air_date = info.air_date or nfo.aired
            if await asyncio.to_thread(find_episode_thumb, video) is not None:
                info.still_url = f"/libraries/files/{row.id}/thumb"
            break
        infos.append(info)

    # 条目还没刮削过（该季在库里毫无集数据）才实时兜底，顺带触发后台刮削自愈
    if not meta_by_number and infos:
        await _fill_from_tmdb_season(item, season_number, infos)
        if item.id is not None:
            from movieclaw_api.services.media_scrape import scrape_media_item

            asyncio.get_running_loop().create_task(scrape_media_item(item.id))
    return infos


async def _fill_from_tmdb_season(
    item: MediaItem, season_number: int, infos: list[EpisodeInfo]
) -> None:
    """TMDB 分季详情兜底：只填空缺字段，绝不覆盖本地刮削成果。失败静默
    （分集区退化为无剧照/无简介，不阻断）。"""
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.media_discover import get_tmdb_client

    settings = get_settings()
    try:
        # 与条目展示信息同一套持久缓存：分季数据首访之后秒开
        data = await _get_display_cache().get_or_fetch(
            f"season:{item.tmdb_id}:{season_number}",
            fresh_ttl=_DISPLAY_CACHE_FRESH_TTL,
            stale_ttl=_DISPLAY_CACHE_STALE_TTL,
            factory=lambda: get_tmdb_client().get(
                f"tv/{item.tmdb_id}/season/{season_number}",
                {"language": settings.tmdb_language},
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- 兜底信息拉不到不阻断
        logger.warning(
            "条目 #%s 第 %s 季的 TMDB 分集信息拉取失败（分集区降级）：%s",
            item.id,
            season_number,
            exc,
        )
        return
    image_base = settings.tmdb_image_base_url.rstrip("/")
    remote = {e.get("episode_number"): e for e in data.get("episodes", [])}
    for info in infos:
        episode = remote.get(info.episode_number)
        if not episode:
            continue
        info.name = info.name or (episode.get("name") or "").strip() or None
        info.overview = info.overview or (episode.get("overview") or "").strip() or None
        info.air_date = info.air_date or episode.get("air_date") or None
        still = episode.get("still_path")
        if info.still_url is None and still:
            info.still_url = f"{image_base}/w300{still}"


async def _fill_actor_thumbs(session: AsyncSession, item: MediaItem, meta: EntryMetadata) -> None:
    """给 NFO 里缺 ``<thumb>`` 或缺 ``<tmdbid>`` 的演员按姓名回填库内档案（就地改 meta）。

    很多刮削器（包括本项目早期版本）只往 NFO 写演员姓名与角色，不写头像，
    详情页的演职员条就是一排灰底占位。而 media_metadata.cast 里的
    ``profile_path`` 通常是全的——数据本来就在库里，只是被"NFO 优先"整份
    挡住了。这里只补空缺的头像与影人 id，姓名/角色一律以 NFO 为准，不动其它字段。
    """
    if item.id is None or not meta.actors:
        return
    # 头像**和**影人 id 都齐了才可以跳过：TMM/Emby 写的 NFO 常见头像全有、
    # <tmdbid> 全无，只看头像就早退会把 id 回填一起跳掉，人物页永远点不开
    if all(actor.thumb and actor.tmdb_person_id is not None for actor in meta.actors):
        return
    from movieclaw_api.core.config import get_settings
    from movieclaw_db.repositories import MediaItemRepository

    row = await MediaItemRepository(session).get_metadata(item.id)
    if row is None or not row.cast:
        return
    by_name = {(c.get("name") or "").strip(): c for c in row.cast if c.get("name")}
    if not by_name:
        return
    image_base = get_settings().tmdb_image_base_url.rstrip("/")
    for actor in meta.actors:
        entry = by_name.get(actor.name.strip())
        if entry is None:
            continue
        if not actor.thumb and entry.get("profile_path"):
            actor.thumb = f"{image_base}/w300{entry['profile_path']}"
        # 影人 id 同理按姓名回填：第三方刮削器写的 NFO 通常没有 <actor><tmdbid>，
        # 而库内档案里有——补上这一格才点得开人物页
        if actor.tmdb_person_id is None and entry.get("tmdb_person_id"):
            actor.tmdb_person_id = int(entry["tmdb_person_id"])


async def _db_meta(session: AsyncSession, item: MediaItem) -> EntryMetadata | None:
    """库内刮削档案（media_metadata）→ 详情页展示元数据。

    标记 ``source="db"``，前端据此注明"信息来自本地档案"。演员头像用
    TMDB 图床地址（经前端缓存代理），头像不入本地资产（几十 KB × 全库
    演员数不值得，缓存代理已经把二次访问变成本地读）。
    """
    if item.id is None:
        return None
    from movieclaw_api.core.config import get_settings
    from movieclaw_db.repositories import MediaItemRepository

    row = await MediaItemRepository(session).get_metadata(item.id)
    if row is None or row.scraped_at is None:
        return None
    image_base = get_settings().tmdb_image_base_url.rstrip("/")
    meta = EntryMetadata(
        plot=row.overview,
        rating=row.vote_average,
        runtime_minutes=row.runtime_minutes,
        genres=list(row.genres),
        directors=list(row.directors),
        actors=[
            NfoActor(
                name=actor["name"],
                role=actor.get("character") or None,
                thumb=(
                    f"{image_base}/w300{actor['profile_path']}"
                    if actor.get("profile_path")
                    else None
                ),
                tmdb_person_id=actor.get("tmdb_person_id"),
            )
            for actor in row.cast
            if actor.get("name")
        ],
        source="db",
    )
    return meta if meta.has_content() else None


async def _tmdb_fallback_meta(item: MediaItem) -> EntryMetadata | None:
    """本地无刮削 NFO 时的展示兜底：TMDB 实时拉简介/评分/片长/演职员。

    一次 append_to_response=credits 请求拿全；网络失败返回 None（页面退回
    "本地未刮削"提示，不阻断详情）。结果标记 ``source="tmdb"``，前端据此
    注明信息来自 TMDB 而非本地刮削。
    """
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.media_discover import get_tmdb_client
    from movieclaw_media.models import MediaKind as _Kind

    settings = get_settings()
    kind = _Kind(item.kind)
    try:
        # 经持久缓存（SWR）：首次访问后同条目秒开，过期先回旧值后台刷新
        data = await _get_display_cache().get_or_fetch(
            f"meta:{kind.value}:{item.tmdb_id}",
            fresh_ttl=_DISPLAY_CACHE_FRESH_TTL,
            stale_ttl=_DISPLAY_CACHE_STALE_TTL,
            factory=lambda: get_tmdb_client().get(
                f"{kind.value}/{item.tmdb_id}",
                {"language": settings.tmdb_language, "append_to_response": "credits"},
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- 兜底信息拉不到不阻断详情页
        logger.warning("条目 #%s 的 TMDB 展示信息拉取失败（详情页降级）：%s", item.id, exc)
        return None

    credits = data.get("credits") or {}
    if kind is _Kind.MOVIE:
        directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"]
        runtime = data.get("runtime")
    else:
        directors = [c["name"] for c in data.get("created_by", []) if c.get("name")]
        run_times = data.get("episode_run_time") or []
        runtime = run_times[0] if run_times else None
    image_base = settings.tmdb_image_base_url.rstrip("/")
    actors = [
        NfoActor(
            name=c["name"],
            role=(c.get("character") or "").strip() or None,
            thumb=f"{image_base}/w300{c['profile_path']}" if c.get("profile_path") else None,
            tmdb_person_id=c.get("id"),
        )
        for c in credits.get("cast", [])[:40]
        if c.get("name")
    ]
    vote = data.get("vote_average")
    meta = EntryMetadata(
        plot=(data.get("overview") or "").strip() or None,
        rating=round(float(vote), 1) if vote else None,
        runtime_minutes=int(runtime) if runtime else None,
        genres=[g["name"] for g in data.get("genres", []) if g.get("name")],
        directors=directors[:5],
        actors=actors,
        source="tmdb",
    )
    return meta if meta.has_content() else None


async def backfill_streams_for_files(file_ids: list[int]) -> None:
    """详情响应后的后台补探入口（自开会话；FastAPI BackgroundTasks 调用）。

    探测结果落库后，前端对"尚未探测"的文件会静默补拉一次详情呈现结果
    ——首屏不再被 ffprobe 拖住，规格晚几秒到但页面秒开。
    """
    if not file_ids:
        return
    from sqlmodel import select

    from movieclaw_db.engine import get_database

    db = get_database()
    try:
        async with db.session() as session:
            rows = list(
                (await session.execute(select(LibraryFile).where(LibraryFile.id.in_(file_ids))))  # type: ignore[attr-defined]
                .scalars()
                .all()
            )
            await backfill_streams(session, rows)
    except Exception:  # noqa: BLE001 -- 后台任务兜底，失败下次打开详情页再试
        logger.exception("后台补探介质规格失败（涉及台账行 %s）", file_ids)


async def backfill_streams(
    session: AsyncSession,
    files: list[LibraryFile],
    *,
    limit: int | None = _DETAIL_PROBE_LIMIT,
    on_probed: Callable[[], bool] | None = None,
) -> int:
    """给没探测过音轨/字幕轨的在位台账行按需补探并回填，返回实际探了几个。

    这是「ffprobe 后装」的唯一救赎路径——扫描对已识别且在位的行整体秒过，
    不会回头重探。两个调用方：

    - 详情页响应后的后台任务：``limit`` 取默认值限量，防止百集剧首开卡死；
    - 扫描的补探阶段：``limit=None`` 不限量（那是有进度与停止按钮的后台
      任务，慢没关系，半途而废才是问题），用 ``on_probed`` 汇报进度——
      回调返回 False 即收尾（用户点了停止）。

    探测失败的行保持 NULL、下次再试：失败常常是暂时的（挂载还没就绪）。
    代价是永远探不出的坏文件每轮都会被重试一次，坏文件多的库要留意。
    """
    from movieclaw_api.services.library.scan import disc_main_stream

    probed = 0
    since_commit = 0
    for row in files:
        if limit is not None and probed >= limit:
            break
        if row.audio_streams is not None or row.missing_since is not None:
            continue
        if row.file_path.lower().endswith(STRM_EXT):
            continue  # strm 占位文件没有媒体流，探了必失败，别每轮白试
        path = Path(row.file_path)
        target = disc_main_stream(path) if row.container in ("bluray", "dvd") else path
        if target is None or not await asyncio.to_thread(target.exists):
            continue
        probed += 1
        spec = await asyncio.to_thread(probe_media, target)
        if spec is not None:
            row.audio_streams = list(spec.audio_streams)
            row.subtitle_streams = list(spec.subtitle_streams)
            # 顺手回填缺失的视频规格（同一次探测的免费产出，不覆盖已有值）
            row.resolution = row.resolution or spec.resolution
            row.video_codec = row.video_codec or spec.video_codec
            row.hdr = row.hdr or spec.hdr
            row.bit_depth = row.bit_depth or spec.bit_depth
            row.duration_seconds = row.duration_seconds or spec.duration_seconds
            row.bit_rate = row.bit_rate or spec.bit_rate
            row.updated_at = utcnow()
            since_commit += 1
        # 分批提交而不是攒到最后：整库补探可能要几个小时，中途断电/重启
        # 时已经探完的那部分不该白探
        if since_commit >= _PROBE_COMMIT_EVERY:
            await session.commit()
            since_commit = 0
        if on_probed is not None and not on_probed():
            break
    if since_commit:
        await session.commit()
    return probed


# ---------------------------------------------------------------------------
# 真实删除（唯一会动磁盘的删除路径，务必克制）
# ---------------------------------------------------------------------------


@dataclass
class DeleteResult:
    """一次条目删除的结论（全部路径都会回给前端展示）。"""

    removed_paths: list[str] = field(default_factory=list)  # 实际从磁盘删除的目录/文件
    rows_deleted: int = 0  # 删掉的台账行数
    freed_bytes: int = 0  # 释放的空间（按台账 size 估算）
    errors: list[str] = field(default_factory=list)


async def delete_item_files(
    session: AsyncSession,
    library: Library,
    media_item_id: int,
    files: list[LibraryFile],
    all_library_files: list[LibraryFile],
) -> DeleteResult:
    """把条目从库中**彻底删除**：磁盘上的条目目录（视频+NFO+海报+字幕）
    整个清掉，台账行随之删除。

    安全边界：
    - 只删库根路径**之内**的内容，且绝不删根目录本身；
    - 条目目录里混有其他条目的台账文件时，不删目录，只删本条目的文件
      及其同名附属文件（NFO/字幕/图片）；
    - 磁盘删除失败的文件保留台账行（并报错给用户），不制造"账没了文件还在"
      的幽灵——下次扫描会把它当新文件重新入账反而更乱。
    """
    result = DeleteResult()
    roots = [Path(p) for p in library.root_paths]

    # 其他条目占用的路径：判定条目目录是否可整删
    foreign_paths = [
        Path(row.file_path) for row in all_library_files if row.media_item_id != media_item_id
    ]

    dirs_to_remove: list[Path] = []
    files_to_remove: dict[int, Path] = {}  # row.id -> 主文件路径（附属文件删除时一并找）
    covered_rows: dict[Path, list[LibraryFile]] = {}  # 整删目录覆盖的行

    for row in files:
        path = Path(row.file_path)
        entry = entry_dir_of(roots, path)
        if entry is None and row.container in ("bluray", "dvd"):
            entry = path  # 直接躺在根下的原盘目录：目录本身就是条目
        if entry is not None and _safe_inside_roots(entry, roots):
            if any(entry in fp.parents or entry == fp for fp in foreign_paths):
                # 目录里混着其他条目：退化为逐文件删除
                assert row.id is not None
                files_to_remove[row.id] = path
            else:
                if entry not in dirs_to_remove:
                    dirs_to_remove.append(entry)
                covered_rows.setdefault(entry, []).append(row)
        elif _safe_inside_roots(path, roots):
            assert row.id is not None
            files_to_remove[row.id] = path
        else:
            result.errors.append(f"「{path}」不在库根路径之内，已跳过（不删库外文件）")

    deleted_row_ids: set[int] = set()

    for directory in dirs_to_remove:
        ok = await asyncio.to_thread(_remove_tree, directory, result)
        if ok:
            for row in covered_rows.get(directory, []):
                assert row.id is not None
                deleted_row_ids.add(row.id)
                result.freed_bytes += row.size_bytes

    by_id = {row.id: row for row in files}
    for row_id, path in files_to_remove.items():
        row = by_id[row_id]
        ok = await asyncio.to_thread(_remove_file_with_sidecars, path, result)
        if ok:
            deleted_row_ids.add(row_id)
            result.freed_bytes += row.size_bytes

    # missing 行没有磁盘实体，账直接清
    for row in files:
        if row.missing_since is not None:
            assert row.id is not None
            deleted_row_ids.add(row.id)

    for row in files:
        if row.id in deleted_row_ids:
            await session.delete(row)
    result.rows_deleted = len(deleted_row_ids)
    await session.commit()

    if result.removed_paths:
        logger.info(
            "已从磁盘删除条目 #%s 的 %d 个路径（库「%s」，释放约 %.1f GB）：%s",
            media_item_id,
            len(result.removed_paths),
            library.name,
            result.freed_bytes / 1024**3,
            "；".join(result.removed_paths),
        )
    return result


async def delete_single_file(
    session: AsyncSession,
    library: Library,
    row: LibraryFile,
    item_rows: list[LibraryFile],
    all_library_files: list[LibraryFile],
) -> DeleteResult:
    """从磁盘删除条目的**单个文件**（多版本洗版 / 删某一集重下的出口）。

    与整条目删除（``delete_item_files``）的分工：
    - 只删这一个主文件及其同名附属文件（NFO/字幕/图片），条目目录与其他
      文件（含剧集的 tvshow.nfo/海报）纹丝不动；
    - **该行是条目在本库的最后一行时退化为整条目删除**——只删文件会留下
      一个装着 NFO/海报的空刮削目录，下次扫描它还在，违背"不留刮削残渣"
      的原则（调用方须在确认界面明确告知这一升级）；
    - missing 行没有磁盘实体，直接清台账；
    - 磁盘删除失败保留台账行（与整条目删除同规则，不制造幽灵账）。
    """
    assert row.media_item_id is not None
    if len(item_rows) == 1:
        return await delete_item_files(
            session, library, row.media_item_id, item_rows, all_library_files
        )

    result = DeleteResult()
    if row.missing_since is not None:
        await session.delete(row)
        result.rows_deleted = 1
        await session.commit()
        return result

    path = Path(row.file_path)
    roots = [Path(p) for p in library.root_paths]
    if not _safe_inside_roots(path, roots):
        result.errors.append(f"「{path}」不在库根路径之内，已跳过（不删库外文件）")
        return result

    ok = await asyncio.to_thread(_remove_file_with_sidecars, path, result)
    if ok:
        result.rows_deleted = 1
        result.freed_bytes = row.size_bytes
        await session.delete(row)
        await session.commit()
        logger.info(
            "已从磁盘删除条目 #%s 的单个文件（库「%s」，释放约 %.1f GB）：%s",
            row.media_item_id,
            library.name,
            result.freed_bytes / 1024**3,
            "；".join(result.removed_paths),
        )
    return result


def _safe_inside_roots(path: Path, roots: list[Path]) -> bool:
    """路径必须严格位于某个库根之内（不等于根本身）——删除的硬边界。"""
    return any(root in path.parents for root in roots)


def _remove_tree(directory: Path, result: DeleteResult) -> bool:
    """整删条目目录（同步，放线程池）。目录已不存在视为成功（幂等）。"""
    if not directory.exists():
        result.removed_paths.append(str(directory))
        return True
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        result.errors.append(f"删除目录失败：{directory}（{exc}）")
        return False
    result.removed_paths.append(str(directory))
    return True


def _remove_file_with_sidecars(path: Path, result: DeleteResult) -> bool:
    """删单个视频文件及其同名附属文件（NFO/字幕/图片）。

    主文件删除失败返回 False（台账保留）；附属文件失败只记错误不影响结论。
    原盘目录（path 是目录）整目录删除。
    """
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            os.remove(path)
    except OSError as exc:
        result.errors.append(f"删除文件失败：{path}（{exc}）")
        return False
    result.removed_paths.append(str(path))

    if path.suffix:
        stem = path.stem.lower()
        try:
            entries = list(path.parent.iterdir())
        except OSError:
            return True
        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.stem.lower()
            is_sidecar = entry.suffix.lower() in _SUBTITLE_EXTS | {".nfo"} | set(_ART_EXTS)
            if is_sidecar and (
                name == stem or name.startswith(stem + ".") or name.startswith(stem + "-")
            ):
                try:
                    os.remove(entry)
                    result.removed_paths.append(str(entry))
                except OSError as exc:
                    result.errors.append(f"删除附属文件失败：{entry}（{exc}）")
    return True
