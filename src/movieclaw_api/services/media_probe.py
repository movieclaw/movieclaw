"""ffprobe 介质探测：读文件本体的真实规格（媒体库 L2/L3 共用）。

设计要点（docs/design/library.md 风险①）：
- 依赖系统 ffprobe（随 ffmpeg 安装，**官方 Docker 镜像已内置**）；源码/裸机
  部署缺失时**降级为跳过探测**，规格列保持 NULL，不阻断入库/扫描——只在
  首次发现缺失时告警一次；
- 探测是同步子进程调用，调用方须放线程池（asyncio.to_thread）执行；
- 库存画质的真相来自文件本体，不来自种子名（种子名会说谎，文件不会）。

补探（重要）：ffprobe 是后装的时候，**扫描不会自动回头补**——已识别且在位
的台账行整体秒过，压根走不到探测那一步。补探由扫描的独立阶段负责
（library_scan 的 PROBING 阶段）与详情页打开时的后台任务兜底。
"""

from __future__ import annotations

import functools
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("movieclaw_api.media_probe")

# ffprobe 缺失只告警一次（每次探测都刷屏毫无意义）
_missing_warned = False

# 单文件探测超时：本地文件读元数据通常毫秒级，超时说明文件/存储有问题
_PROBE_TIMEOUT = 30.0


@functools.cache
def ffprobe_available() -> bool:
    """系统里有没有 ffprobe。缓存结果——PATH 在进程生命周期内不会变，
    而调用方（入库门禁、扫描的补探阶段）会逐文件问，每次都 which 太浪费。"""
    return shutil.which("ffprobe") is not None


@dataclass(frozen=True)
class MediaSpec:
    """一次探测的结论。字段 None = 该项未能取得（三态铁律）。

    音频/字幕流是**列表字段**：空列表 = 探测成功但确实没有该类流
    （与 None=没探测到有区别），元素结构见 ``_audio_stream_info`` /
    ``_subtitle_stream_info``——直接以 dict 形态落 JSON 列，前端负责展示格式化。
    """

    resolution: str | None
    video_codec: str | None
    hdr: str | None
    bit_depth: int | None
    duration_seconds: int | None
    bit_rate: int | None
    frame_rate: float | None = None
    color_space: str | None = None
    audio_streams: list[dict] = field(default_factory=list)
    subtitle_streams: list[dict] = field(default_factory=list)


def probe_media(path: str | Path) -> MediaSpec | None:
    """探测单个视频文件；ffprobe 缺失或探测失败返回 None（调用方规格置 NULL）。"""
    global _missing_warned
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
    except FileNotFoundError:
        if not _missing_warned:
            _missing_warned = True
            logger.warning(
                "系统中未找到 ffprobe（随 ffmpeg 安装），介质规格探测已跳过——"
                "入库仍正常进行，规格列将为空。安装 ffmpeg 后新入库的文件会带规格。"
            )
        return None
    except subprocess.TimeoutExpired:
        logger.warning("ffprobe 探测超时（%s 秒）：%s", _PROBE_TIMEOUT, path)
        return None
    if proc.returncode != 0:
        logger.warning(
            "ffprobe 探测失败：%s（%s）", path, proc.stderr.decode(errors="replace")[:200]
        )
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return _parse_probe(payload, include_mpegts_pids=Path(path).suffix.lower() == ".m2ts")


# --- 探测失败记忆（媒体库入库/补探/点名重探共用）---------------------------
#
# probe_media 失败分两类：瞬时环境故障（网络挂载抖动、文件还在写入）与
# 确定性坏文件（损坏/截断，每探一次就是 30 秒超时）。台账侧只有
# ``audio_streams IS NULL`` 一个「从未探测成功」标记，区分不了两者——没有
# 这张表，坏文件每轮手动扫描都要全额付超时，瞬时失败又只能等用户手动扫描
# 才有第二次机会（无 watch 的网络挂载库尤甚）。
#
# 进程内存表，不落库（同监听导入 ingest.py 的 _failed_retry 先例）：重启即
# 清空，代价只是重启后的首轮补探把失败行都再试一遍——这本来就是期望行为，
# 不值得为它做一次数据库迁移。键统一用**台账行路径**（原盘条目是目录而非
# 其内的 m2ts），三处调用方（入库、补探、秒过行点名重探）以它查/记。

