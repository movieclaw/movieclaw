# 媒体库可见范围：每个库都有「谁能浏览」——实施计划（P1）

> 状态：**P1 已实施（2026-09-05）**。迁移 `b8d4e2f1a9c3`、判定收口
> `services/library/access.py`、清除记录 `services/playback_history.py`；
> 回归测试 `tests/api/test_library_access.py`、`tests/jellyfin/test_library_access.py`。
> 实现与本文的三处偏差见 §9。第一版以「私密库」为独立类型，评审后收敛：
> 私密不是类型，只是「可见范围＝指定成员」这种状态的标签；唯一真正新增的
> 语义是**超管的浏览也受可见范围约束**。
> 关联：[member-management.md](member-management.md) §3.6（库访问模型，
> 本文是其演进）、[library.md](library.md)、
> [library-home-recently-watched.md](library-home-recently-watched.md)、
> [activity.md](activity.md)、[jellyfin-compat.md](jellyfin-compat.md)。
> 后续分期（不在本文范围）：P2 Jellyfin 设备级开关、Web 会话解锁态、
> 库级「不记录播放」、日志脱敏、备份清理提示。

## 0. 一句话定义

**每个库都有一个可见范围：「所有成员」或「指定成员」。谁在范围内谁能浏览；
超管永远能管理，但只有把自己加进范围才能浏览。**

三条原则：

1. **管理权与浏览权分离**。管理权是超管身份自带的，作用于库的配置、扫描、
   整理、待处理、回收站；浏览权作用于内容（海报墙、详情、播放、聚合面），
   对所有身份走同一条判定，超管不豁免。
2. **可见性只管可见性**。它不改变库的其他行为：自动路由、默认库、监听导入、
   订阅投递照旧。一个只对孩子可见的动画库，路由照样能把动画投进去。
3. **不可见就彻底不可见**。任何跨库聚合面、图片资产、协议投影，对范围外的
   主体都不含该库内容。今天有几处做不到，属漏洞，本期一并修。

## 1. 可见范围模型

### 1.1 两侧各一个开关，一张矩阵

| 侧 | 字段 | 含义 |
|---|---|---|
| 库 | `access_mode` = `everyone`（默认）/ `selected` | 是否对「全部成员」自动开放 |
| 库 | `admin_visible`（默认 true） | 超管本人是否在浏览范围内 |
| 成员 | `all_libraries`（现有，默认 true） | 是否自动包含全部 `everyone` 库（含未来新建） |
| 库 × 成员 | `member_library_access`（现有关联表） | 显式授权行 |

可浏览集合：

```
超管会话            = {everyone 且 admin_visible} ∪ {selected 且 admin_visible}
                    = {admin_visible 为真的库}
成员 all_libraries  = {everyone} ∪ 该成员的显式授权行
成员 白名单          = 该成员的显式授权行（everyone / selected 一视同仁）
PAT / Agent 令牌     = {everyone}
Jellyfin member_id=0 = 同超管会话
```

- `admin_visible` 默认 true，所以存量库对超管零变化；把库改成「指定成员」时，
  表单里的「超管（我自己）」一项就是这一列，默认保持勾选，超管自己决定要不要
  把自己摘出去。
- 显式授权行对 `everyone` 库也有意义：它让 `all_libraries=false` 的成员看到
  这个库。库设置页与成员管理页写的是同一行，两处互通。
- 令牌主体只看 `everyone` 库：CLI 输出与 Agent 对话会进日志和会话记录，
  不该带指定成员才能看的内容。以后需要再给单个令牌加 `include_selected`。

### 1.2 不做的事（第一版有、本版删除）

- 不设「私密库」类型，不做 shared/private 切换规则，改模式不清空白名单。
- 不把 `selected` 库排除出自动路由，不与默认库互斥。
- 不动订阅投递语义（成员订的内容可能落在他看不到的库里，仍是 §3.6 的既定语义）。

## 2. 产品改动点与操作路径

### 2.1 建库 / 编辑库：「可见范围」组

**位置**：媒体库首页 → 添加媒体库；单库页 → ⋯ → 编辑。放在表单最后，与
「从首页排除」同组，组名「可见范围」。

- 单选：**所有成员**（默认）/ **指定成员**。
- 选「指定成员」后展开一个**可搜索的多选下拉**（与成员管理页同款交互）：
  输入昵称或用户名过滤，列表里点选或回车切换，已选的人显示为可移除的标签。
  第一个选项固定是「**超管（我自己）**」（默认选中），其后是全部
  `status=active` 成员，显示昵称（空则用户名）。
