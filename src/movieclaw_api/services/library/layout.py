"""入库的公共命名约定与共享常量（扫描 / 监听导入 / 整理共用）。

历史沿革：本模块曾承载订阅专属的"下载完成 → 硬链入库"管线
（import_completed_torrent）。架构定稿"订阅止于投递"后，搬运统一由
监听导入（library_ingest，按 info_hash 认领订阅身份）与库扫描（原地
入账）完成，工单由库存对账关闭（wanted_fulfillment），订阅专属管线
退役。这里沉淀的是三个入库引擎共用的约定：

- ``VIDEO_EXTS`` / ``STRM_EXT`` / ``SCAN_VIDEO_EXTS``：视频文件扩展名
  （入库对象）与 strm 占位文件的接纳约定；
- ``IN_PROGRESS_MARKERS``：下载器/浏览器的"未完成"标记后缀
  （扫描与监听导入的完整性检测共用）；
- ``season_from_dir`` / ``entry_dirs``：季目录与条目目录的判定——识别链
  （取片名证据）、待识别分组、条目真实删除三处必须是同一套约定，各写
  各的会出事（实测隐患：删除按"库根直接子目录"算条目目录，遇到
  ``剧集/大陆/风筝 (2017)/`` 这种分类分组层会把整个「大陆」目录删掉）；
"""

from __future__ import annotations

import re
from pathlib import Path

# 视频文件扩展名（入库对象）；其余（字幕/nfo/图片）v1 不搬运
VIDEO_EXTS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".ts",
    ".m2ts",
    ".wmv",
    ".mov",
    ".flv",
    ".rmvb",
    ".mpg",
    ".mpeg",
    ".m4v",
    ".webm",
}

# strm：Kodi/Emby/Jellyfin 生态约定的"播放地址占位文件"——内容是一行
# 播放 URL 的纯文本（网盘挂载场景的标配，本地不占盘、播放时才拉流）。
# 识别与刮削全靠文件名/目录名，与普通视频一致；但文件本体没有媒体流，
# ffprobe 探测对它天然无意义（规格列留空）。
STRM_EXT = ".strm"

# 库扫描/实时监控/整理的接纳范围 = 视频 + strm 占位。**下载入库链
# （监听导入 ingest）刻意不收 strm**：那里的 ffprobe 完整性门禁
# （挡残缺文件）对 strm 必然失败，会陷入"探测失败自动重试"的死循环。
SCAN_VIDEO_EXTS = VIDEO_EXTS | {STRM_EXT}

# 外挂字幕扩展名（发现对象，docs/design/jellyfin-subtitle.md §2.1）。
# 只收语义明确的文本字幕：.sub 有 MicroDVD/VobSub 歧义、.sup/.idx 是
# 图形字幕（无法转换、播放器支持参差），均不收。
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
# 文件名/路径含这些标记的视频不入库（样品片段等）
_IGNORE_MARKERS = ("sample",)

# 下载器/浏览器的"未完成"标记（文件名小写后缀匹配）：qBittorrent .!qb、
# aria2 控制文件 .aria2、Chrome .crdownload、Firefox/迅雷等 .part/.td、
# BitComet .bc!、通用临时后缀。扫描器与监听导入共用（放在本模块避免
# scan ↔ ingest 的循环导入）
IN_PROGRESS_MARKERS = (
    ".!qb",
    ".part",
    ".aria2",
    ".crdownload",
    ".download",
    ".downloading",
    ".td",
    ".bc!",
    ".tmp",
    ".temp",
    ".unfinished",
)


def is_disc_dir(directory: Path) -> bool:
    """原盘目录判定：蓝光（BDMV）或 DVD（VIDEO_TS）结构。

    扫描（原盘按目录整体入账、不进内部遍历）与监听导入（入库落点避让，
    docs/design/disc-version-layout.md §3）共用同一判据——放在本模块
    避免 scan ↔ ingest 的循环导入。
    """
    return (directory / "BDMV").is_dir() or (directory / "VIDEO_TS").is_dir()


# 季目录名："Season 02" / "S02" / "Specials" / "特别篇"
_SEASON_DIR = re.compile(r"^(?:season[ ._-]*(\d{1,3})|s(\d{1,3}))$", re.IGNORECASE)
_SPECIALS_DIR = re.compile(r"^(?:specials?|特别篇|特典)$", re.IGNORECASE)

# 裸尾号集数：「走向共和01」「大宅门22」——无 SxxExx/「第N集」标记、纯靠
# 结尾数字排集的命名（央视老剧资源极常见）。紧邻的前一个字符不能是字母
# 或数字：挡住 x264/DDP5.1 这类技术尾巴；1~3 位挡住 4 位年份（Movie 2003
# 的 "003" 也因前一位是数字而不中）
_TRAILING_INDEX = re.compile(r"(?<![0-9A-Za-z])(\d{1,3})\s*$")

