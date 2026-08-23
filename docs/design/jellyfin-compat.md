# Jellyfin 兼容播放接口：调研与设计

> 状态：调研定稿 **v1.1**（2026-08-03），待实施。
> v1（2026-08-03）：三轮源码级调研定稿。同日 v1.1：三轮**对抗式逐条复核**
> （每条断言要求 file:line 证据、以"设法推翻"为目标），修正约 50 处——其中
> 五处为会直接导致实现错误的翻案（§6.1 DeviceProfile、§6.4 stream 鉴权、
> §7 落库阈值、§7 play_count 时机、§5.5 Latest 聚合），并补齐 legacy 路由
> 缺口与恒输出字段清单。**本文档即实现契约**：无法抓包对照真实客户端时，
> 以本文（及标注的源码位置）为唯一裁判。
> 源起：让 Infuse / Fileball / VidHub / SenPlayer 等第三方播放器**以 Jellyfin
> 服务器的身份**直接连接 movieclaw——浏览媒体库、直连播放、同步观看进度，
> 播放器侧零改动。
> 协议依据：Jellyfin 服务端源码（`jellyfin/jellyfin` @ `33a8cdf`，12.0.0 dev）。
> 我们对外报 10.10.x 版本号，凡 12.0 与 10.10 有已知差异处均已标注。
> 关联文档：[library.md](library.md)（媒体库架构）、[metadata.md](metadata.md)
> （元数据自足）、[strm-workflow.md](strm-workflow.md)（网盘 strm 工作流）。

## 0. 定位与硬决策

**为什么选 Jellyfin 协议**（对比 Emby / Plex / WebDAV，2026-08-03 用户决策）：

- Jellyfin 全开源 + OpenAPI 完整，每个字段语义可对源码核实；Emby 4.x 闭源只能
  抓包逆向，Plex 认证/发现绑定官方云无法自托管模仿；
- 主流播放器对 Jellyfin 是一等公民支持，覆盖面不损失；
- WebDAV 虽最简单，但播放器会自行重新刮削，movieclaw 的元数据、选图、
  观看状态全部作废，与做这件事的初衷相悖。

**三条硬边界**（本设计的第一性，实现时不可突破）：

1. **最小可用子集，不是复刻**。只实现 Infuse 类播放器实际会调的接口；
   WebSocket、SyncPlay、QuickConnect、DLNA、LiveTV、转码 HLS 一概不做。
2. **不转码**。所有 MediaSource 声明 `SupportsDirectPlay=true,
   SupportsTranscoding=false`——这等价于"用户无转码权限的 Jellyfin"这一
   **协议内合法状态**（Jellyfin 在用户缺 `EnableVideoPlaybackTranscoding` 等
   权限时自己就这么置，`MediaInfoHelper.cs:288-303`）。全解码播放器（Infuse
   等）永远走直连；解码能力不足的客户端（网页端、Chromecast）不在支持范围，
   播放失败是预期行为。
3. **网盘 strm 不代理**。strm 条目直接把云端 URL 交给播放器（见 6.4），
   服务器零流量——这是相对真 Jellyfin（12.0 服务端对远程源做反向代理）的
   **有意偏离**，与 strm-workflow.md 的"零网盘流量"原则一致。社区已有
   MediaWarp / embyExternalUrl / SmartStrm 等项目在 Emby/Jellyfin 上大规模
   使用 302 直链配合 Infuse，可行性经过验证。

**有意偏离清单**（模仿不是复刻，所有偏离集中声明，实现与评审对照用）：
① 不转码（上）；② strm 直连/302 不代理（上）；③ `/Videos/*/stream` 与字幕
接口**要求 token**（真 Jellyfin 匿名，见 6.4）；④ 发现与 `LocalAddress` 的
地址策略（见 3.1/3.2）；⑤ QuickConnect/Enabled 恒 `false`（真默认 true）；
⑥ Latest 聚合简化为两态（见 5.5）；⑦ strm 条目的 `Container` 从 URL 猜而非
Jellyfin 的 `"strm"` 字面量、`ETag` 省略（见 6.4）；⑧ 未知 `parentId` 返回
404（Jellyfin 是 400，见 5.2）；⑨ `stream` 无 `static=true` 时返回 400
（Jellyfin 会走 ffmpeg 转码）。
（原偏离⑩"图片原图直出"已于 2026-08-03 撤销：库封面拼贴引入 Pillow 后，
`maxWidth/maxHeight/width/height/fillWidth/fillHeight` 已按 fit-within
等比缩小实现，变体缓存于 data/cache/jellyfin-images。）

## 1. 协议总览与全局约定

Jellyfin 对外协议 = **HTTP REST（JSON）+ UDP 发现**。一次完整播放会话：

```
UDP 7359 发现（可选）
  → GET /System/Info/Public          确认服务器身份
  → POST /Users/AuthenticateByName   换 AccessToken
  → GET /UserViews                   库列表
  → GET /Items?parentId=...          浏览/搜索
  → POST /Items/{id}/PlaybackInfo    播放协商（拿 MediaSources）
  → GET /Videos/{id}/stream          HTTP Range 直连取流
  → POST /Sessions/Playing[/Progress/Stopped]   进度回报
```

以下全局约定**每个接口都必须遵守**（源码依据：`Jellyfin.Extensions/Json/
JsonDefaults.cs`、`Json/Converters/JsonGuidConverter.cs`、
`ApiServiceCollectionExtensions.cs:137-156`）：

| 约定 | 内容 |
|---|---|
| 字段命名 | 响应 **PascalCase**（`RunTimeTicks`）；**请求体字段名大小写不敏感**（MVC `JsonSerializerDefaults.Web` 的 `PropertyNameCaseInsensitive=true` 未被覆盖） |
| null 处理 | HTTP 管道内 **CLR null 字段不出现**（`WhenWritingNull`）；非可空值类型恒输出。两个例外：UDP 发现响应不走该配置（`EndpointAddress: null` 必然出现）；`Guid?` 为 `Guid.Empty` 时 converter 写**显式 null**（我们直接不输出空 Guid 字段即可，客户端两种都吃） |
| 枚举 | 序列化为**枚举成员名字符串**（`"Movie"`、`"Descending"`、`"movies"`——大小写随成员声明） |
| GUID 出参 | **N 格式：32 位无横线小写 hex**；全部 ID 字段一致 |
| GUID 入参 | 宽松：`Guid.Parse` 语义，N/D/B/P/X 五种格式都收；JSON `null` 按 `Guid.Empty` 处理不报错 |
| 数字入参 | `NumberHandling=AllowReadingFromString`：`{"PositionTicks": "123"}` 字符串数字必须能解析 |
| 时间量 | **ticks = 100 纳秒**；秒 × 10⁷ = ticks（`RunTimeTicks`/`PositionTicks` 同单位） |
| 日期 | 恒输出 ISO8601 UTC 7 位小数：`"2010-07-15T00:00:00.0000000Z"`（Jellyfin 仅毫秒为 0 时保证此格式，我们统一收敛，属安全超集） |
| 错误响应 | **四种形态**，见下表 |
| 路由大小写 | ASP.NET 路由**大小写不敏感**（Starlette 敏感——需归一化中间件，见 9.3） |
| 未知字段 | 请求体反序列化**静默忽略未知字段**（客户端会发 `EventName` 等） |
| 未知枚举值 | 逗号/管道分隔列表里无法解析的值**静默丢弃**，不报 400 |
| 列表分隔符 | 数组参数逗号分隔；**例外**：`genres`/`studios`/`tags`/`officialRatings` 用 `\|`（`PipeDelimitedCollectionModelBinder`；注意 `studioIds`/`genreIds` 官方注释写 pipe 实际是 comma——别抄注释） |

**错误响应四形态**（照抄哪种取决于错误来源，客户端基本不解析 body，但
状态码必须准确）：

| 形态 | 场景 | 响应 |
|---|---|---|
| 业务异常 | 参数缺失(400)/登录失败(401)/权限(403)/不存在(404) | `text/plain`，body 恒 `Error processing request.`（ExceptionMiddleware） |
| 模型校验失败 | 缺 `[Required]` 参数、路由 Guid 解析失败、空 body | **400 + `application/problem+json`**（ASP.NET 默认 ValidationProblemDetails，camelCase） |
| 控制器主动 NotFound("文案") | 如 `"User not found"`、`"Series not found"` | 404 + **JSON 字符串 body**（`application/json`，带引号） |
| 认证管道 | 无 token/token 无效 → 401；授权不过 → 403 | **空 body** |

我们实现时收敛为：认证管道用空 body 401/403；其余按最接近的形态给，
状态码语义不变即可。

## 2. 最小接口清单

按实施优先级分三档。P0 缺一不可（登录→浏览→播放→进度闭环）；P1 影响体验；
P2 兜底兼容。**凡列出 legacy 别名的都必须与新路由一起实现**——我们对外报
10.10 版本号，老客户端（含相当一部分 Infuse 版本）走的就是 legacy 路由。

**P0 必做**

