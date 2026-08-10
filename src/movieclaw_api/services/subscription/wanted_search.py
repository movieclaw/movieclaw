"""主动搜索 worker（F4）：补旧专用 + 追新漏抓兜底。

铁律的另一半在这里落地：补旧工单的候选集只来自真实站点搜索（缓存对旧内容
覆盖不完整）。搜索结果经 TorrentRepository 落库（source=SEARCH）——主动搜索
的副产品沉淀进公共缓存，全局受益。

节流三闸门：tick 间隔 × 每 tick 条目组数 × 指数退避（常量见
subscription_matching，需真实站点试跑校准）。**按条目分组**是关键：同一部剧
的几十个缺集合并为一次跨站搜索，绝不逐集打请求。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.schemas.search import TorrentHit
from movieclaw_api.services.subscription.matching import (
    SEARCH_FAILURE_RETRY,
    SEARCH_GROUPS_PER_TICK,
    SEARCH_TICK_SECONDS,
    backoff_delay,
    evaluate_and_dispatch,
)
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    ActivityType,
    MediaItem,
    SiteTorrent,
    Subscription,
    SubscriptionActivity,
    SubscriptionStatus,
    TorrentSource,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_db.repositories import (
    SubscriptionRepository,
    TorrentObservation,
    TorrentRepository,
)
from movieclaw_enrich import ENRICH_VERSION
from movieclaw_scheduler.registry import register_task
from movieclaw_tracker.models import TorrentCategory

logger = logging.getLogger("movieclaw_api.wanted_search")

# 按订阅类型收窄搜索分类（站点侧过滤，减少噪音、提高召回质量）。
# 哲学与被动匹配粗筛一致：只排除"明确不可能"的分类——纪录片与动漫不排除，
# 因为纪录片电影/动画剧场版/动画剧集在多数站点归入这两类而非电影/剧集类。
_SEARCH_CATEGORIES: dict[str, list[TorrentCategory]] = {
    "movie": [TorrentCategory.MOVIE, TorrentCategory.DOCUMENTARY, TorrentCategory.ANIME],
    "tv": [TorrentCategory.TV, TorrentCategory.DOCUMENTARY, TorrentCategory.ANIME],
}

# tick 互斥：除定时任务外，订阅域写操作产生"立刻可搜"的工单后也会立即踢一次
# tick（首班车不用等最多 5 分钟）。并发进入时串行执行即可——前一轮已把搜过的
# 组按退避排期，后一轮查不到到期项自然空转，不会对站点重复搜索。
_tick_lock = asyncio.Lock()

# fire-and-forget 任务的强引用集合：asyncio 只持弱引用，不留强引用的话
# 任务可能在执行前被垃圾回收。
_kick_tasks: set[asyncio.Task] = set()


async def _kick_once() -> None:
    """即时 tick 的执行体：失败只记日志，绝不向上抛（兜底永远是定时任务）。"""
    try:
        await search_wanted()
    except Exception:  # noqa: BLE001 -- 环境未就绪（如测试/关停中）时静默降级
        logger.debug("即时缺口搜索未能执行，等待定时任务兜底", exc_info=True)


def kick_search_soon() -> None:
    """立刻踢一次缺口搜索（fire-and-forget，任何订阅入口共用的唯一触发点）。

    订阅域的写操作（创建/调整/恢复/缺失重下）产生"立刻可搜"的工单后调用。
    仓储层逐操作即时 commit，调用时数据已落库，不存在"未提交就开搜"的竞态；
    search_wanted 自带互斥锁与节流闸门，重复踢只会空转，天然幂等。
    没有运行中的事件循环时（如同步脚本）静默跳过，交给定时任务兜底。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_kick_once())
    _kick_tasks.add(task)
    task.add_done_callback(_kick_tasks.discard)


