# 网页播放器：架构与实施计划

> 状态：设计定稿 **v1**（2026-08-21），待实施。
> 关联文档：[jellyfin-compat.md](jellyfin-compat.md)（第三方播放器直连）、
> [library.md](library.md)（媒体库架构）、[strm-workflow.md](strm-workflow.md)
> （网盘零流量原则）、[jellyfin-subtitle.md](jellyfin-subtitle.md)（字幕轨中性引用）、
> [library-home-recently-watched.md](library-home-recently-watched.md)（续播入口）。
>
> **本文修订 [jellyfin-compat.md](jellyfin-compat.md) §0 硬边界 2「不转码」**，
> 修订范围与理由见 §0.3。第三方播放器直连侧的行为不变。

---

## 0. 定位与硬决策

### 0.1 目标

在 movieclaw 里做一个**自有的网页播放器**：点开就能看，不必装第三方播放器、
不必配置、不必懂编解码。衡量它好不好只有一句话——

> **尽可能不转码，而不是转码转得多快。**

浏览器的解码能力是残废的（MKV 容器全不支持、AC3/DTS 基本不支持、HEVC 看硬件
脸色），而 PT 片源恰恰是「MKV + HEVC + DTS-HD + ASS」重灾区。所以本设计的
全部复杂度都指向同一件事：**把尽可能多的播放，落到不重编码的档位上**。

### 0.2 五条硬边界（实现时不可突破）

1. **绝不烧录字幕**。烧录会把任何档位瞬间拖进全转码。字幕一律旁挂：文本轨转
   VTT/ASS 前端渲染，PGS 前端解码位图。宁可某条字幕轨暂不支持，也不烧录。
2. **strm 网盘条目只允许直连**。strm 指向云端 URL，一旦允许转码，服务端就得
   先把云端内容拉下来再转——直接推翻 strm-workflow.md 的「零网盘流量」，且用户
   点一部网盘 4K 就能同时打爆 NAS 上行与网盘流量额度，还完全无感。
   strm 条目在决策引擎里是**硬规则分支**，不走通用逻辑。
3. **遥测只落本地**。播放指标写进 SQLite、设置页可看、可导出，**绝不上报任何
   外部服务**，也不提供「匿名统计」开关。这是开源自建软件的隐私红线。
4. **决策与执行分离**。决策引擎是纯函数（零 IO、可表驱动单测，跑在
   `pytest -m "not integration"`）；执行器起 ffmpeg（标 `integration`）。
   转码无法在 CI 跑真硬件，这个切分是质量保障的唯一支点。
5. **失败必须可读**。任何播放失败都要给出中文的「为什么 + 怎么办」，
   不能把 `MEDIA_ERR_SRC_NOT_SUPPORTED` 扔给用户。

### 0.3 对 jellyfin-compat.md 硬边界 2 的修订

原文（§0 硬边界 2）为「**不转码**：所有 MediaSource 声明
`SupportsDirectPlay=true, SupportsTranscoding=false`」。该边界**在 Jellyfin
兼容层内继续有效**——Infuse / VidHub 这类全解码播放器直连时，转码永远是劣化，
不该提供。

修订点是：**「不转码」从产品级硬边界降为 Jellyfin 兼容层的局部策略**。
网页播放器有自己的决策路径，允许转码。

两条链路**共用同一个决策引擎**，区别只在输入的「能力快照」不同：

| 客户端 | 能力快照来源 | 典型结果 |
|---|---|---|
| Infuse / VidHub（Jellyfin 协议） | 恒等快照「我全都能解」 | 永远落档 0 |
| 网页播放器 | `MediaCapabilities` 实测 | 按真实能力落档 0–4 |

这样比现在两套独立逻辑更干净：同一份判定表，不同输入，不同输出。

---

## 1. 总体架构

```text
┌─ 前端（apps/web）
│   ┌────────────────────────────────────────────────────┐
│   │ 能力探测 CapabilityProbe                            │
│   │   MediaCapabilities.decodingInfo() → 能力快照        │
│   │   缓存 localStorage（带版本号），随浏览器升级失效      │
│   └───────────────────┬────────────────────────────────┘
│                       │ POST /api/v1/playback/decide
│   ┌───────────────────▼────────────────────────────────┐
│   │ PlaybackEngine（接口）                              │
│   │   DirectEngine  ← <video src>                       │
│   │   HlsEngine     ← 动态 import hls.js                 │
│   │   （未来）ShakaEngine / Video.js v10                 │
│   ├────────────────────────────────────────────────────┤
│   │ UI 层 Media Chrome + 自有皮肤                        │
│   │ 字幕层 JASSUB(ASS) / libbitsub(PGS) / <track>(VTT)   │
│   │ 遥测层 requestVideoFrameCallback / QoE 采集          │
│   └────────────────────────────────────────────────────┘
│
└─ 后端（src/movieclaw_api）
    ┌────────────────────────────────────────────────────┐
    │ 决策引擎 services/playback/decide.py（纯函数）        │
    │   (能力快照, MediaStreams[], 策略配置) → PlaybackPlan │
    ├────────────────────────────────────────────────────┤
    │ 会话管理 services/playback/session.py                │
    │   进程组 / 分片索引 / 已转区间 / 心跳 / 配额          │
    ├────────────────────────────────────────────────────┤
    │ 执行器 services/playback/ffmpeg_runner.py            │
    │   asyncio subprocess + hwaccel 后端矩阵              │
    ├────────────────────────────────────────────────────┤
    │ 硬件自检 services/playback/hwprobe.py                │
    └────────────────────────────────────────────────────┘
```

**路由**：新增端点全部挂在 `/api/v1/playback/*` 下。`docker/nginx.conf.template`
已有 `location ~* ^/api/v1(/|$)` 直连后端的规则，**取流不经 Node 反代，无需改
nginx**（Node 每 GB 多烧约 10 个 CPU 秒，这条现成的设计正好用上）。

**进程模型**：生产环境是**单进程 uvicorn**（`main.py` 自持 `uvicorn.Config`，
无 `workers=N`），因此会话状态放内存即可，不需要持久化、不需要跨进程同步。
代价是——见 §4.2 的进程契约，一条都不能省。

---

## 2. 五档降级阶梯

| 档 | 名称 | 处理 | 服务端开销 | 目标首帧 | 画质 | 典型场景 |
|---|---|---|---|---|---|---|
| **0** | Direct Play | 原文件直出 + Range | ~0 | <300ms | 无损 | MP4/H.264/AAC；全部 strm |
| **1** | Remux 直通 | 容器重封装，码流逐字节不变 | ~0（仅 IO） | <1s | **无损** | MKV + H.264/HEVC + AAC |
| **2** | 音频单转 | `-c:v copy` + 转音频 | 单核 ~5% | ~1s | 仅音频 | MKV + HEVC + DTS/TrueHD |
| **3** | 硬件转码 | GPU 解+编，含 HDR tone-map | 低（GPU） | <4s | 有 | 编码不支持 / HDR / 降码率 |
| **4** | 软件转码 | libx264 | 高 | <10s | 有 | 无可用硬件时兜底 |

