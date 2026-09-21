# 反馈到上游：诊断包、issue 草稿与一键提报（响应 #434）

> 背景：#434 描述的现场——字幕生成失败 → 「交给 Agent 处理」→ Agent 翻日志、
> 跑 mclaw、定位出两个 bug，最后只能说「我没有提交 issue 的入口，给你一份报告
> 自己去贴」。Agent 握着全部证据却在最后一步断了；非开发者用户到这里多半放弃。
> 本文回答两个问题：① 没有 GitHub 账号/未登录的用户，从产品点一下能走到哪；
> ② Agent 会话里的「上报问题」交互应该长什么样、后端要补什么。

---

## 0. 结论先行

1. **提交 issue 必须有 GitHub 账号，没有任何绕过方式**——GitHub 不接受匿名
   issue。产品能做到的极限是：把「登录/注册」之外的所有步骤都替用户做完，
   让用户点开链接后只剩「登录 → 看一眼 → 点提交」三步。
2. **零配置档用 GitHub 的预填链接**（`/issues/new?template=…&<字段id>=…`）。
   实测（2026-09-21，未登录访问 movieclaw/movieclaw）：
   - 链接总长 ≤ 约 7.5 KB：302 到登录页，`return_to` 带着完整链接，登录后
     回到预填好的表单；
   - 7.8 KB 左右：仍 302 登录页，但 **`return_to` 被丢弃**，登录后落在首页、
     预填全丢；
   - ≥ 8.1 KB：直接 `414 URI Too Long`。

   因此**预填链接只能装「摘要」，装不下诊断包**（一个中文字符 URL 编码后
   9 字节，7 KB 大约只够 600 个汉字加少量 ASCII）。完整证据必须走别的通道
   （§3.3）。
3. **Agent 侧不新增「提交」能力，只新增「起草」能力**：草稿由服务端按诊断包
   与 Agent 的分析拼装，人在会话里过目、改、点「去 GitHub 提交」。issue 是
   公开的，Agent 不能替用户决定公开什么——这一条与 #434 的立场一致，也是
   本设计的硬边界。
4. **自动档（PAT / Device Flow 直接建 issue）不在本期**：它解决的是「用户
   已有账号但不想切浏览器」的便利问题，不解决「没账号」问题，而且引入令牌
   保管面。先把零配置档做扎实，看真实使用再决定。

## 1. 未登录 / 无账号用户的可达路径（研究结论）

### 1.1 GitHub 侧的机制

| 机制 | 是否可用 | 说明 |
|---|---|---|
| `issues/new?title=&body=` | ✅ 官方文档明确支持 | 最稳的两个参数；`labels`/`template`/`assignees`/`milestone`/`projects` 同级 |
| `issues/new?template=01-bug-report.yml&<字段id>=值` | ⚠️ 可用但非官方承诺 | 官方文档只有一句「issue form 字段的查询参数也可以传给模板选择器」；社区实测 `input`/`textarea` 字段按 `id` 预填有效，`dropdown` 不生效，`checkboxes` 无法预填（community #15477、#32200） |
| 未登录访问预填链接 | ✅ | 302 到 `/login?return_to=<完整链接>`，登录后回到预填表单（§0 实测，链接 ≤ 7.5 KB 才成立） |
| 无账号 → 注册后回到预填表单 | ❓ 未验证 | 登录页有「Create an account」入口，本仓库沙箱访问 GitHub 登录页被 403，无法确认注册流是否保留 `return_to`。需要人工用无痕窗口验证一次 |
| 手机装了 GitHub App | ⚠️ 已知坑 | 点链接会被 App 拦截，App 不处理预填参数（community #113726）。NAS 用户很多在手机上操作，文案里要提醒「用浏览器打开」 |
| 匿名 `GET /search/issues` | ✅ | 未认证 10 次/分钟，够做「可能已有 #xxx」的去重提示；走 `UPDATE_API_BASE_URL` 同一通道（用户可自建反代） |

### 1.2 对本产品的含义

- **`blank_issues_enabled: false` 不影响预填**：查询参数会跟着进入模板
  选择器，选中 bug 表单后各字段仍按 `id` 落位。但保险起见，链接里**显式带
  `template=01-bug-report.yml`**，跳过选择器直达表单。
- **表单字段只预填 `input`/`textarea` 类**：`title`、`description`、
  `reproduce`、`version`、`logs`、`extra` 可填；`deploy`、`area` 两个下拉
  与「提交前确认」两个勾选框**必须用户手点**——这三个恰好是「人必须过目」
  的天然停顿点，不用额外设计确认步骤。分诊工作流依赖的 `version`/`deploy`
  字段（`claude-issue-triage.yml`）因此仍能拿到值。
