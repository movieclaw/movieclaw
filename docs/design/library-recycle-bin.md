# 媒体库管理页：「回收站」分区

> 状态：设计稿 + 样稿（2026-09-06），未实现。
>
> 样稿：`docs/design/mockups/library-recycle-bin-demo.html`（四屏：桌面列表 / 「立即清理全部」确认 / 手机端 / 空状态）
>
> 相关：`library-file-recycle.md`（「待回收」第三态与公共延迟删除，本文是它 §7 末尾「留作扩展」
> 的那个全局面板）、`library-manage.md`（管理页，本文往它 §5 预留的标签栏里放第一个新标签）、
> `quality-upgrade.md`（洗版，当前唯一的触发方）

## 1. 问题

「待回收」已经是 `library_file` 的正式生命周期状态，但**只在条目详情页的文件区可见**：一部片一行、
一集一行。洗版跑起来之后，待回收文件是跨条目、跨库累积的（NAS 实测一次洗版替换 57 个旧文件、
53 GB），用户面对的问题是"回收站里到底有多少东西、占多少空间、哪些快到期、我要不要一次性清掉"，
这些问题逐条目点进去看回答不了。`library-file-recycle.md` §7 当时判断"低频管理不值得一个页面"，
现在管理页已经存在，加一个分区的成本足够低。

## 2. 设计结论

**在 `/library/manage` 标题下插入标签栏，回收站是第二个标签**（`?tab=recycle`）。

- 不做独立路由：回收站的服务对象、权限门（`canManageLibraries`）、页头和侧栏高亮与管理页
  完全一致，`library-manage.md` §5 本来就预留了标签栏，只是当时没有第二个标签可放。
- 标签栏本期只有「媒体库 N」「回收站 N」两段；「自动入库」「待处理」按原计划另开 PR 时追加。
  回收站计数为 0 时标签仍渲染（用户需要知道这个入口存在，这是 `library-file-recycle.md` §1
  列的第一条缺陷），只是不带数字。
- 标签切换用现有 `useTabParam`，与 IM 推送、条目问题抽屉同一套写法。

### 2.1 页面结构

```text
‹ 返回媒体库
媒体库管理                                                            [＋ 创建媒体库]
库负责盘点与守护；自动入库负责把下载完成的内容搬进库。
[媒体库 12]  [回收站 57]
57 个文件 · 53.2 GB · 12 个将在 24 小时内自动清理                        [立即清理全部]
[⌕ 按片名或文件名搜索] [全部库 57][电影 41][剧集 14][4K 电影 2]  [洗版替换 52][洗版证伪 5]
┌──┬──────────────┬──────────────────┬──────────────────┬──────────┬────────┐
│☐ │ 条目           │ 文件               │ 原因               │ 自动清理   │ 操作    │
└──┴──────────────┴──────────────────┴──────────────────┴──────────┴────────┘
第 1–20 / 57                                                   ‹ 1 2 3 ›
```

选中若干行后，表格上方的摘要行被**批量操作条**替代：`已选 3 个 · 4.1 GB   [恢复所选] [立即清理所选]`。

### 2.2 摘要行

一句话回答"回收站里有多少、要紧不要紧"：`N 个文件 · 总大小 · M 个将在 24 小时内自动清理`。
24 小时内到期的数为 0 时省去第三段；有「原地待回收」（移入回收站失败、文件仍在原位）的行时
追加 `· K 个仍在原位`。右侧唯一的页级按钮「立即清理全部」，作用域是**当前筛选结果**（见 §2.5）。

### 2.3 筛选

| 控件 | 内容 | 实现 |
|---|---|---|
| 搜索 | 匹配片名 / 剧名 / 文件名 | 服务端 `q` 参数（列表已分页，不能客户端过滤） |
| 库胶囊 | 全部 + 有待回收文件的每个库，带计数；没有待回收的库不出现 | `library_id` |
| 原因胶囊 | 洗版替换 / 洗版证伪 / 手动（词表来自 `trash_context.reason`，只渲染计数 > 0 的） | `reason` |

