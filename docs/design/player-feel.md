# 播放器手感（丝滑度）改进计划

> 本文只谈**手感**：进度条跟不跟手、点一下有没有反应、拖动读数对不对得上。
> 播放能不能起来、降档、字幕、会话生命周期在 `web-player.md`，两者互不覆盖。
>
> 参考源码逐个过，每过一个在 §6 追加一节结论，再回头改 §2 的优先级。
> 已过：ArtPlayer（`zhw2590582/artplayer`，2026-09-08）。

## 0. 验收标准（先定标准再动手）

「丝滑」必须能被观察，否则改完只能靠感觉吵架。五条：

1. **进度条匀速**：播放中圆点每帧走一次，不是每 250ms 跳一格。录屏 60fps
   逐帧看，圆点位移应连续；React DevTools 里播放中的 re-render 次数**不因此
   项增加**（仍是 `timeupdate` 的 4Hz）。
2. **一个位置只有一个读数**：任何时刻屏幕上的落点胶囊、进度条、时间文字
   三者同源（2026-09-08 已修，见 §2.A0）。
3. **点一下必有反应**：单击 → 控制层开关；双击 → 明确的第二种动作；长按 →
   倍速。三者互不误触发，且单击的反馈不被双击判定拖延 300ms。
4. **拖动跟手**：直通/VOD 模式下拖动进度条时画面跟随；转码会话下松手才跳，
   但拖动全程有缩略图 + 时间。
5. **横屏不误触**：锁屏后擦屏幕、握持不会暂停/跳转。

## 1. 现状对照（ArtPlayer 第一轮）

| 能力 | ArtPlayer | movieclaw 现状 | 差距 |
|------|-----------|---------------|------|
| 进度条刷新 | `USE_RAF` 每帧直接写 DOM（`events/updateInit.js`、`control/progress.js`） | 只有 `timeupdate` → `setPositionMs`，width/left 无过渡 | **大**，见 A1 |
| 拖动中读数 | 手机拖动显示时间气泡 + 缩略图；横滑同步 `setBar('played')` | 已同源（A0 已修）；缩略图靠 hover | 小 |
| 拖动实时 seek | 桌面 `mousemove` 直接 `art.seek` | 一律松手提交 | 中，见 C2 |
| 双击 | 自己数 tap（`clickInit.js`），桌面双击全屏、移动端双击播放暂停 | 桌面 `onDoubleClick` 全屏；触屏双击=两次控制层开关（净零） | 中，见 B1 |
| 长按倍速 | `plugins/fastForward.js`，1s → 3× | 无，`playbackRate` 全站未出现 | **大**，见 B2 |
| 锁屏 | `plugins/lock.js`，所有手势判 `isLock` | 无 | 中，见 B3 |
| 章节标记 | `option.highlight` 竖条 + 文字气泡 | 后端有章节，播放器没画 | 中，见 C1 |
| 错误重连 | `video:error` 最多 5 次、间隔 1s 重挂 | 会话自愈 + 降档（更强），direct 档未验证 | 待评估，见 D |
| 迷你进度条 | 插件可选 | 已决策否掉（Netflix 派全出全收） | **不做** |
| seek 规划 / 停顿归因 / QoE / 降档 | 无 | `timeline.ts` / `stall.ts` / `qoe.ts` / `machine.ts` | 我们更厚，不拉平 |

`web-player.md §6.5` 早把「双击快进、上下滑音量/亮度、长按倍速」「播放速率」
列在 P1、「章节标记」列在 P2 —— 本计划就是把这几条落地，不是新开战场。

## 2. 分项方案

### A0（已完成，2026-09-08）拖动读数同源

横滑落点驱动进度条；胶囊在手势被第二根手指/换元素打断时收走；片长转未知时
清掉进度条的 `dragging`。提交 `1597cb4`。

### A1 进度条 60fps 重绘（P0，感知最大）

**问题**：`video-player.tsx` 的 `timeupdate` 是唯一位置来源，浏览器只给约
4Hz 且间隔不均；`player-controls.tsx` 的已播段与圆点又没有任何过渡，于是圆点
每 250ms 跳一格。这是「不够丝滑」最主要的来源。

**方案**：位置的**绘制**与位置的**状态**分家。

- 绘制：`player-controls.tsx` 里一个 rAF effect 直接写 DOM ——
  已播段 `style.width`、圆点 `style.left`。读数来源按优先级：
  `dragging`（进度条拖动）→ `overrideMs`（横滑落点，父组件传）→
  `video.currentTime`（配 `startMs` 换算成文件时间）。
