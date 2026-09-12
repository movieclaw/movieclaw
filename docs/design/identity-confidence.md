# 身份置信度贯穿——同名同年错配的系统性治理

> 状态：**P0–P5 后端全部落地**（P2/P4 处于 shadow 观察期，见 §10.2；P5 的两个
> 按钮待前端接入既有端点，见 §9.4）。起因是一例真实错配（§0）。行文沿用 `subscription.md` /
> `quality-upgrade.md` 的惯例：每节先给结论（决策），再给理由与被否掉的备选。
> 六个工作项按 §3 的优先级独立交付，彼此不阻塞（P1 是 P2–P5 的地基，但 P0
> 可以先走）。

## 0. 起因：一次真实错配

用户订阅了诺兰版《奥德赛》(2026)。系统在 SSD 站搜到
`The.Odyssey.2026.1080p.AMZN.WEB-DL...`，判定身份成立、投递、下载、静默入库、
刮削。实际下到的是 Marcel Walz 版《The Odyssey》——**同名、同年、不同片**。

两条身份守卫都是"通过"的，而且通得毫无争议：

| 守卫 | 订阅条目 | 种子 | 结果 |
|---|---|---|---|
| 别名覆盖率（`identity.py:_match_alias`） | `The Odyssey` | 片名段 `theodyssey` | 100% 覆盖，过 |
| 年份约束（`identity.py:_year_compatible`） | 2026 | 2026 | 精确相等，过 |

入库侧同样没有出错：`_wanted_identity`（`ingest.py:2557`）按 `info_hash` 反查到
这正是为该订阅投出去的种子，于是继承了投递时锚定的身份——这个"零猜测"的设计
是对的，**它忠实继承的是一个上游已经错了的结论**。

发布组在命名上确实无法区分这两部片（都只能叫 `The.Odyssey.2026`）。但把这归为
"任何自动化系统都无解的极端特例"是不成立的：站点详情页标了 IMDb，种子体积与
210 分钟的片长严重不符，内核自己也知道这次是"只靠片名+年份蒙的"——**三份证据
系统都拿得到，一份都没用**。

## 1. 根因：这条链上没有任何一步在质疑

把整条路径按"知识状态"重排一遍，问题就不在某个函数里了：

| 环节 | 系统当时知道的 | 系统实际存下来的 |
|---|---|---|
| 匹配 | `confidence="title_year"`（只靠片名+年份认的） | **扔掉** |
| 选优 | 同上 | 排序键里没有证据强度，做种数高者胜 |
| 投递 | 同上 | `attempt` 只记 site/torrent，无证据标注 |
| 入库 | info_hash 命中在途投递 | `library_file.identity_source` = **NULL** |
| 对账 | — | 工单 `IMPORTED`、订阅 `completed` |
| 修正 | 用户明确说"认错了" | 只前向改挂，**不回传订阅** |

三处可验证的事实：

1. **`IdentityMatch.confidence` 从未被读过。**`models.py:485` 的注释写着
   "（观察用）"——grep 全部 `movieclaw_api/` 与 `movieclaw_db/`，零个读取点。
   内核算出了"我这次有多确信"，在返回给消费方的那一刻被丢弃。
2. **监听导入路径不写 `identity_source`。**`ingest.py` 里那个
   `identity_source = "subscription"`（1777 行）是**路由用的局部字符串**，只影响
   落库目录的文案（1815 行）与同档预检（2103 行）；真正落进
   `LibraryFile.identity_source` 的构造点（2195-2222 行）根本没有这个字段，
   全部是 NULL。按枚举的文档语义（`library_file.py:71-77`），NULL = "旧数据，
   视同机器结论"。
3. **库存对账是单向的。**`close_fulfilled_wanted`（`wanted_fulfillment.py:35`）
   只把"在库单元"的工单关掉；全部 `owned_units` 调用点里没有任何一处会在文件
   离开某条目时把它的 `IMPORTED` 工单退回。

**每一步都在加固上一步的猜测，没有一步在质疑它。** 这才是结构性成因——不是
两部片撞车太巧，是系统没有表达"我不太确定"的能力。

## 2. 目标与反目标

**目标**（按优先级）：

1. **纠错闭环不能是死胡同**：用户修正错配之后，原订阅必须自动复活并继续找，
   且不会立刻抓回同一个错种子。
2. **猜测带着标签走完全程**：内核算出的证据强度必须贯穿选优 → 投递 → 入库 →
   台账，让每一层都能按证据强度决定要不要多问一句。
3. **优先在下载前拦截**：能用手上已有数据判的（体积/时长），不要等下完再说；
   能用一次请求换铁证的（详情页 IMDb），不要靠猜。
4. **绝不静默**：拦不住的错配，至少要让用户当天看见，而不是三个月后自己发现。

**反目标（本期明确不做）**：

- **不因为身份存疑而拦截入库**。踩线的合法情况（导演剪辑版/加长版/TMDB 时长
  填错/多版本封装）频率远高于认错片，为抓一个错配把这些全挡在库外，代价大于
  收益。存疑一律是"照常入库 + 标记 + 告警"。
- **不引入新的用户概念**。不做"置信度分数"这类需要用户理解的东西；置信度是
  内部量，对用户只表达为"系统对这次识别有疑问"。
- **不对剧集开时长体检与歧义探测**（理由见 §8.4、§9.5）。
- 不做发布组/导演/主演级别的 NER 抽取来辅助判别（成本与收益不成比例）。

## 3. 工作项总览

| 优先级 | 工作项 | 性质 | 改动量 | 依赖 |
|---|---|---|---|---|
| **P0** | 修正后的反向对账 + 错种拉黑 ✅**已实现** | **bug 修复** | 小 | 无 |
| **P1** | 反向 ID 否决 + 置信度贯穿 ✅**已实现** | 地基 | 小 | 无 |
| **P2** | 隐含码率反证（下载前） ✅**已实现**（极端档生效，可疑档 shadow） | 增强 | 小 | P1 |
| **P3** | 投递前详情页复核 ✅**已实现**（file_list 预检另计，见 §7.6） | 主力 | 中 | P1 |
| **P4** | 入库时长体检 ✅**已实现（shadow）** | 兜底 | 中 | P1 |
| **P5** | 同名同年歧义（按需探测） ✅**后端已实现**（前端接两个现成端点） | 增强 | 中 | P1/P2/P3 |

