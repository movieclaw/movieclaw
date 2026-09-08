# 播放器手感（丝滑度）改进计划

> 本文只谈**手感**：进度条跟不跟手、点一下有没有反应、拖动读数对不对得上、
> 卡了之后多久恢复。播放能不能起来、降档矩阵、字幕、会话生命周期在
> `web-player.md`，两者互不覆盖。
>
> 参考源码逐个过，结论落在 §6，再回头改 §2 的条目与 §3 的批次。
> 已过：ArtPlayer（2026-09-08）、Jellyfin + jellyfin-web（2026-09-08）。
>
> **实施状态（2026-09-08）**：§2 的 A1/A2/B1/B2/B3/B4/C1/C2/C3/D1 与 F 的两条
> 已落地并通过端到端验收（§7）；F 的另外两条按各自条目里的理由暂缓，
> D2 仍待评估。

## 0. 验收标准（先定标准再动手）

「丝滑」必须能被观察，否则改完只能靠感觉吵架。六条：

1. **进度条匀速**：播放中圆点每帧走一次，不是每 250ms 跳一格。录屏 60fps
   逐帧看，圆点位移应连续；React DevTools 里播放中的 re-render 次数**不因此
   项增加**（仍是 `timeupdate` 的 4Hz）。
2. **一个位置只有一个读数**：落点胶囊、进度条、时间文字三者同源
   （2026-09-08 已修，见 §2.A0）。
3. **点一下必有反应**：单击 → 控制层开关；双击 → 明确的第二种动作；长按 →
   倍速。三者互不误触发，且单击的反馈不被双击判定拖延 300ms。
4. **连按不打架**：连按三次快进，只发生**一次**跳转，画面走 30 秒，
   不是三次「黑一下再走 10 秒」。
5. **拖动跟手**：直通/VOD 模式下拖动进度条时画面跟随；转码会话下松手才跳，
   但拖动全程有缩略图 + 时间 + 章节名。
6. **卡住能自愈**：解码类错误先就地恢复，能救回来的不许降档——降档意味着
   画质掉一级 + 几秒黑屏，是最后手段而不是第一反应。

## 1. 现状对照

| 能力 | 别人怎么做 | movieclaw 现状 | 差距 |
|------|-----------|---------------|------|
| 进度条刷新 | ArtPlayer `USE_RAF` 每帧写 DOM；Jellyfin 事件驱动但用 rAF 合帧 | 只有 `timeupdate` → `setPositionMs`，width/left 无过渡 | **大**，A1 |
| 连按快进 | Jellyfin emby-slider：键盘只改本地值，1s 静默后才真 seek | 每按一次立刻 seek，转码会话下每次可能杀 ffmpeg 重启 | **大**，B4 |
| 解码错误 | jellyfin-web：`recoverMediaError` → `swapAudioCodec`+recover → 才放弃 | fatal MEDIA_ERROR 直接判失败走降档 | **大**，D1 |
| 回拖成本 | Jellyfin：`backBufferLength: Infinity` + 服务端默认不删分片 | `BACK_BUFFER_S = 30`，回拖 31 秒就要重取分片 | 中，C4 |
| 长按倍速 | ArtPlayer `fastForward` 插件 | 无，`playbackRate` 全站未出现 | 中，B2 |
| 双击 | ArtPlayer 自己数 tap；Jellyfin 双击全屏 | 桌面双击全屏；触屏双击=两次控制层开关（净零） | 中，B1 |
| 拖动气泡 | Jellyfin：缩略图 + 章节名 + 时间三合一 | 缩略图 + 时间 | 小，C1 |
| 章节标记 | 两家都在进度条上画 | 后端有章节，播放器没画 | 中，C1 |
| 锁屏 | ArtPlayer `lock` 插件 | 无 | 中，B3 |
| 缓冲段显示 | Jellyfin 只画含播放头的那一段 | 同样只画那一段 | **一致**，佐证既有决策 |
| 分片就绪判据 | Jellyfin：文件存在 **且**下一片存在（防半片） | live.m3u8 台账 + 远端原子替换 | 我们更准 |
| 前跳等 vs 重启 | Jellyfin：超前 24 秒才重启 | 超前 1 段就重启（被 `-readrate 1.5` 逼出来的实测结论） | 已知取舍，§6.2 |
| 迷你进度条 | ArtPlayer 插件可选 | 已决策否掉（Netflix 派全出全收） | **不做** |
| seek 规划 / 停顿归因 / QoE / 降档 | 两家都没有 | `timeline.ts` / `stall.ts` / `qoe.ts` / `machine.ts` | 我们更厚，不拉平 |

