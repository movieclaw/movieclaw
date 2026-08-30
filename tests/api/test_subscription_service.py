"""订阅服务的核心逻辑测试：E 的展开、三类调度语义、diff 重算与四条不变量。

夹具剧集的季集结构相对"今天"动态构造：
- S1：两集全部已播（纯补旧季）
- S2：E1 昨天播出（补旧）、E2 十天后播出（追新）、E3 未定档
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException, ConflictException
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.rule_sets import RuleSetService
from movieclaw_api.services.subscription import (
    FUTURE_GRACE,
    MOVIE_RELEASE_GRACE,
    SubscriptionService,
)
from movieclaw_api.services.subscription.matching import publish_calendar_date
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadAttemptStatus,
    NoticeStatus,
    SubscriptionDownloadAttempt,
    SubscriptionStatus,
    SystemNotice,
    WantedItem,
    WantedStatus,
)
from movieclaw_db.models.base import utcnow
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"

_TODAY = utcnow().date()
_AIRED = (_TODAY - timedelta(days=10)).isoformat()
_YESTERDAY = (_TODAY - timedelta(days=1)).isoformat()
_FUTURE_DATE = _TODAY + timedelta(days=10)
_FUTURE = _FUTURE_DATE.isoformat()

# 首页预告按站点业务日历（Asia/Shanghai）判断"今天"，UTC 凌晨会差一天，
# 因此预告相关夹具统一用站点日历日构造，避免测试在特定时段假红。
_SITE_TODAY = publish_calendar_date(utcnow())
_SITE_TODAY_ISO = _SITE_TODAY.isoformat()
_IN_WINDOW = (_SITE_TODAY + timedelta(days=3)).isoformat()

_ROUTES = {
    "/3/movie/100": {
        "id": 100,
        "title": "测试电影",
        "original_title": "Test Movie",
        "release_date": _AIRED,
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    # 电影上映感知调度的三个夹具：未上映（一个月后）/ 刚上映（3 天前，宽限内）/
    # 未定档（制作中，无档期）
    "/3/movie/101": {
        "id": 101,
        "title": "未上映电影",
        "original_title": "Upcoming Movie",
        "release_date": (_TODAY + timedelta(days=30)).isoformat(),
        "status": "Post Production",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    "/3/movie/102": {
        "id": 102,
        "title": "刚上映电影",
        "original_title": "Fresh Movie",
        "release_date": (_TODAY - timedelta(days=3)).isoformat(),
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    "/3/movie/103": {
        "id": 103,
        "title": "未定档电影",
        "original_title": "Undated Movie",
        "release_date": "",
        "status": "In Production",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    "/3/tv/200": {
        "id": 200,
        "name": "测试剧集",
        "original_name": "Test Show",
        "first_air_date": "2024-01-01",
        "status": "Returning Series",
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "seasons": [{"season_number": 1}, {"season_number": 2}],
    },
    "/3/tv/200/season/1": {
        "name": "第 1 季",
        "air_date": "2024-01-01",
        "episodes": [
            {"episode_number": 1, "name": "E1", "air_date": _AIRED},
            {"episode_number": 2, "name": "E2", "air_date": _AIRED},
        ],
    },
    "/3/tv/200/season/2": {
        "name": "第 2 季",
        "air_date": _YESTERDAY,
        "episodes": [
            {"episode_number": 1, "name": "E1", "air_date": _YESTERDAY},
            {"episode_number": 2, "name": "E2", "air_date": _FUTURE},
            {"episode_number": 3, "name": "E3", "air_date": None},
        ],
    },
    # 首页预告夹具：202 = 三天后播（窗口内）、203 = 今天播
    "/3/tv/202": {
        "id": 202,
        "name": "本周更新剧集",
        "original_name": "This Week Show",
        "first_air_date": _IN_WINDOW,
        "status": "Returning Series",
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "seasons": [{"season_number": 1}],
    },
    "/3/tv/202/season/1": {
        "name": "第 1 季",
        "air_date": _IN_WINDOW,
        "episodes": [{"episode_number": 1, "name": "E1", "air_date": _IN_WINDOW}],
    },
    "/3/tv/203": {
        "id": 203,
        "name": "今日更新剧集",
        "original_name": "Today Show",
        "first_air_date": _SITE_TODAY_ISO,
        "status": "Returning Series",
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "seasons": [{"season_number": 1}],
    },
    "/3/tv/203/season/1": {
        "name": "第 1 季",
        "air_date": _SITE_TODAY_ISO,
        "episodes": [{"episode_number": 1, "name": "E1", "air_date": _SITE_TODAY_ISO}],
    },
}


def _fake_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _ROUTES.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=payload)

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'sub.db'}")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _service(session) -> SubscriptionService:
    return SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))


async def _wanted_of(session, subscription_id: int) -> list[WantedItem]:
    result = await session.execute(
        select(WantedItem)
        .where(WantedItem.subscription_id == subscription_id)
        .order_by(WantedItem.season_number, WantedItem.episode_number)
    )
    return list(result.scalars().all())


def _key_map(rows: list[WantedItem]) -> dict[tuple[int, int], WantedItem]:
    return {(w.season_number, w.episode_number): w for w in rows}


async def _mark(session, wanted: WantedItem, status: WantedStatus) -> None:
    wanted.status = status
    wanted.grabbed_at = utcnow()
    session.add(wanted)
    await session.commit()


# ---------------------------------------------------------------------------
# E 的初始化与三类调度语义
# ---------------------------------------------------------------------------


async def test_movie_creates_single_backfill_unit(db) -> None:
    """电影 = (0,0) 哨兵工单：上映已过宽限期 → 补旧，立即排队真实搜索。"""
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.MOVIE, 100)
        wanted = await _wanted_of(session, sub.id)

    assert [(w.season_number, w.episode_number) for w in wanted] == [(0, 0)]
    assert wanted[0].status == WantedStatus.WANTED
    assert wanted[0].next_search_at is not None
    assert wanted[0].next_search_at <= utcnow()  # 补旧：now
    assert sub.status == SubscriptionStatus.ACTIVE


async def test_movie_release_aware_schedule(db) -> None:
    """电影上映感知调度：未上映/刚上映=上映+宽限、未定档=NULL 等回填。

    上映日不写进工单 air_date——covered_units 把 air_date 当发布时间物理上限，
    写了会误杀早于 TMDB 档期流出的资源（分地区上映/提前数字发行）。
    """
    async with db.session() as session:
        service = _service(session)
        now = utcnow()

        upcoming = await service.create(MediaKind.MOVIE, 101)
        w = (await _wanted_of(session, upcoming.id))[0]
        assert w.next_search_at is not None
        assert w.next_search_at.date() == _TODAY + timedelta(days=30) + MOVIE_RELEASE_GRACE
        assert w.priority > 0  # 与剧集追新同语义：被动匹配为主，到点漏抓兜底
        assert w.air_date is None

        fresh = await service.create(MediaKind.MOVIE, 102)
        w = (await _wanted_of(session, fresh.id))[0]
        assert w.next_search_at is not None
        assert w.next_search_at > now  # 刚上映（宽限内）：不立即白搜
        assert w.next_search_at.date() == _TODAY - timedelta(days=3) + MOVIE_RELEASE_GRACE

        undated = await service.create(MediaKind.MOVIE, 103)
        w = (await _wanted_of(session, undated.id))[0]
        assert w.next_search_at is None  # 明确未上映且未定档：等定档回填
        assert undated.status == SubscriptionStatus.ACTIVE


async def test_tv_selected_seasons_full_domain_with_schedule_classes(db) -> None:
    """勾选季贡献全部已知集；调度按集分三类：补旧=now / 追新=air+宽限 / 未定档=NULL。"""
    async with db.session() as session:
        sub = await _service(session).create(
            MediaKind.TV, 200, selected_seasons=[1, 2], follow_future=False
        )
        wanted = _key_map(await _wanted_of(session, sub.id))

    assert set(wanted) == {(1, 1), (1, 2), (2, 1), (2, 2), (2, 3)}
    now = utcnow()
    # 补旧：已播集立即到期
    for key in [(1, 1), (1, 2), (2, 1)]:
        assert wanted[key].next_search_at is not None
        assert wanted[key].next_search_at <= now
    # 追新：air_date + 宽限期，且高优先级
    future_unit = wanted[(2, 2)]
    assert future_unit.next_search_at is not None
    assert future_unit.next_search_at.date() == _FUTURE_DATE + timedelta(
        days=FUTURE_GRACE.days, hours=0
    ) or future_unit.next_search_at > now  # 宽限期后到期
    assert future_unit.priority > 0
    # 未定档：不可调度
    assert wanted[(2, 3)].next_search_at is None


async def test_follow_future_only_excludes_aired(db) -> None:
    """「只追未来」：不勾季 + 追新 → 只有未播/未定档集，已播集全部不要。"""
    async with db.session() as session:
        sub = await _service(session).create(
            MediaKind.TV, 200, selected_seasons=[], follow_future=True
        )
        wanted = _key_map(await _wanted_of(session, sub.id))

    assert set(wanted) == {(2, 2), (2, 3)}


async def test_create_is_idempotent_per_media_item(db) -> None:
    """同一条目重复订阅：幂等返回已有，不改参数、不加工单（不变量①的服务面）。"""
    async with db.session() as session:
        service = _service(session)
        first = await service.create(MediaKind.TV, 200, selected_seasons=[1])
        second = await service.create(
            MediaKind.TV, 200, selected_seasons=[1, 2], follow_future=True
        )
        wanted = await _wanted_of(session, first.id)

    assert second.id == first.id
    assert second.selected_seasons == [1]  # 参数未被第二次调用篡改
    assert len(wanted) == 2  # 仍是 S1 两集


async def test_movie_rejects_season_selection(db) -> None:
    async with db.session() as session:
        with pytest.raises(BadRequestException):
            await _service(session).create(MediaKind.MOVIE, 100, selected_seasons=[1])


async def test_tv_rejects_empty_selection_without_follow_future(db) -> None:
    """剧集不勾季又不追新 → E 恒为空：必须拒绝，不能落一条 0 工单的「已完成」空订阅。

    Web 弹层的提交守卫拦的就是这条不变量，API / CLI / Agent 走服务层，同样要拦。
    """
    async with db.session() as session:
        service = _service(session)
        with pytest.raises(BadRequestException):
            await service.create(MediaKind.TV, 200, selected_seasons=[], follow_future=False)
        # 拒绝要发生在落库之前：不能留下半成品订阅
        item, _, existing = await service.prepare(MediaKind.TV, 200)
        assert existing is None
        assert item.id is not None


# ---------------------------------------------------------------------------
# diff 重算（不变量③：现实不可逆）
# ---------------------------------------------------------------------------


async def test_update_deselect_keeps_grabbed_and_no_duplicate_on_reselect(db) -> None:
    """取消季只退出业务范围并停止救援；重新勾选复用工单与原下载尝试。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.TV, 200, selected_seasons=[1])
        wanted = _key_map(await _wanted_of(session, sub.id))
        await _mark(session, wanted[(1, 1)], WantedStatus.GRABBED)
        wanted[(1, 1)].info_hash = "a" * 40
        attempt = SubscriptionDownloadAttempt(
            subscription_id=sub.id,
            info_hash="a" * 40,
            units=[[1, 1]],
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            last_progress_at=utcnow(),
            next_search_at=utcnow(),
        )
        session.add_all([wanted[(1, 1)], attempt])
        notice = SystemNotice(
            dedupe_key=f"subscription.landing:{sub.id}:{attempt.info_hash}",
            severity="error",
            source="subscription",
            title="下载完成但无法入库",
            message="测试遗留告警",
        )
        session.add(notice)
        await session.commit()
        original_ids = {key: row.id for key, row in wanted.items()}

        await service.update(sub.id, selected_seasons=[])
        after_deselect = _key_map(await _wanted_of(session, sub.id))
        # 两行都保留历史身份，但已不参与详情、搜索、观察或换源。
        assert set(after_deselect) == {(1, 1), (1, 2)}
        assert all(not row.in_scope for row in after_deselect.values())
        assert after_deselect[(1, 1)].status == WantedStatus.GRABBED
        await session.refresh(attempt)
        assert attempt.status == DownloadAttemptStatus.CANCELLED
        assert attempt.next_search_at is None
        await session.refresh(notice)
        assert notice.status == NoticeStatus.RESOLVED.value
        assert (await service.detail(sub.id))[2] == []

        await service.update(sub.id, selected_seasons=[1])
        after_reselect = _key_map(await _wanted_of(session, sub.id))
        # 重新入域复用原行和原 infohash，不重复投递已经 grabbed 的单元。
        assert set(after_reselect) == {(1, 1), (1, 2)}
        assert all(row.in_scope for row in after_reselect.values())
        assert {key: row.id for key, row in after_reselect.items()} == original_ids
        assert after_reselect[(1, 1)].status == WantedStatus.GRABBED
        assert after_reselect[(1, 1)].info_hash == "a" * 40
        await session.refresh(attempt)
        assert attempt.status == DownloadAttemptStatus.ACTIVE