默认排序：`purge_after` 升序（最先被自动清理的排最上，`NULL` 不自动清理的排最后），再按
`trashed_at` 降序。不提供排序切换——回收站的主问题是"哪些快没了"，一个固定顺序够用。

### 2.4 列定义

| 列 | 内容 | 来源 |
|---|---|---|
| ☐ | 多选，表头全选当前页 | — |
| 条目 | 海报缩略图、片名 + 年份（剧集：剧名 + `S01E03` + 集名），第二行库名；片名链接到条目详情页 `/library/{lib}/item/{item}` | `media_item` + `library` |
| 文件 | 文件名（等宽）、第二行 `大小 · 分辨率 · 片源 · 制作组` 的胶囊；「原地待回收」形态加 `原地` 警示胶囊，悬停说明"移入回收站失败，文件仍在原路径" | `LibraryFile` 既有列 + `trash_original_path IS NULL` |
| 原因 | `trash_context.note` 整句（如"洗版替换：1080p WEB-DL → 2160p Remux"），第二行触发方 `trigger.label`（"《九门》订阅洗版"）+ 进入时间 | `trash_context` `trashed_at` |
| 自动清理 | 倒计时：`3 天 4 小时后`；24 小时内为警示色 `18 小时后`；`purge_after IS NULL` 显示 `不自动清理`。复用详情页 `purgeCountdown` 的算法 | `purge_after` |
| 操作 | `恢复` / `立即清理` 两个行内按钮，与详情页 FileRow 一致；不用 ··· 菜单，回收站的行只有这两件事 | — |

### 2.5 批量操作与确认

- **立即清理全部**：作用域 = 当前筛选（搜索 + 库 + 原因）命中的**全部**行，不只当前页。
  确认弹窗写明数量、总大小、其中原地待回收的数量，以及"删除不可撤销、若文件仍在做种会中断
  做种"两句后果；按钮文案带数字：「清理 57 个文件」。
- **立即清理所选 / 恢复所选**：作用域 = 勾选的 id。清理走同一个确认弹窗；恢复不弹确认
  （恢复可逆：恢复后可以再删）。
- **不提供「恢复全部」**：洗版替换的旧版本恢复回去就是与新版本共存，整批恢复几乎不是用户想要
  的；真要恢复一批，全选当前页再点「恢复所选」即可（20 个一页，够用）。
- 批量结果 toast 一句回执：「已清理 55 个文件，2 个失败」，失败项留在列表里并在行上标红原因
  （后端逐个返回 `error` 文案）。

### 2.6 空状态与手机端

- 空状态：「回收站是空的」+ 一句解释：洗版替换下来的旧版本会在这里停留 7 天再自动删除，期间
  可以恢复。不放按钮。
- 手机端（`max-md`）：一行压成一卡：第一行海报 + 片名 + 年份 + 倒计时；第二行文件名；第三行
  原因；底部 `恢复` `清理` 两个按钮。多选在手机端保留（复选框在卡片左上），批量操作条改为
  底部悬浮。分页控件同桌面。

## 3. 接口

全部 `dependencies=[Depends(require_admin)]`，响应走 `ApiResponse`。

### 3.1 列表

```text
GET /libraries/trashed-files?q=&library_id=&reason=&limit=20&offset=0
```

```json
{
  "total": 57,
  "total_bytes": 57120000000,
  "due_within_24h": 12,
  "kept_in_place": 1,
  "by_library": [{"library_id": 1, "name": "电影", "count": 41}, …],
  "by_reason":  [{"reason": "upgrade_replaced", "count": 52}, …],
  "files": [
    {
      "id": 8801, "file_name": "…", "file_path": "…", "size_bytes": …,
      "resolution": "1080p", "media_source": "WEB-DL", "release_group": "…",
      "kept_in_place": false,
      "trashed_at": "…", "purge_after": "…",
      "reason": "upgrade_replaced", "note": "洗版替换：1080p WEB-DL → 2160p Remux",
      "trigger_label": "《九门》订阅洗版",
      "library": {"id": 1, "name": "电影"},
      "media_item": {"id": 3021, "title": "九门", "year": 2025, "kind": "movie",
                     "season_number": 0, "episode_number": 0, "episode_title": null,
                     "poster_url": "…"}
    }
  ]
}
```

