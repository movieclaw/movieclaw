# 订阅功能实施计划

> 配套文档：[subscription.md](subscription.md)（架构设计与决策理由）。
> 本文是执行视角：端到端流程细节 → 数据模型汇总 → 模块落点 → 分阶段计划与验证标准。

## 一、端到端流程（六条）

### F1 订阅创建（API 同步链路）

```
详情页点「订阅」
  → POST /api/subscriptions/prepare        ① 建档/复用媒体条目，返回季集结构
  → 前端弹层：季选择 + 追新开关 + 规则组     ② 用户决策
  → POST /api/subscriptions                 ③ 创建订阅 + 生成 wanted
```

细节：

1. **prepare 接口**（幂等）：按 `(kind, tmdb_id)` 查 `media_item`，不存在则建档——
   一次 TMDB 请求拉 detail + `alternative_titles` + `translations` + `external_ids`
   （`append_to_response` 合并）；tv 再逐季拉 season detail 写 `media_season`（含集列表 JSON）。
   返回：条目摘要 + 每季 `{season_number, 已播集数/总集数, air_date}`，供弹层渲染。
2. **豆瓣入口**：强制身份收敛到 TMDB 锚（设计稿 1.5），三级优先：① IMDb ID → `/find`
   精确命中（全自动无感知）→ ② 标题+年份搜索，多候选返回弹层让用户确认 → ③ 仍失败不建
   无锚条目，中文提示"该条目暂无法订阅"。豆瓣详情能力未补齐前暂时全部走②；收敛后
   `douban_id` 留存在条目上，与 `site_torrent.douban_id` 构成精确匹配通道。
3. **用户选择**：剧集 = 勾选季（默认全选已播季）+「持续追新」开关；电影无选项。
   规则组默认选中系统默认组，可换。
4. **wanted 生成**：按 E 的定义展开——**勾选季贡献该季全部已知集**（勾了就是要整季，
   含未播集）；**follow_future 贡献"订阅时刻之后播出"的一切集**（含未勾季的未来集、
   F3 发现的新集新季）；特别季 0 仅显式勾选才纳入。电影 = 单个补旧工单（0,0）。
   工单按性质分两类，调度语义不同（铁律：**本地缓存只用来追新，补旧永远真实搜索**）：
   - **补旧工单**（内容已播出/已上映）：`next_search_at=now` 立即排队真实 PT 搜索。
     不查本地缓存——缓存是 t0 前向跟随，对旧内容覆盖不完整，偶然命中给出的是
     残缺候选集，既做不了选优又会造成"已满足"错觉；
   - **追新工单**（尚未播出，由追新开关和 F3 产生）：`next_search_at = air_date + 宽限期
     （如 48h）`——新种子天然流入缓存，被动匹配是主通道，绝大多数工单在到点前已满足；
     真到点仍缺的，worker 捞起即漏抓兜底，无需任何状态翻转机制。未定档集才用
     `NULL`（不可调度，F3 定档时回填）。
   「只追未来」= 不勾任何季、只开 follow_future——正在播的季从下一集起要，
   已播集全部不要。
5. **不阻塞但立即发车**——接口不做同步搜索（跨站搜索数秒起步，会毁掉点击体验），
   但创建/调整/恢复成功后经 BackgroundTasks **立即踢一次 F4 worker tick**：
   走同一节流闸门（每轮组数上限 + tick 互斥锁 + 退避排期防重复搜索），
   首班车从"最多等 5 分钟"变成"落库即发车"。
6. 重复订阅同一条目：幂等返回已有订阅（前端展示「已订阅」态，入口按钮变为管理）。

### F2 被动匹配（新种子入库触发）

```
sync_site_torrents 单站同步完成
  → 尾部直调 matcher.process(new_torrent_ids)
  → 粗筛 → 身份匹配 → 规则过滤 → 决策 → 投递(F5)
  → 记水位
```

细节：

1. **粗筛**（省 CPU）：category 属影视类；`attrs` 非空。
2. **身份匹配**（内核第一级）：候选面 = 有活跃 wanted 的媒体条目（几十~几百量级，全比可行）。
   优先级：种子 `imdb_id`/`douban_id` 与条目精确相等 → 别名反向包含（归一化）+ 年份容差 ±1
   + `attrs.seasons/episodes` 与 wanted 季集吻合。整季包（`attrs.complete` 或有季无集）
   匹配该季全部未满足 wanted。
