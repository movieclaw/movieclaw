"""播放记录在 Web 业务界面的响应模型。"""

from __future__ import annotations

from datetime import datetime

from movieclaw_api.schemas.base import BaseModel
from movieclaw_media.models import MediaKind


class RecentWatchItemView(BaseModel):
    """媒体库首页的一张最近观看卡片。"""

    media_item_id: int
    library_id: int
    kind: MediaKind
    title: str
    year: int | None
    poster_url: str | None
    backdrop_url: str | None
    episode_still_url: str | None
    season_number: int
    episode_number: int
    episode_title: str | None
    # 锚点之后、当前成员从未看过且文件在位的分集数——“还能接着看几集”，
    # 不是“最近入库了几集”：看完全剧、补齐旧季与洗版都不该触发提醒。
    unwatched_ahead_count: int
    position_ms: int
    duration_ms: int | None
    progress_percent: int | None
    played: bool
    play_count: int
    last_played_at: datetime


class RecentWatchView(BaseModel):
    """最近观看横排的数据载荷。"""

    items: list[RecentWatchItemView]


# ---------------------------------------------------------------------------
# 活动页「观看」视角（管理员运维视角，docs/design/activity.md）
# ---------------------------------------------------------------------------


class MediaActivityTarget(BaseModel):
    """播放会话 / 文件下载指向的媒体条目摘要。"""

    media_item_id: int
    # 详情页落点：同一作品跨库时取一个确定可达的库；无在位文件为 None
    library_id: int | None
    kind: MediaKind
    title: str
    year: int | None
    poster_url: str | None
    season_number: int
    episode_number: int
    episode_title: str | None


class PlaybackFileSpec(BaseModel):
    """正在播放文件的技术规格（来自 library_file 台账）。"""

    resolution: str | None
    video_codec: str | None
    hdr: str | None
    container: str | None
    bit_rate: int | None
    size_bytes: int | None


class ActivePlaybackSessionView(BaseModel):
    """一台设备正在进行的播放会话。"""

    device_id: str
    member_name: str
    client: str
    device_name: str
    client_version: str
    media: MediaActivityTarget
    position_ms: int | None
    duration_ms: int | None
    progress_percent: int | None
    paused: bool
    # local = 本地文件直连（速率可测）；remote = 网盘直链等不经过服务器的播放
    play_method: str
    rate_bytes_per_second: float | None
    bytes_sent: int | None
    connections: int
    file: PlaybackFileSpec | None
    started_at: datetime
    last_report_at: datetime


class ActiveFileDownloadView(BaseModel):
    """一条正在进行的整文件下载（播放器的离线缓存）。"""

    device_id: str
    member_name: str
    client: str
    device_name: str
    media: MediaActivityTarget | None
    file_name: str
    size_bytes: int
    bytes_sent: int
    rate_bytes_per_second: float
    # 同一设备对同一文件的多条 Range 连接（断点续传）聚合为一条展示
    connections: int
    # 已下载到文件的哪个位置（Range 起点 + 本次已传），以及据此换算的百分比。
    # 文件大小未知时为 None，界面不画进度条而不是画一条假的。
    position_bytes: int
    progress_percent: int | None
    started_at: datetime


class PlaybackDeviceView(BaseModel):
    """已登记的播放器设备。"""

    device_id: str
    device_name: str
    client: str
    client_version: str
    member_name: str
    last_seen_at: datetime | None
    online: bool


class MediaRecentPlayView(BaseModel):
    """全成员维度的一条最近观看记录。"""

    member_name: str
    media: MediaActivityTarget
    position_ms: int
    duration_ms: int | None
    progress_percent: int | None
    played: bool
    play_count: int
    last_played_at: datetime


class MediaActivityView(BaseModel):
    """活动页「观看」视角的完整数据载荷。"""

    sessions: list[ActivePlaybackSessionView]
    downloads: list[ActiveFileDownloadView]
    devices: list[PlaybackDeviceView]
    recent: list[MediaRecentPlayView]


# ---------------------------------------------------------------------------
# 网页播放器：能力探测与播放决策（docs/design/web-player.md §3）
# ---------------------------------------------------------------------------


class VideoSupportIn(BaseModel):
    """前端 ``MediaCapabilities.decodingInfo()`` 的一项视频探测结果。"""

    codec: str
    max_height: int = 2160
    # decodingInfo 三态之二。canPlayType 给不出这两个信号——它分不清
    # 「能解码」和「能流畅解码」。
    smooth: bool = True
    power_efficient: bool = True


class AudioSupportIn(BaseModel):
    codec: str
    max_channels: int = 8


class ClientCapabilityIn(BaseModel):
    """客户端解码能力快照。前端探测后随决策请求上送，并缓存在 localStorage。"""

    video: list[VideoSupportIn] = []
    audio: list[AudioSupportIn] = []
    containers: list[str] = []
    hdr_passthrough: bool = False
    mse: str = "full"
    is_mobile: bool = False


class PlaybackDecideRequest(BaseModel):
    """一次播放决策请求。``file_id`` 与播放单元二选一——给单元时服务端会在
    该单元的全部版本文件里择优（能直通的 1080p 胜过要转码的 2160p）。"""

    file_id: int | None = None
    media_item_id: int | None = None
    season_number: int = 0
    episode_number: int = 0
    capability: ClientCapabilityIn
    # 运行期降档回路：前端播放失败后带上已失败的档位重来，服务端跳过它们。
    failed_tiers: list[int] = []


class VideoPlanView(BaseModel):
    action: str
    codec: str | None = None
    height: int | None = None
    tone_map: bool = False


class AudioPlanView(BaseModel):
    action: str
    track_ref: str | None = None
    codec: str | None = None
    channels: int | None = None
    downmix: bool = False


class SubtitlePlanView(BaseModel):
    track_ref: str
    kind: str
    language: str | None = None
    is_default: bool = False


class PlaybackDecisionView(BaseModel):
    """决策结果的三态并集。``outcome`` 决定其余字段哪些有值。

    - ``plan``    —— 可以播，按 ``tier`` 走；
    - ``consent`` —— 需要用户同意开启软件转码（§3.6）；
    - ``rejected``—— 放不了，``reason`` / ``suggestion`` 面向用户。
    """

    outcome: str  # plan | consent | rejected

    # outcome == "plan"
    tier: int | None = None
    file_id: int | None = None
    container: str | None = None
    video: VideoPlanView | None = None
    audio: AudioPlanView | None = None
    subtitles: list[SubtitlePlanView] = []
    degraded_from: int | None = None

    # outcome == "consent"
    cost_hint: str | None = None
    can_self_enable: bool | None = None
    setting_namespace: str | None = None
    setting_key: str | None = None

    # 三态共有：中文，为什么是这个结果。诊断面板与失败提示共用同一份文案。
    reason: str = ""
    # outcome == "rejected"
    suggestion: str | None = None
