# 原盘（BDMV）播放：Jellyfin 兼容层与网页播放器

> 状态：已实现（2026-09-13，阶段 0–5 一次交付）。起因：NAS 上《蜘蛛侠：英雄无归》
> UHD 原盘在 Infuse 与 VidHub 均无法播放，根因是兼容层对 container=bluray 的
> 条目一律 404。本文对照 Jellyfin 源码给出 movieclaw 的落法。
>
> **实现与计划稿的差异**（§6 汇总）：新增台账列 `library_file.disc_playlist`
> 落主播放列表清单（浏览/播放零磁盘 IO）；原盘章节取自 MPLS 的 PlayListMark；
> 网页播放器对单剪辑原盘也走 concat 清单（时间轴统一为播放列表时间）。

关联文档：[jellyfin-compat.md](jellyfin-compat.md)（偏离清单①⑨要改）、
[web-player.md](web-player.md)（§12 VOD 预生成分片）、
[disc-version-layout.md](disc-version-layout.md)（原盘目录布局）。

## 0. 现状与两个根因

**根因一：兼容层没有原盘分支。** `movieclaw_jellyfin/routes/playback.py` 的取流
入口用 `path.is_file()` 判断，原盘的 `file_path` 是目录，直接 404；
`catalog.py` 的 `media_source_dto` 又把它声明成 `VideoType=VideoFile`、
`Container=bluray`、可直连，Infuse 才会去请求 `stream.bluray`。
网页播放器同样没有分支：`source_path` 直接喂 ffmpeg，目录必然失败。

**根因二：选主片规则会被诱饵列表骗。** 2026-09-13 对 NAS 上 141 张原盘扫描，
约 50 张选中的「主播放列表」是防拷贝诱饵——同一剪辑循环几百次（如
`00152.mpls` 引用 900 次同一剪辑、时长 54054 秒），台账时长因此虚高一倍以上
（奥本海默 54054 秒 vs 真实 10822 秒）。`read_main_playlist` 只按时长取最长，
没有循环判定；Jellyfin 用的 BDInfo 在 `TSPlaylistFile.Initialize` 里把「同一剪辑
以同一 IN_time 出现两次」标为 `HasLoops`，`IsValid` 直接排除，因此不会中招。

扫描还给出真实的多剪辑主片（去掉诱饵后）：阿凡达 33 段、阿凡达：水之道 41 段、
火星救援 28 段、疯狂动物城 38 段、怪兽电力公司 32 段、寻龙传说 20 段、了不起的
盖茨比 11 段、奇异博士 2 三段、伸冤人 3 两段……约占一成。**多剪辑不是边角
情况，必须支持。**

## 1. Jellyfin 的做法（源码对照，2026-08 主干）

| 环节 | Jellyfin 行为 | 出处 |
|---|---|---|
| 识别 | 含 BDMV 的目录建成 Video，`VideoType=BluRay`，Path 即目录 | `BaseVideoResolver.cs:158` |
| 选主片 | BDInfo 扫盘，取「最长且 IsValid」的播放列表；IsValid 排除循环剪辑 | `BdInfoExaminer.cs:44`、BDInfo `TSPlaylistFile.cs` |
| 探测 | 轨道/章节/时长来自 BDInfo；ffprobe 只跑主片第一个 m2ts，补编码名、色彩、位深 | `FFProbeVideoInfo.cs:122,316` |
| PlaybackInfo | 原盘禁止 DirectPlay，允许 DirectStream；但 `StreamInfo.IsDirectStream` 对原盘恒 false，URL 不带 `Static=true`；最终 `SupportsDirectPlay=false` 并给 `TranscodingUrl`（默认允许 copy） | `StreamBuilder.cs:716`、`StreamInfo.cs:282`、`MediaInfoHelper.cs:322` |
| 取流 | `/Videos/{id}/stream` 的 static 分支显式排除原盘，一律进 ffmpeg | `VideosController.cs:468` |
| ffmpeg 输入 | 主片 m2ts 写成 concat 清单（每行 `file` + `duration`），`-f concat -safe 0 -i 清单` | `EncodingHelper.cs:1261`、`MediaEncoder.cs:1313` |
| HLS 分片 | 原盘没有关键帧索引（只允许 mkv/mp4 抽取），退化为等长分片 | `DynamicHlsPlaylistGenerator.cs:42` |
| 其他 | 原盘不可下载、不做 trickplay；视频只映射第一路 | `Video.cs:446`、`TrickplayManager.cs:631` |

