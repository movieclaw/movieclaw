# Agent 技能（Skills）方案 —— pi 极简路线

> 调研背景：`docs/research/pi-skills.md`（选定路线）、`docs/research/maka-skills.md`（对照）。
> 路线决策：采用 pi 的「系统提示词清单 + 通用 read 工具」渐进披露模型，
> **不引入专用 SkillTool**。理由见 §1.2。

## 1. 目标与路线

### 1.1 要解决的问题

让管理员能用纯 Markdown 给 Agent 扩展「技能」——针对特定任务的工作流指令、
脚本与参考资料（例：站点资源整理规范、字幕处理流程、片单策展方法），
不改代码、不重启服务。

### 1.2 为什么选 pi 路线

- 我们的 Agent 工具集**已经有完整的 read/bash/write/edit**（`movieclaw_agent/tools/`），
  pi 的「清单里给绝对路径、模型自己 read 正文」可以零新增工具落地；
  read 现成的 2000 行/50KB 分页天然处理长技能正文。
- maka 那套（专用工具、窗口比例预算、能力门控、治理锁）解决的是「技能来源
  多且不可信、数量大」的问题；movieclaw 是服务端部署，技能由管理员放进
  `data/`，与站点 YAML 同一信任级，数量预期个位数——那些机制在这里是过度设计。
- 渐进披露的预算特性两家相同：**正文永不常驻，system prompt 只有元数据**，
  N 个技能的常驻成本 ≈ N×(名字+描述)。

### 1.3 范围

- **P0（本方案）**：技能目录扫描 + 系统提示词清单注入 + 模型经 read 自主加载。
  只服务 Web 会话（含 retry / 续聊）。
- **不做**（P1/P2，见 §8）：前端技能管理页、`/skill:` 显式调用、启停状态、
  多来源与优先级、目录预算与搜索。

## 2. 技能的形态

### 2.1 目录：内置 + 用户两层

发现分两层，**用户层优先，同名覆盖内置**：

| 层 | 位置 | 交付方式 |
|---|---|---|
| 系统内置 | `src/movieclaw_agent/builtin-skills/`（包内数据目录） | 随源码打包发版，产品自带的官方技能 |
| 用户 | `AGENT_SKILLS_DIR`（默认 `./data/agent-skills`，新配置进 `core/config.py`） | 管理员放进 `data/` 卷即安装，对齐「运行期数据全落 data/」约定 |

- 内置层路径从 `__file__` 反推（`Path(__file__).parent / "builtin-skills"`），
  与 `prompts._SOURCE_ROOT` 同一机制——**应用内更新的 overlay 生效时自动指向
  新版本的内置技能**，无需任何额外处理；打包上照抄 `spec.json` 的先例，在
  pyproject `[tool.setuptools.package-data]` 加一行
  `movieclaw_agent = ["builtin-skills/**/*"]`（Docker 部署本就整棵 src 拷贝，
  这行是给 wheel 形态兜底）；
- 用户层用来**新增**自己的技能，也用来**覆盖**内置技能：同名时用户版生效、
  内置版不加载，并写一条中文 info 日志（见 §2.3）——想改官方技能的行为，
  复制一份到用户目录改即可，升级不会吞掉定制。

```
src/movieclaw_agent/builtin-skills/     # 内置层（随版本走，用户不改）
└── subtitle-workflow/
    ├── SKILL.md
    └── scripts/fix-encoding.sh

data/agent-skills/                      # 用户层（挂载持久化）
├── subtitle-workflow/                  # 同名 → 覆盖内置版
│   └── SKILL.md
└── curation/
    └── poster-wall/                    # 允许分组文件夹，递归发现
        └── SKILL.md
```

### 2.2 SKILL.md（Agent Skills 标准的 pi 子集）

```markdown
---
name: subtitle-workflow
description: 字幕文件的整理、重命名与编码修复流程。当用户要求处理字幕相关任务时使用。
---

# 字幕整理流程
…正文即给模型的完整指令，相对路径以本技能目录为锚…
```

- `description` **必填**：缺失或为空则不加载，并写中文 warning 日志
  （这是唯一硬性拒载条件，pi 同款）；
