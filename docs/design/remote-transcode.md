# 远程硬件转码 Worker

> **Worker 的认证方式已改由 `docs/design/device-auth.md` 定义**：
> 共享 `worker_token` 与粘贴式配对码（`PairingCode.swift`）整体删除，
> 改为「填地址 → 验证连接 → 网页批准」的设备授权流程，令牌逐设备签发、
> 带 `transcode` scope、可单独吊销。本文其余部分（转码链路本身）不受影响。

> 状态：远程 Worker 协议、网页配置和播放接入已实现；当前随项目提供的 Worker
> 实现是 macOS Apple Silicon 菜单栏 App，其他平台可以复用协议扩展。

## 1. 能力边界

远程转码用于把硬件编码任务交给另一台设备执行。NAS 仍负责鉴权、播放决策、VOD
时间轴、HLS 缓存和浏览器取流；Worker 负责执行 ffmpeg，并声明自己支持的平台、
编码后端、编码器和并发能力。

```text
浏览器 ──播放 API/分片──> NAS
                         ├─ HTTP(S) Range 源文件 ──> Remote Worker
                         ├─ HTTP(S) PUT HLS 产物 <── Remote Worker
                         └─ WebSocket 控制/心跳 <──> Remote Worker
```

Worker 不需要挂载 NAS 文件系统，也不需要把源视频保存到本地。ffmpeg 直接读取带签名
的源地址，并把 HLS 产物上传回 NAS。Worker 内置的上传代理只监听回环地址，单个产物
暂存在内存中，再以固定 `Content-Length` 上传；网络错误和临时 HTTP 错误最多尝试
5 次（退避 0.5 / 1 / 2 / 4 秒，约 7.5 秒，能熬过一次 Wi-Fi 重连）。重试不阻塞
ffmpeg——它交完请求体就走，不等响应。媒体产物不会写入 Worker 硬盘，NAS 仍会写入
自己的转码缓存，这是在线播放 HLS 的必要缓存。

**ffmpeg 不看 HTTP 输出的响应码**（jellyfin-ffmpeg 7.1 / 8.1 实测：分片全被 404，
退出码仍是 0、stderr 为空），所以产物丢了只能由上传代理主动报：重试用尽的产物用
`job.artifact_failed` 告诉 NAS，由它按补片台账从这一片重启（有次数上限）；文件名
不在白名单的产物说明两端版本不一致，代理当场叫停任务，终态带着原因回 NAS
（issue #444）。

同一个任务的所有产物共用一条 HTTP 连接（keep-alive）。分片按秒级节奏产出，逐个新建
连接会让每次上传都重做 TCP 与 TLS 握手，而这些握手的往返恰好都落在起播和 seek 这些
最怕延迟的时刻。

**观众播放位置（`job.playback`，只为 Worker 面板显示）。** NAS 每 3 秒给任务所在的
Worker 推一条 `{"type": "job.playback", "job_id", "position_ms", "viewer_paused",
"duration_ms", "prepared_ms"}`（毫秒，片内时间；拿不到的字段省略）。观众位置取播放器
自己的进度上报（活动页「正在播放」同一份数据），**不用最近请求的分片**——那是播放器的
下载位置，会比画面快几十秒；上报约 10 秒一次，没暂停时按实时外推（最多 30 秒）。
`duration_ms` / `prepared_ms` 只有 VOD 会话（有预生成分片计划）才有。只发给在 hello
的 `capabilities` 里声明了 `"playback_progress": true` 的 Worker：旧版不认识这条消息，
会每条记一行「忽略未知控制消息」。

Worker 上报 `job.progress` 的 `out_time_ms` 是真正的毫秒。注意 ffmpeg `-progress` 输出
里同名的 `out_time_ms` 单位其实是**微秒**（历史遗留），Worker 读的时候已换算。

VOD 会话的 `live.m3u8` 是 ffmpeg 的内部进度列表，服务端对远程会话并不解析它（分片是否
就绪以产物文件本身为准），因此 Worker 只保留最后一份、在任务收尾时补传一次备诊断，
不再每写一个分片就回传一遍。非 VOD 会话的 `index.m3u8` 要直接发给浏览器、服务端起播
时还会阻塞等它出现，仍然实时上传。

当前 Worker 使用 AppKit 菜单栏界面，最低支持 macOS 12，目标平台为 Apple Silicon。
后续 Linux、Windows 或其他硬件设备只需要实现同一控制协议、源文件读取、产物上传和
能力声明，不需要改变 NAS 的播放会话模型。

