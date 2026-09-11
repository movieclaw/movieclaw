# 媒体库批量重定位：批量转移与根路径归并

> 状态：设计待拍板（2026-09-11）
>
> 来源：客户在 30T 规模合库场景下提出的能力扩充（593 部电影 + 238 部剧集要从
> 多个库/多个根归并成一个库一个根）。客户的核心诉求是**不要定制化的「合并库」
> 命令**，而是补齐两个通用原语，让合并、换盘、拍平多级目录这一类任务都变成
> 「提交一次」。
>
> 相关：`library.md`（库模型）、`library-manage.md`（管理页）、
> `persistent-jobs.md`（任务模型）、`cli.md`（CLI 两层命令面）、
> `library-routing.md`（收藏范围路由）

---

## 1. 问题：一切操作都围绕单条目，没有「集合」这一层

三个现成能力各管一段，谁都不覆盖「把一批条目搬到另一个位置」：

| 能力 | 代码位置 | 能做 | 不能做 |
|---|---|---|---|
| 条目转移 | `services/library/transfer.py` | 一次搬**一个** `media_item` 的整个条目目录到另一个库的主根 | 一次搬一批；一批里单条冲突只能整体失败 |
| 整理归位 | `services/library/organize.py` | 按命名模板规范化文件名与条目目录名 | 跨根挪动——目标路径是 `_root_of(roots, file_path)`，**永远留在该文件当前所在的那个根下** |
| 路径收口 | `workflow.library.reconcile-paths.*` | 容器挂载前缀变了以后，把旧路径台账收口到新根 | 只处理单对 `old_root → new_root`，且**只改台账不搬文件** |
| 改库根路径 | `library.update` | 换 `root_paths` 并自动补扫 | 不搬文件——文件还躺在旧目录，换完就全标 missing |

于是合并库被迫走「逐条转移 800 次 → 删空库 → 改根 → 再整理」这条链。800 次调用、
800 个 Job、800 次预览确认，是整件事里最大的低效与不稳定源；而「把旧根下的条目
整体归位到新根」这件事，**当前任何一条路径都做不到**。

客户的判断是对的：缺的不是「合并库」这个功能，是**集合**这一层原语。

---

## 2. 设计结论：一个重定位引擎，两个入口

批量转移和根路径归并不是两件事，是同一件事的两种投影——**把一批条目单元搬到某个
目标根，台账逐条随迁**：

| | 批量转移（跨库） | 根路径归并（库内） |
|---|---|---|
| 目标根 | 目标库的主根 | 本库指定的某个根 |
| 台账变更 | `library_id` + `file_path` | 仅 `file_path` |
| 选择集 | 条目 id 集合 / 整库 | 「这些源根下的全部条目」 |
| 收尾 | 刷新两库统计、订阅改挂 | 更新 `root_paths`（摘掉源根） |
| 典型场景 | 合并库、纠正分错的库、按筛选批量归位 | 换盘、换挂载点、拍平历史多级目录 |

因此**不新写两套搬运逻辑**：把 `transfer.py` 里已经验证过的那套（条目目录识别、
混目录降级为逐文件、跨盘复制续传、`_prune_emptied_dirs`、身份锚重定位）抽成
`services/library/relocate.py`，两个入口都是它的调用方。`transfer.py` 保留跨库
特有的部分（`assert_transferable` 的同类型校验、订阅改挂、两库统计刷新）。

> 这一条同时是对客户「不要定制化」的正面回应：产品侧不增加「合并库」概念，
> 增加的是「一批条目 → 一个目标根」这个可组合的动词。

### 2.1 单条目转移怎么办

**保留** `library.items.preview-transfer` / `library.items.transfer` 两个端点，
内部退化成 `media_item_ids=[id]` 的薄委托（约 20 行）。理由：

- `operation_id` 是公开契约（`cli.md` §3.1），删掉即破坏性变更；
- 条目详情页「转移到其他库」的 URL 语义（`/items/{id}/transfers`）与任务中心的
  `job_resource` 关联都更自然；
- 两套端点、一套实现，不存在分叉风险。

---

## 3. 选择集：只有「id 列表」一种形态

批量接口**不接受筛选表达式**，只接受 `media_item_ids: list[int]` 或 `all_items: true`。