@register_task(
    "search_wanted",
    title="订阅缺口搜索",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=SEARCH_TICK_SECONDS,
    description=(
        "从订阅缺口队列取到期项，按媒体条目分组做跨站搜索（补旧专用；追新工单"
        "由被动匹配为主，到期才进入本队列兜底）。搜索结果沉淀进种子索引。"
    ),
)
async def search_wanted() -> None:
    """tick 任务体：取前 N 个到期条目组，逐组搜索→评估→记账。

    触发有两处：定时任务（兜底节奏）与订阅创建/调整后的即时一脚
    （BackgroundTasks，不阻塞接口）。两处共用本函数，节流闸门一致。
    """
    async with _tick_lock:
        db = get_database()
        async with db.session() as session:
            media_ids = await _due_media_groups(session)
        if not media_ids:
            return
        logger.info("本轮缺口搜索：%d 个条目组到期", len(media_ids))
        for media_id in media_ids:
            try:
                await _search_one_media(media_id)
            except Exception:  # noqa: BLE001 -- 单组失败不拖垮整轮
                logger.exception("条目 #%s 的缺口搜索执行失败", media_id)


async def _due_media_groups(session: AsyncSession) -> list[int]:
    """到期工单按 (priority, next_search_at) 排序后取前 N 个不同条目。"""
    now = utcnow()
    result = await session.execute(
        select(WantedItem.media_item_id)
        .join(Subscription, WantedItem.subscription_id == Subscription.id)  # type: ignore[arg-type]
        .where(
            WantedItem.status.in_(  # type: ignore[attr-defined]
                (WantedStatus.WANTED, WantedStatus.UPGRADING)
            ),
            WantedItem.next_search_at.isnot(None),  # type: ignore[union-attr]
            WantedItem.next_search_at <= now,  # type: ignore[operator]
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
        .order_by(WantedItem.priority.desc(), WantedItem.next_search_at)  # type: ignore[attr-defined]
    )
    ordered: list[int] = []
    for (media_id,) in result.all():
        if media_id not in ordered:
            ordered.append(media_id)
        if len(ordered) >= SEARCH_GROUPS_PER_TICK:
            break
    return ordered


async def _search_one_media(media_id: int) -> None:
    """一个条目组的完整搜索回合：搜索 → 落库 → 评估投递 → 退避记账 → 活动。"""
    from movieclaw_api.services.site_search import search_all_sites

    db = get_database()
    async with db.session() as session:
        item = await session.get(MediaItem, media_id)
        subscription = (
            await session.execute(
                select(Subscription).where(Subscription.media_item_id == media_id)
            )
        ).scalar_one_or_none()
    if item is None or subscription is None:
        return

    # 搜索词首选原名（种子多为英文命名），零结果补一次主标题
    keywords = [item.original_title]
    if item.title and item.title != item.original_title:
        keywords.append(item.title)

    hits: list[TorrentHit] = []
    site_errors: list[str] = []
    sites_ok = 0
    searched_keyword = keywords[0]
    categories = _SEARCH_CATEGORIES.get(item.kind)
    for keyword in keywords:
        searched_keyword = keyword
        response = await search_all_sites(keyword, categories=categories)
        sites_ok = sum(1 for s in response.sites if s.error is None)
        site_errors = [f"{s.site_name}：{s.error}" for s in response.sites if s.error]
        hits = response.items
        if hits:
            break

    async with db.session() as session:
        repo = SubscriptionRepository(session)
        assert subscription.id is not None

        if sites_ok == 0:
            # 搜索本身失败（无可用站点/全站报错）：短冷却重试，不计入退避档
            reason = "；".join(site_errors) if site_errors else "当前没有可用的已验证站点"
            await _postpone_open_wanted(
                session, media_id, delay=SEARCH_FAILURE_RETRY, count_attempt=False
            )
            await repo.add_activity(
                SubscriptionActivity(
                    subscription_id=subscription.id,
                    type=ActivityType.SEARCHED,
                    message=(
                        f"搜索《{item.title}》未能执行：{reason}；"
                        f"约 {int(SEARCH_FAILURE_RETRY.total_seconds() // 60)} 分钟后重试"
                    ),
                    payload={"keyword": searched_keyword, "failed": True},
                )
            )
            return

        # 结果沉淀进公共缓存（source=SEARCH），再回读 ORM 行进共享管道
        persisted = await _persist_hits(session, hits)
        summary = await evaluate_and_dispatch(session, persisted, source="主动搜索")

        # 仍未满足的到期工单：计一次尝试并按退避曲线排下次
        postponed = await _postpone_open_wanted(session, media_id, delay=None, count_attempt=True)

        await repo.add_activity(
            SubscriptionActivity(
                subscription_id=subscription.id,
                type=ActivityType.SEARCHED,
                message=(
                    f"搜索《{item.title}》（关键词「{searched_keyword}」）："
                    f"{sites_ok} 个站点返回 {len(hits)} 个结果，"
                    f"身份命中 {summary.identity_hits}，规则拒绝 {summary.rejected}，"
                    f"投递覆盖 {summary.dispatched_units} 个单元"
                    + (
                        f"；剩余 {postponed} 个缺口按退避曲线排期"
                        if postponed
                        else "；本组缺口已全部安排"
                    )
                ),
                payload={
                    "keyword": searched_keyword,
                    "sites_ok": sites_ok,
                    "results": len(hits),
                    "identity_hits": summary.identity_hits,
                    "rejected": summary.rejected,
                    "dispatched_units": summary.dispatched_units,
                },
            )
        )


async def _persist_hits(session: AsyncSession, hits: list[TorrentHit]) -> list[SiteTorrent]:
    """搜索结果 upsert 进 site_torrent（source=SEARCH），回读 ORM 行供管道消费。"""
    observations: list[TorrentObservation] = []
    for hit in hits:
        try:
            observations.append(
                TorrentObservation(
                    site_id=hit.site_id,
                    torrent_id=hit.torrent_id,
                    source=TorrentSource.SEARCH,
                    title=hit.title,
                    subtitle=hit.subtitle,
                    category=hit.category.value if hit.category else None,
                    site_category_id=hit.site_category_id,
                    size_bytes=hit.size_bytes or None,
                    size_text=hit.size,
                    publish_time=hit.upload_time,
                    uploader=hit.uploader,
                    seeders=hit.seeders,
                    leechers=hit.leechers,
                    snatched=hit.snatched,
                    download_volume_factor=hit.download_volume_factor,
                    upload_volume_factor=hit.upload_volume_factor,
                    free_deadline=hit.free_deadline,
                    hit_and_run=hit.hit_and_run,
                    attrs=(
                        hit.attrs.model_dump(exclude_defaults=True)
                        if hit.attrs is not None
                        else None
                    ),
                    enrich_version=ENRICH_VERSION if hit.attrs is not None else None,
                    detail_url=hit.detail_url,
                    download_url=hit.download_url,
                )
            )
        except ValueError:
            continue  # 脏观测（如空标题）直接跳过
    if not observations:
        return []
    await TorrentRepository(session).bulk_upsert(observations)

    rows: list[SiteTorrent] = []
    for obs in observations:
        row = (
            await session.execute(
                select(SiteTorrent).where(
                    SiteTorrent.site_id == obs.site_id,
                    SiteTorrent.torrent_id == obs.torrent_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            rows.append(row)
    return rows


async def _postpone_open_wanted(
    session: AsyncSession, media_id: int, *, delay, count_attempt: bool
) -> int:
    """给该条目下仍到期未满足的工单排下一次搜索。

    - ``delay`` 给定：统一顺延该间隔（搜索失败场景，不计尝试次数）；
    - ``delay=None``：按各自的 search_attempts 走退避曲线，并 +1 尝试。
    返回被顺延的工单数。
    """
    now = utcnow()
    result = await session.execute(
        select(WantedItem).where(
            WantedItem.media_item_id == media_id,
            WantedItem.status.in_(  # type: ignore[attr-defined]
                (WantedStatus.WANTED, WantedStatus.UPGRADING)
            ),
            WantedItem.next_search_at.isnot(None),  # type: ignore[union-attr]
            WantedItem.next_search_at <= now,  # type: ignore[operator]
        )
    )
    rows = list(result.scalars().all())
    for wanted in rows:
        if count_attempt:
            wanted.next_search_at = now + backoff_delay(wanted.search_attempts)
            wanted.search_attempts += 1
            wanted.last_search_at = now
        else:
            wanted.next_search_at = now + delay
        wanted.updated_at = now
        session.add(wanted)
    await session.commit()
    return len(rows)
