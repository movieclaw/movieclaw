"""内封字幕轨的按需抽取（docs/design/web-player.md §6.2）。

**为什么必须做这个**：PT 片源绝大多数字幕是内封的，外挂 .srt 反而是少数。
只服务外挂轨等于对大部分片子没有字幕——而字幕是「能不能看」而非「好不好看」
的问题。

**为什么不烧录**（硬边界 1）：烧录会把任何档位瞬间拖进全转码。所以内封轨
也走旁挂：抽出来当独立文件下发，由前端渲染。

**为什么保留原格式**：ASS 转 VTT 会丢掉特效与排版，番剧字幕直接崩。因此
ASS/SSA 原样 copy 出来交 JASSUB，纯文本轨才转 SRT（再由服务层按需转 VTT）。
这也是不复用 ``subtitle_gen.extract`` 那套抽取的原因——它为 AI 翻译服务，
一律转成 SRT 拿纯文本，对播放来说是有损的。两处需求不同，各自窄实现比
硬凑一个参数化的公共函数更清楚。

沿用 ``media_probe`` 的直接 subprocess 风格：线程池执行、超时保护、中文报错。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from movieclaw_db.models import LibraryFile
from movieclaw_playback.subtitles import SubtitleRef

logger = logging.getLogger("movieclaw_api.playback.subtitles")

#: 抽取要通读整个容器，大文件是分钟级，比探测慢得多。
_EXTRACT_TIMEOUT = 120.0

#: 纯文本轨：抽成 SRT，服务层再按请求转 VTT 交 ``<track>``。
_TEXT_CODECS = frozenset({"subrip", "srt", "mov_text", "text", "webvtt", "vtt"})
#: 特效轨：原样 copy 出来交 JASSUB，转格式就毁了。
_ASS_CODECS = frozenset({"ass", "ssa"})
#: 蓝光位图轨：原样 copy 成 .sup（HDMV PGS 的标准封装，ffmpeg 的 sup muxer），
#: 交前端 libbitsub 在 canvas 上渲染——与 Jellyfin 10.9+ 的做法一致。
#: 绝不烧录（硬边界 1），也绝不 OCR（错字比没字幕更糟）。
_PGS_CODECS = frozenset({"hdmv_pgs_subtitle", "pgssub", "pgs"})


def cache_dir() -> Path:
    """抽取产物目录。中间品不进媒体库目录，跟随既有 data/ 相对路径惯例。"""
    return Path("data/cache/playback-subs")


def embedded_subtitle_format(codec: str | None) -> str | None:
    """内封轨 codec → 抽取后的文件格式；不支持的轨（VobSub 等）返回 None。"""
    normalized = (codec or "").lower()
    if normalized in _ASS_CODECS:
        return "ass"
    if normalized in _TEXT_CODECS:
        return "srt"
    if normalized in _PGS_CODECS:
        return "sup"
    return None


def embedded_track_codec(file: LibraryFile, index: int) -> str | None:
    """取第 index 条内封字幕轨的 codec；越界或未探测返回 None。

    数组下标与 ffmpeg 的 ``0:s:<k>`` 同源——都是「第 k 条字幕流」，因此可以
    直接用。绝不能换成绝对流序号，那个会被视频/音频/附件流搅乱。
    """
    streams = file.subtitle_streams or []
    if not 0 <= index < len(streams):
        return None
    raw = streams[index]
    return raw.get("codec") if isinstance(raw, dict) else None


def extract_embedded_subtitle(file: LibraryFile, index: int) -> SubtitleRef | None:
    """（阻塞，调用方须放线程池）抽出内封轨并返回可服务的定位。

    轨不存在、格式不支持、ffmpeg 缺失或抽取失败一律返回 None——取流端点对
    所有失败一视同仁按 404 应答，不给探测者区分的机会。
    """
    fmt = embedded_subtitle_format(embedded_track_codec(file, index))
    if fmt is None:
        return None
    video = Path(file.file_path)
    out_path = cache_dir() / f"{file.id}.s{index}.{fmt}"

    if _is_fresh(out_path, video):
        return SubtitleRef(path=out_path, format=fmt)
    if shutil.which("ffmpeg") is None:
        logger.warning(
            "系统中未找到 ffmpeg，无法抽取内封字幕轨——请安装 ffmpeg，"
            "或为该影片放置外挂字幕文件（官方 Docker 镜像已内置 ffmpeg）"
        )
        return None
    if not video.is_file():
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子替换：失败或超时可能留下非空残片，直接写最终路径的话
    # 下一次会被上面的新鲜度判断误认成成功产物。临时文件保留正式后缀，
    # ffmpeg 才能按扩展名选对 muxer。
    tmp_path = out_path.with_name(f".{out_path.stem}.{uuid.uuid4().hex}.part{out_path.suffix}")
    # ASS 用 copy 保住特效与排版；PGS 是位图流只能 copy（ffmpeg 没有 PGS
    # 编码器）；文本轨统一转 SRT，抹平 mov_text 之类的差异。
    codec_args = ["-c:s", "copy"] if fmt in ("ass", "sup") else ["-c:s", "srt"]
    extract_started_at = time.monotonic()
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-i", str(video),
                "-map", f"0:s:{index}",
                *codec_args,
                str(tmp_path),
            ],
            capture_output=True,
            timeout=_EXTRACT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _cleanup(tmp_path)
        logger.warning("内封字幕抽取超时（%.0f 秒）：%s 轨 %d", _EXTRACT_TIMEOUT, video, index)
        return None
    if proc.returncode != 0 or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        _cleanup(tmp_path)
        logger.warning(
            "内封字幕抽取失败：%s 轨 %d（%s）",
            video, index, proc.stderr.decode(errors="replace")[:200],
        )
        return None
    try:
        tmp_path.replace(out_path)
    except OSError as exc:
        _cleanup(tmp_path)
        logger.warning("内封字幕缓存写入失败：%s（%s）", out_path, exc)
        return None
    # 冷抽取要通读整个容器，是「点开字幕菜单卡半天」的头号来源；缓存命中
    # 走上面的 _is_fresh 直接返回不会到这里，所以这一行只记真实的抽取耗时
    logger.info(
        "内封字幕抽取完成：%s 轨 %d → %s 耗时 %.1f 秒",
        video.name, index, fmt, time.monotonic() - extract_started_at,
    )
    return SubtitleRef(path=out_path, format=fmt)


def _is_fresh(out_path: Path, video: Path) -> bool:
    """产物比视频新且非空即可复用。

    抽取要通读整个容器，不能每次点开字幕都重来一遍；而只有视频本体变了
    （洗版、改名归并）才需要重抽。
    """
    try:
        return (
            out_path.is_file()
            and out_path.stat().st_size > 0
            and out_path.stat().st_mtime_ns > video.stat().st_mtime_ns
        )
    except OSError:
        return False  # stat 失败按未缓存处理，走正常抽取


def _cleanup(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


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