_RETRY_BASE_SECONDS = 3600.0  # 首次失败后 1 小时可重试（对齐监听导入的小时级退避）
_RETRY_MAX_SECONDS = 24 * 3600.0  # 连续失败翻倍退避，封顶一天
_RETRY_STATE_MAX = 4096

# 台账路径 -> (连续失败次数, 最近失败时刻 time.monotonic())
_retry_state: dict[str, tuple[int, float]] = {}


def note_probe_failure(path: str | Path) -> None:
    """记一次探测失败；连续失败的重试间隔翻倍增长。

    ffprobe 根本不在系统里时**不记**：那不是这个文件的问题，装好 ffmpeg 后
    的首轮补探不该背着一张全库退避表起步。
    """
    if not ffprobe_available():
        return
    key = str(path)
    if len(_retry_state) >= _RETRY_STATE_MAX and key not in _retry_state:
        # 先清掉已到重试点的旧条目挪位置；仍然满（几千个坏文件，几乎不
        # 可能）就整体放弃——重试提早无害，表无限膨胀才是问题
        now = time.monotonic()
        for stale in [p for p, s in _retry_state.items() if _retry_due(s, now)]:
            _retry_state.pop(stale, None)
        if len(_retry_state) >= _RETRY_STATE_MAX:
            _retry_state.clear()
    failures = _retry_state.get(key, (0, 0.0))[0] + 1
    _retry_state[key] = (failures, time.monotonic())


def note_probe_success(path: str | Path) -> None:
    """探测成功即抹掉失败记忆，之后再失败从最短退避重新起算。"""
    _retry_state.pop(str(path), None)


def probe_retry_due(path: str | Path) -> bool:
    """该路径现在值不值得再探：从没失败过，或退避已到点。"""
    state = _retry_state.get(str(path))
    return state is None or _retry_due(state, time.monotonic())


def probe_retry_paths() -> set[str]:
    """退避到点的失败路径集合（定期对账用它点名，重探规格陈旧的秒过行）。"""
    now = time.monotonic()
    return {p for p, s in _retry_state.items() if _retry_due(s, now)}


def _retry_due(state: tuple[int, float], now: float) -> bool:
    failures, last = state
    delay = min(_RETRY_BASE_SECONDS * (2 ** (failures - 1)), _RETRY_MAX_SECONDS)
    return now - last >= delay


# --- 关键帧密度探测（docs/design/web-player.md §3.5 / §7-②）-----------------
#
# 为什么需要它：remux 直通（档 1/2）是 ``-c:v copy``，分片切点只能落在源片
# 已有的 IDR 上。PT 压制组的关键帧间隔常常是 5~10 秒甚至不规则，硬切非关键帧
# 会花屏黑屏；而 GOP 长到十几秒时，remux 的首帧优势也就没了，不如直接转码。
#
# 为什么是采样而不是全片索引：判定「稀不稀疏」只要一个平均间隔，采样三段
# 即可，几百毫秒；全片关键帧索引对两小时的片有几千个时间点，扫一遍很慢，
# 那是转码会话启动时才需要的东西（届时用户已在等待，且可以边转边给）。
#
# 为什么不落库：存量库无论如何都要懒加载补齐，落库的额外收益只是「重启后
# 不用重算」，不值一次数据库迁移。先内存缓存，实测不够再说。

_KEYFRAME_PROBE_TIMEOUT = 60.0
# 每段采样窗口时长（秒）。取 30 秒：足够覆盖 2~3 个正常 GOP，又不至于读太多包。
_KEYFRAME_WINDOW_S = 30
# 采样位置（占全片比例）。避开片头片尾的特殊编码段，取片中三处。
_KEYFRAME_SAMPLE_POSITIONS = (0.10, 0.50, 0.90)
# 短于这个时长就整片扫，不采样（省得三段窗口互相重叠反而更慢）。
_KEYFRAME_FULL_SCAN_MAX_S = _KEYFRAME_WINDOW_S * len(_KEYFRAME_SAMPLE_POSITIONS)


