"""技术属性词表与带词边界的匹配器——内置提取规则的唯一数据源。

设计要点（吸取 MovieBot 的教训，见 mbot/amr 的补丁化石）：

1. **绝不做裸子串 find()**。MovieBot 用 ``word.find(key)`` 匹配，导致
   'WEB' 命中 'WEBRIP'、裸数字 '1080' 命中集数/年份，被迫维护
   ``invalid_words = {'sense8', ...}`` 这类硬编码补丁。这里每个词条都编译成
   带前后守卫的正则：词条前后不能紧贴同类字符（字母贴字母、数字贴数字）。

2. **同表内键长倒序 + 命中掩蔽（mask）**。先试长键，命中后把匹配区间抹掉，
   短键不会在长键内部二次命中（'DTS' 不会再命中 'DTS-HD MA' 的残余）。
   这取代了 MovieBot 靠"按键长排序 + 祈祷"的做法。

3. **词表值即归一化展示值**。'BLURAY' / 'BLU-RAY' 都归一成 'Blu-ray'，
   前端筛选与展示不必再处理别名。

词条键的书写约定：全大写；空格表示"空格或点号分隔皆可"（'DTS-HD MA' 能匹配
'DTS-HD.MA'）——场景命名里点号与空格等价。
"""

from __future__ import annotations

import re

# -- 词条编译 ---------------------------------------------------------------


def _boundary_pattern(key: str) -> re.Pattern[str]:
    """把词条键编译成带边界守卫的正则。

    守卫规则按键的首/尾字符类型选择：
    - 首/尾是字母 → 相邻侧不能再是字母（'DD' 不命中 'DDP'，但允许 'DDP5.1'
      里 'DDP' 后面跟数字——声道号跟在编码后是命名惯例）；
    - 首/尾是数字 → 相邻侧不能是字母或数字（'1080' 不命中 '21080'/'1080P'，
      后者由更长的 '1080P' 词条负责）；
    - 符号（'+' 等）不加守卫。
    """
    body = re.escape(key).replace(r"\ ", r"[\s.]")
    first, last = key[0], key[-1]
    prefix = ""
    if first.isalpha():
        prefix = r"(?<![A-Za-z])"
    elif first.isdigit():
        prefix = r"(?<![0-9A-Za-z])"
    suffix = ""
    if last.isalpha():
        suffix = r"(?![A-Za-z])"
    elif last.isdigit():
        suffix = r"(?![0-9A-Za-z])"
    return re.compile(prefix + body + suffix)


def compile_table(table: dict[str, str]) -> list[tuple[re.Pattern[str], str]]:
    """把「键 → 归一值」词表编译成按键长倒序的 (正则, 归一值) 列表。"""
    return [
        (_boundary_pattern(key), canon)
        for key, canon in sorted(table.items(), key=lambda kv: len(kv[0]), reverse=True)
    ]


def match_vocab(
    text_upper: str,
    compiled: list[tuple[re.Pattern[str], str]],
    *,
    multi: bool = False,
) -> list[str]:
    """在大写文本上按词表匹配，返回归一值列表（保持词表优先序，去重）。

    命中即掩蔽：把匹配区间替换为空格，防止短键在长键内部二次命中。
    ``multi=False`` 时首个命中即返回（单值字段如分辨率/编码）。
    """
    found: list[str] = []
    masked = text_upper
    for pattern, canon in compiled:
        match = pattern.search(masked)
        if not match:
            continue
        start, end = match.start(), match.end()
        masked = masked[:start] + " " * (end - start) + masked[end:]
        if canon not in found:
            found.append(canon)
        if not multi:
            break
    return found


# -- 分辨率 -----------------------------------------------------------------
# 裸数字键只保留 2160/1080（真实标题里常见裸写），480/720 等裸数字误伤率太高、
# 收益太低，一律要求带 P/I 后缀——这是对 MovieBot 词表的定向裁剪。

