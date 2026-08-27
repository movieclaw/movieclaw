"""失败任务的「忽略」出口（issue #221）。

用户反馈：字幕翻译失败的任务只有重试 / 查看日志 / 交给 Agent 三条路，
"我不想再处理"时没有出口，任务就一直挂在那儿，侧栏红角标永不熄灭。

本文件锁住这个出口的三条约定：
1. 忽略**不改写** status——它仍然是一条失败记录，日志与重试入口都还在；
2. 只对失败任务开放——活跃任务（含 blocked）仍占着去重键与资源锁，
   要终结得走取消，不能靠"藏起来"；
3. 可以反悔——撤销忽略后任务回到「需要处理」。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from movieclaw_api.api.deps import require_admin
from movieclaw_api.app import create_app
from movieclaw_api.core.config import get_settings
from movieclaw_api.services import jobs
from movieclaw_api.services.auth import Principal
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import JobStatus


@pytest_asyncio.fixture
async def job_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'dismiss.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def job_client(job_db):
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: Principal(kind="admin", name="tester")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver/api/v1"
    ) as client:
        yield client


async def _failed_job(job_db, *, job_type: str = "test.dismiss", subject: str = "Movie.mkv"):
    """建一条已经失败的任务：忽略只对失败任务开放，这是所有用例的前置。"""
    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type=job_type,
            subject=subject,
            input_data={"value": 1},
        )
        row = await jobs.get_job(session, created.job.id)
        assert row is not None
        row.status = JobStatus.FAILED
        row.error = {"code": "TEST_FAILED", "message": "翻译失败"}
        await session.commit()
        return created.job.id


async def test_dismiss_marks_job_without_rewriting_status(job_db) -> None:
    """忽略只表达"不再处理"，不改写"这件事失败过"。"""
    job_id = await _failed_job(job_db)

    async with job_db.session() as session:
        row, changed = await jobs.dismiss_job(session, job_id, dismissed_by="admin:tester")

    assert changed is True
    assert row is not None
    # 关键不变量：状态没被动过，日志与重试链仍然建立在它上面
    assert row.status is JobStatus.FAILED
    assert row.dismissed_at is not None
    assert row.dismissed_by == "admin:tester"
    # 跟已取消任务同口径落一个保留期，最终由清理任务收走，不永久占着台账
    assert row.retention_until is not None

    async with job_db.session() as session:
        events = await jobs.job_events(session, job_id)
    assert events[-1].event_type == "dismissed"


async def test_dismiss_is_idempotent(job_db) -> None:
    """重复忽略不再改变状态，也不重复记事件。"""
    job_id = await _failed_job(job_db)

    async with job_db.session() as session:
        _, first = await jobs.dismiss_job(session, job_id, dismissed_by="admin:tester")
    async with job_db.session() as session:
        row, second = await jobs.dismiss_job(session, job_id, dismissed_by="admin:tester")

    assert first is True
    assert second is False
    assert row is not None
    async with job_db.session() as session:
        events = await jobs.job_events(session, job_id)
    assert [e.event_type for e in events].count("dismissed") == 1


async def test_dismiss_rejects_non_failed_jobs(job_db) -> None:
    """活跃任务仍占着去重键与资源锁，只能取消，不能靠"藏起来"了事。"""
    async with job_db.session() as session:
        created = await jobs.create_job(
            session, job_type="test.active", input_data={"value": 1}
        )

    async with job_db.session() as session:
        with pytest.raises(jobs.JobFailed, match="只有失败的任务可以忽略"):
            await jobs.dismiss_job(session, created.job.id, dismissed_by="admin:tester")


async def test_undismiss_restores_attention(job_db) -> None:
    """忽略是用户在信息不全时按下的，就必须能反悔。"""
    job_id = await _failed_job(job_db)

    async with job_db.session() as session:
        await jobs.dismiss_job(session, job_id, dismissed_by="admin:tester")
    async with job_db.session() as session:
        row, restored = await jobs.undismiss_job(session, job_id)

    assert restored is True
    assert row is not None
    assert row.dismissed_at is None
    assert row.dismissed_by is None
    assert row.retention_until is None
    assert row.status is JobStatus.FAILED

    async with job_db.session() as session:
        events = await jobs.job_events(session, job_id)
    assert events[-1].event_type == "undismissed"


async def test_dismiss_all_only_touches_currently_failed_jobs(job_db) -> None:
    """批量忽略收口的是"眼前这批"，不是从此对失败视而不见。"""
    failed_ids = [
        await _failed_job(job_db, subject=f"S01E{index:02d}.mkv") for index in range(1, 4)
    ]
    async with job_db.session() as session:
        active = await jobs.create_job(
            session, job_type="test.active", input_data={"value": 1}
        )

    async with job_db.session() as session:
        rows = await jobs.dismiss_failed_jobs(session, dismissed_by="admin:tester")

    assert sorted(row.id for row in rows) == sorted(failed_ids)

    async with job_db.session() as session:
        still_active = await jobs.get_job(session, active.job.id)
    assert still_active is not None
    assert still_active.dismissed_at is None

    # 之后再失败的任务照常提醒——批量忽略不是一个永久开关
    later_id = await _failed_job(job_db, subject="Later.mkv")
    async with job_db.session() as session:
        later = await jobs.get_job(session, later_id)
    assert later is not None
    assert later.dismissed_at is None


async def test_dismiss_all_filters_by_job_type(job_db) -> None:
    subtitle_id = await _failed_job(job_db, job_type="subtitle.generate")
    scan_id = await _failed_job(job_db, job_type="library.scan")

    async with job_db.session() as session:
        rows = await jobs.dismiss_failed_jobs(
            session, dismissed_by="admin:tester", job_type="subtitle.generate"
        )

    assert [row.id for row in rows] == [subtitle_id]
    async with job_db.session() as session:
        scan = await jobs.get_job(session, scan_id)
    assert scan is not None
    assert scan.dismissed_at is None


async def test_dismissed_job_can_still_be_retried(job_db) -> None:
    """忽略不封死重试：用户随时可以改主意再试一次。"""
    job_id = await _failed_job(job_db)
    async with job_db.session() as session:
        await jobs.dismiss_job(session, job_id, dismissed_by="admin:tester")

    async with job_db.session() as session:
        source = await jobs.get_job(session, job_id)
        assert source is not None
        retried = await jobs.retry_job(
            session, source, actor_kind="admin", actor_name="tester", origin="web"
        )

    assert retried.created is True
    assert retried.job.retry_of_job_id == job_id
    # 新任务是干净的，不继承忽略标记
    assert retried.job.dismissed_at is None


async def test_dismiss_api_round_trip(job_db, job_client) -> None:
    """HTTP 契约：忽略 / 撤销忽略都把最新任务体带回给前端。"""
    job_id = await _failed_job(job_db)

    response = await job_client.post(f"/jobs/{job_id}/dismiss", json={"mute_source": False})
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["dismissed"] is True
    assert payload["muted"] is False
    assert payload["job"]["status"] == "failed"
    assert payload["job"]["dismissed_at"] is not None

    response = await job_client.post(f"/jobs/{job_id}/undismiss")
    assert response.status_code == 200
    assert response.json()["data"]["job"]["dismissed_at"] is None


async def test_dismiss_api_rejects_active_job(job_db, job_client) -> None:
    async with job_db.session() as session:
        created = await jobs.create_job(
            session, job_type="test.active", input_data={"value": 1}
        )

    response = await job_client.post(f"/jobs/{created.job.id}/dismiss", json={})
    assert response.status_code == 400


async def test_dismiss_all_api(job_db, job_client) -> None:
    for index in range(3):
        await _failed_job(job_db, subject=f"Batch{index}.mkv")

    response = await job_client.post("/jobs/dismiss-all", json={})
    assert response.status_code == 200
    assert response.json()["data"]["dismissed"] == 3

    # 幂等：再点一次没有任务可忽略，不报错
    response = await job_client.post("/jobs/dismiss-all", json={})
    assert response.json()["data"]["dismissed"] == 0