def probe_keyframe_interval(
    path: str | Path, duration_seconds: int | None
) -> float | None:
    """采样估算视频轨的关键帧平均间隔（秒）。

    返回 None = 无法判定（ffprobe 缺失/超时/无关键帧信息）。决策引擎见到
    None 会保守地不走 remux——索引未知就赌不起。

    结果按 (路径, mtime, 大小) 缓存：同一文件重复播放、切集来回都不重算；
    文件被换掉（改名归并、洗版）时三元组变化，缓存自然失效。

    **只缓存成功结果**：ffprobe 在繁忙存储上偶发超时会返回 None，若把 None
    也缓存住，一次瞬时故障就会让这个文件在整个进程生命周期里被永久判成
    「关键帧未知 → 只能转码」。宁可下次再探一遍。
    """
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _keyframe_cache.get(key)
    if cached is not None:
        return cached
    value = _probe_keyframe_interval(str(path), duration_seconds)
    if value is not None:
        if len(_keyframe_cache) >= _KEYFRAME_CACHE_MAX:
            _keyframe_cache.clear()
        _keyframe_cache[key] = value
    return value


# (路径, mtime_ns, 大小) -> 平均间隔秒。满了整体清空——这是纯加速缓存，
# 命中率短暂下降无所谓，不值得为它引入 LRU 的复杂度。
_keyframe_cache: dict[tuple[str, int, int], float] = {}
_KEYFRAME_CACHE_MAX = 1024


def _probe_keyframe_interval(path: str, duration_seconds: int | None) -> float | None:
    windows = _keyframe_windows(duration_seconds)
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,flags",
    ]
    if windows:
        cmd += ["-read_intervals", ",".join(f"{s}%+{_KEYFRAME_WINDOW_S}" for s, _ in windows)]
    cmd.append(path)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_KEYFRAME_PROBE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # ffprobe 缺失已由 probe_media 告警过一次，这里不重复刷屏
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return keyframe_interval_from_packets(payload.get("packets") or [], windows)


def probe_keyframe_before(path: str | Path, position_s: float) -> float | None:
    """找 ≤ ``position_s`` 的最后一个视频关键帧时间（秒）。

    为什么需要它（续播漂移的根因，§7-②）：直通档 ``-c:v copy`` 不解码，
    ``-ss`` 只能落在关键帧上——ffmpeg 会静默回退到目标点**之前**的关键帧
    起播。会话的 start_ms 若仍按请求值返回，前端整条时间轴（进度、字幕、
    进度上报）就系统性快了 0~一个 GOP；上报的进度带着这个偏差存库，下次
    续播再回退一次，表现为「每次刷新进来位置都不一样」。开会话前用它把
    start_ms 校正成 ffmpeg 真实会用的起点，偏差归零。

    返回 None = 窗口内没找到（GOP 超长/探测失败），调用方保持原值——
    此时决策引擎多半也不会选 remux，不值得为罕见失败加重试。
    """
    window_start = max(0.0, position_s - _KEYFRAME_WINDOW_S)
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,flags",
        "-read_intervals",
        f"{window_start}%{position_s + 0.001}",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_KEYFRAME_PROBE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return last_keyframe_at_or_before(payload.get("packets") or [], position_s)


def last_keyframe_at_or_before(packets: list[dict], position_s: float) -> float | None:
    """从 ffprobe 的 packet 列表里挑 ≤ position_s 的最后一个关键帧——纯函数，可单测。"""
    best: float | None = None
    for packet in packets:
        if "K" not in (packet.get("flags") or ""):
            continue
        raw = packet.get("pts_time")
        try:
            pts = float(raw)
        except (TypeError, ValueError):
            continue
        if pts <= position_s + 0.001 and (best is None or pts > best):
            best = pts
    return best