- 名单里一个都没勾时常显提示：「当前没有任何人能浏览这个库的内容。你仍然
  可以在这里管理它。」
- 从「指定成员」改回「所有成员」：名单收起，已勾的行保留（对白名单成员仍有
  意义），`admin_visible` 保留原值。
- 保存即生效：被移出范围的主体下一次请求起看不到；进行中的播放不中断。
- 新建库默认「所有成员」，与今天完全一致。

### 2.2 成员管理页（`/settings/members`）

- 「可见范围」两档不变：**全部库** / **指定库**。
- 「全部库」的说明改为：「自动包含全部『所有成员』的库，含以后新建的；
  『指定成员』的库需要单独勾选。」
- 「指定库」的多选列表里，`selected` 库带「指定成员」标签，勾选写同一张表。
- 成员卡片的范围摘要：`all_libraries` 成员写「全部库 + 指定成员的库：家庭录像」；
  白名单成员写库名列表。

### 2.3 媒体库首页（`/library`）

- **成员**：范围外的库完全不出现（库列表接口本就按可浏览集过滤）。
- **超管**：范围外的库仍出现在卡片区，但是**管理形态**：无封面拼图，用锁图标
  占位；显示库名、类型、条目数与体积，加一个「仅管理」标签；待识别 / 缺失胶囊
  照常。点击进入管理视图（2.4）。范围内的库与今天一致（评审后去掉了原计划的
  「指定成员」小标签：谁能看在设置里一目了然，卡片上再标一次没有决策价值）。
- **「最近添加」分区与库封面拼图**：只对可浏览库拉条目。
- **「最近观看」行**：只含可浏览库内的记录。记录本身保留，主体回到范围内即恢复
  显示。

### 2.4 单库页（`/library/{id}`）：管理视图

触发条件：当前主体是超管且该库不可浏览（`admin_visible=false`）。

- 保留：库头部（名称、统计、「仅管理」标签、锁图标）、⋯ 菜单（编辑 / 扫描 /
  整理 / 元数据刷新 / 删除库）、运行状态胶囊、待处理抽屉（待识别 / 复核 /
  缺失 / 已忽略 / 回收站）。
- 不渲染：海报墙、未识别分区、追踪中分区。原位置一句提示：「内容已隐藏。
  把自己加入可见范围即可浏览。」加按钮「把我加入可见范围」（打开编辑对话框并
  定位到可见范围组）。
- 成员对范围外库的任何接口一律 404（沿用「不泄露存在性」）。超管对范围外库的
  **浏览类**接口（条目列表、详情、播放）也是 404，**管理类**接口照常 200。

### 2.5 全站聚合面与协议投影

以下出口对范围外主体一律不含该库内容：

| 出口 | 现状 | 改动 |
|---|---|---|
| `GET /libraries` | 成员按可见集过滤，超管全量 | 超管全量保留，每库带 `access_mode` / `admin_visible` / `member_ids` / `viewer_access` |
| 首页最近添加、封面拼图 | 前端逐库拉 `/items` | 前端只拉 `viewer_access=true` 的库；后端浏览接口对范围外超管 404 |
| 首页最近观看 `GET /playback/recent` | 成员按可见集，超管不限 | 超管也按可浏览集 |
| 全局搜索 `GET /search/library-items` | 同上 | 同上 |
| 发现页「已入库」徽标、详情页入口 | 同上 | 同上 |
| 活动页 `GET /playback/activity`（管理员） | 跨成员全量 | 超管不可浏览的库内记录折叠为一行「N 条记录（不在你的可见范围）」，不出片名与海报 |
| Jellyfin `/UserViews`、`/Items`、Latest、Resume、NextUp、搜索、人物 | 成员按可见集，超管设备不限 | 超管设备也按 `admin_visible`；`user_policy()` 对超管改为 `EnableAllFolders=false` + `EnabledFolders` |
| 图片资产 `GET /images/assets/{media_item_id}/…` | 仅登录 | 条目所属库不在主体可浏览集 → 404 |
| 条目详情、播放决策、取流、字幕、缩略图 | 按可见集 | 超管也按可浏览集 |
| PAT / Agent 令牌 | 等价管理员 | 只见 `everyone` 库 |

### 2.6 播放记录删除

三处入口，同一接口，只删**当前登录主体自己的**记录：

