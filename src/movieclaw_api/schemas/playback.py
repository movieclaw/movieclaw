"""播放记录在 Web 业务界面的响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

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
    native_hls: bool = False


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
    #: 用户在播放器里点选的音轨（``embedded:<k>``）。给了就认它，服务端不再
    #: 自动换轨。**选了非默认轨会把档 0 顶成档 1**——直出时浏览器只放默认轨，
    #: 必须重封装才能把选中的那条带上。
    audio_track: str | None = None
    #: 用户选中的字幕轨（中性引用）。只在指向**内封 PGS 轨**时改变决策：
    #: 视频转码 + 字幕烧录进画面（Emby 语义，硬边界 1 的唯一例外）。
    #: None = 用观看状态里记住的轨；"off" 与文本轨都不影响视频策略。
    subtitle_track: str | None = None
    #: 用户选的画质上限（如 720）。语义是上限而非目标：源不超就照常直通，
    #: 超了才转码降下去。None = 自动。弱网救急用（§10「手动选清晰度」）。
    max_height: int | None = Field(default=None, ge=240, le=2160)


class VideoPlanView(BaseModel):
    action: str
    codec: str | None = None
    height: int | None = None
    tone_map: bool = False
    #: 非空 = 该字幕轨被烧录进画面（用户显式选中 PGS 触发，Emby 语义的
    #: 「字幕压制」）。前端据此：不再旁挂渲染这条轨、菜单选中态指向它、
    #: 诊断面板显示「字幕压制」。
    burn_subtitle: str | None = None


class AudioPlanView(BaseModel):
    action: str
    track_ref: str | None = None
    codec: str | None = None
    channels: int | None = None
    downmix: bool = False


class AudioTrackView(BaseModel):
    """文件里的一条可选音轨。给播放器渲染音轨菜单用——只有候选列表在手，
    前端才能让用户换轨；`audio.track_ref` 说的是「这次放的是哪条」。"""

    ref: str
    codec: str | None = None
    channels: int | None = None
    language: str | None = None
    is_default: bool = False


class SubtitlePlanView(BaseModel):
    track_ref: str
    kind: str
    language: str | None = None
    is_default: bool = False
    #: 本机 AI 生成的字幕（翻译/双语）。播放器的字幕菜单据此打「AI 生成」标
    is_ai: bool = False


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
    #: 这个文件里全部可选音轨（含当前这条）。前端据此渲染音轨菜单。
    audio_tracks: list[AudioTrackView] = []
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


class PlaybackSourceView(BaseModel):
    """源文件的客观规格（台账真值），诊断面板「源 → 处理」层次的左半边。

    Emby 式面板的关键是把「源是什么」与「我们对它做了什么」摆在一起——
    只报处理结果，用户看不出「1080p H264 明明能直通为什么在转码」这类问题。
    """

    container: str | None = None
    resolution: str | None = None
    video_codec: str | None = None
    hdr: str | None = None
    #: 总码率（bps）；探测不出为 None
    bit_rate: int | None = None
    frame_rate: float | None = None
    size_bytes: int | None = None


class PlaybackStateView(BaseModel):
    """一个播放单元在当前成员名下的观看状态。续播与上报共用同一形状。"""

    position_ms: int
    played: bool
    play_count: int
    #: 服务端算出的片长（在位文件实测 > 分集刮削 > 条目刮削）；都没有为 None。
    #: 客户端拿它画进度条兜底，但**已看判定的分母始终以服务端为准**。
    duration_ms: int | None = None
    audio_track: str | None = None
    subtitle_track: str | None = None


class PlaybackArtifactUploadView(BaseModel):
    """远程 Worker 最近一次产物上传的脱敏记录。"""

    name: str
    status: int
    received_bytes: int
    content_length: int | None = None
    transfer_encoding: str | None = None
    occurred_at_ms: int


class PlaybackDiagnosticsView(BaseModel):
    """播放器诊断面板使用的会话快照，不包含任何签名 URL 或令牌。"""

    session_state: str
    session_error: str | None = None
    processing_mode: str
    execution_location: str
    backend: str | None = None
    encoder: str | None = None
    worker_id: str | None = None
    worker_version: str | None = None
    worker_platform: str | None = None
    worker_arch: str | None = None
    ffmpeg_version: str | None = None
    worker_online: bool | None = None
    worker_last_seen_seconds: float | None = None
    job_id: str | None = None
    attempt_id: str | None = None
    job_state: str | None = None
    job_out_time_ms: int | None = None
    job_speed: str | None = None
    job_phase: str | None = None
    job_exit_code: int | None = None
    job_error: str | None = None
    job_stderr_tail: str | None = None
    head_segment: int | None = None
    highest_produced_segment: int | None = None
    requested_segment: int | None = None
    served_segment: int | None = None
    segment_wait_ms: int | None = None
    segment_status: int | None = None
    pending_segments: list[int] = Field(default_factory=list)
    #: ``failed_segments`` 只列当前播放游标之后仍可能影响播放的缺口；旧轮次
    #: 的失败记录单独返回，避免播放器已经走过后仍显示成当前卡点。
    failed_segments: list[int] = Field(default_factory=list)
    historical_failed_segments: list[int] = Field(default_factory=list)
    recent_uploads: list[PlaybackArtifactUploadView] = Field(default_factory=list)
    cache_bytes: int = 0
    total_segments: int | None = None


class PlaybackSessionView(BaseModel):
    """开会话的结果。

    三态里只有 ``plan`` 才会真的起会话；``consent`` / ``rejected`` 原样把
    决策带回前端，由它渲染弹窗或错误说明。
    """

    decision: PlaybackDecisionView
    #: 档 0 没有会话（原文件直出），此处为 None
    session_id: str | None = None
    #: 可直接喂给 <video src> 或 hls.js 的地址，已带签名 token。
    #: 决策不是 plan 时为 None。
    stream_url: str | None = None
    #: 会话时间轴的零点在文件里的位置。**文件时间 = start_ms + currentTime**——
    #: 全前端只有这一处换算（见 ffmpeg_args 模块文档的时间轴取舍）。
    #: timeline="file" 时它退化为「建议的起播位置」：分片时间戳本身就是
    #: 文件绝对时间，currentTime 即文件时间，前端应把播放器 seek 到这里。
    start_ms: int = 0
    #: 时间轴语义：session = 旧会话相对制（流从 0 起）；file = VOD 预生成
    #: 列表（§12），播放列表覆盖全片、时间戳为文件绝对时间，seek 任意位置
    #: 不需要换会话。
    timeline: str = "session"
    #: 旁挂字幕地址（已带 token），与 decision.subtitles 一一对应。
    subtitle_urls: list[str] = []
    #: master 播放列表（带 WEBVTT 字幕组），仅 VOD 会话有。iOS 原生 HLS 用
    #: 它——字幕成为系统级字幕轨，画中画/原生全屏里由系统渲染（§12）。
    master_url: str | None = None
    #: 本次会话实际使用的硬件加速后端（vaapi / qsv / nvenc / videotoolbox）；
    #: None = 纯软件。诊断面板要靠它回答「到底有没有走显卡」——用户报「转码
    #: 很卡」时，这一项与「有没有装对驱动」是同一个问题的两面（§6.5）。
    hw_backend: str | None = None
    #: 本单元的观看状态快照。续播点已按它并入 ``start_ms``，这里整份带回是
    #: 给前端**预填时间轴与恢复字幕记忆**用的——省掉起播链路里「先问 /resume
    #: 再开会话」的一次串行往返（§6.10）。file_id 直连（无播放单元）时为 None。
    watch: PlaybackStateView | None = None
    #: 选中文件的源规格（台账真值）。诊断面板按 Emby 的「源 → 处理」层次
    #: 展示：MKV 24 Mbps → HLS、1080p H264 → 直通……（§6.5）
    source: PlaybackSourceView | None = None


class PlaybackSessionRequest(PlaybackDecideRequest):
    """开会话请求：在决策请求上多一个起播位置。"""

    #: 从文件的哪个位置开始。**None = 服务端按观看状态定**：看完的从头播，
    #: 没看完的接续播点——分享出去的链接因此天然「各看各的进度」。显式给值
    #: （含 0）原样照办：seek 重开、「从头开始」都走这条路。
    start_ms: int | None = None


class PlaybackItemView(BaseModel):
    """播放页要的条目信息，只有播放器用得上的那几样。

    播放路由只带 ``media_item_id``——它以 ``(kind, tmdb_id)`` 为锚、幂等复用，
    比库自增 id 稳定得多，分享出去的地址不会因删库重建而失效（§6.10）。库归
    属由服务端按成员可见性解析，前端只在「退出播放跳回条目页」时用到它。
    """

    media_item_id: int
    library_id: int
    #: movie / tv
    kind: str
    title: str
    year: int | None = None
    #: 海报（本地刮削资产优先、TMDB 兜底），起播前的占位画面用
    poster_url: str | None = None


# ---------------------------------------------------------------------------
# 网页播放器：观看状态（续播点、已看、轨记忆）
# ---------------------------------------------------------------------------


class PlaybackProgressRequest(BaseModel):
    """一次观看状态上报。三种事件同一入口，与 Jellyfin 的 Playing /
    Playing/Progress / Playing/Stopped 一一对应。"""

    media_item_id: int
    #: 电影恒为 (0, 0) 的哨兵单元，与台账、playback_state 的约定一致
    season_number: int = 0
    episode_number: int = 0
    event: str = "progress"  # start | progress | stop
    #: 播到文件的哪个位置。**None = 没报**（视同播到结尾标已看），与报 0
    #: （拖回开头）语义不同——别把「不知道」和「零」合并。
    position_ms: int | None = None
    #: 中性轨引用（external:<文件名> / embedded:<下标> / 字幕的 "off"）。
    #: None = 本次不报该轨，服务端保持原值不动。
    audio_track: str | None = None
    subtitle_track: str | None = None


# PlaybackStateView 定义在会话模型之前（PlaybackSessionView.watch 引用它）。


# ---------------------------------------------------------------------------
# 网页播放器：策略配置（软件转码同意链路 §3.6；独立设置页已撤，上限自动推导）
# ---------------------------------------------------------------------------


class PlaybackPolicyView(BaseModel):
    """播放策略的当前取值。字段与 PlaybackPolicySetting 一一对应。

    数字上限（并发、输出高度、缓存配额）不在这里——它们已改为按机器规格
    自动推导（services/playback/limits.py），不再是配置项。
    """

    software_transcode_enabled: bool
    #: 实测结果而非配置项——用户改不了自己有没有显卡。前端据此说明
    #: 「无可用硬件加速，HDR 片源需要软件转码」这类结论。
    hardware_available: bool = False
    #: 探测到的硬件后端名（vaapi / qsv / nvenc / videotoolbox），无则为空
    hw_backends: list[str] = []


class PlaybackPolicyPayload(BaseModel):
    """策略保存请求。**全字段可选，None = 不动这一项**——同意弹窗只翻
    software_transcode_enabled 一个开关。"""

    software_transcode_enabled: bool | None = None


class PlaybackFontsView(BaseModel):
    """ASS 字幕依赖的内嵌字体地址（已带签名 token）。"""

    fonts: list[str] = []


class HwBackendStatusView(BaseModel):
    """一个硬件加速后端的自检结论。``detail`` 是给用户看的中文原因与修法。"""

    name: str
    label: str
    available: bool
    detail: str


class HwProbeView(BaseModel):
    """硬件加速自检结果。

    `available` 为空即「只能软件转码」——用户据此决定是去挂设备，还是接受
    软件转码的代价。
    """

    backends: list[HwBackendStatusView] = []
    hardware_available: bool = False


class TrickplayView(BaseModel):
    """进度条缩略图索引。

    `ready=false` 表示还在生成（或这部片生成不了）——前端表现为「暂无预览」，
    不影响播放。前端据 `interval_ms` 与格子尺寸算「第 t 秒在哪张图的哪一格」。
    """

    ready: bool = False
    interval_ms: int = 0
    tile_width: int = 0
    tile_height: int = 0
    columns: int = 0
    #: 每张雪碧图的行数。必须下发——少了它前端只能反推每张图的容量，
    #: 而最后一张通常没填满，反推必错。
    rows: int = 0
    count: int = 0
    sheets: list[str] = []


class PlaybackClientLogPayload(BaseModel):
    """播放器客户端事件上报：把浏览器侧的现场（MediaError 详情、播放器状态）
    落进服务端日志。iPhone 上的播放故障没有任何本地可看的控制台，服务端
    日志是唯一能拿到客户端真相的地方。"""

    event: str
    detail: dict = {}


class PlaybackMetricPayload(BaseModel):
    """一次播放结束时上报的质量快照。指标口径按 CTA-2066，不自创。"""

    library_file_id: int | None = None
    tier: int
    degraded_from: int | None = None
    engine: str = ""
    hw_backend: str = ""
    ttff_ms: int | None = None
    rebuffer_ms: int = 0
    rebuffer_count: int = 0
    seek_count: int = 0
    dropped_frames: int | None = None
    total_frames: int | None = None
    watched_ms: int = 0


class PlaybackStatsView(BaseModel):
    """播放质量汇总。样本不足时各项为 null——不编数字。

    `direct_ratio` 是**北极星指标**：档 0 + 档 1 占全部播放的比例。这一个数
    同时代表画质（没重编码）、速度（秒开）和服务器负担（不烧 GPU）。
    """

    sessions: int = 0
    direct_ratio: float | None = None
    degraded_ratio: float | None = None
    ttff_p50_ms: int | None = None
    ttff_p95_ms: int | None = None
    rebuffer_ratio: float | None = None
    dropped_ratio: float | None = None
    tier_counts: dict[int, int] = {}
