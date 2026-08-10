"""订阅服务：期望集合 E 的定义、物化与调和入口（docs/design/subscription.md 第 2 节）。

核心算法只有两个，全部围绕 E：

- **初始化**：``_expected_units`` 按订阅参数展开期望单元，``_schedule_for`` 给每个
  单元写死调度语义（补旧=now / 追新=air_date+宽限 / 未定档=NULL）；
- **diff 重算**：修改订阅时新增缺的、删掉出域且未完成的，**已 grabbed 的永不回收**
  （不变量③：现实不可逆）。

订阅状态是派生值（不变量④）：``_recompute_status`` 随时可从工单集合重算，
paused 是用户显式操作、重算不碰它。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import EllipsisType

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.rule_sets import RuleSetService
from movieclaw_api.services.subscription.quality import (
    LOCK_FIRST,
    UPGRADE,
    normalized_policy,
    public_policy,
    target_profile_from_rule,
)
from movieclaw_db.models import (
    ActivityType,
    MediaEpisode,
    MediaItem,
    MediaSeason,
    Subscription,
    SubscriptionActivity,
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
from movieclaw_matcher import RuleSetSpec
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.subscription")

# 追新工单的漏抓宽限期：air_date + 该值 = 首个真实搜索到期时刻。
# 被动匹配通常在到点前满足工单；真到点仍缺的，worker 捞起即漏抓兜底。
FUTURE_GRACE = timedelta(hours=48)

# 剧集完结类 status：期望集合不再生长的判定输入之一
_ENDED_STATUSES = frozenset({"Ended", "Canceled"})


@dataclass(frozen=True)
class ExpectedUnit:
    """期望单元：E 集合的元素。电影是 (0, 0) 哨兵单元。"""

    season_number: int
    episode_number: int
    air_date: date | None


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
    追新开关时，不回补开关关闭期间播出的集（用户此刻的意图是"从现在起追"）。
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

    电影没有播出日期概念，恒为补旧。追新工单给高优先级：新集是用户
    最急着要的，worker 排序时优先处理。
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
    wanted = await repo.list_wanted(subscription.id)

    def _is_open(w: WantedItem) -> bool:
        if w.status in (WantedStatus.WANTED, WantedStatus.UPGRADING):
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
        quality_policy: dict | None = None,
    ) -> Subscription:
        """创建订阅并生成初始工单。同一条目已有订阅时幂等返回已有（不改参数）。"""
        item, seasons, existing = await self.prepare(kind, tmdb_id, douban_id=douban_id)
        if existing is not None:
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
        saved_quality_policy = await self._build_quality_policy(kind, quality_policy)
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

        subscription = await self._repo.save(
            Subscription(
                media_item_id=item.id,
                kind=kind.value,
                selected_seasons=selected,
                follow_future=follow_future,
                rule_set_id=rule_set_id,
                library_id=library_id,
                quality_policy=saved_quality_policy,
                status=SubscriptionStatus.ACTIVE,
            )
        )
        assert subscription.id is not None

        episodes = await self._media_repo.list_episodes(item.id)
        units = expected_units(kind, episodes, selected, follow_future)
        # 库存联通（媒体库 L3）：库里已有的单元不生成工单——E−H 用真实的 H
        owned = await self._owned_units(item.id)
        skipped_owned = [u for u in units if (u.season_number, u.episode_number) in owned]
        units = [u for u in units if (u.season_number, u.episode_number) not in owned]
        rows = [self._to_wanted(subscription, unit) for unit in units]
        await self._repo.add_wanted(rows)
        await self._reconcile_quality_policy(subscription)
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
                "quality_policy": self._public_quality_policy(subscription.quality_policy),
            },
        )
        await self._recompute_status(subscription, item)
        logger.info(
            "已订阅《%s》(%s)：勾选季 %s，追新 %s，生成工单 %d 个",
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
        quality_policy: dict | None | EllipsisType = ...,
    ) -> Subscription:
        """修改 E 的定义（季选择/追新/规则组/入库目标库），diff 重算工单。

        ``library_id`` 用 ``...``（Ellipsis）作「未传=不变」的哨兵：显式传 None
        表示清除指定库、改回按默认库路由——旧订阅（library_id 为空）需要这条
        双向通道。

        diff 规则（不变量③）：
        - 新入域的单元 → 补工单（调度语义按当下重新判定）；
        - 出域且 status=wanted 的 → 删除；
        - **已 grabbed/downloaded 的一律保留**——现实不可逆，重新入域时也
          因此不会重复下载。
        """
        subscription = await self._get_or_404(subscription_id)
        item = await self._media_repo_get(subscription.media_item_id)
        disabled_quality_policy = None

        new_rule_set = None
        if rule_set_id is not None and rule_set_id != subscription.rule_set_id:
            new_rule_set = await self._rule_sets.get(rule_set_id)
            subscription.rule_set_id = rule_set_id
        if library_id is not ... and library_id != subscription.library_id:
            if library_id is not None:
                await self._validate_library(subscription.kind, library_id)
            subscription.library_id = library_id
        if quality_policy is not ...:
            next_policy = await self._build_quality_policy(
                MediaKind(subscription.kind), quality_policy
            )
            current = normalized_policy(subscription.quality_policy)
            if (
                current is not None
                and next_policy is not None
                and current.get("mode") == next_policy.get("mode")
                and current.get("target_rule_set_id")
                == next_policy.get("target_rule_set_id")
            ):
                next_policy = current  # 相同策略保存时保留锁定档案、锚点和在途候选
            elif current is not None and next_policy is None:
                disabled_quality_policy = current
            elif (
                current is not None
                and current.get("mode") == UPGRADE
                and next_policy is not None
            ):
                pending = current.get("pending")
                if isinstance(pending, dict):
                    next_policy["pending"] = pending
                    if next_policy.get("mode") == UPGRADE:
                        for entry in pending.values():
                            if isinstance(entry, dict):
                                entry["meets_target"] = False
                anchor_unit = current.get("anchor_unit")
                if next_policy.get("mode") == UPGRADE and isinstance(
                    anchor_unit, str
                ):
                    next_policy["anchor_unit"] = anchor_unit
            subscription.quality_policy = next_policy

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
        to_add = [
            self._to_wanted(subscription, unit)
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

        to_remove = [
            w
            for w in existing
            if w.status in (WantedStatus.WANTED, WantedStatus.UPGRADING)
            and (w.season_number, w.episode_number) not in expected_keys
            and w.season_number not in selected_set
            and not _protected_by_follow(w)
        ]

        await self._repo.save(subscription)
        if to_add:
            await self._repo.add_wanted(to_add)
        if to_remove:
            await self._repo.delete_wanted(to_remove)
        quality_requeued = await self._reconcile_quality_policy(
            subscription, previous_policy=disabled_quality_policy
        )
        season_text = self._season_text(list(subscription.selected_seasons))
        rule_note = f"，规则组改为「{new_rule_set.name}」" if new_rule_set is not None else ""
        await self._log(
            subscription,
            ActivityType.ADJUSTED,
            f"调整订阅：勾选{season_text}，持续追新{'开' if subscription.follow_future else '关'}"
            f"{rule_note}；补 {len(to_add)} 个工单，移除 {len(to_remove)} 个未完成工单"
            "（已提交下载的保留，不会重复下载）",
            payload={
                "selected_seasons": list(subscription.selected_seasons),
                "follow_future": subscription.follow_future,
                "rule_set_id": subscription.rule_set_id,
                "added": len(to_add),
                "removed": len(to_remove),
                "quality_policy": self._public_quality_policy(subscription.quality_policy),
            },
        )
        await self._recompute_status(subscription, item)
        logger.info(
            "订阅 #%s 已调整：补工单 %d 个，移除未完成工单 %d 个",
            subscription_id,
            len(to_add),
            len(to_remove),
        )
        if to_add or quality_requeued:
            self._kick_search()  # diff 可能补了新的补旧工单，同样立即发车
        return subscription

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
                continue  # 本来就在队列里，别清人家的搜索退避
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

        只碰"本来就能搜"的工单——未定档（next_search_at 为 None）没有可用
        的搜索时机，待播出（air_date 在未来）搜了也只会空手而归并打乱
        "播出 + 宽限"的原定档期，两者都跳过。已在冷却退避中的工单清零到
        "现在"，随后踢一次缺口搜索，不必等下个调度周期。返回重置的工单数。
        """
        subscription = await self._get_or_404(subscription_id)
        if subscription.status == SubscriptionStatus.PAUSED:
            raise BadRequestException("订阅已暂停，请先恢复追踪再触发搜索")
        now = utcnow()
        today = now.date()
        wanted = await self._repo.list_wanted(subscription_id)
        reset = 0
        for w in wanted:
            if (
                w.status not in (WantedStatus.WANTED, WantedStatus.UPGRADING)
                or w.next_search_at is None
            ):
                continue
            if w.air_date is not None and w.air_date > today:
                continue
            w.next_search_at = now
            self._session.add(w)
            reset += 1
        if reset == 0:
            raise BadRequestException("当前没有可立即搜索的缺口（在途/未定档/待播出的项无需触发）")
        await self._log(
            subscription,
            ActivityType.ADJUSTED,
            f"用户触发立即搜索：{reset} 个缺口跳过冷却、重新排队",
            payload={"reset_count": reset, "reason": "search_now"},
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
        self._kick_search()  # 暂停期间积压的到期工单立即处理
        return subscription

    async def delete(self, subscription_id: int) -> None:
        """删除订阅（工单级联删除；不动已下载内容与下载器任务）。"""
        subscription = await self._get_or_404(subscription_id)
        await self._repo.delete(subscription)
        logger.info("订阅 #%s 已删除", subscription_id)

    async def list_with_progress(
        self, *, kind: str | None = None
    ) -> list[tuple[Subscription, MediaItem, dict[str, int]]]:
        """列表页数据：订阅 + 条目 + 工单状态分布（一次分组统计，不逐条查）。"""
        subscriptions = await self._repo.list_all(kind=kind)
        counts = await self._repo.count_wanted_by_status(
            [s.id for s in subscriptions if s.id is not None]
        )
        rows: list[tuple[Subscription, MediaItem, dict[str, int]]] = []
        for sub in subscriptions:
            item = await self._media_repo_get(sub.media_item_id)
            rows.append((sub, item, counts.get(sub.id or -1, {})))
        return rows

    async def detail(
        self, subscription_id: int
    ) -> tuple[Subscription, MediaItem, list[WantedItem]]:
        subscription = await self._get_or_404(subscription_id)
        item = await self._media_repo_get(subscription.media_item_id)
        wanted = await self._repo.list_wanted(subscription_id)
        return subscription, item, wanted

    # ------------------------------------------------------------------
    # 工单构造与派生状态（核心逻辑在模块级函数，服务只做委托）
    # ------------------------------------------------------------------

    def _to_wanted(self, subscription: Subscription, unit: ExpectedUnit) -> WantedItem:
        assert subscription.id is not None
        next_search, priority = schedule_for(subscription.kind, unit)
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

    async def _build_quality_policy(
        self, kind: MediaKind, requested: dict | None
    ) -> dict | None:
        """请求策略转持久快照；规则组后续修改/删除不改变既有洗版目标。"""
        if requested is None:
            return None
        mode = requested.get("mode")
        if mode == LOCK_FIRST:
            if kind is MediaKind.MOVIE:
                raise BadRequestException("电影订阅不支持首次入库锁版，请选择自动洗版")
            return {"mode": LOCK_FIRST}
        if mode != UPGRADE:
            raise BadRequestException("未知的版本连续性策略")
        target_id = requested.get("target_rule_set_id")
        if not isinstance(target_id, int):
            raise BadRequestException("自动洗版必须选择目标规则组")
        target_rule = await self._rule_sets.get(target_id)
        target = target_profile_from_rule(RuleSetSpec.model_validate(target_rule.spec))
        if not target:
            raise BadRequestException(
                f"洗版目标规则组「{target_rule.name}」没有分辨率、编码、HDR/DV、平台或制作组条件"
            )
        return {
            "mode": UPGRADE,
            "target_rule_set_id": target_id,
            "target_rule_name": target_rule.name,
            "target": target,
        }

    async def _reconcile_quality_policy(
        self, subscription: Subscription, *, previous_policy: dict | None = None
    ) -> int:
        """策略开关与洗版锚点对齐；返回新进入洗版队列的工单数。"""
        assert subscription.id is not None
        policy = normalized_policy(subscription.quality_policy)
        rows = await self._repo.list_wanted(subscription.id)
        changed = 0
        now = utcnow()

        if policy is None:
            pending = (
                previous_policy.get("pending")
                if isinstance(previous_policy, dict)
                else None
            )
            for row in rows:
                entry = (
                    pending.get(f"{row.season_number}:{row.episode_number}")
                    if isinstance(pending, dict)
                    else None
                )
                was_upgrade_dispatch = (
                    row.status in (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)
                    and isinstance(entry, dict)
                    and entry.get("was_upgrading") is True
                )
                if row.status != WantedStatus.UPGRADING and not was_upgrade_dispatch:
                    continue
                row.status = WantedStatus.IMPORTED
                row.next_search_at = None
                row.updated_at = now
                self._session.add(row)
            await self._session.commit()
            return 0

        if policy.get("mode") != UPGRADE:
            return 0

        if subscription.kind == MediaKind.TV.value:
            if policy.get("locked"):
                return 0
            cleared_anchor = False
            anchor_key = policy.get("anchor_unit")
            if isinstance(anchor_key, str):
                active_anchor = next(
                    (
                        row
                        for row in rows
                        if f"{row.season_number}:{row.episode_number}" == anchor_key
                        and row.status
                        in (
                            WantedStatus.UPGRADING,
                            WantedStatus.GRABBED,
                            WantedStatus.DOWNLOADED,
                        )
                    ),
                    None,
                )
                if active_anchor is not None:
                    return 0
                policy.pop("anchor_unit", None)
                cleared_anchor = True
            upgrading = next(
                (row for row in rows if row.status == WantedStatus.UPGRADING), None
            )
            if upgrading is not None:
                if not policy.get("anchor_unit"):
                    policy["anchor_unit"] = f"{upgrading.season_number}:{upgrading.episode_number}"
                    subscription.quality_policy = policy
                    await self._repo.save(subscription)
                return 0

            anchor = next(
                (
                    row
                    for row in sorted(
                        rows, key=lambda item: (item.season_number, item.episode_number)
                    )
                    if row.status == WantedStatus.IMPORTED
                    and row.season_number in set(subscription.selected_seasons)
                ),
                None,
            )
            if anchor is None and subscription.selected_seasons:
                owned = await self._owned_units(subscription.media_item_id)
                owned = {
                    unit
                    for unit in owned
                    if unit[0] in set(subscription.selected_seasons)
                }
                existing_keys = {
                    (row.season_number, row.episode_number) for row in rows
                }
                key = next(iter(sorted(owned - existing_keys)), None)
                if key is not None:
                    episodes = await self._media_repo.list_episodes(
                        subscription.media_item_id
                    )
                    air_dates = {
                        (episode.season_number, episode.episode_number): episode.air_date
                        for episode in episodes
                    }
                    anchor = WantedItem(
                        subscription_id=subscription.id,
                        media_item_id=subscription.media_item_id,
                        season_number=key[0],
                        episode_number=key[1],
                        status=WantedStatus.UPGRADING,
                        air_date=air_dates.get(key),
                        next_search_at=now,
                    )
                    self._session.add(anchor)
            if anchor is None:
                if cleared_anchor:
                    subscription.quality_policy = policy
                    await self._repo.save(subscription)
                return 0
            anchor.status = WantedStatus.UPGRADING
            anchor.info_hash = None
            anchor.grabbed_at = None
            anchor.downloaded_at = None
            anchor.next_search_at = now
            anchor.search_attempts = 0
            anchor.last_search_at = None
            anchor.updated_at = now
            policy["anchor_unit"] = f"{anchor.season_number}:{anchor.episode_number}"
            subscription.quality_policy = policy
            self._session.add(anchor)
            self._session.add(subscription)
            await self._session.commit()
            return 1

        row = next(
            (
                item
                for item in rows
                if (item.season_number, item.episode_number) == (0, 0)
            ),
            None,
        )
        if row is None:
            row = WantedItem(
                subscription_id=subscription.id,
                media_item_id=subscription.media_item_id,
                season_number=0,
                episode_number=0,
                status=WantedStatus.UPGRADING,
                next_search_at=now,
            )
            self._session.add(row)
            changed = 1
        elif row.status == WantedStatus.IMPORTED:
            row.status = WantedStatus.UPGRADING
            row.info_hash = None
            row.grabbed_at = None
            row.downloaded_at = None
            row.next_search_at = now
            row.search_attempts = 0
            row.last_search_at = None
            row.updated_at = now
            self._session.add(row)
            changed = 1
        await self._session.commit()
        return changed

    @staticmethod
    def _public_quality_policy(value: object) -> dict | None:
        return public_policy(value)

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
        if kind is MediaKind.MOVIE:
            return f"创建订阅《{item.title}》：已加入搜索队列，等待搜索任务寻找资源"
        now = utcnow()
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
            f"持续追新{'开' if follow_future else '关'}；"
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
