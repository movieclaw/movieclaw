"""订阅服务：期望集合 E 的定义、物化与调和入口（docs/design/subscription.md 第 2 节）。

核心算法只有两个，全部围绕 E：

- **初始化**：``_expected_units`` 按订阅参数展开期望单元，``_schedule_for`` 给每个
  单元写死调度语义（补旧=now / 追新=air_date+宽限 / 未定档=NULL）；电影另有
  ``movie_schedule`` 的上映感知调度——未上映/刚上映不白搜，等宽限期到点兜底；
- **diff 重算**：修改订阅时新增缺的、切换既有工单的 ``in_scope``；状态与投递
  历史不回退，退出范围的单元停止搜索、观察与救援。

订阅状态是派生值（不变量④）：``_recompute_status`` 随时可从工单集合重算，
paused 是用户显式操作、重算不碰它。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import median
from types import EllipsisType

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.rule_sets import RuleSetService
from movieclaw_api.services.subscription.matching import publish_calendar_date
from movieclaw_api.services.subscription.release_forecast import (
    next_forecast_probe_times_by_wanted,
    refresh_release_forecasts,
)
from movieclaw_api.services.system_notice import resolve_notices
from movieclaw_db.models import (
    ActivityType,
    DownloadAttemptStatus,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    MediaSeason,
    SiteTorrent,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    SubscriptionStatus,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.models.library import Library
from movieclaw_db.repositories import (
    LibraryRepository,
    MediaItemRepository,
    SubscriptionRepository,
)
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.subscription")

# 追新工单的漏抓宽限期：air_date + 该值 = 首个真实搜索到期时刻。
# 被动匹配通常在到点前满足工单；真到点仍缺的，worker 捞起即漏抓兜底。
FUTURE_GRACE = timedelta(hours=48)

# 电影的上映宽限期：上映日 + 该值 = 首次真实搜索时刻（movie_schedule）。
# TMDB 的电影档期通常是最早的院线上映日，院线窗口内几乎不可能有正规资源，
# 上来就搜只会空手而归；流媒体首发的影片虽然上映即有资源，但那类资源以新种
# 进入站点 RSS，被动匹配会第一时间接住，不依赖主动搜索。7 天是"未上映/刚上映
# 不白搜"与"别让搜索才能找到的既有资源干等太久"之间的折中——到期后若仍
# 无资源，退避曲线很快收敛到每周一次。等不及的用户随时可以「立即搜索」强制。
MOVIE_RELEASE_GRACE = timedelta(days=7)

# 首页“预计入库”在缺少本剧历史样本时使用的冷启动值。出现真实入库记录后，
# 会自动切换成该订阅自己的中位耗时，不把这个展示层兜底写进预测快照。
_DEFAULT_RELEASE_TO_IMPORT_MINUTES = 60
_DEFAULT_DOWNLOAD_TO_IMPORT_MINUTES = 10

# 首页预告的前瞻窗口：订阅少的用户今天往往什么都不更新，只看今天会得到一个
# 没有信息量的空栏目。窗口放宽到一周，配合“今天优先”收敛（见 today_arrivals），
# 保证首页永远能回答“下一次是什么时候”。
# 改这个值时记得同步 Web 空态文案里的“接下来 7 天”（subscriptions-view.tsx）。
_UPCOMING_WINDOW_DAYS = 7

# 剧集完结类 status：期望集合不再生长的判定输入之一
_ENDED_STATUSES = frozenset({"Ended", "Canceled"})


def _payload_time(value: object) -> datetime | None:
    """解析活动 payload 中冻结的 UTC 时间；老活动或坏数据安全返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class ExpectedUnit:
    """期望单元：E 集合的元素。电影是 (0, 0) 哨兵单元。"""

    season_number: int
    episode_number: int
    air_date: date | None


@dataclass(frozen=True)
class TodayArrivalCandidate:
    """订阅首页的一行待入库内容，以及由本订阅历史得出的耗时基线。"""

    subscription: Subscription
    media: MediaItem
    wanted: WantedItem
    next_probe_at: datetime | None
    release_to_import_minutes: int
    download_to_import_minutes: int
    #: 该行预计入库/播出的站点日历日；today_arrivals 收敛后同一批候选共享同一天
    expected_day: date
    #: 距今天几天（0=今天）。站点日历与浏览器时区可能不同，判断“是不是今天”
    #: 由服务端一次算好，前端不复制这套时区规则。
    days_ahead: int


def _expected_arrival_day(candidates: tuple[date | None, ...], today: date) -> date | None:
    """从若干候选日历日里取今天或之后最早的一个；全部落在过去时返回 None。

    早该播出却迟迟没抓到的旧集不算“待播预告”——它属于订阅详情页的缺口清单，
    首页硬塞进来只会把过期日期当成未来安排展示。
    """
    upcoming = [day for day in candidates if day is not None and day >= today]
    return min(upcoming) if upcoming else None


def _focus_day_rows(
    rows: list[TodayArrivalCandidate], today: date
) -> list[TodayArrivalCandidate]:
    """今天优先：今天有安排就只留今天，否则只留最近一个有安排的日子。"""
    focus = (
        today
        if any(row.expected_day == today for row in rows)
        else min((row.expected_day for row in rows), default=None)
    )
    return [row for row in rows if row.expected_day == focus]


def _median_pipeline_minutes(
    rows: list[WantedItem],
    *,
    start_field: str,
    end_field: str,
    fallback: int,
    maximum: timedelta,
) -> int:
    """从已完成工单取中位耗时；异常长链路不参与首页的日内时间估算。"""
    durations: list[float] = []
    for row in rows:
        start = getattr(row, start_field)
        end = getattr(row, end_field)
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            continue
        elapsed = end - start
        if timedelta(0) < elapsed <= maximum:
            durations.append(elapsed.total_seconds() / 60)
    return max(1, round(median(durations))) if durations else fallback


def _forecast_predicted_at(wanted: WantedItem) -> datetime | None:
    forecast = wanted.release_forecast
    if not isinstance(forecast, dict):
        return None
    return _payload_time(forecast.get("predicted_at"))


def expected_units(
    kind: MediaKind,
    episodes: list[MediaEpisode],
    selected: list[int],
    follow_future: bool,
) -> list[ExpectedUnit]:
    """按 E 的定义展开期望单元（订阅创建/修改与元数据刷新共用）。

    E = 勾选季的全部已知集 ∪（follow_future ? 评估时刻之后播出的集 : ∅）

    集数据来自 ``media_episode`` 表（集数据唯一事实源，metadata.md 第 1 节）。
    追新的锚定取**评估时刻**而非订阅创建时刻：创建时二者等价；后来才打开
    自动续订开关时，不回补开关关闭期间播出的集（用户此刻的意图是"从现在起追"）。
    更早的历史集通过勾选季表达。特别季 0 仅显式勾选才纳入。
    """
    if kind is MediaKind.MOVIE:
        return [ExpectedUnit(0, 0, None)]

    today = utcnow().date()
    selected_set = set(selected)
    units: list[ExpectedUnit] = []
    for episode in episodes:
        in_selected = episode.season_number in selected_set
        if not in_selected and not follow_future:
            continue
        air = episode.air_date
        if in_selected:
            units.append(ExpectedUnit(episode.season_number, episode.episode_number, air))
        elif episode.season_number != 0 and (air is None or air > today):
            # 追新贡献：未勾季里"尚未播出/未定档"的集；特别季不自动追
            units.append(ExpectedUnit(episode.season_number, episode.episode_number, air))
    return units


