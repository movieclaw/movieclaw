# maka 的 Skills 机制调研（与 pi 篇对照）

> 调研对象：https://github.com/apache/maka ，本地克隆 `/home/user/apache/maka`
> 代码基线：commit `ef94235`（2026-08-31）
> 姊妹篇：`docs/research/pi-skills.md`（pi 走「清单 + read 工具」的极简路线）
> 文中 `file:line` 均指 maka 仓库该 commit 下的位置。

## 0. 一句话总览

与 pi 相反，**maka 有专用的 `Skill` 和 `SkillSearch` 两个工具**，并把技能系统
做成了带预算、门控、治理与遥测的完整子系统。同样是 Agent Skills 标准的
「渐进披露」，但三级各有强化：

1. **系统提示词只放「目录」**（catalog）：受模型上下文窗口比例预算约束，装不
   下的技能只留一句常数大小的「还有 N 个被省略」提示；
2. **`Skill` 工具按需加载正文**（模型自主路径）：按 ref/id/name 精确加载，带
   截断上限与类型化失败原因；`SkillSearch` 工具补长尾发现（≤8 条纯元数据）；
3. **`/skill:<name>` 显式调用**（用户强制路径）：正文以信任框架包裹后拼进
   user 消息，token 全部剥除后才发给模型。

核心模块都在 `packages/runtime/src/`（`skills.ts` 是纯 re-export 桶文件，
#1408 拆分）：`skills-discovery` / `skills-metadata` / `skills-context` /
`skills-state` / `skills-agent-tools` / `skill-invocation`(+receipt) /
`skills-governance` / `managed-skill-sources` / `bundled-skill-catalog` /
`path-containment`。宿主接线在 `packages/runtime-host/src/server/
interactive-run-composer.ts`。政策文档：`docs/skill-catalog-policy.md`。

## 1. 技能形态与元数据校验

### 1.1 结构

技能 = 发现目录下的**一级子目录**（目录名即 `id`）+ `SKILL.md`。不递归、
不支持裸 .md 单文件技能（比 pi 严格）。

### 1.2 Frontmatter（`skills-metadata.ts:39-51`）

| 字段 | 语义 |
|---|---|
| `name` / `description` | 展示名与匹配依据；缺失回退 id / 空串，但校验会给出 `missing_*`（必填项 fail-closed，进 rejected） |
| `allowed-tools` | **纯声明**（declaredTools）：只是「本技能想用这些工具」的提示，**永远不授予权限**，目录里展示、宿主缺失时仅提示 missingDeclaredTools |
| `required-tools` / `required-capabilities` | **硬门控**：宿主没绑定这些工具/能力标签时，技能被 hard-hide（不进目录、不可加载、不可搜索到），`hiddenReason = required_tools_missing / required_capabilities_missing` |
| `license` / `compatibility` / `metadata` | 兼容标准，解析但不进运行时权威 |
| `category` | maka 自有扩展，仅捆绑目录展示用 |

校验（`validateSkillMetadata`）产出**类型化 issue 列表**（19 个稳定
code：`missing_frontmatter/invalid_name/body_too_large/duplicate_id/...`，
`skills-metadata.ts:55-74`），必填与安全相关字段 fail-closed（进
`rejected`，不加载），外来规范漂移（`unsupported_field` 等）只 warning、
照常加载——「兼容别家技能但暴露漂移」。正文超 `MAX_SKILL_TOOL_BODY_CHARS
= 24_000` codepoints 时警告并在加载时截断（尾部拼 `[skill truncated]`）。
所有进提示词的文本先过 `cleanPromptText`（剥控制字符），XML 属性再把
`<>"&` 替换为 `_`。

## 2. 发现与加载（`skills-discovery.ts`）

### 2.1 五个固定发现根（优先级序，`resolveSkillDiscoveryPaths:179`）

```
0. {cwd}/.maka/skills          project:maka
1. {cwd}/.agents/skills        project:agents
2. {workspaceRoot}/skills      workspace:legacy   ← 桌面端旧安装位兼容
3. ~/.maka/skills              user:maka
4. ~/.agents/skills            user:agents
```