## 2. 配置与安全

远程转码配置的唯一来源是「系统 → 应用 → 远程转码」页面，页面上要人做的决定
只有一个开关：

- 开关控制是否允许播放决策分配远程硬件任务；
- 凭证不在这一页：Worker 的令牌是逐台设备配对签发的，在「设置 → 设备」审批与
  吊销（见 `device-auth.md` §5.4）；
- 取源与回传的根地址**默认自动推断**，见下节；
- 单个 HLS 产物大小上限固定 512 MiB，不再暴露给用户：它是 Worker 内存上传代理
  的实现上限，只能调低，而调低只会让上传撞 413。

### 2.1 取源与回传地址：默认自动

服务端下发任务时要告诉 Worker 两件事：去哪儿 Range 读源视频、往哪儿 PUT HLS
产物。只有两层：

1. 页面「高级」里的覆盖地址，通常为空；
2. 为空时，用**接单的这台 Worker 自己连上来的地址**
   （`WorkerConnection.observed_base_url`）。

第 2 层是默认路径，也是绝大多数部署的实际路径。Worker 的控制 WebSocket 本来
就是从某个地址打进来的，那个地址必然是这台 Worker 够得着的——比任何人工填写
都可靠，所以地址不再是启用远程转码的前置条件。推断在
`routes/transcode_worker.py::_observed_base_url` 完成：scheme 优先信
`X-Forwarded-Proto`（反向代理终止 TLS 时，到应用的是 ws 而 Worker 用的是 wss），
host 取 `Host` 头，末尾接上 `root_path`。

**不回退到系统外部访问地址。** 那个地址回答的是「用户从外面怎么访问这个应用」，
常常是公网域名或反向代理；下发给一台明明在同一局域网、刚从内网地址连进来的
Worker，会把大量分片绕出去再绕回来。旧版正因如此才需要再填一个「专用地址」把它
扳回内网——那个输入框解决的问题是上一层自己制造的，所以这一层被整个去掉了。
同理也不能用**播放请求**的 Host 头推导：那是浏览器够得着的地址，和 Worker
够得着的地址不是一回事。

只有代理把 `Host` 改写成上游地址（如 `127.0.0.1:8000`）时推断才会失真，那正是
覆盖项存在的意义。

地址逐 Worker 独立：两台 Worker 从不同入口连进来时，各自用各自的地址；下发前
先在注册表里占住槽位（`RemoteWorkerRegistry.reserve`）再拼 URL，保证「按谁的
地址拼，就发给谁」。

远程转码不再读取环境变量配置。已经部署的实例应在网页中保存配置，避免 Docker、
Compose 和应用数据库出现两套事实源。

控制面使用 `/api/v1/transcode-worker/ws`，只发送任务参数、停止命令和状态；源文件
与产物 URL 使用带过期时间的签名 token。源 token 限定 session、file 和 kind，产物
token 额外限定当前 job/attempt，旧 seek 轮次不能覆盖新轮次的文件。

HTTPS 是公网和不可信网络的默认选择。可信内网可以在远程转码页填写实际可达的
`http://` 地址，控制面会使用 `ws://`；此时源视频、HLS 产物、控制消息和临时 token
均为明文，端口必须通过防火墙限制为内网或指定 Worker，不能暴露到公网。

## 3. 调度与失败语义

只有以下条件同时满足时，播放决策才会选择远程 Worker：

1. 网页配置已启用且 Token、有效地址均已配置；
2. 在线 Worker 仍有可用并发槽位；
3. Worker 声明了播放决策需要的编码后端和编码器；
4. Worker 声明了任务要产出的分片类型（`segment_types`，取值同 ffmpeg
   `-hls_segment_type`）。没声明的旧版 Worker 只当它会 `fmp4`：它的上传代理只放行
   `.m4s`，派给它的 TS 任务会被悄悄丢掉（issue #444，见 jellyfin-transcode.md §4）。
   Worker 上传代理与 NAS 产物端点的文件名白名单必须放行同一批名字，由 NAS 侧测试
   从 Swift 源码读出来核对。

当前服务端主要使用 `videotoolbox`/`h264_videotoolbox` 能力，因此现有可用 Worker
是 macOS Apple Silicon 实现。其他平台可以新增对应后端和编码器能力，不应仅凭平台名
推断硬件可用。

