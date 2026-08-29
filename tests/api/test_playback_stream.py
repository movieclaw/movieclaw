"""网页播放器取流端点测试（docs/design/web-player.md §4.7）。

覆盖的是 HTTP 边界上真正会出事的地方：**签名 token 的每一种不符**、
**路径穿越**、会话归属隔离、Range 直出、strm 重定向、并发拒绝、以及
「开会话 → 拉 playlist → 取分片 → 心跳 → 停止」的完整链路。

转码进程用假 ffmpeg（一段 python 脚本）替身，所以这些用例不需要系统里有
ffmpeg，也能进 CI 的默认门禁。真 ffmpeg 的验证在
``tests/playback/test_transcode_integration.py``。
"""

from __future__ import annotations

import asyncio
import errno
import itertools
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from movieclaw_api.api.routes import playback as routes_playback
from movieclaw_api.api.routes import transcode_worker as routes_transcode_worker
from movieclaw_api.api.routes.transcode_worker import put_transcode_artifact
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.playback import session as session_mod
from movieclaw_api.services.playback.ffmpeg_args import TranscodeCommand
from movieclaw_api.services.playback.remote_signing import issue_remote_grant
from movieclaw_api.services.playback.remote_worker import get_remote_worker_registry
from movieclaw_api.services.playback.session import (
    get_session_manager,
    reset_session_manager,
)
from movieclaw_api.services.playback.signing import issue_stream_token
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan
from movieclaw_playback.hls_vod import SegmentPlan

_PB = "/api/v1/playback"
_ADMIN = {"username": "admin", "password": "Sup3rSecret!"}

# 只能解 H.264+AAC 的浏览器
CAPABILITY = {
    "video": [{"codec": "h264"}],
    "audio": [{"codec": "aac"}],
    "containers": ["mp4", "hls-fmp4"],
}

FAKE_FFMPEG = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("#EXTM3U\\n#EXT-X-VERSION:7\\n#EXT-X-MAP:URI=\\"init.mp4\\"\\n")
(out.parent / "init.mp4").write_bytes(b"INIT")
(out.parent / "seg00000.m4s").write_bytes(b"SEGMENT-DATA")
time.sleep(300)
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MOVIECLAW_TRANSCODE_DIR", str(tmp_path / "transcodes"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    reset_session_manager()

    # 假 ffmpeg：端点测试要验的是 HTTP 边界，不是转码内容
    def fake_build(
        plan, *, source_path, session_dir, start_ms=0, hw_backend=None, start_number=None
    ):
        playlist = Path(session_dir) / "index.m3u8"
        return TranscodeCommand(
            argv=["python3", "-c", FAKE_FFMPEG, str(playlist)],
            playlist_path=playlist,
            init_path=Path(session_dir) / "init.mp4",
        )

    monkeypatch.setattr(session_mod, "build_hls_command", fake_build)
    # 硬件探测不能在测试里真去摸 /dev/dri
    monkeypatch.setattr(
        "movieclaw_api.api.routes.playback.available_backends", lambda: ()
    )
    # 关键帧探测：测试用的是几百字节的假媒体，ffprobe 读不出关键帧会返回 None，
    # 引擎据此保守降级到转码档——那是引擎的正确行为，但会让这些端点用例测不到
    # 直通路径。给它一个正常 GOP 的值。
    monkeypatch.setattr(
        "movieclaw_api.services.playback.plan.probe_keyframe_interval",
        lambda path, duration: 4.0,
    )

    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/v1/auth/bootstrap", json=_ADMIN)
        yield c

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        get_session_manager().shutdown()
    )
    reset_session_manager()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


_seed_counter = itertools.count(1)