RESOLUTION: dict[str, str] = {
    "4320P": "4320p", "8K": "4320p",
    "2160P": "2160p", "2160I": "2160i", "2160": "2160p", "4K": "2160p", "UHD": "2160p",
    "1440P": "1440p", "1440I": "1440i",
    "1080P": "1080p", "1080I": "1080i", "1080": "1080p",
    "720P": "720p", "720I": "720i",
    "576P": "576p", "576I": "576i",
    "480P": "480p", "480I": "480i",
}

# 尺寸写法（3840x2160 / 1920X1080）：宽或高任一命中已知值即可归一
DIMENSION_TO_RESOLUTION: dict[int, str] = {
    7680: "4320p", 4320: "4320p",
    3840: "2160p", 2160: "2160p",
    2560: "1440p", 1440: "1440p",
    1920: "1080p", 1080: "1080p",
    1280: "720p", 720: "720p",
}

# -- 视频编码 ---------------------------------------------------------------
# 注意：REMUX 不在此表——它是封装方式不是编码，MovieBot 把它归进 codec 是错的，
# 我们单列 remux 布尔字段。

VIDEO_CODEC: dict[str, str] = {
    "X265": "x265", "X.265": "x265",
    "H265": "H.265", "H.265": "H.265", "HEVC": "HEVC",
    "X264": "x264", "X.264": "x264",
    "H264": "H.264", "H.264": "H.264", "AVC": "AVC",
    "AV1": "AV1",
    "VC-1": "VC-1", "VC1": "VC-1",
    "VP9": "VP9",
    "MPEG-2": "MPEG-2", "MPEG2": "MPEG-2",
    "MPEG-4": "MPEG-4", "MPEG4": "MPEG-4",
    "XVID": "XviD", "DIVX": "DivX",
}

# 编码族：x265 / H.265 / HEVC 是同一种编码的三种写法（x265 是 HEVC 的一个
# 编码器实现），发布组用哪种纯属命名习惯。用户想表达的是"要 H.265 这一族"，
# 因此筛选与洗版位次都按族比较——规则组写 x265 不该漏掉标 HEVC 的资源。
# （前端 rule-sets-panel.tsx 早已按族提供选项，这里补上后端缺的那一半。）
#
# 这张表同时服务两侧：种子标题解析出的归一值（x265/HEVC/…），以及 ffprobe
# 的 codec_name（hevc/h264/av1，见 library_file.video_codec）——两边查同一张
# 表得到同一个族，洗版基线与候选才可比。表里没有的编码各自成族。
#
# 注意：族**不能**用来表达"二压还是原封"。原封 WEB-DL 也大量标 x265，写法
# 不是事实；重编码与否可靠的维度是 media_source + remux（片源档 T3/T4/T5）。

VIDEO_CODEC_FAMILY: dict[str, str] = {
    "x265": "h265", "h.265": "h265", "hevc": "h265", "h265": "h265",
    "x264": "h264", "h.264": "h264", "avc": "h264", "h264": "h264",
    "av1": "av1",
}


def codec_family(value: str | None) -> str | None:
    """编码 → 族标识；未知/不在表中的编码以自身归一值成族；None 保持 None。"""
    if not value:
        return None
    key = value.casefold()
    return VIDEO_CODEC_FAMILY.get(key, key)


# -- 音频编码 ---------------------------------------------------------------
# 归一值统一去掉声道数（DDP5.1 → DDP）：筛选场景关心"有没有 Atmos / DTS-HD MA"，
# 声道数属于长尾细节，纳入只会让归一值爆炸。
# NAudio（2Audio/3Audio...）是中文 PT 的多音轨标记（常含国语），对用户有筛选价值。