一句话：Jellyfin 从不把 BDMV 当文件供流，而是把主片拼成一路 ffmpeg 输入做
remux；客户端永远走「转码」URL，只是编码器是 copy。

## 2. 目标与硬边界

目标：Infuse / VidHub / 网页播放器都能播放库里的蓝光原盘，单剪辑与多剪辑都覆盖，
时长、轨道、章节正确。

硬边界（沿用兼容层既有原则，见 jellyfin-compat.md）：

1. **不转码。** 原盘允许 remux（`-c copy`）到 HLS，视频与音频都不重编码；
   这是对偏离①的一次收窄修订：「不转码」不变，「不起 ffmpeg」改为「原盘可起
   ffmpeg 做 copy」。
2. **不改盘。** 不 remux 落盘、不生成任何持久化中间文件；concat 清单只写进
   转码会话目录（已登记的缓存目录），随会话清理。
3. **单剪辑零 ffmpeg。** 主片只有一个 m2ts 时直接按 Range 供流，播放器自己解
   复用。这是相对 Jellyfin 的有意偏离（Jellyfin 单剪辑也走 ffmpeg），理由：
   Infuse/VidHub 都自带 TS 解复用，直出能保住 Dolby Vision 双层与全部音轨，
   也不占 NAS CPU；实测 141 张盘里九成是单剪辑。
4. **ISO 与 DVD 不在本期。** ISO 需要 libbluray 挂载（Jellyfin 走 `bluray:`
   协议），VIDEO_TS 的 VOB 拼接另有 IFO 解析，都留到后续。

## 3. 设计

### 3.1 阶段 0：修选主片（独立可合并，先做）

`services/library/bluray.py`：

- `parse_mpls_playlist` 增读每个 PlayItem 的 `is_multi_angle` 与角度数（已有
  偏移，只是没解释），`MplsPlaylist` 增 `has_loops` 属性：同一 `clip_id` 以同一
  `in_time` 出现两次即为循环（与 BDInfo 同口径，只看角度 0）。
- `read_main_playlist` 的候选过滤加 `not has_loops`；其余规则（引用的 STREAM
  必须存在、同剪辑序列去重、最长优先）不变。
- 存量原盘按「`disc_playlist` 缺失」进入补探重算（不 bump
  `PROBE_SCHEMA_VERSION`，见 §6），纠正约 50 张盘的时长；`disc_main_stream`
  自动跟着换到真正主片的最大剪辑。

验证：单测构造「循环列表更长、非循环列表更短」的 MPLS 字节，断言选中后者；
NAS 上重探后奥本海默时长回到 10822 秒。

### 3.2 阶段 1：原盘播放源解析器（两条播放链路共用）

新增 `src/movieclaw_api/services/playback/disc_source.py`（计划稿原拟放 `movieclaw_playback`，但它要读 `bluray.py`，依赖方向只能在 api 侧）：

```python
@dataclass(frozen=True)
class DiscClip:
    path: Path           # BDMV/STREAM/xxxxx.m2ts
    in_s: float          # 播放列表 IN_time（秒）
    out_s: float         # 播放列表 OUT_time（秒）
    size_bytes: int

@dataclass(frozen=True)
class DiscSource:
    disc_dir: Path
    playlist_name: str   # 00001.mpls
    clips: tuple[DiscClip, ...]
    duration_s: float    # 各段 out-in 之和

    @property
    def single_clip(self) -> DiscClip | None: ...
    def write_concat_list(self, target: Path) -> Path: ...
    def keyframes_s(self) -> tuple[float, ...]: ...

def resolve_disc_source(file_path: str) -> DiscSource | None
```

