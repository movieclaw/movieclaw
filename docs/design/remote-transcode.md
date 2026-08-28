# 远程硬件转码 Worker

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
暂存在内存中，再以固定 `Content-Length` 上传；网络错误和临时 HTTP 错误最多自动重试
3 次。媒体产物不会写入 Worker 硬盘，NAS 仍会写入自己的转码缓存，这是在线播放
HLS 的必要缓存。

当前 Worker 使用 AppKit 菜单栏界面，最低支持 macOS 12，目标平台为 Apple Silicon。
后续 Linux、Windows 或其他硬件设备只需要实现同一控制协议、源文件读取、产物上传和
能力声明，不需要改变 NAS 的播放会话模型。

## 2. 配置与安全

远程转码配置的唯一来源是「系统 → 应用 → 远程转码」页面：

- 开关控制是否允许播放决策分配远程硬件任务；
- Worker Token 用于 WebSocket 控制面鉴权，服务端加密保存且不会回显；
- 专用外部访问地址可让 Worker 直连 NAS 的内网入口；留空时使用系统「网络与维护」
  中的外部访问地址；
- 单个 HLS 产物大小上限用于限制 NAS 接收的 init、playlist 和 fMP4 分片；默认且
  最大值为 512 MiB，服务端和 macOS Worker 使用同一上限。

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
3. Worker 声明了播放决策需要的编码后端和编码器。

当前服务端主要使用 `videotoolbox`/`h264_videotoolbox` 能力，因此现有可用 Worker
是 macOS Apple Silicon 实现。其他平台可以新增对应后端和编码器能力，不应仅凭平台名
推断硬件可用。

Worker 断线、接单超时、ffmpeg 失败或产物超时会使当前远程会话失败，并进入播放器已有
的重试或降档流程。远程失败时不会在同一会话中偷偷启动第二个本地写入者，避免两套
ffmpeg 同时写同一组 HLS 文件；没有可用远程 Worker 时，播放决策自然回到 NAS 本地
软件或硬件能力。

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

在「系统 → 应用 → 远程转码」中：

1. 打开远程硬件转码；
2. 设置一个高熵 Worker Token；
3. 按需要填写远程转码专用外部访问地址；
4. 保留或调整单个 HLS 产物大小上限。

专用地址为空时，系统使用「网络与维护」中的外部访问地址。三类请求会使用同一个
有效根地址：Worker 控制 WebSocket、源文件 Range 读取和 HLS 产物 PUT 上传。地址
必须是完整的 HTTP(S) 地址，不能包含用户名、密码、查询参数或片段。

使用 HTTPS 反向代理时，必须转发 WebSocket Upgrade，并确认 Worker 能够访问该地址。
使用内网 HTTP 时，请填写 NAS 实际映射端口，不要假定一定是 `3000`；端口变化后只需
更新页面和 Worker 配置。

## 5. 当前 macOS Worker

项目提供的实现位于 `macos/MovieClawTranscoder`，是 macOS Apple Silicon 菜单栏 App。
它会在启动时检查 Jellyfin-ffmpeg；缺少可用的 `h264_videotoolbox` 时不会登记为可
用硬件 Worker。首次没有可用版本时，用户确认后可以从 Jellyfin 官方 Release 下载
经过 digest 校验的 macOS arm64 portable 资产；菜单栏也提供后续更新入口。

```bash
cd macos/MovieClawTranscoder
scripts/package-app.sh
open "dist/MovieClaw Transcoder.app"
```

打开 App 后在「设置」中填写与服务端匹配的地址和 Token、Worker ID、最大并发数及
ffmpeg 路径。Token 保存在 macOS Keychain，其他非敏感配置保存在 UserDefaults。完整
安装、launchd、HTTP 内网和 Headless 流程见
[`macos/MovieClawTranscoder/README.md`](../../macos/MovieClawTranscoder/README.md)。

## 6. 已知限制与扩展方向

- Worker 注册表和播放会话目前是单进程内存状态；NAS 多副本需要共享任务租约和产物存储。
- 当前使用一个共享 Worker Token；多 Worker 精细撤销可升级为每 Worker 独立凭据或证书。
- 当前重点覆盖 H.264 VideoToolbox。HEVC、HDR tone-map、硬件解码和其他平台后端需要
  先完成能力声明、编码参数和样片矩阵验证。
- 标准视频、内嵌字幕和 HLS 网络输出保持无媒体临时文件路径；外部字幕硬烧或需要额外
  资源文件的复杂滤镜，暂不承诺 Worker 零媒体落盘。
- Worker 到 NAS 的 DNS、证书、MTU、Wi-Fi 稳定性会直接影响 Range 读取和 PUT 上传。
  上线前应验证断线、seek、暂停恢复、磁盘低水位和 NAS 重启。
