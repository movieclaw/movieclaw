"""网页播放器端到端验证：真 HTTP + 真 ffmpeg + 真媒体文件。

其它用例各自只验一层——决策是纯函数、命令装配是纯函数、端点用假 ffmpeg
替身。这个文件把整条链路接起来跑一遍：**登录 → 决策 → 起会话 → 拉
playlist → 按 playlist 逐个取分片 → 拼起来能被 ffprobe 解出正确的流**。

它回答的是那个最终问题：「浏览器照着这个 URL 播，到底能不能出画面。」

标 ``integration``（需要系统 ffmpeg），CI 默认门禁跳过。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.playback.session import (
    get_session_manager,
    reset_session_manager,
)
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="需要系统 ffmpeg/ffprobe",
    ),
]

_PB = "/api/v1/playback"
_ADMIN = {"username": "admin", "password": "Sup3rSecret!"}
DURATION = 6

# 只能解 H.264+AAC 的浏览器：HEVC 源会被判需要转码，MKV 容器会被判需要 remux
CHROME = {
    "video": [{"codec": "h264"}],
    "audio": [{"codec": "aac"}],
    "containers": ["mp4", "hls-fmp4"],
}


def _run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, timeout=180)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-2000:]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}")
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
    # 本机不一定有显卡；固定走软件路径，让用例在任何机器上结论一致
    monkeypatch.setattr(
        "movieclaw_api.api.routes.playback.available_backends", lambda: ()
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


def make_media(root: Path, name: str, *, codec: str, container: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.{container}"
    encoder = {"h264": "libx264", "hevc": "libx265"}[codec]
    argv = [
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
        "-c:v", encoder, "-preset", "ultrafast",
        "-g", "50", "-keyint_min", "50",
        "-c:a", "aac", "-shortest", "-y", str(path),
    ]
    if codec == "hevc":
        argv[argv.index("-preset") + 2 : argv.index("-preset") + 2] = [
            "-x265-params", "log-level=none"
        ]
    _run(argv)
    return path


async def _seed(path: Path, *, codec: str, container: str) -> int:
    async with get_database().session() as session:
        library = await LibraryRepository(session).create(
            name=f"库-{path.stem}", kind="movie", root_paths=[str(path.parent)]
        )
        item = MediaItem(
            kind="movie", tmdb_id=abs(hash(path.stem)) % 10**6,
            title=path.stem, original_title=path.stem,
        )
        session.add(item)
        await session.flush()
        row = LibraryFile(
            library_id=library.id, media_item_id=item.id,
            file_path=str(path), size_bytes=path.stat().st_size,
            source=FileSource.SCANNED, state=FileState.IN_PLACE,
            container=container, video_codec=codec, resolution="240p",
            duration_seconds=DURATION,
            audio_streams=[{"codec": "aac", "channels": 2, "default": True}],
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


def probe_streams(path: Path) -> list[dict]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-1500:]
    import json

    return json.loads(proc.stdout)["streams"]


def fetch_whole_stream(client: TestClient, data: dict, tmp_path: Path) -> Path:
    """照着 playlist 把 init + 全部分片拉下来拼成一个可解码的 mp4。

    这一步就是浏览器干的事——能拼出可解码的流，才叫真的能播。
    """
    playlist = client.get(data["stream_url"])
    assert playlist.status_code == 200, playlist.text
    token = data["stream_url"].split("token=")[1]
    session_id = data["session_id"]

    names = [line.strip() for line in playlist.text.splitlines() if line.strip().endswith(".m4s")]
    assert names, f"playlist 里没有分片：\n{playlist.text}"

    init = client.get(f"{_PB}/sessions/{session_id}/init.mp4?token={token}")
    assert init.status_code == 200
    blob = bytearray(init.content)
    for name in names:
        seg = client.get(f"{_PB}/sessions/{session_id}/{name}?token={token}")
        assert seg.status_code == 200, f"{name}: {seg.status_code}"
        blob += seg.content
    out = tmp_path / "assembled.mp4"
    out.write_bytes(blob)
    return out


def start(client: TestClient, file_id: int, **extra) -> dict:
    resp = client.post(
        f"{_PB}/sessions", json={"file_id": file_id, "capability": CHROME, **extra}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def wait_for_segments(client: TestClient, data: dict, count: int = 1, timeout: float = 30.0):
    """等 playlist 里至少出现 count 个分片——边转边给，不必等全部转完。"""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = client.get(data["stream_url"]).text
        if sum(1 for line in text.splitlines() if line.strip().endswith(".m4s")) >= count:
            return
        time.sleep(0.2)
    raise AssertionError(f"等分片超时：\n{text}")


# ---------------------------------------------------------------------------


def test_mp4_source_is_direct_played_end_to_end(client, tmp_path):
    """MP4 + H.264 + AAC：档 0，浏览器直接拿原文件，服务端零开销。"""
    path = make_media(tmp_path / "m", "direct", codec="h264", container="mp4")
    file_id = client.portal.call(partial(_seed, path, codec="h264", container="mp4"))
    data = start(client, file_id)
    assert data["decision"]["tier"] == 0
    assert data["session_id"] is None

    resp = client.get(data["stream_url"])
    assert resp.status_code == 200
    assert resp.content == path.read_bytes()


def test_mkv_remux_stream_is_decodable_end_to_end(client, tmp_path):
    """MKV → 档 1 remux。把分片拉回来拼起来，必须能解出原样的 H.264 + AAC。"""
    path = make_media(tmp_path / "m", "remux", codec="h264", container="mkv")
    file_id = client.portal.call(partial(_seed, path, codec="h264", container="mkv"))
    data = start(client, file_id)
    assert data["decision"]["tier"] == 1, data["decision"]["reason"]

    wait_for_segments(client, data, count=2)
    assembled = fetch_whole_stream(client, data, tmp_path)
    streams = probe_streams(assembled)
    assert any(s["codec_name"] == "h264" for s in streams)
    assert any(s["codec_name"] == "aac" for s in streams)


def test_hevc_source_transcodes_and_is_decodable(client, tmp_path):
    """HEVC 在只认 H.264 的浏览器上 → 转码档。产物必须真的是 H.264。"""
    from movieclaw_api.settings import PlaybackPolicySetting
    from movieclaw_api.settings.store import get_setting_store

    async def _enable_software():
        await get_setting_store().set(
            PlaybackPolicySetting(software_transcode_enabled=True)
        )

    client.portal.call(_enable_software)

    path = make_media(tmp_path / "m", "hevc", codec="hevc", container="mkv")
    file_id = client.portal.call(partial(_seed, path, codec="hevc", container="mkv"))
    data = start(client, file_id)
    assert data["decision"]["outcome"] == "plan", data["decision"]
    assert data["decision"]["tier"] == 4  # 无硬件，软件转码

    wait_for_segments(client, data, count=1)
    assembled = fetch_whole_stream(client, data, tmp_path)
    assert any(s["codec_name"] == "h264" for s in probe_streams(assembled))


def test_hevc_without_consent_asks_instead_of_transcoding(client, tmp_path):
    """软件转码默认关闭：先问，不偷偷吃满 CPU（§3.6）。"""
    path = make_media(tmp_path / "m", "ask", codec="hevc", container="mkv")
    file_id = client.portal.call(partial(_seed, path, codec="hevc", container="mkv"))
    data = start(client, file_id)
    assert data["decision"]["outcome"] == "consent"
    assert data["session_id"] is None
    assert data["decision"]["setting_key"] == "software_transcode_enabled"
    assert data["decision"]["can_self_enable"] is True  # 超管可以自己开
    assert data["decision"]["cost_hint"]


def test_seek_starts_the_stream_later_in_the_file(client, tmp_path):
    """会话时间轴恒从 0 起；seek 后的内容更短，且不会掐过头。

    取 3 秒而不是 2 秒：源片关键帧在 0/2/4 秒，而 ffmpeg 的 input seek 落在
    **严格早于**请求时间的关键帧上——请求正好等于关键帧时间（2.0）反而会退到
    更前一个（0.0）。3.0 稳定落到 2.0。详见 ffmpeg_args 模块文档的实测表。
    """
    path = make_media(tmp_path / "m", "seek", codec="h264", container="mkv")
    file_id = client.portal.call(partial(_seed, path, codec="h264", container="mkv"))
    data = start(client, file_id, start_ms=3000)
    assert data["start_ms"] == 3000

    wait_for_segments(client, data, count=1)
    assembled = fetch_whole_stream(client, data, tmp_path)
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(assembled)],
        capture_output=True, timeout=60,
    )
    duration = float(proc.stdout.decode().strip())
    assert duration < DURATION  # 掐掉了开头
    assert duration > 1.0       # 但没掐过头


def test_stopping_a_session_removes_its_files(client, tmp_path):
    """会话结束即清盘——转码缓存与数据库同卷，不能越攒越多。"""
    path = make_media(tmp_path / "m", "cleanup", codec="h264", container="mkv")
    file_id = client.portal.call(partial(_seed, path, codec="h264", container="mkv"))
    data = start(client, file_id)
    wait_for_segments(client, data, count=1)

    root = Path(get_settings().transcode_dir)
    assert any(root.iterdir())
    assert client.delete(f"{_PB}/sessions/{data['session_id']}").status_code == 200
    assert list(root.iterdir()) == []


def test_embedded_subtitle_is_served_over_http(client, tmp_path):
    """内封字幕经 HTTP 取回来必须是能渲染的内容。

    PT 片源的字幕绝大多数是内封的——这条链路不通，等于对大部分片子没字幕。
    """
    ass = tmp_path / "styled.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize\nStyle: Karaoke,Arial,72\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:00.50,0:00:03.00,Karaoke,{\\pos(160,120)}特效字幕\n",
        encoding="utf-8",
    )
    srt = tmp_path / "plain.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:03,000\n纯文本字幕\n\n", encoding="utf-8")

    root = tmp_path / "m"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "subs.mkv"
    _run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
        "-i", str(ass), "-i", str(srt),
        "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map", "3:s",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
        "-c:a", "aac", "-c:s:0", "ass", "-c:s:1", "srt",
        "-shortest", "-y", str(path),
    ])

    async def _seed_with_subs():
        async with get_database().session() as session:
            library = await LibraryRepository(session).create(
                name="字幕库", kind="movie", root_paths=[str(root)]
            )
            item = MediaItem(kind="movie", tmdb_id=999, title="字幕片", original_title="Subs")
            session.add(item)
            await session.flush()
            row = LibraryFile(
                library_id=library.id, media_item_id=item.id,
                file_path=str(path), size_bytes=path.stat().st_size,
                source=FileSource.SCANNED, state=FileState.IN_PLACE,
                container="mkv", video_codec="h264", resolution="240p",
                duration_seconds=DURATION,
                audio_streams=[{"codec": "aac", "channels": 2, "default": True}],
                subtitle_streams=[
                    {"codec": "ass", "language": "chi", "default": True},
                    {"codec": "subrip", "language": "eng"},
                ],
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    file_id = client.portal.call(_seed_with_subs)
    data = start(client, file_id)
    urls = data["subtitle_urls"]
    plans = data["decision"]["subtitles"]
    assert len(urls) == 2 and len(plans) == 2
    assert [p["track_ref"] for p in plans] == ["embedded:0", "embedded:1"]
    assert [p["kind"] for p in plans] == ["ass", "vtt"]

    # ASS 原样下发：样式段与定位标签都要在，否则 JASSUB 渲染出的是没排版的白字
    styled = client.get(urls[0])
    assert styled.status_code == 200, styled.text
    assert "[V4+ Styles]" in styled.text
    assert "\\pos(160,120)" in styled.text
    assert "特效字幕" in styled.text

    # 文本轨按前端的要求转 VTT：<track> 只认这个格式
    plain = client.get(f"{urls[1]}&format=vtt")
    assert plain.status_code == 200, plain.text
    assert plain.text.startswith("WEBVTT")
    assert "纯文本字幕" in plain.text