def _keyframe_windows(duration_seconds: int | None) -> list[tuple[int, int]]:
    """采样窗口 [(起点秒, 终点秒)]；短片或时长未知返回空列表表示整片扫。"""
    if not duration_seconds or duration_seconds <= _KEYFRAME_FULL_SCAN_MAX_S:
        return []
    return [
        (start, start + _KEYFRAME_WINDOW_S)
        for start in (int(duration_seconds * p) for p in _KEYFRAME_SAMPLE_POSITIONS)
    ]


def keyframe_interval_from_packets(
    packets: list[dict], windows: list[tuple[int, int]]
) -> float | None:
    """从 ffprobe 的 packet 列表估算关键帧间隔——**纯函数，可单测**。

    取窗口内相邻关键帧时间差的中位数：中位数而非平均，是为了不被片尾静态
    画面那种异常稀疏的一段带偏；**间隔不跨窗口计算**，否则两段采样之间那个
    巨大的空档会被当成一个超长 GOP。

    只有一个关键帧（或一个都没有）的窗口无法给出间隔，退化为「窗口时长 ÷
    关键帧数」的保守估计——这会得到一个偏大的值，正好让决策引擎不走 remux。
    """
    times = sorted(
        float(p["pts_time"])
        for p in packets
        if isinstance(p, dict)
        and str(p.get("flags") or "").startswith("K")
        and _is_number(p.get("pts_time"))
    )
    if not times:
        return None
    if not windows:
        windows = [(int(times[0]), int(times[-1]) + 1)]

    deltas: list[float] = []
    fallbacks: list[float] = []
    for start, end in windows:
        inside = [t for t in times if start <= t <= end]
        if len(inside) >= 2:
            deltas.extend(b - a for a, b in zip(inside, inside[1:], strict=False))
        else:
            span = max(end - start, 1)
            fallbacks.append(float(span) / max(len(inside), 1))
    if deltas:
        return _median(deltas)
    return _median(fallbacks) if fallbacks else None


