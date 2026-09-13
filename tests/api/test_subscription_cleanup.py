"""取消订阅的联动清理（services.subscription.cleanup）测试。

守住四条边界：
1. 默认取消订阅不碰任何内容（承诺不能被这次改动破坏）；
2. 清理计划必须在删订阅**之前**快照——投递记录随订阅级联删除，
   订阅一没就再也查不回来；
3. 入队与删除同一个事务：不会出现"订阅还在但文件已被回收"；
4. 清理任务真的删了种子、把媒体库文件送进回收站，且单项失败不中断其余。
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
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