def schedule_for(kind: str, unit: ExpectedUnit) -> tuple[datetime | None, int]:
    """三类调度语义（创建时写死）：补旧=now / 追新=air+宽限 / 未定档=NULL。

    电影在这里恒为补旧——本函数没有上映档期上下文，只服务"明确要资源"的
    场景（如媒体库缺失重下）。订阅创建/调整会用 ``movie_schedule`` 的上映感知
    结果覆盖它。追新工单给高优先级：新集是用户最急着要的，worker 排序时
    优先处理。
    """
    now = utcnow()
    if kind == MediaKind.MOVIE.value:
        return now, 0
    if unit.air_date is None:
        return None, 0  # 未定档：不可调度，元数据刷新定档时回填
    if unit.air_date <= now.date():
        return now, 0  # 补旧：立即排队真实搜索
    first_due = datetime.combine(unit.air_date, datetime.min.time()) + FUTURE_GRACE
    return first_due, 10  # 追新：被动匹配为主，到点即漏抓兜底


def movie_schedule(release_date: date | None, status: str | None) -> tuple[datetime | None, int]:
    """电影的上映感知调度：未上映/刚上映不白搜，期间资源到来靠被动匹配兜住。

    - 已定档：``上映日 + MOVIE_RELEASE_GRACE`` 之前不安排真实搜索（未来到期给
      高优先级，与剧集追新同语义）；宽限已过按补旧立即排队；
    - 未定档：TMDB status 明确还没上映（In Production / Post Production 等）时
      不可调度，等元数据刷新定档/上映后回填；status 未知或已上映的老片没有
      档期可依，保持"订阅即搜"——宁可白搜一次，不能让老片瘫在不可调度里。

    工单的 ``air_date`` 不写上映日：``covered_units`` 把 air_date 当作"发布时间
    物理上限"剔除候选，而电影资源早于 TMDB 档期流出真实存在（分地区上映、
    提前数字发行），写了会把这类资源永久拒之门外。调度信息只落 next_search_at。
    """
    now = utcnow()
    if release_date is not None:
        available_at = datetime.combine(release_date, datetime.min.time()) + MOVIE_RELEASE_GRACE
        if available_at <= now:
            return now, 0  # 上映已过宽限：补旧，立即排队真实搜索
        return available_at, 10  # 未上映/刚上映：被动匹配为主，到点漏抓兜底
    if status is None or status == "Released":
        return now, 0  # 无档期信息的老片/未知片：维持订阅即搜
    return None, 0  # 明确未上映且未定档：等定档回填


async def recompute_subscription_status(
    session: AsyncSession, subscription: Subscription, item: MediaItem
) -> None:
    """派生状态重算（不变量④）：completed ⟺ 无未满足工单 且 E 不再生长。

    paused 是用户显式状态，不碰。状态翻转本身也是活动（透明化）。

    "满足"的判定（媒体库 L2 收紧）：imported 是唯一硬满足；真实投递的
    grabbed/downloaded 带 info_hash，说明入库管线还在进行 → 仍视为未满足；
    模拟投递（dry-run）的 grabbed 没有 info_hash、不存在后续管线，维持
    P4 语义视为满足——切真实投递后无需任何开关，判定自然收紧。
    模块级函数：订阅服务与投递/刷新管线共用，不依赖 TMDB client。
    """
    if subscription.status == SubscriptionStatus.PAUSED:
        return
    assert subscription.id is not None
    repo = SubscriptionRepository(session)
    wanted = await repo.list_wanted(subscription.id, in_scope_only=True)

    def _is_open(w: WantedItem) -> bool:
        if w.status == WantedStatus.WANTED:
            return True
        in_pipeline = w.status in (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)
        return in_pipeline and w.info_hash is not None

    has_open = any(_is_open(w) for w in wanted)
    growing = (
        subscription.kind == MediaKind.TV.value
        and subscription.follow_future
        and (item.status or "") not in _ENDED_STATUSES
    )
    new_status = (
        SubscriptionStatus.COMPLETED if not has_open and not growing else SubscriptionStatus.ACTIVE
    )
    if subscription.status == new_status:
        return
    subscription.status = new_status
    await repo.save(subscription)
    if new_status == SubscriptionStatus.COMPLETED:
        message = "订阅已收齐：期望的内容都已安排完毕，且暂无会新增的内容"
        activity_type = ActivityType.COMPLETED
    else:
        message = "出现新的缺口，重新进入追踪"
        activity_type = ActivityType.REOPENED
    await repo.add_activity(
        SubscriptionActivity(subscription_id=subscription.id, type=activity_type, message=message)
    )


