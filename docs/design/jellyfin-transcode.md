# Jellyfin 兼容层：转码协商（码率调节）

> 状态：设计 + 实现 **v1**（2026-09-23）。
> 源起：Infuse / VidHub 以 Jellyfin 协议连 movieclaw 时不支持转码——兼容层
> 落地（2026-08）早于网页播放器，当时全局还没有转码能力。现在网页端已有完整
> 的五档阶梯、会话管理与 VOD 分片流水线，本设计把这套能力接到 Jellyfin 协议
> 上，**只复用、不复制**：底层会话/ffmpeg/分片一律走网页端那套，Jellyfin 层
> 只做协议翻译。
> 关联文档：[jellyfin-compat.md](jellyfin-compat.md)（本文修订其 §0 硬边界 2 与
> 偏离①/⑨）、[web-player.md](web-player.md)（会话、VOD 分片、码率自适应）、
> [player-pipeline-optimization.md](player-pipeline-optimization.md) §C（按线路
> 收码率的规则，本文直接复用）、[disc-playback.md](disc-playback.md)（原盘 HLS
> remux 与本文共用 master 路由）。

## 0. 定位与硬决策

**要解决的只有一件事**：用户在外网/移动网络用 Infuse、VidHub 看片，线路装不下
原片码率，在播放器里把画质调低（Infuse 的「串流质量」、VidHub 的清晰度菜单、
jellyfin-web 的码率菜单），服务端要按要求把码率压下来。

四条硬边界：

1. **触发权在客户端**。全解码播放器直连永远是最优解（无损、零开销），
   服务端**绝不主动**判定它们需要转码。只有播放器通过协议明确表达
   「线路装不下」（`MaxStreamingBitrate` 小于源码率）或「我就要转码」
   （`EnableDirectPlay=false` / `AllowVideoStreamCopy=false`）时才转。
   这条与 jellyfin-compat.md 硬边界 2「本层不转码」的精神一致——修订的只是
   「不转码」变为「不主动转码」。
2. **DeviceProfile 的编码条件不解析**。Infuse/VidHub 的 profile 就是
   「我全都能解」，解析它换不来任何行为；真正会让它们要求转码的只有线路。
   只取 `DeviceProfile.MaxStreamingBitrate` 这一个数（Infuse 只在 profile 里带）。
3. **strm 不转码**（沿用 web-player.md 硬边界 2）；**原盘不按码率转码**
   （单剪辑直出、多剪辑 copy remux，disc-playback.md 不变，转码留作后续）。
4. **转码能力不可用时行为与今天完全一致**：无硬件加速且软件转码未开、
   或 HDR 源没有显卡做 tone-map，协商结果仍是直连（并写一条说明原因的日志），
   不会因为播放器限了码率就放不了。第三方播放器没有弹窗可承载「同意软件
   转码」，软转开关只能在网页播放器里开（web-player.md §3.6）。

## 1. 协议事实（真 Jellyfin 10.10 的行为，本设计对齐的目标）

客户端「调码率」在协议上只有一条路：**重新 `POST /Items/{id}/PlaybackInfo`**，
带新的 `MaxStreamingBitrate`（query 或 body，Infuse 放在
`DeviceProfile.MaxStreamingBitrate`），有的还带 `EnableDirectPlay=false`、
`StartTimeTicks`（当前位置）。服务端在 `MediaInfoHelper.SetDeviceSpecificData`
→ `StreamBuilder.BuildVideoItem` 里判定：

- 源 `Bitrate` 有值且 `> MaxStreamingBitrate` → `ContainerBitrateExceedsLimit`，
  直连/直流都不行，落到 Transcode；源码率未知按允许直连；
- `EnableDirectPlay=false` → 跳过直连判定；
- 落到 Transcode 时：`SupportsDirectPlay=false`、`SupportsDirectStream=false`、
  `TranscodingUrl=/videos/{id}/master.m3u8?...`（`StreamInfo.ToUrl`，参数含
  `MediaSourceId / PlaySessionId / VideoCodec / AudioCodec / VideoBitrate /
  MaxHeight / AudioStreamIndex / StartTimeTicks / TranscodeReasons / api_key`）、
  `TranscodingContainer` 与 `TranscodingSubProtocol=hls`；