- 输入是台账 `file_path`（目录，兼容 `disc-version-layout.md` 里嵌套一层 BDROM
  的情况），内部调 `read_main_playlist`；`LibraryFile.is_disc()` 判定才进入。
- **剪辑清单落台账**：新增可空 JSON 列 `library_file.disc_playlist`
  （`bluray.disc_playlist_record`：列表名 + 各剪辑 id/IN/OUT，带结构版本），
  扫描/入库/补探时与主播放列表一起写入。浏览态 DTO（`read_disc=False`）只认
  这一列，绝不回盘上读；播放态存量未补探的行退回读盘，结果按
  `(disc_dir, PLAYLIST 目录 mtime_ns)` 缓存——诱饵盘有上千个 MPLS，走 NFS
  每次全读要一到三秒。主播放列表读不出的残缺盘写「读过但没有」的哨兵
  （空清单），补探不再每轮重读，播放时再试一次读盘。
- `write_concat_list` 输出 ffmpeg concat demuxer 格式：每段 `file`、`inpoint`、
  `outpoint`、`duration`。比 Jellyfin 多写 inpoint/outpoint——播放列表可能只用
  剪辑的一段，整文件拼接会多播花絮；单引号按 concat 规范转义。
- `keyframes_s` 读各剪辑的 CLPI `EP_map`（蓝光自带的 I 帧入口表：PTS 与源包号），
  按段累加偏移后合成全片关键帧表。这是原盘的天然关键帧索引，不需要像 mkv 那样
  扫整个文件（77 GB 的 m2ts 用 ffprobe 列包要读完整个文件，不可接受）；
  Jellyfin 没有这一步所以退化成等长分片，我们的 VOD 预生成分片依赖精确边界
  （web-player.md §12），必须做。解析放在 `bluray.py`，与已有的 CLPI 语言解析
  同一文件（`parse_clpi_ep_map`）。

### 3.3 阶段 2：Jellyfin 兼容层——单剪辑直出

`catalog.py::media_source_dto`，条目 `is_disc()` 且 `resolve_disc_source` 成功：

- 单剪辑：`Path`=主片 m2ts 路径、`Container="m2ts"`、`Size`=剪辑大小、
  `RunTimeTicks`=播放列表时长、`VideoType="VideoFile"`、直连三旗标不变。
  对客户端而言这就是一个普通 m2ts 文件——**偏离⑬**：真 Jellyfin 会报
  `VideoType=BluRay` 并强制转码 URL；我们故意伪装成单文件，换取零 ffmpeg 直出。
- 多剪辑：本阶段先报 `SupportsDirectPlay=false`、`SupportsDirectStream=false`、
  `SupportsTranscoding=false`，让播放器立刻提示不支持，而不是转圈后 404。
  阶段 4 再换成转码 URL。
- 解析失败（缺主片/损坏）：从 MediaSources 剔除，整条按「无可播源」应答，
  与 strm 解析失败同口径。

`routes/playback.py::video_stream`：`selected[0].is_disc()` 时用
`resolve_disc_source` 换出单剪辑的 m2ts 路径，其后 Range 供流、活动登记、
停播回收全部复用现有代码；多剪辑在本阶段 404（阶段 4 接管）。
`stream.{container}` 后缀由 Infuse 按 Container 拼成 `stream.m2ts`，MIME 映射
`streaming.py` 已有 `m2ts → video/mp2t`。

轨道编号：台账 `audio_streams/subtitle_streams` 来自 ffprobe 主片 m2ts（阶段 0
之后保证是真主片），与播放器解复用同一文件看到的顺序一致，默认轨记忆与字幕
投递逻辑不动。Dolby Vision Profile 7 双层盘会带两路 HEVC（2160p 基础层 +
1080p 增强层），直出时由播放器自行处理，能播 DV 就播 DV。