### 2.1 各档的 ffmpeg 形态（约定，实现须对齐）

**档 0** — 不起 ffmpeg。`FileResponse` + Range，或 strm 的云端 URL 原样下发。

**档 1 Remux**

```
ffmpeg -copyts -start_at_zero -ss <最近IDR时间> -i <src>
       -map 0:v:<i> -map 0:a:<j>
       -c copy -tag:v hvc1                 # HEVC 必须 hvc1，见 §7-①
       -avoid_negative_ts make_zero
       -f hls -hls_segment_type fmp4 -hls_flags independent_segments
       -hls_time <按实际IDR对齐，见 §7-②>
       -hls_playlist_type event
       <session_dir>/index.m3u8
```

**档 2 音频单转** — 在档 1 基础上替换音频参数：

```
       -c:v copy -tag:v hvc1
       -c:a eac3 -b:a 640k                 # 或 aac，见 §3.3 音频判定
       -af "pan=stereo|FL=0.5*FC+0.707*FL+0.707*BL|FR=0.5*FC+0.707*FR+0.707*BR"
                                           # 多声道降混必带，见 §7-⑤
```

**档 3 硬件转码**（以 VAAPI 为例，其余后端见 §5.1）

```
ffmpeg -hwaccel vaapi -hwaccel_output_format vaapi -hwaccel_device <dev>
       -copyts -start_at_zero -ss <t> -i <src>
       -vf "scale_vaapi=w=<W>:h=<H>:format=nv12<,tonemap_vaapi=...>"
       -c:v h264_vaapi -b:v <目标码率> -maxrate <1.5x> -bufsize <2x>
       -c:a <见档2> -f hls -hls_segment_type fmp4 -hls_time 2 ...
```

**档 4 软件** — `-c:v libx264 -preset veryfast -crf 21`，其余同档 3。

> 档 3/4 自己控制 GOP，可以固定 2s 分片；**档 1/2 不行**——copy 模式下切点只能
> 落在源片已有的 IDR 上。这是两组档位在分片策略上的根本差异，见 §7-②。

---

## 3. 能力探测与决策引擎

### 3.1 能力快照（前端产出）

**不使用 `canPlayType()`**。它只返回 `""` / `"maybe"` / `"probably"`，分不清
「能解码」和「能流畅解码」——4K HEVC 在无硬解的老笔记本上它会说 `probably`，
实际掉帧到 5fps。

改用 `navigator.mediaCapabilities.decodingInfo()`，逐组合探测，产出：

```jsonc
{
  "version": 1,                    // 快照 schema 版本
  "ua": "...",                     // 仅用于本地诊断，不外发
  "probedAt": "2026-08-21T...",
  "video": [
    { "codec": "avc1.640028", "maxWidth": 1920,
      "supported": true, "smooth": true, "powerEfficient": true },
    { "codec": "hvc1.2.4.L153.B0", "maxWidth": 3840,
      "supported": true, "smooth": true, "powerEfficient": false }
  ],
  "audio": [
    { "codec": "mp4a.40.2", "channels": 6, "supported": true },
    { "codec": "ec-3", "channels": 6, "supported": false }
  ],
  "containers": ["mp4", "m3u8-fmp4"],
  "hdr": { "dynamicRange": "high", "colorGamut": "p3" },  // matchMedia + screen
  "mse": "full" | "managed" | "none"                      // iOS 为 managed
}
```

三态语义：

- `supported=false` → 必须转码。
- `supported=true, smooth=false` → **能解但会卡**，应主动降档（`canPlayType`
  永远给不出这条）。
- `powerEfficient=false` → 软解，移动端/笔记本应降档（发热与续航）。

⚠️ **已知偏差**：浏览器在没有本机统计数据前，会把所有 `supported` 的配置
乐观报成 `smooth=true` / `powerEfficient=true`。因此**首次探测不可全信**，
必须有 §6.4 的运行时降档回路兜底。

快照缓存进 `localStorage`，key 带 schema 版本 + UA 哈希，浏览器升级后自动失效。

### 3.2 决策输入

| 输入 | 来源 | 说明 |
|---|---|---|
| 能力快照 | 前端（或恒等快照） | §3.1 |
| `MediaStreams[]` | `movieclaw_jellyfin/catalog.py` 现有 DTO | codec / VideoRange / 声道 / 码率 / 时长，**已是 ffprobe 落库的真值，无需新增探测** |
| 关键帧密度 | 新增，见 §3.5 | 档 1/2 是否可行的决定性输入 |
| DV profile | ffprobe `dv_profile` | 见 §7-④ |
| 是否 strm | `library_file` 现有标记 | 硬规则分支 |
| 候选文件集 | 同一条目的多版本 | **接口形状是「多候选 → 最优计划」**，见 §3.5 |
| 策略配置 | 设置页 | 最大转码并发、码率上限、是否允许软件转码、缓存配额 |

### 3.3 判定顺序（决策引擎的规范流程）

```text
1. strm 条目？
     → 能力快照能直接播 → 档 0（下发云端 URL）
     → 否则 → 拒绝，reason="网盘条目不支持转码播放，请使用第三方播放器打开"
       （硬边界 2，不进入后续任何分支）

2. 对每个候选文件独立评分（§3.5），取最优；下述 3–8 针对单个候选。

3. 视频编码判定
     supported=false                        → 需转码（档 3/4）
     supported=true, smooth=false           → 需转码（降分辨率/降码率）
     supported=true, powerEfficient=false 且客户端自称移动端 → 需转码
     否则                                   → 视频可 copy

4. HDR 判定
     源为 SDR                               → 无约束
     源为 HDR10/HLG 且 hdr.dynamicRange=high 且视频可 copy → 直通
     源为 DV P5                             → 必须转码 + tone-map（见 §7-④）
     源为 DV P7                             → 丢弃 EL 层后按 HDR10 处理
     需 tone-map 但无可用 GPU               → 拒绝，reason 说明原因
                                              （软件 tone-map 4K 是幻灯片，不硬撑）

5. 音频编码判定
     首选音轨 supported=true                → 音频可 copy
     否则在可用音轨里找一条 supported       → 换轨并提示
     都不行                                 → 音频需转（AAC 或 EAC3）
     多声道 → 立体声时必须带降混系数（§7-⑤）

6. 容器判定
     视频音频均可 copy 且容器为 mp4         → 档 0
     视频音频均可 copy 且容器非 mp4         → 档 1（remux）
     视频可 copy、音频需转                  → 档 2
     视频需转                               → 档 3（有可用硬件）
                                              → 档 4（无硬件）：开关已开则出计划，
                                                否则返回 ConsentRequired（§3.6）

7. 关键帧密度校验（仅档 1/2）
     平均 IDR 间隔 > 15s                    → 首帧收益消失，降级到档 3
     无法取得关键帧索引                     → 保守降级到档 3

8. 字幕规划（不影响档位，硬边界 1）
     文本轨（SRT/ASS/mov_text）             → 转 VTT 或原样 ASS 旁挂
     ASS 且 MKV 内含字体附件                 → 抽取字体一并下发（§7-⑨）
     PGS                                    → 前端 libbitsub 渲染
     VobSub                                 → 暂不支持，明示原因
```

