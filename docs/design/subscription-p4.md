# P4 管线接线：详细设计

> 上游文档：[subscription.md](subscription.md)（架构）、[subscription-plan.md](subscription-plan.md)（流程与阶段）。
> 本文是 P4 的实现级设计：模块边界、函数签名、参数表、并发语义、活动埋点，
> 以及对 P3 内核契约的冻结（P4 只消费该契约，P3 可并行开发）。

## 0. 现状核实（2026-07-12，三个影响设计的事实）

1. ✅ `BaseSite.download_torrent(url) -> bytes` 已存在（tracker/base.py:77），F5 取种子字节直接用。
2. ⚠️ **搜索结果目前不落库**：`site_search.py` 只做流式聚合返回，未写 `site_torrent`。
   `TorrentSource.SEARCH` 是预留枚举。→ F4 新增工作项：搜索结果经 `TorrentRepository`
   upsert（source=SEARCH），主动搜索的副产品才能丰富公共缓存、供后续被动匹配复用。
3. ⚠️ `sync_site_torrents` 只统计 `stats.inserted` 数量、不收集新增 ID。
   → 被动匹配改为**水位驱动**：匹配器自己扫 `site_torrent.id > 水位`，
   sync 尾部只加一行无参调用（零侵入），另注册低频兜底任务防漏。

## 1. P3 内核契约（冻结，P4 只消费）

```python
# movieclaw_matcher/models.py（在 RuleSetSpec 基础上补充）

@dataclass(frozen=True)
class TorrentCandidate:      # 由 services 层从 SiteTorrent 行构造
    site_id: str
    torrent_id: str
    title: str
    subtitle: str
    attrs: dict              # TorrentAttrs JSON（year/seasons/episodes/complete/resolution/…）
    imdb_id: str | None
    douban_id: str | None
    size_bytes: int | None
    seeders: int | None
    is_free: bool | None
    download_volume_factor: float | None
    hit_and_run: bool | None
    download_url: str | None

@dataclass(frozen=True)
class MediaIdentity:         # 由 media_item 行构造
    kind: str                # movie / tv
    year: int | None
    aliases: list[str]
    imdb_id: str | None
    douban_id: str | None

@dataclass(frozen=True)
class IdentityMatch:
    units: frozenset[tuple[int, int]]   # 匹配到的 (season, episode)；电影=(0,0)
    is_pack: bool                       # 整季包/全集包（覆盖该季全部单元）
    confidence: str                     # exact_id / title_year / title_only（观察用）
    matched_alias: str | None           # 命中的别名（拒绝原因与活动记录引用）

@dataclass(frozen=True)
class RuleVerdict:
    accepted: bool
    score: int                          # 通过时的排序分（free/做种/分辨率偏好加权）
    reason_code: str | None             # 拒绝时：resolution_not_allowed / not_free / …
    reason_text: str | None             # 拒绝时：完整中文句子，直接进活动流水

# movieclaw_matcher/identity.py
def match_identity(candidate: TorrentCandidate, media: MediaIdentity) -> IdentityMatch | None:
    """None=不是这个内容。优先级：外部 ID 精确 > 别名+年份±1+季集约束。
    短别名（≤2 字符/纯数字）必须叠加年份精确才允许命中（防《Her》类误报）。"""

# movieclaw_matcher/rules.py
def evaluate_rules(candidate: TorrentCandidate, spec: RuleSetSpec,
                   *, pack_episode_count: int = 1) -> RuleVerdict:
    """纯谓词+评分。体积规则对整季包按 size/pack_episode_count 均摊。
    三态字段（H&R/促销）按 spec 策略处理 NULL，绝不把未知当 False。"""

# movieclaw_matcher/decision.py
def pick_best(verdicts: list[tuple[TorrentCandidate, IdentityMatch, RuleVerdict]],
              ) -> tuple[TorrentCandidate, IdentityMatch, RuleVerdict] | None:
    """同一单元多候选选优：**整季包优先于单集**（一个种子覆盖一季，搜索与下载
    管理成本最低——已确认决策），同类内再按 score 降序。"""
```