否决「在批量接口里重造一套筛选参数」的理由，与 `_filter_params` 被 `/items`、
`/facets`、`/item-index` 三个接口共用是同一个论证：**口径分叉是这类功能最常见的
bug 源**（面板说 593 部、实际搬了 586 部，而且没人说得清差在哪）。筛选面只能有
一份，它已经在 `library.items.list` 上。

两端各自把筛选变成 id 列表：

- **Web**：给 `ui.library.items.ids`（`GET /libraries/{id}/item-ids`）加上
  `Depends(_filter_params)`，它就成了「当前筛选下的全部条目 id」。媒体墙筛选完点
  「全选 → 转移」直接拿到集合。这是一行依赖注入的改动，不新增端点。
- **CLI**：管道。`mclaw library items list` 已经是生成命令、已有全部筛选维度。

一次显式提交的上限定 **2000 个条目**，超过提示改用 `--all`；`--all` 不限。理由见 §4.2
（预检成本按成员数线性增长）。

---

## 4. 预检：批量的成败在这里

单条目转移的预览只回答「这一部搬到哪、多大、是否跨盘」。30T 规模下这远远不够——
**搬到一半才发现空间不够是灾难**。预检必须一次性摆出四件事，并且要快。

### 4.1 四件必须算清的事

**① 目标盘空间够不够 —— 只按跨盘部分算。**
同盘 `rename` 不占任何新空间，把总体积拿去和剩余空间比是错的（会把一次本来零风险
的同盘归并吓停）。`target_required_bytes = cross_device_bytes × 1.05`，5% 余量留给
搬运期间下载器仍在写入。

**② 硬链接会断，而且源盘不会腾出空间。**
跨盘搬运是「复制 → 删源」，复制产生新 inode，做种目录仍指向旧 inode。对
`st_nlink > 1` 的文件，「删源」只是删掉一个链接，**磁盘净增等于该文件大小**。
用户以为搬完源盘腾出 30T，实际一点没腾——这必须在执行前说清楚，因此预检分开给出：

- `target_required_bytes`：目标盘需要吃下的
- `source_reclaimable_bytes`：源盘预计释放的（已扣掉硬链文件）
- `hardlinked_items` / `hardlinked_bytes`：会断种、且不释放空间的部分

**③ 同名冲突逐条列出，并且不阻断整批。**
这是从单条目泛化到集合时的**语义变化点**：单条目的 `blocked` 等于整个操作失败；
批量里「目标已存在同名目录」只是**这一条跳过**，其余 586 条照搬。预检把
`skipped`（逐条，带中文原因）和 `blocked`（整批阻断）分成两个字段呈现。

`blocked` 只留给真正的全局问题：目标根不可访问（盘没挂）、目标盘空间不足、
源库或目标库正忙。

「同名」本身还要再分一层——同一部作品的其他版本、不同作品的目录撞名、身份未知的
目录，是三件事，处理方式完全不同，预检必须分开呈现。判据与动作见 §7。

**④ 跨盘清单。** 哪些成员要复制（耗时按体积走、断硬链），哪些是同盘 rename（秒级）。

### 4.2 预检是 O(成员数)，不是 O(文件数)

593 部 × 每部几十个文件 ≈ 几万次 `stat`。网络挂载（NFS/SMB）上一次 `stat` 毫秒级，
几万次就是几分钟——同步 HTTP 请求扛不住，做成异步 Job 又让「先看一眼」这件事变重。

因此预检**不逐文件 stat**：

| 数据 | 来源 | 成本 |
|---|---|---|
| 成员数、总体积 | 台账 `library_file.size_bytes` | 1 次查询 |
| 同名冲突 | 每成员 1 次目标 `exists()` | O(成员数) |
| 跨盘判定 | 每个源根 1 次 `st_dev` + 目标根 1 次 | O(根数) |
| 剩余空间 | `shutil.disk_usage` | 1 次（已有先例：`services/storage/service.py:154`） |
| 硬链接 | 只对**跨盘成员的视频主文件** `stat`（字幕/NFO 的链接数无意义） | O(成员数) |

593 部约 1200 次 syscall，网络盘上 1–2 秒。逐文件的精确计划留到执行时**逐成员现算**
——这本来就是执行侧的设计（见 §5）。

代价写在 help 里：整库上万条目时预检耗时线性增长，用 `--timeout` 调。

---

## 5. 执行语义：集合冻结，路径重算

现有单条目转移在执行接口里**重新计算** plan（预览到确认之间磁盘可能已变）。批量必须
把这一条拆成两层，否则语义会错：

