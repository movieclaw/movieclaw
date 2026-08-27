"""持久化 Job 状态机：去重、执行、取消与重启恢复。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from movieclaw_api.api.deps import require_admin
from movieclaw_api.app import create_app
from movieclaw_api.core.config import get_settings
from movieclaw_api.services import jobs
from movieclaw_api.services.auth import Principal
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import Job, JobLock, JobStatus, utcnow


@pytest_asyncio.fixture
async def job_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
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


async def _wait_status(job_id: str, expected: JobStatus, timeout: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with get_database().session() as session:
            row = await jobs.get_job(session, job_id)
            assert row is not None
            if row.status == expected:
                return row
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"任务未进入 {expected}：当前 {row.status}")
        await asyncio.sleep(0.01)


async def test_create_job_deduplicates_active_work_and_indexes_resources(job_db) -> None:
    async with job_db.session() as session:
        first = await jobs.create_job(
            session,
            job_type="test.dedupe",
            input_data={"value": 1},
            resources=[jobs.ResourceRef("library_file", 42)],
            dedupe_key="test.dedupe:42",
        )
    async with job_db.session() as session:
        second = await jobs.create_job(
            session,
            job_type="test.dedupe",
            input_data={"value": 2},
            resources=[jobs.ResourceRef("library_file", 42)],
            dedupe_key="test.dedupe:42",
        )
        by_resource = await jobs.list_jobs(session, resource_type="library_file", resource_id=42)

    assert first.created is True
    assert second.created is False
    assert second.job.id == first.job.id
    assert [row.id for row in by_resource] == [first.job.id]

    # 应用层先查只负责友好复用；真正的跨进程竞态必须由数据库部分唯一索引
    # 兜底。直接绕开 create_job 写第二行，证明索引匹配实际存储的小写状态值。
    async with job_db.session() as session:
        session.add(
            Job(
                id="job_forced_duplicate",
                job_type="test.dedupe",
                input_data={"value": 3},
                dedupe_key="test.dedupe:42",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_create_job_rejects_nested_credentials(job_db) -> None:
    async with job_db.session() as session:
        with pytest.raises(ValueError, match="input_data.provider.api-key"):
            await jobs.create_job(
                session,
                job_type="test.secret",
                input_data={"provider": {"api-key": "must-not-persist"}},
            )
        with pytest.raises(ValueError, match="input_data.apiKey"):
            await jobs.create_job(
                session,
                job_type="test.camel-secret",
                input_data={"apiKey": "must-not-persist"},
            )


async def test_latest_event_cursor_starts_at_table_tail(job_db) -> None:
    async with job_db.session() as session:
        first = await jobs.create_job(session, job_type="test.cursor", input_data={})
        cursor = await jobs.latest_job_event_id(session)
        second = await jobs.create_job(session, job_type="test.cursor", input_data={})
        new_events = await jobs.job_events(session, after_id=cursor)

    assert first.job.id != second.job.id
    assert [event.job_id for event in new_events] == [second.job.id]


async def test_dispatcher_persists_progress_result_and_events(job_db) -> None:
    @jobs.register_job_handler("test.success")
    async def success(context: jobs.JobContext, payload: dict):
        await context.update_progress(
            mode="determinate",
            phase="work",
            message="已经完成一半",
            current=1,
            total=2,
            percent=50,
            usage={"request_count": 2, "total_tokens": 321},
        )
        return {"message": "处理完成", "value": payload["value"]}

    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="test.success",
            input_data={"value": 7},
            resources=[jobs.ResourceRef("library_file", 7)],
        )
    await jobs.init_job_dispatcher(max_parallel=1)
    row = await _wait_status(created.job.id, JobStatus.SUCCEEDED)

    assert row.result == {"message": "处理完成", "value": 7}
    assert row.progress["percent"] == 100.0
    assert row.usage == {"request_count": 2, "total_tokens": 321}
    async with job_db.session() as session:
        events = await jobs.job_events(session, created.job.id)
    assert [event.event_type for event in events] == [
        "created",
        "started",
        "progress",
        "succeeded",
    ]


async def test_dispatcher_rejects_unsupported_definition_without_retry(job_db) -> None:
    called = False

    @jobs.register_job_handler("test.definition", definition_versions=(1,))
    async def version_one(_context: jobs.JobContext, _payload: dict):
        nonlocal called
        called = True
        return {}

    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="test.definition",
            definition_version=2,
            input_data={},
        )
    await jobs.init_job_dispatcher(max_parallel=1)
    row = await _wait_status(created.job.id, JobStatus.FAILED)

    assert called is False
    assert row.error is not None
    assert row.error["code"] == "JOB_DEFINITION_UNSUPPORTED"
    assert row.attempt == 1


async def test_graceful_restart_returns_running_job_to_queue_and_resumes(job_db) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    @jobs.register_job_handler("test.resume")
    async def resumable(_context: jobs.JobContext, _payload: dict):
        entered.set()
        await release.wait()
        return {"message": "续传完成"}

    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="test.resume",
            input_data={},
            resources=[jobs.ResourceRef("library_file", 9)],
        )
    await jobs.init_job_dispatcher(max_parallel=1)
    await asyncio.wait_for(entered.wait(), timeout=1)
    await jobs.close_job_dispatcher()
    paused = await _wait_status(created.job.id, JobStatus.QUEUED)
    assert paused.progress["phase"] == "resuming"

    release.set()
    await jobs.init_job_dispatcher(max_parallel=1)
    resumed = await _wait_status(created.job.id, JobStatus.SUCCEEDED)
    assert resumed.result == {"message": "续传完成"}
    # 服务更新只是同一次执行的续跑，不应消耗真正留给异常的重试额度。
    assert resumed.attempt == 1


async def test_cancel_queued_job_is_terminal_and_not_executed(job_db) -> None:
    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="test.never",
            input_data={},
            resources=[jobs.ResourceRef("library_file", 10)],
        )
        row, changed = await jobs.request_cancel(session, created.job.id, requested_by="test-user")

    assert changed is True
    assert row is not None and row.status == JobStatus.CANCELLED
    await jobs.init_job_dispatcher(max_parallel=1)
    await asyncio.sleep(0.05)
    cancelled = await _wait_status(created.job.id, JobStatus.CANCELLED)
    assert cancelled.cancel_requested_by == "test-user"


async def test_progress_and_cancel_allocate_unique_event_revisions(job_db) -> None:
    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="test.concurrent-events",
            input_data={},
        )
        row = await jobs.get_job(session, created.job.id)
        assert row is not None
        row.status = JobStatus.RUNNING
        row.lease_owner = "lease-test"
        row.lease_expires_at = utcnow() + timedelta(minutes=1)
        await session.commit()

    async def update_progress(index: int) -> None:
        context = jobs.JobContext(
            created.job.id,
            lease_token="lease-test",
            lease_lost=asyncio.Event(),
        )
        await context.update_progress(
            mode="determinate",
            phase="work",
            message=f"进度 {index}",
            current=index,
            total=6,
            percent=index / 6 * 100,
        )

    async def cancel() -> None:
        async with job_db.session() as session:
            await jobs.request_cancel(session, created.job.id, requested_by="concurrency-test")

    await asyncio.gather(*(update_progress(index) for index in range(1, 7)), cancel())

    async with job_db.session() as session:
        row = await jobs.get_job(session, created.job.id)
        events = await jobs.job_events(session, created.job.id)
    revisions = [event.revision for event in events]
    assert row is not None
    assert revisions == list(range(1, len(events) + 1))
    assert row.revision == revisions[-1]


async def test_expired_lease_is_recovered_and_next_claim_gets_new_token(job_db) -> None:
    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="test.lease",
            input_data={},
            resources=[jobs.ResourceRef("library_file", 99)],
        )
        row = await jobs.get_job(session, created.job.id)
        assert row is not None
        row.status = JobStatus.RUNNING
        row.lease_owner = "stale-attempt-token"
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(
            JobLock(
                lock_key="library_file:99",
                job_id=row.id,
                lease_expires_at=utcnow() - timedelta(seconds=1),
            )
        )
        await session.commit()

    dispatcher = jobs.JobDispatcher(max_parallel=1)
    await dispatcher._recover_expired_jobs()
    claimed = await dispatcher._claim_one()

    assert claimed is not None
    job_id, lease_token = claimed
    assert job_id == created.job.id
    assert lease_token.startswith(f"{dispatcher.owner}:")
    assert lease_token != "stale-attempt-token"

    lease_lost = asyncio.Event()
    stale_context = jobs.JobContext(
        job_id,
        lease_token="stale-attempt-token",
        lease_lost=lease_lost,
    )
    await stale_context.update_progress(
        mode="determinate",
        phase="stale",
        message="过期执行不应再写进度",
        percent=99,
    )
    assert lease_lost.is_set()
    async with job_db.session() as session:
        row = await jobs.get_job(session, job_id)
    assert row is not None
    assert row.progress["phase"] != "stale"


async def test_retry_blocked_job_requeues_in_place(job_db) -> None:
    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="test.blocked",
            input_data={"value": 1},
            dedupe_key="test.blocked:1",
        )
        row = await jobs.get_job(session, created.job.id)
        assert row is not None
        row.status = JobStatus.BLOCKED
        row.error = {"code": "NEEDS_CONFIG", "message": "请先补充配置"}
        await session.commit()

    async with job_db.session() as session:
        source = await jobs.get_job(session, created.job.id)
        assert source is not None
        retried = await jobs.retry_job(
            session,
            source,
            actor_kind="admin",
            actor_name="tester",
            origin="web",
        )
        events = await jobs.job_events(session, created.job.id)

    assert retried.created is False
    assert retried.job.id == created.job.id
    assert retried.job.status == JobStatus.QUEUED
    assert retried.job.error is None
    assert events[-1].event_type == "unblocked"


async def test_retry_failed_job_creates_auditable_attempt_chain(job_db) -> None:
    async with job_db.session() as session:
        original = await jobs.create_job(
            session,
            job_type="test.failed",
            subject="Movie.mkv",
            input_data={"value": 1},
            dedupe_key="test.failed:1",
        )
        row = await jobs.get_job(session, original.job.id)
        assert row is not None
        row.status = JobStatus.FAILED
        await session.commit()

    async with job_db.session() as session:
        source = await jobs.get_job(session, original.job.id)
        assert source is not None
        retried = await jobs.retry_job(
            session,
            source,
            actor_kind="pat",
            actor_name="mclaw",
            origin="cli",
        )

    assert retried.created is True
    assert retried.job.id != original.job.id
    assert retried.job.retry_of_job_id == original.job.id
    assert retried.job.root_job_id == original.job.id
    assert retried.job.origin == "cli"
    assert retried.job.subject == "Movie.mkv"


async def test_cleanup_expired_ancestor_keeps_retry_job(job_db) -> None:
    async with job_db.session() as session:
        original = await jobs.create_job(
            session,
            job_type="test.cleanup-chain",
            input_data={},
        )
        source = await jobs.get_job(session, original.job.id)
        assert source is not None
        source.status = JobStatus.FAILED
        source.retention_until = utcnow() - timedelta(seconds=1)
        await session.commit()

    async with job_db.session() as session:
        source = await jobs.get_job(session, original.job.id)
        assert source is not None
        retried = await jobs.retry_job(
            session,
            source,
            actor_kind="admin",
            actor_name="tester",
            origin="web",
        )

    assert await jobs.purge_expired_jobs() == 1
    async with job_db.session() as session:
        assert await jobs.get_job(session, original.job.id) is None
        remaining = await jobs.get_job(session, retried.job.id)
    assert remaining is not None
    assert remaining.retry_of_job_id is None
    assert remaining.root_job_id is None


async def test_job_api_lists_waits_events_and_cancels(job_db, job_client: AsyncClient) -> None:
    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="test.api",
            input_data={},
            resources=[jobs.ResourceRef("media_item", 88)],
        )

    listed = await job_client.get(
        "/jobs", params={"resource_type": "media_item", "resource_id": 88}
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["items"][0]["id"] == created.job.id

    waited = await job_client.get(
        f"/jobs/{created.job.id}/wait",
        params={"after_revision": 0, "wait_seconds": 0.1},
    )
    assert waited.status_code == 200
    assert waited.json()["data"]["changed"] is True

    timeline = await job_client.get(f"/jobs/{created.job.id}/events")
    assert [item["event_type"] for item in timeline.json()["data"]["items"]] == ["created"]

    cancelled = await job_client.post(f"/jobs/{created.job.id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["job"]["status"] == "cancelled"


async def test_update_progress_from_stale_lease_writes_nothing(job_db) -> None:
    """列级进度写路径的租约守卫：令牌不符时不落任何东西，只举失联旗。"""
    async with job_db.session() as session:
        created = await jobs.create_job(session, job_type="test.stale-lease", input_data={})
        row = await jobs.get_job(session, created.job.id)
        assert row is not None
        row.status = JobStatus.RUNNING
        row.lease_owner = "lease-current"
        await session.commit()

    lease_lost = asyncio.Event()
    context = jobs.JobContext(created.job.id, lease_token="lease-stale", lease_lost=lease_lost)
    await context.update_progress(mode="determinate", phase="work", message="过期执行的进度")

    assert lease_lost.is_set()
    async with job_db.session() as session:
        row = await jobs.get_job(session, created.job.id)
        events = await jobs.job_events(session, created.job.id)
    assert row is not None
    assert row.progress.get("message") != "过期执行的进度"
    assert all(event.event_type != "progress" for event in events)


async def test_update_progress_after_terminal_state_is_ignored(job_db) -> None:
    """终态后迟到的进度写入必须被丢弃：结果页不能被回退成“进行中”。"""
    async with job_db.session() as session:
        created = await jobs.create_job(session, job_type="test.terminal-progress", input_data={})
        row = await jobs.get_job(session, created.job.id)
        assert row is not None
        row.status = JobStatus.SUCCEEDED
        row.lease_owner = "lease-done"
        await session.commit()

    context = jobs.JobContext(created.job.id, lease_token="lease-done", lease_lost=asyncio.Event())
    await context.update_progress(mode="determinate", phase="work", message="迟到的进度")

    async with job_db.session() as session:
        row = await jobs.get_job(session, created.job.id)
        events = await jobs.job_events(session, created.job.id)
    assert row is not None
    assert row.status is JobStatus.SUCCEEDED
    assert row.progress.get("message") != "迟到的进度"
    assert all(event.event_type != "progress" for event in events)


async def test_job_list_slims_bulky_plan_but_detail_keeps_it(
    job_db, job_client: AsyncClient
) -> None:
    """列表响应裁掉 MB 级计划快照（任务中心高频拉取），详情保留完整输入。"""
    big_plan = {"library_id": 1, "renames": [{"source_path": f"/a/{i}.mkv"} for i in range(50)]}
    async with job_db.session() as session:
        created = await jobs.create_job(
            session,
            job_type="library.organize",
            input_data={"library_id": 1, "plan": big_plan},
        )

    listed = await job_client.get("/jobs")
    item = next(x for x in listed.json()["data"]["items"] if x["id"] == created.job.id)
    assert "plan" not in item["input_data"]
    assert item["input_data"]["library_id"] == 1  # 小键保留，前端详情芯片仍可用

    detail = await job_client.get(f"/jobs/{created.job.id}")
    assert detail.json()["data"]["input_data"]["plan"] == big_plan