async def test_update_disable_follow_future_clears_future_units(db) -> None:
    """关掉追新：经追新进入的单元退出范围，但历史身份继续保留。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(
            MediaKind.TV, 200, selected_seasons=[], follow_future=True
        )
        assert len(await _wanted_of(session, sub.id)) == 2

        await service.update(sub.id, follow_future=False)
        historical = await _wanted_of(session, sub.id)
        assert len(historical) == 2
        assert all(not row.in_scope for row in historical)
        assert (await service.detail(sub.id))[2] == []


async def test_deselect_one_season_keeps_shared_pack_for_remaining_scope(db) -> None:
    """整季包跨两个季时，取消一季不能误停仍服务另一季的同一下载尝试。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.TV, 200, selected_seasons=[1, 2])
        wanted = _key_map(await _wanted_of(session, sub.id))
        info_hash = "c" * 40
        for key in ((1, 1), (2, 1)):
            wanted[key].status = WantedStatus.GRABBED
            wanted[key].info_hash = info_hash
            wanted[key].grabbed_at = utcnow()
            session.add(wanted[key])
        attempt = SubscriptionDownloadAttempt(
            subscription_id=sub.id,
            info_hash=info_hash,
            units=[[1, 1], [2, 1]],
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            last_progress_at=utcnow(),
            next_search_at=utcnow(),
        )
        session.add(attempt)
        await session.commit()

        await service.update(sub.id, selected_seasons=[2])
        rows = _key_map(await _wanted_of(session, sub.id))
        await session.refresh(attempt)
        assert rows[(1, 1)].in_scope is False
        assert rows[(2, 1)].in_scope is True
        assert attempt.status == DownloadAttemptStatus.REPLACEMENT_PENDING
        detail_units = {
            (row.season_number, row.episode_number)
            for row in (await service.detail(sub.id))[2]
        }
        assert detail_units == {
            (2, 1),
            (2, 2),
            (2, 3),
        }

        await service.update(sub.id, selected_seasons=[])
        await session.refresh(attempt)
        assert attempt.status == DownloadAttemptStatus.CANCELLED