| 接口 | 说明 |
|---|---|
| UDP 7359 | 局域网自动发现应答 |
| `GET /System/Info/Public`、`GET\|POST /System/Ping` | 匿名，服务器身份 |
| `POST /Users/AuthenticateByName` | 登录换 token（匿名） |
| `GET /Users/Me`、`GET /Users/{userId}`、`GET /Users/Public` | 用户信息/续验 token（Public 匿名） |
| `GET /UserViews` + legacy `GET /Users/{userId}/Views` | 库视图列表（**不补 legacy 则老客户端库列表直接空**） |
| `GET /Items`（含 `?searchTerm`）+ legacy `GET /Users/{userId}/Items` | 核心查询 |
| `GET /Persons`、`GET /Persons/{name}` | 人物查询。**Infuse 的搜索页是 `/Items` + `/Persons` 两路并发**，缺这一路会让它把整次搜索判为失败、结果页全空（2026-08-22 抓包实证，见 11 节考古） |
| `GET /Items/{itemId}` + legacy `GET /Users/{userId}/Items/{itemId}` | 单条目详情（**全字段语义，无 fields 参数**，见 5.3） |
| `GET /Shows/{seriesId}/Seasons`、`GET /Shows/{seriesId}/Episodes` | 剧集结构 |
| `GET\|HEAD /Items/{itemId}/Images/{type}[/{index}]` | 海报/背景/缩略图 |
| `GET\|POST /Items/{itemId}/PlaybackInfo` | 播放协商（GET 版一行转调 POST，个别客户端先打 GET 探测） |
| `GET\|HEAD /Videos/{itemId}/stream[.{container}]` | 取流（Range/206；strm→302） |
| `POST /Sessions/Playing` `/Progress` `/Stopped` `/Ping` | 进度回报（全部 204；Ping 的 `playSessionId` 是 Required query） |
| `POST\|DELETE /UserPlayedItems/{itemId}` + legacy `POST\|DELETE /Users/{userId}/PlayedItems/{itemId}` | 标记已看/未看（**200 + UserItemDataDto**，非 204） |
| `POST /Sessions/Capabilities` 与 `/Capabilities/Full` | **204 空实现**（官方 SDK 客户端登录后立刻调，404 视为致命——必须在 P0） |

**P1 建议**

| 接口 | 说明 |
|---|---|
| `GET /UserItems/Resume` + legacy `GET /Users/{userId}/Items/Resume` | 继续观看（Infuse 用 legacy 路由） |
| `GET /Shows/NextUp` | 追剧"下一集" |
| `GET /Items/Latest` + legacy `GET /Users/{userId}/Items/Latest` | 最新入库（返回**扁平数组**非 QueryResult） |
| `GET /Videos/{itemId}/{msId}/Subtitles/{idx}/{ticks}/Stream.{fmt}`（及不带 ticks 变体） | 外挂字幕 |
| `POST\|DELETE /UserFavoriteItems/{itemId}` + legacy `POST\|DELETE /Users/{userId}/FavoriteItems/{itemId}` | 收藏（**200 + UserItemDataDto**，与已看接口完全同构，复用 handler 骨架） |
| `GET /Branding/Configuration`、`GET /QuickConnect/Enabled` | 匿名轻量 JSON（固定值） |
| `GET /Branding/Css` + `/Branding/Css.css` | 匿名，**`text/css` 裸文本**（空串即可），非 JSON |
| `GET /Sessions` → `[]`、`DELETE /Videos/ActiveEncodings` → 204、`GET /System/Endpoint` → `{"IsLocal":true,"IsInNetwork":true}` | 敷衍实现防客户端卡启动/退出（Fileball/VidHub 退出时会无条件发 ActiveEncodings 清理） |
| `GET /Search/Hints` | 搜索建议（Jellyfin 官方 Web/Android 的搜索框走它；Infuse 不用）。输出是扁平的 `{SearchHints, TotalRecordCount}`，不是 QueryResult |
| `GET\|HEAD /Items/{itemId}/Download` 与 `/Items/{itemId}/File` | 整文件下载。Policy 宣告了 `EnableContentDownloading:true`，VidHub 等客户端的下载按钮打 Download，缺失则下载 404 无法开始。本地文件回 FileResponse（Download 带 attachment 文件名，File 不带，均支持 Range 断点续传）；strm 偏离真 Jellyfin（后者回 .strm 文本）：302 到云端直链 |

**P2 兜底**：legacy `POST /PlayingItems/{itemId}`、`POST /PlayingItems/{itemId}/Progress`、
**`DELETE /PlayingItems/{itemId}`**（停止是 DELETE 不是 POST）及 `/Users/{userId}/PlayingItems/*`
变体（参数走 query，均 204）；`GET /Items/Filters2`（部分客户端进库时无条件请求，
返回空结构 `{"Genres":[],"Tags":[]}` 兜底）与 `/Items/Filters`；`GET /Items/Root` +
legacy；`GET /Items/{itemId}/Similar`（返回空 QueryResult 比 404 干净）；
`GET /System/Info`（复用
Public 字段）；`/emby/*` 路径前缀别名（**非 Jellyfin 行为**——12.0 源码不存在
该前缀，纯为个别客户端探测兜底）。

**明确不做**：`/socket`（WebSocket）、SyncPlay、QuickConnect 授权流程（Enabled
恒返回 false）、转码（`/master.m3u8` 等 404 即可）、LiveTV/Channels、DLNA、
插件/仪表盘全家桶、`/Sessions/{id}/Viewing`（遥控指令）、Trickplay 接口与
`/LiveStreams/Open|Close`（我们的 DTO 永不输出 `Trickplay`/`PartCount` 字段、
`RequiresOpening` 恒 false，客户端不会调它们——前提见 5.3 的"绝不输出"清单）。

## 3. 发现与握手

### 3.1 UDP 自动发现（AutoDiscoveryHost.cs:52-119）

监听 **UDP 7359**；收到含 `"who is JellyfinServer?"`（大小写不敏感，Contains
匹配）的报文时，**单播回源地址**一个 JSON（字段就这四个，序列化不走 HTTP
管道的 options，`EndpointAddress` 为 null 也会输出）：

```json
{"Address": "http://192.168.1.10:8096", "Id": "<32位hex服务器ID>",
 "Name": "MovieClaw", "EndpointAddress": null}
```

实现为 asyncio DatagramProtocol 后台任务，随 lifespan 启停；端口被占/无权限时
写中文警告日志并跳过（发现失败不影响手动填地址）。控制台的兼容层开关对齐
Jellyfin 的 `AutoDiscovery` 配置语义（默认 true，关闭时不监听）。

**`Address` 必须是客户端真实可达的地址**。真 Jellyfin 的逻辑
（`ApplicationHost.cs:930-941`）：`PublishedServerUrl` 非空则原样返回，否则按
来源地址选 bind address；取不到地址时**不应答**。我们跑在容器里情况更复杂，
采用**语义等价但更保守的三层策略**（非逐字照抄）：

1. **显式配置优先**：新增设置 `published_server_url`（控制台网络设置页，
   等价 Jellyfin 的 "Published Server URL"）。配置了就原样返回。这是
   **Docker 桥接部署的唯一可靠答案**——容器内看到的 IP（172.x）和端口都
   可能与宿主映射不一致，任何容器内自动探测都无法得知宿主的真实映射。
2. **自动探测兜底**（未配置时）：对发现报文的来源地址做一次 UDP
   `connect()`（不发包），读 socket 本地地址——内核路由表选出的、面向该
   客户端的出口 IP。host 网络模式或裸机部署下，这就是正确的局域网 IP；
   端口用应用实际监听端口。
3. **探测结果自检**：探测出的 IP 落在 Docker 默认网段（172.17-31.x）且未
   配置 published_server_url 时，**仍然应答**（有意偏离 Jellyfin 的"取不到
   就沉默"——宁可给个可能错的地址让用户看到问题，也不无声失败），同时写
   中文警告日志引导用户去控制台配置真实地址。

Docker 桥接模式的现实要在部署文档写明：局域网广播报文通常不会穿过
docker-proxy 到达容器，**桥接下自动发现大概率整体不可用**（不只是地址不
准）。要用自动发现：host 网络模式（推荐，与 Jellyfin 官方对 DLNA/发现的
建议一致）或 macvlan；否则播放器里手动填地址即可，发现只是锦上添花。

### 3.2 `GET /System/Info/Public`（匿名）

```json
{"LocalAddress": "http://192.168.1.10:8096", "ServerName": "MovieClaw",
 "Version": "10.10.7", "ProductName": "Jellyfin Server",
 "OperatingSystem": "", "Id": "<服务器ID，首启生成并持久化的32位hex>",
 "StartupWizardCompleted": true}
```

- `LocalAddress`：配置了 `published_server_url` 就用它；否则**回显本次请求
  的 scheme + Host 头**——客户端既然能发起这个请求，该地址必然可达。
  **有意偏离**：真 Jellyfin 默认走 `GetSmartApiUrl`（`EnablePublishedServerUriByRequest`
  默认 false，且开启后 Host 反而优先于 PublishedServerUrl），我们的选择在
  容器部署下更可靠；
- `Version` **报真实存在的 Jellyfin 版本号**（10.10.x），命中客户端兼容分支；
- `StartupWizardCompleted` 必须 `true`（声明是 `bool?` 但真实现恒赋值），
  否则客户端进首次配置流程；
- `ProductName` 保持 `"Jellyfin Server"`（客户端以此识别服务器类型）；
- `OperatingSystem` 是 obsolete 字段但恒序列化，给 `""`。

`GET|POST /System/Ping`：返回**产品名**固定 `"Jellyfin Server"`——注意是
**含双引号的 JSON 字符串**，`Content-Type: application/json; charset=utf-8`
（`[Produces]` 排除了纯文本 formatter），**不是服务器名也不是裸文本**
（SystemController.cs:102-106 返回 `_appHost.Name` = ApplicationProductName）。