### 3.4 `PlaybackDecision` 契约

`decide` 的返回**不是单一的 plan，而是三态**——软件转码需要用户同意（§3.6），
拒绝也要携带可读理由（硬边界 5）。这个形状必须一开始就定死，后期从
「总是返回 plan」改成三态要重写全部调用方。

```python
PlaybackDecision = PlaybackPlan | ConsentRequired | PlaybackRejected


class ConsentRequired(BaseModel):
    """决策落到需要用户显式同意的档位（当前只有档 4）。"""

    tier: int                        # 拟采用的档位
    reason: str                      # 中文，为什么只能走这一档
    cost_hint: str                   # 中文，开启的代价
    setting_namespace: str           # "playback.policy"
    setting_key: str                 # "software_transcode_enabled"
    can_self_enable: bool            # 当前成员是否有权改全局设置


class PlaybackRejected(BaseModel):
    reason: str                      # 中文，为什么放不了
    suggestion: str                  # 中文，怎么办


class PlaybackPlan(BaseModel):
    """一次播放的完整计划。决策引擎的唯一输出，前端据此组装播放器。"""

    tier: int                        # 0–4
    library_file_id: int             # 选中的候选文件
    url: str                         # 签名 URL（§4.7）
    container: Literal["mp4", "hls-fmp4"]

    video: VideoPlan                 # action: copy|transcode, codec, hwaccel, w/h/bitrate
    audio: AudioPlan                 # action, codec, channels, downmix, track_ref
    subtitles: list[SubtitlePlan]    # type: vtt|ass|pgs, url, track_ref, is_default
    fonts: list[str]                 # MKV 抽出的字体 URL（ASS 用）

    trickplay: TrickplayPlan | None  # 进度条缩略图雪碧图 + VTT 索引
    start_position_ms: int           # 续播点（来自 playback_state）
    duration_ms: int

    reason: str                      # ★ 中文，为什么是这个档
    degraded_from: int | None        # 若为自动降档的结果，记录原档位
```

`reason` 是**一等公民字段**，不是调试信息：它同时供给诊断面板（§6.6）和失败
提示（硬边界 5）。例如「视频编码 HEVC 可直通；音轨 TrueHD 浏览器不支持，
已转为 EAC3 5.1」。

轨引用 `track_ref` 沿用 [jellyfin-subtitle.md](jellyfin-subtitle.md) §3.3 已定的
**协议无关中性引用**（`embedded:<k>` / `external:<文件名>` / `off`），与
`playback_state.audio_track` / `subtitle_track` 同源——**网页播放器直接复用
现有的轨选择记忆，不新增字段、不需要迁移**。

### 3.5 三个特殊输入

**关键帧密度**。档 1/2 的分片只能切在 IDR 上（§7-②）。需要
`ffprobe -select_streams v -show_entries packet=pts_time,flags -skip_frame nokey`
取索引。**大文件这个操作很慢，必须缓存**：新增 `library_file.keyframe_index`
（JSON 或独立表），入库探测时顺带生成；存量文件懒加载 + 后台补齐。
取不到索引时保守降到档 3，不赌。

**DV profile**。`dv_profile` 必须单独分支——P5 用 IPTPQc2 色彩空间，当成普通
HDR10 处理会输出**绿紫画面**（见 §7-④）。

**多候选择优**。同一条目常有 1080p / 2160p 两个版本
（[disc-version-layout.md](disc-version-layout.md)）。决策接口的形状必须是
**候选集合 → 最优计划**，而不是单文件判档：优先选能落在低档位的版本
（能直通的 1080p 胜过要转码的 2160p），同档位时选码率高的。
**这个形状一开始就要定死，后期从单候选改多候选要重写调用方。**

### 3.6 软件转码的用户同意链路

**决策：软件转码（档 4）默认关闭。** 低配 NAS 上一路 1080p 软转就能吃满 CPU，
连带影响搜索、扫描、订阅——用户感知到的是「整个应用变卡了」，却完全不会联想到
是自己点了播放。默认开启是拿全站可用性赌一次播放。

但也不能默认关了就完事——那等于把「这部片放不了」的死路直接甩给用户。因此配一条
**播放时询问 + 一次同意永久保存**的交互链路：

```text
用户点播放
   │
   ├─ decide → PlaybackPlan（档 0–3）────────────────> 正常播放
   │
   └─ decide → ConsentRequired（档 4，且开关为关）
         │
         ├─ can_self_enable=true（超管）
         │     弹窗：为什么 + 代价 + 两个按钮
         │       ┌────────────────────────────────────────────┐
         │       │ 这部片需要软件转码才能在浏览器里播放          │
         │       │                                            │
         │       │ 原因：视频编码 VC-1，浏览器不支持；且未检测到 │
         │       │       可用的硬件加速设备。                   │
         │       │ 代价：软件转码会占用大量 CPU，可能让搜索、    │
         │       │       扫描、订阅等后台任务明显变慢，首帧也    │
         │       │       需要更长时间。                        │
         │       │ 之后可在「设置 → 播放」里随时关闭。          │
         │       │                                            │
         │       │        [取消]        [开启并播放]           │
         │       └────────────────────────────────────────────┘
         │     点「开启并播放」
         │       → PATCH playback.policy.software_transcode_enabled = true
         │       → 重新 decide → 得到档 4 的 plan → 播放
         │
         └─ can_self_enable=false（普通成员）
               提示：「这部片需要软件转码，当前未开启。请联系管理员在
               『设置 → 播放』中开启。」不提供开启按钮。
```

四条约定：

1. **保存粒度是全局开关**，不是每次播放、也不做「仅本次允许」的临时态——
   临时态既复杂又没价值，用户第二次遇到还得再点一遍。
2. **幂等**：开启后不再询问；用户在设置页关掉后，再遇到再问。
3. **权限**：全局设置只有超管能改（与成员管理的既有约定一致）。普通成员看到的是
   说明而非按钮——不要给一个点了会 403 的按钮。
4. **`reason` 必须说清「为什么落到这一档」**，尤其要区分两种成因：
   「编码不支持」还是「有编码不支持但也没有可用硬件加速」。后者应顺带把用户
   引到硬件自检结果（§5.2）——很多情况其实是 GPU 没配对，而不是真的只能软转。