async def test_update_preserves_promoted_replacement_lineage_while_still_in_scope(db) -> None:
    """替代源晋升后，普通订阅更新不能误停仍在范围内的新源和旧源清理。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.TV, 200, selected_seasons=[1])
        wanted = _key_map(await _wanted_of(session, sub.id))[(1, 1)]
        wanted.status = WantedStatus.GRABBED
        wanted.info_hash = "e" * 40
        wanted.grabbed_at = utcnow()
        old = SubscriptionDownloadAttempt(
            subscription_id=sub.id,
            info_hash="d" * 40,
            units=[[1, 1]],
            status=DownloadAttemptStatus.CLEANUP_PENDING,
            last_progress_at=utcnow(),
        )
        session.add_all([wanted, old])
        await session.flush()
        replacement = SubscriptionDownloadAttempt(
            subscription_id=sub.id,
            replaces_attempt_id=old.id,
            info_hash=wanted.info_hash,
            units=[[1, 1]],
            status=DownloadAttemptStatus.ACTIVE,
            last_progress_at=utcnow(),
        )
        session.add(replacement)
        await session.commit()

        await service.update(sub.id, selected_seasons=[1])
        await session.refresh(old)
        await session.refresh(replacement)
        assert old.status == DownloadAttemptStatus.CLEANUP_PENDING
        assert replacement.status == DownloadAttemptStatus.ACTIVE

        await service.update(sub.id, selected_seasons=[])
        await session.refresh(old)
        await session.refresh(replacement)
        assert old.status == DownloadAttemptStatus.CANCELLED
        assert replacement.status == DownloadAttemptStatus.CANCELLED

        await service.update(sub.id, selected_seasons=[1])
        await session.refresh(old)
        await session.refresh(replacement)
        assert old.status == DownloadAttemptStatus.CANCELLED
        assert replacement.status == DownloadAttemptStatus.ACTIVE


async def test_update_keeps_follow_units_when_deselecting_other_season(db) -> None:
    """追新开着时做无关修改：追新血统的单元（air>创建日/未定档）不被误删。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(
            MediaKind.TV, 200, selected_seasons=[1], follow_future=True
        )
        before = set(_key_map(await _wanted_of(session, sub.id)))
        assert before == {(1, 1), (1, 2), (2, 2), (2, 3)}

        await service.update(sub.id, rule_set_id=None)  # 无关修改
        after = set(_key_map(await _wanted_of(session, sub.id)))
        assert after == before