内核零 IO、零数据库依赖；P4 的 services 层负责构造输入、消费输出。

## 2. 共享评估管道（F2/F4 的汇合点）

`services/subscription_matching.py`（新，编排层，非纯函数）：

```python
async def evaluate_and_dispatch(session, torrents: list[SiteTorrent],
                                *, source: str) -> MatchSummary:
    """给定一批种子，对所有活跃订阅做 身份匹配→规则过滤→选优→投递。
    F2（被动，source="sync"）与 F4（主动，source="search"）都走这里，
    保证两条路径的行为与活动记录完全一致。"""
```

内部步骤：

1. **加载匹配上下文**（一次查询，进程内使用）：有 `status='wanted'` 工单且订阅
   `active` 的 media_item 及其 identity、订阅的 rule_set spec、未满足单元集合；
2. **粗筛**：`attrs` 非空；category 明确属于非影视类的剔除（NULL 不剔除，宁可多算）；
3. 逐种子 `match_identity` → 命中的按单元找 wanted → `evaluate_rules`（整季包传
   `pack_episode_count`）；
4. 按单元聚合候选 → `pick_best` → 交 F5 投递；
5. **活动记录粒度**（防爆表的关键决策）：
   - 身份**不**匹配：不记录（海量噪音，无信息量）；
   - 身份命中但规则拒绝：记 `MATCH_REJECTED`（"《龙之家族》S02E01 有候选被拒：
     分辨率 720p 不在允许范围（要求 ≥1080p）——来自 mteam 的 xxx"）；
     同一 (wanted, site, torrent) 只记一次（进程内 + 查最近活动去重）；
   - 通过并投递：记录在 F5（GRABBED / DISPATCH_FAILED）。

## 3. F2 被动匹配（services/torrent_matcher.py）

- **水位**：`app_setting` namespace `subscription.match_watermark` 存最后处理的
  `site_torrent.id`。函数 `process_new_torrents(session)`：
  `SELECT * FROM site_torrent WHERE id > 水位 ORDER BY id LIMIT 500` 循环到空，
  每批喂给共享管道，处理完推进水位。
- **触发**：① `sync_site_torrents` 尾部一行 `await process_new_torrents(...)`
  （同进程零延迟）；② `@register_task("match_new_torrents", interval=3600)` 低频
  兜底（进程重启期间的漏网 + sync 异常中断）。两处并发安全：水位读改写用
  单飞锁（进程内 asyncio.Lock 足够，单实例部署）。
- 活跃订阅为零时快速返回（一次 count 查询），不做无用扫描。

## 4. F4 主动搜索（services/wanted_search.py）

- **任务**：`@register_task("search_wanted", interval=300)`（5 分钟 tick，兜底节奏）。
  **即时触发**：订阅创建/调整/恢复的路由在响应返回后经 BackgroundTasks 踢一次
  同一 tick 函数（进程内互斥锁串行化；已搜组被退避排期，重入不会重复打站点）。
- **取单**：`wanted.status='wanted' AND next_search_at<=now AND 订阅 active`，
  `ORDER BY priority DESC, next_search_at ASC`，**按 media_item 分组**后逐组
  处理到本轮搜索预算用满为止（一部 20 季老剧 = 1 组）。
- **搜索**：召回词集合 = 英文名 + 中文名 + 原名（`wanted_search.recall_keywords`，
  按匹配内核的归一化形式去重、下发原样文本），**全部下发后按 (站点, 种子ID)
  合并去重**；**按订阅类型带
  分类过滤**（movie→[电影,纪录片,动漫]、tv→[剧集,纪录片,动漫]——只排除明确不可能的
  music/game/av；纪录片电影、动画剧场版在多数站归前两类，与被动粗筛同哲学），站点
  适配层把应用级分类映射为站点分类 ID 下发；调用现有 `search_all_sites`
  （阻塞合并版，站点级并发/失败隔离复用现状）。