# 显式 SxxEyy 标记：场景命名里语义最强的季集声明。前界挡住词内片段
# （"XS06E01"），后界挡住数字粘连（"S02E051080p" 宁可不中，交回模型通道）
_EXPLICIT_SXXEYY = re.compile(r"(?i)(?<![0-9A-Za-z])S(\d{1,3})[ ._-]?E(\d{1,4})(?!\d)")

# 裸 E/EP 集号标记（无 S 季号前缀）："Hikaru No Go.E01.2020..."、"...EP12..."。
# 单季剧的种子极常见这么命名，而这种串**在信息论上根本不含季号**——模型只能
# 幻觉（线上病例：E10 被同时标成季号与集号，整包散进 25 个不存在的季目录）。
# 集号本身却是确定的，用正则拿下来即可，季号另由证据链求解。
#
# 前界只认真正的分隔符（不是"非字母数字"）：`[` 不算界，否则动漫文件名尾部的
# CRC32 校验码 "[E5F1A2B3]" 会被读成 E5。后界挡数字粘连。可选的 P 收编 "EP01"
# 写法——28024 条金标语料实测：召回从 936 涨到 1665，误命中恒为 7 条且逐条核对
# 全是标注漏标的真集号（E204/E219/E07），真实误报为 0。允许 E 与数字间再夹一个
# 分隔符的变体已否决：会把 "…no Anata e 2022…" 的年份、"…e 2nd Season…" 的季号
# 吃成集号（误命中 7 → 22）。
_EXPLICIT_EPISODE = re.compile(r"(?:^|[ ._\-])(?:[Ee][Pp]?)(\d{1,4})(?!\d)")


def trailing_index_episode(stem: str) -> int | None:
    """裸尾号命名声明的集号；解析不出（或为 0）返回 None。

    只该在常规集号解析（SxxExx/第N集）全灭后作兜底——常规标记的语义
    强得多，兜底规则抢跑会把「S01E03.特辑2」这类名字带偏。
    """
    match = _TRAILING_INDEX.search(stem.strip())
    if match is None:
        return None
    value = int(match.group(1))
    return value if value >= 1 else None


def explicit_unit(stem: str) -> tuple[int, int] | None:
    """文件名显式 SxxEyy 标记声明的 (季号, 集号)；没有该标记返回 None。

    NER 模型面向种子标题训练，对纯场景命名的单集文件名会漏抽/错标集号
    （torrent-ner-v2 把 "S06E01" 的 01 标成季号的线上病例），而显式标记的
    语义是确定的——它在时必须压过模型结果。E00（先导/特辑占位）原样返回
    (season, 0)：集号 0 在管线里是「无集号」哨兵，入库层据此识别"这是
    显式声明的第 0 集"并按占位跳过，而不是误报解析失败。
    """
    match = _EXPLICIT_SXXEYY.search(stem)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def explicit_episode(stem: str) -> int | None:
    """文件名裸 E/EP 标记声明的集号；没有该标记或标记自相矛盾时返回 None。

    只出集号、不猜季号——这正是它与 ``explicit_unit`` 的分工：``E01`` 里没有
    季号信息，硬猜就是幻觉。季号交给 ``units.resolve_units`` 的证据链。

    同一个名字里解出多个不同的 E 号（"EP01-EP04" 这类区间/合集写法）视为歧义
    返回 None：宁可交回上层挂起等人，也不从区间里随手挑一个。E00 原样返回 0，
    与 ``explicit_unit`` 同口径（0 是管线的「无集号」哨兵，上层据此按先导/
    特辑占位跳过，而不是误报解析失败）。
    """
    values = {int(match.group(1)) for match in _EXPLICIT_EPISODE.finditer(stem)}
    return values.pop() if len(values) == 1 else None


def season_from_dir(directory: Path) -> int | None:
    """目录名声明的季号（特别篇为 0）；不是季目录返回 None。"""
    name = directory.name.strip()
    if _SPECIALS_DIR.match(name):
        return 0
    match = _SEASON_DIR.match(name)
    return int(match.group(1) or match.group(2)) if match else None


def entry_dirs(root: Path, file: Path) -> list[Path]:
    """库根与文件之间的各级目录，由近及远；季目录跳过（它不带片名信息）。

    ``{root}/大陆/风筝 (2017)/Season 1/x.mkv`` → ``[风筝 (2017), 大陆]``。
    第一个元素就是**条目目录**；文件直接躺在库根下、或不在库根之下时为空。

    为什么不是"库根的直接子目录"：分类分组层（大陆/欧美/日韩、按年代
    分文件夹）在真实媒体库里非常普遍，认死第一层会把分组名当条目名。
    """
    dirs: list[Path] = []
    current = file.parent
    while current != root and current.parent != current:
        try:
            current.relative_to(root)
        except ValueError:
            break
        if season_from_dir(current) is None:
            dirs.append(current)
        current = current.parent
    return dirs


def entry_dir_of(roots: list[Path], file: Path) -> Path | None:
    """文件归属的条目目录（多库根版本）；不在任何根下或裸文件时为 None。"""
    for root in roots:
        try:
            file.relative_to(root)
        except ValueError:
            continue
        dirs = entry_dirs(root, file)
        return dirs[0] if dirs else None
    return None