**配置落点**：`app_setting` 表的 `playback.policy` namespace（通用配置表，
namespace + JSON，**新增配置域零数据库迁移**）。同域下一并承载最大转码并发、
码率上限、缓存配额等策略项（§4.5 / §4.6）。

---

## 4. 转码会话

### 4.1 生命周期

```text
created ──start──> spawning ──首片就绪──> ready ──客户端拉流──> streaming
   │                  │                                          │
   │                  └──失败──> failed                          │
   │                                                             │
   └──────────── idle(无心跳 60s) / stopped / evicted(配额) ──────┘
                                    │
                                    └──> 杀进程组 + 清目录
```

- 客户端每 15s 打一次 `POST /api/v1/playback/sessions/{id}/ping`。
- **60s 无心跳即回收**。用户关页面不会发任何信号，这是唯一可靠的兜底。

### 4.2 进程契约（五条，一条都不能省）

1. **必须 `asyncio.create_subprocess_exec`**，绝不能用阻塞 `subprocess.run`。
   生产是单进程 async 服务，一个阻塞调用会卡死全部用户的搜索/订阅/扫描。
   （现有 `media_probe.py` 的一次性 ffprobe 短调用不在此列，但新代码统一走异步。）
2. **必须 `start_new_session=True`** 起在独立进程组。
3. **后端 shutdown handler 统一 `killpg(SIGTERM)`，3s 后 `SIGKILL`**。
   `docker/entrypoint.sh` 的 `trap shutdown` 只 `kill "$API_PID"`，**不会连坐
   孙子进程**；而设置页重启（退出码 42）与应用内更新 overlay 都会重启后端。
   不做这条，每次重启都会留下满负荷烧 GPU、持续写盘的孤儿 ffmpeg。
4. **启动时清理残留**：扫 `data/transcodes/` 的孤儿目录并删除；不能假设上次是
   干净退出的（与「台账自愈」同思路）。
5. **stderr 必须持续读取**。ffmpeg 的 stderr 管道写满会阻塞进程本身——表现为
   转码莫名卡死。顺带用它解析进度。

### 4.3 分片与时间轴

- 输出统一 **fMP4 / CMAF**，不用 MPEG-TS。一套分片将来可同时喂 HLS 和 DASH，
  加 DASH/离线只是多一份 manifest。
- 时间戳三件套固定为 `-copyts -start_at_zero -avoid_negative_ts make_zero`，
  `-ss` 放在 `-i` **之前**（input seek）。
- **playlist 先行**：会话 spawn 后立即返回 m3u8，不等分片；分片按需生成。
- 档 3/4 用 `-hls_time 2`；档 1/2 按实际 IDR 对齐，`#EXT-X-TARGETDURATION`
  填实际最大值。

### 4.4 seek 与已转码区间

- 会话维护**已产出分片的区间位图**。
- seek 落在已转区间 → 直接给分片，**不重启进程**。
- seek 落在区间外 → 起新会话，**先杀同一 playback 的旧会话**。
- 前端 seek 必须防抖（拖动结束才发请求）。不做这条，用户连拖 5 下就有 5 个
  ffmpeg 在跑，NAS 直接躺平。

### 4.5 并发与配额

**两个独立信号量**，不能合成一个：

- **转码并发**（档 3/4）：按 CPU/GPU 算，默认 = min(2, GPU 编码会话上限)。
  NVIDIA 消费级显卡有驱动层并发编码会话限制（通常 3–5 路），超限直接报错——
  显式检测并给中文提示，**不内置任何解锁补丁**（灰色地带，不该进开源项目）。
- **直通并发**（档 1/2）：按 IO 算。NAS 机械盘上两路 4K remux 就能打满随机读，
  而 CPU 几乎是空的。

超限时排队并明确告知「当前转码会话已满（2/2），请稍候或停止其它播放」。

两个上限与软件转码开关同属 `playback.policy` 配置域（§3.6），设置页统一承载。

### 4.6 磁盘配额

⚠️ **这是最容易在真实部署炸的一条**。转码分片与 SQLite 在同一个用户挂载卷上，
盘满则 SQLite 写不进，媒体库/订阅/任务全挂——用户看到的现象是
「播了个 4K 之后整个应用坏了」。

- 目录 `data/transcodes/<session_id>/`。
- 设置页可配总配额，**默认 10GB**。
- **写入前检查剩余空间**，不足直接拒绝并给中文提示——不要指望 LRU 跑得比写入快。
- 会话结束即删；LRU 淘汰过期会话；启动清残留。
- 设置页显示当前占用。

### 4.7 鉴权：签名 URL

`<video src>`、hls.js 拉分片、`<track>` 拉字幕**都不能带自定义 header**
（Safari 原生 HLS 尤其）。因此取流 URL 只能用**查询参数里的短时效签名 token**：

- token 绑定 `(member_id, library_file_id, session_id)`，有效期 = 会话生命周期。
- 校验放 FastAPI（分片 2s 一个，QPS 极低，不必过早优化到 nginx `auth_request`）。
- 与 `movieclaw_jellyfin/security.py` 的既有鉴权**共用签名密钥与校验实现**，
  不做第二套。

---

## 5. 硬件加速（P0 范围）

### 5.1 后端矩阵

| 平台 | hwaccel | 编码器 | tone-map |
|---|---|---|---|
| Intel（VAAPI） | `vaapi` | `h264_vaapi` / `hevc_vaapi` | `tonemap_vaapi`（Intel VPP） |
| Intel（QSV） | `qsv` | `h264_qsv` | `vpp_qsv=tonemap` |
| NVIDIA | `cuda` | `h264_nvenc` | `tonemap_cuda` |
| AMD | `vaapi` / AMF | `h264_vaapi` | OpenCL |
| Rockchip RK3588 | `rkmpp` | `h264_rkmpp` | RGA |
| **Apple（仅本机开发）** | `videotoolbox` | `h264_videotoolbox` | Metal |
| 无 | — | `libx264`（档 4） | 不支持，拒绝 HDR |

抽象成 `HwBackend` 协议：`probe()` / `decode_args()` / `filter_args()` /
`encode_args()`。**VideoToolbox 必须是一等后端**——它是 Mac mini 上唯一能真正
跑通硬件路径的后端（见 §9.5）。

### 5.2 硬件自检（P0 必做，不可推迟）

自建软件里硬件加速最大的成本不是写代码，是**用户配不对**：`--device /dev/dri`
给没给、容器内用户在不在 `render` 组（GID 在群晖/unRAID/裸 Debian 上各不相同）、
驱动版本够不够。这些出问题的现象**全都是「转码失败」黑盒**。

参照 `docker/subtitle-smoke-test.sh` 在构建期跑真实 PGS→SRT 的做法，把同一范式
搬到**运行期**：

- 启动时（或设置页点按钮）对每种 hwaccel 跑一次**真实的 1 秒转码探测**。
- 结果与失败原因写进设置页，中文、可操作。例如：
  > 检测到 Intel 核显，但 `/dev/dri/renderD128` 无访问权限。
  > 请在 compose 中加入 `group_add: ["<你的 render 组 GID>"]` 后重启容器。