async def _seed(tmp_path: Path, *, container="mkv", codec="h264", strm=False) -> int:
    """建库 + 建条目 + 落一个真实存在的文件，返回 library_file.id。

    每次调用建独立的库与文件（名字带序号）——同一用例里要播两部片时，
    库名与路径都不能撞。
    """
    n = next(_seed_counter)
    media_root = tmp_path / f"movies{n}"
    media_root.mkdir(exist_ok=True)
    name = f"movie{n}.strm" if strm else f"movie{n}.{container}"
    path = media_root / name
    path.write_text("https://pan.example.com/a.mkv\n") if strm else path.write_bytes(
        b"FAKE-MEDIA-BYTES" * 64
    )
    async with get_database().session() as session:
        library = await LibraryRepository(session).create(
            name=f"电影库{n}", kind="movie", root_paths=[str(media_root)]
        )
        item = MediaItem(kind="movie", tmdb_id=n, title=f"示例{n}", original_title=f"Example{n}")
        session.add(item)
        await session.flush()
        row = LibraryFile(
            library_id=library.id,
            media_item_id=item.id,
            file_path=str(path),
            size_bytes=path.stat().st_size,
            source=FileSource.SCANNED,
            state=FileState.IN_PLACE,
            container=container,
            video_codec=codec,
            resolution="1080p",
            duration_seconds=600,
            audio_streams=[{"codec": "aac", "channels": 2, "default": True}],
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


def seed(client: TestClient, tmp_path: Path, **kwargs) -> int:
    return client.portal.call(partial(_seed, tmp_path, **kwargs))  # type: ignore[attr-defined]


def start_session(client: TestClient, file_id: int, **extra) -> dict:
    resp = client.post(
        f"{_PB}/sessions",
        json={"file_id": file_id, "capability": CAPABILITY, **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_transcoded_session_master_codecs_are_explicit():
    session = SimpleNamespace(
        plan=PlaybackPlan(
            tier=PlaybackTier.HARDWARE_TRANSCODE,
            file_id=1,
            container="hls-fmp4",
            video=VideoPlan(action="transcode", codec="h264", height=1080),
            audio=AudioPlan(action="transcode", track_ref="embedded:0", codec="aac", channels=2),
        )
    )
    assert routes_playback._master_playlist_codecs(session) == "avc1.640029,mp4a.40.2"


def test_master_codecs_are_omitted_when_source_audio_is_copied():
    session = SimpleNamespace(
        plan=PlaybackPlan(
            tier=PlaybackTier.HARDWARE_TRANSCODE,
            file_id=1,
            container="hls-fmp4",
            video=VideoPlan(action="transcode", codec="h264", height=1080),
            audio=AudioPlan(action="copy", track_ref="embedded:0", codec="aac"),
        )
    )
    assert routes_playback._master_playlist_codecs(session) is None


def test_decide_route_forwards_all_playback_preferences(client, tmp_path, monkeypatch):
    """预览决策与开会话必须收到同一组音轨、字幕和清晰度偏好。"""
    file_id = seed(client, tmp_path, container="mp4")
    plan = PlaybackPlan(
        tier=PlaybackTier.DIRECT_PLAY,
        file_id=file_id,
        container="mp4",
        video=VideoPlan(action="copy"),
        audio=AudioPlan(action="copy"),
    )
    file_call = {}

    async def fake_decide_for_file(*args, **kwargs):
        file_call.update(kwargs)
        return plan

    monkeypatch.setattr(
        routes_playback.playback_plan, "decide_for_file", fake_decide_for_file
    )
    response = client.post(
        f"{_PB}/decide",
        json={
            "file_id": file_id,
            "capability": CAPABILITY,
            "audio_track": "embedded:1",
            "subtitle_track": "embedded:0",
            "max_height": 720,
        },
    )
    assert response.status_code == 200, response.text
    assert file_call["preferred_audio"] == "embedded:1"
    assert file_call["preferred_subtitle"] == "embedded:0"
    assert file_call["max_height"] == 720

    unit_call = {}

    async def fake_library_files(*args, **kwargs):
        return []

    async def fake_decide_for_files(*args, **kwargs):
        unit_call.update(kwargs)
        return plan

    monkeypatch.setattr(
        routes_playback.playback_plan,
        "library_files_for_unit",
        fake_library_files,
    )
    monkeypatch.setattr(
        routes_playback.playback_plan,
        "decide_for_files",
        fake_decide_for_files,
    )
    response = client.post(
        f"{_PB}/decide",
        json={
            "media_item_id": 123,
            "capability": CAPABILITY,
            "audio_track": "embedded:1",
            "subtitle_track": "embedded:0",
            "max_height": 480,
        },
    )
    assert response.status_code == 200, response.text
    assert unit_call["preferred_audio"] == "embedded:1"
    assert unit_call["preferred_subtitle"] == "embedded:0"
    assert unit_call["max_height"] == 480


def test_execution_backend_uses_remote_when_local_backend_is_incompatible():
    """PGS 烧录不能因 VAAPI 排在首位而错过可用的远程 VideoToolbox。"""
    decision = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=1,
        container="hls-fmp4",
        video=VideoPlan(
            action="transcode", codec="h264", height=1080, burn_subtitle="embedded:0"
        ),
        audio=AudioPlan(action="copy"),
    )

    assert routes_playback._select_execution_backend(
        decision,
        available=("vaapi", "videotoolbox"),
        local_backends=("vaapi",),
        remote_video_available=True,
    ) == ("videotoolbox", True)


# ---------------------------------------------------------------------------
# 档 0：原文件直出
# ---------------------------------------------------------------------------


def test_direct_play_returns_signed_stream_url_without_session(client, tmp_path):
    """MP4+H264+AAC → 档 0，不起会话，直接给签名直出地址。"""
    file_id = seed(client, tmp_path, container="mp4")
    data = start_session(client, file_id)
    assert data["decision"]["outcome"] == "plan"
    assert data["decision"]["tier"] == 0
    assert data["session_id"] is None
    assert data["stream_url"].startswith(f"{_PB}/files/{file_id}/stream?token=")

    resp = client.get(data["stream_url"])
    assert resp.status_code == 200
    assert resp.content.startswith(b"FAKE-MEDIA-BYTES")


def test_direct_play_supports_range_requests(client, tmp_path):
    file_id = seed(client, tmp_path, container="mp4")
    url = start_session(client, file_id)["stream_url"]
    resp = client.get(url, headers={"Range": "bytes=0-15"})
    assert resp.status_code == 206
    assert resp.content == b"FAKE-MEDIA-BYTES"


def test_strm_entry_redirects_to_cloud_url(client, tmp_path):
    """strm 只允许直连，服务器零流量（硬边界 2）。"""
    file_id = seed(client, tmp_path, container="strm", strm=True)
    url = start_session(client, file_id)["stream_url"]
    resp = client.get(url, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://pan.example.com/a.mkv"


# ---------------------------------------------------------------------------
# 签名 token：每一种不符都必须 404（不给探测者区分的机会）
# ---------------------------------------------------------------------------


def test_stream_without_token_is_rejected(client, tmp_path):
    file_id = seed(client, tmp_path, container="mp4")
    assert client.get(f"{_PB}/files/{file_id}/stream").status_code == 422


def test_tampered_token_is_rejected(client, tmp_path):
    file_id = seed(client, tmp_path, container="mp4")
    url = start_session(client, file_id)["stream_url"]
    assert client.get(url + "tampered").status_code == 404


def test_token_for_another_file_is_rejected(client, tmp_path):
    """token 绑定文件 id：拿到一个也只能取那一个文件。"""
    first = seed(client, tmp_path, container="mp4")
    other = tmp_path / "movies1" / "other.mp4"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"OTHER")
    async def _second():
        async with get_database().session() as session:
            # 复用第一条的库与条目——不能硬编码 id，seed 计数器跨用例递增
            origin = await session.get(LibraryFile, first)
            row = LibraryFile(
                library_id=origin.library_id, media_item_id=origin.media_item_id,
                file_path=str(other), size_bytes=5,
                source=FileSource.SCANNED, state=FileState.IN_PLACE,
                container="mp4", video_codec="h264", resolution="1080p",
                duration_seconds=600,
                audio_streams=[{"codec": "aac", "channels": 2, "default": True}],
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    second = client.portal.call(_second)
    token = start_session(client, first)["stream_url"].split("token=")[1]
    assert client.get(f"{_PB}/files/{second}/stream?token={token}").status_code == 404


def test_expired_token_is_rejected(client, tmp_path):
    file_id = seed(client, tmp_path, container="mp4")
    token = client.portal.call(
        partial(issue_stream_token, member_id=0, file_id=file_id, session_id=None,
                ttl_seconds=-1)
    )
    assert client.get(f"{_PB}/files/{file_id}/stream?token={token}").status_code == 404


# ---------------------------------------------------------------------------
# 转码会话全链路
# ---------------------------------------------------------------------------


def test_session_lifecycle_playlist_segment_ping_stop(client, tmp_path):
    """MKV 容器 → 档 1 remux，起会话后 playlist / 分片 / 心跳 / 停止全通。"""
    file_id = seed(client, tmp_path, container="mkv")
    data = start_session(client, file_id)
    assert data["decision"]["tier"] == 1
    session_id = data["session_id"]
    assert session_id

    playlist = client.get(data["stream_url"])
    assert playlist.status_code == 200
    assert playlist.text.startswith("#EXTM3U")
    assert playlist.headers["cache-control"] == "no-store"

    token = data["stream_url"].split("token=")[1]
    segment = client.get(f"{_PB}/sessions/{session_id}/seg00000.m4s?token={token}")
    assert segment.status_code == 200
    assert segment.content == b"SEGMENT-DATA"
    init = client.get(f"{_PB}/sessions/{session_id}/init.mp4?token={token}")
    assert init.status_code == 200

    assert client.post(f"{_PB}/sessions/{session_id}/ping").status_code == 200
    assert client.delete(f"{_PB}/sessions/{session_id}").status_code == 200
    # 停掉之后 playlist 与心跳都应 404
    assert client.get(data["stream_url"]).status_code == 404
    assert client.post(f"{_PB}/sessions/{session_id}/ping").status_code == 404


def test_remote_only_backend_is_never_sent_to_local_ffmpeg(client, tmp_path, monkeypatch):
    """远程能力短暂不可用时，未授权的软件转码不能被静默启动。"""
    from movieclaw_api.services.playback import hwprobe

    monkeypatch.setattr(hwprobe, "available_backends", lambda: ("videotoolbox",))
    monkeypatch.setattr(routes_playback, "available_backends", lambda: ("videotoolbox",))
    monkeypatch.setattr(routes_playback, "available_local_backends", lambda: ())
    monkeypatch.setattr(routes_playback, "remote_worker_available", lambda _: False)

    file_id = seed(client, tmp_path, container="mkv", codec="hevc")
    response = client.post(
        f"{_PB}/sessions", json={"file_id": file_id, "capability": CAPABILITY}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["decision"]["outcome"] == "consent"
    assert data["session_id"] is None
    assert get_session_manager().active() == []


def test_quality_switch_releases_remote_worker_before_final_decision(
    client, tmp_path, monkeypatch
):
    """同一 Worker 只有一个槽位时，连续切画质仍应保持远程硬件转码。"""
    from movieclaw_api.services.playback import hwprobe

    availability = {"value": False}

    def remote_available(_backend: str) -> bool:
        return availability["value"]

    monkeypatch.setattr(
        hwprobe,
        "available_backends",
        lambda: ("videotoolbox",) if availability["value"] else (),
    )
    monkeypatch.setattr(
        routes_playback,
        "available_backends",
        lambda: ("videotoolbox",) if availability["value"] else (),
    )
    monkeypatch.setattr(routes_playback, "available_local_backends", lambda: ())
    monkeypatch.setattr(routes_playback, "remote_worker_available", remote_available)
    monkeypatch.setattr(
        routes_playback,
        "effective_remote_transcode_config",
        lambda: SimpleNamespace(base_url="http://nas.local"),
    )

    policy = client.put(
        f"{_PB}/policy", json={"software_transcode_enabled": True}
    )
    assert policy.status_code == 200, policy.text

    file_id = seed(client, tmp_path, container="mkv", codec="hevc")
    manager = get_session_manager()
    old_id = "old-remote-session"
    old_directory = Path(get_settings().transcode_dir) / old_id
    old_directory.mkdir(parents=True, exist_ok=True)
    old_plan = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=file_id,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264", height=1080),
        audio=AudioPlan(action="copy", track_ref=None),
        reason="测试",
    )
    manager._sessions[old_id] = session_mod.TranscodeSession(
        id=old_id,
        file_id=file_id,
        member_id=0,
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        directory=old_directory,
        start_ms=0,
        plan=old_plan,
        remote=True,
    )

    original_stop_for_file = manager.stop_for_file

    async def stop_and_release(file_id_value: int, member_id: int) -> int:
        replaced = await original_stop_for_file(file_id_value, member_id)
        if replaced:
            availability["value"] = True
        return replaced

    monkeypatch.setattr(manager, "stop_for_file", stop_and_release)

    async def fake_spawn_remote(remote_session, _base_url: str) -> None:
        remote_session.state = "ready"
        remote_session.remote_worker_id = "mac-mini"
        # 模拟这次新会话占用唯一的 Worker 槽位，下一次切换必须先停它。
        availability["value"] = False

    monkeypatch.setattr(manager, "_spawn_remote", fake_spawn_remote)

    for max_height in (720, 480):
        data = start_session(client, file_id, max_height=max_height)
        assert data["decision"]["tier"] == int(PlaybackTier.HARDWARE_TRANSCODE)
        assert data["hw_backend"] == "videotoolbox"
        current = manager.get(data["session_id"])
        assert current is not None
        assert current.remote is True
        assert current.hw_backend == "videotoolbox"


def test_init_segment_waits_until_fully_written(client, tmp_path, monkeypatch):
    """回归（2026-08-25 真机事故，iPhone 烧录必现「解码失败」）：ffmpeg 起转
    就创建 init.mp4，但 avio 缓冲让它长期 0 字节（实测 ~5 秒，比首个分片还
    晚落盘）。只等「文件存在」会把 0 字节的 init 以 immutable 缓存喂给
    AVPlayer——整个会话被毒缓存钉死。路由必须等到非空且写稳才下发。"""
    late_init = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("#EXTM3U\\n#EXT-X-VERSION:7\\n")
init = out.parent / "init.mp4"
init.touch()  # 存在但 0 字节——avio 缓冲未落盘的形态
(out.parent / "seg00000.m4s").write_bytes(b"SEG")
time.sleep(0.4)
init.write_bytes(b"REAL-INIT")  # 迟到的落盘
time.sleep(300)
"""

    def late_build(
        plan, *, source_path, session_dir, start_ms=0, hw_backend=None, start_number=None
    ):
        playlist = Path(session_dir) / "index.m3u8"
        return TranscodeCommand(
            argv=["python3", "-c", late_init, str(playlist)],
            playlist_path=playlist,
            init_path=Path(session_dir) / "init.mp4",
        )

    monkeypatch.setattr(session_mod, "build_hls_command", late_build)
    file_id = seed(client, tmp_path, container="mkv")
    data = start_session(client, file_id)
    token = data["stream_url"].split("token=")[1]
    init = client.get(f"{_PB}/sessions/{data['session_id']}/init.mp4?token={token}")
    assert init.status_code == 200
    assert init.content == b"REAL-INIT"  # 绝不能把 0 字节的半成品发出去


def test_session_start_ms_is_echoed_for_timeline_mapping(client, tmp_path):
    """会话时间轴恒从 0 起，start_ms 是前端换算回文件时间的唯一依据。"""
    file_id = seed(client, tmp_path, container="mkv")
    data = start_session(client, file_id, start_ms=90_000)
    assert data["start_ms"] == 90_000


def test_seeking_replaces_the_previous_session_for_the_same_file(client, tmp_path):
    """连拖进度条不能堆出一串 ffmpeg（§4.4）。"""
    file_id = seed(client, tmp_path, container="mkv")
    first = start_session(client, file_id)["session_id"]
    second = start_session(client, file_id, start_ms=60_000)["session_id"]
    assert first != second
    assert client.post(f"{_PB}/sessions/{first}/ping").status_code == 404
    assert client.post(f"{_PB}/sessions/{second}/ping").status_code == 200


def test_concurrency_limit_returns_503_with_chinese_message(client, tmp_path, monkeypatch):
    """资源耗尽是 503 而不是 4xx——不是客户端的错，稍后重试就能好。

    并发上限已不是配置项（按机器规格自动推导，limits.py），测试直接钉住
    路由模块里的常量。
    """
    monkeypatch.setattr(routes_playback, "MAX_REMUX_CONCURRENCY", 1)
    ids = [seed(client, tmp_path, container="mkv") for _ in range(2)]
    start_session(client, ids[0])
    resp = client.post(
        f"{_PB}/sessions", json={"file_id": ids[1], "capability": CAPABILITY}
    )
    assert resp.status_code == 503, resp.text
    message = resp.json()["message"]
    assert "上限" in message or "已满" in message
    assert "1/1" in message  # 告诉用户当前占用，而不是干巴巴一句「满了」


def test_low_disk_space_returns_503(client, tmp_path, monkeypatch):
    """盘要满了就别再转了——转码分片与 SQLite 同卷，写满会让数据库也写不进去，
    整个应用不可用。这是「宁可播不了，也不能把应用搞挂」。"""
    monkeypatch.setattr(
        session_mod.shutil, "disk_usage",
        lambda _p: type("U", (), {"total": 0, "used": 0, "free": 64 * 1024**2})(),
    )
    file_id = seed(client, tmp_path, container="mkv")
    resp = client.post(
        f"{_PB}/sessions", json={"file_id": file_id, "capability": CAPABILITY}
    )
    assert resp.status_code == 503, resp.text
    assert "磁盘剩余空间不足" in resp.json()["message"]


# ---------------------------------------------------------------------------
# 路径穿越：文件名来自 URL，必须白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "index.m3u8.bak",
        "seg0.m4s",
        "evil.sh",
        "init.mp4.evil",
    ],
)
def test_segment_names_outside_the_whitelist_are_rejected(client, tmp_path, name):
    file_id = seed(client, tmp_path, container="mkv")
    data = start_session(client, file_id)
    token = data["stream_url"].split("token=")[1]
    resp = client.get(
        f"{_PB}/sessions/{data['session_id']}/{name}?token={token}",
        follow_redirects=False,
    )
    assert resp.status_code in (307, 404), resp.text
    if resp.status_code == 307:
        # httpx 会把 ../ 规范化掉，跟随后仍必须打不到东西
        assert client.get(resp.headers["location"]).status_code in (401, 404, 405)


def test_session_token_cannot_be_used_on_another_session(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    data = start_session(client, file_id)
    token = data["stream_url"].split("token=")[1]
    assert client.get(f"{_PB}/sessions/other-session/index.m3u8?token={token}").status_code == 404


def test_plain_file_token_cannot_fetch_session_segments(client, tmp_path):
    """档 0 的 token 不带 session_id，不能拿去取分片。"""
    file_id = seed(client, tmp_path, container="mkv")
    data = start_session(client, file_id)
    bare = client.portal.call(
        partial(issue_stream_token, member_id=0, file_id=file_id, session_id=None)
    )
    resp = client.get(f"{_PB}/sessions/{data['session_id']}/init.mp4?token={bare}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 未登录与归属
# ---------------------------------------------------------------------------


def test_starting_a_session_requires_login(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    client.cookies.clear()
    resp = client.post(f"{_PB}/sessions", json={"file_id": file_id, "capability": CAPABILITY})
    assert resp.status_code in (401, 403)


def test_unknown_file_is_not_found(client):
    resp = client.post(f"{_PB}/sessions", json={"file_id": 99999, "capability": CAPABILITY})
    assert resp.status_code == 404


def test_request_without_target_is_rejected(client):
    resp = client.post(f"{_PB}/sessions", json={"capability": CAPABILITY})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PGS 位图字幕：.sup 走二进制直出，绝不进文本管线
# ---------------------------------------------------------------------------


def test_pgs_subtitle_served_as_binary_sup(client, tmp_path, monkeypatch):
    """.sup 是二进制位图流（含非法 UTF-8 字节）：必须逐字节原样下发。
    走文本管线（按编码解码再编码）会把它毁掉——libbitsub 直接解析失败。"""
    from movieclaw_playback.subtitles import SubtitleRef

    file_id = seed(client, tmp_path)
    # 含 0xFF/0x00 的假位图数据：任何「按文本解码」的路径都会在这里露馅
    raw = b"PG" + bytes(range(256)) * 4
    sup = tmp_path / "extracted.sup"
    sup.write_bytes(raw)
    async def fake_extract(file, index):
        return SubtitleRef(path=sup, format="sup")

    monkeypatch.setattr(
        "movieclaw_api.api.routes.playback.extract_embedded_subtitle_async",
        fake_extract,
    )
    token = client.portal.call(
        partial(issue_stream_token, member_id=0, file_id=file_id, session_id=None)
    )
    resp = client.get(f"{_PB}/files/{file_id}/subtitles?track=embedded:0&token={token}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/octet-stream")
    assert resp.content == raw


@pytest.mark.asyncio
async def test_embedded_subtitle_disconnect_cancels_extraction_without_asgi_cancel(
    monkeypatch,
):
    """客户端断开只产生受控的内部信号，不能把正常取消冒泡成 Uvicorn 错误。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hanging_extract(_file, _index):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    class DisconnectedRequest:
        async def is_disconnected(self):
            return True

    monkeypatch.setattr(
        routes_playback, "extract_embedded_subtitle_async", hanging_extract
    )
    awaitable = routes_playback._extract_subtitle_until_disconnect(
        DisconnectedRequest(), object(), 0
    )
    with pytest.raises(routes_playback._SubtitleClientDisconnected):
        await awaitable
    assert started.is_set()
    assert cancelled.is_set()


def test_abandoned_embedded_subtitle_request_returns_no_content(client, tmp_path, monkeypatch):
    """路由边界吞掉内部断开信号，真实服务端取消仍不被误报为 500。"""
    file_id = seed(client, tmp_path)

    async def disconnected_extract(_request, _file, _index):
        raise routes_playback._SubtitleClientDisconnected

    monkeypatch.setattr(
        routes_playback, "_extract_subtitle_until_disconnect", disconnected_extract
    )
    token = client.portal.call(
        partial(issue_stream_token, member_id=0, file_id=file_id, session_id=None)
    )
    resp = client.get(f"{_PB}/files/{file_id}/subtitles?track=embedded:0&token={token}")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# 外置 macOS Worker 数据面
# ---------------------------------------------------------------------------


def _install_remote_session(client: TestClient, tmp_path: Path, file_id: int) -> dict[str, str]:
    """只装配一个远程会话，专测 Worker 的 HTTPS 源/产物边界。"""
    session_id = "remote-session-test"
    attempt_id = "remote-attempt-test"
    directory = Path(get_settings().transcode_dir) / session_id
    directory.mkdir(parents=True, exist_ok=True)
    plan = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=file_id,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264", height=1080),
        audio=AudioPlan(action="copy", track_ref=None),
        reason="测试",
    )
    remote = session_mod.TranscodeSession(
        id=session_id,
        file_id=file_id,
        member_id=0,
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        directory=directory,
        start_ms=0,
        plan=plan,
        remote=True,
        remote_job_id=attempt_id,
    )
    manager = get_session_manager()
    manager._sessions[session_id] = remote
    source = client.portal.call(
        partial(issue_remote_grant, session_id=session_id, file_id=file_id, kind="source")
    )
    artifact = client.portal.call(
        partial(
            issue_remote_grant,
            session_id=session_id,
            file_id=file_id,
            kind="artifact",
            attempt_id=attempt_id,
        )
    )
    return {"session_id": session_id, "source": source, "artifact": artifact}


def test_remote_source_supports_range_without_mounting_nas(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)
    response = client.get(
        f"/api/v1/transcode-worker/sessions/{grants['session_id']}/source"
        f"?token={grants['source']}",
        headers={"Range": "bytes=0-15"},
    )
    assert response.status_code == 206
    assert response.content == b"FAKE-MEDIA-BYTES"
    assert response.headers["accept-ranges"] == "bytes"


def test_remote_artifact_upload_is_atomic_and_attempt_scoped(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)
    session_id = grants["session_id"]
    endpoint = f"/api/v1/transcode-worker/sessions/{session_id}/artifacts/seg00000.m4s"
    target = Path(get_settings().transcode_dir) / session_id / "seg00000.m4s"
    target.write_bytes(b"OLD")

    uploaded = client.put(f"{endpoint}?token={grants['artifact']}", content=b"NEW-SEGMENT")
    assert uploaded.status_code == 201
    assert target.read_bytes() == b"NEW-SEGMENT"

    stale = client.portal.call(
        partial(
            issue_remote_grant,
            session_id=session_id,
            file_id=file_id,
            kind="artifact",
            attempt_id="old-attempt",
        )
    )
    rejected = client.put(f"{endpoint}?token={stale}", content=b"STALE")
    assert rejected.status_code == 404
    assert target.read_bytes() == b"NEW-SEGMENT"


def _request_that_disconnects_after_body(
    body: bytes, *, declared_length: int
) -> Request:
    """模拟代理在请求体后关闭连接，覆盖 ffmpeg HLS PUT 的异常边界。"""
    messages = iter(
        [
            {
                "type": "http.request",
                "body": body,
                "more_body": True,
            },
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> dict:
        return next(messages)

    path = "/api/v1/transcode-worker/sessions/remote-session-test/artifacts/live.m3u8"
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"content-length", str(declared_length).encode())],
            "client": ("worker", 1234),
            "server": ("nas", 443),
            "root_path": "",
        },
        receive,
    )


