# MovieClaw Transcoder

MovieClaw Transcoder 是 MovieClaw 远程转码协议的当前 macOS Apple Silicon Worker
实现。它以菜单栏 App 运行，接收服务端下发的 ffmpeg 任务，从服务端签名地址读取源
视频，并把 HLS fMP4 产物上传回服务端。

远程转码协议本身不绑定 macOS，未来可以实现 Linux、Windows 或其他硬件平台的 Worker。
当前 App 最低支持 macOS 12，目标运行环境是 Apple Silicon Mac。

Worker 不挂载 NAS 文件系统，也不把源视频和 HLS 产物保存到 Mac 硬盘。ffmpeg 通过
HTTP(S) 读取源文件；上传代理只在本机回环地址监听，并将单个产物暂存在内存后上传。
App Support 仍会保存 ffmpeg 包、版本信息和普通配置，日志也会写入用户日志目录，
这些不属于媒体临时文件。

## 1. 构建和启动

在 Apple Silicon Mac 上执行：

```bash
cd macos/MovieClawTranscoder
chmod +x scripts/package-app.sh
scripts/package-app.sh
open "dist/MovieClaw Transcoder.app"
```

脚本会执行 Swift Release 构建、生成 App Bundle，并默认使用 ad-hoc 签名。正式分发时
可以通过 `MOVIECLAW_SIGNING_IDENTITY` 指定 Developer ID 签名身份，之后还需要完成
公证。这个变量只用于 App 打包签名，不是远程转码运行配置。

## 2. 服务端配置

先在 MovieClaw 网页打开「系统 → 应用 → 远程转码」：

1. 打开远程硬件转码；
2. 生成并保存一个高熵 Worker Token；
3. 确认系统外部访问地址，或为远程转码填写专用地址；
4. 按需要调整单个 HLS 产物大小上限。

远程转码专用地址留空时，Worker 使用系统「网络与维护」中的外部访问地址。该地址
必须是 Worker 实际能够访问的 HTTP(S) 地址，且不能包含用户名、密码、查询参数或片段。
不要从 Docker、Compose 或旧配置文件中设置远程转码环境变量，服务端只读取网页配置。

## 3. App 首次配置

打开菜单栏中的「设置」，填写：

- 服务端地址：填写上一步远程转码实际生效的地址；
- Worker Token：填写网页中保存的同一个 Token；
- Worker ID：当前机器的唯一标识，例如 `macmini-m1`；
- Jellyfin-ffmpeg 路径：可以使用 App 管理的版本，也可以指定已有的
  `jellyfin-ffmpeg`；
- 最大并发：按 Mac 的性能和 NAS 网络情况设置，当前 App 会限制在 1 到 4；
- 启动时自动连接：需要菜单栏 App 登录后自动登记 Worker 时打开。

Token 只保存到 macOS Keychain，其他非敏感配置保存到 UserDefaults。修改服务端 Token
后，必须在 App 中同步更新；服务端不会通过接口回传 Token 明文。

## 4. Jellyfin-ffmpeg

App 启动时会检查当前 ffmpeg 是否包含 `h264_videotoolbox`。缺少可用版本时，会询问
是否从 [Jellyfin 官方 Releases](https://github.com/jellyfin/jellyfin-ffmpeg/releases)
下载 macOS arm64 portable 版本：

- 确认后显示下载进度，并校验 Release API 提供的 SHA-256 digest、压缩包路径和编码器能力；
- 取消后菜单栏保留下载入口，安装成功后入口变为更新入口；
- 下载包临时保存在 App Support，完成后删除，不进入媒体目录；
- 更新托管版本前会先停止接收新任务，并等待当前任务完成；
- 更新失败不会替换现有可用版本；
- 已配置的自定义 ffmpeg 路径不会被自动覆盖。

如果官方 Release 没有唯一的 macOS arm64 资产或缺少可验证的 digest，App 会拒绝安装，
不会回退到第三方镜像或任意下载地址。

## 5. HTTPS 和可信内网 HTTP

公网或不可信网络必须使用 HTTPS。若服务端和 Mac 位于可信内网，可以在网页和 App 中
填写实际的内网 HTTP 地址，例如：

```text
http://10.1.1.254:实际映射端口
```

端口只是示例，必须替换成 NAS 当前 Nginx 的实际映射端口。HTTP 模式下控制面使用
`ws://`，源视频、HLS 产物、控制消息和临时 Token 都是明文；只允许防火墙放行给 Mac
Worker，绝不能把该端口发布到公网。App 会在保存 HTTP 地址时再次要求确认，不会因为
HTTPS 连接失败而自动降级到 HTTP。

## 6. launchd 开机自启

先在菜单栏 App 中保存一次配置，再复制并修改示例 plist 中的 App 可执行文件路径：

```bash
mkdir -p ~/Library/LaunchAgents
cp launchd/com.movieclaw.transcoder.plist.example \
  ~/Library/LaunchAgents/com.movieclaw.transcoder.plist
chmod 600 ~/Library/LaunchAgents/com.movieclaw.transcoder.plist
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.movieclaw.transcoder.plist
```

launchd 以当前用户启动 App，Token 会从该用户的 Keychain 读取；plist、脚本和日志中
都不保存 Token。更新 App 后可以先卸载旧任务，再重新 bootstrap：

```bash
launchctl bootout gui/$(id -u)/com.movieclaw.transcoder
```

## 7. Headless 排障模式

菜单栏 App 是默认部署方式。需要在没有图形界面的环境中排障时，可以使用同一个 Release
可执行文件的 Headless 模式。Headless 只接受显式命令行参数，不读取环境变量：

```bash
.build/release/movieclaw-transcoder \
  --headless \
  --nas-url https://nas.example.com \
  --token '<同一个 Worker Token>' \
  --ffmpeg /opt/homebrew/bin/jellyfin-ffmpeg \
  --worker-id macmini-m1 \
  --max-jobs 1
```

启动时会检查 `h264_videotoolbox`；能力不满足时不会向服务端登记。Headless 适合临时
验证连接和 ffmpeg，不建议把 Token 长期写入 shell 历史或启动脚本。

## 8. 运行状态和日志

菜单栏提供连接状态、Worker 能力、当前任务、转码进度、暂停接单、立即重连、ffmpeg
下载/更新、日志和脱敏诊断信息。日志文件位于：

```text
~/Library/Logs/MovieClawTranscoder.log
```

日志会在超过约 5 MiB 后轮转，并脱敏 Worker Token 和签名 URL 的 `token=` 查询参数。
遇到播放卡住或 Worker 闪退时，优先查看：

1. App 菜单栏连接状态是否为「已连接」或「转码中」；
2. 服务端「远程转码」页面是否显示配置就绪；
3. 服务端地址的 WebSocket Upgrade、Range 读取和 PUT 上传是否都能从 Mac 访问；
4. App 日志中的 ffmpeg 退出原因、产物上传中断和 NAS 断线信息。

服务端会在 Worker 断线或任务失败后释放远程会话，并走播放器已有的重试或降档路径。