- **预填的 `version` 用运行中的版本**：#434 里 Agent 把 0.23.0 报成 0.26.0，
  原因就是让模型自己去 grep 源码里的 `__version__`（overlay 与镜像基线各有
  一份）。诊断包必须给 `build_status().current_version`（`services/app_update.py`）
  这个**运行中进程**的值，模型不再自己找。
- **没有账号的用户**：产品无法替他们提交。唯一的「无账号」方案是维护者自建
  一个中转（Cloudflare Worker + 仓库机器人令牌，收匿名 POST 后代建 issue），
  代价是垃圾/滥用面和运维一个公网服务，**本期不做**；但诊断包与草稿的产物
  设计成「一份可下载的 Markdown 文件」，无账号用户至少能把它发给群友/维护者
  代提（§3.3）。

## 2. 总体方案：三层，只做前两层

```
┌──────────────────────────────────────────────────────────────┐
│ ① 诊断包  GET /system/diagnostics（服务端脱敏，机器可读）          │
│    版本/部署/平台/LLM 类型 + 按 job/file/session 抽日志片段        │
├──────────────────────────────────────────────────────────────┤
│ ② 草稿    POST /feedback/draft（诊断包 + Agent 分析 → 表单字段）   │
│    Agent 工具 propose_issue_report_v1（render-only，前端画卡片）   │
│    设置页「反馈问题」按钮（不经 Agent，直接起草）                  │
├──────────────────────────────────────────────────────────────┤
│ ③ 提交    零配置档：预填链接（摘要）+ 诊断包文件（拖进表单）        │
│           自动档：PAT / Device Flow ——本期不做                    │
└──────────────────────────────────────────────────────────────┘
```

与现有架构的对应关系（不引入新模式，全部沿用已有的三条先例）：

| 本设计 | 沿用的先例 | 出处 |
|---|---|---|
| 服务端组装证据、前端只拿文本 | 「交给 AI 分析」诊断工单 | `services/diagnosis_handoff.py`、`routes/agent_handoff.py` |
| Agent 工具只描述、不执行，前端按工具名拦截绘制 | `show_media_cards_v1` | `movieclaw_agent/tools/media_ui.py`、`docs/design/agent-generative-ui.md` |
| 按通道决定是否装配工具 | `get_agent_tools(generative_ui=…)` | `routes/agent.py` |

## 3. 各层设计

### 3.1 诊断包 `GET /system/diagnostics`

新增 `services/diagnostics.py`（纯函数，可被路由、mclaw、Agent 工具共用），
返回结构化 JSON；`mclaw system diagnose` 由 spec 自动生成命令面。

**内容**（全部是「不带密钥」的事实）：

| 段 | 字段 | 来源 |
|---|---|---|
| 版本 | `version`、`code_source`（baseline/overlay/dev）、`overlay_version`、`runtime_version`、`model_tag` | `app_update.build_status()`，一律取运行中的值 |
| 部署 | `deploy`：Docker（`MOVIECLAW_RUNTIME_VERSION` 存在）/ 源码；`platform`（`platform.platform()`、`machine`）；`python`；`web_port` | 环境变量 + `platform` 模块 |
| 转码 | 硬件转码后端、远程 Worker 是否启用 | `transcode_worker` 配置（只给类型，不给地址） |
| LLM | 供应商类型与模型名 | `llm_config`（**不带 key、不带自建 base_url**） |
| 站点/下载器 | 各自的数量、类型、启用状态 | 只给 `site_id` 类型与数量，不给站点名（PT 站点名本身是敏感信息） |
| 日志片段 | `logs[]`：按 `job_id` / `file_id` / `session_id` / 时间窗抽取 | 复用 `routes/logs.py` 的按天文件读取，加过滤。现状：日志页只有查看，没有导出/脱敏 |
| 关联任务 | `jobs[]`：`job_type`、`status`、`error`、`input_data`（脱敏后） | `Job` 表 |
| 近期异常 | `last_abnormal_exit`、活跃 `notices` 标题 | `build_status()`、`system_notice` |

**过滤参数**：`?job_id=&file_id=&session_id=&since=&until=&log_lines=`。
按资源过滤时，从 `JobResource` 反查任务 id 与时间窗，再在日志里按
任务 id / 关键字抽取，默认前后各 200 行、总量上限 64 KB——Agent 不再需要
自己 `grep` 整个日志文件（这正是 #434 里「Agent 自己翻日志」的替代）。

**脱敏在服务端做，不靠 Agent 自觉**（#434 原话，作为硬约束）：

- 正则层：`cookie=`/`Cookie:`、`passkey`、`api_key`/`apikey`、`token`、
  `Authorization:`、`secret`、`password` 的取值整体替换为 `***`；
  `magnet:`/`.torrent?…passkey=` 链接整体替换；