class SubscriptionService:
    """订阅的全生命周期编排。持久化走仓储，TMDB 建档走 MediaLibraryService。"""

    def __init__(self, session: AsyncSession, media_library: MediaLibraryService) -> None:
        self._session = session
        self._repo = SubscriptionRepository(session)
        self._media_repo = MediaItemRepository(session)
        self._library = media_library
        self._rule_sets = RuleSetService(session)

    # ------------------------------------------------------------------
    # prepare：订阅弹层的数据来源（建档 + 季集概览 + 已订检查）
    # ------------------------------------------------------------------

    async def prepare(
        self,
        kind: MediaKind,
        tmdb_id: int,
        *,
        douban_id: str | None = None,
        extra_aliases: tuple[str, ...] = (),
    ) -> tuple[MediaItem, list[MediaSeason], Subscription | None]:
        """建档/复用媒体条目，返回 (条目, 季列表, 已有订阅)。幂等，弹层打开即调用。"""
        item = await self._library.ensure_media_item(
            kind, tmdb_id, douban_id=douban_id, extra_aliases=extra_aliases
        )
        assert item.id is not None
        seasons = await self._media_repo.list_seasons(item.id)
        existing = await self._repo.get_by_media_item(item.id)
        return item, seasons, existing

    # ------------------------------------------------------------------
    # 创建：定义 E 并物化
    # ------------------------------------------------------------------

    async def create(
        self,
        kind: MediaKind,
        tmdb_id: int,
        *,
        selected_seasons: list[int] | None = None,
        follow_future: bool = False,
        rule_set_id: int | None = None,
        library_id: int | None = None,
        douban_id: str | None = None,
        member_id: int | None = None,
    ) -> Subscription:
        """创建订阅并生成初始工单。同一条目已有订阅时幂等返回已有（不改参数）。

        ``member_id``：发起成员（None=超管）。已有订阅时成员的"再订"转为
        **关注**（docs/design/member-management.md §3.5）——订阅出现在他的
        列表里，期望集合 E 不变，一份下载全家共享。
        """
        item, seasons, existing = await self.prepare(kind, tmdb_id, douban_id=douban_id)
        if existing is not None:
            assert existing.id is not None
            is_new_follower = (
                member_id is not None
                and existing.created_by_member_id != member_id
                and await self._repo.add_follower(existing.id, member_id)
            )
            if is_new_follower:
                await self._log(
                    existing, ActivityType.CREATED, "有成员关注了本订阅（共享同一份下载）"
                )
            logger.info("条目《%s》已有订阅 #%s，幂等返回", item.title, existing.id)
            return existing
        assert item.id is not None

        selected = self._validate_selection(kind, selected_seasons or [], seasons)
        if kind is MediaKind.MOVIE:
            follow_future = False  # 电影没有"生长"，开关无意义，落库前归一

        if rule_set_id is None:
            rule_set_id = (await self._rule_sets.ensure_default()).id
        else:
            await self._rule_sets.get(rule_set_id)  # 不存在则抛 404
        assert rule_set_id is not None
        # 入库目标：显式指定优先；否则按收藏范围路由并**创建时定格**——粘性
        # 的实现（docs/design/library-routing.md 2.1）：之后每次投递读定格值，
        # 规则中途变更不影响既有订阅，一部剧不会裂在两个库
        route_note: str | None = None
        if library_id is not None:
            await self._validate_library(kind.value, library_id)
        else:
            from movieclaw_api.services.library.routing import route_for_item

            decision = await route_for_item(self._session, kind.value, item)
            if decision.library is not None:
                library_id = decision.library.id
                route_note = decision.reason

        # 订阅弹层打开时（prepare）用户还没选库，条目多半没有刮削归属；
        # 这里入库目标定格了，同一时刻把归属补上（设计文档 §14），
        # 之后的元数据刷新就按这个库的语言/选图口味来
        if library_id is not None:
            from movieclaw_api.services.scrape_config import assign_scrape_library

            library = await self._session.get(Library, library_id)
            if assign_scrape_library(item, library):
                self._session.add(item)

        subscription = await self._repo.save(
            Subscription(
                media_item_id=item.id,
                kind=kind.value,
                selected_seasons=selected,
                follow_future=follow_future,
                rule_set_id=rule_set_id,
                library_id=library_id,
                status=SubscriptionStatus.ACTIVE,
                created_by_member_id=member_id,
            )
        )
        assert subscription.id is not None

        episodes = await self._media_repo.list_episodes(item.id)
        units = expected_units(kind, episodes, selected, follow_future)
        # 库存联通（媒体库 L3）：库里已有的单元不生成工单——E−H 用真实的 H
        owned = await self._owned_units(item.id)
        skipped_owned = [u for u in units if (u.season_number, u.episode_number) in owned]
        units = [u for u in units if (u.season_number, u.episode_number) not in owned]
        movie_plan = await self._movie_plan(item) if kind is MediaKind.MOVIE else None
        rows = [self._to_wanted(subscription, unit, schedule=movie_plan) for unit in units]
        await self._repo.add_wanted(rows)
        created_message = self._created_message(item, kind, selected, follow_future, rows)
        if skipped_owned:
            created_message += f"；库里已有 {len(skipped_owned)} 个单元，无需重复下载"
        if route_note:
            created_message += f"；{route_note}"
        await self._log(
            subscription,
            ActivityType.CREATED,
            created_message,
            payload={
                "selected_seasons": selected,
                "follow_future": follow_future,
                "wanted_total": len(rows),
                "owned_skipped": len(skipped_owned),
                "library_id": library_id,
            },
        )
        await self._recompute_status(subscription, item)
        await refresh_release_forecasts(self._session, media_item_ids={item.id})
        logger.info(
            "已订阅《%s》(%s)：勾选季 %s，自动续订 %s，生成工单 %d 个",
            item.title,
            kind.value,
            selected or "无",
            "开" if follow_future else "关",
            len(units),
        )
        self._kick_search()
        return subscription

    # ------------------------------------------------------------------
    # 修改：E 变更 → diff 重算
    # ------------------------------------------------------------------

    async def update(
        self,
        subscription_id: int,
        *,
        selected_seasons: list[int] | None = None,
        follow_future: bool | None = None,
        rule_set_id: int | None = None,
        library_id: int | None | EllipsisType = ...,
    ) -> Subscription:
        """修改 E 的定义（季选择/自动续订/规则组/入库目标库），diff 重算工单。

        ``library_id`` 用 ``...``（Ellipsis）作「未传=不变」的哨兵：显式传 None
        表示清除指定库、改回按默认库路由——旧订阅（library_id 为空）需要这条
        双向通道。

        diff 规则（不变量③）：
        - 新入域且从未见过的单元 → 补工单；
        - 已存在的单元只切换 ``in_scope``，不回退状态、不删除投递历史；
        - 尝试没有任何入域目标时同步取消观察与换源，但不删除下载器任务。
        """
        subscription = await self._get_or_404(subscription_id)
        item = await self._media_repo_get(subscription.media_item_id)

        new_rule_set = None
        if rule_set_id is not None and rule_set_id != subscription.rule_set_id:
            new_rule_set = await self._rule_sets.get(rule_set_id)
            subscription.rule_set_id = rule_set_id
        if library_id is not ... and library_id != subscription.library_id:
            if library_id is not None:
                await self._validate_library(subscription.kind, library_id)
            subscription.library_id = library_id

        kind = MediaKind(subscription.kind)
        seasons = await self._media_repo.list_seasons(subscription.media_item_id)
        if selected_seasons is not None:
            subscription.selected_seasons = self._validate_selection(
                kind, selected_seasons, seasons
            )
        if follow_future is not None:
            subscription.follow_future = follow_future if kind is MediaKind.TV else False

        episodes = await self._media_repo.list_episodes(subscription.media_item_id)
        expected = expected_units(
            kind, episodes, list(subscription.selected_seasons), subscription.follow_future
        )
        existing = await self._repo.list_wanted(subscription_id)
        existing_keys = {(w.season_number, w.episode_number) for w in existing}
        selected_set = set(subscription.selected_seasons)

        expected_keys = {(u.season_number, u.episode_number) for u in expected}
        owned = await self._owned_units(subscription.media_item_id)
        movie_plan = await self._movie_plan(item) if kind is MediaKind.MOVIE else None
        to_add = [
            self._to_wanted(subscription, unit, schedule=movie_plan)
            for unit in expected
            if (unit.season_number, unit.episode_number) not in existing_keys
            and (unit.season_number, unit.episode_number) not in owned
        ]
        # 出域判定不能只看当前 E 快照：经追新进入的单元播出之后就不在"评估时刻
        # 的未来集"里了，但只要追新开关还开着，它们仍在域内。没有来源字段时用
        # 播出日期区分血统：air_date 晚于订阅创建（或未定档）的单元视作追新进入，
        # 开关开着即受保护；早于创建的只可能来自勾选季，季被取消即出域。
        created_date = subscription.created_at.date()

        def _protected_by_follow(w: WantedItem) -> bool:
            return (
                subscription.follow_future
                and w.season_number != 0
                and (w.air_date is None or w.air_date > created_date)
            )

        def _should_be_in_scope(w: WantedItem) -> bool:
            return (
                (w.season_number, w.episode_number) in expected_keys
                or w.season_number in selected_set
                or _protected_by_follow(w)
            )

        reactivated = 0
        deactivated = 0
        now = utcnow()
        for row in existing:
            target_scope = _should_be_in_scope(row)
            if row.in_scope == target_scope:
                continue
            row.in_scope = target_scope
            if target_scope:
                reactivated += 1
                # 退出范围期间不应消耗搜索机会；重新纳入的已播缺口立即可搜。
                if (
                    row.status == WantedStatus.WANTED
                    and row.air_date is not None
                    and row.air_date <= now.date()
                ):
                    row.search_attempts = 0
                    row.next_search_at = now
            else:
                deactivated += 1
            self._session.add(row)

        subscription.updated_at = now
        self._session.add(subscription)
        for row in to_add:
            self._session.add(row)
        await self._session.flush()
        cancelled_attempts, resumed_attempts, cancelled_hashes = (
            await self._reconcile_attempt_scope(
                subscription_id, [*existing, *to_add], now=now
            )
        )
        await self._session.commit()
        await self._session.refresh(subscription)
        for info_hash in cancelled_hashes:
            await resolve_notices(
                self._session,
                dedupe_key=f"subscription.landing:{subscription_id}:{info_hash}",
            )
        season_text = self._season_text(list(subscription.selected_seasons))
        rule_note = f"，规则组改为「{new_rule_set.name}」" if new_rule_set is not None else ""
        await self._log(
            subscription,
            ActivityType.ADJUSTED,
            f"调整订阅：勾选{season_text}，自动续订{'开' if subscription.follow_future else '关'}"
            f"{rule_note}；新增 {len(to_add)} 个单元，重新纳入 {reactivated} 个，"
            f"退出范围 {deactivated} 个"
            "（下载器任务不会自动删除；退出范围后停止搜索与换源）",
            payload={
                "selected_seasons": list(subscription.selected_seasons),
                "follow_future": subscription.follow_future,
                "rule_set_id": subscription.rule_set_id,
                "added": len(to_add),
                "reactivated": reactivated,
                "deactivated": deactivated,
                "cancelled_attempts": cancelled_attempts,
                "resumed_attempts": resumed_attempts,
            },
        )
        await self._recompute_status(subscription, item)
        await refresh_release_forecasts(
            self._session, media_item_ids={subscription.media_item_id}
        )
        logger.info(
            "订阅 #%s 已调整：新增 %d 个，重新纳入 %d 个，退出范围 %d 个，取消尝试 %d 个",
            subscription_id,
            len(to_add),
            reactivated,
            deactivated,
            cancelled_attempts,
        )
        if to_add or reactivated:
            self._kick_search()  # diff 可能补了新的补旧工单，同样立即发车
        return subscription

    async def set_follow_future(self, subscription_id: int, enabled: bool) -> Subscription:
        """独立开启/关闭剧集自动续订；重复设置同一状态保持幂等。"""
        subscription = await self._get_or_404(subscription_id)
        if MediaKind(subscription.kind) is not MediaKind.TV:
            raise BadRequestException("只有剧集订阅可以设置自动续订")
        if subscription.follow_future == enabled:
            return subscription
        return await self.update(subscription_id, follow_future=enabled)

    async def _reconcile_attempt_scope(
        self,
        subscription_id: int,
        wanted_rows: list[WantedItem],
        *,
        now: datetime,
    ) -> tuple[int, int, set[str]]:
        """让下载尝试与当前范围同步，但绝不替用户删除下载器里的任务。

        一个季包可能覆盖多个单元，因此只在尝试已没有任何入域在途目标时取消；
        重新勾选季则恢复原主尝试，继续使用既有 infohash，避免重复投递。试用源
        是一次性救援过程，退出范围后不自动恢复，后续由主源重新触发救援。
        """
        result = await self._session.execute(
            select(SubscriptionDownloadAttempt).where(
                SubscriptionDownloadAttempt.subscription_id == subscription_id
            )
        )
        attempts = list(result.scalars().all())
        by_id = {row.id: row for row in attempts if row.id is not None}
        children_by_parent: dict[int, list[SubscriptionDownloadAttempt]] = {}
        for row in attempts:
            if row.replaces_attempt_id is not None:
                children_by_parent.setdefault(row.replaces_attempt_id, []).append(row)
        active_statuses = {
            DownloadAttemptStatus.ACTIVE,
            DownloadAttemptStatus.REPLACEMENT_PENDING,
            DownloadAttemptStatus.TRIAL,
            DownloadAttemptStatus.CLEANUP_PENDING,
            DownloadAttemptStatus.COMPLETED,
        }

        def _units(row: SubscriptionDownloadAttempt) -> set[tuple[int, int]]:
            units: set[tuple[int, int]] = set()
            for unit in row.units:
                if isinstance(unit, (list, tuple)) and len(unit) == 2:
                    try:
                        units.add((int(unit[0]), int(unit[1])))
                    except (TypeError, ValueError):
                        continue
            return units

        def _targets(row: SubscriptionDownloadAttempt) -> list[WantedItem]:
            source_hashes = {row.info_hash.lower()}
            if (
                row.status == DownloadAttemptStatus.TRIAL
                and row.replaces_attempt_id is not None
            ):
                parent = by_id.get(row.replaces_attempt_id)
                if parent is not None:
                    # 试用阶段工单仍指向旧源；只有晋升成功后才改成试用源 hash。
                    source_hashes = {parent.info_hash.lower()}
            elif row.status == DownloadAttemptStatus.CLEANUP_PENDING and row.id is not None:
                # 已晋升时旧源不再直接持有工单，但仍由活动子尝试接管。普通订阅
                # 更新不能把这段谱系误判成“无目标”，否则会打断幂等清理。
                source_hashes.update(
                    child.info_hash.lower()
                    for child in children_by_parent.get(row.id, [])
                    if child.status
                    in (DownloadAttemptStatus.ACTIVE, DownloadAttemptStatus.COMPLETED)
                )
            scoped_units = _units(row)
            return [
                wanted
                for wanted in wanted_rows
                if wanted.in_scope
                and wanted.status in (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)
                and (wanted.info_hash or "").lower() in source_hashes
                and (
                    not scoped_units
                    or (wanted.season_number, wanted.episode_number) in scoped_units
                )
            ]

        cancelled = 0
        resumed = 0
        cancelled_hashes: set[str] = set()
        for attempt in attempts:
            targets = _targets(attempt)
            if not targets and attempt.status in active_statuses:
                attempt.status = DownloadAttemptStatus.CANCELLED
                attempt.next_search_at = None
                attempt.cleanup_note = "关联单元已退出当前订阅范围；保留下载器任务，不再观察或换源"
                self._session.add(attempt)
                cancelled += 1
                cancelled_hashes.add(attempt.info_hash.lower())
                continue
            if (
                targets
                and attempt.status == DownloadAttemptStatus.CANCELLED
            ):
                # 未晋升的试用源在 CANCELLED 状态下按自身 hash 找不到目标，不会
                # 复活；已经晋升并成为当前主源的子尝试则应随重新勾选立即恢复。
                attempt.status = (
                    DownloadAttemptStatus.COMPLETED
                    if all(row.status == WantedStatus.DOWNLOADED for row in targets)
                    else DownloadAttemptStatus.ACTIVE
                )
                attempt.last_progress_at = now
                attempt.missing_observations = 0
                attempt.next_search_at = None
                attempt.cleanup_note = None
                self._session.add(attempt)
                resumed += 1
        return cancelled, resumed, cancelled_hashes

    # ------------------------------------------------------------------
    # 状态操作与查询
    # ------------------------------------------------------------------

    async def redownload_missing_units(
        self,
        kind: MediaKind,
        item: MediaItem,
        units: set[tuple[int, int]],
        *,
        library_id: int | None = None,
    ) -> tuple[Subscription, int]:
        """把媒体库缺失的单元重新交给订阅管线补回（缺失清单的「重新下载」）。

        与 update 的 diff 铁律（已 grabbed/imported 一律保留、绝不重复下载）
        刻意相反：这里用户明确表达了"文件没了，再下一次"，缺失单元的既有
        终态工单要显式重置回 wanted 并立即排队搜索；没有工单的补建。
        无订阅时按缺失季创建（create 的初始工单生成会跳过在位文件，
        生成范围恰好覆盖缺失单元）。返回 (订阅, 重新排队的工单数)。
        """
        assert item.id is not None
        existing = await self._repo.get_by_media_item(item.id)
        if existing is None:
            seasons = sorted({s for s, _ in units if s > 0}) if kind is MediaKind.TV else None
            subscription = await self.create(
                kind, item.tmdb_id, selected_seasons=seasons, library_id=library_id
            )
            assert subscription.id is not None
            # 重新下载与「立即搜索」同为用户强制：文件曾经存在，资源显然可得。
            # 电影可能被"未上映/未定档缓搜"排到未来或不可调度（TMDB 档期与
            # 现实不符时），这里把缺失单元改回立即排队，不能让用户点了却没动静
            forced = 0
            now = utcnow()
            for row in await self._repo.list_wanted(subscription.id):
                if (
                    (row.season_number, row.episode_number) in units
                    and row.status == WantedStatus.WANTED
                    and (row.next_search_at is None or row.next_search_at > now)
                ):
                    row.next_search_at = now
                    self._session.add(row)
                    forced += 1
            if forced:
                await self._session.commit()
                self._kick_search()
            return subscription, len(units)

        subscription = existing
        assert subscription.id is not None
        if kind is MediaKind.TV:
            # 缺失季并入勾选季：update 会为新入域且无工单的单元补工单
            merged = sorted(set(subscription.selected_seasons) | {s for s, _ in units if s > 0})
            if merged != sorted(subscription.selected_seasons):
                subscription = await self.update(subscription.id, selected_seasons=merged)
                assert subscription.id is not None

        wanted = await self._repo.list_wanted(subscription.id)
        by_key = {(w.season_number, w.episode_number): w for w in wanted}
        requeued = 0
        now = utcnow()
        for unit in units:
            row = by_key.get(unit)
            if row is None:
                # 有订阅但该单元从未有工单（订阅创建时文件在位被跳过）→ 补建
                await self._repo.add_wanted(
                    [self._to_wanted(subscription, ExpectedUnit(unit[0], unit[1], None))]
                )
                requeued += 1
                continue
            if row.status == WantedStatus.WANTED:
                # 排在未来/不可调度的（电影未上映缓搜、未定档、退避冷却中）
                # 清零到当下重新排队——重新下载与「立即搜索」同为用户强制，
                # 文件曾经存在说明资源可得；search_attempts 保留，不清退避阶梯
                if row.next_search_at is None or row.next_search_at > now:
                    row.next_search_at = now
                    self._session.add(row)
                    requeued += 1
                continue
            row.status = WantedStatus.WANTED
            row.info_hash = None
            row.grabbed_at = None
            row.downloaded_at = None
            row.imported_at = None
            row.search_attempts = 0
            row.last_search_at = None
            row.next_search_at = now  # 补旧语义：立即排队真实搜索
            self._session.add(row)
            requeued += 1
        if requeued:
            await self._log(
                subscription,
                ActivityType.ADJUSTED,
                f"媒体库文件缺失，重新下载：{requeued} 个工单重新排队",
                payload={"requeued_units": sorted(units)},
            )
            item_row = await self._media_repo_get(subscription.media_item_id)
            await self._recompute_status(subscription, item_row)
            self._kick_search()
        return subscription, requeued

    async def search_now(self, subscription_id: int) -> int:
        """用户手动触发「立即搜索」：把可搜索的缺口工单全部重置为立刻到期。

        剧集只碰"本来就能搜"的工单——未定档（next_search_at 为 None）没有
        可用的搜索时机，待播出（air_date 在未来）搜了也只会空手而归并打乱
        "播出 + 宽限"的原定档期，两者都跳过。电影不设这层限制：未上映/未定档
        的缓搜只是系统的默认保守策略，用户主动触发就是明确的"我现在就要搜
        一次"——搜完由搜索管线按上映档期把调度恢复原状（见 wanted_search 的
        movie_plan 地板）。已在冷却退避中的工单清零到"现在"，随后踢一次
        缺口搜索，不必等下个调度周期。返回重置的工单数。
        """
        subscription = await self._get_or_404(subscription_id)
        if subscription.status == SubscriptionStatus.PAUSED:
            raise BadRequestException("订阅已暂停，请先恢复追踪再触发搜索")
        now = utcnow()
        today = now.date()
        is_movie = subscription.kind == MediaKind.MOVIE.value
        wanted = await self._repo.list_wanted(subscription_id, in_scope_only=True)
        reset = 0
        for w in wanted:
            if w.status != WantedStatus.WANTED:
                continue
            if not is_movie:
                if w.next_search_at is None:
                    continue
                if w.air_date is not None and w.air_date > today:
                    continue
            w.next_search_at = now
            self._session.add(w)
            reset += 1
        # 洗版半边：已入库但未到洗版目标的单元一并重置（规则组未配洗版时为 0）
        from movieclaw_api.services.subscription.upgrade import reset_upgrade_search_now

        upgrade_reset = await reset_upgrade_search_now(self._session, subscription_id)
        reset += upgrade_reset
        if reset == 0:
            raise BadRequestException("当前没有可立即搜索的缺口（在途/未定档/待播出的项无需触发）")
        await self._log(
            subscription,
            ActivityType.ADJUSTED,
            f"用户触发立即搜索：{reset} 个缺口跳过冷却、重新排队"
            + (f"（含 {upgrade_reset} 个洗版单元）" if upgrade_reset else ""),
            payload={
                "reset_count": reset,
                "upgrade_reset_count": upgrade_reset,
                "reason": "search_now",
            },
        )
        self._kick_search()
        return reset

    async def set_paused(self, subscription_id: int, paused: bool) -> Subscription:
        """暂停/恢复。暂停是用户显式状态；恢复后由派生重算落到 active/completed。"""
        subscription = await self._get_or_404(subscription_id)
        if paused:
            subscription.status = SubscriptionStatus.PAUSED
            await self._repo.save(subscription)
            await self._log(
                subscription,
                ActivityType.PAUSED,
                "已暂停：资源匹配与搜索将跳过该订阅，随时可恢复",
            )
            return subscription
        subscription.status = SubscriptionStatus.ACTIVE
        await self._repo.save(subscription)
        await self._log(subscription, ActivityType.RESUMED, "已恢复追踪")
        item = await self._media_repo_get(subscription.media_item_id)
        await self._recompute_status(subscription, item)
        await refresh_release_forecasts(
            self._session, media_item_ids={subscription.media_item_id}
        )
        self._kick_search()  # 暂停期间积压的到期工单立即处理
        return subscription

    async def assert_can_manage(self, subscription_id: int, member_id: int | None) -> None:
        """归属校验：修改/暂停订阅只有发起人与超管可以（§3.5）。

        ``member_id`` 为 None（超管/PAT/Agent）直通；成员不是发起人时给
        可读中文错误——他能做的是取消关注，而不是改别人的订阅参数。
        """
        if member_id is None:
            return
        subscription = await self._get_or_404(subscription_id)
        if subscription.created_by_member_id != member_id:
            raise ForbiddenException(
                "只有订阅的发起人（或管理员）可以调整它；如不想再追，可在列表里取消关注"
            )

    async def unsubscribe(self, subscription_id: int, *, member_id: int) -> str:
        """让成员停止关注订阅，返回可直接展示的结果描述。

        这里表达的是成员的“退出”意图，不是管理员删除资源：
        - 非发起人 → 只删自己的关注行，订阅不受影响；
        - 发起人且仍有关注者 → 发起人转移给最早的关注者，订阅继续活着——
          成员的取消永远不影响别人正在追的内容；
        - 发起人且无关注者 → 真删。
        """
        subscription = await self._get_or_404(subscription_id)
        if subscription.created_by_member_id != member_id:
            if await self._repo.remove_follower(subscription_id, member_id):
                logger.info("成员 #%d 已取消关注订阅 #%d", member_id, subscription_id)
                return "已取消关注；订阅本身不受影响"
            raise ForbiddenException(
                "只有订阅的发起人（或管理员）可以删除它；你当前也没有关注本订阅"
            )
        followers = await self._repo.follower_member_ids(subscription_id)
        if followers:
            heir = followers[0]
            subscription.created_by_member_id = heir
            await self._repo.save(subscription)
            await self._repo.remove_follower(subscription_id, heir)
            await self._log(
                subscription, ActivityType.CREATED, "发起人退出，订阅转由最早的关注者接管"
            )
            logger.info(
                "订阅 #%d 发起人 #%d 退出，转移给成员 #%d", subscription_id, member_id, heir
            )
            return "你已退出；订阅转由其他关注的家人继续追更"
        await self._repo.delete(subscription)
        logger.info("成员 #%d 退出并删除无人关注的订阅 #%d", member_id, subscription_id)
        return "已取消订阅"

    async def delete_permanently(self, subscription_id: int) -> str:
        """管理员永久删除订阅记录与工单，不影响已下载文件或下载器任务。"""
        subscription = await self._get_or_404(subscription_id)
        await self._repo.delete(subscription)
        logger.info("管理员已永久删除订阅 #%d", subscription_id)
        return "订阅已永久删除；已下载内容不受影响"

    async def delete(self, subscription_id: int, *, member_id: int | None = None) -> str:
        """兼容旧调用；新代码应明确选择 ``unsubscribe`` 或 ``delete_permanently``。"""
        if member_id is None:
            return await self.delete_permanently(subscription_id)
        return await self.unsubscribe(subscription_id, member_id=member_id)

    async def list_with_progress(
        self, *, kind: str | None = None, member_id: int | None = None
    ) -> list[tuple[Subscription, MediaItem, dict[str, int]]]:
        """列表页数据：订阅 + 条目 + 工单状态分布（一次分组统计，不逐条查）。

        ``member_id``：成员视角只看"自己发起的 + 自己关注的"；None=全部
        （超管视角）。
        """
        subscriptions = await self._repo.list_all(kind=kind)
        if member_id is not None:
            followed = await self._repo.followed_subscription_ids(member_id)
            subscriptions = [
                s
                for s in subscriptions
                if s.created_by_member_id == member_id or s.id in followed
            ]
        counts = await self._repo.count_wanted_by_status(
            [s.id for s in subscriptions if s.id is not None]
        )
        rows: list[tuple[Subscription, MediaItem, dict[str, int]]] = []
        for sub in subscriptions:
            item = await self._media_repo_get(sub.media_item_id)
            rows.append((sub, item, counts.get(sub.id or -1, {})))
        return rows

    async def today_arrivals(self, *, member_id: int | None = None) -> list[TodayArrivalCandidate]:
        """聚合最近一次可能入库的可见内容，不查询外部下载器。

        资源预测只负责给出“何时可能出种”。首页把本订阅过往的
        ``投递→入库`` 中位耗时叠加上去，得到面向用户的预计入库时间；
        下载中的实时 ETA 由 Web 已有的全局下载快照进一步覆盖。

        **今天优先**：候选先按 ``today .. today + _UPCOMING_WINDOW_DAYS`` 收集，
        再**按媒体类型各自**收敛到一个焦点日——今天有安排就只回今天，否则只回
        窗口内最近一个有安排的日子。首页因此永远只讲“下一次是什么时候”，既不会
        在订阅少时空着，也不会退化成一张七天流水账。按类型分开收敛是因为首页的
        分区切换就是按类型分的：混在一起收敛会让“电影今天在下载”顶掉“剧集三天后
        更新”，切到剧集分区就只剩一句并不成立的“接下来一周没有更新”。

        **电影只在管道内纳入**：电影没有播出日，未投递时给不出可信的预告时间，
        常年在找资源的电影会天天占据首页；已投递/已下载的电影则和剧集一样，
        “下载中 / 整理中”对用户完全成立。
        """
        visible = await self.list_with_progress(member_id=member_id)
        subscriptions = {
            sub.id: (sub, media) for sub, media, _counts in visible if sub.id is not None
        }
        wanted_rows = await self._repo.list_wanted_many(list(subscriptions), in_scope_only=True)
        next_probe_by_wanted = await next_forecast_probe_times_by_wanted(
            self._session,
            wanted_items=[row for row in wanted_rows if row.status == WantedStatus.WANTED],
        )
        by_subscription: dict[int, list[WantedItem]] = {}
        for wanted in wanted_rows:
            by_subscription.setdefault(wanted.subscription_id, []).append(wanted)

        today = publish_calendar_date(utcnow())
        horizon = today + timedelta(days=_UPCOMING_WINDOW_DAYS)
        candidates: list[TodayArrivalCandidate] = []
        for subscription_id, (subscription, media) in subscriptions.items():
            rows = by_subscription.get(subscription_id, [])
            release_to_import = _median_pipeline_minutes(
                rows,
                start_field="grabbed_at",
                end_field="imported_at",
                fallback=_DEFAULT_RELEASE_TO_IMPORT_MINUTES,
                maximum=timedelta(days=7),
            )
            download_to_import = _median_pipeline_minutes(
                rows,
                start_field="downloaded_at",
                end_field="imported_at",
                fallback=_DEFAULT_DOWNLOAD_TO_IMPORT_MINUTES,
                maximum=timedelta(days=1),
            )
            for wanted in rows:
                in_pipeline = wanted.status in (
                    WantedStatus.GRABBED,
                    WantedStatus.DOWNLOADED,
                )
                if in_pipeline:
                    # 模拟投递没有后续下载/入库链路，不应伪装成首页的“下载中”。
                    if wanted.info_hash is None:
                        continue
                    # 已经在下载/整理链路里的内容随时可能落地，一律归入今天。
                    expected_day = today
                elif wanted.status == WantedStatus.WANTED:
                    if subscription.status != SubscriptionStatus.ACTIVE:
                        continue
                    # 电影没有播出日，还在找资源时给不出可信的预告时间。
                    if media.kind == MediaKind.MOVIE.value:
                        continue
                    predicted = _forecast_predicted_at(wanted)
                    predicted_import_day = (
                        publish_calendar_date(predicted + timedelta(minutes=release_to_import))
                        if predicted is not None
                        else None
                    )
                    day = _expected_arrival_day((wanted.air_date, predicted_import_day), today)
                    if day is None or day > horizon:
                        continue
                    expected_day = day
                else:
                    continue

                candidates.append(
                    TodayArrivalCandidate(
                        subscription=subscription,
                        media=media,
                        wanted=wanted,
                        next_probe_at=next_probe_by_wanted.get(wanted.id or -1),
                        release_to_import_minutes=release_to_import,
                        download_to_import_minutes=download_to_import,
                        expected_day=expected_day,
                        days_ahead=(expected_day - today).days,
                    )
                )

        # 今天优先收敛：只留焦点日，避免首页把一周的安排一次性倒给用户。
        # 按媒体类型各算一次——首页的分区切换正是按类型分的，混在一起收敛会让
        # “电影今天在下载”顶掉“剧集三天后更新”，用户切到剧集分区就会看到一句
        # 并不成立的“接下来一周没有更新”。前端过滤完当前分区后再收敛到最近一天。
        by_kind: dict[str, list[TodayArrivalCandidate]] = {}
        for row in candidates:
            by_kind.setdefault(row.media.kind, []).append(row)
        candidates = [
            row for rows_of_kind in by_kind.values() for row in _focus_day_rows(rows_of_kind, today)
        ]

        status_order = {
            WantedStatus.DOWNLOADED: 0,
            WantedStatus.GRABBED: 1,
            WantedStatus.WANTED: 2,
        }
        candidates.sort(
            key=lambda row: (
                status_order.get(row.wanted.status, 9),
                _forecast_predicted_at(row.wanted) or datetime.max,
                row.media.title,
                row.wanted.season_number,
                row.wanted.episode_number,
            )
        )
        return candidates

    async def detail(
        self, subscription_id: int
    ) -> tuple[Subscription, MediaItem, list[WantedItem]]:
        subscription = await self._get_or_404(subscription_id)
        item = await self._media_repo_get(subscription.media_item_id)
        wanted = await self._repo.list_wanted(subscription_id, in_scope_only=True)
        return subscription, item, wanted

    async def resource_timings(
        self, subscription_id: int
    ) -> dict[tuple[int, int], dict[str, object]]:
        """按季集返回最近一次成功投递的资源时间链。

        新活动直接读取冻结快照；上线前的老活动没有这些键时，再用其
        ``site_id/torrent_id`` 从现存 ``site_torrent`` 回补发布时间和首次索引
        时间。站点索引已经被删除的老记录自然降级，不伪造耗时。
        """
        activities = await self._repo.list_grabbed_activities(subscription_id)
        if not activities:
            return {}

        legacy_refs: dict[str, set[str]] = {}
        for activity in activities:
            payload = activity.payload
            site_id = payload.get("site_id")
            torrent_id = payload.get("torrent_id")
            if not isinstance(site_id, str) or not isinstance(torrent_id, str):
                continue
            if not payload.get("resource_publish_time") or not payload.get(
                "resource_first_seen_at"
            ):
                legacy_refs.setdefault(site_id, set()).add(torrent_id)

        indexed: dict[tuple[str, str], SiteTorrent] = {}
        if legacy_refs:
            conditions = [
                and_(
                    SiteTorrent.site_id == site_id,
                    SiteTorrent.torrent_id.in_(torrent_ids),  # type: ignore[union-attr]
                )
                for site_id, torrent_ids in legacy_refs.items()
            ]
            rows = await self._session.execute(select(SiteTorrent).where(or_(*conditions)))
            indexed = {(row.site_id, row.torrent_id): row for row in rows.scalars().all()}

        result: dict[tuple[int, int], dict[str, object]] = {}
        for activity in activities:
            payload = activity.payload
            site_id = payload.get("site_id")
            torrent_id = payload.get("torrent_id")
            if not isinstance(site_id, str) or not isinstance(torrent_id, str):
                continue
            torrent = indexed.get((site_id, torrent_id))
            publish_time = _payload_time(payload.get("resource_publish_time"))
            first_seen_at = _payload_time(payload.get("resource_first_seen_at"))
            submitted_at = _payload_time(payload.get("submitted_at")) or activity.created_at
            if publish_time is None and torrent is not None:
                publish_time = torrent.publish_time
            if first_seen_at is None and torrent is not None:
                first_seen_at = torrent.created_at

            units = payload.get("units")
            if not isinstance(units, list):
                continue
            timing: dict[str, object] = {
                "site_id": site_id,
                "torrent_id": torrent_id,
                "publish_time": publish_time,
                "first_seen_at": first_seen_at,
                "submitted_at": submitted_at,
                "dry_run": payload.get("dry_run") is True,
            }
            for unit in units:
                if (
                    isinstance(unit, list)
                    and len(unit) == 2
                    and all(isinstance(number, int) for number in unit)
                ):
                    # 活动按最新在前；重新下载过的单元只展示最近一次拉取耗时。
                    result.setdefault((unit[0], unit[1]), timing)
        return result

    # ------------------------------------------------------------------
    # 工单构造与派生状态（核心逻辑在模块级函数，服务只做委托）
    # ------------------------------------------------------------------

    def _to_wanted(
        self,
        subscription: Subscription,
        unit: ExpectedUnit,
        *,
        schedule: tuple[datetime | None, int] | None = None,
    ) -> WantedItem:
        """``schedule``：显式调度覆盖（电影传 ``movie_schedule`` 的上映感知结果）；
        不传时按单元自身的三类调度语义。"""
        assert subscription.id is not None
        next_search, priority = (
            schedule if schedule is not None else schedule_for(subscription.kind, unit)
        )
        return WantedItem(
            subscription_id=subscription.id,
            media_item_id=subscription.media_item_id,
            season_number=unit.season_number,
            episode_number=unit.episode_number,
            status=WantedStatus.WANTED,
            air_date=unit.air_date,
            priority=priority,
            next_search_at=next_search,
        )

    async def _recompute_status(self, subscription: Subscription, item: MediaItem) -> None:
        await recompute_subscription_status(self._session, subscription, item)

    async def activities(
        self, subscription_id: int, *, limit: int = 100
    ) -> list[SubscriptionActivity]:
        """订阅活动流水（时间倒序）——详情页时间线的数据源。"""
        await self._get_or_404(subscription_id)
        return await self._repo.list_activities(subscription_id, limit=limit)

    # ------------------------------------------------------------------
    # 活动流水（透明化：每个动作落一条中文可读记录）
    # ------------------------------------------------------------------

    async def _log(
        self,
        subscription: Subscription,
        activity_type: ActivityType,
        message: str,
        *,
        wanted_item_id: int | None = None,
        payload: dict | None = None,
    ) -> None:
        assert subscription.id is not None
        await self._repo.add_activity(
            SubscriptionActivity(
                subscription_id=subscription.id,
                wanted_item_id=wanted_item_id,
                type=activity_type,
                message=message,
                payload=payload or {},
            )
        )

    def _created_message(
        self,
        item: MediaItem,
        kind: MediaKind,
        selected: list[int],
        follow_future: bool,
        rows: list[WantedItem],
    ) -> str:
        """创建活动的中文摘要：把 E 的定义和调度分布一句话说清楚。"""
        now = utcnow()
        if kind is MediaKind.MOVIE:
            row = rows[0] if rows else None
            if row is None:
                return f"创建订阅《{item.title}》"
            if row.next_search_at is None:
                return (
                    f"创建订阅《{item.title}》：影片尚未定档，定档/上映后自动安排搜索；"
                    "期间站点新出的资源仍会被自动匹配"
                )
            if row.next_search_at > now:
                return (
                    f"创建订阅《{item.title}》：影片未上映或刚上映，"
                    f"{row.next_search_at.date().isoformat()} 起开始搜索资源；"
                    "期间站点新出的资源仍会被自动匹配"
                )
            return f"创建订阅《{item.title}》：已加入搜索队列，等待搜索任务寻找资源"
        immediate = sum(1 for w in rows if w.next_search_at is not None and w.next_search_at <= now)
        future = sum(1 for w in rows if w.next_search_at is not None and w.next_search_at > now)
        undated = sum(1 for w in rows if w.next_search_at is None)
        parts = []
        if immediate:
            parts.append(f"{immediate} 集已播出、排入搜索队列")
        if future:
            parts.append(f"{future} 集未播出、播出后先等被动匹配")
        if undated:
            parts.append(f"{undated} 集未定档、定档后再安排")
        detail = "；".join(parts) if parts else "暂无待办集"
        return (
            f"创建订阅《{item.title}》：勾选{self._season_text(selected)}，"
            f"自动续订{'开' if follow_future else '关'}；"
            f"共生成 {len(rows)} 个追踪项——{detail}"
        )

    @staticmethod
    def _season_text(selected: list[int]) -> str:
        if not selected:
            return "（未勾选任何季）"
        return "第 " + "、".join(str(n) for n in selected) + " 季"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _kick_search(self) -> None:
        """产生了"立刻可搜"的工单后立即踢一次缺口搜索。

        收口在 service 层：任何入口（HTTP 路由、媒体库缺失重下、未来的
        agent 工具等）只要走本服务的写方法，就自动获得"创建即搜"语义，
        不依赖调用方自觉。延迟导入避免与搜索管线形成环。
        """
        from movieclaw_api.services.subscription.wanted_search import kick_search_soon

        kick_search_soon()

    async def _movie_plan(self, item: MediaItem) -> tuple[datetime | None, int]:
        """电影哨兵工单的上映感知调度：上映日期取自条目档案（建档事务已写入）。"""
        assert item.id is not None
        release_date = (
            await self._session.execute(
                select(MediaMetadata.release_date).where(MediaMetadata.media_item_id == item.id)
            )
        ).scalar_one_or_none()
        return movie_schedule(release_date, item.status)

    async def _owned_units(self, media_item_id: int) -> set[tuple[int, int]]:
        """库存 H：该条目在媒体库里已拥有的期望单元（媒体库 L3 联通）。"""
        from movieclaw_db.repositories.library_file_repo import LibraryFileRepository

        return await LibraryFileRepository(self._session).owned_units(media_item_id)

    async def _validate_library(self, kind: str, library_id: int) -> Library:
        """校验目标库存在且类型匹配（电影订阅只能入电影库，剧集同理）。"""
        row = await LibraryRepository(self._session).get(library_id)
        if row is None:
            raise BadRequestException(f"媒体库不存在：id={library_id}")
        if row.kind != kind:
            kind_text = "电影" if row.kind == MediaKind.MOVIE.value else "剧集"
            raise BadRequestException(f"媒体库「{row.name}」是{kind_text}库，与该订阅的类型不匹配")
        return row

    @staticmethod
    def _validate_selection(
        kind: MediaKind, selected: list[int], seasons: list[MediaSeason]
    ) -> list[int]:
        if kind is MediaKind.MOVIE:
            if selected:
                raise BadRequestException("电影订阅不支持季选择")
            return []
        known = {s.season_number for s in seasons}
        unknown = sorted(set(selected) - known)
        if unknown:
            raise BadRequestException(f"该剧不存在这些季：{unknown}")
        return sorted(set(selected))

    @staticmethod
    def _is_movie_unit(wanted: WantedItem) -> bool:
        return wanted.season_number == 0 and wanted.episode_number == 0

    async def _get_or_404(self, subscription_id: int) -> Subscription:
        subscription = await self._repo.get(subscription_id)
        if subscription is None:
            raise NotFoundException(f"订阅不存在：#{subscription_id}")
        return subscription

    async def _media_repo_get(self, media_item_id: int) -> MediaItem:
        item = await self._session.get(MediaItem, media_item_id)
        if item is None:  # 外键保证下理论不可达
            raise NotFoundException("订阅关联的媒体条目不存在")
        return item
