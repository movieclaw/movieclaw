"""内置提取器集合——**只负责封闭词表的技术字段**。

每个提取器是一个纯函数：``(原始文本) -> 部分字段字典``，提取不到就返回空字典
（绝不返回猜测值）。管线（见 ``__init__.enrich``）逐个调用并合并结果，单个
提取器抛异常只会被跳过，不影响其它字段——与"单站失败不拖垮整次搜索"同一铁律。

分工边界：分辨率/编码/音频/HDR/片源/REMUX/压制组这些**写法固定**的字段走
本模块的词表正则（精确且零成本）；片名/年份/季集/题材这些**边界模糊**的
字段由小模型抽取（见 inference.py）——规则解决写法固定的，模型解决边界
模糊的，两边各守各的。

要新增可提取的信息：写一个新函数，加进 ``EXTRACTORS`` 注册表，并把
``__init__.ENRICH_VERSION`` +1（触发存量数据在下次启动时自动重算）。
"""

from __future__ import annotations

import re

from movieclaw_enrich.vocab import (
    AUDIO_COMPILED,
    DIMENSION_TO_RESOLUTION,
    HDR_COMPILED,
    MEDIA_SOURCE_COMPILED,
    RELEASE_GROUP_CASE,
    RESOLUTION_COMPILED,
    TECH_TOKENS,
    VIDEO_CODEC_COMPILED,
    match_platforms,
    match_vocab,
)

# -- 技术属性：词表匹配 --------------------------------------------------------

# 尺寸写法兜底：3840x2160 / 1920X1080（裸数字词条会被相邻的 x 挡住，走这里）
_DIMENSION_RE = re.compile(r"(?<![0-9])(\d{3,4})\s?[X×]\s?(\d{3,4})(?![0-9])")


def extract_resolution(text: str) -> dict[str, object]:
    up = text.upper()
    hits = match_vocab(up, RESOLUTION_COMPILED)
    if hits:
        return {"resolution": hits[0]}
    m = _DIMENSION_RE.search(up)
    if m:
        for value in (int(m.group(1)), int(m.group(2))):
            resolution = DIMENSION_TO_RESOLUTION.get(value)
            if resolution:
                return {"resolution": resolution}
    return {}


def extract_video_codec(text: str) -> dict[str, object]:
    hits = match_vocab(text.upper(), VIDEO_CODEC_COMPILED)
    return {"video_codec": hits[0]} if hits else {}


def extract_audio(text: str) -> dict[str, object]:
    hits = match_vocab(text.upper(), AUDIO_COMPILED, multi=True)
    return {"audio": hits} if hits else {}


# 「字幕(?!组)」：「无字幕组水印」说的是发布组水印，不是没有字幕
_NO_SUBTITLE_RE = re.compile(
    r"(?:无|没有|不含)(?:任何|全部|中文|简体中文)?字幕(?!组)|"
    r"字幕(?:语言|語言)?\s*[:：]\s*(?:无|無|没有|沒有)|"
    r"(?<![A-Za-z])NO[\s._-]*SUBS?(?![A-Za-z])",
    re.I,
)

# 音轨/配音语境守卫。两处易错边界：
# ① 「(?<!字幕)语言」——「字幕语言：简体」是字幕声明，不能被当成语言字段吃掉；
# ② 结尾负向前瞻——「国语配音 简体中文字幕」里「简体」属于其后的字幕声明，
#   守卫若把「配音 简体中文」删掉会连带毁掉字幕证据。
_NON_SUBTITLE_SIMPLIFIED_RE = re.compile(
    r"(?:简体(?:中文)?|簡體(?:中文)?|简中|簡中)\s*"
    r"(?:配音|音轨|音軌|语音|語音|剧情简介|劇情簡介)|"
    r"(?:配音|音轨|音軌|语音|語音|(?<!字幕)语言|(?<!字幕)語言)\s*[:：]?\s*"
    r"(?:简体(?:中文)?|簡體(?:中文)?|简中|簡中)"
    r"(?!(?:特效|硬|软|軟)?(?:中文)?字幕|中字)",
    re.I,
)