Worker 每 15 秒发一次心跳，服务端超过 45 秒没收到任何消息就判定该 Worker 离线、不再
派单。Worker 侧用同一个 45 秒窗口反向判定：超时没收到 NAS 的任何响应（含心跳 ack）
就主动断开重连，避免半开连接下服务端已判离线、Worker 却以为自己在线而迟迟不重连。
握手成功过的连接掉线后，重连退避从头开始计时；始终连不上才继续指数退避。

Worker 断线、接单超时、ffmpeg 失败或产物超时会使当前远程会话失败，并进入播放器已有
的重试或降档流程。远程失败时不会在同一会话中偷偷启动第二个本地写入者，避免两套
ffmpeg 同时写同一组 HLS 文件；没有可用远程 Worker 时，播放决策自然回到 NAS 本地
软件或硬件能力。

**正常结束（`job.finished`）不是失败**：转到片尾后回拖到起转点之前，或任务被提前收尾，
缺的分片与本地 ffmpeg 退出同理——从缺口重新下发一轮，不把会话判死。写者从某段起转
却没产出它就退出（源比台账时长短、片尾孤儿分片），同一段最多再重试一次，之后本会话内
直接 404，避免无限重下发（本地会话同一规则，此前 30 秒等待里会拉起约 300 个进程）。

**取源连接要能续读**：ffmpeg 的 HTTP 输入默认不重连，连接中途断开会被当成读到片尾、
退出码 0，后面的分片永远不来（实测 40 秒片源在 6 MB 处掐断只产出 4/10 个分片）。最常见
的断开来自领先量节流——job 被挂起超过容器内 nginx 的 `send_timeout`（600 秒）。远程命令
因此带 `-reconnect 1 -reconnect_on_network_error 1 -reconnect_delay_max 15`，按断点发
Range 续读，只重试网络错误、不重试 HTTP 4xx。

**Worker 有任务时阻止空闲睡眠与 App Nap**（`ProcessInfo.beginActivity(.userInitiated)`）：
空闲睡眠只看键鼠，不看 CPU，默认电源设置下没人碰的 Mac 十来分钟就会睡过去，控制连接
随之中断、会话失败；菜单栏 App 没有可见窗口，也是 App Nap 的目标，心跳被拖慢就会被判
离线。任务全部结束即归还。

产物上传的终态会保留在播放诊断中：Worker 上报 ffmpeg 退出码和 stderr 尾部，NAS
写缓存遇到 `ENOSPC`/`EDQUOT` 返回 507（空间或配额不足），不会伪装成会话 404。
Worker 在 ffmpeg 退出后还会等待已接收的最后一个产物上传完成，再发送任务终态，避免
`init.mp4` 或最后分片仍在上传时被代理取消。

每次 seek 或质量切换都会使用新的 job/attempt 和产物 token。停止或 seek 重启前会
先恢复暂停状态，再发送 stop，避免暂停的 Worker 无法及时退出。NAS 仍沿用现有会话
目录、配额、Range 取流、VOD playlist 和启动清理机制。

Worker 不自行决定转码质量参数，`ffmpeg_args` 由 NAS 的统一命令装配器下发。当前
1080p 目标使用 `h264_videotoolbox`、High@4.1、`yuv420p`，码率上限 6M、缓冲区
12M；软件回退使用 `libx264`、`superfast`、CRF 21、同样的 H.264 兼容格式。

## 4. NAS 部署

在「系统 → 应用 → 远程转码」中，只需要打开远程硬件转码开关。地址和令牌都不用
配：令牌在「设置 → 设备」批准 Worker 时签发，取源与回传地址按 §2.1 自动推断。

使用 HTTPS 反向代理时，必须转发 WebSocket Upgrade，并保留 `Host`
（`proxy_set_header Host $http_host;`）与 `X-Forwarded-Proto`——项目自带的
`docker/nginx.conf.template` 已经这样配。代理如果把 `Host` 改写成上游地址，自动
推断会失真，这时才需要在「高级」里填覆盖地址；它必须是完整的 HTTP(S) 地址，
不能包含用户名、密码、查询参数或片段。

## 5. 当前 macOS Worker

项目提供的实现位于 `macos/MovieClawTranscoder`，是 macOS Apple Silicon 菜单栏 App。
它会在启动时检查 Jellyfin-ffmpeg；缺少可用的 `h264_videotoolbox` 时不会登记为可
用硬件 Worker。首次没有可用版本时，用户确认后可以从 Jellyfin 官方 Release 下载
经过 digest 校验的 macOS arm64 portable 资产；菜单栏也提供后续更新入口。