- `name` 可省略，回退为技能目录名；
- 校验宽松（warn-but-load）：name 超 64 字符/含非法字符、description 超
  1024 字符只记 warning 日志照常加载；未知 frontmatter 字段忽略；
- 无 frontmatter 的 SKILL.md 视为缺 description，不加载并警告。

### 2.3 发现规则（pi 算法的简化版 + 两层合并）

单目录扫描 `scan_skills(root)`：

1. 目录含 `SKILL.md` → 整个目录是一个技能根，**不再深入**（`references/`
   里再放 SKILL.md 不会被当成新技能）；
2. 否则递归子目录；跳过 `.` 开头目录与 `node_modules`；
3. **不跟随 symlink**（目录与文件都 lstat 判断）。比 pi 严格：服务端 data
   卷内没有共享技能目录的需求，一行检查同时防环与防逃逸；
4. 根层裸 `.md` 单文件技能**不支持**（pi 支持，我们砍掉——少一种形态少一类
   歧义，需要时 P1 再加）；
5. 目录项按名字排序遍历，产出确定性。

两层合并 `discover_skills()`（pi 的多来源先到先得，收敛成固定两层）：

1. **先扫用户层、后扫内置层**，同名（name 小写比对）先到先得——用户层
   自然胜出；
2. 用户技能覆盖内置技能时写中文 **info** 日志（这是覆盖机制的正常使用，
   不是异常）：`用户技能「subtitle-workflow」已覆盖同名内置技能（{用户路径}
   覆盖 {内置路径}）`；
3. **同层**内同名仍是 warning（多半是配置失误）：输家不加载，日志含双方路径；
4. 缺失的目录（用户层还没建、精简部署没带内置层）静默跳过。

产物 `Skill = {name, description, file_path, scope}`（file_path 为 SKILL.md
绝对路径；`scope: "builtin" | "user"` 供日志与未来管理页区分来源，**不进
提示词清单**——模型不需要关心技能从哪来）。**不缓存正文**——正文永远在模型
read 的那一刻现读，改技能即时生效。

## 3. 清单注入（系统提示词）

### 3.1 渲染格式（pi 同款结构，指令改中文）

新增 `movieclaw_agent/skills.py::build_skills_fragment(skills) -> str | None`：

```
# 技能
以下技能提供特定任务的专项指令。当用户请求与某技能的描述匹配时，先用 read
工具读取该技能文件的完整内容，再按其中的指令行动。技能文件里的相对路径以该
技能所在目录（SKILL.md 的父目录）为锚，转换成绝对路径后再在工具调用中使用。
技能目录是只读资料，不要修改其中的文件。

<available_skills>
  <skill>
    <name>subtitle-workflow</name>
    <description>字幕文件的整理、重命名与编码修复流程。…</description>
    <location>/app/data/agent-skills/subtitle-workflow/SKILL.md</location>
  </skill>
</available_skills>
```

- name/description/location 做 XML 转义；
- 无技能时返回 `None`，整段不输出；
- 渲染结果超 16KB 时记一条中文 warning 日志（提示管理员精简描述或减少
  技能），**不截断**——P0 的技能量级用不到预算机制，先给可见性。

### 3.2 注入点与门控

在 `routes/agent.py::_agent_system_prompt()` 末尾追加：每次运行现场
`discover_skills`（两层合并）+ `build_skills_fragment`，与 external_url
「保存即生效、不做缓存」同一思路——**改技能无需重启，下一轮运行生效**。
个位数技能的目录扫描是几次 stat + 小文件读，成本可忽略。

**read 门控（pi 规则照搬）**：清单只应在工具集含 read 工具时注入。落点上
无需写显式判断——

- Web 会话（`get_agent_tools`）恒含 read，`_agent_system_prompt` 注入即可；
- IM/微信通道是受限工具集（仅 mclaw、无 read），且走 runner 的默认
  `build_system_prompt()`（不经 `_agent_system_prompt`），**天然不含清单**，
  一行代码都不用动。在 `_agent_system_prompt` 的 docstring 里写明这条依赖：
  若未来 IM 要接技能，必须同时给 read 工具。

retry 与续聊复用 `_launch_user_message` → 同一组装函数，自动生效。