P0 单独成立，**必须先做**：没有它，P1–P5 的发现能力越强，用户被推进死胡同的
频率越高（发现了 → 修正了 → 订阅悄悄躺平）。

P2/P4 含拍脑袋的阈值，**一律先以 shadow 模式上线**，见 §10。

---

## 4. P0｜修正后的反向对账（bug）

### 4.1 结论

把单向的 `close_fulfilled_wanted` 升级成**双向对账原语**
`reconcile_wanted(media_item_id)`：在库的单元关工单，**不在库的单元退回工单**。
但"退回"这个方向必须有触发源白名单（§4.4），不能挂在全量扫描上。
同时把那个错的 `(site_id, torrent_id)` 写进该订阅的负面记忆。

### 4.2 现状与后果

`claim.py:128`、`claim.py:207`、`scan.py:4106` 三条身份变更路径都只调
`close_fulfilled_wanted(session, item.id)`——只给**新条目**对账。

于是 §0 的 case 修正之后的真实状态是：

- 文件改挂到了正确条目 ✅
- **诺兰版订阅的工单仍是 `IMPORTED`，订阅仍是 `completed`** ❌
- 它再也不会搜索了，用户永远等不到，且没有任何提示

用户以为"各就各位"，实际上这个订阅已经静默死亡。

**覆盖面比"改挂路径"更宽**：`owned_units`（`library_file_repo.py:135-148`）
已经正确地"只算在位的文件（missing 的不算拥有）"。也就是说用户**直接删掉**
那个错误入库的文件时，库存事实层面已经对了——只是没有任何一处去重算工单。
所以这不是"在 claim 里补一个 reopen 调用"能了结的，改挂、删除、误删、
文件被外部移走，是同一个缺失的四种表现。

### 4.3 改动点

> 已实现。落地时与本节初稿有两处偏离，均记在下方。

`subscription/wanted_fulfillment.py` 新增 `reopen_unfulfilled_wanted(session,
media_item_id, *, lost_sources)`，与既有的 `close_fulfilled_wanted` 并列成为
对账的两个方向：

> **偏离一：两个函数，不是一个带 `allow_reopen` 开关的 `reconcile_wanted`。**
> 初稿设想的是单一原语。实现时发现调用点**永远只需要一个方向**（新条目只关、
> 被腾空的旧条目只退，两者是不同的 `media_item_id`），而关闭方向自带
> `verify_upgrades`、洗版快照、IM 推送、webhook、媒体服务器刷新一长串副作用
> ——对一个"文件刚走光"的条目跑这半边纯属白费且有副作用风险。合并成一个函数
> 只会逼每个调用点跑一段自己不需要的逻辑。

- **关闭方向**（`close_fulfilled_wanted`，现状不变）：`owned_units` 覆盖到的
  开放工单 → `IMPORTED`
- **退回方向**（`reopen_unfulfilled_wanted`，新增）：`status == IMPORTED` 的
  工单与 `owned_units(media_item_id)` 求差集
- 差集内的工单退回：`status=WANTED`、`info_hash=None`、`grabbed_at/downloaded_at/
  imported_at=None`、`search_attempts=0`、`next_search_at=now`
  （字段清理口径直接复用 `core.py:814-830` 的「缺失重下」，语义完全一致）
- 逐订阅 `recompute_subscription_status` + 记一条 `ActivityType.REOPENED`
- 洗版侧同步：清掉该单元的 `quality` 快照与洗版排期（基线随文件一起走了）

**错种拉黑**（同样在 `claim_files` 里）：被改挂的文件带着 `site_id`/`torrent_id`
来源戳（`ingest.py:2219-2220` 落的），把它写进原订阅的负面记忆。

> 不做这一步，退回的工单下一轮搜索会**立刻再抓回同一个种子**——它还躺在下载器
> 里、已完成、秒"下载成功"、再入库、再认错。这与 `_record_content_missing`
> （`download_progress.py:1216`）记录的教训一字不差："秒完成 → 再核验 → 再退回"
> 无限循环。来源清单同样要取该 hash 的历次投递活动，而不只是 attempt 上的首次
> 投递者——同一份发布常在多站镜像。

### 4.4 退回方向的触发源白名单（关键约束）

**"双向对账"绝不能理解成"哪里调 close 就哪里调 reopen"。**

反例：一个媒体库的盘临时掉线，扫描把整库文件标 missing，`owned_units` 随即
返回空集——若退回方向挂在全量扫描上，会把整库工单一次性退回 `WANTED` 并
立即排队，订阅开始疯狂重下整个媒体库。盘一恢复，这些下载全是白费的，还烧了
一轮 PT 流量。

所以 `reopen_unfulfilled_wanted` 只允许挂在**单条目粒度、由明确的身份变更事件
驱动**的调用点上：

| 允许 | 触发源 |
|---|---|
| ✅ | `claim.py::claim_files` 的 `displaced` 集合 |
| ✅ | `claim.py::resolve_review` 的 `displaced` |
| ✅ | `scan.py` 单条目重新识别的改挂路径 |
| ❌ | 用户在库内显式删除文件（**偏离二**，见下） |
| ❌ | 全量扫描的 `marked_missing`（盘掉线、挂载点变更、外部临时移走） |
| ❌ | 任何批量/定时任务 |

判据是"这次不在库是**用户意图**还是**环境状态**"。环境状态引起的缺失由
既有的 missing 标记表达即可，工单不动——文件回来了什么都不用做。

> **偏离二：用户删除文件不进白名单**（初稿标的是 ✅）。实现时看清了两件事：
> ① **删除的意图不可判**——"删掉腾空间"和"删掉重下"在这一层完全无法区分，
> 而把前者当成后者，用户会发现自己刚删的东西又被下回来了；
> ② **"删掉重下"已有明确出口**——`core.py` 的 requeue 流程（「媒体库文件缺失，
> 重新下载」按钮）就是用户表达这个意图的地方，不需要在删除里再猜一次。
> 另外 `recycle_file` 也不是用户删除的入口：它当前唯一的调用方是洗版清理
> （`upgrade.py`），在那里退回工单会把刚洗好的版本再下一遍。

### 4.5 验收标准（已落地）

`tests/api/test_wanted_fulfillment.py`（对账另一半的单元测试）：

- `test_reopen_requeues_wanted_when_file_reanchored` —— 文件改挂走 → 工单回
  `WANTED`、`info_hash`/`imported_at` 清空、`next_search_at` 立即到期、洗版
  基线清掉、订阅脱离 completed、时间线有 `REOPENED`；再退一次为 0（幂等）