- **成员集合在预览时冻结**，写进 Job 输入，执行期不再变。用户确认的是「这 593 部」；
  筛选结果会随刮削和扫描漂移，「预览看到 593、执行时变成 601」不可接受。
- **每个成员的路径计划在轮到它时现算**。既保住「以磁盘最新状态为准」，也避免把
  593 部的完整路径计划（几十万条路径，数 MB）一次性塞进 SQLite 的 `job.input_data`
  ——`persistent-jobs.md` 明确「大断点不塞进 SQLite」。

### 5.1 一个 Job，不是 593 个

**否决「父 Job 派生 N 个子 Job」**，尽管 `persistent-jobs.md` 为父子任务预留了
`waiting` 状态：

| | 单 Job + 成员级检查点（**选定**） | 父 Job + N 个子 Job |
|---|---|---|
| 用户心智 | 一个任务、一条进度条（正是客户要的） | 任务中心被 593 行刷屏 |
| 存储 | 1 行 job + 若干 job_event | 593 行 job × 2 resource = 1186 行 job_resource；媒体墙按 resource 聚合会被冲垮 |
| 失败隔离 | 自己做：per-member try/except，记进 result | 天然成立 |
| 单条重试 | 「用失败子集重新发起」——本来就该走一次预览确认 | 天然成立 |
| 锁 | 全程持有源/目标两侧库锁，一次获取 | 593 个子任务抢同一把库锁，锁竞争与 retry churn |
| 新机制 | 无 | 父子租约、取消传播、进度聚合 |

失败隔离的实现代价远小于父子编排的机制代价，而单条重试的诉求用「失败子集重新发起」
满足后语义反而更干净（`简洁优先`）。

### 5.2 检查点与幂等

- **成员级**：已完成成员 id 写进 `job.progress.details.done`，重启后直接跳过。
- **成员内**：完全复用现有 `checkpoint_id` 机制——跨盘复制写目标旁的隐藏续传文件，
  按已复制字节续传，完整后原子发布。几十 GB 不会重来。
- **幂等**：`dedupe_key = "library.relocate:{source}:{target}:{sha1(sorted ids)}"`，
  `conflict_policy="return_existing"`。网络重试不会搬两次。
- **互斥**：全程持有库级任务位（`TaskState`），扫描/整理/重识别/另一次搬运一律挡下
  ——与单条目转移同一套，不新增机制。

### 5.3 结论（result）只存汇总与异常

593 条逐成员明细塞进 `job.result` 是几百 KB 的 JSON。只存：

```
{ moved: 586, skipped: 7, failed: 0, bytes_moved, bytes_reclaimed,
  hardlinks_broken: 210, failures: [...], skips: [...] }
```

成功明细不存——`job_event` 时间线里有。

---

## 6. 根路径归并：两个正确性关键点

`POST /libraries/{id}/root-consolidations`，入参 `{into, from[]}`。`from` 省略即
「除 `into` 外的全部根」。`into` **允许是一个当前不在 `root_paths` 里的新路径**
（换盘、换挂载点场景），这正是客户说的「改根 → 归位 → 删空库」三步并成一步。

### 6.1 先加后删：任何时刻中断，台账都在根内

若 `into` 是新路径，**必须在开始搬之前就把它加进 `root_paths`**，全部搬完才移除
`from` 的根。顺序反了会出事：文件先落到库根之外，下一次扫描（或崩溃后的恢复扫描）
会把它们全标 missing。

先加后删的代价是：任务中途失败时 `root_paths` 里会多出一个根、处于「两个根都有内容」
的半搬完状态。这个中间态是**可观察、可续跑**的——重跑同一条归并命令即可收敛，
比「文件在根外」的静默损坏好得多。

### 6.2 归并期间改配置不能触发自动补扫

`library.update` 在 `roots_changed` 时会 `enqueue_scan_job`（`libraries.py:1177`）。
归并任务自己持着库锁，走那条路会和自己抢锁。归并直接写 `Library.root_paths`，
**不触发补扫**——台账是逐条精确随迁的，不需要靠扫描重建；这与单条目转移搬完不补扫
保持一致。结论页给出「建议扫描」的下一步动作，由用户决定。

### 6.3 与「整理归位」的分工，要在文案里说死

- `organize-files`：**改名字**，永远留在当前根下。
- `consolidate-roots`：**换位置**，不碰名字。