AUDIO: dict[str, str] = {
    "DTS-HD MA": "DTS-HD MA", "DTSHD MA": "DTS-HD MA", "DTS-HDMA": "DTS-HD MA",
    "DTSHDMA": "DTS-HD MA",
    "DTS-HD": "DTS-HD", "DTSHD": "DTS-HD",
    "DTS-X": "DTS:X", "DTSX": "DTS:X", "DTS:X": "DTS:X",
    "DTS": "DTS",
    "TRUEHD": "TrueHD", "TRUE-HD": "TrueHD", "TRUE HD": "TrueHD",
    "ATMOS": "Atmos",
    "DDP": "DDP", "DD+": "DDP", "EAC3": "DDP", "E-AC3": "DDP", "E-AC-3": "DDP",
    "DD": "DD", "AC3": "DD", "AC-3": "DD",
    "AAC": "AAC",
    "FLAC": "FLAC",
    "LPCM": "LPCM", "PCM": "LPCM",
    "OPUS": "OPUS",
    "MP3": "MP3",
    "2AUDIO": "2Audio", "3AUDIO": "3Audio", "4AUDIO": "4Audio", "5AUDIO": "5Audio",
    "2AUDIOS": "2Audio", "3AUDIOS": "3Audio", "4AUDIOS": "4Audio", "5AUDIOS": "5Audio",
}

# -- HDR 格式 ---------------------------------------------------------------
# 可叠加（DV 与 HDR10 常同时出现），故是列表字段。裸 'HDR' 只在更具体的词条
# 都未命中时才有机会命中（键长倒序 + 掩蔽保证）。

HDR: dict[str, str] = {
    "DOLBY VISION": "DV", "DOLBYVISION": "DV", "DOVI": "DV", "DV": "DV",
    "HDR10+": "HDR10+", "HDR10PLUS": "HDR10+",
    "HDR10": "HDR10",
    "HDR VIVID": "HDR Vivid", "HDRVIVID": "HDR Vivid",
    "HLG": "HLG",
    "HDR": "HDR",
}

# -- 片源 -------------------------------------------------------------------

MEDIA_SOURCE: dict[str, str] = {
    "UHD BLURAY": "UHD Blu-ray", "UHD BLU-RAY": "UHD Blu-ray", "UHD-BLURAY": "UHD Blu-ray",
    "BLURAY": "Blu-ray", "BLU-RAY": "Blu-ray", "BD": "Blu-ray",
    "WEB-DL": "WEB-DL", "WEBDL": "WEB-DL", "WEB": "WEB-DL",
    "WEBRIP": "WEBRip", "WEB-RIP": "WEBRip",
    "BDRIP": "BDRip",
    "HDTVRIP": "HDTVRip", "HDTV": "HDTV", "TVRIP": "TVRip",
    "HDRIP": "HDRip",
    "DVDRIP": "DVDRip", "DVD9": "DVD", "DVD5": "DVD", "DVD": "DVD",
    "HD-DVD": "HD-DVD", "HDDVD": "HD-DVD",
}

# -- 流媒体平台 ---------------------------------------------------------------
# 平台与压制组是**两个正交字段**：``IQ.WEB-DL...-HHWEB`` 的平台是 iQIYI、
# 压制组是 HHWEB；``...-Pure@HDSWEB`` 没有平台标记，整体是压制组。
#
# 两道闸把误判压到最低（"宁缺毋滥"，与本模块其余词表同一取向）：
#   ① 整条文本必须带 WEB / WEB-DL / WEBRip 来源标记，否则一律不识别平台——
#      平台标记只在流媒体资源上有意义，片名里的 Apple / Max 不是平台；
#   ② 短别名（见 _PLATFORM_SHORT_ALIASES）还必须**紧邻**来源标记，
#      "Apocalypse.Now.2160p.WEB-DL" 这类不会把片名词当成平台。
#
# 词表是**定向裁剪**过的，以下别名有意不收，请勿"顺手补全"：
#   NOW / PLAY / IT / CORE / CRIT / DISC / FRIDAY —— 都是常用词或技术标记
#   （"Blu-ray DISC"、"DISC 1"、语言标签 IT、Friday the 13th），即便有 WEB
#   上下文也会误命中；对应平台改收不易碰撞的长写法（NOWPLAYER / GOOGLE TV /
#   ITUNES / SONY PICTURES CORE / CRITERION / DISCOVERY+）。
#
# HBO / HBO MAX / MAX 合并成同一个规范值：它们是同一平台的新旧名，拆成三个
# 值只会让用户配置时无所适从（发布名用哪个纯看发布组习惯）。