- `test_reopen_blacklists_wrong_source` —— 错种子写进 `content_missing` 负面记忆
- `test_reopen_only_touches_units_no_longer_owned` —— 同条目仍在库的单元不受影响
- `test_scan_path_never_reopens_on_missing_files` —— **盘掉线回归**：文件标
  missing 后走扫描收尾那条路（`close_fulfilled_wanted`），工单必须纹丝不动

`tests/api/test_library_item_detail.py`（端到端）：

- `test_claim_revives_the_subscription_left_behind` —— 复刻 §0 现场：错挂条目
  上挂一个"已收齐"的订阅，走真实的 `claim_files_batch` 路由改正身份，断言
  订阅复活、工单重新排队

---

## 5. P1｜反向 ID 否决 + 置信度贯穿（地基）

### 5.1 结论

外部 ID 从"只用于命中"改为"也用于否决"；`IdentityMatch.confidence` 从观察量
升格为一等公民，贯穿选优 → 投递台账 → 入库台账。

### 5.2 反向 ID 否决——按证据一致性分档，不是一律否决

现状是"两边都有 ID 且不等"会掉到别名匹配继续走——站点已经明确说了"这是另一
部片"，而我们装作没看见。但**一律否决是错的**，因为 NexusPHP 的 IMDb 字段是
上传者手填的，填错真实存在（贴成同系列另一部、贴成剧集页、复制粘贴串行）。

一律否决的代价不对称：错配是"下错了片，用户看得见、能修"；而误否决是**漏配**
——订阅永远不满足，用户只会觉得"这片怎么一直搜不到"，唯一线索是活动流水里的
一行字。所以要看**冲突的方向和其余证据的一致性**：

| 冲突形态 | 判断 | 处置 | 状态 |
|---|---|---|---|
| 冲突 ID 恰好是已知孪生（§9）的 ID | 铁证认错 | 直接否决 + 拉黑 | 部分：ID 冲突即否决，未再细分是否孪生 |
| ID 冲突，且时长/体积反证（§6）也不支持本条目 | 两项独立证据同向 | 直接否决 | 同上（结果一致） |
| ID 冲突，但片名、年份、隐含码率**全都吻合** | 更像站点标错，而不是我们认错 | **转待确认**，不静默否决 | 未做，见下 |
| ID 冲突，其余证据不足以判断 | 存疑 | 否决，但活动文案写明理由，指路手动选种 | ✅ 已实现 |

`match_identity` 内核只做最保守的一件事：把冲突**如实报告**给消费方（在
`IdentityMatch` 上加 `id_conflict: str | None`），由 `matching.py` 的消费侧按
上表裁决。理由是内核拿不到时长/体积/孪生这些上下文，让它单方面 `return None`
就把可挽回的情况变成了不可见的漏配。

> **落地范围**：内核的如实报告已完整落地；消费侧统一走"ID 冲突即否决 + 留下
> 完整解释"（`reason_code=identity_id_conflict`，文案含双方 ID 与手动选种指路）。
> 前两行的**结果**与它一致，所以没有再去细分"冲突 ID 是不是孪生的"——多一层
> 判断不改变行为，只会多一处要维护的分支。
>
> 第三行（全都吻合时转待确认）**没有做**：它要求"隐含码率吻合"这个正向判据，
> 而 §6.3 的实测表明码率的合理区间太宽，"吻合"几乎恒真，据此放行等于取消这条
> 反证。等 shadow 期攒到真实数据、能给出可信的"吻合"定义时再说。
>
> 手动选种通道刻意不经过这道否决——`manual_grab` 直接调 `match_identity` +
> `dispatch`，用户的显式选择永远高于自动反证，拒绝文案里指的就是这条路。

> 这是对本文档初稿的修正：初稿写的是"两边 ID 不等直接 `return None`，守卫方向
> 与'宁可漏，绝不静默错配'一致，零风险"。方向一致是对的，"零风险"是**过度断言**
> ——"宁可漏"的前提是漏得**可见**，而内核静默 `return None` 恰恰是不可见的。

### 5.3 置信度贯穿三处

**① 选优排序（`matching.py`，改排序键）**

改前的语义是："只靠片名蒙的、做种数多"的候选会**赢过**"IMDb 精确命中、做种数
少"的候选。证据强度（`exact_id` > `title_year` > `title_only`）插到 `is_pack`
之后、洗版档位与 `score` 之前——先要**对的片**，再谈档位和评分。位置在
`is_pack` 之后是刻意的：「整季包优先」是既有的已确认决策，本次只补身份维度，
不顺手改包优先的语义。

**② 投递台账（`subscription_download_attempt` 加两列）**

`identity_confidence`、`matched_alias`（后者内核也算了、也扔了）。这样
`_wanted_identity` 反查回来的就不只是"是哪部片"，还有"当初凭什么这么认的"。

**③ 入库台账（`ingest.py` 填上一直是 NULL 的 `identity_source`）**

`IdentitySource` 枚举新增两档：

- `SUBSCRIPTION_EXACT` —— 投递时有 ID 佐证
- `SUBSCRIPTION_GUESS` —— 投递时只有片名+年份

只有后者需要触发 P4 的时长体检、需要在库里可被审计；前者安静通过。顺带把
`forced_item`（用户拍板）与手动下载确认的身份落成 `MANUAL`——改前这些信息在
入库路径上全部丢失，`identity_source` 一律是 NULL（那个局部变量
`identity_source = "subscription"` 只用于选落库目录的文案，从没进过台账）。

取值由 `ingest.py::_ledger_identity_source` 收口。旧数据的证据强度未知（台账那
两列是本次才加的），按 `SUBSCRIPTION_GUESS` 记——保守：宁可日后多做一次反证
体检，不可漏掉真错配。

> 枚举扩容对既有对账机制是安全的：`MANUAL` 永不被自动翻案的规则不变，两个新
> 值与 `RESOLVED`/`NFO` 同属"机器结论"，参与识别器升级后的复核。

### 5.4 为什么这是地基

做完 P1，P2–P5 不再是各自为战的补丁，而是同一个"证据强度"概念的三个消费者：
P3 是**提升**证据强度的手段，P5 是**要求更高**证据强度的策略，P2/P4 是证据
强度不足时的反证与事后复核。

### 5.5 验收标准（已落地）

`tests/matcher/test_identity.py`（判例文件，按 §10.3 并入而非另起）：