## 4. 认证

### 4.1 Authorization 头解析（AuthorizationContext.cs:229-317）

Scheme 为 `MediaBrowser`（大小写不敏感）：

```
Authorization: MediaBrowser Client="Infuse", Device="Apple TV", DeviceId="xxx", Version="8.2", Token="<hex>"
```

解析规则（**不是简单 split，需状态机**，照抄以下语义）：

- 头值里没有空格 → 整个头作废（返回无认证信息）；scheme 取第一个空格前部分；
- 引号内的逗号不是分隔符（`x="123,123"` → 值 `123,123`）；
- 键 `.Trim()` 去空白；值先 `Trim('"')` 再 **URL 解码**；值不要求带引号；
  空值片段（`X=,`）直接丢弃；
- 键名**大小写敏感**（字典无 comparer），精确五个：`Client` / `Device` /
  `DeviceId` / `Version` / `Token`；
- 多个 `Authorization` 头只取第一个。

token 的全部合法位置（**我们全部无条件支持**）：`Authorization` 头 `Token=`、
旧头 `X-Emby-Authorization`（同格式，scheme 可为 `Emby`）、`X-Emby-Token`、
`X-MediaBrowser-Token`、query `?ApiKey=`、query `?api_key=`。
版本注记：12.0 里除 `Authorization` 头与 `?ApiKey=` 外，其余四种全部被
`EnableLegacyAuthorization`（默认 **false**）门控；**10.10 无此开关、全部
无条件生效**——我们面向 10.10 时代客户端，必须全支持。

### 4.2 `POST /Users/AuthenticateByName`（匿名）

请求体只有两个字段（**没有 `Password`**，键名大小写不敏感）：

```json
{"Username": "admin", "Pw": "明文密码"}
```

- 前置校验：`Client`/`Device`/`DeviceId`/`Version` 四键必须都能从
  Authorization 头解析出来（Jellyfin 校验顺序 Client→DeviceId→Device→
  Version），缺任一 → **400 text/plain**；
- 空 body / 非法 JSON → **400 `application/problem+json`**（模型校验形态）；
- 密码错 → **401 text/plain**（body `Error processing request.`）；
- Jellyfin 还有一族 **403**（账号 disabled、非本地网络且无 EnableRemoteAccess、
  设备被禁、超 MaxActiveSessions）——我们单管理员场景只需 401，但不要把
  403 语义挪作他用。

响应 `AuthenticationResult`（就四个字段）：

```json
{"User": {<UserDto>}, "SessionInfo": {<SessionInfoDto>},
 "AccessToken": "<32位无横线hex，随机生成>", "ServerId": "<服务器ID>"}
```

**重要副作用（照抄，安全相关）**：同 `(userId, deviceId)` 重复登录时，Jellyfin
会**吊销旧 token 再发新 token**（SessionManager.cs:1712-1735）。
`jellyfin_device` 表必须按 `device_id` 唯一约束覆盖写入，否则客户端反复
重装/重登录会无限累积长期有效 token。

**UserDto 最小合法形态**（省略 = null 不输出；所有给出的键都是"非可空或
带非 null 默认值、真 Jellyfin 必然输出"的字段）：

```json
{"Name": "admin", "ServerId": "<服务器ID>",
 "Id": "<用户GUID(N)>", "HasPassword": true,
 "HasConfiguredPassword": true, "HasConfiguredEasyPassword": false,
 "EnableAutoLogin": false,
 "Configuration": {
   "PlayDefaultAudioTrack": true, "SubtitleLanguagePreference": "",
   "DisplayMissingEpisodes": false, "GroupedFolders": [],
   "SubtitleMode": "Default", "DisplayCollectionsView": false,
   "EnableLocalPassword": false, "OrderedViews": [],
   "LatestItemsExcludes": [], "MyMediaExcludes": [],
   "HidePlayedInLatest": true, "RememberAudioSelections": true,
   "RememberSubtitleSelections": true, "EnableNextEpisodeAutoPlay": true},
 "Policy": {
   "IsAdministrator": true, "IsHidden": false, "IsDisabled": false,
   "BlockedTags": [], "AllowedTags": [],
   "EnableUserPreferenceAccess": true, "AccessSchedules": [],
   "BlockUnratedItems": [],
   "EnableRemoteControlOfOtherUsers": false,
   "EnableSharedDeviceControl": true, "EnableRemoteAccess": true,
   "EnableLiveTvManagement": true, "EnableLiveTvAccess": true,
   "EnableMediaPlayback": true,
   "EnableAudioPlaybackTranscoding": true,
   "EnableVideoPlaybackTranscoding": true,
   "EnablePlaybackRemuxing": true,
   "ForceRemoteSourceTranscoding": false,
   "EnableContentDeletion": false, "EnableContentDeletionFromFolders": [],
   "EnableContentDownloading": true,
   "EnableSyncTranscoding": true, "EnableMediaConversion": true,
   "EnabledDevices": [], "EnableAllDevices": true,
   "EnabledChannels": [], "EnableAllChannels": true,
   "EnabledFolders": [], "EnableAllFolders": true,
   "InvalidLoginAttemptCount": 0, "LoginAttemptsBeforeLockout": -1,
   "MaxActiveSessions": 0, "EnablePublicSharing": true,
   "BlockedMediaFolders": [], "BlockedChannels": [],
   "RemoteClientBitrateLimit": 0,
   "AuthenticationProviderId": "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider",
   "PasswordResetProviderId": "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider",
   "SyncPlayAccess": "CreateAndJoinGroups",
   "EnableCollectionManagement": false,
   "EnableSubtitleManagement": false, "EnableLyricManagement": false}}
```

要点：

- **`ForceRemoteSourceTranscoding` 必须显式 `false`**——置 true 会逼客户端
  对远程源（strm）走转码，直接击穿硬边界 2/3；
- **`IsHidden` 必须 `false`**（Jellyfin 构造默认是 true）：`/Users/Public`
  按 hidden 过滤，true 会让电视端登录页拿到空列表；
- 两个 ProviderId 带 `[Required(AllowEmptyStrings=false)]`，必须非空；
- ~~CastReceiverId~~ 不要输出 null 键（可空字段，省略即可）。

`SessionInfoDto` 最小集：`Id`（32 hex 会话标识）、
`UserName/Client/DeviceId/DeviceName/ApplicationVersion/ServerId`，外加九个
非可空必出现字段：`UserId`（Guid）、`PlayableMediaTypes: []`、
`SupportedCommands: []`、`LastActivityDate`、`LastPlaybackCheckIn`、
`IsActive: true`、`SupportsMediaControl: false`、
`SupportsRemoteControl: false`、`HasCustomDeviceName: false`。其余全部可空省略。

### 4.3 movieclaw 侧账号模型

movieclaw 是单管理员账号（`AdminAccountSetting`）。映射：

- Jellyfin"用户" = 这一个管理员账号，用户 GUID 用固定编码（见 8.1）；
- `AuthenticateByName` 校验直接复用 `auth_service.authenticate`；
- **新表 `jellyfin_device`**：每个播放器设备一行，`device_id` 唯一约束，
  重登录覆盖并换发 token（见 4.2 副作用）：
  `(token唯一, client, device_name, device_id唯一, version, last_seen_at)`。
  与 Web 控制台的会话 token 体系**分开**——播放器 token 长期有效，控制台
  token 有过期策略，混用会互相伤害；
- `GET /Users/Public` 返回 `[UserDto]`（单元素数组）；`GET /Users/Me` 与
  `GET /Users/{userId}` 返回同一 UserDto（Me 在 token 无对应用户时 400；
  {userId} 不存在时 404 + JSON 字符串 `"User not found"`）。

## 5. 媒体库浏览

### 5.1 `GET /UserViews` + legacy `GET /Users/{userId}/Views`

每个启用的 movieclaw 库 → 一个视图。两条路由同一实现（legacy 的 userId 在
route）。**此接口无 fields 参数，是全字段语义**（`new DtoOptions()` =
allFields），我们没有的字段靠 null 省略天然合法。响应 `QueryResult`
（`Items`/`TotalRecordCount`/`StartIndex` 三字段恒出现，下同）：

```json
{"Id": "<库GUID>", "Name": "电影", "ServerId": "...",
 "Type": "CollectionFolder", "CollectionType": "movies",
 "IsFolder": true, "ImageTags": {}, "BackdropImageTags": [],
 "UserData": {<默认值即可，见下>}}
```

- `CollectionType`：**小写**，电影库 `"movies"`、剧集库 `"tvshows"`
  （枚举成员名字面即小写，对应 `Library.kind`）；
- `Type` 恒 `"CollectionFolder"`（真 Jellyfin 仅在库分组/presetViews 场景
  才返回 `"UserView"`，我们不实现分组）；
- **库视图的 UserData 用默认值**（position=0, played=false, …）：
  `CollectionFolder.SupportsPlayedStatus => false`，真 Jellyfin 不为库视图
  算未看数/百分比聚合——不要在这里做 5.4 的文件夹聚合；
- 库封面（`ImageTags.Primary`）可后续增强，空对象合法。

### 5.2 `GET /Items` + legacy `GET /Users/{userId}/Items` —— 核心查询

必须支持的参数（约 90 个参数里的关键子集，其余接受但忽略）：

