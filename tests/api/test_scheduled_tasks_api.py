"""定时任务的周期与启停可配置（GET/PUT /scheduled-tasks）。

调度器在测试里不启动（SCHEDULER_ENABLED=false）：接口改的是库里的定义，重排
交给单例——单例不在就只落库，下次启动按新定义加载。这里验三件事：列得出注册
的任务与默认周期；改间隔 / 改成每天固定时刻 / 停用都落库；非法值 400。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 任务是靠导入注册的（lifespan 启动时统一导入各领域包）：这里显式导入对账任务
# 所在模块，注册表里才有 library_reconcile 可播种、可列出
import movieclaw_api.services.library.scan  # noqa: E402, F401
from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.repositories.scheduled_task_repo import ScheduledTaskRepository
from movieclaw_scheduler.registry import iter_tasks


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()

    async def _seed() -> None:
        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        # 调度器没启动就不会把注册表同步进库：这里手动补齐，与启动时同一份定义
        async with get_database().session() as session:
            repo = ScheduledTaskRepository(session)
            for defn in iter_tasks():
                await repo.create_if_absent(
                    task_key=defn.key,
                    trigger_type=defn.default_trigger_type,
                    interval_seconds=defn.default_interval_seconds,
                    cron_expr=defn.default_cron,
                    enabled=defn.default_enabled,
                )
        await dispose_db()

    asyncio.run(_seed())

    from movieclaw_api.api.deps import require_admin
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_admin] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _reconcile(client: TestClient) -> dict:
    tasks = client.get("/api/v1/scheduled-tasks").json()["data"]
    return next(t for t in tasks if t["key"] == "library_reconcile")


def test_lists_registered_tasks_with_defaults(client: TestClient) -> None:
    task = _reconcile(client)
    assert task["title"] == "媒体库对账"
    assert task["enabled"] is True
    assert task["trigger_type"] == "interval" and task["interval_seconds"] == 6 * 3600


def test_update_interval_daily_cron_and_disable(client: TestClient) -> None:
    resp = client.put(
        "/api/v1/scheduled-tasks/library_reconcile",
        json={"enabled": True, "trigger_type": "interval", "interval_seconds": 3600},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["interval_seconds"] == 3600
    assert _reconcile(client)["interval_seconds"] == 3600

    resp = client.put(
        "/api/v1/scheduled-tasks/library_reconcile",
        json={"enabled": True, "trigger_type": "cron", "cron_expr": "30 3 * * *"},
    )
    assert resp.status_code == 200, resp.text
    task = _reconcile(client)
    assert task["trigger_type"] == "cron" and task["cron_expr"] == "30 3 * * *"

    resp = client.put(
        "/api/v1/scheduled-tasks/library_reconcile",
        json={"enabled": False, "trigger_type": "cron", "cron_expr": "30 3 * * *"},
    )
    assert resp.status_code == 200, resp.text
    assert _reconcile(client)["enabled"] is False


def test_rejects_bad_values(client: TestClient) -> None:
    too_short = client.put(
        "/api/v1/scheduled-tasks/library_reconcile",
        json={"enabled": True, "trigger_type": "interval", "interval_seconds": 5},
    )
    assert too_short.status_code == 400, too_short.text
    bad_cron = client.put(
        "/api/v1/scheduled-tasks/library_reconcile",
        json={"enabled": True, "trigger_type": "cron", "cron_expr": "not a cron"},
    )
    assert bad_cron.status_code == 400, bad_cron.text
    unknown = client.put(
        "/api/v1/scheduled-tasks/no_such_task",
        json={"enabled": True, "trigger_type": "interval", "interval_seconds": 3600},
    )
    assert unknown.status_code == 404, unknown.text
