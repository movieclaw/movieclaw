# Agent 运行时稳定性方案（停机 / 崩溃 / 取消的恢复语义）

> 覆盖三个场景：①停机升级（优雅关闭）②故障异常停机（crash / kill -9 /
> 断电）③用户主动停止。目标是给每个场景一个**明确、可验证的恢复语义**，
> 并修掉现状中的真实缺口。
> 相关既有设计:`agent-context-compaction.md`（转录格式）、
> `in-app-update.md`（升级重启机制）。

## 0. 一句话结论

现有架构的地基是对的——**append-only 转录做事实源 + 心跳自愈的运行标记 +
易失的进程内事件日志**，三场景的恢复框架都已存在。本方案不推翻任何机制，
只做四件事：修一个会让会话**永久报错**的 crash 缺口（P0）、修一个取消收尾
的竞态（P0）、收割孤儿子进程（P1）、把三场景的恢复语义补齐到「用户视角
无歧义」（P1）。

## 1. 现状盘点：哪些已经是对的

| 机制 | 位置 | 作用 |
|---|---|---|
| append-only JSONL 转录，只落定稿消息 | `agent_sessions.py` | 事实源；崩溃最多丢「未定稿的半条」，已有内容永不损坏 |
| 逐条定稿即落盘（assistant/tool/压缩行） | `runner._notify` → `recorder.on_message` | 运行中途的每一步完成即持久，crash 丢失面最小化 |
| 先写文件、后更 DB 的固定顺序 + 启动 `rebuild_agent_session_index` | `recorder.py` | 两步写入间崩溃 → 启动自愈校准索引 |
| 心跳（10s）+ 超时窗（30s）的运行标记 | `repo.is_running` | 不持久化静态 status；进程硬崩后 30s 状态自愈为「已结束」，无需任何清理代码 |
| 运行终态收尾：停心跳 → `seal_pending_tool_calls` 补配对 → 清 `active_run_id` | `recorder.on_terminal` | 取消/出错后转录保持「tool_calls 与回执配对完整」，resume 直接回喂 API |
| 进程内事件日志（可回放、带序号）明确**易失** | `agent_runs.py` | SSE 断线重连回放；重启即丢是设计选择，恢复依据是转录不是事件 |
| 关闭顺序：IM → jobs → **Agent registry**（cancel + gather 等待）→ 下游客户端/DB | `lifespan.py` | 优雅停机时 Agent 在依赖释放前先停 |
| retry 的 `discard_from_user_message` 整文件原子重写 | `agent_sessions.py` | 改写历史的唯一例外也做到了「崩溃只会旧文件完好或新文件完整」 |
| 前端 SSE 404 兜底文案「服务已重启，请重新发起」 | `agent-conversations.tsx` | 事件史丢失后用户有明确出路 |
| 压缩落盘失败降级为告警 | `runner._notify_compaction` | 转录退化为「未压缩但仍合法」的上下文，一致性不破 |

## 2. 核心不变量（方案的纲）

后续所有修改都为维护这三条：

1. **转录随时可回喂**：任何时刻读 `build_history()`，得到的都是 OpenAI
   兼容 API 接受的合法上下文（tool_calls 全配对、无残缺结构）。
2. **运行状态以心跳为准**：`active_run_id` 只是意图声明，真伪由
   `last_heartbeat_at` 裁决；任何进程死法最多 30 秒后状态自愈。
3. **事件日志易失、转录持久**：崩溃后 UI 的恢复路径永远是「重读转录」，
   不依赖事件史；因此转录必须自含「这轮怎么结束的」信息（见缺口 C）。

## 3. 差距分析：六个真实缺口

### 缺口 A（P0）：crash 遗留孤儿 tool_call ⇒ 会话永久不可用

`seal_pending_tool_calls` 只挂在 `on_terminal`，而 crash（kill -9、断电、
OOM）没有 on_terminal。时序：assistant(tool_calls) 已定稿落盘 → 工具执行中
进程死亡 → tool 回执永远缺失。下次发消息 `build_history` 把孤儿 tool_calls
原样投影给模型 → OpenAI 兼容端点 **400**（assistant 的 tool_calls 必须跟
tool 响应）→ **该会话之后每次发消息都失败**，用户没有任何自救手段（除了
放弃会话）。这是三场景中唯一「不自愈且越陷越深」的缺口。

