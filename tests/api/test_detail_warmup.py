"""详情页起播预热的接线（services/playback/warmup.py）。

预热只替「上报过解码能力、放这部片可能走直通」的网页客户端做关键帧采样。
这里走真实路由验证两端接上了：决策接口按身份 + User-Agent 记下能力，详情接口
按同一个 User-Agent 查出来再判定。2026-09 NAS 实测：iOS UI 测试批量打开详情，
每部电影都被预热读盘（一部 15 GB 的 mkv 白读 171 MB）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, Library, LibraryFile, MediaItem

CHROME_UA = "Mozilla/5.0 (Macintosh) Chrome/140.0"
IOS_UA = "MovieClaw/1 CFNetwork/3860 Darwin/25.0"
#: Chrome 的真实能力快照：不认 mkv，mkv/H.264/AAC 在它上面走直通（档 1）。
CHROME_CAPABILITY = {
    "video": [{"codec": "h264"}, {"codec": "vp9"}, {"codec": "av1"}],
    "audio": [{"codec": "aac"}, {"codec": "opus"}],
    "containers": ["mp4", "hls-fmp4"],
}


@pytest.fixture
def stack(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'warm.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()
    video = tmp_path / "media" / "movie.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")

    async def _seed() -> None:
        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        async with get_database().session() as session:
            library = Library(name="电影", kind="movie", root_paths=[str(video.parent)])
            item = MediaItem(
                kind="movie", tmdb_id=27205, title="盗梦空间", original_title="Inception", year=2010
            )
            session.add_all([library, item])
            await session.flush()
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item.id,
                    file_path=str(video),
                    size_bytes=1,
                    source=FileSource.SCANNED,
                    state=FileState.IN_PLACE,
                    container="mkv",
                    video_codec="h264",
                    resolution="1080p",
                    bit_depth=8,
                    duration_seconds=7200,
                    audio_streams=[{"codec": "aac", "channels": 2, "default": True}],
                    subtitle_streams=[],
                )
            )
            await session.commit()
        await dispose_db()

    asyncio.run(_seed())

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal
    from movieclaw_api.services.playback import warmup

    probed: list[str] = []

    def fake_probe(path, duration):
        probed.append(path)
        return 2.0

    # 关键帧采样替换成计数：起播决策与详情预热共用同一个函数，这里只关心预热
    monkeypatch.setattr(warmup, "probe_keyframe_interval", fake_probe)
    warmup._capabilities.clear()
    warmup._in_flight.clear()

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="admin")
    with TestClient(app) as client:
        yield client, probed
    warmup._capabilities.clear()
    get_settings.cache_clear()


def _open_detail(client: TestClient, user_agent: str) -> None:
    resp = client.get("/api/v1/libraries/1/items/1", headers={"User-Agent": user_agent})
    assert resp.status_code == 200, resp.text


def _settle(probed: list[str], *, expect: int, timeout: float = 3.0) -> list[str]:
    """预热是 TestClient 事件循环里的后台任务，响应返回时它未必跑完。

    等到采样次数达到 ``expect``（或超时）再交给断言；expect=0 时等满一小段，
    让本该不发生的采样有机会冒出来——否则「没读盘」的断言没有意义。
    """
    deadline = time.monotonic() + (timeout if expect else 0.3)
    while time.monotonic() < deadline and len(probed) < max(expect, 1):
        time.sleep(0.02)
    return probed


def test_app_opening_details_never_reads_the_file(stack) -> None:
    """App / UI 测试没上报过网页能力：批量打开详情，一次采样都不做。"""
    client, probed = stack
    for _ in range(3):
        _open_detail(client, IOS_UA)
    assert _settle(probed, expect=0) == []


def test_browser_that_reported_capability_is_warmed(stack) -> None:
    """Chrome 播过片（决策接口记下能力）后打开这部 mkv 的详情：值得提前采样。"""
    client, probed = stack
    decide = client.post(
        "/api/v1/playback/decide",
        json={"media_item_id": 1, "capability": CHROME_CAPABILITY},
        headers={"User-Agent": CHROME_UA},
    )
    assert decide.status_code == 200, decide.text
    probed.clear()  # 决策接口本身的现场采样不算预热

    _open_detail(client, IOS_UA)  # 同一账号的 App 不借用浏览器的能力
    assert _settle(probed, expect=0) == []
    _open_detail(client, CHROME_UA)
    assert len(_settle(probed, expect=1)) == 1