_LATIN_SIMPLIFIED_SUBTITLE_RE = re.compile(
    r"(?<![A-Za-z])(?:CHS|ZHS|ZH[-_]?HANS)(?![A-Za-z])",
    re.I,
)

_FULL_SIMPLIFIED_SUBTITLE_RE = re.compile(
    r"(?:简体(?:中文)?|簡體(?:中文)?)(?:特效|硬|软|軟)?(?:中文字幕|字幕|中字)|"
    r"字幕(?:语言|語言)?\s*[:：/]\s*(?:简体(?:中文)?|簡體(?:中文)?)",
    re.I,
)

# 桥接段：配对/分隔标记与「字幕」尾词之间允许的填充。不跨句读（逗号即换了
# 从句，「简繁双语配音，内封英文字幕」不能把简繁桥到英文字幕上），也不跨
# 配音/音轨词（同一从句里「简繁双语配音 内封字幕」同理）。
_BRIDGE = r"(?:(?!配音|音轨|音軌)[^。，；;！？!?\n]){0,24}?"

# 配对标记：简+第二语言（简英/简日/简韩/简繁及其 体/中 插入变体，如
# 「简体日语双字幕」「简中英三语字幕」），繁在前的 繁简 也算含简体
_PAIRED_SIMPLIFIED_SUBTITLE_RE = re.compile(
    r"(?:(?:简|簡)(?:体|體|中)?(?:英|日|韩|韓|繁)|繁简|繁簡)" + _BRIDGE +
    r"(?:字幕|中字|软字幕|軟字幕|硬字幕|SUP)|"
    r"(?:简|簡)(?:体|體)?(?:英|日)\s*(?:双语|雙語)",
    re.I,
)

_SHORT_SIMPLIFIED_SUBTITLE_RE = re.compile(
    r"(?:内封|內封|内嵌|內嵌|外挂|外掛)\s*(?:简中|簡中)|"
    r"(?:简中|簡中)\s*(?:内封|內封|内嵌|內嵌|外挂|外掛)|"
    r"(?:简中|簡中)(?:SUP|特效|硬|软|軟)*字幕|"
    r"(?:简中|簡中)(?![A-Za-z0-9\u3400-\u9fff])",
    re.I,
)

_DELIMITED_SIMPLIFIED_SUBTITLE_RE = re.compile(
    r"(?:简体|簡體|简|簡)\s*[|/、+&]" + _BRIDGE +
    r"(?:字幕|中字|软字幕|軟字幕|硬字幕|SUP)",
    re.I,
)


def extract_subtitle_languages(text: str) -> dict[str, object]:
    """提取明确声明的字幕语言；「中字」等泛称不推断简繁，避免误筛。

    不进 ``EXTRACTORS`` 注册表：「无字幕」等否定要跨主/副标题生效（标题带
    CHS、描述写「无字幕」时后者推翻前者），由管线用双段合并文本单独调用一次。
    """
    # 「无字幕」是全局否定；「无内嵌字幕，外挂简中」只否定一种承载方式，不应误杀。
    if _NO_SUBTITLE_RE.search(text):
        return {}

    # 先抹掉音轨/配音/语言字段里的「简体中文」，再识别字幕上下文，避免词序变化误报。
    text = _NON_SUBTITLE_SIMPLIFIED_RE.sub("", text)
    if any(
        pattern.search(text)
        for pattern in (
            _LATIN_SIMPLIFIED_SUBTITLE_RE,
            _FULL_SIMPLIFIED_SUBTITLE_RE,
            _PAIRED_SIMPLIFIED_SUBTITLE_RE,
            _SHORT_SIMPLIFIED_SUBTITLE_RE,
            _DELIMITED_SIMPLIFIED_SUBTITLE_RE,
        )
    ):
        return {"subtitle_languages": ["zh-Hans"]}
    return {}


def extract_hdr(text: str) -> dict[str, object]:
    hits = match_vocab(text.upper(), HDR_COMPILED, multi=True)
    return {"hdr": hits} if hits else {}


