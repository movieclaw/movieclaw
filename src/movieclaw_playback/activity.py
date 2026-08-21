"""媒体库播放活动的进程内实时注册表（活动页「观看」视角的数据源）。

设计边界（与 docs/design/activity.md 的"统一观察、分域执行"一致）：

- ``playback_state`` 仍是观看进度的唯一事实源，本模块**只做实时快照**——
  正在播放的会话、正在服务的字节流，全部放在进程内存里，进程重启即清空，
  不落库、不新增迁移；持久的"最近观看"由 API 层直接查 ``playback_state``。
- 两条互不认识的通路在这里汇合：播放器的进度上报（/Sessions/Playing*）
  提供"谁在看什么、看到哪、是否暂停"；取流响应（DisconnectAwareFileResponse）
  提供"传了多少字节、多快"。两者都以设备 ``device_id`` 为锚关联。
- strm 网盘条目播放走 302 直链，流量不经过服务器（既定的零网盘流量设计），
  因此该类会话只有上报、没有字节流——速率标注"不可测"而不是伪造为 0。

单 worker 部署（与 streaming._active_stream_stops 同前提），无需跨进程共享。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from movieclaw_db.models.base import utcnow
from movieclaw_playback.events import ClientInfo
from movieclaw_playback.state import Unit

#: 会话保鲜期：超过这么久没有任何进度上报、且设备没有活跃取流，
#: 视为播放器已消失（异常退出、断网），快照时静默清除。
SESSION_TTL_SECONDS = 300.0

#: 速率滑动窗口：太短抖动大，太长对"暂停后恢复"反应迟钝。
_RATE_WINDOW_SECONDS = 8.0

STREAM_KIND_PLAY = "play"
STREAM_KIND_DOWNLOAD = "download"


@dataclass
class StreamMeter:
    """一条正在服务的字节流（播放取流或整文件下载）的计量器。

    由取流响应在每发出一块后调用 :meth:`add`；速率用滑动窗口内的
    字节增量求平均，读数单位是字节/秒。
    """

    device_id: str
    kind: str  # STREAM_KIND_PLAY / STREAM_KIND_DOWNLOAD
    member_id: int
    unit: Unit
    file_id: int | None
    file_name: str
    size_bytes: int
    client: ClientInfo
    #: 本条连接的 Range 起点。整文件下载据此还原"下载到文件哪个位置"——
    #: 断点续传从中间开始，只看 bytes_sent 会把进度算回 0。
    start_offset: int = 0
    started_at: datetime = field(default_factory=utcnow)
    bytes_sent: int = 0
    _samples: deque = field(default_factory=deque)  # [(monotonic, 字节数)]

    def add(self, count: int) -> None:
        if count <= 0:
            return
        now = time.monotonic()
        self.bytes_sent += count
        self._samples.append((now, count))
        self._prune(now)

    def _prune(self, now: float) -> None:
        while self._samples and now - self._samples[0][0] > _RATE_WINDOW_SECONDS:
            self._samples.popleft()

    @property
    def position_bytes(self) -> int:
        """本条连接已推进到文件的哪个字节位置（Range 起点 + 本次已传）。"""
        return self.start_offset + self.bytes_sent

    def rate_bytes_per_second(self, now: float | None = None) -> float:
        """滑动窗口内的平均传输速率（字节/秒）；窗口内无流量为 0。"""
        now = time.monotonic() if now is None else now
        self._prune(now)
        if not self._samples:
            return 0.0
        span = max(now - self._samples[0][0], 1.0)
        return sum(count for _, count in self._samples) / span


@dataclass
class PlaySession:
    """一台设备正在进行的播放会话（由播放器的进度上报驱动）。"""

    device_id: str
    member_id: int
    client: ClientInfo
    unit: Unit
    position_ms: int | None = None
    paused: bool = False
    started_at: datetime = field(default_factory=utcnow)
    last_report_at: datetime = field(default_factory=utcnow)
    #: 本会话是否走过本地取流。False = 网盘直链（302）或播放器只上报不取流，
    #: 展示层据此把速率标注为"不经过服务器"，而不是显示 0。
    local_streamed: bool = False
    #: 本会话内**已结束**连接累计传出的字节。播放器一场播放里会换很多条
    #: Range 连接（每次 seek、每次缓冲区续拉都是新连接，旧连接随即关闭），
    #: 只统计当下还在服务的连接会让"已传输"在整场播放里反复归零，
    #: 与观看进度完全对不上。仍在服务的连接由各自计量器实时累加，
    #: 展示口径 = 本字段 + 活跃计量器的 bytes_sent 之和。
    bytes_transferred: int = 0
    #: 保鲜时钟（monotonic），进度上报与本地取流字节都会续期。
    last_activity_mono: float = field(default_factory=time.monotonic)


_sessions: dict[str, PlaySession] = {}
_meters: list[StreamMeter] = []


def report_start(
    device_id: str, *, member_id: int, client: ClientInfo, unit: Unit
) -> None:
    """播放开始上报：为设备建立（或切换到）新的播放会话。

    同一设备对同一单元重复上报开始（seek、暂停后恢复、部分客户端换源
    重协商都会再发 Playing），保留原会话身份——否则起始时间与已看位置
    会在实时视图里无意义地闪回。
    """
    if not device_id:
        return
    existing = _sessions.get(device_id)
    if existing is not None and existing.unit == unit:
        existing.member_id = member_id
        existing.client = client
        existing.last_report_at = utcnow()
        existing.last_activity_mono = time.monotonic()
        return
    _sessions[device_id] = PlaySession(
        device_id=device_id, member_id=member_id, client=client, unit=unit
    )


def report_progress(
    device_id: str,
    *,
    member_id: int,
    client: ClientInfo,
    unit: Unit,
    position_ms: int | None,
    paused: bool | None,
) -> None:
    """进度上报：刷新会话状态；会话缺席时就地重建（进程重启、丢开始包）。

    ``position_ms``/``paused`` 为 None 表示本次上报没带该字段，保持原值。
    """
    if not device_id:
        return
    session = _sessions.get(device_id)
    if session is None or session.unit != unit:
        session = PlaySession(
            device_id=device_id, member_id=member_id, client=client, unit=unit
        )
        _sessions[device_id] = session
    if position_ms is not None:
        session.position_ms = position_ms
    if paused is not None:
        session.paused = paused
    session.last_report_at = utcnow()
    session.last_activity_mono = time.monotonic()


def report_stop(device_id: str) -> None:
    """停止上报（含播放失败）：会话立即结束并从实时视图消失。"""
    _sessions.pop(device_id, None)


def register_stream(
    *,
    device_id: str,
    kind: str,
    member_id: int,
    unit: Unit,
    file_id: int | None,
    file_name: str,
    size_bytes: int,
    client: ClientInfo,
    start_offset: int = 0,
) -> StreamMeter:
    """登记一条开始服务的字节流并返回其计量器。

    播放取流会顺带把同设备会话标记为"本地直连"；若会话尚未建立
    （上报晚于取流是常态），标记在下一次快照合并时按计量器补判。
    """
    meter = StreamMeter(
        device_id=device_id,
        kind=kind,
        member_id=member_id,
        unit=unit,
        file_id=file_id,
        file_name=file_name,
        size_bytes=size_bytes,
        client=client,
        start_offset=start_offset,
    )
    _meters.append(meter)
    if kind == STREAM_KIND_PLAY:
        session = _sessions.get(device_id)
        if session is not None and session.unit == unit:
            session.local_streamed = True
            session.last_activity_mono = time.monotonic()
    return meter


def unregister_stream(meter: StreamMeter) -> None:
    """回收已结束的字节流；字节数结转进会话累计，保鲜时钟顺带续期。

    先 ``remove`` 再结转：重复回收（同一条连接的 on_close 被调用两次）在
    第一次之后就直接返回，不会把同一批字节计两遍。只结转给单元相同的会话——
    换集时新会话可能先建立，上一集的收尾连接不该把字节记到新一集头上。
    """
    try:
        _meters.remove(meter)
    except ValueError:
        return
    session = _sessions.get(meter.device_id)
    if session is not None and meter.kind == STREAM_KIND_PLAY and meter.bytes_sent > 0:
        if session.unit == meter.unit:
            session.bytes_transferred += meter.bytes_sent
        session.local_streamed = True
        session.last_activity_mono = time.monotonic()


def snapshot(
    now_mono: float | None = None,
) -> tuple[list[PlaySession], list[StreamMeter]]:
    """取当前实时快照：先清掉过期会话，再返回（会话列表, 字节流列表）。

    过期判定：超过 :data:`SESSION_TTL_SECONDS` 没有进度上报，**且**设备
    没有仍在服务的播放流——只要还在读盘发包，会话就算活着。
    """
    now = time.monotonic() if now_mono is None else now_mono
    devices_streaming = {
        m.device_id for m in _meters if m.kind == STREAM_KIND_PLAY
    }
    for device_id in list(_sessions):
        session = _sessions[device_id]
        if (
            now - session.last_activity_mono > SESSION_TTL_SECONDS
            and device_id not in devices_streaming
        ):
            del _sessions[device_id]
            continue
        # 上报先于取流到达时错过的标记，在快照口径补齐
        if not session.local_streamed and device_id in devices_streaming:
            for m in _meters:
                if (
                    m.device_id == device_id
                    and m.kind == STREAM_KIND_PLAY
                    and m.unit == session.unit
                ):
                    session.local_streamed = True
                    break
    return list(_sessions.values()), list(_meters)


def reset() -> None:
    """清空注册表（仅测试使用）。"""
    _sessions.clear()
    _meters.clear()