- 状态：`positionMs` 这个 React state 保持 `timeupdate` 4Hz 不变，继续供
  时间文字、片尾卡片、上报、诊断面板用。**绝不在 rAF 里 setState**——
  这个组件重，60fps 重渲染扛不住。
- 生命周期：`playing && !document.hidden` 才跑循环（`document.hidden` 时
  rAF 本就自动停，这里显式判一次省掉恢复瞬间的抖动）；暂停、seek、换会话
  各补画一次；`override` 存在时停循环、由 React 直接写一次。
- 缓冲段维持事件驱动（`progress`/`timeupdate`）：它变化慢，60fps 是浪费。

**新增 prop**：`PlayerControls` 需要 `video`（元素）、`startMs`、`overrideMs`。
`positionMs` 的语义收窄为「文字用的秒级位置」，注释里写明。

**纯函数抽出**（进单测）：`lib/player/timeline.ts` 加
`progressRatio(positionMs, durationMs)`（clamp 到 0~1，片长未知返回 0）。

**验收**：录屏逐帧圆点连续；播放 10 秒内 re-render 次数与改动前一致。

**风险**：iOS 低电量模式 rAF 降到 30fps（可接受，仍远好于 4Hz）；
`prefers-reduced-motion` 不受影响——这是位置更新，不是装饰动画。

### A2 桌面微交互对齐（P2，顺手）

圆点 `hover: scale(1.2)`、`active: scale(1)`（ArtPlayer 同款，按下有回弹）；
我们现在只有 dragging 放大一档。纯 CSS，跟 A1 一起做。

### B1 触屏双击左右 ±10 秒（P1）

**要推翻一条旧决策**：`video-player.tsx:1691` 现在写着「触屏的双击就是两次
控制层开关，净效果回到原状」。YouTube/B 站的手机端双击左右快退快进已经是
肌肉记忆，值得改，改完在 `web-player.md §6.5` 记一笔。

**判定**：不用 `dblclick`（触屏上不稳），照 ArtPlayer 的做法自己数 tap，
300ms 窗口。分区按画面横向三等分：

- 左 1/3 双击 → `seekBy(-10)`；右 1/3 双击 → `seekBy(+10)`；
- 中 1/3 双击 → 维持控制层开关（不做播放/暂停：整块画面当暂停键的误触
  代价我们已经吃过，见 `onSurfaceClick` 的注释）。

**单击不能被拖延**：第一次 tap 照旧立刻切控制层（延迟 300ms 等双击会让
「点一下唤出控制层」变迟钝）；第二次 tap 命中左右区时触发 seek，并把控制层
**恢复到第一次点之前的状态**，净效果就是只 seek。

**复用**：`seekBy` 与 `seekFlash` 胶囊都现成，且 `flashSeek` 已有同方向连点
累加（连点三下显示 ±30 秒，YouTube 同款）。

**纯函数**：`lib/player/tap.ts` —— `resolveTap({ nowMs, lastTapMs, xRatio })`
→ `"chrome" | "seek-back" | "seek-forward"`，进单测。

### B2 长按倍速（P1）

**触发**：手指按住画面 500ms（ArtPlayer 用 1000ms，偏迟钝；B 站量级是
400~500ms）→ 倍速播放；松手/取消/手指移动过门槛（说明是横滑）立即还原。

**倍数**：统一 **2×**，不做 3×。转码会话是边转边给的单向流，3× 必然追上
编码器，用户得到的是「快进两秒然后转圈」——那比没有这个功能更糟。2× 也要
带一条护栏：`stall.ts` 判到 starve（缓冲耗尽）时自动退出倍速并提示一次。

**细节**：`preservesPitch = true`（Safari 还要 `webkitPreservesPitch`），
否则 2× 是鸭子叫；长按结束要**吞掉那一次 click**，不然松手会顺带切控制层；
HUD 复用现有胶囊（「2× 快进中 ▶▶」）。

**纯函数**：`lib/player/hold-speed.ts` 的状态机（idle → pending → active →
released，含「移动超阈值取消」），进单测。

### B3 横屏锁屏（P1）

横屏时控制层右侧加一颗锁。`isLock` 为真时：所有触摸手势（横滑、竖滑、
tap、长按）一律 return，控制层不再被唤出，只留解锁键悬浮 3 秒后淡出。
与 `chromeMustStayVisible` 的关系要写清：锁屏优先级最高，暂停中也不强制
展开控制层。

### C1 进度条章节标记（P1，数据现成）

后端 `services/library/chapters.py` 已经算好有效章节（内嵌优先、按时长合成
兜底），详情页与 Jellyfin 都在用，播放器没画。