| 入口 | 位置 | 语义 |
|---|---|---|
| 清除本片记录 | 条目详情页 ⋯ → 「清除观看记录」 | 该条目全部季集的 `playback_state` 与关联 `playback_metric` |
| 清空本库记录 | 单库页 ⋯ → 「清空我的观看记录」 | 该库内全部条目 |
| 清空全部记录 | 设置 → 个人信息 → 「观看历史」卡片 → 「清空全部观看记录」 | 全部 |

- 超管删的是 member_id=0 的记录；跨成员删除不提供，活动页不给删除按钮。
- 每处二次确认，说明「续播进度与已看标记一起清除，无法恢复」。
- 删除后首页最近观看、Jellyfin Resume/NextUp 即刻不再出现（同一张表）。
- 确认框附一句：「应用更新前的自动备份仍包含历史记录。」清理备份放 P2。

## 3. 数据模型

`library` 加两列，迁移只加列带默认值，旧代码可读（发布规范第 3 条）：

```
access_mode    VARCHAR NOT NULL DEFAULT 'everyone'   -- everyone / selected
admin_visible  BOOLEAN NOT NULL DEFAULT 1            -- 超管本人在浏览范围内
```

- 超管不是成员行，不为哨兵 0 拆 `member_library_access` 的外键，超管授权落
  库上一列。
- 存量库 `everyone` + `admin_visible=1`，行为零变化。
- 无状态切换规则，无与 `is_default` 的耦合。

## 4. 后端改动

### 4.1 判定收口（`services/library/access.py`）

`visible_library_ids` / `member_visible_ids` 从「None = 不受限」改为**总是返回
具体集合**，全部消费点删除 `if visible is not None` 分支（这正是当初收口的
回报：改一处，消费点只做删分支的机械修改）。新增 `manageable_library_ids`：
超管 = 全部库，成员 = 空集。

集合按请求算一次（一条 `library` 查询 + 一条授权行查询），挂在 `Principal`
上缓存，请求内多处消费不重复查。

### 4.2 库配置服务与接口

- 创建 / 更新接受 `access_mode`、`admin_visible`、`member_ids`（整体覆盖该库的
  授权行）。`member_ids` 与成员接口的 `library_ids` 写同一张表，互通不需特殊处理。
- `GET /libraries` 超管响应逐库带 `viewer_access`（= 当前主体可浏览）。

### 4.3 聚合面逐个落实

- `playback_recent.py`、`discover_library.py`、`search_visible_library_items`、
  `playback/plan.py`、`libraries.py` 浏览类路由：改用 4.1 的集合，删 None 分支。
- `playback_activity.py::_recent_plays`：按 library_id 分流，范围外的记录聚合为
  `MediaRecentPlayView(hidden=True, count=N)`，前端渲染占位行。
- `images.py::get_metadata_asset`：路径首段是 `media_item_id` 时，查条目
  `scrape_library_id` 所在库是否在主体可浏览集，否则 404。用进程内
  「库 id → access_mode/admin_visible」小缓存（库配置写入时失效），避免每图一查。
- Jellyfin：`identity.py::user_policy()` 超管也走 `EnableAllFolders=false` +
  `EnabledFolders`；`ViewerScope` 对 member_id=0 不再返回 None；`persons.py`
  与搜索同步。

### 4.4 播放记录删除接口

```
DELETE /playback/history?scope=item&media_item_id=…
DELETE /playback/history?scope=library&library_id=…
DELETE /playback/history?scope=all
```

`require_login`，作用于 `principal.member_id`（超管=0）。同一事务删
`playback_state` 与按 `library_file_id` 关联到同成员的 `playback_metric`。
响应 `data={deleted_states, deleted_metrics}`，中文 message。审计日志只记成员
与数量，不记片名。`scope=library` 要求该库在主体可浏览集内（范围外无从得知
条目，也不该能删）。

## 5. 前端改动

| 文件 | 改动 |
|---|---|
| `library-view.tsx` | 表单「可见范围」组（单选 + 可搜索多选下拉 + 空名单提示）；库卡片管理形态；最近添加与封面拼图只拉 `viewer_access` 库 |
| `library-detail-view.tsx` | `viewer_access=false` 时的管理视图；⋯ 菜单「清空我的观看记录」 |
| `library-item-detail-view.tsx` | ⋯ 菜单「清除观看记录」 |
| `members-section.tsx` | 「全部库」文案；库多选里的「指定成员」标签；卡片摘要 |
| 活动页观看视角 | 占位行 |
| 设置 → 个人信息 | 「观看历史」卡片与清空按钮 |
| `lib/api/libraries.ts` / `playback.ts` | 新字段与新接口 |

