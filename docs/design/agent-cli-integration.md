# 产品内 Agent 集成 mclaw：独立工具设计（tool 描述 + 提示词思路）

> 背景：movieclaw_agent 已有 bash/read/write/edit 四个基础工具，P1 已完成
> 工作区自动授权。**集成方式定为：新增一个独立的 `mclaw` 工具**——不在 bash
> 描述上捎带，而是让模型在「选工具」这一层就看见一个明确的产品操作入口，
> 工具描述本身携带一级服务目录（有哪些 router/命令族可用）。
>
> 这与现有提示词架构完全同构：`prompts.py` 的既有原则是「正文只写通用行为
> 准则，**领域语义由各工具的 description 承载**」——mclaw 的一切知识都放进
> 它自己的工具描述，系统提示词正文**零改动**。

---

## 0. 为什么独立工具优于 bash 捎带（本设计的三个硬收益）

1. **选择面清晰**：模型决定「用什么做这件事」时看的是工具列表。独立的
   `mclaw` 工具让「操作产品 → 选 mclaw」一步成立，不需要模型先想到 bash
   再想起里面装了个 CLI；bash 回归纯粹的通用 shell 定位。
2. **令牌隔离（安全升级）**：MOVIECLAW_TOKEN 只注入 mclaw 工具的子进程，
   **不再进 bash 的环境**——bash 里 `env`/`echo $MOVIECLAW_TOKEN` 从此拿不到
   凭证，泄漏面从「整个 shell」收窄到「一个不透传环境的专用工具」。
3. **硬闸位点**：递归禁令（不允许在工具里调用 `session start/retry/follow`）从提示词
   软约束升级为工具 handler 里的代码硬闸，模型绕不过去。

---

## 1. 工具定义（评审点 ①：参数面与执行语义）

```python
# movieclaw_agent/tools/mclaw.py
def make_mclaw_tool(
    workdir: Path,
    extra_env: dict[str, str],      # MOVIECLAW_SERVER / MOVIECLAW_TOKEN（只给本工具）
    service_map: str,               # 一级服务目录文本，由 API 层从 spec 渲染后传入
) -> AgentTool: ...
```

参数 schema（刻意最小——mclaw 自身就是完整的参数体系，工具不再重复建模）：

```json
{
  "type": "object",
  "properties": {
    "args": {
      "type": "string",
      "description": "mclaw 后面的完整参数串（不含 mclaw 本身），如 'subscriptions list' 或 'search torrents \"沙丘2\" --resolution 2160p'"
    },
    "timeout": {
      "type": "number",
      "description": "超时秒数（可选，默认 300；长任务等待时适当调大）"
    }
  },
  "required": ["args"]
}
```

执行语义：

- **`shlex.split(args)` 后以 argv 直接执行 mclaw 可执行文件，不经 shell**
  ——没有管道/重定向/变量展开，注入面为零；需要组合处理输出时，模型把
  JSON 结果交给 bash/read 等其他工具，职责分明。
- 子进程环境 = 进程环境 + extra_env（令牌仅此处注入）；cwd = 工作区。
- **硬闸**：`session start/retry/follow` 不执行子进程，防止当前 Agent
  递归创建或嵌套跟随会话；`login`/`logout` 同样拦截。任何 `--server` 参数
  也被拒绝，避免把短时令牌发往其他服务器。
- 输出组装（复用 bash 工具的截断实现）：

```
<stdout 截断后内容>
[stderr]
<stderr 截断后内容>
[退出码 7：结果有歧义——stdout 是候选清单，选定后按提示重跑]
```

  **退出码语义标注是本工具独有的运行时教学**：handler 按退出码契约附一行
  中文解读（0 不标注；1 业务错误看 stderr；2 用法错误先 --help；3 授权失效
  应停止并报告用户；5 需要 --yes；6 等待超时任务仍在后台；7 歧义待选）。
  模型即使没读过任何文档，也能从工具结果本身学会正确的下一步。
  唯一例外是 `session get-transcript`：它返回未经工具层截断的完整轨迹，
  description 明确提醒长会话结果可能很大，由模型自行决定是否读取和如何使用。