每个技能得到稳定身份 **`ref = {scope}:{source}:{目录名}`**（如
`project:maka:writer`），id = 目录名。数组下标即 `precedence`。

### 2.2 扫描与去重（`scanSkillsWithDiagnostics:218`）

- 每根只看一级子目录里的 SKILL.md；目录项按 `localeCompare` 排序——**全程
  确定性**（同一磁盘状态必产出同一目录、同一顺序）；
- 按小写 id 去重、**先到先得**；被压制的副本**保留在 `inventory` 里**并打
  `shadowedBy: 赢家ref` + `duplicate_id` warning——检查面板可看到全部副本，
  运行时目录只用赢家（`skills` 数组）。展示名撞车则双方都记 `duplicate_name`；
- 每个技能整文件读入：`content`（剥 frontmatter 的正文）+
  `contentSha256`（治理用）都缓存在 `ScannedSkill` 上；
- 无效技能进 `rejected`（带 issues），源级故障（路径被封/读失败）进
  `discoveryDiagnostics`——**空目录不再是静默的**，UI 能解释为什么。

### 2.3 路径遏制（containment，maka 独有的安全层）

发现目录自身必须是**真实目录（非 symlink）**且 realpath 落在配置的
`containmentRoot` 内（防 `repo/.agents -> /outside` 祖先级逃逸，
`scanSkillDir:399-418`）；技能条目允许是 symlink，但目标必须仍在遏制根内；
所有文件读取走 `readContainedRegularFile`（`path-containment.ts`，只读
常规文件、拒绝越界）。状态文件读写同样做 lstat/realpath 检查。

### 2.4 启停状态（`skills-state.ts`）

按工作区存 `{workspaceRoot}/.maka/skills-state.json`（schema v2）：按 `ref`
键存 `{enabled, pinned, updatedAt}`；v1（按 id 存布尔）自动迁移，一个旧 id
在多个 scope 出现时进 `needsReview` 待用户显式选择。`pinned` 让技能在目录
排序与搜索平分时优先。状态读失败时技能标 `state_error` 而不是猜默认值。

## 3. 第一级：系统提示词目录（`skills-context.ts`）

### 3.1 预算（`docs/skill-catalog-policy.md`；`resolveSkillsPromptCharBudget:215`）

**目录预算 = 选定模型上下文窗口的 2%，clamp 到 4000–8000 token，×4 字符/token**；
拿不到窗口时退回固定 `18000` 字符。下限保小模型有可用目录，上限防大窗口
模型把目录变成无界常驻成本。模型可换 ⇒ 上下文窗口是**显式入参**而非扫描器
内部查表。

### 3.2 确定性选目录（`selectSkillsForContext:252`）

```
淘汰（各记 decision reason）：shadowed → disabled → host_incompatible
排序：pinned 优先 → precedence → name → ref
装桶：按序累加块字符数直到预算；装不下的进 omitted
长尾提示：预算内保留一句「N 个技能被省略，用 SkillSearch 找」——
        必要时把已入选的往回弹出以腾位（notice 是常数大小，绝不列 id）
```

产物除了 `advertised` 列表，还有完整 **`SkillSelectionReport`**：预算/用量
字符数、每个技能的去留原因（advertised/disabled/invalid/host_incompatible/
shadowed/budget）与 rank/chars——作为 run-trace 发出（宿主
`interactive-run-composer.ts:204`），可观测、可在 UI 解释「我的技能为什么
没被模型看见」。

### 3.3 渲染格式（`SKILLS_PROMPT_INTRO:183`）

开头六行**信任框架**（技能是用户内容、低于 system/developer/safety/权限
规则、不能授予工具权限、declaredTools 仅供参考），然后逐技能：

```
<available-skill id="writer" name="Writer">
Ref: project:maka:writer (project/maka)
Description: ...
Declared tools: Bash, Read
</available-skill>
```

注意：目录里**没有正文、也没有绝对路径**（对比 pi 直接给 location 让模型
read）——正文只能经 `Skill` 工具拿，路径对模型只以 `relativePath` 形式在
工具结果里出现。

