"""取消订阅的联动清理（services.subscription.cleanup）测试。

守住四条边界：
1. 默认取消订阅不碰任何内容（承诺不能被这次改动破坏）；
2. 清理计划必须在删订阅**之前**快照——投递记录随订阅级联删除，
   订阅一没就再也查不回来；
3. 入队与删除同一个事务：不会出现"订阅还在但文件已被回收"；
4. 清理任务真的删了种子、把媒体库文件送进回收站，且单项失败不中断其余；
5. 范围按季收口：整条退订只处置这条订阅**覆盖过的季**（从没订阅过的季不碰），
   减季后的按季清理只处置退出那几季、且不碰跨季包；
6. 按季清理必须退回工单——否则日后重新勾选那一季会被当成早已满足。
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services import download_tasks
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_api.services.subscription.cleanup import (
    JOB_TYPE,
    _run_subscription_cleanup_job,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    ClientType,
    DownloaderClient,
    FileSource,
    FileState,
    Job,
    LibraryFile,
    Subscription,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_downloader.exceptions import DownloaderConnectError
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"
_HASH = "a" * 40

_ROUTES = {
    "/3/movie/100": {
        "id": 100,
        "title": "测试电影",
        "original_title": "Test Movie",
        "release_date": (utcnow().date() - timedelta(days=30)).isoformat(),
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
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
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cleanup.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _service(session) -> SubscriptionService:
    return SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))


async def _seed(db, tmp_path) -> tuple[int, int, str]:
    """一条电影订阅 + 一次投递记录 + 一个在位媒体库文件。

    返回（订阅 id、台账行 id、库内文件路径）。
    """
    root = tmp_path / "movies"
    root.mkdir(exist_ok=True)
    path = root / "Test.Movie.2024.1080p.mkv"
    path.write_bytes(b"data")

    async with db.session() as session:
        subscription = await _service(session).create(MediaKind.MOVIE, 100)
        assert subscription.id is not None
        # 直接落行而不走仓储：仓储会加密密码，本用例不需要凭据（下载器适配器
        # 整体打桩），也就不必在测试里初始化 SecretBox
        downloader = DownloaderClient(
            name="测试下载器",
            client_type=ClientType.QBITTORRENT,
            url="http://127.0.0.1:8080",
            save_path="/downloads",
        )
        session.add(downloader)
        await session.flush()
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=subscription.id,
                downloader_id=downloader.id,
                info_hash=_HASH,
                torrent_title="Test.Movie.2024.1080p",
                hit_and_run=False,
                last_progress_at=utcnow(),
            )
        )
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
        row = LibraryFile(
            library_id=library.id,
            media_item_id=subscription.media_item_id,
            file_path=str(path),
            size_bytes=4,
            source=FileSource.IMPORTED,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return subscription.id, row.id, str(path)


@pytest.mark.asyncio
async def test_default_delete_touches_nothing(db, tmp_path) -> None:
    """默认取消订阅：不建清理任务，媒体库台账与文件原封不动。"""
    subscription_id, file_id, path = await _seed(db, tmp_path)
    async with db.session() as session:
        outcome = await _service(session).delete_permanently(subscription_id)
    assert outcome.cleanup_job_id is None
    assert "不受影响" in outcome.message
    async with db.session() as session:
        assert await session.get(Subscription, subscription_id) is None
        row = await session.get(LibraryFile, file_id)
        assert row is not None and row.state == FileState.IN_PLACE
        assert (await session.execute(select(Job))).scalars().all() == []


@pytest.mark.asyncio
async def test_removal_preview_counts_torrents_and_files(db, tmp_path) -> None:
    """预览如实报数：种子按 infohash 去重、媒体库体积取自台账。"""
    subscription_id, _file_id, _path = await _seed(db, tmp_path)
    async with db.session() as session:
        plan = await _service(session).removal_preview(subscription_id)
    assert [t.info_hash for t in plan.torrents] == [_HASH]
    assert plan.hit_and_run_count == 0  # hit_and_run=False：明确无考核风险
    assert len(plan.files) == 1
    assert plan.library_bytes == 4


@pytest.mark.asyncio
async def test_delete_with_cleanup_snapshots_plan_into_job(db, tmp_path) -> None:
    """勾了清理：订阅立刻消失，任务里带着删订阅前快照下来的完整计划。"""
    subscription_id, file_id, path = await _seed(db, tmp_path)
    async with db.session() as session:
        outcome = await _service(session).delete_permanently(
            subscription_id, delete_torrents=True, delete_library_files=True
        )
    assert outcome.cleanup_job_id is not None
    assert "后台清理" in outcome.message

    async with db.session() as session:
        assert await session.get(Subscription, subscription_id) is None
        job = await session.get(Job, outcome.cleanup_job_id)
        assert job is not None and job.job_type == JOB_TYPE
        # 投递记录已随订阅级联删除，计划只能来自删除前的快照
        assert (
            await session.execute(select(SubscriptionDownloadAttempt))
        ).scalars().all() == []
        assert [t["info_hash"] for t in job.input_data["torrents"]] == [_HASH]
        assert [f["file_id"] for f in job.input_data["files"]] == [file_id]
        # 任务只是入队，文件此刻仍在原位——真正的清理由处理器完成
        assert (await session.get(LibraryFile, file_id)).state == FileState.IN_PLACE


@pytest.mark.asyncio
async def test_only_selected_side_is_cleaned(db, tmp_path) -> None:
    """只勾一项时另一侧不进计划（勾"删种子"不该顺手回收媒体库文件）。"""
    subscription_id, _file_id, _path = await _seed(db, tmp_path)
    async with db.session() as session:
        outcome = await _service(session).delete_permanently(
            subscription_id, delete_torrents=True
        )
    async with db.session() as session:
        job = await session.get(Job, outcome.cleanup_job_id)
        assert job.input_data["files"] == []
        assert len(job.input_data["torrents"]) == 1


class _FakeContext:
    """处理器需要的窄接口打桩：只记进度，不碰任务状态机。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def raise_if_cancelled(self) -> None:
        return None

    def progress_due(self, *, min_interval: float = 1.0) -> bool:
        return True

    async def update_progress(self, **kwargs) -> None:
        self.messages.append(kwargs["message"])


