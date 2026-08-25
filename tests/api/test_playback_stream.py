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
import itertools
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.api.routes import playback as routes_playback
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.playback import session as session_mod
from movieclaw_api.services.playback.ffmpeg_args import TranscodeCommand
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
    monkeypatch.setattr(
        "movieclaw_api.api.routes.playback.extract_embedded_subtitle",
        lambda file, index: SubtitleRef(path=sup, format="sup"),
    )
    token = client.portal.call(
        partial(issue_stream_token, member_id=0, file_id=file_id, session_id=None)
    )
    resp = client.get(f"{_PB}/files/{file_id}/subtitles?track=embedded:0&token={token}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/octet-stream")
    assert resp.content == raw