PLATFORM: dict[str, str] = {
    # 国际
    "NETFLIX": "netflix", "NF": "netflix",
    "AMAZON": "amazon", "AMZN": "amazon",
    "DISNEY+": "disney_plus", "DISNEY PLUS": "disney_plus", "DSNP": "disney_plus",
    "HBO MAX": "hbo_max", "HMAX": "hbo_max", "HBOM": "hbo_max",
    "HBO": "hbo_max", "MAX": "hbo_max",
    "HULU": "hulu",
    "APPLE TV+": "apple_tv_plus", "APPLE TV PLUS": "apple_tv_plus",
    "APPLE TV": "apple_tv_plus", "ATVP": "apple_tv_plus", "APTV": "apple_tv_plus",
    "ATV": "apple_tv_plus",
    "PARAMOUNT+": "paramount_plus", "PARAMOUNT PLUS": "paramount_plus",
    "PMTP": "paramount_plus",
    "PEACOCK": "peacock", "PCOK": "peacock",
    "SHOWTIME": "showtime", "SHO": "showtime",
    "STARZ": "starz",
    "DISCOVERY+": "discovery_plus", "DSCP": "discovery_plus",
    "CRAVE": "crave", "STAN": "stan", "ROKU": "roku",
    "GOOGLE TV": "google_tv", "ITUNES": "itunes",
    "SONY PICTURES CORE": "sony_core", "BCORE": "sony_core",
    "CRITERION": "criterion",
    # 中国及亚洲
    "IQIYI": "iqiyi", "IQ": "iqiyi",
    "TENCENT VIDEO": "wetv", "WETV": "wetv",
    "YOUKU": "youku",
    "MANGOTV": "mangotv", "MGTV": "mangotv",
    "BILIBILI": "bilibili", "BILI": "bilibili",
    "VIU": "viu", "NOWPLAYER": "nowplayer", "MYTVSUPER": "mytv_super",
    "HAMIVIDEO": "hami_video", "HAMI": "hami_video",
    "LINETV": "line_tv", "KKTV": "kktv",
    "TVING": "tving", "WAVVE": "wavve",
    "COUPANG PLAY": "coupang_play", "CPNG": "coupang_play",
    "KOCOWA": "kocowa", "KCW": "kocowa", "VIKI": "viki",
    "U-NEXT": "unext", "TVER": "tver", "FOD": "fod", "DMM-TV": "dmm_tv",
    "HOTSTAR": "hotstar", "HTSR": "hotstar",
    # 动漫
    "CRUNCHYROLL": "crunchyroll", "CR": "crunchyroll",
    "HIDIVE": "hidive", "HIDI": "hidive",
    "ABEMA TV": "abema", "ABEMA": "abema", "ADN": "adn",
    "FUNIMATION": "funimation", "FUNI": "funimation",
    "VRV": "vrv", "WAKANIM": "wakanim", "WKN": "wakanim",
    "B-GLOBAL": "b_global",
}

# 规范值全集：规则组只接受这些值（词表演进时，配置里的陈旧值被丢弃而非报错，
# 见 RuleSetSpec.platforms 的说明）
PLATFORM_IDS: frozenset[str] = frozenset(PLATFORM.values())

# 必须紧邻 WEB 来源标记才成立的短别名：本身是常用词、人名或过短的代码。
# HBO 有意不在此列：三字母但辨识度极高，不与常用词碰撞，加约束只会漏识别
_PLATFORM_SHORT_ALIASES: frozenset[str] = frozenset(
    {
        "NF", "MAX", "ATV", "SHO", "STAN", "IQ", "BILI", "VIU", "HAMI",
        "KCW", "FOD", "HTSR", "CR", "HIDI", "ADN", "VRV", "WKN",
    }
)

# WEB 来源标记：WEB / WEB-DL / WEBRip（含点号、下划线、连字符分隔）
_WEB_SOURCE_RE = re.compile(r"(?<![A-Z])WEB(?:[\s._-]?(?:DL|RIP))?(?![A-Z])")
# "紧邻" = 两者之间只有分隔符
_SEPARATORS_ONLY_RE = re.compile(r"^[\s._-]*$")