```bash
cd macos/MovieClawTranscoder
scripts/package-app.sh
open "dist/MovieClaw 转码器.app"
```

打开 App 后在「设置」中填写与服务端匹配的地址和 Token、Worker ID、最大并发数及
ffmpeg 路径。Token 保存在 macOS Keychain，其他非敏感配置保存在 UserDefaults。完整
安装、开机自启动、HTTP 内网和 Headless 流程见
[`macos/MovieClawTranscoder/README.md`](../../macos/MovieClawTranscoder/README.md)。

### 5.1 Worker 容错

转码器是常驻服务，目标是「出了事自己爬起来；爬不起来，就把原因和下一步摆在面板上」。
分四层，每层只兜自己这一层。阈值都是纯逻辑（`FaultTolerance.swift`），有单测钉住边界。

**进程层：菜单栏 App + 转码内核两个进程。** 连 NAS、管 ffmpeg 的 WorkerClient 跑在同一个
可执行文件以 `--core` 启动的子进程里（`CoreRunner`），菜单栏 App 里的 `CoreSupervisor`
看管它，内核出任何事菜单栏 App 都不受影响：

- 崩溃（被信号杀掉、非约定的退出码）按 1、2、4…秒退避重启，最长 60 秒；10 分钟内第 5 次
  崩溃就停手，面板给「重试」——那多半是环境坏了，无限重启只会刷屏、烧 CPU。
- 卡死：内核每 10 秒报一次平安，这条消息要先经过 WorkerClient actor，actor 被同步调用
  堵死就报不出来；35 秒收不到内核任何消息即 SIGKILL，按崩溃处理。
- 内存失控：内核 footprint 超过 1 GB 且手上没有任务时重启一次（不计入崩溃）。
- 按设计退出不重启：配置无效（退出码 64）、ffmpeg 不可用（65），重启多少次都一样。
- 通道：内核 stdin 收指令、stdout 发事件，一行一个 JSON；令牌走管道，不进命令行参数和
  环境变量。界面进程一退出管道就断，内核读到 EOF 自行收尾，不会留下没人管的内核。

App 本身的崩溃由登录项兜：App 在 `~/Library/LaunchAgents` 放一份 launchd 配置，
`KeepAlive.SuccessfulExit=false`，意外退出后 launchd 重新拉起（`ThrottleInterval` 10 秒）。
用户点「退出」是退出码 0，不拉起；防多开的实例也以 0 退出，不会被当成崩溃反复拉。

- **不用 SMAppService.agent**：它按注册时的 cdhash 把任务钉死（launchd 的 LWCR），App 是
  ad-hoc 签名、每次构建 cdhash 都变，更新后 launchd 以 EX_CONFIG 拒绝拉起新版本，开机就
  不再自启；重新注册要等系统后台处理完新版本（实测半分钟以上）才生效。传统 LaunchAgent
  没有这层约束（macOS 27 实测）。有了 Developer ID 签名后可以换回来。
- **手动打开时交班**：只有 launchd 拉起的进程受 KeepAlive 保护。手动打开的实例（更新后
  重新打开之类）在读到连接密钥之后、启动内核之前，请 launchd 按配置另起一个
  （`launchctl kickstart`），等它出现就退出；launchd 那个遇到正在交班的手动实例，会等它
  退出再接班，而不是按防多开直接退出。靠 `XPC_SERVICE_NAME` 区分两者：launchd 设成配置
  的 Label，手动打开的是 `application.<bundle id>.…`。5 秒内没等到就自己接着跑。
  交班放在读到密钥之后，是因为 ad-hoc 签名每次更新都要在钥匙串里重新授权：授权弹窗得留在
  用户亲手打开、正在最前面的实例里，launchd 在后台拉起的实例不一定能把模态弹窗摆到眼前
  （macOS 14 起激活要「协商」）；选了「始终允许」后接班的实例不会再问。
- 配置里写的是可执行文件的绝对路径，App 挪了位置下次打开时改写并重新装载。

**任务层（内核内）。**

- 卡死看门狗：起转 90 秒没有第一条进度、或之后 60 秒没有新进度，强制结束 ffmpeg，
  `job.failed` 带上原因，NAS 照常重试或降档。被 NAS 暂停（`job.pause`）的任务不计时。