`web-player.md §6.5` 早把「双击快进、上下滑音量/亮度、长按倍速」「播放速率」
列在 P1、「章节标记」列在 P2 —— 本计划是把这几条落地，不是新开战场。

## 2. 分项方案

### A0（已完成，2026-09-08）拖动读数同源

横滑落点驱动进度条；胶囊在手势被第二根手指/换元素打断时收走；片长转未知时
清掉进度条的 `dragging`。提交 `1597cb4`。

### A1 进度条 60fps 重绘（P0，感知最大） ✅

**问题**：`video-player.tsx` 的 `timeupdate` 是唯一位置来源，浏览器只给约
4Hz 且间隔不均；`player-controls.tsx` 的已播段与圆点又没有任何过渡，于是圆点
每 250ms 跳一格。

**方案**：位置的**绘制**与位置的**状态**分家。

- 绘制：`player-controls.tsx` 里一个 rAF 循环直接写 DOM —— 已播段
  `style.width`、圆点 `style.left`。读数按优先级取：`dragging`（进度条拖动）
  → `overrideMs`（横滑落点/连按累积落点，父组件传）→ `video.currentTime`
  （配 `startMs` 换算成文件时间）。
- 写 DOM 必须**合帧**：照 emby-slider 的写法，排新帧前先
  `cancelAnimationFrame` 掉上一帧，保证一帧最多写一次。
- 状态：`positionMs` 这个 React state 保持 `timeupdate` 4Hz 不变，继续供
  时间文字、片尾卡片、上报、诊断面板用。**绝不在 rAF 里 setState**。
- 生命周期：`playing && !document.hidden` 才跑循环；暂停、seek、换会话各补画
  一次；`override` 存在时停循环、由 React 直接写一次。
- 缓冲段维持事件驱动（`progress`/`timeupdate`）：变化慢，60fps 是浪费。

**新增 prop**：`PlayerControls` 需要 `video`、`startMs`、`overrideMs`；
`positionMs` 语义收窄为「文字用的秒级位置」，注释写明。

**纯函数**：`lib/player/timeline.ts` 加 `progressRatio(positionMs, durationMs)`。

**验收**：§0 第 1 条。**风险**：iOS 低电量模式 rAF 降到 30fps（可接受）。

### A2 桌面微交互对齐（P2，顺手） ✅

圆点 `hover: scale(1.2)`、`active: scale(1)`（ArtPlayer 同款，按下有回弹）。
纯 CSS，跟 A1 一起做。

### B1 触屏双击左右 ±10 秒（P1） ✅

**要推翻一条旧决策**：`video-player.tsx:1691` 现在写着「触屏的双击就是两次
控制层开关，净效果回到原状」。双击左右快退快进已经是肌肉记忆，值得改，改完
在 `web-player.md §6.5` 记一笔。

**判定**：不用 `dblclick`（触屏上不稳），照 ArtPlayer 的做法自己数 tap，
300ms 窗口。画面横向三等分：左 1/3 双击 → `seekBy(-10)`；右 1/3 双击 →
`seekBy(+10)`；中 1/3 维持控制层开关（整块画面当暂停键的误触代价我们吃过）。

**单击不能被拖延**：第一次 tap 照旧立刻切控制层；第二次 tap 命中左右区时
触发 seek，并把控制层恢复到第一次点之前的状态，净效果就是只 seek。

**纯函数**：`lib/player/tap.ts` 的 `resolveTap({ nowMs, lastTapMs, xRatio })`。

### B2 长按倍速（P1） ✅

**触发**：按住画面 500ms（ArtPlayer 用 1000ms，偏迟钝）→ 倍速；松手/取消/
手指移动过门槛（说明是横滑）立即还原。