def _compile_platform_table() -> list[tuple[re.Pattern[str], str, bool]]:
    """与 compile_table 同构，额外带上"该别名是否需要紧邻来源标记"。"""
    return [
        (_boundary_pattern(alias), canon, alias in _PLATFORM_SHORT_ALIASES)
        for alias, canon in sorted(PLATFORM.items(), key=lambda kv: len(kv[0]), reverse=True)
    ]


def _adjacent_to_web_source(text: str, start: int, end: int, spans) -> bool:
    """别名与任一 WEB 来源标记之间是否只隔着分隔符。"""
    return any(
        (source_end <= start and _SEPARATORS_ONLY_RE.match(text[source_end:start]))
        or (source_start >= end and _SEPARATORS_ONLY_RE.match(text[end:source_start]))
        for source_start, source_end in spans
    )


def match_platforms(text_upper: str) -> list[str]:
    """提取流媒体平台规范值（同一资源可带多个标记，去重保序）。"""
    spans = [m.span() for m in _WEB_SOURCE_RE.finditer(text_upper)]
    if not spans:
        return []  # 没有 WEB 来源标记 → 不是流媒体资源，平台一律不识别
    found: list[str] = []
    masked = text_upper
    for pattern, canon, needs_adjacency in PLATFORM_COMPILED:
        while match := pattern.search(masked):
            start, end = match.span()
            # 命中即掩蔽（同 match_vocab）：短别名不会在长别名内部二次命中，
            # 未通过紧邻判定的那次也被抹掉，循环继续找同一别名的其他出现
            masked = masked[:start] + " " * (end - start) + masked[end:]
            if needs_adjacency and not _adjacent_to_web_source(text_upper, start, end, spans):
                continue
            if canon not in found:
                found.append(canon)
            break
    return found


# -- 压制组大小写归一表 -------------------------------------------------------
# 种子标题里组名大小写混乱（wiki/WiKi/WIKI），此表把已知组归一成官方写法。
# 词表来自 MovieBot 积累的 media_stream.json，是它多年踩坑攒下的真实资产。
# 未知组不依赖此表——尾段提取到什么就原样保留（详见 extractors.release_group）。