- 直连可行时：`SupportsDirectPlay=true` **且** `SupportsTranscoding=true`
  但**不给** `TranscodingUrl`（客户端三选一，前两者可用就不碰转码）。

HLS 侧：`master.m3u8` → `main.m3u8` 是**从 0 起、覆盖全片的 VOD 列表**
（`DynamicHlsPlaylistGenerator`），`StartTimeTicks` 只决定 ffmpeg 第一次从哪
起转，客户端自己 seek 到该位置；seek 落到未转分片时服务端杀掉 ffmpeg 从目标
分片重起（`GetHlsVideoSegment`）。这与网页播放器的 VOD 分片流水线
（web-player.md §12）是同一个模型——所以复用是零阻抗的。

停播：`POST /Sessions/Playing/Stopped`（带 `PlaySessionId`）和
`DELETE /Videos/ActiveEncodings?deviceId=&playSessionId=`；心跳：转码期间官方
SDK 客户端每 10 秒 `POST /Sessions/Playing/Ping?playSessionId=`，所有客户端都
周期性 `POST /Sessions/Playing/Progress`。

## 2. 分层

```text
Jellyfin 协议层（src/movieclaw_jellyfin）
  transcode.py           协商解析 / 直连判定 / TranscodingUrl 契约 / PlaySessionId 登记（纯函数 + 内存表）
  routes/playback.py     PlaybackInfo 接协商；master.m3u8 起会话（普通文件转码 + 原盘 remux）；字幕路由抽内封文本轨
  routes/playstate.py    Progress/Ping 给会话续命；Stopped 按 PlaySessionId 停
  routes/misc.py         DELETE /Videos/ActiveEncodings 按 PlaySessionId / 设备停
        │ 复用
服务层（src/movieclaw_api/services/playback）
  plan.load_policy()               硬件自检 + 软转开关 → PlaybackPolicy（与网页端 decide 同一份）
  plan.select_execution_backend()  执行后端选择（从网页路由挪到服务层，两条链路共用）
  adaptive.adapt_to_downlink()     总码率 → (目标高度, 码率上限)，多一个 label 参数改文案
  session.TranscodeSessionManager  会话/ffmpeg/VOD 分片/缓存/限速，原样复用；新增 touch_for_device
        │
领域层（src/movieclaw_playback）
  decide.plan_capped_transcode()   「播放器主动要求转码」的计划装配（纯函数，表驱动单测）
```

**为什么不让 Jellyfin 层走 `decide_playback`**：web-player.md §12.9 的结论
不变——那条引擎回答的是「按客户端能力该不该转」，对全解码播放器答案恒为
「不该」。本设计的问题是「客户端已决定要转，怎么转」，是另一个函数
（`plan_capped_transcode`），但它复用同一个模块里的视频计划装配
（`_build_video_plan`：H.264、高度上限、HDR tone-map、色彩空间）和音频计划
值对象，码率收紧复用 `adapt_to_downlink`。判定与执行的分离（硬边界 4）照旧：
`plan_capped_transcode` 零 IO，在 `tests/playback/test_decide.py` 表驱动覆盖。

## 3. 协商规则（PlaybackInfo）

对每个 MediaSource（普通本地文件；strm 与原盘各走原路）：

```text
1. EnableTranscoding=false                         → 直连，不声明转码（照旧）
2. plan_capped_transcode(profile, policy)
     无硬件且软转未开 / HDR 无显卡 / strm / 原盘   → 拒绝 → 直连，不声明转码；
                                                    播放器要求了转码时写 warning 日志说明原因
3. SupportsTranscoding = true
4. direct_play_allowed(源码率, 协商)：
     EnableDirectPlay=false 或 AllowVideoStreamCopy=false → 否
     MaxStreamingBitrate 缺省 / 源码率未知               → 是
     否则 源码率 <= MaxStreamingBitrate                    → 是
   是 → 直连（SupportsDirectPlay=true，无 TranscodingUrl）
5. 否 → adapt_to_downlink(计划, MaxStreamingBitrate, label="播放器要求的码率上限")
        （八成给视频；目标高度阶梯的七五折装得下就只压码率，装不下才降高度；
          量化到 250 kbps 便于转码缓存复用——与网页端完全同一条规则）
      SupportsDirectPlay=false, SupportsDirectStream=false
      TranscodingContainer="mp4", TranscodingSubProtocol="hls"
      DefaultAudioStreamIndex = 计划选中的音轨（协议编号）
      TranscodingUrl = /Videos/{item}/master.m3u8?MediaSourceId&PlaySessionId
                       &VideoCodec=h264&AudioCodec=aac,eac3,ac3,mp3,opus&SegmentContainer=mp4
                       [&VideoBitrate=<上限 bps>][&MaxHeight=<高度>][&AudioStreamIndex=<n>]
                       [&StartTimeTicks=<ticks>][&TranscodeReasons=ContainerBitrateExceedsLimit]&ApiKey=<token>
      内封字幕：文本轨（SRT/ASS/mov_text…）补 DeliveryMethod=External + DeliveryUrl
               （由字幕路由按需抽出，与网页播放器共用抽取缓存）；
               位图轨（PGS/VobSub）从 MediaStreams 撤掉——转码后 HLS 里没有字幕流，
               又不烧录（web-player.md 硬边界 1），留着只会让用户选中一条永远显示
               不出来的轨；DefaultSubtitleStreamIndex 指向被撤轨时一并清掉
```