**倍数**：统一 **2×**，不做 3×。转码会话是边转边给的单向流，3× 必然追上
编码器，用户得到「快进两秒然后转圈」，比没有更糟。2× 也要带护栏：
`stall.ts` 判到 starve（缓冲耗尽）时自动退出倍速并提示一次。

**细节**：`preservesPitch = true`（Safari 还要 `webkitPreservesPitch`）；
长按结束要**吞掉那一次 click**；HUD 复用现有胶囊。

**纯函数**：`lib/player/hold-speed.ts` 状态机。

### B3 横屏锁屏（P1） ✅

横屏时控制层加一颗锁。`isLock` 为真时所有触摸手势（横滑、竖滑、tap、长按）
一律 return，控制层不再被唤出，只留解锁键悬浮 3 秒后淡出。锁屏优先级高于
`chromeMustStayVisible`。

### B4 连按快进合并成一次 seek（P0，新增） ✅

**问题**：`seekBy` 每次调用都立刻 `seekToFileMs`。转码会话下一次 seek 可能
就是一次「杀 ffmpeg → 换会话/重启直奔目标」，连按三次 ⟳10 就是三次重启，
画面黑三次才走到 +30 秒——这是「不丝滑」里最难受的一种。

**方案**（照 Jellyfin emby-slider 的 keyboardDragging）：`seekBy` 只累积
落点并立刻更新 UI，防抖窗口内没有新按键才真正提交一次 seek。

- 窗口取 **400ms**（Jellyfin 用 1000ms，单次快进要等 1 秒才动，太钝）。
- **判据是「这一跳贵不贵」**：有转码会话 **且** 落点不在已缓冲区间内才合并。
  实施时纠正过一次——最初按 `seekBeyondBufferedRestarts` 判，而 VOD 预生成
  列表下它恒为 false，可那条路上服务端**照样**会按分片请求把 ffmpeg 杀掉
  重启（只是不换会话而已），最常见的转码路径反而不会合并。同一条判据
  （`isCheapSeek`）也给 C2 的拖动跟随用：便宜就跟，贵就等松手。
- UI 侧现成：`flashSeek` 已经在累加显示（连按三下显示 ±30 秒），累积落点
  正好喂给 A1 的 `overrideMs`，进度条同步跟着走。
- 键盘 J/L/←/→ 与中央 ⟲10/⟳10 走同一条路。

**纯函数**：`lib/player/seek-batch.ts` —— 累积器 + 该不该防抖的判定。

**验收**：§0 第 4 条；转码会话下连按三次，服务端日志只出现一次重启。

### C1 进度条章节标记 + 气泡三合一（P1，数据现成） ✅

后端 `services/library/chapters.py` 已经算好有效章节，详情页与 Jellyfin 协议
都在用，播放器没画。

- **后端**：会话/决策响应加 `chapters: [{start_ms, title}] | null`，复用
  `_chapter_views` 的 `effective_chapters`。只带时间和标题，**不带图**
  （预览图由 trickplay 负责）；**合成章节（`synthetic`）不下发**——等距分段
  没有信息量，画出来就是一排毫无意义的竖条。
- **前端**：轨道上按 `start_ms/durationMs` 画竖条；把**章节名并进现有的
  trickplay 气泡**（Jellyfin 的气泡是「缩略图 + 章节名 + 时间」三合一，
  拖动时知道自己拖到了哪一段，比只有时间有用得多）。
- 不做「点竖条跳章节」：小目标在手机上点不中。

### C2 拖动实时跟随（P2，谨慎）✅

松手才提交是转码会话逼出来的规矩（拖动中每次 move 都跳会让服务端一路杀
ffmpeg 重启）。但跳转不要钱时没有理由不跟随：**档 0 直出**（整个文件随便跳）
或**落点已在缓冲里**（数据就在手上）就跟着手指走，其余情况维持松手提交。
节流 100ms：hls.js 在列表内跳转会取消在途分片请求，一秒跳六十次反而让缓冲
永远建立不起来。判据与 B4 共用 `isCheapSeek`。

### C3 回拖不再必然重取分片（P1，新增） ✅