- 探测结果缓存，设置页可手动重测。

### 5.3 ffmpeg 与 runtime-version

Debian bookworm 的 `ffmpeg`（5.1.x，现镜像所装）对档 0/1/2 够用，但**做档 3 必须
换 jellyfin-ffmpeg**：

- 硬件加速覆盖不全（QSV / RKMPP / AMF 基本没有）；
- **HDR tone-mapping 没有 GPU 路径**，软件 `zscale+tonemap` 转 4K 是幻灯片；
- jellyfin-ffmpeg 维护着一批上游因优先级不同未合入的补丁，恰是媒体库刚需：
  Intel VPP / CUDA / Metal tone-map、DV RPU 透传进 HLS、**libx265 fMP4 HLS 的
  Safari 兼容修复**、Atmos(EAC-3+BSI) 透传进 mov、**PGS 在硬件滤镜里叠加**、
  **ffmpeg 暂停支持**（用户按暂停时挂起转码而不是继续烧 GPU——上游语境里没有
  这个使用场景）、`ffprobe` 只取首帧（对入库探测也有价值）。

**连带动作**（按 CLAUDE.md 发布规范）：

- Dockerfile 换 jellyfin-ffmpeg，保留 tesseract / seconv / 字体那一整套不动。
- **`docker/runtime-version` 8 → 9**，并在合并后发布新镜像（CI 守卫会拦截漏 bump）。
- **不为「旧镜像 + 新应用」做任何降级兼容**：runtime-version 守卫本就是干这件事的，
  应用内更新 overlay 与镜像不兼容时会被既有机制拦下并提示升级镜像。不要为了让
  老镜像也能用而在决策引擎里加「ffmpeg 能力探测降级」分支——那是给自己加一条
  永久维护的岔路。部署者升级镜像是既定的运维动作，按现有流程走即可。

---

## 6. 前端

### 6.1 引擎抽象（必须，不可省）

```ts
interface PlaybackEngine {
  attach(video: HTMLVideoElement, plan: PlaybackPlan): Promise<void>
  destroy(): void
}
// P0：DirectEngine（<video src>）+ HlsEngine（动态 import hls.js）
// 未来：ShakaEngine（要 DASH/DRM/离线时）/ Video.js v10
```

2026 年这一层正在剧烈洗牌：Vidstack / Media Chrome / Plyr / Mux Player 四家已
合并到 Mux 重建 **Video.js v10**（2025-10 tech preview，2026-03 beta，目标
mid-2026 GA，官方迁移指南计划 Q4 2026）。**因此：**

- **P0 用 Media Chrome**（Web Components，React 19 / Next 15 零摩擦，团队即 v10
  班底，迁移路径最短）。
- **明确不新押注 Vidstack**（作者已公开说明撞到自身架构上限）。
- 引擎与 UI 都在接口后面，v10 GA 后按需评估，不做仓促迁移。

**打包预算**：hls.js 只在计划为 `hls-fmp4` 时动态 import；JASSUB 只在存在 ASS
轨时加载；libbitsub 只在存在 PGS 轨时加载。播放器路由的初始 JS 不带这三个。

### 6.2 字幕

| 类型 | 方案 |
|---|---|
| SRT / mov_text | 服务端转 VTT，`<track>` 原生渲染 |
| ASS / SSA | 原样下发 + **JASSUB**（libass WASM）渲染 |
| ASS 内嵌字体 | `ffmpeg -dump_attachment` 抽出缓存，随计划下发。**不做这条字体会回退成默认字体，番剧字幕效果直接崩** |
| PGS | 前端 **libbitsub** 解码位图 |
| VobSub | P0 不支持，明示原因 |

外挂字幕的**时间轴微调（±0.1s 步进）与样式配置**（字号/字体/描边/背景/位置）
是自建媒体库刚需，不是锦上添花——外挂字幕经常不同步。

### 6.3 失败自动降档（比穷举边界情况现实得多）

有一类「看起来 codec 兼容、实际 copy 出来是坏流」的源片：MKV header
compression、参数集只在 CodecPrivate、开放 GOP……穷举不完。因此必须有回路：

```text
播放中检测到 error 事件 / 长时间 stall（>8s 无进展）
   → 上报 {plan_id, tier, 错误码, 已播时长}
   → 重新 decide，携带「上一档失败」标记
   → 服务端降一档重开会话，PlaybackPlan.degraded_from 记录原档
   → 同一文件连续失败 2 次 → 直接到最高兜底档

**降档阶梯是逐级的，档 1 失败后仍要试档 2**（实现期定，2026-08-21）。
档 2 同样 `-c:v copy`，若失败原因在视频码流会同样失败；但若原因在音轨，
档 2 正好修好。浏览器给不出可靠的失败归因，只能取舍：多试一档只浪费几秒
（一次性），跳过档 2 却可能让本可直通的视频永久多转一路（每次播放都付）。
这个不对称决定了保留 1→2。无可用显卡时降档要越过档 3 直接落档 4。
   → 兜底档仍失败 → 中文错误 + 诊断面板 + 建议用第三方播放器
```

降档事件要落进指标（§8），它是「决策引擎判错了多少」的直接度量。

### 6.4 iOS：P0 就要定的决策

iOS Safari 是整件事最难的一块：MSE 只有 `ManagedMediaSource` 子集、原生全屏
强制走系统控件（自定义 UI 在全屏时消失）、必须 `playsinline`、音量不可编程控制。

**决策：iOS 上不硬撑自定义 UI。** 走原生 HLS + 系统播放控件，只在非全屏内联
模式保留自有皮肤。硬撑的结果是投入巨大且永远差一口气。这条影响 UI 层抽象方式，
所以必须在 P0 定下。

### 6.5 体验清单

**P0 必做**

- 显式**状态机**：`idle → deciding → session-starting → buffering → playing →
  seeking → degrading → error → ended`，含降档回边。用 boolean 拼必然踩竞态坑。
- **SourceBuffer 回收**（hls.js `backBufferLength`）——长片不回收会吃到几个 G 然后崩。
- **Media Session API**：媒体键、锁屏/通知中心显示海报标题、蓝牙耳机按钮。成本极低，
  自建播放器普遍没做，做了立刻显得正经。
- **`navigator.wakeLock`** 防息屏。
- **键盘快捷键**按 YouTube 惯例（空格 / ←→ / JKL / F / M / C），不自创。
- **Trickplay 进度条缩略图**：入库时抽帧生成雪碧图 + VTT 索引。ffmpeg 与入库
  流程都现成，**性价比最高**，是「这个播放器不便宜」的第一印象来源。
- **下一集自动播 + 片尾倒计时**；续播点来自 `playback_state`。
- **软件转码同意弹窗**（§3.6）：说清原因与代价，一次同意永久保存；无权限的成员
  看到的是说明而非按钮。
