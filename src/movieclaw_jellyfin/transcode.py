"""Jellyfin 协议的转码协商（docs/design/jellyfin-transcode.md）。

本模块只做**协议翻译**，不碰数据库、不起 ffmpeg，可被表驱动单测覆盖：

- 把 PlaybackInfo 的入参（query 优先于 body，键名大小写不敏感）解析成
  ``Negotiation``：播放器声明的码率上限 ``MaxStreamingBitrate``、四个开关
  （``EnableDirectPlay`` / ``EnableDirectStream`` / ``EnableTranscoding`` /
  ``AllowVideoStreamCopy``）、起播位置与点选的音轨；
- 按真 Jellyfin ``StreamBuilder`` 的口径判定一个版本能不能直连
  （``direct_play_allowed``）：只看码率上限与开关，**不看 DeviceProfile 的
  编码条件**——Infuse / VidHub 这类全解码播放器发来的 profile 本来就是
  「我全都能解」，真正会让它们要求转码的只有线路；
- 生成/解析 ``TranscodingUrl``（``/Videos/{item}/master.m3u8?…``）。URL 是
  PlaybackInfo 与 master 路由之间**唯一**的契约：master 不查任何服务端状态，
  转码的目标高度、码率、音轨全部从 URL 还原，与真 Jellyfin 的
  ``StreamInfo.ToUrl`` 同构；
- ``PlaySessionId`` → 转码会话的登记表：播放器换清晰度时会给旧的
  PlaySessionId 发 ``Stopped`` / ``DELETE /Videos/ActiveEncodings``，而新会话
  这时可能已经起来了——按设备停会误杀新会话，必须按 PlaySessionId 精确停。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from movieclaw_jellyfin.catalog import TICKS_PER_MS

#: 真 Jellyfin ``TranscodeReason`` 里「容器码率超过上限」的成员名，原样放进
#: TranscodingUrl，客户端（jellyfin-web 的播放信息面板等）据此展示转码原因。
REASON_BITRATE_EXCEEDED = "ContainerBitrateExceedsLimit"


@dataclass(frozen=True)
class Negotiation:
    """一次 PlaybackInfo 里与转码有关的全部入参。缺省值即真 Jellyfin 的缺省值。"""

    #: 播放器声明的总码率上限（bps）；None = 不限。
    max_bitrate_bps: int | None = None
    enable_direct_play: bool = True
    enable_direct_stream: bool = True
    enable_transcoding: bool = True
    #: false = 客户端要求视频必须重编码（jellyfin-web 换清晰度时会带）。
    allow_video_stream_copy: bool = True
    #: 协议编号的音轨（外挂字幕置前的合成编号）；None = 用默认轨。
    audio_stream_index: int | None = None
    #: 起播位置（毫秒）；None = 从头/客户端自行 seek。
    start_ms: int | None = None

    @property
    def wants_transcode(self) -> bool:
        """客户端是否**明确**要求转码（不看码率）。"""
        return not self.enable_direct_play or not self.allow_video_stream_copy


def _lookup(params: Mapping[str, Any], key: str) -> Any:
    wanted = key.lower()
    for k, v in params.items():
        if str(k).lower() == wanted:
            return v
    return None


def _as_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        text = str(raw).strip()
        return int(float(text)) if text else None
    except ValueError:
        return None


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return default


def parse_negotiation(query: Mapping[str, Any], body: Mapping[str, Any] | None) -> Negotiation:
    """query 优先于 body（MediaInfoController 的合并顺序），键名大小写不敏感。

    ``MaxStreamingBitrate`` 三处来源按优先级：query → body 顶层 →
    ``body.DeviceProfile.MaxStreamingBitrate``（Infuse 只在 profile 里带）。
    0 与负数视为不限（真 Jellyfin ``GetMaxBitrate`` 对 ≤0 同样当无限制）。
    """
    body = body if isinstance(body, dict) else {}

    def pick(key: str) -> Any:
        value = _lookup(query, key)
        return value if value is not None else _lookup(body, key)

    max_bitrate = _as_int(pick("MaxStreamingBitrate"))
    if max_bitrate is None:
        profile = _lookup(body, "DeviceProfile")
        if isinstance(profile, dict):
            max_bitrate = _as_int(_lookup(profile, "MaxStreamingBitrate"))
    if max_bitrate is not None and max_bitrate <= 0:
        max_bitrate = None
    start_ticks = _as_int(pick("StartTimeTicks"))
    return Negotiation(
        max_bitrate_bps=max_bitrate,
        enable_direct_play=_as_bool(pick("EnableDirectPlay"), True),
        enable_direct_stream=_as_bool(pick("EnableDirectStream"), True),
        enable_transcoding=_as_bool(pick("EnableTranscoding"), True),
        allow_video_stream_copy=_as_bool(pick("AllowVideoStreamCopy"), True),
        audio_stream_index=_as_int(pick("AudioStreamIndex")),
        start_ms=(max(0, start_ticks // TICKS_PER_MS) if start_ticks is not None else None),
    )


def direct_play_allowed(source_bitrate_bps: int | None, negotiation: Negotiation) -> bool:
    """能不能直连（对齐 ``StreamBuilder.IsEligibleForDirectPlay`` 的码率条款）。

    源码率未知（台账没探到）按允许——真 Jellyfin 同样只在 ``Bitrate.HasValue``
    时比较；宁可让全解码播放器直连，也不为一个未知数起一路转码。
    """
    if negotiation.wants_transcode:
        return False
    limit = negotiation.max_bitrate_bps
    if limit is None or source_bitrate_bps is None:
        return True
    return source_bitrate_bps <= limit


@dataclass(frozen=True)
class TranscodeParams:
    """master.m3u8 从 TranscodingUrl 还原出的转码目标。"""

    media_source_id: str | None = None
    play_session_id: str | None = None
    #: 视频码率上限（bps），None = 只按分辨率阶梯。
    video_bitrate_bps: int | None = None
    #: 目标高度上限，None = 服务端默认。
    max_height: int | None = None
    audio_stream_index: int | None = None
    start_ms: int = 0


def build_transcoding_url(
    item_guid: str,
    *,
    media_source_id: str,
    play_session_id: str,
    token: str,
    video_bitrate_bps: int | None,
    max_height: int | None,
    audio_stream_index: int | None,
    start_ms: int | None,
    bitrate_exceeded: bool,
) -> str:
    """形态对齐 ``StreamInfo.ToUrl``：``/Videos/{id}/master.m3u8?`` + 目标参数。

    参数名沿用 Jellyfin 的（VideoCodec / AudioCodec / VideoBitrate / MaxHeight /
    TranscodeReasons…），客户端只会原样回传，但抓包对照时一眼能认。
    ``ApiKey`` 拼在 URL 上——播放器媒体内核拉 HLS 不带自定义认证头。
    """
    query: list[tuple[str, str]] = [
        ("MediaSourceId", media_source_id),
        ("PlaySessionId", play_session_id),
        ("VideoCodec", "h264"),
        ("AudioCodec", "aac,eac3,ac3,mp3,opus"),
        ("SegmentContainer", "mp4"),
    ]
    if video_bitrate_bps:
        query.append(("VideoBitrate", str(video_bitrate_bps)))
    if max_height:
        query.append(("MaxHeight", str(max_height)))
    if audio_stream_index is not None:
        query.append(("AudioStreamIndex", str(audio_stream_index)))
    if start_ms:
        query.append(("StartTimeTicks", str(start_ms * TICKS_PER_MS)))
    if bitrate_exceeded:
        query.append(("TranscodeReasons", REASON_BITRATE_EXCEEDED))
    query.append(("ApiKey", token))
    return f"/Videos/{item_guid}/master.m3u8?{urlencode(query)}"


def parse_transcode_params(query: Mapping[str, Any]) -> TranscodeParams:
    """master.m3u8 的入参（键名大小写不敏感；客户端会原样回传我们生成的 URL）。"""
    start_ticks = _as_int(_lookup(query, "StartTimeTicks"))
    video_bitrate = _as_int(_lookup(query, "VideoBitrate"))
    max_height = _as_int(_lookup(query, "MaxHeight"))
    return TranscodeParams(
        media_source_id=(_lookup(query, "MediaSourceId") or None),
        play_session_id=(_lookup(query, "PlaySessionId") or None),
        video_bitrate_bps=video_bitrate if video_bitrate and video_bitrate > 0 else None,
        max_height=max_height if max_height and max_height > 0 else None,
        audio_stream_index=_as_int(_lookup(query, "AudioStreamIndex")),
        start_ms=max(0, start_ticks // TICKS_PER_MS) if start_ticks else 0,
    )


# ---------------------------------------------------------------------------
# PlaySessionId → 转码会话
# ---------------------------------------------------------------------------

#: PlaySessionId → 转码会话 id。进程内单例（生产是单进程 uvicorn，与会话
#: 管理器同一前提）；条目随会话消失而失效，登记新条目时顺手清掉已死的。
_play_sessions: dict[str, str] = {}


def register_play_session(play_session_id: str, session_id: str, *, alive) -> None:
    """登记一次协商对应的转码会话。``alive(session_id) -> bool`` 用于清理死条目。"""
    for key in [k for k, sid in _play_sessions.items() if not alive(sid)]:
        _play_sessions.pop(key, None)
    _play_sessions[play_session_id] = session_id


def take_play_session(play_session_id: str | None) -> str | None:
    """取出并注销 PlaySessionId 对应的会话 id；未登记返回 None。"""
    if not play_session_id:
        return None
    return _play_sessions.pop(play_session_id, None)


def reset_play_sessions() -> None:
    """测试用。"""
    _play_sessions.clear()