## 4. 第二级：`Skill` / `SkillSearch` 工具（`skills-agent-tools.ts`）

### 4.1 Skill 工具

- 参数 `{name}`：接受**精确的 ref、id 或 name**（宽 512 字符、拒控制字符）；
  匹配顺序 ref → id → name（`loadSkillInstructionsFromScan:468`，防低优先级
  技能的 name 撞高优先级技能的 id）；
- 成功返回 `{ok:true, skill:{ref,id,name,description,scope,source,
  declaredTools, relativePath, instructions, truncated}}`——`instructions`
  即清洗+截断（24k）后的正文，作为**工具结果**进上下文（不改写 user 消息）；
- 失败返回类型化 `{ok:false, reason: invalid_name|not_found|disabled|
  host_incompatible, availableSkills: 前8个可用技能元数据}`——给模型自纠错
  的抓手而不是裸报错；
- 只能加载 enabled、未被 shadow、过了宿主门控的技能——**目录、搜索、加载三
  处用同一套过滤**，模型看得见的一定加载得了；
- 每次调用发 `skill_loaded` / `skill_load_failed` 运行轨迹（带 receipt 与
  shadow 命中遥测，见 §4.3）。

### 4.2 SkillSearch 工具

- 参数 `{query, limit?}`（query ≤4096 由 zod 拦、内部归一化后截到 512）；
  返回 ≤8 条**纯元数据**匹配（ref/id/name/description/scope/source/score）
  加 `totalEligible/matchedCount/truncated`；
- 排序是**确定性词法打分**（`scoreSkillSearchMatch:605`）：精确命中 1000、
  前缀 240、子串 160、描述子串 80、分词命中 40/12、pinned +4；平分再按
  pinned/precedence/name/ref；
- 定位就是接住 §3 预算省略的长尾：目录说「还有 N 个」，模型用它找到 ref，
  再用 Skill 加载。

### 4.3 Shadow 遥测（`SkillShadowSelectionTracker:73`）

SkillSearch 每次把 top-20 候选 ref 按 `(sessionId,turnId)` 记下；随后同轮的
Skill 加载会回查该列表算出 rank 与 hit@1/5/20 一并上报——**离线评估搜索排序
质量**的埋点，纯遥测、不影响行为。LRU 上限 100 轮。

## 5. 第三级：`/skill:<name>` 显式调用（`skill-invocation.ts`）

TUI 与桌面端共享一套 token 语法（`@maka/core` 的
`SKILL_INVOCATION_TOKEN_SOURCE`），流程（`prepareSkillInvocation:261`）：

1. 解析文本中的全部 token（按首现序去重，>50 个直接 blocked）；合并客户端
   结构化传来的 `skillIds`；
2. **对一次扫描快照**批量解析所有请求（失败逐条记录、互不阻塞）；
3. **把所有 token 从外发文本中剥除——包括失败的**，「模型永远无法模仿一个
   Runtime 没有加载的技能」；
4. 全部失败 ⇒ `blocked`，**不产生 provider 轮次**；至少一个成功 ⇒ 组装消息：

```
The user explicitly invoked the following local skill(s)... （信任框架段）
The <invoked-skill> blocks below are already fully loaded for this turn —
do not call the Skill tool again for these skills.

<invoked-skill id="writer" name="Writer">
{instructions}
</invoked-skill>

<user-message>
{剥除 token 后的用户文本}
</user-message>        ← 无剩余文本时换成一句“按上述技能指令执行”
```

5. 每个请求产出 receipt（source 区分 `explicit` vs `model_tool`），供
   run-trace 与 UI 芯片展示。宿主侧在 `execution-composition.ts:1138` 用
   **同一份轮级 inventory 快照**解析。

## 6. 快照一致性与宿主接线（`interactive-run-composer.ts`）