| 参数 | 语义 |
|---|---|
| `userId` | **可省，缺省 = token 对应用户**（源码回落 claims；"缺失→400"实践中是死代码）。传了但非本人且非管理员 → 403。我们单用户直接忽略该参数即可 |
| `parentId` | 缺省 = 根（返回视图列表级）；库 GUID → 库内容；剧 GUID → 季；季 GUID → 集。**未知 id：Jellyfin 抛 400（ArgumentException）**，我们魔数不匹配返 404（偏离⑧，客户端影响可忽略） |
| `includeItemTypes` / `excludeItemTypes` | `Movie` `Series` `Season` `Episode`（大小写不敏感解析，逗号分隔） |
| `recursive` | 默认 false；**特例照抄**（ItemsController.cs:335-340）：parentId 是库（ICollectionFolder）且 `includeItemTypes` 非空且客户端未显式传 recursive 时自动 true——否则 Infuse 在剧集库拿到空列表 |
| `startIndex` / `limit` | 分页；`TotalRecordCount` = 过滤后总数 |
| `sortBy` / `sortOrder` | 至少支持 `SortName` `Name` `DateCreated` `PremiereDate` `ProductionYear` `CommunityRating` `Runtime` `Random` `DatePlayed` `AiredEpisodeOrder` `ParentIndexNumber,IndexNumber`；`Ascending`/`Descending` 按位配对 |
| `fields` | 字段门控，见 5.3 |
| `filters` | `IsPlayed` `IsUnplayed` `IsResumable` `IsFavorite` |
| `searchTerm` | 逐字对齐 `BaseItemRepository.TranslateQuery.cs:178`：`CleanName.Contains(cleanedTerm) \|\| OriginalTitle LIKE %term%`，其中 `cleanedTerm`/`CleanName` 都过 `GetCleanValue()`（去变音符 → 小写 → 标点转空格 → 折叠空白）。实现见 `movieclaw_jellyfin/search.py`，口径单测在 `tests/jellyfin/test_search_matching.py` |
| `genres` `years` `officialRatings` | 筛选；genres/officialRatings 用 `\|` 分隔，years 逗号 |
| `ids` | 批量取条目（逗号分隔，非法项静默丢弃） |
| `isPlayed` / `isFavorite` | 与 filters 等价的另一入口 |
| `enableUserData` `enableImages` `imageTypeLimit` `enableImageTypes` `enableTotalRecordCount` | 输出控制（enableImages/enableTotalRecordCount 默认 true） |

排序需要 SortName 语义：首期直接用 title 的 NOCASE 排序（不引入拼音库）。

### 5.3 BaseItemDto 字段映射

**无条件输出**（不受 fields 门控；"无条件"指不看 fields，可空字段值为 null
时仍省略）：`Id` `Name` `ServerId` `Type` `MediaType`（非可空恒输出：
Movie/Episode=`"Video"`，Series/Season=`"Unknown"`）`IndexNumber`
`ParentIndexNumber` `PremiereDate` `ProductionYear` `RunTimeTicks`
`OfficialRating` `CommunityRating`（**仅 >0 输出**）`CollectionType`
`UserData`（受 enableUserData 控制）`IsFolder`（bool?，对我们四类型都输出），
以及剧集族的 `SeriesId` `SeasonId` `SeriesName` `SeasonName`
`SeriesPrimaryImageTag`。

**受图片选项控制**（enableImages=false 或对应 imageTypeLimit=0 时整体省略）：
`ImageTags` `BackdropImageTags` `ParentPrimaryImageItemId/Tag`
`ParentBackdropItemId/ImageTags`（集/季无自有图时继承季/剧海报——Infuse 靠
这个显示卡片，必须实现继承）。`PrimaryImageAspectRatio` 受 fields 门控，
但 Episode/Season 自身无 Primary 图时有无条件旁路（取剧海报比例）——照抄。

**fields 门控**（传了才输出）：`Overview` `Genres` `People` `MediaSources`
`MediaStreams` `Path` `DateCreated` **`ParentId`**（陷阱：不传 fields=ParentId
就不输出，而 SeriesId/SeasonId 是无条件的）`Studios` `ProviderIds` `Taglines`
`OriginalTitle` `ChildCount` `RecursiveItemCount`。

**绝不输出清单**（协议合法且是"明确不做"的前提）：`PartCount`（否则客户端
调 /AdditionalParts）、`Trickplay`（否则调 Trickplay 接口）。
（`People` 原在此清单，2026-08-03 按用户反馈改为正式支持：受 fields 门控
输出 BaseItemPerson（Name/Id/Role/Type/PrimaryImageTag，演员按主次序在前、
导演随后，数据源 media_item_person ⋈ person）；配套人物头像接口
（TMDB profile 经图片代理缓存直出）、人物条目 GUID（类型 0x06）与
`/Items?personIds=` 反查参演作品。）

**单条目接口特例**：`GET /Items/{itemId}` 与 legacy **没有 fields 参数、
全字段语义**（含 Overview/MediaSources/MediaStreams/Path/ParentId/Studios/
ProviderIds/ChildCount…全开）——实现必须单独走"全开"分支，否则 Infuse
详情页缺简介/媒体信息。`itemId` 全零 GUID → 根文件夹；不存在 → 404 空 body。

四种类型 ↔ movieclaw 数据源：

| Jellyfin 类型 | 数据源 | 关键字段来源 |
|---|---|---|
| `Movie` | `media_item(kind=movie)` + 其 `library_file` 行 | 标题/年份←media_item；简介/类型/评分/分级/时长←media_metadata；RunTimeTicks←file.duration_seconds×10⁷（缺则 metadata.runtime_minutes×60×10⁷）；多版本文件→多 MediaSources |
| `Series` | `media_item(kind=tv)` | `Status`：TMDB status 映射 `Returning Series→"Continuing"`、`Ended/Canceled→"Ended"`；ChildCount=有文件的季数 |
| `Season` | `media_season` | IndexNumber=season_number（0=Specials）；SeriesId/SeriesName 无条件输出 |
| `Episode` | `media_episode` ⋈ `library_file`（按 (item,season,episode) 数字对） | IndexNumber=集号、ParentIndexNumber=季号（Infuse 强依赖）；PremiereDate←air_date；名称←episode.name |

**只输出"有文件"的内容**：季/集列表以 `library_file` 存在为准（缺集不虚构
Missing 条目，`DisplayMissingEpisodes=false` 与之呼应，Jellyfin 源码同款
判定）；`missing_since` 非空或 `media_item_id` 为 NULL 的文件行不进任何列表。

### 5.4 UserData 与观看状态

每个条目都带 `UserData`（enableUserData=false 除外）：

```json
{"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": false,
 "Played": false, "Key": "<稳定字符串>", "ItemId": "<条目GUID(N)>",
 "PlayedPercentage": 43.5, "LastPlayedDate": "...", "UnplayedItemCount": 3}
```

- 非可空恒输出六个：`PlaybackPositionTicks` `PlayCount` `IsFavorite` `Played`
  `Key`（required）`ItemId`（非可空 Guid，恒输出）；
- **`Key` 是不透明稳定字符串**，客户端不解析。真 Jellyfin 默认是
  `Guid.ToString()` 的 **D 格式**（带横线），剧集族是 provider key + 季集号。
  我们用条目 GUID 字符串即可，格式不限但必须稳定非空；
- 叶子条目（Movie/Episode）：`PlayedPercentage` = position/runtime×100
  （**0-100**），仅结果 >0 时输出；
- **文件夹条目（Series/Season）走另一套公式**（Folder.FillUserDataDtoValues，
  不调 base）：`UnplayedItemCount` = 总数−已看（**无条件输出，含 0**）；
  总数 >0 时 `PlayedPercentage` = 已看/总×100（**playedCount=0 时输出 0，
  没有">0 才输出"规则**）、`Played` = 已看 ≥ 总数；**总数为 0 时
  `Played=true`、PlayedPercentage 不输出**；
- 库视图不做聚合（见 5.1）。

**新表 `playback_state`**：`(media_item_id, season_number, episode_number)` 唯一
（电影 (0,0) 哨兵，与 wanted/library_file 同约定），列：**`position_ms`
（毫秒——领域层不用 Jellyfin 的 ticks 方言，换算在协议边界做，见 8.5）**、
`played`、`play_count`、`last_played_at`、`is_favorite`（收藏 P1 顺手做）。
单用户不带 user 维度（真要多用户时加列即可向前兼容）。

### 5.5 剧集接口、继续观看、首页

- `GET /Shows/{seriesId}/Seasons`：无分页；`isSpecialSeason=false` 滤掉 0 季；
  seriesId 无效/非剧集 → **404 空 body**（NotFound() 无文案）。
- `GET /Shows/{seriesId}/Episodes`：`seasonId` 优先于 `season` 号（且该分支
  不校验 seriesId）；都不传 = 全剧集数（按季集序）；`sortBy` 是**单值**参数
  且仅 `Random` 生效；`TotalRecordCount` 是分页前总数；404 有三种 text 文案
  （`"No season exists with Id ..."`/`"Series not found"`）。
- `GET /UserItems/Resume` + legacy：服务端强制语义（控制器根本没有
  sortBy/recursive/filters 参数，客户端不能覆盖）——
  **`IsResumable = 播放位置 > 0`**（源码无 `NOT played` 条件；我们在
  标记已看时清零 position，语义即对齐），按 `last_played_at` 降序，
  Recursive，IsVirtualItem=false。支持 `mediaTypes`/`limit`/`startIndex`。