### 缺口 B（P0）：取消收尾与 runner 落盘的竞态

`registry.cancel()` 的顺序是：先 publish `agent_cancelled`（→ 立即触发
`on_terminal` → seal）→ 再 `task.cancel()`。seal 执行时 runner 协程还活着：
若它恰好刚执行完一个工具、正要 `on_message` 落盘 tool 回执，就会出现
**seal 的合成回执与真实回执同 tool_call_id 各一条**——下次回喂，同一
tool_call_id 两条 tool 消息，兼容端点行为未定义（部分 400）。
（cancel 先写事件的动机是对的：task 可能从未被调度、没机会自己补终态
事件——要保留这个保证，换一个不竞态的实现。）

### 缺口 C（P1）：中断没有可见标志，三种结束不可区分

`finish_reason="aborted"` 在转录格式注释里承诺了、但**没有任何代码写入**。
被取消/被停机打断的运行，转录里唯一痕迹是补配对回执的固定文案；纯文本
输出阶段被打断则**毫无痕迹**（流式半成品不落盘）。后果：
- 重启后用户打开会话，分不清「模型答完了」和「答到一半被打断」；
- 续聊时模型也分不清，可能把半途状态当成已完成。

### 缺口 D（P1）：合成回执文案的副作用语义错误

现文案「操作已被中断，工具未执行完成。」断言了「未执行」。crash/取消
场景的真相是**结果未知**：mclaw 提交下载、创建订阅这类副作用工具可能
已经生效、只是回执没落。模型读到「未执行完成」可能直接重发同一调用 →
**重复下单**。文案必须表达「已中断、结果未知、如需确认请先查询状态」。

### 缺口 E（P1）：孤儿 bash/mclaw 子进程

`task.cancel()` 打断 `proc.communicate()` 的 await，但 cancel 路径没有
`proc.kill()`（只有 timeout 分支有）。用户取消/优雅停机时，正在跑的
bash 命令（如一个 ffmpeg）**继续在后台跑完**；crash 场景子进程被 init
收养。Docker 部署整容器重启会连带回收，裸机源码部署会真遗留。

### 缺口 F（P2）：crash 后 30 秒的「假运行中」窗口

心跳超时前 `is_running=True`：用户此时发消息被拒（「已有正在进行的
运行」），前端也显示运行中。30 秒自愈，属可接受的设计代价，但拒绝文案
可以更诚实（提示稍候重试）。

## 3.5 参照系调研：pi 与 maka 怎么处理同类问题

出方案前把两家的中断/恢复实现读了一遍，结论与吸收点：

**pi 现行版**（`packages/agent/src/agent-loop.ts`、`ai/src/api/*`）：

- **协作式取消**：AbortSignal 贯穿整个 loop（传给流式请求与每个工具），
  abort 后 loop **自己走完收尾再返回**——流式中的 assistant 以
  `stopReason: "aborted"` **连同半截内容定稿落盘**；工具批次里未执行的
  调用在循环内直接补 `Operation aborted` 错误回执。收尾发生在运行协程
  内部，**结构上不存在外部收尾竞态**——印证并强化了我们 4.1 的方向；
- **半截 assistant 落盘**是我们没有的：pi 里「用户看到的」与「转录存的」
  永远一致，续聊时模型也知道自己说到哪被打断（吸收进 4.3）；
- crash 遗留孤儿：pi 的加载修复只处理 JSONL 半行截断，孤儿 toolCall
  **没有读取侧修复**（本地单用户 CLI，容忍度高）——我们的双保险 seal
  在这点上比它完备；
- pi harness 规格（下一代，`docs/harness.md` §4）把同一思想推到极致：
  崩溃后重开发现 `effect_pending` → 按既定策略重试或**以合成错误封顶**，
  `getLastResult()` 作为崩溃后对账口——「合成收尾而非自动续跑」是
  两代一致的选择，佐证我们 4.6 的第一条。

**maka**（`runtime/src/agent-run-recovery.ts`、`ai-sdk-backend.ts`、
`docs/session-task-ledger-lifecycle.md`）：