- `test_conflicting_imdb_is_reported_not_silently_dropped` —— 冲突被如实报告，
  身份仍成立（裁决权在消费侧）
- `test_missing_id_on_either_side_is_not_a_conflict` —— 只有一边有 ID 不算反证
- `test_matching_imdb_never_carries_a_conflict` —— ID 命中即 exact_id，另一个 ID
  对不上只是数据噪音
- `test_twin_movies_same_title_same_year_are_indistinguishable_by_title` ——
  §10.3 要求补的**孪生对抗**形态：同一候选喂给同名同年的两个条目都成立，钉死
  "这不是调阈值能解决的问题"；站点一旦标了 IMDb 两者立刻可分

`tests/api/test_subscription_pipeline.py`（管线）：

- `test_conflicting_site_imdb_is_rejected_with_an_explanation` —— 不投递，且
  拒绝理由含双方 ID
- `test_id_backed_candidate_beats_a_higher_seeded_guess` —— ID 命中的候选赢过
  做种数 999 的 title_year 候选
- `test_dispatch_records_the_identity_evidence` —— 台账落 `title_year` + 命中别名

---

## 6. P2｜隐含码率反证（下载前，零请求）

### 6.1 结论

用「体积 ÷ 片长 = 隐含码率」在**下载前**做一次反证。所需数据全部已在手上：
`site_torrent.size_bytes`、`MediaMetadata.runtime_minutes`、
`attrs.resolution/media_source/video_codec`（`enrich/models.py:45-48`），
**一次网络请求都不用发**。

### 6.2 算术

§0 的 case：一个 4GB 的种子，若真是 210 分钟的诺兰版，隐含码率
= 4×8×1024 / 12600 ≈ **2.6 Mbps**——对一个标着 `1080p AMZN WEB-DL` 的发布低到
不合理。若是 88 分钟，≈ 6.2 Mbps，完全正常。

### 6.3 决策：只做相对比较，不做绝对下限

绝对码率下限这条路**明确否掉**：1080p 的合理区间跨度太大（x265 压到 2-3 Mbps
能看，老片低码转制更低），做成硬门禁必然误伤。

比绝对下限更有力的是**相对比较**，也就是 P5 的孪生判别场景：同一个文件、同一
个编码、同一个片源，"该用多少码率"这个未知量在孪生之间被抵消掉一部分。所以 P2
的主形态是 **P5 的判别器**，而不是独立门禁：算每个孪生的隐含码率，取最接近该
分辨率典型区间的那个；赢家不是本订阅的条目 → 直接否决，连问用户都不用问。

> ⚠️ **本节初稿的一处判断被实测推翻，已改正。** 初稿写的是"'该用多少码率'这个
> 未知量在两个孪生之间**被消掉了**……问题退化成'4GB 更像 88 分钟还是 210 分钟'
> ——答案毫无悬念"。把 §0 的真实数字代进去并不成立：4 GB 按 210 分钟算是
> 2.7 Mbps、按 88 分钟算是 6.5 Mbps，**两个都落在 1080p 的合理区间内**，对数
> 距离只差 0.13（噪音级）。未知量只被抵消了一部分，没有被消掉。
>
> 所以判别器对 §0 这个 case **判不出来**，会返回"分不出"并转给用户确认。把门槛
> 降到能判这一档，等于对几乎每一对孪生都强行表态，会错一半。它真正能开口的是
> 一边的体积对那个片长**明显说不通**的情形（如 1 GB 配 210 分钟 = 0.68 Mbps）。
>
> 结论没变（P2 该做、形态该是判别器），变的是对它威力的估计——以及 §9.4 那张
> 裁决表里"不打扰用户"的行会比预期少、"问用户"的行会比预期多。用例
> `test_twin_discriminator_stays_silent_on_the_real_odyssey_numbers` 钉死了这条
> 边界，免得日后有人靠调低门槛"修好"它。

单机形态（非歧义条目）只保留一个极粗的单边哨兵：隐含码率低于该分辨率典型下限
的 **1/3**（`identity.py::_BITRATE_FLOOR_MBPS`，⚠ 待校准）。这个倍数下没有任何
正常发布会踩线，踩线的基本是预告片、sample 和假种。

### 6.4 落地范围与两条边界

单边哨兵**分两档**，只有贴近阈值的那一档走 shadow：

| 档位 | 判据 | 行为 | 要不要校准 |
|---|---|---|---|
| 可疑档 | `_BITRATE_FLOOR_MBPS` 之下（1080p = 1.5） | shadow：只记进投递活动 payload 的 `shadow` 键 + INFO 日志，不改变任何行为、不进用户可见文案 | **要**（§10.2） |
| 极端档 | `max(离谱下限 ÷ 5, 0.25)`（1080p = 0.3，480p/720p 取地板 0.25）；分辨率未知时直接用地板 | **直接否决**，记 `MATCH_REJECTED`（`reason_code=size_absurd_for_runtime`） | **不要** |

极端档是后补的一档，起因是一例真实漏配：一部 110 分钟的电影收到 113 MB 的
「1080p WEB-DL」（隐含码率 0.14 Mbps，是离谱下限的十五分之一），实测只有
5 分钟——是预告片。它能中选不是排序问题，而是**那一轮只有它一个候选通过了
规则**：选优只在多个合格候选里挑好的，唯一候选无论多小都会中，体积根本不参与
打分。当时链路上有两处都"看见"了（投递前的 shadow 记录、入库时的时长体检），
两处都只记录不拦截。

绝对地板 0.25 Mbps 是**所有分辨率共用的下界**，不只是分辨率未知时的兜底：只当
兜底会留一个反常的洞——480p 的离谱下限 0.4 再除 5 是 0.08，比未知分辨率的 0.25
还松，同一条 0.14 Mbps 的假种标 1080p 会被拦、标成 480p 反而放行，而"标低分辨率"
恰恰是假种最省事的伪装。0.25 Mbps 跑 110 分钟只有 206 MB，而真实的 480p 正片压制
在 0.5 Mbps 上下（一部片 400-700 MB），取较大值不会误伤。

**为什么极端档不需要校准**：§10.1 说这些阈值"没有真实数据支撑、误报代价落在
用户身上"，那个顾虑对 1.4 Mbps 成立（那一档站着正常的 x265 低码压制），对
0.3 Mbps 不成立——0.3 Mbps 的 1080p 跑 110 分钟只有 247 MB，这样的正片不存在。
把两档混为一谈的代价，就是为一个不可能存在的正片保留"宁可不判"的余地。