- `GET /Shows/NextUp`：候选 = 有任意一集 `LastPlayedDate` 非空的剧，按各剧
  最后观看时间降序；每剧取"最后已看集之后的下一集"（已看判定排除 0 季）。
  `enableResumable` 默认 **true**——看了一半的那集本身会出现在 NextUp。
  版本注记：12.0 的 `DisplaySpecialsWithinSeasons`（默认 true）会让带
  AirsBefore/After 排期的特别篇插入候选；我们不产出该元数据，等效跳过 0 季。
- `GET /Items/Latest` + legacy：按 `library_file.created_at` 降序；
  **返回扁平 `[BaseItemDto]`**，不是 QueryResult；`limit` 默认 20，
  `groupItems` 默认 true。真 Jellyfin 12.0 按 24 小时窗口聚合，可能返回
  **Episode / Season / Series 三种 Type 混合**（1 集→Episode；同季多集的
  单季剧→Series；同季多集的多季剧→Season；跨季→Series），聚合项带
  `ChildCount`=新增数。**我们简化为两态**（偏离⑥）：单集→Episode、
  同剧多集→Series + `ChildCount`，客户端均能正常显示。

### 5.6 图片

路由（每条都有配对的 `[HttpHead]`）：
`GET|HEAD /Items/{itemId}/Images/{type}` 与 `.../{type}/{index}`（index 也可走
`?imageIndex=`；另有一条含 tag/format 的长位置参数路由，P2 可忽略）。
Backdrop 数组下标即 index，本设计每条目至多 1 张背景，只需支持 index=0。

| Jellyfin 图 | movieclaw 资产 |
|---|---|
| 库视图 `Primary` | **服务端渲染的「氛围光货架」拼贴**（`services/library/cover.py`：该库最近入库 4 部作品海报，复刻控制台 LibraryCover 构图——21:10 画布、首图重模糊氛围光、圆角海报排 + 倒影 + 地面光斑；素材指纹做 key，内容变化自动重渲；控制台 `/api/v1/libraries/{id}/cover` 与本接口吐**同一张图**，前端媒体库页也直接 `<img>` 引用替代客户端 CSS 合成） |
| Movie/Series `Primary` | `media_metadata.poster_file` |
| Movie/Series `Backdrop/0` | `media_metadata.backdrop_file` |
| Season `Primary` | `media_season.poster_file`（无 → 404，客户端自动退剧海报） |
| Episode `Primary` | `media_episode.still_file` |
| `Logo` / `Thumb` / `Banner` | 无资产，404（合法降级） |

- 缩放参数：`maxWidth/maxHeight/quality` 按需缩放（产物落 `data/` 缓存目录）；
  **`fillWidth/fillHeight` 也要接受**（Infuse 常带，按 max 语义处理即可）；
  `format` 参数接受但可忽略；
- `tag` **纯缓存语义**——服务端不校验、不参与选图，仅：有 tag → 回显进
  `ETag`（**值带双引号**）+ `Cache-Control: public, max-age=31536000,
  immutable`；无 tag → 仅 `Cache-Control: public`；请求头带
  `Cache-Control: no-cache` → 改发 no-store 组合并跳过 304 逻辑；
- tag 生成：`md5(资产相对路径 + 文件mtime)`，图变则 tag 变即可；
- 304 条件两种都支持：`If-None-Match`（带/不带引号都接受）与
  `If-Modified-Since`；304 空 body；
- 404 两种：条目不存在 → 空 body；条目在但无该类型图 → text 文案
  （`"{Name} does not have an image of type {type}"`）；
- DTO 侧：`ImageTags: {"Primary": "<tag>"}`、`BackdropImageTags: ["<tag>"]`，
  有资产才给键（客户端只对有 tag 的类型发请求）。

## 6. 播放链路

### 6.1 `GET|POST /Items/{itemId}/PlaybackInfo`

- POST body 可为空（`EmptyBodyBehavior.Allow`），同名 query 参数优先于 body；
  GET 版只有 `userId` 参数，转调同一实现；
- **我们不解析也不缓存 `DeviceProfile`**，永远返回未经设备适配的
  MediaSources。**准确的协议事实**（v1.1 修正）：真 Jellyfin 并非"profile
  为 null 就跳过适配"——body 无 profile 时它会从
  `_deviceManager.GetCapabilities(deviceId)` 回退取 `/Sessions/Capabilities/
  Full` 上报过的缓存 profile（MediaInfoController.cs:137-147）。我们对
  Capabilities 恒 204 且**不存储**，因此永远无 profile 可回退，结果等价于
  "无转码权限的 Jellyfin"（MediaInfoHelper.cs:288-303），协议合法。
  连带结论：真 Jellyfin 在无 profile 时也**不输出**字幕的
  `DeliveryMethod/DeliveryUrl`——我们无条件输出是让外挂字幕可用的必要
  超集（见 6.3）；
- 入参处理：`MediaSourceId` 筛选单版本（**OrdinalIgnoreCase 匹配**）；
  **`LiveStreamId` 必须显式忽略**（Jellyfin 里它会短路整个源解析，我们
  不能把它当 mediaSourceId 用）；其余参数接受并忽略；
- 响应：

```json
{"MediaSources": [<MediaSourceInfo>...],
 "PlaySessionId": "<每次请求新生成的32位hex>"}
```

有可播源时**不得**输出 `ErrorCode`；无源时 `MediaSources: []` +
`ErrorCode: "NoCompatibleStream"`（此时不给 PlaySessionId）。

### 6.2 MediaSourceInfo（每个 library_file 一个）

本地文件版本（**所有键都经 v1.1 逐字段核实为"真 Jellyfin 必然输出"**，
包括 8 个容易漏的非可空默认字段）：

```json
{"Protocol": "File", "Id": "<文件GUID(N)>",
 "Path": "/media/movies/Inception (2010)/xxx.mkv",
 "Type": "Default", "Container": "mkv", "Size": 1234567890,
 "Name": "2160p HEVC", "IsRemote": false,
 "ETag": "<md5(mtime.ticks)>", "RunTimeTicks": 88800000000,
 "ReadAtNativeFramerate": false, "IgnoreDts": false, "IgnoreIndex": false,
 "GenPtsInput": false, "SupportsTranscoding": false,
 "SupportsDirectStream": true, "SupportsDirectPlay": true,
 "IsInfiniteStream": false, "UseMostCompatibleTranscodingProfile": false,
 "RequiresOpening": false, "RequiresClosing": false, "RequiresLooping": false,
 "SupportsProbing": false, "VideoType": "VideoFile",
 "MediaStreams": [<见6.3>], "MediaAttachments": [], "Formats": [],
 "Bitrate": 25000000, "RequiredHttpHeaders": {},
 "TranscodingSubProtocol": "Http",
 "DefaultAudioStreamIndex": 1, "DefaultSubtitleStreamIndex": null,
 "HasSegments": false}
```

不转码的正确姿势（源码确认合法，无客户端报错风险）：

- `SupportsTranscoding: false` + **不输出** `TranscodingUrl`；
  `TranscodingSubProtocol` 是非可空枚举、恒输出，给 `"Http"`；
- `SupportsDirectPlay: true` 必须同时成立（客户端三选一：DirectPlay →
  DirectStream → Transcode，前两者可用就不会碰转码）；
- `SupportsProbing: false`、`RequiresOpening: false`（否则客户端会调
  `/LiveStreams/Open`）；
- `DefaultSubtitleStreamIndex` 无字幕时省略/null（`-1` 在协议里是"用户明确
  不要字幕"的有效值，不是非法值——但我们没有该语义，省略即可）；
- `HasSegments` 是 12.0 新增字段（10.10 没有），给 false 或省略均可。

**`Id` 的偏离标注**（偏离清单外的实现要点）：真 Jellyfin 的主版本
`MediaSourceInfo.Id == ItemId`（`BaseItem.cs:1153`），多版本是各 alternate
item 的 GUID。我们用文件 GUID（类型 0x05）是结构化选择，合法，但**因此
必须实现 `mediaSourceId == itemId` 时回落第一个版本**（6.4），且字幕
DeliveryUrl 中的 `{msGuid}` 要能被字幕接口反解。

### 6.3 MediaStream（来自 ffprobe 台账）

`library_file` 的探测字段直接映射；`Index` 与 ffprobe 流序号一致，同一
MediaSource 内全局唯一，外挂字幕排在内嵌流之后。

视频流：`Type:"Video"`、`Codec`（hevc/h264/av1，小写）、`Width/Height`、
`BitRate`（注意大写 R）、`BitDepth`、`VideoRange`/`VideoRangeType`（源码为
只读计算属性、无 JsonIgnore，**确认会序列化**，我们直接输出字符串）——
SDR→`"SDR"/"SDR"`；HDR10→`"HDR"/"HDR10"`；HLG→`"HDR"/"HLG"`；
杜比视界→`"HDR"/"DOVI"`（真枚举有 7 个 DOVI* 细分变体与 HDR10Plus，
我们统一用 `DOVI`，客户端按 VideoRange=HDR 已可正确标记）。

音频流（`audio_streams` JSON 逐条映射）：`Type:"Audio"`、`Codec`、`Language`
（ISO 639-2 三字母）、`Channels`、`ChannelLayout`、`SampleRate`、`IsDefault`。

