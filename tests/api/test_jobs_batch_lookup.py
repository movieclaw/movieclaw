"""``list_jobs_by_resource`` 与逐个 ``list_jobs`` 的等价性守护测试。

批量版存在的唯一理由是替掉媒体库列表页的 N+1（79 个库 = 79 次 join 查询）。
既然是"替掉"，它就必须与被替掉的那个逐字一致——否则首页卡片和单库详情页
会对同一个库显示不同的扫描状态。这里把等价性钉成测试，而不是靠人肉比对。
"""

from __future__ import annotations

from datetime import timedelta

import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import jobs
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import Job, JobResource, utcnow

_LIBRARIES = [1, 2, 3]
# 刻意大于 limit_per_resource，才能验证"每库取前 N"而不是"总共取 N"
_JOBS_PER_LIBRARY = 14
_LOOKBACK = 10


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(session) -> None:
    """每个库灌 _JOBS_PER_LIBRARY 条扫描作业，外加一条别的类型作为干扰项。"""
    now = utcnow()
    for library_id in _LIBRARIES:
        for n in range(_JOBS_PER_LIBRARY):
            job_id = f"job-{library_id}-{n}"
            session.add(
                Job(
                    id=job_id,
                    job_type="library.scan",
                    status="succeeded",
                    origin="scheduler",
                    # 时间倒着排：越靠后的 n 越旧，用于验证倒序取前 N
                    created_at=now - timedelta(minutes=n),
                    updated_at=now - timedelta(minutes=n),
                )
            )
            session.add(
                JobResource(
                    job_id=job_id,
                    resource_type="library",
                    resource_id=str(library_id),
                    relation="target",
                )
            )
        # 干扰项：同一个库下别的类型的作业，按 job_type 过滤时不该混进来
        session.add(
            Job(
                id=f"job-{library_id}-organize",
                job_type="library.organize",
                status="succeeded",
                origin="user",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            JobResource(
                job_id=f"job-{library_id}-organize",
                resource_type="library",
                resource_id=str(library_id),
                relation="target",
            )
        )
    await session.commit()


async def test_批量查询与逐个查询返回完全相同的作业序列(db) -> None:
    async with db.session() as session:
        await _seed(session)

        batch = await jobs.list_jobs_by_resource(
            session,
            resource_type="library",
            resource_ids=_LIBRARIES,
            job_type="library.scan",
            limit_per_resource=_LOOKBACK,
        )
        for library_id in _LIBRARIES:
            one_by_one = await jobs.list_jobs(
                session,
                resource_type="library",
                resource_id=library_id,
                job_type="library.scan",
                limit=_LOOKBACK,
            )
            assert [job.id for job in batch[str(library_id)]] == [
                job.id for job in one_by_one
            ], f"库 {library_id} 的批量结果与逐个查询不一致"


async def test_每个资源各取前_N_条而不是总共_N_条(db) -> None:
    """N+1 改批量最容易踩的坑：LIMIT 加在了整体而不是每个分区上。"""
    async with db.session() as session:
        await _seed(session)
        batch = await jobs.list_jobs_by_resource(
            session,
            resource_type="library",
            resource_ids=_LIBRARIES,
            job_type="library.scan",
            limit_per_resource=_LOOKBACK,
        )
        for library_id in _LIBRARIES:
            assert len(batch[str(library_id)]) == _LOOKBACK


async def test_按_job_type_过滤_干扰类型不混入(db) -> None:
    async with db.session() as session:
        await _seed(session)
        batch = await jobs.list_jobs_by_resource(
            session,
            resource_type="library",
            resource_ids=_LIBRARIES,
            job_type="library.scan",
            limit_per_resource=_LOOKBACK,
        )
        assert all(
            job.job_type == "library.scan"
            for rows in batch.values()
            for job in rows
        )


async def test_边界_空输入不打库_不存在的资源回空列表(db) -> None:
    async with db.session() as session:
        await _seed(session)
        assert await jobs.list_jobs_by_resource(
            session, resource_type="library", resource_ids=[]
        ) == {}
        # 键必须齐全（调用方按库 id 取值，缺键会变成 KeyError）
        result = await jobs.list_jobs_by_resource(
            session, resource_type="library", resource_ids=[999], job_type="library.scan"
        )
        assert result == {"999": []}


async def test_单条查询仍然可用_没有被批量版取代(db) -> None:
    """守住另一条路径：单库详情页仍走 list_jobs，两者必须都活着。"""
    async with db.session() as session:
        await _seed(session)
        rows = await jobs.list_jobs(
            session,
            resource_type="library",
            resource_id=1,
            job_type="library.scan",
            limit=_LOOKBACK,
        )
        assert len(rows) == _LOOKBACK
        assert (await session.execute(select(Job).limit(1))).scalars().first() is not None