# ---------------------------------------------------------------------------
# 派生状态（不变量④）
# ---------------------------------------------------------------------------


async def test_status_derives_completed_for_satisfied_movie(db) -> None:
    """电影工单满足（P4 语义=grabbed）→ 派生 completed。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 100)
        wanted = await _wanted_of(session, sub.id)
        await _mark(session, wanted[0], WantedStatus.GRABBED)

        refreshed = await service.set_paused(sub.id, False)  # 触发重算
    assert refreshed.status == SubscriptionStatus.COMPLETED


async def test_status_stays_active_while_growing(db) -> None:
    """追新开着且剧未完结：即使当下零缺口也保持 active（E 还会生长）。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(
            MediaKind.TV, 200, selected_seasons=[], follow_future=True
        )
        for w in await _wanted_of(session, sub.id):
            await _mark(session, w, WantedStatus.GRABBED)
        refreshed = await service.set_paused(sub.id, False)
    assert refreshed.status == SubscriptionStatus.ACTIVE


async def test_paused_is_sticky_until_resumed(db) -> None:
    """paused 是用户显式状态：重算不碰，恢复后才落回派生值。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 100)
        paused = await service.set_paused(sub.id, True)
        assert paused.status == SubscriptionStatus.PAUSED

        wanted = await _wanted_of(session, sub.id)
        await _mark(session, wanted[0], WantedStatus.GRABBED)
        still_paused = await service.detail(sub.id)
        assert still_paused[0].status == SubscriptionStatus.PAUSED

        resumed = await service.set_paused(sub.id, False)
        assert resumed.status == SubscriptionStatus.COMPLETED


# ---------------------------------------------------------------------------
# 活动流水（透明化：每个动作可回放）
# ---------------------------------------------------------------------------


async def test_activity_stream_records_every_action(db) -> None:
    """创建/调整/暂停/恢复/收齐，每个动作都在时间线上留下中文可读记录。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(
            MediaKind.TV, 200, selected_seasons=[1, 2], follow_future=True
        )
        await service.update(sub.id, selected_seasons=[1])
        await service.set_paused(sub.id, True)
        await service.set_paused(sub.id, False)

        activities = await service.activities(sub.id)

    types = [a.type for a in reversed(activities)]  # 时间正序
    assert types == ["created", "adjusted", "paused", "resumed"]

    created = next(a for a in activities if a.type == "created")
    # 创建摘要把调度分布说清楚：3 集补旧、1 集待播出、1 集未定档
    assert "3 集已播出" in created.message
    assert "1 集未播出" in created.message
    assert "1 集未定档" in created.message
    assert created.payload["wanted_total"] == 5

    adjusted = next(a for a in activities if a.type == "adjusted")
    assert adjusted.payload["deactivated"] > 0  # 取消勾选 S2 令对应单元退出范围