一次操作只做一件事，用户才说得清「刚才那一下到底改了什么」——这是 `transfer.py`
开篇定的哲学，这里继续。两条命令的 help 互相指路。

---

## 7. 同名冲突：判据换成「锚」，动作复用既有多版本约定

### 7.1 今天的判据是错的

`_build_plan_sync` 只做 `dst.exists()` → 整批 `blocked`。这把三种完全不同的情况
混成了一种：

| 目标目录里的台账行挂在 | 实际是什么 | 该怎么办 |
|---|---|---|
| 同一个 `media_item_id` | 同一部作品的另一个版本，或同一份的副本 | **可以合并** |
| 不同的 `media_item_id` | 目录名撞车，压根不是同一部片 | **真冲突，跳过** |
| 没有台账行 | 用户手放的、正在下载的、没扫过的目录 | **身份未知，跳过** |

**「同名」是坏判据，「同锚」才是好判据。** 而且换判据的成本为零——plan 构建里
那句查 `foreign` 的语句本来就是按 `library_id + media_item_id` 查的，加一句
「目标库里挂在同一个 item 上的行」即可，仍是 O(1) 次查询。

### 7.2 台账层面没有任何东西需要合并

这一条把问题缩小了一大半，值得单独说：`MediaItem` 的唯一键是
`(source, kind, external_id)`，**不含 library_id**（`media_item.py:65`）。同一部
TMDB 影片在两个库里本来就是**同一行** MediaItem——`cleanup_orphan_items` 的注释
说得很直白：「还在别的库里（同一部剧的集分散在两个库是常态）→ 保留」。

所以观看进度、收藏、播放次数、订阅**早就是共享的一份**，合并不需要迁移任何一样。
`reanchor.py` 那套 `migrate_watch_state` 在这里用不上——它服务的是「换身份锚」
（重新识别把文件挂到另一条目），不是「同锚的文件换位置」。

**同名合并是一个纯文件系统问题。** 这也是我认为它可以放宽的底气：它不触碰
身份、不触碰用户数据。

### 7.3 合并动作：一条新语义都不发明

确定可合并后，逐文件走 ingest 已经在用的 `_resolve_transfer_target`
（`ingest.py:3289`）那套判据，一字不改：

1. 目标文件不存在 → 直接落位；
2. 存在且 `_same_payload`（同 inode，或同尺寸）→ **不搬**，源文件走回收站（§7.5）；
3. 存在且内容不同 → 退让到 `标题 (年份) - 标签.ext`，标签走 `organize.py` 的
   `_version_labels` 确定性阶梯（分辨率 → +片源 → +发布组 → 按体积 V1/V2）；
4. 退让后仍撞名且内容不同 → 这一条跳过，**绝不覆盖**（继承单条目转移的底线）。

于是合并产出的目录结构与入库、洗版、整理产出的**完全一致**：
`阿凡达 (2009)/阿凡达 (2009) - 2160p.mkv` + `… - 1080p.mkv`。Jellyfin/Emby 认得，
`organize-files` 重跑不会把它们改回去（标签是确定性的，`_version_labels` 的
docstring 就是为这个写的），洗版能继续在这堆版本上工作。

**零新概念**——正是 `quality-upgrade.md` §0 列的第一条产品目标。

### 7.4 原盘是硬边界，不并进去

`disc-version-layout.md` §1 的调研结论：生态里**没有任何一家**支持「同一文件夹内
多版本含原盘」，Jellyfin 官方文档明确 BDMV/VIDEO_TS 不支持多版本、多部分或外挂
字幕。因此源或目标任一为原盘目录（`layout.is_disc_dir`）时不并进条目目录，按
`disc-version-layout.md` §2 的既有约定落到**同级独立目录**
`标题 (年份) - 4K原盘/`——`_avoid_disc_entry_dir` / `_disc_destination` 也已经写好了。

### 7.5 合并只增不减

**铁律：合并绝不删除、绝不改名目标库里已有的任何文件。** 所有判重结论只作用于
**源**（源文件进回收站），永不作用于目标。

这条铁律让最坏情况可控：万一判重判错了（两个不同版本恰好同尺寸——`_same_payload`
只比 inode 与尺寸，这是它已知的精度上限），损失是「多了一份躺在回收站里的源文件」，
而不是「目标库里的好版本被顶掉了」。