- **落库**（新增能力，见 0.2）：结果经 `TorrentRepository.upsert`（source=SEARCH，
  易变层可信）→ 拿落库后的 `SiteTorrent` 行进共享管道（enrich 在 upsert 链路已有）。
- **记账**：组内每个未满足 wanted：`search_attempts+=1`、`last_search_at=now`、
  `next_search_at = now + BACKOFF[min(attempts, len-1)]`；
  活动一条（组级）：`SEARCHED`——"搜索《沙丘2》：3 个站点返回 42 个结果，
  2 个命中身份，1 个通过规则并已投递"（payload 带各站结果数）。
- **搜索失败**（全站失败/超时）：不加 attempts，`next_search_at=now+15min`，
  活动记失败原因（可读中文）。

## 5. F5 投递（services/download_dispatch.py）

```python
async def dispatch(session, wanted_ids: list[int], candidate: TorrentCandidate,
                   subscription: Subscription) -> bool:
```

1. **认领**（防线①）：`UPDATE wanted_item SET status='grabbed', grabbed_at=now
   WHERE id IN (...) AND status='wanted'`——整季包一次认领全部覆盖单元；
   0 行生效 → 全部被抢先，直接返回；部分生效 → 只为生效的继续。
2. **取种**：tracker registry 拿站点实例 → `download_torrent(candidate.download_url)`。
3. **选下载器**：默认下载器（`is_default` 且 usable）；没有 → 视为投递失败。
4. **提交**（防线②）：`BaseDownloader.submit(DownloadRequest(torrent_bytes,
   save_path=下载器配置, category="movieclaw", tags=["movieclaw-sub"]))`；
   `already_exists=True` 视为成功。
5. **成功**：活动 `GRABBED`——"已投递《龙之家族》S02E01：来自 mteam 的
   xxx（2160p · free · 12 做种）"；重算订阅派生状态。
6. **失败**（2/3/4 任一步）：认领回滚——`UPDATE ... SET status='wanted',
   grabbed_at=NULL WHERE id IN (...) AND status='grabbed'`；
   `next_search_at = now + 30min`（复用调度通道重试，零新增组件）；
   活动 `DISPATCH_FAILED`（中文原因：站点取种失败/无可用下载器/下载器拒绝）。

**整季包策略**（✅ 已确认）：**整季包优先于单集**；覆盖 ≥1 个未满足单元即可投
（代价：可能重复下载少量已有集，接受）。

**投递 mock（✅ 已确认；2026-07-24 已切真投递）**：P4 阶段步骤 2~4 曾由配置开关
`SUBSCRIPTION_DISPATCH_DRY_RUN`（当时默认 true）短路为**日志投递器**——不取种、
不碰下载器，打完整中文日志并照常推进状态机。下载器路径映射闭环后开关默认已翻转
为 false（真实投递：取种 → 保存目录过路径映射翻译 → 提交默认下载器）；
设回 true 可随时切回模拟投递调试匹配规则，代码路径不变。

## 6. F3 元数据刷新（services/media_refresh.py）

- **任务**：`@register_task("refresh_media_metadata", interval=900)` tick，
  每 tick 处理 `next_refresh_at IS NULL OR <= now` 的条目最多 5 个。
- **刷新**：复用 P1 的 `fetch_media_profile`（含别名与季集），diff 后：
  1. 更新 `media_item`（status/aliases/海报/年份）与 `media_season`（upsert 按
     季号，episodes JSON 整体替换）；
  2. **wanted 生长**：对该条目每个非 paused 订阅——勾选季的新集一律补工单；
     未勾季新集/新季按 follow_future 补；调度语义按当下判定（已过宽限=now）；
     活动 `WANTED_ADDED`："发现新集 S03E01（8 月 1 日播出），已加入追踪"；
  3. **改档期**：同步 `wanted.air_date`；未定档→定档回填 `next_search_at =
     air+宽限`；档期延后且工单未满足 → 顺延 next_search_at；
  4. 剧集 status 变完结 → 重算订阅派生状态（COMPLETED 活动自然产生）。