- **诊断面板**（类似 YouTube 的 Stats for nerds）：当前档位、源/目标编码、
  是否硬件加速、实时码率、掉帧数、缓冲秒数、会话 ID、`plan.reason`。
  用户报 bug 直接截这张图——**对开源项目是支持成本的直接节省**。

**P1**

- Picture-in-Picture（含 Chrome 的 Document PiP）。
- **AirPlay**（Safari 加 `x-webkit-airplay`，近乎免费）。
- 移动端手势（双击快进、上下滑音量/亮度、长按倍速）+ Screen Orientation 锁定。
- 播放速率 + `preservesPitch`；逐帧步进。
- **响度归一化**：入库时 `ffmpeg -af loudnorm=print_format=json` 算 EBU R128
  参数存库，播放时 WebAudio `GainNode` 补偿。不同片源音量差异能到 15dB，
  用户切着看要反复调音量——很少有人做，但感知极强。

**P2**

- 跳过片头/片尾（同季各集音频指纹匹配；可复用现有 ML/ONNX 基建，是差异化点）。
- 章节标记。
- Google Cast（要注册接收端应用，成本高，最后做）。

---

## 7. 已知陷阱与对应措施

| # | 陷阱 | 现象 | 措施 |
|---|---|---|---|
| ① | **`hvc1` vs `hev1`** | Safari **静默黑屏**，无 error 无日志 | HEVC 输出一律 `-tag:v hvc1`；集成测试断言 codec tag |
| ② | **分片必须 IDR 对齐** | copy 模式硬切非关键帧 → 花屏/黑屏 | 档 1/2 按关键帧索引切；IDR 间隔 >15s 降到档 3 |
| ③ | **`-ss` 位置语义** | 时间轴错位、进度条与字幕不同步 | 固定 `-ss` 在 `-i` 前 + `-copyts -start_at_zero -avoid_negative_ts make_zero` |
| ③b | **MP4 edit list / VFR** | 音画差几十~几百 ms；长片累积漂移 | 检测 elst 并补偿；VFR 显式 `-fps_mode` |
| ④ | **Dolby Vision P5** | **绿紫画面**（最显眼的 bug） | 读 `dv_profile` 单独分支；P5 强制转码+tone-map，无 GPU 则拒绝 |
| ④b | **tone-map 用简单 clip** | 雪景/天空/爆炸高光死白 | 必须 BT.2390 EETF |
| ⑤ | **多声道降混** | 「音效很响但听不清台词」（投诉第一名） | 默认带 `pan` 提升中置权重；不用 `volume=2`（削顶失真） |
| ⑥ | **pipe 输出不能 seek** | 用户一 seek 就重开进程从头转 | 档 1 也必须会话化 + 分片落盘，不做「curl 管道」 |
| ⑦ | **连拖进度条起 N 个 ffmpeg** | NAS 躺平 | 前端防抖 + 已转区间复用 + 新会话先杀旧会话（§4.4） |
| ⑧ | **MKV header compression / 参数集只在 CodecPrivate** | copy 出来是坏流 | 检测并降档；兜底靠 §6.3 自动降档回路 |
| ⑨ | **MKV 内嵌字体未抽取** | ASS 字体回退，番剧字幕效果崩 | `-dump_attachment` 抽出缓存并随计划下发 |
| ⑩ | **stderr 管道写满** | ffmpeg 莫名卡死 | 持续读取 stderr（§4.2-5） |
| ⑪ | **孤儿 ffmpeg** | 后端重启后持续烧 GPU、写盘 | `start_new_session` + `killpg` + 启动清残留（§4.2） |
| ⑫ | **转码缓存爆盘** | SQLite 写不进，整个应用挂 | 配额 + 写前检查 + LRU + 启动清理（§4.6） |
| ⑬ | **阻塞 subprocess** | 全站 API 卡死 | 一律 `asyncio.create_subprocess_exec`（§4.2-1） |
| ⑭ | **首次能力探测过于乐观** | 判为直通实际掉帧 | §6.3 运行时降档回路 |
| ⑮ | **NVENC 并发会话上限** | 第 4 路直接报错 | 显式检测 + 中文提示；不内置解锁补丁 |

---

## 8. 指标与遥测

不自创算法：**CTA-2066** 定义播放器 QoE 事件/属性/指标及其计算方式，
**CTA-5004（CMCD）** 定义客户端向服务端携带遥测的格式。照标准实现。

### 8.1 指标清单

| 层 | 指标 | 说明 |
|---|---|---|
| 启动 | **TTFF** p50/p95 | 拆四段：decide 往返 / 会话就绪 / 首分片 / 播放器缓冲 |
| | 启动失败率 VSF | 点了播但从未出画 |
| 稳定 | **卡顿率**（停顿时长/总时长） | CTA-2066 里最贴近流失的单一指标 |
| | 卡顿频次（次/小时） | 与卡顿率互补 |
| | **掉帧率** | `dropped/total`，**<1% 是及格线** |
| | 中途失败率 EBVS | |
| 交互 | Seek 延迟 p50/p95 | **直通档与转码档必须分开统计** |
| | 控件 INP | |
| 画质 | **VMAF**（转码档 vs 源） | 见 §9.4 |
| | **直通率 =(档0+档1)/总播放** | ⭐ **北极星指标** |
| | 自动降档率 | 决策引擎判错的直接度量 |
| 资源 | 每小时转码 GPU/CPU 秒、并发峰值 | |
| | 缓存占盘、3h 内存增长 | 前者炸盘，后者崩页 |

**为什么直通率是北极星**：一个数同时代表画质（无重编码=无损）、速度（秒开）、
服务器负载（不烧 GPU）。其它指标各自只覆盖一个侧面。**建议做进设置页给用户
看**——它同时回答「这个软件对我的库适配得好不好」。

### 8.2 采集点

- **TTFF 只能用 `video.requestVideoFrameCallback()`**。`canplay` / `playing` /
  `loadeddata` 全都早于真实出画（有时早几百 ms），用它们量会系统性偏乐观，
  然后困惑「数据好看但用户说慢」。
- 掉帧：`video.getVideoPlaybackQuality()` 定时采样算增量。
- 卡顿：配对 `waiting` → `playing` 累计时长，**必须排除 seek 引起的 `waiting`**，
  否则用户拖一下进度条就被记成一次卡顿，数据全废。
- 缓冲健康：`video.buffered` 末端 − `currentTime`。
- INP：`PerformanceObserver` 的 `event` timing。
- 内存：`performance.measureUserAgentSpecificMemory()`（Chrome）。
- **CMCD**：把 buffer length / measured throughput / object duration / session id
  塞进分片请求查询参数。对自建软件是绝配——**服务端访问日志直接就是 QoE 数据源**，
  不需要任何第三方 SaaS，也天然满足硬边界 3。

### 8.3 存储

