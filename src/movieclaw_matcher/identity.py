"""身份匹配（内核第一级）：这个种子是不是这个条目？是它的哪些单元？

信号优先级（docs/design/subscription.md 3.1）：
1. 外部 ID 精确相等（imdb/douban，详情富化带回）——免费且最可靠；
2. 别名 × 标题段**覆盖率**匹配 + 年份约束。

覆盖率而非子串包含，源自真实误配教训：《金特务：本色回归》(김부장) 的 TMDB
泛化别名 "Mr Kim" 曾以子串命中另一部剧《The Dream Life of Mr Kim》。因此别名
必须覆盖候选"标题段"（去掉年份/季集/画质等标记后的片名部分）的大多数字符——
"mrkim" 只占 "thedreamlifeofmrkim" 的 26%，拒；真正的同名资源覆盖率接近 100%。
NER 上线后将升级为"正向抽取 + 反向验证"双向校验，本启发式是其保守前身。

保守原则：**宁可漏（返回 None，等更好的候选/更多信号），绝不静默错配**。
所有守卫（年份、短别名、类型冲突、覆盖率）都朝"多拒少错"的方向倾斜。
"""

from __future__ import annotations

import re
import unicodedata

from movieclaw_matcher.models import IdentityMatch, MediaIdentity, TorrentCandidate

# 短别名守卫阈值：归一化后 ≤3 个字符的别名（Her / Up / 24 / 色戒）误报风险极高，
# 必须叠加"提取到的年份与条目年份精确相等"才允许命中
_SHORT_ALIAS_LEN = 3

# 电影年份容差：站点标注偶有上映年/资源年一年之差
_MOVIE_YEAR_TOLERANCE = 1

# 别名对标题段的最小覆盖率：低于此值说明别名只是段内一小截，多半是别的作品
_MIN_COVERAGE = 0.6

# 单部电影候选不能横跨多个发行年份。PT 合集常把首部片名和起始年放在最前，
# 例如 ``The Fast And The Furious 2001-2023``；旧逻辑会把它精确命中 2001
# 年首部电影并投递整套合集。年份范围是比“合集/Trilogy”词表更稳定的结构证据，
# 也不依赖 NER 是否恰好抽出 complete。
_MOVIE_YEAR_RANGE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*[-–—~～]\s*((?:19|20)\d{2})(?!\d)"
)

# 标题段边界：场景命名里片名之后的第一个标记（年份/季集/画质/发布类标记），
# 命中任一边界即认为片名部分结束
_BOUNDARY_RE = re.compile(
    r"^(19\d{2}|20\d{2}"  # 年份
    r"|s\d{1,3}(e\d{1,4})?|ep?\d{1,4}"  # S01 / S01E02 / E05 / EP05
    r"|第.{1,4}[季集话期]|全.{1,4}[季集话]"  # 中文季集标记
    r"|\d{3,4}[pi]|[24]k|uhd|fhd"  # 画质
    r"|complete|bluray|blu|remux|web|webdl|webrip|hdtv|dvdrip)$",  # 发布类标记
    re.IGNORECASE,
)

# 副标题里并列别名的分隔符（NexusPHP 惯例："中文名 / 别名2 | 类型：剧情"）
_SUBTITLE_SPLIT_RE = re.compile(r"[/|｜,，;；]")