`DisplayTitle` 需服务端拼好（客户端直接展示），照 Jellyfin 规则：
音频 `"Chinese - AAC - 5.1 - Default"`（有 Profile 时 Profile 顶掉 Codec 名，
如 `DTS-HD MA`）；视频 `"1080p HEVC HDR"`（空格连接，用 VideoRange 一级值，
**SDR 也拼**：`"1080p H264 SDR"`）；字幕 `"Chinese - SUBRIP - External"`。
**不输出 `Title` 字段**（Jellyfin 里 Title 非空会改变 DisplayTitle 拼接基底，
省略最稳）。

外挂字幕流的声明（客户端单独拉取的关键）：

```json
{"Type": "Subtitle", "Index": 3, "Codec": "subrip", "Language": "chi",
 "IsExternal": true, "SupportsExternalStream": true, "IsTextSubtitleStream": true,
 "DeliveryMethod": "External", "IsExternalUrl": false,
 "DeliveryUrl": "/Videos/{itemGuid}/{msGuid}/Subtitles/3/0/Stream.srt?ApiKey=<token>"}
```

`DeliveryUrl` 是**相对路径**（`/Videos/` 开头）；`DeliveryMethod` 必须
`"External"` 才会被使用（枚举：Encode/Embed/External/Hls/Drop）。
（首期范围：`subtitle_streams` 只有内封轨——内封字幕 DirectPlay 时由播放器
自行解封装，无需服务端参与；**外挂字幕文件的发现与台账是新需求**，列入
P1 的实施前提，见第 11 节。）

### 6.4 取流：`GET|HEAD /Videos/{itemId}/stream[.{container}]`

**鉴权（偏离③，实施要点）**：真 Jellyfin 此接口**完全匿名**——
VideosController.cs:314-318 无 `[Authorize]`、全局无 FallbackPolicy，不带
token 也能拉流；字幕 Stream 接口同样匿名。**我们默认要求 token**
（`?ApiKey=` 或任一 4.1 位置）：movieclaw 可能暴露公网，媒体文件裸奔不可
接受；官方客户端与 Infuse 构造流 URL 时恒带 `ApiKey`，风险很低。若实测遇到
不带 token 取流的客户端，再评估按库开关放行——先记录，不预做。

参数与行为：

- `static=true`：直连必带。**Jellyfin 语义**：缺省/false 时走 ffmpeg 转码；
  我们无转码 → 返回 400（偏离⑨）。非 File/Http 协议的源 Jellyfin 也是 400；
- `mediaSourceId`：缺省取第一个版本；**此接口匹配是 Ordinal（大小写敏感），
  与 PlaybackInfo 的 IgnoreCase 不一致**——我们全程输出小写 GUID 并两端
  统一小写比较规避；值等于 itemId 时回落第一个版本（配合 6.2 的 Id 偏离，
  必须实现）；解析失败给 404（Jellyfin 此处未 TryParse 会 500，不复刻）；
- `.{container}` 后缀路由与不带后缀完全同一处理（后缀只为帮助部分播放器
  按扩展名选解封装器）。

**本地文件**：完整实现 HTTP Range 语义——`Accept-Ranges: bytes`、
`Range: bytes=x-y` → `206 Partial Content` + `Content-Range`、`If-Range`、
HEAD 支持（Jellyfin 侧由 `PhysicalFileResult(EnableRangeProcessing=true)`
全自动处理，我们用 Starlette `FileResponse` 0.36+ 的原生 Range 支持并逐项
验证）。`Content-Type` 按容器查表：mkv→`video/x-matroska`、mp4→`video/mp4`、
ts→`video/mp2t`，未知视频扩展名兜底 `video/{ext}`。
**拖进度条完全依赖这套，是播放体验的生命线。**

**strm 网盘条目（本设计的差异化，不代理）**：

Jellyfin 对 strm 的原生行为（源码逐条确认）：`.strm` 文件被标记
`IsShortcut`；探测时读**第一个非空、非 `#` 开头的行**，仅接受
http/https/rtsp/rtp 绝对 URI；生成 MediaSourceInfo 时若该 URL 协议**非
File** 则覆盖 `Path=<URL>`、`Protocol="Http"`、`IsRemote=true`。
**安全条款必须照抄**（BaseItem.cs:1191-1194 注释明示）：strm 内容解析出
File 协议/相对路径时**拒绝**，否则 strm 就成了任意本地文件读取漏洞。

"客户端直连 Path"的依据（v1.1 修正）：12.0 服务端源码里"DirectPlay 直接播
Path"是**客户端侧约定**，服务端可引的最强证据是
`ForceRemoteSourceTranscoding` 权限的存在与实现（PermissionKind.cs:108-111 +
MediaInfoHelper.cs:305-317：仅当用户被显式赋予该权限时才禁用远程源直连并强制
转码）——**默认情况下远程源就是客户端直连 Path**。同时 12.0 服务端对仍打
`/stream` 的客户端实现了反向代理（FileStreamResponseHelpers.cs:29-97）。
我们两层处理（偏离②）：

1. PlaybackInfo 里 strm 条目输出 `Protocol:"Http"` + `Path:<strm 内容 URL>` +
   `IsRemote:true`，主流客户端直连云端，服务器零流量。**strm 只在
   PlaybackInfo 与 /stream 这两个播放场景现读**（直链多带时效签名，须现读
   现用）；浏览场景（列表/单条目的 `fields=MediaSources`）**不读 strm 文件**
   ——`Path` 保留 .strm 占位路径、`Protocol/IsRemote` 照常输出。每个 strm
   条目读一次文件在云盘挂载上就是一次网络往返，千余条目的列表请求曾因此
   耗时 20 余秒（issue #88），而浏览场景根本用不到直链；
2. 兜底：客户端仍请求 `/Videos/{id}/stream` 时，读 strm 内容后返回
   **302 重定向**（不做反向代理）。302 注意：HEAD 同样 302；不塞 body；
   重定向目标须自己支持 Range（CloudDrive/Alist 直链均支持）。
   已知风险：部分网盘校验重定向后请求的 **User-Agent**（Infuse 8.0.6 曾因
   停止透传 UA 导致 302 场景大面积失败，8.0.7 修复）——文档化即可，
   服务端无从代劳。

strm 条目字段：`Container` 从 URL 扩展名猜、猜不到省略（**偏离⑦**：Jellyfin
会给字面量 `"strm"`）；`ETag` 省略（Jellyfin 会给 .strm 文件自身 mtime 的
md5，可空字段省略合法）；`VideoType: "VideoFile"` 照给；`Size/Bitrate/
MediaStreams` 按台账有啥给啥，`MediaStreams` 可为空数组——Jellyfin 在
PlaybackInfo 时会对 strm 强制远程探测刷新（MediaSourceManager.cs:181-198），
我们不探测云端是既有原则（strm-workflow.md），DirectPlay 下播放器自行探测。

### 6.5 外挂字幕接口（P1）

```
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/{startPositionTicks}/Stream.{format}
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/Stream.{format}
```

DeliveryUrl 用的是带 ticks 的那条（ticks 恒传 0；带 ticks 版转调不带 ticks
版）。每个 route 段都有同名 query 参数可覆盖（itemId/mediaSourceId/index/
format）。鉴权同 6.4 决策（真 Jellyfin 匿名，我们要求 token）。

format 支持 `srt`/`vtt`/`ass`：与源文件同格式 → 原样吐字节；`srt→vtt` 做
纯文本转换（加 `WEBVTT` 头、逗号改点）；`ass` 不做跨格式转换（Jellyfin 同样
不支持 ass 互转）；query `?format=`（显式空串）→ 原样返回源文件。
Content-Type：srt→`application/x-subrip`、vtt→`text/vtt`、
**ass→`text/x-ssa`**（不是 x-ass）。

## 7. 播放进度回报

| 接口 | 请求体关键字段 | 响应 |
|---|---|---|
| `POST /Sessions/Playing` | 同 Progress（PlaybackStartInfo 类体为空，纯继承） | 204 |
| `POST /Sessions/Playing/Progress` | `ItemId`、`PositionTicks`、`IsPaused`、`PlaySessionId`、`MediaSourceId`、`PlayMethod`（其余 15 个字段接受并忽略） | 204 |
| `POST /Sessions/Playing/Stopped` | `ItemId`、`PositionTicks`、`PlaySessionId`、`MediaSourceId`、**`Failed`** | 204 |
| `POST /Sessions/Playing/Ping` | query `playSessionId`（**Required**，缺失 400；值忽略） | 204 |
| `POST /UserPlayedItems/{itemId}` | query `datePlayed?`（LegacyDateTime 格式宽容） | **200 + UserItemDataDto** |
| `DELETE /UserPlayedItems/{itemId}` | — | 200 + UserItemDataDto |

`SessionId` 字段服务端无条件覆盖（客户端传值被忽略）；`PlayMethod` 上报
`Transcode` 时 Jellyfin 校验无转码任务后降级记为 DirectPlay——我们等价。

**落库逻辑（写 `playback_state`，v1.1 按源码逐条修正）**：

- **`play_count += 1` 与 `last_played_at` 在 `POST /Sessions/Playing`（开始）
  时更新**——不是结束时（SessionManager.cs:845）；
