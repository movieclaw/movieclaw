"""媒体库 → Jellyfin BaseItemDto 的查询与构建（设计文档 §5）。

数据源映射：
    Movie   = media_item(kind=movie) + 其 library_file 行（多版本 → 多 MediaSource）
    Series  = media_item(kind=tv)
    Season  = media_season（以 library_file 存在的季为准）
    Episode = media_episode ⋈ library_file（(item, season, episode) 数字对）

只输出"有文件"的内容：missing_since 非空或未识别（media_item_id NULL）的
文件行不进任何列表。复杂 Jellyfin 筛选/排序仍在内存完成；首页高频的
电影列表、Latest 与播放状态查询先在 SQL 取轻量候选，最后才水合 bundle。
这样在不牺牲兼容性的前提下避免读取整库详情 JSON。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import load_only
from sqlmodel.ext.asyncio.session import AsyncSession

from movieclaw_db.models import (
    Library,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaItemPerson,
    MediaMetadata,
    MediaSeason,
    Person,
    PlaybackState,
)
from movieclaw_jellyfin.identity import format_datetime
from movieclaw_jellyfin.ids import (
    episode_guid,
    item_guid,
    library_guid,
    person_guid,
    root_guid,
    season_guid,
)
from movieclaw_playback.streaming import container_mime_type, is_strm  # noqa: F401

TICKS_PER_SECOND = 10_000_000
TICKS_PER_MS = 10_000


# ---------------------------------------------------------------------------
# 输出控制（fields 门控，设计文档 5.3）
# ---------------------------------------------------------------------------


@dataclass
class DtoOptions:
    """一次请求的字段门控。all_fields=True 用于单条目/UserViews（无 fields 参数）。"""

    fields: set[str] = field(default_factory=set)
    enable_user_data: bool = True
    enable_images: bool = True
    all_fields: bool = False

    def has(self, name: str) -> bool:
        return self.all_fields or name in self.fields


# ---------------------------------------------------------------------------
# 数据装载：一次请求相关条目的全量素材（批量查询，避免 N+1）
# ---------------------------------------------------------------------------


@dataclass
class ItemBundle:
    item: MediaItem
    metadata: MediaMetadata | None = None
    # (season, episode) → 文件行列表（多版本多行）；电影用 (0,0)
    files: dict[tuple[int, int], list[LibraryFile]] = field(default_factory=dict)
    seasons: dict[int, MediaSeason] = field(default_factory=dict)
    episodes: dict[tuple[int, int], MediaEpisode] = field(default_factory=dict)
    states: dict[tuple[int, int], PlaybackState] = field(default_factory=dict)
    # (department, character, credit_order, Person)，演员按 credit_order 升序
    people: list[tuple[str, str | None, int, Person]] = field(default_factory=list)

    @property
    def units(self) -> list[tuple[int, int]]:
        """有文件的 (season, episode) 单元，季集序。"""
        return sorted(self.files)

    def state(self, season: int, episode: int) -> PlaybackState | None:
        return self.states.get((season, episode))

    def unit_runtime_ms(self, season: int, episode: int) -> int | None:
        """单元时长（毫秒）：文件探测优先，缺则元数据。"""
        for f in self.files.get((season, episode), []):
            if f.duration_seconds:
                return f.duration_seconds * 1000
        if self.item.kind == "tv":
            ep = self.episodes.get((season, episode))
            if ep and ep.runtime_minutes:
                return ep.runtime_minutes * 60_000
        if self.metadata and self.metadata.runtime_minutes:
            return self.metadata.runtime_minutes * 60_000
        return None


@dataclass(frozen=True)
class LatestUnitCandidate:
    """Latest 的轻量候选行；只含排序、分组和播放过滤所需字段。"""

    created_at: datetime
    media_item_id: int
    kind: str
    season_number: int
    episode_number: int


@dataclass(frozen=True)
class ResumeUnitCandidate:
    """Resume 的轻量候选行，避免为未进入分页的条目水合完整 bundle。"""

    media_item_id: int
    season_number: int
    episode_number: int


def _list_load_columns(
    options: DtoOptions,
) -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any]]:
    """列举列表 DTO、筛选和排序真正会读取的列。

    ``load_bundles`` 的结果会在 session 关闭后才构建 DTO，不能依赖 SQLAlchemy
    的惰性加载；因此这个列集必须覆盖所有列表路径。演员档案、媒体流 JSON 等
    大字段只在客户端明确请求时加入，避免大库首页白白反序列化数千段 JSON。
    """
    item_columns = [
        MediaItem.id,
        MediaItem.kind,
        MediaItem.tmdb_id,
        MediaItem.imdb_id,
        MediaItem.title,
        MediaItem.original_title,
        MediaItem.year,
        MediaItem.aliases,
        MediaItem.status,
        MediaItem.created_at,
    ]
    metadata_columns = [
        MediaMetadata.id,
        MediaMetadata.media_item_id,
        MediaMetadata.content_rating,
        MediaMetadata.vote_average,
        MediaMetadata.release_date,
        MediaMetadata.runtime_minutes,
        MediaMetadata.genres,
    ]
    if options.enable_images:
        metadata_columns.extend(
            [
                MediaMetadata.poster_file,
                MediaMetadata.backdrop_file,
                MediaMetadata.updated_at,
            ]
        )
    if options.has("Overview"):
        metadata_columns.append(MediaMetadata.overview)
    if options.has("Studios"):
        metadata_columns.append(MediaMetadata.studios)
    if options.has("Taglines"):
        metadata_columns.append(MediaMetadata.tagline)

    file_columns = [
        LibraryFile.id,
        LibraryFile.media_item_id,
        LibraryFile.season_number,
        LibraryFile.episode_number,
        LibraryFile.duration_seconds,
        LibraryFile.created_at,
    ]
    if options.has("ParentId"):
        file_columns.append(LibraryFile.library_id)
    if options.has("MediaSources") or options.has("MediaStreams"):
        file_columns.extend(
            [
                LibraryFile.file_path,
                LibraryFile.size_bytes,
                LibraryFile.file_mtime_ns,
                LibraryFile.container,
                LibraryFile.resolution,
                LibraryFile.video_codec,
                LibraryFile.hdr,
                LibraryFile.bit_depth,
                LibraryFile.bit_rate,
                LibraryFile.audio_streams,
                LibraryFile.subtitle_streams,
            ]
        )
    elif options.has("Path"):
        file_columns.append(LibraryFile.file_path)

    season_columns = [
        MediaSeason.id,
        MediaSeason.media_item_id,
        MediaSeason.season_number,
        MediaSeason.name,
        MediaSeason.air_date,
    ]
    if options.enable_images:
        season_columns.extend([MediaSeason.poster_file, MediaSeason.updated_at])

    episode_columns = [
        MediaEpisode.id,
        MediaEpisode.media_item_id,
        MediaEpisode.season_number,
        MediaEpisode.episode_number,
        MediaEpisode.name,
        MediaEpisode.air_date,
        MediaEpisode.runtime_minutes,
        MediaEpisode.vote_average,
    ]
    if options.enable_images:
        episode_columns.extend([MediaEpisode.still_file, MediaEpisode.updated_at])
    if options.has("Overview"):
        episode_columns.append(MediaEpisode.overview)
    return item_columns, metadata_columns, file_columns, season_columns, episode_columns


async def load_bundles(
    session: AsyncSession,
    item_ids: list[int],
    *,
    library_id: int | None = None,
    include_people: bool = False,
    dto_options: DtoOptions | None = None,
) -> dict[int, ItemBundle]:
    """批量装载条目素材。library_id 限定时只装该库的文件行。

    ``include_people`` 只在输出会用到 People 时为 True（fields=People 或
    单条目全字段）：演职员是量最大的关联（条目数 × 十余人的 join +
    ORM 水合），列表请求默认不带 fields=People，装了也是白装——1200 部
    电影的库这一项就要秒级开销（issue #88）。

    ``dto_options`` 仅由列表接口传入。它触发最小列集读取，避免列表在不输出
    演员、音轨和字幕时仍反序列化这些大 JSON 列；全字段详情和未迁移的调用
    保持原有整行读取语义。
    """
    if not item_ids:
        return {}
    summary_columns = (
        _list_load_columns(dto_options)
        if dto_options is not None and not dto_options.all_fields
        else None
    )
    if dto_options is not None:
        include_people = include_people or dto_options.has("People")

    item_q = select(MediaItem).where(MediaItem.id.in_(item_ids))
    if summary_columns is not None:
        item_q = item_q.options(load_only(*summary_columns[0]))
    items = (
        (await session.execute(item_q))
        .scalars()
        .all()
    )
    bundles = {i.id: ItemBundle(item=i) for i in items}

    metadata_q = select(MediaMetadata).where(MediaMetadata.media_item_id.in_(item_ids))
    if summary_columns is not None:
        metadata_q = metadata_q.options(load_only(*summary_columns[1]))
    metas = (
        (await session.execute(metadata_q))
        .scalars()
        .all()
    )
    for m in metas:
        if m.media_item_id in bundles:
            bundles[m.media_item_id].metadata = m

    file_q = select(LibraryFile).where(
        LibraryFile.media_item_id.in_(item_ids),
        LibraryFile.missing_since.is_(None),
    )
    if library_id is not None:
        file_q = file_q.where(LibraryFile.library_id == library_id)
    if summary_columns is not None:
        file_q = file_q.options(load_only(*summary_columns[2]))
    for f in (await session.execute(file_q)).scalars():
        b = bundles.get(f.media_item_id)
        if b is not None:
            b.files.setdefault((f.season_number, f.episode_number), []).append(f)

    tv_ids = [i.id for i in items if i.kind == "tv"]
    if tv_ids:
        season_q = select(MediaSeason).where(MediaSeason.media_item_id.in_(tv_ids))
        if summary_columns is not None:
            season_q = season_q.options(load_only(*summary_columns[3]))
        for s in (await session.execute(season_q)).scalars():
            bundles[s.media_item_id].seasons[s.season_number] = s
        episode_q = select(MediaEpisode).where(MediaEpisode.media_item_id.in_(tv_ids))
        if summary_columns is not None:
            episode_q = episode_q.options(load_only(*summary_columns[4]))
        for e in (await session.execute(episode_q)).scalars():
            bundles[e.media_item_id].episodes[(e.season_number, e.episode_number)] = e

    for st in (
        await session.execute(
            select(PlaybackState).where(PlaybackState.media_item_id.in_(item_ids))
        )
    ).scalars():
        b = bundles.get(st.media_item_id)
        if b is not None:
            b.states[(st.season_number, st.episode_number)] = st

    if include_people:
        # 演职员：media_item_person ⋈ person（人物页同款数据，头像经图片代理）
        people_rows = (
            await session.execute(
                select(MediaItemPerson, Person)
                .join(Person, Person.id == MediaItemPerson.person_id)
                .where(MediaItemPerson.media_item_id.in_(item_ids))
            )
        ).all()
        for link, person in people_rows:
            b = bundles.get(link.media_item_id)
            if b is not None:
                b.people.append(
                    (link.department, link.character, link.credit_order, person)
                )
        for b in bundles.values():
            # 演员在前（按剧组主次序），导演/主创随后——对齐 Jellyfin People 惯例
            b.people.sort(key=lambda t: (0 if t[0] == "cast" else 1, t[2]))

    # 没有任何在位文件的条目不对外呈现
    return {k: v for k, v in bundles.items() if v.files}


async def latest_unit_candidates(
    session: AsyncSession,
    *,
    library_id: int | None = None,
    is_played: bool | None = None,
) -> list[LatestUnitCandidate]:
    """只查询 Latest 的排序单元，延后到选页后再装载 bundle。

    旧路径先把整个库的 ``MediaItem``、元数据、文件和播放状态全部水合，
    然后才按 ``created_at`` 排序并取 20 条。这里把同一条目/季/集的文件
    聚合成一行，播放状态也在数据库侧筛掉；返回固定的小标量列，不会
    触发列表 DTO 不需要的 JSON 反序列化。``min(file.id)`` 只用于复现旧
    路径在入库时间相同的情况下的稳定顺序，不参与业务语义。
    """
    latest_created = func.max(LibraryFile.created_at).label("latest_created")
    q = (
        select(
            LibraryFile.media_item_id,
            LibraryFile.season_number,
            LibraryFile.episode_number,
            MediaItem.kind,
            latest_created,
        )
        .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)
        .where(
            LibraryFile.media_item_id.is_not(None),
            LibraryFile.missing_since.is_(None),
        )
    )
    if library_id is not None:
        q = q.where(LibraryFile.library_id == library_id)
    if is_played is not None:
        q = q.outerjoin(
            PlaybackState,
            and_(
                PlaybackState.media_item_id == LibraryFile.media_item_id,
                PlaybackState.season_number == LibraryFile.season_number,
                PlaybackState.episode_number == LibraryFile.episode_number,
            ),
        )
        if is_played:
            q = q.where(PlaybackState.played.is_(True))
        else:
            q = q.where(
                or_(PlaybackState.id.is_(None), PlaybackState.played.is_(False))
            )
    q = q.group_by(
        LibraryFile.media_item_id,
        LibraryFile.season_number,
        LibraryFile.episode_number,
        MediaItem.kind,
    ).order_by(
        latest_created.desc(),
        func.min(LibraryFile.id).asc(),
        LibraryFile.media_item_id.asc(),
        LibraryFile.season_number.asc(),
        LibraryFile.episode_number.asc(),
    )
    rows = (await session.execute(q)).all()
    return [
        LatestUnitCandidate(
            created_at=row.latest_created,
            media_item_id=row.media_item_id,
            kind=row.kind,
            season_number=row.season_number,
            episode_number=row.episode_number,
        )
        for row in rows
    ]


async def resume_unit_candidates(session: AsyncSession) -> list[ResumeUnitCandidate]:
    """查询有续播位置且文件仍在位的单元，不加载未进入结果页的 bundle。"""
    file_exists = (
        select(LibraryFile.id)
        .where(
            LibraryFile.media_item_id == PlaybackState.media_item_id,
            LibraryFile.season_number == PlaybackState.season_number,
            LibraryFile.episode_number == PlaybackState.episode_number,
            LibraryFile.missing_since.is_(None),
        )
        .exists()
    )
    q = (
        select(
            PlaybackState.media_item_id,
            PlaybackState.season_number,
            PlaybackState.episode_number,
        )
        .join(MediaItem, MediaItem.id == PlaybackState.media_item_id)
        .where(
            PlaybackState.position_ms > 0,
            file_exists,
            or_(
                and_(
                    MediaItem.kind == "movie",
                    PlaybackState.season_number == 0,
                    PlaybackState.episode_number == 0,
                ),
                MediaItem.kind == "tv",
            ),
        )
        .order_by(
            func.coalesce(PlaybackState.last_played_at, MediaItem.created_at).desc(),
            PlaybackState.media_item_id.asc(),
            PlaybackState.season_number.asc(),
            PlaybackState.episode_number.asc(),
        )
    )
    rows = (await session.execute(q)).all()
    return [
        ResumeUnitCandidate(
            media_item_id=row.media_item_id,
            season_number=row.season_number,
            episode_number=row.episode_number,
        )
        for row in rows
    ]


async def next_up_item_ids(
    session: AsyncSession, *, series_id: int | None = None
) -> list[int]:
    """返回有在位文件和播放活动的剧集 id，供 NextUp 延后加载。"""
    file_exists = (
        select(LibraryFile.id)
        .where(
            LibraryFile.media_item_id == PlaybackState.media_item_id,
            LibraryFile.season_number == PlaybackState.season_number,
            LibraryFile.episode_number == PlaybackState.episode_number,
            LibraryFile.missing_since.is_(None),
        )
        .exists()
    )
    q = (
        select(PlaybackState.media_item_id)
        .join(MediaItem, MediaItem.id == PlaybackState.media_item_id)
        .where(
            MediaItem.kind == "tv",
            PlaybackState.season_number != 0,
            or_(PlaybackState.played.is_(True), PlaybackState.position_ms > 0),
            file_exists,
        )
        .distinct()
        .order_by(PlaybackState.media_item_id.asc())
    )
    if series_id is not None:
        q = q.where(PlaybackState.media_item_id == series_id)
    return list((await session.execute(q)).scalars())


async def movie_library_page(
    session: AsyncSession,
    library_id: int,
    *,
    start_index: int,
    limit: int,
) -> tuple[int, list[int]]:
    """电影库无筛选列表的 SQL 计数和分页，只返回最终要装载的条目 id。"""
    file_exists = (
        select(LibraryFile.id)
        .where(
            LibraryFile.library_id == library_id,
            LibraryFile.media_item_id == MediaItem.id,
            LibraryFile.season_number == 0,
            LibraryFile.episode_number == 0,
            LibraryFile.missing_since.is_(None),
        )
        .exists()
    )
    condition = and_(MediaItem.kind == "movie", file_exists)
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(MediaItem).where(condition)
            )
        ).scalar_one()
    )
    q = (
        select(MediaItem.id)
        .where(condition)
        .order_by(MediaItem.id.asc())
        .offset(start_index)
        .limit(limit)
    )
    return total, list((await session.execute(q)).scalars())


async def item_ids_with_files(
    session: AsyncSession,
    *,
    kind: str | None = None,
    library_id: int | None = None,
) -> list[int]:
    """有在位文件的条目 id 集合（粗筛）。"""
    q = (
        select(LibraryFile.media_item_id)
        .where(LibraryFile.media_item_id.is_not(None), LibraryFile.missing_since.is_(None))
        .distinct()
    )
    if library_id is not None:
        q = q.where(LibraryFile.library_id == library_id)
    ids = [row for row in (await session.execute(q)).scalars()]
    if kind is None or not ids:
        return ids
    kind_ids = (
        await session.execute(
            select(MediaItem.id).where(MediaItem.id.in_(ids), MediaItem.kind == kind)
        )
    ).scalars()
    return list(kind_ids)


async def list_libraries(session: AsyncSession) -> list[Library]:
    return list((await session.execute(select(Library))).scalars())


@dataclass
class LibraryStats:
    """库卡片素材：条目数（播放器的库列表用；封面另走拼贴服务）。"""

    item_count: int = 0  # 顶层条目数（电影部数 / 剧集部数）
    episode_count: int = 0


async def load_library_stats(session: AsyncSession) -> dict[int, LibraryStats]:
    """一次性算出全部库的条目数（封面见 library.cover 拼贴服务）。"""
    stats: dict[int, LibraryStats] = {}
    rows = (
        await session.execute(
            select(
                LibraryFile.library_id,
                LibraryFile.media_item_id,
                LibraryFile.season_number,
                LibraryFile.episode_number,
            ).where(
                LibraryFile.media_item_id.is_not(None),
                LibraryFile.missing_since.is_(None),
            )
        )
    ).all()
    per_lib_items: dict[int, set[int]] = {}
    per_lib_units: dict[int, set[tuple[int, int, int]]] = {}
    for r in rows:
        per_lib_items.setdefault(r.library_id, set()).add(r.media_item_id)
        per_lib_units.setdefault(r.library_id, set()).add(
            (r.media_item_id, r.season_number, r.episode_number)
        )
    for lib_id, items in per_lib_items.items():
        stats[lib_id] = LibraryStats(
            item_count=len(items), episode_count=len(per_lib_units[lib_id])
        )
    return stats


# ---------------------------------------------------------------------------
# 图片 tag
# ---------------------------------------------------------------------------


def _asset_tag(rel_path: str | None, version: datetime | None) -> str | None:
    """图片 tag：md5(相对路径 + 所属行 updated_at)——纯缓存语义，零文件系统调用。

    图片由刮削管线写入，重刮必然刷新所属行的 updated_at，tag 随之改变。
    此前按图片文件 mtime 现算，列表请求要对 data/ 逐图 stat 数千次，
    data/ 挂在网络存储上时就是慢源之一（issue #88）。
    """
    if not rel_path:
        return None
    stamp = version.isoformat() if version else ""
    return hashlib.md5(f"{rel_path}:{stamp}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# UserData
# ---------------------------------------------------------------------------


def _leaf_user_data(
    bundle: ItemBundle, season: int, episode: int, guid: str
) -> dict[str, Any]:
    st = bundle.state(season, episode)
    runtime_ms = bundle.unit_runtime_ms(season, episode)
    data: dict[str, Any] = {
        "PlaybackPositionTicks": (st.position_ms if st else 0) * TICKS_PER_MS,
        "PlayCount": st.play_count if st else 0,
        "IsFavorite": bool(st and st.is_favorite),
        "Played": bool(st and st.played),
        "Key": guid,
        "ItemId": guid,
    }
    if st and st.position_ms > 0 and runtime_ms:
        pct = st.position_ms / runtime_ms * 100
        if pct > 0:
            data["PlayedPercentage"] = pct
    if st and st.last_played_at:
        data["LastPlayedDate"] = format_datetime(st.last_played_at)
    return data


def _folder_user_data(
    bundle: ItemBundle, guid: str, *, season: int | None = None
) -> dict[str, Any]:
    """Series/Season 的聚合 UserData（Folder.FillUserDataDtoValues 语义）。"""
    units = [u for u in bundle.units if season is None or u[0] == season]
    played = sum(1 for u in units if (st := bundle.state(*u)) and st.played)
    total = len(units)
    # 文件夹级收藏用哨兵单元：Season → (season, -1)，Series → (-1, -1)
    sentinel = (season, -1) if season is not None else (-1, -1)
    favorite = bool((st := bundle.state(*sentinel)) and st.is_favorite)
    data: dict[str, Any] = {
        "PlaybackPositionTicks": 0,
        "PlayCount": 0,
        "IsFavorite": favorite,
        "UnplayedItemCount": total - played,
        "Key": guid,
        "ItemId": guid,
    }
    if total > 0:
        data["PlayedPercentage"] = played / total * 100
        data["Played"] = played >= total
    else:
        data["Played"] = True
    last = [
        st.last_played_at
        for u in units
        if (st := bundle.state(*u)) and st.last_played_at
    ]
    if last:
        data["LastPlayedDate"] = format_datetime(max(last))
    return data


# ---------------------------------------------------------------------------
# BaseItemDto 构建
# ---------------------------------------------------------------------------


@dataclass
class DtoContext:
    server_id: str
    assets_root: Path


def _common(
    ctx: DtoContext, guid: str, name: str, item_type: str, media_type: str
) -> dict[str, Any]:
    return {
        "Name": name,
        "ServerId": ctx.server_id,
        "Id": guid,
        "Type": item_type,
        "MediaType": media_type,
    }


def _apply_metadata_fields(
    dto: dict[str, Any], bundle: ItemBundle, options: DtoOptions
) -> None:
    meta = bundle.metadata
    if meta is None:
        return
    if meta.content_rating:
        dto["OfficialRating"] = meta.content_rating
    if meta.vote_average and meta.vote_average > 0:
        dto["CommunityRating"] = round(meta.vote_average, 1)
    if meta.release_date:
        dto["PremiereDate"] = meta.release_date.strftime("%Y-%m-%dT00:00:00.0000000Z")
    if options.has("Overview") and meta.overview:
        dto["Overview"] = meta.overview
    if options.has("Genres"):
        dto["Genres"] = list(meta.genres or [])
    if options.has("Studios"):
        dto["Studios"] = [{"Name": s} for s in (meta.studios or [])]
    if options.has("Taglines") and meta.tagline:
        dto["Taglines"] = [meta.tagline]


def people_dto(bundle: ItemBundle) -> list[dict[str, Any]]:
    """BaseItemPerson 列表：Name/Id/Role/Type/PrimaryImageTag（BaseItemPerson.cs）。"""
    result: list[dict[str, Any]] = []
    for department, character, _order, person in bundle.people:
        entry: dict[str, Any] = {
            "Name": person.name,
            "Id": person_guid(person.id),
            "Type": "Actor" if department == "cast" else "Director",
        }
        if character:
            entry["Role"] = character
        if person.profile_path:
            entry["PrimaryImageTag"] = hashlib.md5(
                person.profile_path.encode()
            ).hexdigest()
        result.append(entry)
    return result


def _apply_people(dto: dict[str, Any], bundle: ItemBundle, options: DtoOptions) -> None:
    if options.has("People") and bundle.people:
        dto["People"] = people_dto(bundle)


def _apply_parent_id(dto: dict[str, Any], parent: str | None, options: DtoOptions) -> None:
    if parent and options.has("ParentId"):
        dto["ParentId"] = parent


def _item_library_guid(bundle: ItemBundle) -> str | None:
    """条目所属库（多库归属取第一个文件行的库）。"""
    for files in bundle.files.values():
        for f in files:
            return library_guid(f.library_id)
    return None


def _apply_provider_ids(dto: dict[str, Any], bundle: ItemBundle, options: DtoOptions) -> None:
    if not options.has("ProviderIds"):
        return
    providers: dict[str, str] = {"Tmdb": str(bundle.item.tmdb_id)}
    if bundle.item.imdb_id:
        providers["Imdb"] = bundle.item.imdb_id
    dto["ProviderIds"] = providers


def _apply_item_images(
    dto: dict[str, Any], ctx: DtoContext, bundle: ItemBundle, options: DtoOptions
) -> None:
    if not options.enable_images:
        return
    tags: dict[str, str] = {}
    meta = bundle.metadata
    poster = _asset_tag(meta.poster_file if meta else None, meta.updated_at if meta else None)
    if poster:
        tags["Primary"] = poster
    dto["ImageTags"] = tags
    backdrop = _asset_tag(
        meta.backdrop_file if meta else None, meta.updated_at if meta else None
    )
    dto["BackdropImageTags"] = [backdrop] if backdrop else []


def _unit_can_download(bundle: ItemBundle, season: int, episode: int) -> bool:
    """单元是否可整文件下载（对齐 Video.CanDownload：IsFileProtocol）。

    真 Jellyfin 只有本地文件协议的条目才可下载，strm（Http 协议）恒 false，
    客户端据此隐藏/禁用下载入口。客户端拿不到 CanDownload 时会自己猜下载
    链路（拿播放直链甚至文件路径当下载地址），产生"下载瞬间结束、文件
    不可播"的坏结果——所以这个字段必须输出。
    """
    return any(
        not is_strm(f.file_path) for f in bundle.files.get((season, episode), [])
    )


def movie_dto(ctx: DtoContext, bundle: ItemBundle, options: DtoOptions) -> dict[str, Any]:
    guid = item_guid(bundle.item.id)
    dto = _common(ctx, guid, bundle.item.title, "Movie", "Video")
    dto["IsFolder"] = False
    if options.has("CanDownload"):
        dto["CanDownload"] = _unit_can_download(bundle, 0, 0)
    if bundle.item.year:
        dto["ProductionYear"] = bundle.item.year
    runtime_ms = bundle.unit_runtime_ms(0, 0)
    if runtime_ms:
        dto["RunTimeTicks"] = runtime_ms * TICKS_PER_MS
    if bundle.item.original_title and options.has("OriginalTitle"):
        dto["OriginalTitle"] = bundle.item.original_title
    _apply_metadata_fields(dto, bundle, options)
    _apply_provider_ids(dto, bundle, options)
    _apply_item_images(dto, ctx, bundle, options)
    if options.has("ParentId"):
        _apply_parent_id(dto, _item_library_guid(bundle), options)
    _apply_people(dto, bundle, options)
    if options.has("Path"):
        files = bundle.files.get((0, 0), [])
        if files:
            dto["Path"] = files[0].file_path
    if options.has("DateCreated"):
        files = bundle.files.get((0, 0), [])
        if files:
            dto["DateCreated"] = format_datetime(min(f.created_at for f in files))
    if options.has("MediaSources") or options.has("MediaStreams"):
        sources = [s for f in bundle.files.get((0, 0), []) if (s := media_source_dto(f))]
        if options.has("MediaSources"):
            dto["MediaSources"] = sources
        if options.has("MediaStreams") and sources:
            dto["MediaStreams"] = sources[0]["MediaStreams"]
    if options.enable_user_data:
        dto["UserData"] = _leaf_user_data(bundle, 0, 0, guid)
    return dto


def series_dto(ctx: DtoContext, bundle: ItemBundle, options: DtoOptions) -> dict[str, Any]:
    guid = item_guid(bundle.item.id)
    dto = _common(ctx, guid, bundle.item.title, "Series", "Unknown")
    dto["IsFolder"] = True
    if bundle.item.year:
        dto["ProductionYear"] = bundle.item.year
    status = (bundle.item.status or "").lower()
    if status:
        dto["Status"] = "Ended" if status in ("ended", "canceled") else "Continuing"
    if bundle.metadata and bundle.metadata.runtime_minutes:
        dto["RunTimeTicks"] = bundle.metadata.runtime_minutes * 60_000 * TICKS_PER_MS
    if bundle.item.original_title and options.has("OriginalTitle"):
        dto["OriginalTitle"] = bundle.item.original_title
    _apply_metadata_fields(dto, bundle, options)
    _apply_provider_ids(dto, bundle, options)
    _apply_item_images(dto, ctx, bundle, options)
    if options.has("ParentId"):
        _apply_parent_id(dto, _item_library_guid(bundle), options)
    _apply_people(dto, bundle, options)
    if options.has("ChildCount"):
        dto["ChildCount"] = len({s for s, _ in bundle.units})
    if options.has("RecursiveItemCount"):
        dto["RecursiveItemCount"] = len(bundle.units)
    if options.enable_user_data:
        dto["UserData"] = _folder_user_data(bundle, guid)
    return dto


def season_dto(
    ctx: DtoContext, bundle: ItemBundle, season: int, options: DtoOptions
) -> dict[str, Any]:
    guid = season_guid(bundle.item.id, season)
    row = bundle.seasons.get(season)
    name = (row.name if row else "") or (
        "Specials" if season == 0 else f"Season {season}"
    )
    dto = _common(ctx, guid, name, "Season", "Unknown")
    dto["IsFolder"] = True
    dto["IndexNumber"] = season
    dto["SeriesId"] = item_guid(bundle.item.id)
    dto["SeriesName"] = bundle.item.title
    _apply_parent_id(dto, item_guid(bundle.item.id), options)
    if row and row.air_date:
        dto["PremiereDate"] = row.air_date.strftime("%Y-%m-%dT00:00:00.0000000Z")
        dto["ProductionYear"] = row.air_date.year
    if options.enable_images:
        tags: dict[str, str] = {}
        poster = _asset_tag(
            row.poster_file if row else None, row.updated_at if row else None
        )
        if poster:
            tags["Primary"] = poster
        dto["ImageTags"] = tags
        dto["BackdropImageTags"] = []
        meta = bundle.metadata
        series_poster = _asset_tag(
            meta.poster_file if meta else None, meta.updated_at if meta else None
        )
        if series_poster:
            dto["SeriesPrimaryImageTag"] = series_poster
        series_backdrop = _asset_tag(
            meta.backdrop_file if meta else None, meta.updated_at if meta else None
        )
        if series_backdrop:
            dto["ParentBackdropItemId"] = item_guid(bundle.item.id)
            dto["ParentBackdropImageTags"] = [series_backdrop]
    if options.enable_user_data:
        dto["UserData"] = _folder_user_data(bundle, guid, season=season)
    return dto


def episode_dto(
    ctx: DtoContext, bundle: ItemBundle, season: int, episode: int, options: DtoOptions
) -> dict[str, Any]:
    guid = episode_guid(bundle.item.id, season, episode)
    row = bundle.episodes.get((season, episode))
    name = (row.name if row else "") or f"Episode {episode}"
    dto = _common(ctx, guid, name, "Episode", "Video")
    dto["IsFolder"] = False
    if options.has("CanDownload"):
        dto["CanDownload"] = _unit_can_download(bundle, season, episode)
    dto["IndexNumber"] = episode
    dto["ParentIndexNumber"] = season
    dto["SeriesId"] = item_guid(bundle.item.id)
    dto["SeasonId"] = season_guid(bundle.item.id, season)
    dto["SeriesName"] = bundle.item.title
    _apply_parent_id(dto, season_guid(bundle.item.id, season), options)
    season_row = bundle.seasons.get(season)
    dto["SeasonName"] = ((season_row.name if season_row else "") or
                         ("Specials" if season == 0 else f"Season {season}"))
    if row and row.air_date:
        dto["PremiereDate"] = row.air_date.strftime("%Y-%m-%dT00:00:00.0000000Z")
        dto["ProductionYear"] = row.air_date.year
    runtime_ms = bundle.unit_runtime_ms(season, episode)
    if runtime_ms:
        dto["RunTimeTicks"] = runtime_ms * TICKS_PER_MS
    if options.has("Overview") and row and row.overview:
        dto["Overview"] = row.overview
    if row and row.vote_average and row.vote_average > 0:
        dto["CommunityRating"] = round(row.vote_average, 1)
    if options.enable_images:
        tags = {}
        still = _asset_tag(
            row.still_file if row else None, row.updated_at if row else None
        )
        if still:
            tags["Primary"] = still
        dto["ImageTags"] = tags
        dto["BackdropImageTags"] = []
        # 无自有图时客户端按 Parent* 字段退级：优先季海报，再剧海报/剧背景
        meta = bundle.metadata
        season_poster = _asset_tag(
            season_row.poster_file if season_row else None,
            season_row.updated_at if season_row else None,
        )
        series_poster = _asset_tag(
            meta.poster_file if meta else None, meta.updated_at if meta else None
        )
        if season_poster:
            dto["ParentPrimaryImageItemId"] = season_guid(bundle.item.id, season)
            dto["ParentPrimaryImageTag"] = season_poster
        elif series_poster:
            dto["ParentPrimaryImageItemId"] = item_guid(bundle.item.id)
            dto["ParentPrimaryImageTag"] = series_poster
        if series_poster:
            dto["SeriesPrimaryImageTag"] = series_poster
        series_backdrop = _asset_tag(
            meta.backdrop_file if meta else None, meta.updated_at if meta else None
        )
        if series_backdrop:
            dto["ParentBackdropItemId"] = item_guid(bundle.item.id)
            dto["ParentBackdropImageTags"] = [series_backdrop]
    if options.has("Path"):
        files = bundle.files.get((season, episode), [])
        if files:
            dto["Path"] = files[0].file_path
    if options.has("DateCreated"):
        files = bundle.files.get((season, episode), [])
        if files:
            dto["DateCreated"] = format_datetime(min(f.created_at for f in files))
    if options.has("MediaSources") or options.has("MediaStreams"):
        sources = [
            s for f in bundle.files.get((season, episode), []) if (s := media_source_dto(f))
        ]
        if options.has("MediaSources"):
            dto["MediaSources"] = sources
        if options.has("MediaStreams") and sources:
            dto["MediaStreams"] = sources[0]["MediaStreams"]
    _apply_people(dto, bundle, options)
    if options.enable_user_data:
        dto["UserData"] = _leaf_user_data(bundle, season, episode, guid)
    return dto


def library_view_dto(
    ctx: DtoContext,
    library: Library,
    stats: LibraryStats | None = None,
    cover_tag: str | None = None,
) -> dict[str, Any]:
    dto = _common(ctx, library_guid(library.id), library.name, "CollectionFolder", "Unknown")
    dto["IsFolder"] = True
    dto["CollectionType"] = "movies" if library.kind == "movie" else "tvshows"
    # 封面 = 服务端渲染的氛围光货架拼贴（library.cover 服务），tag 即素材指纹
    dto["ImageTags"] = {"Primary": cover_tag} if cover_tag else {}
    dto["BackdropImageTags"] = []
    dto["ParentId"] = root_guid()
    if stats is not None:
        # UserViews 是全字段语义：CollectionFolder 带 ChildCount（库卡片计数）
        dto["ChildCount"] = stats.item_count
        dto["RecursiveItemCount"] = (
            stats.episode_count if library.kind == "tv" else stats.item_count
        )
    # 库视图不做已看聚合（CollectionFolder.SupportsPlayedStatus=false）
    guid = library_guid(library.id)
    dto["UserData"] = {
        "PlaybackPositionTicks": 0,
        "PlayCount": 0,
        "IsFavorite": False,
        "Played": False,
        "Key": guid,
        "ItemId": guid,
    }
    return dto


# ---------------------------------------------------------------------------
# MediaSource / MediaStream（设计文档 6.2/6.3）
# ---------------------------------------------------------------------------

_LANG_DISPLAY = {
    "chi": "Chinese", "zho": "Chinese", "eng": "English", "jpn": "Japanese",
    "kor": "Korean", "fre": "French", "fra": "French", "ger": "German",
    "deu": "German", "spa": "Spanish", "rus": "Russian", "ita": "Italian",
    "por": "Portuguese", "tha": "Thai", "hin": "Hindi", "ara": "Arabic",
    "can": "Cantonese", "yue": "Cantonese",
}


def _lang_display(code: str | None) -> str | None:
    if not code:
        return None
    return _LANG_DISPLAY.get(code.lower(), code.capitalize())


def _video_range(hdr: str | None) -> tuple[str, str]:
    if not hdr:
        return "SDR", "SDR"
    normalized = hdr.upper()
    if "HLG" in normalized:
        return "HDR", "HLG"
    if "DOLBY" in normalized or normalized == "DV" or "DOVI" in normalized:
        return "HDR", "DOVI"
    return "HDR", "HDR10"


def _resolution_text(f: LibraryFile) -> str:
    if f.resolution:
        return f.resolution.replace("2160p", "4K").replace("4320p", "8K")
    return ""


def _video_stream(f: LibraryFile) -> dict[str, Any]:
    video_range, range_type = _video_range(f.hdr)
    codec = (f.video_codec or "").lower()
    title_parts = [p for p in (_resolution_text(f), codec.upper(), video_range) if p]
    stream: dict[str, Any] = {
        "Type": "Video",
        "Index": 0,
        "IsDefault": True,
        "IsForced": False,
        "IsExternal": False,
        "VideoRange": video_range,
        "VideoRangeType": range_type,
        "DisplayTitle": " ".join(title_parts) or "Video",
    }
    if codec:
        stream["Codec"] = codec
    if f.bit_depth:
        stream["BitDepth"] = f.bit_depth
    if f.bit_rate:
        stream["BitRate"] = f.bit_rate
    if f.resolution:
        # 归一化分辨率标签反推常见宽高（探测层未落 width/height，先给标称值）
        heights = {
            "4320p": (7680, 4320),
            "2160p": (3840, 2160),
            "1440p": (2560, 1440),
            "1080p": (1920, 1080),
            "720p": (1280, 720),
            "480p": (720, 480),
        }
        wh = heights.get(f.resolution)
        if wh:
            stream["Width"], stream["Height"] = wh
    return stream


def _audio_stream(raw: dict, index: int) -> dict[str, Any]:
    lang = raw.get("language")
    codec = (raw.get("codec") or "").lower()
    profile = raw.get("profile")
    channels = raw.get("channels")
    layout = raw.get("channel_layout")
    parts = [
        _lang_display(lang),
        profile or (codec.upper() if codec else None),
        layout or (f"{channels} ch" if channels else None),
        "Default" if raw.get("default") else None,
    ]
    stream: dict[str, Any] = {
        "Type": "Audio",
        "Index": index,
        "IsDefault": bool(raw.get("default")),
        "IsForced": False,
        "IsExternal": False,
        "DisplayTitle": " - ".join(p for p in parts if p) or "Audio",
    }
    if codec:
        stream["Codec"] = codec
    if lang:
        stream["Language"] = lang
    if profile:
        stream["Profile"] = profile
    if channels:
        stream["Channels"] = channels
    if layout:
        stream["ChannelLayout"] = layout
    return stream


def _subtitle_stream(raw: dict, index: int) -> dict[str, Any]:
    lang = raw.get("language")
    codec = (raw.get("codec") or "").lower()
    parts = [
        _lang_display(lang) or "Und",
        "Default" if raw.get("default") else None,
        "Forced" if raw.get("forced") else None,
        codec.upper() if codec else None,
    ]
    stream: dict[str, Any] = {
        "Type": "Subtitle",
        "Index": index,
        "IsDefault": bool(raw.get("default")),
        "IsForced": bool(raw.get("forced")),
        "IsExternal": False,
        "DisplayTitle": " - ".join(p for p in parts if p),
    }
    if codec:
        stream["Codec"] = codec
    if lang:
        stream["Language"] = lang
    return stream


def media_streams_dto(f: LibraryFile) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = [_video_stream(f)]
    index = 1
    for raw in f.audio_streams or []:
        streams.append(_audio_stream(raw, index))
        index += 1
    for raw in f.subtitle_streams or []:
        streams.append(_subtitle_stream(raw, index))
        index += 1
    return streams


def version_name(f: LibraryFile) -> str:
    parts = [p for p in (f.resolution, (f.video_codec or "").upper() or None, f.hdr) if p]
    return " ".join(parts) or Path(f.file_path).stem


def media_source_dto(f: LibraryFile, *, resolve_strm: bool = False) -> dict[str, Any] | None:
    """单个文件版本 → MediaSourceInfo（含 v1.1 核实的 8 个恒输出字段）。

    strm 条目：Protocol=Http、IsRemote=true——客户端 DirectPlay 直连云端，
    服务器零流量。``resolve_strm`` 只在播放协商（PlaybackInfo）时为 True：
    现读 strm 拿云端 URL（直链多带时效签名，不缓存），解析失败（非法/被
    安全条款拒绝）返回 None，调用方把该版本剔除（全部失败 →
    NoCompatibleStream）。列表/详情等浏览场景保持 False：**不读文件**——
    每个 strm 条目读一次文件在云盘挂载上就是一次网络往返（issue #88），
    而浏览场景根本用不到直链，Path 保留 strm 占位路径即可。
    """
    from movieclaw_jellyfin.ids import media_source_guid
    from movieclaw_playback.streaming import resolve_strm_url

    streams = media_streams_dto(f)
    audio_streams = [s for s in streams if s["Type"] == "Audio"]
    audio_index = next(
        (s["Index"] for s in audio_streams if s.get("IsDefault")),
        audio_streams[0]["Index"] if audio_streams else None,
    )

    source: dict[str, Any] = {
        "Protocol": "File",
        "Id": media_source_guid(f.id),
        "Path": f.file_path,
        "Type": "Default",
        "Size": f.size_bytes or None,
        "Name": version_name(f),
        "IsRemote": False,
        "RunTimeTicks": f.duration_seconds * TICKS_PER_SECOND if f.duration_seconds else None,
        "ReadAtNativeFramerate": False,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsTranscoding": False,
        "SupportsDirectStream": True,
        "SupportsDirectPlay": True,
        "IsInfiniteStream": False,
        "UseMostCompatibleTranscodingProfile": False,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "RequiresLooping": False,
        "SupportsProbing": False,
        "VideoType": "VideoFile",
        "MediaStreams": streams,
        "MediaAttachments": [],
        "Formats": [],
        "RequiredHttpHeaders": {},
        "TranscodingSubProtocol": "Http",
        "HasSegments": False,
    }
    if f.container and not is_strm(f.file_path):
        source["Container"] = f.container
    if f.bit_rate:
        source["Bitrate"] = f.bit_rate
    if audio_index is not None:
        source["DefaultAudioStreamIndex"] = audio_index

    if is_strm(f.file_path):
        source["Protocol"] = "Http"
        source["IsRemote"] = True
        if resolve_strm:
            url = resolve_strm_url(f.file_path)
            if url is None:
                return None
            source["Path"] = url
            ext = Path(url.split("?", 1)[0]).suffix.lstrip(".").lower()
            if ext:
                source["Container"] = ext
    else:
        etag = bundle_etag(f)
        if etag:
            source["ETag"] = etag
    return {k: v for k, v in source.items() if v is not None}


def bundle_etag(f: LibraryFile) -> str | None:
    """本地文件的 ETag（对齐 Jellyfin：mtime 派生的 md5）。

    mtime 来自台账列（扫描/入库时落库），**不做文件系统调用**——此前每次
    列表/详情请求都对媒体文件本体逐个 stat，云盘挂载上千余部电影要 20
    多秒（issue #88）。旧行未回填时省略 ETag（纯缓存语义），重扫自动补齐。
    """
    if f.file_mtime_ns is None:
        return None
    return hashlib.md5(str(f.file_mtime_ns).encode()).hexdigest()


# ---------------------------------------------------------------------------
# 排序 / 过滤（内存实现）
# ---------------------------------------------------------------------------


def sort_entries(
    entries: list[dict[str, Any]], sort_by: list[str], sort_orders: list[str]
) -> list[dict[str, Any]]:
    """按 sortBy/sortOrder 排序 DTO 列表；未知键忽略。"""
    if not sort_by:
        return entries
    if sort_by[0] == "Random":
        shuffled = entries[:]
        random.shuffle(shuffled)
        return shuffled

    key_funcs = {
        "SortName": lambda d: (d.get("Name") or "").lower(),
        "Name": lambda d: (d.get("Name") or "").lower(),
        "ProductionYear": lambda d: d.get("ProductionYear") or 0,
        "PremiereDate": lambda d: d.get("PremiereDate") or "",
        "CommunityRating": lambda d: d.get("CommunityRating") or 0,
        "Runtime": lambda d: d.get("RunTimeTicks") or 0,
        "DateCreated": lambda d: d.get("DateCreated") or "",
        "DatePlayed": lambda d: (d.get("UserData") or {}).get("LastPlayedDate") or "",
        "AiredEpisodeOrder": lambda d: (
            d.get("ParentIndexNumber") or 0,
            d.get("IndexNumber") or 0,
        ),
        "ParentIndexNumber": lambda d: (
            d.get("ParentIndexNumber") or 0,
            d.get("IndexNumber") or 0,
        ),
        "IndexNumber": lambda d: d.get("IndexNumber") or 0,
    }
    result = entries
    # 从次要键到主键逐轮稳定排序（未知排序键静默忽略，对齐枚举宽容语义）
    for pos in range(len(sort_by) - 1, -1, -1):
        key_fn = key_funcs.get(sort_by[pos])
        if key_fn is None:
            continue
        order = (
            sort_orders[pos]
            if pos < len(sort_orders)
            else (sort_orders[-1] if sort_orders else "Ascending")
        )
        result = sorted(result, key=key_fn, reverse=order.lower().startswith("desc"))
    return result