### 3.4 阶段 3：网页播放器原盘支持

`api/routes/playback.py` 开会话处：

- `file.is_disc()` → `resolve_disc_source`；单剪辑 `source_path`=m2ts 路径，
  多剪辑 `source_path`=会话目录里刚写的 concat 清单，并给 `build_hls_command`
  新增 `input_format: str | None`，多剪辑时在 `-i` 前插 `-f concat -safe 0`。
  `-ss` 仍放在 `-i` 前，concat demuxer 支持按累计时长定位到对应剪辑；
  `-copyts` 三件套保持「全片绝对时间」语义不变（concat demuxer 输出的时间戳
  本身就是累计过偏移的）。
- 关键帧索引：`read_keyframe_index` 对原盘改走 `DiscSource.keyframes_s()`，
  再进 `compute_segment_plan`。
- `decide_playback` 的 `MediaProfile.container` 对原盘取 `"m2ts"`，自然落到
  remux 档（m2ts 不能 `<video src>` 直出）；多剪辑视频只映射 `0:v:0`，DV 增强层
  丢弃、退化 HDR10，与 Jellyfin 一致。
- 章节：MPLS PlayListMark（mark_type=1 的入口标记）转成章节表供进度条使用；
  `chapters.py::stills_eligible` 继续排除原盘抓图。可与本阶段分开合并。

### 3.5 阶段 4：Jellyfin 兼容层——多剪辑走 HLS remux

- PlaybackInfo 对多剪辑：`SupportsDirectPlay=false`、`SupportsDirectStream=false`、
  `SupportsTranscoding=true`、`TranscodingContainer="mp4"`、
  `TranscodingSubProtocol="hls"`、`TranscodingUrl=/Videos/{item}/master.m3u8?
  MediaSourceId=…&PlaySessionId=…&ApiKey=…`。Infuse/VidHub 都消费 Jellyfin 的
  HLS 转码 URL。
- 新路由 `GET /Videos/{item_id}/master.m3u8`、`GET /Videos/{item_id}/hls1/main/
  {name}`：复用网页播放器的会话管理器（`manager.start` + VOD 预生成播放列表），
  只允许 copy 计划；音轨用 query 的 `AudioStreamIndex`（协议编号减外挂偏移）。
  `router.py` 命名空间兜底里的「master.m3u8 404」注释同步删除。
- 停播：`/Sessions/Playing/Stopped` 已能按设备停止流，扩到停会话。
- jellyfin-compat.md 偏离①改写为「不转码；原盘多剪辑允许 copy remux 到 HLS」，
  偏离⑨补一句「原盘例外」，新增偏离⑬（单剪辑伪装成 m2ts 文件）。

### 3.6 阶段 5：验证与收尾

- 单测：`tests/api/test_bluray.py`（循环判定、EP_map 解析、concat 清单转义与
  inpoint/outpoint）、`tests/jellyfin/test_disc_playback.py`（单剪辑 PlaybackInfo
  形态、`stream.m2ts` 206、多剪辑 TranscodingUrl 形态、损坏盘无可播源）、
  网页播放器开会话对原盘的 source_path/input_format 断言。
- 实机：NAS 用 `movieclaw-nas-dev-deploy` 部署开发包，Infuse 与 VidHub 各验
  单剪辑《蜘蛛侠：英雄无归》（含 DV 是否点亮、音轨切换、拖动）、多剪辑
  《阿凡达》41 段（拖到段边界前后、总时长、结尾）；网页端同两张盘。
- 不需要 bump `docker/runtime-version`：concat demuxer 是 jellyfin-ffmpeg 自带
  能力，无新依赖；不新增 `data/` 目录；无数据库迁移。

## 4. 阶段顺序与合并粒度