@pytest.mark.asyncio
async def test_cleanup_job_deletes_torrent_and_recycles_file(db, tmp_path, monkeypatch) -> None:
    """处理器跑完：下载器收到带数据文件的删除命令，库文件进回收站可恢复。"""
    subscription_id, file_id, path = await _seed(db, tmp_path)
    async with db.session() as session:
        outcome = await _service(session).delete_permanently(
            subscription_id, delete_torrents=True, delete_library_files=True
        )
    async with db.session() as session:
        job = await session.get(Job, outcome.cleanup_job_id)
        input_data = dict(job.input_data)

    deleted: list[tuple[str, bool]] = []

    class _FakeAdapter:
        async def delete_torrent(self, info_hash: str, *, delete_files: bool = False) -> None:
            deleted.append((info_hash, delete_files))

        async def close(self) -> None:
            return None

    monkeypatch.setattr(download_tasks, "create_downloader", lambda config: _FakeAdapter())

    result = await _run_subscription_cleanup_job(_FakeContext(), input_data)

    assert deleted == [(_HASH, True)]  # 种子与下载目录里的数据一起删
    assert result["torrent_removed"] == 1
    assert result["file_recycled"] == 1
    assert result["failed"] == 0
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert row.state == FileState.TRASHED
        assert row.trash_original_path == path
        assert row.purge_after is not None  # 保留期内可恢复
        assert row.trash_context["reason"] == "subscription_cancelled"


@pytest.mark.asyncio
async def test_cleanup_job_reports_torrent_failure_but_keeps_going(
    db, tmp_path, monkeypatch
) -> None:
    """下载器不可达不该让媒体库那一半白等：失败计数上报，文件照常回收。"""
    subscription_id, file_id, _path = await _seed(db, tmp_path)
    async with db.session() as session:
        outcome = await _service(session).delete_permanently(
            subscription_id, delete_torrents=True, delete_library_files=True
        )
    async with db.session() as session:
        input_data = dict((await session.get(Job, outcome.cleanup_job_id)).input_data)

    class _BrokenAdapter:
        async def delete_torrent(self, info_hash: str, *, delete_files: bool = False) -> None:
            raise DownloaderConnectError("下载器连接失败")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(download_tasks, "create_downloader", lambda config: _BrokenAdapter())

    result = await _run_subscription_cleanup_job(_FakeContext(), input_data)

    assert result["torrent_removed"] == 0
    assert result["failed"] == 1
    assert "下载器连接失败" in result["failures"][0]
    async with db.session() as session:
        assert (await session.get(LibraryFile, file_id)).state == FileState.TRASHED