新增 `playback_metric` 表（会话粒度聚合，不存逐事件流水），设置页展示趋势并
可导出 JSON。**不外发**（硬边界 3）。

---

## 9. 测试方案

### 9.1 黄金样本库（Golden Corpus）—— 整个质量保障的地基

构造 20–40 个 10–30 秒小片段（每个几 MB），矩阵覆盖：

```text
编码   H.264 / HEVC(8bit & 10bit) / AV1 / VC-1 / MPEG-2
容器   MP4 / MKV / TS / M2TS
音频   AAC / AC3 / EAC3(Atmos) / DTS / DTS-HD / TrueHD / FLAC / 多音轨
色彩   SDR / HDR10 / HLG / DV P5 / DV P7 / HDR10+
字幕   ASS(带内嵌字体) / PGS / SRT / VobSub / mov_text
病态   长GOP(15s) / 开放GOP / VFR / 负时间戳 / edit list / MKV header compression
```

- 生成脚本 `scripts/player-bench/gen-corpus.sh`：绝大多数用 ffmpeg 合成
  （`testsrc2` + `sine` + 人工构造的病态参数），少量确实合成不出的从真实片源
  剪 10 秒。
- **产物不进 git**（体积），挂 GitHub Release 资产，脚本按 SHA256 校验下载——
  与现有 NER 模型、seconv 的做法一致。
- 每个样本配一份**期望决策 JSON**（期望档位 + 关键参数 + 期望 `reason` 要点）。

### 9.2 决策引擎单测（进 CI 门禁）

决策引擎是纯函数，直接表驱动跑 §9.1 的期望清单：
`(能力快照 × 样本 MediaStreams) → 期望 PlaybackPlan`。

- 能力快照准备若干**固定档案**：Chrome/Win+核显、Chrome/Mac、Safari/Mac、
  Safari/iOS、Firefox/Linux、恒等快照（Infuse）。
- 落在 `pytest -m "not integration"`，**每个 PR 都跑**。
- 这是最划算的一项：决策错了，用户看到的是「莫名其妙全在转码」或
  「莫名其妙播不了」，而这类回归靠人工完全兜不住。

### 9.3 执行器集成测试（标 `integration`）

不要只断言「进程退出码 0」。用 ffprobe 校验输出：

- codec tag 是 **`hvc1`**（不是 `hev1`）；
- 时间戳单调递增，无负值；
- 时长与源片一致（容差 100ms）；
- 音视频轨齐全、声道数符合计划；
- **每个分片首帧是关键帧**。

§7 里大半陷阱能被这组断言在本地/runner 上抓住。

### 9.4 VMAF 画质回归

`ffmpeg -lavfi libvmaf` 比对转码输出与源片。两个用途：**调参**（CRF/preset/
码率阶梯有客观依据而非拍脑袋）和**回归保护**（改参数导致画质掉了能发现）。

- 阈值：**1080p 转码 VMAF ≥ 93**，低于视为可感知劣化。
- 太慢，不进 CI，放发版前跑。
- 这是把「画质好」从主观争论变成可验证契约的唯一办法。

### 9.5 Mac mini 本地模拟测试环境

> ⚠️ **必须先明确一条物理限制**：Docker Desktop on macOS 跑的是 Linux 虚拟机，
> **没有 GPU 直通**——容器内既没有 `/dev/dri`（VAAPI/QSV），也拿不到
> VideoToolbox。所以**硬件转码无法在 Mac mini 的容器里测**。这不是配置问题，
> 换任何参数都不行。

因此 Mac mini 上分两种跑法，各测各的：

#### A. 容器模式（`docker compose up`）—— 覆盖除 hwaccel 之外的全部服务端

```bash
scripts/build-image.sh                 # 含新的 jellyfin-ffmpeg
docker compose up -d
# 样本库挂进去当媒体库
docker compose exec movieclaw ls /app/data
```

覆盖：档 0 / 1 / 2 / **4（软件转码）**、决策引擎端到端、会话生命周期、
seek 区间复用、并发信号量、磁盘配额与清理、孤儿进程回收（**故意 `docker
restart` 验证 §4.2-3/4**）、签名 URL 鉴权、指标采集。

**档 4 软件转码跑通 = 档 3 的代码路径除 hwaccel 参数外全部验证过**，
所以这不是"测不了硬件转码就什么都测不了"。

#### B. 原生模式 —— 真正跑通硬件路径

```bash
brew install ffmpeg                    # Homebrew 版自带 VideoToolbox
export MOVIECLAW_FFMPEG_BIN=$(which ffmpeg)
PYTHONPATH=src uvicorn movieclaw_api.main:app --factory --port 8000
pnpm web:dev
```

Apple Silicon 的 VideoToolbox 可用，能真正验证：`HwBackend` 抽象是否正确、
硬件自检（§5.2）的探测与中文诊断、`h264_videotoolbox` / `hevc_videotoolbox`
编码、Metal tone-map（HDR→SDR）。

> 这也是为什么 §5.1 把 **VideoToolbox 列为一等后端**——它不只是给 macOS 用户
> 用的，更是开发期唯一能在手边跑通硬件路径的后端。
>
> VAAPI / QSV / NVENC / RKMPP 无法在 Mac mini 验证，见 §9.7。

#### C. 客户端矩阵 —— Mac mini 在这块反而是最强的

Linux CI 做不到、Mac mini 独有的能力：

| 目标 | 怎么测 |
|---|---|
| **`hvc1` 陷阱（§7-①）** | **Safari 是唯一能验的浏览器**。喂 `hev1` 应黑屏，喂 `hvc1` 应正常 |
| HEVC 硬解直通 | Apple Silicon 原生支持，验证档 1 的真实出画 |
| 原生 HLS 路径 | Safari 不走 hls.js，单独一条链路 |
| **AirPlay** | 真机验证 |
| Media Session | macOS 通知中心 / 媒体键 / 蓝牙耳机 |
| **iOS**（§6.4） | 同局域网 iPhone/iPad 直连 Mac mini 上的实例 |
| Chrome / Firefox | 三引擎横向对比 |

#### D. 自动化采集（Playwright）

**沿用 `scripts/perf/` 的既有约定**——那里已有 Python + `playwright.async_api`
的真实浏览器压测脚本（`e2e_library_ux.py` 采集 FCP/LCP/长任务/XHR，
`seed_library_dataset.py` 造数据），新脚本同风格、同目录、同命名：

```text
scripts/perf/
├── seed_player_corpus.py   # 生成/下载黄金样本库并建成测试媒体库
├── e2e_player_qoe.py       # 注入 §8.2 采集，驱动真实播放，浏览器 × 样本矩阵
├── e2e_player_soak.py      # 3 小时长稳：内存增长、SourceBuffer 回收、A/V 漂移
└── bench_transcode.py      # 服务端侧：各档位 TTFF / GPU 占用 / VMAF
```

