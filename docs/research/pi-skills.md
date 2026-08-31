# pi 的 Skills 机制调研（加载 + 运行时注入上下文）

> 调研对象：https://github.com/earendil-works/pi （badlogic 的开源编码 Agent）
> 代码基线：commit `853a80d`（2026-08-28），本地克隆 `/home/user/earendil-works/pi`
> 目的：为 movieclaw Agent 引入技能（skill）机制提供可交接的机制拆解。
> 文中 `file:line` 均指 pi 仓库该 commit 下的位置。

## 0. 一句话总览

**pi 没有专门的 SkillTool 工具。**它实现的是 [Agent Skills 标准](https://agentskills.io/specification)
的「渐进披露」（progressive disclosure）模型，技能进入上下文共有三条路径：

1. **系统提示词清单 + 通用 read 工具**（默认路径）：启动时只把每个技能的
   `name/description/location` 以 XML 清单塞进 system prompt，正文不进上下文；
   模型判断任务匹配后**用普通的 read 工具自己去读 SKILL.md 全文**。
2. **`/skill:name` 斜杠命令**（用户强制路径）：把技能正文展开成一条
   `<skill>` 包裹的 user 消息直接发给模型（并持久化进会话转录）。
3. **`harness.skill(name)` 编程接口**（v2 规范，尚未实现）：给上层应用的
   lane 操作，语义同 2，未知技能返回类型化错误 `UnknownSkill`。

pi 里有两套并行实现：`packages/coding-agent`（**现行产品**，同步 fs 版）和
`packages/agent`（**下一代 harness 规范**，ExecutionEnv 抽象的异步版，
`AgentHarness` 目前还是抛 `HarnessNotImplemented` 的脚手架）。两套的发现算法
和格式化输出几乎逐行一致，仅少量校验严格度不同（见 §6）。

## 1. 技能的形态与规范

官方文档：`packages/coding-agent/docs/skills.md`。

### 1.1 目录结构

一个技能 = 一个含 `SKILL.md` 的目录，其余内容自由（脚本、参考文档、资产）：

```
my-skill/
├── SKILL.md              # 必需：frontmatter + 指令正文
├── scripts/process.sh    # 可选辅助脚本
├── references/api.md     # 可选按需加载的深度文档
└── assets/template.json
```

也允许「裸 .md 文件」作为单文件技能（无目录），条件见 §2.2。

### 1.2 Frontmatter 字段

解析：coding-agent `core/skills.ts:277`（`loadSkillFromFile`）、harness
`harness/skills.ts:244`。YAML frontmatter，未知字段一律忽略。

| 字段 | 必需 | 行为 |
|---|---|---|
| `name` | 否（有回退） | 缺省时**回退为父目录名**（`skills.ts:321`）。≤64 字符，只许 `a-z0-9-`，不得首尾/连续连字符 |
| `description` | **是** | ≤1024 字符。**没有非空 description 的技能不加载**（`skills.ts:330`），这是唯一的硬性拒载条件 |
| `disable-model-invocation` | 否 | `true` 时该技能**不出现在 system prompt 清单里**，只能用户 `/skill:name` 显式调用（`skills.ts:356`） |
| `license` / `compatibility` / `metadata` | 否 | 文档承认但代码不消费 |
| `allowed-tools` | 否 | 文档标注 experimental，**代码完全未实现**（grep 无消费点）——只是标准占位 |

### 1.3 校验哲学：warn-but-load

`validateName` / `validateDescription`（`skills.ts:92-127`）产出的都是
**warning 级诊断**，技能照常加载；唯一例外是缺 description。pi 还刻意放宽了
标准的一条：**不要求 name 与父目录名一致**（文档 `skills.md:7` 明说标准这条
「对多 harness 共享技能目录不友好」）。注意：harness 版（`harness/skills.ts:303`）
反而会对 name≠父目录名发 warning，coding-agent 版完全不检查——两套实现的
分歧点之一。

## 2. 发现与加载（编译期：从磁盘到技能列表）

### 2.1 扫描来源与优先级

由 `DefaultPackageManager.resolve()`（`core/package-manager.ts`）统一汇总为
`ResolvedResource[]{path, enabled, metadata}`，来源共六类：

| 来源 | 位置 | 备注 |
|---|---|---|
| 用户全局 | `~/.pi/agent/skills/`、`~/.agents/skills/` | 恒定加载 |
| 项目 | `.pi/skills/`；`cwd` 及各级祖先目录的 `.agents/skills/`（向上到 git 仓库根，非仓库则到文件系统根，`package-manager.ts:461`） | **仅在项目被信任（trust）后加载**（`package-manager.ts:2398,2418`）——不信任的仓库不能往上下文里注入技能，这是安全模型的一部分 |
| 包（packages） | settings `packages` 数组里的 npm/git 包：包内 `skills/` 约定目录，或 `package.json` 的 `pi.skills` 清单（`pi-manifest.ts`） | 包条目可带过滤器 `{source, skills: [patterns], autoload}`（`package-manager.ts:2153-2266`） |
| settings `skills` 数组 | 全局 `~/.pi/agent/settings.json` 与项目 `.pi/settings.json` | **混合语义**（`resolveLocalEntries`，`package-manager.ts` 约 2320 行）：普通条目=追加路径（文件或目录，项目条目相对 `.pi/` 解析，所以接 Claude Code 项目技能要写 `"../.claude/skills"`）；`!pat`/`+path`/`-path` 前缀条目=启停覆写模式 |
| CLI | `--skill <path>`（可重复） | **叠加生效，即使 `--no-skills` 也照常加载**（`resource-loader.ts:468`） |
| 扩展（extensions） | 扩展响应 `resources_discover` 事件返回 skillPaths（`agent-session.ts:2463`） | 注入后重建 system prompt（`agent-session.ts:2484`） |

优先级（同名冲突时**先到先得**，`resourcePrecedenceRank`，`package-manager.ts:189`）：

```
0 项目 settings 条目 > 1 项目自动发现 > 2 用户 settings 条目 > 3 用户自动发现 > 4 包资源
```

同名冲突产生 `collision` 诊断（赢家/输家路径都记录，`skills.ts:430-446`）；
同一物理文件经 symlink 重复到达时按 realpath 去重、静默跳过（`skills.ts:423`）。

启停覆写语义（`isEnabledByOverrides`，`package-manager.ts:712`）：
`!glob` 排除 → `+exact` 强制包含（压过排除）→ `-exact` 强制排除（压过强制包含）。
禁用的技能仍在 `/settings` 资源面板可见（enabled=false），只是不进清单。

### 2.2 目录扫描算法

`loadSkillsFromDirInternal`（coding-agent `skills.ts:173`；harness 版逐行同构）：

1. **当前目录含 `SKILL.md` → 整个目录视为一个技能根，立刻返回、不再深入**
   （`skills.ts:195-221`）——技能的 `references/` 子目录里再放 SKILL.md 也不会
   被当成新技能；
2. 否则遍历子目录递归（跳过 `.` 开头目录与 `node_modules`）；
3. 根层的裸 `.md` 文件按模式收载（`collectSkillEntries` 的
   `SkillDiscoveryMode`，`package-manager.ts:363`）：
   - `pi` 模式（`~/.pi/agent/skills/`、`.pi/skills/`）：**只收根层**裸 .md；
   - `agents` 模式（`~/.agents/skills/`、项目 `.agents/skills/`）：根层裸 .md
     忽略，**只收子目录（分组文件夹）里的**裸 .md；
   - 裸 .md 必须带含非空 description 的 skill frontmatter 才算技能，否则
     **静默忽略**（`skills.ts:306`）；`SKILL.md` 解析失败则发 warning；
4. 每一层都读取并叠加 `.gitignore` / `.ignore` / `.fdignore` 规则（模式加相对
   前缀后合并进同一个 matcher，`addIgnoreRules`，`skills.ts:47`），被 ignore
   的文件/目录不扫描；
5. symlink 跟随（stat 目标判断文件/目录），断链跳过。

加载产物（coding-agent 的 `Skill`，`skills.ts:74`）：**只有元数据，不含正文**——
`{name, description, filePath, baseDir, sourceInfo, disableModelInvocation}`。
正文永远在用到的那一刻才读盘（见 §3、§4）。harness 版的 `Skill`
（`harness/types.ts:45-56`）则**在加载时就把 frontmatter 剥掉、把正文存进
`content` 字段**——因为 harness 的 `skill()` 是编程调用，没有「现场再读盘」的语境。

### 2.3 汇总与重载

`ResourceLoader.reload()`（`resource-loader.ts:388`）驱动全流程：
settings 重载 → packageManager.resolve() → 过滤 enabled → 拼出 skillPaths →
`loadSkills({skillPaths, includeDefaults:false})` 逐路径加载。`--no-skills`
只砍自动发现，CLI 路径保留。TUI 的 `/reload` 命令重跑整个流程并重建
system prompt；技能加载诊断（warning/collision）在资源面板展示
（`interactive-mode.ts:1732`）。

## 3. 注入路径一：系统提示词清单 + read 工具（默认）

### 3.1 清单格式

`formatSkillsForPrompt`（`skills.ts:355`；harness 版 `harness/system-prompt.ts:3`）：

```
The following skills provide specialized instructions for specific tasks.
Use the read tool to load a skill's file when the task matches its description.
When a skill file references a relative path, resolve it against the skill
directory (parent of SKILL.md / dirname of the path) and use that absolute
path in tool commands.

<available_skills>
  <skill>
    <name>brave-search</name>
    <description>Web search via Brave API...</description>
    <location>/abs/path/brave-search/SKILL.md</location>
  </skill>
  ...
</available_skills>
```

要点：
- `disable-model-invocation: true` 的技能被过滤掉；全被过滤/为空时**整段不输出**；
- name/description/location 都做 XML 转义（描述里可以放引号尖括号）；
- `location` 是**绝对路径**——read 的目标直接可用，还兼作相对引用的锚点；
- 清单在会话启动时随 system prompt 构建一次（`agent-session.ts:1086` 取
  `resourceLoader.getSkills()` 传入 `buildSystemPrompt`），`/reload` 或扩展
  注资源时重建。

### 3.2 关键门控：没有 read 工具就不注入清单

`system-prompt.ts:64-67,161-164`：**仅当 read 工具在本会话可用时**才追加技能
清单（自定义 system prompt 时同样检查 `selectedTools.includes("read")`）。
因为该路径的「运行时加载器」就是 read 工具本身——工具不在，清单就是死承诺。

### 3.3 运行时：模型自己 read，无任何专用工具

模型判断任务匹配某技能描述后，发起普通 `read(SKILL.md绝对路径)`；正文以
tool result 形式进入上下文。之后模型按正文指令行动（跑技能目录里的脚本、
再 read `references/` 里的深度文档……）——这就是渐进披露的第二级、第三级。

产品层为这条路径做的唯一「特殊化」是**展示**：read 工具的 TUI 渲染发现目标
文件名是 `SKILL.md` 时，把调用行折叠为 `[skill] <目录名> (ctrl+o 展开)`
（`tools/read.ts:130-160`），与 `/skill:` 注入的视觉样式统一。

文档坦承的弱点（`skills.md:69`）：**模型并不总会主动去 read**；需要靠
description 写得好、提示语引导，或用户直接 `/skill:name` 强制。

## 4. 注入路径二：`/skill:name` 斜杠命令（显式强制）

### 4.1 注册

技能自动注册为 `/skill:<name>` 命令：
- 交互模式的自动补全列表（`interactive-mode.ts:759-771`），受设置
  `enableSkillCommands`（默认 true，`settings-manager.ts:1119`）控制；
- RPC 模式 `get_commands` 同样列出（`rpc-mode.ts:703`）。

注意：`enableSkillCommands` **只影响补全展示**；展开逻辑本身不查这个开关，
手敲 `/skill:xxx` 仍会展开。

### 4.2 展开机制

`AgentSession._expandSkillCommand`（`agent-session.ts:1354-1378`），在
sendMessage 管线里执行，顺序为：扩展 input 拦截 → **技能展开** → 提示词模板
展开 → 入库/发送（streaming 时同样先展开再入 steer/followUp 队列）：

1. 文本以 `/skill:` 开头才处理；按第一个空格切出 `skillName` 和 `args`；
2. 按名字查已加载技能，**查不到原文透传**（不报错，当普通消息发出）；
3. **现场重新读盘** `skill.filePath`（不是用缓存）→ 剥 frontmatter → 包装：

```
<skill name="brave-search" location="/abs/.../SKILL.md">
References are relative to /abs/.../brave-search.

{SKILL.md 正文}
</skill>

{args（如有，空两行接在块后）}
```

4. 读盘失败发 `skill_expansion` 扩展错误事件、原文透传。

（文档 `skills.md:83` 说参数会以 `User: <args>` 前缀附加——**与代码不符**，
代码是裸拼接，以代码为准。）

### 4.3 持久化与展示

- 展开后的全文**作为 user 消息存进会话转录**——技能正文成为持久历史的一部分，
  反复调用会重复占上下文，由通用 compaction 兜底；这也意味着退出重开会话后
  技能内容仍在历史里，无需重注入；
- TUI 用正则 `parseSkillBlock`（`agent-session.ts:132`）识别这类消息，渲染为
  可折叠组件 `SkillInvocationMessageComponent`
  （`components/skill-invocation-message.ts`）：折叠态一行
  `[skill] name (ctrl+o 展开)`，展开态渲染 Markdown 全文；args 部分作为普通
  用户消息另行渲染。

## 5. 注入路径三：harness `skill()`（v2 规范，未上线）

`packages/agent` 是下一代持久化 harness（规格书 `packages/agent/docs/harness.md`，
2600+ 行）。与技能相关的公共面：

- `AgentLane.skill(name, additionalInstructions?): Promise<RunResult>`
  （`agent-harness.ts:276`，规格 `harness.md:1988`）——把技能作为一次 lane
  级 prompt 操作编程触发；
- 未知技能名返回类型化错误 `UnknownSkill`（`harness.md:2085`）——比
  coding-agent 的静默透传严格；
- 「Skill/template expansion precedes storage」（`harness.md:2024`）：展开先于
  持久化，落库的就是展开后的消息，与现行产品语义一致；
- 消息构造 `formatSkillInvocation(skill, additionalInstructions)`
  （`harness/skills.ts:38`）：与 §4.2 完全相同的 `<skill>` 块 + 换行拼接附加
  指令；区别是正文来自加载时缓存的 `skill.content`，不再现场读盘；
- 清单注入 `formatSkillsForSystemPrompt`（`harness/system-prompt.ts`）与
  coding-agent 输出格式一致（指令行措辞略有差异："Read the full skill file..."）；
- 现状：`AgentHarness.skill()` 抛 `HarnessNotImplemented`
  （`agent-harness.ts:368`），只有加载/格式化函数和测试就绪。

harness 版加载器的其它差异：文件系统操作全部走 `ExecutionEnv` 抽象（可远程
/SSH）；目录遍历前按名字 `localeCompare` 排序保证确定性（coding-agent 依赖
readdir 顺序）；诊断带稳定 code（`file_info_failed/parse_failed/...`）；
`loadSourcedSkills` 支持调用方自定义 source 标签透传。

## 6. 细节与坑清单（交接必读）

1. **description 是唯一硬门槛**：缺了不加载；其它一切违规（名字非法、超长）
   都只 warning 照常加载。
2. **name 回退目录名**：frontmatter 没写 name 时用父目录名，所以裸 .md 技能的
   name 是**其所在目录**的名字——同目录多个裸 .md 会天然撞名（先到先得+诊断）。
3. **SKILL.md 短路**：技能目录内部不会再发现嵌套技能；想并列多技能就各建目录。
4. **read 工具缺席则清单不注入**（§3.2）；`/skill:` 展开不受此限。
5. **`/skill:` 每次现场读盘**：改完 SKILL.md 不必 `/reload` 就能生效（清单里
   的 description 变更才需要 reload）；read 路径同理天然是新鲜的。
6. **展开内容进转录**：技能正文会被会话历史永久携带、参与后续每次请求与
   compaction 预算。
7. **信任模型**：项目级技能（`.pi/skills/`、祖先 `.agents/skills/`）在项目未
   被用户信任前一律不加载；文档明确警示技能可含可执行代码，用前先审。
8. **`--no-skills` 不是全关**：显式 `--skill` 路径仍加载；settings 里
   `"skills": []` 空数组对包资源的语义是「显式全禁用」（`applyPackageFilter`）。
9. **跨 harness 复用**：靠 settings 把 `~/.claude/skills`、`~/.codex/skills`
   等目录加进来即可——这也是 pi 放宽 name==目录名校验的动机。
10. **`allowed-tools` 未实现**、`license/compatibility/metadata` 未消费——
    对标准的兼容是「解析不报错」级别。
11. **文档 vs 代码的两处出入**：args 的 `User:` 前缀（文档有、代码无）；
    name≠目录名的 warning（harness 有、coding-agent 无、文档说不要求）。

## 7. 对 movieclaw 的启示（简记，非方案）

- pi 证明了**不需要专用 SkillTool**：system prompt 元数据清单 + 既有的文件读
  取工具就能闭环，成本最低；movieclaw Agent 若已有 read/文件类工具可直接套用
  这个模式，没有的话则更适合做成一个「加载技能」的专用工具（等价于把 read
  收窄到技能目录，权限面更小）。
- 渐进披露的预算特性好：N 个技能常驻成本 ≈ N×(名字+描述)，正文按需付费。
- `/skill:name` 的「展开成 user 消息并持久化」设计把强制注入、会话记忆、UI
  折叠展示三件事用一条消息通道解决，与我们图片消息的「转录即事实」思路同构。
- 若引入，需要决策的三件事：技能目录放哪（对齐 `data/` 约定 vs 项目内）、
  是否需要 pi 式多来源/优先级（初期单目录即可）、以及信任边界（movieclaw 是
  服务端部署，技能=管理员供给，天然可信，可比 pi 简化）。
