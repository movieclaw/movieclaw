"""网页播放器的观看状态与策略端点测试（docs/design/web-player.md §6.5 / §3.6）。

阈值三分支本身由 ``tests/`` 里的进度单测覆盖，这里测 HTTP 边界上真正会出事的
四件事：**续播点往返**、**片长由服务端算**（客户端报什么都不影响已看判定）、
**不可见库的单元一律 404**、**策略增量保存不覆盖别的字段**。
"""

from __future__ import annotations

import itertools
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository

_PB = "/api/v1/playback"
_ADMIN = {"username": "admin", "password": "Sup3rSecret!"}
_MEMBER = {"username": "family", "password": "family-pass-1"}

#: 播放单元的片长：10 分钟。已看阈值 90% → 540 秒是分界线。
_DURATION_S = 600


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    # 硬件探测不能在测试里真去摸 /dev/dri
    monkeypatch.setattr(
        "movieclaw_api.api.routes.playback.available_backends", lambda: ()
    )

    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/v1/auth/bootstrap", json=_ADMIN)
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


_seed_counter = itertools.count(1)


async def _seed(tmp_path: Path) -> tuple[int, int]:
    """建库 + 建条目 + 落一个在位文件，返回 (library_id, media_item_id)。"""
    n = next(_seed_counter)
    media_root = tmp_path / f"movies{n}"
    media_root.mkdir(exist_ok=True)
    path = media_root / f"movie{n}.mp4"
    path.write_bytes(b"FAKE-MEDIA-BYTES" * 64)
    async with get_database().session() as session:
        library = await LibraryRepository(session).create(
            name=f"电影库{n}", kind="movie", root_paths=[str(media_root)]
        )
        item = MediaItem(
            kind="movie", tmdb_id=n, title=f"示例{n}", original_title=f"Example{n}"
        )
        session.add(item)
        await session.flush()
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                file_path=str(path),
                size_bytes=path.stat().st_size,
                source=FileSource.SCANNED,
                state=FileState.IN_PLACE,
                container="mp4",
                video_codec="h264",
                resolution="1080p",
                duration_seconds=_DURATION_S,
                audio_streams=[{"codec": "aac", "channels": 2, "default": True}],
            )
        )
        await session.commit()
        return library.id, item.id


def seed(client: TestClient, tmp_path: Path) -> tuple[int, int]:
    return client.portal.call(partial(_seed, tmp_path))  # type: ignore[attr-defined]