- **启动恢复分类器** `classifyAgentRunRecovery`：对非终态 run 按最后
  一个事件分类（`tool_interrupted` / `stale_user_wait` /
  `model_stream_completed_without_terminal` / `run_interrupted`…），统一
  判 `failed` + `failureClass: app_restarted` + 结构化诊断——比我们
  「一刀切 seal」多一层可观测性（吸收进 4.2 的日志分级）；
- **读取侧成对丢弃**：回放层把「无回执的调用」与「无调用的回执」成对
  丢弃（Anthropic 400 防御），记 non-blocking 诊断。maka 走读取侧修复
  路线；我们坚持写入侧 seal（转录自含答案、无需每次读时修），但把
  「成对丢弃」吸收为第三层最后防线（4.2）；
- **resumeTrust 分级**（task ledger）：中断时活跃的任务标 `stale`、证据
  缺失标 `needs_revalidation`、修复过的标 `repaired`，untrusted 不进
  模型视野；任务完成必须携带 `completionEvidence`。这是「副作用结果
  未知 → 引导核实」思想的重型版，验证 4.3 文案方向；其完整机制服务于
  maka 的多任务账本，我们的场景用文案分级即够。

**取舍总结**：pi 证明协作式「循环内收尾」是竞态问题的根治解；maka 证明
启动分类诊断与读取侧防线的价值。我们的落点：**收尾进协程 finally（pi 的
效果、最小改动）+ 写入侧 seal 双保险 + 读取侧修复兜底（maka 思路）+
半截落盘与 aborted 标志（pi）**。

## 4. 方案

### 4.1 终态收尾时序重构（修缺口 B，兼护 A）

把「补配对 + 清标记」从「第一个终态事件触发」改为「**运行协程真正结束后
执行**」，从根上消灭 seal 与落盘的并发：

- `registry._execute` 增加 `finally`：无论 done / error / cancelled，
  协程退出前**由协程自己**调用 `on_terminal`（此刻 runner 必然不再写）；
- `cancel()` 保持「先 publish 终态事件、再 task.cancel()」——事件时序
  保证不变（订阅者不会永久等待）；`on_terminal` 不再由 publish 触发，
  而是等 task 的 finally；「task 从未调度」的场景，cancel() 后协程首次
  被调度即抛 CancelledError → finally 照跑收尾；
- `registry.close()` 现有的 gather 语义自动升级为「等全部收尾完成」——
  优雅停机保证每个运行补配对、清标记后才放行进程退出；给 gather 加
  总超时（如 10s），超时强制放行并记错误日志（收尾靠启动自愈兜底），
  避免一个卡死的收尾拖死整个停机。

recorder 侧无需改动（`_lifecycle_lock` + `_terminated` 已保证幂等与
begin/terminal 有序）。

**取消看门狗（实现期补充）**：压测中偶发首次 `task.cancel()` 的
CancelledError 在深层 await（wait_for/子进程管道）的取消竞态中被吞，
任务卡住不进 finally——收尾悬着、会话对外要等 30 秒心跳超时才显示结束。
`cancel()` 现在会派出一个看门狗任务：每 3 秒检查一次，任务仍未停下就
再投递一次取消（至多 3 次，仍不停则记错误日志交由停机/启动自愈兜底）。
同时防御第三方工具代码捕获 CancelledError 后不重新抛出的情况。

### 4.2 启动自愈 seal + 接受路径防御（修缺口 A）

双保险，两处都幂等：

1. **启动自愈**：`rebuild_agent_session_index` 扫描时（本就逐文件读了
   全量 entry），对尾部存在未配对 tool_call 的会话调用
   `seal_pending_tool_calls`（crash 文案，见 4.3）。正常运行的部署里
   命中数恒为 0，只有上次异常停机才有活干。日志按中断位置分类记录
   （maka `classifyAgentRunRecovery` 的轻量版）：死在工具执行中 /
   流式输出中 / 刚启动——一行中文日志带会话号与分类，排查停机原因时
   不用翻转录；
2. **接受路径防御**：`_accept_user_message` / retry 在 `build_history`
   之前调用 `seal_pending_tool_calls`（已是幂等函数，无孤儿时零写入）。
   兜住「运行中 crash → 30 秒心跳窗过后、进程没重启，用户直接续聊」的
   路径——此时启动自愈还没跑过；