- **Progress 与 Stopped 跑同一套阈值判定**（Jellyfin 在 Progress 上也调
  UpdatePlayState——只发 Progress 不发 Stopped 的客户端（强杀 App）播过
  90% 也必须被标已看）。阈值三分支互斥（UserDataManager.cs:449-473，
  百分比对 runtime 计算；默认 MinResumePct=5 / MaxResumePct=90 /
  MinResumeDurationSeconds=300）：
  1. 进度 < 5% → `position=0`，**Played 不变**（视为未开始）；
  2. 进度 > 90% 或 position ≥ runtime−1s → `position=0`、`played=true`；
  3. 5%~90% 之间：runtime < 300s 的短片 → `position=0`、`played=true`
     （**短片直接算看完，不是"视为未开始"**）；否则记录 position（续播点）；
  4. 无 runtime 数据 → `played=true`；
- **`Failed=true` 的 Stopped：完全跳过落库**（SessionManager.cs:1164-1167）；
- `POST /UserPlayedItems`：`played=true, position=0`；**仅在传了 `datePlayed`
  时 `play_count+=1`，否则 `play_count=max(play_count,1)`**；
  `last_played_at = datePlayed ?? 原值 ?? now`；
- `DELETE /UserPlayedItems`：**全部清零**（play_count=0、position=0、
  last_played_at=NULL、played=false），不是减一；
- 作用于 Series/Season GUID 时级联到全部子集（Folder.MarkPlayed 递归语义）；
- 请求体必须容忍未知字段与大小写（`EventName` 等，见 §1）。

## 8. movieclaw 侧数据映射与工程设计

### 8.1 GUID 编码方案（无映射表，双向可逆）

Jellyfin 的一切 ID 都是 32 位 hex GUID；movieclaw 是整型主键 + 数字对。用
**结构化编码**取代映射表（无状态、稳定、可逆）：

```
16 字节 = [魔数 "MC" 2B][类型 1B][保留 1B][载荷 12B]，hex 后即 32 位 GUID

类型 0x01 库视图      载荷 = library.id (8B)
类型 0x02 电影/剧集   载荷 = media_item.id (8B)
类型 0x03 季          载荷 = media_item.id (8B) + season (2B)
类型 0x04 集          载荷 = media_item.id (8B) + season (2B) + episode (2B)
类型 0x05 媒体源      载荷 = library_file.id (8B)
类型 0x00 用户/服务器  载荷 = 固定常量
```

- 集的 GUID 编码 `(item, season, episode)` 数字对而非 `media_episode.id`——
  集行随元数据刷新可能增删重建，行 id 不稳定，数字对才是 movieclaw 全局
  约定的稳定引用（与 wanted/library_file 同源）；
- **hex 输出全小写**（6.4 的 Ordinal 匹配问题由此规避）；入参解析剥横线、
  不区分大小写，按魔数+类型分发；魔数不匹配 → 404；
- 服务器 ID、用户 GUID 首启生成/固定编码，持久化在 `app_setting`。

### 8.2 新增持久化（迁移向前兼容，遵守发布规范第 3 条）

| 表 | 用途 | 结构 |
|---|---|---|
| `jellyfin_device` | 播放器设备 token（协议层专属） | token(唯一)、client、device_name、**device_id(唯一，重登录覆盖换发)**、version、last_seen_at |
| `playback_state` | 观看状态（领域层，协议无关） | (media_item_id, season, episode) 唯一；**position_ms**、played、play_count、last_played_at、is_favorite |

纯增表迁移，可自由向前兼容；不动任何既有表。

### 8.3 模块与挂载

- 新包 `src/movieclaw_jellyfin/`：协议层（DTO 序列化、GUID 编解码、
  Authorization 解析）+ 路由层；查询复用 `movieclaw_media` / `movieclaw_db`
  的服务与仓储，不直接裸写 SQL 散落各处；
- 路由挂载在**同一 FastAPI 应用、同一端口**的根路径（`/System/*`、
  `/Users/*`、`/Items/*`……），注册在 SPA catch-all 之前；`/emby/*` 前缀
  别名一并注册（非 Jellyfin 行为，纯兜底）。选择同端口的理由：单容器单端口
  是部署契约，开新端口要动 compose/文档/runtime-version，收益不成比例；
  Jellyfin 的根路径命名空间（System/Users/Items/Videos/Shows/Sessions/
  Branding/QuickConnect/UserViews/UserItems/UserPlayedItems/
  UserFavoriteItems/Search/Library）与 movieclaw 现有 `/api/*` 及前端路由无
  冲突（实施第一步先做一次冲突清点）；
- 响应模型独立一套 Pydantic（PascalCase alias + exclude_none），与业务
  接口的 `success/code/message/data` 规范**彻底隔离**——这是模仿外部协议，
  不是 movieclaw 业务接口，两套规范互不渗透；错误响应按 §1 的四形态语义
  返回，不走统一异常处理器。

### 8.4 运行时、部署与开关

- **兼容层默认开启**（用户决策 2026-08-03：接口本身成本不高，开箱即用价值
  大于最小暴露面顾虑）。服务器 ID 首启自动生成持久化，无需任何配置即可被
  播放器连接；控制台提供关闭开关与 `published_server_url` 配置项。
  安全面不变：所有非匿名接口都要求登录换 token，与控制台同源账号；
  流媒体接口也要求 token（偏离③）。
- **HTTP 端口零新增**：兼容接口与主应用同端口，用户现有的 Docker 端口映射
  原样覆盖，升级即生效。播放器里填的地址就是控制台地址。
- **UDP 7359（自动发现）是唯一的新端口**：compose 示例补 `7359:7359/udp`
  映射；同时在部署文档写明桥接模式下广播可能到不了容器（见 3.1），
  自动发现不可用不影响手动填地址连接。
- 运行时依赖：2026-08-03 因库封面拼贴与图片缩放引入 **Pillow**，
  `docker/runtime-version` 已按发布规范 bump（2→3），合并后需发布新镜像；
  compose 模板/部署文档更新随本特性一并发布。

### 8.5 领域层与协议层分离（为未来网页端播放预留，2026-08-03 用户提出）

播放相关的底层能力**必须与 Jellyfin 协议解耦**，将来控制台自带网页播放器时
直接复用，Jellyfin 层只是它的一层"翻译皮"：

**领域层**（落 `movieclaw_media` 或独立 `movieclaw_playback`，协议无关）：

1. **播放状态**：`playback_state` 表 + 读写服务。进度单位用**毫秒**
   （`position_ms`），不用 ticks——ticks 是 Jellyfin 方言；
2. **进度判定规则**：`record_playback_progress()` / `mark_played()` /
   `mark_unplayed()` 实现第 7 节的阈值三分支（<5% 未开始 / >90% 已看 /
   短片直接算看完 / 开始播放时 play_count+1）。这套本质是通行的 scrobble
   语义，网页端播放器回报进度走**同一个函数**；
3. **取流服务**：Range/206 文件服务、strm 解析与 302（含 File 协议拒绝的
   安全条款）、MIME 表——纯函数/服务形态。网页 `<video>` 同样靠 Range
   拖进度、同样吃 302；
4. **字幕服务**：外挂字幕发现、srt→vtt 转换（网页播放器只认 vtt，正好
   同一套）。

**协议层**（`movieclaw_jellyfin`，只做翻译，未来网页端不碰）：GUID 编解码、
PascalCase DTO、Authorization 头解析、DisplayTitle 拼装、ticks↔ms 换算、
`jellyfin_device` 设备 token。

未来网页端播放走业务接口（`/api/playback/*`，`success/data` 规范 + 整型
id），调同一套领域服务——**不**让自家前端去消费 Jellyfin 兼容接口，两边
各自演进互不牵连（如 Jellyfin 层将来跟进 10.11 行为变更，不波及播放页）。

## 9. 风险与对策

1. **路由大小写**：ASP.NET 大小写不敏感，Starlette 敏感。对策：ASGI 中间件
   把命中 Jellyfin 命名空间前缀（大小写不敏感匹配 system/users/items/videos/
   shows/sessions/…）的路径归一化为注册时的规范大小写。
2. **客户端行为差异**：各播放器请求序列不同，静态调研覆盖不了全部。对策：
   本文档经三轮对抗式源码复核充当契约；首发支持列表明确写 Infuse/Fileball/
   VidHub，其余"理论兼容"。上线后遇到兼容问题，按 §0 偏离清单逐项排查
   （偏离处是首要嫌疑）。
3. **未识别/缺元数据条目**：`media_item_id IS NULL` 的文件不出现在兼容接口
   里（待识别清单是控制台的事）；有条目无 metadata 行时靠 null 省略降级。
4. **strm 直链时效与 UA 校验**：strm 内容若是带签名的临时直链，客户端缓存
   Path 过期会播放失败；部分网盘校验重定向后请求的 User-Agent。对策：
   strm 场景 Path 也可指向我们的 stream 端点（302 每次现读 strm 文件），
   牺牲一跳换稳定；两种模式做成库级开关，默认直连。
5. **性能**：/Items 大库分页 + fields 门控天然限量；图片缩放有缓存；台账
   查询已有热路径索引。**浏览零媒体 IO 原则**（issue #88 后的硬约束）：
   入库完成后，除非用户主动扫描/刷新元数据，浏览类请求不对媒体文件本体
   做任何文件系统调用——本地文件的 `ETag` 由台账 `file_mtime_ns` 派生
   （扫描/入库时随 stat 落库，旧行 NULL 省略、重扫回填），strm 只在播放
   场景现读，图片 tag 由资产相对路径 + 所属行 `updated_at` 派生，演职员
   只在 `fields=People` 时装载。

## 10. 分期实施与验收

