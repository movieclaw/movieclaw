"""播放侧的内封字幕与字体供给（docs/design/web-player.md §6.2）。

**为什么必须做这个**：PT 片源绝大多数字幕是内封的，外挂 .srt 反而是少数。
只服务外挂轨等于对大部分片子没有字幕——而字幕是「能不能看」而非「好不好看」
的问题。

**为什么不烧录**（硬边界 1）：烧录会把任何档位瞬间拖进全转码。所以内封轨
也走旁挂：抽出来当独立文件下发，由前端渲染。

字幕轨的抽取本身（单飞、可取消、缓存、格式选择）住在中性的
``services/media_extract``——播放器与 AI 字幕生成要的是同一条轨、同一条
ffmpeg 命令、同一份产物，各抽各的会把同一个大文件通读两遍（issue #432）。
本模块只做播放侧的那层包装：把产物表达成 ``SubtitleRef``，外加**内嵌字体**
——它是 ASS 特效字幕的另一半，纯属播放渲染，不与生产端共享。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from movieclaw_api.services.media_extract import (
    EXTRACT_TIMEOUT as _EXTRACT_TIMEOUT,
)
from movieclaw_api.services.media_extract import (
    ExtractedTrack,
    cache_dir,
    extract_track,
    extract_track_async,
    subtitle_format,
    track_codec,
)
from movieclaw_db.models import LibraryFile
from movieclaw_playback.subtitles import SubtitleRef

logger = logging.getLogger("movieclaw_api.playback.subtitles")

#: 内封轨 codec → 抽取后的文件格式；不支持的轨（VobSub 等）返回 None。
embedded_subtitle_format = subtitle_format
#: 第 index 条内封字幕轨的 codec；越界或未探测返回 None。
embedded_track_codec = track_codec

__all__ = [
    "cache_dir",
    "embedded_subtitle_format",
    "embedded_track_codec",
    "extract_embedded_fonts",
    "extract_embedded_subtitle",
    "extract_embedded_subtitle_async",
    "font_cache_dir",
    "safe_font_name",
]


def _as_ref(track: ExtractedTrack | None) -> SubtitleRef | None:
    """抽取产物 → 播放层的可服务定位（两者同形，只是分属不同层的词汇）。"""
    if track is None:
        return None
    return SubtitleRef(path=track.path, format=track.format)


def extract_embedded_subtitle(file: LibraryFile, index: int) -> SubtitleRef | None:
    """阻塞地抽出内封轨，供既有同步/离线调用使用。"""
    return _as_ref(extract_track(file, index))


async def extract_embedded_subtitle_async(
    file: LibraryFile, index: int
) -> SubtitleRef | None:
    """异步抽出内封轨，并在请求取消时回收对应的 ffmpeg 进程。"""
    return _as_ref(await extract_track_async(file, index))


# ---------------------------------------------------------------------------
# 内嵌字体：ASS 特效字幕的另一半
# ---------------------------------------------------------------------------
#
# 番剧的 ASS 字幕把字体作为附件放在 MKV 里。不把它抽出来喂给 JASSUB，字幕会
# 回退成默认字体——排版、字号、描边全走样，特效字幕的观感直接崩。这是
# 「ASS 能播」和「ASS 播得对」之间的差距。

#: 附件按 mimetype 或扩展名认字体。两个都看：压制组填的 mimetype 五花八门，
#: 而有些容器干脆不填。
_FONT_MIMETYPES = frozenset({
    "application/x-truetype-font", "application/x-font-ttf", "application/x-font-otf",
    "application/font-sfnt", "application/vnd.ms-opentype", "application/font-woff",
    "font/ttf", "font/otf", "font/sfnt", "font/collection", "font/woff", "font/woff2",
})
_FONT_SUFFIXES = frozenset({".ttf", ".otf", ".ttc", ".woff", ".woff2", ".pfb"})

#: 允许落盘的附件文件名。**文件名来自媒体文件本体，是不可信输入**——
#: 压制组可以在里面塞 `../../` 或绝对路径。这里用白名单而不是过滤：
#: 只认「字母数字下划线连字符空格点」，且必须是字体扩展名。
_SAFE_FONT_NAME = re.compile(r"^[\w\-. ]{1,120}$")


def font_cache_dir(file_id: int) -> Path:
    return cache_dir() / "fonts" / str(file_id)


def _is_font(filename: str, mimetype: str) -> bool:
    if mimetype.lower() in _FONT_MIMETYPES:
        return True
    return Path(filename).suffix.lower() in _FONT_SUFFIXES


def safe_font_name(filename: str) -> str | None:
    """附件文件名 → 可安全落盘的名字；不合规返回 None。

    只取 basename 并过白名单——绝不能把容器里写的路径当路径用。
    """
    name = Path(filename).name
    if not name or not _SAFE_FONT_NAME.match(name):
        return None
    if Path(name).suffix.lower() not in _FONT_SUFFIXES:
        return None
    return name


def extract_embedded_fonts(file: LibraryFile) -> list[str]:
    """（阻塞，调用方须放线程池）抽出容器里的字体附件，返回文件名列表。

    整个目录一次抽完再原子换上：JASSUB 要的是「这部片的全部字体」，抽到一半
    就被使用会渲染出半套字体，比没有字体更难排查。目录存在即视为抽全了。
    """
    out_dir = font_cache_dir(file.id or 0)
    if out_dir.is_dir():
        return sorted(p.name for p in out_dir.iterdir() if p.is_file())

    video = Path(file.file_path)
    if shutil.which("ffmpeg") is None or not video.is_file():
        return []
    attachments = _list_font_attachments(video)
    if not attachments:
        out_dir.mkdir(parents=True, exist_ok=True)  # 记住「这部片没有字体」，别每次重探
        return []

    staging = out_dir.with_name(f".{out_dir.name}.{uuid.uuid4().hex}.part")
    staging.mkdir(parents=True, exist_ok=True)
    # 一条命令抽全部附件：每个附件单独起一次 ffmpeg 要把容器读 N 遍。
    dump_args: list[str] = []
    for index, name in attachments:
        dump_args += [f"-dump_attachment:t:{index}", str(staging / name)]
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", *dump_args, "-i", str(video), "-f", "null", "-"],
            capture_output=True,
            timeout=_EXTRACT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(staging, ignore_errors=True)
        logger.warning("内嵌字体抽取超时（%.0f 秒）：%s", _EXTRACT_TIMEOUT, video)
        return []
    # ffmpeg 用 -f null 输出时返回码可能非零，但附件已经落盘——以产物为准。
    written = sorted(p.name for p in staging.iterdir() if p.is_file() and p.stat().st_size > 0)
    if not written:
        shutil.rmtree(staging, ignore_errors=True)
        logger.warning(
            "内嵌字体抽取没有产物：%s（%s）",
            video, proc.stderr.decode(errors="replace")[:200],
        )
        return []
    try:
        staging.replace(out_dir)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return []
    return written


def _list_font_attachments(video: Path) -> list[tuple[int, str]]:
    """列出字体附件的 (附件流序号, 安全文件名)。

    序号是「第几条附件流」——与 ffmpeg 的 ``-dump_attachment:t:<k>`` 同源，
    不是绝对流序号。
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-select_streams", "t", "-show_entries", "stream_tags=filename,mimetype",
                str(video),
            ],
            capture_output=True,
            timeout=_EXTRACT_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    try:
        streams = json.loads(proc.stdout).get("streams") or []
    except json.JSONDecodeError:
        return []

    fonts: list[tuple[int, str]] = []
    seen: set[str] = set()
    for order, stream in enumerate(streams):
        tags = stream.get("tags") or {}
        raw_name = str(tags.get("filename") or "")
        mimetype = str(tags.get("mimetype") or "")
        if not _is_font(raw_name, mimetype):
            continue
        name = safe_font_name(raw_name)
        # 同名附件只取第一个：后写的会覆盖前一个，落盘数量与列表对不上
        if name is None or name in seen:
            continue
        seen.add(name)
        fonts.append((order, name))
    return fonts