Playwright 与现有 perf 脚本一样是开发期手动安装，**不进 `pyproject` 运行依赖**
（否则要 bump runtime-version，且用户镜像里根本不需要）。

两条与工具链有关的事实（会省掉白折腾）：

- ✅ **Playwright 自 v1.57 起默认 Chromium 即 Chrome for Testing，自带
  H.264/AAC 专有编解码**——历史上「Playwright 的 Chromium 播不了 H.264」那个
  经典坑已经不存在。
- ⚠️ **HEVC 在 Linux 容器里基本测不了**（Chrome 的 HEVC 依赖操作系统/硬件
  解码器）。所以 CI 里 HEVC 只能验证「决策判为直通」，**无法验证真的出画**——
  真出画只能靠 Mac mini 的 Safari/Chrome（§9.5-C）。

补充手段：

- **弱网**：CDP `Network.emulateNetworkConditions` 限速/加延迟，验证降档与缓冲。
- **A/V 同步客观测量**：用带同步标记的测试片（每秒一个闪白帧 + 同刻一声哔），
  播放录制后比对偏移。检测 VFR / edit list 那类漂移，人眼靠不住。

### 9.6 门禁阈值

| 指标 | 目标 | 进 CI？ |
|---|---|---|
| 决策引擎期望值符合率 | **100%** | ✅ 表驱动单测 |
| TTFF p95（档 0/1 直通） | < 1.0s | ✅ Playwright |
| 掉帧率 | < 1% | ✅ |
| 卡顿率 | < 0.5% | ✅ |
| Seek 延迟 p95（直通） | < 500ms | ✅ |
| 输出 codec tag / 时间戳 / 关键帧断言 | 全通过 | ⚠️ `integration`，本地或自建 runner |
| TTFF p95（档 3 硬件转码） | < 4.0s | ❌ 需真硬件 |
| VMAF（1080p 转码） | ≥ 93 | ❌ 太慢，发版前 |
| 3 小时内存增长 | < 300MB | ❌ 发版前 |
| **直通率**（对参考片库） | 记录趋势，不设门禁 | 📊 |

### 9.7 硬件矩阵：发版前人工检查单

Intel 核显（VAAPI/QSV）、NVIDIA（NVENC）、AMD、RK3588 都无法进 CI，也无法在
Mac mini 验证。做成发版前的人工清单，列进 `.claude/skills/release/SKILL.md`：

对每种硬件：跑一次 §5.2 自检 → 转一个 HDR 样本 → 转一个 4K HEVC 样本 →
记录 TTFF 与 GPU 占用 → 截图诊断面板。

---

## 10. 实施计划

### P0（含硬件转码，需发新镜像）

**后端**

- [ ] `services/playback/decide.py` 决策引擎（纯函数）+ 判定表 + **`PlaybackDecision` 三态**
- [ ] `services/playback/session.py` 会话管理（生命周期、心跳、区间位图、配额）
- [ ] `services/playback/ffmpeg_runner.py` 执行器（§4.2 五条进程契约）
- [ ] `services/playback/hwprobe.py` 硬件自检 + 设置页中文诊断
- [ ] `HwBackend` 抽象：VAAPI / QSV / NVENC / VideoToolbox / 软件
- [ ] 关键帧索引：入库时生成 + 存量懒加载补齐
- [ ] 字幕规划：VTT 转换、ASS 原样、**MKV 字体抽取**、PGS 下发
- [ ] Trickplay 雪碧图 + VTT 生成（入库流程内）
- [ ] `api/routes/playback.py` 扩展：`decide` / `sessions` / `ping` / 分片 / 字幕 / 字体
- [ ] 签名 URL（与 `movieclaw_jellyfin/security.py` 共用密钥与实现）
- [ ] `playback.policy` 配置域（`app_setting` namespace + Pydantic 模型，**零迁移**）：
      软件转码开关、转码并发、直通并发、码率上限、缓存配额
- [ ] `playback_metric` 表 + 迁移（向前兼容）——**唯一需要迁移的新表**
- [ ] Jellyfin 兼容层改用同一决策引擎（恒等快照），行为不变

**前端**

- [ ] `CapabilityProbe` + 快照缓存
- [ ] `PlaybackEngine` 接口 + `DirectEngine` + `HlsEngine`
- [ ] Media Chrome UI + 自有皮肤 + 播放器状态机
- [ ] JASSUB / libbitsub / VTT 三条字幕路径 + 时间轴微调 + 样式配置
- [ ] 自动降档回路（§6.3）
- [ ] **软件转码同意弹窗 + 权限分支**（§3.6）；设置 → 播放 页承载对应开关
- [ ] Trickplay 预览、下一集自动播、续播接入 `playback_state`
- [ ] Media Session、wakeLock、键盘快捷键
- [ ] **诊断面板**
- [ ] QoE 采集（`requestVideoFrameCallback` 等）+ CMCD
- [ ] iOS 走原生 HLS 分支

**基建**

- [ ] Dockerfile 换 jellyfin-ffmpeg；**`docker/runtime-version` 8 → 9**
- [ ] `scripts/perf/` 播放器测试脚本四件套 + 黄金样本库 + Release 资产
- [ ] 决策引擎表驱动单测进 CI；集成测试标 `integration`
- [ ] 更新 `jellyfin-compat.md` §0 硬边界 2（按 §0.3）
- [ ] `release/SKILL.md` 增加硬件矩阵检查单

### P1

- [ ] PiP / Document PiP、AirPlay
- [ ] 移动端手势 + 横屏锁定
- [ ] 播放速率 + `preservesPitch`、逐帧步进
- [ ] **响度归一化**（入库算 EBU R128 存库 + WebAudio 补偿）
- [ ] AMF / RKMPP 后端
- [ ] 弱网与长稳测试进发版流程

### P2

- [ ] 跳过片头/片尾（音频指纹）
- [ ] 章节标记
- [ ] Google Cast
- [ ] WebCodecs 增强层：AC3/EAC3 前端 WASM 解码 + 视频直通，把档 2 拉回档 1
- [ ] 评估 Video.js v10（GA 且迁移指南就绪后）

---

## 11. 待定问题

1. **转码码率策略**：CRF/CQ 恒定质量 vs 固定码率。倾向 CRF（画质优先，
   自建场景带宽通常不是瓶颈），但需要 VMAF 数据支撑，P0 先用 CRF 21 起步。
2. **多码率 ABR**：本设计**明确不做**。自建媒体库主战场是局域网 + 单用户，
   ABR 收益（应对带宽抖动）远小于成本（同内容转 N 份，GPU 翻倍）。
   远程访问需求出现后再评估，届时优先做「手动选清晰度」而非自动 ABR。
3. **`playback_metric` 的保留期**：默认存多久、要不要滚动清理。
4. ~~档 4 软件转码的默认开关~~ —— **已决（2026-08-21）**：默认关闭，
   播放时按 §3.6 的同意链路询问并永久保存。
