"""P4 管线的服务级测试：水位被动匹配、拒绝记录、整季包选优、认领竞态、
搜索退避、元数据刷新生长。全程 dry-run 投递（fixture 显式开启——真投递
默认已启用，这里只测匹配与状态机，不连站点与下载器）。

夹具剧集同 test_subscription_service：S1 两集已播；S2 = E1 昨播/E2 十天后/E3 未定档。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.subscription import ResourceTimingView
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.rule_sets import RuleSetService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_api.services.subscription.dispatch import dispatch
from movieclaw_api.services.subscription.matching import evaluate_and_dispatch
from movieclaw_api.services.torrent_matcher import process_new_torrents
from movieclaw_api.settings.store import init_setting_store, reset_setting_store
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadAttemptStatus,
    MediaItem,
    SiteTorrent,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    TorrentSource,
    WantedItem,
    WantedStatus,
)
from movieclaw_db.models.base import utcnow
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_downloader.models import SubmitResult
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import RuleVerdict, TorrentCandidate
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"

_TODAY = utcnow().date()
_AIRED = (_TODAY - timedelta(days=10)).isoformat()
_YESTERDAY = (_TODAY - timedelta(days=1)).isoformat()
_FUTURE = (_TODAY + timedelta(days=10)).isoformat()

_TV_ROUTES = {
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
}


_MOVIE_ROUTES = {
    # 电影上映感知调度夹具：未上映（30 天后）与未定档（制作中）
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
}


def _fake_tmdb(routes: dict) -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = routes.get(request.url.path)
        return httpx.Response(200 if payload else 404, json=payload or {})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pipe.db'}")
    # 本文件只测匹配与状态机，显式开 dry-run 隔离站点/下载器依赖
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "true")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    # 被动匹配水位走配置内核，测试环境同样要初始化配置存储
    init_setting_store()
    yield get_database()
    reset_setting_store()
    await dispose_db()
    get_settings.cache_clear()


def _service(session) -> SubscriptionService:
    return SubscriptionService(
        session, MediaLibraryService(session, _fake_tmdb({**_TV_ROUTES, **_MOVIE_ROUTES}))
    )


async def _insert_torrent(session, torrent_id: str, title: str, attrs: dict, **kw) -> SiteTorrent:
    row = SiteTorrent(
        site_id="testsite",
        torrent_id=torrent_id,
        title=title,
        subtitle=kw.pop("subtitle", ""),
        attrs=attrs,
        enrich_version=1,
        source=TorrentSource.LIST,
        seeders=kw.pop("seeders", 10),
        download_volume_factor=kw.pop("dvf", 0.0),
        is_free=kw.pop("is_free", True),
        **kw,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _wanted_map(session, sub_id: int) -> dict[tuple[int, int], WantedItem]:
    rows = (
        (await session.execute(select(WantedItem).where(WantedItem.subscription_id == sub_id)))
        .scalars()
        .all()
    )
    return {(w.season_number, w.episode_number): w for w in rows}


async def _activities(session, sub_id: int) -> list[SubscriptionActivity]:
    return list(
        (
            await session.execute(
                select(SubscriptionActivity)
                .where(SubscriptionActivity.subscription_id == sub_id)
                .order_by(SubscriptionActivity.id)
            )
        )
        .scalars()
        .all()
    )


_S1_PACK_ATTRS = {
    "media_type": "tv",
    "year": 2024,
    "seasons": [1],
    "episodes": [1, 2],
    "complete": True,
    "resolution": "2160p",
}


# ---------------------------------------------------------------------------
# F2 被动匹配：水位语义 + dry-run 投递闭环
# ---------------------------------------------------------------------------


async def test_watermark_skips_history_then_follows_new_torrents(db) -> None:
    """首跑水位初始化=当前最大 id（历史缓存不参与——铁律）；此后新种子被跟随匹配。"""
    async with db.session() as session:
        sub = await _service(session).create(
            MediaKind.TV, 200, selected_seasons=[1, 2], follow_future=True
        )
        await _insert_torrent(
            session, "hist", "Test Show S01 2160p WEB-DL 历史种子", _S1_PACK_ATTRS
        )

    await process_new_torrents()  # 首跑：只初始化水位
    published_at = utcnow() - timedelta(minutes=8)
    async with db.session() as session:
        wanted = await _wanted_map(session, sub.id)
        assert all(w.status == WantedStatus.WANTED for w in wanted.values())

        await _insert_torrent(
            session,
            "new1",
            "Test Show S01 2160p WEB-DL 新种子",
            _S1_PACK_ATTRS,
            publish_time=published_at,
        )

    await process_new_torrents()  # 二跑：跟随到新种子并投递
    async with db.session() as session:
        wanted = await _wanted_map(session, sub.id)
        assert wanted[(1, 1)].status == WantedStatus.GRABBED
        assert wanted[(1, 2)].status == WantedStatus.GRABBED
        assert wanted[(2, 1)].status == WantedStatus.WANTED  # 未被 S1 包覆盖

        grabbed = [a for a in await _activities(session, sub.id) if a.type == "grabbed"]
        assert len(grabbed) == 1
        assert "模拟投递" in grabbed[0].message
        assert grabbed[0].payload["dry_run"] is True
        assert sorted(grabbed[0].payload["units"]) == [[1, 1], [1, 2]]
        assert datetime.fromisoformat(grabbed[0].payload["resource_publish_time"]).replace(
            tzinfo=None
        ) == published_at
        first_seen = datetime.fromisoformat(
            grabbed[0].payload["resource_first_seen_at"]
        ).replace(tzinfo=None)
        submitted = datetime.fromisoformat(grabbed[0].payload["submitted_at"]).replace(
            tzinfo=None
        )
        assert published_at <= first_seen <= submitted

        # 一个整季包只落一条活动，但详情映射到它覆盖的每个工单；用户逐集都能
        # 看到同一份资源发布→索引→提交耗时。
        timings = await _service(session).resource_timings(sub.id)
        assert set(timings) >= {(1, 1), (1, 2)}
        assert timings[(1, 1)] == timings[(1, 2)]
        assert timings[(1, 1)]["publish_time"] == published_at
        timing_view = ResourceTimingView.from_snapshot(timings[(1, 1)])
        assert timing_view is not None
        assert timing_view.publish_to_seen_seconds is not None
        assert timing_view.seen_to_submit_seconds is not None
        summed = timing_view.publish_to_seen_seconds + timing_view.seen_to_submit_seconds
        assert abs(timing_view.publish_to_submit_seconds - summed) <= 1

        # 上线前的活动没有冻结键：仍可凭 site/torrent 从现存索引回补。
        grabbed[0].payload = {
            key: value
            for key, value in grabbed[0].payload.items()
            if key
            not in {"resource_publish_time", "resource_first_seen_at", "submitted_at"}
        }
        session.add(grabbed[0])
        await session.commit()
        legacy_timings = await _service(session).resource_timings(sub.id)
        assert legacy_timings[(1, 1)]["publish_time"] == published_at
        assert legacy_timings[(1, 1)]["first_seen_at"] == first_seen

    await process_new_torrents()  # 三跑：水位已推进，幂等无副作用
    async with db.session() as session:
        grabbed = [a for a in await _activities(session, sub.id) if a.type == "grabbed"]
        assert len(grabbed) == 1


async def test_corrupt_watermark_self_heals(db) -> None:
    """水位记录损坏（脏 JSON）时按首次运行自愈，而不是每 tick 永久报错。"""
    from sqlalchemy import text

    from movieclaw_api.services.torrent_matcher import MatchWatermark
    from movieclaw_api.settings.store import get_setting_store

    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO app_setting (namespace, value_json, created_at, updated_at)"
                " VALUES ('subscription.match_watermark', 'not-json',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        await session.commit()
        await _insert_torrent(session, "t1", "Test Show S01 2160p WEB-DL", _S1_PACK_ATTRS)

    await process_new_torrents()  # 不抛异常：脏行按首次运行处理并被覆盖

    get_setting_store().invalidate()  # 绕开缓存，从库里读回验证脏行已被修复
    watermark = await get_setting_store().get(MatchWatermark)
    assert watermark.last_id is not None and watermark.last_id >= 1


async def test_dispatch_derives_save_path_from_library(db) -> None:
    """投递目录三级兜底之②：库无监听规则时回落库推导条目目录（原地入库），
    GRABBED 活动的 message 与 payload 都带完整路径——dry-run 同样可见（L1.3）。"""
    async with db.session() as session:
        lib = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/media/tv"]
        )
        sub = await _service(session).create(
            MediaKind.TV, 200, selected_seasons=[1], library_id=lib.id
        )

    await process_new_torrents()  # 首跑：只初始化水位
    async with db.session() as session:
        await _insert_torrent(session, "libpack", "Test Show S01 2160p WEB-DL", _S1_PACK_ATTRS)

    await process_new_torrents()
    async with db.session() as session:
        grabbed = [a for a in await _activities(session, sub.id) if a.type == "grabbed"]
        assert len(grabbed) == 1
        assert "将直接下载到「剧集库」库内目录：/media/tv/测试剧集 (2024)" in grabbed[0].message
        assert grabbed[0].payload["library_id"] == lib.id
        assert grabbed[0].payload["save_path"] == "/media/tv/测试剧集 (2024)"
        # 无监听规则：实际投递目录就是条目目录，不再退下载器默认目录
        assert grabbed[0].payload["dispatch_dir"] == "/media/tv/测试剧集 (2024)"


async def test_subscription_rejects_kind_mismatched_library(db) -> None:
    """电影库不能作为剧集订阅的入库目标（类型校验，中文可读报错）。"""
    from movieclaw_api.exceptions import BadRequestException

    async with db.session() as session:
        movie_lib = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/media/movies"]
        )
        with pytest.raises(BadRequestException, match="类型不匹配"):
            await _service(session).create(
                MediaKind.TV, 200, selected_seasons=[1], library_id=movie_lib.id
            )


async def test_rule_rejection_logged_once_with_reason(db) -> None:
    """身份命中但规则拒绝：记一条中文原因活动；同一候选不重复刷屏。"""
    async with db.session() as session:
        rule = await RuleSetService(session).create("只要4K", {"resolutions": ["2160p"]})
        sub = await _service(session).create(
            MediaKind.TV, 200, selected_seasons=[1], rule_set_id=rule.id
        )
        row = await _insert_torrent(
            session,
            "lowres",
            "Test Show S01 720p WEB-DL",
            {**_S1_PACK_ATTRS, "resolution": "720p"},
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")
        await evaluate_and_dispatch(session, [row], source="被动匹配")  # 重复评估

        activities = await _activities(session, sub.id)
        rejected = [a for a in activities if a.type == "match_rejected"]
        assert len(rejected) == 1  # 去重生效
        assert "720p 不在允许范围" in rejected[0].message
        assert rejected[0].payload["reason_code"] == "resolution_not_allowed"

        wanted = await _wanted_map(session, sub.id)
        assert all(w.status == WantedStatus.WANTED for w in wanted.values())


async def test_pack_preferred_over_higher_scored_single(db) -> None:
    """同批出现单集与整季包：整季包优先（已确认决策），单集不再重复投。"""
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        single = await _insert_torrent(
            session,
            "single",
            "Test Show S01E01 2160p WEB-DL",
            {**_S1_PACK_ATTRS, "episodes": [1], "complete": None},
            seeders=500,
        )
        pack = await _insert_torrent(
            session, "pack", "Test Show S01 2160p WEB-DL", _S1_PACK_ATTRS, seeders=3
        )
        await evaluate_and_dispatch(session, [single, pack], source="被动匹配")

        grabbed = [a for a in await _activities(session, sub.id) if a.type == "grabbed"]
        assert len(grabbed) == 1
        assert grabbed[0].payload["torrent_id"] == "pack"
        assert sorted(grabbed[0].payload["units"]) == [[1, 1], [1, 2]]


async def test_dispatch_claim_race_second_caller_loses(db) -> None:
    """认领条件更新（防线①）：同一工单第二个投递方 0 行生效、直接放弃。"""
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        wanted = await _wanted_map(session, sub.id)
        item = (await _service(session).detail(sub.id))[1]
        candidate = TorrentCandidate(
            site_id="testsite",
            torrent_id="x",
            title="Test Show S01 2160p",
            subtitle="",
            attrs=TorrentAttrs.model_validate(_S1_PACK_ATTRS),
        )
        verdict = RuleVerdict(accepted=True, score=1)
        targets = [wanted[(1, 1)], wanted[(1, 2)]]

        first = await dispatch(
            session,
            subscription=sub,
            item=item,
            wanted_rows=targets,
            candidate=candidate,
            verdict=verdict,
            source="测试",
        )
        second = await dispatch(
            session,
            subscription=sub,
            item=item,
            wanted_rows=targets,
            candidate=candidate,
            verdict=verdict,
            source="测试",
        )
    assert first is True and second is False


async def test_real_dispatch_reusing_hash_merges_units_and_freezes_hash_in_activity(
    db, monkeypatch
) -> None:
    """同一种子后续认领新增单元时累计覆盖范围，活动保存精确 infohash。"""
    from importlib import import_module

    dispatch_mod = import_module("movieclaw_api.services.subscription.dispatch")

    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "false")
    get_settings.cache_clear()

    async def submit_same_hash(*args, **kwargs):
        return (
            SubmitResult(info_hash="a" * 40, name="Test Show S01", already_exists=False),
            SimpleNamespace(id=None),
        )

    monkeypatch.setattr(dispatch_mod, "_submit_real", submit_same_hash)
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        wanted = await _wanted_map(session, sub.id)
        item = (await _service(session).detail(sub.id))[1]
        candidate = TorrentCandidate(
            site_id="testsite",
            torrent_id="same-hash",
            title="Test Show S01 2160p WEB-DL",
            subtitle="",
            attrs=TorrentAttrs.model_validate(_S1_PACK_ATTRS),
        )
        verdict = RuleVerdict(accepted=True, score=1)

        assert await dispatch(
            session,
            subscription=sub,
            item=item,
            wanted_rows=[wanted[(1, 1)]],
            candidate=candidate,
            verdict=verdict,
            source="测试",
        )
        assert await dispatch(
            session,
            subscription=sub,
            item=item,
            wanted_rows=[wanted[(1, 2)]],
            candidate=candidate,
            verdict=verdict,
            source="测试",
        )

        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub.id
                )
            )
        ).scalar_one()
        assert sorted(attempt.units) == [[1, 1], [1, 2]]
        grabbed = [
            activity
            for activity in await _activities(session, sub.id)
            if activity.type == "grabbed"
        ]
        assert [activity.payload["info_hash"] for activity in grabbed] == ["a" * 40, "a" * 40]


async def test_real_dispatch_rechecks_scope_after_network_submit(db, monkeypatch) -> None:
    """取消订阅与网络提交撞车时保留真实 hash，但尝试必须直接停止，不能救援。"""
    from importlib import import_module

    dispatch_mod = import_module("movieclaw_api.services.subscription.dispatch")
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "false")
    get_settings.cache_clear()

    async def submit_after_scope_removed(*args, **kwargs):
        async with db.session() as concurrent:
            rows = list(
                (
                    await concurrent.execute(
                        select(WantedItem).where(WantedItem.status == WantedStatus.GRABBED)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.in_scope = False
                concurrent.add(row)
            await concurrent.commit()
        return (
            SubmitResult(info_hash="b" * 40, name="Concurrent.S01", already_exists=False),
            SimpleNamespace(id=None),
        )

    monkeypatch.setattr(dispatch_mod, "_submit_real", submit_after_scope_removed)
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        wanted = await _wanted_map(session, sub.id)
        item = (await _service(session).detail(sub.id))[1]
        candidate = TorrentCandidate(
            site_id="testsite",
            torrent_id="scope-race",
            title="Test Show S01E01 2160p WEB-DL",
            subtitle="",
            attrs=TorrentAttrs.model_validate(
                {**_S1_PACK_ATTRS, "episodes": [1], "complete": None}
            ),
        )

        assert await dispatch(
            session,
            subscription=sub,
            item=item,
            wanted_rows=[wanted[(1, 1)]],
            candidate=candidate,
            verdict=RuleVerdict(accepted=True, score=1),
            source="并发测试",
        )
        await session.refresh(wanted[(1, 1)])
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub.id
                )
            )
        ).scalar_one()

        assert wanted[(1, 1)].info_hash == "b" * 40
        assert wanted[(1, 1)].in_scope is False
        assert attempt.status == DownloadAttemptStatus.CANCELLED
        assert attempt.next_search_at is None


async def test_failed_dispatch_does_not_restore_backoff_after_scope_cancel(
    db, monkeypatch
) -> None:
    """投递失败与取消撞车时回滚认领，但退出范围的工单不能重新排搜索。"""
    from importlib import import_module

    dispatch_mod = import_module("movieclaw_api.services.subscription.dispatch")
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "false")
    get_settings.cache_clear()

    async def fail_after_scope_removed(*args, **kwargs):
        async with db.session() as concurrent:
            row = (
                await concurrent.execute(
                    select(WantedItem).where(WantedItem.status == WantedStatus.GRABBED)
                )
            ).scalar_one()
            row.in_scope = False
            concurrent.add(row)
            await concurrent.commit()
        raise RuntimeError("模拟下载器拒绝")

    monkeypatch.setattr(dispatch_mod, "_submit_real", fail_after_scope_removed)
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        wanted = await _wanted_map(session, sub.id)
        item = (await _service(session).detail(sub.id))[1]
        candidate = TorrentCandidate(
            site_id="testsite",
            torrent_id="failed-scope-race",
            title="Test Show S01E01 2160p WEB-DL",
            subtitle="",
            attrs=TorrentAttrs.model_validate(
                {**_S1_PACK_ATTRS, "episodes": [1], "complete": None}
            ),
        )

        assert not await dispatch(
            session,
            subscription=sub,
            item=item,
            wanted_rows=[wanted[(1, 1)]],
            candidate=candidate,
            verdict=RuleVerdict(accepted=True, score=1),
            source="失败并发测试",
        )
        await session.refresh(wanted[(1, 1)])
        assert wanted[(1, 1)].in_scope is False
        assert wanted[(1, 1)].status == WantedStatus.WANTED
        assert wanted[(1, 1)].next_search_at is None


async def test_pack_covers_only_aired_by_publish_time(db) -> None:
    """真实教训回归：在播季的整季包只能满足"种子发布时已播出"的集。
    S2 = E1 昨播 / E2 十天后 / E3 未定档：今天发布的 S2 整季包只覆盖 E1，
    未播与未定档保持 wanted，订阅不得误判收齐。"""
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[2])
        pack = await _insert_torrent(
            session,
            "s2pack",
            "Test Show S02 2160p WEB-DL",
            {"media_type": "tv", "year": 2026, "seasons": [2], "resolution": "2160p"},
            publish_time=utcnow(),
        )
        await evaluate_and_dispatch(session, [pack], source="被动匹配")

        wanted = await _wanted_map(session, sub.id)
        assert wanted[(2, 1)].status == WantedStatus.GRABBED  # 已播：可满足
        assert wanted[(2, 2)].status == WantedStatus.WANTED  # 未播：物理上不可能在包里
        assert wanted[(2, 3)].status == WantedStatus.WANTED  # 未定档：无证据不覆盖


async def _proven_missing_attempt(session, sub_id: int, units: list[list[int]], source) -> None:
    """写入内容核验的负面记忆：这份发布下完后被证明不含 ``units``。"""
    session.add(
        SubscriptionDownloadAttempt(
            subscription_id=sub_id,
            info_hash="a" * 40,
            site_id=source[0],
            torrent_id=source[1],
            units=units,
            last_progress_at=utcnow(),
            status=DownloadAttemptStatus.COMPLETED,
            content_missing={"units": units, "sources": [list(source)]},
        )
    )
    await session.commit()


async def test_proven_missing_release_not_grabbed_again(db) -> None:
    """真实教训回归：全集包下完后被证明不含某一集，退回重找时又选中同一个种子
    ——它还在下载器里且已完成，于是秒完成 → 再核验 → 再退回，无限循环。
    证伪过的发布不再参与该集选种。"""
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        await _proven_missing_attempt(session, sub.id, [[1, 1], [1, 2]], ("testsite", "pack"))
        pack = await _insert_torrent(
            session, "pack", "Test Show S01 2160p WEB-DL", _S1_PACK_ATTRS
        )
        await evaluate_and_dispatch(session, [pack], source="主动搜索")

        wanted = await _wanted_map(session, sub.id)
        assert all(w.status == WantedStatus.WANTED for w in wanted.values())
        assert [a for a in await _activities(session, sub.id) if a.type == "grabbed"] == []


async def test_proven_missing_is_per_unit_not_per_release(db) -> None:
    """负面记忆按集记：包里确实存在的集照常投递，只有被证伪的那一集被跳过。"""
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        await _proven_missing_attempt(session, sub.id, [[1, 2]], ("testsite", "pack"))
        pack = await _insert_torrent(
            session, "pack", "Test Show S01 2160p WEB-DL", _S1_PACK_ATTRS
        )
        await evaluate_and_dispatch(session, [pack], source="主动搜索")

        wanted = await _wanted_map(session, sub.id)
        assert wanted[(1, 1)].status == WantedStatus.GRABBED
        assert wanted[(1, 2)].status == WantedStatus.WANTED


async def test_proven_missing_does_not_block_other_releases(db) -> None:
    """换一个发布仍可满足该集：负面记忆只针对被证伪的那份内容。"""
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        await _proven_missing_attempt(session, sub.id, [[1, 1], [1, 2]], ("testsite", "pack"))
        other = await _insert_torrent(
            session, "other", "Test Show S01 2160p WEB-DL-OTHER", _S1_PACK_ATTRS
        )
        await evaluate_and_dispatch(session, [other], source="主动搜索")

        wanted = await _wanted_map(session, sub.id)
        assert all(w.status == WantedStatus.GRABBED for w in wanted.values())


async def test_old_pack_cannot_cover_future_show(db) -> None:
    """发布时间早于所有集播出日期的整季包（同名他剧的典型形态）：覆盖为零，
    整次投递不发生。"""
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1, 2])
        old_pack = await _insert_torrent(
            session,
            "oldpack",
            "Test Show S01 2160p WEB-DL",
            {"media_type": "tv", "year": 2025, "seasons": [1], "resolution": "2160p"},
            publish_time=utcnow() - timedelta(days=400),  # 早于夹具所有集的播出日
        )
        await evaluate_and_dispatch(session, [old_pack], source="被动匹配")

        wanted = await _wanted_map(session, sub.id)
        assert all(w.status == WantedStatus.WANTED for w in wanted.values())


async def test_non_video_category_never_matches(db) -> None:
    """真实教训回归：《霸王别姬》原声专辑（标题含英文名+年份精确）曾胜出投递。
    站点分类明确为 music/game/av 的资源必须在粗筛剔除，进不了内核。"""
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        soundtrack = await _insert_torrent(
            session,
            "ost",
            "原声大碟 - Test Show S01 2024 APE 整轨",
            {"year": 2024, "seasons": [1], "complete": True},
            category="music",
            seeders=999,
        )
        await evaluate_and_dispatch(session, [soundtrack], source="被动匹配")

        wanted = await _wanted_map(session, sub.id)
        assert all(w.status == WantedStatus.WANTED for w in wanted.values())
        activities = await _activities(session, sub.id)
        assert all(a.type == "created" for a in activities)  # 连拒绝记录都不该有


# ---------------------------------------------------------------------------
# F4 主动搜索：失败短冷却 / 未果退避 / 命中投递
# ---------------------------------------------------------------------------


def _fake_search(monkeypatch, *, sites_ok: int, hits: list, calls: list | None = None) -> None:
    from movieclaw_api.schemas.search import SearchResponse, SiteSearchStatus
    from movieclaw_api.services import site_search

    async def fake(
        keyword, categories=None, site_ids=None, label=None, page=1, exclude_protected=False
    ):
        if calls is not None:
            calls.append({"keyword": keyword, "categories": categories})
        statuses = [
            SiteSearchStatus(site_id=f"s{i}", site_name=f"站{i}", count=len(hits))
            for i in range(sites_ok)
        ]
        if sites_ok == 0:
            statuses = [
                SiteSearchStatus(site_id="s0", site_name="站0", count=0, error="站点访问失败")
            ]
        return SearchResponse(
            keyword=keyword,
            label=label,
            categories=[],
            total=len(hits),
            items=hits,
            sites=statuses,
        )

    monkeypatch.setattr(site_search, "search_all_sites", fake)


async def test_search_failure_short_retry_without_attempt(db, monkeypatch) -> None:
    """搜索本身失败：短冷却重试、不计退避档，活动如实解释。"""
    from movieclaw_api.services.subscription.wanted_search import search_wanted

    _fake_search(monkeypatch, sites_ok=0, hits=[])
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])

    await search_wanted()
    async with db.session() as session:
        wanted = await _wanted_map(session, sub.id)
        for w in wanted.values():
            assert w.search_attempts == 0
            assert w.next_search_at > utcnow()  # 已顺延
        searched = [a for a in await _activities(session, sub.id) if a.type == "searched"]
        assert len(searched) == 1
        assert "未能执行" in searched[0].message


async def test_search_no_result_backs_off_with_attempt(db, monkeypatch) -> None:
    """搜索成功但无结果：计一次尝试、进退避曲线首档；且按订阅类型带分类过滤。"""
    from movieclaw_api.services.subscription.wanted_search import search_wanted
    from movieclaw_tracker.models import TorrentCategory

    calls: list = []
    _fake_search(monkeypatch, sites_ok=2, hits=[], calls=calls)
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])

    await search_wanted()
    async with db.session() as session:
        wanted = await _wanted_map(session, sub.id)
        for w in wanted.values():
            assert w.search_attempts == 1
            assert w.last_search_at is not None
        searched = [a for a in await _activities(session, sub.id) if a.type == "searched"]
        assert "2 个站点返回 0 个结果" in searched[0].message

    # 剧集订阅的搜索必须带分类收窄（剧集/纪录片/动漫），不带 music/game/av 噪音
    assert calls and calls[0]["categories"] == [
        TorrentCategory.TV,
        TorrentCategory.DOCUMENTARY,
        TorrentCategory.ANIME,
    ]


async def test_search_hit_persists_and_dispatches(db, monkeypatch) -> None:
    """搜索命中：结果落库（source=SEARCH）→ 共享管道投递 → 活动记全链路数字。"""
    from movieclaw_api.schemas.search import TorrentHit
    from movieclaw_api.services.subscription.wanted_search import search_wanted

    hit = TorrentHit(
        site_id="testsite",
        site_name="测试站",
        torrent_id="found1",
        title="Test Show S01 2160p WEB-DL Complete",
        subtitle="测试剧集 全2集",
        seeders=8,
        download_volume_factor=0.0,
        free=True,
        attrs=TorrentAttrs.model_validate(_S1_PACK_ATTRS),
    )
    _fake_search(monkeypatch, sites_ok=2, hits=[hit])
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])

    await search_wanted()
    async with db.session() as session:
        wanted = await _wanted_map(session, sub.id)
        assert wanted[(1, 1)].status == WantedStatus.GRABBED
        assert wanted[(1, 2)].status == WantedStatus.GRABBED

        persisted = (
            await session.execute(select(SiteTorrent).where(SiteTorrent.torrent_id == "found1"))
        ).scalar_one()
        assert persisted.source == TorrentSource.SEARCH  # 副产品沉淀进公共缓存

        searched = [a for a in await _activities(session, sub.id) if a.type == "searched"]
        assert "投递覆盖 2 个单元" in searched[0].message


async def test_movie_forced_search_restores_release_schedule(db, monkeypatch) -> None:
    """强制搜索未上映电影无果后，退避地板把调度恢复到"上映 + 宽限"，
    不落进 15 分钟起步的退避曲线在明知没资源的窗口里反复空搜。"""
    from movieclaw_api.services.subscription import MOVIE_RELEASE_GRACE
    from movieclaw_api.services.subscription.wanted_search import search_wanted

    _fake_search(monkeypatch, sites_ok=2, hits=[])
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 101)
        await service.search_now(sub.id)  # 用户强制：未上映也搜一次

    await search_wanted()
    async with db.session() as session:
        w = list((await _wanted_map(session, sub.id)).values())[0]
        assert w.search_attempts == 1  # 强制的这次真实搜索照常记账
        assert w.next_search_at is not None
        assert w.next_search_at.date() == _TODAY + timedelta(days=30) + MOVIE_RELEASE_GRACE


async def test_movie_forced_search_on_undated_returns_to_unschedulable(db, monkeypatch) -> None:
    """未定档电影强制搜索无果后回到不可调度（NULL），等定档回填，不进退避循环。"""
    from movieclaw_api.services.subscription.wanted_search import search_wanted

    _fake_search(monkeypatch, sites_ok=2, hits=[])
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 103)
        await service.search_now(sub.id)

    await search_wanted()
    async with db.session() as session:
        w = list((await _wanted_map(session, sub.id)).values())[0]
        assert w.search_attempts == 1
        assert w.next_search_at is None


# ---------------------------------------------------------------------------
# F3 元数据刷新：新集生长 + 定档回填
# ---------------------------------------------------------------------------


async def test_refresh_grows_new_episode_and_schedules_dated(db, monkeypatch) -> None:
    """刷新发现新集 → 追新订阅补工单 + 活动；未定档集定档 → 回填调度。"""
    from movieclaw_api.services import media_refresh

    async with db.session() as session:
        sub = await _service(session).create(
            MediaKind.TV, 200, selected_seasons=[1], follow_future=True
        )
        before = await _wanted_map(session, sub.id)
        assert (2, 4) not in before
        assert before[(2, 3)].next_search_at is None  # 未定档

    updated_routes = {
        **_TV_ROUTES,
        "/3/tv/200/season/2": {
            "name": "第 2 季",
            "air_date": _YESTERDAY,
            "episodes": [
                {"episode_number": 1, "name": "E1", "air_date": _YESTERDAY},
                {"episode_number": 2, "name": "E2", "air_date": _FUTURE},
                {"episode_number": 3, "name": "E3", "air_date": _FUTURE},  # 定档了
                {"episode_number": 4, "name": "E4", "air_date": _FUTURE},  # 新集
            ],
        },
    }
    # 刷新管线已合流进刮削服务（media_scrape），TMDB client 在 media_discover 取
    from movieclaw_api.services import media_discover

    monkeypatch.setattr(media_discover, "get_tmdb_client", lambda: _fake_tmdb(updated_routes))

    await media_refresh.refresh_media_metadata()
    async with db.session() as session:
        wanted = await _wanted_map(session, sub.id)
        assert (2, 4) in wanted  # 新集已生长
        assert wanted[(2, 3)].next_search_at is not None  # 定档回填调度

        activities = await _activities(session, sub.id)
        added = [a for a in activities if a.type == "wanted_added"]
        assert len(added) == 1
        assert "1 个新集" in added[0].message

        from movieclaw_db.models import MediaItem

        item = (
            await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 200))
        ).scalar_one()
        assert item.next_refresh_at is not None  # 分档排期已写回


async def test_refresh_backfills_movie_release_schedule(db, monkeypatch) -> None:
    """电影的定档回填：未定档订阅（NULL 不可调度）在 TMDB 档期出现后，
    刷新把哨兵工单调度对齐到"上映 + 宽限"，并落一条可读活动。"""
    from movieclaw_api.services import media_discover, media_refresh
    from movieclaw_api.services.subscription import MOVIE_RELEASE_GRACE

    async with db.session() as session:
        sub = await _service(session).create(MediaKind.MOVIE, 103)
        w = list((await _wanted_map(session, sub.id)).values())[0]
        assert w.next_search_at is None  # 未定档：不可调度

    release = _TODAY + timedelta(days=20)
    updated_routes = {
        **_TV_ROUTES,
        **_MOVIE_ROUTES,
        "/3/movie/103": {
            **_MOVIE_ROUTES["/3/movie/103"],
            "release_date": release.isoformat(),
            "status": "Post Production",
        },
    }
    monkeypatch.setattr(media_discover, "get_tmdb_client", lambda: _fake_tmdb(updated_routes))

    await media_refresh.refresh_media_metadata()
    async with db.session() as session:
        w = list((await _wanted_map(session, sub.id)).values())[0]
        assert w.next_search_at is not None  # 定档回填调度
        assert w.next_search_at.date() == release + MOVIE_RELEASE_GRACE
        assert w.priority > 0

        adjusted = [a for a in await _activities(session, sub.id) if a.type == "adjusted"]
        assert any("档期更新" in a.message for a in adjusted)


async def test_refresh_keeps_user_forced_search_pending(db, monkeypatch) -> None:
    """定档回填不得回撤用户已触发、尚未执行的强制搜索：next<=now 是等待
    搜索管线消费的排队信号，刷新往未来/NULL 改写会把强制悄悄吞掉。"""
    from movieclaw_api.services import media_discover, media_refresh

    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 101)  # 未上映：排在上映+宽限
        await service.search_now(sub.id)  # 用户强制：next 清零到当下
        forced_at = list((await _wanted_map(session, sub.id)).values())[0].next_search_at
        assert forced_at is not None and forced_at <= utcnow()

    monkeypatch.setattr(
        media_discover, "get_tmdb_client", lambda: _fake_tmdb({**_TV_ROUTES, **_MOVIE_ROUTES})
    )
    await media_refresh.refresh_media_metadata()
    async with db.session() as session:
        w = list((await _wanted_map(session, sub.id)).values())[0]
        assert w.next_search_at == forced_at  # 强制仍在排队，未被改回未来档期


# ---------------------------------------------------------------------------
# 身份证据：ID 反证、证据强度选优、证据落台账
# （docs/design/identity-confidence.md §5）
# ---------------------------------------------------------------------------


async def _set_item_imdb(session, sub, imdb_id: str) -> None:
    """给夹具条目补一个 IMDb 身份（TMDB 假实现不带这个字段）。"""
    from movieclaw_db.models import MediaItem

    row = await session.get(MediaItem, sub.media_item_id)
    row.imdb_id = imdb_id
    await session.commit()


async def test_conflicting_site_imdb_is_rejected_with_an_explanation(db) -> None:
    """站点标注的 IMDb 与条目不符 → 不投递，且必须留下能读懂的拒绝理由。

    静默拒绝会把漏配变成查不出原因的哑巴故障：站点的 IMDb 是上传者手填的，
    填错真实存在，用户看到理由才可能判断"这是站点标错了"并手动选种。
    """
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        await _set_item_imdb(session, sub, "tt32138219")
        row = await _insert_torrent(
            session,
            "wrongimdb",
            "Test Show S01 2160p WEB-DL",
            {"media_type": "tv", "year": 2025, "seasons": [1], "resolution": "2160p"},
            imdb_id="tt3559656",
            seeders=999,
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        wanted = await _wanted_map(session, sub.id)
        assert all(w.status == WantedStatus.WANTED for w in wanted.values())
        rejected = [a for a in await _activities(session, sub.id) if a.type == "match_rejected"]
        assert len(rejected) == 1
        assert rejected[0].payload["reason_code"] == "identity_id_conflict"
        assert "tt3559656" in rejected[0].message and "tt32138219" in rejected[0].message


async def test_id_backed_candidate_beats_a_higher_seeded_guess(db, monkeypatch) -> None:
    """选优先看身份证据：IMDb 命中的候选赢过做种数高得多、只靠片名蒙的候选。

    旧排序里做种数能压过身份证据，这正是同名同年错配能走到投递的原因之一。
    """
    from importlib import import_module

    dispatch_mod = import_module("movieclaw_api.services.subscription.dispatch")
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "false")
    get_settings.cache_clear()

    async def submit(*args, **kwargs):
        return (
            SubmitResult(info_hash="b" * 40, name="Test Show S01", already_exists=False),
            SimpleNamespace(id=None),
        )

    monkeypatch.setattr(dispatch_mod, "_submit_real", submit)
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        await _set_item_imdb(session, sub, "tt32138219")
        attrs = {"media_type": "tv", "year": 2025, "seasons": [1], "resolution": "2160p"}
        popular_guess = await _insert_torrent(
            session, "guess", "Test Show S01 2160p WEB-DL", attrs, seeders=999
        )
        id_backed = await _insert_torrent(
            session,
            "idbacked",
            "Test Show S01 2160p WEB-DL",
            attrs,
            imdb_id="tt32138219",
            seeders=1,
        )
        await evaluate_and_dispatch(session, [popular_guess, id_backed], source="被动匹配")

        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub.id
                )
            )
        ).scalar_one()
        assert attempt.torrent_id == "idbacked"
        # 证据强度同时落进台账，入库时 info_hash 认领据此分级
        assert attempt.identity_confidence == "exact_id"


async def test_dispatch_records_the_identity_evidence(db, monkeypatch) -> None:
    """只靠片名+年份认下来的投递，台账要如实记成 title_year 并留下命中的别名。"""
    from importlib import import_module

    dispatch_mod = import_module("movieclaw_api.services.subscription.dispatch")
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "false")
    get_settings.cache_clear()

    async def submit(*args, **kwargs):
        return (
            SubmitResult(info_hash="c" * 40, name="Test Show S01", already_exists=False),
            SimpleNamespace(id=None),
        )

    monkeypatch.setattr(dispatch_mod, "_submit_real", submit)
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        row = await _insert_torrent(
            session,
            "guessonly",
            "Test Show S01 2160p WEB-DL",
            {"media_type": "tv", "year": 2025, "seasons": [1], "resolution": "2160p"},
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub.id
                )
            )
        ).scalar_one()
        assert attempt.identity_confidence == "title_year"
        assert attempt.matched_alias == "Test Show"


async def _set_runtime(session, sub, runtime_minutes: int) -> None:
    """给夹具条目补片长（TMDB 假实现不带 runtime；建订阅时元数据行已建好）。"""
    from movieclaw_db.models import MediaMetadata

    row = (
        await session.execute(
            select(MediaMetadata).where(MediaMetadata.media_item_id == sub.media_item_id)
        )
    ).scalar_one()
    row.runtime_minutes = runtime_minutes
    await session.commit()


async def test_bitrate_counter_evidence_is_recorded_but_does_not_block(db, monkeypatch) -> None:
    """体积÷片长 反证走 shadow 模式：照常投递，只把判定记进投递活动 payload。

    阈值是凭经验拍的，直接开成否决会误伤正常发布（identity-confidence.md §10.2）。
    先在真实流量上攒触发率与误报率，再决定是否生效——所以这个用例同时钉死
    两件事：**记录发生了**，且**行为没有变**。
    """
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 101)
        await _set_runtime(session, sub, 120)

        # 0.2 GB 的"1080p 电影"：隐含码率约 0.24 Mbps，预告片体量
        row = await _insert_torrent(
            session,
            "tiny",
            "Upcoming Movie 2026 1080p WEB-DL",
            {"media_type": "movie", "year": 2026, "resolution": "1080p"},
            size_bytes=int(0.2 * 1024**3),
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        wanted = await _wanted_map(session, sub.id)
        assert wanted[(0, 0)].status == WantedStatus.GRABBED  # 行为未变：照常投递
        grabbed = [a for a in await _activities(session, sub.id) if a.type == "grabbed"]
        assert len(grabbed) == 1
        assert "bitrate_reject" in grabbed[0].payload["shadow"]
        # shadow 判定绝不进用户可见的文案
        assert "Mbps" not in grabbed[0].message


async def test_normal_sized_release_records_no_shadow_note(db) -> None:
    """正常体积的发布不留 shadow 记录——否则统计触发率时全是噪音。"""
    async with db.session() as session:
        service = _service(session)
        sub = await service.create(MediaKind.MOVIE, 101)
        await _set_runtime(session, sub, 120)

        row = await _insert_torrent(
            session,
            "normal",
            "Upcoming Movie 2026 1080p WEB-DL",
            {"media_type": "movie", "year": 2026, "resolution": "1080p"},
            size_bytes=int(6 * 1024**3),  # ≈ 7.2 Mbps，完全正常
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        grabbed = [a for a in await _activities(session, sub.id) if a.type == "grabbed"]
        assert len(grabbed) == 1 and "shadow" not in grabbed[0].payload


# ---------------------------------------------------------------------------
# 投递前的外部 ID 复核（identity-confidence.md §7）
# ---------------------------------------------------------------------------


def _fake_detail(monkeypatch, *, imdb_id=None, douban_id=None, calls=None, boom=False):
    """替掉「按站点取已认证客户端 → 拉详情页」这一步，不连真实站点。"""
    from movieclaw_api.services.subscription import identity_recheck as mod

    class _Site:
        async def get_torrent_detail(self, target):
            if calls is not None:
                calls.append(target)
            if boom:
                raise RuntimeError("站点超时")
            return SimpleNamespace(imdb_id=imdb_id, douban_id=douban_id)

    class _Access:
        async def get(self, site_id):
            return _Site()

    monkeypatch.setattr(mod, "get_site_access", lambda: _Access(), raising=False)
    import movieclaw_api.services.site_access as access_mod

    monkeypatch.setattr(access_mod, "get_site_access", lambda: _Access())


async def _movie_sub_with_imdb(session, imdb_id: str):
    sub = await _service(session).create(MediaKind.MOVIE, 101)
    await _set_item_imdb(session, sub, imdb_id)
    return sub


async def test_pre_dispatch_recheck_blocks_a_torrent_the_site_says_is_another_film(
    db, monkeypatch
) -> None:
    """站点详情页标的 IMDb 和条目不符 → 不投递，理由写明双方编号。

    这是 §0 那个错配的正面拦截：两部片同名同年，片名和年份都区分不了，但站点
    详情页上的 IMDb 编号可以。
    """
    _fake_detail(monkeypatch, imdb_id="tt3559656")
    async with db.session() as session:
        sub = await _movie_sub_with_imdb(session, "tt32138219")
        row = await _insert_torrent(
            session,
            "twin",
            "Upcoming Movie 2026 1080p WEB-DL",
            {"media_type": "movie", "year": 2026, "resolution": "1080p"},
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        wanted = await _wanted_map(session, sub.id)
        assert wanted[(0, 0)].status == WantedStatus.WANTED  # 没投出去
        rejected = [a for a in await _activities(session, sub.id) if a.type == "match_rejected"]
        assert len(rejected) == 1
        assert "tt3559656" in rejected[0].message and "tt32138219" in rejected[0].message


async def test_pre_dispatch_recheck_confirms_and_upgrades_the_evidence(db, monkeypatch) -> None:
    """详情页 IMDb 与条目一致 → 照常投递，且台账记成 exact_id；ID 回填种子索引。"""
    from importlib import import_module

    dispatch_mod = import_module("movieclaw_api.services.subscription.dispatch")
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "false")
    get_settings.cache_clear()

    async def submit(*args, **kwargs):
        return (
            SubmitResult(info_hash="d" * 40, name="Upcoming Movie", already_exists=False),
            SimpleNamespace(id=None),
        )

    monkeypatch.setattr(dispatch_mod, "_submit_real", submit)
    _fake_detail(monkeypatch, imdb_id="tt32138219")
    async with db.session() as session:
        sub = await _movie_sub_with_imdb(session, "tt32138219")
        row = await _insert_torrent(
            session,
            "confirmed",
            "Upcoming Movie 2026 1080p WEB-DL",
            {"media_type": "movie", "year": 2026, "resolution": "1080p"},
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        wanted = await _wanted_map(session, sub.id)
        assert wanted[(0, 0)].status == WantedStatus.GRABBED
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub.id
                )
            )
        ).scalar_one()
        assert attempt.identity_confidence == "exact_id"
        # 回填是关键：被动匹配下次遇到同一行直接走信号一，不必再拉详情页
        refreshed = await session.get(SiteTorrent, row.id)
        assert refreshed.imdb_id == "tt32138219"


async def test_pre_dispatch_recheck_passes_when_the_site_has_no_id(db, monkeypatch) -> None:
    """站点没标 IMDb：当作没有这条证据，照常投递（不能因为查不到就不下）。"""
    _fake_detail(monkeypatch, imdb_id=None)
    async with db.session() as session:
        sub = await _movie_sub_with_imdb(session, "tt32138219")
        row = await _insert_torrent(
            session,
            "noid",
            "Upcoming Movie 2026 1080p WEB-DL",
            {"media_type": "movie", "year": 2026, "resolution": "1080p"},
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        assert (await _wanted_map(session, sub.id))[(0, 0)].status == WantedStatus.GRABBED


async def test_pre_dispatch_recheck_never_blocks_on_a_site_failure(db, monkeypatch) -> None:
    """详情页请求失败一律放行——绝不让一次网络抖动卡死投递。"""
    _fake_detail(monkeypatch, boom=True)
    async with db.session() as session:
        sub = await _movie_sub_with_imdb(session, "tt32138219")
        row = await _insert_torrent(
            session,
            "boom",
            "Upcoming Movie 2026 1080p WEB-DL",
            {"media_type": "movie", "year": 2026, "resolution": "1080p"},
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        assert (await _wanted_map(session, sub.id))[(0, 0)].status == WantedStatus.GRABBED


async def test_pre_dispatch_recheck_spares_tv_and_id_less_items(db, monkeypatch) -> None:
    """两种情况不花这次请求：剧集（成本集中在这、收益几乎没有）、条目自己没 ID。"""
    calls: list = []
    _fake_detail(monkeypatch, imdb_id="tt999", calls=calls)
    async with db.session() as session:
        # 剧集：条目有 IMDb 也不复核
        tv = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        await _set_item_imdb(session, tv, "tt32138219")
        pack = await _insert_torrent(
            session,
            "tvpack",
            "Test Show S01 2160p WEB-DL",
            {"media_type": "tv", "year": 2025, "seasons": [1], "resolution": "2160p"},
        )
        await evaluate_and_dispatch(session, [pack], source="被动匹配")
        assert calls == []

        # 电影但条目没有外部 ID：拿回来也没得比，同样不花
        movie = await _service(session).create(MediaKind.MOVIE, 103)
        row = await _insert_torrent(
            session,
            "noimdbitem",
            "Undated Movie 1080p WEB-DL",
            {"media_type": "movie", "resolution": "1080p"},
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert calls == []
        assert movie is not None


# ---------------------------------------------------------------------------
# 同名同年歧义（identity-confidence.md §9）
# ---------------------------------------------------------------------------


def _fake_twin_probe(monkeypatch, twins: list[dict], calls: list | None = None):
    """替掉 TMDB 孪生探测，避免连网。"""
    from movieclaw_api.services.subscription import twins as twins_mod

    async def probe(item):
        if calls is not None:
            calls.append(item.id)
        return list(twins)

    monkeypatch.setattr(twins_mod, "_probe", probe)


_TWIN = {
    "tmdb_id": 555,
    "title": "另一部同名片",
    "year": 2026,
    "imdb_id": "tt3559656",
    "runtime_minutes": 88,
}


async def test_ambiguous_movie_stops_and_asks_the_user(db, monkeypatch) -> None:
    """有同名同年的兄弟、又没有任何可区分的证据 → 不投递，点亮待确认告警。

    这正是 §0 现场在"站点没标影片编号"时的正确归宿：宁可停下来问一句，
    也不要凭片名+年份蒙一个。
    """
    from movieclaw_db.models import SystemNotice

    _fake_detail(monkeypatch, imdb_id=None)  # 站点没标编号
    _fake_twin_probe(monkeypatch, [_TWIN])
    async with db.session() as session:
        sub = await _movie_sub_with_imdb(session, "tt32138219")
        await _set_runtime(session, sub, 210)
        row = await _insert_torrent(
            session,
            "ambiguous",
            "Upcoming Movie 2026 1080p WEB-DL",
            {"media_type": "movie", "year": 2026, "resolution": "1080p"},
            size_bytes=int(4 * 1024**3),  # 两边的片长都解释得通 → 判别器不表态
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        assert (await _wanted_map(session, sub.id))[(0, 0)].status == WantedStatus.WANTED
        notice = (await session.execute(select(SystemNotice))).scalars().one()
        assert notice.payload["site_id"] == "testsite"
        assert notice.payload["torrent_id"] == "ambiguous"
        assert notice.payload["twins"][0]["tmdb_id"] == 555
        # 探测结果落缓存，下轮不再打 TMDB
        item = await session.get(MediaItem, sub.media_item_id)
        assert item.identity_twins == [_TWIN]


async def test_ambiguous_movie_is_rejected_silently_when_size_says_it_is_the_twin(
    db, monkeypatch
) -> None:
    """体积明显只解释得通孪生那一部 → 直接否决，**不打扰用户**。"""
    from movieclaw_db.models import SystemNotice

    _fake_detail(monkeypatch, imdb_id=None)
    _fake_twin_probe(monkeypatch, [{**_TWIN, "runtime_minutes": 45}])
    async with db.session() as session:
        sub = await _movie_sub_with_imdb(session, "tt32138219")
        await _set_runtime(session, sub, 210)
        row = await _insert_torrent(
            session,
            "tinyfortwin",
            "Upcoming Movie 2026 1080p WEB-DL",
            {"media_type": "movie", "year": 2026, "resolution": "1080p"},
            size_bytes=int(1 * 1024**3),  # 1 GB 配 210 分钟 = 0.68 Mbps，说不通
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        assert (await _wanted_map(session, sub.id))[(0, 0)].status == WantedStatus.WANTED
        assert (await session.execute(select(SystemNotice))).scalars().all() == []
        rejected = [a for a in await _activities(session, sub.id) if a.type == "match_rejected"]
        assert rejected[0].payload["reason_code"] == "identity_ambiguous_reject"
        assert "另一部同名片" in rejected[0].message


async def test_id_backed_candidate_ignores_the_twin_gate(db, monkeypatch) -> None:
    """有编号佐证的候选不受歧义影响——ID 说了算，照常投递。"""
    calls: list = []
    _fake_detail(monkeypatch, imdb_id="tt32138219")
    _fake_twin_probe(monkeypatch, [_TWIN], calls=calls)
    async with db.session() as session:
        sub = await _movie_sub_with_imdb(session, "tt32138219")
        await _set_runtime(session, sub, 210)
        row = await _insert_torrent(
            session,
            "idwins",
            "Upcoming Movie 2026 1080p WEB-DL",
            {"media_type": "movie", "year": 2026, "resolution": "1080p"},
        )
        await evaluate_and_dispatch(session, [row], source="被动匹配")

        assert (await _wanted_map(session, sub.id))[(0, 0)].status == WantedStatus.GRABBED
        assert calls == []  # 连孪生探测都不必做


async def test_twin_probe_is_skipped_for_tv(db, monkeypatch) -> None:
    """剧集不探孪生：另有季集号做区分，而剧集条目多得多，成本不值当。"""
    calls: list = []
    _fake_twin_probe(monkeypatch, [_TWIN], calls=calls)
    async with db.session() as session:
        sub = await _service(session).create(MediaKind.TV, 200, selected_seasons=[1])
        pack = await _insert_torrent(
            session,
            "tvnotwin",
            "Test Show S01 2160p WEB-DL",
            {"media_type": "tv", "year": 2025, "seasons": [1], "resolution": "2160p"},
        )
        await evaluate_and_dispatch(session, [pack], source="被动匹配")

        assert calls == []
        assert (await _wanted_map(session, sub.id))[(1, 1)].status == WantedStatus.GRABBED


async def test_ambiguous_movie_asks_at_most_once_per_round(db, monkeypatch) -> None:
    """一批几十个候选时最多问一次——逐个点灯等于给用户刷屏。

    候选已按证据强度与评分排序，问最靠前的那个就够；后续候选照常评估
    （其中带影片编号的仍能自动裁决出结果），只是不再重复发问。
    """
    from movieclaw_db.models import SystemNotice

    _fake_detail(monkeypatch, imdb_id=None)
    _fake_twin_probe(monkeypatch, [_TWIN])
    async with db.session() as session:
        sub = await _movie_sub_with_imdb(session, "tt32138219")
        await _set_runtime(session, sub, 210)
        rows = [
            await _insert_torrent(
                session,
                f"many{n}",
                "Upcoming Movie 2026 1080p WEB-DL",
                {"media_type": "movie", "year": 2026, "resolution": "1080p"},
                size_bytes=int(4 * 1024**3),
                seeders=100 - n,
            )
            for n in range(5)
        ]
        await evaluate_and_dispatch(session, rows, source="被动匹配")

        notices = (await session.execute(select(SystemNotice))).scalars().all()
        assert len(notices) == 1
        # 每个候选仍各自留下了拒绝记录（可解释性不打折）
        rejected = [a for a in await _activities(session, sub.id) if a.type == "match_rejected"]
        assert len(rejected) == 5


async def test_twin_cache_is_invalidated_by_metadata_refresh(db, monkeypatch) -> None:
    """元数据刷新会作废孪生缓存：用户常在上映前订阅，同名的另一部可能几个月
    后才进 TMDB，缓存成 [] 就再也发现不了。"""
    from movieclaw_api.services.media_scrape import _merge_identity
    from movieclaw_media.library import MediaProfile

    async with db.session() as session:
        sub = await _service(session).create(MediaKind.MOVIE, 101)
        item = await session.get(MediaItem, sub.media_item_id)
        item.identity_twins = []  # 上次探测：干净
        await session.commit()

        _merge_identity(
            item,
            MediaProfile(
                kind="movie", tmdb_id=101, title="未上映电影", original_title="Upcoming Movie"
            ),
        )
        assert item.identity_twins is None  # 回到"未探测"，下轮重新探
