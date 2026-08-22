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
import logging
import shutil
import subprocess
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


def cache_dir() -> Path:
    """抽取产物目录。中间品不进媒体库目录，跟随既有 data/ 相对路径惯例。"""
    return Path("data/cache/playback-subs")


def embedded_subtitle_format(codec: str | None) -> str | None:
    """内封轨 codec → 抽取后的文件格式；不支持的轨（PGS/VobSub）返回 None。"""
    normalized = (codec or "").lower()
    if normalized in _ASS_CODECS:
        return "ass"
    if normalized in _TEXT_CODECS:
        return "srt"
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
    # ASS 用 copy 保住特效与排版；文本轨统一转 SRT，抹平 mov_text 之类的差异。
    codec_args = ["-c:s", "copy"] if fmt == "ass" else ["-c:s", "srt"]
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
