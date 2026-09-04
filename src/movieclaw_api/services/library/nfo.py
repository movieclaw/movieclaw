"""NFO 写出与读取（媒体库 L4 / 条目详情）。

写出：入库时在条目目录生成身份 NFO，反哺播放器生态。价值双向：
- **给 Emby/Jellyfin**：NFO 里的 tmdbid 让下游零歧义入档（命名歧义导致的
  误合并是 Emby 社区最大痛点，见设计文档 1.5 节）；
- **给自己**：库目录一旦重扫（换库、迁移后重建台账），扫描器的 NFO 优先
  识别链直接读回精确身份，免去再走 TMDB 收敛。

原则：**已存在的 NFO 绝不覆盖**（存量目录的 NFO 可能来自 TMM/Emby，内容
比我们的最小 NFO 丰富）；写出失败只告警不阻断入库。

读取：条目详情页的本地元数据来源——存量目录常有 TMM/Emby 刮削好的
完整 NFO（简介/评分/片长/演职员），逐字段容错解析，读不出的字段保持
None/空（三态铁律），绝不因个别字段畸形丢掉整份 NFO。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from movieclaw_db.models import MediaEpisode, MediaItem, MediaMetadata
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.library_nfo")

_KIND_NAMES = {MediaKind.MOVIE: "电影", MediaKind.TV: "剧集"}


def _kind_conflicting_nfo(entry_dir: Path, kind: MediaKind, item: MediaItem) -> Path | None:
    """条目目录里是否已有**另一类型**的条目 NFO（有则返回其路径，并告警）。

    这一刀是 NFO 写出的最后一道闸：目录里躺着 ``tvshow.nfo`` 却要写
    ``movie.nfo``，说明这个身份多半是错的（TMDB 的 movie/tv 是两套独立
    id 空间，库类型选错时同一个数字在另一侧照样能取到条目、全程零报错）。
    实测代价：一次库类型选错的扫描往 27 个剧集目录写进了 movie.nfo，
    库改回剧集后这些 NFO 反过来把 444 个文件判成"放错库了"。
    **写错的 NFO 会被下次识别当权威读回**，污染是自我固化的——宁可不写。
    """
    other = entry_dir / ("tvshow.nfo" if kind is MediaKind.MOVIE else "movie.nfo")
    if not other.is_file():
        return None
    logger.warning(
        "跳过 NFO 写出：目录已有 %s（声明这是%s），与条目《%s》的类型「%s」不符，"
        "身份多半判错了——%s",
        other.name,
        _KIND_NAMES[MediaKind.TV if kind is MediaKind.MOVIE else MediaKind.MOVIE],
        item.title,
        _KIND_NAMES[kind],
        entry_dir,
    )
    return other


def _identity_lines(root_tag: str, item: MediaItem) -> list[str]:
    """最小身份 NFO 的全部行（write_entry_nfo 与认领纠错的改写共用）。"""
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f"<{root_tag}>",
        f"  <title>{escape(item.title)}</title>",
        f"  <originaltitle>{escape(item.original_title)}</originaltitle>",
    ]
    if item.year is not None:
        lines.append(f"  <year>{item.year}</year>")
    lines.append(f"  <tmdbid>{item.tmdb_id}</tmdbid>")
    lines.append(f'  <uniqueid type="tmdb" default="true">{item.tmdb_id}</uniqueid>')
    if item.imdb_id:
        lines.append(f'  <uniqueid type="imdb">{escape(item.imdb_id)}</uniqueid>')
    lines.append(f"</{root_tag}>")
    return lines


def write_entry_nfo(entry_dir: Path, item: MediaItem) -> None:
    """在条目目录写出最小身份 NFO（电影 movie.nfo / 剧集 tvshow.nfo）。

    同步函数（调用方放线程池）。目录不存在、NFO 已存在、或目录里已有另一
    类型的条目 NFO（见 ``_kind_conflicting_nfo``）时直接返回。
    """
    if not entry_dir.is_dir():
        return
    kind = MediaKind(item.kind)
    root_tag = "movie" if kind is MediaKind.MOVIE else "tvshow"
    nfo_path = entry_dir / ("movie.nfo" if kind is MediaKind.MOVIE else "tvshow.nfo")
    if nfo_path.exists():
        return  # 尊重既有刮削成果（TMM/Emby 的 NFO 比我们的更丰富）
    if _kind_conflicting_nfo(entry_dir, kind, item) is not None:
        return

    try:
        nfo_path.write_text("\n".join(_identity_lines(root_tag, item)) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("NFO 写出失败（不影响入库）：%s（%s）", nfo_path, exc)
        return
    logger.info("已写出 NFO：%s（tmdbid=%s）", nfo_path, item.tmdb_id)


def rewrite_identity_nfo(nfo_path: Path, item: MediaItem) -> bool:
    """把一份身份错误的条目 NFO **原地改写**为正确的最小身份 NFO。

    仅供人工认领链路调用（覆盖策略与 write_entry_nfo 相反，绝不能混用）：
    用户拍板的身份是最高权威，此刻这份声明着另一个 tmdbid 的 NFO 已被
    证实指错了条目——不改写，下次全量重扫会把错误身份原样读回（识别链
    的 NFO 优先是自我固化的），Emby/Jellyfin 也会继续错挂。旧内容不保留：
    简介/演员本就属于错误条目；刮削任务随后会把这份最小档升级为正确
    条目的完整版（write_full_nfo 对"tmdbid 一致的最小档"放行覆盖）。
    """
    kind = MediaKind(item.kind)
    root_tag = "movie" if kind is MediaKind.MOVIE else "tvshow"
    try:
        nfo_path.write_text("\n".join(_identity_lines(root_tag, item)) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("认领纠错 NFO 改写失败：%s（%s）", nfo_path, exc)
        return False
    logger.info("认领纠错：%s 已改写为《%s》（tmdbid=%s）", nfo_path, item.title, item.tmdb_id)
    return True


def write_full_nfo(entry_dir: Path, item: MediaItem, meta: MediaMetadata | None) -> None:
    """在条目目录写出完整 NFO（身份 + 简介/评分/片长/分级/演职员）。

    同步函数（调用方放线程池）。覆盖规则（docs/design/metadata.md 6.2，
    2026-08-04 决策翻转）：NFO 是库内档案在媒体目录的镜像，**每次刮削/刷新
    都与 media_metadata 对齐**——内容比对，无变化不落盘（不动 mtime，免得
    watchdog 与播放器无谓重扫）。三条保护不变：

    - 目录里已有另一类型的条目 NFO 时不写（``_kind_conflicting_nfo``，
      身份判错的强信号）；
    - 既有 NFO 声明了**不同的 tmdbid** 时不写（留给认领纠错链路改写；
      没有 tmdbid 声明的按本条目身份对齐——调用方已用高置信身份门槛把关）；
    - **没有刮削档案时绝不覆盖已有文件**（不能拿最小身份档降级 TMM 的富 NFO）。
    """
    if not entry_dir.is_dir():
        return
    kind = MediaKind(item.kind)
    root_tag = "movie" if kind is MediaKind.MOVIE else "tvshow"
    nfo_path = entry_dir / ("movie.nfo" if kind is MediaKind.MOVIE else "tvshow.nfo")
    if _kind_conflicting_nfo(entry_dir, kind, item) is not None:
        return
    exists = nfo_path.exists()
    if exists:
        declared = read_tmdb_id(nfo_path)
        if declared is not None and declared != item.tmdb_id:
            return  # 身份对不上，宁可不写
        if meta is None or meta.scraped_at is None:
            return  # 无档案可对齐，保留既有内容

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f"<{root_tag}>",
        f"  <title>{escape(item.title)}</title>",
        f"  <originaltitle>{escape(item.original_title)}</originaltitle>",
    ]
    if item.year is not None:
        lines.append(f"  <year>{item.year}</year>")
    lines.append(f"  <tmdbid>{item.tmdb_id}</tmdbid>")
    lines.append(f'  <uniqueid type="tmdb" default="true">{item.tmdb_id}</uniqueid>')
    if item.imdb_id:
        lines.append(f'  <uniqueid type="imdb">{escape(item.imdb_id)}</uniqueid>')
    if meta is not None:
        if meta.overview:
            lines.append(f"  <plot>{escape(meta.overview)}</plot>")
        if meta.tagline:
            lines.append(f"  <tagline>{escape(meta.tagline)}</tagline>")
        if meta.runtime_minutes:
            lines.append(f"  <runtime>{meta.runtime_minutes}</runtime>")
        if meta.content_rating:
            lines.append(f"  <mpaa>{escape(meta.content_rating)}</mpaa>")
        if meta.release_date is not None:
            lines.append(f"  <premiered>{meta.release_date.isoformat()}</premiered>")
        if meta.vote_average:
            votes = f"\n      <votes>{meta.vote_count}</votes>" if meta.vote_count else ""
            lines.append(
                "  <ratings>\n"
                f'    <rating name="themoviedb" max="10" default="true">\n'
                f"      <value>{meta.vote_average}</value>{votes}\n"
                "    </rating>\n"
                "  </ratings>"
            )
        lines.extend(f"  <genre>{escape(g)}</genre>" for g in meta.genres)
        lines.extend(f"  <studio>{escape(s)}</studio>" for s in meta.studios)
        lines.extend(f"  <country>{escape(c)}</country>" for c in meta.origin_countries)
        lines.extend(f"  <director>{escape(d)}</director>" for d in meta.directors)
        # 演员头像用 TMDB 图床绝对地址（Kodi/TMM 同款惯例，详情页可回读展示）
        from movieclaw_api.core.config import get_settings

        image_base = get_settings().tmdb_image_base_url.rstrip("/")
        for actor in meta.cast[:_MAX_ACTORS]:
            name = (actor.get("name") or "").strip()
            if not name:
                continue
            lines.append("  <actor>")
            lines.append(f"    <name>{escape(name)}</name>")
            if actor.get("character"):
                lines.append(f"    <role>{escape(actor['character'])}</role>")
            if actor.get("profile_path"):
                thumb = f"{image_base}/w300{actor['profile_path']}"
                lines.append(f"    <thumb>{escape(thumb)}</thumb>")
            # 影人 id（Kodi/TMM 惯例的 <actor><tmdbid>）：人物页链接的依据，
            # 也让我们自己写出的 NFO 能原地回读出这个身份
            if actor.get("tmdb_person_id"):
                lines.append(f"    <tmdbid>{int(actor['tmdb_person_id'])}</tmdbid>")
            lines.append(f"    <order>{actor.get('order') or 0}</order>")
            lines.append("  </actor>")
    lines.append(f"</{root_tag}>")

    content = "\n".join(lines) + "\n"
    if exists:
        try:
            if nfo_path.read_text(encoding="utf-8") == content:
                return  # 内容无变化，不动 mtime
        except OSError:
            pass
    try:
        nfo_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("完整 NFO 写出失败（不阻断）：%s（%s）", nfo_path, exc)
        return
    logger.info("已写出完整 NFO：%s（tmdbid=%s）", nfo_path, item.tmdb_id)


def write_episode_nfo(video: Path, episode: MediaEpisode) -> None:
    """写出分集 NFO（视频同名 .nfo，根元素 <episodedetails>）。

    同步函数（调用方放线程池）。与条目 NFO 同款对齐语义（2026-08-04）：
    每次刷新按 media_episode 重写，内容比对无变化不落盘；调用方已按
    高置信身份门槛把关，季集号本就来自台账与档案的同一套数字对。
    防降级闸：档案行只有集号骨架（无简介且无播出日期，说明 TMDB 没数据）
    时不覆盖已有文件——第三方刮的分集 NFO 大概率比骨架富。
    """
    nfo_path = video.with_suffix(".nfo")
    if nfo_path.exists() and not episode.overview and episode.air_date is None:
        return
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<episodedetails>",
        f"  <title>{escape(episode.name)}</title>",
        f"  <season>{episode.season_number}</season>",
        f"  <episode>{episode.episode_number}</episode>",
    ]
    if episode.overview:
        lines.append(f"  <plot>{escape(episode.overview)}</plot>")
    if episode.air_date is not None:
        lines.append(f"  <aired>{episode.air_date.isoformat()}</aired>")
    if episode.runtime_minutes:
        lines.append(f"  <runtime>{episode.runtime_minutes}</runtime>")
    lines.append("</episodedetails>")
    content = "\n".join(lines) + "\n"
    if nfo_path.exists():
        try:
            if nfo_path.read_text(encoding="utf-8") == content:
                return  # 内容无变化，不动 mtime
        except OSError:
            pass
    try:
        nfo_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("分集 NFO 写出失败（不阻断）：%s（%s）", nfo_path, exc)


# ---------------------------------------------------------------------------
# NFO 读取（条目详情页的本地元数据）
# ---------------------------------------------------------------------------


@dataclass
class NfoActor:
    """NFO 里的一位演员。``thumb`` 通常是 TMM/Emby 写入的 TMDB 头像 URL。

    ``tmdb_person_id`` 是人物页链接的依据（姓名不是身份）。来源有三：NFO 的
    ``<actor><tmdbid>``（Kodi/TMM 惯例，我们自己写出的完整 NFO 也照这个写）、
    库内档案 media_metadata.cast 的同名回填、TMDB 实时兜底。取不到就是 None，
    前端把这一格渲染成不可点。
    """

    name: str
    role: str | None = None
    thumb: str | None = None
    tmdb_person_id: int | None = None


@dataclass
class EntryMetadata:
    """条目的展示元数据；字段 None/空 = 来源里没有。

    来源两种：``source="nfo"`` 解析自条目目录的刮削 NFO（首选）；
    ``source="tmdb"`` 是本地无可用 NFO 时详情接口的 TMDB 实时兜底
    （见 library_items._tmdb_fallback_meta），前端据此标注信息出处。
    """

    plot: str | None = None
    rating: float | None = None
    runtime_minutes: int | None = None
    genres: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    actors: list[NfoActor] = field(default_factory=list)
    nfo_name: str = ""  # 来源文件名（前端标注"信息来自 xxx.nfo"）
    source: str = "nfo"  # nfo / tmdb

    def has_content(self) -> bool:
        """是否解出了任何有展示价值的字段（最小身份 NFO 会返回 False）。"""
        return bool(self.plot or self.rating or self.runtime_minutes or self.genres or self.actors)


# 演员数量上限：TMM 会把全量 cast 写进 NFO（上百人），详情页展示前 40 位足够
_MAX_ACTORS = 40

# Kodi 惯例允许 NFO 的 XML 之后附加裸 URL 等杂质文本，标准解析器会报错；
# 兜底时把根元素片段切出来重试。非贪婪：多集合一的 NFO 并列多个
# <episodedetails> 根时取第一段（展示第一集），贪婪会吞到最后一个闭合标签
_ROOT_SLICE = re.compile(r"<(movie|tvshow|episodedetails)[\s>].*?</\1>", re.DOTALL | re.IGNORECASE)


def read_entry_metadata(nfo_path: Path) -> EntryMetadata | None:
    """解析一份条目级 NFO（movie.nfo / tvshow.nfo）。

    同步函数（调用方放线程池）。文件不存在、不是合法 XML、根元素不认识
    时返回 None；个别字段畸形只跳过该字段。
    """
    try:
        text = nfo_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    root = _parse_xml(text)
    if root is None or root.tag.lower() not in {"movie", "tvshow"}:
        return None

    meta = EntryMetadata(nfo_name=nfo_path.name)
    meta.plot = _first_text(root, "plot") or _first_text(root, "outline")
    meta.rating = _parse_rating(root)
    meta.runtime_minutes = _to_positive_int(_first_text(root, "runtime"))
    meta.genres = _all_texts(root, "genre")
    meta.directors = _all_texts(root, "director")
    for actor in root.findall("actor")[:_MAX_ACTORS]:
        name = (actor.findtext("name") or "").strip()
        if not name:
            continue
        role = (actor.findtext("role") or "").strip() or None
        thumb = (actor.findtext("thumb") or "").strip() or None
        # 只保留 http(s) 头像地址：本地相对路径没有服务通道，前端无法加载
        if thumb and not thumb.lower().startswith(("http://", "https://")):
            thumb = None
        # <actor><tmdbid> 是**影人** id（不是条目 id——条目身份只认根元素的直接
        # 子节点，见 read_tmdb_id）。人物页链接靠它
        meta.actors.append(
            NfoActor(
                name=name,
                role=role,
                thumb=thumb,
                tmdb_person_id=_to_positive_int(actor.findtext("tmdbid")),
            )
        )
    return meta


@dataclass
class NfoIdentity:
    """条目级 NFO 声明的身份：**媒体类型**（根元素）+ TMDB id。

    类型与 id 同等重要：TMDB 的 movie 与 tv 是两套独立 id 空间，同一个数字
    在两边往往都能取到条目（且是毫不相干的两部作品）。``<movie>`` /
    ``<tvshow>`` 这个根元素是刮削器留下的**明确类型声明**，扫描器据此
    校验"文件是否放错了媒体库"，见 library_scan 的类型冲突检测。
    """

    kind: MediaKind
    tmdb_id: int | None


def read_entry_identity(nfo_path: Path) -> NfoIdentity | None:
    """读条目级 NFO 声明的身份（类型 + TMDB id），非条目级 NFO 返回 None。

    只信任条目级根元素（movie/tvshow）：分集 NFO（episodedetails）根级的
    uniqueid 是**那一集**的 id（实测旺达幻视分集 NFO 带集级 imdb/tvdb id），
    拿去当条目身份会把整部剧钉死到一个集上。
    结构化解析后**只看根元素的直接子节点**：Emby 刮削的 <actor> 块里也有
    <tmdbid>（演员的 person id），正则全文匹配会把第一个演员误当条目身份
    （实测：潘粤明 138734 → 误配成同 id 的剧集），XML 树上则天然隔离。
    <uniqueid type="tmdb"> 带类型标注语义无歧义，优先于裸 <tmdbid>。
    文件不存在或 XML 无法解析时返回 None；解析出根元素但没有 id 时
    ``tmdb_id`` 为 None——**类型声明本身仍然有效**，不该一并丢掉。
    """
    try:
        text = nfo_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    root = _parse_xml(text)
    if root is None:
        return None
    tag = root.tag.lower()
    if tag not in {"movie", "tvshow"}:
        return None
    kind = MediaKind.MOVIE if tag == "movie" else MediaKind.TV
    return NfoIdentity(kind=kind, tmdb_id=_read_tmdb_id(root))


def read_tmdb_id(nfo_path: Path) -> int | None:
    """读 NFO 里的条目级 TMDB id；没有则 None（见 read_entry_identity）。"""
    identity = read_entry_identity(nfo_path)
    return identity.tmdb_id if identity is not None else None


def _read_tmdb_id(root: ET.Element) -> int | None:
    """从条目级根元素的直接子节点取 TMDB id（带类型标注的 uniqueid 优先）。"""
    for node in root.findall("uniqueid"):
        if (node.get("type") or "").lower() == "tmdb":
            value = _to_positive_int(node.text)
            if value is not None:
                return value
    return _to_positive_int(root.findtext("tmdbid"))


@dataclass
class EpisodeNfo:
    """一集的 NFO（视频同名 .nfo，根元素 <episodedetails>）解出的展示信息。"""

    title: str | None = None
    plot: str | None = None
    aired: str | None = None  # ISO 日期字符串
    # 季/集号：刮削器写盘的定位信息，比文件名解析更可信（文件名可能同时
    # 含「第一季」与 S09 两个互相矛盾的季信号）。season 允许 0（特别篇）。
    season: int | None = None
    episode: int | None = None


def read_episode_metadata(nfo_path: Path) -> EpisodeNfo | None:
    """解析分集 NFO（同步，调用方放线程池）。

    多集合一文件的 NFO 允许并列多个 <episodedetails>，标准解析器会因
    多根报错——走 _ROOT_SLICE 兜底取第一段即可（展示第一集的信息）。
    根元素不是 episodedetails（如条目级 movie.nfo）返回 None。
    """
    try:
        text = nfo_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    root = _parse_xml(text)
    if root is None or root.tag.lower() != "episodedetails":
        return None
    return EpisodeNfo(
        title=_first_text(root, "title"),
        plot=_first_text(root, "plot") or _first_text(root, "outline"),
        aired=_first_text(root, "aired"),
        season=_to_nonneg_int(root.findtext("season")),
        episode=_to_positive_int(root.findtext("episode")),
    )


@dataclass
class LocalNfo:
    """本地来源条目从 sidecar NFO 里**全字段吸收**的信息（docs/design/library-other-kind.md 4.1）。

    与 ``EntryMetadata`` 的差别：这里连标题/年份/日期一起读——本地条目没有
    远程档案可回填，NFO 是它唯一的元数据来源。``tmdbid``/``uniqueid`` 一律
    忽略（有也不建身份，身份来源是本地）。字段 None/空 = NFO 里没有。
    """

    title: str | None = None
    sort_title: str | None = None
    plot: str | None = None
    year: int | None = None
    release_date: str | None = None  # ISO 日期串（premiered / aired / releasedate）
    runtime_minutes: int | None = None
    rating: float | None = None
    genres: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    actors: list[NfoActor] = field(default_factory=list)
    nfo_name: str = ""


def read_local_sidecar(nfo_path: Path) -> LocalNfo | None:
    """解析视频同名 sidecar NFO，**不校验根元素**（Jellyfin 同款：``<movie>`` /
    ``<video>`` / ``<musicvideo>`` / ``<episodedetails>`` 任何根都行，直接读子节点）。

    同步函数（调用方放线程池）。文件不存在或 XML 无法解析返回 None；
    个别字段畸形只跳过该字段。
    """
    try:
        text = nfo_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    root = _parse_xml(text)
    if root is None:
        return None
    nfo = LocalNfo(nfo_name=nfo_path.name)
    nfo.title = (
        _first_text(root, "title") or _first_text(root, "name") or _first_text(root, "localtitle")
    )
    nfo.sort_title = _first_text(root, "sorttitle")
    nfo.plot = _first_text(root, "plot") or _first_text(root, "outline")
    nfo.year = _to_positive_int(_first_text(root, "year"))
    nfo.release_date = (
        _first_text(root, "premiered")
        or _first_text(root, "aired")
        or _first_text(root, "releasedate")
    )
    nfo.runtime_minutes = _to_positive_int(_first_text(root, "runtime"))
    nfo.rating = _parse_rating(root)
    nfo.genres = [
        *_all_texts(root, "genre"),
        *[t for t in _all_texts(root, "tag") if t not in _all_texts(root, "genre")],
    ]
    nfo.studios = _all_texts(root, "studio")
    nfo.directors = _all_texts(root, "director")
    for actor in root.findall("actor")[:_MAX_ACTORS]:
        name = (actor.findtext("name") or "").strip()
        if not name:
            continue
        thumb = (actor.findtext("thumb") or "").strip() or None
        if thumb and not thumb.lower().startswith(("http://", "https://")):
            thumb = None
        nfo.actors.append(
            NfoActor(
                name=name,
                role=(actor.findtext("role") or "").strip() or None,
                thumb=thumb,
                tmdb_person_id=_to_positive_int(actor.findtext("tmdbid")),
            )
        )
    return nfo


def _parse_xml(text: str) -> ET.Element | None:
    text = text.lstrip("﻿ \n\r\t")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        pass
    # Kodi 允许 XML 后面跟裸 URL 等杂质，切出根元素片段重试
    match = _ROOT_SLICE.search(text)
    if match is None:
        return None
    try:
        return ET.fromstring(match.group(0))
    except ET.ParseError:
        return None


def _parse_rating(root: ET.Element) -> float | None:
    """评分：新式 <ratings><rating><value> 优先（themoviedb 优先），
    退回旧式顶层 <rating>；超出 0~10 的按脏数据丢弃。"""
    candidates: list[tuple[int, str | None]] = []
    for rating in root.findall("./ratings/rating"):
        name = (rating.get("name") or "").lower()
        priority = 0 if name == "themoviedb" else 1
        candidates.append((priority, rating.findtext("value")))
    candidates.sort(key=lambda pair: pair[0])
    candidates.append((9, _first_text(root, "rating")))
    for _, raw in candidates:
        value = _to_float(raw)
        if value is not None and 0 < value <= 10:
            return round(value, 1)
    return None


def _first_text(root: ET.Element, tag: str) -> str | None:
    value = root.findtext(tag)
    return value.strip() if value and value.strip() else None


def _all_texts(root: ET.Element, tag: str) -> list[str]:
    seen: list[str] = []
    for node in root.findall(tag):
        value = (node.text or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _to_positive_int(raw: str | None) -> int | None:
    value = _to_float(raw)
    if value is None or value <= 0:
        return None
    return int(value)


def _to_nonneg_int(raw: str | None) -> int | None:
    """季号专用：0 是合法值（Specials 特别篇季）。"""
    value = _to_float(raw)
    if value is None or value < 0:
        return None
    return int(value)