前置（P0 开工前）：暂无法抓包真实客户端（用户环境限制），以本文档 v1.1 为
契约直接实施；每个接口的单测断言直接取自文中 JSON 形态与状态码表。后续
具备条件时再用 mitmproxy 抓 Infuse ↔ 真 Jellyfin 会话补一层回归剧本
（`tests/fixtures/jellyfin_compat/`）。

| 期 | 内容 | 验收 |
|---|---|---|
| P0-a | GUID 编解码、Authorization 解析、序列化基建、System/Auth、Capabilities 204、UDP 发现、新表迁移 | 单测：解析/编解码边界、UserDto 金样对照；Infuse 能发现并登录成功 |
| P0-b | UserViews(+legacy) / Items(+legacy) / 单条目(+legacy，全字段) / Shows / 图片 | Infuse 完整浏览两类库，海报/详情/季集结构正确 |
| P0-c | PlaybackInfo(GET+POST) / stream(Range+302) / 进度回报 / 已看标记 | Infuse 播放本地文件可拖动进度条；strm 条目直连云端（服务器观察零媒体流量）；退出后控制台与 Infuse 双向可见进度；播过 90% 强杀 App 也标已看 |
| P1 | Resume(+legacy) / NextUp / Latest(+legacy) / 字幕接口 / 收藏 / Branding/QuickConnect/Sessions/Endpoint/ActiveEncodings 敷衍接口 / 外挂字幕台账 | Infuse 首页三区正确；外挂字幕可选可显；收藏双向同步 |
| P2 | legacy PlayingItems 别名、/emby 前缀、Filters2/Root/Similar/Search/Hints/System/Info、Fileball/VidHub 回归 | 三款播放器全链路手测通过 |

合并前照例全绿：`pytest`、`ruff check .`、`pnpm web:lint`、`pnpm web:typecheck`。

## 11. 开放问题与实现状态

已决：兼容层**默认开启**（2026-08-03 用户拍板，落地见 8.4）；发现地址
三层策略（见 3.1）；收藏接口**做**（v1.1 复核确认四条路由与响应结构，
与已看接口完全同构、复用 handler 骨架，成本极低，列入 P1，见 §2）；
流媒体接口**要求 token**（偏离③，理由见 6.4，若实测遇到问题再评估开关）。

**实现状态（2026-08-03，两轮实现↔源码对抗终验后）**：P0 + P1 已全量落地
（`src/movieclaw_jellyfin/` 协议层 + `src/movieclaw_playback/` 领域层，
55 个协议测试全绿），终验发现的 28 项缺陷已修复。此外把 ASP.NET 的
**query 键大小写不敏感**语义补进了 §1 同款归一化中间件（v1.1 调研只覆盖
了路径，实为同一差异的两半——教训记档）。

**补遗（2026-08-11，issue #124）**：新版 Infuse 添加媒体库时的探测链路比
v1.1 调研更长，还会请求 `/Plugins`、`/Library/VirtualFolders`、
`/UserViews/GroupingOptions`、`/DisplayPreferences/{id}`（GET+POST）——
这些接口经前端端口返回 Next.js 404 HTML 会让 Infuse 在"验证媒体库"一步
失败。已补齐后端敷衍/映射实现与前端 `Plugins`/`DisplayPreferences` 命名空间
转发。**Library 命名空间不能整段通配**：Next 的 rewrite source 匹配大小写
不敏感、且 afterFiles rewrites 先于动态路由求值，`/Library/:path*` 会劫持
控制台自己的 `/library/[id]` 页面——只按字面注册 VirtualFolders/
MediaFolders/PhysicalPaths/Refresh 四个 API 子路径。
实现已对照 v10.10.7 源码逐条复核：DisplayPreferencesDto 的 Id 按
`GetMD5`（**UTF-16LE**，即 C# Encoding.Unicode + .NET Guid 小端字节序）
派生（金样 `usersettings` → `3ce5b65d-…`），CustomPrefs 对齐新建实体
默认值；GroupingOptions 按名称排序并注册 legacy
`/Users/{userId}/GroupingOptions`；VirtualFolderInfo 带 LibraryOptions
静态子集，RefreshStatus 接扫描/元数据刷新任务线（Active+百分比/Queued/
Idle）。有意放宽的偏离：真 Jellyfin 的 /Plugins 与 /Library/VirtualFolders
是仅管理员（RequiresElevation）接口，这里放开给已认证设备但成员只见
白名单库、不下发文件系统路径。
**补遗（2026-08-20，nginx 前门）**：容器对外端口改由 nginx 接住
（`docker/nginx.conf.template`），Jellyfin 命名空间与 `/api/v1` 直达 uvicorn，
不再经 Next 反代——此前取流/整文件下载每 GB 要多烧约 10 个 CPU 秒（Node
反代本身 5.4 秒 + 它小块慢读把 uvicorn 拖慢），并发下载时一个核被吃光。
nginx 实测 0.7 秒/GB（`proxy_buffering off` + 64 KB 同步读缓冲；默认 4–8 KB
缓冲是 2.9 秒/GB）。**nginx 的路由表必须与 `next.config.ts` 的 rewrites 同步
维护**（命名空间清单、Sessions/Library 只放行字面子路径的规则一模一样），
Next 侧的 rewrite 保留给裸机开发。
考古备注：2026-08-04 曾在 `jellyfin-compat` 分支按真实 Infuse 逐轮实测
修过同一问题（a50127f，含上述 rewrite 劫持教训与任务线映射），但该分支
尾部 5 个提交从未合并进 main，v0.8.0 因此不含此修复——本次已吸收其成果
（并修正其 GetMD5 误用 UTF-8 的编码差异），另补齐了它没覆盖的 /Plugins。

**已知差距**（有意留下的小缺口，不影响 Infuse 主链路）：
- `imageTypeLimit`/`enableImageTypes`/`enableTotalRecordCount` 接受但忽略
  （超集输出，协议宽容）；
- `PrimaryImageAspectRatio` 不输出（本地未存图片尺寸，纯排版观感）；
- `/Search/Hints` 的 `isNews`/`isKids`/`isSports` 接受但忽略（无直播电视、
  无分级标签数据），`includeGenres`/`includeStudios`/`includeArtists` 同理
  （没有 Genre/Studio/MusicArtist 实体，能给的按名类型只有 Person）；
- `/Persons` 的 `IsFavorite` 筛成空（本层不落人物收藏台账）；
- `/Items` 里 Person 与媒体条目混排时不参与 `sortBy` 比较，恒排在媒体之后；
- 图片两种 404 body 未细分（均给 text 文案；客户端不解析 body）。

**2026-08-23 补齐**（起因：Infuse 搜索页整体不可用，抓包定位）：
- `GET /Persons`、`GET /Persons/{name}` 实现（`routes/persons.py`）。此前
  `persons` 不在 `NAMESPACE_PREFIXES` 里，请求连兼容层都进不去，掉到前端
  catch-all 返回 21KB 的 Next.js HTML 404；
- `GET /Search/Hints` 实现（`routes/search.py`）；
- 搜索匹配从「标题/原名/**别名**的裸子串」改为 Jellyfin 口径（见 5.2）。
  副作用是别名不再参与匹配——搜 "Nirvana in Fire" 不再命中《琅琊榜》，
  与真 Jellyfin 一致；相应地分集标题变得可搜（Episode 有自己的 Name），
  剧名命中也不再把整季集数一并带出；
- 带 `searchTerm` 的查询改为「先按名字列粗筛、只水合命中条目」。此前是全库
  水合后再在内存过滤，千级条目库单次 1.4 秒，Infuse 逐字符发请求且不取消，
  4 个并发就把单次拖到 5~8 秒。

**与产品侧读策略的对齐记录**（2026-08-04，Infuse 联调后重构）：
- 单条目详情与图片接口已复用 Web 的共享读策略（`library/items.py` 的
  `layered_item_meta` / `local_item_artwork`）：详情叠加 NFO/TMDB 分层文本，
  条目 Primary/Backdrop 走「目录美术图 > 刮削资产 > TMDB 图床（经代理缓存）」
  三层；列表装配仍只读库内档案（批量性能）。ImageTags 在资产未落地时以
  TMDB 路径兜底出 tag——否则客户端不发图片请求，兜底层永远走不到。
- 自愈刮削两条触发：档案缺失/从未刮过（分层读内部，与 Web 同源）；
  档案有 cast 但影人关系为空（存量补齐，收敛条件）。仅挂单条目详情。
- **仍独立实现**：/Items 的搜索与排序口径（2026-08-23 起改为逐字对齐
  Jellyfin，见 `movieclaw_jellyfin/search.py`；与 Web 搜索服务的拼音、简繁等
  能力**有意不同源**——协议层要的是"和真 Jellyfin 一样"，不是"更好用"）；People 不做
  NFO 叠加（人物页需要关系表影人 id，NFO 给不出，靠自愈收敛）；
  季/集图片未接目录美术图层（`season{NN}-poster.jpg` 目前只在镜像写出，
  读取仍走资产）。

1. **外挂字幕台账**：library_file 目前只有内封 `subtitle_streams`；同目录
   `.srt/.ass` 的发现、命名解析（语言后缀）、台账落位是独立小设计（§6.5
   字幕接口随台账一起实施），已单开文档：
   [jellyfin-subtitle.md](jellyfin-subtitle.md)（接口范围经真 Jellyfin
   源码逐条比对定稿，含技术选型与分期）。
