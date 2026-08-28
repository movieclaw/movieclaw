"""媒体库 → Jellyfin BaseItemDto 的查询与构建（设计文档 §5）。

数据源映射：
    Movie   = media_item(kind=movie) + 其 library_file 行（多版本 → 多 MediaSource）
    Series  = media_item(kind=tv)
    Season  = media_season（以 library_file 存在的季为准）
    Episode = media_episode ⋈ library_file（(item, season, episode) 数字对）

只输出"有文件"的内容：非在位（state != in_place）或未识别（media_item_id NULL）的
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

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Load, load_only
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
from movieclaw_jellyfin.search import SearchTerm, person_name_matches
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
    # 两段式装载时由骨架查询回填：条目归属库（多库归属取入库最早的那行文件的库，
    # 与整行装载路径下 files 的首行同义）。整行装载路径保持 None，走原逻辑。
    primary_library_id: int | None = None

    # units 的缓存。装载阶段一次性建好 files 的键集合，之后只往各键的列表里
    # 追加行、不再新增键（两段式补料用 setdefault 落在已有键上），所以键集合
    # 一旦算出就不会失效。
    _units: list[tuple[int, int]] | None = field(default=None, repr=False)

    @property
    def units(self) -> list[tuple[int, int]]:
        """有文件的 (season, episode) 单元，季集序。

        一条 Series DTO 就要问三次（ChildCount / RecursiveItemCount / 聚合
        UserData），一次整库浏览是几百次；每次都 ``sorted`` 一遍几百个键
        纯属重复劳动，缓存掉。
        """
        if self._units is None:
            self._units = sorted(self.files)
        return self._units

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


# 叶子单元三元组 (media_item_id, season_number, episode_number)。
# ``None`` = 不限定（装载条目名下全部单元），空集合 = 一个叶子都不渲染
# （Series/Season 这类文件夹条目只吃"哪些单元有文件"的键集合）。
LeafScope = set[tuple[int, int, int]] | None


def _unit_in(item_col, season_col, episode_col, units) -> Any:
    """(条目, 季, 集) 三元组批量匹配。

    用 SQL 行值 ``(a,b,c) IN ((…),(…))``：SQLite ≥3.15 支持，且能直接命中
    ``ix_library_file_media_unit`` 复合索引（实测计划为 COVERING INDEX
    精确查找）。相比按条目 id 粗筛再在 Python 里过滤，它不会把一部 200 集的
    剧全部读回来只为了取其中一集。
    """
    return tuple_(item_col, season_col, episode_col).in_(sorted(units))


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
        # TMDB 路径兜底出 tag（资产未落地时）也在列表路径读，短字符串列
        item_columns.extend([MediaItem.poster_path, MediaItem.backdrop_path])
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
        # 叶子条目的恒输出字段（Container/VideoType 等，_apply_leaf_media_fields）
        # 在列表路径也要读；都是短字符串列，不在"大 JSON 列"限制之列
        LibraryFile.file_path,
        LibraryFile.container,
        LibraryFile.resolution,
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
                LibraryFile.external_subtitles,
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


async def _load_scoped_files(
    session: AsyncSession,
    bundles: dict[int, ItemBundle],
    leaf_scope: set[tuple[int, int, int]],
    file_scope: list[Any],
    summary_columns,
) -> None:
    """只为白名单单元装完整文件行，追加进已建好的骨架。"""
    wanted = [u for u in leaf_scope if u[0] in bundles]
    if not wanted:
        return
    file_q = (
        select(LibraryFile)
        .where(
            *file_scope,
            _unit_in(
                LibraryFile.media_item_id,
                LibraryFile.season_number,
                LibraryFile.episode_number,
                wanted,
            ),
        )
        # 同一单元多版本时的行序即输出序（Path/Container 取第一行、
        # MediaSources 按序展开）。旧路径没有 ORDER BY，靠的是
        # ix_library_file_browse_unit 末列恰好是 created_at 而"碰巧"按入库
        # 时间出行——换了 WHERE 就换计划、换计划就换顺序。这里把那个隐式
        # 次序写成显式约束，顺带让它不再随查询计划漂移。
        .order_by(LibraryFile.created_at, LibraryFile.id)
    )
    if summary_columns is not None:
        file_q = file_q.options(load_only(*summary_columns[2]))
    for f in (await session.execute(file_q)).scalars():
        b = bundles.get(f.media_item_id)
        if b is None:
            continue
        key = (f.season_number, f.episode_number)
        if key not in b.files:
            # 骨架与这里用的是同一套 file_scope，正常不会出现新键；真出现了
            # 就把 units 缓存作废，宁可多排一次序也不能给出错的单元集合
            b.files[key] = []
            b._units = None
        b.files[key].append(f)


async def _load_scoped_episodes(
    session: AsyncSession,
    bundles: dict[int, ItemBundle],
    leaf_scope: set[tuple[int, int, int]],
    summary_columns,
) -> None:
    """只为白名单单元装分集元数据（电影哨兵单元 episode=0 天然不参与）。"""
    wanted = [u for u in leaf_scope if u[0] in bundles and u[2] > 0]
    if not wanted:
        return
    episode_q = select(MediaEpisode).where(
        _unit_in(
            MediaEpisode.media_item_id,
            MediaEpisode.season_number,
            MediaEpisode.episode_number,
            wanted,
        )
    )
    if summary_columns is not None:
        episode_q = episode_q.options(load_only(*summary_columns[4]))
    for e in (await session.execute(episode_q)).scalars():
        b = bundles.get(e.media_item_id)
        if b is not None:
            b.episodes[(e.season_number, e.episode_number)] = e


async def hydrate_leaves(
    session: AsyncSession,
    bundles: dict[int, ItemBundle],
    leaf_scope: set[tuple[int, int, int]],
    *,
    library_id: int | None = None,
    visible_library_ids: set[int] | None = None,
    dto_options: DtoOptions | None = None,
) -> None:
    """给 ``leaf_scope=set()`` 装出来的骨架补上指定单元的文件行与分集元数据。

    列表接口的筛选、排序、分页只吃"哪些单元有文件"和播放状态，直到最后
    才知道这一页要渲染哪些叶子。先按骨架选页、再回头补这一页的料，
    整库浏览就不用为了输出 100 行 Series 而水合几千条文件行和分集元数据。
    """
    if not leaf_scope:
        return
    summary_columns = (
        _list_load_columns(dto_options)
        if dto_options is not None and not dto_options.all_fields
        else None
    )
    file_scope = [
        LibraryFile.media_item_id.in_([u[0] for u in leaf_scope]),
        LibraryFile.in_place(),
    ]
    if library_id is not None:
        file_scope.append(LibraryFile.library_id == library_id)
    if visible_library_ids is not None:
        file_scope.append(LibraryFile.library_id.in_(visible_library_ids))
    await _load_scoped_files(
        session, bundles, leaf_scope, file_scope, summary_columns
    )
    await _load_scoped_episodes(session, bundles, leaf_scope, summary_columns)


async def load_bundles(
    session: AsyncSession,
    item_ids: list[int],
    *,
    member_id: int = 0,
    library_id: int | None = None,
    visible_library_ids: set[int] | None = None,
    include_people: bool = False,
    include_fileless: bool = False,
    dto_options: DtoOptions | None = None,
    leaf_scope: LeafScope = None,
    include_seasons: bool = True,
) -> dict[int, ItemBundle]:
    """批量装载条目素材。库参数限定时只装对应范围内的文件行。

    ``member_id``：观看者（0=超管哨兵）——bundle 里的播放状态只装该
    成员自己的行，UserData（进度/已看/收藏）随之按人投影。

    ``visible_library_ids``：成员可见库范围。条目可能同时存在于多个库，
    不能只在进入详情前判断“至少有一份可见”，否则 DTO 会夹带隐藏库的
    MediaSourceId，播放器选择后又在取流层被 404，形成“能看见但播不了”。

    ``include_people`` 只在输出会用到 People 时为 True（fields=People 或
    单条目全字段）：演职员是量最大的关联（条目数 × 十余人的 join +
    ORM 水合），列表请求默认不带 fields=People，装了也是白装——1200 部
    电影的库这一项就要秒级开销（issue #88）。

    ``dto_options`` 仅由列表接口传入。它触发最小列集读取，避免列表在不输出
    演员、音轨和字幕时仍反序列化这些大 JSON 列；全字段详情和未迁移的调用
    保持原有整行读取语义。

    ``leaf_scope`` 是"这次响应真正会渲染成叶子条目（Movie/Episode）的单元"
    白名单，由调用方在装载前就能算出来时传入（Latest 选完页、Resume 选完页、
    库分页选完页、Seasons 一个叶子都不渲染 → 空集）。传了它就走两段式装载：
    单元键用列查询建骨架，完整文件行与分集元数据只装白名单内的。
    **传入的白名单必须覆盖后续所有会构建 DTO 的单元**，漏传会让那些单元
    的 Path/MediaSources/RunTimeTicks 变空——`tests/jellyfin` 与
    `scripts/perf/bench_jellyfin_scan.py --compare` 的逐字节比对守着这条线。
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

    # 条目与它的档案是一对一，合成一条 LEFT JOIN 取回：每条查询在 aiosqlite
    # 下都是一次线程往返，装一个 bundle 本来就要打七八条，能并的都并掉
    item_q = (
        select(MediaItem, MediaMetadata)
        .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)
        .where(MediaItem.id.in_(item_ids))
    )
    if summary_columns is not None:
        item_q = item_q.options(
            Load(MediaItem).load_only(*summary_columns[0]),
            Load(MediaMetadata).load_only(*summary_columns[1]),
        )
    items: list[MediaItem] = []
    bundles: dict[int, ItemBundle] = {}
    for item, meta in (await session.execute(item_q)).all():
        items.append(item)
        bundles[item.id] = ItemBundle(item=item, metadata=meta)

    file_scope = [
        LibraryFile.media_item_id.in_(item_ids),
        LibraryFile.in_place(),
    ]
    if library_id is not None:
        file_scope.append(LibraryFile.library_id == library_id)
    if visible_library_ids is not None:
        file_scope.append(LibraryFile.library_id.in_(visible_library_ids))

    tv_ids = [i.id for i in items if i.kind == "tv"]
    if leaf_scope is None:
        # 全量装载：单条目详情、以及排序键需要每个单元文件行的列表路径。
        # 这里刻意不加 ORDER BY——整库口径下那是一次几千行的临时排序，而行序
        # 本来就由 ix_library_file_browse_unit 给出（末列 created_at），
        # 补一道显式排序只是把同样的结果重算一遍。两段式那条路径行数很少、
        # 又换了 WHERE 会换计划，才需要显式定序（见 _load_scoped_files）。
        file_q = select(LibraryFile).where(*file_scope)
        if summary_columns is not None:
            file_q = file_q.options(load_only(*summary_columns[2]))
        for f in (await session.execute(file_q)).scalars():
            b = bundles.get(f.media_item_id)
            if b is not None:
                b.files.setdefault((f.season_number, f.episode_number), []).append(f)
    else:
        # 两段式装载（列表路径的默认姿势）：先用**列查询**把"哪些单元有文件"
        # 的骨架建起来——units/ChildCount/RecursiveItemCount/聚合 UserData
        # 只认这个键集合，不需要文件行本身；再只为真正会渲染成叶子条目
        # （Movie/Episode）的单元装完整行。
        #
        # 为什么值得这么绕：一次 Latest 只输出 20 条，旧路径却要为这 20 个条目
        # 把它们名下**全部**文件行和分集元数据水合成 ORM 对象（生产实测 1499 行），
        # 一个剧集库的浏览更是要水合整库（3114 文件 + 3416 集）。ORM 对象构造
        # 和大 JSON 列反序列化是这条链路上最大的一笔开销，而其中 99% 的行
        # 从头到尾没人读。
        #
        # 骨架里刻意不取 created_at：DATETIME 列每行都要在 Python 侧解析成
        # datetime 对象，几千行下来比其余整型列加起来还贵。
        # 归属库（ParentId 用）要与整行装载路径同义 = 入库最早那行文件的库。
        # 查询已按库收口时答案就是那个库，一列都不用多取；否则条目也几乎总是
        # 只属于一个库（生产实测 855 个条目里跨库的只有 1 个），扫骨架时顺手
        # 判掉即可，真出现跨库条目才为那几个单独做一次入库时间比较。
        unit_cols = [
            LibraryFile.media_item_id,
            LibraryFile.season_number,
            LibraryFile.episode_number,
        ]
        straddling: set[int] = set()
        if library_id is not None:
            for b in bundles.values():
                b.primary_library_id = library_id
            for mid, season_no, episode_no in (
                await session.execute(select(*unit_cols).where(*file_scope))
            ).all():
                b = bundles.get(mid)
                if b is not None:
                    b.files.setdefault((season_no, episode_no), [])
        else:
            unit_q = select(*unit_cols, LibraryFile.library_id).where(*file_scope)
            for mid, season_no, episode_no, lib in (
                await session.execute(unit_q)
            ).all():
                b = bundles.get(mid)
                if b is None:
                    continue
                b.files.setdefault((season_no, episode_no), [])
                if b.primary_library_id is None:
                    b.primary_library_id = lib
                elif b.primary_library_id != lib:
                    straddling.add(mid)
        if straddling:
            # SQLite 明文保证：聚合里只有一个 min()/max() 时，同 SELECT 的裸列
            # 取自命中该聚合的那一行。入库时间精确到微秒都相同的并列由 SQLite
            # 任选（要求同一条目在两个库里最早的文件时间戳完全相同，现实中不会
            # 发生），其余情况与旧路径逐行比较的结果一致。
            owner_q = (
                select(
                    LibraryFile.media_item_id,
                    LibraryFile.library_id,
                    func.min(LibraryFile.created_at),
                )
                .where(*file_scope, LibraryFile.media_item_id.in_(straddling))
                .group_by(LibraryFile.media_item_id)
            )
            for mid, lib, _created in (await session.execute(owner_q)).all():
                b = bundles.get(mid)
                if b is not None:
                    b.primary_library_id = lib
        await _load_scoped_files(
            session, bundles, leaf_scope, file_scope, summary_columns
        )

    if tv_ids and include_seasons:
        # 季元数据只有 Season/Episode DTO 会读（季名、季海报继承）。一部剧
        # 十几行不构成开销，但整库浏览只输出 Series 行时是几百行的白装，
        # 调用方明确不产出季/集条目就跳过（include_seasons=False）。
        season_q = select(MediaSeason).where(MediaSeason.media_item_id.in_(tv_ids))
        if summary_columns is not None:
            season_q = season_q.options(load_only(*summary_columns[3]))
        for s in (await session.execute(season_q)).scalars():
            bundles[s.media_item_id].seasons[s.season_number] = s
    if tv_ids and leaf_scope is None:
        episode_q = select(MediaEpisode).where(MediaEpisode.media_item_id.in_(tv_ids))
        if summary_columns is not None:
            episode_q = episode_q.options(load_only(*summary_columns[4]))
        for e in (await session.execute(episode_q)).scalars():
            bundles[e.media_item_id].episodes[(e.season_number, e.episode_number)] = e
    elif leaf_scope is not None:
        await _load_scoped_episodes(session, bundles, leaf_scope, summary_columns)

    for st in (
        await session.execute(
            select(PlaybackState).where(
                PlaybackState.media_item_id.in_(item_ids),
                PlaybackState.member_id == member_id,
            )
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

    # 没有任何在位文件的条目不对外呈现；标记类接口除外（include_fileless）——
    # 真 Jellyfin 里文件丢失的条目仍可手动标记已看，响应体要能算聚合 UserData
    if include_fileless:
        return bundles
    return {k: v for k, v in bundles.items() if v.files}


async def latest_unit_candidates(
    session: AsyncSession,
    *,
    member_id: int = 0,
    library_id: int | None = None,
    visible_library_ids: set[int] | None = None,
    is_played: bool | None = None,
    row_limit: int | None = None,
) -> list[LatestUnitCandidate]:
    """只查询 Latest 的排序单元，延后到选页后再装载 bundle。

    旧路径先把整个库的 ``MediaItem``、元数据、文件和播放状态全部水合，
    然后才按 ``created_at`` 排序并取 20 条。这里把同一条目/季/集的文件
    聚合成一行，播放状态也在数据库侧筛掉；返回固定的小标量列，不会
    触发列表 DTO 不需要的 JSON 反序列化。``min(file.id)`` 只用于复现旧
    路径在入库时间相同的情况下的稳定顺序，不参与业务语义。

    ``row_limit`` 把"只要最新的前 N 个单元"下推到 SQL。排序是全序（入库时间
    之后还有四级 tiebreak），所以取前缀与"取全部再切片"结果完全一致。不下推
    的话，一次只输出 20 条的 Latest 要把全库六千多个单元逐行搬进 Python 再扔掉
    ——调用方按需放大 N 重试即可覆盖同剧聚合把多行折叠成一条的情况。
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
            LibraryFile.in_place(),
        )
    )
    if library_id is not None:
        q = q.where(LibraryFile.library_id == library_id)
    if visible_library_ids is not None:
        q = q.where(LibraryFile.library_id.in_(visible_library_ids))
    if is_played is not None:
        q = q.outerjoin(
            PlaybackState,
            and_(
                PlaybackState.media_item_id == LibraryFile.media_item_id,
                PlaybackState.season_number == LibraryFile.season_number,
                PlaybackState.episode_number == LibraryFile.episode_number,
                PlaybackState.member_id == member_id,
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
    if row_limit is not None:
        q = q.limit(row_limit)
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


async def resume_unit_candidates(
    session: AsyncSession,
    *,
    member_id: int = 0,
    visible_library_ids: set[int] | None = None,
) -> list[ResumeUnitCandidate]:
    """查询该成员有续播位置且文件仍在位的单元，不加载未进入结果页的 bundle。"""
    file_conditions = [
        LibraryFile.media_item_id == PlaybackState.media_item_id,
        LibraryFile.season_number == PlaybackState.season_number,
        LibraryFile.episode_number == PlaybackState.episode_number,
        LibraryFile.in_place(),
    ]
    if visible_library_ids is not None:
        file_conditions.append(LibraryFile.library_id.in_(visible_library_ids))
    file_exists = select(LibraryFile.id).where(*file_conditions).exists()
    q = (
        select(
            PlaybackState.media_item_id,
            PlaybackState.season_number,
            PlaybackState.episode_number,
        )
        .join(MediaItem, MediaItem.id == PlaybackState.media_item_id)
        .where(
            PlaybackState.member_id == member_id,
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
    session: AsyncSession, *, member_id: int = 0, series_id: int | None = None
) -> list[int]:
    """返回该成员有在位文件和播放活动的剧集 id，供 NextUp 延后加载。"""
    file_exists = (
        select(LibraryFile.id)
        .where(
            LibraryFile.media_item_id == PlaybackState.media_item_id,
            LibraryFile.season_number == PlaybackState.season_number,
            LibraryFile.episode_number == PlaybackState.episode_number,
            LibraryFile.in_place(),
        )
        .exists()
    )
    q = (
        select(PlaybackState.media_item_id)
        .join(MediaItem, MediaItem.id == PlaybackState.media_item_id)
        .where(
            MediaItem.kind == "tv",
            PlaybackState.member_id == member_id,
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
            LibraryFile.in_place(),
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
    visible_library_ids: set[int] | None = None,
) -> list[int]:
    """有在位文件的条目 id 集合（粗筛）。``visible_library_ids`` 限定成员
    可见库（None=不受限）——跨库递归查询的可见性收口点。"""
    q = (
        select(LibraryFile.media_item_id)
        .where(LibraryFile.media_item_id.is_not(None), LibraryFile.in_place())
        .distinct()
    )
    if library_id is not None:
        q = q.where(LibraryFile.library_id == library_id)
    if visible_library_ids is not None:
        q = q.where(LibraryFile.library_id.in_(visible_library_ids))
    ids = [row for row in (await session.execute(q)).scalars()]
    if kind is None or not ids:
        return ids
    kind_ids = (
        await session.execute(
            select(MediaItem.id).where(MediaItem.id.in_(ids), MediaItem.kind == kind)
        )
    ).scalars()
    return list(kind_ids)


async def search_candidate_item_ids(
    session: AsyncSession, term: SearchTerm, candidate_ids: list[int]
) -> set[int]:
    """搜索粗筛：候选条目里，自身**或其任一季/集**能命中搜索词的条目 id。

    为什么要先粗筛
    --------------
    带 searchTerm 的查询原先要把候选条目全部水合成 bundle（条目 ⋈ 文件 ⋈ 季 ⋈
    集 ⋈ 元数据 ⋈ 播放状态）之后才在内存里做子串过滤——命中 1 条也得先装完
    整库。Infuse 是逐字符发请求且不取消上一个，几个并发就把单次拖到 5~8 秒
    （实测千级条目的库 1.4 秒/次）。这里改成先用「只取名字列」的轻量投影选出
    命中的条目 id，水合只覆盖真正要输出的那几条。

    只查三张表的名字列（不 join、不反序列化 JSON 大列），因此代价与库规模
    线性但常数极小。季/集也纳入判定是因为 Jellyfin 里 Season/Episode 是各自
    独立的 BaseItem，有自己的 Name——搜集名在真 Jellyfin 是能搜到的。
    最终每条候选是否真的进结果，仍由调用方按条目类型逐条判定（见
    ``routes/library.py`` 的 ``_entry_search_match``），这里只负责收窄水合面。
    """
    if not candidate_ids or term.empty:
        return set()
    matched: set[int] = set()

    rows = await session.execute(
        select(MediaItem.id, MediaItem.title, MediaItem.original_title).where(
            MediaItem.id.in_(candidate_ids)
        )
    )
    for item_id, title, original_title in rows:
        if term.matches(title, original_title):
            matched.add(item_id)

    season_rows = await session.execute(
        select(MediaSeason.media_item_id, MediaSeason.name).where(
            MediaSeason.media_item_id.in_(candidate_ids)
        )
    )
    for item_id, name in season_rows:
        if item_id not in matched and term.matches(name):
            matched.add(item_id)

    episode_rows = await session.execute(
        select(MediaEpisode.media_item_id, MediaEpisode.name).where(
            MediaEpisode.media_item_id.in_(candidate_ids)
        )
    )
    for item_id, name in episode_rows:
        if item_id not in matched and term.matches(name):
            matched.add(item_id)

    return matched


async def query_persons(
    session: AsyncSession,
    *,
    name_contains: str | None = None,
    name_starts_with: str | None = None,
    name_less_than: str | None = None,
    name_starts_with_or_greater: str | None = None,
    appears_in_item_id: int | None = None,
    person_types: set[str] | None = None,
    exclude_person_types: set[str] | None = None,
    visible_item_ids: list[int] | None = None,
    start_index: int = 0,
    limit: int = 0,
) -> tuple[int, list[Person]]:
    """人物查询（PeopleRepository.cs:270-360 的等价筛选），返回 (总数, 该页)。

    ``visible_item_ids`` 是可见性收口：人物不属于任何库，真 Jellyfin 靠
    「至少在一部该用户能看到的片里有署名」来判定（PersonsController.cs:128
    的 BuildAccessFilter），这里同构——传入的是该观看者可见的条目 id。

    两段式：先只取 (id, name) 做结构筛选与姓名匹配，分页切完才水合整行。
    人物表是万级的，全量水合 ORM 行只为返回一页 24 条不值当；而
    ``NameContains`` 的口径（Unicode 大写子串）SQLite 的 LIKE 表达不出来
    （LIKE 只对 ASCII 大小写不敏感，重音字母会漏），必须在 Python 侧判定。
    """
    projection = select(Person.id, Person.name)
    linked = select(MediaItemPerson.person_id)
    needs_link = False
    if visible_item_ids is not None:
        linked = linked.where(MediaItemPerson.media_item_id.in_(visible_item_ids))
        needs_link = True
    if appears_in_item_id is not None:
        linked = linked.where(MediaItemPerson.media_item_id == appears_in_item_id)
        needs_link = True
    if person_types:
        linked = linked.where(MediaItemPerson.department.in_(person_types))
        needs_link = True
    if exclude_person_types:
        linked = linked.where(MediaItemPerson.department.not_in(exclude_person_types))
        needs_link = True
    if needs_link:
        projection = projection.where(Person.id.in_(linked))

    # StartsWith/LessThan/StartsWithOrGreater 照抄 PeopleRepository.cs:344-357：
    # 过滤值先转小写再比，比对列不转——SQLite 的 LIKE/序比对与之同解
    if name_starts_with:
        projection = projection.where(
            Person.name.like(f"{_escape_like(name_starts_with.lower())}%", escape="\\")
        )
    if name_less_than:
        projection = projection.where(Person.name < name_less_than.lower())
    if name_starts_with_or_greater:
        projection = projection.where(Person.name >= name_starts_with_or_greater.lower())

    rows = list(await session.execute(projection.order_by(Person.name.asc())))
    if name_contains:
        rows = [r for r in rows if person_name_matches(r[1], name_contains)]

    total = len(rows)
    page_rows = rows[start_index : start_index + limit] if limit > 0 else rows[start_index:]
    page_ids = [r[0] for r in page_rows]
    if not page_ids:
        return total, []
    loaded = {
        p.id: p
        for p in (await session.execute(select(Person).where(Person.id.in_(page_ids))))
        .scalars()
    }
    return total, [loaded[i] for i in page_ids if i in loaded]


def _escape_like(value: str) -> str:
    """转义 LIKE 元字符——过滤值里的 % 和 _ 是字面量，不是通配符。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def person_dto(ctx: DtoContext, person: Person) -> dict[str, Any]:
    """人物条目的 BaseItemDto（Type=Person 的 ItemsByName 形态）。

    人物不是媒体，没有 MediaSources/UserData/库归属；Jellyfin 侧由
    `DtoService.GetItemByNameDto` 产出，字段集就是这么窄。
    """
    dto: dict[str, Any] = {
        "Name": person.name,
        "ServerId": ctx.server_id,
        "Id": person_guid(person.id or 0),
        "Type": "Person",
        "MediaType": "Unknown",
        "IsFolder": False,
        "ImageTags": (
            {"Primary": hashlib.md5(person.profile_path.encode()).hexdigest()}  # noqa: S324
            if person.profile_path
            else {}
        ),
        "BackdropImageTags": [],
    }
    if person.original_name and person.original_name != person.name:
        dto["OriginalTitle"] = person.original_name
    return dto


async def list_libraries(
    session: AsyncSession, *, visible_ids: set[int] | None = None
) -> list[Library]:
    """全部库；``visible_ids`` 限定成员可见库（None=不受限）。"""
    q = select(Library)
    if visible_ids is not None:
        q = q.where(Library.id.in_(visible_ids))
    return list((await session.execute(q)).scalars())


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
    """条目所属库（多库归属取第一个文件行的库）。

    两段式装载下只有本页的叶子单元装了文件行，"第一个文件行"会随分页漂移，
    所以骨架查询会把口径一致的归属库直接算好放进 ``primary_library_id``，
    这里优先用它。
    """
    if bundle.primary_library_id is not None:
        return library_guid(bundle.primary_library_id)
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


def _tmdb_tag(tmdb_path: str | None) -> str | None:
    """TMDB 图床兜底的图片 tag：资产未落地时也要让客户端来请求图片——
    没有 ImageTags 播放器根本不会发起图片请求，三层解析的兜底层就永远
    走不到（图片端点会经图片代理拉取并缓存）。路径变即 tag 变。"""
    if not tmdb_path:
        return None
    return hashlib.md5(f"tmdb:{tmdb_path}".encode()).hexdigest()


def _apply_item_images(
    dto: dict[str, Any], ctx: DtoContext, bundle: ItemBundle, options: DtoOptions
) -> None:
    if not options.enable_images:
        return
    tags: dict[str, str] = {}
    meta = bundle.metadata
    # 资产已落地 → 零 IO 派生 tag（issue #88 硬约束）；未落地 → TMDB 路径
    # 兜底出 tag（a303b8e：图片接口按三层解析，tag 有值客户端才来取图）
    poster = _asset_tag(
        meta.poster_file if meta else None, meta.updated_at if meta else None
    ) or _tmdb_tag(bundle.item.poster_path)
    if poster:
        tags["Primary"] = poster
    dto["ImageTags"] = tags
    backdrop = _asset_tag(
        meta.backdrop_file if meta else None, meta.updated_at if meta else None
    ) or _tmdb_tag(bundle.item.backdrop_path)
    dto["BackdropImageTags"] = [backdrop] if backdrop else []


# 标称分辨率 → 常见宽高（探测层未落 width/height，与 _video_stream 同源）
_RESOLUTION_WH = {
    "4320p": (7680, 4320),
    "2160p": (3840, 2160),
    "1440p": (2560, 1440),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "480p": (720, 480),
}


def _apply_leaf_media_fields(
    dto: dict[str, Any], files: list[LibraryFile], options: DtoOptions
) -> None:
    """可播叶子（Movie/Episode）的下载与介质字段（对齐 DtoService）。

    CanDownload 是客户端渲染"下载按钮"的依据：真 Jellyfin 里只有 Video
    子类返回 true、Series/Season 等 Folder 恒 false——缺了它，客户端退回
    只看 Policy.EnableContentDownloading，会在剧集层级也放行下载，打到
    /Videos/{seriesGuid}/stream 得到 404 空 body 存成 0 字节"成品"。
    CanDelete 按现有 Policy（EnableContentDeletion=false）恒为 false。
    LocationType/VideoType/Container 是真 Jellyfin 的恒输出字段；strm 不给
    Container（浏览态偏离，见 media_source_dto）。
    """
    if options.has("CanDownload"):
        # 有在位文件才可下载；strm 对齐真 Jellyfin（Path 是本地文件 → true）
        dto["CanDownload"] = bool(files)
    if options.has("CanDelete"):
        dto["CanDelete"] = False
    dto["LocationType"] = "FileSystem"
    dto["VideoType"] = "VideoFile"
    if files:
        f = files[0]
        if f.container and not is_strm(f.file_path):
            dto["Container"] = f.container
        wh = _RESOLUTION_WH.get(f.resolution or "")
        if wh:
            if options.has("Width"):
                dto["Width"] = wh[0]
            if options.has("Height"):
                dto["Height"] = wh[1]
            # 真 Jellyfin 只在为 true 时输出 IsHD
            if options.has("IsHD") and wh[1] >= 720:
                dto["IsHD"] = True


def movie_dto(ctx: DtoContext, bundle: ItemBundle, options: DtoOptions) -> dict[str, Any]:
    guid = item_guid(bundle.item.id)
    dto = _common(ctx, guid, bundle.item.title, "Movie", "Video")
    dto["IsFolder"] = False
    _apply_leaf_media_fields(dto, bundle.files.get((0, 0), []), options)
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
    # Folder 不可整体下载（BaseItem.CanDownload 恒 false）；缺失时部分客户端
    # 退回 Policy 全局开关误放行，见 _apply_leaf_media_fields 注释
    if options.has("CanDownload"):
        dto["CanDownload"] = False
    if options.has("CanDelete"):
        dto["CanDelete"] = False
    dto["LocationType"] = "FileSystem"
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
    if options.has("CanDownload"):
        dto["CanDownload"] = False
    if options.has("CanDelete"):
        dto["CanDelete"] = False
    dto["LocationType"] = "FileSystem"
    dto["IndexNumber"] = season
    dto["SeriesId"] = item_guid(bundle.item.id)
    dto["SeriesName"] = bundle.item.title
    # 真 Jellyfin 对 Season 的 ChildCount 不受 fields 门控（DtoService 短路分支
    # ChildCount = RecursiveItemCount），恒输出该季集数
    season_units = [u for u in bundle.units if u[0] == season]
    dto["ChildCount"] = len(season_units)
    if options.has("RecursiveItemCount"):
        dto["RecursiveItemCount"] = len(season_units)
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
    _apply_leaf_media_fields(dto, bundle.files.get((season, episode), []), options)
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
    cover_tag: str | None = None,
) -> dict[str, Any]:
    dto = _common(ctx, library_guid(library.id), library.name, "CollectionFolder", "Unknown")
    dto["IsFolder"] = True
    dto["CollectionType"] = "movies" if library.kind == "movie" else "tvshows"
    # 封面 = 服务端渲染的氛围光货架拼贴（library.cover 服务），tag 即素材指纹
    dto["ImageTags"] = {"Primary": cover_tag} if cover_tag else {}
    dto["BackdropImageTags"] = []
    dto["ParentId"] = root_guid()
    # UserViews 是全字段语义：CollectionFolder 带 ChildCount（库卡片计数）。
    # 直接读 library 上由扫描/入库写路径维护的快照，不再为每次
    # Jellyfin 浏览请求扫描 library_file 全表。
    dto["ChildCount"] = library.stats_item_count
    dto["RecursiveItemCount"] = (
        library.stats_episode_count
        if library.kind == "tv"
        else library.stats_item_count
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


def _video_range(f: LibraryFile) -> tuple[str, str]:
    if not f.hdr:
        # hdr 为空有两种含义（media_probe 三态铁律）：探测跑过、确实是 SDR；
        # 或探测根本没跑（ffprobe 缺失/strm 远程文件）。后者不能妄断 SDR——
        # Jellyfin 语义里 Unknown 让客户端隐藏画质角标，而非挂错误的 SDR 标。
        if f.video_codec or f.resolution or f.bit_depth:
            return "SDR", "SDR"
        return "Unknown", "Unknown"
    normalized = f.hdr.upper()
    if "HLG" in normalized:
        return "HDR", "HLG"
    if "DOLBY" in normalized or normalized == "DV" or "DOVI" in normalized:
        return "HDR", "DOVI"
    return "HDR", "HDR10"


def _resolution_text(f: LibraryFile) -> str:
    if f.resolution:
        return f.resolution.replace("2160p", "4K").replace("4320p", "8K")
    return ""


def _video_stream(f: LibraryFile, index: int) -> dict[str, Any]:
    video_range, range_type = _video_range(f)
    codec = (f.video_codec or "").lower()
    title_parts = [
        p
        for p in (_resolution_text(f), codec.upper(), video_range)
        if p and p != "Unknown"
    ]
    stream: dict[str, Any] = {
        "Type": "Video",
        "Index": index,
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


# 外挂字幕格式 → Jellyfin 惯用 codec 名（真 Jellyfin 由 ffprobe 得出，
# 我们由台账扩展名映射）
_EXTERNAL_SUB_CODEC = {"srt": "subrip", "vtt": "webvtt", "ass": "ass", "ssa": "ssa"}


def _external_subtitle_stream(entry: dict, index: int, media_dir: Path) -> dict[str, Any]:
    """台账外挂字幕元素 → MediaStream（jellyfin-subtitle.md §4.2）。

    DisplayTitle 照内封拼接规则加 " - External" 尾缀；language 解析不出时
    用 title 原文顶格（"简中&英文 - SUBRIP - External" 这类中文命名照样
    可读），两者都有时 title 跟在语言后——AI 生成字幕（title="ai"，
    subtitle-ai-translate.md §0）要靠 "Chinese - ai - …" 在播放器轨列表里
    与人工字幕区分开。SupportsExternalStream/IsTextSubtitleStream 恒
    true——v1 只收文本字幕（SUBTITLE_EXTS 已排除图形格式）。
    """
    lang = entry.get("language")
    fmt = str(entry.get("format") or "").lower()
    codec = _EXTERNAL_SUB_CODEC.get(fmt, fmt)
    lang_display = _lang_display(lang)
    title = entry.get("title")
    parts = [
        lang_display or title or "Und",
        title if (lang_display and title) else None,
        "Default" if entry.get("default") else None,
        "Forced" if entry.get("forced") else None,
        codec.upper() if codec else None,
        "External",
    ]
    filename = entry.get("filename")
    stream: dict[str, Any] = {
        "Type": "Subtitle",
        "Index": index,
        "IsDefault": bool(entry.get("default")),
        "IsForced": bool(entry.get("forced")),
        "IsHearingImpaired": bool(entry.get("sdh")),
        "IsExternal": True,
        "SupportsExternalStream": True,
        "IsTextSubtitleStream": True,
        "DisplayTitle": " - ".join(p for p in parts if p),
    }
    # 真 Jellyfin 在详情 MediaStream.Path 下发服务端绝对路径。客户端不应
    # 直接访问它，但 VidHub 会据此识别外挂流，不能只给 basename。
    if filename:
        stream["Path"] = str(media_dir / filename)
    if codec:
        stream["Codec"] = codec
    if lang:
        stream["Language"] = lang
    if entry.get("title"):
        stream["Title"] = entry["title"]
    return stream


def media_streams_dto(f: LibraryFile) -> list[dict[str, Any]]:
    """合成流编号是 Jellyfin 方言、唯一产地在本层（jellyfin-subtitle.md §4.1）：
    **外挂字幕置前**（台账数组序），再接 video、audio、内封字幕。此顺序
    对齐 Jellyfin master；VidHub 会按官方布局识别外挂流，不能假设只要
    Index 在单次响应内自洽就协议等价。
    编号↔中性轨引用的换算（subtitle_track_for_index 等）必须与本函数同源。
    """
    streams: list[dict[str, Any]] = []
    index = 0
    media_dir = Path(f.file_path).parent
    for entry in f.external_subtitles or []:
        streams.append(_external_subtitle_stream(entry, index, media_dir))
        index += 1
    streams.append(_video_stream(f, index))
    index += 1
    for raw in f.audio_streams or []:
        streams.append(_audio_stream(raw, index))
        index += 1
    for raw in f.subtitle_streams or []:
        streams.append(_subtitle_stream(raw, index))
        index += 1
    return streams


# -- 合成编号 ↔ 中性轨引用（协议层 ↔ 领域层的翻译，§4.1） --------------------


def subtitle_track_for_index(f: LibraryFile, index: int) -> str | None:
    """合成 Index → 字幕的中性轨引用；不在字幕区间返回 None。"""
    from movieclaw_playback.subtitles import embedded_track, external_track

    externals = f.external_subtitles or []
    if 0 <= index < len(externals):
        filename = externals[index].get("filename")
        return external_track(filename) if filename else None
    embedded_base = len(externals) + 1 + len(f.audio_streams or [])
    n_embedded = len(f.subtitle_streams or [])
    if embedded_base <= index < embedded_base + n_embedded:
        return embedded_track(index - embedded_base)
    return None


def index_for_subtitle_track(f: LibraryFile, track: str) -> int | None:
    """中性轨引用 → 合成 Index；引用悬空（轨已不在）返回 None。"""
    from movieclaw_playback.subtitles import parse_embedded_track, parse_external_track

    externals = f.external_subtitles or []
    embedded_base = len(externals) + 1 + len(f.audio_streams or [])
    n_embedded = len(f.subtitle_streams or [])
    k = parse_embedded_track(track)
    if k is not None:
        return embedded_base + k if k < n_embedded else None
    filename = parse_external_track(track)
    if filename is not None:
        for j, entry in enumerate(externals):
            if entry.get("filename") == filename:
                return j
    return None


def audio_track_for_index(f: LibraryFile, index: int) -> str | None:
    """合成 Index → 音轨的中性轨引用；不在音轨区间返回 None。"""
    from movieclaw_playback.subtitles import embedded_track

    audio_base = len(f.external_subtitles or []) + 1
    if audio_base <= index < audio_base + len(f.audio_streams or []):
        return embedded_track(index - audio_base)
    return None


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
