from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class DownloaderType(StrEnum):
    """已适配的下载器类型。"""

    QBITTORRENT = "qbittorrent"
    TRANSMISSION = "transmission"


class DownloaderConfig(BaseModel):
    """下载器连接配置。

    url 填下载器 Web 服务的完整地址：
    - qBittorrent: WebUI 地址，如 ``http://192.168.1.10:8080``
    - Transmission: RPC 地址，如 ``http://192.168.1.10:9091``
      （路径缺省时自动补全为 ``/transmission/rpc``）
    """

    type: DownloaderType
    url: str
    username: str | None = None
    password: str | None = None
    timeout: float = 30.0


class DownloadRequest(BaseModel):
    """提交下载任务的输入参数。

    种子来源二选一（必须且只能提供一个）：
    - torrent_bytes: .torrent 文件内容。PT 站点的种子需要带 cookie 才能下载，
      由 tracker 层的 download_torrent() 取回字节后从这里递交，下载器无需
      也无法直接访问站点。
    - magnet: 磁力链接（或下载器可直接访问的 .torrent URL）。
    """

    torrent_bytes: bytes | None = None
    magnet: str | None = None
    # 保存目录。None 表示使用下载器自己的默认目录。
    save_path: str | None = None
    # 分类：qBittorrent 映射为原生 category；Transmission 没有分类概念，
    # 映射为第一个 label。
    category: str | None = None
    # 标签：qBittorrent 映射为 tags；Transmission 追加到 labels。
    tags: list[str] = Field(default_factory=list)
    # 是否以暂停状态添加（只入队不开始下载）。
    paused: bool = False

    @model_validator(mode="after")
    def _validate_source(self) -> DownloadRequest:
        if (self.torrent_bytes is None) == (self.magnet is None):
            raise ValueError("torrent_bytes 和 magnet 必须且只能提供一个")
        return self


class SubmitResult(BaseModel):
    """提交下载任务的输出。

    调用成功即代表任务已被下载器接收；失败通过 Downloader* 异常抛出。
    info_hash 是跨下载器的统一标识，后续查询/管理该任务都以它为键。
    """

    # 种子 v1 infohash（40 位小写十六进制）。由种子内容/磁力链接本地计算得出，
    # 极少数情况下（纯 v2 磁力链接）无法解析时为 None。
    info_hash: str | None
    # 下载器中的任务名称。提交后未能立即回查到时为空字符串。
    name: str = ""
    # 该种子提交前已存在于下载器中（幂等：不视为错误，也不会重复添加）。
    already_exists: bool = False
    # already_exists 的特例：已存在的任务是**刷流引擎自己抢下的**，提交层
    # 已把它迁到本次请求的目标目录并从刷流台账转出。对调用方而言等同于
    # 一次成功的全新投递（数据在正确位置、任务归 movieclaw 所有），订阅
    # 侧据此把 owned_by_movieclaw 记为 True。
    reclaimed_from_boost: bool = False


class DownloaderInfo(BaseModel):
    """连接测试返回的下载器信息。"""

    type: DownloaderType
    version: str


class DownloaderLimits(BaseModel):
    """下载器全局限制的统一视图（get_limits 返回 / set_limits 输入）。

    归一约定：
    - 限速一律**字节/秒**，``None`` = 不限速。qBittorrent 原生即字节/秒
      （0=不限）；Transmission 原生是 kB/s（1 kB = 1000 字节）+ 独立开关，
      适配器负责换算与开关归一。
    - ``alt_speed_enabled``：备用限速档（qB 的 Alternative Speed Limits /
      Transmission 的 Turtle Mode）当前是否生效。
    - 队列上限控制"同时活动的任务数"，超限任务进 queued 排队——这正是刷流
      大量做种时最容易撞上的墙。``max_active_torrents``（下载+做种总数上限）
      为 qBittorrent 独有，Transmission 读为 None、写时忽略。
    - 字段值 ``None`` 在 set_limits 中表示"该项设为不限/不改"，各适配器
      docstring 说明细节。
    """

    download_limit_bytes: int | None = None
    upload_limit_bytes: int | None = None
    alt_speed_enabled: bool | None = None
    # 队列机制总开关（qB queueing_enabled；Transmission 读 download_queue_enabled，
    # 写时同时作用于下载与做种两个队列开关）
    queue_enabled: bool | None = None
    max_active_downloads: int | None = None
    max_active_uploads: int | None = None
    max_active_torrents: int | None = None