3. **规则过滤**（内核第二级）：拒绝 → 写 `match_record(decision=rejected, reason)`；
   通过 → 带评分进决策。
4. **决策**：目标 wanted 已非 `wanted` 态则跳过（去重）；同批多候选取评分最高。
5. **水位**：`app_setting` 存最后处理的 `site_torrent.id`；启动时补扫水位之后的行，防止
   进程重启漏种。

### F3 元数据刷新（定时任务，新集的发动机）

```
tick 扫 media_item.next_refresh_at 到期
  → 拉 TMDB detail → diff(status / 季 / 集)
  → 更新条目与季 → 追新订阅追加 wanted → 重排 next_refresh_at
```

细节：

1. 分档间隔（详见设计稿 2.3）：在播剧 6~12h / 未上映 24h / 完结 7d（无活跃订阅可跳过）。
2. **diff 逻辑**：对比 `media_season.episodes` JSON——新集出现且该条目有「追新」订阅
   → 追加追新工单（`next_search_at = air_date + 宽限期`；未定档为 NULL，定档时回填）。
   改档期/改集数同步更新。
3. status 变为 Ended/Canceled → 条目降档；订阅的所选季全部满足且无追新余地 →
   subscription 推 `completed`。
4. 别名同步刷新（`alternative_titles` 偶有增补）。

### F4 主动搜索（定时 worker，补旧专用）

```
tick → SELECT wanted WHERE status='wanted' AND next_search_at<=now
       ORDER BY priority DESC, next_search_at LIMIT N
  → 按媒体条目分组 → 每组一次跨站搜索 → 结果照常入 site_torrent(source=SEARCH)
  → 同一内核：身份匹配 → 规则过滤 → 决策 → 投递(F5)
  → 未满足的 wanted：search_attempts+1，next_search_at 指数退避
```

细节：

0. **职责定位**：主动搜索只为"缓存不可能覆盖"的内容服务——补旧工单（内容早于缓存
   的 t0 前向跟随范围）与逾期未满足的追新工单（漏抓兜底）。追新的主通道是 F2 被动
   匹配；本地缓存**不作为补旧数据源**（覆盖不完整，命中即残缺候选集）。
1. **按条目分组是关键**：同一部剧的 40 个缺集合并为一次搜索，结果在本地按季集分发到各
   wanted。订阅 20 季老剧绝不能变成几百次跨站请求。
2. 每 tick 只取少量分组（如 3~5 个条目），叠加站点级并发上限（复用聚合搜索的扇出控制）——
   双重限流，对 PT 站保持克制。
3. **搜索词策略**：首选 `original_title`（种子多为英文命名），空结果补一次主 `title`；
   词条带年份与否交给各站适配层现状，不做特殊处理。
4. **退避曲线**：15min → 1h → 6h → 24h → 7d 封顶。追新工单的首个到期时刻即
   `air_date + 宽限期`（创建时写死，见 F1），被动匹配没接住时 worker 才会见到它。
5. 搜索结果写入 `site_torrent` 走现有机制，顺带丰富公共缓存——主动搜索的副产品全局受益。

### F5 投递与状态推进

```
候选通过内核（身份匹配 + 规则过滤 + 评分）
  ① 决策：条件更新认领工单 → 同批选优 → 整季包批量展开
  ② tracker.download_torrent() 取种子字节
  ③ BaseDownloader.submit() → info_hash（幂等）
  ④ match_record 写 accepted（score/info_hash/来源种子）
  ⑤ wanted.status=grabbed，grabbed_at 落表
  ⑥ 派生重算订阅状态（不手工设值）
  ⑦ [P5] check_download_progress 轮询 → downloaded → completed 汇总
```

三个钉死的语义：

- **幂等三层防线**：①的条件更新（`UPDATE … WHERE status='wanted'`，0 行=另一路径已
  抢先）+ ③的 info_hash 判重 + 工单唯一约束。被动/主动两路并发，缺一层都有窗口期。
- **投递失败零新增组件**：②③失败不写 accepted、不推 grabbed，工单退回 wanted 并置
  `next_search_at = now + 短冷却（如 30min）`，worker 下个 tick 自然重拾——失败恢复
  统一复用调度通道。match_record 允许记 `dispatch_failed`（中文原因）供详情页展示。