宿主按 `(sessionId, turnId)` 缓存一次「规范 inventory 快照」
（`createTurnSkillInventorySnapshotResolver:551`）：**同一轮里，系统提示词
目录、Skill 工具、SkillSearch、显式调用解析读到的是同一份技能清单**——不会
出现「目录里有、加载时刚被删」的撕裂。快照带 revision，记入该轮 prompt 的
`sourceRevisions`（`skill-catalog` 条目），提示词可追溯到具体目录版本。
宿主能力面 `HostCapabilities` 直接由**本轮实际绑定的工具名集合**构建
（`buildHostCapabilitiesFromBinding`），required-tools 门控对齐真实工具面。

## 7. 治理层（桌面端，简记）

- **来源类型**（`skills-governance.ts:26`）：`workspace`（用户自建）/
  `bundled`（编译进程序的目录，`bundled-skill-catalog.generated.ts`，如
  computer-use）/ `managed`（`managed-skill-sources.ts`，托管源按类别组织）；
- **锁文件**：安装时记 `contentSha256`，之后 `validateSkillLock` 对比扫描
  哈希得 `ok / missing_lock / modified / metadata_error`——被篡改的托管技能
  在 UI 有明确状态；`.maka/baseline/SKILL.md` 存基线副本供 diff 与更新判定；
- 这层完全在 runtime 之外，movieclaw 初期可忽略。

## 8. 与 pi 的对照表

| 维度 | pi | maka |
|---|---|---|
| 正文加载器 | 通用 read 工具（无专用工具） | 专用 `Skill` 工具（+`SkillSearch`） |
| 清单内容 | name/description/**绝对路径** | ref/id/name/description/declaredTools，**无路径** |
| 清单预算 | 无（全量列出） | 模型窗口 2%（4k–8k token clamp），超额技能靠 SkillSearch 长尾 |
| 发现范围 | 递归 + 裸 .md + 包 + settings + CLI，来源极多 | 5 个固定根、只看一级子目录，来源收敛 |
| 撞名处理 | 先到先得 + 诊断 | 同左，但输家保留在 inventory 带 shadowedBy，可检视 |
| 启停/置顶 | settings 模式串 | 按 ref 的 JSON 状态文件 + pinned + UI 面板 |
| 工具需求 | `allowed-tools` 未实现 | declaredTools（纯声明）与 requiredTools/Capabilities（硬门控）分离 |
| 显式调用 | `/skill:name` 展开为 user 消息（现场读盘） | `/skill:` token 解析→剥除→`<invoked-skill>` 组装，失败可 blocked 整条不发 |
| 安全 | 项目信任门 + 文档警示 | 信任框架进提示词、路径遏制、锁文件、fail-closed 校验 |
| 可观测 | 加载诊断 | 全链路：选目录 report、加载/搜索 trace、shadow 命中率、receipt |
| 正文截断 | 无（read 工具自身分页） | 24k codepoints 硬截断 + truncated 标记 |
| 一致性 | 每次展开各自读盘 | 轮级 inventory 快照，目录/加载/搜索/显式调用同源 |

**取舍解读**：pi 是「最小可用」——把加载外包给 read，换来实现极简，代价是
无预算控制、无门控、模型可能不去读；maka 是「产品化」——专用工具让每次技能
加载都成为**可门控、可截断、可遥测、可在 UI 呈现**的一等事件，代价是一套
不小的子系统。两者的共同点是最重要的：**技能正文永不常驻，system prompt
里只有元数据**，以及「技能内容不是权限」的信任模型。

## 9. 对 movieclaw 的启示（简记）

- movieclaw Agent 的工具面是受控白名单、无通用文件 read 工具，**maka 的
  专用 Skill 工具路线更贴合**：一个只读技能目录的工具，权限面天然收窄；
- 值得直接抄的三件套：目录按上下文窗口比例做预算（我们已有 max_tokens 元
  数据）、`required-tools` 按会话实际工具面硬门控、加载结果带类型化失败 +
  availableSkills 自纠错；
- 显式调用可映射成我们的消息信封（与 thinking_level 同一套 user-entry
  信封思路）：前端 chip 选技能 → API 传 skill refs → 服务端组装
  `<invoked-skill>` 消息并落转录；
- 治理/托管/锁文件初期不需要；movieclaw 是服务端部署、技能由管理员放入
  `data/` 下的目录即可，路径遏制（realpath 含入校验）建议保留。