去重的源文件走已有回收站（`recycle.py`）：普通文件 `moved_to_trash`，做种中的
走 `kept_in_place` 原地待回收、不断种，保留期内用户可以后悔。**不直接删。**

### 7.6 缺省跳过，合并是显式选项

`--on-conflict skip | merge | fail`，缺省 `skip`（即今天的行为）。

否决「缺省 merge」的理由是**可逆性不对称**：批量场景下用户看不见每一条。593 部里
7 部冲突——缺省跳过的损失是「7 部没搬，得手工处理」；缺省合并的损失可能是「7 部
文件名被改了、3 部被判重扔进了回收站」，而用户根本没细看预检里那一行。
**可逆性不对称时，缺省取可逆的那一侧。**

但缺省跳过会让合库场景变得没用（如果 200 部都冲突）。所以真正的答案不在缺省值上，
而在**预检必须把冲突拆开摆出来**，让用户看清了再选：

```
7 部同名：5 部是同一部作品的其他版本（可合并——新增 3 个版本文件、
2 个重复文件将去重），2 部是不同作品的目录撞名（只能跳过）。
```

CLI 走 `--dry-run` → `--on-conflict merge --yes` 两步；Web 端是弹窗里的三选一单选。
`fail` 留给脚本场景（要么全成要么不动）。

### 7.7 明确不做：合并时顺手选优

**不做「保留好的、删掉差的」。** 那是洗版的职责，它有完整的档位阶梯、基线单调
递增与证伪排除机制（`quality-upgrade.md` §2、§4.3）。在一次磁盘搬运里嵌一个质量
判定，等于把两套语义纠缠到一起——出了问题没人说得清是搬错了还是判错了。

合并只负责「都留下、各就各位」，选优交给用户随后跑洗版或手工删。这是 `transfer.py`
开篇那条「一次操作只做一件事」的贯彻。

---

## 8. API 面

| 端点 | operation_id | x-cli |
|---|---|---|
| `POST /libraries/{id}/item-transfer-preview` | `workflow.library.transfer-items.preview` | `x-cli-hidden` |
| `POST /libraries/{id}/item-transfers` | `workflow.library.transfer-items.start` | `x-cli-hidden`、`x-cli-job` |
| `POST /libraries/{id}/root-consolidation-preview` | `workflow.library.consolidate-roots.preview` | `x-cli-hidden` |
| `POST /libraries/{id}/root-consolidations` | `workflow.library.consolidate-roots.start` | `x-cli-hidden`、`x-cli-job` |
| `GET /libraries/{id}/item-transfer-status` | `library.items.get-transfer-status`（已有） | 扩字段，兼容投影 |

路径命名对齐现有三对工作流端点（`file-organization-preview` / `file-organizations`、
`path-reconciliation-preview` / `path-reconciliations`、`transfer-preview` / `transfers`）。

四个端点全部 `x-cli-hidden`：**正式执行必须走精选层的「预览 → 回显影响面 → --yes」**，
不给生成命令绕过确认的旁路——与 `organize-files`、`reconcile-paths` 同一立场。

`library.items.transfer`（单条目）从生成层改由精选层提供，命令名与位置参数形态保持
兼容（见 §8），`operation_id` 不变。命令树快照 diff 会红一次，需在 PR 里显式确认。

预览响应（批量转移）：

请求体除 `target_library_id` 与选择集外带 `on_conflict: "skip" | "merge" | "fail"`
（缺省 `skip`，见 §7.6）。预览与执行**收同一个值**：预览按该策略算出的影响面，
就是执行会做的事。

```jsonc
{
  "target_library_id": 7, "target_library_name": "电影", "target_root": "/media/movies",
  "on_conflict": "skip",
  "selected": 593, "movable": 586,
  // 同名的三类拆开给，让用户判断该不该改用 merge（§7.1）
  "conflicts": {
    "same_anchor": 5,        // 同一部作品的其他版本 → merge 时可并
    "different_anchor": 2,   // 目录撞名但不是同一部片 → 任何策略下都跳过
    "unknown": 0             // 目标目录没有台账行，身份不明 → 任何策略下都跳过
  },
  // on_conflict=merge 时额外给出合并的预期动作（§7.3）
  "merge_preview": {"new_version_files": 3, "deduped_files": 2, "disc_sidecar_dirs": 0},
  "skipped": [{"media_item_id": 91, "title": "风筝", "reason": "目标库里已存在同名目录…"}],
  "total_bytes": 32100000000000,
  "cross_device_items": 586, "cross_device_bytes": 32100000000000,
  "target_free_bytes": 40000000000000,
  "target_required_bytes": 33705000000000,
  "source_reclaimable_bytes": 21000000000000,
  "hardlinked_items": 210, "hardlinked_bytes": 11100000000000,
  "blocked": []
}
```