否决点在**选优之前**（规则过滤刚通过时），不是投递前：放在投递前它已经占掉了
"唯一合格候选"的位置，拦下来等于这一轮什么都没投；放在候选池之前，同一轮里的
其他候选照常竞争。

判别器形态（相对比较）等 §9 的孪生探测提供第二个比较对象后再接，本次只落了它
的算术基础 `implied_bitrate_mbps`。

两条边界，都由用例钉死：

1. **单边哨兵抓不住 §0 那个 case**。4 GB ÷ 210 分钟 ≈ 2.7 Mbps，低得可疑但仍在
   1080p 的离谱下限之上，放行。这不是实现打了折扣——把阈值调到能抓住它，代价
   是误伤大量正常的低码压制。用例
   `test_single_sided_sentinel_does_not_catch_the_twin_movie_case` 存在的意义
   就是让日后想调高阈值的人先看到这个代价。
2. **只对电影生效**。`MediaMetadata.runtime_minutes` 对剧集是**单集**时长，而
   整季包的体积覆盖 N 集，拿单集时长去除整季体积算出的"码率"虚高 N 倍，两者
   根本不可比。剧集侧另有季集号做区分，不缺这条反证。

### 6.5 验收标准（已落地）

`tests/matcher/test_identity.py`：

- `test_trailer_sized_release_is_implausible_for_a_feature_length_movie` ——
  0.2 GB ÷ 120 分钟 ≈ 0.24 Mbps，判为离谱
- `test_low_bitrate_x265_encode_is_not_flagged` —— 3.0 Mbps 的正常压制不误伤
- `test_missing_evidence_never_judges` —— 片长/体积/分辨率任一未知都不判
- `test_single_sided_sentinel_does_not_catch_the_twin_movie_case` —— 边界 1
- `test_runtime_counter_evidence_is_movie_only` —— 边界 2

极端档（`absurdly_small_for_runtime`）：

- `test_absurd_tier_catches_the_trailer_that_shipped_as_a_feature` —— 113 MB ÷
  110 分钟 ≈ 0.14 Mbps 的真实样本
- `test_absurd_tier_falls_back_to_an_absolute_floor_without_resolution` ——
  分辨率未知时的绝对地板（可疑档在这种情况下直接放弃判断）
- `test_absurd_tier_floor_applies_to_low_resolutions_too` —— 地板对所有分辨率
  生效，且不误伤真实的 480p 压制
- `test_absurd_tier_leaves_the_shadow_band_alone` —— 1.07 Mbps 仍然只观测
- `test_absurd_tier_does_not_touch_normal_encodes_or_the_twin_case` ——
  **极端档是新增的一档，不是把既有边界往上挪**：3.0 Mbps 的正常压制与 §0 的
  2.7 Mbps 孪生错配都不碰
- `test_absurd_tier_never_judges_without_evidence` —— 证据不足与剧集边界

`tests/api/test_subscription_pipeline.py`：

- `test_bitrate_counter_evidence_is_recorded_but_does_not_block` —— 同时钉死
  "记录发生了"与"行为没有变"，并断言判定不进用户可见文案
- `test_normal_sized_release_records_no_shadow_note` —— 正常发布不留记录，
  否则统计触发率时全是噪音

---

## 7. P3｜投递前详情页复核（主力）

### 7.1 结论

投递取种之前，对"条目有外部 ID 而候选行没有"的情况拉一次种子详情页，回填
`site_torrent.imdb_id/douban_id`，然后按 §5.2 的反向否决裁决。

### 7.2 为什么覆盖面够

- `get_torrent_detail`（`nexusphp.py:87-123`）**在整个 API 层一次都没被调用过**
  ——解析器写好了，从来没接上。`site_torrent.imdb_id`（`site_torrent.py:121`）
  因此几乎恒为 NULL，只有 M-Team 因列表 API 自带 imdb 才有值。
- 默认选择器 `a[href*='imdb.com']::attr(href)`（`selectors.py:127`）是通用的，
  24 个站点配置全部适用，只有 3 个站点做了同值覆写。
- 成本可忽略：投递本身是稀有事件（不是每个种子都查），而且**下一步本来就要向
  同一个站点发请求取 .torrent**。

### 7.3 只对电影（落地时收紧的一条）

初稿没限定类型。实现时收紧为**只对电影**，理由是收益与成本的分布正好相反：

- **收益集中在电影**：同名同年撞车是电影独有的问题，剧集另有季集号做区分；
- **成本集中在剧集**：追新是一集一次投递，一季就是几十次多余的详情页请求。
  "对 PT 站克制"是本项目的铁律（`matching.py` 的洗版调度注释里写着同一句），
  不该为剧集侧几乎用不上的收益去换这个量级的请求。

判据收口在 `identity_recheck.py::needs_external_id_recheck`：条目有外部 ID、
候选还没有、且是电影，三条同时满足才花这次请求。

### 7.4 裁决表

| 情况 | 处置 |
|---|---|
| 详情页 ID 与条目相等 | confidence 升为 `exact_id`，照常投 |
| 详情页 ID 与条目不等 | 交给 §5.2 的分档裁决（不是无条件否决），记 `MATCH_REJECTED` 时写明双方 ID |
| 详情页无 ID | 普通订阅放行；歧义订阅（P5）**不投** |
| 详情页请求失败 | 放行（不能让站点抖动卡死投递），不缓存结果 |

回填的 ID 落进 `site_torrent`，全局受益——被动匹配下次遇到同一行直接走信号一。

### 7.5 验收标准（已落地）

`tests/api/test_subscription_pipeline.py`，覆盖裁决表的每一行：

- `test_pre_dispatch_recheck_blocks_a_torrent_the_site_says_is_another_film` ——
  §0 错配的正面拦截：片名年份区分不了，站点详情页的 IMDb 可以
- `test_pre_dispatch_recheck_confirms_and_upgrades_the_evidence` —— 一致时照常
  投递、台账记 `exact_id`、ID 已回填进 `site_torrent`
- `test_pre_dispatch_recheck_passes_when_the_site_has_no_id` —— 站点没标就当没
  这条证据，不能因为查不到就不下
- `test_pre_dispatch_recheck_never_blocks_on_a_site_failure` —— 请求失败一律放行
- `test_pre_dispatch_recheck_spares_tv_and_id_less_items` —— 剧集与无 ID 条目
  根本不花这次请求（断言详情页一次都没被调用）

