"""缓存管理（docs/design/cache-management.md）：登记表守卫 + 统计/清理行为。

守卫测试是维护规范的执行者：源码里任何 ``data/...`` 字面量都必须被登记表覆盖，
登记项之间不得嵌套，data/ 根下的未登记条目要能被发现。业务功能新增落盘目录
却没登记时，这里会红——这是刻意的。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.storage import registry, service

_SRC = Path(__file__).resolve().parents[2] / "src"


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


def test_every_data_literal_in_source_is_registered():
    """src/ 下所有 data/... 路径字面量都必须落在某条登记目录之下。"""
    offenders: list[str] = []
    for file in _SRC.rglob("*.py"):
        if "builtin-skills" in file.parts:
            continue
        for literal in registry.iter_data_literals(file.read_text(encoding="utf-8")):
            if not registry.covers(literal):
                offenders.append(f"{file.relative_to(_SRC)}: {literal}")
    assert not offenders, (
        "发现未登记的 data/ 路径，请在 services/storage/registry.py 登记：\n" + "\n".join(offenders)
    )


def test_registry_keys_unique_and_not_nested():
    keys = [d.key for d in registry.DATA_DIRS]
    assert len(keys) == len(set(keys))
    defaults = [d.default.rstrip("/") for d in registry.DATA_DIRS]
    for a in defaults:
        for b in defaults:
            assert a == b or not b.startswith(a + "/"), f"登记项嵌套：{b} 位于 {a} 之下"


def test_registry_policy_is_consistent():
    for d in registry.DATA_DIRS:
        if d.group is registry.Group.DATA:
            assert not d.clearable and d.orphans is None, f"{d.key}：用户数据不允许清理"
        else:
            assert d.clearable or d.orphans is not None, f"{d.key}：缓存项至少要有一种清理方式"
            assert d.rebuild_cost is not registry.RebuildCost.NONE


def test_data_literal_extraction_handles_prefixes():
    literals = list(
        registry.iter_data_literals(
            'a = "./data/cache/images"\nb = Path("data/models/torrent-ner")\nc = "data/"\n'
        )
    )
    assert literals == ["data/cache/images", "data/models/torrent-ner", "data"]
    assert registry.covers("data/cache/images/ab")
    assert not registry.covers("data/brand-new-dir")


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """把整个 data/ 根与各登记目录指到临时目录，统计/清理都在里面进行。"""
    root = tmp_path / "data"
    monkeypatch.setenv("MOVIECLAW_DATA_DIR", str(root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{root / 'movieclaw.db'}")
    for env, sub in {
        "LOG_DIR": "logs",
        "MEDIA_DIR": "uploads",
        "METADATA_DIR": "metadata",
        "IMAGE_CACHE_DIR": "cache/images",
        "MOVIECLAW_TRICKPLAY_CACHE_DIR": "cache/playback-trickplay",
        "MOVIECLAW_PLAYBACK_SUBS_CACHE_DIR": "cache/playback-subs",
        "MOVIECLAW_SUBTITLE_GEN_CACHE_DIR": "cache/subtitle_gen",
        "SECRET_KEY_FILE": ".secret_key",
        "SITE_CONFIGS_DIR": "site-configs",
        "AGENT_WORKSPACE_DIR": "agent-workspace",
        "AGENT_SESSIONS_DIR": "agent-sessions",
        "AGENT_SKILLS_DIR": "agent-skills",
        "MOVIECLAW_UPDATES_DIR": "updates",
        "MOVIECLAW_WEB_PORT_FILE": "config/web-port",
        "MOVIECLAW_MODELS_DIR": "models/ner",
        "MOVIECLAW_TRANSCODE_DIR": "transcodes",
    }.items():
        monkeypatch.setenv(env, str(root / sub))
    get_settings.cache_clear()
    service.reset_for_tests()
    root.mkdir()
    yield root
    service.reset_for_tests()
    get_settings.cache_clear()


def _write(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_unregistered_entries_only_reports_strays(data_root):
    _write(data_root / "cache/images/ab/abc", 5)
    _write(data_root / "movieclaw.db-wal", 5)
    _write(data_root / "mystery/junk.bin", 5)
    _write(data_root / "cache/unknown-cache/x", 5)
    strays = registry.unregistered_entries()
    assert [p.name for p in strays] == ["unknown-cache", "mystery"]


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def test_usage_snapshot_sums_registered_dirs(data_root):
    _write(data_root / "cache/images/ab/abc", 100)
    _write(data_root / "cache/images/ab/abc.json", 20)
    _write(data_root / "movieclaw.db", 300)
    _write(data_root / "movieclaw.db-wal", 50)
    _write(data_root / "stray/file", 7)
    snapshot = asyncio.run(service.wait_for_usage())
    assert snapshot is not None
    by_key = {d.key: d for d in snapshot.dirs}
    assert by_key["cache.images"].bytes == 120
    assert by_key["cache.images"].entries == 1
    assert by_key["database"].bytes == 350
    assert by_key["logs"].exists is False and by_key["logs"].bytes == 0
    assert snapshot.cache_bytes == 120
    assert snapshot.data_bytes == 350 + 7
    assert [u.bytes for u in snapshot.unregistered] == [7]
    assert snapshot.disk_total > 0


def test_usage_returns_immediately_and_fills_in_background(data_root):
    """打开页面不能被统计卡住：第一次读取立刻返回「正在统计、暂无数据」。"""

    async def scenario():
        state = await service.usage()
        assert (state.usage, state.computing) == (None, True)
        return await service.wait_for_usage()

    snapshot = asyncio.run(scenario())
    assert snapshot is not None and snapshot.computed_at > 0


def test_usage_keeps_old_snapshot_until_refresh_lands(data_root):
    """快照没有 TTL：一直用到点刷新；重算期间旧数据仍然可读。"""

    async def scenario():
        first = await service.wait_for_usage()
        _write(data_root / "cache/images/ab/new", 99)
        assert (await service.usage()).usage is first  # 不会自己重算
        during = await service.usage(refresh=True)
        assert during.usage is first and during.computing is True
        return await service.wait_for_usage()

    assert asyncio.run(scenario()).cache_bytes == 99


def test_clean_marks_snapshot_stale_and_recomputes_in_background(data_root):
    """清理后不清空页面：旧快照继续返回，下一次读取拉起后台重算。"""

    async def scenario():
        _write(data_root / "cache/images/ab/abc", 100)
        first = await service.wait_for_usage()
        assert first is not None and first.cache_bytes == 100
        await service.clean("cache.images", "all")
        after = await service.usage()
        assert after.usage is first and after.computing is True
        return await service.wait_for_usage()

    assert asyncio.run(scenario()).cache_bytes == 0


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


def test_clean_all_removes_children_but_keeps_dir(data_root):
    _write(data_root / "cache/images/ab/abc", 100)
    _write(data_root / "cache/images/jellyfin-scaled/old.jpg", 40)
    result = asyncio.run(service.clean("cache.images", "all"))
    assert (result.removed, result.freed_bytes, result.skipped_busy) == (2, 140, 0)
    assert (data_root / "cache/images").is_dir()
    assert list((data_root / "cache/images").iterdir()) == []


def test_clean_skips_busy_entries(data_root, monkeypatch):
    live = _write(data_root / "transcodes/live-session/seg0.ts", 30)
    _write(data_root / "transcodes/dead-session/seg0.ts", 30)

    class _Session:
        id = "live-session"

    class _Manager:
        def active(self):
            return [_Session()]

    from movieclaw_api.services.playback import session as playback_session

    monkeypatch.setattr(playback_session, "get_session_manager", lambda: _Manager())
    result = asyncio.run(service.clean("transcodes", "all"))
    assert (result.removed, result.skipped_busy, result.freed_bytes) == (1, 1, 30)
    assert live.exists()
    assert not (data_root / "transcodes/dead-session").exists()


def test_clean_orphans_uses_probe_and_respects_busy(data_root, monkeypatch):
    _write(data_root / "cache/playback-trickplay/1/index.json", 10)
    _write(data_root / "cache/playback-trickplay/2/index.json", 10)
    _write(data_root / "cache/playback-trickplay/.2.deadbeef.part/sprite_0.jpg", 10)
    _write(data_root / "cache/playback-trickplay/notes.txt", 10)

    async def fake_ids(_model: str) -> set[int]:
        return {1}

    monkeypatch.setattr(registry, "_existing_ids", fake_ids)
    result = asyncio.run(service.clean("cache.trickplay", "orphans"))
    assert (result.removed, result.freed_bytes) == (1, 10)
    assert (data_root / "cache/playback-trickplay/1").exists()
    assert not (data_root / "cache/playback-trickplay/2").exists()
    # staging 与非整数命名的条目都不属于孤儿判定范围
    assert (data_root / "cache/playback-trickplay/.2.deadbeef.part").exists()
    assert (data_root / "cache/playback-trickplay/notes.txt").exists()


def test_clean_rejects_forbidden_modes(data_root):
    from movieclaw_api.exceptions import BadRequestException, NotFoundException

    with pytest.raises(BadRequestException):
        asyncio.run(service.clean("uploads", "all"))
    with pytest.raises(BadRequestException):
        asyncio.run(service.clean("metadata.images", "all"))
    with pytest.raises(BadRequestException):
        asyncio.run(service.clean("cache.images", "orphans"))
    with pytest.raises(NotFoundException):
        asyncio.run(service.clean("nope", "all"))


# ---------------------------------------------------------------------------
# 接口（管理员鉴权 + 信封）
# ---------------------------------------------------------------------------


@pytest.fixture
def client(data_root, monkeypatch):
    from fastapi.testclient import TestClient

    from movieclaw_api.services.auth import reset_auth_state
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box

    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.delenv("MOVIECLAW_WEB_PORT", raising=False)
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/v1/auth/bootstrap", json={"username": "admin", "password": "s3cret-pass"})
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()


def _poll_usage(client, timeout: float = 10.0) -> dict:
    """接口从不阻塞：先拿到「正在统计」，轮询到后台算完再取快照。"""
    deadline = time.monotonic() + timeout
    while True:
        resp = client.get("/api/v1/app/storage")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        state = body["data"]
        if state["usage"] is not None and not state["computing"]:
            return state["usage"]
        assert time.monotonic() < deadline, "后台统计迟迟没有结果"
        time.sleep(0.05)


def test_storage_endpoints(client, data_root):
    _write(data_root / "cache/images/ab/abc", 64)
    first = client.get("/api/v1/app/storage").json()["data"]
    assert first["computing"] is True and first["usage"] is None
    usage = _poll_usage(client)
    images = next(d for d in usage["dirs"] if d["key"] == "cache.images")
    assert images["bytes"] == 64 and images["clearable"] is True

    resp = client.post("/api/v1/app/storage/cache.images/clean", json={"mode": "all"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["freed_bytes"] == 64

    resp = client.post("/api/v1/app/storage/uploads/clean", json={"mode": "all"})
    assert resp.status_code == 400
    assert resp.json()["success"] is False