- 连续失败熔断：5 分钟内 3 个任务都在 20 秒内失败，说明这台 Mac 出了问题——发
  `worker.draining` 暂停接单，跑一遍 ffmpeg 能力探测；通过则 10 分钟后发 `worker.ready`
  恢复，不通过就以 65 退出、面板提示换 ffmpeg。刚起转就被叫停（拖进度条）不计数；
  转完或跑满 20 秒的任务说明 ffmpeg 是好的，清零。
- 孤儿 ffmpeg：内核崩溃时它起的 ffmpeg 会被 launchd 收养继续跑（被暂停的永远挂着）。
  新内核启动时清理，三个条件同时满足才杀：父进程是 1、可执行文件就是配置的 ffmpeg、
  参数里有 `/transcode-worker/` 取源地址和 `-progress pipe:1`——用户自己跑的 ffmpeg 不误杀。

**连接层（内核内）。**

- 握手看门狗：发起连接 20 秒内没收到 `worker.accepted` 就断开重连。心跳与 45 秒静默
  检测（§3）都在握手之后才开始；服务端握手一成功就发关闭帧时，URLSession 的 send /
  receive 会一直挂着（Python websockets 库实测），没有这一层内核会永远停在「正在连接」。
- 睡眠唤醒、网络恢复（NWPathMonitor）：退避中立刻重连；连着的发一个心跳，5 秒没有
  回应就断开重连——睡眠后 TCP 多半已死，等 45 秒的静默检测 NAS 早判离线了。
- NAS 拒绝按关闭理由分三类：凭证失效每 5 分钟重试一次（不停下：NAS 从备份恢复后凭证
  可能重新有效；重新配对后内核带新凭证重启，不用等）；远程转码没开每分钟一次；其他
  （协议版本不一致、HTTP 403 / 404）每分钟一次，原因原样摆出来。
- **服务端拒绝必须先 accept 再 1008 关闭**。accept 之前 close，uvicorn 按 ASGI 规范只回
  一个空包体的 HTTP 403，理由整句丢失，Worker 分不清凭证失效还是开关没开（Starlette 的
  TestClient 不模拟这一点，测试直接检查 ASGI 消息顺序）。旧版服务端就是这样，Worker 把
  裸 403 翻成「请确认开关已打开；已打开则重新配对」。

**面板。** 需要用户知道的故障（`WorkerProblem`）每种一张卡片：发生了什么、App 在做
什么、用户要不要做点什么；普通的断线重连不单独出卡片。面板底部显示「24 小时内自动恢复过
N 次」和最近一次的原因。

### 5.2 原盘与各种片源格式

Mac 能硬解的编码、带的 Metal 滤镜因机器而异（AV1 要 M3 起），NAS 探测不到，只能信
Worker 在 hello 的 `capabilities` 里的申报：

| 字段 | 含义 | 没申报（旧版 Worker）时 |
|---|---|---|
| `disc_sources` | 能读原盘的 ffconcat 清单 | 原盘任务不派给它 |
| `hw_decoders` | VideoToolbox 能硬解的片源编码（ffmpeg 编码名），Worker 用 `VTIsHardwareDecodeSupported` 逐个实测 | 只按 `h264`、`hevc` 算 |
| `filters` | ffmpeg 带的 Metal 滤镜（`scale_vt`、`tonemap_videotoolbox`……） | 当它一个没有 |

**原盘。** NAS 本机读盘用 concat 清单（`disc-playback.md` §3.4），远程 Worker 读的是
同一份剪辑序列与 IN/OUT，只是每段换成 HTTP 地址：源地址是
`/transcode-worker/sessions/{id}/source.ffconcat`，清单里每段写相对地址
`clips/{i}?token=…`（按清单自己的地址解析，反向代理子路径也对得上），令牌沿用同一个。
每段还逐个带上 `option rw_timeout / reconnect…`——命令行上的续读参数只作用于清单这一个
输入，管不到清单里各段剪辑自己的 HTTP 连接。原盘任务只派给申报了 `disc_sources` 的
Worker。

**命令的三种形态**（`ffmpeg_args._videotoolbox_mode`）：

1. **GPU 全链路**：片源编码在 `hw_decoders` 里、`scale_vt` 与（HDR 时）
   `tonemap_videotoolbox` 都有、链上没有只有软件做得了的步骤（烧录、BT.2020 SDR 的色彩
   空间转换）。硬解帧不下载回内存：`scale_vt` 缩放 →（HDR）`tonemap_videotoolbox`
   → `h264_videotoolbox`。杜比视界由 `apply_dovi` 按元数据还原（DV Profile 5 也不偏色，
   CPU 那条链做不到）。