def report(client: TestClient, item_id: int, **body) -> dict:
    resp = client.post(f"{_PB}/progress", json={"media_item_id": item_id, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def resume(client: TestClient, item_id: int) -> dict:
    resp = client.get(f"{_PB}/resume", params={"media_item_id": item_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# 续播点
# ---------------------------------------------------------------------------


def test_never_played_unit_resumes_from_zero(client, tmp_path):
    """从未播过不是错误——给全零状态，播放器从头放。"""
    _, item_id = seed(client, tmp_path)
    assert resume(client, item_id) == {
        "position_ms": 0,
        "played": False,
        "play_count": 0,
        "duration_ms": _DURATION_S * 1000,
        "audio_track": None,
        "subtitle_track": None,
    }


def test_start_then_progress_round_trips_resume_point(client, tmp_path):
    """开播计数、心跳记位置、续播读回来——播放器最核心的一条往返。"""
    _, item_id = seed(client, tmp_path)
    started = report(client, item_id, event="start")
    assert started["play_count"] == 1

    report(client, item_id, event="progress", position_ms=300_000)  # 50%
    state = resume(client, item_id)
    assert state["position_ms"] == 300_000
    assert state["played"] is False
    assert state["play_count"] == 1


def test_watching_past_threshold_marks_played_and_clears_resume(client, tmp_path):
    """播过 90% 即已看，续播点清零——下次点开是重看而不是跳到片尾。"""
    _, item_id = seed(client, tmp_path)
    report(client, item_id, event="start")
    stopped = report(client, item_id, event="stop", position_ms=570_000)  # 95%
    assert stopped["played"] is True
    assert stopped["position_ms"] == 0


def test_runtime_comes_from_server_not_client(client, tmp_path):
    """已看判定的分母是服务端算的片长。

    客户端只报位置、报不了片长——同一部片在网页端和 Jellyfin 客户端才会给出
    一致的已看结论。这里用「按客户端谎报的短片长算就该已看、按真片长算不该」
    的位置来验：300 秒是真片长的 50%，不能被判成已看。
    """
    _, item_id = seed(client, tmp_path)
    state = report(client, item_id, event="progress", position_ms=300_000)
    assert state["played"] is False
    assert state["duration_ms"] == _DURATION_S * 1000


def test_track_selection_is_remembered_and_partial_reports_keep_it(client, tmp_path):
    """轨记忆：报了就改，没报的项保持原值（心跳可能只报进度不报轨）。"""
    _, item_id = seed(client, tmp_path)
    report(
        client,
        item_id,
        event="start",
        audio_track="embedded:1",
        subtitle_track="external:zh.srt",
    )
    report(client, item_id, event="progress", position_ms=120_000)
    state = resume(client, item_id)
    assert state["audio_track"] == "embedded:1"
    assert state["subtitle_track"] == "external:zh.srt"

    report(client, item_id, event="progress", position_ms=180_000, subtitle_track="off")
    state = resume(client, item_id)
    assert state["subtitle_track"] == "off"
    assert state["audio_track"] == "embedded:1"  # 没报的音轨没被清掉


# ---------------------------------------------------------------------------
# 可见性
# ---------------------------------------------------------------------------


def _make_member(client: TestClient, *, library_ids: list[int]) -> None:
    """建一个成员并把可见库收成白名单（新建接口只收账号，白名单走编辑）。"""
    resp = client.post("/api/v1/members", json={**_MEMBER, "nickname": "家人"})
    assert resp.status_code == 200, resp.text
    member_id = resp.json()["data"]["id"]
    updated = client.put(
        f"/api/v1/members/{member_id}",
        json={"all_libraries": False, "library_ids": library_ids},
    )
    assert updated.status_code == 200, updated.text


def test_unit_outside_visible_libraries_is_not_found(client, tmp_path):
    """不可见库的播放单元：报进度和读续播点都 404，与「不存在」不可区分。"""
    _hidden_library_id, hidden_item_id = seed(client, tmp_path)
    visible_library_id, visible_item_id = seed(client, tmp_path)
    _make_member(client, library_ids=[visible_library_id])
    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json=_MEMBER).status_code == 200

    assert (
        client.get(f"{_PB}/resume", params={"media_item_id": hidden_item_id}).status_code
        == 404
    )
    assert (
        client.post(
            f"{_PB}/progress", json={"media_item_id": hidden_item_id, "event": "start"}
        ).status_code
        == 404
    )
    # 可见库里的同类单元照常可用——证明 404 来自可见性而不是别的原因
    assert resume(client, visible_item_id)["position_ms"] == 0


def test_progress_is_isolated_per_member(client, tmp_path):
    """各人看各的：成员的进度不会写进管理员的续播点。"""
    library_id, item_id = seed(client, tmp_path)
    _make_member(client, library_ids=[library_id])
    report(client, item_id, event="progress", position_ms=300_000)

    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json=_MEMBER).status_code == 200
    assert resume(client, item_id)["position_ms"] == 0

    report(client, item_id, event="progress", position_ms=120_000)
    assert resume(client, item_id)["position_ms"] == 120_000

    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json=_ADMIN).status_code == 200
    assert resume(client, item_id)["position_ms"] == 300_000


# ---------------------------------------------------------------------------
# 策略配置
# ---------------------------------------------------------------------------


def test_policy_defaults_and_hardware_is_probed_not_configured(client):
    data = client.get(f"{_PB}/policy").json()["data"]
    assert data["software_transcode_enabled"] is False  # 默认关闭（§3.6）
    assert data["hardware_available"] is False  # 实测结果，不是配置项
    assert data["hw_backends"] == []


def test_policy_saves_software_transcode_toggle(client):
    """同意弹窗翻开软转开关后要持久生效。"""
    data = client.put(
        f"{_PB}/policy", json={"software_transcode_enabled": True}
    ).json()["data"]
    assert data["software_transcode_enabled"] is True
    assert (
        client.get(f"{_PB}/policy").json()["data"]["software_transcode_enabled"] is True
    )


def test_policy_ignores_retired_limit_fields(client):
    """数字上限已撤为自动推导（limits.py）。旧版网页可能还带着这些字段来
    保存——忽略而不是 422，别把只想翻软转开关的老客户端挡在门外。"""
    resp = client.put(
        f"{_PB}/policy",
        json={"software_transcode_enabled": True, "max_transcode_concurrency": 4},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["software_transcode_enabled"] is True
    assert "max_transcode_concurrency" not in data


def test_policy_is_admin_only(client, tmp_path):
    library_id, _ = seed(client, tmp_path)
    _make_member(client, library_ids=[library_id])
    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json=_MEMBER).status_code == 200
    assert client.get(f"{_PB}/policy").status_code == 403
    assert client.put(f"{_PB}/policy", json={}).status_code == 403


# ---------------------------------------------------------------------------
# 续播并入开会话（§6.10：省掉起播链路里「先问 /resume 再开会话」的串行往返）
# ---------------------------------------------------------------------------

#: 与种子文件（h264/aac/mp4）匹配的能力：判成档 0 直出，不需要 ffmpeg
_CAPABILITY = {
    "video": [{"codec": "h264"}],
    "audio": [{"codec": "aac"}],
    "containers": ["mp4", "hls-fmp4"],
}


def start_session(client: TestClient, item_id: int, **extra) -> dict:
    resp = client.post(
        f"{_PB}/sessions",
        json={"media_item_id": item_id, "capability": _CAPABILITY, **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_session_without_start_ms_resumes_from_stored_position(client, tmp_path):
    """start_ms 缺省 = 服务端接续播点，整份观看状态随响应带回。"""
    _, item_id = seed(client, tmp_path)
    report(client, item_id, event="progress", position_ms=120_000)

    data = start_session(client, item_id)
    assert data["start_ms"] == 120_000
    assert data["watch"]["position_ms"] == 120_000
    assert data["watch"]["duration_ms"] == _DURATION_S * 1000
    assert data["decision"]["tier"] == 0


def test_session_explicit_start_ms_wins_over_resume(client, tmp_path):
    """显式给值（含 0）原样照办：seek 重开与「从头播」都不受续播点干扰。"""
    _, item_id = seed(client, tmp_path)
    report(client, item_id, event="progress", position_ms=120_000)

    assert start_session(client, item_id, start_ms=0)["start_ms"] == 0
    assert start_session(client, item_id, start_ms=90_000)["start_ms"] == 90_000


def test_session_replays_finished_unit_from_zero(client, tmp_path):
    """看完的重播从头开始——续播到最后三十秒等于点开就是片尾。"""
    _, item_id = seed(client, tmp_path)
    report(client, item_id, event="stop", position_ms=570_000)  # 95% → 已看

    data = start_session(client, item_id)
    assert data["start_ms"] == 0
    assert data["watch"]["played"] is True


def test_session_reuses_remembered_audio_track(client, tmp_path):
    """audio_track 缺省时用上次听的那条轨（记忆轨并入决策）。"""
    _, item_id = seed(client, tmp_path)
    report(client, item_id, event="start", audio_track="embedded:0")

    data = start_session(client, item_id)
    assert data["watch"]["audio_track"] == "embedded:0"
    assert data["decision"]["audio"]["track_ref"] == "embedded:0"


# ---------------------------------------------------------------------------
# 播放页条目信息（§6.10 路由只带 media_item_id，库归属服务端解析）
# ---------------------------------------------------------------------------


def test_playback_item_info_resolves_library(client, tmp_path):
    library_id, item_id = seed(client, tmp_path)
    resp = client.get(f"{_PB}/items/{item_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["media_item_id"] == item_id
    assert data["library_id"] == library_id
    assert data["kind"] == "movie"
    assert data["title"].startswith("示例")


def test_playback_item_outside_visible_libraries_is_not_found(client, tmp_path):
    """不可见与不存在同样 404——与决策接口同一判据。"""
    _hidden_library_id, hidden_item_id = seed(client, tmp_path)
    visible_library_id, visible_item_id = seed(client, tmp_path)
    _make_member(client, library_ids=[visible_library_id])
    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json=_MEMBER).status_code == 200

    assert client.get(f"{_PB}/items/{hidden_item_id}").status_code == 404
    assert client.get(f"{_PB}/items/{visible_item_id}").status_code == 200
    assert client.get(f"{_PB}/items/99999").status_code == 404