- 字典层：从数据库现读 `SiteCredential`（站点 id → 站点域名/名称）、
  下载器 URL、外部访问地址、路径映射里的宿主机目录，逐个整词替换为
  `<site-1>`、`<downloader-1>`、`<external-url>`、`<host-path-1>`；
- 路径层：`/home/<用户>`、`/Users/<用户>`、`/volume1/<共享名>` 这类用户目录
  统一为 `<user-dir>`；
- 输出末尾附「脱敏说明」一段，告诉用户替换了哪几类，方便他判断是否还有
  漏网之鱼。

诊断包接口本身**管理员专属**（与 `agent-handoff` 同口径）：即使脱敏了，
它仍是一份运维视角的全景。

### 3.2 草稿 `POST /feedback/draft` 与 Agent 工具

**草稿的形状 = bug 表单的字段**（`.github/ISSUE_TEMPLATE/01-bug-report.yml`），
不是一段自由 Markdown。这样前端渲染、预填链接、文件导出三条出口共用一份
数据，字段 id 与表单一一对应：

```json
{
  "title": "字幕生成失败：大文件预检挂住请求",
  "description": "实际发生：……\n期望行为：……",
  "reproduce": "1. …\n2. …",
  "version": "0.23.0",
  "area": "AI 助手",
  "logs": "<脱敏后的日志片段，≤ 2 KB 摘要>",
  "extra": "环境摘要（部署方式/平台/转码后端/LLM 类型）+ 「完整诊断包见附件」",
  "similar_issues": [{"number": 432, "title": "…", "state": "open"}],
  "diagnostics_id": "diag-20260921-153012"
}
```

- `area` 只是给用户的建议值（下拉预填不了，见 §1.2），前端在卡片里显示
  「建议选：AI 助手」；
- `similar_issues` 来自匿名 `GET /search/issues?q=repo:movieclaw/movieclaw+<标题关键词>`
  （未认证 10 次/分钟），请求与更新检查同一条路：`update_api_base_url` +
  `movieclaw_net.egress` 的 `github` 出口标签（走用户配置的代理/镜像）；
  失败（离线、限流）静默为空，不阻塞起草；
- `diagnostics_id` 指向本次生成的诊断包文件（落在 `data/diagnostics/`，需在
  `storage/registry.py` 登记，可清理，保留 7 天）。

**两个入口共用同一个服务函数** `build_feedback_draft(session, *, summary, analysis, job_id, file_id, agent_session_id)`：

1. **Agent 工具 `propose_issue_report_v1`**（`movieclaw_agent/tools/feedback.py`）：
   - 参数：`title`、`description`、`reproduce`（可选）、`area`（枚举，取
     表单的选项文本）、`job_id`/`file_id`（可选，决定日志按什么抽）；
   - handler **不写文件、不发网络请求**，只做参数校验并返回 `ok`（与
     `show_media_cards_v1` 完全同一模式）。前端拦截到 tool_call 后调
     `POST /feedback/draft` 拿到完整草稿（服务端此时才拼诊断包、脱敏、
     查重），画成「问题反馈卡片」；
   - 为什么 handler 不直接生成草稿：草稿内含脱敏后的日志与查重结果，是
     给人看的；进转录只会占上下文、且转录里落一份可能过期的日志快照。
     卡片按 `diagnostics_id` 现取，和媒体卡片「不留过期快照」同一取舍；
   - description 里的触发条件：**Agent 判断问题是产品缺陷（而非配置错误/
     环境问题）且已完成分析时**主动提出；用户明确说「帮我反馈/提个 issue」
     时必须用；不要在还没定位的时候就提；
   - 与 `show_media_cards_v1` 同样只在网页会话装配（`get_agent_tools(feedback=True)`），
     IM 通道没有卡片界面，不带。
2. **设置页按钮**（「设置 → 更新与维护」的版本区，`components/app-update-section.tsx`
   → 「反馈问题」）：不经 Agent，
   `POST /feedback/draft` 只带 `summary`（用户自己填一句话），其余由诊断包
   填充。这是「Agent 没配 LLM」的用户也能走的路。

### 3.3 提交：零配置档

前端「问题反馈卡片」（`components/agent-feedback-card.tsx`）上三个动作：

1. **「去 GitHub 提交」**：打开预填链接。链接只装**摘要**：
   `template=01-bug-report.yml&title=&description=&reproduce=&version=&logs=<前 1.5 KB>&extra=`，
   前端拼完后**校验编码后总长 ≤ 6 KB**（留出登录页 `return_to` 的裕量），
   超出则按 `logs` → `reproduce` → `description` 的顺序截断并在 `extra`
   里写「完整内容见附件诊断包」；
2. **「下载诊断包」**：`GET /feedback/drafts/{diagnostics_id}/bundle` 得到
   `movieclaw-diagnostics-<时间>.md`（草稿全文 + 脱敏日志 + 环境）。表单的
   「补充信息」框支持拖文件上传，文案明确写「把这个文件拖进最后一个输入框」。
   没有账号的用户拿着这个文件也能请人代提；
