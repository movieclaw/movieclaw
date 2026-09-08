"""身份匹配（内核第一级）：这个种子是不是这个条目？是它的哪些单元？

信号优先级（docs/design/subscription.md 3.1）：
1. 外部 ID 精确相等（imdb/douban，详情富化带回）——免费且最可靠；
2. 别名 × 标题段**覆盖率**匹配 + 年份约束。

外部 ID **不等**同样是信号，但方向相反：走完 2 仍命中时，``IdentityMatch``
上会带一条 ``id_conflict`` 说明。内核只报告不裁决——站点的 IMDb 是上传者
手填的，填错真实存在，而误否决的代价（漏配，用户只看得到活动流水里一行字）
比错配更难被发现。裁决需要时长/体积/孪生条目等内核拿不到的上下文，交给
消费侧（docs/design/identity-confidence.md §5.2）。

覆盖率而非子串包含，源自真实误配教训：《金特务：本色回归》(김부장) 的 TMDB
泛化别名 "Mr Kim" 曾以子串命中另一部剧《The Dream Life of Mr Kim》。因此别名
必须覆盖候选"标题段"（去掉年份/季集/画质等标记后的片名部分）的大多数字符——
"mrkim" 只占 "thedreamlifeofmrkim" 的 26%，拒；真正的同名资源覆盖率接近 100%。

标题段有两个来源（subscription.md 3.1 的"正向抽取 + 反向验证"双向校验）：
1. 启发式切段：从原始 title/subtitle 按边界标记截出片名部分；
2. NER 抽取片名（attrs.titles_zh / titles_en）：边界启发式看不懂的命名靠它。
   两个来源的段走同一套覆盖率与守卫验证——NER 只负责"提名"，是否算命中
   仍由保守规则复核。

覆盖率之外还有一条"季名组合等式"（真实漏配教训：中餐厅 S10E01）：国综每季
带副标题，发布组把它并进片名——"The Chinese Restaurant Southeast Asian
Flavors" / "中餐厅·南洋拾光季"。主标题别名对这类段的覆盖率必然不达标，且
NER 的正确抽取同样是季全名（这就是该季的官方名，不是抽取错误），所以覆盖率
路线对这一类命名结构性无解。解法是引入条目自己的季名清单（media_season）：
片名段恰好等于"别名+季名"即命中——这个组合不可能属于别的作品。

保守原则：**宁可漏（返回 None，等更好的候选/更多信号），绝不静默错配**。
所有守卫（年份、短别名、类型冲突、覆盖率）都朝"多拒少错"的方向倾斜。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

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


def _ner_title_segments(candidate: TorrentCandidate) -> list[str]:
    """NER 抽取片名 → 可比对片名段（"正向抽取"来源）。

    只取 titles_zh / titles_en（模型判定的片名及别名）。**不取
    title_candidates**——它自我声明是"像片名但未抽出"的噪音保险层，真实
    样本里混有嘉宾人名（"王俊凯"），拿来当片名段会给同名人物条目开误配口子。

    抽取结果同样过一遍边界截断：干净片名不受影响，万一模型把相邻的年份/
    画质 token 泄漏进 span，截断能把它修回片名本体。
    """
    segments: list[str] = []
    attrs = candidate.attrs
    for raw in (*attrs.titles_zh, *attrs.titles_en):
        seg = _title_segment(raw)
        if seg:
            segments.append(seg)
    return segments


def _match_alias(candidate: TorrentCandidate, media: MediaIdentity) -> str | None:
    """在候选的片名段里找一个覆盖率达标的条目别名；找不到返回 None。

    NER 段只参与常规别名的覆盖率比对，**短别名分支刻意不碰它**：真实样本里
    NER 偶见碎片产出（"中餐厅"被抽成"餐厅"），碎片段对长别名天然免疫
    （别名比段还长就当不了子串），但会让短别名的"整段相等"变得廉价——
    一部叫《餐厅》的剧不该因为碎片就认领《中餐厅》的种子。
    """
    segments = _candidate_segments(candidate) + _ner_title_segments(candidate)
    if not segments:
        return None
    season_titles = [t for t in (normalize_title(s) for s in media.season_titles) if t]
    tokens: set[str] | None = None  # 短别名整词判定用，懒构建
    for alias in media.aliases:
        needle = normalize_title(alias)
        if not needle:
            continue
        # 季名组合等式：片名段**恰好等于**"别名+已知季名"（"中餐厅"+"南洋拾光季"
        # == "中餐厅南洋拾光季"）。国综每季带副标题、发布组把它并进片名，主标题
        # 别名对这类段的覆盖率必然被稀释；而组合等式零歧义——不存在另一部作品
        # 恰好叫"<本剧别名><本剧季名>"。因为是整段精确相等（非子串），对短别名
        # 同样安全，故放在长短分支之前、对全部别名生效。
        for season_title in season_titles:
            if needle + season_title in segments:
                return alias
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
    match = _derive_units(candidate, media, confidence=confidence, alias=matched_alias)
    # 走到这里说明两边的 ID 没能相等（信号一没命中）。若两边**都有** ID 而它们
    # 不同，这是一条必须带出去的反证：站点已经明确说了"这是另一部片"，而旧逻辑
    # 掉到别名匹配就当没看见。内核只报告，裁决在消费侧（见字段注释）
    if match is not None and (conflict := _id_conflict(candidate, media)):
        return replace(match, id_conflict=conflict)
    return match


def _id_conflict(candidate: TorrentCandidate, media: MediaIdentity) -> str | None:
    """两边都有外部 ID 且不相等时，给一句可直接进活动流水的中文说明。

    只在信号一未命中后调用：任一 ID 相等即已按 exact_id 返回，那种情况下
    另一个 ID 对不上是数据噪音（站点两个链接贴串行），不构成反证。
    """
    if candidate.imdb_id and media.imdb_id and candidate.imdb_id != media.imdb_id:
        return f"站点标注 IMDb {candidate.imdb_id}，本条目是 {media.imdb_id}"
    if candidate.douban_id and media.douban_id and candidate.douban_id != media.douban_id:
        return f"站点标注豆瓣 {candidate.douban_id}，本条目是 {media.douban_id}"
    return None


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