class TorrentFile(BaseModel):
    """下载任务内的单个文件（路径是种子内相对路径）。"""

    path: str
    size_bytes: int
    # 单文件完成字节数是分批入库的权威证据。None 表示旧适配器没有提供，
    # 调用方只能退回整种子 completed，不能按磁盘大小猜测（下载器可能预分配）。
    completed_bytes: int | None = None
    # 未选中的文件不会被下载器写入，也不应阻塞同目录其他已完成文件。
    selected: bool = True


class TorrentBrief(BaseModel):
    """种子的轻量概览（list_torrents 的返回，下载监听导入按名称匹配用）。

    ``content_name`` 是**磁盘上的落盘根目录/文件名**（qBittorrent 取
    content_path 的末段，Transmission 与 name 相同）——监听目录里的条目名
    就是它，按名称匹配天然免疫容器路径映射（save_path 视角差异）。
    ``info_hash`` 供监听导入反查订阅工单：命中即继承投递时锚定的精确身份。
    """

    name: str
    content_name: str
    completed: bool
    info_hash: str = ""
    # 下列字段仍来自下载器的同一批列表结果，不会额外逐任务请求。监听导入只
    # 消费上面的身份字段；任务中心则用这些可选快照展示进度、速度与 ETA。
    progress: float | None = None
    # 已完成字节用于订阅救援的持久心跳；比瞬时速度更能证明任务确实前进。
    completed_bytes: int | None = None
    size_bytes: int | None = None
    dlspeed_bytes: int | None = None
    upspeed_bytes: int | None = None
    # 累计上传量与分享率：刷流引擎按 tick 差分累计上传量计算上传效率。
    # None 表示旧适配器没有提供——消费方必须跳过该 tick，不能当 0 参与差分
    # （会把效率 EMA 错误地打到 0 而触发误汰换）。
    uploaded_bytes: int | None = None
    # 累计下载量（含重下/废弃块，可能大于体积）：刷流的真实带宽成本台账。
    # 与 completed_bytes（有效完成量）语义不同；None 同样表示未提供不可当 0
    downloaded_bytes: int | None = None
    ratio: float | None = None
    # 蜂群规模（tracker 汇报的全网数字，非已连接 peer 数）：刷流汰换的第二
    # 信源——leechers=0 的死种可立即汰换。None=下载器未提供，不可当 0
    swarm_seeders: int | None = None
    swarm_leechers: int | None = None
    eta_seconds: int | None = None
    state: str = "unknown"
    # state == "error" 时下载器给出的可读原因（如 qBittorrent 的"文件缺失"、
    # Transmission 的 errorString）。任务中心据此告诉用户"错在哪、该去哪处理"，
    # 而不是笼统的"任务异常"；下载器没给原因或非错误态时为 None
    error_message: str | None = None


class TorrentStatus(BaseModel):
    """下载任务的当前状态（get_torrent 的返回，入库管线的输入）。

    ``save_path`` 是**下载器视角**的保存目录；movieclaw 与下载器不同容器/
    机器时该路径可能在本进程不可达，入库管线要对此给出可读的中文报错
    （容器路径映射问题，见 docs/design/library.md 第 6 节风险③）。

    速度/体积/ETA/状态是**展示用快照**（订阅详情页的实时进度），全部可缺省：
    入库管线只依赖 progress/completed/save_path/files，旧适配器不补这些字段
    也不影响任何判定逻辑。
    """

    info_hash: str
    name: str
    # 下载进度 0.0 ~ 1.0；completed = 全部数据已落盘（做种/完成态）
    progress: float
    # 当前已完成字节。救援巡检只比较它是否增长，不用瞬时速度猜死活。
    completed_bytes: int | None = None
    # 下载器累计从网络接收的字节。与 completed_bytes 不同，本地校验、复用旧
    # 文件不会增加它；替代源只用该计数证明“新种确实能从 peer 下载”。
    downloaded_bytes: int | None = None
    completed: bool
    save_path: str
    files: list[TorrentFile] = Field(default_factory=list)
    # 任务总体积（已选文件），未知为 None
    size_bytes: int | None = None
    # 当前下载速度（字节/秒），未知为 None
    dlspeed_bytes: int | None = None
    # 预计剩余秒数；下载器认为无法估算（无速度/已完成）时为 None
    eta_seconds: int | None = None
    # 归一化状态词表：downloading / stalled / queued / paused / checking /
    # completed / error / unknown。
    # 各适配器把自家状态收敛到这几个词，前端据此定色与文案
    state: str = "unknown"
    # 错误态的可读原因，语义同 TorrentBrief.error_message
    error_message: str | None = None