@pytest.mark.asyncio
async def test_cleanup_job_is_repeatable(db, tmp_path, monkeypatch) -> None:
    """崩溃重跑不会把回收站里的文件再搬一次（已待回收的行直接跳过）。"""
    subscription_id, file_id, _path = await _seed(db, tmp_path)
    async with db.session() as session:
        outcome = await _service(session).delete_permanently(
            subscription_id, delete_library_files=True
        )
    async with db.session() as session:
        input_data = dict((await session.get(Job, outcome.cleanup_job_id)).input_data)

    first = await _run_subscription_cleanup_job(_FakeContext(), input_data)
    async with db.session() as session:
        trashed_path = (await session.get(LibraryFile, file_id)).file_path
    second = await _run_subscription_cleanup_job(_FakeContext(), input_data)

    assert first["file_recycled"] == 1
    assert second["file_recycled"] == 0
    assert second["failed"] == 0
    async with db.session() as session:
        assert (await session.get(LibraryFile, file_id)).file_path == trashed_path


_TV_AIRED = (utcnow().date() - timedelta(days=10)).isoformat()
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
        "seasons": [{"season_number": 1}, {"season_number": 2}, {"season_number": 3}],
    },
}
for _n in (1, 2, 3):
    _TV_ROUTES[f"/3/tv/200/season/{_n}"] = {
        "name": f"第 {_n} 季",
        "air_date": _TV_AIRED,
        "episodes": [{"episode_number": 1, "name": "E1", "air_date": _TV_AIRED}],
    }