3. **读取侧最后防线**（借 maka 的读取侧防线思路，落地取修复而非丢弃）：
   `build_history` 投影时若仍发现未配对的 tool_call（前两层生效后理论
   不可达），复用交接快照的修复逻辑在内存中补「结果未知」回执 / 降级
   孤立回执，并记错误日志——比成对丢弃多保住一步历史，同样绝不把必
   400 的上下文发向供应商。只改投影不回写文件。防御纵深，不是主路径。

三层生效后，不变量 1（转录随时可回喂）在任何死法下成立，「会话永久
400」被彻底消灭。**用户在任一场景后都可以直接再次发消息运行**。

### 4.3 中断标志与合成回执文案分级（修缺口 C、D）

`seal_pending_tool_calls` 增加 `reason` 参数，文案按场景分级，并同时
落一个可见的中断标志：

- **用户取消**（on_terminal，event=`agent_cancelled`）：
  「用户停止了本次运行，此工具调用被中断。它可能已产生部分效果；如需
  确认实际结果，请先用查询类操作核实，不要盲目重发。」
- **服务中断**（启动自愈 / 接受路径兜底 / 停机取消）：
  「服务在工具执行期间重启，此调用的结果未知。继续任务前请先查询相关
  状态确认它是否已生效，避免重复执行有副作用的操作。」

**半截 assistant 落盘**（借鉴 pi 的 `stopReason: "aborted"` 语义）：
取消发生在流式输出中时，runner 捕获 CancelledError，把已累积的部分
文本/思维链以 `finish_reason="aborted"` 定稿 `_notify` 落盘后再抛出
（实现要点：流式循环维护累积 partial，except CancelledError 分支定稿；
无任何累积内容时不落空行）。收益双重：用户屏幕上看到的半截回答与转录
一致（刷新不「消失」），续聊时模型知道自己上次说到哪被打断。

中断标志：被打断的运行，本轮最后一条 assistant 行（含上述半截行）的
信封 `finish_reason` 为 `"aborted"`（兑现格式注释的既有承诺，老读端
忽略该字段）。半截落盘后「本轮无任何 assistant 行」只剩一种情况——
模型还没吐出任何内容，此时前端以「该 user 消息后无 assistant 回应」
推断显示「已中断」即可，转录不为此扩格式。
前端会话回放据 `finish_reason=aborted` 在轮次页脚显示「已停止/已中断」。

### 4.4 子进程组收割（修缺口 E）

`make_bash_tool` / `make_mclaw_tool` 的 subprocess 调用：

- `create_subprocess_shell(..., start_new_session=True)`——子进程自成
  进程组；
- handler 用 `try/finally` 包 `communicate()`：被取消（CancelledError）
  或超时时 `os.killpg(proc.pid, SIGKILL)` 后再 `communicate()` 收尸；
  现有 timeout 分支合并进同一收割逻辑；
- crash 场景（API 进程直接死亡）无法在进程内收割：Docker 部署容器重启
  自然回收；裸机部署在 README 部署章节注明该边界。

### 4.5 三场景的停机/恢复语义（定稿）

| | ①停机升级（SIGTERM） | ②异常停机（kill -9 等） | ③用户点停止 |
|---|---|---|---|
| 运行中的 loop | registry.close() 取消全部，逐个等 finally 收尾（10s 总超时） | 立即死亡，无收尾 | cancel 单个运行，等 finally 收尾 |
| 转录终态 | 补配对（服务中断文案）+ aborted 标志 | 尾部可能遗留孤儿 → **启动自愈 seal**（服务中断文案） | 补配对（用户取消文案）+ aborted 标志 |
| 运行标记 | finish_run 清空 | 心跳 30s 超时自愈 | finish_run 清空 |
| 子进程 | killpg 收割 | 容器重启回收 / 裸机遗留（已注明） | killpg 收割 |
| 事件日志 | 丢弃（设计如此） | 丢弃 | 保留至 24h 过期 |
| 前端感知 | SSE 断→重连 404→「服务已重启」文案；刷新后转录显示已中断 | 同左（30s 内暂显运行中） | agent_cancelled 事件实时到达 |
| **用户再次发消息** | **立即可以**（标记已清） | 30s 心跳窗内被拒（文案改为「运行状态确认中，稍候几秒重试」）；窗后可以 | **立即可以** |
| 半途工具副作用 | 合成回执引导模型先查询核实 | 同左 | 同左 |
| 升级特化 | in-app update 走同一 SIGTERM 路径，无需专门逻辑；更新页提示「进行中的 AI 会话将被停止」 | — | — |

