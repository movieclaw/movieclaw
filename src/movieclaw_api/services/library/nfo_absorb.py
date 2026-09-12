"""把条目目录里的 NFO **吸收进库内档案**（docs/design/metadata.md 第 5 节）。

为什么有这一层
--------------
NFO 曾经是**读时**才解析的：详情页每打开一次就回媒体盘读一份，分集区每打开
一次把一季每集都读一遍（40 集的季一次 200 次系统调用）。而 NFO 的内容几乎
从不变——它是刮削器或用户写完就放在那儿的。读时解析等于把「一次性的事实」
摊到每一次浏览上，且摊在 NAS 上最该少碰的那块盘。

改成：**入库/刮削时读一次、写进库内档案，之后读路径一律只读库**。用户改了
NFO 想立刻生效，走「刷新元数据」——那是一次明确的用户动作，重读理所当然。

优先级不变：NFO 压过 TMDB
-------------------------
吸收发生在 TMDB 档案落库**之后**，NFO 里**有值的字段**覆盖上去，没值的保留
TMDB 的。这和改动前「本地 NFO 最优先」是同一个意思，只是判定从读时挪到了写时。

一个刻意的差别：改动前是**整份**取舍——NFO 只要解出实质内容，详情页的简介/
评分/类型/演职员就全部以它为准，NFO 没写的字段显示为空。现在是**逐字段**合并，
只写了 ``<plot>`` 的 NFO 不会再把演职员表清空。这是收敛到更合理的一侧：
用户补一句简介，不该让整部片的演员表消失。

演员表合并
----------
NFO 的演员通常只有姓名/角色，没有头像和 TMDB 影人 id（TMM 写盘时就没有）。
所以吸收时按**姓名**与既有 TMDB 演员表对齐，把头像与影人 id 带过来——这正是
改动前读时 ``_fill_actor_thumbs`` 做的事，只是同样挪到了写时。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from movieclaw_api.services.library.nfo import (
    EntryMetadata,
    read_entry_metadata,
    read_episode_metadata,
)
from movieclaw_db.models import LibraryFile, MediaEpisode, MediaItem, MediaMetadata
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.nfo_absorb")

#: ``nfo_fingerprint`` 的这个取值表示「查过了，条目目录里确实没有 NFO」，
#: 与 NULL（从未查过，存量回填的目标）区分开。
NO_NFO = ""


def fingerprint_of(path: Path) -> str | None:
    """NFO 的轻量指纹；文件不在返回 None。

    媒体目录镜像写完条目 NFO 后也用它记台账（``media_scrape._record_mirrored_nfo``）。
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def entry_nfo_candidates(
    entry_dirs: list[Path], files: list[LibraryFile], kind: MediaKind
) -> list[Path]:
    """条目级 NFO 的候选路径，**顺序即优先级**。

    与改动前读路径（``items._read_meta``）同一套顺序：条目目录的
    movie.nfo / tvshow.nfo 优先，其次各视频文件的同名 .nfo（散装电影惯例）。
    """
    entry_name = "movie.nfo" if kind is MediaKind.MOVIE else "tvshow.nfo"
    candidates = [directory / entry_name for directory in entry_dirs]
    for row in files:
        path = Path(row.file_path)
        if path.suffix:  # 原盘目录没有同名 NFO 一说
            candidates.append(path.with_suffix(".nfo"))
    return candidates


def _pick_entry_nfo(candidates: list[Path]) -> tuple[Path, str, EntryMetadata] | None:
    """取第一份解出实质内容的 NFO（与改动前的取舍一致），带它的指纹。"""
    for nfo in candidates:
        meta = read_entry_metadata(nfo)
        if meta is not None and meta.has_content():
            return nfo, fingerprint_of(nfo) or NO_NFO, meta
    return None


def _merge_cast(nfo_actors, existing_cast: list) -> list[dict]:
    """NFO 演员表 + 既有 TMDB 演员表（按姓名带回头像与影人 id）。

    顺序以 NFO 为准——刮削器写盘的顺序就是它认为的主次。
    """
    by_name = {
        (entry.get("name") or "").strip(): entry
        for entry in existing_cast
        if isinstance(entry, dict) and entry.get("name")
    }
    merged: list[dict] = []
    for order, actor in enumerate(nfo_actors):
        known = by_name.get(actor.name.strip(), {})
        merged.append(
            {
                "name": actor.name,
                "character": actor.role or known.get("character"),
                "order": order,
                "profile_path": known.get("profile_path"),
                # NFO 里的 <thumb> 是 http(s) 绝对地址，与 TMDB 的相对
                # ``profile_path`` 不是一个东西，各存各的位置；展示时前者优先
                "nfo_thumb": actor.thumb or None,
                "tmdb_person_id": actor.tmdb_person_id or known.get("tmdb_person_id"),
            }
        )
    return merged


def apply_entry_nfo(meta_row: MediaMetadata, nfo_path: Path, nfo: EntryMetadata) -> None:
    """把一份解析好的条目 NFO 合并进档案行（只覆盖 NFO 里有值的字段）。"""
    if nfo.plot:
        meta_row.overview = nfo.plot
    if nfo.rating is not None:
        meta_row.vote_average = nfo.rating
    if nfo.runtime_minutes is not None:
        meta_row.runtime_minutes = nfo.runtime_minutes
    if nfo.genres:
        meta_row.genres = list(nfo.genres)
    if nfo.directors:
        meta_row.directors = list(nfo.directors)
    if nfo.actors:
        meta_row.cast = _merge_cast(nfo.actors, list(meta_row.cast))
    meta_row.nfo_name = nfo_path.name