def _tv_service(session) -> SubscriptionService:
    """剧集夹具要走 /3/tv/200，与电影用的 _fake_tmdb 路由表不同。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _TV_ROUTES.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=payload)

    client = TmdbClient(_KEY, transport=httpx.MockTransport(handler))
    return SubscriptionService(session, MediaLibraryService(session, client))


async def _seed_tv(db, tmp_path) -> tuple[int, dict[int, int]]:
    """一条只勾了第 2、3 季的剧集订阅；媒体库里三季各有一个文件。

    第 1 季是用户自己刮削进来的（从没订阅过）——它是"范围按季收口"的关键证人。
    返回（订阅 id、季号 → 台账行 id）。
    """
    root = tmp_path / "tv"
    root.mkdir(exist_ok=True)
    async with db.session() as session:
        subscription = await _tv_service(session).create(
            MediaKind.TV, 200, selected_seasons=[2, 3]
        )
        assert subscription.id is not None
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
        file_ids: dict[int, int] = {}
        for season in (1, 2, 3):
            path = root / f"S{season:02d}E01.mkv"
            path.write_bytes(b"data")
            row = LibraryFile(
                library_id=library.id,
                media_item_id=subscription.media_item_id,
                season_number=season,
                episode_number=1,
                file_path=str(path),
                size_bytes=4,
                source=FileSource.IMPORTED,
            )
            session.add(row)
            await session.flush()
            file_ids[season] = row.id  # type: ignore[assignment]
        await session.commit()
        return subscription.id, file_ids


@pytest.mark.asyncio
async def test_full_cancel_spares_never_subscribed_seasons(db, tmp_path) -> None:
    """整条退订只处置订阅覆盖过的季：用户自己弄来的第 1 季不进计划。

    此前按 media_item_id 一刀切，只订了第 2、3 季的用户取消订阅会连带回收
    自己刮削进来的第 1 季，而弹窗只显示一个总数、看不出来。
    """
    subscription_id, file_ids = await _seed_tv(db, tmp_path)
    async with db.session() as session:
        plan = await _service(session).removal_preview(subscription_id)
    assert sorted(f.file_id for f in plan.files) == sorted([file_ids[2], file_ids[3]])


@pytest.mark.asyncio
async def test_season_cleanup_scopes_files_to_dropped_season(db, tmp_path) -> None:
    """按季清理只处置退出的那一季，订阅与其余季的文件都留着。"""
    subscription_id, file_ids = await _seed_tv(db, tmp_path)
    async with db.session() as session:
        await _tv_service(session).update(subscription_id, selected_seasons=[3])
    async with db.session() as session:
        plan = await _service(session).removal_preview(subscription_id, seasons=[2])
        assert [f.file_id for f in plan.files] == [file_ids[2]]
        outcome = await _service(session).cleanup_seasons(
            subscription_id, [2], delete_library_files=True
        )
    assert outcome.cleanup_job_id is not None
    async with db.session() as session:
        # 订阅还在（它还在追第 3 季）
        assert await session.get(Subscription, subscription_id) is not None
        job = await session.get(Job, outcome.cleanup_job_id)
        assert [f["file_id"] for f in job.input_data["files"]] == [file_ids[2]]
        assert "第 2 季" in job.subject


@pytest.mark.asyncio
async def test_season_cleanup_rejects_still_tracked_season(db, tmp_path) -> None:
    """仍在订阅范围内的季一律拒绝——这个接口不是绕过减季直接删内容的后门。"""
    subscription_id, _file_ids = await _seed_tv(db, tmp_path)
    async with db.session() as session:
        with pytest.raises(BadRequestException):
            await _service(session).cleanup_seasons(
                subscription_id, [3], delete_library_files=True
            )


@pytest.mark.asyncio
async def test_season_cleanup_keeps_cross_season_pack(db, tmp_path) -> None:
    """跨季包覆盖到保留的季时不删，并如实进 retained 清单交给弹窗告知。"""
    subscription_id, _file_ids = await _seed_tv(db, tmp_path)
    async with db.session() as session:
        downloader = DownloaderClient(
            name="测试下载器",
            client_type=ClientType.QBITTORRENT,
            url="http://127.0.0.1:8080",
            save_path="/downloads",
        )
        session.add(downloader)
        await session.flush()
        session.add_all(
            [
                # 只服务第 2 季：可以删
                SubscriptionDownloadAttempt(
                    subscription_id=subscription_id,
                    downloader_id=downloader.id,
                    info_hash="b" * 40,
                    torrent_title="Show.S02.1080p",
                    units=[[2, 1]],
                    last_progress_at=utcnow(),
                ),
                # 跨第 2、3 季：第 3 季还在追，删了会把它一起毁掉
                SubscriptionDownloadAttempt(
                    subscription_id=subscription_id,
                    downloader_id=downloader.id,
                    info_hash="c" * 40,
                    torrent_title="Show.S02-S03.COMPLETE",
                    units=[[2, 1], [3, 1]],
                    last_progress_at=utcnow(),
                ),
            ]
        )
        await session.commit()
        await _tv_service(session).update(subscription_id, selected_seasons=[3])
    async with db.session() as session:
        plan = await _service(session).removal_preview(subscription_id, seasons=[2])
    assert [t.info_hash for t in plan.torrents] == ["b" * 40]
    assert [(r.title, r.seasons) for r in plan.retained] == [("Show.S02-S03.COMPLETE", (2, 3))]


@pytest.mark.asyncio
async def test_season_cleanup_reopens_wanted_so_reselect_redownloads(db, tmp_path) -> None:
    """清掉内容后出域工单退回缺口：重新勾选这一季会重新下载，而不是静静地缺着。

    不退回的话工单仍停在 imported，引擎认为早已满足——那一季既不下载也不报缺。
    """
    subscription_id, _file_ids = await _seed_tv(db, tmp_path)
    async with db.session() as session:
        # 第 2 季已入库
        rows = (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == subscription_id,
                    WantedItem.season_number == 2,
                )
            )
        ).scalars().all()
        assert rows, "夹具应为第 2 季建了工单"
        for row in rows:
            row.status = WantedStatus.IMPORTED
            row.imported_at = utcnow()
            session.add(row)
        await session.commit()
        await _tv_service(session).update(subscription_id, selected_seasons=[3])
        await _service(session).cleanup_seasons(
            subscription_id, [2], delete_library_files=True
        )
    async with db.session() as session:
        rows = (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == subscription_id,
                    WantedItem.season_number == 2,
                )
            )
        ).scalars().all()
        assert [r.status for r in rows] == [WantedStatus.WANTED] * len(rows)
        # 出域期间不排期；重新纳入时由 SubscriptionService.update 统一重挂
        assert all(r.next_search_at is None and not r.in_scope for r in rows)