def test_remote_artifact_upload_keeps_complete_body_on_client_disconnect(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)
    body = b"#EXTM3U\n#EXT-X-VERSION:7\n"
    request = _request_that_disconnects_after_body(body, declared_length=len(body))

    response = client.portal.call(
        partial(
            put_transcode_artifact,
            session_id=grants["session_id"],
            name="live.m3u8",
            request=request,
            token=grants["artifact"],
        )
    )

    target = Path(get_settings().transcode_dir) / grants["session_id"] / "live.m3u8"
    assert response.status_code == 201
    assert target.read_bytes() == body
    assert not list(target.parent.glob("*.upload"))


def _request_with_chunked_body(body: bytes) -> Request:
    """模拟 FFmpeg HLS 的 chunked PUT，覆盖无 Content-Length 的正常结束。"""
    midpoint = max(1, len(body) // 2)
    messages = iter(
        [
            {
                "type": "http.request",
                "body": body[:midpoint],
                "more_body": True,
            },
            {
                "type": "http.request",
                "body": body[midpoint:],
                "more_body": True,
            },
            {"type": "http.request", "body": b"", "more_body": False},
        ]
    )

    async def receive() -> dict:
        return next(messages)

    path = "/api/v1/transcode-worker/sessions/remote-session-test/artifacts/live.m3u8"
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"transfer-encoding", b"chunked")],
            "client": ("worker", 1234),
            "server": ("nas", 80),
            "root_path": "",
        },
        receive,
    )