- **后端**：会话/决策响应（`schemas/playback.py`）加
  `chapters: [{start_ms, title}] | null`。只带时间和标题，**不带图**——
  雪碧图预览已经由 trickplay 负责，章节图会把响应撑大好几倍。
  复用 `_chapter_views` 里的 `effective_chapters`。合成章节（`synthetic`）
  是等距分段、没有信息量，**不下发**，否则进度条上一排毫无意义的竖条。
- **前端**：`player-controls.tsx` 在轨道上按 `start_ms/durationMs` 画竖条；
  hover/拖动到附近时把章节名并进现有的时间气泡（定位逻辑复用 trickplay 的
  边界夹取）。
- 不做「点竖条跳章节」：进度条上的小目标在手机上点不中，跳转靠拖。

### C2 直通/VOD 模式下拖动实时 seek（P2，谨慎）

`playback-mode.ts` 已经把「越界要不要换会话」抽成了
`seekBeyondBufferedRestarts`。它为 false（档 0 直出、VOD 全片列表）时，seek
就是播放器内跳转，没有杀 ffmpeg 的代价——这时可以在拖动中节流 100ms 实时
seek，手感立刻不一样。为 true 的旧会话相对制**维持松手提交**。

风险：hls.js 在 VOD 列表内跳转会取消在途分片请求，频繁拖动反而更慢。
所以节流 + 仅当 `video.seekable` 覆盖目标时才执行，否则退回松手提交。

### D 直通档的错误重挂（待评估，先不做）

ArtPlayer 的 `video:error` 重连（最多 5 次、间隔 1s）在我们这儿大部分被
`machine.ts` 的降档回路和心跳自愈覆盖了。**唯一存疑**的是 direct 档
（无会话）下的一次性网络抖动：现在是不是直接降档、而不是原地重挂一次？
读完 `machine.ts` 的 error 分支再定，不拍脑袋加重试。

### E 明确不做

- **迷你进度条**：`web-player.md` 已拍板全出全收，不因为 ArtPlayer 有就翻案。
- **3× 倍速**：见 B2。
- 截图 / 弹幕 / 画面翻转 / 长宽比：与本项目的定位无关。

## 3. 落地顺序

| 批次 | 内容 | 影响面 | 备注 |
|------|------|--------|------|
| PR1 | A1 + A2 | 纯前端，两个文件 | 感知最大、风险最低，先做 |
| PR2 | B1 + B2 + B3 | 前端手势层 | 三条都在同一套触摸接线上，一起改一次接线 |
| PR3 | C1（含后端字段）+ C2 | 前后端 | 后端只加响应字段，无迁移 |

发布约束（`CLAUDE.md` 三条硬约束）核对：**不动运行时依赖**（无新 npm 包、
无 Dockerfile 变更）→ 不需要 bump `docker/runtime-version`；**无数据库迁移**；
**不新增 `data/` 目录**。C1 动了 API 响应体，要同步更新 `tests/api` 里的契约断言。

## 4. 测试

纯函数进 `apps/web/test/`（`node --test`，与现有播放器测试同规格）：

- `progressRatio`：片长未知/越界/正常。
- `resolveTap`：窗口内外、三个分区、连点三次。
- `hold-speed` 状态机：长按成立、移动取消、松手还原、starve 强制退出。
- 章节竖条位置：越界章节夹取、`synthetic` 过滤。

接线（rAF、触摸事件、DOM 写入）不写组件测试，靠真机检查单：
iOS Safari / Android Chrome / 桌面 Chrome × 直通档 / 转码会话，各跑
§0 那五条验收。

## 5. 对既有文档的修订

- `web-player.md §6.5`：双击快进、长按倍速、播放速率从 P1 移入已实施；
  章节标记从 P2 移入；触屏双击那条语义改写（B1 推翻了旧决策）。
- 本文件 §6 逐个参考源码追加结论。

## 6. 参考源码结论

### 6.1 ArtPlayer（2026-09-08）

值得学的四条已并入 §2：rAF 重绘（A1）、长按倍速（B2）、自己数 tap 的
单/双击判定（B1）、锁屏（B3）。另外三条小的：`preload` 在 Safari 用 `auto`
其余 `metadata`；所有跳转走 `art.seek` 单一入口（我们的 `seekToFileMs` 已经
是）；notice 统一 2 秒节流（我们的胶囊已有两段式退场，更细）。

不学的：它的 seek 就是 `currentTime = clamp(...)` 一行，没有会话/降档概念；
`switchQuality` 的保位恢复我们已有等价实现（`pendingFileMs` + restart）。