`engine.ts` 的 `BACK_BUFFER_S = 30`：回拖超过 30 秒就要重新请求分片，转码
会话下还可能触发服务端重启——而回拖恰恰是最常见的操作（没听清、走神）。
Jellyfin 反着来：客户端 `backBufferLength: Infinity` + 服务端默认**不删分片**
（`EnableSegmentDeletion = false`），所以回拖零成本。

我们不必上 Infinity（长片内存确实会炸，那条注释是对的），但 30 秒太紧。
提到 **180 秒**（约 4Mbps × 180s ≈ 90MB，可接受），并在 QoE 里盯一段时间的
内存与卡顿指标；同时确认服务端会话目录内已产出的分片在会话存活期间不被清理
（`_enforce_disk_watermark` 的水位是否会误伤当前会话的历史分片）。

### D1 hls.js 解码错误就地恢复阶梯（P0，新增） ✅

**问题**：`engine.ts` 现在遇到 fatal `MEDIA_ERROR` 直接 `onFailed` → 降档。
用户看到的是画质掉一级 + 几秒黑屏，而这类错误（buffer append error、
bufferStalledError 后的解码器抽风）hls.js 自己往往就能救回来。

**方案**（照 jellyfin-web `handleHlsJsMediaError`）：

1. 第一次 → `hls.recoverMediaError()`（重建 SourceBuffer，不重新拉流，
   通常 1 秒内恢复）；
2. 3 秒内又来 → `hls.swapAudioCodec()` + `recoverMediaError()`（音频编解码
   选错是这类错误的常见成因）；
3. 再来 → 才 `onFailed` 走降档。

与 `stall.ts` 的关系要理清：`decode-stalled` 判定（8 秒没进展且缓冲充足）
仍然走降档，但它的**前面**多了这两级便宜的自救；两条路都要记 QoE 事件，
否则「恢复了几次」这件事在遥测里彻底看不见。

### D2 direct 档的错误重挂（待评估，先不做）

ArtPlayer 的 `video:error` 重连（5 次、间隔 1s）在我们这儿大部分被降档回路
和心跳自愈覆盖。存疑的只有 direct 档（无会话）下的一次性网络抖动：现在是不是
直接降档而不是原地重挂一次？读完 `machine.ts` 的 error 分支再定。

### F 小项集合（P2，攒一批一起做）

- ✅ **起播 seek 收紧**：目标与当前差 <1 秒就不 seek。VOD 列表里流本来就从
  目标分片起，再赋一次 `currentTime` 会打断刚建立的缓冲，首帧要多等一拍。
- ✅ **暂停时逐帧步进**：`.` / `,` 各走一帧，帧率取台账 `source.frame_rate`、
  未知按 24 兜底（宁可一次走得偏少——多按一下总比跳过想看的那一帧强）。
- ⏸ **快退/快进步长可配置**（Jellyfin 默认后退 10 / 前进 30 且可改）：**暂缓**。
  它的血脉比看上去长——中央簇那两颗按钮的图标把「10」直接画进了 SVG，还牵动
  aria-label、键盘映射、双击跳转四处取值。为一个 P2 便利项改图标与四处调用，
  不如等倍速菜单一起做（那时设置面板本来就要动）。
- ⏸ **高码率下调小前向缓冲**（jellyfin-web 在 ≥25Mbps 时把 `maxBufferLength`
  从 30 压到 6，规避 hls.js#876）：**按本条原定的条件暂缓**——先在 QoE 里看
  有没有对应现象，有再加这个分支。凭别人的阈值给自己的播放路径加条件分支，
  等于把一个没验证过的假设写进代码。

### E 明确不做

- **迷你进度条**：`web-player.md` 已拍板全出全收，不因为别人有就翻案。
- **3× 倍速**：见 B2。
- **服务端 ffmpeg 节流**（Jellyfin `TranscodingThrottler`：客户端领先 60 秒
  就给 ffmpeg 发暂停键）：Jellyfin 自己在 10.11 的迁移里把它**默认关掉**了
  （`DisableTranscodingThrottling`，理由是「对某些格式是坏的」）。我们用
  `-readrate 1.5` 达到同样目的且没有暂停/恢复的状态机，不引入。