- 聚合字段（`total*` / `due_within_24h` / `kept_in_place` / `by_*`）按**搜索 + 库 + 原因筛选后**
  的口径计算，与摘要行、胶囊计数、「立即清理全部」的作用域三者同一份数字。
- 分页用现有 `Annotated[int, Query(ge=…)]` 的 `limit/offset` 写法，加 `total` 是为了画页码——
  回收站是管理表格，批量操作需要用户先知道总数，滚动加载不合适。
- 一次查询：`library_file` 按 `state='trashed'` 走既有 `ix_library_file_state`，join `media_item` 与
  `library`。剧集的集名从 `media_item` 的季集表取，与详情页文件区同源。

### 3.2 批量

```text
POST /libraries/trashed-files/purge    body: {"ids": [..]} 或 {"filter": {"q", "library_id", "reason"}}
POST /libraries/trashed-files/restore  body: {"ids": [..]}
```

- 两者互斥：`ids` 用于「所选」，`filter` 用于「全部」。服务端按 filter 重新查一遍 id 再逐个执行，
  不信任前端传来的 total。
- 逐文件调用既有 `purge_file` / `restore_file`，每个文件单独 commit，失败不回滚已成功的；返回
  `{"done": 55, "failed": [{"id": …, "file_name": …, "error": "…中文原因"}]}`。
- **同步执行，不做后台任务**：清理是 `unlink`（原盘目录才 `rmtree`），57 个文件毫秒级；上限用
  `filter` 一次最多处理 500 个，超过时返回 `done` 并由前端提示"还有 N 个，再点一次"。真出现
  几千个待回收再升持久化任务，那时页面只需把按钮换成进度条。
- `purge` 标 `openapi_extra={"x-cli-dangerous": "destructive"}`，与单文件接口一致。

### 3.3 标签计数

管理页加载时已经拉 `listLibraries`，给 `LibraryStats` 加 `trashed_count`（`refresh_stats` 里照
`stats_missing_count` 的写法加一列），标签「回收站 N」= 各库之和，**不为一个数字多打一个接口**。
回收站标签激活后再打 3.1。

## 4. 文件改动（实施时）

| 文件 | 改动 |
|---|---|
| `src/movieclaw_api/routes/libraries.py` | 3.1 / 3.2 三个路由，放在现有 restore/purge 单文件接口旁 |
| `src/movieclaw_api/schemas/library.py` | `TrashedFilesData` / `TrashedFileView` / 批量请求与结果 schema；`LibraryStats.trashed_count` |
| `src/movieclaw_db/repositories/library_repo.py` | `refresh_stats` 加 `stats_trashed_count`（迁移加一列，默认 0，向前兼容） |
| `apps/web/components/library-manage-view.tsx` | 标题下插入标签栏；`tab === "recycle"` 时渲染 `LibraryRecycleBin` 代替库表格 |
| `apps/web/components/library-recycle-bin.tsx`（新） | 摘要行、筛选、表格 / 手机卡片、多选、分页、确认弹窗 |
| `apps/web/lib/api/libraries.ts` | `listTrashedFiles` / `purgeTrashedFiles` / `restoreTrashedFiles` |
| `apps/web/lib/library-recycle.ts`（新）+ `test/library-recycle.test.mjs` | 纯函数：倒计时分档、摘要文案、字节格式化；`node --test` |
| `src/movieclaw_api/data/spec.json` 等 | `scripts/export-spec.sh` 重新导出 |

无运行时依赖变化，不 bump `docker/runtime-version`。

## 5. 明确不做

- 手动删除文件改走回收站（`library-file-recycle.md` §11 立场不变，另开）。
- 保留期可配置、按库配置。
- 排序切换、按日期范围筛选。
- 「恢复全部」（见 §2.5）。
- 回收站里的文件预览 / 播放。