def _is_number(value) -> bool:  # noqa: ANN001
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _parse_probe(payload: dict, *, include_mpegts_pids: bool = False) -> MediaSpec:
    video = next(
        (s for s in payload.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    fmt = payload.get("format", {})

    resolution = None
    codec = None
    hdr = None
    bit_depth = None
    if video is not None:
        codec = video.get("codec_name")
        resolution = _resolution_label(video.get("width"), video.get("height"))
        hdr = _hdr_label(video)
        bit_depth = _bit_depth(video)

    streams = payload.get("streams", [])
    return MediaSpec(
        resolution=resolution,
        video_codec=codec,
        hdr=hdr,
        bit_depth=bit_depth,
        duration_seconds=_to_int(fmt.get("duration")),
        bit_rate=_to_int(fmt.get("bit_rate")),
        frame_rate=(
            _frame_rate(video.get("avg_frame_rate"))
            or _frame_rate(video.get("r_frame_rate"))
            if video
            else None
        ),
        color_space=_color_space_label(video) if video else None,
        audio_streams=[
            _audio_stream_info(s, include_pid=include_mpegts_pids)
            for s in streams
            if s.get("codec_type") == "audio"
        ],
        subtitle_streams=[
            _subtitle_stream_info(s, include_pid=include_mpegts_pids)
            for s in streams
            if s.get("codec_type") == "subtitle"
        ],
    )


def _audio_stream_info(stream: dict, *, include_pid: bool = False) -> dict:
    """音轨的展示要素。``profile`` 比 codec 更接近用户认知（如 DTS-HD MA、
    Dolby TrueHD + Atmos 探不出 Atmos 层，先给基础格式），缺失时前端退回 codec。"""
    tags = stream.get("tags") or {}
    result = {
        "codec": stream.get("codec_name"),
        "profile": stream.get("profile"),
        "channels": _to_int(stream.get("channels")),
        "channel_layout": stream.get("channel_layout"),
        "language": tags.get("language"),
        "title": tags.get("title"),
        "default": bool((stream.get("disposition") or {}).get("default")),
    }
    pid = _stream_pid(stream.get("id")) if include_pid else None
    if pid is not None:
        # MPEG-TS/BDMV 用 PID 与 CLPI 关联。详情 API 显式投影公开字段，内部键
        # 只落 JSON 台账，不改变前端契约。
        result["pid"] = pid
    return result


def _subtitle_stream_info(stream: dict, *, include_pid: bool = False) -> dict:
    """内封字幕轨的展示要素（外挂字幕文件由媒体库详情层另行发现）。"""
    tags = stream.get("tags") or {}
    disposition = stream.get("disposition") or {}
    result = {
        "codec": stream.get("codec_name"),
        "language": tags.get("language"),
        "title": tags.get("title"),
        "forced": bool(disposition.get("forced")),
        "default": bool(disposition.get("default")),
    }
    pid = _stream_pid(stream.get("id")) if include_pid else None
    if pid is not None:
        result["pid"] = pid
    return result


def _stream_pid(value) -> int | None:
    """ffprobe 的 MPEG-TS stream.id 通常是 ``0x1200``，也兼容整数/十进制。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    try:
        stripped = value.strip().lower()
        parsed = int(stripped, 16 if stripped.startswith("0x") else 10)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _resolution_label(width: int | None, height: int | None) -> str | None:
    """宽高 → 行业惯用分辨率标签。以宽度为主判据（电影常有非 16:9 裁切，
    2.39:1 的 4K 片高度只有 ~1600，按高度判会误降档）。"""
    if not width and not height:
        return None
    w = width or 0
    h = height or 0
    if w >= 3200 or h >= 1900:
        return "2160p"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    if h:
        return f"{h}p"
    return None


def _hdr_label(video: dict) -> str | None:
    """识别用户真正关心的 HDR 格式；SDR 返回 None。

    Dolby Vision 与 HDR10+ 都通过 ffprobe 的 side data/codec tag 判定；若动态
    元数据不可见，再按 BT.2100 传输特性回退 HDR10（PQ）或 HLG。顺序不能
    反过来，否则带 PQ 基础层的 Dolby Vision/HDR10+ 会被误标成普通 HDR10。
    """
    side_data_types = {
        str(item.get("side_data_type") or "").lower()
        for item in (video.get("side_data_list") or [])
        if isinstance(item, dict)
    }
    codec_tag = str(video.get("codec_tag_string") or "").lower()
    if codec_tag in {"dvh1", "dvhe"} or any(
        "dovi" in value or "dolby vision" in value for value in side_data_types
    ):
        return "Dolby Vision"
    if any(
        "hdr10+" in value or "smpte2094-40" in value or "smpte 2094-40" in value
        for value in side_data_types
    ):
        return "HDR10+"
    transfer = (video.get("color_transfer") or "").lower()
    if transfer == "smpte2084":
        return "HDR10"
    if transfer == "arib-std-b67":
        return "HLG"
    return None


def _frame_rate(value) -> float | None:
    """ffprobe 的 ``24000/1001`` 等有理数帧率转成稳定的小数展示值。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            rate = float(numerator) / denominator_value
        else:
            rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate <= 0 or rate > 1000:
        return None
    return round(rate, 3)


def _color_space_label(video: dict) -> str | None:
    """把 ffprobe 的色彩原色/矩阵值收敛成用户可识别的标准名称。"""
    primaries = str(video.get("color_primaries") or "").lower()
    matrix = str(video.get("color_space") or "").lower()
    # 某些文件的 color_primaries 会写成 unknown，但矩阵仍有有效值；逐项
    # 尝试，避免一个无意义的非空字符串挡住后备信息。
    for value in (primaries, matrix):
        if value.startswith("bt2020"):
            return "BT.2020"
        if value == "bt709":
            return "BT.709"
        if value == "smpte432":
            return "Display P3"
        if value == "smpte431":
            return "DCI-P3"
        if value in {"bt470bg", "smpte170m", "smpte240m"}:
            return "BT.601"
    return None


def _bit_depth(video: dict) -> int | None:
    raw = _to_int(video.get("bits_per_raw_sample"))
    if raw:
        return raw
    pix_fmt = video.get("pix_fmt") or ""
    if "12le" in pix_fmt or "12be" in pix_fmt:
        return 12
    if "10le" in pix_fmt or "10be" in pix_fmt:
        return 10
    if pix_fmt:
        return 8
    return None


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