`TransferStatusView` 扩 `total_items` / `done_items` / `current_title`；原有
`media_item_id` / `title` 在批量时填**当前正在搬的那一条**，老前端不改也能用。

---

## 9. mclaw CLI 命令面

按 `cli.md` §7 的准入标准——「需要客户端编排或本地状态才收进精选层」。这两条都要
编排「预览 → 确认 → 执行 → 等待」，且要读 stdin 组装选择集，够格。

```
mclaw library items transfer <library_id> [media_item_id] --to <target_library_id>
       [--item ID]...  [--items-from FILE|-]  [--all]
       [--on-conflict skip|merge|fail]
       [--dry-run] [--yes] [--wait] [--wait-timeout 6h]

mclaw library consolidate-roots <library_id> --into <path> [--from <path>]...
       [--on-conflict skip|merge|fail]
       [--dry-run] [--yes] [--wait] [--wait-timeout 6h]
```

**批量命令与单条目命令同名**（overlay 同名覆盖生成命令）。`mclaw library items
transfer 3 42 --to 7` 是单条，`--all` / `--item` / `--items-from` 是同一条命令的
集合形态——批量是单条的泛化，命令面不因此增肥。`--to` 是 `--target-library-id`
的短别名，生成层的原标志名保留。

**筛选走管道，不在这条命令上重造**（§3）：

```bash
# 把库 3 里的韩国内容全部转到库 7
mclaw library items list 3 --c KR --all -o json \
  | mclaw library items transfer 3 --to 7 --items-from - --dry-run
```

`--items-from` 接受两种明确形态（写进 help，不猜）：纯文本一行一个 id；或
`library items list` 的 JSON 输出（抽 `id` 字段）。后者让 Agent 不必依赖 `jq`。

### 8.1 确认与退出码

沿用 `clierr` 契约（`cli/internal/clierr`）：

- `--dry-run`：打印预检，退出 0，绝不动盘；
- 无 `--yes`：打印预检 + 退出码 **5**（需要确认），hint 给出「核对后加 --yes」；
- 预检 `blocked` 非空：退出码 **1**，中文透传原因（盘没挂 / 空间不足 / 库忙）；
- 执行后 `--wait` 超时或任务失败：退出码 **6**。

回显的那一行必须把四件事一次说完，而不是只报个数：

```
转移计划：593 部选中，586 部可搬；跨盘复制 29.2 TiB，目标盘剩余 36.4 TiB
（需要 30.7 TiB，够）；其中 210 部与做种目录有硬链接，复制后做种链接断开、
源盘不会释放这 10.1 TiB。
7 部同名：5 部是同一部作品的其他版本（--on-conflict merge 可并入，将新增
3 个版本文件、2 个重复文件去重进回收站），2 部是不同作品的目录撞名，只能跳过。
```

### 8.2 默认不等待

`--wait` 默认 **false**，与 `persistent-jobs.md`「开始命令默认 `--no-wait`，适合
Agent 和脚本」一致。这里**刻意不跟随** `organize-files` 的 `--wait` 默认 true：
30T 跨盘批量是小时级，默认阻塞会让 Agent 会话挂死。输出里直接给下一步命令
（`mclaw jobs wait <id>`）。

---

## 10. Web 端

- 媒体墙进入**多选模式**（长按/勾选框 → 顶部操作条），已有筛选面板即选择面板；
  「全选」调加了筛选依赖的 `ui.library.items.ids`。
- 转移弹窗从「一部」变成「N 部」，四段式对话框结构不变，预检区按 §4.1 四件事排版。
- 管理页（`/library/manage`）一库一行的 `···` 菜单新增「归并根路径」，多根库才亮。
- 进度与结论复用任务中心，不做第二套状态。

---

## 11. 明确不做