## 6. 明确接受的边界（P1 不做）

- 待识别 / 缺失 / 回收站 / 作业中心 / 下载任务 / 通知 / 系统日志里的文件名与
  种子名对超管可见。这是管理动作自带的信息，P1 靠「管理入口需主动点进」控制；
  日志脱敏放 P2。
- 范围内的库与普通库行为一致：会出现在范围内主体的首页、最近观看、Jellyfin
  首页。共享屏幕上的进一步收紧（设备开关、会话解锁）是 P2。
- 不做「不记录播放」模式；隐私靠删除兜底。
- 更新前自动备份中的历史记录不随删除清理，只提示。

## 7. 测试与验收

后端（pytest，`tests/api` 与 `tests/jellyfin`）：

1. 可浏览集合：超管 admin_visible 真/假、`all_libraries` 成员、白名单成员、PAT，
   对 `everyone` 与 `selected` 库各一条。
2. 范围外超管：`GET /libraries` 含该库且 `viewer_access=false`；
   `/libraries/{id}/items` 404；库配置 200；扫描 200；待识别清单 200。
3. 聚合面：最近观看、搜索、发现页徽标、活动页占位、Jellyfin Views/Latest/
   Resume/NextUp/人物、图片资产，各一条「范围外不含、范围内含」。
4. 互通：库设置写 `member_ids` 后成员接口的 `library_ids` 同步，反之亦然。
5. 播放记录删除：三种 scope 只删当前成员；metric 随删；其他成员记录不动；
   `scope=library` 对范围外库 404。

前端：`node --test` 覆盖「可浏览 / 仅管理」形态判定与表单展开逻辑的纯函数；
其余走 lint / typecheck 与 NAS 真机验收。

真机验收路径（NAS）：把「其他」库改为「指定成员」并取消勾选自己 → 超管首页
卡片变「仅管理」、最近添加无该库、最近观看无其记录、VidHub（超管登录）看不到
该库、直接请求其条目海报 404 → 勾回「超管（我自己）」→ 全部恢复 → 新建一个
成员账号不勾选 → 该成员登录 Web 与 VidHub 均看不到 → 在成员页或库设置页任一处
勾选 → 另一处同步显示、该成员可见。

## 8. 实施顺序

1. 迁移 + 模型 + `access.py` 判定改造 + 全部消费点删 None 分支（同一 PR 内完成，
   中间态会让超管失去范围外库的管理入口）。
2. 库配置服务与接口字段、与成员接口互通。
3. 聚合面：活动页占位、图片资产校验、Jellyfin 超管策略投影。
4. 播放记录删除接口。
5. 前端五个面 + 设置页观看历史卡片。
6. 测试补齐，NAS 真机验收。

一个 PR，后端约 15 个文件、前端约 8 个文件，一条迁移，`runtime-version`
不需要 bump（无运行时依赖变化）。

## 9. 实施记录与偏差（2026-09-05）

1. **消费点保留 `set[int] | None` 签名**。`access.py` 对任何请求主体都返回具体
   集合，但 catalog / plan / recent 等二十来处的 `if visible is not None` 分支
   没有删：None 在那里只剩「内部管理流程直接调用时不受限」一个用途（封面
   渲染、扫描），删分支属无收益改动，按精准修改原则留着。
2. **活动页的范围外记录用计数字段而不是占位行**：`MediaActivityView` 加
   `hidden_recent_count`，前端在最近观看列表尾部渲染一行「另有 N 条记录不在
   你的可见范围内」。条目跨库时只要有一个库可浏览就照常展示，详情落点取可
   浏览库里 id 最小的那个；没有任何台账行的条目（文件已删只剩记录）不受
   范围约束。
3. **监听导入规则表单未动**：可见性不影响入库路径，规则里的库下拉不需要标签。
4. **没有做 Principal 级缓存，也没有 `manageable_library_ids`**：可浏览集合是
   两条只读小查询（库表整取 + 授权行），一次请求里最多算两三次，先不加缓存；
   管理权仍由路由上的 `require_admin` 表达，不需要单独的集合函数。

另外落地的两处收口：库配置读接口新增 `require_library_readable`（超管对任何
库可读配置，成员仍按可浏览集），成员管理页保存「全部库」的成员时只回写
「指定成员」模式库的显式授权行，不再把整份白名单清空。