- **非 wanted 态对候选完全免疫（第一版）**：grabbed/downloaded 遇到更优候选直接忽略；
  这个"忽略"处即 P6 洗版的扩展点（届时改 cutoff 比较），现在不留半成品逻辑。

细节：

1. submit 参数：save_path/category/tags 从下载器配置与全局设置取，tag 建议打
   `movieclaw-sub` 便于后续对账。
2. `already_exists=True`（下载器里已有同 hash）视为成功，正常推进状态。
3. **能力缺口**：确认下载完成需要 `BaseDownloader` 补 `get_torrent(info_hash)` 查询接口
   （qB/Tr 各实现一次），配一个低频轮询任务。**第一版可以先到 grabbed 为止**（提交即视为
   满足），downloaded 状态在 Phase 5 补——不阻塞主流程上线。
4. 汇总状态：电影 wanted 满足 → `completed`；剧集所选季全满足且不追新 → `completed`；
   追新 → 常驻 `tracking`（对应前端 mock 已有的三态）。

### F6 展示与管理（前端）

1. ✅ **订阅列表页**（替换 `mock-media.ts`）：海报墙 + 状态徽标 + 进度（缺 x 集），
   卡片点击进订阅详情分析页。
2. **订阅详情分析页**（`/subscriptions/[id]`，✅ 骨架已上线）：
   - ✅ 追踪明细：按季分组，每个工单一行——状态徽标 + "此刻卡在哪、下一步是什么"
     的调度解释（排队搜索/待播出/未定档/冷却中/已提交下载），与三类调度语义一一对应；
   - ✅ 活动时间线：`subscription_activity` 流水（创建/调整/暂停恢复/收齐；
     P4 起自动出现搜索/匹配/**拒绝原因**/投递记录，回答"为什么还没下到"）；
   - ✅ 操作：暂停/恢复、取消订阅；[P5] 修改季选择、手动「立即搜索」（重置 next_search_at）。
3. **操作**：暂停/恢复（暂停 = worker 跳过其 wanted）、修改季选择与规则组（重算 wanted，
   已 grabbed 的不回收）、删除订阅（不动已下载内容与下载器任务）。
4. 详情页「订阅追踪」按钮接线（组件已有，换真实调用）。

---

## 二、数据模型汇总（全部新表）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `media_item` | kind+tmdb_id 唯一锚、imdb/douban_id、title/original_title/year、aliases JSON、status、poster/backdrop、next_refresh_at | 设计稿 1.3 |
| `media_season` | media_item_id+season_number 唯一、air_date、episode_count、episodes JSON | 集不单独建表（设计稿 1.6） |
| `rule_set` | name(唯一)、is_default、spec JSON（RuleSetSpec：resolutions 顺序即偏好/编码/平台与制作组黑白名单/HDR 三态+DV 三态(正交，DV 单独控)/仅free/做种下限/体积区间(整季包按每集均摊)/HR 三态策略；预留 sites、cutoff_resolution） | 纯参数包，判断逻辑在 matcher；订阅只持引用不做 override；被引用禁删；修改不追溯已 grabbed；评分公式第一版内置不暴露权重 |
| `subscription` | media_item_id(唯一——重复订阅幂等)、kind 冗余、selected_seasons JSON、follow_future、rule_set_id、status(active/paused/completed，派生可重算) | E 的定义：E = 勾选季全部已知集 ∪（follow_future ? 订阅后播出的一切集(含新季) : ∅） |
| `wanted_item` | subscription_id、media_item_id(冗余，被动匹配直达索引)、season/episode(**NOT NULL，电影=(0,0) 哨兵**——SQLite 唯一索引对 NULL 失效，不变量①需 DB 兜底)、status(wanted/grabbed/downloaded)、air_date 快照、priority、next_search_at/search_attempts/last_search_at、grabbed_at | 物化的是"期望单元+满足状态"，status=wanted 子集才是缺口；只进不出（仅移除季选择时删未完成项）。**不存 quality 快照**——洗版所需质量从 match_record→site_torrent.attrs 回溯，不冗余第二真相 |
| `subscription_activity` ✅ | subscription_id、wanted_item_id(SET NULL 保历史)、type(created/adjusted/paused/resumed/completed/reopened + P4 的 searched/match_accepted/match_rejected/grabbed/dispatch_failed/wanted_added)、message(**写入时渲染成完整中文句子**)、payload JSON(结构化细节) | 统一活动流水：订阅透明化的落点，详情页时间线数据源。**原 match_record 方案并入本表**——文中提到的 match_record 记录（accepted/rejected/dispatch_failed）均以活动形式落此表，payload 带 site/torrent/score/reason 结构化字段 |