### 4.6 明确不做的（及理由）

- **不自动续跑被打断的运行**：恢复 = 状态一致 + 用户可续聊，不是替用户
  把任务跑完。重放半途 tool_call 不幂等（下载/订阅类副作用），自动续跑
  的风险远大于收益；用户一句「继续」就能让模型基于补配对后的上下文接着
  干，这是最安全的续跑方式。
- **不持久化事件日志**：事件是 UI 通道的投影，转录已含全部恢复所需信息；
  持久化事件是第二事实源，引入一致性负担。
- **不给 tool 执行加 WAL/两阶段标记**：「先记 intent 再执行、恢复时比对」
  能精确区分「未执行/已执行」，但要给每个工具定义幂等性与对账逻辑——
  当前工具面（bash/read/write/edit/mclaw）用「结果未知 + 引导查询」的
  文案已足够，等出现真正高危的写操作工具再评估。
- **不做多 worker/分布式**：registry 单进程假设不变（agent_runs.py 顶部
  注释已声明该边界）。

## 5. 实现清单

| # | 改动 | 位置 | 修缺口 |
|---|---|---|---|
| 1 | on_terminal 触发点移至 `_execute` 的 finally；`close()` 加 10s 总超时 | `agent_runs.py` | B |
| 2 | `seal_pending_tool_calls(reason=...)` 文案分级；启动自愈扫描时对孤儿会话 seal（日志按中断位置分类）；`_accept_user_message`/retry 在 build_history 前防御 seal；`build_history` 读取侧修复兜底（复用交接快照修复逻辑） | `agent_sessions.py`、`recorder.py`、`routes/agent.py` | A、D |
| 3 | 取消时半截 assistant 以 `finish_reason="aborted"` 定稿落盘（runner 的 CancelledError 分支累积 partial 定稿后再抛，pi 同款语义）。**取舍已定**：aborted 只落在这条半截行上，不给已定稿的历史行补写/修订信封——已定稿行内容本就完整，重写历史行会破坏「转录只追加」的不变量；无半截内容的中断轮次由前端按「没有终答 assistant」的形状推导「已中断」标记（见 #6） | `runner.py` | C |
| 4 | bash/mclaw 子进程 `start_new_session` + finally killpg | `tools/bash.py`、`tools/mclaw.py` | E |
| 5 | 「已有正在进行的运行」拒绝文案区分心跳新鲜/陈旧两态 | `routes/agent.py` | F |
| 6 | 前端：`finish_reason=aborted` 轮次显示「已停止」；无 assistant 回应的历史轮次显示「已中断」 | `agent-conversations.tsx` 等 | C |
| 7 | README 部署章节注明裸机 crash 的子进程边界 | README | E |

### 验收标准（含混沌测试）

1. **kill -9 恢复**：运行中（工具执行期）kill -9 API 进程 → 重启 →
   会话列表 30s 内显示已结束、打开会话看到「已中断」、**直接发消息能
   正常运行**（转录含服务中断回执，模型可见）；
2. **优雅停机**：运行中 SIGTERM → 进程在收尾完成后退出（日志有补配对
   计数）→ 重启后同上，且启动自愈命中数为 0（停机时已 seal 干净）；
3. **取消竞态**：压力循环「发起长工具运行 → 随机延迟后取消」×100，
   转录中同一 tool_call_id 永远恰好一条回执；
4. **取消后续聊**：点停止 → 立即发「继续」→ 模型基于中断上下文续跑；
5. **子进程收割**：运行 `bash sleep 600` → 取消 → `ps` 无残留 sleep；
6. **孤儿自愈幂等**：人为构造尾部孤儿 tool_call 的转录 → 启动两次 →
   仅第一次 seal、第二次零写入；接受路径同验。

## 6. 测试计划

- 单元：seal 文案分级/幂等、aborted 标志落点、build_history 对已 seal
  历史的投影；
- API：TestClient 内模拟三场景（cancel、close registry、直接构造孤儿
  转录后重启 store）断言恢复语义表逐格成立；
- 混沌（真机脚本，不进 CI）：验收 1/3/5 的自动化脚本入 scripts/perf 或
  scratchpad 留档。