音轨规则（`plan_capped_transcode`）：点选轨（`AudioStreamIndex`）> 记忆/默认轨；
编码属于 `aac / ac3 / eac3 / mp3 / opus`（有损、单轨不过 640 kbps）原样 copy，
否则多声道转 E-AC-3（ffmpeg 编码器上限 5.1，7.1 降混）、立体声转 AAC——
DTS 核心 1.5 Mbps、TrueHD/FLAC/LPCM 动辄数 Mbps，弱网场景留着等于没降码率。

视频规则：恒 H.264；高度 = min(源高度, 服务端上限 1080p, `adapt_to_downlink`
的结果)；HDR 一律 tone-map（转码输出恒 8-bit BT.709，装不下 HDR，issue #331 的
教训）；档位按硬件自检落 3 或 4。

**URL 是 PlaybackInfo 与 master 之间唯一的契约**：`VideoBitrate` / `MaxHeight`
是协商时算好的**结果**，master 路由只按它们装配计划、不再重算，两处必然一致；
master 也不查任何服务端状态，播放器重放同一 URL 得到同样的会话。

## 4. master.m3u8 与会话

`GET /Videos/{item}/master.m3u8`（原 disc-playback.md §3.5 的路由，本设计扩到
普通文件）：

1. 解析 URL → `TranscodeParams`；解 GUID、按 `MediaSourceId` 选版本；strm 404；
2. 原盘 → 原来的 copy remux 规划（`_disc_remux_spec`，行为不变）；
   普通文件 → `_capped_transcode_spec`：`plan_capped_transcode(max_height=MaxHeight)`
   + `bitrate_cap_bps=VideoBitrate`；硬件档经 `select_execution_backend` 选本地
   后端或远程 VideoToolbox Worker；硬件在准备阶段落空（Worker 断线/滤镜链不
   兼容）时按软转开关退档 4 或 404——绝不把硬件档的计划悄悄交给 libx264；
   VOD 规划 = `compute_uniform_plan(时长, 4s)`（转码档 force_key_frames 等长分片）；
3. `stop_for_file` 收掉同片同成员旧会话（换清晰度/换音轨都会再打这一条）；
   `manager.start(...)`，参数与网页端开会话一致（并发/配额自动推导、转码缓存开关、
   设备标识进活动页）；
4. 登记 `PlaySessionId → session.id`；签取流 token；返回 master 列表，媒体列表
   指向 `/api/v1/playback/sessions/{id}/index.m3u8?token=`——之后的分片、seek、
   限速、缓存全部是网页播放器的会话端点在服务。

`StartTimeTicks` → `start_ms`：会话从该位置所在分片起转（VOD 时间轴是文件绝对
时间，客户端 seek 到哪都行）。

## 5. 生命周期：心跳与停播

网页播放器每 15 秒 ping、180 秒无心跳回收；Jellyfin 播放器不打那条 ping，暂停
时分片请求也停了，不接心跳的话「暂停超过三分钟再继续」就撞上会话不存在。
接法（`TranscodeSessionManager.touch_for_device`）：

- `POST /Sessions/Playing/Progress`、`POST /Sessions/Playing/Ping` → 该设备名下
  全部会话续命（Infuse/VidHub 暂停时仍周期性上报 Progress；官方 SDK 客户端转码
  期间每 10 秒 Ping）；