改动既有：`BaseDownloader` 增 `get_torrent(info_hash)`（Phase 5）；豆瓣 client 补详情能力（后续）。
水位等轻量状态用 `app_setting`，不新建表。

---

## 三、模块落点

```
src/movieclaw_db/models/          media_item.py / subscription.py(含wanted) / rule_set.py / match_record.py
alembic/versions/                 对应迁移（media 一支，subscription 一支，match_record 一支）

src/movieclaw_media/library.py    ✅ 纯函数：fetch_media_profile（档案拉取+别名构建）、resolve_douban_to_tmdb
                                  （media 包不依赖 db，落库编排在 api 层——见下）
src/movieclaw_media/douban.py     [后续] 补详情取 IMDb ID

src/movieclaw_matcher/            ★ 新包：公共匹配内核，纯函数无 IO
  ├─ models.py                    输入/输出契约（候选种子、匹配结果、拒绝原因枚举）
  ├─ identity.py                  第一级：身份匹配（归一化、别名包含、年份/季集约束、ID 精确）
  ├─ rules.py                     第二级：RuleSet 评估 + 评分
  └─ decision.py                  决策：去重、多候选选优、洗版比较（预留）

src/movieclaw_api/services/
  ├─ media_library.py             ✅ MediaLibraryService：ensure_media_item 落库编排、豆瓣收敛入口
  ├─ subscription.py              订阅 CRUD、prepare、wanted 生成/重算
  ├─ media_refresh.py             F3 定时任务（@register_task）
  ├─ wanted_search.py             F4 worker（@register_task）：分组搜索/退避/限流
  ├─ torrent_matcher.py           F2 钩子编排：粗筛→调内核→落台账→调投递；水位管理
  └─ download_dispatch.py         F5：取种子字节 → submit → 状态推进
src/movieclaw_api/api/routes/     subscriptions.py / rule_sets.py

src/movieclaw_downloader/         [P5] base.py 增 get_torrent；qbittorrent/transmission 实现

apps/web/
  ├─ lib/api/subscriptions.ts     API client
  ├─ components/subscribe-dialog  订阅弹层（季选择器 + 追新 + 规则组）
  ├─ components/subscriptions-view  接真数据（mock 三态语义不变）
  └─ components/media-detail-view   订阅按钮接线
```

依赖方向保持现状约定：db 不依赖上层；matcher 只依赖 db 模型的数据形状（传入 dict/dataclass），
不做 IO——所有读写由 api/services 层编排。

---

## 四、分阶段计划

依赖关系：P1 → P2 → P4；**P3 与 P2 可并行**（P3 只依赖 P1 的条目形状）；P5/P6 在 P4 之后。

### Phase 1 媒体身份层 ✅（2026-07-12 完成）
| # | 事项 | 验证 |
|---|---|---|
| 1.1 | ✅ `media_item`/`media_season` 模型 + 迁移（a9b3e6d2c754） | ✅ 升/降/升 + alembic check 零漂移 |
| 1.2 | ✅ `ensure_media_item`：TMDB 建档、别名构建、季集拉取、幂等复用 | ✅ 单测 13 例全过（tests/media + tests/api） |
| 1.3 | ✅ 豆瓣收敛（搜索兜底通路 + 多候选返回结构） | ✅ 命中/歧义/失败/年份回退分支覆盖 |

### Phase 2 订阅层（依赖 P1）✅（2026-07-12 完成）
| # | 事项 | 验证 |
|---|---|---|
| 2.1 | ✅ `subscription`/`wanted_item`/`rule_set` 模型 + 迁移（c8d5e9f3a627）+ 默认规则组懒种子 | ✅ 升/降/升；seed 幂等 |
| 2.2 | ✅ prepare / CRUD API + wanted 生成与 diff 重算（movieclaw_matcher 包以 RuleSetSpec 起步） | ✅ 13 例单测：三类调度语义、diff 不回收 grabbed、派生状态、规则组禁删 |
| 2.3 | ✅ 前端：订阅弹层（季选择/追新/规则组/豆瓣候选确认）、列表页替换 mock、详情按钮接线 | ✅ 浏览器 e2e（stub TMDB）：订阅→已订阅态→取消→重订→列表进度全链路 |