RELEASE_GROUP_CASE: dict[str, str] = {
    "CMCT": "CMCT", "CMCTV": "CMCTV", "OLDBOYS": "Oldboys", "CHD": "CHD",
    "CHDBITS": "CHD", "CHDWEB": "CHDWEB", "SGNB": "SGNB", "TTG": "TTG",
    "WIKI": "WiKi", "NGB": "NGB", "HDS": "HDS", "HDSKY": "HDSky",
    "HDSTV": "HDSTV", "HDSWEB": "HDSWEB", "HDSPAD": "HDSPad",
    "MTEAM": "MTeam", "MPAD": "MPAD", "CNHK": "CNHK", "TNP": "TnP",
    "KISHD": "KiSHD", "BMDRU": "BMDru", "ONEHD": "OneHD", "STBOX": "StBOX",
    "R2HD": "R2HD", "GEEK": "Geek", "HDC": "HDC", "HDCHINA": "HDC",
    "HDWING": "HDWinG", "FRDS": "FRDS", "CFANDORA": "cfandora",
    "PTER": "PTer", "PTERWEB": "PTerWEB", "BEITAI": "BeiTai",
    "OURBITS": "OurBits", "PBK": "PbK", "OURTV": "OurTV",
    "ILOVETV": "iLoveTV", "ILOVEHD": "iLoveHD", "FLTTH": "FLTTH",
    "AO": "Ao", "HDH": "HDH", "LHD": "LHD", "LEAGUE": "League",
    "I18N": "i18n", "CINT": "CiNT", "BEAST": "beAst", "PTH": "PTH",
    "ZZH": "ZZH", "BTSCHOOL": "BTSCHOOL", "BTSHD": "BtsHD", "BTSTV": "BtsTV",
    "DREAM": "Dream", "DBTV": "DBTV", "QHSTUDIO": "QHstudIo",
    "AUDIENCES": "Audiences", "ADE": "ADE", "ADWEB": "ADWeb",
    "HARES": "Hares", "DISCFAN": "DiscFan", "HKFACT": "HKFACT", "DGB": "DGB",
    "PUTAO": "PuTao", "TJUPT": "TJUPT", "OPS": "OPS", "FFANS": "FFans",
    "HDZ": "HDZ", "HDZONE": "HDZone", "HDZTV": "HDZTV", "JOYHD": "JoyHD",
    "NBMAX": "NBMAX", "NTG": "NTG", "NTB": "NTb", "EVO": "EVO",
    "CMRG": "CMRG", "TOMMY": "TOMMY", "FLUX": "FLUX", "CTRLHD": "CtrlHD",
    "NOGRP": "NOGRP", "LOKIHD": "LOKiHD", "X0R": "x0r", "PBO": "PBO",
    "MZABI": "MZABI", "DTONE": "DTOne", "IFT": "iFT", "ENICHI": "Enichi",
    "ALFAHD": "alfaHD", "IKA": "iKA", "TEPES": "TEPES", "SKYFIRE": "SKYFiRE",
    "CBFM": "CBFM", "WRB": "WRB", "AFG": "AFG", "CAKES": "CAKES",
    "MSD": "mSD", "IEVA": "IEVA", "MEGUSTA": "MeGusta", "MIXED": "Mixed",
    "HDMAN": "HDMaN", "PLAYHD": "playHD", "PLAYTV": "playTV",
    "PLAYSD": "playSD", "PLAYWEB": "playWEB", "SLOT": "SLOT",
    "NOSIVID": "NOSiViD", "KOGI": "KOGi", "NONDR": "NonDR", "HAMR": "HAMR",
    "AJP69": "AJP69", "BEYONDHD": "BeyondHD", "CHOTAB": "Chotab",
    "TRIM": "TRiM", "HIFI": "HiFi", "FGT": "FGT", "RARBG": "RARBG",
    "SPARKS": "SPARKS", "ROVERS": "ROVERS", "DRONES": "DRONES",
}

# -- 技术 token 黑名单 --------------------------------------------------------
# 压制组尾段提取时的反例集合：标题以 "-x265"、"-REMUX"、"-4K" 这类技术词结尾时
# 不能把它们当组名。集合汇总全部词表的键与归一值，再补上词表外的常见工艺标记。

_EXTRA_TECH_TOKENS: set[str] = {
    "REMUX", "DIY", "10BIT", "8BIT", "HDR", "SDR", "3D", "IMAX", "60FPS",
    "HFR", "PROPER", "REPACK", "INTERNAL", "LIMITED", "UNRATED", "EXTENDED",
    "COMPLETE", "GBR", "CEE", "ESP", "FRA", "GER", "ITA", "JPN", "KOR",
    # 技术词被连字符切开后的尾段：标题以 "WEB-DL"/"Blu-ray"/"DTS-HD" 结尾时，
    # 尾段提取看到的是 "-DL"/"-ray"/"-HD"，这些残片不是组名
    "DL", "RIP", "RAY", "HD", "MA", "X", "PLUS", "VISION", "DOLBY",
}


def _all_tech_tokens() -> set[str]:
    tokens: set[str] = set(_EXTRA_TECH_TOKENS)
    for table in (RESOLUTION, VIDEO_CODEC, AUDIO, HDR, MEDIA_SOURCE):
        tokens.update(k.upper() for k in table)
        tokens.update(v.upper() for v in table.values())
    return tokens


TECH_TOKENS: set[str] = _all_tech_tokens()

# -- 预编译词表（模块加载时一次性完成）---------------------------------------

RESOLUTION_COMPILED = compile_table(RESOLUTION)
VIDEO_CODEC_COMPILED = compile_table(VIDEO_CODEC)
AUDIO_COMPILED = compile_table(AUDIO)
HDR_COMPILED = compile_table(HDR)
MEDIA_SOURCE_COMPILED = compile_table(MEDIA_SOURCE)
PLATFORM_COMPILED = _compile_platform_table()
