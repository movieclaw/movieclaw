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
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.subscription.quality import normalized_policy, profile_verdict
from movieclaw_db.models import (
    ActivityType,
    MediaItem,
    RuleSet,
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
    RuleSetSpec,
    RuleVerdict,
    TorrentCandidate,
    evaluate_rules,
    match_identity,
)

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


def backoff_delay(attempts: int) -> timedelta:
    """按已尝试次数取退避档位（attempts 从 0 起：首次未果 → 15 分钟后再试）。"""
    return SEARCH_BACKOFF[min(attempts, len(SEARCH_BACKOFF) - 1)]


# ---------------------------------------------------------------------------
# 匹配上下文：一次加载，整批复用
# ---------------------------------------------------------------------------


@dataclass
class MediaContext:
    """单个媒体条目的匹配上下文：身份 + 该条目的未满足工单与规则。"""

    item: MediaItem
    identity: MediaIdentity
    subscription: Subscription
    spec: RuleSetSpec
    open_wanted: dict[tuple[int, int], WantedItem]


@dataclass
class MatchSummary:
    """一批评估的结果统计（活动与日志用）。"""

    torrents_seen: int = 0
    identity_hits: int = 0
    rejected: int = 0
    dispatched_units: int = 0
    dispatched_torrents: list[str] = field(default_factory=list)