### 7.6 file_list 预检：本期不做

初稿把它算作 P3 "顺带买到"的部分——`TorrentDetail.file_list` 确实是这次请求白
送的，而 `_verify_content`（`download_progress.py:1170`）对电影是第一行直接
`return`，电影侧至今没有任何内容核验。

但它**不是顺带就能做完的**：文件数量异常、单文件体积占比、sample/预告目录，
每一条都是需要阈值的启发式，都得像 P2 那样先跑一段 shadow 才敢生效。把它和 ID
复核绑在一起，只会让真正能拦住错配的那部分一起等。

拆出来单做，做的时候沿用 §10 的灰度纪律。ID 复核已经落地，届时再拉一次详情页
的成本也只是"回填时顺手多存一个 file_list"，不构成阻碍。

---

## 8. P4｜入库时长体检（兜底）

### 8.1 结论

电影入库时比对「实测时长 vs TMDB 片长」，严重不符则**照常入库**、标记存疑、
点亮告警。flag-not-block。

### 8.2 为什么检查点在 ingest

订阅认领在 `ingest.py:1776` 就短路了名称识别链：

```python
item, pinned_library_id = await _wanted_identity(session, matched_hashes or [])  # 命中即结束
...
if item is None:
    item = await _identify(...)   # ← resolve.py 的全套佐证/反证机器在这里，走不到
```

`resolve.py` 有成熟的时长互证机器（`_RUNTIME_STRONG_SECONDS=180`、
`_RUNTIME_WEAK_SECONDS=600`、`_counter_evidence`），但只挂在名称识别链上。
短路本身是对的（"继承投递时锚定的精确身份，零猜测"），前提是投递没错；投递
错了，它就是错误的高速通道。**P4 是给订阅认领补上它被跳过的那次反证。**

检查点只能是 ingest：下载完成时只有文件清单（名字+体积），体积推时长太弱；而
`ingest.py:1731` 的 `probe_media(main)` 就在身份判定的前 14 行，
`spec.duration_seconds` 已就绪，`MediaMetadata.runtime_minutes` 也在手上。

### 8.3 判定口径

```
gap = |probe_duration − tmdb_runtime|
触发：gap > 25% × tmdb_runtime  且  gap > 15 分钟   （两个条件都要满足）
```

"两个都要"而非取大值：只看比例，30 分钟的纪录短片差 7.5 分钟就报警，噪音爆表；
只看绝对值，210 分钟的片差 15 分钟完全正常，也会报。两条一起——§0 的 case
是 210 vs 88，gap=122 分钟、58%，两条线远远踩爆；而 120 分钟正片的导演剪辑版
+18 分钟（15%）不报。

**不判的情况**（证据不足一律不动，与 `_verify_content` 同一哲学）：
`runtime_minutes` 为 NULL、probe 失败、原盘/多碟、以及 §5.3 里
`identity_source == SUBSCRIPTION_EXACT` 的（已有 ID 佐证，不必再疑）。

### 8.4 只对电影开

剧集单集时长噪音太大（OP/ED、导视、双集合并、番外），`MediaEpisode.runtime_minutes`
本身也常不准，开了就是噪音源。与 `resolve.py:_strong_corroborations` 里
`kind is MediaKind.MOVIE` 的既有取舍一致。

### 8.5 存疑之后走哪条路

**照常硬链入库、照常关工单**，然后三件事：

1. **台账落痕**：`library_file` 新增 `identity_doubt: dict | None`，形如
   `{"reason":"runtime_mismatch","expected_minutes":210,"actual_minutes":88}`。

   > **不复用 `review_suggestion`**：它的语义是"我觉得应该改成那个"，结构里必须
   > 有 `media_item_id`/`title`（`scan.py:2841`）。时长反证给不出替代条目——与
   > `scan.py` 里 KIND_MISMATCH 那段注释记的困境同构（"没有替代条目可建议——复核
   > 清单帮不上忙"）。硬塞进去会让 `claim.py:181` 的
   > `pending = [row for row in rows if row.review_suggestion]` 混进一批点不动
   > "采纳建议"的行，`resolve_review(accept=True)` 拿不到 `media_item_id` 只能
   > 空转，等于给既有状态机埋歧义。

2. **告警中心点灯**：`upsert_notice(dedupe_key=f"library:runtime-doubt:{file_id}", …)`，
   文案说清楚"可能认错了片，也可能是剪辑版/加长版"。
   `system_notice.py` 的两条既有语义正好用上：用户 dismiss 过就永久沉默；用户
   走「修正识别结果」时由 `claim_files` 调 `resolve_notices` **自动熄灯**，
   不需要额外做收口。

3. **订阅时间线**：`ActivityType` 新增 `IDENTITY_DOUBT`（该枚举的模块头明确说
   "类型先占位保证前端枚举稳定"，加枚举是这张表的既定玩法）。

用户的两条出路都已有实现：**「就是它」** → dismiss；**「认错了」** →
`claim.py::claim_files`（改挂 + 迁移观看状态 + 改写盘上矛盾的 NFO + 对账 +
孤儿清理，再加上 P0 的反向对账）。

### 8.6 落地范围：只做了第 1 件

shadow 阶段（§10.2）只落**台账**：`identity_doubt` 写进 `library_file`，外加
一条 INFO 日志。第 2、3 件（告警点灯、订阅时间线）都是用户可见的，属于"生效"
而不是"观察"，等校准完再接——现在接上去就是让导演剪辑版和加长版去刷用户的
待处理事项。

**存疑记录在 shadow 期刻意不做任何清理**（改挂时也不清）。看起来像遗漏，其实
是校准需要：一条被标了存疑、随后又被用户手动改挂走的记录，正是这条体检**命中
真错配**的证据——那是整个校准里最有价值的一类样本，清掉就没了。等升格时再补
"改挂后重算或清空"，那时 `resolve_notices` 也一并接管用户可见的熄灯。

### 8.7 验收标准（已落地）

`tests/api/test_library_ingest_auto.py`：

- `test_runtime_doubt_thresholds` —— 四组边界：§0 现场（210 vs 88，58%/122 分钟）
  触发；导演剪辑版 +18 分钟（15%）不触发（**本方案最怕的误报**）；30 分钟短片
  差 8 分钟被绝对值兜住；预告片体量触发