## 2. 工具 description（评审点 ②：产品能力地图 + 使用协议）

description = 静态协议文本 + 动态服务目录（`service_map` 拼接）。全文如下：

```text
movieclaw 的官方命令行工具。用于从 TMDB 和豆瓣发现实时热点、最新、热映/在播、
热门和高分电影/剧集，搜索并下载 PT 资源，持续订阅追更并自动整理入库，管理本地
媒体库；也可查看任务进度，以及配置资源站点、下载器、规则、消息渠道、AI 模型、
网络和应用更新。查询或变更 movieclaw 产品状态都用本工具完成（不要通过 bash
调用，bash 环境没有授权）。授权已自动配置，永远不需要 login。

可用服务（按用户意图选一级目录；参数细节用 --help 现查，如 args="sub --help"）：
- app      应用设置与维护（设置外部访问地址、重启；检查/升级/回退应用，更新 NER
  模型并查看进度/兼容性）
- appearance 首页背景与图库（查看、上传、下载、切换和删除背景图）
- auth     个人信息与 CLI 访问（查看身份，修改头像/昵称/密码，管理 API 令牌）
- channels 消息推送与 AI 对话入口（微信、Telegram、Discord 配对/解绑，配置事件
  推送并测试；绑定后可发消息搜片、订阅、查进度）
- discover 发现电影/剧集（来自 TMDB、豆瓣的实时热点、热映/待上映/在播、热门、
  高分、口碑及地区/类型片单；list-collections 列片单，browse-collection 浏览片单，
  get-title-details 看资料/演职员/剧照/相关推荐）
- dl       qBittorrent/Transmission 下载器与投递（接入/验证/启停/设默认实例，
  配置保存路径与路径映射，预演落点并提交种子）
- extension Chromium 浏览器插件 Cookie 同步（管理同步令牌/支持站点，把页面中的
  httpOnly 站点 Cookie 安全同步到服务端）
- health   API 存活检查（通常优先用顶级 status 查看更完整的部署状态）
- jobs     后台作业（按来源/状态/类型查询，查看事件与执行器健康，等待/取消/重试；
  订阅的在途下载进度看 sub downloads）
- library  本地电影/剧集媒体库（建库与默认路由，扫描和规范命名入库；查看库存条目与
  物理文件，处理待识别/错识别/缺失内容，并管理元数据、图片、字幕和跨库转移）
- llm      AI 模型供应商（接入 OpenAI、阿里云百炼或任意 OpenAI 兼容服务，选择
  模型并验证连通性，供 AI 对话等智能能力使用）
- net      网络与代理（配置全局/指定服务代理及镜像地址，立即生效；按 TMDB/豆瓣/
  GitHub/PT 站点等服务测试连通性）
- notices  系统待处理事项（查看按严重程度排序的活跃问题，或忽略指定提示）
- people   本地媒体库影人档案（按 TMDB 人物 ID 查看资料及已入库参演作品）
- rules    订阅过滤规则组（管理分辨率、编码、HDR、字幕/音轨、免费/H&R、做种数、
  体积和制作组等条件及默认规则组）
- search   统一搜索（titles 搜 TMDB/豆瓣影视条目，torrents 跨 PT 站点搜种子并可把
  结果行号交给 download，library-items 搜已入库内容；另可管理搜索预设和历史结果）
- session  用户与智能体的会话管理（发起新对话或继续已有对话，按指定用户消息重新提问，
  读取并分析完整 message/compaction 轨迹；也可重命名、压缩上下文、跟随或停止处理，
  以及删除会话）
- site     PT 资源站点（查看支持目录/鉴权要求，配置、验证、启停站点，查看本地种子
  缓存统计；Cookie 可由 extension 同步）
- sub      电影/剧集订阅与自动追更（持续追踪新资源，按规则自动搜索、下载并整理
  入库；支持消歧/选季、缺口工单、立即搜索/手动选种、暂停恢复、活动/下载进度与链路体检）
- transcode 远程硬件转码 Worker（配置远程转码开关、地址与产物上限，查看 Worker 状态）
- ui       Web 界面质感与显示偏好（读取或整体保存各页面的布局、显示和样式设置）
- watch    监听导入（监控已完成且稳定的下载，自动识别、标准命名并转移到目标媒体库；
  查看并认领、忽略或恢复异常条目）
- webhook  事件 Webhook（把播放、收藏等事件以 JSON 推送到外部端点；配置 HMAC
  签名，测试投递、查记录并轮换密钥）
- download 下载：把上次 search 的结果行号投递到下载器；默认先识别 TMDB 身份并
  预演智能入库，唯一且路由可用才提交。退出码 7 时读取 stdout 候选并用
  `--tmdb-id` 重试；也可显式指定 `--library`/`--save-path`，只有明确使用
  `--downloader-default` 才落下载器默认目录。站点+链接形态不含媒体身份，不自动猜测
- status   部署总览：服务健康、当前身份、客户端/服务端版本与命令目录同步状态

使用协议：
- 常用链路：search titles 找片/找剧 → subscriptions create 订阅；search torrents
  搜 PT 种子 → download 投递；search library-items 查库存，discover 浏览榜单，
  library 管理已入库内容；订阅会持续追踪，并在出现符合规则的新资源
  后自动搜索、下载和整理入库。
- 输出即数据：stdout 是 JSON（默认），stderr 是过程提示与错误原因。
- 参数拿不准就先 --help（域级与命令级都有，含示例），不要凭记忆猜参数或取值。
- 列表默认有条数上限、长字段有截断；下结论前确认数据没有被截断（--limit 可调）。
- 带 ⚠ 的命令需要 --yes 确认。其中 library items delete 会删除磁盘上的媒体文件：
  必须先用只读命令查清将删除的具体条目、向用户复述并取得本轮明确同意后才能
  执行；用户泛泛说「清理/整理」不构成删除文件的同意。其余 ⚠ 命令（删配置、
  清记录）在用户任务明确要求时可直接 --yes。
- 扫描/整理/元数据刷新默认阻塞等待完成；预计超过 4 分钟的任务用 --no-wait
  启动后轮询进度，或调大本工具的 timeout 参数。
```