def normalize_title(text: str) -> str:
    """匹配用归一化：NFKC（全角→半角）+ casefold + 只保留字母数字与 CJK。

    分隔符（./-/_/空格/冒号/中点）全部剔除，"Dune.Part.Two" 与 "Dune: Part Two"
    归一到同一形态。注意繁简**不**转换——别名集合本身已包含 CN/HK/TW 各地区
    写法（数据覆盖，而非规则转换）。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _tokenize(text: str) -> list[str]:
    """按非字母数字切词（casefold+NFKC），保序。"""
    folded = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    current: list[str] = []
    for ch in folded:
        if ch.isalnum():
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _title_segment(text: str) -> str:
    """提取"片名段"：从头累积 token，遇到第一个边界标记即停。

    "The.Dream.Life.of.Mr.Kim.S01.2025.1080p" → "thedreamlifeofmrkim"。
    没有任何边界时整个文本就是片名段（覆盖率约束相应变严，符合保守原则）。
    """
    parts: list[str] = []
    for token in _tokenize(text):
        if _BOUNDARY_RE.match(token):
            break
        parts.append(token)
    return "".join(parts)


def _candidate_segments(candidate: TorrentCandidate) -> list[str]:
    """候选的全部可比对片名段：主标题一段 + 副标题按分隔符拆出的每段。

    副标题段同样做边界截断（"问心2 全40集" → "问心2"）。
    """
    segments: list[str] = []
    title_seg = _title_segment(candidate.title)
    if title_seg:
        segments.append(title_seg)
    for raw in _SUBTITLE_SPLIT_RE.split(candidate.subtitle):
        seg = _title_segment(raw)
        if seg:
            segments.append(seg)
    return segments


def _match_alias(candidate: TorrentCandidate, media: MediaIdentity) -> str | None:
    """在候选的片名段里找一个覆盖率达标的条目别名；找不到返回 None。"""
    segments = _candidate_segments(candidate)
    if not segments:
        return None
    tokens: set[str] | None = None  # 短别名整词判定用，懒构建
    for alias in media.aliases:
        needle = normalize_title(alias)
        if not needle:
            continue
        if len(needle) <= _SHORT_ALIAS_LEN:
            # 短别名（Her/24/她）双重守卫：年份必须精确相等 + 必须以完整
            # token 出现（子串无法表达词边界——"her" 会命中 "Hercules"）
            if candidate.attrs.year is None or candidate.attrs.year != media.year:
                continue
            if tokens is None:
                tokens = set(_tokenize(candidate.title)) | set(
                    _tokenize(candidate.subtitle)
                )
            if needle in tokens:
                return alias
            continue
        for segment in segments:
            if needle in segment and len(needle) >= _MIN_COVERAGE * len(segment):
                return alias
    return None


def _is_multi_year_movie_pack(candidate: TorrentCandidate) -> bool:
    """候选是否明确写了跨年份电影合集。"""
    attrs = candidate.attrs
    text = " ".join(
        (
            candidate.title,
            candidate.subtitle,
            *attrs.titles_zh,
            *attrs.titles_en,
            *attrs.title_candidates,
        )
    )
    return any(start != end for start, end in _MOVIE_YEAR_RANGE_RE.findall(text))


def match_identity(
    candidate: TorrentCandidate, media: MediaIdentity
) -> IdentityMatch | None:
    """判定候选种子是否为该条目，并给出覆盖的单元。None = 不是/无法确认。"""
    attrs = candidate.attrs

    # 类型冲突：enrich 明确判定的类型与条目不符，直接排除。
    # 注意只信 media_type 字段——电影种子可能被误提取出 seasons 噪音
    # （如 "Zombi VIII" 的罗马数字），不能拿 seasons 是否为空当类型信号。
    if attrs.media_type is not None and attrs.media_type != media.kind:
        return None

    # 单片订阅不能用跨年份合集来满足。即使合集带首部电影的 IMDb/Douban ID，
    # 也不能让精确 ID 绕过该结构守卫——当前下载/入库契约只接受一个电影单元。
    if media.kind == "movie" and _is_multi_year_movie_pack(candidate):
        return None

    # -- 信号一：外部 ID 精确相等 -------------------------------------------
    if candidate.imdb_id and media.imdb_id and candidate.imdb_id == media.imdb_id:
        return _derive_units(candidate, media, confidence="exact_id", alias=None)
    if candidate.douban_id and media.douban_id and candidate.douban_id == media.douban_id:
        return _derive_units(candidate, media, confidence="exact_id", alias=None)

    # -- 信号二：别名覆盖率匹配 + 年份约束 -----------------------------------
    matched_alias = _match_alias(candidate, media)
    if matched_alias is None:
        return None

    if not _year_compatible(media, attrs.year):
        return None

    confidence = "title_year" if attrs.year is not None else "title_only"
    return _derive_units(candidate, media, confidence=confidence, alias=matched_alias)


def _year_compatible(media: MediaIdentity, torrent_year: int | None) -> bool:
    """年份约束（按类型分别处理）。

    - 电影：标题匹配必须有年份佐证——种子年份缺失直接拒（电影场景命名
      几乎必带年份，缺失本身就可疑）；有则容差 ±1。
    - 剧集：种子年份通常是"当季年份"而非首播年（HotD S02 标 2024，首播 2022），
      只做下限校验：早于首播前一年的必是别的作品。年份缺失放行（剧集单集
      命名常不带年，季集约束在消费侧兜底）。
    """
    if media.year is None:
        return True  # 条目自身无年份（罕见），无从约束
    if media.kind == "movie":
        if torrent_year is None:
            return False
        return abs(torrent_year - media.year) <= _MOVIE_YEAR_TOLERANCE
    if torrent_year is None:
        return True
    return torrent_year >= media.year - 1


def _derive_units(
    candidate: TorrentCandidate,
    media: MediaIdentity,
    *,
    confidence: str,
    alias: str | None,
) -> IdentityMatch | None:
    """从 enrich 属性推导覆盖单元。身份成立但单元无法落定时返回 None（不可用）。"""
    attrs = candidate.attrs

    if media.kind == "movie":
        return IdentityMatch(
            episodes=frozenset({(0, 0)}),
            confidence=confidence,
            matched_alias=alias,
        )

    seasons = attrs.seasons
    episodes = attrs.episodes
    # 明确标注全集、或有季无集，都是 pack：一个种子覆盖一批单元
    # （enrich v5 起"全 N 集"不再展开集列表，走 complete + 有季无集分支）
    is_pack = attrs.complete is True or (bool(seasons) and not episodes)

    if episodes:
        if len(seasons) == 1:
            season = seasons[0]
        elif not seasons:
            # 无季号的集：仅单正季剧可安全推断为该季，多季剧太歧义、放弃
            regular = [n for n in media.season_numbers if n != 0]
            if len(regular) != 1:
                return None
            season = regular[0]
        else:
            # 多季 + 集号并存（S01-S03 E01 之类）：按整季包处理最不易错
            return IdentityMatch(
                pack_seasons=frozenset(seasons),
                is_pack=True,
                confidence=confidence,
                matched_alias=alias,
            )
        return IdentityMatch(
            episodes=frozenset((season, e) for e in episodes),
            is_pack=is_pack,
            confidence=confidence,
            matched_alias=alias,
        )

    if seasons:
        return IdentityMatch(
            pack_seasons=frozenset(seasons),
            is_pack=True,
            confidence=confidence,
            matched_alias=alias,
        )

    if attrs.complete is True:
        return IdentityMatch(
            is_complete_series=True,
            is_pack=True,
            confidence=confidence,
            matched_alias=alias,
        )

    # 剧集但没有任何季集信息：身份可能成立，但落不到具体单元，不可用
    return None
