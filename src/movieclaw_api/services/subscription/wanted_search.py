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
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.schemas.search import TorrentHit
from movieclaw_api.services.subscription.matching import (
    SEARCH_FAILURE_RETRY,
    SEARCH_REQUESTS_PER_TICK,
    SEARCH_TICK_SECONDS,
    backoff_delay,
    evaluate_and_dispatch,
)
from movieclaw_api.services.subscription.release_forecast import refresh_release_forecasts
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    ActivityType,
    MediaItem,
    MediaMetadata,
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
from movieclaw_matcher import normalize_title
from movieclaw_media.models import MediaKind
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

def recall_keywords(item: MediaItem) -> list[str]:
    """条目 → 召回词集合（保序去重，至多三个）。

    顺序即"站点拿它命名的可能性"，三个词**全部下发、结果合并**，不再
    "第一个词有结果就不搜第二个"：

    ① **英文名**——scene/P2P 命名规范里片名段就是它，国内压制组也照办
       （``The.Gangster.the.Cop.the.Devil.2019.1080p.BluRay.x264-WiKi``）；
    ② **中文名**——国内站主标题/副标题的事实标准，国内压制组直接用它命名；
    ③ **原名**——拉丁语系（法/西/意/德）是真通道（``El.laberinto.del.fauno``
       与英文名 ``Pan's Labyrinth`` 是两批不同的发布），日文是弱通道（动画/
       日剧副标题常写原文），韩文/泰文/西里尔几乎为零。排末位但不剪掉——
       按语种剪是拿脆弱的启发式换一次请求，前两个词落地后它只是兜底。

    **原名为什么不再是第一顺位**：设计初稿写的是"种子多为英文命名"，想要的
    一直是英文名，拿到的却是 TMDB 的「原始语言标题」——两者只在影片原语言
    就是英语时才重合。韩语片给的是「악인전」，恰恰是中文 PT 站最不可能用的
    那个写法（真实教训：原名召回 4 条且全被规则拒，中文名召回 70 条正片，
    而那 70 条从未进入过候选池）。

    去重按匹配内核的归一化形式比对（大小写/分隔符的差异不值得多打一次站点），
    但**下发原样文本**：归一化是匹配的职责，站点搜索吃的是原文。
    """
    picked: list[str] = []
    seen: set[str] = set()
    for raw in (item.english_title, item.title, item.original_title):
        text = (raw or "").strip()
        if not text:
            continue
        key = normalize_title(text)
        if not key or key in seen:
            continue
        seen.add(key)
        picked.append(text)
    # 全空时**返回空列表**而不是硬塞一个主标题：主标题本身就可能是空串，拿它
    # 去搜等于向站点发一次空关键词查询（多数站点会回整个索引）。调用方据此跳过
    return picked


def _keyword_text(per_keyword: list[tuple[str, int]]) -> str:
    """逐词结果数的活动文案："关键词「A」12 条 /「B」70 条"。

    合并去重之后单看总数看不出是哪个词召回的。"为什么没搜到正片"这个问题
    只有逐词结果数能回答，它是本模块最该留给用户的一条线索。
    """
    return "关键词" + " / ".join(f"「{word}」{count} 条" for word, count in per_keyword)


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
    """tick 任务体：取到期条目组，逐组搜索→评估→记账，用满本轮搜索预算为止。

    触发有两处：定时任务（兜底节奏）与订阅创建/调整后的即时一脚
    （BackgroundTasks，不阻塞接口）。两处共用本函数，节流闸门一致。

    预算按**搜索次数**算（``SEARCH_REQUESTS_PER_TICK``）：一个条目组下发几个
    召回词由它的标题决定，按组计数会让召回词的增加悄悄放大站点压力。组不中途
    截断——合并去重要求一轮把该组的词全部下发完。
    """
    async with _tick_lock:
        db = get_database()
        async with db.session() as session:
            media_ids = await _due_media_groups(session)
        if not media_ids:
            return
        logger.info(
            "本轮缺口搜索：%d 个条目组候选，预算 %d 次搜索",
            len(media_ids),
            SEARCH_REQUESTS_PER_TICK,
        )
        budget = _SearchBudget(SEARCH_REQUESTS_PER_TICK)
        for media_id in media_ids:
            if budget.exhausted:
                logger.info("本轮搜索预算已用满，其余条目组下轮再来")
                break
            try:
                await _search_one_media(media_id, budget)
            except Exception:  # noqa: BLE001 -- 单组失败不拖垮整轮
                logger.exception("条目 #%s 的缺口搜索执行失败", media_id)


@dataclass
class _SearchBudget:
    """一轮 tick 的搜索次数预算。

    **逐次立即记账**，不是等一组跑完再结算：一个条目组在搜完之后的评估/落库/
    记账环节抛异常时，已经打出去的请求必须照样计数。按返回值结算的话，那一组
    等于免费——本 tick 反而会比正常情况打出更多请求，而这正是这个阀门要防的事
    （旧的"每 tick 两个条目组"是硬上限，异常与否都拦得住，换成按次计量后这条
    保证得自己补上）。
    """

    remaining: int

    def charge(self) -> None:
        self.remaining -= 1

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


