"""应用内更新服务测试（docs/design/in-app-update.md M3）。

重点覆盖安全性质：sha256 不匹配/路径穿越/布局异常时绝不切换版本指针，
切换与回退的符号链接操作正确且可互换，非 Docker 部署形态拒绝更新。
网络部分（GitHub API）不在此测——那是薄封装，错误路径已转成中文提示。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path

import pytest

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services import app_update


@pytest.fixture
def updates_dir(tmp_path, monkeypatch):
    """把更新目录隔离到临时目录，并模拟 Docker entrypoint 环境。"""
    updates = tmp_path / "updates"
    monkeypatch.setenv("MOVIECLAW_UPDATES_DIR", str(updates))
    monkeypatch.setenv("MOVIECLAW_RUNTIME_VERSION", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    get_settings.cache_clear()
    app_update.reset_progress_for_tests()
    yield updates
    get_settings.cache_clear()


@pytest.fixture
def no_restart(monkeypatch):
    """拦截重启调度（否则测试进程会被优雅停机干掉），并记录调用。"""
    calls: list[int] = []
    monkeypatch.setattr(app_update, "schedule_restart", lambda code=42: calls.append(code))
    return calls


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_manifest(download_dir: Path, version: str = "0.2.0") -> dict:
    """按 build-release-artifacts.sh 的产物布局伪造一套下载完成的文件。"""
    backend = _tar_bytes(
        {
            "src/movieclaw_api/main.py": b"# main",
            "src/movieclaw_api/data/spec.json": b"{}",
            "alembic.ini": b"[alembic]",
            "alembic/env.py": b"# env",
        }
    )
    web = _tar_bytes({"apps/web/server.js": b"// server"})
    download_dir.mkdir(parents=True, exist_ok=True)
    (download_dir / "app-backend.tar.gz").write_bytes(backend)
    (download_dir / "app-web.tar.gz").write_bytes(web)
    files = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in (("app-backend.tar.gz", backend), ("app-web.tar.gz", web))
    }
    raw = json.dumps(
        {"schema": 1, "version": version, "requires_runtime": 1, "files": files}
    ).encode()
    return {"version": version, "requires_runtime": 1, "files": files, "raw": raw}


# ---------------------------------------------------------------------------
# 版本比较
# ---------------------------------------------------------------------------


def test_is_newer_semver_ordering():
    assert app_update._is_newer("0.10.0", "0.9.9")
    assert app_update._is_newer("1.0.0", "0.99.0")
    assert not app_update._is_newer("0.2.0", "0.2.0")
    # 主段长度对齐：1.2 与 1.2.0 等价
    assert not app_update._is_newer("1.2", "1.2.0")
    # 预发布比同主段正式版旧（semver 语义）
    assert app_update._is_newer("0.2.0", "0.2.0-beta.1")
    assert not app_update._is_newer("0.2.0-beta.1", "0.2.0")
    assert app_update._is_newer("0.2.1-beta", "0.2.0")
    # 预发布数字段按数值比较（beta.10 > beta.9，字符串序会反转）
    assert app_update._is_newer("1.2.0-beta.10", "1.2.0-beta.9")
    assert not app_update._is_newer("1.2.0-beta.9", "1.2.0-beta.10")
    assert app_update._is_newer("1.2.0-rc.1", "1.2.0-beta.9")


def test_is_newer_never_raises_on_weird_versions():
    # 混合数字/非数字段、完全不规范的版本号：退化为「不相等即更新」，绝不抛异常
    assert app_update._is_newer("0.2.x", "0.2.0")
    assert app_update._is_newer("0.2.0-beta.1", "0.2.0-1") in (True, False)
    assert not app_update._is_newer("garbage", "garbage")


def test_latest_app_release_filters_model_and_prerelease():
    releases = [
        {"tag_name": "torrent-ner-v2"},  # 模型发布：绝不能被当成应用 latest
        {"tag_name": "v0.3.0-rc1", "prerelease": True},
        {"tag_name": "v0.2.5", "draft": True},
        {"tag_name": "v0.2.0"},
        {"tag_name": "v0.10.0"},
        {"tag_name": "v0.3.0"},
    ]
    assert app_update._latest_app_release(releases)["tag_name"] == "v0.10.0"
    assert app_update._latest_app_release([{"tag_name": "torrent-ner-v9"}]) is None


# ---------------------------------------------------------------------------
# 应用（下载后的本地流程）
# ---------------------------------------------------------------------------


def test_apply_switches_current_and_prunes(updates_dir, tmp_path):
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    app_update._apply_downloaded(manifest, tmp_path / "dl")

    current = updates_dir / "current"
    assert current.is_symlink()
    assert Path(current).resolve().name == "v0.2.0"
    assert (current / "manifest.json").is_file()
    assert (current / "backend" / "src" / "movieclaw_api" / "main.py").is_file()
    assert (current / "web" / "apps" / "web" / "server.js").is_file()
    # 首次更新没有 previous
    assert not (updates_dir / "previous").exists()

    # 第二次更新：current → 新版本，previous → 旧版本；保留策略 keep_versions=2
    # 时最早的版本被清理（默认 5 则会保留，供回退选择器使用）
    manifest2 = _make_manifest(tmp_path / "dl2", "0.3.0")
    app_update._apply_downloaded(manifest2, tmp_path / "dl2")
    manifest3 = _make_manifest(tmp_path / "dl3", "0.4.0")
    app_update._apply_downloaded(manifest3, tmp_path / "dl3", keep_versions=2)
    assert Path(updates_dir / "current").resolve().name == "v0.4.0"
    assert Path(updates_dir / "previous").resolve().name == "v0.3.0"
    assert not (updates_dir / "versions" / "v0.2.0").exists()


def test_apply_retains_versions_within_keep_limit(updates_dir, tmp_path):
    """默认保留策略（5 个）下，历史版本目录留在盘上供回退选择器使用。"""
    for i, ver in enumerate(("0.2.0", "0.3.0", "0.4.0")):
        manifest = _make_manifest(tmp_path / f"dl{i}", ver)
        app_update._apply_downloaded(manifest, tmp_path / f"dl{i}")
    assert (updates_dir / "versions" / "v0.2.0").is_dir()
    assert (updates_dir / "versions" / "v0.3.0").is_dir()
    assert (updates_dir / "versions" / "v0.4.0").is_dir()
    # release-info 随版本目录落盘（这里没传 release_info，也要有占位记录）
    info = (updates_dir / "versions" / "v0.4.0" / "release-info.json").read_text("utf-8")
    assert '"version": "0.4.0"' in info


def test_apply_clears_bad_markers_and_backs_up_db(updates_dir, tmp_path):
    import sqlite3

    # 真实的 SQLite 库（备份走 sqlite3 backup API，假文件会被正确拒绝）
    db = Path(get_settings().database_url.split(":///", 1)[1])
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('data-1')")
    conn.commit()
    conn.close()
    state = updates_dir / "state"
    state.mkdir(parents=True)
    (state / "bad-0.2.0").touch()
    (state / "failures-0.2.0").write_text("1\n")

    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    app_update._apply_downloaded(manifest, tmp_path / "dl")

    assert not (state / "bad-0.2.0").exists()
    assert not (state / "failures-0.2.0").exists()
    backups = list((updates_dir / "backup").glob("movieclaw-*.db"))
    assert len(backups) == 1
    # 备份是可打开的完整数据库，数据在
    check = sqlite3.connect(backups[0])
    assert check.execute("SELECT v FROM t").fetchall() == [("data-1",)]
    check.close()


def test_apply_aborts_when_db_backup_fails(updates_dir, tmp_path):
    # 数据库文件损坏（非 SQLite 格式）→ 备份失败必须中止更新，不切换版本
    db = Path(get_settings().database_url.split(":///", 1)[1])
    db.write_bytes(b"not-a-sqlite-file")
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    with pytest.raises(RuntimeError, match="备份失败"):
        app_update._apply_downloaded(manifest, tmp_path / "dl")
    assert not (updates_dir / "current").exists()


def test_apply_prune_keeps_running_version(updates_dir, tmp_path, monkeypatch):
    """current 指向已标 bad 的 vB、实际运行 previous 的 vA 时更新 vC：
    previous 必须指向真正在运行的 vA（而非 bad 的 vB），vA 不能被清理。"""
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    app_update._apply_downloaded(_make_manifest(tmp_path / "d2", "0.3.0"), tmp_path / "d2")
    # 模拟 entrypoint 因 v0.3.0 坏而回落运行 v0.2.0
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.2.0")

    app_update._apply_downloaded(_make_manifest(tmp_path / "d3", "0.4.0"), tmp_path / "d3")
    assert Path(updates_dir / "current").resolve().name == "v0.4.0"
    assert Path(updates_dir / "previous").resolve().name == "v0.2.0"
    assert (updates_dir / "versions" / "v0.2.0").exists()


def test_apply_rejects_bad_checksum(updates_dir, tmp_path):
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    manifest["files"]["app-web.tar.gz"]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="校验和不匹配"):
        app_update._apply_downloaded(manifest, tmp_path / "dl")
    assert not (updates_dir / "current").exists()


def test_apply_rejects_path_traversal(updates_dir, tmp_path):
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    evil = _tar_bytes({"../evil.py": b"boom"})
    (tmp_path / "dl" / "app-backend.tar.gz").write_bytes(evil)
    manifest["files"]["app-backend.tar.gz"] = {
        "sha256": hashlib.sha256(evil).hexdigest(),
        "size": len(evil),
    }
    with pytest.raises(tarfile.TarError):
        app_update._apply_downloaded(manifest, tmp_path / "dl")
    assert not (updates_dir / "current").exists()
    assert not (tmp_path.parent / "evil.py").exists()


def test_apply_rejects_incomplete_layout(updates_dir, tmp_path):
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    incomplete = _tar_bytes({"src/movieclaw_api/main.py": b"# main"})  # 缺 alembic/spec
    (tmp_path / "dl" / "app-backend.tar.gz").write_bytes(incomplete)
    manifest["files"]["app-backend.tar.gz"] = {
        "sha256": hashlib.sha256(incomplete).hexdigest(),
        "size": len(incomplete),
    }
    with pytest.raises(RuntimeError, match="布局异常"):
        app_update._apply_downloaded(manifest, tmp_path / "dl")
    assert not (updates_dir / "current").exists()


# ---------------------------------------------------------------------------
# 回退
# ---------------------------------------------------------------------------


def test_rollback_swaps_current_and_previous(updates_dir, tmp_path, no_restart):
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    app_update._apply_downloaded(_make_manifest(tmp_path / "d2", "0.3.0"), tmp_path / "d2")
    app_update.reset_progress_for_tests()  # 直调 _apply_downloaded 会停在 applying 态

    app_update.rollback()
    assert Path(updates_dir / "current").resolve().name == "v0.2.0"
    assert Path(updates_dir / "previous").resolve().name == "v0.3.0"
    assert no_restart == [app_update.FULL_RESTART_EXIT_CODE]
    # 回退后处于 restarting 占位（重启窗口内拒绝并发操作）
    with pytest.raises(BadRequestException, match="正在进行中"):
        app_update.rollback()

    # 再次回退 = 撤销回退，切回新版（真实场景中重启后进度自然复位）
    app_update.reset_progress_for_tests()
    app_update.rollback()
    assert Path(updates_dir / "current").resolve().name == "v0.3.0"


def test_rollback_without_previous_falls_back_to_baseline(
    updates_dir, tmp_path, no_restart, monkeypatch
):
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.2.0")  # 正在运行该 overlay
    app_update.reset_progress_for_tests()
    app_update.rollback()
    assert not (updates_dir / "current").exists()
    assert no_restart == [app_update.FULL_RESTART_EXIT_CODE]


def test_rollback_skips_unusable_previous(updates_dir, tmp_path, no_restart, monkeypatch):
    """previous 指向坏版本时视同不存在：回退直接落基线，绝不安排一次
    「entrypoint 会拒绝目标、什么都没变」的全量重启。"""
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    app_update._apply_downloaded(_make_manifest(tmp_path / "d2", "0.3.0"), tmp_path / "d2")
    state = updates_dir / "state"
    state.mkdir(exist_ok=True)
    (state / "bad-0.2.0").touch()  # previous(v0.2.0) 被标坏
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.3.0")
    app_update.reset_progress_for_tests()

    assert app_update.build_status().has_previous is False
    app_update.rollback()
    assert not (updates_dir / "current").exists()  # 落基线而非 swap 到坏版本


def test_apply_skips_bad_current_as_previous_and_gc_markers(updates_dir, tmp_path):
    """current 指向坏版本、运行基线时更新：坏版本不配当 previous；
    其目录被清理后 bad/failures 标记一并 GC。"""
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    state = updates_dir / "state"
    state.mkdir(exist_ok=True)
    (state / "bad-0.2.0").touch()
    (state / "failures-0.2.0").write_text("1\n")

    app_update._apply_downloaded(_make_manifest(tmp_path / "d2", "0.3.0"), tmp_path / "d2")
    assert not (updates_dir / "previous").exists()
    assert not (updates_dir / "versions" / "v0.2.0").exists()
    assert not (state / "bad-0.2.0").exists()
    assert not (state / "failures-0.2.0").exists()


def test_status_exposes_previous_version(updates_dir, tmp_path, monkeypatch):
    """有可回退目标时，状态里给出具体版本号——回退 UI 要向用户明示落点。"""
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    app_update._apply_downloaded(_make_manifest(tmp_path / "d2", "0.3.0"), tmp_path / "d2")
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.3.0")

    status = app_update.build_status()
    assert status.has_previous is True
    assert status.previous_version == "0.2.0"


def test_status_exposes_last_abnormal_exit(updates_dir):
    """entrypoint 落盘的异常退出记录要外显到状态接口（自愈事件不能悄无声息）。"""
    state = updates_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "last-exit.json").write_text(
        json.dumps(
            {
                "ts": int(time.time()) - 60,
                "reason": "watchdog_unhealthy",
                "exit_code": 1,
                "detail": "完整健康链路连续失败，容器主动退出等待自动拉起",
            }
        ),
        encoding="utf-8",
    )

    status = app_update.build_status()
    assert status.last_abnormal_exit is not None
    assert status.last_abnormal_exit.reason == "watchdog_unhealthy"
    assert "健康链路" in status.last_abnormal_exit.detail


def test_status_hides_stale_or_broken_last_exit(updates_dir):
    """超出展示窗口的旧记录与损坏文件都按无记录处理，不打扰用户也不报错。"""
    state = updates_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    record = state / "last-exit.json"
    record.write_text(
        json.dumps({"ts": int(time.time()) - 8 * 86400, "reason": "api_crash",
                    "exit_code": 3, "detail": "后端进程异常退出"}),
        encoding="utf-8",
    )
    assert app_update.build_status().last_abnormal_exit is None

    record.write_text("not-json", encoding="utf-8")
    assert app_update.build_status().last_abnormal_exit is None


def test_status_exposes_inactive_overlay(updates_dir, tmp_path, monkeypatch):
    """current 指向的版本没在运行时，状态页外显原因（bad 回落场景）。"""
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    app_update._apply_downloaded(_make_manifest(tmp_path / "d2", "0.3.0"), tmp_path / "d2")
    state = updates_dir / "state"
    state.mkdir(exist_ok=True)
    (state / "bad-0.3.0").touch()
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.2.0")  # 回落运行 v0.2.0

    status = app_update.build_status()
    assert status.inactive_overlay_version == "0.3.0"
    assert "启动失败" in (status.inactive_overlay_reason or "")
    assert app_update._is_marked_bad("0.3.0") is True


# ---------------------------------------------------------------------------
# 陈旧 overlay 启动清场（镜像升级后 requires_runtime 低于新镜像）
# ---------------------------------------------------------------------------


def _write_overlay_dir(updates_dir: Path, version: str, requires: int) -> Path:
    """只落 manifest 的最小版本目录（清场只看 manifest，不要求布局完整）。"""
    vdir = updates_dir / "versions" / f"v{version}"
    vdir.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 1, "version": version, "requires_runtime": requires, "files": {}}
    (vdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return vdir


def test_prune_stale_overlays_cleans_leftovers_after_image_upgrade(
    updates_dir, tmp_path, monkeypatch
):
    """镜像 runtime 前进后：旧 runtime 的版本连指针带标记一起清掉，备份保留。"""
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    app_update._apply_downloaded(_make_manifest(tmp_path / "d2", "0.3.0"), tmp_path / "d2")
    state = updates_dir / "state"
    state.mkdir(exist_ok=True)
    (state / "bad-0.2.0").touch()
    backup = updates_dir / "backup" / "movieclaw-v0.2.0-20260802-133527.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.touch()
    monkeypatch.setenv("MOVIECLAW_RUNTIME_VERSION", "3")  # 模拟升级后的新镜像

    asyncio.run(app_update.prune_stale_overlays())

    assert not (updates_dir / "current").is_symlink()
    assert not (updates_dir / "previous").is_symlink()
    assert list((updates_dir / "versions").iterdir()) == []
    assert not (state / "bad-0.2.0").exists()
    assert backup.exists()  # 备份是跨镜像升级的最后恢复点，清场不动它
    assert app_update.build_status().inactive_overlay_version is None


def test_prune_stale_overlays_keeps_matching_future_and_broken(updates_dir, monkeypatch):
    """requires 等于/高于镜像 runtime 的不动（后者是镜像降级场景），清单损坏的也不碰。"""
    matching = _write_overlay_dir(updates_dir, "0.4.0", requires=3)
    future = _write_overlay_dir(updates_dir, "0.5.0", requires=4)
    broken = updates_dir / "versions" / "v0.9.9"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("not json", encoding="utf-8")
    app_update._atomic_symlink(future, updates_dir / "current")
    monkeypatch.setenv("MOVIECLAW_RUNTIME_VERSION", "3")

    asyncio.run(app_update.prune_stale_overlays())

    assert matching.is_dir() and future.is_dir() and broken.is_dir()
    assert (updates_dir / "current").is_symlink()


def test_prune_stale_overlays_noop_outside_docker(updates_dir, monkeypatch):
    """源码部署（无 runtime 环境变量）没有 overlay 机制，不做任何清理。"""
    stale = _write_overlay_dir(updates_dir, "0.1.0", requires=1)
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    asyncio.run(app_update.prune_stale_overlays())
    assert stale.is_dir()


def test_overlay_state_reason_distinguishes_runtime_direction(updates_dir, monkeypatch):
    """runtime 落后 → 引导升级镜像；runtime 超前的残留 → 明说已停用，不再误导升级。"""
    stale = _write_overlay_dir(updates_dir, "0.3.0", requires=1)
    future = _write_overlay_dir(updates_dir, "9.9.9", requires=9)
    monkeypatch.setenv("MOVIECLAW_RUNTIME_VERSION", "3")
    _version, usable, reason = app_update._overlay_state(stale)
    assert usable is False and "已停用" in reason and "需升级镜像" not in reason
    _version, usable, reason = app_update._overlay_state(future)
    assert usable is False and "需升级镜像" in reason


# ---------------------------------------------------------------------------
# 多版本回退选择器与数据兼容判定
# ---------------------------------------------------------------------------


def _make_db(path: Path, rev: str) -> None:
    """造一个带 alembic_version 表的最小 SQLite 库。"""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num TEXT)")
    conn.execute("DELETE FROM alembic_version")
    conn.execute("INSERT INTO alembic_version VALUES (?)", (rev,))
    conn.commit()
    conn.close()


def _make_backup(updates_dir: Path, version: str, rev: str, stamp: str) -> Path:
    """按真实命名与边车格式伪造一份「离开 version 时」的备份。"""
    backup_dir = updates_dir / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"movieclaw-v{version}-{stamp}.db"
    _make_db(backup, rev)
    sidecar = {"from_version": version, "alembic_rev": rev, "taken_at": "2026-08-01T10:00:00"}
    backup.with_name(backup.name + ".json").write_text(json.dumps(sidecar), encoding="utf-8")
    return backup


def test_backup_writes_sidecar_with_version_and_rev(updates_dir, tmp_path):
    db = Path(get_settings().database_url.split(":///", 1)[1])
    _make_db(db, "rev-abc")
    app_update._backup_database(updates_dir)
    backups = list((updates_dir / "backup").glob("movieclaw-*.db"))
    assert len(backups) == 1
    sidecar = json.loads(backups[0].with_name(backups[0].name + ".json").read_text("utf-8"))
    assert sidecar["from_version"] == app_update.__version__
    assert sidecar["alembic_rev"] == "rev-abc"
    assert sidecar["taken_at"]


def test_rollback_options_classifies_schema_action(updates_dir, tmp_path, monkeypatch):
    """三种数据兼容判定：迁移位置没变 → switch；变了但有对时备份 → restore；
    无备份 → unknown。正在运行的版本与标坏的版本不进候选。"""
    db = Path(get_settings().database_url.split(":///", 1)[1])
    _make_db(db, "rev-live")

    for i, ver in enumerate(("0.2.0", "0.3.0", "0.4.0", "0.5.0")):
        app_update._apply_downloaded(_make_manifest(tmp_path / f"d{i}", ver), tmp_path / f"d{i}")
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.5.0")

    (updates_dir / "backup").mkdir(exist_ok=True)
    for old in (updates_dir / "backup").glob("*"):
        old.unlink()  # 清掉 _apply_downloaded 产生的真实备份，用伪造的精确控制
    _make_backup(updates_dir, "0.4.0", "rev-live", "20260801-100000")  # 同 rev → switch
    _make_backup(updates_dir, "0.3.0", "rev-old", "20260801-090000")  # 异 rev → restore
    # 0.2.0 无备份 → unknown

    view = app_update._build_rollback_options("0.1.0", 5)
    by_version = {t.version: t for t in view.targets if t.kind == "version"}
    assert set(by_version) == {"0.4.0", "0.3.0", "0.2.0"}  # 0.5.0 在运行，不列
    assert by_version["0.4.0"].schema_action == "switch"
    assert by_version["0.3.0"].schema_action == "restore"
    assert by_version["0.3.0"].backup_taken_at == "2026-08-01T10:00:00"
    assert by_version["0.2.0"].schema_action == "unknown"
    baseline = next(t for t in view.targets if t.kind == "baseline")
    assert baseline.version == "0.1.0"
    assert view.versions_dir_bytes > 0


def test_rollback_to_specific_version_swaps_links(updates_dir, tmp_path, monkeypatch, no_restart):
    for i, ver in enumerate(("0.2.0", "0.3.0", "0.4.0")):
        app_update._apply_downloaded(_make_manifest(tmp_path / f"d{i}", ver), tmp_path / f"d{i}")
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.4.0")
    app_update.reset_progress_for_tests()

    message = app_update.rollback_to("0.2.0", restore_backup=False)
    assert "v0.2.0" in message
    assert Path(updates_dir / "current").resolve().name == "v0.2.0"
    # previous ← 回退前正在运行的版本：一步切回的撤销路径
    assert Path(updates_dir / "previous").resolve().name == "v0.4.0"
    assert not (updates_dir / "state" / "restore-pending.json").exists()
    assert no_restart == [43]


def test_rollback_to_with_restore_stages_pending(updates_dir, tmp_path, monkeypatch, no_restart):
    db = Path(get_settings().database_url.split(":///", 1)[1])
    _make_db(db, "rev-live")
    for i, ver in enumerate(("0.2.0", "0.3.0")):
        app_update._apply_downloaded(_make_manifest(tmp_path / f"d{i}", ver), tmp_path / f"d{i}")
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.3.0")
    app_update.reset_progress_for_tests()
    backup = _make_backup(updates_dir, "0.2.0", "rev-old", "20260801-100000")

    app_update.rollback_to("0.2.0", restore_backup=True)
    pending = json.loads((updates_dir / "state" / "restore-pending.json").read_text("utf-8"))
    assert pending["backup"] == str(backup)
    assert no_restart == [43]


def test_rollback_to_restore_requires_backup(updates_dir, tmp_path, monkeypatch, no_restart):
    for i, ver in enumerate(("0.2.0", "0.3.0")):
        app_update._apply_downloaded(_make_manifest(tmp_path / f"d{i}", ver), tmp_path / f"d{i}")
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.3.0")
    app_update.reset_progress_for_tests()
    for old in (updates_dir / "backup").glob("*"):
        old.unlink()

    with pytest.raises(BadRequestException, match="没有找到"):
        app_update.rollback_to("0.2.0", restore_backup=True)
    assert no_restart == []


def test_rollback_to_baseline_unlinks_current(updates_dir, tmp_path, monkeypatch, no_restart):
    app_update._apply_downloaded(_make_manifest(tmp_path / "d", "0.2.0"), tmp_path / "d")
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.2.0")
    app_update.reset_progress_for_tests()

    message = app_update.rollback_to("baseline", restore_backup=False)
    assert "镜像内置" in message
    assert not (updates_dir / "current").exists()
    assert Path(updates_dir / "previous").resolve().name == "v0.2.0"
    assert no_restart == [43]


def test_execute_pending_restore_swaps_db(updates_dir, tmp_path):
    db = Path(get_settings().database_url.split(":///", 1)[1])
    _make_db(db, "rev-new")
    backup = _make_backup(updates_dir, "0.2.0", "rev-old", "20260801-100000")
    app_update._stage_restore(updates_dir, backup, "v0.2.0")

    app_update.execute_pending_restore()
    assert app_update._read_alembic_rev(db) == "rev-old"  # 已换成备份内容
    assert not (updates_dir / "state" / "restore-pending.json").exists()
    # 换库前把现库（rev-new）又归档了一份：恢复操作自身不是数据的单向门
    archived = [
        b for b in (updates_dir / "backup").glob("movieclaw-*.db")
        if app_update._read_alembic_rev(b) == "rev-new"
    ]
    assert archived


def test_execute_pending_restore_ignores_stale(updates_dir, tmp_path):
    db = Path(get_settings().database_url.split(":///", 1)[1])
    _make_db(db, "rev-new")
    backup = _make_backup(updates_dir, "0.2.0", "rev-old", "20260801-100000")
    state_dir = updates_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "restore-pending.json").write_text(
        json.dumps({"backup": str(backup), "target": "v0.2.0", "requested_at": 1000.0}),
        encoding="utf-8",
    )

    app_update.execute_pending_restore()
    assert app_update._read_alembic_rev(db) == "rev-new"  # 陈旧暂存被忽略，库未动
    assert not (state_dir / "restore-pending.json").exists()  # 且已被消费清除


def test_prune_backups_keeps_restore_sources(updates_dir, tmp_path):
    """仍保留在盘上的版本，各自最近一份「离开时」的备份不能被清——
    它们是回退恢复源；其余按时间保留最近 5 份。"""
    for i in range(8):
        _make_backup(updates_dir, "0.9.9", "rev-x", f"20260801-10000{i}")
    old_restore_source = _make_backup(updates_dir, "0.2.0", "rev-old", "20260701-000000")
    import os as _os
    import time as _time

    stale = _time.time() - 86400
    _os.utime(old_restore_source, (stale, stale))  # 比 5 份池子里的都老

    app_update._prune_backups(updates_dir, {"0.2.0"})
    remaining = {b.name for b in (updates_dir / "backup").glob("movieclaw-*.db")}
    assert old_restore_source.name in remaining  # 0.2.0 目录还在 → 恢复源保留
    assert len(remaining) == 6  # 最近 5 份 + 1 份恢复源


def test_rollback_on_baseline_rejected(updates_dir, no_restart):
    with pytest.raises(BadRequestException, match="没有可回退"):
        app_update.rollback()
    assert no_restart == []


def test_rollback_rejected_outside_docker(updates_dir, monkeypatch, no_restart):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    with pytest.raises(BadRequestException, match="不支持"):
        app_update.rollback()


# ---------------------------------------------------------------------------
# 状态与更新前置校验
# ---------------------------------------------------------------------------


def test_build_status_reflects_env(updates_dir, monkeypatch):
    monkeypatch.setenv("MOVIECLAW_CODE_SOURCE", "overlay")
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.2.0")
    state = updates_dir / "state"
    state.mkdir(parents=True)
    (state / "bad-0.1.5").touch()
    status = app_update.build_status()
    assert status.code_source == "overlay"
    assert status.overlay_version == "0.2.0"
    assert status.runtime_version == 1
    assert status.can_update is True
    assert status.bad_versions == ["0.1.5"]


def test_build_status_dev_mode(updates_dir, monkeypatch):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    monkeypatch.delenv("MOVIECLAW_CODE_SOURCE", raising=False)
    status = app_update.build_status()
    assert status.code_source == "dev"
    assert status.can_update is False


@pytest.mark.asyncio
async def test_start_update_rejected_outside_docker(updates_dir, monkeypatch):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    with pytest.raises(BadRequestException, match="不支持应用内更新"):
        await app_update.start_update()


# ---------------------------------------------------------------------------
# NER 模型更新
# ---------------------------------------------------------------------------


@pytest.fixture
def models_dir(tmp_path, monkeypatch, updates_dir):
    models = tmp_path / "models"
    monkeypatch.setenv("MOVIECLAW_MODELS_DIR", str(models))
    get_settings.cache_clear()
    return models


def test_model_tag_ordering_and_latest_release():
    releases = [
        {"tag_name": "v0.2.0"},
        {"tag_name": "torrent-ner-v2"},
        {"tag_name": "torrent-ner-v10"},
        {"tag_name": "torrent-ner-v1"},
    ]
    latest = app_update._latest_model_release(releases)
    assert latest["tag_name"] == "torrent-ner-v10"
    assert app_update._latest_model_release([{"tag_name": "v1.0.0"}]) is None
    # 无法识别当前版本（老镜像无 tag 记录）时按 0 处理 → 一律视为可更新
    assert app_update._model_tag_num(None) == 0


def test_current_model_tag_reads_release_tag_file(tmp_path, monkeypatch):
    model = tmp_path / "ner"
    model.mkdir()
    monkeypatch.setenv("MOVIECLAW_NER_DIR", str(model))
    assert app_update._current_model_tag() is None
    (model / ".release-tag").write_text("torrent-ner-v1\n")
    assert app_update._current_model_tag() == "torrent-ner-v1"


def test_install_model_files_switches_pointer_and_prunes(models_dir, tmp_path):
    def make_download(tag: str) -> tuple[dict, Path]:
        dl = tmp_path / f"dl-{tag}"
        dl.mkdir()
        files = {}
        for name in app_update._MODEL_FILES:
            data = f"{tag}:{name}".encode()
            (dl / name).write_bytes(data)
            files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        return {"files": files}, dl

    manifest, dl = make_download("torrent-ner-v2")
    app_update._install_model_files("torrent-ner-v2", manifest, dl)
    current = models_dir / "current"
    assert current.is_symlink()
    assert Path(current).resolve().name == "torrent-ner-v2"
    assert (current / ".release-tag").read_text().strip() == "torrent-ner-v2"
    assert (current / "model.int8.onnx").is_file()

    # 安装更新版本后：指针切换、旧模型目录被清理
    manifest3, dl3 = make_download("torrent-ner-v3")
    app_update._install_model_files("torrent-ner-v3", manifest3, dl3)
    assert Path(current).resolve().name == "torrent-ner-v3"
    assert not (models_dir / "torrent-ner-v2").exists()


def test_install_model_files_protects_active_dir(models_dir, tmp_path, monkeypatch):
    """当前进程正在用的模型目录（MOVIECLAW_NER_DIR）不被清理：
    距重启生效有约 11 秒窗口，删了会让窗口内的 NER 推理失败。"""
    active = models_dir / "torrent-ner-v1"
    active.mkdir(parents=True)
    (active / "model.int8.onnx").write_bytes(b"old")
    monkeypatch.setenv("MOVIECLAW_NER_DIR", str(active))

    dl = tmp_path / "dl"
    dl.mkdir()
    files = {}
    for name in app_update._MODEL_FILES:
        data = f"v2:{name}".encode()
        (dl / name).write_bytes(data)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    app_update._install_model_files("torrent-ner-v2", {"files": files}, dl)

    assert active.is_dir()  # 活动目录被保护
    assert Path(models_dir / "current").resolve().name == "torrent-ner-v2"


def test_install_model_files_rejects_bad_checksum(models_dir, tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    for name in app_update._MODEL_FILES:
        (dl / name).write_bytes(b"data")
    manifest = {
        "files": {name: {"sha256": "0" * 64, "size": 4} for name in app_update._MODEL_FILES}
    }
    with pytest.raises(RuntimeError, match="校验和不匹配"):
        app_update._install_model_files("torrent-ner-v2", manifest, dl)
    assert not (models_dir / "current").exists()


@pytest.mark.asyncio
async def test_start_model_update_rejected_outside_docker(models_dir, monkeypatch):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    with pytest.raises(BadRequestException, match="不支持应用内更新模型"):
        await app_update.start_model_update()


# ---------------------------------------------------------------------------
# 更新清单签名（可选启用：配置 UPDATE_MANIFEST_PUBKEY 后强制验签）
# ---------------------------------------------------------------------------


def _signing_pair() -> tuple[bytes, str]:
    """生成测试密钥对：返回（私钥对象可签名的 raw 清单签名函数用密钥, 公钥 base64）。"""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    raw = b'{"schema":1}'
    sig_b64 = base64.b64encode(key.sign(raw))
    return raw, pub_b64, sig_b64  # type: ignore[return-value]


def test_manifest_signature_roundtrip(monkeypatch):

    raw, pub_b64, sig_b64 = _signing_pair()
    monkeypatch.setenv("UPDATE_MANIFEST_PUBKEY", pub_b64)
    get_settings.cache_clear()
    try:
        # 正确签名通过；尾随换行（脚本产物带 \n）也通过
        app_update._verify_manifest_signature(raw, sig_b64)
        app_update._verify_manifest_signature(raw, sig_b64 + b"\n")
        # 内容被篡改 → 拒绝
        with pytest.raises(BadRequestException, match="签名校验失败"):
            app_update._verify_manifest_signature(raw + b" ", sig_b64)
        # 签名本身是垃圾 → 拒绝
        with pytest.raises(BadRequestException, match="签名校验失败"):
            app_update._verify_manifest_signature(raw, b"not-base64!!")
        # 公钥配置无效 → 明确报配置错误
        monkeypatch.setenv("UPDATE_MANIFEST_PUBKEY", "bad key")
        get_settings.cache_clear()
        with pytest.raises(BadRequestException, match="配置无效"):
            app_update._verify_manifest_signature(raw, sig_b64)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 更新提醒提速：ETag 条件请求 / 启动首查 / dismiss 后版本变化重点亮
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_releases_uses_etag_cache(updates_dir, monkeypatch):
    """第二次请求带 If-None-Match，304 时直接用缓存（不计 GitHub 配额）。"""
    import httpx

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if request.headers.get("if-none-match") == 'W/"abc"':
            return httpx.Response(304)
        return httpx.Response(
            200, json=[{"tag_name": "v0.2.0"}], headers={"ETag": 'W/"abc"'}
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(app_update.httpx, "AsyncClient", patched_client)
    app_update.reset_release_cache_for_tests()
    try:
        first = await app_update._fetch_releases("更新")
        second = await app_update._fetch_releases("更新")
    finally:
        app_update.reset_release_cache_for_tests()
    assert first == second == [{"tag_name": "v0.2.0"}]
    assert "if-none-match" not in calls[0]
    assert calls[1].get("if-none-match") == 'W/"abc"'


@pytest.mark.asyncio
async def test_startup_check_runs_after_delay(updates_dir, monkeypatch):
    ran = []

    async def fake_task():
        ran.append(True)

    monkeypatch.setattr(app_update, "_STARTUP_CHECK_DELAY_SECONDS", 0)
    monkeypatch.setattr(app_update, "check_app_update_task", fake_task)
    app_update.start_startup_check()
    assert app_update._startup_check_task is not None
    await app_update._startup_check_task
    await app_update.close_startup_check()
    assert ran == [True]


@pytest.mark.asyncio
async def test_startup_check_skipped_outside_docker(updates_dir, monkeypatch):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    app_update.start_startup_check()
    assert app_update._startup_check_task is None


# ---------------------------------------------------------------------------
# 更新检查结果快照（侧栏更新徽标与「设置 → 应用」进页自动提示的数据源）
# ---------------------------------------------------------------------------


@pytest.fixture
def update_state(monkeypatch):
    """把快照读写替换成内存实例，免去建库与配置存储单例的初始化。"""
    from movieclaw_api.settings.schemas import AppUpdateStateSetting

    state = AppUpdateStateSetting()

    async def fake_get():
        return state

    async def fake_save(value):
        assert value is state

    monkeypatch.setattr(app_update, "get_app_update_state", fake_get)
    monkeypatch.setattr(app_update, "save_app_update_state", fake_save)
    return state


def _check_view(**kwargs) -> app_update.UpdateCheckView:
    base = dict(
        current_version=app_update.__version__,
        latest_version="9.9.9",
        update_available=True,
        compatible=True,
        requires_runtime=1,
        changelog="## v9.9.9",
        published_at="2026-08-01T00:00:00Z",
        latest_known_bad=False,
    )
    return app_update.UpdateCheckView(**{**base, **kwargs})


@pytest.mark.asyncio
async def test_record_app_check_writes_snapshot(update_state):
    await app_update._record_app_check(_check_view())
    pending = await app_update.read_pending()
    assert pending.app_version == "9.9.9"
    assert pending.app_compatible is True
    assert pending.app_changelog == "## v9.9.9"
    assert pending.checked_at


@pytest.mark.asyncio
async def test_record_app_check_clears_snapshot_when_up_to_date(update_state):
    await app_update._record_app_check(_check_view())
    # 装完新版后再查：快照必须熄灭，否则侧栏徽标会永远亮着
    await app_update._record_app_check(_check_view(update_available=False))
    assert (await app_update.read_pending()).app_version is None


@pytest.mark.asyncio
async def test_read_pending_filters_stale_snapshot_after_update(update_state):
    """装完新版重启后的窗口期：快照还写着"发现新版本 vX"、但当前运行的已是
    vX（下一轮检查才会清快照）。读取必须按当前版本过滤，徽标立刻熄灭。"""
    await app_update._record_app_check(
        _check_view(latest_version=app_update.__version__)
    )
    pending = await app_update.read_pending()
    assert pending.app_version is None
    assert pending.app_changelog == ""


@pytest.mark.asyncio
async def test_read_pending_filters_stale_model_snapshot(update_state, monkeypatch):
    """模型侧同理：快照里的 tag 不比当前生效的新就不再提醒。"""
    view = app_update.ModelUpdateCheckView(
        current_tag=None,
        latest_tag="torrent-ner-v2",
        update_available=True,
        installable=True,
        published_at="",
    )
    await app_update._record_model_check(view)
    # 当前无法识别模型（较早的镜像）：提醒照常
    monkeypatch.setattr(app_update, "_current_model_tag", lambda: None)
    assert (await app_update.read_pending()).model_tag == "torrent-ner-v2"
    # 装完 v2 重启后：快照未清也不再提醒
    monkeypatch.setattr(app_update, "_current_model_tag", lambda: "torrent-ner-v2")
    assert (await app_update.read_pending()).model_tag is None


@pytest.mark.asyncio
async def test_record_app_check_skips_known_bad_version(update_state):
    """曾在本机连续启动失败被回落的版本不提醒——让用户再装一次刚坑过自己的版本是误导。"""
    await app_update._record_app_check(_check_view(latest_known_bad=True))
    assert (await app_update.read_pending()).app_version is None


@pytest.mark.asyncio
async def test_record_model_check_requires_installable(update_state):
    """没带更新清单的模型发布装不进来，提醒了也没有对应操作，按无更新处理。"""
    view = app_update.ModelUpdateCheckView(
        current_tag="torrent-ner-v1",
        latest_tag="torrent-ner-v2",
        update_available=True,
        installable=False,
        published_at="",
    )
    await app_update._record_model_check(view)
    assert (await app_update.read_pending()).model_tag is None

    await app_update._record_model_check(view.model_copy(update={"installable": True}))
    assert (await app_update.read_pending()).model_tag == "torrent-ner-v2"