3. **「复制全文」**：剪贴板兜底（手机上文件拖拽不方便）。

卡片顶部固定一段提醒：「issue 是公开的，提交前请通读一遍」+ 「若手机装了
GitHub App，请用浏览器打开链接」。查重结果非空时在卡片里列「可能已有：
#432 …」，点击直达。

**为什么不在链接里塞完整正文**：§0 的实测——超过 7.5 KB 登录后就丢，超过
8 KB 直接 414；而一份有价值的日志片段轻易过 10 KB。

### 3.4 自动档（记录，不做）

设置里填 `public_repo` 权限的 PAT，或走 Device Flow，服务端调
`POST /repos/{owner}/{repo}/issues`。复用 `UPDATE_API_BASE_URL` 通道。
草稿数据结构已按表单字段组织，届时只需把字段拼成 Markdown 正文
（issue forms 提交后的正文就是「### 字段标题\n\n值」的固定格式，机器分诊
仍可解析）。不做的原因见 §0 第 4 条。

## 4. 实现清单

| # | 改动 | 位置 | 备注 |
|---|---|---|---|
| 1 | `DIAGNOSTICS_DIR` 配置 + registry 登记 | `core/config.py`、`services/storage/registry.py` | CLAUDE.md 第 4 条硬约束 |
| 2 | 诊断包组装与脱敏 | 新 `services/diagnostics.py` | 纯函数；脱敏单测覆盖每一类 |
| 3 | `GET /system/diagnostics` | 新 `routes/diagnostics.py` | 管理员；spec 自动生成 `mclaw system diagnose` |
| 4 | 草稿组装 + 查重 + 文件导出 | 新 `services/feedback.py` | 查重走 `update_api_base_url`，失败静默 |
| 5 | `POST /feedback/draft`、`GET /feedback/drafts/{id}/bundle` | 新 `routes/feedback.py` | `x-cli-hidden`（与 handoff 同） |
| 6 | Agent 工具 `propose_issue_report_v1` + 装配开关 | `movieclaw_agent/tools/feedback.py`、`routes/agent.py` | 守护测试同 `test_media_ui_tool_wiring.py` |
| 7 | 前端卡片 + 拦截 + 设置页按钮 | 新 `components/agent-feedback-card.tsx`、`lib/agent-feedback.ts`（纯解析，仿 `lib/agent-media-cards.ts`）；接入点 `agent-conversation-view.tsx` 的 `TurnView`（媒体卡片组之后）与 `ProcessBlock` 的过滤清单；按钮放 `app-update-section.tsx` 版本区 | 链接长度校验有 node --test |
| 8 | 系统提示词环境段加一句 | `routes/agent.py::_agent_system_prompt` | 「确认是产品缺陷后用 propose_issue_report 提出反馈，不要让用户自己去复制粘贴」 |

分期：**P0 = 1–5 + 设置页按钮**（无 Agent 也能用，先把诊断包与预填链接跑通，
人工验证注册流 `return_to` 与表单字段预填）；**P1 = 6–8**（Agent 卡片）。

## 5. 验收标准

1. 无痕窗口点「去 GitHub 提交」：登录后落在 bug 表单，`title`/`description`/
   `version`/`logs` 已填、两个下拉与勾选框为空待填；
2. 同一链接在已登录状态直接打开表单，不出现 414；构造 20 KB 日志的草稿，
   链接仍 ≤ 6 KB 且 `extra` 提示见附件；
3. 诊断包里搜不到任何站点域名、cookie、passkey、下载器地址、宿主机用户目录
   （单测用含这些字段的假日志断言）；
4. Agent 会话：复现 #434 场景（字幕任务失败 → 交给 Agent），模型在定位后
   调用 `propose_issue_report_v1`，卡片出现、版本号与「关于与更新」页一致；
5. 未配置 LLM 时设置页「反馈问题」按钮仍可用；
6. IM 通道的工具集不含 `propose_issue_report_v1`。

## 6. 待验证 / 待拍板

- [ ] 注册流是否保留 `return_to`（人工无痕验证；不保留则文案改为「先注册再点链接」）；
- [ ] issue forms 字段预填在当前 GitHub 版本上的实际表现（社区有「时好时坏」
      的反馈，需用真实账号点一次；不可用时退回 `title=&body=` + Markdown
      正文，分诊工作流改为从正文标题解析）；
- [ ] 诊断包是否要包含 Agent 会话转录摘要（会话里可能有用户的个人表述，默认
      不带，只带 Agent 最后一轮的结论文本）；
- [ ] 自动档是否值得做：看 P0 上线后「下载诊断包」与「去 GitHub 提交」的
      使用比例。
