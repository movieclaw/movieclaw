"""本地来源条目的主图与背景图（docs/design/library-other-kind.md 4.7）。
本地条目没有图床可拉，图只能从文件旁边或文件本身来。

**主图**（Primary）取图顺序：
1. **海报 sidecar**：``<主干>.jpg`` / ``<主干>-poster.jpg`` /（目录归它时）``poster.jpg``
   ——用户或刮削工具已经放好的竖版海报，尊重之；规则与 Web / Jellyfin 图片接口
   同源（artwork.py），选出来的一定是同一张；
2. **横版 sidecar**：``<主干>-thumb.jpg`` / ``-landscape.jpg``（Jellyfin 语义里这是
   Thumb 不是 Primary，我们只有一个卡片图位，没海报时拿它当主图）；
3. **内嵌封面**：容器里 ``attached_pic`` 视频流（mp4/mkv 的 cover art）；
4. **抓帧**：跳到时长 10% 处（片头黑场之后），在 24 个关键帧里挑一帧
   "最有代表性"的（ffmpeg ``thumbnail`` 滤镜），逐行化、HDR 色调映射后
   缩到宽 ≤1280。

**背景图**（Backdrop，宽 ≤1920）：``<主干>-fanart.jpg`` /（目录归它时）``fanart.jpg``，有则入账、
没有就没有——播放器的 BackdropImageTags 靠它，不入账 Infuse 详情页就是黑底。

产物统一写到资产目录 ``data/metadata/images/{id}/poster.jpg`` 与 ``backdrop.jpg``
（与 TMDB 资产同一路径约定，``/images/assets`` 路由、海报墙、Jellyfin 图片接口
零改动），并把主图像素宽高记进 ``media_metadata.poster_width/height``——卡片按
真实比例排版，而不是把 16:9 的抓帧硬塞进 2:3 的海报框。
按库开关 ``generate_thumbnails``：网络挂载的大库抓帧就是全量下载，用户可以关。
失败只记日志、字段保持 NULL（前端出占位图），下次刷新自愈。
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

from sqlmodel import select

from movieclaw_api.services.library.artwork import find_artwork
from movieclaw_db.engine import get_database
from movieclaw_db.models import Library, LibraryFile, MediaItem, MediaMetadata, MediaSource, utcnow
from movieclaw_db.repositories.media_repo import MediaItemRepository

logger = logging.getLogger("movieclaw_api.library.thumbs")

_MAX_WIDTH = 1280  # 主图（海报/缩略图）
_MAX_BACKDROP_WIDTH = 1920  # 背景图：电视端全屏铺底，1280 会糊
_JPEG_QUALITY = "3"  # ffmpeg -q:v，2~5 是"肉眼无损"区间
_FFMPEG_TIMEOUT = 90  # 秒；网络挂载上抓帧要读几十 MB，给足余量

# HDR 抓帧的色调映射链（HDR10/HLG/DV 基础层 → BT.709 SDR）。章节场景图
# （library/chapters.py）复用同一条链，两处出的图观感一致
TONEMAP_FILTERS: tuple[str, ...] = (
    "zscale=t=linear:npl=100",
    "format=gbrpf32le",
    "zscale=p=bt709",
    "tonemap=hable",
    "zscale=t=bt709:m=bt709:r=tv",
)

# 抓帧闸：主图与章节场景图共用，扫描期间最多两路 ffmpeg 同时解码，
# 不把 CPU 打满、也不让网络挂载被并发读打散
FRAME_GRAB_GATE = asyncio.Semaphore(2)


async def ensure_local_assets(media_item_id: int, *, force: bool = False) -> None:
    """给本地来源条目补主图与背景图（缺失才做，``force`` 重做）。TMDB 条目直接返回。"""
    db = get_database()
    async with db.session() as session:
        item = await session.get(MediaItem, media_item_id)
        if item is None or item.source != MediaSource.LOCAL:
            return
        meta = await MediaItemRepository(session).get_metadata(media_item_id)
        if meta is None:
            meta = MediaMetadata(media_item_id=media_item_id)
            session.add(meta)
        rows = (
            await session.execute(
                select(LibraryFile, Library)
                .join(Library, Library.id == LibraryFile.library_id)  # type: ignore[arg-type]
                .where(LibraryFile.media_item_id == media_item_id, LibraryFile.in_place())
                .order_by(LibraryFile.file_path)
            )
        ).all()
        sources = [f for f, lib in rows if lib.generate_thumbnails]
        if not sources:
            return
        file = sources[0]  # 其他库一文件一条目；影视库的临时条目取路径最靠前的文件
        from movieclaw_api.services.media_scrape import assets_root

        video = Path(file.file_path)
        item_dir = assets_root() / str(media_item_id)
        poster_dest, backdrop_dest = item_dir / "poster.jpg", item_dir / "backdrop.jpg"
        poster_rel = str(poster_dest.relative_to(assets_root()))
        backdrop_rel = str(backdrop_dest.relative_to(assets_root()))
        changed = False

        poster_ready = meta.poster_file == poster_rel and await asyncio.to_thread(
            poster_dest.is_file
        )
        if force or not poster_ready:
            async with FRAME_GRAB_GATE:
                size = await asyncio.to_thread(
                    build_thumbnail,
                    video,
                    poster_dest,
                    duration_seconds=file.duration_seconds,
                    is_disc=file.container in ("bluray", "dvd"),
                    hdr=file.hdr,
                )
            if size is not None:
                meta.poster_file = poster_rel
                meta.poster_width, meta.poster_height = size
                changed = True

        backdrop_ready = meta.backdrop_file == backdrop_rel and await asyncio.to_thread(
            backdrop_dest.is_file
        )
        if force or not backdrop_ready:
            if await asyncio.to_thread(build_backdrop, video, backdrop_dest):
                meta.backdrop_file = backdrop_rel
                changed = True
            elif force and meta.backdrop_file == backdrop_rel:
                meta.backdrop_file = None  # fanart 被用户删了：重做时一并撤掉
                changed = True

        if changed:
            meta.updated_at = utcnow()
            session.add(meta)
            await session.commit()


def build_thumbnail(
    video: Path,
    dest: Path,
    *,
    duration_seconds: int | None,
    is_disc: bool = False,
    hdr: str | None = None,
) -> tuple[int, int] | None:
    """同步版：按海报 sidecar → 横版 sidecar → 内嵌封面 → 抓帧的顺序产出 ``dest``，
    返回 (宽, 高)。

    失败返回 None（ffmpeg 缺失/超时/文件损坏都算），不抛异常。
    """
    if is_disc:
        return None  # 原盘目录：没有单一视频文件可抓，留占位图
    try:
        for kind in ("poster", "thumb"):
            candidate = find_artwork(video.parent, kind, [video])
            if candidate is not None and _transcode_image(candidate, dest):
                return _image_size(dest)
        cover_index = _attached_pic_index(video)
        if cover_index is not None and _extract_stream(video, cover_index, dest):
            return _image_size(dest)
        if _grab_frame(video, dest, duration_seconds, hdr):
            return _image_size(dest)
    except FileNotFoundError:
        logger.warning("系统中未找到 ffmpeg，本地视频缩略图已跳过：%s", video)
    except subprocess.TimeoutExpired:
        logger.warning("生成缩略图超时（%s 秒）：%s", _FFMPEG_TIMEOUT, video)
    except OSError as exc:
        logger.warning("生成缩略图失败：%s（%s）", video, exc)
    return None


def build_backdrop(video: Path, dest: Path) -> bool:
    """同步版：把 fanart sidecar 转成 ``dest``（宽 ≤1920）；没有 fanart 或失败返回 False。"""
    candidate = find_artwork(video.parent, "fanart", [video])
    if candidate is None:
        return False
    try:
        return _transcode_image(candidate, dest, max_width=_MAX_BACKDROP_WIDTH)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("生成背景图失败：%s（%s）", video, exc)
        return False


def _run(cmd: list[str]) -> bool:
    proc = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT)
    if proc.returncode != 0:
        logger.debug("ffmpeg 失败：%s", proc.stderr.decode(errors="replace")[-300:])
    return proc.returncode == 0


def _scale_filter(max_width: int = _MAX_WIDTH) -> str:
    # 宽超过上限才缩，-2 保证高是偶数（yuv420p 的要求）
    return f"scale='min({max_width},iw)':-2"


def _output_args(dest: Path) -> list[str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    return ["-frames:v", "1", "-q:v", _JPEG_QUALITY, "-y", str(dest)]


def _transcode_image(src: Path, dest: Path, *, max_width: int = _MAX_WIDTH) -> bool:
    return _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(src),
            "-vf",
            _scale_filter(max_width),
            *_output_args(dest),
        ]
    )


def _attached_pic_index(video: Path) -> int | None:
    """容器内嵌封面（disposition.attached_pic=1）的流序号；没有返回 None。"""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "stream=index,codec_type:stream_disposition=attached_pic",
            str(video),
        ],
        capture_output=True,
        timeout=_FFMPEG_TIMEOUT,
    )
    if proc.returncode != 0:
        return None
    try:
        streams = json.loads(proc.stdout).get("streams") or []
    except json.JSONDecodeError:
        return None
    for stream in streams:
        if stream.get("codec_type") == "video" and (stream.get("disposition") or {}).get(
            "attached_pic"
        ):
            return int(stream["index"])
    return None


def _extract_stream(video: Path, index: int, dest: Path) -> bool:
    return _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-map",
            f"0:{index}",
            "-vf",
            _scale_filter(),
            *_output_args(dest),
        ]
    )


def _grab_frame(video: Path, dest: Path, duration_seconds: int | None, hdr: str | None) -> bool:
    """抓帧：跳到 10% 处，在 24 帧里选代表帧。

    三级回退：① 只解关键帧（大文件最快，Jellyfin 同款）；② 关键帧稀疏
    （短片/单 GOP 编码，10% 处之后没有关键帧，ffmpeg 会零帧退出且不报错）
    时全解码；③ 仍拿不到（时长未知的超短片）从头再来。HDR 先尝试色调映射，
    滤镜不可用（ffmpeg 没编 zimg）时退回不映射。
    """
    position = max(1.0, duration_seconds * 0.1) if duration_seconds else 10.0
    base = ["bwdif=mode=send_frame:deint=interlaced", "thumbnail=n=24", _scale_filter()]
    chains = [base]
    if hdr:  # 台账探测出的 HDR 格式（HDR10/HLG/DV…），SDR 为 NULL
        chains.insert(0, base[:2] + list(TONEMAP_FILTERS) + base[2:])
    attempts = [(position, True), (position, False), (0.0, False)]
    for start, keyframes_only in attempts:
        for chain in chains:
            cmd = ["ffmpeg", "-v", "error"]
            if keyframes_only:
                cmd += ["-skip_frame", "nokey"]
            cmd += [
                "-ss",
                f"{start:.3f}",
                "-i",
                str(video),
                "-an",
                "-sn",
                "-vf",
                ",".join([*chain, "format=yuv420p"]),
                *_output_args(dest),
            ]
            if _run(cmd) and dest.is_file():
                return True
    return False


def _image_size(path: Path) -> tuple[int, int] | None:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "stream=width,height",
            str(path),
        ],
        capture_output=True,
        timeout=_FFMPEG_TIMEOUT,
    )
    if proc.returncode != 0:
        return None
    try:
        streams = json.loads(proc.stdout).get("streams") or []
    except json.JSONDecodeError:
        return None
    for stream in streams:
        if stream.get("width") and stream.get("height"):
            return int(stream["width"]), int(stream["height"])
    return None


def primary_aspect(item: MediaItem, width: int | None, height: int | None) -> float:
    """卡片主图宽高比：有真实像素尺寸按尺寸，否则按来源的惯例
    （TMDB 海报 2:3，本地抓帧 16:9）。前端只读这个值，不猜。"""
    if width and height:
        return round(width / height, 4)
    return round(2 / 3, 4) if item.source == MediaSource.TMDB else round(16 / 9, 4)