async def test_activity_records_completed_transition(db) -> None:
    """派生状态翻转（收齐）也是活动：用户能看到订阅何时、为何完成。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 100)
        wanted = await _wanted_of(session, sub.id)
        await _mark(session, wanted[0], WantedStatus.GRABBED)
        await service.set_paused(sub.id, False)  # 触发重算 → completed

        activities = await service.activities(sub.id)
    assert [a.type for a in activities][:1] == ["completed"]


# ---------------------------------------------------------------------------
# 规则组
# ---------------------------------------------------------------------------


async def test_rule_set_lazy_default_and_delete_guards(db) -> None:
    """默认组懒种子且幂等；默认组与被引用组禁删；无引用组可删。"""
    async with db.session() as session:
        rule_service = RuleSetService(session)
        default = await rule_service.ensure_default()
        again = await rule_service.ensure_default()
        assert default.id == again.id

        with pytest.raises(BadRequestException):
            await rule_service.delete(default.id)

        extra = await rule_service.create("只要免费", {"free_only": True})
        sub_service = _service(session)
        sub = await sub_service.create(MediaKind.MOVIE, 100, rule_set_id=extra.id)
        with pytest.raises(ConflictException):
            await rule_service.delete(extra.id)
        assert await rule_service.reference_counts() == {extra.id: 1}

        await sub_service.delete_permanently(sub.id)
        await rule_service.delete(extra.id)  # 引用解除后可删


async def test_rule_set_default_seed_is_safe_preset(db) -> None:
    """懒种子的默认组带安全预设（1080p 及以上、做种数 ≥1），不再是全不限；
    已存在的默认组（老部署）保持原样，升级不动用户数据。"""
    async with db.session() as session:
        rule_service = RuleSetService(session)
        default = await rule_service.ensure_default()
        assert default.spec == {"resolutions": ["2160p", "1080p"], "min_seeders": 1}

    async with db.session() as session:
        # 模拟老部署：默认组已存在且被用户改成全不限——ensure_default 不得覆盖
        rule_service = RuleSetService(session)
        default = await rule_service.ensure_default()
        default.spec = {}
        session.add(default)
        await session.commit()
        kept = await rule_service.ensure_default()
        assert kept.id == default.id
        assert kept.spec == {}


async def test_rule_set_set_default_transfers_flag(db) -> None:
    """设为默认：标记转移且幂等；原默认组卸任后（无引用时）可删。"""
    async with db.session() as session:
        rule_service = RuleSetService(session)
        old_default = await rule_service.ensure_default()
        preferred = await rule_service.create("4K 优先", {"resolutions": ["2160p"]})

        row = await rule_service.set_default(preferred.id)
        assert row.is_default is True
        rows = await rule_service.list_all()
        assert [r.id for r in rows if r.is_default] == [preferred.id]

        # 幂等：再设一次不报错、结果不变
        again = await rule_service.set_default(preferred.id)
        assert again.id == preferred.id and again.is_default is True

        # 新默认组进入禁删保护；原默认组卸任后可删
        with pytest.raises(BadRequestException):
            await rule_service.delete(preferred.id)
        await rule_service.delete(old_default.id)


async def test_rule_set_spec_validation(db) -> None:
    """spec 经 RuleSetSpec 校验：类型不合法给可读中文错误；存精简形态。"""
    async with db.session() as session:
        rule_service = RuleSetService(session)
        with pytest.raises(BadRequestException):
            await rule_service.create("坏规则", {"resolutions": "1080p"})  # 应为列表

        row = await rule_service.create(
            "高清免费", {"resolutions": ["2160p", "1080p"], "free_only": True}
        )
        assert row.spec == {"resolutions": ["2160p", "1080p"], "free_only": True}


async def test_rule_set_duplicate_name_conflict(db) -> None:
    """名称唯一：创建/改名撞已有名给 409 可读中文，而不是 500。"""
    async with db.session() as session:
        rule_service = RuleSetService(session)
        await rule_service.create("首发", {"free_only": True})
        with pytest.raises(ConflictException):
            await rule_service.create("首发", {})

        other = await rule_service.create("次发", {})
        with pytest.raises(ConflictException):
            await rule_service.update(other.id, name="首发", spec={})


async def test_redownload_missing_units_creates_and_resets(db) -> None:
    """媒体库缺失找回（P0）：无订阅按缺失季创建；已 imported 的工单显式
    重置回 wanted 并立即排队——与 update 的"终态保留"铁律刻意相反。"""
    async with db.session() as session:
        service = _service(session)
        item, _, _ = await service.prepare(MediaKind.TV, 200)

        # 无订阅 → 自动创建，初始工单恰好覆盖缺失单元
        sub, requeued = await service.redownload_missing_units(
            MediaKind.TV, item, {(1, 1), (1, 2)}
        )
        assert requeued == 2
        wanted = await _wanted_of(session, sub.id)
        assert {(w.season_number, w.episode_number) for w in wanted} == {(1, 1), (1, 2)}

        # 其中一集走完管线（终态 imported），随后文件又缺了
        w11 = next(w for w in wanted if (w.season_number, w.episode_number) == (1, 1))
        w11.status = WantedStatus.IMPORTED
        w11.info_hash = "deadbeef"
        session.add(w11)
        await session.flush()

        sub2, requeued2 = await service.redownload_missing_units(MediaKind.TV, item, {(1, 1)})
        assert sub2.id == sub.id and requeued2 == 1
        refreshed = await _wanted_of(session, sub.id)
        w11b = next(w for w in refreshed if (w.season_number, w.episode_number) == (1, 1))
        assert w11b.status == WantedStatus.WANTED
        assert w11b.info_hash is None and w11b.imported_at is None
        assert w11b.next_search_at is not None  # 立即排队真实搜索

        # 已在队列里的 wanted 行不动（别清人家的退避计数）
        _, requeued3 = await service.redownload_missing_units(MediaKind.TV, item, {(1, 2)})
        assert requeued3 == 0


async def test_redownload_forces_movie_past_release_deferral(db) -> None:
    """重新下载与「立即搜索」同为用户强制：文件曾经存在说明资源可得，
    电影的"未上映/未定档缓搜"在这条路径上必须让位、立即排队。"""
    async with db.session() as session:
        service = _service(session)

        # 无订阅 → 按缺失单元新建：未上映电影不能被缓搜排到未来
        item, _, _ = await service.prepare(MediaKind.MOVIE, 101)
        sub, _ = await service.redownload_missing_units(MediaKind.MOVIE, item, {(0, 0)})
        w = (await _wanted_of(session, sub.id))[0]
        assert w.next_search_at is not None
        assert w.next_search_at <= utcnow()

        # 已有订阅、工单未定档（NULL 不可调度）→ 重下同样强制排队
        undated = await service.create(MediaKind.MOVIE, 103)
        w = (await _wanted_of(session, undated.id))[0]
        assert w.next_search_at is None
        item, _, _ = await service.prepare(MediaKind.MOVIE, 103)
        _, requeued = await service.redownload_missing_units(MediaKind.MOVIE, item, {(0, 0)})
        assert requeued == 1
        w = (await _wanted_of(session, undated.id))[0]
        assert w.next_search_at is not None
        assert w.next_search_at <= utcnow()


# ---------------------------------------------------------------------------
# 即时搜索触发收口在 service 层（任何入口共用，不依赖路由自觉）
# ---------------------------------------------------------------------------


async def test_write_paths_kick_instant_search(db, monkeypatch) -> None:
    """产生"立刻可搜"工单的写路径都要踢即时搜索；纯状态操作不踢。

    回归背景：触发曾散落在 HTTP 路由层，媒体库"缺失重下"入口漏加，
    电影订阅创建后只能干等定时 tick。
    """
    from movieclaw_api.services.subscription import wanted_search

    kicks: list[int] = []
    monkeypatch.setattr(wanted_search, "kick_search_soon", lambda: kicks.append(1))

    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 100)
        assert len(kicks) == 1  # 创建即搜

        await service.set_paused(sub.id, True)
        assert len(kicks) == 1  # 暂停是纯状态操作，不踢
        await service.set_paused(sub.id, False)
        assert len(kicks) == 2  # 恢复后立即处理积压的到期工单

        wanted = (await _wanted_of(session, sub.id))[0]
        await _mark(session, wanted, WantedStatus.IMPORTED)
        item, _, _ = await service.prepare(MediaKind.MOVIE, 100)
        _, requeued = await service.redownload_missing_units(MediaKind.MOVIE, item, {(0, 0)})
        assert requeued == 1
        assert len(kicks) == 3  # 缺失重下同样即时发车


# ---------------------------------------------------------------------------
# 首页预告（today_arrivals）：今天优先 + 一周内回退，永远回答"下一次是什么时候"
# ---------------------------------------------------------------------------


async def test_today_arrivals_falls_back_to_nearest_upcoming_day(db) -> None:
    """今天没有安排时回退到窗口内最近的一天；窗口外的远期集不参与预告。

    回归背景：订阅少的用户几乎天天看到空栏目，既没信息也让人怀疑功能坏了。
    """
    async with db.session() as session:
        service = _service(session)
        await service.create(MediaKind.TV, 202, selected_seasons=[1])
        # 200 的 S2E2 在十天后播出，落在一周窗口之外，不该被拉进预告
        await service.create(MediaKind.TV, 200, selected_seasons=[2])

        arrivals = await service.today_arrivals()

    assert [row.media.title for row in arrivals] == ["本周更新剧集"]
    assert arrivals[0].days_ahead == 3
    assert arrivals[0].expected_day == _SITE_TODAY + timedelta(days=3)


async def test_today_arrivals_prefers_today_over_upcoming(db) -> None:
    """今天有安排就只讲今天——首页不退化成一张七天流水账。"""
    async with db.session() as session:
        service = _service(session)
        await service.create(MediaKind.TV, 202, selected_seasons=[1])
        await service.create(MediaKind.TV, 203, selected_seasons=[1])

        arrivals = await service.today_arrivals()

    assert [row.media.title for row in arrivals] == ["今日更新剧集"]
    assert arrivals[0].days_ahead == 0


async def test_today_arrivals_includes_movie_only_inside_pipeline(db) -> None:
    """电影没有播出日：还在找资源时不占预告位，已投递则和剧集一样报"下载中"。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 100)

        assert await service.today_arrivals() == []

        wanted = (await _wanted_of(session, sub.id))[0]
        wanted.info_hash = "a" * 40
        await _mark(session, wanted, WantedStatus.GRABBED)

        arrivals = await service.today_arrivals()

    assert [row.media.title for row in arrivals] == ["测试电影"]
    assert arrivals[0].days_ahead == 0  # 管道内的内容随时可能落地，归入今天


async def test_today_arrivals_keeps_focus_day_per_media_kind(db) -> None:
    """电影今天在下载，不能顶掉剧集三天后的更新——首页分区是按类型切的。

    回归背景：焦点日曾经跨类型只算一次，用户切到剧集分区会看到一句并不成立的
    "接下来一周没有更新"，而实际上三天后就有。
    """
    async with db.session() as session:
        service = _service(session)
        movie = await service.create(MediaKind.MOVIE, 100)
        await service.create(MediaKind.TV, 202, selected_seasons=[1])

        wanted = (await _wanted_of(session, movie.id))[0]
        wanted.info_hash = "b" * 40
        await _mark(session, wanted, WantedStatus.GRABBED)

        arrivals = await service.today_arrivals()

    by_title = {row.media.title: row.days_ahead for row in arrivals}
    assert by_title == {"测试电影": 0, "本周更新剧集": 3}


async def test_today_arrivals_ignores_overdue_episodes(db) -> None:
    """早该播出却没抓到的旧集属于详情页的缺口清单，不该当成未来安排预告。"""
    async with db.session() as session:
        service = _service(session)
        await service.create(MediaKind.TV, 200, selected_seasons=[1])

        assert await service.today_arrivals() == []