async def _due_media_groups(session: AsyncSession) -> list[int]:
    """到期工单按 (priority, next_search_at) 排序后取前 N 个不同条目。

    N 取搜索预算：每组至少下发一个召回词，所以一轮最多只可能开这么多组，
    多取的行在本轮一定用不上（真正的停止判据在 ``search_wanted`` 的预算记账）。

    洗版单元（imported 且已排期）与缺口同队列：priority=-10 保证永远排在
    补旧(0)/追新(10)后面，洗版搜索绝不挤占缺口配额（quality-upgrade.md §6.4）。
    """
    from sqlalchemy import and_, or_

    now = utcnow()
    result = await session.execute(
        select(WantedItem.media_item_id)
        .join(Subscription, WantedItem.subscription_id == Subscription.id)  # type: ignore[arg-type]
        .where(
            or_(
                WantedItem.status == WantedStatus.WANTED,  # type: ignore[arg-type]
                and_(
                    WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                    WantedItem.quality.isnot(None),  # type: ignore[union-attr]
                ),
            ),
            WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            WantedItem.next_search_at.isnot(None),  # type: ignore[union-attr]
            WantedItem.next_search_at <= now,  # type: ignore[operator]
            # != PAUSED 而非 == ACTIVE：洗版单元挂在已收齐（completed）的
            # 订阅上；缺口行的订阅必然 active，对它语义不变
            Subscription.status != SubscriptionStatus.PAUSED,  # type: ignore[arg-type]
        )
        .order_by(WantedItem.priority.desc(), WantedItem.next_search_at)  # type: ignore[attr-defined]
    )
    ordered: list[int] = []
    for (media_id,) in result.all():
        if media_id not in ordered:
            ordered.append(media_id)
        if len(ordered) >= SEARCH_REQUESTS_PER_TICK:
            break
    return ordered