- `test_runtime_doubt_needs_evidence_and_is_movie_only` —— 证据不足不判、剧集不判
- `test_subscription_claimed_movie_records_runtime_doubt` —— 端到端：订阅按
  info_hash 认领的电影时长对不上时，**文件照常入库**、存疑落账、身份来源同时
  分档为 `subscription_guess`

---

## 9. P5｜同名同年歧义（按需探测）

### 9.1 结论

在「电影 + `confidence == "title_year"` + 即将投递」这个交叉点上，按需向 TMDB
探一次"有没有同名同年的孪生条目"，结果缓存进 `media_item.identity_twins`。
歧义条目的自动投递门槛升一档。

### 9.2 决策：按需探测，不在建档时全量探

早期方案是放在 `ensure_media_item`（建档时）全量探测。**否掉**：要加三态字段
语义、给每次建档加一次 TMDB 请求、存量条目还要靠刷新任务慢慢回填。

按需探测的交叉点比建档稀有得多（绝大多数候选在规则过滤阶段就被拒了），且：
不需要三态语义（探不到就是当次不收紧）、不需要回填脚本、结果照样可缓存复用。
符合"不为一次性代码创建抽象"。

### 9.3 探测逻辑

用 `original_title` 搜一轮，再带 `year` 参数搜一轮（普通搜索按热度排序，冷门
正主可能掉出前 5——这个坑 `resolve.py:234` 已经踩过并留了注释）。筛出
`tmdb_id != 自己` 且 `|year − 自己.year| ≤ 1` 且
`normalize_title(候选.title 或 original_title)` 等于自己的任一别名。

归一化**必须**复用 `movieclaw_matcher.identity.normalize_title`——与匹配内核
同口径，否则会出现"探测说不歧义、匹配却撞车"的裂缝。

只比 `title` / `original_title` 两个字段，**不拉 `alternative_titles`**：那要给
每个候选各发一次请求，成本翻几倍而收益边际——真正会撞车的是发布组会用的那个
名字，基本就是 original_title。

缓存字段 `media_item.identity_twins`：`[{tmdb_id,title,year,imdb_id}]`。
**存清单而非布尔**，第二个理由才是价值所在：孪生的 `imdb_id` 是一件武器——P3
拉回来的详情页 ID 若恰好等于孪生的 ID，那不是"证据不足"，是**铁证错配**，
直接拉黑，连问都不用问。

### 9.4 裁决表

| 情况 | 处置 |
|---|---|
| `confidence == "exact_id"` | 照常投（ID 说话，歧义与否无关） |
| 歧义 + P3 详情页 ID 等于本条目 | 照常投 |
| 歧义 + 详情页 ID 等于**孪生的** | 一票否决 + 拉黑，**不打扰用户** |
| 歧义 + 详情页 ID 是第三方 | 一票否决 |
| 歧义 + 无 ID + P2 判别器判给孪生 | 一票否决，**不打扰用户** |
| 歧义 + 无 ID + P2 也分不出 | ← **唯一需要问用户的格子** |

最后一格的实际频率：**比初稿估计的高**。P3 能自动裁决的前提是站点标了 IMDb；
标了就没歧义问题了。而站点没标时，P2 判别器往往也开不了口（见 §6.3 的修正：
两个片长常常都解释得通同一个体积）。所以真正的分工是：

- **站点标了 IMDb** → P3 直接裁决，用户完全无感（这是主路径）；
- **站点没标、且体积明显只解释得通一边** → P2 判别器否决，用户无感；
- **站点没标、体积也分不出** → 问用户。§0 的现场如果站点没标 IMDb，落的就是
  这一格。

即便如此，交互成本仍然可接受：它只在"电影 + 只有片名年份 + 真有同名同年兄弟
+ 已经决定要投"这个四重交叉上触发，而不是每次匹配。**问一句，好过默默下错。**

交互复用告警中心，**不需要新页面、也不需要新接口**——两个按钮都落在既有端点上：

| 按钮 | 走哪个现成接口 | 为什么够 |
|---|---|---|
| 就是这部，下载 | `POST /subscriptions/{id}/grab`（手动选种） | 它本来就绕过全部自动裁决，正是"用户显式选择高于一切"的既有通道，还会把 `attempt.manual` 标上 |
| 不是，别再推荐 | `POST /system/notices/{id}/dismiss` | `upsert_notice` 自带"dismiss 过就永久沉默"，下轮匹配照常不投、但不再打扰 |

> 初稿设想过给"不是"单独做一套拉黑存储（照抄 `upgrade_excluded`）。落地时发现
> **不需要**：那条记忆挂在 `SubscriptionDownloadAttempt` 上，而一个从未投递过的
> 候选根本没有 attempt 行；而告警的 dismissed 语义已经完整覆盖了"别再问我"。
> 少一个字段、少一次迁移、少一个端点。

告警在内容进库时自动熄灭（`wanted_fulfillment` 里按
`subscription.ambiguous:{sub_id}:` 前缀 resolve，与既有的落点告警同一处）。

### 9.5 边界与被否掉的备选

- **只对电影开**：剧集有季集号做额外区分，年份口径本来就松
  （`_year_compatible` 对剧集只做下限校验），撞车概率显著低于电影。
- **不做孪生别名差集**（"只用本条目独有的别名匹配"）：§0 的 case 里
  `The Odyssey` 是唯一别名，差集为空，直接漏配。歧义只能靠收紧 + 外部证据解决，
  不能靠削弱别名。
- **TMDB 不可达**：当次不收紧、不缓存（保持 NULL 的"未探测"语义），等下次。
  绝不能让网络抖动把订阅卡成待确认。
- **歧义标记不进 `MediaIdentity`**（初稿设想给内核加 `ambiguous: bool`，落地时
  没这么做）。裁决全部发生在消费侧、且只在投递前那一个点上，内核不需要知道这件
  事；不加字段也就不存在"另外三个 `MediaIdentity` 构造点忘了带"的裂缝——那正是
  初稿担心的问题，最好的解法是让它没有发生的机会。

### 9.6 验收标准（已落地）

`tests/matcher/test_identity.py`（判别器）：

- `test_twin_discriminator_speaks_only_when_one_side_is_implausible`
- `test_twin_discriminator_stays_silent_on_the_real_odyssey_numbers` —— §6.3 那条
  修正的守门用例
- `test_twin_discriminator_never_speaks_without_evidence`

`tests/api/test_subscription_pipeline.py`（裁决表）：