### 3.3 与转录/压缩的关系

- system prompt 不入转录（历史重建只回放消息），因此技能清单每轮重扫、
  永不过期，也不占转录体积；
- 模型 read 到的技能正文以 tool result 进转录，是「转录即事实」的一部分：
  后续轮次记得已读过的内容，压缩时由通用 compaction 按普通工具结果处理，
  无需任何特判。

## 4. 运行时行为（零新增代码，靠既有工具）

1. 模型看到清单，判断任务匹配某技能描述；
2. `read(location绝对路径)` 加载正文（长文自动分页，模型按尾部提示续读）；
3. 按正文指令行动：`read` 技能目录里的参考文档、`bash` 执行其中脚本
   （指令行已要求用绝对路径，bash 的 cwd 在工作区不受影响）。

pi 文档坦承的弱点同样适用：模型不保证主动去 read。对策也相同——把
description 写具体（「做什么 + 何时用」），这是技能作者的责任；P1 的显式
调用是最终兜底。

## 5. 安全边界

- **信任模型**：技能由管理员放入 `data/`，与站点 YAML 配置、CLAUDE.md 同
  信任级——内容本身可信，不做 maka 式锁文件/信任门。文档（README 部署章节）
  加一句：技能能指挥 Agent 执行任意 bash，只放自己审过的内容；
- **不跟随 symlink**（§2.3）：防 data 卷内意外链接把系统路径拉进扫描面；
- **提示词层约束**「技能目录只读」（§3.1）：工具层不加路径限制（bash 本就
  不受限，单独限制 write/edit 是假围栏），靠指令约束 + 转录可见性兜底；
- 递归深度上限 16：symlink 已禁，真实目录树到不了这个深度，纯粹是防御性
  常数。

## 6. 实现清单

| # | 改动 | 位置 |
|---|---|---|
| 1 | `AGENT_SKILLS_DIR` 配置（默认 `./data/agent-skills`） | `movieclaw_api/core/config.py` |
| 2 | `Skill` dataclass（含 scope）+ `scan_skills()` + `discover_skills()`（两层合并、覆盖日志）+ `build_skills_fragment()`，frontmatter 解析（yaml），中文日志 | 新文件 `movieclaw_agent/skills.py`（纯库，不依赖 API 层） |
| 3 | 内置技能目录（可先空建或放第一个官方技能）+ package-data 打包行 | `src/movieclaw_agent/builtin-skills/`、`pyproject.toml` |
| 4 | `_agent_system_prompt()` 追加发现与注入 + read 门控依赖说明 | `movieclaw_api/api/routes/agent.py` |
| 5 | 单元测试：单目录规则 ×6（短路/递归/缺 description/`name` 回退/同层同名/symlink 跳过）、两层合并 ×3（用户覆盖内置 + info 日志/两层互不同名全量保留/缺失目录静默）、fragment 渲染（转义/空清单 None/超限警告） | `tests/agent/test_skills.py` |
| 6 | API 测试：有技能时 system prompt 含清单、空目录不含；技能改动后新一轮生效 | `tests/api/test_agent_skills.py` |
| 7 | 部署文档补技能目录与覆盖机制说明 | README 部署章节 |

预估全部改动 ≤ 300 行（含测试），不动 runner、不动前端、不加迁移。

### 验收标准

1. 放入一个含脚本的真实技能，Web 会话提出匹配任务：模型 read SKILL.md、
   按指令执行脚本、产出正确结果（真机 E2E）；
2. 不匹配的任务不触发技能加载（清单只占元数据成本）；
3. 修改 SKILL.md 后，无需重启，下一条消息的运行即用新内容；
4. IM 通道的运行 system prompt 不含技能清单；
5. 缺 description 的技能不出现在清单中，日志有可读的中文警告；
6. 在用户目录放一个与内置技能同名的技能：清单里只出现用户版（location 指向
   `data/`），日志有覆盖提示；删掉用户版后内置版恢复。

## 7. 设计权衡记录

- **不做专用 Skill 工具**：read 已存在且带分页；专用工具的收益（类型化失败、
  遥测、门控）在单来源可信技能场景下不成立。若未来出现「无 read 工具但要
  技能」的运行形态（如 IM），届时再评估 maka 式专用工具。