- **分档**（写回 next_refresh_at，待确认决策④）：

| 档位 | 条件 | 间隔 |
|---|---|---|
| 在播剧 | tv & Returning Series & 有非 paused 订阅 | 8h |
| 未上映 | movie 未上映 / tv 有未定档季，有订阅 | 24h |
| 已完结/已上映有订阅 | 其余有订阅 | 7d |
| 无订阅条目 | —— | 30d（仅保鲜别名，防归档条目腐烂） |

## 7. 并发与幂等（三层防线的实现落点）

- 防线①条件更新在 F5 认领（第 5 节步骤 1/6）；
- 防线② info_hash 幂等由下载器适配器保证（已实现）；
- 防线③唯一约束防工单重复（已上线）；
- F2 与 F4 同 tick 命中同一单元：都汇入共享管道 + 认领兜底，无需额外锁；
- 单实例部署假设：水位与 tick 任务用进程内锁即可，不引入分布式锁。

## 8. 参数表（集中在 services/subscription_matching.py 顶部常量）

| 常量 | 值 | 说明 |
|---|---|---|
| `SEARCH_TICK_SECONDS` | 300 | F4 tick |
| `SEARCH_REQUESTS_PER_TICK` | 4 | 每 tick 的**搜索次数**预算（站点压力主阀门）。原为 `SEARCH_GROUPS_PER_TICK=2`（按条目组计），改按次计是因为一个条目组的召回词从 1 个变成最多 3 个（见下），按组计会让站点压力悄悄放大 3 倍。组不中途截断，故每 tick 每站硬上限 6 次 |
| `SEARCH_BACKOFF` | 15min→1h→6h→24h→7d | 退避曲线（按 attempts 取档）|
| `DISPATCH_RETRY_DELAY` | 30min | 投递失败重试 |
| `MATCH_BATCH_SIZE` | 500 | 被动匹配每批行数 |
| `REFRESH_PER_TICK` | 5 | F3 每 tick 条目数 |
| `FUTURE_GRACE` | 48h | （P2 已有）追新宽限 |

需在真实站点小流量试跑后校准的：`SEARCH_REQUESTS_PER_TICK`、退避曲线首档。

## 9. 实现顺序与验证

| 步骤 | 内容 | 验证 |
|---|---|---|
| P4.0 | P3 内核（identity/rules/decision + 真实数据样本集）——前置 | 表驱动单测 + 样本集通过率 |
| P4.1 | F5 投递（mock 下载器/站点可独立测） | 认领竞态、失败回滚、already_exists |
| P4.2 | 共享评估管道 + F2 水位匹配 | 模拟新种子入库命中；重启不漏不重 |
| P4.3 | F4 worker（含搜索结果落库） | 20 季订阅=常数次搜索；退避曲线；SEARCHED 活动 |
| P4.4 | F3 刷新与 wanted 生长 | mock TMDB diff 出新集 → 追新订阅长出工单 + 活动 |
| —— | **端到端验收** | 订阅在播剧：详情页时间线出现 搜索→拒绝(原因)→投递 完整流水；种子出现在下载器 |

## 10. 决策（2026-07-12 已确认）

1. ✅ **拒绝记录粒度**：只记"身份命中但规则拒绝"，身份不匹配不记（噪音）。
2. ✅ **整季包优先于单集**（用户确认，与初稿建议相反）；覆盖 ≥1 缺口即投。
3. ✅ **搜索节流初值**：tick 5min × 每 tick 2 组 × 退避首档 15min，试跑后校准。
4. ✅ **F3 分档**：见第 6 节表格。
5. ✅ **下载器先 mock**：`SUBSCRIPTION_DISPATCH_DRY_RUN` 曾默认开（见第 5 节）；
   2026-07-24 已切真投递（开关默认 false），只投默认下载器的策略随之生效。
6. ✅ **站点范围**：搜索/匹配覆盖全部启用站点（`rule_set.sites` 预留不消费）。