async def load_match_context(session: AsyncSession) -> dict[int, MediaContext]:
    """加载"有未满足工单且订阅活跃"的条目上下文：{media_item_id: MediaContext}。

    活跃订阅通常只有几十个，整体载入进程内、逐种子比对是可承受的；
    返回空 dict 表示当下没有任何缺口，调用方应快速返回。
    """
    result = await session.execute(
        select(WantedItem, Subscription)
        .join(Subscription, WantedItem.subscription_id == Subscription.id)  # type: ignore[arg-type]
        .where(
            WantedItem.status.in_(  # type: ignore[attr-defined]
                (WantedStatus.WANTED, WantedStatus.UPGRADING)
            ),
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )
    rows = result.all()
    if not rows:
        return {}

    contexts: dict[int, MediaContext] = {}
    # None = spec 解析失败的坏规则组（如版本回退后 JSON 里有当前版本不认识的
    # 枚举值）：跳过引用它的订阅并大声报错，绝不让一条脏配置拖垮整轮匹配，
    # 也不静默降级为"全不限"（那会乱抓资源）。
    specs: dict[int, RuleSetSpec | None] = {}
    for wanted, subscription in rows:
        ctx = contexts.get(wanted.media_item_id)
        if ctx is None:
            item = await session.get(MediaItem, wanted.media_item_id)
            if item is None:  # 外键保证下理论不可达
                continue
            if subscription.rule_set_id not in specs:
                rule_set = await session.get(RuleSet, subscription.rule_set_id)
                try:
                    specs[subscription.rule_set_id] = RuleSetSpec.model_validate(
                        rule_set.spec if rule_set else {}
                    )
                except ValueError:
                    specs[subscription.rule_set_id] = None
                    logger.exception(
                        "规则组「%s」(id=%s) 的过滤参数无法解析，引用它的订阅本轮"
                        "跳过匹配。常见原因：应用回退到旧版本后规则组包含新版本"
                        "字段值，请在规则组页面重新保存修正",
                        rule_set.name if rule_set else "已删除",
                        subscription.rule_set_id,
                    )
            spec = specs[subscription.rule_set_id]
            if spec is None:
                continue
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
            contexts[wanted.media_item_id] = ctx
        ctx.open_wanted[(wanted.season_number, wanted.episode_number)] = wanted

    # 回填已知季号（"无季号单集"的安全推断依赖它；从工单推导即覆盖订阅关心的季）
    for ctx in contexts.values():
        seasons = tuple(sorted({s for s, _ in ctx.open_wanted}))
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
        return [w for w in open_units.values() if _airable(w, require_dated=True)]
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


async def evaluate_and_dispatch(
    session: AsyncSession, torrents: list[SiteTorrent], *, source: str
) -> MatchSummary:
    """共享管道主入口：一批种子 × 全部活跃缺口 → 匹配/过滤/选优/投递。

    ``source`` 是可读中文（"被动匹配"/"主动搜索"），进日志与活动 payload。
    """
    # 循环导入规避：投递层引用本模块的常量
    from movieclaw_api.services.subscription.dispatch import dispatch

    summary = MatchSummary(torrents_seen=len(torrents))
    contexts = await load_match_context(session)
    if not contexts:
        return summary

    # 第一遍：逐种子评估，按条目聚合通过的候选，规则拒绝当场记活动
    accepted: dict[
        int,
        list[
            tuple[
                TorrentCandidate,
                IdentityMatch,
                RuleVerdict,
                frozenset[tuple[int, int]],
            ]
        ],
    ] = {}
    repo = SubscriptionRepository(session)
    for row in torrents:
        candidate = to_candidate(row)
        if candidate is None:
            continue
        published = (
            candidate.publish_time.date() if candidate.publish_time is not None else utcnow().date()
        )
        for media_id, ctx in contexts.items():
            match = match_identity(candidate, ctx.identity)
            if match is None:
                continue
            covered = covered_units(match, ctx.open_wanted, published=published)
            if not covered:
                continue  # 身份命中但没有可满足的缺口（都已安排），无需任何动作
            summary.identity_hits += 1
            pack_units = len(match.episodes) or len(covered)
            verdict = evaluate_rules(candidate, ctx.spec, pack_episode_count=pack_units)
            if not verdict.accepted:
                summary.rejected += 1
                await _log_rejection(repo, ctx, candidate, covered, verdict, source)
                continue
            policy = normalized_policy(ctx.subscription.quality_policy)
            locked = policy.get("locked") if policy else None
            if locked:
                locked_verdict = profile_verdict(candidate, locked)
                if not locked_verdict.accepted:
                    summary.rejected += 1
                    await _log_rejection(
                        repo, ctx, candidate, covered, locked_verdict, source
                    )
                    continue
                eligible = covered
            else:
                target = policy.get("target") if policy else None
                target_verdict = profile_verdict(candidate, target) if target else None
                eligible = [
                    wanted
                    for wanted in covered
                    if wanted.status != WantedStatus.UPGRADING
                    or (target_verdict is not None and target_verdict.accepted)
                ]
                if not eligible:
                    summary.rejected += 1
                    assert target_verdict is not None
                    await _log_rejection(
                        repo, ctx, candidate, covered, target_verdict, source
                    )
                    continue
            eligible_keys = frozenset(
                (wanted.season_number, wanted.episode_number) for wanted in eligible
            )
            accepted.setdefault(media_id, []).append(
                (candidate, match, verdict, eligible_keys)
            )

    # 第二遍：按条目选优投递。整季包优先（已确认决策）；一个候选投出后，
    # 它覆盖的单元从缺口里划掉，剩余缺口继续由次优候选补
    for media_id, entries in accepted.items():
        ctx = contexts[media_id]
        entries.sort(key=lambda e: (e[1].is_pack, e[2].score, e[0].seeders or 0), reverse=True)
        remaining = dict(ctx.open_wanted)
        for candidate, match, verdict, eligible_keys in entries:
            published = (
                candidate.publish_time.date()
                if candidate.publish_time is not None
                else utcnow().date()
            )
            targets = covered_units(match, remaining, published=published)
            targets = [
                wanted
                for wanted in targets
                if (wanted.season_number, wanted.episode_number) in eligible_keys
            ]
            if not targets:
                continue
            done = await dispatch(
                session,
                subscription=ctx.subscription,
                item=ctx.item,
                wanted_rows=targets,
                candidate=candidate,
                verdict=verdict,
                source=source,
            )
            if done:
                summary.dispatched_units += len(targets)
                summary.dispatched_torrents.append(f"{candidate.site_id}/{candidate.torrent_id}")
                for w in targets:
                    remaining.pop((w.season_number, w.episode_number), None)

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
