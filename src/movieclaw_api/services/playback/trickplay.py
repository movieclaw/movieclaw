"""进度条缩略图（Trickplay，docs/design/web-player.md §6.5）。

拖进度条时能看见画面，是「这个播放器不便宜」的第一印象来源——Plex / Emby /
Jellyfin 都有。实现上就是把整片按固定间隔抽帧拼成雪碧图，外加一份「第几秒
对应哪一格」的索引。

三条实现取舍
------------

**用 ``-skip_frame nokey``**。只解关键帧，比逐帧解码快一两个数量级（实测
120 秒的片子 0.3 秒出图）。缩略图不需要精确到帧，落在最近的关键帧上完全够用。

**索引给 JSON 而不是 WebVTT**。设计文档原写的是 VTT，实现时改了：VTT 里的
雪碧图路径是相对的，而我们的取流地址带签名 token——相对路径解析会把 query
丢掉，浏览器请求雪碧图时没有凭据。让前端拿 JSON 自己算格子偏移只多四行数学，
却省掉一整套「服务端按 token 重写 VTT」的机制。VTT 的价值在于跨播放器互操作，
而我们做的是自己的播放器。

**后台生成，不挡首帧**。生成要通读整个容器，放进开会话会让首帧白等。会话
起来时顺手在后台开工，前端轮询；没生成好就是没有预览，不影响播放。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from movieclaw_db.models import LibraryFile

logger = logging.getLogger("movieclaw_api.playback.trickplay")

#: 抽帧间隔（秒）。10 秒是通行取值：再密雪碧图体积上去了，再疏拖动时跳变明显。
INTERVAL_S = 10
#: 每张雪碧图的格子数。100 格一张，一部两小时的片约 8 张。
COLUMNS = 10
ROWS = 10
TILES_PER_SHEET = COLUMNS * ROWS
#: 单格宽度。320 足够在进度条上方看清场景，又不至于让雪碧图过大。
THUMB_WIDTH = 320

#: 生成要通读整个容器；给足余量，超时就放弃（没有预览不影响播放）。
#: 生成上限。加了 -readrate 4 之后生成时长 ≈ 片长 / 4（两小时片约 30 分钟），
#: 上限要按最长的片（4 小时）留足；超时残片会被清掉、下次重来。
_GENERATE_TIMEOUT = 7200.0

_SHEET_PATTERN = "sprite_%03d.jpg"
_INDEX_NAME = "index.json"

#: 正在生成的文件 id，避免同一部片被并发触发多次生成。
_in_flight: set[int] = set()


@dataclass(frozen=True)
class TrickplayIndex:
    """一部片的缩略图索引。前端据此算「第 t 秒在哪张图的哪一格」。"""

    interval_ms: int
    tile_width: int
    tile_height: int
    columns: int
    rows: int
    #: 雪碧图文件名（不含路径与 token），按顺序
    sheets: list[str]
    #: 有效缩略图张数——最后一张雪碧图通常没填满，超出的格子是黑的
    count: int


def trickplay_dir(file_id: int) -> Path:
    return Path("data/cache/playback-trickplay") / str(file_id)


def load_index(file_id: int) -> TrickplayIndex | None:
    """读已生成的索引；没有就返回 None（前端表现为「暂无预览」）。"""
    path = trickplay_dir(file_id) / _INDEX_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return TrickplayIndex(**raw)
    except TypeError:
        return None


def build_index(
    *, duration_seconds: int, sheet_width: int, sheet_height: int, sheets: list[str]
) -> TrickplayIndex:
    """由片长与雪碧图尺寸算出索引——**纯函数，可单测**。

    格子尺寸从雪碧图整体尺寸反推而不是照抄 ``THUMB_WIDTH``：``scale=320:-2``
    的高度取决于源片宽高比，而且会向偶数对齐，算出来的和实际差一两个像素就会
    让预览图错位一条边。
    """
    count = max(1, math.ceil(duration_seconds / INTERVAL_S))
    # 最后一张雪碧图没填满，但可用格子数不会超过总容量
    count = min(count, len(sheets) * TILES_PER_SHEET)
    return TrickplayIndex(
        interval_ms=INTERVAL_S * 1000,
        tile_width=sheet_width // COLUMNS,
        tile_height=sheet_height // ROWS,
        columns=COLUMNS,
        rows=ROWS,
        sheets=sheets,
        count=count,
    )


def generate(file: LibraryFile) -> TrickplayIndex | None:
    """（阻塞，调用方须放线程池）抽帧生成雪碧图与索引。

    整个目录一次做完再原子换上：半套雪碧图配一份完整索引，会让后半部片的
    预览指向不存在的图。
    """
    file_id = file.id or 0
    existing = load_index(file_id)
    if existing is not None:
        return existing
    if shutil.which("ffmpeg") is None or not file.duration_seconds:
        return None
    video = Path(file.file_path)
    if not video.is_file():
        return None

    out_dir = trickplay_dir(file_id)
    staging = out_dir.with_name(f".{out_dir.name}.{uuid.uuid4().hex}.part")
    staging.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                # 只解关键帧：缩略图不需要精确到帧，快一两个数量级
                "-skip_frame", "nokey",
                # 读入限速（4 倍实时）：skip_frame 只省解码不省 IO，不限速的话
                # demuxer 会以磁盘/网络的极限速度通读整个容器，跟同一块盘上的
                # 首片转码/直通取流抢 IO——「首次起播卡缓冲、重进就好」的头号
                # 元凶（§6.10）。4 倍 = 24Mbps 的片约 12MB/s，千兆网口一成，
                # 两小时片约 30 分钟出图——缩略图晚点完全可以接受。
                "-readrate", "4",
                "-i", str(video),
                "-an", "-sn", "-dn",
                "-vf", f"fps=1/{INTERVAL_S},scale={THUMB_WIDTH}:-2,tile={COLUMNS}x{ROWS}",
                "-qscale:v", "5",
                str(staging / _SHEET_PATTERN),
            ],
            capture_output=True,
            timeout=_GENERATE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(staging, ignore_errors=True)
        logger.warning("缩略图生成超时（%.0f 秒）：%s", _GENERATE_TIMEOUT, video)
        return None

    sheets = sorted(p.name for p in staging.glob("sprite_*.jpg") if p.stat().st_size > 0)
    if not sheets:
        shutil.rmtree(staging, ignore_errors=True)
        logger.warning(
            "缩略图生成没有产物：%s（%s）",
            video, proc.stderr.decode(errors="replace")[:200],
        )
        return None

    size = _probe_size(staging / sheets[0])
    if size is None:
        shutil.rmtree(staging, ignore_errors=True)
        return None
    index = build_index(
        duration_seconds=file.duration_seconds,
        sheet_width=size[0],
        sheet_height=size[1],
        sheets=sheets,
    )
    (staging / _INDEX_NAME).write_text(
        json.dumps(vars(index), ensure_ascii=False), encoding="utf-8"
    )
    try:
        staging.replace(out_dir)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return None
    return index


def schedule(file: LibraryFile, *, delay_s: float = 0) -> None:
    """后台生成，不阻塞调用方。同一部片并发触发只做一次。

    生成要通读整个容器，放进开会话的同步路径会让首帧白等；而缩略图晚几十秒
    出现完全可以接受——没有预览不影响播放。

    ``delay_s``：开会话时传 90 秒——即便有 -readrate 限速，起播头一分钟正是
    转码抢首片、播放器攒缓冲的关键窗口，缩略图这种最不急的活让开它。延迟
    期间用户退出播放也照样生成（低速后台任务，下次进来直接有预览）。
    """
    file_id = file.id or 0
    if file_id in _in_flight or load_index(file_id) is not None:
        return
    _in_flight.add(file_id)

    async def run() -> None:
        try:
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            await asyncio.to_thread(generate, file)
        except Exception:  # noqa: BLE001 — 缩略图失败绝不能影响播放
            logger.warning("缩略图生成失败：file_id=%s", file_id, exc_info=True)
        finally:
            _in_flight.discard(file_id)

    with contextlib.suppress(RuntimeError):  # 没有事件循环时（同步上下文）跳过
        asyncio.create_task(run())


def _probe_size(sheet: Path) -> tuple[int, int] | None:
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0",
                str(sheet),
            ],
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        width, height = (int(v) for v in proc.stdout.decode().strip().split(",")[:2])
    except ValueError:
        return None
    return width, height