设计取舍说明（供评审）：

- **能力地图进 description，参数合同进 `--help`**：21 个开放域各用一行说明
  用户能完成什么，并只点出有助于选路的代表性入口；181 个生成接口的完整参数
  不平铺进提示词，避免膨胀和漂移。当前完整 description 约 2.4k 字符。
- **前端用户心智是文案基线，CLI 域是执行边界**：能力名和结果承诺沿用页面文案
  （如“持续追踪后自动下载入库”“消息渠道也是 AI 对话入口”），但一个页面聚合的
  多项设置仍拆回准确域：资源站点对应 `site/search/extension`，订阅对应
  `sub/rules`，外观对应 `appearance/ui`。这样既让模型理解产品，也不会选错命令。
- **域集合自动、语义人工**：开放域集合来自 CLI 内置 spec，保证不会静默漏域；
  每域的用户语义由 `_DOMAIN_LINES` 人工维护，测试强制每个开放域都经过润色。
  新域在运行期仍可回落 `DOMAIN_HELP` 保证可用，但提交代码时会被守卫测试拦下，
  必须补齐功能介绍并显式评审快照。
- **明确相邻域分工**：工具协议直接说明 discover=浏览榜单、search=搜索影视/种子/
  库存，并给出「搜影视 → 订阅」「搜种 → 投递」「查库存 → 管理」三条常用链路，
  降低模型仅凭技术名词误选命令的概率。
- **危险规约放 description 而非系统提示词**：这是 mclaw 的领域语义，按
  现有架构归工具承载；且模型每次调用工具时 description 都在注意力窗口里，
  比相隔很远的系统提示词更「贴现场」。
- **退出码表不进 description**：由工具结果的运行时标注承载（§1），
  省 token 且教在事发现场。

## 3. 系统提示词与其他工具：几乎零改动（评审点 ③）

- `prompts.py` 正文：**不改**。通用准则（先查证、并行调用、工作循环）
  已经覆盖 mclaw 的使用姿势；领域语义全部在工具 description。
