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

### 2.1 目录

新增配置 `AGENT_SKILLS_DIR`（默认 `./data/agent-skills`，进 `core/config.py`），
对齐「运行期数据全部落 `data/`」的项目约定——部署挂载 `data/` 卷即持久化，
管理员把技能目录放进去（或再挂一层子卷）即完成安装。

```
data/agent-skills/
├── subtitle-workflow/
│   ├── SKILL.md              # 必需：frontmatter + 指令正文
│   └── scripts/fix-encoding.sh
└── curation/
    └── poster-wall/          # 允许分组文件夹，递归发现
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

### 2.3 发现规则（pi 算法的简化版）

`scan_skills(root)`，从 `AGENT_SKILLS_DIR` 出发：

1. 目录含 `SKILL.md` → 整个目录是一个技能根，**不再深入**（`references/`
   里再放 SKILL.md 不会被当成新技能）；
2. 否则递归子目录；跳过 `.` 开头目录与 `node_modules`；
3. **不跟随 symlink**（目录与文件都 lstat 判断）。比 pi 严格：服务端 data
   卷内没有共享技能目录的需求，一行检查同时防环与防逃逸；
4. 根层裸 `.md` 单文件技能**不支持**（pi 支持，我们砍掉——少一种形态少一类
   歧义，需要时 P1 再加）；
5. 同名（name 小写比对）先到先得，输家不加载并写 warning 日志（含双方路径）；
6. 目录项按名字排序遍历，产出确定性。

产物 `Skill = {name, description, file_path}`（file_path 为 SKILL.md 绝对
路径）。**不缓存正文**——正文永远在模型 read 的那一刻现读，改技能即时生效。

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
`scan_skills` + `build_skills_fragment`，与 external_url「保存即生效、不做
缓存」同一思路——**改技能无需重启，下一轮运行生效**。个位数技能的目录扫描
是几次 stat + 小文件读，成本可忽略。

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
| 2 | `Skill` dataclass + `scan_skills()` + `build_skills_fragment()`，frontmatter 解析（yaml），中文警告日志 | 新文件 `movieclaw_agent/skills.py`（纯库，不依赖 API 层） |
| 3 | `_agent_system_prompt()` 追加扫描与注入 + read 门控依赖说明 | `movieclaw_api/api/routes/agent.py` |
| 4 | 单元测试：发现规则 ×6（短路/递归/缺 description/`name` 回退/同名先到先得/symlink 跳过）、fragment 渲染（转义/空目录 None/超限警告） | `tests/agent/test_skills.py` |
| 5 | API 测试：有技能时 system prompt 含清单、空目录不含；技能改动后新一轮生效 | `tests/api/test_agent_skills.py` |
| 6 | 部署文档补技能目录说明 | README 部署章节 |

预估全部改动 ≤ 300 行（含测试），不动 runner、不动前端、不加迁移。

### 验收标准

1. 放入一个含脚本的真实技能，Web 会话提出匹配任务：模型 read SKILL.md、
   按指令执行脚本、产出正确结果（真机 E2E）；
2. 不匹配的任务不触发技能加载（清单只占元数据成本）；
3. 修改 SKILL.md 后，无需重启，下一条消息的运行即用新内容；
4. IM 通道的运行 system prompt 不含技能清单；
5. 缺 description 的技能不出现在清单中，日志有可读的中文警告。

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

- **P1**：前端技能管理页（列表/查看/启停——启停状态可仿 pi 存 settings 或
  仿 maka 存 `data/agent-skills/.state.json`）；composer 显式调用（选技能 →
  API 传 refs → 服务端组装进 user 消息信封，复用 thinking_level 的信封机制）；
  届时一并支持 `disable-model-invocation`。
- **P2**：技能量级增长后的目录预算与 SkillSearch（maka 路线）；IM 通道技能
  （需先解决 IM 无 read 工具的问题）。