def extract_media_source(text: str) -> dict[str, object]:
    hits = match_vocab(text.upper(), MEDIA_SOURCE_COMPILED)
    return {"media_source": hits[0]} if hits else {}


def extract_platforms(text: str) -> dict[str, object]:
    hits = match_platforms(text.upper())
    return {"platforms": hits} if hits else {}


_REMUX_RE = re.compile(r"(?<![A-Za-z])REMUX(?![A-Za-z])")


def extract_remux(text: str) -> dict[str, object]:
    return {"remux": True} if _REMUX_RE.search(text.upper()) else {}


# 全集标记：写法固定（COMPLETE / Fin / 全集 / 全话），属封闭词表归本通道；
# 带数字的"全12集"由模型通道抽 EPISODE_TOTAL，二者互补。
# Fin 是动漫 BDRip 命名的完结标记（"TV 01-12 Fin"），必须大小写敏感——
# 全大写 FIN 是芬兰国家代码（"1080p.FIN.Blu-ray"），全小写 fin 见于西语词内。
# "End/完"刻意不收——它们标记"本集是大结局"（单集种子），不代表这是全集包
_COMPLETE_MARKER_RE = re.compile(
    r"(?<![A-Za-z])(?:COMPLETE|(?-i:Fin))(?![A-Za-z])|全[集话話]", re.I
)


def extract_complete_marker(text: str) -> dict[str, object]:
    return {"complete": True} if _COMPLETE_MARKER_RE.search(text) else {}


# -- 压制组：尾段优先 ---------------------------------------------------------
# 场景命名惯例：组名在标题末尾的 '-' 之后（"...x265-WiKi"）。策略：
# 1. 先剥掉末尾的括号装饰段（"-CMCT[国语中字]" 的 [国语中字]）；
# 2. 取末尾 '-'/'@' 后的 token 作为候选——含字母、非纯数字、不是技术词
#    （"-REMUX"/"-4K" 结尾不是组名）即采纳，已知组按词表归一大小写；
# 3. 尾段无候选时，按已知组词表找 "-组名" 形态兜底（必须带 '-' 前缀，
#    避免 MovieBot 那种 CHD/TTG 短词命中片名、被迫用位置启发式硬扛的坑）。

_TRAILING_DECOR_RE = re.compile(r"\s*[\[【（(][^\[\]【】（）()]*[\]】）)]\s*$")
_TAIL_GROUP_RE = re.compile(r"[-@]\s?([A-Za-z0-9@!]{2,20})\s*$")

_KNOWN_GROUP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"-{re.escape(key)}(?![A-Za-z0-9])"), canon)
    for key, canon in sorted(
        RELEASE_GROUP_CASE.items(), key=lambda kv: len(kv[0]), reverse=True
    )
]


def extract_release_group(text: str) -> dict[str, object]:
    stripped = text.rstrip()
    # 最多剥两层末尾装饰（"[中字][DIY]" 这种叠加）
    for _ in range(2):
        cleaned = _TRAILING_DECOR_RE.sub("", stripped)
        if cleaned == stripped:
            break
        stripped = cleaned

    m = _TAIL_GROUP_RE.search(stripped)
    if m:
        token = m.group(1)
        up = token.upper()
        if not token.isdigit() and re.search(r"[A-Za-z]", token) and up not in TECH_TOKENS:
            return {"release_group": RELEASE_GROUP_CASE.get(up, token)}

    up_text = stripped.upper()
    for pattern, canon in _KNOWN_GROUP_PATTERNS:
        if pattern.search(up_text):
            return {"release_group": canon}
    return {}


# -- 注册表 -------------------------------------------------------------------
# 顺序即执行顺序；各提取器彼此独立（不读对方结果），顺序只影响日志可读性。

EXTRACTORS: list[tuple[str, object]] = [
    ("resolution", extract_resolution),
    ("video_codec", extract_video_codec),
    ("audio", extract_audio),
    ("hdr", extract_hdr),
    ("media_source", extract_media_source),
    ("platforms", extract_platforms),
    ("remux", extract_remux),
    ("complete_marker", extract_complete_marker),
    ("release_group", extract_release_group),
]