async def absorb_entry_nfo(
    session,  # noqa: ANN001 - AsyncSession，避免为类型再引一次依赖
    item: MediaItem,
    meta_row: MediaMetadata,
    entry_dirs: list[Path],
    files: list[LibraryFile],
    kind: MediaKind,
) -> bool:
    """读条目 NFO 并合并进 ``meta_row``；返回是否吸收到了内容。

    没有可用 NFO 时把台账记成「查过、没有」（``nfo_fingerprint=NO_NFO``），
    并清掉 ``nfo_name``——否则用户删掉 NFO 之后，详情页会一直挂着
    「信息来自 xxx.nfo」的出处标注。**已经写进展示列的内容不回滚**：那是
    上一次吸收的成果，TMDB 侧下一次刷新自然会把它盖回去。

    跳过的判据是 ``nfo_mirror_fingerprint``——**磁盘上这份是不是我们自己写
    出去的**，而不是"这份我们吸收过没有"。媒体目录镜像每次刷新都按库内档案
    重写条目 NFO（6.2 的「NFO 是档案的镜像」），不认出自家副本的话，下一次
    刷新读到的就是我们上一轮写出去的那份，它会把刚拉回来的 TMDB 新数据原样
    盖掉——正是 2026-08-04 决策要消灭的「NFO 挡住新数据」。

    反过来，**用户的 NFO 每次刷新都要重新压上去**，哪怕它一个字节没改：
    刷新时 ``apply_display_profile`` 刚把 TMDB 值无条件写回展示列，不重压
    一次，"NFO 有值的字段压过 TMDB"这条约定就只在第一次吸收时生效了。关掉
    媒体目录镜像的库尤其吃这一刀——磁盘上一直是用户那份、指纹永远不变。
    解析结果有 mtime/大小缓存（``nfo._cached_parse``），重压不等于重读盘。
    """
    candidates = entry_nfo_candidates(entry_dirs, files, kind)
    picked = await asyncio.to_thread(_pick_entry_nfo, candidates)
    if picked is None:
        meta_row.nfo_name = None
        meta_row.nfo_fingerprint = NO_NFO
        session.add(meta_row)
        return False
    nfo_path, fingerprint, nfo = picked
    if fingerprint != NO_NFO and fingerprint == meta_row.nfo_mirror_fingerprint:
        # 我们自己镜像出去的那份，内容是档案的副本，重新吸收只会拿旧值盖新值
        meta_row.nfo_fingerprint = fingerprint
        session.add(meta_row)
        return False
    apply_entry_nfo(meta_row, nfo_path, nfo)
    meta_row.nfo_fingerprint = fingerprint
    session.add(meta_row)
    logger.info("已吸收条目 NFO：《%s》← %s", item.title, nfo_path.name)
    return True


def _episode_nfo_path(row: LibraryFile) -> Path | None:
    path = Path(row.file_path)
    return path.with_suffix(".nfo") if path.suffix else None


async def absorb_episode_nfos(
    session,  # noqa: ANN001 - AsyncSession
    episodes: list[MediaEpisode],
    files: list[LibraryFile],
) -> int:
    """把分集 NFO（视频同名 .nfo）合并进 ``media_episode``，返回改动的集数。

    一个视频对应一集；同一集有多个版本文件时取**第一个**读得出内容的，与
    改动前分集区「取首个在位文件」的口径一致。

    与条目级同款的自家副本守卫（``media_episode.nfo_mirror_fingerprint``）：
    镜像每次刷新都按 ``media_episode`` 重写 ``<视频名>.nfo``，认不出自家副本
    的话，下一轮刷新会把上一轮的集名/简介读回来盖掉 TMDB 刚补的内容——新播
    剧集尤其吃亏，TMDB 初期只有占位的"第 N 集"，几天后补上的真标题会永远
    显示不出来。
    """
    by_unit: dict[tuple[int, int], MediaEpisode] = {
        (e.season_number, e.episode_number): e for e in episodes
    }
    if not by_unit:
        return 0
    targets: list[tuple[MediaEpisode, Path]] = []
    seen: set[tuple[int, int]] = set()
    for row in files:
        unit = (row.season_number, row.episode_number)
        episode = by_unit.get(unit)
        if episode is None or unit in seen:
            continue
        nfo_path = _episode_nfo_path(row)
        if nfo_path is None:
            continue
        seen.add(unit)
        targets.append((episode, nfo_path))
    if not targets:
        return 0

    def _read_all() -> list[tuple[MediaEpisode, str | None, object]]:
        return [
            (episode, fingerprint_of(path), read_episode_metadata(path))
            for episode, path in targets
        ]

    changed = 0
    for episode, fingerprint, nfo in await asyncio.to_thread(_read_all):
        if nfo is None:
            continue
        if fingerprint is not None and fingerprint == episode.nfo_mirror_fingerprint:
            continue  # 我们自己写出去的那份，吸收它等于拿旧值盖新值
        touched = False
        if nfo.title:
            episode.name, touched = nfo.title, True
        if nfo.plot:
            episode.overview, touched = nfo.plot, True
        if nfo.aired and episode.air_date is None:
            # 播出日期**只在库里没有时**才用 NFO 的（与改动前读路径的
            # ``info.air_date = info.air_date or nfo.aired`` 同一口径）。不能
            # 反过来让 NFO 压过 TMDB：这一列不只是展示，订阅的期望集合、
            # 追新调度都按它判断已播/未播，第三方 NFO 写错一个日期就会把
            # 用户的追新排期带歪
            from movieclaw_api.services.media_scrape import _parse_iso_date

            parsed = _parse_iso_date(nfo.aired)
            if parsed is not None:
                episode.air_date, touched = parsed, True
        if touched:
            session.add(episode)
            changed += 1
    return changed