- **不加「合并库」命令**。合并 = 一次批量转移 + 一次删空库，两个已有动词的组合。
- **转移后不自动删源库**。一次操作只做一件事，删库有它自己的确认。
- **不改 `organize-files` 的语义**让它跨根归位（§6.3）。
- **不做撤销**。搬运直接落盘，撤销等于再搬一次；预检就是撤销的替代品。
- **合并时不做选优**（§7.7）。保留哪个版本是洗版的职责，不在搬运里顺手判质量。
- **不为合并发明新的目录/命名约定**（§7.3）。产出形态与入库、洗版、整理完全一致。
- **不做筛选表达式入参**（§3）、**不做父子 Job**（§5.1）、**不做逐文件预检**（§4.2）。

---

## 12. 实施步骤与验证

| 步骤 | 验证 |
|---|---|
| 1. 抽 `services/library/relocate.py`：条目单元识别、逐成员搬运、检查点 | 现有 `tests/api/test_library_transfer.py` 全绿（纯重构，行为不变） |
| 2. 预检 `preflight()`：空间/冲突/硬链/跨盘，O(成员数) | 构造 593 成员的 fixture，断言 syscall 次数 < 2× 成员数 |
| 3. 批量转移两个端点 + 单条目端点改薄委托 | 单条目端点响应逐字段与改动前一致 |
| 4. 批量 Job 处理器：成员级检查点、per-member 隔离 | 第 300 个成员注入异常 → 前 299 个已落盘、result 记 1 条 failure、其余照搬完 |
| 5. 中断恢复 | 搬到一半杀进程 → 重启后从 `done` 续跑，不重搬、不重复计数 |
| 5.5 同名合并：按锚分类 + 复用 `_resolve_transfer_target` / `_version_labels` | 同锚两版本 → 目标目录出现两个带版本标签的文件且 `organize-files` 重跑不改名；同锚同尺寸 → 源进回收站、目标一字未动；异锚同名 → 任何策略下都跳过；原盘 → 落同级独立目录 |
| 6. 根归并端点 + 先加后删的配置更新 | 归并中途杀进程 → `root_paths` 含两个根、全部台账都在某个根内、重跑收敛 |
| 7. `ui.library.items.ids` 加筛选依赖 | 与 `/items` 在同一筛选下条数一致（口径不分叉） |
| 8. CLI 两条精选命令 | `--dry-run` 零写请求；无 `--yes` 退出码 5；`--items-from -` 两种输入形态都吃 |
| 9. 命令树快照更新 | 快照 diff 只含预期的四个新端点与 transfer 的层归属变化 |

发布相关：本设计**不动运行时依赖**（无新增 pyproject 依赖、无 Node/系统包变更），
不需要 bump `docker/runtime-version`；**不新增 `data/` 下目录**（跨盘续传临时文件
落在目标根旁的隐藏路径，沿用现有机制），不涉及 `storage/registry.py` 登记。

---

## 13. 需要产品拍板

1. **单条目端点保留还是删**？本文推荐保留为薄委托（§2.1）。删掉能少一组端点，
   但 `operation_id` 是公开契约，且条目详情页的 URL 语义会变差。
2. **显式选择集上限 2000** 合不合适？定太低会逼用户用 `--all`（范围更大更危险），
   定太高预检会慢。
3. **空间余量 5%** 够不够？做种目录还在写的场景下，用户可能需要更保守的值，
   或者做成库级配置。
4. **归并完成后要不要自动跑一次扫描**？本文选不跑（§6.2）。跑一次更保险，
   代价是 30T 库的扫描本身要小时级，且会把「归并完成」的结论页拖长。
5. **`--on-conflict` 的缺省值是 `skip` 还是 `merge`**？§7.6 推荐 `skip`（可逆性
   不对称），但合库是这个功能的头号场景，缺省 `skip` 意味着用户第一次跑总要再跑
   第二次。折中方案：缺省仍 `skip`，但预检发现「可合并的同锚冲突 ≥ 1」时，CLI 的
   hint 与 Web 的弹窗直接把 `merge` 作为推荐下一步摆出来。
6. **`_same_payload` 的判重精度够不够**？它只比 inode 与尺寸（`ingest.py:2979`），
   两个不同版本恰好同尺寸就会被判成重复。§7.5 的「只增不减 + 进回收站」把后果
   兜成了「多一份可恢复的源文件」，但如果要更准，得读首尾若干 MB 做采样哈希——
   30T 规模下这是几分钟的额外 IO。当前推荐维持现状（与入库同一精度，口径不分叉）。