2. **CPU 软解**：片源编码不在 `hw_decoders` 里（VC-1、WMV、RealVideo、VP6，这台 Mac 上
   还有 MPEG-2）。不发 `-hwaccel`，CPU 解码 + 软件滤镜，编码仍用 VideoToolbox。
3. **原来的装法**：其余情况（烧录、没申报滤镜的旧版 Worker）——硬解后下载回内存走
   软件滤镜。

**实测踩过的 ffmpeg 坑**（jellyfin-ffmpeg 8.1，macOS 27）：

- 解不了的编码硬要硬件帧（`-hwaccel_output_format videotoolbox_vld`）：硬解初始化失败
  后退回软解，软件帧喂给 `hwdownload` 以 -22 失败（VC-1 原盘实测）。所以要逐编码判断。
- ffmpeg 8 的 `-colorspace bt709` 参与格式协商：无色彩标签的硬件帧会被自动插一个接不上
  的软件 scale 去转换，整条链失败。GPU 链路在 `scale_vt` 后用 `setparams` 给帧打上
  BT.709 标签。
- `tonemap_videotoolbox` 只收 10-bit：8-bit HLG（广电 4K 节目）报
  「Unsupported input format depth: 8」。HDR 缩放时一律 `format=p010le`。
- `h264_videotoolbox` 写 A53 隐藏字幕进 SEI 时出错（MPEG-2 源常带），一律 `-a53cc 0`。

实测速度（M 系列 Mac，经 NAS HTTP 取源，1080p 输出，VOD 模式整条命令）：4K HDR10 原盘
5.5×、多剪辑 4K HDR10 原盘 5.7×、杜比视界 P5 4.3×、8-bit HLG 5.8×、VP9 4K 6.0×、
H.264 原盘 7.8×、VC-1 原盘 5.4×、MPEG-2 原盘 3.6×、WMV 约 20×、RealVideo 6.3×。
CPU 版色调映射（tonemapx）在同一台 Mac 上是 2.6× 且占满 4 个核，NAS 上连 1× 都不到。

**降档底线**：硬件档在执行时落空（Worker 刚断开、或它接不了这个任务）时，决策输入里的
`hardware_available` 仍为 True，`_judge_video` 那道「HDR 要显卡」的闸拦不住。网页端
（`_resolve_tier`）与 Jellyfin 端（转码会话规格）都在降到软件档时补了同一条：HDR 直接
拒绝并提示，不让 NAS 用 CPU 做 4K 色调映射（真机：首帧 12.6 秒、33 秒卡 3 次）。

## 6. 已知限制与扩展方向

- Worker 注册表和播放会话目前是单进程内存状态；NAS 多副本需要共享任务租约和产物存储。
- 当前使用一个共享 Worker Token；多 Worker 精细撤销可升级为每 Worker 独立凭据或证书。
- 输出只有 H.264（8-bit、BT.709）。HEVC 输出、HDR 直通输出和其他平台后端需要先完成
  能力声明、编码参数和样片矩阵验证。
- 原盘只支持 BDMV 目录；ISO（蓝光与 DVD）、DVD 目录（VIDEO_TS）在 NAS 本机也还不能播
  （`disc-playback.md` §2），另起任务。
- Jellyfin 协议（Infuse）按码率转码时原盘仍直接拒绝、走原画：PlaybackInfo 对原盘不做
  码率协商，要改协商与会话规格两处。
- 隔行片源（1080i 蓝光、DVD）不做反交错；Worker 已申报 `yadif/bwdif_videotoolbox`，
  缺的是探测层记录场序。
- 标准视频、内嵌字幕和 HLS 网络输出保持无媒体临时文件路径；外部字幕硬烧或需要额外
  资源文件的复杂滤镜，暂不承诺 Worker 零媒体落盘。
- Worker 到 NAS 的 DNS、证书、MTU、Wi-Fi 稳定性会直接影响 Range 读取和 PUT 上传。
  上线前应验证断线、seek、暂停恢复、磁盘低水位和 NAS 重启。
- **控制连接断开仍会让会话失败**：取源续读与上传重试能熬过短暂的数据面抖动，但
  WebSocket 一断，NAS 就把在跑的任务判失败（`_mark_jobs_lost`）。Worker 通常一两秒
  就重连回来，理论上可以对新连接重新下发（产物 token 按 attempt 隔离，旧任务的迟到
  上传会被拒，不会两路同写），但要和网页播放器自己的「掉线自愈」、Infuse 的 404
  处理一起设计，暂未做。