### Phase 3 匹配内核（依赖 P1，可与 P2 并行）✅（2026-07-12 完成）
| # | 事项 | 验证 |
|---|---|---|
| 3.1 | ✅ 归一化 + 身份匹配（identity.py：ID 精确/别名+年份、短别名**整词** token 守卫、电影季噪音容忍、单季剧无季号推断、整季/全集包） | ✅ 表驱动单测（含 Her↔Hercules、沙丘↔沙丘2 等对抗用例） |
| 3.2 | ✅ RuleSet 评估 + 内置评分 + 中文拒绝原因（rules.py：三态保守、整季包体积均摊）；选优 decision.py（**整季包优先**） | ✅ 每条规则的接受/拒绝/未知三态覆盖，47 例全过 |
| 3.3 | ✅ 真实数据样本：从 dev 库 site_torrent 抽样（问心2 全集包、Lie to Me、幽灵公主无类型、Zombi VIII 季噪音、韩综 E536）固化为回归用例 | ✅ 全过；坏 case 已归档进 tests/matcher/test_identity.py |

### Phase 4 管线接线（依赖 P2+P3）✅（2026-07-12 完成，dry-run 投递）
| # | 事项 | 验证 |
|---|---|---|
| 4.1 | ✅ F4 wanted_search：条目分组搜索、结果落库(source=SEARCH)、退避、失败短冷却 | ✅ 失败/无果/命中三态测试 |
| 4.2 | ✅ F2 torrent_matcher：水位驱动（首跑初始化=当前最大 id，历史缓存不参与——铁律落点）+ sync 尾调 + 兜底任务 | ✅ 水位跳历史/跟新/幂等 |
| 4.3 | ✅ F5 download_dispatch：条件更新认领、失败回滚+退避；真实投递已启用（2026-07-24，保存目录过下载器路径映射翻译），SUBSCRIPTION_DISPATCH_DRY_RUN=true 可切回模拟投递调试 | ✅ 认领竞态、整季包优先选优 |
| 4.4 | ✅ F3 media_refresh：分档刷新、新集生长、定档回填、别名保鲜 | ✅ mock diff 新集→追新工单+活动 |
| — | ✅ **端到端验收**（stub 环境） | 详情页时间线完整流水：搜索失败(原因)→720p 拒绝(中文原因)→2160p 整季包模拟投递；追踪明细推进"已提交下载" |

### Phase 5 完成度补全（依赖 P4）
| # | 事项 |
|---|---|
| 5.1 | ~~`BaseDownloader.get_torrent` + 下载完成轮询任务~~ → **并入媒体库计划 L2**（[library.md](library.md)）：下载完成检测与入库整理是同一条管线，终态收紧为 imported |
| 5.2 | 订阅详情页：wanted 明细、匹配/拒绝历史、手动立即搜索 |
| 5.3 | 暂停/恢复/编辑/删除的完整语义 |
| 5.4 | 订阅挂媒体库（library.md L1.2/L1.3）：入库到哪个库 + 投递路径由库推导 |

### Phase 6 后续增强（本期不做，仅记录）
洗版交互（cutoff 已在模型预留）/ 用户自定义规则订阅（内核天然支持）/ NER 双向校验接入
（`enrich_version` 重算机制已就绪）/ 豆瓣详情取 IMDb ID / 日历视图。

---

## 五、风险与开口决策

1. **身份匹配误报**：反向包含对短别名（如《Her》《Up》）风险高——内核需对超短别名加长度
   与词边界守卫；P3 样本集必须覆盖。置信度不足先丢弃并落原因，观察后再决定人工确认交互。
2. **下载完成判定缺口**：P4 结束时状态只到 grabbed，用户看到的"完成"语义在 P5 才闭环——
   前端文案先用"已提交下载"避免误导。
3. **PT 站搜索压力**：F4 的 tick 批量、站点并发、退避三个参数需在真实站点上小流量试跑后定值。
4. **TMDB 依赖**：prepare 同步链路依赖 TMDB 可用性；不可用时订阅创建直接失败（中文报错），
   不做降级建档（与"不建无锚条目"一致）。