- 环境段：**不改**（仍只有日期）。部署状态一条 `status` 就能查，不预注入。
- bash 工具：**revert P1 的 extra_env 注入**（令牌改为只进 mclaw 工具），
  description 不加任何 mclaw 内容。bash/read/write/edit 回归纯工作区定位。
- 装配（`routes/agent.py`）：

```python
def get_agent_tools(cli_env: dict[str, str]) -> list[AgentTool]:
    workdir = ...
    return [
        *builtin_tools(workdir),                      # bash 不再携带 cli_env
        make_mclaw_tool(workdir, cli_env, render_service_map()),
    ]
```

`render_service_map()` 放 `movieclaw_api/services/mclaw_tool.py`：数据源为
`movieclaw_api.services.spec_catalog.load_spec()`（自动发现开放域）+
`_DOMAIN_LINES`（人工维护用户语义与关键入口），进程内缓存一次；`DOMAIN_HELP`
同时承载 `mclaw --help` 的域级短简介，并作为新域运行期的安全回落。

## 4. 安全设计（双硬闸 + 令牌收窄）

| 防线 | 位置 | 拦什么 |
|---|---|---|
| 工具 handler 硬闸 | `make_mclaw_tool` | `session start/retry/follow`（递归）、`login/logout`、`--server` |
| 服务端硬闸 | `session.start/retry/stop` 路由 | 持 `agent:` 身份令牌开始、继续或重试会话，以及停止自身会话 |
| 令牌收窄 | 只注入 mclaw 工具子进程 | bash 里 `env` 再也看不到 MOVIECLAW_TOKEN |

服务端硬闸实现（同前版设计）：

```python
async def start_session(payload, identity: Principal = Depends(require_login), ...):
    if identity.kind == "agent":
        raise BadRequestException("Agent 工作区内不能再发起新的 Agent 运行（禁止递归）")
```

## 5. 落点与测试

| 改动 | 文件 | 性质 |
|---|---|---|
| mclaw 工具（schema/描述/handler/硬闸/退出码标注） | `movieclaw_agent/tools/mclaw.py`（新） | 核心 |
| 截断工具函数抽公用 | `tools/bash.py` → `tools/_output.py` | 小重构 |
| bash 撤销 extra_env | `tools/bash.py`、`tools/__init__.py` | revert |
| 服务目录渲染器 | `movieclaw_api/services/mclaw_tool.py`（新） | 渲染自 spec |
| 装配 + 递归服务端硬闸 | `movieclaw_api/api/routes/agent.py` | 两处小改 |

测试：
1. **description 快照测试**：完整工具描述（含渲染目录）全文快照——描述是
   模型行为的一部分，改动必须显式过评审；
2. **目录同步守护**：service_map 覆盖的域集合 == spec 非 hidden 域集合；
3. **硬闸测试**：`session start/retry/follow`、`login` 与 `--server` 返回拒绝
   文本且未起子进程；服务端 agent 令牌开始会话/发送消息或停止自身会话 → 400；
4. **令牌隔离测试**：bash 子进程 `echo $MOVIECLAW_TOKEN` 为空，mclaw 工具
   子进程能成功调用（e2e：真实 uvicorn + 真实 mclaw）；
5. **退出码标注测试**：构造 5/7 退出码场景，断言工具结果含对应中文解读；
6. **golden 任务（人工）**：「我的订阅有哪些」（只读）、「订阅沙丘2」
   （消歧链路）、「整理 1 号库」（危险确认链路：验证模型先报影响面再执行）。

## 6. 待你拍板的评审点汇总

① 参数面：单一 `args` 字符串（shlex 解析、无 shell）+ `timeout`，是否够用；
② description 全文（尤其一级目录的取舍粒度、危险规约措辞、「不要用 bash
直接调 API」的排他性表述）；
③ bash 撤销令牌注入——bash 里将无法调 mclaw（没有授权），这是特性而非
缺陷（一切产品操作走专用工具），确认接受；
④ 退出码语义标注放工具结果（运行时教学）而非 description，确认此取舍。