- 分片/列表请求本身也 touch（网页端既有行为）。

停播两条入口都**优先按 PlaySessionId 精确停**：播放器换清晰度是「新
PlaybackInfo → 打新 master.m3u8 → 给旧 PlaySessionId 发 Stopped / ActiveEncodings」，
此时新会话已起来，按设备一锅端会误杀新会话（旧会话多半已被 `stop_for_file`
收掉，精确停是空操作）。不带 PlaySessionId 的客户端退回按设备停（原有行为）。
登记表是进程内字典（生产是单进程 uvicorn，与会话管理器同一前提），登记新条目
时顺手清掉会话已不存在的旧条目。

## 6. 有意偏离与已知取舍

- **不解析 DeviceProfile 编码条件、不做 DirectStream（remux）档**：`SupportsDirectStream`
  与 `SupportsDirectPlay` 同值。全解码播放器用不上 remux；jellyfin-web 直连
  movieclaw 不在支持范围（它有自己的网页播放器）。
- **`TranscodeReasons` 只进 URL 不进 DTO**：10.10 的 `MediaSourceInfo.TranscodeReasons`
  序列化形态（flags 枚举）无客户端依赖，省略最稳。
- **原盘不按码率转码**：单剪辑直出、多剪辑 copy remux 照旧；协商对原盘版本
  静默跳过。原盘 concat 输入进转码档在会话层是通的（网页端已这么用），
  留作后续。
- **PGS 在转码时不可用**：不烧录是既定硬边界；用户转码时看不到位图字幕，
  菜单里也不再列出。文本轨靠抽取旁挂（首次通读容器，之后走缓存）。
- **HDR 无显卡不转**：软件 tone-map 是幻灯片，与网页端同一条底线；此时限码率
  只能得到直连——日志里写明原因。
- **心跳依赖 Progress**：若某客户端暂停期间既不发 Progress 也不拉分片，
  180 秒后会话回收，继续播放时播放器会报错重开——实测遇到再针对性处理。
- **Stopped 带 PlaySessionId 时只停对应会话**：这条也改变了原盘 remux
  的停播路径（此前按设备停）。不带 PlaySessionId 的客户端行为不变。

## 7. 实现清单与验收

| 位置 | 内容 |
|---|---|
| `movieclaw_playback/decide.py` | `plan_capped_transcode`、`CAPPED_COPY_AUDIO_CODECS`、`audio_note` |
| `services/playback/adaptive.py` | `adapt_to_downlink(label=)` |
| `services/playback/plan.py` | `load_policy` 公开；`select_execution_backend` 从网页路由挪入 |
| `services/playback/session.py` | `touch_for_device` |
| `movieclaw_jellyfin/transcode.py` | 协商解析、直连判定、URL 契约、PlaySessionId 登记（新） |
| `movieclaw_jellyfin/routes/playback.py` | PlaybackInfo 协商；master.m3u8 通用化；字幕路由抽内封文本轨 |
| `movieclaw_jellyfin/routes/playstate.py` / `misc.py` | 心跳续命；Stopped / ActiveEncodings 精确停 |
| `movieclaw_jellyfin/router.py` | 协商参数登记进 query 大小写归一化表 |

测试：`tests/playback/test_decide.py`（计划装配四类拒绝 + 三类计划）、
`tests/jellyfin/test_transcode.py`（协商/URL 纯函数；HTTP 全链路：装得下直连、
Infuse 形态限码率转码并起会话、强制转码与点选音轨、转码不可用回落、strm/HDR
不转、Progress/Ping 续命与换清晰度不误杀、内封字幕抽取投递）。

实机验收（待做，需 NAS + 真客户端）：Infuse 把「串流质量」设为 3 Mbps 播一部
25 Mbps 的 4K HEVC HDR（有显卡机器）：应看到 480p/720p 的 H.264 流、能拖动、
暂停三分钟后能继续、退出后活动页里会话消失；VidHub 清晰度菜单在原画与 1080p
之间来回切换，ffmpeg 进程数始终 ≤ 1；无显卡机器上同样操作应保持直连并在日志
里看到原因。

不需要 bump `docker/runtime-version`：无新依赖；不新增 `data/` 目录；无数据库迁移。