- 截图 / 弹幕 / 画面翻转 / 长宽比：与本项目定位无关。

## 3. 落地顺序

| 批次 | 内容 | 影响面 | 为什么这样分 |
|------|------|--------|-------------|
| PR1「跟手」 | A1 + A2 + B4 | 前端两个文件 + 一个纯函数 | §0 的 1、4 两条，感知最大、风险最低 |
| PR2「稳」 | D1 + C3 | `engine.ts` | 都在引擎层，一起改一次错误/缓冲策略 |
| PR3「手势」 | B1 + B2 + B3 | 前端触摸接线 | 三条共用同一套触摸接线，一次改完 |
| PR4「信息」 | C1（含后端字段）+ C2 + F | 前后端 | 后端只加响应字段，无迁移 |

发布约束（`CLAUDE.md` 三条硬约束）核对：**不动运行时依赖**（无新 npm 包、
无 Dockerfile 变更）→ 不需要 bump `docker/runtime-version`；**无数据库迁移**；
**不新增 `data/` 目录**。PR4 动了 API 响应体，要同步更新 `tests/api` 的契约断言。

## 4. 测试

纯函数进 `apps/web/test/`（`node --test`，与现有播放器测试同规格）：

- `progressRatio`：片长未知/越界/正常。
- `resolveTap`：窗口内外、三个分区、连点三次。
- `hold-speed` 状态机：长按成立、移动取消、松手还原、starve 强制退出。
- `seek-batch`：连按累积、窗口内不提交、direct 模式不防抖。
- 章节竖条位置：越界夹取、`synthetic` 过滤。

接线（rAF、触摸事件、hls 错误阶梯）不写组件测试，靠真机检查单：
iOS Safari / Android Chrome / 桌面 Chrome × 直通档 / 转码会话，各跑 §0 六条。
D1 另需一条构造用例：用坏样本触发 `MEDIA_ERROR`，确认先恢复、不降档。

## 5. 对既有文档的修订

- `web-player.md §6.5`：双击快进、长按倍速、播放速率从 P1 移入已实施；
  章节标记从 P2 移入；触屏双击那条语义改写（B1 推翻了旧决策）。
- `web-player.md §6.3`：降档回路前面多了 D1 这两级自救，判定顺序要补一句。

## 6. 参考源码结论

### 6.1 ArtPlayer（2026-09-08）

四条并入 §2：rAF 重绘（A1）、长按倍速（B2）、自己数 tap 的单/双击判定（B1）、
锁屏（B3）。小的三条：`preload` 在 Safari 用 `auto` 其余 `metadata`；所有跳转
走单一入口（我们的 `seekToFileMs` 已经是）；notice 统一 2 秒节流（我们的
两段式退场更细）。

不学的：它的 seek 就是 `currentTime = clamp(...)` 一行，没有会话/降档概念。

### 6.2 Jellyfin + jellyfin-web（2026-09-08）

**客户端（jellyfin-web）**，四条新条目：

- `handleHlsJsMediaError` 的三级恢复阶梯 → **D1**（我们缺得最明显的一条）。
- emby-slider 的 `KeyboardDraggingTimeout = 1000`：键盘调整只改本地值，
  静默 1 秒后才发一次 `change` → **B4**。
- `backBufferLength: Infinity` → **C3**（我们 30 秒太紧）。
- 气泡「缩略图 + 章节名 + 时间」三合一 → 并入 **C1**。

佐证既有决策的两条：`setBufferedRanges` 只画包含播放头的那一段（与我们
「只认播放头所在那段连续缓冲」的注释同一个理由）；进度条的 DOM 写入全部
放进 rAF 并 `cancelAnimationFrame` 合帧（A1 的实施细节照抄）。

注意**它并不是 60fps**：`updateValues` 由 `timeupdate` 事件和一个 100ms
`setInterval` 驱动，rAF 只是合帧不是驱动源。所以进度条平滑这件事，
ArtPlayer 的 `USE_RAF` 才是范本，Jellyfin 不是。

**服务端（jellyfin）**，`DynamicHlsController.GetDynamicSegment` 是核心：