async def _search_one_media(media_id: int, budget: _SearchBudget) -> None:
    """一个条目组的完整搜索回合：搜索 → 落库 → 评估投递 → 退避记账 → 活动。

    ``budget`` 在每次下发关键词前扣减：请求已经发出去了就得认账，哪怕后面的
    环节抛异常（见 ``_SearchBudget``）。组内**不检查**预算——合并去重要求一轮
    把该组的词全部下发完，是否开工由调用方在进组前判定。
    """
    from movieclaw_api.services.site_search import search_all_sites

    db = get_database()
    async with db.session() as session:
        item = await session.get(MediaItem, media_id)
        subscription = (
            await session.execute(
                select(Subscription).where(Subscription.media_item_id == media_id)
            )
        ).scalar_one_or_none()
        # 电影的调度地板：搜索未果后的退避不能早于"上映 + 宽限"——用户强制
        # 搜索一部未上映的电影后，档期要恢复原定节奏，而不是被 15 分钟起步的
        # 退避曲线打乱、在明知没资源的窗口里反复空搜。
        movie_plan: tuple | None = None
        if item is not None and item.kind == MediaKind.MOVIE.value:
            from movieclaw_api.services.subscription.core import movie_schedule

            release_date = (
                await session.execute(
                    select(MediaMetadata.release_date).where(
                        MediaMetadata.media_item_id == media_id
                    )
                )
            ).scalar_one_or_none()
            movie_plan = movie_schedule(release_date, item.status)
    if item is None or subscription is None:
        return

    keywords = recall_keywords(item)
    if not keywords:
        # 三个标题字段全空或全是符号（脏数据）。空关键词打到站点上等于"搜全站"，
        # 会把整个索引灌进 site_torrent，比不搜坏得多，所以一次请求都不发。
        # 但**必须照样顺延**：不postpone 的话这个条目组永远停在"已到期"，每个
        # tick 都被优先挑中又原地跳过，取单的名额被它长期占住，够几个就能把其他
        # 订阅饿死。口径与"搜索本身失败"一致：短冷却重试、不计退避档
        logger.warning("条目 #%s《%s》没有可用的召回词，跳过本轮搜索", media_id, item.title)
        async with db.session() as session:
            from movieclaw_api.services.subscription.upgrade import postpone_upgrade_wanted

            await _postpone_open_wanted(
                session, media_id, delay=SEARCH_FAILURE_RETRY, count_attempt=False
            )
            await postpone_upgrade_wanted(
                session, media_id, delay=SEARCH_FAILURE_RETRY, count_attempt=False
            )
        return

    hits_by_key: dict[tuple[str, str], TorrentHit] = {}
    per_keyword: list[tuple[str, int]] = []
    site_errors: list[str] = []
    sites_ok = 0
    categories = _SEARCH_CATEGORIES.get(item.kind)
    for keyword in keywords:
        budget.charge()  # 请求发出前先记账：中途抛异常也不让预算回血
        # exclude_protected：受保护站点不参与订阅链路的自动拉种（保护开关语义）
        response = await search_all_sites(
            keyword, categories=categories, exclude_protected=True
        )
        # sites_ok 取各词的**最大值**而非末词的值：末词恰好全站超时，不该把
        # 前面几个词的有效结果一起判成"搜索本身失败"（与换源搜索同口径，
        # 见 replacement.py 的跨站搜索）
        sites_ok = max(sites_ok, sum(1 for s in response.sites if s.error is None))
        site_errors.extend(f"{s.site_name}：{s.error}" for s in response.sites if s.error)
        # 按 (站点, 种子ID) 合并去重：同一个种子被多个召回词搜到只评估一次
        for hit in response.items:
            hits_by_key[(hit.site_id, hit.torrent_id)] = hit
        per_keyword.append((keyword, len(response.items)))
    hits = list(hits_by_key.values())

    async with db.session() as session:
        repo = SubscriptionRepository(session)
        assert subscription.id is not None

        from movieclaw_api.services.subscription.upgrade import postpone_upgrade_wanted

        if sites_ok == 0:
            # 搜索本身失败（无可用站点/全站报错）：短冷却重试，不计入退避档
            # site_errors 跨关键词会重复（同一个站点每个词都报一次），保序去重
            deduped = list(dict.fromkeys(site_errors))
            reason = "；".join(deduped) if deduped else "当前没有可用的已验证站点"
            await _postpone_open_wanted(
                session, media_id, delay=SEARCH_FAILURE_RETRY, count_attempt=False
            )
            await postpone_upgrade_wanted(
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
                    payload={"keywords": keywords, "failed": True},
                )
            )
            return

        # 结果沉淀进公共缓存（source=SEARCH），再回读 ORM 行进共享管道
        persisted = await _persist_hits(session, hits)
        summary = await evaluate_and_dispatch(session, persisted, source="主动搜索")
        # 搜索回填的历史单集也属于有效发布时间观测，可帮助同剧后续集从 E2
        # 开始形成预测；限定当前条目，避免一次主动搜索触发无关全量重算。
        await refresh_release_forecasts(session, media_item_ids={media_id})

        # 仍未满足的到期工单：计一次尝试并按退避曲线排下次
        # （洗版单元走独立的 7d/14d/30d 慢退避，见 upgrade.py）
        postponed = await _postpone_open_wanted(
            session, media_id, delay=None, count_attempt=True, movie_plan=movie_plan
        )
        await postpone_upgrade_wanted(session, media_id, delay=None, count_attempt=True)

        await repo.add_activity(
            SubscriptionActivity(
                subscription_id=subscription.id,
                type=ActivityType.SEARCHED,
                message=(
                    f"搜索《{item.title}》（{_keyword_text(per_keyword)}）："
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
                    "keywords": [
                        {"keyword": word, "results": count} for word, count in per_keyword
                    ],
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
    session: AsyncSession,
    media_id: int,
    *,
    delay,
    count_attempt: bool,
    movie_plan: tuple | None = None,
) -> int:
    """给该条目下仍到期未满足的工单排下一次搜索。

    - ``delay`` 给定：统一顺延该间隔（搜索失败场景，不计尝试次数）；
    - ``delay=None``：按各自的 search_attempts 走退避曲线，并 +1 尝试；
    - ``movie_plan``：电影的上映感知调度（``movie_schedule`` 的结果），作为
      退避的地板——下次搜索不早于"上映 + 宽限"；影片未定档时直接回到不可
      调度，等元数据刷新定档回填。两种情况都对应"用户强制搜索了一部还没到
      档期的电影"，搜完恢复原定节奏。
    返回被顺延的工单数。
    """
    now = utcnow()
    result = await session.execute(
        select(WantedItem).where(
            WantedItem.media_item_id == media_id,
            WantedItem.status == WantedStatus.WANTED,
            WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            WantedItem.next_search_at.isnot(None),  # type: ignore[union-attr]
            WantedItem.next_search_at <= now,  # type: ignore[operator]
        )
    )
    rows = list(result.scalars().all())
    postponed = 0
    for wanted in rows:
        if count_attempt:
            next_at = now + backoff_delay(wanted.search_attempts)
            if movie_plan is not None:
                plan_at, _priority = movie_plan
                if plan_at is None:
                    next_at = None  # 未定档：回到等待定档，不再空搜
                elif plan_at > next_at:
                    next_at = plan_at  # 上映宽限未到：退避不早于原定档期
            values = {
                "next_search_at": next_at,
                "search_attempts": wanted.search_attempts + 1,
                "last_search_at": now,
                "updated_at": now,
            }
        else:
            values = {"next_search_at": now + delay, "updated_at": now}
        updated = await session.execute(
            update(WantedItem)
            .where(
                WantedItem.id == wanted.id,
                WantedItem.status == WantedStatus.WANTED,
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            )
            .values(**values)
        )
        postponed += int(updated.rowcount or 0)
    await session.commit()
    return postponed
