// 由 apps/apple/scripts/gen_api.py 生成，勿手改。重新生成见脚本头部说明。
import Foundation

nonisolated extension API {
    /// 浏览器当前持有的一个账号（GET /auth/accounts 列表项）。
    struct AccountView: Codable, Hashable, Sendable {
        var username: String
        var nickname: String
        /// 头像相对 URL；未上传过为空
        var avatarUrl: String?
        /// admin=超级管理员；member=成员
        var role: String
        /// 是否为当前激活账号（列表里恰有一个为 true）
        var active: Bool

        enum CodingKeys: String, CodingKey {
            case username
            case nickname
            case avatarUrl = "avatar_url"
            case role
            case active
        }
    }

    /// 切换当前生效背景图的请求体。``backdrop_id`` 为空表示切回内置默认背景。
    struct ActiveBackdropUpdate: Codable, Hashable, Sendable {
        /// 要启用的背景图 id；为空表示使用内置默认背景
        var backdropId: String?

        enum CodingKeys: String, CodingKey {
            case backdropId = "backdrop_id"
        }
    }

    /// 一条正在进行的整文件下载（播放器的离线缓存）。
    struct ActiveFileDownloadView: Codable, Hashable, Sendable {
        var deviceId: String
        var revocable: Bool
        var memberName: String
        var client: String
        var deviceName: String
        var media: API.MediaActivityTarget?
        var fileName: String
        var sizeBytes: Int
        var bytesSent: Int
        var rateBytesPerSecond: Double
        var connections: Int
        var positionBytes: Int
        var progressPercent: Int?
        var startedAt: String

        enum CodingKeys: String, CodingKey {
            case deviceId = "device_id"
            case revocable
            case memberName = "member_name"
            case client
            case deviceName = "device_name"
            case media
            case fileName = "file_name"
            case sizeBytes = "size_bytes"
            case bytesSent = "bytes_sent"
            case rateBytesPerSecond = "rate_bytes_per_second"
            case connections
            case positionBytes = "position_bytes"
            case progressPercent = "progress_percent"
            case startedAt = "started_at"
        }
    }

    /// 一台设备正在进行的播放会话。
    struct ActivePlaybackSessionView: Codable, Hashable, Sendable {
        var deviceId: String
        var revocable: Bool
        var memberName: String
        var client: String
        var deviceName: String
        var clientVersion: String
        var media: API.MediaActivityTarget
        var positionMs: Int?
        var durationMs: Int?
        var progressPercent: Int?
        var paused: Bool
        var playMethod: String
        var rateBytesPerSecond: Double?
        var bytesSent: Int?
        var connections: Int
        var file: API.PlaybackFileSpec?
        var startedAt: String
        var lastReportAt: String

        enum CodingKeys: String, CodingKey {
            case deviceId = "device_id"
            case revocable
            case memberName = "member_name"
            case client
            case deviceName = "device_name"
            case clientVersion = "client_version"
            case media
            case positionMs = "position_ms"
            case durationMs = "duration_ms"
            case progressPercent = "progress_percent"
            case paused
            case playMethod = "play_method"
            case rateBytesPerSecond = "rate_bytes_per_second"
            case bytesSent = "bytes_sent"
            case connections
            case file
            case startedAt = "started_at"
            case lastReportAt = "last_report_at"
        }
    }

    /// 活动时间线的一条记录：message 已是完整中文句子，前端直接展示。
    struct ActivityView: Codable, Hashable, Sendable {
        var id: Int
        var type: String
        var message: String
        var payload: [String: API.JSONValue]
        var createdAt: String
        var wantedItemId: Int?

        enum CodingKeys: String, CodingKey {
            case id
            case type
            case message
            case payload
            case createdAt = "created_at"
            case wantedItemId = "wanted_item_id"
        }
    }

    /// 本地刮削（NFO）的一位演员。
    struct ActorView: Codable, Hashable, Sendable {
        var name: String
        var role: String?
        /// 头像地址（NFO 里的图床 URL）
        var thumbUrl: String?
        /// TMDB 影人 ID；有值时前端把这一格链到人物页
        var tmdbPersonId: Int?

        enum CodingKeys: String, CodingKey {
            case name
            case role
            case thumbUrl = "thumb_url"
            case tmdbPersonId = "tmdb_person_id"
        }
    }

    /// 创建 CLI API 令牌。
    struct ApiTokenCreateRequest: Codable, Hashable, Sendable {
        /// 令牌名字，如 'nas-cron'，便于识别与吊销
        var name: String

        enum CodingKeys: String, CodingKey {
            case name
        }
    }

    /// 创建成功的返回体：token 明文仅此一次，请立即保存。
    struct ApiTokenCreatedView: Codable, Hashable, Sendable {
        var id: String
        var name: String
        var createdAt: String
        /// 客户端形态：worker / cli / manual
        var clientType: String
        /// 最近一次使用时间；None 表示从未使用过
        var lastUsedAt: String?
        /// 令牌明文；服务端只存哈希，之后无法再次查看
        var token: String

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case createdAt = "created_at"
            case clientType = "client_type"
            case lastUsedAt = "last_used_at"
            case token
        }
    }

    /// 令牌元信息（列表用；不含任何可用于认证的内容）。
    struct ApiTokenView: Codable, Hashable, Sendable {
        var id: String
        var name: String
        var createdAt: String
        /// 客户端形态：worker / cli / manual
        var clientType: String
        /// 最近一次使用时间；None 表示从未使用过
        var lastUsedAt: String?

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case createdAt = "created_at"
            case clientType = "client_type"
            case lastUsedAt = "last_used_at"
        }
    }

    /// 保存请求体：与 ``AppServerSetting`` 配置域字段一一对应（整体覆盖语义）。
    struct AppConfigPayload: Codable, Hashable, Sendable {
        /// 网络可访问到本应用的完整地址（http/https），如 http://192.168.1.10:3000；空 = 未配置
        var externalUrl: String?

        enum CodingKeys: String, CodingKey {
            case externalUrl = "external_url"
        }
    }

    /// 读取响应 = 可保存字段 + 对外端口的运行时状态（只读，端口另有专用端点）。
    /// 端口没有并进 ``AppConfigPayload``：其它字段是「失焦即存、保存即生效」，
    /// 而改端口要重启整个应用、且改错会把用户关在门外，两者的交互语义完全不同，
    /// 混在一个整体覆盖的请求体里会让改地址时误触发重启。
    struct AppConfigView: Codable, Hashable, Sendable {
        /// 网络可访问到本应用的完整地址（http/https），如 http://192.168.1.10:3000；空 = 未配置
        var externalUrl: String
        /// 当前生效的对外端口（前端监听口）
        var webPort: Int
        /// 端口来源：setting（应用内设置）/ env（环境变量）/ default（默认）
        var webPortSource: String
        /// 内置默认端口，用于「恢复默认」的展示
        var webPortDefault: Int
        /// 上次启动时因无法绑定而被自动废弃的端口；null = 无
        var webPortRejected: Int?
        /// 当前部署形态是否支持应用内改端口（仅由容器入口托管进程的 Docker 部署）
        var webPortConfigurable: Bool

        enum CodingKeys: String, CodingKey {
            case externalUrl = "external_url"
            case webPort = "web_port"
            case webPortSource = "web_port_source"
            case webPortDefault = "web_port_default"
            case webPortRejected = "web_port_rejected"
            case webPortConfigurable = "web_port_configurable"
        }
    }

    /// 外观设置的对外视图。
    /// 背景图是按账号隔离的「图库」：用户上传的图全部保留（``backdrops``，按上传
    /// 时间升序），其中至多一张为当前生效图（``active_id`` / ``active_url``）。
    /// 二者为空表示正在使用内置默认背景——默认背景是前端内置资源，不出现在图库列表里。
    struct AppearanceView: Codable, Hashable, Sendable {
        /// 当前生效的背景图 id；为空表示使用内置默认背景
        var activeId: String?
        /// 当前生效背景图的相对 URL（含版本号）；为空表示内置默认
        var activeUrl: String?
        /// 图库中的全部自定义背景图（上传时间升序）
        var backdrops: [API.BackdropItem]

        enum CodingKeys: String, CodingKey {
            case activeId = "active_id"
            case activeUrl = "active_url"
            case backdrops
        }
    }

    /// 「更换图片」弹层里的一张候选（docs/design/metadata.md 6.3）。
    struct ArtworkCandidateView: Codable, Hashable, Sendable {
        /// TMDB 图片路径（选定时原样回传）
        var filePath: String
        /// 缩略预览地址（TMDB 图床，前端经代理加载）
        var previewUrl: String
        var width: Int?
        var height: Int?
        /// 图上文字的语言；null=无文字（背景首选这类）
        var language: String?
        var voteAverage: Double?
        var voteCount: Int?

        enum CodingKeys: String, CodingKey {
            case filePath = "file_path"
            case previewUrl = "preview_url"
            case width
            case height
            case language
            case voteAverage = "vote_average"
            case voteCount = "vote_count"
        }
    }

    /// 条目的全部候选图，按与自动选图一致的规则排序。
    /// ``current_*`` 是**实际在用**的图路径（前端据此标「当前」）——不能用
    /// "列表第一张"推断：策略升级前刮的条目、手动锁定的条目、TMDB 新增更高票
    /// 的图，三种情况下第一张都不是在用的那张。
    struct ArtworkCandidatesView: Codable, Hashable, Sendable {
        var posters: [API.ArtworkCandidateView]
        var backdrops: [API.ArtworkCandidateView]
        /// 当前在用的海报路径
        var currentPoster: String?
        /// 当前在用的背景路径
        var currentBackdrop: String?
        /// 海报已手动选定，刷新不覆盖
        var posterLocked: Bool
        /// 背景已手动选定，刷新不覆盖
        var backdropLocked: Bool

        enum CodingKeys: String, CodingKey {
            case posters
            case backdrops
            case currentPoster = "current_poster"
            case currentBackdrop = "current_backdrop"
            case posterLocked = "poster_locked"
            case backdropLocked = "backdrop_locked"
        }
    }

    /// 选图请求：kind 指海报还是背景；file_path 为 null 表示恢复自动选图。
    struct ArtworkSelectPayload: Codable, Hashable, Sendable {
        /// poster=海报 / backdrop=背景图
        var kind: String
        /// TMDB 图片路径；null=解锁并恢复自动选图
        var filePath: String?

        enum CodingKeys: String, CodingKey {
            case kind
            case filePath = "file_path"
        }
    }

    /// 图片附件上传成功的回执；attachment_id 随后交给 session.start 引用。
    struct AttachmentUploadView: Codable, Hashable, Sendable {
        /// 附件稳定编号，绑定会话前 24 小时内有效
        var attachmentId: String
        /// 原始文件名（服务端截断到 120 字符）
        var name: String
        /// 图片像素宽度
        var width: Int
        /// 图片像素高度
        var height: Int
        /// 原始字节数
        var bytes: Int

        enum CodingKeys: String, CodingKey {
            case attachmentId = "attachment_id"
            case name
            case width
            case height
            case bytes
        }
    }

    struct AudioPlanView: Codable, Hashable, Sendable {
        var action: String
        var trackRef: String?
        var codec: String?
        var channels: Int?
        var downmix: Bool

        enum CodingKeys: String, CodingKey {
            case action
            case trackRef = "track_ref"
            case codec
            case channels
            case downmix
        }
    }

    /// 一条音轨（ffprobe 探测；字段 None=该项探不出）。
    struct AudioStreamView: Codable, Hashable, Sendable {
        var codec: String?
        /// 编码档次（如 DTS-HD MA），比 codec 更贴近用户认知
        var profile: String?
        var channels: Int?
        /// 声道布局（如 5.1(side)）
        var channelLayout: String?
        /// 语言标签（ISO 639，如 chi/eng）
        var language: String?
        var title: String?
        var `default`: Bool

        enum CodingKeys: String, CodingKey {
            case codec
            case profile
            case channels
            case channelLayout = "channel_layout"
            case language
            case title
            case `default`
        }
    }

    struct AudioSupportIn: Codable, Hashable, Sendable {
        var codec: String
        var maxChannels: Int?

        enum CodingKeys: String, CodingKey {
            case codec
            case maxChannels = "max_channels"
        }
    }

    /// 文件里的一条可选音轨。给播放器渲染音轨菜单用——只有候选列表在手，
    /// 前端才能让用户换轨；`audio.track_ref` 说的是「这次放的是哪条」。
    struct AudioTrackView: Codable, Hashable, Sendable {
        var ref: String
        var codec: String?
        var channels: Int?
        var language: String?
        var isDefault: Bool

        enum CodingKeys: String, CodingKey {
            case ref
            case codec
            case channels
            case language
            case isDefault = "is_default"
        }
    }

    /// 站点认证方式。与 ``movieclaw_tracker`` 的三种 AuthProvider 一一对应。
    /// - ``COOKIE``：用户直接粘贴浏览器 cookie（最简单，见 CookieAuthProvider）
    /// - ``APIKEY``：走站点 API 的密钥认证（如 M-Team，见 ApiKeyAuthProvider）
    /// - ``CREDENTIAL``：用户名 + 密码，由程序模拟登录（见 CredentialAuthProvider）
    typealias AuthType = String
    // 取值：'cookie', 'apikey', 'credential'

    /// 某授权类型及其要求用户填写的字段，供前端渲染表单。
    struct AuthTypeRequirement: Codable, Hashable, Sendable {
        var authType: API.AuthType
        /// 该授权类型需要填写的字段名
        var requiredFields: [String]

        enum CodingKeys: String, CodingKey {
            case authType = "auth_type"
            case requiredFields = "required_fields"
        }
    }

    /// 图库中的一张背景图。
    /// ``url`` 是**带版本号**的相对地址（形如 ``/api/v1/appearance/backdrops/<id>?v=…``），
    /// 版本号取文件修改时间的纳秒值，用来强制刷新浏览器与 WebGL 着色器对旧图的缓存。
    struct BackdropItem: Codable, Hashable, Sendable {
        /// 背景图 id（uuid4 hex）
        var id: String
        /// 图片文件的相对 URL（含版本号）
        var url: String

        enum CodingKeys: String, CodingKey {
            case id
            case url
        }
    }

    /// 批量转移的请求体：目标库 + 选择集 + 冲突策略。
    /// 选择集**只有两种形态**：显式 id 列表，或整库（``all_items``）。刻意不收
    /// 筛选表达式——筛选面已经在 ``library.items.list`` 上，在这里复制一份必然
    /// 分叉（面板说 593 部、实际搬了 586 部，而且没人说得清差在哪）。
    struct BatchTransferPayload: Codable, Hashable, Sendable {
        /// 转移目标库 id（必须与当前库同类型）
        var targetLibraryId: Int
        /// 要转移的条目 id 列表（最多 2000 个）
        var mediaItemIds: [Int]?
        /// true=转移该库的全部条目，忽略 id 列表
        var allItems: Bool?
        /// 目标已有同名目录时：skip=跳过这一条、其余照搬（缺省）；merge=目标那个目录若属于同一部作品就把文件并进去（撞名的按多版本约定退让成「标题 - 分辨率.ext」，绝不覆盖；只是目录重名的另一部片、以及原盘目录仍然跳过）；fail=整批中止（脚本场景要求要么全成要么不动）
        var onConflict: String?

        enum CodingKeys: String, CodingKey {
            case targetLibraryId = "target_library_id"
            case mediaItemIds = "media_item_ids"
            case allItems = "all_items"
            case onConflict = "on_conflict"
        }
    }

    /// 批量转移预检：执行前把「将要发生什么」一次摆清。
    struct BatchTransferPreviewView: Codable, Hashable, Sendable {
        var targetLibraryId: Int
        var targetLibraryName: String
        var targetRoot: String
        var onConflict: String
        /// 选中的条目数
        var selected: Int
        /// 其中真正会搬的条目数
        var movable: Int
        var totalBytes: Int
        var members: [API.PreflightMemberView]
        var crossDeviceItems: Int
        /// 需要完整复制的字节数（同盘搬运是 rename，不占新空间）
        var crossDeviceBytes: Int
        var targetFreeBytes: Int
        /// 目标盘需要的空间（只算跨盘部分，含余量）
        var targetRequiredBytes: Int
        /// 源盘预计释放的字节数——已扣掉硬链接文件。全硬链接库跨盘搬时这个数是 0：下载目录还引用着，删源不释放空间
        var sourceReclaimableBytes: Int
        var hardlinkedItems: Int
        var hardlinkedBytes: Int
        /// null 表示下载器不可达、无法确认（不阻断执行）
        var seedingInPlaceItems: Int?
        /// 同名冲突按锚分类的计数
        var conflicts: [String: Int]
        /// 整批阻断（目标根不可访问、空间不足、库正忙）；非空则不给执行
        var blocked: [String]

        enum CodingKeys: String, CodingKey {
            case targetLibraryId = "target_library_id"
            case targetLibraryName = "target_library_name"
            case targetRoot = "target_root"
            case onConflict = "on_conflict"
            case selected
            case movable
            case totalBytes = "total_bytes"
            case members
            case crossDeviceItems = "cross_device_items"
            case crossDeviceBytes = "cross_device_bytes"
            case targetFreeBytes = "target_free_bytes"
            case targetRequiredBytes = "target_required_bytes"
            case sourceReclaimableBytes = "source_reclaimable_bytes"
            case hardlinkedItems = "hardlinked_items"
            case hardlinkedBytes = "hardlinked_bytes"
            case seedingInPlaceItems = "seeding_in_place_items"
            case conflicts
            case blocked
        }
    }

    /// 首次初始化：创建超级管理员账号。
    struct BootstrapRequest: Codable, Hashable, Sendable {
        /// 管理员用户名
        var username: String
        /// 管理员密码，至少 8 位
        var password: String

        enum CodingKeys: String, CodingKey {
            case username
            case password
        }
    }

    /// 首次初始化状态：前端据此决定进引导页（/setup）还是登录页（/login）。
    struct BootstrapStatus: Codable, Hashable, Sendable {
        var initialized: Bool

        enum CodingKeys: String, CodingKey {
            case initialized
        }
    }

    struct CalibratePayload: Codable, Hashable, Sendable {
        var filename: String

        enum CodingKeys: String, CodingKey {
            case filename
        }
    }

    struct CalibrateResultView: Codable, Hashable, Sendable {
        var ok: Bool
        var message: String
        var scale: Double?
        var offsetMs: Int?
        var score: Double?

        enum CodingKeys: String, CodingKey {
            case ok
            case message
            case scale
            case offsetMs = "offset_ms"
            case score
        }
    }

    /// 目录项：一个系统支持的可配置站点。
    struct CatalogItem: Codable, Hashable, Sendable {
        var siteId: String
        var displayName: String
        var baseUrl: String
        /// 支持的授权类型及各自的必填字段
        var supportedAuthTypes: [API.AuthTypeRequirement]

        enum CodingKeys: String, CodingKey {
            case siteId = "site_id"
            case displayName = "display_name"
            case baseUrl = "base_url"
            case supportedAuthTypes = "supported_auth_types"
        }
    }

    /// 标签栏里的内置分类标签；在列表中的位置即展示顺序。
    /// ``id`` 用枚举类型：未知分类在请求校验阶段即被拒（422），
    /// 存储层无需再防脏数据。
    struct CategoryTabItem: Codable, Hashable, Sendable {
        var type: String
        var id: API.TorrentCategory
        var visible: Bool

        enum CodingKeys: String, CodingKey {
            case type
            case id
            case visible
        }
    }

    /// 标签栏里的内置分类标签；在列表中的位置即展示顺序。
    /// ``id`` 用枚举类型：未知分类在请求校验阶段即被拒（422），
    /// 存储层无需再防脏数据。
    struct CategoryTabItemInput: Codable, Hashable, Sendable {
        var type: String?
        var id: API.TorrentCategory
        var visible: Bool

        enum CodingKeys: String, CodingKey {
            case type
            case id
            case visible
        }
    }

    struct ChangePasswordRequest: Codable, Hashable, Sendable {
        /// 当前密码（校验身份）
        var oldPassword: String
        /// 新密码，至少 8 位
        var newPassword: String

        enum CodingKeys: String, CodingKey {
            case oldPassword = "old_password"
            case newPassword = "new_password"
        }
    }

    /// 推送内容开关(GET 返回与 PUT 载荷同构)。
    struct ChannelPushConfigView: Codable, Hashable, Sendable {
        /// 订阅开始下载时推送
        var pushDispatch: Bool
        /// 入库完成时推送
        var pushImported: Bool

        enum CodingKeys: String, CodingKey {
            case pushDispatch = "push_dispatch"
            case pushImported = "push_imported"
        }
    }

    /// 整库生成章节的作业状态（docs/design/video-chapters.md §4.5）。
    /// 章节作业是低优先级后台 Job，点了菜单后常要排在扫描/刷新后面才跑；随库
    /// 列表一并返回（见 LibraryView.chapter_job），管理页才能显示"排队中 /
    /// 生成到第几个"，用户不必去活动页找。只投影未完成态，跑完即 null。
    struct ChapterJobView: Codable, Hashable, Sendable {
        var jobId: String
        /// Job 未完成态原词：queued / running / cancelling …
        var status: String
        /// 已处理文件数（含失败）
        var processed: Int
        var total: Int
        /// 生成失败的文件数
        var failed: Int
        /// 0-100；排队中或分母未知为 null
        var percent: Double?
        /// 已请求停止，正在收尾
        var stopping: Bool

        enum CodingKeys: String, CodingKey {
            case jobId = "job_id"
            case status
            case processed
            case total
            case failed
            case percent
            case stopping
        }
    }

    /// 一个章节（docs/design/video-chapters.md §4.6）：详情页「场景」横排的一张卡。
    struct ChapterView: Codable, Hashable, Sendable {
        /// 章节序号（0 起），与 Jellyfin 章节图路由的 index 同义
        var index: Int
        /// 章节起点（毫秒）
        var startMs: Int
        /// 章节终点（毫秒）；末章无终点时为 null
        var endMs: Int?
        /// 场景图上那一帧的真实时间（毫秒）；跳播用它，无图时为 null（退回 start_ms）
        var frameMs: Int?
        /// 章节标题；合成章节与无名章节为 null
        var title: String?
        /// 是否按时长合成（容器里没有内嵌章节）
        var synthetic: Bool
        /// 场景图地址（本地资产相对路径）；未生成为 null
        var imageUrl: String?

        enum CodingKeys: String, CodingKey {
            case index
            case startMs = "start_ms"
            case endMs = "end_ms"
            case frameMs = "frame_ms"
            case title
            case synthetic
            case imageUrl = "image_url"
        }
    }

    /// 整组认领：一次把多个待识别文件挂到同一个 TMDB 条目。
    /// 季集号不在这里指定——每个文件沿用扫描时已从文件名解析出的季集号，
    /// 这正是"一部剧几十集一次认领"能成立的前提。
    struct ClaimBatchPayload: Codable, Hashable, Sendable {
        /// 待识别文件 id 数组（来自待识别清单接口），如 [101,102]
        var fileIds: [Int]
        /// Discover 返回的 TMDB 影视条目稳定引用，如 tmdb:tv:1396
        var titleRef: String

        enum CodingKeys: String, CodingKey {
            case fileIds = "file_ids"
            case titleRef = "title_ref"
        }
    }

    struct CleanPayload: Codable, Hashable, Sendable {
        /// all=全部清空，orphans=只删孤儿条目
        var mode: String

        enum CodingKeys: String, CodingKey {
            case mode
        }
    }

    struct CleanResultView: Codable, Hashable, Sendable {
        var key: String
        var mode: String
        var removed: Int
        /// 因正在使用而跳过的条目数
        var skippedBusy: Int
        var freedBytes: Int

        enum CodingKeys: String, CodingKey {
            case key
            case mode
            case removed
            case skippedBusy = "skipped_busy"
            case freedBytes = "freed_bytes"
        }
    }

    /// 客户端解码能力快照。前端探测后随决策请求上送，并缓存在 localStorage。
    struct ClientCapabilityIn: Codable, Hashable, Sendable {
        var video: [API.VideoSupportIn]?
        var audio: [API.AudioSupportIn]?
        var containers: [String]?
        var hdrPassthrough: Bool?
        var mse: String?
        var isMobile: Bool?
        var nativeHls: Bool?

        enum CodingKeys: String, CodingKey {
            case video
            case audio
            case containers
            case hdrPassthrough = "hdr_passthrough"
            case mse
            case isMobile = "is_mobile"
            case nativeHls = "native_hls"
        }
    }

    /// 下载器类型。取值与 ``movieclaw_downloader.DownloaderType`` 一一对应。
    /// 此处独立定义而非直接 import —— movieclaw_db 是纯存储层，
    /// 不反向依赖领域库（与 SiteCredential 不依赖 tracker 同理）。
    typealias ClientType = String
    // 取值：'qbittorrent', 'transmission'

    /// 合集卡片的一张封面图。
    /// 合集自己没有图，封面就是成员的海报。由服务端在列合集时一并给出——否则
    /// 客户端要为每个合集再请求一次成员才画得出卡片，一屏合集就是一屏请求。
    struct CollectionCover: Codable, Hashable, Sendable {
        var url: String
        var blur: String?

        enum CodingKeys: String, CodingKey {
            case url
            case blur
        }
    }

    /// 手动合集的成员操作：加入与排序共用这一个形状。
    struct CollectionItemsPayload: Codable, Hashable, Sendable {
        /// 作品 id 列表。加入时是「要加的这些」，排序时是「新的先后顺序」
        var mediaItemIds: [Int]?

        enum CodingKeys: String, CodingKey {
            case mediaItemIds = "media_item_ids"
        }
    }

    /// 创建 / 更新合集的请求体。不传的字段一律「不改动」。
    struct CollectionPayload: Codable, Hashable, Sendable {
        /// 展示名
        var name: String?
        /// 所属库（创建时必给）
        var libraryId: Int?
        /// 收录规则；给空表 = 改成名单驱动（配合 item_ids 快照）
        var rules: [API.JSONValue]?
        /// 合集内默认排序
        var sort: String?
        var visibility: String?
        /// 隐藏 / 取消隐藏（自动合集删不掉，只能藏；藏了要能放回来）
        var hidden: Bool?
        /// 固定名单（「固定当前这 N 部」就是把此刻的命中集快照过来）；给了它就是名单驱动的合集
        var itemIds: [Int]?
        /// 创建时把 rules 此刻的命中集固化成名单（此后不再自动收录）。客户端因此不必把上千个 id 回传一遍——它要表达的本来就是「就这一批」，而不是「这一批具体是哪些」
        var snapshot: Bool?

        enum CodingKeys: String, CodingKey {
            case name
            case libraryId = "library_id"
            case rules
            case sort
            case visibility
            case hidden
            case itemIds = "item_ids"
            case snapshot
        }
    }

    /// 系列合集的「已有 N / 共 M」与缺片名单（docs/design/library-series-collections.md 6.5）。
    /// 只在合集详情页展示，**不上卡片**——一屏几十个红色角标是压迫感不是帮助。
    struct CollectionSeriesView: Codable, Hashable, Sendable {
        var seriesName: String?
        /// 库里已有几部
        var ownedCount: Int
        /// 这个系列一共几部（TMDB 档案）
        var total: Int
        /// 系列官方海报
        var imageUrl: String?
        var parts: [API.SeriesPartView]
        /// 拉到上游档案了吗；false=没配 TMDB / 网络不通 / 本地系列没有上游档案
        var available: Bool

        enum CodingKeys: String, CodingKey {
            case seriesName = "series_name"
            case ownedCount = "owned_count"
            case total
            case imageUrl = "image_url"
            case parts
            case available
        }
    }

    /// 一个合集。形态（规则驱动 / 名单驱动、可不可改）是**推导**出来的，不是存的。
    struct CollectionView: Codable, Hashable, Sendable {
        var id: Int
        var name: String
        /// 所属库；null=跨库合集
        var libraryId: Int?
        /// 收录规则，与 library.match_rules 同构
        var rules: [API.JSONValue]
        /// 合集内默认排序
        var sort: String
        /// household=全家可见 / private=只有我
        var visibility: String
        /// 内置合集标识；null=用户创建
        var builtin: String?
        /// 能不能改规则（builtin 为 null 才能）
        var editable: Bool
        /// 规则驱动（会自己长）还是名单驱动（固定）
        var ruleDriven: Bool
        /// 当前可见成员数
        var itemCount: Int
        /// 封面取哪部作品；null=取首个成员
        var coverItemId: Int?
        /// 封面素材（前若干个成员的海报）；由服务端取，客户端不必为每个合集再请求一次成员
        var covers: [API.CollectionCover]
        /// 合集从哪来：user=用户自建 / builtin=内置 / series=按作品系列自动生成
        var kind: String
        /// 已隐藏（自动合集的「删除」落成墓碑）
        var hidden: Bool
        var position: Int

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case libraryId = "library_id"
            case rules
            case sort
            case visibility
            case builtin
            case editable
            case ruleDriven = "rule_driven"
            case itemCount = "item_count"
            case coverItemId = "cover_item_id"
            case covers
            case kind
            case hidden
            case position
        }
    }

    /// 站点配置的验证状态机。
    /// 用户填入授权信息后并不立刻可用，需异步验证通过才算数。状态流转：
    /// PENDING ──► VERIFYING ──► ACTIVE   （验证成功，可用）
    /// └─────► FAILED   （验证失败，见 last_error）
    /// - ``PENDING``：已保存，等待验证（刚配置或刚更新后的初始态）。
    /// - ``VERIFYING``：验证进行中（异步任务已接手）。
    /// - ``ACTIVE``：验证通过，凭据真实有效。
    /// - ``FAILED``：验证失败（密码错误、cookie 过期、网络不通等，原因见 last_error）。
    /// 注意：「一个站点是否可用」= ``enabled=True`` 且 ``status=ACTIVE``。
    /// ``enabled`` 是用户的启用开关（意图），``status`` 是系统的验证结果，二者正交。
    typealias ConfigStatus = String
    // 取值：'pending', 'verifying', 'active', 'failed'

    /// 已配置站点的对外视图（**脱敏**：绝不回传 cookie/api_key/密码）。
    struct ConfiguredSite: Codable, Hashable, Sendable {
        var siteId: String
        var authType: API.AuthType
        var enabled: Bool
        var status: API.ConfigStatus
        /// 是否可用 = 已启用且验证通过（status=active）
        var usable: Bool
        /// 站点保护开关：订阅链路绕开该站，手动搜索/下载不受影响
        var protected: Bool
        /// 自动刷分享率开关
        var boostEnabled: Bool
        /// 刷流暂停中：做种压到极低上传限速，停止汰换与拉新种（任务保留）
        var boostPaused: Bool
        /// 刷流存储预算（字节）
        var boostBudgetBytes: Int
        /// 刷流汰换最低保留天数；0=不保护
        var boostHoldDays: Int
        /// 最近验证成功时间
        var lastVerifiedAt: String?
        /// 最近验证尝试时间
        var lastCheckedAt: String?
        /// 最近验证失败原因（清晰中文）
        var lastError: String?
        /// 站点用户资料快照；从未验证成功过则为 null
        var profile: API.SiteUserProfileView?
        var createdAt: String
        var updatedAt: String

        enum CodingKeys: String, CodingKey {
            case siteId = "site_id"
            case authType = "auth_type"
            case enabled
            case status
            case usable
            case protected
            case boostEnabled = "boost_enabled"
            case boostPaused = "boost_paused"
            case boostBudgetBytes = "boost_budget_bytes"
            case boostHoldDays = "boost_hold_days"
            case lastVerifiedAt = "last_verified_at"
            case lastCheckedAt = "last_checked_at"
            case lastError = "last_error"
            case profile
            case createdAt = "created_at"
            case updatedAt = "updated_at"
        }
    }

    /// 根路径归并的请求体：并到哪个根、从哪些根并过来。
    struct ConsolidateRootsPayload: Codable, Hashable, Sendable {
        /// 要并到的目标根路径。**允许是当前还不在媒体库配置里的新路径**（换盘、换挂载点场景）：归并会先把它加进配置再开始搬
        var into: String
        /// 要并过来的源根路径；留空表示「除 into 之外的全部根」
        var fromRoots: [String]?

        enum CodingKeys: String, CodingKey {
            case into
            case fromRoots = "from_roots"
        }
    }

    /// 根路径归并预检：与批量转移同一套影响面，外加根配置的变化。
    struct ConsolidateRootsPreviewView: Codable, Hashable, Sendable {
        var libraryId: Int
        var into: String
        var fromRoots: [String]
        /// true=归并前会先把 into 加进媒体库配置
        var intoIsNewRoot: Bool
        var selected: Int
        var movable: Int
        var totalBytes: Int
        var members: [API.PreflightMemberView]
        var crossDeviceItems: Int
        var crossDeviceBytes: Int
        var targetFreeBytes: Int
        var targetRequiredBytes: Int
        var sourceReclaimableBytes: Int
        var hardlinkedItems: Int
        var hardlinkedBytes: Int
        var seedingInPlaceItems: Int?
        var conflicts: [String: Int]
        var blocked: [String]

        enum CodingKeys: String, CodingKey {
            case libraryId = "library_id"
            case into
            case fromRoots = "from_roots"
            case intoIsNewRoot = "into_is_new_root"
            case selected
            case movable
            case totalBytes = "total_bytes"
            case members
            case crossDeviceItems = "cross_device_items"
            case crossDeviceBytes = "cross_device_bytes"
            case targetFreeBytes = "target_free_bytes"
            case targetRequiredBytes = "target_required_bytes"
            case sourceReclaimableBytes = "source_reclaimable_bytes"
            case hardlinkedItems = "hardlinked_items"
            case hardlinkedBytes = "hardlinked_bytes"
            case seedingInPlaceItems = "seeding_in_place_items"
            case conflicts
            case blocked
        }
    }

    /// 插件推送某站点 Cookie 的请求体。
    /// 插件只需上报"当前浏览器域名 + 拼好的 Cookie 串"，站点识别交给后端按域名反查。
    struct CookiePushRequest: Codable, Hashable, Sendable {
        /// 浏览器域名，如 kp.m-team.cc
        var domain: String
        /// 拼好的 Cookie 请求头字符串：name=value; name2=value2
        var cookie: String

        enum CodingKeys: String, CodingKey {
            case domain
            case cookie
        }
    }

    /// 推送结果：告诉插件命中了哪个站点、当前验证状态如何。
    struct CookieSyncResult: Codable, Hashable, Sendable {
        var siteId: String
        var displayName: String
        /// 命中该站点所用的浏览器域名
        var domain: String
        /// 凭据验证状态（推送后通常为 verifying）
        var status: API.ConfigStatus
        /// 是否可用 = 已启用且验证通过
        var usable: Bool

        enum CodingKeys: String, CodingKey {
            case siteId = "site_id"
            case displayName = "display_name"
            case domain
            case status
            case usable
        }
    }

    struct CountryOption: Codable, Hashable, Sendable {
        /// ISO 3166-1 地区码（CN / US …）
        var code: String
        /// 地区中文名（TMDB native_name，缺失回落英文名）
        var name: String

        enum CodingKeys: String, CodingKey {
            case code
            case name
        }
    }

    /// 一条投递记录（内存环形缓冲，诊断用途）。
    struct DeliveryView: Codable, Hashable, Sendable {
        var eventId: String
        var event: String
        var ok: Bool
        var statusCode: Int?
        var durationMs: Int
        var attempts: Int
        var error: String
        var at: String

        enum CodingKeys: String, CodingKey {
            case eventId = "event_id"
            case event
            case ok
            case statusCode = "status_code"
            case durationMs = "duration_ms"
            case attempts
            case error
            case at
        }
    }

    /// 「这不是独立作品」：摘掉身份锚并忽略，不动磁盘。
    /// 花絮/预告/片段被高置信错挂到别的影片时用它——用户要表达的不是"改挂
    /// 到条目 Y"，而是"它根本不该是个条目"。可在「已忽略」里恢复。
    struct DetachPayload: Codable, Hashable, Sendable {
        /// 要摘掉身份的台账行 id 数组
        var fileIds: [Int]

        enum CodingKeys: String, CodingKey {
            case fileIds = "file_ids"
        }
    }

    /// 客户端发起接入请求。
    /// 刻意**没有权限字段**：客户端只声明自己是什么形态、叫什么名字，
    /// 能做什么由批准者决定。
    struct DeviceAuthorizeRequest: Codable, Hashable, Sendable {
        /// 客户端形态：worker（转码 Worker）或 cli（命令行 / Agent）
        var clientType: String
        /// 设备名，批准页上给人看的，如 'Yi的Mac-mini'
        var clientName: String

        enum CodingKeys: String, CodingKey {
            case clientType = "client_type"
            case clientName = "client_name"
        }
    }

    /// 接入请求的回执。``user_code`` 给人看，``device_code`` 用于兑换。
    struct DeviceAuthorizeView: Codable, Hashable, Sendable {
        /// 配对码，客户端显示给用户，在网页上核对
        var userCode: String
        /// 兑换凭据，仅客户端持有，不得展示给用户
        var deviceCode: String
        /// 用户应当打开的网页地址
        var verificationUri: String
        /// 建议的轮询间隔（秒），不要比这更快
        var interval: Int
        /// 配对码有效期（秒），超时需重新发起
        var expiresIn: Int

        enum CodingKeys: String, CodingKey {
            case userCode = "user_code"
            case deviceCode = "device_code"
            case verificationUri = "verification_uri"
            case interval
            case expiresIn = "expires_in"
        }
    }

    /// 待批准的接入请求（网页审批卡的数据源）。
    struct DeviceRequestView: Codable, Hashable, Sendable {
        var userCode: String
        var clientType: String
        var clientName: String
        /// 请求来源 IP，帮助用户判断这是不是自己那台机器；容器桥接网络会把源地址 NAT 掉，那种情况下为空串，界面应如实说无法确定
        var sourceIp: String
        /// 剩余有效秒数
        var expiresIn: Int

        enum CodingKeys: String, CodingKey {
            case userCode = "user_code"
            case clientType = "client_type"
            case clientName = "client_name"
            case sourceIp = "source_ip"
            case expiresIn = "expires_in"
        }
    }

    /// 客户端轮询兑换令牌。
    struct DeviceTokenRequest: Codable, Hashable, Sendable {
        var deviceCode: String

        enum CodingKeys: String, CodingKey {
            case deviceCode = "device_code"
        }
    }

    /// 兑换成功的返回体：令牌明文仅此一次。
    struct DeviceTokenView: Codable, Hashable, Sendable {
        /// 令牌明文；服务端只存哈希，之后无法再次查看
        var token: String
        var clientName: String
        var clientType: String
        /// 批准者身份，仅用于客户端回显「你现在是谁」
        var grantedBy: String

        enum CodingKeys: String, CodingKey {
            case token
            case clientName = "client_name"
            case clientType = "client_type"
            case grantedBy = "granted_by"
        }
    }

    struct DirUsageView: Codable, Hashable, Sendable {
        /// 登记目录的稳定标识
        var key: String
        var title: String
        /// 一句话用途（行内展示）
        var summary: String
        /// 完整说明与清理后果（悬停/确认时展示）
        var description: String
        /// 运行期实际路径
        var path: String
        /// cache=可清理派生物，data=只展示
        var group: String
        var rebuildCost: String
        /// 是否允许「全部清空」
        var clearable: Bool
        /// 是否提供「清理孤儿条目」
        var orphanAware: Bool
        var exists: Bool
        var bytes: Int
        /// 直接子项数量
        var entries: Int

        enum CodingKeys: String, CodingKey {
            case key
            case title
            case summary
            case description
            case path
            case group
            case rebuildCost = "rebuild_cost"
            case clearable
            case orphanAware = "orphan_aware"
            case exists
            case bytes
            case entries
        }
    }

    /// 库内人物关系表中的一位导演。
    struct DirectorView: Codable, Hashable, Sendable {
        var name: String
        /// 头像地址（TMDB 图床 URL）
        var thumbUrl: String?
        /// TMDB 影人 ID；用于人物页链接
        var tmdbPersonId: Int

        enum CodingKeys: String, CodingKey {
            case name
            case thumbUrl = "thumb_url"
            case tmdbPersonId = "tmdb_person_id"
        }
    }

    struct DiscoverRegionPayload: Codable, Hashable, Sendable {
        /// ISO 3166-1 地区码
        var region: String

        enum CodingKeys: String, CodingKey {
            case region
        }
    }

    /// 当前生效的院线地区（含名称，页脚直接展示）。
    struct DiscoverRegionView: Codable, Hashable, Sendable {
        var region: String
        /// 当前用户是否可修改（管理员）
        var canEdit: Bool

        enum CodingKeys: String, CodingKey {
            case region
            case canEdit = "can_edit"
        }
    }

    /// 发现页影人档案：TMDB 中的全部影视作品及本地库存状态。
    struct DiscoveredPersonDetailsView: Codable, Hashable, Sendable {
        var tmdbPersonId: Int
        var name: String
        var avatarUrl: String?
        /// 参演与幕后作品合并去重后的完整 TMDB 影视履历
        var titles: [API.DiscoveredTitleView]

        enum CodingKeys: String, CodingKey {
            case tmdbPersonId = "tmdb_person_id"
            case name
            case avatarUrl = "avatar_url"
            case titles
        }
    }

    /// 电影详情中的系列作品。
    struct DiscoveredTitleCollectionView: Codable, Hashable, Sendable {
        var id: String
        var name: String
        var titles: [API.DiscoveredTitleView]

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case titles
        }
    }

    /// 影视条目的完整资料、预告片、图片、相关推荐与本地媒体库入口。
    struct DiscoveredTitleDetailsView: Codable, Hashable, Sendable {
        var title: API.DiscoveredTitleView
        var metadata: API.DiscoveredTitleMetadata
        /// 主横幅剧照的 original 原图；详情页沉浸背景的高清升级源，缺失时为 None
        var backdropOriginalUrl: String?
        /// 预告片与花絮（正式预告在前）；来源未提供时为空数组
        var videos: [API.MediaVideo]
        var backdrops: [API.MediaImage]
        var posters: [API.MediaImage]
        var collection: API.DiscoveredTitleCollectionView?
        var recommendations: [API.DiscoveredTitleView]
        var libraryLinks: [API.MediaLibraryLink]

        enum CodingKeys: String, CodingKey {
            case title
            case metadata
            case backdropOriginalUrl = "backdrop_original_url"
            case videos
            case backdrops
            case posters
            case collection
            case recommendations
            case libraryLinks = "library_links"
        }
    }

    /// 影视条目详情中的作品资料与演职员。
    struct DiscoveredTitleMetadata: Codable, Hashable, Sendable {
        var directors: [String]
        /// 带头像和人物 ID 的导演/主创；缺失时前端回退 directors 姓名
        var directorCredits: [API.MediaCastMember]
        var cast: [API.MediaCastMember]
        var country: String
        var language: String
        var released: String
        var network: String?
        var aliases: [String]
        var sourceUrl: String?

        enum CodingKeys: String, CodingKey {
            case directors
            case directorCredits = "director_credits"
            case cast
            case country
            case language
            case released
            case network
            case aliases
            case sourceUrl = "source_url"
        }
    }

    /// 发现/搜索结果中的影视条目摘要。
    /// 豆瓣轻量搜索不提供媒体类型、年份与原名，这些字段允许为空；进入详情后
    /// 服务端会补齐。``external_id`` 只用于展示或排障，后续调用应使用
    /// ``title_ref``，避免不同来源的数字 ID 相互碰撞。
    struct DiscoveredTitleView: Codable, Hashable, Sendable {
        /// 影视条目稳定引用；详情接口原样消费
        var titleRef: String
        var provider: API.MediaSource
        var externalId: String
        var mediaType: API.MediaKind?
        var title: String
        var originalTitle: String
        var releaseYear: Int?
        /// 来源站评分，0 表示暂无评分
        var providerRating: Double
        var genres: [String]
        /// 规模展示文本：电影片长或剧集季数；列表结果可能为空
        var extentLabel: String
        var overview: String
        var posterUrl: String
        var backdropUrl: String?
        /// 本地在位库存摘要；未匹配到本地条目时为空
        var libraryStatus: API.MediaLibraryStatus?

        enum CodingKeys: String, CodingKey {
            case titleRef = "title_ref"
            case provider
            case externalId = "external_id"
            case mediaType = "media_type"
            case title
            case originalTitle = "original_title"
            case releaseYear = "release_year"
            case providerRating = "provider_rating"
            case genres
            case extentLabel = "extent_label"
            case overview
            case posterUrl = "poster_url"
            case backdropUrl = "backdrop_url"
            case libraryStatus = "library_status"
        }
    }

    /// 指定来源和媒体类型下，按产品编排顺序返回的全部片单。
    struct DiscoveryCollectionListView: Codable, Hashable, Sendable {
        var provider: API.MediaSource
        var mediaType: API.MediaKind
        var collections: [API.DiscoveryCollectionView]

        enum CodingKeys: String, CodingKey {
            case provider
            case mediaType = "media_type"
            case collections
        }
    }

    /// 一个片单及本次返回的影视条目。
    struct DiscoveryCollectionTitlesView: Codable, Hashable, Sendable {
        var collection: API.DiscoveryCollectionView
        var titles: [API.DiscoveredTitleView]
        var returnedCount: Int
        /// 服务端当前取得的条目是否因本次 limit 被截断
        var truncated: Bool
        var page: Int
        var totalPages: Int
        var totalResults: Int
        var hasMore: Bool

        enum CodingKeys: String, CodingKey {
            case collection
            case titles
            case returnedCount = "returned_count"
            case truncated
            case page
            case totalPages = "total_pages"
            case totalResults = "total_results"
            case hasMore = "has_more"
        }
    }

    /// 一个可浏览的影视片单及其能力说明。
    struct DiscoveryCollectionView: Codable, Hashable, Sendable {
        /// 片单稳定引用；浏览内容时原样传给 browse-collection
        var collectionRef: String
        /// 片单数据来源：tmdb / douban
        var provider: API.MediaSource
        /// 片单中的影视类型：movie / tv
        var mediaType: API.MediaKind
        /// 面向用户的片单名称
        var name: String
        /// 片单内容与选取口径说明
        var description: String
        /// 是否为有明确名次的排行榜
        var isRanked: Bool
        /// 发现页预览该片单时的默认条数
        var defaultLimit: Int
        /// 是否支持请求完整榜单（可用较大的 limit）
        var supportsFullListing: Bool

        enum CodingKeys: String, CodingKey {
            case collectionRef = "collection_ref"
            case provider
            case mediaType = "media_type"
            case name
            case description
            case isRanked = "is_ranked"
            case defaultLimit = "default_limit"
            case supportsFullListing = "supports_full_listing"
        }
    }

    /// 指定媒体类型可用于组合发现的动态选项。
    struct DiscoveryFilterOptionsView: Codable, Hashable, Sendable {
        var mediaType: API.MediaKind
        var genres: [API.DiscoveryGenreView]

        enum CodingKeys: String, CodingKey {
            case mediaType = "media_type"
            case genres
        }
    }

    /// 筛选器里一个本地化的 TMDB 类型。
    struct DiscoveryGenreView: Codable, Hashable, Sendable {
        var id: Int
        var name: String

        enum CodingKeys: String, CodingKey {
            case id
            case name
        }
    }

    /// 发现页的一个展示分区，数据由 collection_ref 指向领域接口。
    struct DiscoveryPageSectionView: Codable, Hashable, Sendable {
        var collectionRef: String
        var title: String
        var presentation: API.DiscoveryPresentation
        var previewLimit: Int
        var supportsFullListing: Bool

        enum CodingKeys: String, CodingKey {
            case collectionRef = "collection_ref"
            case title
            case presentation
            case previewLimit = "preview_limit"
            case supportsFullListing = "supports_full_listing"
        }
    }

    /// Web 发现页编排；只声明分区，不携带片单条目。
    struct DiscoveryPageView: Codable, Hashable, Sendable {
        var provider: API.MediaSource
        var mediaType: API.MediaKind
        var sections: [API.DiscoveryPageSectionView]

        enum CodingKeys: String, CodingKey {
            case provider
            case mediaType = "media_type"
            case sections
        }
    }

    /// 发现页支持的展示形态；只在 Web 专用协议中出现。
    typealias DiscoveryPresentation = String
    // 取值：'hero', 'ranked-row', 'poster-row'

    /// 搜索时的数据来源范围；浏览片单仍要求指定单一来源。
    typealias DiscoveryProviderSelection = String
    // 取值：'all', 'tmdb', 'douban'

    /// TMDB 组合筛选支持的稳定排序值。
    typealias DiscoverySort = String
    // 取值：'popular', 'rating', 'newest', 'most-rated'

    /// 一次组合筛选得到的一页影视条目。
    struct DiscoveryTitlePageView: Codable, Hashable, Sendable {
        var mediaType: API.MediaKind
        var titles: [API.DiscoveredTitleView]
        var page: Int
        var totalPages: Int
        var totalResults: Int
        var hasMore: Bool

        enum CodingKeys: String, CodingKey {
            case mediaType = "media_type"
            case titles
            case page
            case totalPages = "total_pages"
            case totalResults = "total_results"
            case hasMore = "has_more"
        }
    }

    /// 投递路由预检（订阅弹窗选库时的即时提示）。
    /// 与真实投递的三级兜底 + 映射守门同源判定，把"下载完成后能不能进库"
    /// 这个问题在订阅那一刻就回答掉，而不是等投递失败/落点告警才暴露。
    struct DispatchPreviewView: Codable, Hashable, Sendable {
        /// 投递路由：监听导入目录 / 直接下载进库 / 下载器默认目录
        var mode: String
        /// movieclaw 视角的投递基底目录
        var path: String?
        /// 条目目录的完整路径预览（按生效的命名模板渲染）。前端展示「直接下载到 …」时必须用它，不要自己拼「标题 (年份)」——命名模板可全局/按库自定义，自己拼会与真实落点不一致
        var entryDir: String?
        /// 命中自定义目录规则时的整理落点：下载完成后整理到该目录（不直接入库），文件外部流转回库根后才入账
        var stagingPath: String?
        /// 解析出的目标库（前端预选用）
        var libraryId: Int?
        var libraryName: String?
        var downloaderName: String?
        /// 收藏范围路由结论：true=命中声明库 / false=默认库兜底；未走路由为 null
        var routeMatched: Bool?
        /// 路由理由（中文整句，弹窗徽标直接展示）
        var routeReason: String?
        /// 按规则组适用范围自动选出的规则组（与 route_* 同条件：走了路由才有）
        var ruleSetId: Int?
        var ruleSetName: String?
        /// true=命中某组适用范围 / false=默认规则组兜底
        var ruleSetMatched: Bool?
        /// 选组理由（中文整句，弹窗与模拟一单直接展示）
        var ruleSetReason: String?
        /// 按当前配置投递能否顺利入库
        var ok: Bool
        /// 不 ok 时的中文指引
        var warning: String?

        enum CodingKeys: String, CodingKey {
            case mode
            case path
            case entryDir = "entry_dir"
            case stagingPath = "staging_path"
            case libraryId = "library_id"
            case libraryName = "library_name"
            case downloaderName = "downloader_name"
            case routeMatched = "route_matched"
            case routeReason = "route_reason"
            case ruleSetId = "rule_set_id"
            case ruleSetName = "rule_set_name"
            case ruleSetMatched = "rule_set_matched"
            case ruleSetReason = "rule_set_reason"
            case ok
            case warning
        }
    }

    /// 手动提交下载的请求体：搜索结果里的一条种子。
    /// site_id + download_url 均来自搜索接口返回的 TorrentHit，后端凭它们
    /// 带站点登录态取回 .torrent 字节再递交下载器。
    struct DownloadSubmitPayload: Codable, Hashable, Sendable {
        /// 种子所属站点 ID（TorrentHit.site_id）
        var siteId: String
        /// 种子下载入口（TorrentHit.download_url）
        var downloadUrl: String
        /// 站点内种子 ID（TorrentHit.torrent_id）
        var torrentId: String?
        /// 入库到哪个媒体库
        var libraryId: Int?
        /// 条目标题（推导条目子目录用）
        var title: String?
        /// 条目年份
        var year: Int?
        /// 种子副标题（识别线索用）
        var subtitle: String?
        /// 手选保存目录（覆盖库推导）
        var savePath: String?
        /// 指定下载器；缺省用默认下载器
        var downloaderId: Int?
        /// 按确认的 TMDB 身份自动匹配媒体库并投递
        var autoRoute: Bool?
        /// 智能入库的媒体类型
        var mediaKind: String?
        /// 智能入库已确认的 TMDB 条目 ID
        var tmdbId: Int?
        /// 种子分类；带上即表示记住本次保存位置，缺省不记
        var category: String?

        enum CodingKeys: String, CodingKey {
            case siteId = "site_id"
            case downloadUrl = "download_url"
            case torrentId = "torrent_id"
            case libraryId = "library_id"
            case title
            case year
            case subtitle
            case savePath = "save_path"
            case downloaderId = "downloader_id"
            case autoRoute = "auto_route"
            case mediaKind = "media_kind"
            case tmdbId = "tmdb_id"
            case category
        }
    }

    /// 手动提交下载的结果视图。
    struct DownloadSubmitView: Codable, Hashable, Sendable {
        /// 种子 infohash（极少数纯 v2 磁力无法解析时为空）
        var infoHash: String?
        /// 下载器中的任务名称（提交后未能立即回查到时为空）
        var name: String
        /// 种子提交前已存在于下载器（幂等，未重复添加）
        var alreadyExists: Bool
        /// 接收本次提交的下载器 ID
        var downloaderId: Int
        /// 接收本次提交的下载器名称
        var downloaderName: String
        /// 实际使用的保存目录（下载器视角，已过路径映射；空 = 下载器自身默认目录）
        var savePath: String?

        enum CodingKeys: String, CodingKey {
            case infoHash = "info_hash"
            case name
            case alreadyExists = "already_exists"
            case downloaderId = "downloader_id"
            case downloaderName = "downloader_name"
            case savePath = "save_path"
        }
    }

    /// 一条保存位置记忆（确认条与「不再记住」的数据来源）。
    /// ``downloader_name`` 由服务端解析后回显：确认条要在预检返回前就把「保存到
    /// 哪台下载器」显示出来，让前端再去拉一次下载器列表来翻译 ID 没有必要。
    /// 下载器已被删除时为 None，前端据此判定记忆失效、回落完整弹窗。
    struct DownloadTargetPrefView: Codable, Hashable, Sendable {
        /// 种子分类（TorrentCategory 值）
        var category: String
        /// smart=智能入库 / dir=固定目录 / default=下载器默认目录
        var kind: String
        /// 固定目录；仅 kind=dir 有值
        var savePath: String?
        /// 指定下载器；空=默认下载器
        var downloaderId: Int?
        /// 下载器名称；空=用默认下载器，或指定的下载器已被删除
        var downloaderName: String?
        /// 这条记忆最后一次被改变的时间
        var updatedAt: String

        enum CodingKeys: String, CodingKey {
            case category
            case kind
            case savePath = "save_path"
            case downloaderId = "downloader_id"
            case downloaderName = "downloader_name"
            case updatedAt = "updated_at"
        }
    }

    /// 删除下载器任务的结果。
    struct DownloadTaskDeleteView: Codable, Hashable, Sendable {
        var downloaderId: Int
        var infoHash: String
        var deleteFiles: Bool

        enum CodingKeys: String, CodingKey {
            case downloaderId = "downloader_id"
            case infoHash = "info_hash"
            case deleteFiles = "delete_files"
        }
    }

    struct DownloadTaskListView: Codable, Hashable, Sendable {
        var items: [API.DownloadTaskView]
        var sources: [API.DownloadTaskSourceView]

        enum CodingKeys: String, CodingKey {
            case items
            case sources
        }
    }

    /// 下载完成后**还没发生**的那段路：内容会被搬到哪、进不进库。
    /// 口径来自 ``resolve_save_path``（投递、预检、体检共用的唯一实现），因此
    /// 卡片上预告的落点与真正投递时的落点必然一致。任务中心据此把"等待入库 ·
    /// 下一步"这种空话，换成"复制到「综艺」→ 扫描入账"的真实步骤链；推不出
    /// 落点（外部任务、无业务身份）时整体为 null。
    struct DownloadTaskPlanView: Codable, Hashable, Sendable {
        /// watch=命中监听导入规则，下载完成后按策略搬进目标；inplace=直接下载在库内目录，扫描即入账，没有搬运动作；downloader_default=没有可用库/库无根路径，落下载器默认目录，不会自动入库
        var mode: String
        /// 监听导入的搬运策略；mode=watch 才有
        var strategy: String?
        /// 目标媒体库名；库未定（按收藏范围自动选库）或不进库时为 null
        var libraryName: String?
        /// 落点目录（库内条目目录或自定义目录）；推不出时为 null
        var destPath: String?
        /// 整理后是否进入媒体库。自定义目录规则为 false——文件落规则目录后不写库台账，需外部流转后由库根扫描收尾
        var entersLibrary: Bool

        enum CodingKeys: String, CodingKey {
            case mode
            case strategy
            case libraryName = "library_name"
            case destPath = "dest_path"
            case entersLibrary = "enters_library"
        }
    }

    /// 用户立即换种请求的受理结果。
    struct DownloadTaskReplaceView: Codable, Hashable, Sendable {
        var downloaderId: Int
        var infoHash: String
        var attemptId: Int

        enum CodingKeys: String, CodingKey {
            case downloaderId = "downloader_id"
            case infoHash = "info_hash"
            case attemptId = "attempt_id"
        }
    }

    /// 一台下载器在本次快照中的可观测状态；单台故障不拖垮整页。
    struct DownloadTaskSourceView: Codable, Hashable, Sendable {
        var id: Int
        var name: String
        var clientType: API.ClientType
        var status: String
        var message: String?
        var taskCount: Int

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case clientType = "client_type"
            case status
            case message
            case taskCount = "task_count"
        }
    }

    /// 下载器任务关联的订阅摘要，供任务中心回到业务上下文。
    /// ``units`` 是**种子声明覆盖的全集**（含已入库的集），不是"还欠哪些集"。
    /// 早期这里按在途工单过滤，导致列表随入库推进不断缩水、末集入库后变成空，
    /// "覆盖剧集"标签下却少了已入库的集（真实教训）。
    /// ``purpose`` 区分补缺下载与洗版：两者在任务中心同形，但"已入库"对洗版是
    /// 前提而非成果，进度必须换成 ``units[].replaced`` 的替换口径来讲。
    struct DownloadTaskSubscriptionView: Codable, Hashable, Sendable {
        var id: Int
        var mediaItemId: Int
        var mediaTitle: String
        var mediaKind: String
        var posterUrl: String?
        var purpose: String
        /// 洗版成功后是否保留旧版本共存（规则组的收藏家模式）。默认 false=旧版本移入回收站，保留期满自动清理——这是洗版真正会对磁盘做的事，任务中心要在替换发生前就说清楚
        var upgradeKeepOld: Bool
        var units: [API.DownloadTaskUnitView]

        enum CodingKeys: String, CodingKey {
            case id
            case mediaItemId = "media_item_id"
            case mediaTitle = "media_title"
            case mediaKind = "media_kind"
            case posterUrl = "poster_url"
            case purpose
            case upgradeKeepOld = "upgrade_keep_old"
            case units
        }
    }

    /// 订阅下载覆盖的一个追踪单元；电影沿用 0/0 哨兵。
    /// ``status`` 直接透传工单状态机（wanted/grabbed/downloaded/imported）。分批
    /// 入库时同一个种子里各集进度不同，只有逐集状态才能让任务中心说清"覆盖 10
    /// 集、已入库 2 集"——入库 Job 在两批之间并不存活，不能拿它当状态源。
    struct DownloadTaskUnitView: Codable, Hashable, Sendable {
        var seasonNumber: Int
        var episodeNumber: Int
        var status: String
        /// 洗版任务专用：该集是否已被本种子替换完成。洗版单元在投递前就是 imported（库里有旧版本），status 无法表达洗版进度，只有工单改指本 种子才说明替换真的完成了；补缺下载恒为 false
        var replaced: Bool
        /// 内容核验证明种子里没有这一集（声明的覆盖范围与实际文件不符）。这一集已退回重新寻找资源，不会再等这个种子——任务中心必须显示为需关注，而不是继续挂「等待下载完成」
        var contentMissing: Bool

        enum CodingKeys: String, CodingKey {
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case status
            case replaced
            case contentMissing = "content_missing"
        }
    }

    /// 任务中心使用的下载器实时快照。
    /// 下载器仍是下载状态的事实源；这里只在请求时汇总快照，并通过 infohash
    /// 关联订阅工单、换源心跳或手动下载意图，不把实时下载进度复制进本地数据库。
    struct DownloadTaskView: Codable, Hashable, Sendable {
        /// 稳定前端键：下载器 ID + infohash；缺失任务用 missing 前缀
        var id: String
        var infoHash: String
        var name: String?
        var downloaderId: Int?
        var downloaderName: String?
        var downloaderType: API.ClientType?
        var progress: Double?
        var sizeBytes: Int?
        var dlspeedBytes: Int?
        var upspeedBytes: Int?
        /// 累计上传量；刷流分组汇总用
        var uploadedBytes: Int?
        /// 已完成字节；刷流分组汇总用
        var completedBytes: Int?
        var etaSeconds: Int?
        var state: String
        var errorMessage: String?
        var landingError: String?
        var source: String
        var siteId: String?
        /// 来源站点显示名；未知站点回落 site_id
        var siteName: String?
        /// 站点种子详情页 URL；能定位到站内种子时提供
        var pageUrl: String?
        /// 投递时快照的分辨率，如 2160p
        var resolution: String?
        /// 投递时快照的片源，如 WEB-DL
        var mediaSource: String?
        /// 投递时快照是否为原盘 Remux
        var remux: Bool
        var mediaItemId: Int?
        var mediaTitle: String?
        var mediaKind: String?
        var posterUrl: String?
        var plan: API.DownloadTaskPlanView?
        var subscriptions: [API.DownloadTaskSubscriptionView]
        var rescueState: String?
        var noProgressSeconds: Int?
        var canReplace: Bool
        var replacementDueAt: String?
        var rescueMessage: String?

        enum CodingKeys: String, CodingKey {
            case id
            case infoHash = "info_hash"
            case name
            case downloaderId = "downloader_id"
            case downloaderName = "downloader_name"
            case downloaderType = "downloader_type"
            case progress
            case sizeBytes = "size_bytes"
            case dlspeedBytes = "dlspeed_bytes"
            case upspeedBytes = "upspeed_bytes"
            case uploadedBytes = "uploaded_bytes"
            case completedBytes = "completed_bytes"
            case etaSeconds = "eta_seconds"
            case state
            case errorMessage = "error_message"
            case landingError = "landing_error"
            case source
            case siteId = "site_id"
            case siteName = "site_name"
            case pageUrl = "page_url"
            case resolution
            case mediaSource = "media_source"
            case remux
            case mediaItemId = "media_item_id"
            case mediaTitle = "media_title"
            case mediaKind = "media_kind"
            case posterUrl = "poster_url"
            case plan
            case subscriptions
            case rescueState = "rescue_state"
            case noProgressSeconds = "no_progress_seconds"
            case canReplace = "can_replace"
            case replacementDueAt = "replacement_due_at"
            case rescueMessage = "rescue_message"
        }
    }

    /// 追踪单元（电影为 0/0）——下载快照与手动选种结果共用。
    struct DownloadUnitView: Codable, Hashable, Sendable {
        var seasonNumber: Int
        var episodeNumber: Int

        enum CodingKeys: String, CodingKey {
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
        }
    }

    /// 写入下载器全局限制的请求体（读-改-写：建议先 GET 再整体提交）。
    /// - 限速 null = 取消限速，有值 = 限到该字节/秒；
    /// - 其余字段 null = 保持下载器现状不修改；
    /// - Transmission 不支持 ``max_active_torrents``，传了也会被忽略。
    struct DownloaderLimitsUpdate: Codable, Hashable, Sendable {
        var downloadLimitBytes: Int?
        var uploadLimitBytes: Int?
        var altSpeedEnabled: Bool?
        var queueEnabled: Bool?
        var maxActiveDownloads: Int?
        var maxActiveUploads: Int?
        var maxActiveTorrents: Int?

        enum CodingKeys: String, CodingKey {
            case downloadLimitBytes = "download_limit_bytes"
            case uploadLimitBytes = "upload_limit_bytes"
            case altSpeedEnabled = "alt_speed_enabled"
            case queueEnabled = "queue_enabled"
            case maxActiveDownloads = "max_active_downloads"
            case maxActiveUploads = "max_active_uploads"
            case maxActiveTorrents = "max_active_torrents"
        }
    }

    /// 下载器全局限制（实时读自下载器，不落库）。
    /// - 限速单位**字节/秒**，null = 不限速；
    /// - ``alt_speed_enabled``：备用限速档（qB Alternative Speed / Tr Turtle Mode）；
    /// - 队列上限决定同时活动的任务数，超限任务进 queued 排队——刷流做种多时
    /// 最容易撞上的墙；``max_active_torrents``（下载+做种总数上限）为
    /// qBittorrent 独有，Transmission 恒为 null。
    struct DownloaderLimitsView: Codable, Hashable, Sendable {
        var downloadLimitBytes: Int?
        var uploadLimitBytes: Int?
        var altSpeedEnabled: Bool?
        var queueEnabled: Bool?
        var maxActiveDownloads: Int?
        var maxActiveUploads: Int?
        var maxActiveTorrents: Int?

        enum CodingKeys: String, CodingKey {
            case downloadLimitBytes = "download_limit_bytes"
            case uploadLimitBytes = "upload_limit_bytes"
            case altSpeedEnabled = "alt_speed_enabled"
            case queueEnabled = "queue_enabled"
            case maxActiveDownloads = "max_active_downloads"
            case maxActiveUploads = "max_active_uploads"
            case maxActiveTorrents = "max_active_torrents"
        }
    }

    /// 新增/更新下载器的请求体（更新时 id 走路径参数）。
    /// 与站点配置同语义：更新是**全字段覆盖**，密码出于安全不回显，
    /// 编辑时需要重新填写（未填则视为该下载器无需密码）。
    struct DownloaderPayload: Codable, Hashable, Sendable {
        /// 下载器名称（全局唯一）
        var name: String
        /// 下载器类型：qbittorrent / transmission
        var clientType: API.ClientType
        /// 下载器地址，如 http://192.168.1.10:8080
        var url: String
        /// 登录用户名（未开鉴权可留空）
        var username: String?
        /// 登录密码（未开鉴权可留空）
        var password: String?
        /// 默认保存目录（留空用下载器默认）
        var savePath: String?
        /// 路径映射（movieclaw 路径 → 下载器路径，视角一致时留空）
        var pathMappings: [API.PathMapping]?
        /// 是否启用（默认启用）
        var enabled: Bool?

        enum CodingKeys: String, CodingKey {
            case name
            case clientType = "client_type"
            case url
            case username
            case password
            case savePath = "save_path"
            case pathMappings = "path_mappings"
            case enabled
        }
    }

    /// 启用/停用请求体。
    struct DownloaderStatusUpdate: Codable, Hashable, Sendable {
        /// true=启用 / false=停用（停用后不接收新的下载提交）
        var enabled: Bool

        enum CodingKeys: String, CodingKey {
            case enabled
        }
    }

    /// 下载器配置的对外视图（**脱敏**：绝不回传密码）。
    struct DownloaderView: Codable, Hashable, Sendable {
        var id: Int
        var name: String
        var clientType: API.ClientType
        var url: String
        var username: String?
        /// 提交下载时的默认保存目录
        var savePath: String?
        /// 路径映射 JSON 数组（movieclaw 路径 → 下载器路径），形如 [{"local":"/volume1/downloads","remote":"/downloads"}]
        var pathMappings: [API.PathMapping]?
        var enabled: Bool
        /// 是否为默认下载器（一键下载不选目标时投给它）
        var isDefault: Bool
        var status: API.ConfigStatus
        /// 是否可用 = 已启用且连接测试通过（status=active）
        var usable: Bool
        /// 最近一次连接成功获取的版本号
        var version: String?
        /// 最近测试失败原因（清晰中文）
        var lastError: String?
        /// 最近一次测试时间
        var lastCheckedAt: String?
        /// 路径映射体检结果；null = 尚未体检或未配置映射
        var pathHealth: [API.PathProbeView]?
        /// 路径映射是否全部可达（未体检/未配置映射视为健康，不误报）
        var pathsHealthy: Bool
        var createdAt: String
        var updatedAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case clientType = "client_type"
            case url
            case username
            case savePath = "save_path"
            case pathMappings = "path_mappings"
            case enabled
            case isDefault = "is_default"
            case status
            case usable
            case version
            case lastError = "last_error"
            case lastCheckedAt = "last_checked_at"
            case pathHealth = "path_health"
            case pathsHealthy = "paths_healthy"
            case createdAt = "created_at"
            case updatedAt = "updated_at"
        }
    }

    /// 一个多文件单元里的一个文件。
    struct DuplicateFileView: Codable, Hashable, Sendable {
        var id: Int
        var fileName: String
        /// 绝对路径（悬停显示；成员视图不返回本接口）
        var filePath: String
        /// 版本签名的质量部分：「分辨率 片源[ HDR]」
        var qualityLabel: String
        var sizeBytes: Int
        var bitRate: Int?
        var resolution: String?
        var mediaSource: String?
        var hdr: String?
        var videoCodec: String?
        /// 首条音轨「编码 声道」
        var audioLabel: String?
        var origin: API.FileOriginView
        /// 版本签名：质量标签|来源 label（整季留这个版本时传回）
        var versionKey: String
        /// 系统建议保留的那个（只是建议）
        var suggested: Bool
        /// 建议依据：档位最高 / 档位无法比较，按实测码率建议 / 同档，…
        var suggestReason: String?
        /// 用户「都留着」过；null=未标记
        var keptAt: String?

        enum CodingKeys: String, CodingKey {
            case id
            case fileName = "file_name"
            case filePath = "file_path"
            case qualityLabel = "quality_label"
            case sizeBytes = "size_bytes"
            case bitRate = "bit_rate"
            case resolution
            case mediaSource = "media_source"
            case hdr
            case videoCodec = "video_codec"
            case audioLabel = "audio_label"
            case origin
            case versionKey = "version_key"
            case suggested
            case suggestReason = "suggest_reason"
            case keptAt = "kept_at"
        }
    }

    /// 重复文件页的一次读取：扫描状态 + 分档摘要 + 本页条目。
    /// 页面落地只看前两样（``limit=0`` 时不带条目）；点进某一档才拉明细。
    struct DuplicateFilesData: Codable, Hashable, Sendable {
        var scan: API.DuplicateScanStateView
        /// 三档：放心清 / 建议清 / 要你决定
        var tiers: [API.DuplicateGroupView]
        /// 「需要你决定」按取舍类型分组，同一种取舍一次决定一批
        var reviewGroups: [API.DuplicateGroupView]
        var totalUnits: Int
        var totalFiles: Int
        var totalBytes: Int
        /// 当前筛选下有重复的条目数（分页总数）
        var totalItems: Int
        var items: [API.DuplicateItemView]

        enum CodingKeys: String, CodingKey {
            case scan
            case tiers
            case reviewGroups = "review_groups"
            case totalUnits = "total_units"
            case totalFiles = "total_files"
            case totalBytes = "total_bytes"
            case totalItems = "total_items"
            case items
        }
    }

    /// 一档、或「需要你决定」里的一组：有多少活、清掉能腾多少。
    struct DuplicateGroupView: Codable, Hashable, Sendable {
        /// safe / suggested / review；或取舍类型 resolution / hdr / …
        var key: String
        var label: String
        /// 一句话说明这一档是什么、该怎么处置
        var hint: String
        var units: Int
        /// 按建议清理会清掉几个文件
        var files: Int
        var bytes: Int

        enum CodingKeys: String, CodingKey {
            case key
            case label
            case hint
            case units
            case files
            case bytes
        }
    }

    struct DuplicateItemView: Codable, Hashable, Sendable {
        var library: API.TrashedLibraryRefView
        var mediaItem: API.TrashedItemRefView
        var seasons: [API.DuplicateSeasonView]

        enum CodingKeys: String, CodingKey {
            case library
            case mediaItem = "media_item"
            case seasons
        }
    }

    /// 一整档、或「需要你决定」里的一组，一次决定一批。
    /// 同一种取舍的几百个单元，用户其实只有一个答案（"我要 4K" / "都留着"），
    /// 所以批量的粒度是**档 / 组**而不是"全部重复文件"。
    struct DuplicateResolveAllPayload: Codable, Hashable, Sendable {
        /// safe 可以放心清理（机器确认没区别）/ suggested 建议清理（有一个档位明显更高）/ review 需要你决定
        var tier: String
        /// tier=review 时只处理这一种取舍；省略 = 整个 review 档
        var reviewKind: String?
        /// 只处理某个库；省略 = 全部库
        var libraryId: Int?
        /// true = 这一组都留着（只盖标记不动文件）；false = 按「建议保留」清理
        var keepAll: Bool?

        enum CodingKeys: String, CodingKey {
            case tier
            case reviewKind = "review_kind"
            case libraryId = "library_id"
            case keepAll = "keep_all"
        }
    }

    /// 一个单元 / 一季的决定：三选一，正好给一个。
    /// 三个字段而不是一个联合类型的 ``keep``：联合类型在 OpenAPI 生成的 CLI 里会被
    /// 压成单一标量（实测生成出 ``--keep int``），三种决定里只剩一种表达得出来。
    /// 拆开之后 JSON 与命令行都自解释：``--keep-file-id`` / ``--keep-version`` /
    /// ``--keep-all``。
    struct DuplicateResolvePayload: Codable, Hashable, Sendable {
        /// 条目 id
        var mediaItemId: Int
        /// 季号；电影为 0（哨兵）
        var seasonNumber: Int?
        /// 集号；省略 = 整季（电影传 0 或省略）
        var episodeNumber: Int?
        /// 留这个：保留该文件，单元内其余移入回收站
        var keepFileId: Int?
        /// 整季留这个版本：版本签名（列表接口的 version_key，形如「1080p Blu-ray|监听目录自动识别入库」）；没有该版本的集保留建议保留者
        var keepVersion: String?
        /// 都留着：这些版本都要，单元不再列为重复（不动文件）
        var keepAll: Bool?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case keepFileId = "keep_file_id"
            case keepVersion = "keep_version"
            case keepAll = "keep_all"
        }
    }

    /// 扫描本身的状态：扫过没有、上次什么时候、现在是不是正在跑。
    struct DuplicateScanStateView: Codable, Hashable, Sendable {
        /// null=从未扫描；queued/running/… =正在跑；succeeded=有结果
        var status: String?
        var jobId: String?
        /// 正在跑时的进度文案
        var message: String?
        var percent: Double?
        /// 上一轮扫描完成的时间
        var scannedAt: String?
        /// 洗版验证在途、暂不列出的单元数
        var upgradingUnits: Int
        /// 规则组「保留共存」、不列出的条目数
        var keepOldItems: Int

        enum CodingKeys: String, CodingKey {
            case status
            case jobId = "job_id"
            case message
            case percent
            case scannedAt = "scanned_at"
            case upgradingUnits = "upgrading_units"
            case keepOldItems = "keep_old_items"
        }
    }

    /// 一个条目的一季在某一堆里的块（一季两种都有时两堆各一块）。电影恰好一季一集。
    struct DuplicateSeasonView: Codable, Hashable, Sendable {
        var seasonNumber: Int
        var bucket: String
        /// 同构：各集版本签名一致，可整季按版本决定
        var uniform: Bool
        var versions: [API.DuplicateVersionView]
        var units: [API.DuplicateUnitView]

        enum CodingKeys: String, CodingKey {
            case seasonNumber = "season_number"
            case bucket
            case uniform
            case versions
            case units
        }
    }

    /// 一个单元（电影 = 条目；剧集 = 某季某集）。
    struct DuplicateUnitView: Codable, Hashable, Sendable {
        var seasonNumber: Int
        var episodeNumber: Int
        var bucket: String
        var files: [API.DuplicateFileView]

        enum CodingKeys: String, CodingKey {
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case bucket
            case files
        }
    }

    /// 同构季的一个版本行：这版本覆盖哪些集、多大、从哪来。
    struct DuplicateVersionView: Codable, Hashable, Sendable {
        var key: String
        var qualityLabel: String
        var originLabel: String
        var episodes: [Int]
        var bytes: Int
        var suggested: Bool

        enum CodingKeys: String, CodingKey {
            case key
            case qualityLabel = "quality_label"
            case originLabel = "origin_label"
            case episodes
            case bytes
            case suggested
        }
    }

    /// 设置页「走代理」开关列表里的一项。
    struct EgressServiceOption: Codable, Hashable, Sendable {
        var id: String
        var label: String
        var description: String

        enum CodingKeys: String, CodingKey {
            case id
            case label
            case description
        }
    }

    struct EndpointCreateRequest: Codable, Hashable, Sendable {
        /// 展示名，如「家庭影音助理」
        var name: String
        /// 地址标识，URL 末段
        var slug: String
        /// 选中的服务域
        var services: [String]
        var description: String?
        var expandTools: Bool?
        var timeoutSeconds: Int?

        enum CodingKeys: String, CodingKey {
            case name
            case slug
            case services
            case description
            case expandTools = "expand_tools"
            case timeoutSeconds = "timeout_seconds"
        }
    }

    /// 创建/轮换的响应：唯一一次带令牌明文。
    struct EndpointCreatedView: Codable, Hashable, Sendable {
        var endpoint: API.EndpointView
        /// 令牌明文，仅本次返回，服务端只存哈希
        var token: String

        enum CodingKeys: String, CodingKey {
            case endpoint
            case token
        }
    }

    /// 只更新给出的字段；地址标识建成后不可改。
    struct EndpointUpdateRequest: Codable, Hashable, Sendable {
        var name: String?
        var description: String?
        var services: [String]?
        var expandTools: Bool?
        var enabled: Bool?
        var timeoutSeconds: Int?

        enum CodingKeys: String, CodingKey {
            case name
            case description
            case services
            case expandTools = "expand_tools"
            case enabled
            case timeoutSeconds = "timeout_seconds"
        }
    }

    /// 端点的展示形态。永远不含令牌明文——它只在创建与轮换时返回一次。
    struct EndpointView: Codable, Hashable, Sendable {
        var id: String
        var slug: String
        var name: String
        var description: String
        var services: [String]
        /// 配置里存在但当前版本已没有的服务域（已忽略）
        var missingServices: [String]
        var expandTools: Bool
        var enabled: Bool
        var tokenHint: String
        var timeoutSeconds: Int
        /// 当前配置下的实际工具数
        var toolCount: Int
        /// 供客户端填写的完整地址
        var url: String
        var createdAt: String
        var lastUsedAt: String?

        enum CodingKeys: String, CodingKey {
            case id
            case slug
            case name
            case description
            case services
            case missingServices = "missing_services"
            case expandTools = "expand_tools"
            case enabled
            case tokenHint = "token_hint"
            case timeoutSeconds = "timeout_seconds"
            case toolCount = "tool_count"
            case url
            case createdAt = "created_at"
            case lastUsedAt = "last_used_at"
        }
    }

    /// 剧集分集区的一集：季集结构 + 本地分集刮削 + TMDB 兜底的合并结果。
    struct EpisodeView: Codable, Hashable, Sendable {
        var episodeNumber: Int
        var name: String?
        /// 分集简介
        var overview: String?
        var airDate: String?
        /// 分集剧照：本地缩略图接口相对路径或 TMDB 图床地址；无为 null
        var stillUrl: String?
        /// 该集有在位文件；false=缺集或文件缺失（前端置灰）
        var owned: Bool
        /// 该集的台账文件 id
        var fileIds: [Int]
        /// 当前观看者上次看到的位置（毫秒）
        var positionMs: Int
        /// 当前观看者已看完该集
        var played: Bool
        /// 观看进度 1~99；已看完由 played 表达，不给百分比
        var progressPercent: Int?

        enum CodingKeys: String, CodingKey {
            case episodeNumber = "episode_number"
            case name
            case overview
            case airDate = "air_date"
            case stillUrl = "still_url"
            case owned
            case fileIds = "file_ids"
            case positionMs = "position_ms"
            case played
            case progressPercent = "progress_percent"
        }
    }

    struct EventCatalogEntry: Codable, Hashable, Sendable {
        var event: String
        var group: String
        var label: String
        var jellyfinSupported: Bool
        var defaultOn: Bool

        enum CodingKeys: String, CodingKey {
            case event
            case group
            case label
            case jellyfinSupported = "jellyfin_supported"
            case defaultOn = "default_on"
        }
    }

    /// 供插件识别"当前站点是否被支持"的站点视图。
    struct ExtensionSiteView: Codable, Hashable, Sendable {
        var siteId: String
        var displayName: String
        /// 该站点的匹配域名（可注册域名），插件据此比对当前标签页
        var domain: String
        /// 用户是否已配置该站点
        var configured: Bool
        /// 已配置时的验证状态
        var status: API.ConfigStatus?
        /// 是否可用 = 已启用且验证通过
        var usable: Bool

        enum CodingKeys: String, CodingKey {
            case siteId = "site_id"
            case displayName = "display_name"
            case domain
            case configured
            case status
            case usable
        }
    }

    /// 筛选面板里的一个候选值（docs/design/library-filtering.md 3.3）。
    struct FacetValueView: Codable, Hashable, Sendable {
        /// 取值（类型是 TMDB genre id、地区是国家码、年代是档名）
        var value: String
        /// 展示名（类型/地区走内置映射表，未知取值原样显示）
        var label: String
        /// 在**其他维度**已选条件下勾上本值还剩几部——算本维时排除本维自身的条件，否则勾了「动画」之后其他类型全变 0，多选就废了
        var count: Int

        enum CodingKeys: String, CodingKey {
            case value
            case label
            case count
        }
    }

    /// 首页「我的收藏」的一格：单库海报墙的条目视图 + 收藏上下文。
    /// 收藏层级来自最近一次收藏的那一行：整剧两者皆 null，整季只有季号，
    /// 单集季集都有；电影恒为 null（内部 (0,0) 哨兵不外泄）。
    struct FavoriteItemView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var kind: API.MediaKind
        /// 卡片的详情落点库（同一作品跨库时取首页顺序第一个可见库）
        var libraryId: Int
        /// 身份来源：tmdb / local（local=未识别或其他库）
        var source: String
        /// TMDB 条目 ID；本地来源条目为 null
        var tmdbId: Int?
        var title: String
        var year: Int?
        var posterUrl: String?
        var backdropUrl: String?
        /// 主图宽高比（真实像素尺寸或来源惯例），卡片按它排版
        var primaryAspect: Double
        /// 内容日期：影视为上映/首播日，本地条目为拍摄/录制日（图片库按月分组与悬停日期用）
        var releaseDate: String?
        /// 评分（0~10，TMDB 或 NFO）；海报墙默认不印，悬停层与按评分排序/筛选时才显示
        var rating: Double?
        /// 主图的微缩占位图 data URI（约 300 字节）：缩略图到达前铺一层模糊色块
        var posterBlur: String?
        /// 条目的首个在位文件 id（一文件一条目的库用它取原图；多文件条目取最早入账的）
        var primaryFileId: Int?
        var fileCount: Int
        var totalSizeBytes: Int
        var seasons: [Int]
        var episodeCount: Int
        var resolutions: [String]
        /// 标记 missing 的文件数（>0 时前端提示）
        var missingCount: Int
        /// 剧集播出状态：airing=在播 / ended=完结；电影或状态未知为 NULL
        var airStatus: String?
        /// 已播出但所有媒体库里都没有的正季集数（电影恒 0）——「补齐缺集」的依据
        var missingEpisodeCount: Int
        /// 最近一次文件入账时间（首页「最近添加」排序依据）
        var addedAt: String?
        /// 当前观看者是否收藏了这部作品（海报右上角那颗心）；不认人的调用恒 False
        var isFavorite: Bool
        /// 最近一次可追溯入库批次的剧集摘要；NULL=电影或迁移前旧台账
        var recentAddition: API.LibraryRecentAdditionView?
        /// 本库在位剧集相对 TMDB 季集结构的完整度摘要；电影或无有效集号为 NULL
        var inventorySummary: API.LibraryInventorySummaryView?
        /// 在位但尚未探出介质规格的文件数——扫描补探阶段前端据此把「还在处理」的条目排到海报墙前面并点亮标记
        var probePendingCount: Int
        var favoriteSeasonNumber: Int?
        var favoriteEpisodeNumber: Int?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case kind
            case libraryId = "library_id"
            case source
            case tmdbId = "tmdb_id"
            case title
            case year
            case posterUrl = "poster_url"
            case backdropUrl = "backdrop_url"
            case primaryAspect = "primary_aspect"
            case releaseDate = "release_date"
            case rating
            case posterBlur = "poster_blur"
            case primaryFileId = "primary_file_id"
            case fileCount = "file_count"
            case totalSizeBytes = "total_size_bytes"
            case seasons
            case episodeCount = "episode_count"
            case resolutions
            case missingCount = "missing_count"
            case airStatus = "air_status"
            case missingEpisodeCount = "missing_episode_count"
            case addedAt = "added_at"
            case isFavorite = "is_favorite"
            case recentAddition = "recent_addition"
            case inventorySummary = "inventory_summary"
            case probePendingCount = "probe_pending_count"
            case favoriteSeasonNumber = "favorite_season_number"
            case favoriteEpisodeNumber = "favorite_episode_number"
        }
    }

    /// 「我的收藏」分区的数据载荷。``total`` 是去重后的收藏作品总数，
    /// ``items`` 受 limit 截断——前端据此决定要不要给「展开全部」。
    struct FavoritesView: Codable, Hashable, Sendable {
        var items: [API.FavoriteItemView]
        var total: Int

        enum CodingKeys: String, CodingKey {
            case items
            case total
        }
    }

    /// 接入飞书群自定义机器人(粘贴 Webhook 地址即绑即用,无配对码)。
    struct FeishuBindPayload: Codable, Hashable, Sendable {
        /// 飞书自定义机器人 Webhook 地址
        var webhookUrl: String
        /// 签名校验密钥;未开启签名校验留空
        var secret: String?

        enum CodingKeys: String, CodingKey {
            case webhookUrl = "webhook_url"
            case secret
        }
    }

    /// 文件来源快照（docs/design/library-duplicate-files.md §2）：这个文件是怎么进库的。
    struct FileOriginView: Codable, Hashable, Sendable {
        /// subscription / manual_download / watch_import / scan
        var kind: String
        /// 一句话：订阅《九门》自动投递 / 手动下载 / 监听目录自动识别入库 / 存量扫描发现
        var label: String
        /// 第二行：站点 · 种子标题 · 下载器 · 搬运方式
        var detail: String?

        enum CodingKeys: String, CodingKey {
            case kind
            case label
            case detail
        }
    }

    /// 修复卡里的一个可选修法。
    struct FixOptionView: Codable, Hashable, Sendable {
        /// 选项标题（如「补一条公共父目录映射」）
        var title: String
        /// 为什么这么做 / 适合谁（帮用户在多个选项间取舍）
        var why: String
        /// 具体做什么，含建议值
        var steps: String
        /// 修复去处：设置分区 id 或 libraries（前端映射到路由）
        var fixSection: String
        /// 跳转按钮文案
        var fixLabel: String
        /// 跳转预填参数（目标设置页读取后自动填表单）
        var fixParams: [String: String]?

        enum CodingKeys: String, CodingKey {
            case title
            case why
            case steps
            case fixSection = "fix_section"
            case fixLabel = "fix_label"
            case fixParams = "fix_params"
        }
    }

    /// 一次目录浏览的结果：当前位置 + 上级 + 子目录列表。
    struct FsBrowseView: Codable, Hashable, Sendable {
        /// 当前目录的绝对路径（已规范化）
        var path: String
        /// 上级目录路径；已在根目录时为 null
        var parent: String?
        /// 子目录列表（只含目录，按名称排序）
        var entries: [API.FsEntry]

        enum CodingKeys: String, CodingKey {
            case path
            case parent
            case entries
        }
    }

    /// 当前目录下的一个子目录。
    struct FsEntry: Codable, Hashable, Sendable {
        /// 目录名
        var name: String
        /// 绝对路径
        var path: String

        enum CodingKeys: String, CodingKey {
            case name
            case path
        }
    }

    /// 无法生成时的原因与下一步；前端据此渲染指导弹窗。
    struct GenPreviewBlockerView: Codable, Hashable, Sendable {
        var code: String
        var title: String
        var message: String
        var suggestions: [String]

        enum CodingKeys: String, CodingKey {
            case code
            case title
            case message
            case suggestions
        }
    }

    /// 预检还没有结论：内封轨正在后台抽取，稍后重试同一个接口即可。
    /// 与 ``blocker`` 互斥语义：blocker 说「这份片源做不了」，pending 说
    /// 「再等一会儿」。前端据此显示进度文案并轮询，而不是把用户挡在错误里。
    struct GenPreviewPendingView: Codable, Hashable, Sendable {
        /// 面向用户的等待文案
        var message: String
        /// 正在抽取的候选，如 embedded:0
        var candidateKey: String
        /// 建议的下次轮询间隔
        var retryAfterMs: Int

        enum CodingKeys: String, CodingKey {
            case message
            case candidateKey = "candidate_key"
            case retryAfterMs = "retry_after_ms"
        }
    }

    /// 发起前的确认素材：选源结果 + 成本估算。
    struct GenPreviewView: Codable, Hashable, Sendable {
        var candidates: [API.SourceCandidateView]
        var chosenKey: String?
        var selectedSourceKey: String?
        var eventCount: Int
        var estimatedTokens: Int
        var alreadyGenerated: Bool
        var warnings: [String]
        var pgsConversion: API.PgsConversionView?
        var blocker: API.GenPreviewBlockerView?
        var outputFilename: String?
        var pending: API.GenPreviewPendingView?

        enum CodingKeys: String, CodingKey {
            case candidates
            case chosenKey = "chosen_key"
            case selectedSourceKey = "selected_source_key"
            case eventCount = "event_count"
            case estimatedTokens = "estimated_tokens"
            case alreadyGenerated = "already_generated"
            case warnings
            case pgsConversion = "pgs_conversion"
            case blocker
            case outputFilename = "output_filename"
            case pending
        }
    }

    struct GenStartPayload: Codable, Hashable, Sendable {
        /// 目标语言，如 chs / eng
        var targetLanguage: String?
        /// 双语第二行语言
        var secondaryLanguage: String?
        /// 参考字幕标识，如 embedded:1
        var sourceCandidateKey: String?
        /// 确认把图片字幕识别为文本
        var convertPgs: Bool?
        /// 图片字幕原始语言
        var pgsOcrLanguage: String?

        enum CodingKeys: String, CodingKey {
            case targetLanguage = "target_language"
            case secondaryLanguage = "secondary_language"
            case sourceCandidateKey = "source_candidate_key"
            case convertPgs = "convert_pgs"
            case pgsOcrLanguage = "pgs_ocr_language"
        }
    }

    /// 人工选择种子下载：把搜索结果里的一条种子直接投给本订阅。
    /// 字段即搜索结果行（TorrentHit）原样回传——交互式搜索现算现返、不落
    /// 种子索引，只能由前端带回。attrs 同样回传（它本就是搜索链路里服务端
    /// enrich 的产物，用户按它筛选后选中了这条）；缺失时服务端重新推导兜底。
    struct GrabPayload: Codable, Hashable, Sendable {
        var siteId: String
        var torrentId: String
        var title: String
        var subtitle: String?
        /// 站点分类（movie/tv/…）
        var category: String?
        /// 搜索结果里的结构化属性（TorrentAttrs）；缺失时服务端重算
        var attrs: [String: API.JSONValue]?
        var downloadUrl: String?
        var sizeBytes: Int?
        var seeders: Int?
        var isFree: Bool?
        var hitAndRun: Bool?
        var imdbId: String?
        var doubanId: String?
        var publishTime: String?

        enum CodingKeys: String, CodingKey {
            case siteId = "site_id"
            case torrentId = "torrent_id"
            case title
            case subtitle
            case category
            case attrs
            case downloadUrl = "download_url"
            case sizeBytes = "size_bytes"
            case seeders
            case isFree = "is_free"
            case hitAndRun = "hit_and_run"
            case imdbId = "imdb_id"
            case doubanId = "douban_id"
            case publishTime = "publish_time"
        }
    }

    /// 手动选种的投递结果。
    struct GrabResultView: Codable, Hashable, Sendable {
        /// 本次投递满足的追踪单元
        var units: [API.DownloadUnitView]

        enum CodingKeys: String, CodingKey {
            case units
        }
    }

    struct HandoffPromptView: Codable, Hashable, Sendable {
        var title: String
        var prompt: String

        enum CodingKeys: String, CodingKey {
            case title
            case prompt
        }
    }

    struct HandoffRequest: Codable, Hashable, Sendable {
        var kind: String
        var ref: String

        enum CodingKeys: String, CodingKey {
            case kind
            case ref
        }
    }

    /// 订阅链路体检里的一段检查结论。
    struct HealthCheckView: Codable, Hashable, Sendable {
        /// downloader / dispatch_dir / mapping / transfer_disk / watch_active
        var key: String
        /// 段落名（如「下载器」「路径映射」）
        var label: String
        /// ok=正常 / warn=能转但降级 / error=会失败，必须修
        var status: String
        /// 中文事实陈述，直接展示
        var detail: String
        /// 修复去处：设置分区 id（sites/downloaders/import-watch）或 libraries
        var fixSection: String?

        enum CodingKeys: String, CodingKey {
            case key
            case label
            case status
            case detail
            case fixSection = "fix_section"
        }
    }

    /// 按根因聚合的问题卡：一个根因 = 一张卡，不随受影响的库数膨胀。
    struct HealthIssueView: Codable, Hashable, Sendable {
        /// 与被聚合检查项的 HealthCheck.key 同词表
        var key: String
        var status: String
        /// 一句话根因
        var title: String
        /// 根因的事实陈述与后果
        var detail: String
        /// 受影响的库名
        var affectedLibraries: [String]
        /// 结构化修复选项（多个时由用户取舍）
        var options: [API.FixOptionView]

        enum CodingKeys: String, CodingKey {
            case key
            case status
            case title
            case detail
            case affectedLibraries = "affected_libraries"
            case options
        }
    }

    struct HealthResponse: Codable, Hashable, Sendable {
        var status: String
        var service: String
        var environment: String
        var specHash: String

        enum CodingKeys: String, CodingKey {
            case status
            case service
            case environment
            case specHash = "spec_hash"
        }
    }

    /// 媒体库首页的一「行」：来源 × 排序 × 名字（docs/design/library-home-perspective.md）。
    /// - 内置行（``up-next`` / ``favorites`` / ``libraries``）只存 ``hidden``，收藏行多一个
    /// ``sort``；来源与名字由前端决定，这里不存；
    /// - 默认库行 ``lib:<library_id>`` 每库一条，能藏、能改排序和名字，不能删；
    /// - 自加行 ``row:<slug>`` 必须且只能带 ``library_id`` 或 ``collection_id`` 之一。
    /// 除 ``id`` 外全部可空：空即默认（排序用预设、名字跟随推荐、不隐藏）。
    /// 坏形状在 PUT 时就拒掉，读取端不再兜底——与 ``NavUiPrefs`` 一样，存下来的
    /// 只是提示：指向已删库 / 不可见合集的行由前端合并时静默丢弃。
    struct HomeRowPref: Codable, Hashable, Sendable {
        /// 行 id，见类注释的四种形状
        var id: String
        /// 排序档；空 = 该行的默认排序
        var sort: String?
        /// 排序方向 asc / desc；空 = 该档的自然方向。前端只在反转自然方向时才存它
        var order: String?
        /// 用户起的名字；空 = 跟随推荐
        var name: String?
        /// 只显示没看过的（仅库行）
        var unwatched: Bool?
        /// 隐藏这一行，位置保留
        var hidden: Bool?
        /// 自加库行的来源库
        var libraryId: Int?
        /// 合集行的来源合集
        var collectionId: Int?

        enum CodingKeys: String, CodingKey {
            case id
            case sort
            case order
            case name
            case unwatched
            case hidden
            case libraryId = "library_id"
            case collectionId = "collection_id"
        }
    }

    /// 媒体库首页的一「行」：来源 × 排序 × 名字（docs/design/library-home-perspective.md）。
    /// - 内置行（``up-next`` / ``favorites`` / ``libraries``）只存 ``hidden``，收藏行多一个
    /// ``sort``；来源与名字由前端决定，这里不存；
    /// - 默认库行 ``lib:<library_id>`` 每库一条，能藏、能改排序和名字，不能删；
    /// - 自加行 ``row:<slug>`` 必须且只能带 ``library_id`` 或 ``collection_id`` 之一。
    /// 除 ``id`` 外全部可空：空即默认（排序用预设、名字跟随推荐、不隐藏）。
    /// 坏形状在 PUT 时就拒掉，读取端不再兜底——与 ``NavUiPrefs`` 一样，存下来的
    /// 只是提示：指向已删库 / 不可见合集的行由前端合并时静默丢弃。
    struct HomeRowPrefInput: Codable, Hashable, Sendable {
        /// 行 id，见类注释的四种形状
        var id: String
        /// 排序档；空 = 该行的默认排序
        var sort: String?
        /// 排序方向 asc / desc；空 = 该档的自然方向。前端只在反转自然方向时才存它
        var order: String?
        /// 用户起的名字；空 = 跟随推荐
        var name: String?
        /// 只显示没看过的（仅库行）
        var unwatched: Bool?
        /// 隐藏这一行，位置保留
        var hidden: Bool?
        /// 自加库行的来源库
        var libraryId: Int?
        /// 合集行的来源合集
        var collectionId: Int?

        enum CodingKeys: String, CodingKey {
            case id
            case sort
            case order
            case name
            case unwatched
            case hidden
            case libraryId = "library_id"
            case collectionId = "collection_id"
        }
    }

    /// 媒体库首页的行清单（每个成员一份，超管走全局域）。
    /// 空列表 = 出厂布局；合并规则（存过的按存的顺序、没存过的内置行与每库默认行追加
    /// 在后、认不出的 id 忽略）在前端 ``lib/home-rows.ts``。
    /// 上限 128 只是防脏数据的安全阀：自定义页会把合并后的整份清单存回来（三个内置行 +
    /// 每个可见库一条 + 自加的行），上限必须留得比"家里有很多库"大得多。
    struct HomeUiPrefs: Codable, Hashable, Sendable {
        /// 首页的行，按显示顺序；空 = 出厂布局
        var rows: [API.HomeRowPref]

        enum CodingKeys: String, CodingKey {
            case rows
        }
    }

    /// 媒体库首页的行清单（每个成员一份，超管走全局域）。
    /// 空列表 = 出厂布局；合并规则（存过的按存的顺序、没存过的内置行与每库默认行追加
    /// 在后、认不出的 id 忽略）在前端 ``lib/home-rows.ts``。
    /// 上限 128 只是防脏数据的安全阀：自定义页会把合并后的整份清单存回来（三个内置行 +
    /// 每个可见库一条 + 自加的行），上限必须留得比"家里有很多库"大得多。
    struct HomeUiPrefsInput: Codable, Hashable, Sendable {
        /// 首页的行，按显示顺序；空 = 出厂布局
        var rows: [API.HomeRowPrefInput]?

        enum CodingKeys: String, CodingKey {
            case rows
        }
    }

    /// 一个硬件加速后端的自检结论。``detail`` 是给用户看的中文原因与修法。
    struct HwBackendStatusView: Codable, Hashable, Sendable {
        var name: String
        var label: String
        var available: Bool
        var detail: String

        enum CodingKeys: String, CodingKey {
            case name
            case label
            case available
            case detail
        }
    }

    /// 硬件加速自检结果。
    /// `available` 为空即「只能软件转码」——用户据此决定是去挂设备，还是接受
    /// 软件转码的代价。
    struct HwProbeView: Codable, Hashable, Sendable {
        var backends: [API.HwBackendStatusView]
        var hardwareAvailable: Bool

        enum CodingKeys: String, CodingKey {
            case backends
            case hardwareAvailable = "hardware_available"
        }
    }

    /// 身份复核的明确决策，避免调用方猜测布尔值含义。
    typealias IdentityReviewDecision = String
    // 取值：'accept_suggestion', 'keep_current'

    /// 已绑定的 TG/Discord bot 账号(绑定页列表项)。
    struct ImAccountView: Codable, Hashable, Sendable {
        var channelId: String
        var accountId: String
        var boundUserId: String?
        var status: String
        var running: Bool
        var lastError: String?
        var boundAt: String

        enum CodingKeys: String, CodingKey {
            case channelId = "channel_id"
            case accountId = "account_id"
            case boundUserId = "bound_user_id"
            case status
            case running
            case lastError = "last_error"
            case boundAt = "bound_at"
        }
    }

    /// 发起绑定:提交 bot token。
    struct ImBindTokenPayload: Codable, Hashable, Sendable {
        /// bot token
        var token: String

        enum CodingKeys: String, CodingKey {
            case token
        }
    }

    /// 配对绑定状态(发起返回 + 前端 poll 同一结构)。
    /// status 取值:pending / confirmed / expired / failed。
    struct ImBindingView: Codable, Hashable, Sendable {
        var challengeId: String
        var status: String
        var pairCode: String
        var botName: String
        var message: String
        var account: API.ImAccountView?

        enum CodingKeys: String, CodingKey {
            case challengeId = "challenge_id"
            case status
            case pairCode = "pair_code"
            case botName = "bot_name"
            case message
            case account
        }
    }

    /// 图片输入。三种形态（docs/design/agent-image-input.md）：
    /// - ``url``：http(s) 直链，原样发给供应商；
    /// - ``data`` + ``media_type``：base64 内联，仅存在于发请求前的内存消息里；
    /// - ``attachment_id``：服务端附件引用（会话 assets 目录下的 id）。**转录里
    /// 只存引用**，发请求前由 API 层水合成 data；传输层遇到「只有引用、没有
    /// data/url」的块会降级为占位文本，绝不把内部引用发给供应商。
    struct ImagePart: Codable, Hashable, Sendable {
        var type: String
        var url: String?
        var data: String?
        var mediaType: String?
        var attachmentId: String?
        var name: String?

        enum CodingKeys: String, CodingKey {
            case type
            case url
            case data
            case mediaType = "media_type"
            case attachmentId = "attachment_id"
            case name
        }
    }

    /// 创建/更新监听导入规则的请求体。
    /// 目标三态：``library_id`` 指定库；``target_path`` 自定义目录（movie/tv
    /// 识别改名、video 原样搬运后落该目录，不进任何媒体库——整理结果需外部
    /// 流转再进库的场景，此时 ``kind`` 必填）；两者都为 null 即**自动路由**（识别出作品后按各库收藏
    /// 范围选库，``kind`` 同样必填）。``library_id`` 与 ``target_path`` 互斥。
    struct ImportWatchPayload: Codable, Hashable, Sendable {
        /// 源目录（绝对路径，不得与任何库根路径重叠）
        var sourcePath: String
        /// 搬运策略：hardlink（零占用需与落点同盘）/ copy（可跨盘）
        var strategy: String
        /// 目标媒体库；null=自动路由或自定义目录
        var libraryId: Int?
        /// 自定义目录目标（绝对路径，不得与库根/监听源重叠）；与 library_id 互斥
        var targetPath: String?
        /// 自动路由/自定义目录的媒体类型；指定库时忽略（video：不识别不改名，自动路由落默认其他库、自定义目录原样落该目录）
        var kind: String?
        /// 是否整理源目录里已有的存量内容；false=跳过存量只处理新增（存量条目在首轮巡检被标记为已忽略，清单中可恢复）
        var processExisting: Bool?

        enum CodingKeys: String, CodingKey {
            case sourcePath = "source_path"
            case strategy
            case libraryId = "library_id"
            case targetPath = "target_path"
            case kind
            case processExisting = "process_existing"
        }
    }

    /// 一条监听导入规则（带目标展示信息）。
    struct ImportWatchView: Codable, Hashable, Sendable {
        var id: Int
        var sourcePath: String
        var strategy: String
        /// null=自动路由或自定义目录
        var libraryId: Int?
        var libraryName: String?
        /// 自定义目录目标（其余目标为 null）
        var targetPath: String?
        /// 自动路由/自定义目录的媒体类型（指定库时为 null）
        var kind: String?
        /// 目标展示名：库名 /「自动路由（电影/剧集）」/「自定义目录 …」
        var targetLabel: String
        /// 是否整理存量（false=跳过存量只处理新增）
        var processExisting: Bool
        /// 台账状态计数（imported/pending/failed/skipped/ignored → 条目数）
        var stats: [String: Int]
        /// 已入库条目累计入库的文件数（剧集一条目是一个季包，条目数说不清入了几集）
        var importedFiles: Int
        /// 已入库的作品数（同一部剧的多季、多版本合并为一部）——「已入库」展示的就是这个数，stats.imported 是记账条目数
        var importedWorks: Int
        var createdAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case sourcePath = "source_path"
            case strategy
            case libraryId = "library_id"
            case libraryName = "library_name"
            case targetPath = "target_path"
            case kind
            case targetLabel = "target_label"
            case processExisting = "process_existing"
            case stats
            case importedFiles = "imported_files"
            case importedWorks = "imported_works"
            case createdAt = "created_at"
        }
    }

    /// 一条规则的台账清单 + 各状态计数。
    struct IngestEntriesView: Codable, Hashable, Sendable {
        var counts: [String: Int]
        var entries: [API.IngestEntryView]

        enum CodingKeys: String, CodingKey {
            case counts
            case entries
        }
    }

    /// 监听目录一个条目的处理台账行（清单展示）。
    struct IngestEntryView: Codable, Hashable, Sendable {
        var id: Int
        /// 条目名（源目录顶层的文件/目录名）
        var name: String
        var entryPath: String
        var status: String
        /// 处理结论（中文）
        var message: String?
        var importedCount: Int
        var attemptedAt: String
        /// 电影合集里识别不出、待逐个认领的视频（条目内相对路径）；普通条目为空
        var unresolvedFiles: [String]

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case entryPath = "entry_path"
            case status
            case message
            case importedCount = "imported_count"
            case attemptedAt = "attempted_at"
            case unresolvedFiles = "unresolved_files"
        }
    }

    /// 作品详情页那一行「合集」的一项：只要名字和落点。
    /// **刻意不带封面与成员数**。合集封面是从成员海报里借的——在《千与千寻》
    /// 的页面上摆「日本动画」的封面卡，那张图很可能就是《千与千寻》自己；
    /// 而成员数属于合集卡片，这一行回答的是"它在哪儿"，不是"那儿有多大"。
    struct ItemCollectionRef: Codable, Hashable, Sendable {
        var id: Int
        var name: String

        enum CodingKeys: String, CodingKey {
            case id
            case name
        }
    }

    /// 条目真实删除的结论。
    struct ItemDeleteResultView: Codable, Hashable, Sendable {
        /// 实际从磁盘删除的目录/文件
        var removedPaths: [String]
        var rowsDeleted: Int
        var freedBytes: Int
        var errors: [String]

        enum CodingKeys: String, CodingKey {
            case removedPaths = "removed_paths"
            case rowsDeleted = "rows_deleted"
            case freedBytes = "freed_bytes"
            case errors
        }
    }

    struct JobCancelView: Codable, Hashable, Sendable {
        var cancelled: Bool
        var job: API.JobView

        enum CodingKeys: String, CodingKey {
            case cancelled
            case job
        }
    }

    struct JobDismissAllRequest: Codable, Hashable, Sendable {
        /// 只忽略该类型；留空表示全部
        var jobType: String?

        enum CodingKeys: String, CodingKey {
            case jobType = "job_type"
        }
    }

    struct JobDismissAllView: Codable, Hashable, Sendable {
        /// 本次忽略的任务条数
        var dismissed: Int
        var jobs: [API.JobView]

        enum CodingKeys: String, CodingKey {
            case dismissed
            case jobs
        }
    }

    struct JobDismissRequest: Codable, Hashable, Sendable {
        /// 同时静音自动来源：不再自动为该对象重建同类任务。只对有自动来源的任务类型有效（当前为字幕自动生成）；手动触发不受影响
        var muteSource: Bool?

        enum CodingKeys: String, CodingKey {
            case muteSource = "mute_source"
        }
    }

    struct JobDismissView: Codable, Hashable, Sendable {
        /// 本次是否真的改变了状态；重复忽略为 false
        var dismissed: Bool
        /// 是否落了自动来源静音记录
        var muted: Bool
        var job: API.JobView

        enum CodingKeys: String, CodingKey {
            case dismissed
            case muted
            case job
        }
    }

    struct JobEventListView: Codable, Hashable, Sendable {
        var items: [API.JobEventView]

        enum CodingKeys: String, CodingKey {
            case items
        }
    }

    struct JobEventView: Codable, Hashable, Sendable {
        var id: Int
        var jobId: String
        var revision: Int
        var eventType: String
        var payload: [String: API.JSONValue]
        var createdAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case jobId = "job_id"
            case revision
            case eventType = "event_type"
            case payload
            case createdAt = "created_at"
        }
    }

    struct JobListView: Codable, Hashable, Sendable {
        var items: [API.JobView]

        enum CodingKeys: String, CodingKey {
            case items
        }
    }

    struct JobProgressView: Codable, Hashable, Sendable {
        /// determinate / indeterminate / waiting / paused
        var mode: String
        /// 当前领域阶段的稳定标识
        var phase: String
        /// 面向用户的即时进展
        var message: String
        /// 当前完成量
        var current: Int?
        /// 总量；未知时为空
        var total: Int?
        /// 真实可计算时才返回百分比
        var percent: Double?
        /// 当前阶段序号
        var phaseIndex: Int?
        /// 总阶段数
        var phaseCount: Int?
        /// 领域进度明细
        var details: [String: API.JSONValue]

        enum CodingKeys: String, CodingKey {
            case mode
            case phase
            case message
            case current
            case total
            case percent
            case phaseIndex = "phase_index"
            case phaseCount = "phase_count"
            case details
        }
    }

    struct JobResourceView: Codable, Hashable, Sendable {
        var resourceType: String
        var resourceId: String
        var relation: String

        enum CodingKeys: String, CodingKey {
            case resourceType = "resource_type"
            case resourceId = "resource_id"
            case relation
        }
    }

    struct JobRetryView: Codable, Hashable, Sendable {
        var created: Bool
        var job: API.JobView

        enum CodingKeys: String, CodingKey {
            case created
            case job
        }
    }

    /// 用户可见后台任务的持久化状态。
    /// 状态值是 API、CLI、前端和执行器共同依赖的稳定协议，不能拿临时协程状态
    /// 代替。``retry_wait`` 表示系统会自动重试，``blocked`` 表示必须由用户修复
    /// 配置或补充输入；二者都不是普通失败。
    /// 「用户忽略」刻意**不做成状态**：忽略只表达"我不打算处理了"，不改写
    /// "这件事失败过"这个事实。日志溯源、重试链（``retry_of_job_id``）与终态
    /// 集合都建立在 status 上，多一个终态会让每一处判定都要跟着改。忽略因此
    /// 记在 ``dismissed_at`` 这一维上，与 status 正交。
    typealias JobStatus = String
    // 取值：'queued', 'running', 'retry_wait', 'cancelling', 'waiting', 'blocked', 'succeeded', 'failed', 'cancelled'

    struct JobView: Codable, Hashable, Sendable {
        var id: String
        var jobType: String
        var subject: String?
        var definitionVersion: Int
        var handlerRevision: String
        var promptRevision: String?
        var providerRef: String?
        var status: API.JobStatus
        /// 已确认的任务输入（永不包含密钥）
        var inputData: [String: API.JSONValue]
        var progress: API.JobProgressView
        var result: [String: API.JSONValue]?
        var error: [String: API.JSONValue]?
        var usage: [String: API.JSONValue]
        var resources: [API.JobResourceView]
        var origin: String
        var actorKind: String?
        var actorName: String?
        var parentJobId: String?
        var rootJobId: String?
        var retryOfJobId: String?
        var attempt: Int
        var maxAttempts: Int
        var revision: Int
        var cancelRequestedAt: String?
        /// 取消发起方；system: 前缀代表系统自动收口，前端据此隐藏「重新执行」
        var cancelRequestedBy: String?
        /// 用户忽略的时间；非空表示不再计入「需要处理」，但任务本身仍是失败
        var dismissedAt: String?
        /// 忽略操作者
        var dismissedBy: String?
        var createdAt: String
        var updatedAt: String
        var startedAt: String?
        var finishedAt: String?

        enum CodingKeys: String, CodingKey {
            case id
            case jobType = "job_type"
            case subject
            case definitionVersion = "definition_version"
            case handlerRevision = "handler_revision"
            case promptRevision = "prompt_revision"
            case providerRef = "provider_ref"
            case status
            case inputData = "input_data"
            case progress
            case result
            case error
            case usage
            case resources
            case origin
            case actorKind = "actor_kind"
            case actorName = "actor_name"
            case parentJobId = "parent_job_id"
            case rootJobId = "root_job_id"
            case retryOfJobId = "retry_of_job_id"
            case attempt
            case maxAttempts = "max_attempts"
            case revision
            case cancelRequestedAt = "cancel_requested_at"
            case cancelRequestedBy = "cancel_requested_by"
            case dismissedAt = "dismissed_at"
            case dismissedBy = "dismissed_by"
            case createdAt = "created_at"
            case updatedAt = "updated_at"
            case startedAt = "started_at"
            case finishedAt = "finished_at"
        }
    }

    struct JobWaitView: Codable, Hashable, Sendable {
        var changed: Bool
        var job: API.JobView

        enum CodingKeys: String, CodingKey {
            case changed
            case job
        }
    }

    struct JobWorkerHealthView: Codable, Hashable, Sendable {
        var running: Bool
        var owner: String?
        var activeWorkers: Int
        var counts: [String: Int]
        var oldestQueuedSeconds: Int?

        enum CodingKeys: String, CodingKey {
            case running
            case owner
            case activeWorkers = "active_workers"
            case counts
            case oldestQueuedSeconds = "oldest_queued_seconds"
        }
    }

    struct LanguageOption: Codable, Hashable, Sendable {
        /// ISO 639-1 语言码（zh / en / ja …）
        var code: String
        /// 该语言的本族名（TMDB name，缺失回落英文名）
        var name: String
        var englishName: String

        enum CodingKeys: String, CodingKey {
            case code
            case name
            case englishName = "english_name"
        }
    }

    /// 上一次容器异常退出的记录（entrypoint 落盘 updates/state/last-exit.json）。
    /// 容器被 Docker 自动拉起后，用户需要知道曾发生过什么——无人值守的自愈
    /// 不能悄无声息。正常停机与设置页/更新触发的重启不会产生该记录。
    struct LastAbnormalExitView: Codable, Hashable, Sendable {
        var at: Int
        var reason: String
        var exitCode: Int
        var detail: String

        enum CodingKeys: String, CodingKey {
            case at
            case reason
            case exitCode = "exit_code"
            case detail
        }
    }

    /// 最近一次整理的结论——给用户"整理完成了什么"的反馈。
    struct LastOrganizeView: Codable, Hashable, Sendable {
        var finishedAt: String
        /// 改名归位的主文件数
        var renamed: Int
        /// 跟随改名的附属文件数（字幕、分集剧照等）
        var sidecarsRenamed: Int
        /// 跟随条目目录改名的镜像资产数（海报/背景/季海报/条目 NFO）
        var entryAssetsMoved: Int
        /// 本就符合规范、无需动作的文件数
        var alreadyOk: Int
        /// 计划阶段跳过的文件数（原因见预览）
        var skipped: Int
        /// 搬空后清理掉的目录数
        var removedDirs: Int
        var errors: [String]

        enum CodingKeys: String, CodingKey {
            case finishedAt = "finished_at"
            case renamed
            case sidecarsRenamed = "sidecars_renamed"
            case entryAssetsMoved = "entry_assets_moved"
            case alreadyOk = "already_ok"
            case skipped
            case removedDirs = "removed_dirs"
            case errors
        }
    }

    /// 最近一次扫描的结论——扫描常毫秒级结束，前端靠它给用户"点了有反应"的反馈。
    struct LastScanView: Codable, Hashable, Sendable {
        var finishedAt: String
        /// 本轮新入账文件数
        var scanned: Int
        var identified: Int
        var unidentified: Int
        /// 本轮标记丢失的文件数
        var markedMissing: Int
        /// 本轮自动清理出台账的丢失记录数（库开了自动清理才非 0）
        var clearedMissing: Int
        /// 本轮因已移除根路径而标记缺失的旧台账数
        var removedRootMarkedMissing: Int
        /// 本轮因已移除根路径而自动清理的旧台账数
        var removedRootCleared: Int
        /// 本轮已移除根路径台账的身份冲突数（需人工处理）
        var removedRootConflicts: Int
        /// 疑似写入中暂缓入账的文件数（稍后自动补扫）
        var deferred: Int
        /// 识别重试数：在位但待识别的文件重走识别链（不算新入账）
        var retried: Int
        /// 本轮扫描被用户手动停止（未扫完）
        var cancelled: Bool
        var errors: [String]

        enum CodingKeys: String, CodingKey {
            case finishedAt = "finished_at"
            case scanned
            case identified
            case unidentified
            case markedMissing = "marked_missing"
            case clearedMissing = "cleared_missing"
            case removedRootMarkedMissing = "removed_root_marked_missing"
            case removedRootCleared = "removed_root_cleared"
            case removedRootConflicts = "removed_root_conflicts"
            case deferred
            case retried
            case cancelled
            case errors
        }
    }

    /// 库的能力位（docs/design/library-other-kind.md 3.1）：前端按位显隐功能，
    /// 不按 kind 字面分叉——新增类型/来源时前端零改动。
    struct LibraryCapabilitiesView: Codable, Hashable, Sendable {
        /// 有外部刮削链（识别/刷新元数据/选图/待识别清单）
        var scraped: Bool
        /// 有季集结构（分集区、缺集统计）
        var episodic: Bool
        /// 有规范命名（整理功能）
        var naming: Bool
        /// 可作为订阅入库目标
        var subscribable: Bool
        /// 向媒体目录写 NFO/图片镜像
        var writeNfo: Bool
        /// 卡片主图默认宽高比（无真实尺寸时）
        var defaultAspect: Double
        /// Jellyfin 视图类型：movies / tvshows / homevideos / photos
        var jellyfinCollection: String
        /// 条目可播放；假 = 只可查看（图片库：点击开灯箱而非播放器）
        var playable: Bool

        enum CodingKeys: String, CodingKey {
            case scraped
            case episodic
            case naming
            case subscribable
            case writeNfo = "write_nfo"
            case defaultAspect = "default_aspect"
            case jellyfinCollection = "jellyfin_collection"
            case playable
        }
    }

    /// 一次筛选下的全部候选值与计数。
    /// 与 /items 共用同一组筛选参数，因此两者口径天然一致：面板上显示多少部，
    /// 点下去墙上就是多少部。为 0 的候选值仍然返回（前端置灰不可点），
    /// 这是"永不空货架"的第一道闸。
    struct LibraryFacetsView: Codable, Hashable, Sendable {
        /// 当前条件下的命中总数
        var total: Int
        /// 类型，按数量倒序
        var genres: [API.FacetValueView]
        /// 地区，按数量倒序
        var countries: [API.FacetValueView]
        /// 年代，按时间倒序
        var decades: [API.FacetValueView]
        /// 观看状态：未看/在看/已看完是一个划分，另加我收藏的
        var watch: [API.FacetValueView]
        /// 评分档（找片）
        var ratings: [API.FacetValueView]
        /// 片长档（找片）
        var runtimes: [API.FacetValueView]
        /// 原始语言（找片）
        var languages: [API.FacetValueView]
        /// 分辨率（查库）
        var resolutions: [API.FacetValueView]
        /// 动态范围（查库）
        var hdr: [API.FacetValueView]
        /// 库存状态（查库）
        var stock: [API.FacetValueView]

        enum CodingKeys: String, CodingKey {
            case total
            case genres
            case countries
            case decades
            case watch
            case ratings
            case runtimes
            case languages
            case resolutions
            case hdr
            case stock
        }
    }

    /// 条目详情页的一个物理文件（一个版本 / 一集）。
    struct LibraryFileView: Codable, Hashable, Sendable {
        var id: Int
        var filePath: String
        var fileName: String
        var sizeBytes: Int
        var container: String?
        var resolution: String?
        var videoCodec: String?
        var hdr: String?
        var bitDepth: Int?
        var durationSeconds: Int?
        var bitRate: Int?
        var frameRate: Double?
        var colorSpace: String?
        var mediaSource: String?
        /// 片源为人工标注（含 user-lowest 哨兵）
        var mediaSourceManual: Bool
        var releaseGroup: String?
        /// imported（入库管线）/ scanned（存量扫描）
        var source: String
        var seasonNumber: Int
        var episodeNumber: Int
        /// 文件当前不在磁盘（missing 标记）
        var missing: Bool
        /// 生命周期：in_place 在位 / missing 缺失 / trashed 待回收
        var state: String
        /// 待回收的预计自动清理时间；null 且 trashed = 做种保护，不自动删
        var purgeAfter: String?
        /// 待回收原因（中文整句，含触发方），文件区直接展示
        var trashNote: String?
        /// 来源快照：这个文件是怎么进库的
        var origin: API.FileOriginView
        /// 用户在重复文件页点过「都留着」的时间；null=未标记
        var keptAt: String?
        /// 音轨列表；null=尚未探测（ffprobe 缺失或文件不可达）
        var audioStreams: [API.AudioStreamView]?
        /// 字幕列表：内封轨 + 外挂文件
        var subtitleStreams: [API.SubtitleStreamView]
        /// 有效章节列表（内嵌或按时长合成）；null=尚未探测
        var chapters: [API.ChapterView]?
        var addedAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case filePath = "file_path"
            case fileName = "file_name"
            case sizeBytes = "size_bytes"
            case container
            case resolution
            case videoCodec = "video_codec"
            case hdr
            case bitDepth = "bit_depth"
            case durationSeconds = "duration_seconds"
            case bitRate = "bit_rate"
            case frameRate = "frame_rate"
            case colorSpace = "color_space"
            case mediaSource = "media_source"
            case mediaSourceManual = "media_source_manual"
            case releaseGroup = "release_group"
            case source
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case missing
            case state
            case purgeAfter = "purge_after"
            case trashNote = "trash_note"
            case origin
            case keptAt = "kept_at"
            case audioStreams = "audio_streams"
            case subtitleStreams = "subtitle_streams"
            case chapters
            case addedAt = "added_at"
        }
    }

    /// 图廊按条目分的一组（一部作品的全部图），墙上是一段标题 + 一面瀑布流。
    struct LibraryGalleryGroupView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        /// 这一组的详情落点库：段标题与灯箱「前往详情」的地址按它拼。单库图廊恒等于本库；「我的收藏」的图廊跨库，每组各带自己的落点库
        var libraryId: Int
        var kind: API.MediaKind
        var title: String
        var year: Int?
        /// 当前观看者是否收藏了这部作品：瓦片右上角的心形角标与灯箱里那颗心的初始态
        var isFavorite: Bool
        var images: [API.LibraryGalleryImageView]

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case libraryId = "library_id"
            case kind
            case title
            case year
            case isFavorite = "is_favorite"
            case images
        }
    }

    /// 图廊里的一张图：条目的海报 / 剧照 / 分集剧照 / 章节场景图之一。
    /// 影视库与其他库的「图床浏览模式」把这些图铺平成一面瀑布流墙，灯箱里
    /// 除了看图还能一键进条目详情、从这一帧起播——所以每张图都带着它在
    /// 作品里的坐标（季集号 + 起播秒数），前端拼播放地址时不必再查详情。
    struct LibraryGalleryImageView: Codable, Hashable, Sendable {
        /// poster=海报 / backdrop=横幅剧照 / still=分集剧照 / chapter=章节场景图
        var kind: String
        /// 图片地址：本地资产相对路径或 TMDB 图床绝对地址（前端走缓存代理）
        var url: String
        /// 宽高比：海报按真实像素或 2:3 惯例，剧照与场景图 16:9
        var aspect: Double
        /// 角标文案：海报 / 剧照 / 第 N 集 / 章节标题
        var label: String
        /// 分集剧照与剧集章节图所属的季号
        var season: Int?
        /// 分集剧照与剧集章节图所属的集号
        var episode: Int?
        /// 章节场景图对应的起播秒数（「从此处播放」）；其它图为 null
        var tSeconds: Double?

        enum CodingKeys: String, CodingKey {
            case kind
            case url
            case aspect
            case label
            case season
            case episode
            case tSeconds = "t_seconds"
        }
    }

    /// 海报墙 A-Z 索引条的一档（按标题排序下的首字母分组）。
    struct LibraryIndexEntryView: Codable, Hashable, Sendable {
        /// 首字母档：A-Z，落不进的（数字/符号/假名等）为 #
        var initial: String
        /// 该档的条目数
        var count: Int
        /// 该档第一格在按标题排序中的位置——即 /items?sort=title&offset= 的取值
        var offset: Int

        enum CodingKeys: String, CodingKey {
            case initial
            case count
            case offset
        }
    }

    /// 剧集库海报 hover 的在位库存完整度摘要。
    struct LibraryInventorySummaryView: Codable, Hashable, Sendable {
        /// 在位正季数；仅特别篇时为 0
        var seasonCount: Int
        /// 摘要覆盖的在位去重集数
        var episodeCount: Int
        /// 只覆盖一季时的季号（0=特别篇）；多季为 NULL
        var seasonNumber: Int?
        /// 摘要所覆盖季的 TMDB 已知总集数；任一季未知时为 NULL
        var totalEpisodeCount: Int?
        /// 是否覆盖 TMDB 已知的全部正季
        var allSeasonsOwned: Bool
        /// 摘要所覆盖的每一季是否都已收齐
        var allEpisodesOwned: Bool

        enum CodingKeys: String, CodingKey {
            case seasonCount = "season_count"
            case episodeCount = "episode_count"
            case seasonNumber = "season_number"
            case totalEpisodeCount = "total_episode_count"
            case allSeasonsOwned = "all_seasons_owned"
            case allEpisodesOwned = "all_episodes_owned"
        }
    }

    /// 条目详情页的完整数据：基本信息 + 本地刮削元数据 + 逐文件真实规格。
    /// 图片优先级：条目目录里的本地美术图（poster.jpg/fanart.jpg，走
    /// /libraries/.../artwork 接口的相对路径）优先，其次 TMDB 图床绝对地址
    /// ——前端按"是否 http 开头"区分两种加载方式。
    struct LibraryItemDetailView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var kind: API.MediaKind
        /// 身份来源：tmdb / local（local=未识别或其他库）
        var source: String
        /// TMDB 条目 ID；本地来源条目为 null
        var tmdbId: Int?
        var imdbId: String?
        var doubanId: String?
        var title: String
        var originalTitle: String
        var year: Int?
        var posterUrl: String?
        var backdropUrl: String?
        /// 主图宽高比（同海报墙）
        var primaryAspect: Double
        /// NFO 本地刮削元数据；目录里没有可用 NFO 时为 null
        var localMeta: API.LocalMetaView?
        /// 条目在磁盘上的目录（删除确认时展示）
        var entryDirs: [String]
        var files: [API.LibraryFileView]
        var fileCount: Int
        var totalSizeBytes: Int
        /// 季号列表（电影为空）
        var seasons: [Int]
        /// 该条目正在后台刮削元数据
        var scraping: Bool
        /// 刮削当前阶段；没在刮为 null
        var scrapingPhase: String?
        /// 章节场景图正在后台生成
        var chaptersPending: Bool
        /// 所属作品系列名；不属于任何系列为 null
        var seriesName: String?
        /// 所属系列合集的 id；本库没生成该合集时为 null
        var seriesCollectionId: Int?
        /// 这部片所属的合集（不含系列与「我的收藏」）
        var collections: [API.ItemCollectionRef]

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case kind
            case source
            case tmdbId = "tmdb_id"
            case imdbId = "imdb_id"
            case doubanId = "douban_id"
            case title
            case originalTitle = "original_title"
            case year
            case posterUrl = "poster_url"
            case backdropUrl = "backdrop_url"
            case primaryAspect = "primary_aspect"
            case localMeta = "local_meta"
            case entryDirs = "entry_dirs"
            case files
            case fileCount = "file_count"
            case totalSizeBytes = "total_size_bytes"
            case seasons
            case scraping
            case scrapingPhase = "scraping_phase"
            case chaptersPending = "chapters_pending"
            case seriesName = "series_name"
            case seriesCollectionId = "series_collection_id"
            case collections
        }
    }

    /// 库内一个媒体条目的库存聚合（单库海报墙的一格）。
    struct LibraryItemView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var kind: API.MediaKind
        /// 所属库；单库墙上恒为该库
        var libraryId: Int?
        /// 身份来源：tmdb / local（local=未识别或其他库）
        var source: String
        /// TMDB 条目 ID；本地来源条目为 null
        var tmdbId: Int?
        var title: String
        var year: Int?
        var posterUrl: String?
        var backdropUrl: String?
        /// 主图宽高比（真实像素尺寸或来源惯例），卡片按它排版
        var primaryAspect: Double
        /// 内容日期：影视为上映/首播日，本地条目为拍摄/录制日（图片库按月分组与悬停日期用）
        var releaseDate: String?
        /// 评分（0~10，TMDB 或 NFO）；海报墙默认不印，悬停层与按评分排序/筛选时才显示
        var rating: Double?
        /// 主图的微缩占位图 data URI（约 300 字节）：缩略图到达前铺一层模糊色块
        var posterBlur: String?
        /// 条目的首个在位文件 id（一文件一条目的库用它取原图；多文件条目取最早入账的）
        var primaryFileId: Int?
        var fileCount: Int
        var totalSizeBytes: Int
        var seasons: [Int]
        var episodeCount: Int
        var resolutions: [String]
        /// 标记 missing 的文件数（>0 时前端提示）
        var missingCount: Int
        /// 剧集播出状态：airing=在播 / ended=完结；电影或状态未知为 NULL
        var airStatus: String?
        /// 已播出但所有媒体库里都没有的正季集数（电影恒 0）——「补齐缺集」的依据
        var missingEpisodeCount: Int
        /// 最近一次文件入账时间（首页「最近添加」排序依据）
        var addedAt: String?
        /// 当前观看者是否收藏了这部作品（海报右上角那颗心）；不认人的调用恒 False
        var isFavorite: Bool
        /// 最近一次可追溯入库批次的剧集摘要；NULL=电影或迁移前旧台账
        var recentAddition: API.LibraryRecentAdditionView?
        /// 本库在位剧集相对 TMDB 季集结构的完整度摘要；电影或无有效集号为 NULL
        var inventorySummary: API.LibraryInventorySummaryView?
        /// 在位但尚未探出介质规格的文件数——扫描补探阶段前端据此把「还在处理」的条目排到海报墙前面并点亮标记
        var probePendingCount: Int

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case kind
            case libraryId = "library_id"
            case source
            case tmdbId = "tmdb_id"
            case title
            case year
            case posterUrl = "poster_url"
            case backdropUrl = "backdrop_url"
            case primaryAspect = "primary_aspect"
            case releaseDate = "release_date"
            case rating
            case posterBlur = "poster_blur"
            case primaryFileId = "primary_file_id"
            case fileCount = "file_count"
            case totalSizeBytes = "total_size_bytes"
            case seasons
            case episodeCount = "episode_count"
            case resolutions
            case missingCount = "missing_count"
            case airStatus = "air_status"
            case missingEpisodeCount = "missing_episode_count"
            case addedAt = "added_at"
            case isFavorite = "is_favorite"
            case recentAddition = "recent_addition"
            case inventorySummary = "inventory_summary"
            case probePendingCount = "probe_pending_count"
        }
    }

    /// 创建/更新库的请求体。kind/source 仅创建时生效，更新时忽略（创建后不可改）。
    struct LibraryPayload: Codable, Hashable, Sendable {
        /// 库的展示名（全局唯一）
        var name: String
        /// 内容形态：movie=电影 / tv=剧集 / video=其他（无结构假设）
        var kind: API.MediaKind
        /// 身份来源：tmdb / local；不传按形态默认（movie、tv → tmdb，video → local）。与 kind 一起定位库的能力档案，创建后不可改
        var source: String?
        /// 缺图时是否从视频抓帧生成缩略图：本地来源内容的封面、剧集库里 TMDB 没有剧照的分集（网络挂载库抓帧等于全量下载，可关）；不传表示不改动，新建时默认开启
        var generateThumbnails: Bool?
        /// 是否为视频章节抓取场景图（后台低优先级作业，每个文件按章节数 seek 若干次）；不传表示不改动，新建时默认开启
        var extractChapterImages: Bool?
        /// 是否从首页「最近添加」等汇总里排除该库；不传表示不改动，新建时默认关闭
        var excludeFromHome: Bool?
        /// 是否按作品系列自动生成合集（《哈利·波特》这种）。这是**展示**偏好：关掉之后系列信息照常落库、NFO 的 <set> 照常写，只是合集页不自动多出几十个系列；重新打开会把已有的系列补齐，不重新联网刮削。不传表示不改动，新建时默认开启
        var autoSeriesCollections: Bool?
        /// 可见范围：everyone=对全部成员自动开放（含以后新建的成员）/ selected=只对 member_ids 里显式授权的成员开放；不传表示不改动，新建时默认 everyone
        var accessMode: String?
        /// 超管本人是否可浏览本库内容（管理权不受影响）；不传表示不改动，新建时默认可浏览
        var adminVisible: Bool?
        /// 显式授权的成员 id（整体覆盖式；与成员管理页的可见库白名单是同一份数据）；不传表示不改动
        var memberIds: [Int]?
        /// 根路径列表（绝对路径），第一个为主根——新入库落在这里
        var rootPaths: [String]
        /// 收藏范围条件（条件间 AND、条件内任一匹配）；genres 存 TMDB 类型 ID，origin_countries 存国家码；空=未声明
        var matchRules: [[String: API.JSONValue]]?
        /// 扫描后自动清理已确认丢失的库存记录（只删台账不动磁盘，不可恢复）；不传表示不改动，新建时默认关闭
        var autoClearMissing: Bool?
        /// 库级刮削偏好覆盖（语言/选图/命名/目录写入，即「刮削与整理」的全部字段）；不传=不改动，空对象=清空覆盖回到全跟全局。语言与选图这类产物挂全局条目的设置，按条目的**刮削归属库**生效
        var scrapeOverrides: [String: API.JSONValue]?
        /// 是否启用实时文件监控（关闭后该库不建 watchdog 监听，靠定期对账与手动扫描发现新文件——SMB/NFS 网络挂载建议关闭）；不传表示不改动，新建时默认开启
        var realtimeWatch: Bool?

        enum CodingKeys: String, CodingKey {
            case name
            case kind
            case source
            case generateThumbnails = "generate_thumbnails"
            case extractChapterImages = "extract_chapter_images"
            case excludeFromHome = "exclude_from_home"
            case autoSeriesCollections = "auto_series_collections"
            case accessMode = "access_mode"
            case adminVisible = "admin_visible"
            case memberIds = "member_ids"
            case rootPaths = "root_paths"
            case matchRules = "match_rules"
            case autoClearMissing = "auto_clear_missing"
            case scrapeOverrides = "scrape_overrides"
            case realtimeWatch = "realtime_watch"
        }
    }

    /// 一个库的完整入库链路结论。
    struct LibraryPipelineView: Codable, Hashable, Sendable {
        var libraryId: Int
        var libraryName: String
        var kind: String
        var isDefault: Bool
        var mode: String
        /// 投递基底目录（movieclaw 视角）
        var path: String?
        /// 库主根（入库节点的落点）
        var libraryRoot: String?
        /// 命中自定义目录规则时的整理落点（非空时转移段不直接入库，外部流转后回库根入账）
        var stagingPath: String?
        /// 全链路最坏状态
        var status: String
        /// 「订阅命中本库会发生什么」的一句话叙事（正向可预期）
        var narrative: String
        var checks: [API.HealthCheckView]

        enum CodingKeys: String, CodingKey {
            case libraryId = "library_id"
            case libraryName = "library_name"
            case kind
            case isDefault = "is_default"
            case mode
            case path
            case libraryRoot = "library_root"
            case stagingPath = "staging_path"
            case status
            case narrative
            case checks
        }
    }

    /// 让条目进入「最近添加」的最后一批剧集的紧凑摘要。
    struct LibraryRecentAdditionView: Codable, Hashable, Sendable {
        var seasonCount: Int
        var episodeCount: Int
        /// 仅涉及一季时的季号；跨季为 NULL
        var seasonNumber: Int?
        /// 同季连续批次的起始集；否则 NULL
        var firstEpisodeNumber: Int?
        /// 同季连续批次的结束集；否则 NULL
        var lastEpisodeNumber: Int?
        /// 本批是否完整覆盖该季 TMDB 已知集数
        var completeSeason: Bool

        enum CodingKeys: String, CodingKey {
            case seasonCount = "season_count"
            case episodeCount = "episode_count"
            case seasonNumber = "season_number"
            case firstEpisodeNumber = "first_episode_number"
            case lastEpisodeNumber = "last_episode_number"
            case completeSeason = "complete_season"
        }
    }

    /// 筛空之后的出路。
    /// 不渲染空墙，而是告诉用户「放宽哪一条能救回多少部」。**只列救得回内容的
    /// 条件**——多维交叉时经常出现"去掉它还是 0 部"的剔除项，把它们摆出来是
    /// 噪音不是建议；一条都救不回时 suggestions 为空，前端只留「清空全部条件」。
    struct LibraryRelaxView: Codable, Hashable, Sendable {
        /// 当前条件下的命中数（调用方通常在它为 0 时才用本接口）
        var total: Int
        /// 按能救回的数量倒序，最多三条
        var suggestions: [API.RelaxSuggestionView]

        enum CodingKeys: String, CodingKey {
            case total
            case suggestions
        }
    }

    /// 媒体库重排的请求体：必须一次给全所有库的 id（漏/多/重复都拒绝）。
    struct LibraryReorderPayload: Codable, Hashable, Sendable {
        /// 全部媒体库 id 的目标顺序（越靠前展示越靠前）
        var orderedIds: [Int]

        enum CodingKeys: String, CodingKey {
            case orderedIds = "ordered_ids"
        }
    }

    /// 媒体库搜索结果的一组：一个库内命中关键词的条目（组内按标题拼音排序）。
    struct LibrarySearchGroupView: Codable, Hashable, Sendable {
        var libraryId: Int
        var libraryName: String
        var kind: API.MediaKind
        var items: [API.LibraryItemView]

        enum CodingKeys: String, CodingKey {
            case libraryId = "library_id"
            case libraryName = "library_name"
            case kind
            case items
        }
    }

    /// 库存统计快照（台账变化时重算，查询时直接读取 library 表）。
    /// **扫描进行中读到的是中间态，不是结论**：扫描按事务分批落账，文件先入账、
    /// 随后才识别，所以 ``unidentified_count`` 在扫描途中会先冲高再回落（一次
    /// 上万文件的扫描中途读到两千多、扫完是 0，两个数都是真的）。要判断"这个库
    /// 还有多少待识别"，先看同一响应里的 ``scanning``：为 true 时这几个数只能当
    /// 进度看。刻意不把统计改成"只在扫描结束后更新"——那会让扫描期间完全看不到
    /// 进展，比抖动更糟。
    struct LibraryStats: Codable, Hashable, Sendable {
        /// 在位且已识别的媒体条目数
        var itemCount: Int
        /// 在位文件总数（含待识别）
        var fileCount: Int
        /// 在位文件总大小（字节）
        var totalSizeBytes: Int
        /// 在位待识别文件数（不含已忽略）；scanning=true 时是中间态，扫完才是结论
        var unidentifiedCount: Int
        /// 标记 missing 的文件数（缺失清单入口）
        var missingCount: Int
        /// 在位且被用户忽略的文件数（不再参与识别，可在已忽略清单恢复）
        var ignoredCount: Int

        enum CodingKeys: String, CodingKey {
            case itemCount = "item_count"
            case fileCount = "file_count"
            case totalSizeBytes = "total_size_bytes"
            case unidentifiedCount = "unidentified_count"
            case missingCount = "missing_count"
            case ignoredCount = "ignored_count"
        }
    }

    struct LibraryView: Codable, Hashable, Sendable {
        var id: Int
        var name: String
        var kind: API.MediaKind
        /// 身份来源：tmdb / local
        var source: String
        var capabilities: API.LibraryCapabilitiesView
        /// 缺图时是否抓帧生成缩略图（本地内容封面、TMDB 无剧照的分集）
        var generateThumbnails: Bool
        /// 是否为视频章节抓取场景图
        var extractChapterImages: Bool
        /// 是否从首页汇总里排除
        var excludeFromHome: Bool
        /// 是否按作品系列自动生成合集（展示偏好）
        var autoSeriesCollections: Bool
        /// 可见范围：everyone=所有成员 / selected=指定成员
        var accessMode: String
        /// 超管本人是否可浏览本库内容
        var adminVisible: Bool
        /// 显式授权的成员 id（仅管理员可见，成员端恒空）
        var memberIds: [Int]
        /// 当前请求主体能否浏览本库内容。成员端恒 true（看不到的库根本不在列表里）；超管端为 false 时表示只有管理权：首页显示带锁的管理卡片，海报墙/详情/播放不可用
        var viewerAccess: Bool
        var rootPaths: [String]
        /// 主根路径（root_paths 第一项）
        var primaryRoot: String?
        var isDefault: Bool
        /// 收藏范围条件
        var matchRules: [[String: API.JSONValue]]
        /// 扫描后自动清理已确认丢失的库存记录
        var autoClearMissing: Bool
        /// 是否启用实时文件监控
        var realtimeWatch: Bool
        /// 任一根路径落在网络挂载（NFS/SMB/fuse）上。这种库实时监控收不到远端变更，新文件靠定期对账发现——界面据此把话说清楚，而不是让开关看起来有效
        var networkMount: Bool
        /// 封面是用户上传的自定义图（而非自动拼贴）。前端据此决定「恢复自动拼贴」按钮的形态，以及空库要不要照样出图
        var customCover: Bool
        /// 库级刮削偏好覆盖；空对象 = 全跟全局设置
        var scrapeOverrides: [String: API.JSONValue]
        var stats: API.LibraryStats
        /// 是否正在扫描
        var scanning: Bool
        /// 扫描实时进度
        var scanProgress: API.ScanProgressView?
        /// 最近一次扫描结论
        var lastScan: API.LastScanView?
        /// 是否正在整理文件名
        var organizing: Bool
        /// 整理实时进度（与扫描进度同构）
        var organizeProgress: API.ScanProgressView?
        /// 最近一次整理结论
        var lastOrganize: API.LastOrganizeView?
        /// 整库元数据刷新状态；没在刷为 null
        var metadataRefresh: API.MetadataRefreshView?
        /// 整库生成章节的作业状态（排队/进行中）；没在生成为 null
        var chapterJob: API.ChapterJobView?
        var createdAt: String
        var updatedAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case kind
            case source
            case capabilities
            case generateThumbnails = "generate_thumbnails"
            case extractChapterImages = "extract_chapter_images"
            case excludeFromHome = "exclude_from_home"
            case autoSeriesCollections = "auto_series_collections"
            case accessMode = "access_mode"
            case adminVisible = "admin_visible"
            case memberIds = "member_ids"
            case viewerAccess = "viewer_access"
            case rootPaths = "root_paths"
            case primaryRoot = "primary_root"
            case isDefault = "is_default"
            case matchRules = "match_rules"
            case autoClearMissing = "auto_clear_missing"
            case realtimeWatch = "realtime_watch"
            case networkMount = "network_mount"
            case customCover = "custom_cover"
            case scrapeOverrides = "scrape_overrides"
            case stats
            case scanning
            case scanProgress = "scan_progress"
            case lastScan = "last_scan"
            case organizing
            case organizeProgress = "organize_progress"
            case lastOrganize = "last_organize"
            case metadataRefresh = "metadata_refresh"
            case chapterJob = "chapter_job"
            case createdAt = "created_at"
            case updatedAt = "updated_at"
        }
    }

    /// 保存 AI 设定：各用途的默认模型引用（取自 llm.models 的 ref），传 null 清除。
    struct LlmDefaultsPayload: Codable, Hashable, Sendable {
        /// 智能体默认模型引用
        var agentModel: String?
        /// 字幕处理默认模型引用
        var subtitleModel: String?

        enum CodingKeys: String, CodingKey {
            case agentModel = "agent_model"
            case subtitleModel = "subtitle_model"
        }
    }

    /// AI 设定（各用途默认模型）的对外视图。
    /// ``*_model`` 是存下来的引用：首次接入供应商时自动设为其目录里第一个模型，
    /// 之后由用户改；一个实例都没有时为 null。``effective_*`` 是运行时实际生效的
    /// 引用，正常与前者一致，只在预设目录变动等漂移场景下按最早实例兜底。
    struct LlmDefaultsView: Codable, Hashable, Sendable {
        /// 智能体默认模型引用（null = 未设置）
        var agentModel: String?
        /// 字幕处理默认模型引用（null = 未设置）
        var subtitleModel: String?
        /// 智能体实际生效的引用
        var effectiveAgentModel: String?
        /// 字幕处理实际生效的引用
        var effectiveSubtitleModel: String?

        enum CodingKeys: String, CodingKey {
            case agentModel = "agent_model"
            case subtitleModel = "subtitle_model"
            case effectiveAgentModel = "effective_agent_model"
            case effectiveSubtitleModel = "effective_subtitle_model"
        }
    }

    /// 对话框模型选择器的一个选项（口径见 services.llm_config 模块说明）。
    struct LlmModelOptionView: Codable, Hashable, Sendable {
        /// 提交给 session.start 的模型引用：裸模型 id，或同 id 在多个实例时的「实例名/模型id」
        var ref: String
        /// 展示文案：裸模型 id，冲突时为「模型id（实例名）」
        var label: String
        /// 模型 id
        var modelId: String
        /// 所属实例 id
        var providerId: Int
        /// 所属实例名
        var providerName: String
        /// 是否为智能体默认模型（AI 设定），清单里恰有一个
        var isDefault: Bool
        /// 该模型的思考档位菜单；空 = 隐藏档位选择器
        var thinkingLevels: [String]

        enum CodingKeys: String, CodingKey {
            case ref
            case label
            case modelId = "model_id"
            case providerId = "provider_id"
            case providerName = "provider_name"
            case isDefault = "is_default"
            case thinkingLevels = "thinking_levels"
        }
    }

    /// 供应商预设的对外视图：设置页用它渲染类型选项与模型目录。
    struct LlmPresetView: Codable, Hashable, Sendable {
        var id: String
        var displayName: String
        var baseUrl: String?
        var requiresBaseUrl: Bool
        var defaultUserAgent: String
        var models: [API.ModelInfo]

        enum CodingKeys: String, CodingKey {
            case id
            case displayName = "display_name"
            case baseUrl = "base_url"
            case requiresBaseUrl = "requires_base_url"
            case defaultUserAgent = "default_user_agent"
            case models
        }
    }

    /// 新增 / 编辑 LLM 供应商实例的请求体。
    /// API Key 出于安全不回显，编辑时需要重新填写。
    struct LlmProviderPayload: Codable, Hashable, Sendable {
        /// 实例名（全局唯一，不含斜杠），如「官方 OpenAI」
        var name: String
        /// 供应商类型：openai / bailian / openai_compat
        var providerType: String
        /// API 端点（留空用预设默认）
        var baseUrl: String?
        /// 自定义 User-Agent 请求头（留空使用 openai SDK 自带 UA）
        var userAgent: String?
        /// API Key
        var apiKey: String
        /// 连接测试用的模型 id；留空取目录里第一个（预设目录或自定义目录）
        var defaultModel: String?
        /// 自定义模型目录 JSON 数组，元素形如 {"id":"模型id","context_window":131072,"max_output_tokens":8192}（openai_compat 端点至少一条）
        var extraModels: [API.ModelInfoInput]?

        enum CodingKeys: String, CodingKey {
            case name
            case providerType = "provider_type"
            case baseUrl = "base_url"
            case userAgent = "user_agent"
            case apiKey = "api_key"
            case defaultModel = "default_model"
            case extraModels = "extra_models"
        }
    }

    /// LLM 供应商实例的对外视图（**脱敏**：绝不回传 API Key）。
    struct LlmProviderView: Codable, Hashable, Sendable {
        var id: Int
        /// 实例名（全局唯一，路由键）
        var name: String
        var providerType: String
        var baseUrl: String?
        /// 自定义 User-Agent；null 表示用 SDK 默认 UA
        var userAgent: String?
        /// 连接测试用的模型 id（目录里第一个）
        var defaultModel: String
        var status: API.ConfigStatus
        /// 是否可用 = 连接测试通过（status=active）
        var usable: Bool
        /// 最近测试失败原因（清晰中文）
        var lastError: String?
        var lastCheckedAt: String?
        /// 最近验证成功时端点上报的可用模型列表
        var availableModels: [String]?
        /// 用户补录的自定义模型目录（含参数）
        var extraModels: [API.ModelInfo]
        var createdAt: String
        var updatedAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case providerType = "provider_type"
            case baseUrl = "base_url"
            case userAgent = "user_agent"
            case defaultModel = "default_model"
            case status
            case usable
            case lastError = "last_error"
            case lastCheckedAt = "last_checked_at"
            case availableModels = "available_models"
            case extraModels = "extra_models"
            case createdAt = "created_at"
            case updatedAt = "updated_at"
        }
    }

    /// 条目的展示元数据：本地 NFO > 库内刮削档案 > TMDB 实时兜底；
    /// 三个来源都拉不到时整体为 null（docs/design/metadata.md 第 5 节）。
    struct LocalMetaView: Codable, Hashable, Sendable {
        var plot: String?
        var rating: Double?
        var runtimeMinutes: Int?
        var genres: [String]
        var directors: [String]
        /// 从 person 关系表读取的结构化导演；空列表时前端回退 directors 姓名
        var directorCredits: [API.DirectorView]
        var actors: [API.ActorView]
        /// 来源 NFO 文件名（source=nfo 时给出）
        var nfoName: String
        /// 信息出处：nfo=本地刮削 / db=库内档案 / tmdb=实时兜底
        var source: String

        enum CodingKeys: String, CodingKey {
            case plot
            case rating
            case runtimeMinutes = "runtime_minutes"
            case genres
            case directors
            case directorCredits = "director_credits"
            case actors
            case nfoName = "nfo_name"
            case source
        }
    }

    /// 某天的日志内容（可能只是末尾片段，truncated 标记是否被截断）。
    struct LogContent: Codable, Hashable, Sendable {
        var day: String
        var lines: [String]
        var totalLines: Int
        var truncated: Bool
        var sizeBytes: Int

        enum CodingKeys: String, CodingKey {
            case day
            case lines
            case totalLines = "total_lines"
            case truncated
            case sizeBytes = "size_bytes"
        }
    }

    /// 一个可查看的日志日期（对应磁盘上的一个日志文件）。
    struct LogDay: Codable, Hashable, Sendable {
        var day: String
        var sizeBytes: Int

        enum CodingKeys: String, CodingKey {
            case day
            case sizeBytes = "size_bytes"
        }
    }

    struct LogDayList: Codable, Hashable, Sendable {
        var days: [API.LogDay]

        enum CodingKeys: String, CodingKey {
            case days
        }
    }

    struct LoginRequest: Codable, Hashable, Sendable {
        var username: String
        var password: String
        /// 记住我：会话有效期 7 天 → 30 天
        var remember: Bool?

        enum CodingKeys: String, CodingKey {
            case username
            case password
            case remember
        }
    }

    /// 退出登录。默认只退当前账号并切到袋子里的下一个；all=True 清空全部。
    struct LogoutRequest: Codable, Hashable, Sendable {
        /// true=退出本浏览器里的全部账号
        var all: Bool?

        enum CodingKeys: String, CodingKey {
            case all
        }
    }

    /// 预检未收敛时留给用户确认的 TMDB 候选。
    struct ManualDownloadCandidateView: Codable, Hashable, Sendable {
        var tmdbId: Int
        var title: String
        var year: Int?
        var episodeCount: Int?

        enum CodingKeys: String, CodingKey {
            case tmdbId = "tmdb_id"
            case title
            case year
            case episodeCount = "episode_count"
        }
    }

    /// 手动下载的识别预检输入：只接受搜索结果已解析出的最小身份线索。
    struct ManualDownloadTargetPayload: Codable, Hashable, Sendable {
        /// 搜索结果识别出的媒体类型
        var kind: String
        /// 搜索结果识别出的主标题
        var title: String
        /// 搜索结果识别出的发行/首播年份
        var year: Int
        /// 种子副标题（中文别名等识别补强）
        var subtitle: String?
        /// 预检指定下载器；缺省用默认下载器
        var downloaderId: Int?
        /// 用户从本次识别候选中确认的 TMDB 条目 ID
        var selectedTmdbId: Int?

        enum CodingKeys: String, CodingKey {
            case kind
            case title
            case year
            case subtitle
            case downloaderId = "downloader_id"
            case selectedTmdbId = "selected_tmdb_id"
        }
    }

    /// 手动下载的「识别 → 路由 → 投递目录」预检结论。
    struct ManualDownloadTargetView: Codable, Hashable, Sendable {
        var status: String
        var tmdbId: Int?
        var candidates: [API.ManualDownloadCandidateView]
        var libraryId: Int?
        var libraryName: String?
        var mode: String?
        /// movieclaw 视角的实际投递目录
        var path: String?
        /// 条目目录的完整路径预览（按生效的命名模板渲染）；前端展示投递落点时用它，不要自己拼「标题 (年份)」
        var entryDir: String?
        /// 自定义目录规则的整理落点
        var stagingPath: String?
        /// 是否命中媒体库收藏范围
        var routeMatched: Bool?
        /// 媒体库路由理由
        var routeReason: String?
        /// 当前选择的下载器和投递配置能否自动入库
        var ok: Bool
        /// 不可自动入库时的中文指引
        var warning: String?

        enum CodingKeys: String, CodingKey {
            case status
            case tmdbId = "tmdb_id"
            case candidates
            case libraryId = "library_id"
            case libraryName = "library_name"
            case mode
            case path
            case entryDir = "entry_dir"
            case stagingPath = "staging_path"
            case routeMatched = "route_matched"
            case routeReason = "route_reason"
            case ok
            case warning
        }
    }

    /// 播放会话 / 文件下载指向的媒体条目摘要。
    struct MediaActivityTarget: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var libraryId: Int?
        var browsable: Bool
        var kind: API.MediaKind
        var title: String
        var year: Int?
        var posterUrl: String?
        var seasonNumber: Int
        var episodeNumber: Int
        var episodeTitle: String?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case libraryId = "library_id"
            case browsable
            case kind
            case title
            case year
            case posterUrl = "poster_url"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case episodeTitle = "episode_title"
        }
    }

    /// 活动页「观看」视角的实时快照：正在播放与正在下载。
    /// 历史（每场一行的播放记录）走 ``PlaybackHistoryView``，本载荷只装页面 8 秒
    /// 轮询真正要刷新的实时部分。
    struct MediaActivityView: Codable, Hashable, Sendable {
        var sessions: [API.ActivePlaybackSessionView]
        var downloads: [API.ActiveFileDownloadView]
        /// 不在你可见范围内的正在播放数
        var hiddenSessionCount: Int
        /// 不在你可见范围内的正在下载数
        var hiddenDownloadCount: Int

        enum CodingKeys: String, CodingKey {
            case sessions
            case downloads
            case hiddenSessionCount = "hidden_session_count"
            case hiddenDownloadCount = "hidden_download_count"
        }
    }

    /// 弹层与列表共用的条目摘要。
    struct MediaBrief: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var kind: API.MediaKind
        var tmdbId: Int
        var doubanId: String?
        var title: String
        var originalTitle: String
        var year: Int?
        /// 完整海报 URL（按配置的图床基址拼好）
        var posterUrl: String?
        /// 宽幅剧照 URL（w1280，沉浸场景可换 original 档）
        var backdropUrl: String?
        /// 片名 Logo URL（透明底 PNG）；没有时前端显示文字片名
        var logoUrl: String?
        var status: String?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case kind
            case tmdbId = "tmdb_id"
            case doubanId = "douban_id"
            case title
            case originalTitle = "original_title"
            case year
            case posterUrl = "poster_url"
            case backdropUrl = "backdrop_url"
            case logoUrl = "logo_url"
            case status
        }
    }

    /// 演职员表的一位人物：姓名 + 可选角色 + 头像。
    /// 发现页详情要按「演职员横滚条」呈现（与媒体库条目详情同一套版式），
    /// 导演与演员都需要结构化头像和人物 ID；演员额外携带角色名。头像在数据源
    /// 里常常缺失（小众条目、配音演员），前端按占位渲染，
    /// 不必为此过滤掉这个人——名字与角色本身就是有效信息。
    struct MediaCastMember: Codable, Hashable, Sendable {
        /// 人物姓名
        var name: String
        /// 饰演角色；数据源未提供为空
        var role: String?
        /// 头像地址；数据源未提供为空
        var avatarUrl: String?
        /// TMDB 影人 ID；有值时前端把这一格链到人物页。豆瓣来源没有此 id
        var tmdbPersonId: Int?

        enum CodingKeys: String, CodingKey {
            case name
            case role
            case avatarUrl = "avatar_url"
            case tmdbPersonId = "tmdb_person_id"
        }
    }

    /// 一张剧照/海报：横滚条用预览图，灯箱看原图。
    struct MediaImage: Codable, Hashable, Sendable {
        /// 缩略预览（剧照 w780 / 海报 w342）
        var previewUrl: String
        /// 原图（original，灯箱全屏用）
        var fullUrl: String
        var width: Int
        var height: Int

        enum CodingKeys: String, CodingKey {
            case previewUrl = "preview_url"
            case fullUrl = "full_url"
            case width
            case height
        }
    }

    /// 内容形态：电影 / 剧集 / 其他视频 / 图片（docs/design/library-other-kind.md 3.1、
    /// library-photo-kind.md 2.1）。
    /// ``movie`` 与 ``tv`` 的取值与 TMDB 的路径段一致，在 ``source=tmdb`` 的
    /// 识别/刮削路径里可直接拼接 URL；``video`` 是没有结构假设的单本视频
    /// （家庭录像、自录内容），``photo`` 是单张图片（照片、截图），两者都只在
    /// 本地来源下出现，永远不会进 TMDB 请求。形态描述结构，不描述题材也不
    /// 描述来源。
    typealias MediaKind = String
    // 取值：'movie', 'tv', 'video', 'photo'

    /// 发现详情跳转到一个本地媒体库条目所需的最小身份信息。
    struct MediaLibraryLink: Codable, Hashable, Sendable {
        /// 媒体库 id
        var libraryId: Int
        /// 媒体库展示名称
        var libraryName: String
        /// 本地媒体条目 id
        var mediaItemId: Int

        enum CodingKeys: String, CodingKey {
            case libraryId = "library_id"
            case libraryName = "library_name"
            case mediaItemId = "media_item_id"
        }
    }

    /// 发现卡片对应的轻量库存摘要。
    /// 这是发现页与本地库存之间唯一共享的列表级契约：只表达是否存在在位
    /// 文件及其聚合数量，不携带文件路径、介质规格或探测 JSON，避免海报墙
    /// 查询扩大为逐条目明细读取。
    struct MediaLibraryStatus: Codable, Hashable, Sendable {
        /// 本地媒体条目 id，用于详情深链
        var mediaItemId: Int
        /// 包含在位文件的媒体库数量
        var libraryCount: Int
        /// 在位文件数量
        var fileCount: Int

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case libraryCount = "library_count"
            case fileCount = "file_count"
        }
    }

    /// 媒体数据来源；ID 只在同一来源内部唯一。
    typealias MediaSource = String
    // 取值：'tmdb', 'douban'

    /// 标注弹窗预览的一行：将被标注的文件（片源未知或此前人工标注）。
    struct MediaSourceAnnotationCandidateView: Codable, Hashable, Sendable {
        var fileId: Int
        var fileName: String
        var episodeNumber: Int
        var sizeBytes: Int
        /// 当前片源；null=未知
        var mediaSource: String?
        /// 当前值是否为此前的人工标注
        var mediaSourceManual: Bool

        enum CodingKeys: String, CodingKey {
            case fileId = "file_id"
            case fileName = "file_name"
            case episodeNumber = "episode_number"
            case sizeBytes = "size_bytes"
            case mediaSource = "media_source"
            case mediaSourceManual = "media_source_manual"
        }
    }

    /// 整季片源人工标注（docs/design/media-source-annotation.md §4）。
    /// 值域与洗版片源档阶梯对齐；``user-lowest`` 是「不确定，按最低档处理」
    /// 的显式哨兵（T0，会触发整季自动洗版重下）。
    struct MediaSourceAnnotationPayload: Codable, Hashable, Sendable {
        /// 媒体条目 id
        var mediaItemId: Int
        /// 季号；电影固定 0
        var seasonNumber: Int
        /// 标注的片源档
        var mediaSource: String

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case seasonNumber = "season_number"
            case mediaSource = "media_source"
        }
    }

    /// 详情页的一段预告片/花絮。
    /// TMDB 只给出 YouTube 的视频 key，**不提供可直接播放的视频流**，因此播放这一步
    /// 依赖浏览器自身能连上 YouTube——服务端配的代理帮不上忙。封面图则不同：它是
    /// 普通图片，前端统一走 /images/proxy 由服务端回源缓存，所以只要服务端能出网，
    /// 即使浏览器连不上 YouTube，预告片卡片也照样完整展示并给出外链入口。
    struct MediaVideo: Codable, Hashable, Sendable {
        /// YouTube 视频 ID（前端当作不透明键使用）
        var key: String
        /// 视频标题，TMDB 原样给出（多为英文）
        var name: String
        /// 中文类型标签：预告片 / 先导预告 / 片段 / 花絮 / 幕后
        var kind: String
        /// YouTube 封面图；4:3 带上下黑边，前端按 cover 裁切即得 16:9 画面
        var thumbnailUrl: String
        /// 内嵌播放地址（youtube-nocookie，不落跟踪 cookie）
        var embedUrl: String
        /// YouTube 站内地址，供无法内嵌时外链打开
        var watchUrl: String

        enum CodingKeys: String, CodingKey {
            case key
            case name
            case kind
            case thumbnailUrl = "thumbnail_url"
            case embedUrl = "embed_url"
            case watchUrl = "watch_url"
        }
    }

    /// 新建成员：用户名 + 初始密码（前端默认一键生成随机密码）。
    struct MemberCreateRequest: Codable, Hashable, Sendable {
        /// 登录名，创建后不可改
        var username: String
        /// 初始密码，至少 8 位
        var password: String
        /// 展示昵称，可留空
        var nickname: String?

        enum CodingKeys: String, CodingKey {
            case username
            case password
            case nickname
        }
    }

    /// 重置密码的返回体：新密码明文仅此一次，请立即复制发给成员。
    struct MemberPasswordResetView: Codable, Hashable, Sendable {
        var id: Int
        var username: String
        /// 新密码明文；服务端只存哈希，之后无法再次查看
        var password: String

        enum CodingKeys: String, CodingKey {
            case id
            case username
            case password
        }
    }

    /// 启用 / 停用成员。停用即时踢下线，数据全部保留。
    struct MemberStatusRequest: Codable, Hashable, Sendable {
        var enabled: Bool

        enum CodingKeys: String, CodingKey {
            case enabled
        }
    }

    /// 编辑成员：None 字段不改动；白名单为整体覆盖式保存。
    struct MemberUpdateRequest: Codable, Hashable, Sendable {
        var nickname: String?
        var allowSubscribe: Bool?
        var allowSearch: Bool?
        var allowDirectDownload: Bool?
        /// True=全部库可见（含未来新建）；False=按 library_ids 白名单
        var allLibraries: Bool?
        /// 可见库白名单（整体覆盖）；仅 all_libraries=False 时生效
        var libraryIds: [Int]?
        /// True=全部站点可用；False=按 site_ids 白名单
        var allSites: Bool?
        /// 可用站点白名单（整体覆盖）；仅 all_sites=False 时生效
        var siteIds: [String]?
        /// 内容年龄上限（岁）。设了之后，超过这个年龄分级的作品在海报墙、搜索、合集、Jellyfin、条目详情与起播六处一律不可见。**传 -1 表示取消上限**（传 null 是「不改动」，两者不是一回事）
        var contentAgeLimit: Int?
        /// 设了年龄上限时，未分级的作品是否仍可见。默认关闭——大量中文影片在 TMDB 上没有分级信息，「我不确定的一律不给看」才是家长要的默认值
        var allowUnrated: Bool?

        enum CodingKeys: String, CodingKey {
            case nickname
            case allowSubscribe = "allow_subscribe"
            case allowSearch = "allow_search"
            case allowDirectDownload = "allow_direct_download"
            case allLibraries = "all_libraries"
            case libraryIds = "library_ids"
            case allSites = "all_sites"
            case siteIds = "site_ids"
            case contentAgeLimit = "content_age_limit"
            case allowUnrated = "allow_unrated"
        }
    }

    /// 成员详情（管理页列表与详情共用）。
    struct MemberView: Codable, Hashable, Sendable {
        var id: Int
        var username: String
        var nickname: String
        var avatarUrl: String?
        var status: String
        /// 最近登录时间；None=从未登录
        var lastLoginAt: String?
        var allowSubscribe: Bool
        var allowSearch: Bool
        var allowDirectDownload: Bool
        var allLibraries: Bool
        var libraryIds: [Int]
        var allSites: Bool
        var siteIds: [String]
        /// 内容年龄上限（岁）；null=不限
        var contentAgeLimit: Int?
        /// 设了上限时未分级的作品是否可见
        var allowUnrated: Bool
        var createdAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case username
            case nickname
            case avatarUrl = "avatar_url"
            case status
            case lastLoginAt = "last_login_at"
            case allowSubscribe = "allow_subscribe"
            case allowSearch = "allow_search"
            case allowDirectDownload = "allow_direct_download"
            case allLibraries = "all_libraries"
            case libraryIds = "library_ids"
            case allSites = "all_sites"
            case siteIds = "site_ids"
            case contentAgeLimit = "content_age_limit"
            case allowUnrated = "allow_unrated"
            case createdAt = "created_at"
        }
    }

    /// 整库刷新的实时状态——全量重刷很慢，用户要看到"到哪部了、在做什么"。
    /// 随库列表一并返回（见 LibraryView.metadata_refresh），媒体库首页的库卡片
    /// 因此不必额外请求就能显示刷新进度；单库页另有专用端点做 2 秒级的阶段
    /// 刷新（首页 10 秒一轮的节奏跟不上阶段变化）。
    struct MetadataRefreshView: Codable, Hashable, Sendable {
        var refreshing: Bool
        /// 已完成条目数（含失败）
        var processed: Int
        var total: Int
        /// 刮削失败的条目数（多为 TMDB 不可达）
        var failed: Int
        /// 已请求停止，正在收尾
        var stopping: Bool
        /// 正在处理的条目及其阶段
        var active: [API.RefreshActiveView]

        enum CodingKeys: String, CodingKey {
            case refreshing
            case processed
            case total
            case failed
            case stopping
            case active
        }
    }

    /// 刮削管线的用户偏好。所有字段的默认值即当前写死的行为。
    struct MetadataScrapeSetting: Codable, Hashable, Sendable {
        /// 元数据语言优先级（1~3 项）；首位为主语言（请求语言），空 = 跟随环境变量 TMDB_LANGUAGE + en-US 兜底
        var languagePriority: [String]
        /// 内容分级的地区优先级：按顺序取第一个有分级数据的地区
        var certCountryPriority: [String]
        /// 海报选择：default=TMDB 默认（与发现页一致）；language=按语言优先级挑选
        var posterMode: String
        /// 海报语言优先级（poster_mode=language 时生效）
        var posterLanguagePriority: [String]
        /// 背景图语言优先级；「无文字」排首位即无文字优先（现状）
        var backdropLanguagePriority: [String]
        /// 海报最低宽度门槛；0 = 不限制
        var posterMinWidth: Int
        /// 背景图最低宽度门槛；0 = 不限制
        var backdropMinWidth: Int
        /// 海报档位；空 = 跟随环境变量
        var posterSize: String
        /// 背景档位；空 = 跟随环境变量
        var backdropSize: String
        /// 分集剧照档位；空 = 跟随环境变量
        var stillSize: String
        /// 条目目录模板；空 = 默认 {title} ({year})
        var namingEntryDir: String
        /// 电影文件名模板；空 = 默认 {title} ({year})
        var namingMovieFile: String
        /// 季目录模板；空 = 默认 Season {season:02d}
        var namingSeasonDir: String
        /// 剧集文件名模板；空 = 默认 {title} ({year}) - S{season:02d}E{episode:02d}
        var namingEpisodeFile: String
        /// 镜像条目图片到媒体目录（poster/fanart/季海报）
        var mirrorImages: Bool
        /// 镜像 NFO 元数据到媒体目录
        var mirrorNfo: Bool
        /// 镜像分集剧照（长剧集写入量最大，可单独关）
        var mirrorEpisodeThumbs: Bool

        enum CodingKeys: String, CodingKey {
            case languagePriority = "language_priority"
            case certCountryPriority = "cert_country_priority"
            case posterMode = "poster_mode"
            case posterLanguagePriority = "poster_language_priority"
            case backdropLanguagePriority = "backdrop_language_priority"
            case posterMinWidth = "poster_min_width"
            case backdropMinWidth = "backdrop_min_width"
            case posterSize = "poster_size"
            case backdropSize = "backdrop_size"
            case stillSize = "still_size"
            case namingEntryDir = "naming_entry_dir"
            case namingMovieFile = "naming_movie_file"
            case namingSeasonDir = "naming_season_dir"
            case namingEpisodeFile = "naming_episode_file"
            case mirrorImages = "mirror_images"
            case mirrorNfo = "mirror_nfo"
            case mirrorEpisodeThumbs = "mirror_episode_thumbs"
        }
    }

    /// 刮削管线的用户偏好。所有字段的默认值即当前写死的行为。
    struct MetadataScrapeSettingInput: Codable, Hashable, Sendable {
        /// 元数据语言优先级（1~3 项）；首位为主语言（请求语言），空 = 跟随环境变量 TMDB_LANGUAGE + en-US 兜底
        var languagePriority: [String]?
        /// 内容分级的地区优先级：按顺序取第一个有分级数据的地区
        var certCountryPriority: [String]?
        /// 海报选择：default=TMDB 默认（与发现页一致）；language=按语言优先级挑选
        var posterMode: String?
        /// 海报语言优先级（poster_mode=language 时生效）
        var posterLanguagePriority: [String]?
        /// 背景图语言优先级；「无文字」排首位即无文字优先（现状）
        var backdropLanguagePriority: [String]?
        /// 海报最低宽度门槛；0 = 不限制
        var posterMinWidth: Int?
        /// 背景图最低宽度门槛；0 = 不限制
        var backdropMinWidth: Int?
        /// 海报档位；空 = 跟随环境变量
        var posterSize: String?
        /// 背景档位；空 = 跟随环境变量
        var backdropSize: String?
        /// 分集剧照档位；空 = 跟随环境变量
        var stillSize: String?
        /// 条目目录模板；空 = 默认 {title} ({year})
        var namingEntryDir: String?
        /// 电影文件名模板；空 = 默认 {title} ({year})
        var namingMovieFile: String?
        /// 季目录模板；空 = 默认 Season {season:02d}
        var namingSeasonDir: String?
        /// 剧集文件名模板；空 = 默认 {title} ({year}) - S{season:02d}E{episode:02d}
        var namingEpisodeFile: String?
        /// 镜像条目图片到媒体目录（poster/fanart/季海报）
        var mirrorImages: Bool?
        /// 镜像 NFO 元数据到媒体目录
        var mirrorNfo: Bool?
        /// 镜像分集剧照（长剧集写入量最大，可单独关）
        var mirrorEpisodeThumbs: Bool?

        enum CodingKeys: String, CodingKey {
            case languagePriority = "language_priority"
            case certCountryPriority = "cert_country_priority"
            case posterMode = "poster_mode"
            case posterLanguagePriority = "poster_language_priority"
            case backdropLanguagePriority = "backdrop_language_priority"
            case posterMinWidth = "poster_min_width"
            case backdropMinWidth = "backdrop_min_width"
            case posterSize = "poster_size"
            case backdropSize = "backdrop_size"
            case stillSize = "still_size"
            case namingEntryDir = "naming_entry_dir"
            case namingMovieFile = "naming_movie_file"
            case namingSeasonDir = "naming_season_dir"
            case namingEpisodeFile = "naming_episode_file"
            case mirrorImages = "mirror_images"
            case mirrorNfo = "mirror_nfo"
            case mirrorEpisodeThumbs = "mirror_episode_thumbs"
        }
    }

    /// 清理缺失记录（只删台账行，绝不动磁盘）。media_item_id 缺省 = 清整库。
    struct MissingClearPayload: Codable, Hashable, Sendable {
        /// 所属媒体库 id
        var libraryId: Int
        /// 只清理该条目的缺失记录；不传=清理整库
        var mediaItemId: Int?

        enum CodingKeys: String, CodingKey {
            case libraryId = "library_id"
            case mediaItemId = "media_item_id"
        }
    }

    /// 缺失清单里的一个文件。
    struct MissingFileView: Codable, Hashable, Sendable {
        var id: Int
        var filePath: String
        var seasonNumber: Int
        var episodeNumber: Int
        var sizeBytes: Int

        enum CodingKeys: String, CodingKey {
            case id
            case filePath = "file_path"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case sizeBytes = "size_bytes"
        }
    }

    /// 缺失清单的一行：按媒体条目聚合（一个条目可能缺多个文件）。
    struct MissingItemView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var kind: API.MediaKind
        /// TMDB 条目 ID；本地来源条目为 null
        var tmdbId: Int?
        var title: String
        var year: Int?
        var posterUrl: String?
        /// 该条目已有订阅时给出——清理记录前提示用户（订阅可能重新下回来）
        var subscriptionId: Int?
        var files: [API.MissingFileView]

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case kind
            case tmdbId = "tmdb_id"
            case title
            case year
            case posterUrl = "poster_url"
            case subscriptionId = "subscription_id"
            case files
        }
    }

    /// 模型目录条目：agent 做上下文预算 / 能力判断的依据。
    /// token 三元组的语义（None 一律表示官方未单独公布）：
    /// - context_window     输入+输出共享的总上下文；
    /// - max_input_tokens   单独的输入上限（百炼公布，OpenAI 不单独公布）；
    /// - max_output_tokens  单次响应的输出上限，agent 设 max_tokens 的依据。
    struct ModelInfo: Codable, Hashable, Sendable {
        var id: String
        var contextWindow: Int?
        var maxInputTokens: Int?
        var maxOutputTokens: Int?
        var supportsTools: Bool
        var supportsParallelToolCalls: Bool
        var supportsThinking: Bool
        var maxThinkingTokens: Int?
        var thinkingControl: API.ThinkingControl?
        var modalities: [String]
        /// 本模型的思考档位菜单（服务端推导，前端不必理解方言）。
        /// 空列表 = 无菜单，UI 隐藏选择器。budget 制未声明预算上限时退化为
        /// 仅开关（没有预算可分段）。
        var thinkingLevels: [String] = []

        enum CodingKeys: String, CodingKey {
            case id
            case contextWindow = "context_window"
            case maxInputTokens = "max_input_tokens"
            case maxOutputTokens = "max_output_tokens"
            case supportsTools = "supports_tools"
            case supportsParallelToolCalls = "supports_parallel_tool_calls"
            case supportsThinking = "supports_thinking"
            case maxThinkingTokens = "max_thinking_tokens"
            case thinkingControl = "thinking_control"
            case modalities
            case thinkingLevels = "thinking_levels"
        }
    }

    /// 模型目录条目：agent 做上下文预算 / 能力判断的依据。
    /// token 三元组的语义（None 一律表示官方未单独公布）：
    /// - context_window     输入+输出共享的总上下文；
    /// - max_input_tokens   单独的输入上限（百炼公布，OpenAI 不单独公布）；
    /// - max_output_tokens  单次响应的输出上限，agent 设 max_tokens 的依据。
    struct ModelInfoInput: Codable, Hashable, Sendable {
        var id: String
        var contextWindow: Int?
        var maxInputTokens: Int?
        var maxOutputTokens: Int?
        var supportsTools: Bool?
        var supportsParallelToolCalls: Bool?
        var supportsThinking: Bool?
        var maxThinkingTokens: Int?
        var thinkingControl: API.ThinkingControlInput?
        var modalities: [String]?

        enum CodingKeys: String, CodingKey {
            case id
            case contextWindow = "context_window"
            case maxInputTokens = "max_input_tokens"
            case maxOutputTokens = "max_output_tokens"
            case supportsTools = "supports_tools"
            case supportsParallelToolCalls = "supports_parallel_tool_calls"
            case supportsThinking = "supports_thinking"
            case maxThinkingTokens = "max_thinking_tokens"
            case thinkingControl = "thinking_control"
            case modalities
        }
    }

    /// 「检查模型更新」的结果（NER 模型独立于代码更新，见设计文档）。
    struct ModelUpdateCheckView: Codable, Hashable, Sendable {
        var currentTag: String?
        var latestTag: String
        var updateAvailable: Bool
        var installable: Bool
        var publishedAt: String

        enum CodingKeys: String, CodingKey {
            case currentTag = "current_tag"
            case latestTag = "latest_tag"
            case updateAvailable = "update_available"
            case installable
            case publishedAt = "published_at"
        }
    }

    /// 侧边栏主导航的个人排序。
    /// 只存**顺序**（导航项 id 的列表），不存导航项本身——导航有哪些项、叫什么、
    /// 什么权限可见，全部由前端与权限决定，这里存下来的仅仅是"这个人希望它们按
    /// 什么次序排"。
    /// 因此本字段是提示而非契约，前端按"排过的按此顺序在前，没排过的按内置默认
    /// 顺序追加在后"合并（见 apps/web/lib/sidebar-nav.ts）：
    /// - 版本升级新增的导航入口，在存过排序的老用户那里也一定会出现（追加在后），
    /// 不会因为不在这个列表里而永远消失——这是这类"存死一份顺序"功能最常见的事故；
    /// - 已经不存在的 id（导航项被删）读取时直接忽略，无需迁移。
    /// 上限 32 只是防脏数据无限增长的安全阀，不是产品限制（主导航实际只有个位数项）。
    struct NavUiPrefs: Codable, Hashable, Sendable {
        /// 侧栏主导航的展示顺序（导航项 id）；空列表 = 用内置默认顺序
        var order: [String]

        enum CodingKeys: String, CodingKey {
            case order
        }
    }

    /// 侧边栏主导航的个人排序。
    /// 只存**顺序**（导航项 id 的列表），不存导航项本身——导航有哪些项、叫什么、
    /// 什么权限可见，全部由前端与权限决定，这里存下来的仅仅是"这个人希望它们按
    /// 什么次序排"。
    /// 因此本字段是提示而非契约，前端按"排过的按此顺序在前，没排过的按内置默认
    /// 顺序追加在后"合并（见 apps/web/lib/sidebar-nav.ts）：
    /// - 版本升级新增的导航入口，在存过排序的老用户那里也一定会出现（追加在后），
    /// 不会因为不在这个列表里而永远消失——这是这类"存死一份顺序"功能最常见的事故；
    /// - 已经不存在的 id（导航项被删）读取时直接忽略，无需迁移。
    /// 上限 32 只是防脏数据无限增长的安全阀，不是产品限制（主导航实际只有个位数项）。
    struct NavUiPrefsInput: Codable, Hashable, Sendable {
        /// 侧栏主导航的展示顺序（导航项 id）；空列表 = 用内置默认顺序
        var order: [String]?

        enum CodingKeys: String, CodingKey {
            case order
        }
    }

    /// 保存请求体：与配置域字段一一对应。
    /// 整体覆盖语义：保存会替换全部字段（未传字段按默认值处理），
    /// 建议先读取当前配置、改动后整体写回。
    struct NetworkConfigPayload: Codable, Hashable, Sendable {
        /// 代理模式：off=全部直连；env=代理地址取自环境变量；manual=手动填写（需同时给 proxy_url）
        var proxyMode: String?
        /// manual 模式的代理地址，如 http://127.0.0.1:7890 或 socks5://…（可含账号密码）
        var proxyUrl: String?
        /// 走代理的服务 id 列表：tmdb / image / douban / llm / telegram / discord / webhook / github / site:<站点id>；不在列表内的服务直连
        var proxyServices: [String]?
        /// TMDB 接口镜像地址；留空用默认（见读取接口的 mirror_defaults）
        var tmdbApiBaseUrl: String?
        /// TMDB 图床镜像地址；留空用默认
        var tmdbImageBaseUrl: String?
        /// 豆瓣接口镜像地址；留空用默认
        var doubanApiBaseUrl: String?

        enum CodingKeys: String, CodingKey {
            case proxyMode = "proxy_mode"
            case proxyUrl = "proxy_url"
            case proxyServices = "proxy_services"
            case tmdbApiBaseUrl = "tmdb_api_base_url"
            case tmdbImageBaseUrl = "tmdb_image_base_url"
            case doubanApiBaseUrl = "douban_api_base_url"
        }
    }

    /// 读取响应：配置本体 + 前端渲染所需的目录与默认值。
    struct NetworkConfigView: Codable, Hashable, Sendable {
        /// 代理模式：off=全部直连；env=代理地址取自环境变量；manual=手动填写（需同时给 proxy_url）
        var proxyMode: String
        /// manual 模式的代理地址，如 http://127.0.0.1:7890 或 socks5://…（可含账号密码）
        var proxyUrl: String
        /// 走代理的服务 id 列表：tmdb / image / douban / llm / telegram / discord / webhook / github / site:<站点id>；不在列表内的服务直连
        var proxyServices: [String]
        /// TMDB 接口镜像地址；留空用默认（见读取接口的 mirror_defaults）
        var tmdbApiBaseUrl: String
        /// TMDB 图床镜像地址；留空用默认
        var tmdbImageBaseUrl: String
        /// 豆瓣接口镜像地址；留空用默认
        var doubanApiBaseUrl: String
        var services: [API.EgressServiceOption]
        /// 三个镜像地址的生效默认值（设置为空时的回落）
        var mirrorDefaults: [String: String]
        /// 环境变量中探测到的代理地址；env 模式下供用户确认
        var envProxyDetected: String

        enum CodingKeys: String, CodingKey {
            case proxyMode = "proxy_mode"
            case proxyUrl = "proxy_url"
            case proxyServices = "proxy_services"
            case tmdbApiBaseUrl = "tmdb_api_base_url"
            case tmdbImageBaseUrl = "tmdb_image_base_url"
            case doubanApiBaseUrl = "douban_api_base_url"
            case services
            case mirrorDefaults = "mirror_defaults"
            case envProxyDetected = "env_proxy_detected"
        }
    }

    struct NetworkTestPayload: Codable, Hashable, Sendable {
        /// 要测试连通性的服务 id：tmdb / image / douban / llm / telegram / discord / webhook / github / site:<站点id>
        var service: String

        enum CodingKeys: String, CodingKey {
            case service
        }
    }

    struct NetworkTestResult: Codable, Hashable, Sendable {
        var ok: Bool
        var latencyMs: Int?
        var message: String

        enum CodingKeys: String, CodingKey {
            case ok
            case latencyMs = "latency_ms"
            case message
        }
    }

    /// 一条待处理事项（面板列表行）。
    struct NoticeView: Codable, Hashable, Sendable {
        var id: Int
        var severity: String
        var source: String
        var title: String
        var message: String
        var payload: [String: API.JSONValue]
        var createdAt: String
        var updatedAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case severity
            case source
            case title
            case message
            case payload
            case createdAt = "created_at"
            case updatedAt = "updated_at"
        }
    }

    /// 整理预览：完整的「将要发生什么」清单，用户确认后才执行。
    struct OrganizePreviewView: Codable, Hashable, Sendable {
        /// 台账在位文件总数（= 改名 + 已规范 + 跳过）
        var total: Int
        /// 已符合规范命名的文件数
        var alreadyOk: Int
        var renames: [API.OrganizeRenameView]
        var skips: [API.OrganizeSkipView]
        /// 条目目录改名时跟着搬的镜像资产（poster.jpg / fanart.jpg / seasonNN-poster.jpg / movie.nfo / tvshow.nfo）——不搬走旧目录就清不掉
        var entryAssets: [API.OrganizeSidecarView]

        enum CodingKeys: String, CodingKey {
            case total
            case alreadyOk = "already_ok"
            case renames
            case skips
            case entryAssets = "entry_assets"
        }
    }

    /// 预览里的一条改名计划：旧路径 → 规范路径。
    struct OrganizeRenameView: Codable, Hashable, Sendable {
        var fileId: Int
        /// 所属条目——前端按条目分组展示
        var mediaItemId: Int
        var title: String
        var year: Int?
        var sourcePath: String
        var targetPath: String
        /// 相对所在库根的旧路径（展示用）
        var sourceRel: String
        /// 相对所在库根的规范路径（展示用）
        var targetRel: String
        var sizeBytes: Int
        var sidecars: [API.OrganizeSidecarView]

        enum CodingKeys: String, CodingKey {
            case fileId = "file_id"
            case mediaItemId = "media_item_id"
            case title
            case year
            case sourcePath = "source_path"
            case targetPath = "target_path"
            case sourceRel = "source_rel"
            case targetRel = "target_rel"
            case sizeBytes = "size_bytes"
            case sidecars
        }
    }

    /// 跟随主文件改名的附属文件（字幕等）。
    struct OrganizeSidecarView: Codable, Hashable, Sendable {
        var sourcePath: String
        var targetPath: String

        enum CodingKeys: String, CodingKey {
            case sourcePath = "source_path"
            case targetPath = "target_path"
        }
    }

    /// 预览里的一条跳过说明：哪个文件、为什么不动它。
    struct OrganizeSkipView: Codable, Hashable, Sendable {
        var filePath: String
        var reason: String

        enum CodingKeys: String, CodingKey {
            case filePath = "file_path"
            case reason
        }
    }

    /// 整理启动响应。
    struct OrganizeStartView: Codable, Hashable, Sendable {
        var started: Bool
        var message: String
        /// 持久化后台作业 ID，可在活动页继续观察
        var jobId: String
        /// false 表示复用了仍在进行的同一作业
        var created: Bool

        enum CodingKeys: String, CodingKey {
            case started
            case message
            case jobId = "job_id"
            case created
        }
    }

    /// 一条路径映射：movieclaw 视角的目录前缀 → 下载器视角的对应前缀。
    /// 跨容器/跨主机部署时同一块盘两边挂载路径不同（movieclaw 看到
    /// ``/data/downloads``，下载器容器里是 ``/downloads``），提交下载前
    /// 按最长前缀把保存目录翻译成下载器视角。视角一致的部署无需配置。
    struct PathMapping: Codable, Hashable, Sendable {
        /// movieclaw 上的路径前缀
        var local: String
        /// 下载器上的对应路径前缀
        var remote: String

        enum CodingKeys: String, CodingKey {
            case local
            case remote
        }
    }

    /// 一条路径映射的可达性体检结论（services.downloader_paths.PathProbe）。
    struct PathProbeView: Codable, Hashable, Sendable {
        /// movieclaw 视角路径
        var local: String
        /// 下载器视角路径
        var remote: String
        /// ok / empty / not_dir / missing / unmapped
        var state: String
        /// 结论与该做什么（中文）
        var detail: String

        enum CodingKeys: String, CodingKey {
            case local
            case remote
            case state
            case detail
        }
    }

    /// 历史根路径迁移修复的范围：旧前缀与当前配置中的目标前缀。
    struct PathReconcilePayload: Codable, Hashable, Sendable {
        /// 已移除、需要收口的旧根路径（绝对路径）
        var oldRoot: String
        /// 当前媒体库配置中的目标根路径（绝对路径）
        var newRoot: String

        enum CodingKeys: String, CodingKey {
            case oldRoot = "old_root"
            case newRoot = "new_root"
        }
    }

    /// 路径迁移修复预览：所有数字均只涉及数据库台账，磁盘文件永不删除。
    struct PathReconcilePreviewView: Codable, Hashable, Sendable {
        var libraryId: Int
        var oldRoot: String
        var newRoot: String
        var samePathCandidates: Int
        var safeMerges: Int
        var markedMissing: Int
        var conflicts: [String]
        var unconfirmed: [String]
        var oldRowsToDeleteFromLedger: Int
        var diskFilesToDelete: Int

        enum CodingKeys: String, CodingKey {
            case libraryId = "library_id"
            case oldRoot = "old_root"
            case newRoot = "new_root"
            case samePathCandidates = "same_path_candidates"
            case safeMerges = "safe_merges"
            case markedMissing = "marked_missing"
            case conflicts
            case unconfirmed
            case oldRowsToDeleteFromLedger = "old_rows_to_delete_from_ledger"
            case diskFilesToDelete = "disk_files_to_delete"
        }
    }

    /// 待更新快照：最近一次检查（定时或手动）留下的结论，读它不触网。
    /// 前端的更新提醒完全建立在这个视图上——侧栏常驻徽标据此点亮，「设置 →
    /// 应用」进页即用它直接渲染出新版本卡片，用户不必再手点一次「检查更新」。
    struct PendingUpdateView: Codable, Hashable, Sendable {
        var appVersion: String?
        var appCompatible: Bool
        var appChangelog: String
        var appPublishedAt: String
        var modelTag: String?
        var checkedAt: String?

        enum CodingKeys: String, CodingKey {
            case appVersion = "app_version"
            case appCompatible = "app_compatible"
            case appChangelog = "app_changelog"
            case appPublishedAt = "app_published_at"
            case modelTag = "model_tag"
            case checkedAt = "checked_at"
        }
    }

    /// 人物页作品列表的一格：一部我库里的片 + 这个人在其中的身份。
    struct PersonCreditView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var kind: API.MediaKind
        var tmdbId: Int
        var title: String
        var year: Int?
        /// 海报（优先本地刮削资产，回落 TMDB 图床）；都没有为 NULL
        var posterUrl: String?
        /// 任一拥有该条目文件的库 id，供前端跳条目详情页；文件已全部删除、只剩档案时为 NULL，前端渲染为不可点
        var libraryId: Int?
        /// 身份：cast=演员 / director=导演（剧集为主创）
        var department: String
        /// 饰演角色（仅 cast）
        var character: String?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case kind
            case tmdbId = "tmdb_id"
            case title
            case year
            case posterUrl = "poster_url"
            case libraryId = "library_id"
            case department
            case character
        }
    }

    /// 人物页的完整数据。
    struct PersonView: Codable, Hashable, Sendable {
        var tmdbPersonId: Int
        var name: String
        var originalName: String?
        /// 头像（TMDB 图床，经前端缓存代理）；TMDB 无照片为 NULL
        var avatarUrl: String?
        /// 库内作品，主演在前、同档按剧组主次顺序与年份倒序（见 PersonRepository）
        var credits: [API.PersonCreditView]

        enum CodingKeys: String, CodingKey {
            case tmdbPersonId = "tmdb_person_id"
            case name
            case originalName = "original_name"
            case avatarUrl = "avatar_url"
            case credits
        }
    }

    /// PGS 自动转 SRT 的候选轨道与当前设备能力。
    struct PgsConversionView: Codable, Hashable, Sendable {
        var candidateKey: String
        var language: String?
        var available: Bool
        var engine: String?
        var platform: String
        var architecture: String
        var cached: Bool
        var message: String
        var suggestions: [String]
        var ocrLanguage: String?
        var ocrLanguageLabel: String?
        var languageConfirmationRequired: Bool
        var languageReason: String
        var languageOptions: [API.PgsOcrLanguageOptionView]

        enum CodingKeys: String, CodingKey {
            case candidateKey = "candidate_key"
            case language
            case available
            case engine
            case platform
            case architecture
            case cached
            case message
            case suggestions
            case ocrLanguage = "ocr_language"
            case ocrLanguageLabel = "ocr_language_label"
            case languageConfirmationRequired = "language_confirmation_required"
            case languageReason = "language_reason"
            case languageOptions = "language_options"
        }
    }

    /// 当前设备可用的一种 PGS 图片语言。
    struct PgsOcrLanguageOptionView: Codable, Hashable, Sendable {
        var code: String
        var label: String

        enum CodingKeys: String, CodingKey {
            case code
            case label
        }
    }

    /// 连接自检结果。
    struct PingResult: Codable, Hashable, Sendable {
        var ok: Bool
        var appName: String

        enum CodingKeys: String, CodingKey {
            case ok
            case appName = "app_name"
        }
    }

    /// 订阅链路体检的整体结论（订阅设定页与订阅列表警示横幅共用）。
    struct PipelineHealthView: Codable, Hashable, Sendable {
        /// 整体状态：库链路 + 全局段（站点/下载器）的最坏值
        var status: String
        /// 链路有 error 的库数
        var errorCount: Int
        var warnCount: Int
        /// 资源搜索段（全局，链路第一环）
        var siteCheck: API.HealthCheckView
        /// 是否有可用的默认下载器
        var downloaderOk: Bool
        /// 是否配置过站点（无论当前可用与否）——开局清单只看它，配置过但失效的老用户看到的是体检红项而非新手清单
        var sitesConfigured: Bool
        /// 是否配置过下载器（同上语义）
        var downloadersConfigured: Bool
        /// 按根因聚合的问题卡（error 在前）——前端置顶展示为「需要处理 N 件事」
        var issues: [API.HealthIssueView]
        var libraries: [API.LibraryPipelineView]

        enum CodingKeys: String, CodingKey {
            case status
            case errorCount = "error_count"
            case warnCount = "warn_count"
            case siteCheck = "site_check"
            case downloaderOk = "downloader_ok"
            case sitesConfigured = "sites_configured"
            case downloadersConfigured = "downloaders_configured"
            case issues
            case libraries
        }
    }

    /// 远程 Worker 最近一次产物上传的脱敏记录。
    struct PlaybackArtifactUploadView: Codable, Hashable, Sendable {
        var name: String
        var status: Int
        var receivedBytes: Int
        var contentLength: Int?
        var transferEncoding: String?
        var occurredAtMs: Int

        enum CodingKeys: String, CodingKey {
            case name
            case status
            case receivedBytes = "received_bytes"
            case contentLength = "content_length"
            case transferEncoding = "transfer_encoding"
            case occurredAtMs = "occurred_at_ms"
        }
    }

    /// 进度条上的章节刻度（docs/design/player-feel.md §2.C1）。
    /// 只有起点与标题：预览图由 trickplay 雪碧图负责，章节图片再塞一份会把
    /// 起播响应撑大好几倍，而进度条上根本画不下。
    struct PlaybackChapterMarkView: Codable, Hashable, Sendable {
        var startMs: Int
        var title: String?

        enum CodingKeys: String, CodingKey {
            case startMs = "start_ms"
            case title
        }
    }

    /// 播放器客户端事件上报：把浏览器侧的现场（MediaError 详情、播放器状态）
    /// 落进服务端日志。iPhone 上的播放故障没有任何本地可看的控制台，服务端
    /// 日志是唯一能拿到客户端真相的地方。
    struct PlaybackClientLogPayload: Codable, Hashable, Sendable {
        var event: String
        var detail: [String: API.JSONValue]?

        enum CodingKeys: String, CodingKey {
            case event
            case detail
        }
    }

    /// 一次播放决策请求。``file_id`` 与播放单元二选一——给单元时服务端会在
    /// 该单元的全部版本文件里择优（能直通的 1080p 胜过要转码的 2160p）。
    struct PlaybackDecideRequest: Codable, Hashable, Sendable {
        var fileId: Int?
        var mediaItemId: Int?
        var seasonNumber: Int?
        var episodeNumber: Int?
        var capability: API.ClientCapabilityIn
        var failedTiers: [Int]?
        var audioTrack: String?
        var subtitleTrack: String?
        var maxHeight: Int?
        var deviceId: String?
        var downlinkBps: Int?

        enum CodingKeys: String, CodingKey {
            case fileId = "file_id"
            case mediaItemId = "media_item_id"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case capability
            case failedTiers = "failed_tiers"
            case audioTrack = "audio_track"
            case subtitleTrack = "subtitle_track"
            case maxHeight = "max_height"
            case deviceId = "device_id"
            case downlinkBps = "downlink_bps"
        }
    }

    /// 决策结果的三态并集。``outcome`` 决定其余字段哪些有值。
    /// - ``plan``    —— 可以播，按 ``tier`` 走；
    /// - ``consent`` —— 需要用户同意开启软件转码（§3.6）；
    /// - ``rejected``—— 放不了，``reason`` / ``suggestion`` 面向用户。
    struct PlaybackDecisionView: Codable, Hashable, Sendable {
        var outcome: String
        var tier: Int?
        var fileId: Int?
        var container: String?
        var video: API.VideoPlanView?
        var audio: API.AudioPlanView?
        var audioTracks: [API.AudioTrackView]
        var subtitles: [API.SubtitlePlanView]
        var degradedFrom: Int?
        var costHint: String?
        var canSelfEnable: Bool?
        var settingNamespace: String?
        var settingKey: String?
        var reason: String
        var suggestion: String?

        enum CodingKeys: String, CodingKey {
            case outcome
            case tier
            case fileId = "file_id"
            case container
            case video
            case audio
            case audioTracks = "audio_tracks"
            case subtitles
            case degradedFrom = "degraded_from"
            case costHint = "cost_hint"
            case canSelfEnable = "can_self_enable"
            case settingNamespace = "setting_namespace"
            case settingKey = "setting_key"
            case reason
            case suggestion
        }
    }

    /// 播放器诊断面板使用的会话快照，不包含任何签名 URL 或令牌。
    struct PlaybackDiagnosticsView: Codable, Hashable, Sendable {
        var sessionState: String
        var sessionError: String?
        var processingMode: String
        var executionLocation: String
        var backend: String?
        var encoder: String?
        var workerId: String?
        var workerVersion: String?
        var workerPlatform: String?
        var workerArch: String?
        var ffmpegVersion: String?
        var workerOnline: Bool?
        var workerLastSeenSeconds: Double?
        var jobId: String?
        var attemptId: String?
        var jobState: String?
        var jobOutTimeMs: Int?
        var jobSpeed: String?
        var jobPhase: String?
        var jobExitCode: Int?
        var jobError: String?
        var jobStderrTail: String?
        var headSegment: Int?
        var highestProducedSegment: Int?
        var requestedSegment: Int?
        var servedSegment: Int?
        var segmentWaitMs: Int?
        var segmentStatus: Int?
        var pendingSegments: [Int]
        var failedSegments: [Int]
        var historicalFailedSegments: [Int]
        var recentUploads: [API.PlaybackArtifactUploadView]
        var cacheBytes: Int
        var totalSegments: Int?
        var leadSeconds: Double?
        var pauseReasons: [String]
        var cacheHit: Bool
        var cachedSegments: Int

        enum CodingKeys: String, CodingKey {
            case sessionState = "session_state"
            case sessionError = "session_error"
            case processingMode = "processing_mode"
            case executionLocation = "execution_location"
            case backend
            case encoder
            case workerId = "worker_id"
            case workerVersion = "worker_version"
            case workerPlatform = "worker_platform"
            case workerArch = "worker_arch"
            case ffmpegVersion = "ffmpeg_version"
            case workerOnline = "worker_online"
            case workerLastSeenSeconds = "worker_last_seen_seconds"
            case jobId = "job_id"
            case attemptId = "attempt_id"
            case jobState = "job_state"
            case jobOutTimeMs = "job_out_time_ms"
            case jobSpeed = "job_speed"
            case jobPhase = "job_phase"
            case jobExitCode = "job_exit_code"
            case jobError = "job_error"
            case jobStderrTail = "job_stderr_tail"
            case headSegment = "head_segment"
            case highestProducedSegment = "highest_produced_segment"
            case requestedSegment = "requested_segment"
            case servedSegment = "served_segment"
            case segmentWaitMs = "segment_wait_ms"
            case segmentStatus = "segment_status"
            case pendingSegments = "pending_segments"
            case failedSegments = "failed_segments"
            case historicalFailedSegments = "historical_failed_segments"
            case recentUploads = "recent_uploads"
            case cacheBytes = "cache_bytes"
            case totalSegments = "total_segments"
            case leadSeconds = "lead_seconds"
            case pauseReasons = "pause_reasons"
            case cacheHit = "cache_hit"
            case cachedSegments = "cached_segments"
        }
    }

    /// 正在播放文件的技术规格（来自 library_file 台账）。
    struct PlaybackFileSpec: Codable, Hashable, Sendable {
        var resolution: String?
        var videoCodec: String?
        var hdr: String?
        var container: String?
        var bitRate: Int?
        var sizeBytes: Int?

        enum CodingKeys: String, CodingKey {
            case resolution
            case videoCodec = "video_codec"
            case hdr
            case container
            case bitRate = "bit_rate"
            case sizeBytes = "size_bytes"
        }
    }

    /// ASS 字幕依赖的内嵌字体地址（已带签名 token）。
    struct PlaybackFontsView: Codable, Hashable, Sendable {
        var fonts: [String]

        enum CodingKeys: String, CodingKey {
            case fonts
        }
    }

    /// 清除观看记录的结果：删掉了多少条状态与多少条播放质量指标。
    struct PlaybackHistoryClearView: Codable, Hashable, Sendable {
        /// 删除的观看状态行数（续播点/已看/播放次数）
        var deletedStates: Int
        /// 删除的播放质量指标行数
        var deletedMetrics: Int

        enum CodingKeys: String, CodingKey {
            case deletedStates = "deleted_states"
            case deletedMetrics = "deleted_metrics"
        }
    }

    struct PlaybackHistoryView: Codable, Hashable, Sendable {
        var entries: [API.PlaybackLogEntryView]
        /// 不在你可见范围内的记录数
        var hiddenCount: Int
        var hasMore: Bool
        var nextCursor: Int?

        enum CodingKeys: String, CodingKey {
            case entries
            case hiddenCount = "hidden_count"
            case hasMore = "has_more"
            case nextCursor = "next_cursor"
        }
    }

    /// 播放页要的条目信息，只有播放器用得上的那几样。
    /// 播放路由只带 ``media_item_id``——它以 ``(kind, tmdb_id)`` 为锚、幂等复用，
    /// 比库自增 id 稳定得多，分享出去的地址不会因删库重建而失效（§6.10）。库归
    /// 属由服务端按成员可见性解析，前端只在「退出播放跳回条目页」时用到它。
    struct PlaybackItemView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var libraryId: Int
        var kind: String
        var title: String
        var year: Int?
        var posterUrl: String?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case libraryId = "library_id"
            case kind
            case title
            case year
            case posterUrl = "poster_url"
        }
    }

    /// 一场播放（playback_log 的一行）。
    struct PlaybackLogEntryView: Codable, Hashable, Sendable {
        var id: Int
        var memberName: String
        var media: API.MediaActivityTarget
        var client: String
        var deviceName: String
        var startedAt: String
        var endedAt: String?
        /// 实际观看时长（毫秒）
        var watchedMs: Int
        var startPositionMs: Int
        var endPositionMs: Int
        var durationMs: Int?
        var progressPercent: Int?
        /// 本场是否看完
        var completed: Bool

        enum CodingKeys: String, CodingKey {
            case id
            case memberName = "member_name"
            case media
            case client
            case deviceName = "device_name"
            case startedAt = "started_at"
            case endedAt = "ended_at"
            case watchedMs = "watched_ms"
            case startPositionMs = "start_position_ms"
            case endPositionMs = "end_position_ms"
            case durationMs = "duration_ms"
            case progressPercent = "progress_percent"
            case completed
        }
    }

    /// 一次标记：目标 + 要改成什么。
    /// 目标的表达与 Jellyfin 的 Series / Season / Episode 三级一一对应：不带季集
    /// = 整个条目（电影，或整剧级联到全部集）；只带季 = 整季；季集都带 = 单集。
    /// 电影也可以像播放接口那样带哨兵 ``(0, 0)``，落到同一个单元。
    /// ``played`` 与 ``favorite`` 至少给一个，没给的那个保持原值。
    struct PlaybackMarksRequest: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var seasonNumber: Int?
        var episodeNumber: Int?
        var played: Bool?
        var favorite: Bool?
        var deviceId: String?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case played
            case favorite
            case deviceId = "device_id"
        }
    }

    /// 目标在当前成员名下的已看 / 收藏状态。读接口与写接口同一形状，
    /// 写完直接拿它刷新按钮，不必再查一次。
    struct PlaybackMarksView: Codable, Hashable, Sendable {
        var played: Bool
        var isFavorite: Bool
        var unplayedCount: Int?

        enum CodingKeys: String, CodingKey {
            case played
            case isFavorite = "is_favorite"
            case unplayedCount = "unplayed_count"
        }
    }

    /// 一次播放结束时上报的质量快照。指标口径按 CTA-2066，不自创。
    struct PlaybackMetricPayload: Codable, Hashable, Sendable {
        var libraryFileId: Int?
        var tier: Int
        var degradedFrom: Int?
        var engine: String?
        var hwBackend: String?
        var ttffMs: Int?
        var rebufferMs: Int?
        var rebufferCount: Int?
        var seekCount: Int?
        var droppedFrames: Int?
        var totalFrames: Int?
        var watchedMs: Int?

        enum CodingKeys: String, CodingKey {
            case libraryFileId = "library_file_id"
            case tier
            case degradedFrom = "degraded_from"
            case engine
            case hwBackend = "hw_backend"
            case ttffMs = "ttff_ms"
            case rebufferMs = "rebuffer_ms"
            case rebufferCount = "rebuffer_count"
            case seekCount = "seek_count"
            case droppedFrames = "dropped_frames"
            case totalFrames = "total_frames"
            case watchedMs = "watched_ms"
        }
    }

    /// 策略保存请求。**全字段可选，None = 不动这一项**——同意弹窗只翻
    /// software_transcode_enabled 一个开关。
    struct PlaybackPolicyPayload: Codable, Hashable, Sendable {
        var softwareTranscodeEnabled: Bool?
        var trickplayEnabled: Bool?
        var transcodeCacheEnabled: Bool?

        enum CodingKeys: String, CodingKey {
            case softwareTranscodeEnabled = "software_transcode_enabled"
            case trickplayEnabled = "trickplay_enabled"
            case transcodeCacheEnabled = "transcode_cache_enabled"
        }
    }

    /// 播放策略的当前取值。字段与 PlaybackPolicySetting 一一对应。
    /// 数字上限（并发、输出高度、缓存配额）不在这里——它们已改为按机器规格
    /// 自动推导（services/playback/limits.py），不再是配置项。
    struct PlaybackPolicyView: Codable, Hashable, Sendable {
        var softwareTranscodeEnabled: Bool
        var trickplayEnabled: Bool
        var transcodeCacheEnabled: Bool
        var hardwareAvailable: Bool
        var hwBackends: [String]

        enum CodingKeys: String, CodingKey {
            case softwareTranscodeEnabled = "software_transcode_enabled"
            case trickplayEnabled = "trickplay_enabled"
            case transcodeCacheEnabled = "transcode_cache_enabled"
            case hardwareAvailable = "hardware_available"
            case hwBackends = "hw_backends"
        }
    }

    /// 一次观看状态上报。三种事件同一入口，与 Jellyfin 的 Playing /
    /// Playing/Progress / Playing/Stopped 一一对应。
    struct PlaybackProgressRequest: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var seasonNumber: Int?
        var episodeNumber: Int?
        var event: String?
        var positionMs: Int?
        var audioTrack: String?
        var subtitleTrack: String?
        var deviceId: String?
        var paused: Bool?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case event
            case positionMs = "position_ms"
            case audioTrack = "audio_track"
            case subtitleTrack = "subtitle_track"
            case deviceId = "device_id"
            case paused
        }
    }

    /// 开会话请求：在决策请求上多一个起播位置。
    struct PlaybackSessionRequest: Codable, Hashable, Sendable {
        var fileId: Int?
        var mediaItemId: Int?
        var seasonNumber: Int?
        var episodeNumber: Int?
        var capability: API.ClientCapabilityIn
        var failedTiers: [Int]?
        var audioTrack: String?
        var subtitleTrack: String?
        var maxHeight: Int?
        var deviceId: String?
        var downlinkBps: Int?
        var startMs: Int?

        enum CodingKeys: String, CodingKey {
            case fileId = "file_id"
            case mediaItemId = "media_item_id"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case capability
            case failedTiers = "failed_tiers"
            case audioTrack = "audio_track"
            case subtitleTrack = "subtitle_track"
            case maxHeight = "max_height"
            case deviceId = "device_id"
            case downlinkBps = "downlink_bps"
            case startMs = "start_ms"
        }
    }

    /// 开会话的结果。
    /// 三态里只有 ``plan`` 才会真的起会话；``consent`` / ``rejected`` 原样把
    /// 决策带回前端，由它渲染弹窗或错误说明。
    struct PlaybackSessionView: Codable, Hashable, Sendable {
        var decision: API.PlaybackDecisionView
        var sessionId: String?
        var streamUrl: String?
        var startMs: Int
        var timeline: String
        var subtitleUrls: [String]
        var masterUrl: String?
        var hwBackend: String?
        var watch: API.PlaybackStateView?
        var source: API.PlaybackSourceView?
        var chapters: [API.PlaybackChapterMarkView]

        enum CodingKeys: String, CodingKey {
            case decision
            case sessionId = "session_id"
            case streamUrl = "stream_url"
            case startMs = "start_ms"
            case timeline
            case subtitleUrls = "subtitle_urls"
            case masterUrl = "master_url"
            case hwBackend = "hw_backend"
            case watch
            case source
            case chapters
        }
    }

    /// 源文件的客观规格（台账真值），诊断面板「源 → 处理」层次的左半边。
    /// Emby 式面板的关键是把「源是什么」与「我们对它做了什么」摆在一起——
    /// 只报处理结果，用户看不出「1080p H264 明明能直通为什么在转码」这类问题。
    struct PlaybackSourceView: Codable, Hashable, Sendable {
        var container: String?
        var resolution: String?
        var videoCodec: String?
        var hdr: String?
        var bitRate: Int?
        var frameRate: Double?
        var sizeBytes: Int?

        enum CodingKeys: String, CodingKey {
            case container
            case resolution
            case videoCodec = "video_codec"
            case hdr
            case bitRate = "bit_rate"
            case frameRate = "frame_rate"
            case sizeBytes = "size_bytes"
        }
    }

    /// 一个播放单元在当前成员名下的观看状态。续播与上报共用同一形状。
    struct PlaybackStateView: Codable, Hashable, Sendable {
        var positionMs: Int
        var played: Bool
        var playCount: Int
        var durationMs: Int?
        var audioTrack: String?
        var subtitleTrack: String?
        var endedByAdmin: Bool

        enum CodingKeys: String, CodingKey {
            case positionMs = "position_ms"
            case played
            case playCount = "play_count"
            case durationMs = "duration_ms"
            case audioTrack = "audio_track"
            case subtitleTrack = "subtitle_track"
            case endedByAdmin = "ended_by_admin"
        }
    }

    struct PlaybackStatsClientRow: Codable, Hashable, Sendable {
        var client: String
        var plays: Int
        var watchedMs: Int

        enum CodingKeys: String, CodingKey {
            case client
            case plays
            case watchedMs = "watched_ms"
        }
    }

    struct PlaybackStatsDayRow: Codable, Hashable, Sendable {
        /// 按浏览器时区的日期 YYYY-MM-DD
        var date: String
        var plays: Int
        var watchedMs: Int
        var completed: Int
        /// 当天有播放的成员数
        var members: Int

        enum CodingKeys: String, CodingKey {
            case date
            case plays
            case watchedMs = "watched_ms"
            case completed
            case members
        }
    }

    struct PlaybackStatsMemberRow: Codable, Hashable, Sendable {
        var memberId: Int
        var memberName: String
        var plays: Int
        var watchedMs: Int
        var completed: Int

        enum CodingKeys: String, CodingKey {
            case memberId = "member_id"
            case memberName = "member_name"
            case plays
            case watchedMs = "watched_ms"
            case completed
        }
    }

    /// 网页播放按档位的分解（直连 / 重封装 / 音频转码 / 硬件转码 / 软件转码）。
    struct PlaybackStatsTierRow: Codable, Hashable, Sendable {
        var tier: Int
        var label: String
        var plays: Int

        enum CodingKeys: String, CodingKey {
            case tier
            case label
            case plays
        }
    }

    struct PlaybackStatsTitleRow: Codable, Hashable, Sendable {
        var media: API.MediaActivityTarget
        var plays: Int
        var watchedMs: Int
        /// 看过这部作品的成员数
        var members: Int

        enum CodingKeys: String, CodingKey {
            case media
            case plays
            case watchedMs = "watched_ms"
            case members
        }
    }

    /// 一个周期的四个汇总数。
    struct PlaybackStatsTotals: Codable, Hashable, Sendable {
        /// 播放场次
        var plays: Int
        /// 观看总时长（毫秒）
        var watchedMs: Int
        /// 看完的场次
        var completed: Int
        /// 有播放的成员数
        var activeMembers: Int

        enum CodingKeys: String, CodingKey {
            case plays
            case watchedMs = "watched_ms"
            case completed
            case activeMembers = "active_members"
        }
    }

    /// 播放质量汇总。样本不足时各项为 null——不编数字。
    /// `direct_ratio` 是**北极星指标**：档 0 + 档 1 占全部播放的比例。这一个数
    /// 同时代表画质（没重编码）、速度（秒开）和服务器负担（不烧 GPU）。
    struct PlaybackStatsView: Codable, Hashable, Sendable {
        var sessions: Int
        var directRatio: Double?
        var degradedRatio: Double?
        var ttffP50Ms: Int?
        var ttffP95Ms: Int?
        var rebufferRatio: Double?
        var droppedRatio: Double?
        var tierCounts: [String: Int]

        enum CodingKeys: String, CodingKey {
            case sessions
            case directRatio = "direct_ratio"
            case degradedRatio = "degraded_ratio"
            case ttffP50Ms = "ttff_p50_ms"
            case ttffP95Ms = "ttff_p95_ms"
            case rebufferRatio = "rebuffer_ratio"
            case droppedRatio = "dropped_ratio"
            case tierCounts = "tier_counts"
        }
    }

    /// 一段时间内的观看统计（docs/design/activity.md「观看统计」）。
    /// 当前周期与**上一周期**成对返回：没有参照系的数字只是数据，不是洞察。
    /// ``by_day`` 与 ``previous_by_day`` 按天对齐（同为 days+1 行），主图把两条线画在
    /// 同一坐标系里。``by_hour`` 是星期 × 小时的观看时长矩阵（周一为 0 行），按浏览器
    /// 时区分桶，回答「家里什么时候有人在看」。
    struct PlaybackWatchStatsView: Codable, Hashable, Sendable {
        var days: Int
        var current: API.PlaybackStatsTotals
        var previous: API.PlaybackStatsTotals
        /// 上一周期有没有日志（日志刚开始记时没有）
        var previousAvailable: Bool
        var byDay: [API.PlaybackStatsDayRow]
        var previousByDay: [API.PlaybackStatsDayRow]
        /// 7×24 观看时长（毫秒），行=星期（0=周一），列=小时
        var byHour: [[Int]]
        var byMember: [API.PlaybackStatsMemberRow]
        var byClient: [API.PlaybackStatsClientRow]
        /// 网页播放按档位；Jellyfin 客户端恒为直连，不在内
        var byTier: [API.PlaybackStatsTierRow]
        var topTitles: [API.PlaybackStatsTitleRow]
        /// 作品榜里不在你可见范围内的条数
        var hiddenTitleCount: Int
        /// 本期最受欢迎前三：看过的成员最多，并列取时长长的；与作品榜（按时长）口径不同
        var favorites: [API.PlaybackStatsTitleRow]
        /// 上一周期的前三，用来标「蝉联 / 上期第 n / 新上榜」
        var previousFavorites: [API.PlaybackStatsTitleRow]

        enum CodingKeys: String, CodingKey {
            case days
            case current
            case previous
            case previousAvailable = "previous_available"
            case byDay = "by_day"
            case previousByDay = "previous_by_day"
            case byHour = "by_hour"
            case byMember = "by_member"
            case byClient = "by_client"
            case byTier = "by_tier"
            case topTitles = "top_titles"
            case hiddenTitleCount = "hidden_title_count"
            case favorites
            case previousFavorites = "previous_favorites"
        }
    }

    /// 预检里的一个成员：它会落到哪、多大、是否冲突。
    struct PreflightMemberView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var title: String
        var targetPaths: [String]
        var sizeBytes: Int
        /// true=需要完整复制（耗时且断开硬链接）
        var crossDevice: Bool
        /// same_anchor=目标已有这部作品的其他版本 / different_anchor=目录撞名但不是同一部片 / unknown=目标位置有内容但媒体库没有记录；null=不冲突
        var conflict: String?
        var conflictPath: String
        /// 跨盘后会断开硬链接、且源盘不会释放的字节数
        var hardlinkedBytes: Int
        /// 下载器里有同名落盘根——搬走可能让做种任务失效
        var seedingInPlace: Bool
        /// 没有可搬内容时的中文说明
        var reason: String

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case title
            case targetPaths = "target_paths"
            case sizeBytes = "size_bytes"
            case crossDevice = "cross_device"
            case conflict
            case conflictPath = "conflict_path"
            case hardlinkedBytes = "hardlinked_bytes"
            case seedingInPlace = "seeding_in_place"
            case reason
        }
    }

    /// 预检结果三态：ready 可直接渲染弹层；ambiguous 先让用户选候选；
    /// not_found 提示该条目暂无法订阅。
    struct PrepareView: Codable, Hashable, Sendable {
        var status: String
        var media: API.MediaBrief?
        var seasons: [API.SeasonOverview]
        /// 该条目已有订阅时给出，前端展示「已订阅」态
        var existingSubscriptionId: Int?
        /// 电影：媒体库里已有本片（弹层提示，不拦订阅）
        var movieOwned: Bool
        /// 建议预勾选的季号。豆瓣把剧集按季拆条目，用户点进「中餐厅 第十季」要订的就是那一季；这里给出收敛通路用首播日期定案的 TMDB 季号（与豆瓣季号未必相同）。为空表示无可信结论，前端按原默认规则勾选
        var suggestedSeasons: [Int]
        var candidates: [API.ResolveCandidateView]

        enum CodingKeys: String, CodingKey {
            case status
            case media
            case seasons
            case existingSubscriptionId = "existing_subscription_id"
            case movieOwned = "movie_owned"
            case suggestedSeasons = "suggested_seasons"
            case candidates
        }
    }

    /// 标签栏里的自定义分类标签：命名的「分类组合 × 站点组合」预设。
    struct PresetTabItem: Codable, Hashable, Sendable {
        var type: String
        /// 预设标识（创建时生成）
        var id: String
        /// 展示名称（1~16 字）
        var name: String
        var visible: Bool
        /// 勾选的一级分类；空 = 不限分类
        var categories: [API.TorrentCategory]
        /// 勾选的站点；空 = 全部可用站点
        var siteIds: [String]
        /// 图览模式：用该分类搜索时，结果页默认以图墙展示（结果页可临时切换）
        var posterMode: Bool
        /// 无痕搜索：用该分类搜索时不写入搜索历史（隐私敏感场景的开关）
        var skipHistory: Bool

        enum CodingKeys: String, CodingKey {
            case type
            case id
            case name
            case visible
            case categories
            case siteIds = "site_ids"
            case posterMode = "poster_mode"
            case skipHistory = "skip_history"
        }
    }

    /// 标签栏里的自定义分类标签：命名的「分类组合 × 站点组合」预设。
    struct PresetTabItemInput: Codable, Hashable, Sendable {
        var type: String?
        /// 预设标识（创建时生成）
        var id: String
        /// 展示名称（1~16 字）
        var name: String
        var visible: Bool
        /// 勾选的一级分类；空 = 不限分类
        var categories: [API.TorrentCategory]?
        /// 勾选的站点；空 = 全部可用站点
        var siteIds: [String]?
        /// 图览模式：用该分类搜索时，结果页默认以图墙展示（结果页可临时切换）
        var posterMode: Bool?
        /// 无痕搜索：用该分类搜索时不写入搜索历史（隐私敏感场景的开关）
        var skipHistory: Bool?

        enum CodingKeys: String, CodingKey {
            case type
            case id
            case name
            case visible
            case categories
            case siteIds = "site_ids"
            case posterMode = "poster_mode"
            case skipHistory = "skip_history"
        }
    }

    /// 试算：给定服务集合与模式，返回将暴露的工具清单。
    struct PreviewRequest: Codable, Hashable, Sendable {
        var services: [String]?
        var expandTools: Bool?

        enum CodingKeys: String, CodingKey {
            case services
            case expandTools = "expand_tools"
        }
    }

    struct PreviewView: Codable, Hashable, Sendable {
        var toolCount: Int
        var commandCount: Int
        var approxBytes: Int
        var tools: [API.ToolPreview]

        enum CodingKeys: String, CodingKey {
            case toolCount = "tool_count"
            case commandCount = "command_count"
            case approxBytes = "approx_bytes"
            case tools
        }
    }

    /// 列表页进度：total = 工单总数，wanted 子集是缺口，imported 是已入库终态。
    struct ProgressView: Codable, Hashable, Sendable {
        var total: Int
        var wanted: Int
        var grabbed: Int
        var downloaded: Int
        var imported: Int
        var upgrading: Int

        enum CodingKeys: String, CodingKey {
            case total
            case wanted
            case grabbed
            case downloaded
            case imported
            case upgrading
        }
    }

    /// 测试推送文本(缺省用默认文案)。
    struct PushTestPayload: Codable, Hashable, Sendable {
        var text: String?

        enum CodingKeys: String, CodingKey {
            case text
        }
    }

    /// 「刚刚入库」一批里的一个季集单元（电影是哨兵 0/0）。
    struct RecentArrivalUnitView: Codable, Hashable, Sendable {
        var seasonNumber: Int
        var episodeNumber: Int

        enum CodingKeys: String, CodingKey {
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
        }
    }

    /// 订阅首页「刚刚入库」的一张卡：一部作品最近入库、当前账号还没看完的那一批。
    /// 同一部作品只出一张卡；整批看完即不再返回（规则见
    /// ``services/subscription/recent_arrivals.py``）。播放入口是这一批里第一个
    /// 没看完的单元，客户端直接按 ``media.media_item_id`` + 季集起播。
    struct RecentArrivalView: Codable, Hashable, Sendable {
        var subscriptionId: Int
        var media: API.MediaBrief
        /// 播放入口：这一批里第一个没看完的单元；电影=0
        var seasonNumber: Int
        /// 播放入口的集号；电影=0
        var episodeNumber: Int
        /// 播放入口那一集的集名；电影或缺档案为空
        var episodeName: String?
        /// 播放入口那一集的剧照；电影或缺剧照为空（客户端改用 media.backdrop_url）
        var stillUrl: String?
        /// 这一批里还没看完、文件在位的全部单元（季集正序，第一个即播放入口）
        var units: [API.RecentArrivalUnitView]
        /// 播放入口看了一半时的进度（1~99）；没看过为空
        var progressPercent: Int?
        /// 这一批最近一次整理入库的时间
        var importedAt: String

        enum CodingKeys: String, CodingKey {
            case subscriptionId = "subscription_id"
            case media
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case episodeName = "episode_name"
            case stillUrl = "still_url"
            case units
            case progressPercent = "progress_percent"
            case importedAt = "imported_at"
        }
    }

    /// 重新下载：把某条目的缺失单元交回订阅管线。
    struct RedownloadPayload: Codable, Hashable, Sendable {
        /// 所属媒体库 id
        var libraryId: Int
        /// 要重新下载缺失内容的条目 id
        var mediaItemId: Int

        enum CodingKeys: String, CodingKey {
            case libraryId = "library_id"
            case mediaItemId = "media_item_id"
        }
    }

    /// 整库刷新中正在处理的一部片（并发若干路，故是列表）。
    struct RefreshActiveView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var title: String
        /// 当前阶段：拉取 TMDB 档案 / 写入元数据 / 下载图片 / …
        var phase: String

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case title
            case phase
        }
    }

    /// 预览的一组：识别结论相同的文件聚成一条，用户按组拍板。
    struct ReidentifyGroupView: Codable, Hashable, Sendable {
        var key: String
        var outcome: API.ReidentifyOutcomeView
        var fileIds: [Int]
        var fileCount: Int
        var totalSizeBytes: Int
        /// 前几个文件名
        var sampleNames: [String]

        enum CodingKeys: String, CodingKey {
            case key
            case outcome
            case fileIds = "file_ids"
            case fileCount = "file_count"
            case totalSizeBytes = "total_size_bytes"
            case sampleNames = "sample_names"
        }
    }

    /// 预览里一组文件的识别结论：命中某条目，或没命中（带原因与候选）。
    struct ReidentifyOutcomeView: Codable, Hashable, Sendable {
        var mediaItemId: Int?
        var tmdbId: Int?
        var title: String?
        var year: Int?
        var posterUrl: String?
        /// 身份来源：path_tag=目录名标记 / nfo / resolved=名称收敛
        var source: String?
        /// 结论与条目现有身份一致
        var sameAsCurrent: Bool
        /// 没命中时的中文原因
        var reason: String?
        /// 没命中时的失败分类
        var code: String?
        var candidates: [API.UnidentifiedCandidateView]

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case tmdbId = "tmdb_id"
            case title
            case year
            case posterUrl = "poster_url"
            case source
            case sameAsCurrent = "same_as_current"
            case reason
            case code
            case candidates
        }
    }

    /// 「修正识别结果」第一阶段：重跑识别链给出的结论，**尚未落库**。
    /// 用户在此拍板：采纳某组结论 / 自己搜一个条目 / 把这些文件标为非独立
    /// 作品；关掉面板则台账零改动。
    struct ReidentifyPreviewView: Codable, Hashable, Sendable {
        /// 条目当前挂着的身份
        var current: API.ReviewItemView
        /// 本库是电影库（搜索与文案按类型走）
        var movie: Bool
        var groups: [API.ReidentifyGroupView]
        /// missing 文件数（无磁盘实体，不参与）
        var skippedMissing: Int
        /// 身份被目录名 tmdbid 标记或 NFO 钉死——改了还会被扫描改回去
        var pinnedIdentity: Bool
        /// 有文件因 TMDB 不通而无结论，此刻不宜拍板
        var unreachable: Bool
        /// 「自己搜」的预填词（解析出的片名）
        var searchSeed: String

        enum CodingKeys: String, CodingKey {
            case current
            case movie
            case groups
            case skippedMissing = "skipped_missing"
            case pinnedIdentity = "pinned_identity"
            case unreachable
            case searchSeed = "search_seed"
        }
    }

    /// 单条目重新识别的结论。
    struct ReidentifyResultView: Codable, Hashable, Sendable {
        /// 参与重识别的在位文件数
        var total: Int
        var identified: Int
        var unidentified: Int
        /// missing 文件数（无磁盘实体，保持原身份）
        var skippedMissing: Int
        /// TMDB 网络类失败、保留原身份的文件数（修复网络后可重试）
        var keptOnError: Int
        /// 识别结果与原身份是否不同
        var changed: Bool
        /// 全部文件收敛到的新条目；识别失败或分裂为多个条目时为 null
        var newMediaItemId: Int?
        var newTitle: String?
        /// 身份由目录名 tmdbid 标记或 NFO 钉死——结果不满意需先改标记/NFO 或人工认领
        var pinnedIdentity: Bool
        /// 面向用户的结论文案
        var message: String

        enum CodingKeys: String, CodingKey {
            case total
            case identified
            case unidentified
            case skippedMissing = "skipped_missing"
            case keptOnError = "kept_on_error"
            case changed
            case newMediaItemId = "new_media_item_id"
            case newTitle = "new_title"
            case pinnedIdentity = "pinned_identity"
            case message
        }
    }

    /// 筛空时的一条放宽建议（docs/design/library-filtering.md 3.3）。
    struct RelaxSuggestionView: Codable, Hashable, Sendable {
        /// 维度：genres / countries / decades / watch
        var dim: String
        /// 维度展示名：类型 / 地区 / 年代 / 观看
        var dimLabel: String
        /// 要去掉的那个取值
        var value: String
        /// 该取值的展示名
        var label: String
        /// 去掉它之后能找回多少部（恒 > 0）
        var count: Int

        enum CodingKeys: String, CodingKey {
            case dim
            case dimLabel = "dim_label"
            case value
            case label
            case count
        }
    }

    /// 网页保存的远程转码配置。
    /// 没有令牌字段：Worker 的凭证在「设置 → 设备」里配对签发与吊销
    /// （docs/design/device-auth.md §5.4）。
    struct RemoteTranscodeConfigPayload: Codable, Hashable, Sendable {
        /// 是否启用远程硬件转码
        var enabled: Bool?
        /// 取源/回传根地址的覆盖项；null=保持，空字符串=清除并回到自动推断
        var baseUrl: String?
        /// 单个 HLS 产物上传的最大字节数
        var maxArtifactBytes: Int?

        enum CodingKeys: String, CodingKey {
            case enabled
            case baseUrl = "base_url"
            case maxArtifactBytes = "max_artifact_bytes"
        }
    }

    /// 网页展示的远程转码配置。
    struct RemoteTranscodeConfigView: Codable, Hashable, Sendable {
        var enabled: Bool
        /// 静态配置出的根地址；空表示自动使用 Worker 连上来的地址
        var baseUrl: String
        /// 网页配置的覆盖地址；空表示不覆盖
        var baseUrlOverride: String
        var baseUrlSource: String
        var maxArtifactBytes: Int
        /// 开关已开，且填过的覆盖地址（如果填了）合法
        var ready: Bool
        /// 覆盖地址的格式问题；地址留空不算问题，不包含任何令牌内容
        var issues: [String]

        enum CodingKeys: String, CodingKey {
            case enabled
            case baseUrl = "base_url"
            case baseUrlOverride = "base_url_override"
            case baseUrlSource = "base_url_source"
            case maxArtifactBytes = "max_artifact_bytes"
            case ready
            case issues
        }
    }

    /// 豆瓣收敛歧义时的确认候选。
    struct ResolveCandidateView: Codable, Hashable, Sendable {
        var tmdbId: Int
        /// 选定候选后用于订阅的稳定引用
        var titleRef: String
        var title: String
        var originalTitle: String
        var year: Int?
        var posterUrl: String?

        enum CodingKeys: String, CodingKey {
            case tmdbId = "tmdb_id"
            case titleRef = "title_ref"
            case title
            case originalTitle = "original_title"
            case year
            case posterUrl = "poster_url"
        }
    }

    /// 一集最近一次成功投递所使用资源的发布→发现→提交时间链。
    struct ResourceTimingView: Codable, Hashable, Sendable {
        var siteId: String
        var torrentId: String
        var publishTime: String?
        var firstSeenAt: String?
        var submittedAt: String
        var publishToSeenSeconds: Int?
        var seenToSubmitSeconds: Int?
        var publishToSubmitSeconds: Int?
        var dryRun: Bool

        enum CodingKeys: String, CodingKey {
            case siteId = "site_id"
            case torrentId = "torrent_id"
            case publishTime = "publish_time"
            case firstSeenAt = "first_seen_at"
            case submittedAt = "submitted_at"
            case publishToSeenSeconds = "publish_to_seen_seconds"
            case seenToSubmitSeconds = "seen_to_submit_seconds"
            case publishToSubmitSeconds = "publish_to_submit_seconds"
            case dryRun = "dry_run"
        }
    }

    /// 恢复已忽略的文件：清掉忽略标记，重新参与识别。
    struct RestorePayload: Codable, Hashable, Sendable {
        /// 要恢复识别的已忽略文件 id 数组
        var fileIds: [Int]

        enum CodingKeys: String, CodingKey {
            case fileIds = "file_ids"
        }
    }

    /// 按季清理时被有意保留的跨季种子（整季包覆盖到仍在追的季）。
    struct RetainedTorrentView: Codable, Hashable, Sendable {
        /// 下载任务名
        var title: String
        /// 该种子覆盖到的季号；空=无从按季定位的存量数据
        var seasons: [Int]

        enum CodingKeys: String, CodingKey {
            case title
            case seasons
        }
    }

    /// 身份复核清单的一组：同一条目目录下、现身份与建议都一致的文件聚成一条。
    /// 识别器升级后扫描复核发现新旧结论不一致的行进入本清单——身份未被改动，
    /// 由用户拍板：采纳建议（改挂新条目）或维持现状。两种拍板都转为人工身份，
    /// 此后对账永不再打扰。
    struct ReviewGroupView: Codable, Hashable, Sendable {
        /// 分组键：条目目录路径 + 现身份 + 建议身份
        var key: String
        /// 展示名：条目目录名（裸文件为文件名）
        var label: String
        var libraryId: Int
        var libraryName: String
        var fileCount: Int
        var totalSizeBytes: Int
        var fileIds: [Int]
        /// 现挂身份
        var current: API.ReviewItemView
        /// 新识别器给出的建议身份
        var suggestion: API.ReviewItemView

        enum CodingKeys: String, CodingKey {
            case key
            case label
            case libraryId = "library_id"
            case libraryName = "library_name"
            case fileCount = "file_count"
            case totalSizeBytes = "total_size_bytes"
            case fileIds = "file_ids"
            case current
            case suggestion
        }
    }

    /// 身份复核里的一方（现身份 / 建议身份）的条目信息。
    struct ReviewItemView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var tmdbId: Int?
        var title: String
        var year: Int?
        var posterUrl: String?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case tmdbId = "tmdb_id"
            case title
            case year
            case posterUrl = "poster_url"
        }
    }

    /// 复核拍板：采纳识别器建议，或明确维持当前身份。
    /// 两种拍板都把这些文件的身份来源转为 manual——用户已经看过并做了决定，
    /// 后续识别器升级不再对它们提复核建议。
    struct ReviewResolvePayload: Codable, Hashable, Sendable {
        /// 同一复核组内的文件 id 数组
        var fileIds: [Int]
        /// accept_suggestion=采纳建议；keep_current=维持当前身份
        var decision: API.IdentityReviewDecision

        enum CodingKeys: String, CodingKey {
            case fileIds = "file_ids"
            case decision
        }
    }

    /// 回退选择器的数据：候选版本列表 + 保留策略现状。
    struct RollbackOptionsView: Codable, Hashable, Sendable {
        var targets: [API.RollbackTargetView]
        var versionsDirBytes: Int
        var keepVersions: Int

        enum CodingKeys: String, CodingKey {
            case targets
            case versionsDirBytes = "versions_dir_bytes"
            case keepVersions = "keep_versions"
        }
    }

    /// 执行回退的请求体。空 target 走旧语义（切回上一版本/基线）。
    struct RollbackPayload: Codable, Hashable, Sendable {
        var target: String?
        var restoreBackup: Bool?

        enum CodingKeys: String, CodingKey {
            case target
            case restoreBackup = "restore_backup"
        }
    }

    /// 一个可回退（或切换）的目标版本。
    struct RollbackTargetView: Codable, Hashable, Sendable {
        var kind: String
        var version: String?
        var changelog: String?
        var installedAt: String?
        var sizeBytes: Int?
        var schemaAction: String
        var backupTakenAt: String?

        enum CodingKeys: String, CodingKey {
            case kind
            case version
            case changelog
            case installedAt = "installed_at"
            case sizeBytes = "size_bytes"
            case schemaAction = "schema_action"
            case backupTakenAt = "backup_taken_at"
        }
    }

    struct RuleSetPayload: Codable, Hashable, Sendable {
        /// 规则组名称（订阅列表与选择器里的展示名）
        var name: String
        /// 过滤规则 JSON（全部键可缺省=不限）：resolutions 分辨率偏好序（如 ["2160p","1080p"]，顺序即优先级）、media_sources 片源档白名单兼偏好序（值域 remux/blu-ray/web-dl/rip/tv，顺序即优先级；与 resolutions 一样参与候选选优，空=不限片源）、video_codecs 编码白名单（按编码族匹配：写 x265 即接受 H.265/HEVC）、platforms/platforms_block 流媒体平台白/黑名单（规范值如 netflix、disney_plus、iqiyi；词表外的值读取时丢弃不报错）、release_groups_allow/release_groups_block 制作组白/黑名单、hdr_levels/hdr_block HDR 白/黑名单（值域 DV/HDR10+/HDR10/HLG/SDR，SDR=资源未标注 HDR，资源只标注泛指 HDR 时按 HDR10 计；白名单任一命中即过、顺序即偏好，黑名单命中即排除；旧的 hdr/dv 三态字段仍可写入，读取时自动换算并反向回填）、free_only 只要免费种、min_seeders 做种数下限、size_min_mb/size_max_mb 体积区间（整季包按每集均摊）、exclude_hr 排除 H&R、hr_unknown_policy 决定 H&R 状态未知时宽松/严格处理、未填写的条件均不限制。
        /// subtitle_languages_require：要求的字幕语言（BCP 47，任一命中即通过）。
        /// audio_languages_require：要求的音轨语言（BCP 47，任一命中即通过）。
        /// upgrade_source 洗版目标片源档（web-dl/blu-ray/remux，缺省=不洗版）、cutoff_resolution 洗版目标分辨率（缺省=resolutions 首选，必须在 resolutions 允许范围内；同理 upgrade_source 必须在 media_sources 允许范围内）、upgrade_ladder 参与洗版比较的维度及优先级（顺序即位次，值域 resolution/source/video_codec/platform，缺省 [resolution, source] 即只比分辨率与片源；偏好列表为空的维度自动跳过）。
        /// 示例：{"resolutions":["2160p"],"free_only":true,"upgrade_source":"remux"}
        var spec: [String: API.JSONValue]?
        /// 适用范围（新订阅未指定规则组时据此自动选组）：条件列表，条件间为「且」，每条形如 {"field": ..., "op": "any_of", "values": [...]}。field 取值：kind（movie/tv）、genres（TMDB 类型 ID）、origin_countries（国家码如 JP、KR）。多个规则组同时命中时条件多者优先，都不命中用默认规则组。创建时缺省=不声明；更新时缺省=不改，传 [] 清空。示例：[{"field":"kind","op":"any_of","values":["tv"]},{"field":"origin_countries","op":"any_of","values":["JP","KR"]}]
        var matchRules: [[String: API.JSONValue]]?

        enum CodingKeys: String, CodingKey {
            case name
            case spec
            case matchRules = "match_rules"
        }
    }

    struct RuleSetView: Codable, Hashable, Sendable {
        var id: Int
        var name: String
        var isDefault: Bool
        var spec: [String: API.JSONValue]
        /// 适用范围条件；空=未声明（只能手选或作默认兜底）
        var matchRules: [[String: API.JSONValue]]
        /// 正在引用本规则组的订阅数；>0 时不可删除
        var referenceCount: Int

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case isDefault = "is_default"
            case spec
            case matchRules = "match_rules"
            case referenceCount = "reference_count"
        }
    }

    /// 进行中扫描/整理的实时进度（前端在库封面上画进度环，两种任务共用）。
    /// ``phase`` 是必填的：一次"扫描"内部分好几段（盘点 → 逐文件入账 →
    /// 补齐图片资产），分子分母各段各算。前端**必须**按阶段选文案，否则
    /// 进度走完文件数后还要跑几分钟资产，界面就会僵在"已处理 = 总数"上
    /// 对用户撒谎（见 library_scan.ScanPhase）。
    struct ScanProgressView: Codable, Hashable, Sendable {
        /// 进行中的阶段，取值见 library_scan.ScanPhase / organizing
        var phase: String
        var processed: Int
        /// 总数；0 表示分母未知，前端画不确定态转圈
        var total: Int

        enum CodingKeys: String, CodingKey {
            case phase
            case processed
            case total
        }
    }

    /// 扫描启动响应。
    struct ScanResultView: Codable, Hashable, Sendable {
        var started: Bool
        var message: String
        /// 持久化后台作业 id，可用于等待、取消和查看时间线
        var jobId: String
        /// 本次是否新建作业；false 表示复用同库进行中的扫描
        var created: Bool

        enum CodingKeys: String, CodingKey {
            case started
            case message
            case jobId = "job_id"
            case created
        }
    }

    struct ScheduledTaskUpdate: Codable, Hashable, Sendable {
        var enabled: Bool
        var triggerType: API.TriggerType
        var intervalSeconds: Int?
        var cronExpr: String?

        enum CodingKeys: String, CodingKey {
            case enabled
            case triggerType = "trigger_type"
            case intervalSeconds = "interval_seconds"
            case cronExpr = "cron_expr"
        }
    }

    struct ScheduledTaskView: Codable, Hashable, Sendable {
        var key: String
        var title: String
        var description: String
        var enabled: Bool
        var triggerType: API.TriggerType
        var intervalSeconds: Int?
        var cronExpr: String?
        var lastRunAt: String?
        var nextRunAt: String?

        enum CodingKeys: String, CodingKey {
            case key
            case title
            case description
            case enabled
            case triggerType = "trigger_type"
            case intervalSeconds = "interval_seconds"
            case cronExpr = "cron_expr"
            case lastRunAt = "last_run_at"
            case nextRunAt = "next_run_at"
        }
    }

    struct ScrapeConfigView: Codable, Hashable, Sendable {
        var setting: API.MetadataScrapeSetting
        var effective: API.ScrapeEffectiveView

        enum CodingKeys: String, CodingKey {
            case setting
            case effective
        }
    }

    /// "跟随环境变量"字段当前的生效值（前端展示"跟随中：xxx"用）。
    struct ScrapeEffectiveView: Codable, Hashable, Sendable {
        var languagePriority: [String]
        var certCountryPriority: [String]
        var posterSize: String
        var backdropSize: String
        var stillSize: String

        enum CodingKeys: String, CodingKey {
            case languagePriority = "language_priority"
            case certCountryPriority = "cert_country_priority"
            case posterSize = "poster_size"
            case backdropSize = "backdrop_size"
            case stillSize = "still_size"
        }
    }

    /// 全站背景蒙版（.page-scrim）的样式偏好。
    /// 蒙版是铺在背景大图之上、页面内容之下的一层深色模糊层，压住背景、
    /// 突出内容（见 apps/web/app/globals.css 的 .page-scrim）。全站只有这
    /// 一档蒙版（除「新任务」首页大图直出外，所有页面统一），两个可调项
    /// 分别驱动前端 CSS 变量 ``--scrim-blur`` / ``--scrim-dark``：
    /// - ``blur``：高斯模糊半径（px）。0 = 不模糊、背景大图清晰透出；越大背景
    /// 越朦胧。
    /// - ``dark``：压暗程度（蒙版底色的不透明度）。0 = 完全不压暗，1 = 全黑。
    /// 默认值是实际调校后确定的出厂观感：中等模糊 + 近七成压暗，背景大图化为
    /// 朦胧色块托住内容、又不至于抢走注意力；必须与前端 DEFAULT_UI_PREFS
    /// 以及 globals.css 里 .page-scrim 的变量兜底值保持一致。
    struct ScrimUiPrefs: Codable, Hashable, Sendable {
        /// 蒙版高斯模糊半径（px）：0 不模糊，越大背景越朦胧
        var blur: Double
        /// 蒙版压暗程度：0 完全不压暗，1 全黑
        var dark: Double

        enum CodingKeys: String, CodingKey {
            case blur
            case dark
        }
    }

    /// 全站背景蒙版（.page-scrim）的样式偏好。
    /// 蒙版是铺在背景大图之上、页面内容之下的一层深色模糊层，压住背景、
    /// 突出内容（见 apps/web/app/globals.css 的 .page-scrim）。全站只有这
    /// 一档蒙版（除「新任务」首页大图直出外，所有页面统一），两个可调项
    /// 分别驱动前端 CSS 变量 ``--scrim-blur`` / ``--scrim-dark``：
    /// - ``blur``：高斯模糊半径（px）。0 = 不模糊、背景大图清晰透出；越大背景
    /// 越朦胧。
    /// - ``dark``：压暗程度（蒙版底色的不透明度）。0 = 完全不压暗，1 = 全黑。
    /// 默认值是实际调校后确定的出厂观感：中等模糊 + 近七成压暗，背景大图化为
    /// 朦胧色块托住内容、又不至于抢走注意力；必须与前端 DEFAULT_UI_PREFS
    /// 以及 globals.css 里 .page-scrim 的变量兜底值保持一致。
    struct ScrimUiPrefsInput: Codable, Hashable, Sendable {
        /// 蒙版高斯模糊半径（px）：0 不模糊，越大背景越朦胧
        var blur: Double?
        /// 蒙版压暗程度：0 完全不压暗，1 全黑
        var dark: Double?

        enum CodingKeys: String, CodingKey {
            case blur
            case dark
        }
    }

    /// 搜索历史的单条记录，供前端渲染「最近搜索」快捷入口。
    /// ``label`` / ``categories`` / ``site_ids`` 是搜索发生时的快照：点历史重搜时
    /// 按快照原样再搜，预设后来改名/删除都不影响。
    struct SearchHistoryItem: Codable, Hashable, Sendable {
        var id: Int
        var keyword: String
        var vertical: String
        var label: String?
        var categories: [String]
        var siteIds: [String]
        var posterMode: Bool
        var searchCount: Int
        var lastSearchedAt: String
        var hasSnapshot: Bool

        enum CodingKeys: String, CodingKey {
            case id
            case keyword
            case vertical
            case label
            case categories
            case siteIds = "site_ids"
            case posterMode = "poster_mode"
            case searchCount = "search_count"
            case lastSearchedAt = "last_searched_at"
            case hasSnapshot = "has_snapshot"
        }
    }

    /// 立即搜索缺失资源的结果。
    struct SearchNowView: Codable, Hashable, Sendable {
        /// 跳过冷却、重新排队的缺口工单数
        var resetCount: Int

        enum CodingKeys: String, CodingKey {
            case resetCount = "reset_count"
        }
    }

    /// 资源搜索预设：全量内置分类（含隐藏项）+ 全部自定义组合。
    struct SearchPresetListView: Codable, Hashable, Sendable {
        var presets: [API.JSONValue]

        enum CodingKeys: String, CodingKey {
            case presets
        }
    }

    /// 整体保存资源搜索预设；缺失的内置分类由后端按默认补齐。
    struct SearchPresetUpdate: Codable, Hashable, Sendable {
        var presets: [API.JSONValue]

        enum CodingKeys: String, CodingKey {
            case presets
        }
    }

    /// 跨站点聚合搜索的返回结构。
    /// ``items`` 是所有站点结果的合并列表（每条自带 ``site_id`` / ``site_name``），
    /// ``sites`` 给出逐站执行状态。前端既能直接铺一个大列表，也能按站点分组、或单独
    /// 提示失败站点。``label`` / ``categories`` 是请求参数的回显，供结果页直接标注
    /// 本次搜索的范围。
    struct SearchResponse: Codable, Hashable, Sendable {
        var keyword: String
        var label: String?
        var categories: [String]
        var total: Int
        var items: [API.TorrentHit]
        var sites: [API.SiteSearchStatus]

        enum CodingKeys: String, CodingKey {
            case keyword
            case label
            case categories
            case total
            case items
            case sites
        }
    }

    /// 清理已移出订阅范围的那几季的内容（减季后的可选收尾）。
    struct SeasonCleanupPayload: Codable, Hashable, Sendable {
        /// 要清理的季号；必须都已移出订阅范围
        var seasons: [Int]
        /// 从下载器删除这几季的种子任务及其数据文件（不可恢复）
        var deleteTorrents: Bool?
        /// 把这几季在媒体库里的文件移入回收站（保留期内可恢复）
        var deleteLibraryFiles: Bool?

        enum CodingKeys: String, CodingKey {
            case seasons
            case deleteTorrents = "delete_torrents"
            case deleteLibraryFiles = "delete_library_files"
        }
    }

    /// 一季的分集清单（分集横滚区数据源）。
    struct SeasonEpisodesView: Codable, Hashable, Sendable {
        var seasonNumber: Int
        var episodes: [API.EpisodeView]

        enum CodingKeys: String, CodingKey {
            case seasonNumber = "season_number"
            case episodes
        }
    }

    /// 弹层季选择器的一行：季号 + 播出进度 + 库存进度。
    struct SeasonOverview: Codable, Hashable, Sendable {
        var seasonNumber: Int
        var name: String
        var airDate: String?
        var episodeCount: Int?
        /// 已播集数（air_date<=今天）
        var airedCount: Int
        /// 媒体库已有的集数（库存 H）
        var ownedCount: Int

        enum CodingKeys: String, CodingKey {
            case seasonNumber = "season_number"
            case name
            case airDate = "air_date"
            case episodeCount = "episode_count"
            case airedCount = "aired_count"
            case ownedCount = "owned_count"
        }
    }

    /// 自检结果：这个端点现在能不能用，断在哪一环。
    struct SelfCheckView: Codable, Hashable, Sendable {
        var ok: Bool
        var message: String
        var protocolVersion: String
        var toolCount: Int
        var elapsedMs: Int
        /// 被试调的只读工具；空=没有可安全试调的工具
        var probeTool: String
        var probeOk: Bool
        var probeMessage: String
        var warnings: [String]

        enum CodingKeys: String, CodingKey {
            case ok
            case message
            case protocolVersion = "protocol_version"
            case toolCount = "tool_count"
            case elapsedMs = "elapsed_ms"
            case probeTool = "probe_tool"
            case probeOk = "probe_ok"
            case probeMessage = "probe_message"
            case warnings
        }
    }

    /// 系列里的一部作品：库里有没有、在追没在追。
    struct SeriesPartView: Codable, Hashable, Sendable {
        var tmdbId: Int
        var title: String
        var releaseDate: String?
        var posterUrl: String?
        /// 库里已有的那条；null=缺这一部
        var mediaItemId: Int?
        /// 已经在追（有订阅工单）
        var subscribed: Bool

        enum CodingKeys: String, CodingKey {
            case tmdbId = "tmdb_id"
            case title
            case releaseDate = "release_date"
            case posterUrl = "poster_url"
            case mediaItemId = "media_item_id"
            case subscribed
        }
    }

    /// 一个可勾选的服务：给管理页算「选了它会多几个工具」用。
    struct ServiceView: Codable, Hashable, Sendable {
        /// 服务域名，如 subscriptions
        var domain: String
        /// 这个服务能做什么（一行）
        var description: String
        /// 该服务的命令数 = 展开模式下的工具数
        var commandCount: Int
        /// 展开模式下这些工具定义的大致体积（字节）
        var expandedBytes: Int
        /// 折叠模式下这一个工具的描述体积（字节）
        var collapsedBytes: Int

        enum CodingKeys: String, CodingKey {
            case domain
            case description
            case commandCount = "command_count"
            case expandedBytes = "expanded_bytes"
            case collapsedBytes = "collapsed_bytes"
        }
    }

    /// 当前主体的能力开关快照（前端据此裁剪入口；安全边界仍在后端 403）。
    struct SessionCapabilities: Codable, Hashable, Sendable {
        var allowSubscribe: Bool
        var allowSearch: Bool
        var allowDirectDownload: Bool

        enum CodingKeys: String, CodingKey {
            case allowSubscribe = "allow_subscribe"
            case allowSearch = "allow_search"
            case allowDirectDownload = "allow_direct_download"
        }
    }

    /// 完整轨迹中的上下文压缩记录，不属于 message。
    struct SessionCompactionEntryView: Codable, Hashable, Sendable {
        /// 轨迹类型判别值
        var type: String
        /// 这条上下文压缩记录的稳定编号
        var compactionId: String
        /// 上一条轨迹 entry 的编号
        var parentId: String?
        /// 压缩记录写入时间（ISO 8601 UTC）
        var timestamp: String
        /// 模型生成的历史交接摘要
        var summary: String
        /// 后续模型请求实际采用的压缩后历史，不包含 system 消息
        var replacementHistory: [API.SessionMessageView]
        /// 压缩前的估算 token 数
        var tokensBefore: Int?
        /// 压缩后的估算 token 数
        var tokensAfter: Int?

        enum CodingKeys: String, CodingKey {
            case type
            case compactionId = "compaction_id"
            case parentId = "parent_id"
            case timestamp
            case summary
            case replacementHistory = "replacement_history"
            case tokensBefore = "tokens_before"
            case tokensAfter = "tokens_after"
        }
    }

    /// 手动压缩的回执：摘要与前后 token 估算（bytes/4 启发式，非精确值）。
    struct SessionContextCompactionView: Codable, Hashable, Sendable {
        /// 模型生成的历史交接摘要
        var summary: String
        /// 压缩前的估算 token 数
        var tokensBefore: Int
        /// 压缩后的估算 token 数
        var tokensAfter: Int
        /// 写入完整轨迹的 compaction 记录编号
        var compactionId: String

        enum CodingKeys: String, CodingKey {
            case summary
            case tokensBefore = "tokens_before"
            case tokensAfter = "tokens_after"
            case compactionId = "compaction_id"
        }
    }

    /// 新会话的来源快照；replacement_history 是后续请求实际继承的上下文。
    struct SessionHandoffEntryView: Codable, Hashable, Sendable {
        /// 轨迹类型判别值
        var type: String
        /// 交接记录的稳定编号
        var handoffId: String
        /// 上一条轨迹 entry 的编号
        var parentId: String?
        /// 创建快照的时间（ISO 8601 UTC）
        var timestamp: String
        /// 源会话稳定编号
        var sourceSessionId: String
        /// 源会话快照时的链尾编号
        var sourceLeafId: String?
        /// 快照时的源会话标题
        var sourceTitle: String?
        /// 新会话后续模型请求继承的完整上下文（不含 system 消息）。仅在 session.fork 的创建响应中携带；session.get-transcript 固定返回空列表——快照已持久化在服务端，每次读取会话都重复下发整份历史太浪费
        var replacementHistory: [API.SessionMessageView]

        enum CodingKeys: String, CodingKey {
            case type
            case handoffId = "handoff_id"
            case parentId = "parent_id"
            case timestamp
            case sourceSessionId = "source_session_id"
            case sourceLeafId = "source_leaf_id"
            case sourceTitle = "source_title"
            case replacementHistory = "replacement_history"
        }
    }

    /// 用户消息已持久化并开始处理的回执。
    struct SessionMessageAcceptedView: Codable, Hashable, Sendable {
        /// 会话稳定编号，后续 session 操作都使用它
        var sessionId: String
        /// 本次新建的 user message 稳定编号，可作为 retry 的定位锚点
        var messageId: String

        enum CodingKeys: String, CodingKey {
            case sessionId = "session_id"
            case messageId = "message_id"
        }
    }

    /// 完整轨迹中的一条消息记录；message_id 是可寻址的持久化身份。
    struct SessionMessageEntryView: Codable, Hashable, Sendable {
        /// 轨迹类型判别值
        var type: String
        /// 这条持久化消息的稳定编号
        var messageId: String
        /// 上一条轨迹 entry 的编号
        var parentId: String?
        /// 消息写入时间（ISO 8601 UTC）
        var timestamp: String
        /// LLM 协议格式的完整消息
        var message: API.SessionMessageView
        /// assistant 行：实际使用的模型 id；user 行：本轮请求的模型引用（null = 默认模型）
        var model: String?
        /// assistant 消息的 token 用量
        var usage: API.TokenUsage?
        /// assistant 消息的模型结束原因
        var finishReason: String?
        /// user 消息生效的思维链档位；null = 模型默认
        var thinkingLevel: String?

        enum CodingKeys: String, CodingKey {
            case type
            case messageId = "message_id"
            case parentId = "parent_id"
            case timestamp
            case message
            case model
            case usage
            case finishReason = "finish_reason"
            case thinkingLevel = "thinking_level"
        }
    }

    /// 会话中的一条协议消息。
    /// user 是用户输入；assistant 是模型输出；tool 是工具执行回执；system 通常
    /// 只在运行时组装而不写入 transcript，但保留在完整消息定义中。
    struct SessionMessageView: Codable, Hashable, Sendable {
        /// 消息角色：用户输入、模型输出、工具回执或系统消息
        var role: String
        /// 消息正文；可能是纯文本或 text/thinking/image 内容块
        var content: API.JSONValue?
        /// assistant 发起的工具调用；其他角色通常为空
        var toolCalls: [API.ToolCall]?
        /// tool 消息所回应的工具调用编号
        var toolCallId: String?
        /// tool 消息对应的工具名称
        var name: String?

        enum CodingKeys: String, CodingKey {
            case role
            case content
            case toolCalls = "tool_calls"
            case toolCallId = "tool_call_id"
            case name
        }
    }

    /// 重命名会话的请求体。
    /// 标题只存索引表（元数据不入转录文件，见 agent_sessions 模块的
    /// append-only 约定）；索引整体重建时非空标题会被保留。
    struct SessionRenamePayload: Codable, Hashable, Sendable {
        /// 新的会话标题
        var title: String

        enum CodingKeys: String, CodingKey {
            case title
        }
    }

    /// 从指定用户消息处重试，可用新内容替换原问题。
    struct SessionRetryPayload: Codable, Hashable, Sendable {
        /// 要重新提问的 user message 编号
        var messageId: String
        /// 替换后的用户消息正文；留空时原文重试
        var content: String?
        /// 重试消息携带的图片附件编号：不传（null）沿用原消息的附件，空数组显式去掉图片，非空数组替换为新附件
        var attachments: [String]?
        /// 模型引用（同 session.start）；传 default 清回默认模型，留空沿用原消息的模型
        var model: String?
        /// 思维链强度档位；传 default 清回模型默认，不传沿用原消息的档位
        var thinkingLevel: String?

        enum CodingKeys: String, CodingKey {
            case messageId = "message_id"
            case content
            case attachments
            case model
            case thinkingLevel = "thinking_level"
        }
    }

    /// 提交一条用户消息；有 session_id 时继续已有会话，否则开始新会话。
    /// 图片以 attachment_id 引用（先经 ``session.attachments.upload`` 上传）；
    /// 协议永不接受调用方直接提交内容块——ContentPart 由服务端组装，杜绝注入
    /// 任意 url 或内联 base64（docs/design/agent-image-input.md §8.1）。
    struct SessionStartPayload: Codable, Hashable, Sendable {
        /// 用户消息正文；带图片时允许为空
        var content: String?
        /// 随消息发送的图片附件编号列表（上传接口返回的 attachment_id）
        var attachments: [String]?
        /// 已有会话编号；留空时创建新会话
        var sessionId: String?
        /// 模型引用：裸模型 id，或同 id 在多个实例时的「实例名/模型id」（见 llm.models）；传 default 显式用默认实例的默认模型；留空时沿用会话最近一条消息的模型（新会话即默认）
        var model: String?
        /// 思维链强度档位（off/minimal/low/medium/high/xhigh/max），传 default 显式清回模型默认；不传时沿用会话最近一条消息的档位
        var thinkingLevel: String?

        enum CodingKeys: String, CodingKey {
            case content
            case attachments
            case sessionId = "session_id"
            case model
            case thinkingLevel = "thinking_level"
        }
    }

    /// 会话列表项（索引表投影 + 派生的运行状态）。
    struct SessionSummary: Codable, Hashable, Sendable {
        /// 会话稳定编号（session_id）
        var id: String
        /// 会话标题；未命名时为空
        var title: String?
        /// 最近一条用户消息的短预览
        var lastPrompt: String?
        /// 完整轨迹的 entry 数量，包含 message 与 compaction
        var entryCount: Int
        /// 当前是否有仍在处理的用户消息
        var running: Bool
        /// 会话创建时间（ISO 8601 UTC）
        var createdAt: String
        /// 会话最近活动时间（ISO 8601 UTC）
        var updatedAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case title
            case lastPrompt = "last_prompt"
            case entryCount = "entry_count"
            case running
            case createdAt = "created_at"
            case updatedAt = "updated_at"
        }
    }

    /// 会话详情：列表项字段 + 完整轨迹回放。
    /// message / compaction / handoff 是三种明确的 entry；各自使用自己的稳定
    /// 编号，parent_id 只描述同一会话内跨 entry 的线性轨迹链。
    struct SessionTranscriptView: Codable, Hashable, Sendable {
        /// 会话摘要与当前运行状态
        var session: API.SessionSummary
        /// 按写入顺序排列的完整 message/compaction 轨迹
        var entries: [API.JSONValue]

        enum CodingKeys: String, CodingKey {
            case session
            case entries
        }
    }

    /// 当前登录状态（GET /auth/me 与登录成功后的返回体）。
    struct SessionView: Codable, Hashable, Sendable {
        var username: String
        var nickname: String
        /// 头像相对 URL（含版本号）；未上传过头像时为空
        var avatarUrl: String?
        /// admin=超级管理员；member=成员
        var role: String
        /// 能力开关快照；管理员恒为全开
        var capabilities: API.SessionCapabilities

        enum CodingKeys: String, CodingKey {
            case username
            case nickname
            case avatarUrl = "avatar_url"
            case role
            case capabilities
        }
    }

    /// 创建分享。有效期只有四档，没有「永久」。
    struct ShareCreateRequest: Codable, Hashable, Sendable {
        /// 有效期（天）：1 / 3 / 7 / 30
        var expiresInDays: Int?
        /// 访问密码；空 = 不设密码
        var password: String?

        enum CodingKeys: String, CodingKey {
            case expiresInDays = "expires_in_days"
            case password
        }
    }

    /// 访客打开链接时的探针：只说「要不要密码」，不露片名海报。
    struct SharePublicView: Codable, Hashable, Sendable {
        var requiresPassword: Bool
        /// 无密码恒为 true；有密码时表示本浏览器已解锁
        var unlocked: Bool
        var expiresAt: String
        /// 被分享的条目 id；解锁之前、或分享的是合集时为 null
        var mediaItemId: Int?
        /// 被分享的合集 id；解锁之前、或分享的是条目时为 null
        var collectionId: Int?

        enum CodingKeys: String, CodingKey {
            case requiresPassword = "requires_password"
            case unlocked
            case expiresAt = "expires_at"
            case mediaItemId = "media_item_id"
            case collectionId = "collection_id"
        }
    }

    struct ShareUnlockRequest: Codable, Hashable, Sendable {
        var password: String

        enum CodingKeys: String, CodingKey {
            case password
        }
    }

    /// 一条分享（创建者视角）。
    struct ShareView: Codable, Hashable, Sendable {
        var id: Int
        var slug: String
        /// 分享链接；未配置外部访问地址时为相对路径 /s/{slug}
        var url: String
        var mediaItemId: Int?
        var collectionId: Int?
        var libraryId: Int?
        var title: String
        /// 被分享条目的形态；合集分享为 null
        var kind: API.MediaKind?
        var year: Int?
        var posterUrl: String?
        /// 合集分享此刻有几部；条目分享为 null
        var itemCount: Int?
        /// 访问密码原文；无密码为 null
        var password: String?
        var expiresAt: String
        var createdAt: String
        var viewCount: Int
        var lastAccessedAt: String?

        enum CodingKeys: String, CodingKey {
            case id
            case slug
            case url
            case mediaItemId = "media_item_id"
            case collectionId = "collection_id"
            case libraryId = "library_id"
            case title
            case kind
            case year
            case posterUrl = "poster_url"
            case itemCount = "item_count"
            case password
            case expiresAt = "expires_at"
            case createdAt = "created_at"
            case viewCount = "view_count"
            case lastAccessedAt = "last_accessed_at"
        }
    }

    /// 合集分享页上的一格：只有认得出这部片所需的最少信息。
    struct SharedCollectionItemView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var title: String
        var year: Int?
        var kind: API.MediaKind
        var posterUrl: String?

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case title
            case year
            case kind
            case posterUrl = "poster_url"
        }
    }

    /// 分享页的合集信息：名字 + 此刻的成员卡片。
    /// 成员是**每次访问现算**的（走 resolve_members）：规则驱动的合集会自己长，
    /// 分享出去之后新入库的片也会出现在里面——这正是分享一个合集而不是一串
    /// 条目的意义。反过来，被移出去的片立刻打不开，不需要任何撤销动作。
    struct SharedCollectionView: Codable, Hashable, Sendable {
        var name: String
        var itemCount: Int
        var items: [API.SharedCollectionItemView]

        enum CodingKeys: String, CodingKey {
            case name
            case itemCount = "item_count"
            case items
        }
    }

    /// 访客能看到的一个文件：只有规格与章节，没有路径、文件名、生命周期细节。
    struct SharedFileView: Codable, Hashable, Sendable {
        var id: Int
        var sizeBytes: Int
        var container: String?
        var resolution: String?
        var videoCodec: String?
        var hdr: String?
        var bitDepth: Int?
        var durationSeconds: Int?
        var mediaSource: String?
        var seasonNumber: Int
        var episodeNumber: Int
        var missing: Bool
        var state: String
        var audioStreams: [API.AudioStreamView]?
        var subtitleStreams: [API.SubtitleStreamView]
        var chapters: [API.ChapterView]?

        enum CodingKeys: String, CodingKey {
            case id
            case sizeBytes = "size_bytes"
            case container
            case resolution
            case videoCodec = "video_codec"
            case hdr
            case bitDepth = "bit_depth"
            case durationSeconds = "duration_seconds"
            case mediaSource = "media_source"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case missing
            case state
            case audioStreams = "audio_streams"
            case subtitleStreams = "subtitle_streams"
            case chapters
        }
    }

    /// 分享页的影片信息：详情视图的浏览面投影。
    struct SharedItemView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var kind: API.MediaKind
        var tmdbId: Int?
        var imdbId: String?
        var doubanId: String?
        var title: String
        var originalTitle: String
        var year: Int?
        var posterUrl: String?
        var backdropUrl: String?
        var primaryAspect: Double
        var localMeta: API.LocalMetaView?
        var files: [API.SharedFileView]
        var seasons: [Int]
        var expiresAt: String

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case kind
            case tmdbId = "tmdb_id"
            case imdbId = "imdb_id"
            case doubanId = "douban_id"
            case title
            case originalTitle = "original_title"
            case year
            case posterUrl = "poster_url"
            case backdropUrl = "backdrop_url"
            case primaryAspect = "primary_aspect"
            case localMeta = "local_meta"
            case files
            case seasons
            case expiresAt = "expires_at"
        }
    }

    /// 侧边栏（液态玻璃面板）的样式偏好。
    /// 两个值直接对应前端 WebGL 着色器的参数（见 apps/web/lib/glass.ts）：
    /// 侧栏玻璃的基底是 LiquidGlassCard 同款材质（见 apps/web/lib/glass.ts），
    /// 三个值是在其上微调的滑杆，默认值即 Card 出厂观感：
    /// - ``transparency``：玻璃透明程度。0 = Card 标准玻璃，1 = 玻璃完全
    /// 隐去；对应 shader 的 u_opacity（材质整体淡出）。
    /// - ``brightness``：玻璃明暗。-1 最暗 ~ 1 最亮，0 = 不加暗不提亮；
    /// 对应 shader 的 tint 参数。
    /// - ``depth``：玻璃厚度（边缘曲率带宽度，px）。越大越像厚玻璃、边缘折射带
    /// 越宽；对应 shader 的 u_zRadius，过小会使高度场退化，故下限取 10。
    /// 默认值是实际调校后确定的出厂观感（半透 + 略压暗 + 偏薄的边缘折射带），
    /// 而非 Card 材质的原始参数；必须与前端 DEFAULT_UI_PREFS 保持一致。
    struct SidebarUiPrefs: Codable, Hashable, Sendable {
        /// 玻璃透明程度：0 标准玻璃，1 完全隐去
        var transparency: Double
        /// 玻璃明暗：-1 最暗，1 最亮
        var brightness: Double
        /// 玻璃厚度（边缘曲率带宽度，px）
        var depth: Double

        enum CodingKeys: String, CodingKey {
            case transparency
            case brightness
            case depth
        }
    }

    /// 侧边栏（液态玻璃面板）的样式偏好。
    /// 两个值直接对应前端 WebGL 着色器的参数（见 apps/web/lib/glass.ts）：
    /// 侧栏玻璃的基底是 LiquidGlassCard 同款材质（见 apps/web/lib/glass.ts），
    /// 三个值是在其上微调的滑杆，默认值即 Card 出厂观感：
    /// - ``transparency``：玻璃透明程度。0 = Card 标准玻璃，1 = 玻璃完全
    /// 隐去；对应 shader 的 u_opacity（材质整体淡出）。
    /// - ``brightness``：玻璃明暗。-1 最暗 ~ 1 最亮，0 = 不加暗不提亮；
    /// 对应 shader 的 tint 参数。
    /// - ``depth``：玻璃厚度（边缘曲率带宽度，px）。越大越像厚玻璃、边缘折射带
    /// 越宽；对应 shader 的 u_zRadius，过小会使高度场退化，故下限取 10。
    /// 默认值是实际调校后确定的出厂观感（半透 + 略压暗 + 偏薄的边缘折射带），
    /// 而非 Card 材质的原始参数；必须与前端 DEFAULT_UI_PREFS 保持一致。
    struct SidebarUiPrefsInput: Codable, Hashable, Sendable {
        /// 玻璃透明程度：0 标准玻璃，1 完全隐去
        var transparency: Double?
        /// 玻璃明暗：-1 最暗，1 最亮
        var brightness: Double?
        /// 玻璃厚度（边缘曲率带宽度，px）
        var depth: Double?

        enum CodingKeys: String, CodingKey {
            case transparency
            case brightness
            case depth
        }
    }

    /// 刷流暂停/恢复请求体。
    struct SiteBoostPauseUpdate: Codable, Hashable, Sendable {
        /// true=暂停（做种压到极低上传限速、停止汰换与拉新种，任务与数据保留）/ false=恢复（解除限速，回到正常刷流节奏）
        var paused: Bool

        enum CodingKeys: String, CodingKey {
            case paused
        }
    }

    /// 单个站点的刷流运行统计（数据来源见 RatioBoostTask 台账）。
    struct SiteBoostStatsView: Codable, Hashable, Sendable {
        /// 在池刷流任务数
        var activeCount: Int
        /// 在池任务占用的预算（字节）
        var usedBytes: Int
        /// 当前预算（字节）
        var budgetBytes: Int
        /// 刷流累计上传量（含已汰换任务的历史贡献，字节）
        var uploadedBytesTotal: Int
        /// 累计汰换任务数
        var evictedCount: Int
        /// 近 24 小时上传量（字节）
        var uploadedBytes24h: Int
        /// 近 24 小时平均在池体积（字节）
        var avgUsedBytes24h: Int
        /// 近 7 天上传量（字节）
        var uploadedBytes7d: Int
        /// 近 7 天平均在池体积（字节）
        var avgUsedBytes7d: Int

        enum CodingKeys: String, CodingKey {
            case activeCount = "active_count"
            case usedBytes = "used_bytes"
            case budgetBytes = "budget_bytes"
            case uploadedBytesTotal = "uploaded_bytes_total"
            case evictedCount = "evicted_count"
            case uploadedBytes24h = "uploaded_bytes_24h"
            case avgUsedBytes24h = "avg_used_bytes_24h"
            case uploadedBytes7d = "uploaded_bytes_7d"
            case avgUsedBytes7d = "avg_used_bytes_7d"
        }
    }

    /// 配置/更新站点的请求体。
    /// 按所选 auth_type 填写对应字段；未用到的字段留空即可（服务端会校验必填项）：
    /// - cookie      → cookie 字符串
    /// - apikey      → api_key
    /// - credential  → username + password
    struct SiteConfigCreate: Codable, Hashable, Sendable {
        /// 要配置的站点标识，须来自站点目录（CLI：mclaw site catalog）
        var siteId: String
        /// 选用的授权类型，须在该站点 supported 列表内
        var authType: API.AuthType
        /// COOKIE 模式：浏览器 cookie 字符串
        var cookie: String?
        /// APIKEY 模式：API 密钥
        var apiKey: String?
        /// CREDENTIAL 模式：用户名
        var username: String?
        /// CREDENTIAL 模式：密码
        var password: String?
        /// 是否启用（默认启用）
        var enabled: Bool?

        enum CodingKeys: String, CodingKey {
            case siteId = "site_id"
            case authType = "auth_type"
            case cookie
            case apiKey = "api_key"
            case username
            case password
            case enabled
        }
    }

    /// 更新站点授权信息的请求体（site_id 走路径参数，故此处不含）。
    struct SiteConfigUpdate: Codable, Hashable, Sendable {
        /// 选用的授权类型，须在该站点 supported 列表内
        var authType: API.AuthType
        /// COOKIE 模式：浏览器 cookie 字符串
        var cookie: String?
        /// APIKEY 模式：API 密钥
        var apiKey: String?
        /// CREDENTIAL 模式：用户名
        var username: String?
        /// CREDENTIAL 模式：密码
        var password: String?
        /// true=启用 / false=停用
        var enabled: Bool?

        enum CodingKeys: String, CodingKey {
            case authType = "auth_type"
            case cookie
            case apiKey = "api_key"
            case username
            case password
            case enabled
        }
    }

    /// 站点保护开关请求体（语义见 docs/design/site-protection-ratio-boost.md）。
    struct SiteProtectionUpdate: Codable, Hashable, Sendable {
        /// true=保护（订阅链路绕开该站，手动搜索/下载不受影响）/ false=取消保护
        var protected: Bool

        enum CodingKeys: String, CodingKey {
            case protected
        }
    }

    /// 自动刷分享率设置请求体。
    struct SiteRatioBoostUpdate: Codable, Hashable, Sendable {
        /// true=开启自动刷分享率 / false=关闭
        var enabled: Bool
        /// 刷流存储预算（字节，≥1 GiB）；None=不修改现有预算
        var budgetBytes: Int?
        /// 汰换最低保留天数（0～30；0=站点无 H&R、不设保护）；None=不修改
        var holdDays: Int?

        enum CodingKeys: String, CodingKey {
            case enabled
            case budgetBytes = "budget_bytes"
            case holdDays = "hold_days"
        }
    }

    /// 单个站点在本次搜索中的执行情况。
    struct SiteSearchStatus: Codable, Hashable, Sendable {
        var siteId: String
        var siteName: String
        var count: Int
        var error: String?
        var elapsedMs: Int?

        enum CodingKeys: String, CodingKey {
            case siteId = "site_id"
            case siteName = "site_name"
            case count
            case error
            case elapsedMs = "elapsed_ms"
        }
    }

    /// 启用/停用请求体。
    struct SiteStatusUpdate: Codable, Hashable, Sendable {
        /// true=启用 / false=停用（停用后不再参与搜索与同步）
        var enabled: Bool

        enum CodingKeys: String, CodingKey {
            case enabled
        }
    }

    /// 站点种子缓存与同步节奏的对外视图（数据来源见 SiteTorrent / SiteSyncCursor）。
    /// 供站点配置页展示「本地缓存了多少、上次/下次什么时候同步」。可空语义：
    /// - ``last_sync_at`` 为 None = 从未同步过；
    /// - ``next_sync_at`` 为 None = 立即到期（新站等待首刷）；
    /// - ``last_error`` 为 None = 上次同步成功。
    struct SiteSyncStatsView: Codable, Hashable, Sendable {
        /// 该站点已缓存的种子数
        var torrentCount: Int
        /// 开始跟踪时间(t0)
        var trackingSince: String?
        /// 上次同步完成时间
        var lastSyncAt: String?
        /// 上次同步成功时间；None=从未成功
        var lastSuccessAt: String?
        /// 下次同步到期时刻；None=立即到期
        var nextSyncAt: String?
        /// 当前自适应轮询间隔（秒）
        var syncIntervalSeconds: Int?
        /// 上次同步新增种子数
        var lastNewCount: Int?
        /// 上次同步失败原因；成功为 None
        var lastError: String?
        /// 连续同步失败次数；成功清零
        var consecutiveFailures: Int

        enum CodingKeys: String, CodingKey {
            case torrentCount = "torrent_count"
            case trackingSince = "tracking_since"
            case lastSyncAt = "last_sync_at"
            case lastSuccessAt = "last_success_at"
            case nextSyncAt = "next_sync_at"
            case syncIntervalSeconds = "sync_interval_seconds"
            case lastNewCount = "last_new_count"
            case lastError = "last_error"
            case consecutiveFailures = "consecutive_failures"
        }
    }

    /// 站点用户资料快照的对外视图（数据来源见 SiteUserProfile 模型）。
    /// 上传/下载量只回传字节数，由前端统一格式化；``ratio`` 为 None 表示站点
    /// 未提供（与 0.0 —— 真实无上传 —— 含义不同，前端应显示为"—"）。
    struct SiteUserProfileView: Codable, Hashable, Sendable {
        var username: String
        var userClass: String
        var uploadedBytes: Int
        var downloadedBytes: Int
        var ratio: Double?
        var bonus: Double?
        var seedingCount: Int
        var leechingCount: Int
        var fetchedAt: String

        enum CodingKeys: String, CodingKey {
            case username
            case userClass = "user_class"
            case uploadedBytes = "uploaded_bytes"
            case downloadedBytes = "downloaded_bytes"
            case ratio
            case bonus
            case seedingCount = "seeding_count"
            case leechingCount = "leeching_count"
            case fetchedAt = "fetched_at"
        }
    }

    /// 一个可显式调用的 Agent 技能（composer 加号菜单的数据源）。
    struct SkillView: Codable, Hashable, Sendable {
        /// 技能名，也是 /skill:名字 占位符里的名字
        var name: String
        /// 技能用途描述（菜单项的提示文案）
        var description: String
        /// 来源层级：builtin=随产品内置，user=用户技能目录
        var scope: String

        enum CodingKeys: String, CodingKey {
            case name
            case description
            case scope
        }
    }

    struct SourceCandidateView: Codable, Hashable, Sendable {
        var kind: String
        var key: String
        var language: String?
        var format: String?
        var provenance: String
        var excluded: String?
        var reasons: [String]
        var selectable: Bool
        var requiresOcr: Bool

        enum CodingKeys: String, CodingKey {
            case kind
            case key
            case language
            case format
            case provenance
            case excluded
            case reasons
            case selectable
            case requiresOcr = "requires_ocr"
        }
    }

    /// 分区首屏需要的一切：总开关、端点、可选服务目录。
    struct StatusView: Codable, Hashable, Sendable {
        /// MCP 总开关
        var enabled: Bool
        /// 端点地址的公共前缀
        var baseUrl: String
        /// 是否已配置外部访问地址；否则上面的地址只在局域网可用
        var externalUrlConfigured: Bool
        var endpoints: [API.EndpointView]
        var services: [API.ServiceView]

        enum CodingKeys: String, CodingKey {
            case enabled
            case baseUrl = "base_url"
            case externalUrlConfigured = "external_url_configured"
            case endpoints
            case services
        }
    }

    /// 面板读取占用时拿到的状态：统计很慢，所以接口给的是「上次结果 + 是否在算」。
    struct StorageStateView: Codable, Hashable, Sendable {
        /// 上一次统计的结果；进程内还没统计过时为空
        var usage: API.StorageUsageView?
        /// 后台是否正在统计，前端据此显示进行中并轮询
        var computing: Bool
        /// 上一次统计失败的原因，旧结果仍然可用
        var error: String?

        enum CodingKeys: String, CodingKey {
            case usage
            case computing
            case error
        }
    }

    struct StorageUsageView: Codable, Hashable, Sendable {
        var dataRoot: String
        var diskTotal: Int
        var diskUsed: Int
        var diskFree: Int
        var cacheBytes: Int
        var dataBytes: Int
        var dirs: [API.DirUsageView]
        var unregistered: [API.UnregisteredEntryView]
        /// 统计时刻（Unix 秒）
        var computedAt: Int

        enum CodingKeys: String, CodingKey {
            case dataRoot = "data_root"
            case diskTotal = "disk_total"
            case diskUsed = "disk_used"
            case diskFree = "disk_free"
            case cacheBytes = "cache_bytes"
            case dataBytes = "data_bytes"
            case dirs
            case unregistered
            case computedAt = "computed_at"
        }
    }

    /// 从 Discover 条目创建订阅的公开请求。
    /// 调用方只传递上游返回的稳定引用；来源识别、豆瓣到 TMDB 的锚定、媒体
    /// 建档和初始工单生成均由服务端完成。豆瓣发生歧义时，错误详情会返回可重试
    /// 的 TMDB ``title_ref`` 候选。
    struct SubscriptionCreatePayload: Codable, Hashable, Sendable {
        /// Discover 搜索、片单或详情返回的影视条目稳定引用
        var titleRef: String
        /// 可选的原始来源引用；从豆瓣歧义候选改选 TMDB 条目时原样回传，用于保留豆瓣身份
        var sourceTitleRef: String?
        /// 剧集要订阅的季号数组，如 [1,2]；剧集至少要给一季，只想追以后播出的新集就留空并把 follow_future 设为 true；电影留空
        var selectedSeasons: [Int]?
        /// 自动续订：未来新集与新季自动纳入订阅
        var followFuture: Bool?
        /// 缺省按规则组适用范围自动选组，都不命中用默认规则组
        var ruleSetId: Int?
        /// 入库目标库；缺省用该类型默认库
        var libraryId: Int?

        enum CodingKeys: String, CodingKey {
            case titleRef = "title_ref"
            case sourceTitleRef = "source_title_ref"
            case selectedSeasons = "selected_seasons"
            case followFuture = "follow_future"
            case ruleSetId = "rule_set_id"
            case libraryId = "library_id"
        }
    }

    /// 完整创建工作流结果：订阅本身以及管理员可见的下载路由预检。
    struct SubscriptionCreateView: Codable, Hashable, Sendable {
        var subscription: API.SubscriptionDetailView
        /// 管理员可见的下载与入库路由预检；成员调用时为空
        var downloadRouting: API.DispatchPreviewView?

        enum CodingKeys: String, CodingKey {
            case subscription
            case downloadRouting = "download_routing"
        }
    }

    /// 删除订阅的结果；勾了联动清理时带上后台任务 id 供任务中心跟进。
    struct SubscriptionDeleteView: Codable, Hashable, Sendable {
        /// 联动清理任务 id；没有可清理内容或未勾选时为空
        var cleanupJobId: String?

        enum CodingKeys: String, CodingKey {
            case cleanupJobId = "cleanup_job_id"
        }
    }

    struct SubscriptionDetailView: Codable, Hashable, Sendable {
        var id: Int
        var media: API.MediaBrief
        var status: String
        var selectedSeasons: [Int]
        var followFuture: Bool
        var ruleSetId: Int
        /// 入库目标库；null=该类型默认库
        var libraryId: Int?
        var progress: API.ProgressView
        /// 剧集按季收录统计；电影或无需展示时为空
        var seasonCollection: [API.SeasonOverview]
        var createdAt: String
        var updatedAt: String
        var wanted: [API.WantedView]
        /// 资源发布时间预测正在后台刷新（订阅创建/调整/恢复后的几秒内）；为 true 时 wanted[].release_forecast 可能还是旧值或空值，稍后重取即可
        var forecastPending: Bool

        enum CodingKeys: String, CodingKey {
            case id
            case media
            case status
            case selectedSeasons = "selected_seasons"
            case followFuture = "follow_future"
            case ruleSetId = "rule_set_id"
            case libraryId = "library_id"
            case progress
            case seasonCollection = "season_collection"
            case createdAt = "created_at"
            case updatedAt = "updated_at"
            case wanted
            case forecastPending = "forecast_pending"
        }
    }

    /// 订阅在途种子的实时下载快照（详情页轮询展示）。
    /// state 词表与 TorrentStatus.state 一致，另加 missing——种子已不在任何
    /// 可用下载器中（可能被手动删除，救援巡检稍后会退回工单重新找资源）。
    struct SubscriptionDownloadView: Codable, Hashable, Sendable {
        var infoHash: String
        /// 下载器中的任务名；missing 时为空
        var name: String?
        /// 0.0~1.0；missing 时为空
        var progress: Double?
        var sizeBytes: Int?
        var dlspeedBytes: Int?
        var etaSeconds: Int?
        /// downloading / stalled / paused / completed / error / missing / unknown
        var state: String
        /// state 为 error 时下载器给出的可读原因；其余为空
        var errorMessage: String?
        var downloaderName: String?
        var units: [API.DownloadUnitView]

        enum CodingKeys: String, CodingKey {
            case infoHash = "info_hash"
            case name
            case progress
            case sizeBytes = "size_bytes"
            case dlspeedBytes = "dlspeed_bytes"
            case etaSeconds = "eta_seconds"
            case state
            case errorMessage = "error_message"
            case downloaderName = "downloader_name"
            case units
        }
    }

    /// 自动续订是详情页上的独立动作，不与选季等批量调整耦合。
    struct SubscriptionFollowFuturePayload: Codable, Hashable, Sendable {
        /// 是否持续追踪之后播出的新集与新一季
        var enabled: Bool

        enum CodingKeys: String, CodingKey {
            case enabled
        }
    }

    /// 清理确认弹窗的预览：勾上开关会连带处理掉多少东西。
    /// 体积只给媒体库那一侧——它来自台账、是准确值；种子体积要连下载器才知道，
    /// 不值得让一个确认弹窗等网络往返，也不该拿估算值吓唬用户。
    struct SubscriptionRemovalPreviewView: Codable, Hashable, Sendable {
        /// 范围内、仍可定位的下载任务数
        var torrentCount: Int
        /// 下载任务名（最多前 5 条，供弹窗举例）
        var torrentTitles: [String]
        /// 其中处于 H&R 考核或考核状态未知的任务数；删除可能影响站点考核
        var hitAndRunCount: Int
        /// 范围内的媒体库文件数（含缺失记录）
        var libraryFileCount: Int
        /// 上述文件的台账体积合计（字节）
        var libraryBytes: Int
        /// 媒体库文件删除后在回收站的保留天数，期间可恢复
        var recycleRetentionDays: Int
        /// 按季清理时不会删除的跨季种子（仍被保留的季使用）；整条退订时为空
        var retainedCrossSeason: [API.RetainedTorrentView]

        enum CodingKeys: String, CodingKey {
            case torrentCount = "torrent_count"
            case torrentTitles = "torrent_titles"
            case hitAndRunCount = "hit_and_run_count"
            case libraryFileCount = "library_file_count"
            case libraryBytes = "library_bytes"
            case recycleRetentionDays = "recycle_retention_days"
            case retainedCrossSeason = "retained_cross_season"
        }
    }

    /// 订阅表单打开前的内部预览请求。
    /// ``title_ref`` 必须直接来自 Discover；服务端负责识别来源、解析豆瓣候选并
    /// 建立 TMDB 锚点，Web 不再拼装 ``source/kind/external_id`` 组合。
    struct SubscriptionTargetPreviewPayload: Codable, Hashable, Sendable {
        /// Discover 返回的影视条目稳定引用
        var titleRef: String

        enum CodingKeys: String, CodingKey {
            case titleRef = "title_ref"
        }
    }

    /// 用户可显式设置的追踪状态；完成态仍由工单自动派生。
    typealias SubscriptionTrackingState = String
    // 取值：'active', 'paused'

    struct SubscriptionTrackingStatePayload: Codable, Hashable, Sendable {
        /// 目标追踪状态：active 恢复追踪，paused 暂停搜索与投递
        var state: API.SubscriptionTrackingState

        enum CodingKeys: String, CodingKey {
            case state
        }
    }

    /// 部分更新语义：不传的字段一律保持不变。
    struct SubscriptionUpdatePayload: Codable, Hashable, Sendable {
        /// 新的季选择，如 [1,2]；不传=不变
        var selectedSeasons: [Int]?
        /// 是否自动续订（未来新集与新季自动纳入）；不传=不变
        var followFuture: Bool?
        /// 换绑规则组 id；不传=不变
        var ruleSetId: Int?
        /// 换入库目标库；显式传 null=清除指定、改回按默认库路由；不传=不变
        var libraryId: Int?

        enum CodingKeys: String, CodingKey {
            case selectedSeasons = "selected_seasons"
            case followFuture = "follow_future"
            case ruleSetId = "rule_set_id"
            case libraryId = "library_id"
        }
    }

    struct SubscriptionView: Codable, Hashable, Sendable {
        var id: Int
        var media: API.MediaBrief
        var status: String
        var selectedSeasons: [Int]
        var followFuture: Bool
        var ruleSetId: Int
        /// 入库目标库；null=该类型默认库
        var libraryId: Int?
        var progress: API.ProgressView
        /// 剧集按季收录统计；电影或无需展示时为空
        var seasonCollection: [API.SeasonOverview]
        var createdAt: String
        var updatedAt: String

        enum CodingKeys: String, CodingKey {
            case id
            case media
            case status
            case selectedSeasons = "selected_seasons"
            case followFuture = "follow_future"
            case ruleSetId = "rule_set_id"
            case libraryId = "library_id"
            case progress
            case seasonCollection = "season_collection"
            case createdAt = "created_at"
            case updatedAt = "updated_at"
        }
    }

    /// 字幕预览中的一条对白；时间统一使用毫秒，前端只负责格式化。
    struct SubtitleCueView: Codable, Hashable, Sendable {
        var startMs: Int
        var endMs: Int
        var text: String

        enum CodingKeys: String, CodingKey {
            case startMs = "start_ms"
            case endMs = "end_ms"
            case text
        }
    }

    /// 删除一个外挂字幕文件后的回执（路径回显，便于用户核对删的是哪一个）。
    struct SubtitleDeleteResultView: Codable, Hashable, Sendable {
        /// 已删除的字幕文件完整路径
        var path: String
        /// 释放的磁盘空间
        var freedBytes: Int

        enum CodingKeys: String, CodingKey {
            case path
            case freedBytes = "freed_bytes"
        }
    }

    struct SubtitlePlanView: Codable, Hashable, Sendable {
        var trackRef: String
        var kind: String
        var language: String?
        var isDefault: Bool
        var isAi: Bool

        enum CodingKeys: String, CodingKey {
            case trackRef = "track_ref"
            case kind
            case language
            case isDefault = "is_default"
            case isAi = "is_ai"
        }
    }

    /// 详情页字幕预览：格式元数据 + 已去样式的时间轴对白。
    struct SubtitlePreviewView: Codable, Hashable, Sendable {
        /// 本次预览的中性轨引用
        var track: String
        var format: String?
        var eventCount: Int
        var cues: [API.SubtitleCueView]
        /// 等待文案；为空表示已就绪
        var pending: String?
        /// 建议的下次轮询间隔
        var retryAfterMs: Int

        enum CodingKeys: String, CodingKey {
            case track
            case format
            case eventCount = "event_count"
            case cues
            case pending
            case retryAfterMs = "retry_after_ms"
        }
    }

    /// 一条字幕：内封轨（ffprobe）或外挂文件（目录发现）。
    struct SubtitleStreamView: Codable, Hashable, Sendable {
        /// 内封轨编码（subrip/ass/pgs…）；外挂为文件扩展名
        var codec: String?
        var language: String?
        var title: String?
        var forced: Bool
        var `default`: Bool
        /// 是否外挂字幕文件
        var external: Bool
        /// 外挂字幕的文件名
        var fileName: String?

        enum CodingKeys: String, CodingKey {
            case codec
            case language
            case title
            case forced
            case `default`
            case external
            case fileName = "file_name"
        }
    }

    /// 切换到浏览器已保存的某个账号（按用户名，用户名在超管与成员间全局唯一）。
    struct SwitchAccountRequest: Codable, Hashable, Sendable {
        var username: String

        enum CodingKeys: String, CodingKey {
            case username
        }
    }

    /// 同步令牌的对外视图。
    struct SyncTokenView: Codable, Hashable, Sendable {
        /// 是否已启用同步（令牌是否存在）
        var enabled: Bool
        /// 当前令牌明文，供复制进插件；未启用为 None
        var token: String?
        /// 令牌生成时间
        var createdAt: String?

        enum CodingKeys: String, CodingKey {
            case enabled
            case token
            case createdAt = "created_at"
        }
    }

    struct TextPart: Codable, Hashable, Sendable {
        var type: String
        var text: String

        enum CodingKeys: String, CodingKey {
            case type
            case text
        }
    }

    /// 模型的思考控制方言声明（能力事实，不是功能承诺）。
    /// 三种 kind 覆盖已知端点：
    /// - ``effort``：档位原词直传（请求体 reasoning_effort），levels 声明该模型
    /// 的原生档位子集 = 用户菜单；off 编码为 reasoning_effort="none"；
    /// - ``budget``：enable_thinking + thinking_budget，档位按模型
    /// max_thinking_tokens 比例分段（低 25% / 中 50% / 高 100%）；未声明
    /// 预算上限的模型退化为仅开关；
    /// - ``toggle``：仅开/关（如 GLM 的 thinking.type），菜单只有「关」。
    /// 未声明本字段的模型没有菜单、档位永不落请求（fail-closed）。
    struct ThinkingControl: Codable, Hashable, Sendable {
        var kind: String
        var levels: [String]
        var supportsOff: Bool

        enum CodingKeys: String, CodingKey {
            case kind
            case levels
            case supportsOff = "supports_off"
        }
    }

    /// 模型的思考控制方言声明（能力事实，不是功能承诺）。
    /// 三种 kind 覆盖已知端点：
    /// - ``effort``：档位原词直传（请求体 reasoning_effort），levels 声明该模型
    /// 的原生档位子集 = 用户菜单；off 编码为 reasoning_effort="none"；
    /// - ``budget``：enable_thinking + thinking_budget，档位按模型
    /// max_thinking_tokens 比例分段（低 25% / 中 50% / 高 100%）；未声明
    /// 预算上限的模型退化为仅开关；
    /// - ``toggle``：仅开/关（如 GLM 的 thinking.type），菜单只有「关」。
    /// 未声明本字段的模型没有菜单、档位永不落请求（fail-closed）。
    struct ThinkingControlInput: Codable, Hashable, Sendable {
        var kind: String
        var levels: [String]?
        var supportsOff: Bool?

        enum CodingKeys: String, CodingKey {
            case kind
            case levels
            case supportsOff = "supports_off"
        }
    }

    /// 模型的思考过程（如 deepseek-r1 的 reasoning_content）。
    /// 仅出现在响应侧的历史消息里；发回供应商时协议层会将其丢弃——
    /// 各家 API 均不接受思考内容作为输入。
    struct ThinkingPart: Codable, Hashable, Sendable {
        var type: String
        var text: String

        enum CodingKeys: String, CodingKey {
            case type
            case text
        }
    }

    /// 一次历史影视条目搜索保存的完整结果。
    struct TitleSearchHistoryResultsView: Codable, Hashable, Sendable {
        var vertical: String
        var historyId: Int
        var keyword: String
        var snapshotAt: String
        var total: Int
        var items: [[String: API.JSONValue]]

        enum CodingKeys: String, CodingKey {
            case vertical
            case historyId = "history_id"
            case keyword
            case snapshotAt = "snapshot_at"
            case total
            case items
        }
    }

    /// 执行一次影视条目搜索。
    struct TitleSearchPayload: Codable, Hashable, Sendable {
        /// 要搜索的影视片名
        var query: String
        /// 搜索来源：all 同时搜索 TMDB 和豆瓣，也可只指定 tmdb / douban
        var provider: API.DiscoveryProviderSelection?
        /// 是否把本次搜索及结果快照写入当前账号的搜索历史
        var saveHistory: Bool?

        enum CodingKeys: String, CodingKey {
            case query
            case provider
            case saveHistory = "save_history"
        }
    }

    /// 多来源搜索中单个来源的执行状态。
    struct TitleSearchProviderStatus: Codable, Hashable, Sendable {
        var provider: API.MediaSource
        var success: Bool
        var resultCount: Int
        var message: String?

        enum CodingKeys: String, CodingKey {
            case provider
            case success
            case resultCount = "result_count"
            case message
        }
    }

    /// 一次统一影视搜索的结果；单个来源失败不会丢掉其他来源的结果。
    struct TitleSearchView: Codable, Hashable, Sendable {
        var query: String
        var titles: [API.DiscoveredTitleView]
        var providers: [API.TitleSearchProviderStatus]
        /// 已记录历史时返回其 ID
        var historyId: Int?

        enum CodingKeys: String, CodingKey {
            case query
            case titles
            case providers
            case historyId = "history_id"
        }
    }

    /// 订阅首页的单集待入库摘要；不携带海报和下载进度等重复信息。
    struct TodayArrivalView: Codable, Hashable, Sendable {
        var subscriptionId: Int
        var wantedId: Int
        var mediaTitle: String
        var mediaKind: String
        var seasonNumber: Int
        var episodeNumber: Int
        var status: String
        var airDate: String?
        /// 预计入库/播出的站点日历日，用于展示日期
        var expectedDay: String
        /// expected_day 距今天几天（0=今天）；站点日历口径，前端据此切换今日/预告文案
        var daysAhead: Int
        var releaseForecast: [String: API.JSONValue]?
        /// 按站点游标与礼貌间隔换算后的下一次有效预测探测时间
        var nextProbeAt: String?
        var infoHash: String?
        var grabbedAt: String?
        var downloadedAt: String?
        /// 预计出种后到入库的分钟数；优先使用本订阅历史中位数
        var estimatedReleaseToImportMinutes: Int
        /// 下载完成后到入库的分钟数；优先使用本订阅历史中位数
        var estimatedDownloadToImportMinutes: Int

        enum CodingKeys: String, CodingKey {
            case subscriptionId = "subscription_id"
            case wantedId = "wanted_id"
            case mediaTitle = "media_title"
            case mediaKind = "media_kind"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case status
            case airDate = "air_date"
            case expectedDay = "expected_day"
            case daysAhead = "days_ahead"
            case releaseForecast = "release_forecast"
            case nextProbeAt = "next_probe_at"
            case infoHash = "info_hash"
            case grabbedAt = "grabbed_at"
            case downloadedAt = "downloaded_at"
            case estimatedReleaseToImportMinutes = "estimated_release_to_import_minutes"
            case estimatedDownloadToImportMinutes = "estimated_download_to_import_minutes"
        }
    }

    struct ToggleRequest: Codable, Hashable, Sendable {
        var enabled: Bool

        enum CodingKeys: String, CodingKey {
            case enabled
        }
    }

    struct TokenUsage: Codable, Hashable, Sendable {
        var promptTokens: Int
        var completionTokens: Int
        var totalTokens: Int
        var cacheReadTokens: Int

        enum CodingKeys: String, CodingKey {
            case promptTokens = "prompt_tokens"
            case completionTokens = "completion_tokens"
            case totalTokens = "total_tokens"
            case cacheReadTokens = "cache_read_tokens"
        }
    }

    /// 模型发起的一次工具调用。
    struct ToolCall: Codable, Hashable, Sendable {
        var id: String
        var name: String
        var arguments: [String: API.JSONValue]
        var rawArguments: String
        var parseError: String?

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case arguments
            case rawArguments = "raw_arguments"
            case parseError = "parse_error"
        }
    }

    /// 折叠模式下一个服务工具覆盖的一条命令。
    /// 折叠模式把整个服务压成一个工具，命令清单本来只以自然语言写在 description
    /// 里给模型看。管理页要是照搬那段文本，用户面对的就是一堵几百行的散文墙——
    /// 所以这里把同一份元数据结构化再给一遍，页面按表格渲染。
    struct ToolCommand: Codable, Hashable, Sendable {
        /// 命令名，即 command 参数的取值
        var name: String
        var summary: String
        /// params 里可填的字段名，必填的带 * 后缀
        var params: [String]
        /// confirm | destructive | 空
        var dangerous: String
        /// 提交后台任务，返回 job_id
        var isJob: Bool

        enum CodingKeys: String, CodingKey {
            case name
            case summary
            case params
            case dangerous
            case isJob = "is_job"
        }
    }

    /// 工具的一个参数。管理页要展示它，用户才能判断「这个工具好不好用」。
    struct ToolParameter: Codable, Hashable, Sendable {
        var name: String
        /// JSON Schema 类型，如 integer / string[]
        var type: String
        var required: Bool
        var description: String
        /// 落点：path / query / body
        var location: String
        /// 枚举取值。折叠模式下 command 的取值就靠它列出该服务覆盖了哪些命令
        var options: [String]

        enum CodingKeys: String, CodingKey {
            case name
            case type
            case required
            case description
            case location
            case options
        }
    }

    struct ToolPreview: Codable, Hashable, Sendable {
        var name: String
        var summary: String
        var description: String
        /// 所属服务域，详情页按它分组
        var service: String
        var readOnly: Bool
        var destructive: Bool
        var parameters: [API.ToolParameter]
        /// 仅折叠模式：这个服务工具覆盖的命令清单
        var commands: [API.ToolCommand]

        enum CodingKeys: String, CodingKey {
            case name
            case summary
            case description
            case service
            case readOnly = "read_only"
            case destructive
            case parameters
            case commands
        }
    }

    /// 从种子标题/副标题推导出的结构化属性。
    /// 空值语义：
    /// - 标量字段 ``None`` / 列表字段 ``[]`` = 未提取到；
    /// - ``remux`` 例外地用 ``False`` 当默认：种子名里不写 REMUX 基本就是非 Remux，
    /// 这是行业命名惯例里少数"缺席即否定"的标记；
    /// - ``complete`` 三态：True=明确标注全集/合集，None=没有标注（≠不是全集）。
    struct TorrentAttrs: Codable, Hashable, Sendable {
        var mediaType: String?
        var contentType: String?
        var titlesZh: [String]
        var titlesEn: [String]
        var titleCandidates: [String]
        var year: Int?
        var seasons: [Int]
        var episodes: [Int]
        var episodesTotal: Int?
        var complete: Bool?
        var resolution: String?
        var videoCodec: String?
        var hdr: [String]
        var mediaSource: String?
        var remux: Bool
        var audio: [String]
        var subtitleLanguages: [String]
        var subtitleCarriers: [String]
        var audioLanguages: [String]
        var platforms: [String]
        var releaseGroup: String?

        enum CodingKeys: String, CodingKey {
            case mediaType = "media_type"
            case contentType = "content_type"
            case titlesZh = "titles_zh"
            case titlesEn = "titles_en"
            case titleCandidates = "title_candidates"
            case year
            case seasons
            case episodes
            case episodesTotal = "episodes_total"
            case complete
            case resolution
            case videoCodec = "video_codec"
            case hdr
            case mediaSource = "media_source"
            case remux
            case audio
            case subtitleLanguages = "subtitle_languages"
            case subtitleCarriers = "subtitle_carriers"
            case audioLanguages = "audio_languages"
            case platforms
            case releaseGroup = "release_group"
        }
    }

    /// 应用级一级分类枚举。
    typealias TorrentCategory = String
    // 取值：'movie', 'tv', 'documentary', 'anime', 'music', 'game', 'av', 'other'

    /// 搜索结果里的单条种子——在 ``TorrentListItem`` 基础上补上来源站点与扩充属性。
    struct TorrentHit: Codable, Hashable, Sendable {
        var torrentId: String
        var title: String
        var subtitle: String
        var category: API.TorrentCategory?
        var siteCategoryId: String?
        var siteCategoryName: String?
        var size: String?
        var sizeBytes: Int
        var seeders: Int
        var leechers: Int
        var snatched: Int
        var uploadTime: String?
        var uploader: String
        var posterUrl: String?
        var imageUrls: [String]
        var free: Bool
        var freeDeadline: String?
        var downloadVolumeFactor: Double
        var uploadVolumeFactor: Double
        var hitAndRun: Bool?
        var detailUrl: String?
        var downloadUrl: String?
        var siteId: String
        var siteName: String
        var attrs: API.TorrentAttrs?

        enum CodingKeys: String, CodingKey {
            case torrentId = "torrent_id"
            case title
            case subtitle
            case category
            case siteCategoryId = "site_category_id"
            case siteCategoryName = "site_category_name"
            case size
            case sizeBytes = "size_bytes"
            case seeders
            case leechers
            case snatched
            case uploadTime = "upload_time"
            case uploader
            case posterUrl = "poster_url"
            case imageUrls = "image_urls"
            case free
            case freeDeadline = "free_deadline"
            case downloadVolumeFactor = "download_volume_factor"
            case uploadVolumeFactor = "upload_volume_factor"
            case hitAndRun = "hit_and_run"
            case detailUrl = "detail_url"
            case downloadUrl = "download_url"
            case siteId = "site_id"
            case siteName = "site_name"
            case attrs
        }
    }

    /// 一次历史 PT 资源搜索保存的完整结果。
    /// 结构与 ``SearchResponse`` 同构（items/sites/total 直接复用前端结果页的渲染
    /// 管线），外加历史行的范围回显与快照时间——前端据 ``snapshot_at`` 渲染
    /// 「这是 X 分钟前的快照」提示条。
    struct TorrentSearchHistoryResultsView: Codable, Hashable, Sendable {
        var vertical: String
        var historyId: Int
        var keyword: String
        var label: String?
        var categories: [String]
        var siteIds: [String]
        var snapshotAt: String
        var total: Int
        var elapsedMs: Int?
        var items: [API.TorrentHit]
        var sites: [API.SiteSearchStatus]

        enum CodingKeys: String, CodingKey {
            case vertical
            case historyId = "history_id"
            case keyword
            case label
            case categories
            case siteIds = "site_ids"
            case snapshotAt = "snapshot_at"
            case total
            case elapsedMs = "elapsed_ms"
            case items
            case sites
        }
    }

    /// 转移预览里的一个搬运单元。
    struct TransferMoveView: Codable, Hashable, Sendable {
        var sourcePath: String
        var targetPath: String
        /// true=整个条目目录搬走；false=只搬这一个文件
        var isDir: Bool
        var sizeBytes: Int
        /// 本单元随迁的台账行数
        var fileCount: Int

        enum CodingKeys: String, CodingKey {
            case sourcePath = "source_path"
            case targetPath = "target_path"
            case isDir = "is_dir"
            case sizeBytes = "size_bytes"
            case fileCount = "file_count"
        }
    }

    /// 条目转移的请求体：只需要目标库。
    struct TransferPayload: Codable, Hashable, Sendable {
        /// 转移目标库 id（必须与当前库同类型）
        var targetLibraryId: Int

        enum CodingKeys: String, CodingKey {
            case targetLibraryId = "target_library_id"
        }
    }

    /// 转移预览：完整的「将要发生什么」，用户确认后才执行。
    struct TransferPreviewView: Codable, Hashable, Sendable {
        var targetLibraryId: Int
        var targetLibraryName: String
        /// 目标库主根——条目目录会搬到这里
        var targetRoot: String
        var moves: [API.TransferMoveView]
        /// 不参与转移的路径与中文原因
        var skips: [API.TransferSkipView]
        var totalBytes: Int
        /// 缺失文件数（磁盘无实体，只随迁台账归属）
        var missingCount: Int
        /// 目标与源不在同一块盘——将退化为完整复制（耗时，且断开与做种目录的硬链接）
        var crossDevice: Bool
        /// 阻断性问题（如目标已有同名目录）；非空则不给执行
        var blocked: [String]

        enum CodingKeys: String, CodingKey {
            case targetLibraryId = "target_library_id"
            case targetLibraryName = "target_library_name"
            case targetRoot = "target_root"
            case moves
            case skips
            case totalBytes = "total_bytes"
            case missingCount = "missing_count"
            case crossDevice = "cross_device"
            case blocked
        }
    }

    /// 预览里的一条跳过说明：哪个路径、为什么不搬它。
    struct TransferSkipView: Codable, Hashable, Sendable {
        var filePath: String
        var reason: String

        enum CodingKeys: String, CodingKey {
            case filePath = "file_path"
            case reason
        }
    }

    /// 转移启动响应。
    struct TransferStartView: Codable, Hashable, Sendable {
        var started: Bool
        var message: String
        /// 持久化后台作业 ID，可在活动页继续观察
        var jobId: String
        /// false 表示复用了仍在进行的同一作业
        var created: Bool

        enum CodingKeys: String, CodingKey {
            case started
            case message
            case jobId = "job_id"
            case created
        }
    }

    /// 转移进度 / 最近一次结论（前端弹窗轮询这一个接口收尾）。
    struct TransferStatusView: Codable, Hashable, Sendable {
        var running: Bool
        var mediaItemId: Int?
        var title: String?
        var targetLibraryId: Int?
        var processed: Int
        var total: Int
        var finishedAt: String?
        var targetLibraryName: String?
        var movedPaths: [String]
        var filesRelocated: Int
        var bytesMoved: Int
        var removedDirs: Int
        /// 该片的订阅是否一并改挂到目标库（后续剧集直接投新库）
        var subscriptionMoved: Bool
        /// 成功搬运的条目数（批量/归并）
        var movedItems: Int
        /// 按策略跳过的条目数（如同名冲突）
        var skippedItems: Int
        /// 执行时出错的条目数
        var failedItems: Int
        /// 逐条跳过的中文原因
        var skips: [String]
        var errors: [String]

        enum CodingKeys: String, CodingKey {
            case running
            case mediaItemId = "media_item_id"
            case title
            case targetLibraryId = "target_library_id"
            case processed
            case total
            case finishedAt = "finished_at"
            case targetLibraryName = "target_library_name"
            case movedPaths = "moved_paths"
            case filesRelocated = "files_relocated"
            case bytesMoved = "bytes_moved"
            case removedDirs = "removed_dirs"
            case subscriptionMoved = "subscription_moved"
            case movedItems = "moved_items"
            case skippedItems = "skipped_items"
            case failedItems = "failed_items"
            case skips
            case errors
        }
    }

    struct TrashedBatchFailureView: Codable, Hashable, Sendable {
        var id: Int
        var fileName: String
        var error: String

        enum CodingKeys: String, CodingKey {
            case id
            case fileName = "file_name"
            case error
        }
    }

    /// 批量结果：逐文件执行、单独提交，失败不回滚已成功的。
    struct TrashedBatchResultView: Codable, Hashable, Sendable {
        /// 成功处理的文件数
        var done: Int
        var failed: [API.TrashedBatchFailureView]
        /// 按筛选清理时超出单次上限、尚未处理的文件数
        var remaining: Int

        enum CodingKeys: String, CodingKey {
            case done
            case failed
            case remaining
        }
    }

    /// 回收站里的一个待回收文件（一集 / 一个旧版本）。
    /// 品质字段来自台账行的 ffprobe 探测列；``audio_label`` 由服务端从首条音轨拼
    /// 「编码 声道」（与条目详情页文件区同一读法），前端不再解析音轨数组。
    struct TrashedFileView: Codable, Hashable, Sendable {
        var id: Int
        var fileName: String
        /// 当前物理位置（已移入回收站时是回收站内路径）
        var filePath: String
        /// 移入回收站前的原路径；NULL=原地待回收（移动失败降级），file_path 即原位
        var trashOriginalPath: String?
        /// 原地待回收形态（trash_original_path 为空）
        var keptInPlace: Bool
        var sizeBytes: Int
        var resolution: String?
        var mediaSource: String?
        var hdr: String?
        var videoCodec: String?
        var bitDepth: Int?
        /// 首条音轨的「编码 声道」，如 DTS-HD MA 5.1；未探测为 null
        var audioLabel: String?
        var releaseGroup: String?
        /// 季号；电影=0
        var seasonNumber: Int
        /// 集号；电影=0
        var episodeNumber: Int
        /// 集名（media_episode 表）；电影或未刮到为 null
        var episodeTitle: String?
        var trashedAt: String?
        /// 预计自动清理时间；null=不自动清理
        var purgeAfter: String?
        /// 审计快照 reason：upgrade_replaced / upgrade_refuted / manual …
        var reason: String?
        /// 审计快照 note（中文整句，如「洗版替换：1080p WEB-DL → 2160p Remux」）
        var note: String?
        /// 上次批量清理失败的中文原因；成功或恢复后清掉
        var lastError: String?

        enum CodingKeys: String, CodingKey {
            case id
            case fileName = "file_name"
            case filePath = "file_path"
            case trashOriginalPath = "trash_original_path"
            case keptInPlace = "kept_in_place"
            case sizeBytes = "size_bytes"
            case resolution
            case mediaSource = "media_source"
            case hdr
            case videoCodec = "video_codec"
            case bitDepth = "bit_depth"
            case audioLabel = "audio_label"
            case releaseGroup = "release_group"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case episodeTitle = "episode_title"
            case trashedAt = "trashed_at"
            case purgeAfter = "purge_after"
            case reason
            case note
            case lastError = "last_error"
        }
    }

    /// 回收站列表响应：聚合口径 + 本页条目。
    /// ``total_*`` / ``due_within_24h`` / ``kept_in_place`` 按全部筛选（搜索 + 库 +
    /// 原因）计算，与摘要行、「立即清理全部」的作用域是同一份数字；``by_library``
    /// 不受库筛选影响、``by_reason`` 不受原因筛选影响（分面计数，切换胶囊时其他
    /// 胶囊的数字不归零）。
    struct TrashedFilesData: Codable, Hashable, Sendable {
        var totalFiles: Int
        var totalItems: Int
        var totalBytes: Int
        /// 24 小时内将被自动清理的文件数
        var dueWithin24h: Int
        /// 原地待回收（移入回收站失败）的文件数
        var keptInPlace: Int
        var byLibrary: [API.TrashedLibraryCountView]
        var byReason: [API.TrashedReasonCountView]
        var items: [API.TrashedItemView]

        enum CodingKeys: String, CodingKey {
            case totalFiles = "total_files"
            case totalItems = "total_items"
            case totalBytes = "total_bytes"
            case dueWithin24h = "due_within_24h"
            case keptInPlace = "kept_in_place"
            case byLibrary = "by_library"
            case byReason = "by_reason"
            case items
        }
    }

    /// 「立即清理全部」的作用域：与列表接口同一组筛选参数，服务端重新查一遍 id。
    struct TrashedFilter: Codable, Hashable, Sendable {
        var q: String?
        var libraryId: Int?
        var reason: String?

        enum CodingKeys: String, CodingKey {
            case q
            case libraryId = "library_id"
            case reason
        }
    }

    /// 分组所属的媒体条目（片名、年份、海报）。
    struct TrashedItemRefView: Codable, Hashable, Sendable {
        var id: Int
        var title: String
        var year: Int?
        var kind: API.MediaKind
        var posterUrl: String?

        enum CodingKeys: String, CodingKey {
            case id
            case title
            case year
            case kind
            case posterUrl = "poster_url"
        }
    }

    /// 回收站列表的一行：一个条目（电影 / 剧）及其全部待回收文件。
    struct TrashedItemView: Codable, Hashable, Sendable {
        /// 分组键：条目 id；未识别文件按行各自成组（前端 React key）
        var key: String
        var library: API.TrashedLibraryRefView
        /// 未识别的待回收文件为 null，前端以文件名代标题
        var mediaItem: API.TrashedItemRefView?
        /// 涉及的季号（电影为空）
        var seasons: [Int]
        var fileCount: Int
        var totalBytes: Int
        /// 组内最早到期；全组不自动清理时为 null
        var earliestPurgeAfter: String?
        var latestPurgeAfter: String?
        /// 组内按 reason 计数
        var reasons: [String: Int]
        /// 组内 note 一致时的整句；混合时为 null，前端按 reasons 拼计数
        var note: String?
        /// 触发方快照文案，如「《九门》订阅洗版」
        var triggerLabel: String?
        var latestTrashedAt: String?
        var quality: API.TrashedQualityView
        var files: [API.TrashedFileView]

        enum CodingKeys: String, CodingKey {
            case key
            case library
            case mediaItem = "media_item"
            case seasons
            case fileCount = "file_count"
            case totalBytes = "total_bytes"
            case earliestPurgeAfter = "earliest_purge_after"
            case latestPurgeAfter = "latest_purge_after"
            case reasons
            case note
            case triggerLabel = "trigger_label"
            case latestTrashedAt = "latest_trashed_at"
            case quality
            case files
        }
    }

    struct TrashedLibraryCountView: Codable, Hashable, Sendable {
        var libraryId: Int
        var name: String
        var count: Int

        enum CodingKeys: String, CodingKey {
            case libraryId = "library_id"
            case name
            case count
        }
    }

    struct TrashedLibraryRefView: Codable, Hashable, Sendable {
        var id: Int
        var name: String

        enum CodingKeys: String, CodingKey {
            case id
            case name
        }
    }

    /// 批量清理：``ids``（所选 / 条目行 / 单个文件）与 ``filter``（全部）二选一。
    struct TrashedPurgePayload: Codable, Hashable, Sendable {
        /// 待清理的文件 id 列表
        var ids: [Int]?
        /// 按筛选清理全部（一次最多 500 个）
        var filter: API.TrashedFilter?

        enum CodingKeys: String, CodingKey {
            case ids
            case filter
        }
    }

    /// 组内品质汇总：``tiers`` 按「分辨率 片源」计数，其余字段去重列表。
    struct TrashedQualityView: Codable, Hashable, Sendable {
        var tiers: [String: Int]
        var hdr: [String]
        var videoCodecs: [String]
        var audioLabels: [String]
        var releaseGroups: [String]

        enum CodingKeys: String, CodingKey {
            case tiers
            case hdr
            case videoCodecs = "video_codecs"
            case audioLabels = "audio_labels"
            case releaseGroups = "release_groups"
        }
    }

    struct TrashedReasonCountView: Codable, Hashable, Sendable {
        var reason: String
        var count: Int

        enum CodingKeys: String, CodingKey {
            case reason
            case count
        }
    }

    struct TrashedRestorePayload: Codable, Hashable, Sendable {
        /// 待恢复的文件 id 列表
        var ids: [Int]

        enum CodingKeys: String, CodingKey {
            case ids
        }
    }

    /// 进度条缩略图索引。
    /// `ready=false` 表示还在生成（或这部片生成不了）——前端表现为「暂无预览」，
    /// 不影响播放。前端据 `interval_ms` 与格子尺寸算「第 t 秒在哪张图的哪一格」。
    struct TrickplayView: Codable, Hashable, Sendable {
        var ready: Bool
        var intervalMs: Int
        var tileWidth: Int
        var tileHeight: Int
        var columns: Int
        var rows: Int
        var count: Int
        var sheets: [String]

        enum CodingKeys: String, CodingKey {
            case ready
            case intervalMs = "interval_ms"
            case tileWidth = "tile_width"
            case tileHeight = "tile_height"
            case columns
            case rows
            case count
            case sheets
        }
    }

    /// 定时任务的触发方式。
    /// - ``INTERVAL``：固定间隔重复（如每 300 秒一次），配 ``interval_seconds``。
    /// - ``CRON``：按 cron 表达式在具体时刻触发（如每天 03:00），配 ``cron_expr``。
    /// 两种方式覆盖了绝大多数周期性任务场景，且都能被 APScheduler 原生表达。
    typealias TriggerType = String
    // 取值：'interval', 'cron'

    /// 全站界面样式偏好，按页面分组。新页面的设定加嵌套模型字段即可。
    struct UiPreferencesSetting: Codable, Hashable, Sendable {
        /// 主题 id，取值见前端 lib/themes.ts 的注册表（silver / netflix）。存纯字符串并放宽校验：未知值由前端 normalizeThemeId 兜底为默认主题，老后端读到新主题 id 也不会整体拒绝（前向兼容）。
        var theme: String
        /// 桌面端（≥768px 视口）的主题 id 覆盖；空 = 跟随 theme。校验口径与 theme 相同（纯字符串、前端兜底未知值），2026-09 起桌面 / 移动端可分别选主题，见前端 lib/ui-prefs.tsx 的 resolveThemeId。
        var themeDesktop: String?
        /// 移动端（<768px 视口）的主题 id 覆盖；空 = 跟随 theme。
        var themeMobile: String?
        /// 侧边栏玻璃面板
        var sidebar: API.SidebarUiPrefs
        /// 全站背景蒙版
        var scrim: API.ScrimUiPrefs
        /// 侧边栏主导航排序
        var nav: API.NavUiPrefs
        /// 媒体库首页的行清单
        var home: API.HomeUiPrefs

        enum CodingKeys: String, CodingKey {
            case theme
            case themeDesktop = "theme_desktop"
            case themeMobile = "theme_mobile"
            case sidebar
            case scrim
            case nav
            case home
        }
    }

    /// 全站界面样式偏好，按页面分组。新页面的设定加嵌套模型字段即可。
    struct UiPreferencesSettingInput: Codable, Hashable, Sendable {
        /// 主题 id，取值见前端 lib/themes.ts 的注册表（silver / netflix）。存纯字符串并放宽校验：未知值由前端 normalizeThemeId 兜底为默认主题，老后端读到新主题 id 也不会整体拒绝（前向兼容）。
        var theme: String?
        /// 桌面端（≥768px 视口）的主题 id 覆盖；空 = 跟随 theme。校验口径与 theme 相同（纯字符串、前端兜底未知值），2026-09 起桌面 / 移动端可分别选主题，见前端 lib/ui-prefs.tsx 的 resolveThemeId。
        var themeDesktop: String?
        /// 移动端（<768px 视口）的主题 id 覆盖；空 = 跟随 theme。
        var themeMobile: String?
        /// 侧边栏玻璃面板
        var sidebar: API.SidebarUiPrefsInput?
        /// 全站背景蒙版
        var scrim: API.ScrimUiPrefsInput?
        /// 侧边栏主导航排序
        var nav: API.NavUiPrefsInput?
        /// 媒体库首页的行清单
        var home: API.HomeUiPrefsInput?

        enum CodingKeys: String, CodingKey {
            case theme
            case themeDesktop = "theme_desktop"
            case themeMobile = "theme_mobile"
            case sidebar
            case scrim
            case nav
            case home
        }
    }

    /// 收敛器判不了时留下的一个候选（用户点一下即可认领）。
    struct UnidentifiedCandidateView: Codable, Hashable, Sendable {
        var tmdbId: Int
        var title: String
        var year: Int?
        /// 该候选对应季的集数——同名双版本靠它一眼区分
        var episodeCount: Int?
        /// 本地证据对它的佐证（年份相同/本地 N 集吻合…）
        var reasons: [String]

        enum CodingKeys: String, CodingKey {
            case tmdbId = "tmdb_id"
            case title
            case year
            case episodeCount = "episode_count"
            case reasons
        }
    }

    /// 批量忽略整库的待识别文件（只打忽略标记，绝不动磁盘）。
    struct UnidentifiedClearPayload: Codable, Hashable, Sendable {
        /// 要批量忽略待识别文件的媒体库 id
        var libraryId: Int

        enum CodingKeys: String, CodingKey {
            case libraryId = "library_id"
        }
    }

    /// 待识别清单的一行。
    struct UnidentifiedFileView: Codable, Hashable, Sendable {
        var id: Int
        var libraryId: Int
        var libraryName: String
        var filePath: String
        var sizeBytes: Int
        var seasonNumber: Int
        var episodeNumber: Int
        /// 识别失败原因整句（展开/悬停查看；清单上只显示标签）
        var reason: String?
        /// 失败分类：unparsable / tmdb_unreachable / ambiguous / no_match
        var code: String?
        var candidates: [API.UnidentifiedCandidateView]

        enum CodingKeys: String, CodingKey {
            case id
            case libraryId = "library_id"
            case libraryName = "library_name"
            case filePath = "file_path"
            case sizeBytes = "size_bytes"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case reason
            case code
            case candidates
        }
    }

    /// 待识别清单的一组：同一条目目录下的文件聚成一条。
    /// 一部剧几十集全认不出时，逐集列出来会把清单刷爆、也让人无从下手；
    /// 按条目目录聚合后一次认领整组（各文件沿用自己已解析的季集号）。
    struct UnidentifiedGroupView: Codable, Hashable, Sendable {
        /// 分组键：条目目录绝对路径；裸文件用文件自身路径
        var key: String
        /// 展示名：条目目录名（裸文件为文件名）
        var label: String
        var libraryId: Int
        var libraryName: String
        var fileCount: Int
        var totalSizeBytes: Int
        /// 组内共同的识别失败原因整句
        var reason: String?
        /// 组内共同的失败分类（决定标签与配色）
        var code: String?
        /// 组内共同的候选：点一下整组认领
        var candidates: [API.UnidentifiedCandidateView]
        var files: [API.UnidentifiedFileView]

        enum CodingKeys: String, CodingKey {
            case key
            case label
            case libraryId = "library_id"
            case libraryName = "library_name"
            case fileCount = "file_count"
            case totalSizeBytes = "total_size_bytes"
            case reason
            case code
            case candidates
            case files
        }
    }

    struct UnregisteredEntryView: Codable, Hashable, Sendable {
        var path: String
        var bytes: Int

        enum CodingKeys: String, CodingKey {
            case path
            case bytes
        }
    }

    /// 媒体库首页「接下来继续」的一张卡片。
    /// 卡片指向的**永远是还没看完的那个单元**——电影是它自己，剧集是从最近播放
    /// 那一集起往后第一个没看完的。看完的作品不出卡，所以这里没有"已看完"态。
    struct UpNextItemView: Codable, Hashable, Sendable {
        var mediaItemId: Int
        var libraryId: Int
        var kind: API.MediaKind
        var title: String
        var year: Int?
        var posterUrl: String?
        /// 海报宽高比（同海报墙 primary_aspect）
        var posterAspect: Double
        var backdropUrl: String?
        var episodeStillUrl: String?
        var seasonNumber: Int
        var episodeNumber: Int
        var episodeTitle: String?
        var unwatchedAheadCount: Int
        var positionMs: Int
        var durationMs: Int?
        var progressPercent: Int?
        /// 指向的是下一集，而不是上次那一个
        var advanced: Bool
        var lastPlayedAt: String

        enum CodingKeys: String, CodingKey {
            case mediaItemId = "media_item_id"
            case libraryId = "library_id"
            case kind
            case title
            case year
            case posterUrl = "poster_url"
            case posterAspect = "poster_aspect"
            case backdropUrl = "backdrop_url"
            case episodeStillUrl = "episode_still_url"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case episodeTitle = "episode_title"
            case unwatchedAheadCount = "unwatched_ahead_count"
            case positionMs = "position_ms"
            case durationMs = "duration_ms"
            case progressPercent = "progress_percent"
            case advanced
            case lastPlayedAt = "last_played_at"
        }
    }

    /// 「接下来继续」横排的数据载荷。
    struct UpNextView: Codable, Hashable, Sendable {
        var items: [API.UpNextItemView]

        enum CodingKeys: String, CodingKey {
            case items
        }
    }

    /// 「检查更新」的结果。
    struct UpdateCheckView: Codable, Hashable, Sendable {
        var currentVersion: String
        var latestVersion: String
        var updateAvailable: Bool
        var compatible: Bool
        var requiresRuntime: Int
        var changelog: String
        var publishedAt: String
        var latestKnownBad: Bool

        enum CodingKeys: String, CodingKey {
            case currentVersion = "current_version"
            case latestVersion = "latest_version"
            case updateAvailable = "update_available"
            case compatible
            case requiresRuntime = "requires_runtime"
            case changelog
            case publishedAt = "published_at"
            case latestKnownBad = "latest_known_bad"
        }
    }

    /// 修改个人信息（当前只有昵称；登录用户名不可改）。
    struct UpdateProfileRequest: Codable, Hashable, Sendable {
        /// 展示昵称
        var nickname: String

        enum CodingKeys: String, CodingKey {
            case nickname
        }
    }

    /// 更新执行进度（前端轮询）。
    struct UpdateProgressView: Codable, Hashable, Sendable {
        var phase: String
        var detail: String
        var percent: Double?
        var error: String?
        var targetVersion: String?

        enum CodingKeys: String, CodingKey {
            case phase
            case detail
            case percent
            case error
            case targetVersion = "target_version"
        }
    }

    /// 保存本地版本保留数的请求体。
    struct UpdateRetentionPayload: Codable, Hashable, Sendable {
        /// 本地保留的版本目录数（含当前版本）
        var keepVersions: Int

        enum CodingKeys: String, CodingKey {
            case keepVersions = "keep_versions"
        }
    }

    /// 「设置 → 关于与更新」的状态区。
    struct UpdateStatusView: Codable, Hashable, Sendable {
        var currentVersion: String
        var codeSource: String
        var overlayVersion: String?
        var runtimeVersion: Int?
        var canUpdate: Bool
        var hasPrevious: Bool
        var previousVersion: String?
        var badVersions: [String]
        var modelTag: String?
        var inactiveOverlayVersion: String?
        var inactiveOverlayReason: String?
        var lastAbnormalExit: API.LastAbnormalExitView?

        enum CodingKeys: String, CodingKey {
            case currentVersion = "current_version"
            case codeSource = "code_source"
            case overlayVersion = "overlay_version"
            case runtimeVersion = "runtime_version"
            case canUpdate = "can_update"
            case hasPrevious = "has_previous"
            case previousVersion = "previous_version"
            case badVersions = "bad_versions"
            case modelTag = "model_tag"
            case inactiveOverlayVersion = "inactive_overlay_version"
            case inactiveOverlayReason = "inactive_overlay_reason"
            case lastAbnormalExit = "last_abnormal_exit"
        }
    }

    /// 「一轮洗版」请求（docs/design/quality-upgrade.md §13.2）。
    struct UpgradeRunPayload: Codable, Hashable, Sendable {
        /// 可选：先换用该规则组再触发（组必须已配置洗版目标）；缺省用当前组
        var ruleSetId: Int?

        enum CodingKeys: String, CodingKey {
            case ruleSetId = "rule_set_id"
        }
    }

    /// 一轮洗版的逐集体检结果。
    struct UpgradeRunUnitView: Codable, Hashable, Sendable {
        var seasonNumber: Int
        var episodeNumber: Int
        /// 可洗已排期 / 已达目标 / 洗版在途 / 无法识别当前版本 / 缺失走补缺
        var state: String
        /// 当前版本档位标签；未入库/无法识别为 null
        var currentLabel: String?
        /// 洗版目标档位标签
        var targetLabel: String

        enum CodingKeys: String, CodingKey {
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case state
            case currentLabel = "current_label"
            case targetLabel = "target_label"
        }
    }

    /// 一轮洗版的体检报告（同步返回的一次性快照，不落库）。
    struct UpgradeRunView: Codable, Hashable, Sendable {
        var targetLabel: String
        /// 本轮实际生效的规则组（换组后为新组）
        var ruleSetId: Int
        /// 中文摘要句，前端直接展示
        var summary: String
        var counts: [String: Int]
        var units: [API.UpgradeRunUnitView]

        enum CodingKeys: String, CodingKey {
            case targetLabel = "target_label"
            case ruleSetId = "rule_set_id"
            case summary
            case counts
            case units
        }
    }

    struct VideoPlanView: Codable, Hashable, Sendable {
        var action: String
        var codec: String?
        var height: Int?
        var toneMap: Bool
        var bitrateCapBps: Int?
        var burnSubtitle: String?

        enum CodingKeys: String, CodingKey {
            case action
            case codec
            case height
            case toneMap = "tone_map"
            case bitrateCapBps = "bitrate_cap_bps"
            case burnSubtitle = "burn_subtitle"
        }
    }

    /// 前端 ``MediaCapabilities.decodingInfo()`` 的一项视频探测结果。
    struct VideoSupportIn: Codable, Hashable, Sendable {
        var codec: String
        var maxHeight: Int?
        var smooth: Bool?
        var powerEfficient: Bool?

        enum CodingKeys: String, CodingKey {
            case codec
            case maxHeight = "max_height"
            case smooth
            case powerEfficient = "power_efficient"
        }
    }

    /// 单元的洗版派生状态（docs/design/quality-upgrade.md §8.3/§9）。
    /// 只在"已入库且规则组配置了洗版目标"的单元上出现；标签由后端用统一的
    /// 档位阶梯生成，前端零拼接直接展示。
    struct WantedUpgradeView: Codable, Hashable, Sendable {
        /// 是否洗版中（可证明低于目标且未熔断）
        var active: Bool
        /// 当前版本档位标签（如「1080p WEB-DL」）
        var currentLabel: String
        /// 洗版目标档位标签（如「1080p Remux」）
        var targetLabel: String
        /// 已洗版搜索次数
        var searchAttempts: Int
        /// 无法确认档位：证明不了低于目标也证明不了已达标——不参与自动洗版，可手动选种替换（§13.8）
        var indeterminate: Bool

        enum CodingKeys: String, CodingKey {
            case active
            case currentLabel = "current_label"
            case targetLabel = "target_label"
            case searchAttempts = "search_attempts"
            case indeterminate
        }
    }

    struct WantedView: Codable, Hashable, Sendable {
        var id: Int
        var seasonNumber: Int
        var episodeNumber: Int
        var status: String
        var airDate: String?
        var priority: Int
        var infoHash: String?
        var nextSearchAt: String?
        var searchAttempts: Int
        var lastSearchAt: String?
        var releaseForecast: [String: API.JSONValue]?
        var resourceTiming: API.ResourceTimingView?
        var grabbedAt: String?
        var downloadedAt: String?
        var importedAt: String?
        var lastRejectReason: String?
        var grabTitle: String?
        var upgrade: API.WantedUpgradeView?

        enum CodingKeys: String, CodingKey {
            case id
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
            case status
            case airDate = "air_date"
            case priority
            case infoHash = "info_hash"
            case nextSearchAt = "next_search_at"
            case searchAttempts = "search_attempts"
            case lastSearchAt = "last_search_at"
            case releaseForecast = "release_forecast"
            case resourceTiming = "resource_timing"
            case grabbedAt = "grabbed_at"
            case downloadedAt = "downloaded_at"
            case importedAt = "imported_at"
            case lastRejectReason = "last_reject_reason"
            case grabTitle = "grab_title"
            case upgrade
        }
    }

    /// 对外端口的保存请求体（PUT /app/port）。
    struct WebPortPayload: Codable, Hashable, Sendable {
        /// 要监听的对外端口（1~65535）；null 或 0 = 清除设置、恢复默认
        var port: Int?

        enum CodingKeys: String, CodingKey {
            case port
        }
    }

    struct WebhookConfigPayload: Codable, Hashable, Sendable {
        var enabled: Bool?
        var endpoints: [API.WebhookEndpointPayload]?

        enum CodingKeys: String, CodingKey {
            case enabled
            case endpoints
        }
    }

    struct WebhookConfigView: Codable, Hashable, Sendable {
        var enabled: Bool
        var endpoints: [API.WebhookEndpointView]
        var catalog: [API.EventCatalogEntry]

        enum CodingKeys: String, CodingKey {
            case enabled
            case endpoints
            case catalog
        }
    }

    /// 保存配置时的单个 endpoint。``id`` 为空 = 新建（服务端生成 id 与 secret）。
    struct WebhookEndpointPayload: Codable, Hashable, Sendable {
        var id: String?
        var name: String?
        var url: String?
        var format: String?
        var enabled: Bool?
        var events: [String]?
        var template: String?
        var headers: [String: String]?
        var egressScope: String?

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case url
            case format
            case enabled
            case events
            case template
            case headers
            case egressScope = "egress_scope"
        }
    }

    struct WebhookEndpointView: Codable, Hashable, Sendable {
        var id: String
        var name: String
        var url: String
        var format: String
        var enabled: Bool
        var events: [String]
        var template: String
        var headers: [String: String]
        var egressScope: String
        var secretMasked: String
        /// secret 明文——仅在新建/轮换的响应里一次性返回，此后只给打码值
        var secret: String?
        var lastDelivery: API.DeliveryView?

        enum CodingKeys: String, CodingKey {
            case id
            case name
            case url
            case format
            case enabled
            case events
            case template
            case headers
            case egressScope = "egress_scope"
            case secretMasked = "secret_masked"
            case secret
            case lastDelivery = "last_delivery"
        }
    }

    /// 已绑定账号(绑定页列表项)。
    struct WeixinAccountView: Codable, Hashable, Sendable {
        var accountId: String
        var boundUserId: String?
        var status: String
        var running: Bool
        var lastError: String?
        var boundAt: String

        enum CodingKeys: String, CodingKey {
            case accountId = "account_id"
            case boundUserId = "bound_user_id"
            case status
            case running
            case lastError = "last_error"
            case boundAt = "bound_at"
        }
    }

    /// 发起绑定的返回:前端渲染二维码并开始 poll。
    struct WeixinBindingStartView: Codable, Hashable, Sendable {
        var challengeId: String
        var qrcodeUrl: String
        var qrcodeImage: String
        var message: String

        enum CodingKeys: String, CodingKey {
            case challengeId = "challenge_id"
            case qrcodeUrl = "qrcode_url"
            case qrcodeImage = "qrcode_image"
            case message
        }
    }

    /// 绑定状态快照(前端每 1-2 秒 poll 一次,后端只读内存,毫秒级返回)。
    /// status 取值:
    /// pending / scanned / need_verify_code / confirmed / already_bound /
    /// expired / failed。confirmed 时通道已启动,account 字段附带账号信息。
    struct WeixinBindingStatusView: Codable, Hashable, Sendable {
        var challengeId: String
        var status: String
        var message: String
        var qrcodeUrl: String
        var qrcodeImage: String
        var account: API.WeixinAccountView?

        enum CodingKeys: String, CodingKey {
            case challengeId = "challenge_id"
            case status
            case message
            case qrcodeUrl = "qrcode_url"
            case qrcodeImage = "qrcode_image"
            case account
        }
    }

    /// 提交手机微信上显示的配对数字。
    struct WeixinVerifyCodePayload: Codable, Hashable, Sendable {
        /// 配对码
        var code: String

        enum CodingKeys: String, CodingKey {
            case code
        }
    }

    /// 人工认领请求：把条目钉到指定 TMDB 条目并恢复后台入库作业。
    struct MovieclawApiApiRoutesImportWatchClaimPayload: Codable, Hashable, Sendable {
        /// TMDB 条目 id（类型按规则先验：库类型或规则声明）
        var tmdbId: Int
        /// 电影合集按文件认领：取 unresolved_files 里的一项；普通条目或只剩一个待认领文件时可省略
        var entryFile: String?

        enum CodingKeys: String, CodingKey {
            case tmdbId = "tmdb_id"
            case entryFile = "entry_file"
        }
    }

    /// 人工指定文件身份：把文件关联到 Discover 返回的影视条目。
    struct MovieclawApiSchemasLibraryClaimPayload: Codable, Hashable, Sendable {
        /// Discover 返回的 TMDB 影视条目稳定引用，如 tmdb:tv:1396
        var titleRef: String
        /// 季号；电影固定 0
        var seasonNumber: Int?
        /// 集号；电影固定 0
        var episodeNumber: Int?

        enum CodingKeys: String, CodingKey {
            case titleRef = "title_ref"
            case seasonNumber = "season_number"
            case episodeNumber = "episode_number"
        }
    }

}
