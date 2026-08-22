"""订阅匹配的共享评估管道——被动匹配（F2）与主动搜索（F4）的汇合点。

给定一批 ``site_torrent`` 行，对所有活跃订阅执行：
身份匹配（内核第一级）→ 规则过滤（第二级）→ 选优 → 投递（F5）。
两条路径共用本管道，保证行为与活动记录完全一致（docs/design/subscription-p4.md 第 2 节）。

活动记录粒度（防爆表的关键决策，已确认）：
- 身份不匹配：不记录（海量噪音）；
- 身份命中但规则拒绝：记 MATCH_REJECTED（可解释性的核心），同一
  (订阅, 站点, 种子) 只记一次；
- 通过并投递：由投递层记 GRABBED / DISPATCH_FAILED。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models import (
    ActivityType,
    MediaItem,
    RuleSet,
    SiteCredential,
    SiteTorrent,
    Subscription,
    SubscriptionActivity,
    SubscriptionStatus,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories import SubscriptionRepository
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import (
    IdentityMatch,
    MediaIdentity,
    QualitySnapshot,
    RuleSetSpec,
    RuleVerdict,
    TorrentCandidate,
    candidate_ladder_rank,
    evaluate_rules,
    match_identity,
)
from movieclaw_tracker.datetime_utils import DEFAULT_SITE_TIMEZONE

logger = logging.getLogger("movieclaw_api.subscription_matching")

# ---------------------------------------------------------------------------
# 管线参数（docs/design/subscription-p4.md 第 8 节；标注 ⚠ 的需真实站点试跑校准）
# ---------------------------------------------------------------------------

SEARCH_TICK_SECONDS = 300  # ⚠ F4 tick 间隔
SEARCH_GROUPS_PER_TICK = 2  # ⚠ 每 tick 搜索的条目组数（站点压力主阀门）
SEARCH_BACKOFF = (  # ⚠ 退避曲线：按 search_attempts 取档，超出取末档
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(days=7),
)
SEARCH_FAILURE_RETRY = timedelta(minutes=15)  # 搜索本身失败（非无结果）的重试间隔
DISPATCH_RETRY_DELAY = timedelta(minutes=30)  # 投递失败后经调度通道重试
MATCH_BATCH_SIZE = 500  # 被动匹配每批处理的种子行数
REFRESH_PER_TICK = 5  # F3 每 tick 刷新的条目数

# -- 洗版调度参数（docs/design/quality-upgrade.md §6.4）------------------------
# 被动匹配是洗版主通道，主动搜索只是极低频兜底：洗版不急，对 PT 站克制是铁律
UPGRADE_BACKOFF = (timedelta(days=7), timedelta(days=14), timedelta(days=30))
UPGRADE_PRIORITY = -10  # 永远排在补旧(0)与追新(10)后面
UPGRADE_FUSE_LIMIT = 3  # 连续证伪熔断阈值（§6.3）
UPGRADE_FUSE_COOLDOWN = timedelta(days=30)  # 熔断后的长冷却
UPGRADE_FIRST_SEARCH_SPREAD_HOURS = 24  # 首搜在 24h 内错峰，避免瞬时搜索风暴
# 覆盖了缺口单元的候选在"洗版档位"这一排序位上的取值：空元组低于任何非空
# 向量，使缺口单元的竞争者之间仍完全由评分决定顺序（§15.4）。
# 用空元组而不是 (-1, -1)：档位向量的长度随规则组的 upgrade_ladder 变化，
# 定长哨兵会在多维阶梯下与真实向量按位比较出无意义的结果
_NO_UPGRADE_RANK: tuple[int, ...] = ()


def upgrade_backoff_delay(attempts: int) -> timedelta:
    """洗版搜索退避档位（7d → 14d → 30d 封顶）。"""
    return UPGRADE_BACKOFF[min(attempts, len(UPGRADE_BACKOFF) - 1)]

_SITE_CALENDAR_TIMEZONE = ZoneInfo(DEFAULT_SITE_TIMEZONE)


def publish_calendar_date(value: datetime | None) -> date:
    """把库内 UTC 发布时间换回站点业务日历日期。

    播出日与集数有效期按中国站点的本地日期判断。直接对 naive UTC 调 ``date``
    会把本地凌晨发布误算到前一天，导致本该覆盖的单集资源被过滤。
    """
    utc_value = value or utcnow()
    aware = (
        utc_value.replace(tzinfo=UTC)
        if utc_value.tzinfo is None
        else utc_value.astimezone(UTC)
    )
    return aware.astimezone(_SITE_CALENDAR_TIMEZONE).date()


def backoff_delay(attempts: int) -> timedelta:
    """按已尝试次数取退避档位（attempts 从 0 起：首次未果 → 15 分钟后再试）。"""
    return SEARCH_BACKOFF[min(attempts, len(SEARCH_BACKOFF) - 1)]


# ---------------------------------------------------------------------------
# 匹配上下文：一次加载，整批复用
# ---------------------------------------------------------------------------


@dataclass
class MediaContext:
    """单个媒体条目的匹配上下文：身份 + 该条目的未满足工单与规则。

    洗版（quality-upgrade.md §5/§6）：``upgrade_wanted`` 是"已入库但可证明
    低于洗版目标、且当前无在途洗版投递"的单元；``upgrade_snapshots`` 是
    这些单元的当前版本质量快照（比较基线）；``upgrade_excluded`` 是该订阅
    既往证伪候选的 (site_id, torrent_id) 排除清单。
    """

    item: MediaItem
    identity: MediaIdentity
    subscription: Subscription
    spec: RuleSetSpec
    open_wanted: dict[tuple[int, int], WantedItem]
    # 内容核验的负面记忆：{(季, 集): {(站点, 种子ID), ...}}——这些发布下完后
    # 被证明不含该集，选种阶段直接跳过，否则会无限重抓（见
    # SubscriptionDownloadAttempt.content_missing）
    content_missing: dict[tuple[int, int], set[tuple[str, str]]] = field(default_factory=dict)
    upgrade_wanted: dict[tuple[int, int], WantedItem] = field(default_factory=dict)
    upgrade_snapshots: dict[tuple[int, int], QualitySnapshot] = field(default_factory=dict)
    upgrade_excluded: frozenset[tuple[str, str]] = frozenset()
    # 已入库但**不可洗**的单元（到顶/快照不可比/熔断冷却/洗版在途）：
    # 整季包铁律的另一半——包覆盖到任何一个这样的单元就整体放弃洗版维度，
    # 否则"为洗一部分重下整季"会把已到顶的集也重新下一遍
    upgrade_blocked: dict[tuple[int, int], WantedItem] = field(default_factory=dict)


@dataclass
class MatchSummary:
    """一批评估的结果统计（活动与日志用）。"""

    torrents_seen: int = 0
    identity_hits: int = 0
    rejected: int = 0
    dispatched_units: int = 0
    dispatched_torrents: list[str] = field(default_factory=list)


async def _load_specs(session: AsyncSession) -> dict[int, RuleSetSpec | None]:
    """一次载入全部规则组并解析 spec。

    None = spec 解析失败的坏规则组（如版本回退后 JSON 里有当前版本不认识的
    枚举值）：跳过引用它的订阅并大声报错，绝不让一条脏配置拖垮整轮匹配，
    也不静默降级为"全不限"（那会乱抓资源）。
    """
    specs: dict[int, RuleSetSpec | None] = {}
    for rule_set in (await session.execute(select(RuleSet))).scalars():
        if rule_set.id is None:
            continue
        try:
            specs[rule_set.id] = RuleSetSpec.model_validate(rule_set.spec or {})
        except ValueError:
            specs[rule_set.id] = None
            logger.exception(
                "规则组「%s」(id=%s) 的过滤参数无法解析，引用它的订阅本轮"
                "跳过匹配。常见原因：应用回退到旧版本后规则组包含新版本"
                "字段值，请在规则组页面重新保存修正",
                rule_set.name,
                rule_set.id,
            )
    return specs


async def _ensure_context(
    session: AsyncSession,
    contexts: dict[int, MediaContext],
    media_item_id: int,
    subscription: Subscription,
    spec: RuleSetSpec,
) -> MediaContext | None:
    """按需创建条目上下文（缺口与洗版两条加载路径共用）。"""
    ctx = contexts.get(media_item_id)
    if ctx is not None:
        return ctx
    item = await session.get(MediaItem, media_item_id)
    if item is None:  # 外键保证下理论不可达
        return None
    ctx = MediaContext(
        item=item,
        identity=MediaIdentity(
            kind=item.kind,
            year=item.year,
            aliases=tuple(item.aliases),
            imdb_id=item.imdb_id,
            douban_id=item.douban_id,
            season_numbers=(),  # 先占位，收集完工单后统一回填
        ),
        subscription=subscription,
        spec=spec,
        open_wanted={},
    )
    contexts[media_item_id] = ctx
    return ctx


def upgrade_ready(wanted: WantedItem, spec: RuleSetSpec, *, now: datetime) -> bool:
    """该单元当下是否可参与洗版（quality-upgrade.md §2.4/§6.3 的调度口径）。

    - 快照必须能证明"低于洗版目标"（证明不了的安静，不打扰站点）；
    - 证伪熔断中的单元（连续证伪达阈值且长冷却**未到期**）不参与。
      冷却到期即恢复（计数由 postpone_upgrade_wanted 在恢复后清零重新观察）；
      next_search_at 为 None 不算熔断中——排期缺失不能变成永久停摆。
    """
    from movieclaw_matcher import provably_below_cutoff

    if not wanted.quality:  # NULL 未回填 / {} 无法识别哨兵
        return False
    snapshot = QualitySnapshot.model_validate(wanted.quality)
    if not provably_below_cutoff(snapshot, spec):
        return False
    fused = (  # 熔断冷却中
        wanted.upgrade_verify_failures >= UPGRADE_FUSE_LIMIT
        and wanted.next_search_at is not None
        and wanted.next_search_at > now
    )
    return not fused


async def load_match_context(session: AsyncSession) -> dict[int, MediaContext]:
    """加载匹配上下文：缺口单元 + 洗版单元，{media_item_id: MediaContext}。

    活跃订阅通常只有几十个，整体载入进程内、逐种子比对是可承受的；
    返回空 dict 表示当下既没有缺口也没有洗版目标，调用方应快速返回。
    """
    from movieclaw_matcher import QualitySnapshot

    specs = await _load_specs(session)

    contexts: dict[int, MediaContext] = {}
    result = await session.execute(
        select(WantedItem, Subscription)
        .join(Subscription, WantedItem.subscription_id == Subscription.id)  # type: ignore[arg-type]
        .where(
            WantedItem.status == WantedStatus.WANTED,
            WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )
    for wanted, subscription in result.all():
        spec = specs.get(subscription.rule_set_id, RuleSetSpec())
        if spec is None:
            continue  # 坏规则组，_load_specs 已报错
        ctx = await _ensure_context(session, contexts, wanted.media_item_id, subscription, spec)
        if ctx is None:
            continue
        ctx.open_wanted[(wanted.season_number, wanted.episode_number)] = wanted

    # -- 内容核验的负面记忆 ---------------------------------------------------
    # 下完后被证明"包里根本没有这一集"的发布不再参与该集的选种。缺了这一步，
    # 退回重找会立刻再次选中同一个种子（它还在下载器里且已完成），秒完成 →
    # 再核验 → 再退回，无限循环。
    gap_subscriptions = {
        ctx.subscription.id
        for ctx in contexts.values()
        if ctx.open_wanted and ctx.subscription.id is not None
    }
    if gap_subscriptions:
        from movieclaw_db.models import SubscriptionDownloadAttempt

        proven: dict[int, dict[tuple[int, int], set[tuple[str, str]]]] = {}
        attempts = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id.in_(gap_subscriptions),  # type: ignore[union-attr]
                )
            )
        ).scalars()
        for attempt in attempts:
            memory = attempt.content_missing or {}
            sources = {
                (str(source[0]), str(source[1]))
                for source in memory.get("sources", [])
                if isinstance(source, list) and len(source) == 2
            }
            if not sources:
                continue
            for unit in memory.get("units", []):
                if isinstance(unit, list) and len(unit) == 2:
                    proven.setdefault(attempt.subscription_id, {}).setdefault(
                        (int(unit[0]), int(unit[1])), set()
                    ).update(sources)
        for ctx in contexts.values():
            if ctx.subscription.id is not None:
                ctx.content_missing = proven.get(ctx.subscription.id, {})

    # -- 洗版单元（quality-upgrade.md §5）------------------------------------
    upgrade_rule_ids = {
        rid for rid, spec in specs.items() if spec is not None and spec.upgrade_source is not None
    }
    if upgrade_rule_ids:
        now = utcnow()
        # 拿全部 imported 单元（含 quality NULL 的）：可洗的进 upgrade_wanted，
        # 其余进 upgrade_blocked——铁律需要知道"包会不会碰到不该重下的集"
        result = await session.execute(
            select(WantedItem, Subscription)
            .join(Subscription, WantedItem.subscription_id == Subscription.id)  # type: ignore[arg-type]
            .where(
                WantedItem.status == WantedStatus.IMPORTED,
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                # 洗版的主场景恰恰是"已收齐"（completed）的订阅——只有
                # 用户显式暂停才停（quality-upgrade.md §6.3）
                Subscription.status != SubscriptionStatus.PAUSED,  # type: ignore[arg-type]
                Subscription.rule_set_id.in_(upgrade_rule_ids),  # type: ignore[union-attr]
            )
        )
        upgrade_subs: set[int] = set()
        for wanted, subscription in result.all():
            spec = specs.get(subscription.rule_set_id)
            if spec is None:
                continue
            ctx = await _ensure_context(
                session, contexts, wanted.media_item_id, subscription, spec
            )
            if ctx is None:
                continue
            unit = (wanted.season_number, wanted.episode_number)
            if wanted.quality and upgrade_ready(wanted, spec, now=now):
                ctx.upgrade_wanted[unit] = wanted
                ctx.upgrade_snapshots[unit] = QualitySnapshot.model_validate(wanted.quality)
                if subscription.id is not None:
                    upgrade_subs.add(subscription.id)
            else:
                ctx.upgrade_blocked[unit] = wanted

        if upgrade_subs:
            # 在途去重与证伪排除：一次载入相关订阅的洗版 attempt 历史
            from movieclaw_db.models import DownloadAttemptStatus, SubscriptionDownloadAttempt

            attempts = (
                await session.execute(
                    select(SubscriptionDownloadAttempt).where(
                        SubscriptionDownloadAttempt.subscription_id.in_(upgrade_subs),  # type: ignore[union-attr]
                        SubscriptionDownloadAttempt.purpose == "upgrade",
                    )
                )
            ).scalars()
            in_flight: dict[int, set[tuple[int, int]]] = {}
            excluded: dict[int, set[tuple[str, str]]] = {}
            terminal = {
                DownloadAttemptStatus.SUPERSEDED,
                DownloadAttemptStatus.RETAINED,
                DownloadAttemptStatus.IMPORTED,
                DownloadAttemptStatus.FAILED,
                DownloadAttemptStatus.CANCELLED,
            }
            for attempt in attempts:
                units = {
                    (int(u[0]), int(u[1]))
                    for u in attempt.units
                    if isinstance(u, list) and len(u) == 2
                }
                if attempt.status == DownloadAttemptStatus.FAILED:
                    # 证伪的候选进排除清单（§4.3），同一假资源不抓第二次
                    if attempt.site_id and attempt.torrent_id:
                        excluded.setdefault(attempt.subscription_id, set()).add(
                            (attempt.site_id, attempt.torrent_id)
                        )
                elif attempt.status not in terminal:
                    in_flight.setdefault(attempt.subscription_id, set()).update(units)
            for ctx in contexts.values():
                sub_id = ctx.subscription.id
                if sub_id is None or not ctx.upgrade_wanted:
                    continue
                for unit in in_flight.get(sub_id, ()):  # 在途单元本轮不再比，转入铁律阻挡集
                    moved = ctx.upgrade_wanted.pop(unit, None)
                    ctx.upgrade_snapshots.pop(unit, None)
                    if moved is not None:
                        ctx.upgrade_blocked[unit] = moved
                ctx.upgrade_excluded = frozenset(excluded.get(sub_id, set()))

    # 空上下文（缺口与洗版都为零）没有比对价值，剔除以便调用方快速返回
    contexts = {
        media_id: ctx
        for media_id, ctx in contexts.items()
        if ctx.open_wanted or ctx.upgrade_wanted
    }

    # 回填已知季号（"无季号单集"的安全推断依赖它；从工单推导即覆盖订阅关心的季）
    for ctx in contexts.values():
        seasons = tuple(
            sorted({s for s, _ in ctx.open_wanted} | {s for s, _ in ctx.upgrade_wanted})
        )
        ctx.identity = MediaIdentity(
            kind=ctx.identity.kind,
            year=ctx.identity.year,
            aliases=ctx.identity.aliases,
            imdb_id=ctx.identity.imdb_id,
            douban_id=ctx.identity.douban_id,
            season_numbers=seasons,
        )
    return contexts


# ---------------------------------------------------------------------------
# 评估与投递
# ---------------------------------------------------------------------------


# 站点分类粗筛：这些分类的资源不可能满足影视订阅。真实教训——《霸王别姬》
# 的原声音乐专辑（标题含英文名+年份精确匹配）曾胜出投递：站点已经明确告诉
# 我们它是 music，必须在进内核之前剔除。分类 NULL（未知）与 other（杂项，
# 语义模糊）不剔除，宁可多算——电影/剧集互斥由 attrs.media_type 冲突检查兜住。
_NON_VIDEO_CATEGORIES = frozenset({"music", "game", "av"})


def to_candidate(row: SiteTorrent) -> TorrentCandidate | None:
    """SiteTorrent 行 → 内核候选。粗筛：未扩充属性 / 明确非影视分类的行不可匹配。"""
    if not row.attrs:
        return None
    if row.category in _NON_VIDEO_CATEGORIES:
        return None
    return TorrentCandidate(
        site_id=row.site_id,
        torrent_id=row.torrent_id,
        title=row.title,
        subtitle=row.subtitle,
        attrs=TorrentAttrs.model_validate(row.attrs),
        imdb_id=row.imdb_id,
        douban_id=row.douban_id,
        size_bytes=row.size_bytes,
        seeders=row.seeders,
        is_free=row.is_free,
        hit_and_run=row.hit_and_run,
        download_url=row.download_url,
        publish_time=row.publish_time,
    )


def covered_units(
    match: IdentityMatch,
    open_units: dict[tuple[int, int], WantedItem],
    *,
    published,
) -> list[WantedItem]:
    """身份匹配结果 × 未满足工单 → 本候选能满足的工单列表（整季/全集包在此展开）。

    **发布时间是覆盖范围的物理上限**（真实教训：2025-12 发布的他剧整季包曾把
    2026-06 才开播订阅的未播集标记为已投递）：
    - 整季/全集包展开只覆盖"种子发布时已播出"的集；未定档集无证据，不覆盖；
    - 显式声明的集号（标题写明 E05）信其声明，但播出日期晚于发布时间的仍剔除
      ——未来的集在物理上不可能已经存在于种子里。
    ``published``：种子发布日期（date）；未知时调用方传评估当日（保守可用）。
    """

    def _airable(w: WantedItem, *, require_dated: bool) -> bool:
        if w.air_date is None:
            return not require_dated
        return w.air_date <= published

    if match.is_complete_series:
        # 全集包不承诺特别篇（season 0 的具体集）：市面上的"全集/合集"几乎
        # 从不收录 SP/短剧集，赌它包含的代价是工单挂上一个永远等不来的种子
        # （真实教训：绝命毒师全集包 62 个文件全是 S01-S05 正剧，S00 工单
        # 以 grabbed 卡死"等待入库"）。特别篇只信显式声明——标题写明 S00/SP
        # 时走 episodes / pack_seasons 通道覆盖。电影单元 (0, 0) 不受影响。
        return [
            w
            for w in open_units.values()
            if not (w.season_number == 0 and w.episode_number > 0)
            and _airable(w, require_dated=True)
        ]
    result = [
        w
        for key, w in open_units.items()
        if key in match.episodes and _airable(w, require_dated=False)
    ]
    if match.pack_seasons:
        result.extend(
            w
            for (season, _), w in open_units.items()
            if season in match.pack_seasons and w not in result and _airable(w, require_dated=True)
        )
    return result


def drop_proven_missing(
    ctx: MediaContext, candidate: TorrentCandidate, covered: list[WantedItem]
) -> list[WantedItem]:
    """剔除"这份发布下完后被证明没有"的集。

    负面记忆按 (季, 集) 记，不是整个候选一票否决：全集包缺一集特别篇时，它对
    其余集仍然有效——真实教训是"缺 1 集"把整包重抓无数遍，不是包本身没用。
    """
    if not ctx.content_missing:
        return covered
    source = (candidate.site_id, candidate.torrent_id)
    return [
        wanted
        for wanted in covered
        if source
        not in ctx.content_missing.get((wanted.season_number, wanted.episode_number), ())
    ]


async def _resolve_upgrade(
    repo: SubscriptionRepository,
    ctx: MediaContext,
    candidate: TorrentCandidate,
    match: IdentityMatch,
    units: dict[tuple[int, int], WantedItem],
    published,
    source: str,
    *,
    quiet: bool = False,
) -> tuple[list[WantedItem], tuple[str, str] | None]:
    """候选对洗版单元的判定（quality-upgrade.md §5）。

    整季包铁律：候选覆盖的**每个**洗版单元都必须构成合法升级，任一判否即
    整体放弃洗版维度（缺口维度不受影响）——杜绝"为洗 3 集重下整季"。

    噪音控制：``upgrade_not_better`` / ``upgrade_at_cutoff`` 是常态不记活动；
    ``upgrade_not_comparable`` 是用户该知道的数据质量问题，按 MATCH_REJECTED
    既有去重规则记录（第二遍 ``quiet=True`` 不重复记）。

    返回 (可洗单元列表, (当前档位, 候选档位) 标签)——标签供投递活动文案。
    """
    from movieclaw_matcher import compare_upgrade

    if not units:
        return [], None
    if (candidate.site_id, candidate.torrent_id) in ctx.upgrade_excluded:
        return [], None  # 既往证伪的候选（§4.3），不抓第二次
    covered = covered_units(match, units, published=published)
    if not covered:
        return [], None
    # 铁律的另一半：包还覆盖了不可洗的已入库单元（到顶/不可比/在途）→
    # 抓它就是为洗一部分重下整季，整体放弃洗版维度
    if ctx.upgrade_blocked and covered_units(match, ctx.upgrade_blocked, published=published):
        return [], None
    labels: tuple[str, str] | None = None
    for wanted in covered:
        snapshot = ctx.upgrade_snapshots.get((wanted.season_number, wanted.episode_number))
        if snapshot is None:  # 理论不可达：快照与单元同步维护
            return [], None
        upgrade_verdict = compare_upgrade(candidate, snapshot, ctx.spec)
        if not upgrade_verdict.accepted:
            if upgrade_verdict.reason_code == "upgrade_not_comparable" and not quiet:
                await _log_rejection(repo, ctx, candidate, covered, upgrade_verdict, source)
            return [], None
        if labels is None and upgrade_verdict.current_label and upgrade_verdict.candidate_label:
            labels = (upgrade_verdict.current_label, upgrade_verdict.candidate_label)
    return covered, labels


async def drop_protected_sites(
    session: AsyncSession, torrents: list[SiteTorrent]
) -> list[SiteTorrent]:
    """站点保护第二道闸（第一道在搜索扇出）：剔除受保护站点的种子行。

    被动匹配（种子同步照常索引受保护站点）只有这一道闸，绝不可绕过，
    见 docs/design/site-protection-ratio-boost.md 1.2。
    """
    if not torrents:
        return torrents
    protected_sites = set(
        (
            await session.execute(
                select(SiteCredential.site_id).where(
                    SiteCredential.protected == True  # noqa: E712 -- SQL 表达式需用 ==
                )
            )
        )
        .scalars()
        .all()
    )
    if not protected_sites:
        return torrents
    return [t for t in torrents if t.site_id not in protected_sites]


async def evaluate_and_dispatch(
    session: AsyncSession, torrents: list[SiteTorrent], *, source: str
) -> MatchSummary:
    """共享管道主入口：一批种子 × 全部活跃缺口 → 匹配/过滤/选优/投递。

    ``source`` 是可读中文（"被动匹配"/"主动搜索"），进日志与活动 payload。
    """
    # 循环导入规避：投递层引用本模块的常量
    from movieclaw_api.services.subscription.dispatch import dispatch

    summary = MatchSummary(torrents_seen=len(torrents))
    torrents = await drop_protected_sites(session, torrents)
    contexts = await load_match_context(session)
    if not contexts:
        return summary

    # 第一遍：逐种子评估，按条目聚合通过的候选，规则拒绝当场记活动
    accepted: dict[
        int, list[tuple[TorrentCandidate, IdentityMatch, RuleVerdict, tuple[int, ...]]]
    ] = {}
    repo = SubscriptionRepository(session)
    for row in torrents:
        candidate = to_candidate(row)
        if candidate is None:
            continue
        published = publish_calendar_date(candidate.publish_time)
        for media_id, ctx in contexts.items():
            match = match_identity(candidate, ctx.identity)
            if match is None:
                continue
            covered = drop_proven_missing(
                ctx, candidate, covered_units(match, ctx.open_wanted, published=published)
            )
            upgrade_covered, _ = await _resolve_upgrade(
                repo, ctx, candidate, match, ctx.upgrade_wanted, published, source
            )
            if not covered and not upgrade_covered:
                continue  # 身份命中但既无缺口也无可洗单元，无需任何动作
            summary.identity_hits += 1
            pack_units = len(match.episodes) or (len(covered) + len(upgrade_covered))
            verdict = evaluate_rules(candidate, ctx.spec, pack_episode_count=pack_units)
            if not verdict.accepted:
                summary.rejected += 1
                await _log_rejection(
                    repo, ctx, candidate, covered or upgrade_covered, verdict, source
                )
                continue
            # 洗版选优的排序位次（§15.4）：只有**纯洗版**候选才带真实档位。
            # 判据带上 `not covered` 是有意的——凡是覆盖了缺口单元的候选一律
            # 取哨兵值，于是缺口单元的竞争者之间仍完全按评分排序，本次改动
            # 对缺口侧的选优结果可证明为零影响（免费优先是既有决策，
            # 不该被一个只修洗版的补丁顺带改掉）。
            upgrade_rank = (
                candidate_ladder_rank(candidate.attrs, ctx.spec)
                if upgrade_covered and not covered
                else _NO_UPGRADE_RANK
            )
            accepted.setdefault(media_id, []).append(
                (candidate, match, verdict, upgrade_rank)
            )

    # 第二遍：按条目选优投递。整季包优先（已确认决策）；一个候选投出后，
    # 它覆盖的单元从缺口/洗版清单里划掉，剩余单元继续由次优候选补。
    # 洗版档位排在评分之前（§15.4）：洗版不急，宁可等一个档位更高的，也不要
    # 因为免费加分先抓个"只高半档"的版本、下一轮再洗一次（同一单元两次下载）。
    # 缺口侧不受影响——覆盖缺口的候选档位位次恒为 _NO_UPGRADE_RANK（见上），
    # 而纯洗版候选按定义不碰缺口单元，两侧的选优互不干扰。
    for media_id, entries in accepted.items():
        ctx = contexts[media_id]
        entries.sort(
            key=lambda e: (e[1].is_pack, e[3], e[2].score, e[0].seeders or 0), reverse=True
        )
        remaining = dict(ctx.open_wanted)
        remaining_upgrade = dict(ctx.upgrade_wanted)
        for candidate, match, verdict, _rank in entries:
            published = publish_calendar_date(candidate.publish_time)
            targets = drop_proven_missing(
                ctx, candidate, covered_units(match, remaining, published=published)
            )
            upgrade_targets, upgrade_labels = await _resolve_upgrade(
                repo, ctx, candidate, match, remaining_upgrade, published, source, quiet=True
            )
            if not targets and not upgrade_targets:
                continue
            done = await dispatch(
                session,
                subscription=ctx.subscription,
                item=ctx.item,
                wanted_rows=targets,
                candidate=candidate,
                verdict=verdict,
                source=source,
                upgrade_rows=upgrade_targets,
                upgrade_labels=upgrade_labels,
            )
            if done:
                summary.dispatched_units += len(targets) + len(upgrade_targets)
                summary.dispatched_torrents.append(f"{candidate.site_id}/{candidate.torrent_id}")
                for w in targets:
                    remaining.pop((w.season_number, w.episode_number), None)
                for w in upgrade_targets:
                    remaining_upgrade.pop((w.season_number, w.episode_number), None)

    if summary.identity_hits:
        logger.info(
            "%s：评估 %d 个种子，身份命中 %d，规则拒绝 %d，投递覆盖 %d 个单元",
            source,
            summary.torrents_seen,
            summary.identity_hits,
            summary.rejected,
            summary.dispatched_units,
        )
    return summary


async def _log_rejection(
    repo: SubscriptionRepository,
    ctx: MediaContext,
    candidate: TorrentCandidate,
    covered: list[WantedItem],
    verdict: RuleVerdict,
    source: str,
) -> None:
    """记一条规则拒绝活动；同一 (订阅, 站点, 种子) 去重（查最近活动）。"""
    subscription_id = ctx.subscription.id
    assert subscription_id is not None
    recent = await repo.list_activities(subscription_id, limit=200)
    for activity in recent:
        if (
            activity.type == ActivityType.MATCH_REJECTED
            and activity.payload.get("site_id") == candidate.site_id
            and activity.payload.get("torrent_id") == candidate.torrent_id
        ):
            return  # 已经解释过这个候选为什么被拒，不重复刷屏
    units_label = units_text(covered)
    await repo.add_activity(
        SubscriptionActivity(
            subscription_id=subscription_id,
            wanted_item_id=covered[0].id,
            type=ActivityType.MATCH_REJECTED,
            message=(
                f"{units_label}有候选被拒：{verdict.reason_text}"
                f"——来自 {candidate.site_id} 的「{candidate.title[:60]}」"
            ),
            payload={
                "site_id": candidate.site_id,
                "torrent_id": candidate.torrent_id,
                "reason_code": verdict.reason_code,
                "source": source,
                "units": [[w.season_number, w.episode_number] for w in covered],
            },
        )
    )


def units_text(rows: list[WantedItem]) -> str:
    """工单列表 → 可读单元描述："正片" / "S02E01" / "S02E01 等 8 集"。"""
    first = rows[0]
    if first.season_number == 0 and first.episode_number == 0:
        return "正片"
    label = f"S{first.season_number:02d}E{first.episode_number:02d}"
    if len(rows) == 1:
        return label
    return f"{label} 等 {len(rows)} 集"