- `test_ambiguous_movie_stops_and_asks_the_user` —— 不投递、点亮待确认告警、
  payload 带齐两个按钮要用的字段、探测结果落缓存
- `test_ambiguous_movie_is_rejected_silently_when_size_says_it_is_the_twin` ——
  判别器能开口时直接否决，**不产生任何告警**
- `test_id_backed_candidate_ignores_the_twin_gate` —— 有 ID 佐证时连探测都不做
- `test_twin_probe_is_skipped_for_tv` —— 剧集不探

---

## 10. 阈值校准与灰度（P2/P4 必读）

### 10.1 问题

本方案里有三个拍脑袋的数字：§6.3 的"典型下限的 1/3"、§8.3 的"25% 且 15 分钟"。
它们凭直觉和一个样本（§0 的 case）定的，**没有任何真实数据支撑**，而两者的
误报代价都直接落在用户身上——P2 误报是漏配（静默），P4 误报是噪音告警（吵）。

项目对这类参数已有惯例：`matching.py:56` 的"标注 ⚠ 的需真实站点试跑校准"。
本方案的三个阈值**全部按 ⚠ 标注**，且必须走下面的灰度。

### 10.2 shadow 模式先行

P2 的**可疑档**与 P4 上线时**默认不改变任何行为**，只记录判定结果
（P2 的极端档不在此列：它不含需要校准的判断，见 §6.4）：

- P2：照常投递，但把"隐含码率反证本会否决"记进活动 payload（不记 message，
  不打扰用户）
- P4：照常入库，只写 `identity_doubt` 台账，**不点亮 SystemNotice**

跑满一个观察期后回答三个问题，再决定是否开成实际生效：

1. 触发率是多少？（一天几条 vs 一天几百条，量级决定它是不是个可用信号）
2. 触发的样本里，人工看有几个是真错配？——这是误报率的唯一真实来源
3. §0 那个 case 在语料里能不能被稳定复现出来？

### 10.3 回归夹具挂在既有判例文件上

`tests/matcher/test_identity.py` 已经是表驱动的真实样本回归基线（26 个用例，
文件头注明"取自开发库 site_torrent 的实际种子名与 enrich 产出，是误报/漏报的
回归基线"）。**不另起炉灶**，新用例直接加进它的"真实样本"区。

需要补的是一类该文件目前完全没有的夹具形态——**孪生对抗**：同一个候选喂给
两个 `MediaIdentity`，断言"至多只有一个成立"。现有 26 个用例全是"一个候选 ×
一个条目"，结构上覆盖不到 §0 这类错配。

### 10.4 可观测性

`identity_confidence` 落库后（§5.3），订阅详情页的活动流水天然可以回答
"这次是凭什么认的"。除此之外补一处聚合即可：投递活动 payload 里带上
confidence，用于统计"title_year-only 投递占比"——这个比例就是本方案要压低的
核心指标，也是日后调阈值的依据。

---

## 11. 数据库迁移清单

全部是加列/加枚举值，**无破坏性变更**，满足"迁移只能向前兼容"的硬约束：

| 工作项 | 表 | 变更 |
|---|---|---|
| P1 ✅ | `subscription_download_attempt` | + `identity_confidence` TEXT NULL<br>+ `matched_alias` TEXT NULL（revision `b7e3a9c1d240`） |
| P1 ✅ | `library_file.identity_source` | 枚举新增 `subscription_exact` / `subscription_guess`（列本身是 TEXT，无需 DDL） |
| P4 ✅ | `library_file` | + `identity_doubt` JSON NULL（revision `c9f4b1e7a352`） |
| P5 ✅ | `media_item` | + `identity_twins` JSON NULL（revision `d5a8c3f61b24`） |

迁移文件命名沿用 `YYYYMMDD_HHMM_<rev>_<slug>.py`。

回退语义：全部新列可空，旧版本读到多余列不受影响；`identity_source` 的两个新值
在旧版本里落入"未知机器结论"分支（NULL 同款处理），不会崩。

## 12. 硬约束核对

| 硬约束 | 本方案 |
|---|---|
| ① 版本号三处一致 | 发版时按 `.claude/skills/release/SKILL.md` 走，本方案不涉及 |
| ② 动运行时依赖须 bump `docker/runtime-version` | **不涉及**：无新增 pyproject 依赖、无 Node/系统包/基础镜像/entrypoint 变更 |
| ③ 迁移只能向前兼容 | 满足，见 §10 |
| ④ `data/` 新增目录须登记 | **不涉及**：无任何新增落盘目录 |

## 13. 明确不做的

- **不做存量库的一次性审计**（初稿曾列为 P6，已否掉）。理由有二：① **顺序矛盾**
  ——§10 明确要求这几个阈值先 shadow 观察再启用，却又安排一个拿未校准阈值全库
  跑的任务；一个几千部电影的库会一次性弹出塞满导演剪辑版、加长版、原盘、TMDB
  片长填错的清单，真错配可能一个都没有，第一印象就把用户对这条提醒的信任烧光，
  日后 P4 真报出问题也不会有人看。② **前提未验证**——"这一例几乎不可能是唯一
  一例"是断言而非事实：要撞上需同时满足片名相同、年份相同、且恰好都被同一用户
  订阅，本就极罕见；且存量老文件多数没有 `site_id`/`torrent_id` 来源戳（该字段
  是后来为洗版验证加的），审计连"这文件是不是订阅下的"都分不出来。
  若日后确有需要，正确形态是把 §8 的检查挂进**既有的扫描复核链**（
  `scan.py` 的 `RESOLVER_VERSION` 复核机制，天然分批、随扫描节奏渗透），
  而不是一个一次性审计任务——且必须等阈值校准完成之后。
- 不做"两边 ID 不等就无条件否决"（§5.2 —— 初稿的做法，已修正为分档裁决）
- 不把工单退回挂在全量扫描的 missing 标记上（§4.4 盘掉线反例）
- 不做绝对码率下限门禁（§6.3）
- 不做 `alternative_titles` 全别名比对（§9.3）
- 不做孪生别名差集（§9.5）
- 不因存疑拦截入库（§2 反目标）
- 不对剧集开时长体检与歧义探测（§8.4、§9.5）
- 不引入用户可见的"置信度/分数"概念（§2 反目标）
- 不新建独立的匹配判例库（§10.3 —— `tests/matcher/test_identity.py` 已是
  表驱动的真实样本回归基线，新用例并入它）
- P2/P4 不在未经 shadow 观察的情况下直接开成实际生效（§10.2）