def test_remote_artifact_upload_accepts_complete_chunked_body(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)
    body = b"#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n"
    request = _request_with_chunked_body(body)

    response = client.portal.call(
        partial(
            put_transcode_artifact,
            session_id=grants["session_id"],
            name="live.m3u8",
            request=request,
            token=grants["artifact"],
        )
    )

    target = Path(get_settings().transcode_dir) / grants["session_id"] / "live.m3u8"
    assert response.status_code == 201
    assert target.read_bytes() == body
    assert not list(target.parent.glob("*.upload"))


def test_remote_artifact_upload_discards_partial_body_on_client_disconnect(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)
    body = b"partial-playlist"
    request = _request_that_disconnects_after_body(body, declared_length=len(body) + 1)

    response = client.portal.call(
        partial(
            put_transcode_artifact,
            session_id=grants["session_id"],
            name="live.m3u8",
            request=request,
            token=grants["artifact"],
        )
    )

    target = Path(get_settings().transcode_dir) / grants["session_id"] / "live.m3u8"
    assert response.status_code == 499
    assert not target.exists()
    assert not list(target.parent.glob("*.upload"))
    session = get_session_manager().get(grants["session_id"])
    assert session is not None
    assert session.remote_failed_segments == set()
    assert session.remote_uploads[-1].status == 499