| 阶段 | 内容 | 依赖 | 独立价值 |
|---|---|---|---|
| 0 | 选主片排除循环 + 重探 | 无 | 修 50 张盘的时长，探测目标回到真主片 |
| 1 | `disc_source.py` + CLPI EP_map | 0 | 纯库代码，无行为变化 |
| 2 | 兼容层单剪辑直出 | 1 | 九成原盘在 Infuse/VidHub 可播（含本次这张） |
| 3 | 网页播放器单/多剪辑 | 1 | 网页端全部原盘可播 |
| 4 | 兼容层多剪辑 HLS | 3 | 剩余一成原盘在 Infuse/VidHub 可播 |
| 5 | 测试与实机 | 各阶段随行 | — |

0→1→2 三步可以先发一个版本，用户立刻受益；3、4 跟进。

## 6. 实现纪要（2026-09-13）

- **数据**：`alembic/versions/20260913_2100_d4e7f2a9c631_library_file_disc_playlist.py`
  纯新增可空列，向前兼容；补探条件加 `disc_playlist` 缺失/版本落后
  （`scan._probe_backfill` / `items.backfill_streams`），存量约 141 张盘在下一次
  手动扫描时重算主片、时长与章节。没有 bump `PROBE_SCHEMA_VERSION`：那会把
  整库 9500 个文件都拉进补探，原盘只有一百多张。
- **章节**：`MplsPlaylist.chapters()` 把 PlayListMark 的 entry mark 转成台账
  `chapters` 元素（播放列表时间轴），Jellyfin `Chapters[]` 与网页进度条刻度
  自然生效；原盘仍不抓场景图（`stills_eligible` 未动）。
- **播放源**：`services/playback/disc_source.py`（`DiscSource` /
  `disc_source_for_file`）。concat 清单写进会话目录 `source.concat`
  （`session.start(source_concat=…)`，`ffmpeg_args.build_hls_command(
  input_format="concat")`），随会话目录清理。
- **决策**：`MediaProfile.disc_clips`；全解码播放器 + 多剪辑 → REMUX（copy），
  其余原盘照旧；关键帧密度来自 `DiscSource.keyframe_interval_s()`。
- **Jellyfin 层**：`catalog._apply_disc_source`、`routes/playback.py` 的
  `_apply_disc_transcoding` 与 `GET /Videos/{id}/master.m3u8`；
  `/Sessions/Playing/Stopped` 顺手 `stop_for_device`。偏离⑬登记在
  jellyfin-compat.md。
- **音轨回退（NAS 实测发现）**：TrueHD 在 ffmpeg 的 mov muxer 里仍是
  experimental，`-c:a copy` 进 fMP4 直接「Could not write header」；LPCM 更没有
  MP4 形态。`decide.fmp4_copy_audio_track` 按「点选轨 → 默认轨 → 同语言可封装轨
  → 首条可封装轨」回退，蓝光 TrueHD 自带的 AC-3 核心（ffmpeg 拆成独立 ac3 轨）
  总能顶上；网页播放器的 universal 分支与 Jellyfin 的 master 路由共用它。
- **EP_map 精度**：粗表/细表拼出的 PTS 丢低 8 位（≤ 5.7 毫秒），实测与 ffprobe
  逐个对照最大偏差 5.6 毫秒；分片边界偶有一个 GOP 的错位，分片内时间戳是绝对
  时间（`-copyts`），播放器自我校正，与 mkv Cues 的毫秒精度同一量级。

## 5. 明确不做与已知取舍

- 不做 ISO、DVD（VIDEO_TS）；不做 BD-J 菜单、多角度（取角度 0）、SubPath
  画中画。
- 多剪辑 remux 只保留第一路视频，Dolby Vision Profile 7 退化 HDR10；单剪辑直出
  不受影响。
- 原盘不支持下载与 trickplay 缩略图（与 Jellyfin 一致；`CanDownload` 语义在
  兼容层对应「不给下载入口」）。
- 台账 `hdr` 列对 DV 原盘只识别到 HDR10：MPEG-TS 里 DV 靠 PMT 描述符标识，
  ffprobe 不出 side data；如需显示 DV 徽标，另起任务解析 PMT。