| 请求的分片 | Jellyfin 的处置 | 我们的处置 |
|-----------|----------------|-----------|
| 已在磁盘上 | 直接给（回拖不重启） | 同（`completed_segments` 台账） |
| 早于当前转码头 | 杀掉重启直奔该段 | 同（带 `_PROBE_GRACE_S` 宽限，防 iOS 的 seg0 连发） |
| 超前转码头 ≤ 24 秒 | **等它转过来** | 超前 >1 段就重启 |
| 超前 > 24 秒 | 杀掉重启直奔该段 | 同 |

差异只在「超前多少才值得重启」：Jellyfin 24 秒，我们 1 段（约 4 秒）。
我们的注释里有实测理由——`-readrate 1.5` 限速下等转码追 n 段要 n×4/1.5 秒
（最长 16 秒），而重启直奔实测 1~3 秒。**这是被 readrate 逼出来的取舍**，
留一个待验证的实验：seek 之后短暂解除 readrate 让 ffmpeg 冲刺，追上再回到
1.5——那样短距离前跳就可以「等」而不必重启，省掉一次前向缓冲的丢弃。
不排期，等 A1/B4 落地后看 QoE 里前跳的实际分布再说。

分片就绪判据上我们更准：Jellyfin 用「本片存在**且**下一片也存在」来绕开
「ffmpeg 正在写这一片」，我们直接读 `live.m3u8` 台账（远端走原子替换），
不靠下一片的存在来推断。

另外两条只作记录、不改：Jellyfin 默认**不删分片**（`EnableSegmentDeletion
= false`，开了也保 720 秒），配合客户端 Infinity 回退缓冲，是他们回拖顺滑的
根本原因（对应我们的 C3）；`TranscodingThrottler` 已被他们自己默认关掉，
见 §2.E。

## 7. 端到端验收（2026-09-08）

真浏览器 + 真视频 + 真后端跑通，脚本落在 `scripts/perf/e2e_player_feel.py`
（Playwright + 本机 Chromium，用法见文件头）。

**样片**：ffmpeg 合成 120 秒 480×270、VP9 + Opus、每 2 秒一个关键帧、内嵌三个
章节（0 / 40 / 80 秒）。**为什么不用 H.264**：Playwright 的 Chromium 不含专有
解码器（`canPlayType('video/mp4; codecs="avc1…"')` 返回空串），VP9 + Opus 封进
mp4 才能在这台浏览器里真的出画面，同时仍然走档 0 直出这条路。

一次实跑：

```
首帧 1032 ms（rVFC）
✓ 进度条匀速：1.2 秒内 73/73 帧取值不同
✓ 双击前进十秒：+11.28 秒
✓ 长按倍速：按住 2× → 松手 1×，保音高 True
✓ 拖动跟手：拖动中画面走到 [36.0, 42.1, 48.1]
✓ 章节刻度：['33.3333%', '66.6667%']，气泡 ['中段', '1:00']
✓ 逐帧步进：暂停后走了 0.042 秒
```

第一条是本次改动的直接度量：跟着 `timeupdate` 走时 1.2 秒内只有个位数个不同
取值，每帧自绘则等于采样数本身。章节刻度只有两条——0 秒那条按 C1 的规则不画。

**转码 / HLS 那条路不在浏览器里验**（同一个原因：Chromium 解不了 H.264），
改走真 HTTP + 真 ffmpeg：起一路档 1 remux 会话 → 拉 VOD 列表（30 个分片、
覆盖全片 120 秒）→ **直接请求最后一个分片**（模拟拖到片尾，走服务端「按分片
请求杀掉 ffmpeg 重启直奔目标」那条路），188 毫秒返回 200 → 把 init + 30 个
分片顺序拼起来，`ffprobe` 解出 120.05 秒的 h264 + aac，`ffmpeg -f null` 全片
解码零报错。

两条如实记录：`tests/api/test_playback_e2e.py` 的四个用例在本容器里等分片
超时，**同样的失败在改动前的基线上复现**，是环境问题不是回归（上面那条真
HTTP 路径覆盖了它们要验的东西）；D1 的自救阶梯要构造坏流才能触发
`MEDIA_ERROR`，本轮只有纯函数单测覆盖，标为真机待验。