- **每轮重扫不缓存**：与 external_url 现读一致；个位数技能的扫描成本远低于
  一次模型调用，换来「改完即生效」与零失效逻辑。
- **不支持裸 .md / symlink / 多来源**：每一项都是真实的歧义与安全面，P0
  的场景（管理员手放目录）用不到。加回去都是纯增量，不影响已有技能。
- **16KB 只警告不截断**：截断策略（谁被截、怎么提示）本身就是复杂度；量级
  到了再抄 maka 的预算制（上下文窗口 2%），我们已有模型 `max_tokens` 元数据。

## 8. 分期

- **P1（已设计，见 §9）**：composer 显式调用——加号菜单选技能、文本占位符
  `/skill:名字`、服务端展开。
- **P1 之后**：前端技能管理页（列表/查看/启停——启停状态可仿 pi 存 settings
  或仿 maka 存 `data/agent-skills/.state.json`）；届时一并支持
  `disable-model-invocation`。
- **P2**：技能量级增长后的目录预算与 SkillSearch（maka 路线）；IM 通道技能
  （需先解决 IM 无 read 工具的问题）。

## 9. P1：显式调用（加号菜单 + 占位符展开）

### 9.1 参照系调研

**pi 的机制**（`agent-session.ts::_expandSkillCommand`）：消息以
`/skill:name args` 开头时，服务端在发送管线里**现场读盘** SKILL.md →
剥 frontmatter → 包成
`<skill name="..." location="...">\nReferences are relative to {baseDir}.\n\n{正文}</skill>\n\n{args}`
→ **展开后的全文作为 user 消息入转录**；TUI 用正则
（`^<skill name="([^"]+)" location="([^"]+)">\n([\s\S]*?)\n</skill>(?:\n\n([\s\S]+))?$`）
识别这类消息，折叠渲染成一行 `[skill] name（可展开）`，用户文本单独渲染。
未知技能名**原文透传**（当普通消息发出）。限制：token 只能在消息开头、
一次一个。

**maka 的机制**（`skill-invocation.ts` + `@maka/core` 共享语法）：token
正则 `(?:^|(?<=\s))\/skill:([A-Za-z0-9._-]+)`——**行首或空白后**即可，
支持任意位置、多个 token（首现序去重，上限 50）。展开时把**全部 token
从外发文本剥除（含解析失败的）**，组装「信任框架前言 + `<invoked-skill>`
块们 + `<user-message>` 包裹的剩余文本」；全部失败则 blocked、不产生
模型轮次。UI（TUI/桌面）在草稿里把 token 高亮为芯片。

取舍：**主体抄 pi（服务端展开 + 展开文入转录 + 前端折叠渲染），token
语法抄 maka（任意位置 + 多个）**——加号菜单会把占位符插到光标处，pi 的
「必须在消息开头」不适配；maka 的 blocked 语义、信任框架前言、receipt
遥测则不引入（单来源可信技能用不到）。

### 9.2 交互（前端）

1. **加号升级为菜单**：复用 ThinkingLevelMenu 的向上弹出 `menu-surface`
   浮层，两个入口——「上传图片」（原文件选择，仅 `imageUpload` 时出现）与
   「使用技能」子列表（每项显示 name，description 做 title 提示）。加号的
   渲染条件从「imageUpload」放宽为「imageUpload 或技能列表非空」；
2. **技能列表**来自新接口 `GET /api/v1/skills`，打开菜单时现拉（个位数
   条目、与「改技能即生效」语义一致，不做缓存）；
3. **选中即插占位符**：在光标处插入文本 `/skill:名字 `。占位符就是纯文本
   ——可见、可编辑、可删除、可手敲，不引入结构化 chip 状态（这是「占位符
   然后替换」的最简形态；输入框内高亮芯片是纯增强，后续可加）；
4. 发送链路完全不变：文本原样进 `POST /sessions`，展开是服务端的事。

### 9.3 展开（服务端）

落点 `_launch_user_message` 之前（与附件 compose 同层），新函数
`expand_skill_invocations(content, skills) -> str`（放 `movieclaw_agent/skills.py`）：