def test_playback_diagnostics_reports_remote_execution_and_upload_gap(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)
    session = get_session_manager().get(grants["session_id"])
    assert session is not None
    session.remote_worker_id = "mac-mini"
    session.hw_backend = "videotoolbox"
    session.segment_plan = SegmentPlan(
        boundaries=tuple(float(i * 4) for i in range(10)), duration_s=40.0
    )
    session.head_segment = 3
    session.completed_segments = {3}
    session.pending_segments[5] = 1
    session.last_requested_segment = 5
    session.last_requested_at_ms = 2_000
    session.last_served_segment = 4
    # 旧的并行请求可能晚于当前请求完成，不能让它把诊断游标退回 4。
    session.last_served_at_ms = 3_000
    session.last_segment_wait_ms = 2300
    session.last_segment_status = 404
    session.error = "source=https://nas.example/stream?token=secret"
    session.record_remote_upload(
        "seg00002.m4s",
        status=499,
        received_bytes=123,
        content_length=456,
        transfer_encoding="chunked",
    )
    session.record_remote_upload(
        "seg00004.m4s",
        status=499,
        received_bytes=123,
        content_length=456,
        transfer_encoding="chunked",
    )
    session.record_remote_upload(
        "seg00005.m4s",
        status=499,
        received_bytes=123,
        content_length=456,
        transfer_encoding="chunked",
    )
    token = client.portal.call(
        partial(
            issue_stream_token,
            member_id=0,
            file_id=file_id,
            session_id=grants["session_id"],
        )
    )

    response = client.get(
        f"{_PB}/sessions/{grants['session_id']}/diagnostics?token={token}"
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["processing_mode"] == "remote-hardware"
    assert data["execution_location"] == "remote_worker"
    assert data["backend"] == "videotoolbox"
    assert data["encoder"] == "h264_videotoolbox"
    assert data["worker_id"] == "mac-mini"
    assert data["worker_online"] is False
    assert data["highest_produced_segment"] == 3
    assert data["failed_segments"] == [5]
    assert data["historical_failed_segments"] == [2, 4]
    assert data["recent_uploads"][0]["name"] == "seg00005.m4s"
    assert data["recent_uploads"][0]["status"] == 499
    assert "secret" not in data["session_error"]


def test_playback_diagnostics_reports_remote_ffmpeg_failure_details(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)
    registry = get_remote_worker_registry()
    registry._job_states["remote-attempt-test"] = {
        "type": "job.failed",
        "job_id": "remote-attempt-test",
        "exit_code": 187,
        "error": "ffmpeg 退出码：187",
        "stderr_tail": "HTTP 输出失败 token=secret",
    }
    try:
        token = client.portal.call(
            partial(
                issue_stream_token,
                member_id=0,
                file_id=file_id,
                session_id=grants["session_id"],
            )
        )
        response = client.get(
            f"{_PB}/sessions/{grants['session_id']}/diagnostics?token={token}"
        )
    finally:
        registry._job_states.pop("remote-attempt-test", None)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["job_exit_code"] == 187
    assert data["job_error"] == "ffmpeg 退出码：187"
    assert "token=<redacted>" in data["job_stderr_tail"]


def test_remote_artifact_upload_maps_disk_quota_to_507(
    client, tmp_path, monkeypatch
):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)

    def fail_replace(_source, _target):
        raise OSError(errno.EDQUOT, "Disc quota exceeded")

    monkeypatch.setattr(routes_transcode_worker.os, "replace", fail_replace)
    response = client.put(
        f"/api/v1/transcode-worker/sessions/{grants['session_id']}/artifacts/init.mp4"
        f"?token={grants['artifact']}",
        content=b"INIT",
    )

    assert response.status_code == 507
    assert response.json()["code"] == "INSUFFICIENT_STORAGE"
    session = get_session_manager().get(grants["session_id"])
    assert session is not None
    assert session.remote_uploads[-1].status == 500


def test_remote_artifact_upload_rejects_oversized_body(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)
    configured = client.put(
        "/api/v1/transcode-worker/config",
        json={
            "enabled": False,
            "max_artifact_bytes": 3,
        },
    )
    assert configured.status_code == 200

    response = client.put(
        f"/api/v1/transcode-worker/sessions/{grants['session_id']}/artifacts/init.mp4"
        f"?token={grants['artifact']}",
        content=b"TOO-LONG",
    )
    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"


def test_remote_artifact_upload_rejects_finished_job(client, tmp_path):
    file_id = seed(client, tmp_path, container="mkv")
    grants = _install_remote_session(client, tmp_path, file_id)
    registry = get_remote_worker_registry()
    registry._job_states["remote-attempt-test"] = {"type": "job.failed"}
    try:
        response = client.put(
            f"/api/v1/transcode-worker/sessions/{grants['session_id']}/artifacts/init.mp4"
            f"?token={grants['artifact']}",
            content=b"STALE",
        )
        assert response.status_code == 404
    finally:
        registry._job_states.pop("remote-attempt-test", None)