1. **解析**：maka 同款正则（名字字符集放宽为 `[A-Za-z0-9._-]+` 以容忍
   大小写/下划线手误），首现序去重，**一条消息最多展开 8 个**（超出的
   token 原样保留）；
2. **匹配**：对 `discover_skills()` 结果按 name 小写比对；命中的现场读
   SKILL.md、剥 frontmatter，正文超 **32_000 字符截断**并附中文截断注记
   （一次性注入没有 read 分页兜底，必须设上限防单文件撑爆上下文）；
3. **组装**（pi 块格式，中文锚语句）：

   ```
   <skill name="douban-picks" location="/abs/.../SKILL.md">
   技能文件里的相对路径以 /abs/... 目录为锚。

   {正文}
   </skill>

   {剥除已命中 token 后的用户文本}
   ```

   多个技能依次多个块；命中 token 从文本剥除（maka 的行内清理：残留
   多余空白折叠）；剥完为空时补一句「用户未附加说明，按上述技能指令执行」；
4. **未命中的 token 原样保留**（pi 式透传）：技能刚被删/名字敲错时，
   模型看得见原文并能向用户解释，比静默吞掉或整条拒发（maka blocked）
   更符合我们的会话产品形态；
5. **展开后的全文替换 content 入转录**（pi 语义）：内容冻结在调用时刻、
   历史可复现，续轮/压缩/retry 走既有链路零特判。幂等性天然成立：已展开
   文本里的 `<skill ...>` 块不含裸 token，retry 复用旧文本不会二次展开；
6. IM 通道不接入（不经此展开点，token 在 IM 里就是普通文本）——与清单
   的 read 门控同一条边界，docstring 注明。

### 9.4 渲染（前端会话页）

- `entriesToTurns` 用 pi 同款正则解析 user 消息：开头连续的 `<skill>` 块
  拆出 `skills: [{name}]`，余下是用户文本；
- 气泡渲染成**技能 chip**（如 `⚡ douban-picks`）+ 用户文本，技能正文
  P1 不做展开查看（转录里有全文，需要时后续加）；
- 侧栏预览（后端 `message_preview`）：`<skill>` 块替换为 `[技能]` 占位，
  与 `[图片]` 同一处理。

### 9.5 实现清单（P1）

| # | 改动 | 位置 |
|---|---|---|
| 1 | `SKILL_TOKEN_RE` + `expand_skill_invocations()`（解析/匹配/读盘剥 frontmatter/截断/组装）+ 消息解析辅助（供 preview） | `movieclaw_agent/skills.py` |
| 2 | `GET /api/v1/skills`（name/description/scope 列表）+ `_launch_user_message` 前接展开 + retry 链路确认幂等 | `movieclaw_api/api/routes/agent.py`、schemas |
| 3 | `message_preview` 的 `[技能]` 占位 | `movieclaw_api/services/agent_sessions.py` |
| 4 | composer 加号菜单（图片/技能两入口）+ 光标处插 token；`lib/api/agent.ts` 加 listSkills | `apps/web/components/composer.tsx` 等 |
| 5 | 会话页 user 气泡的技能 chip 解析与渲染 | `apps/web/lib/agent-conversations.tsx`、`agent-conversation-view.tsx` |
| 6 | 测试：展开单元（命中/未命中透传/多 token 去重/上限/截断/幂等）、API（转录存展开文/preview 占位/skills 列表）、composer 交互 | `tests/agent`、`tests/api`、前端类型检查 |

### 验收标准（P1）

1. 加号菜单能列出全部技能（含内置 skill-creator），选中后输入框出现
   `/skill:名字 ` 占位符；
2. 发送后模型收到的 user 消息是展开后的技能正文 + 用户文本（真机验证
   模型直接按技能行动、不再自己 read SKILL.md）；
3. 会话气泡显示技能 chip + 用户原文，侧栏预览显示 `[技能]`；
4. 敲错名字的 token 原样透传，模型能向用户解释；
5. retry 不产生重复展开；技能改动后**新的**显式调用用新内容，历史轮次
   保持调用时刻的旧内容。
