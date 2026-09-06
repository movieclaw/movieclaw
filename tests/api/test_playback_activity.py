"""活动页「观看」视角接口（/playback/activity、设备注销）的测试。

成员越权由 tests/api/test_member_auth.py 的守护测试兜底（本路由挂
require_admin 且不在成员白名单），这里只测管理员视角的数据装配。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    JellyfinDevice,
    Library,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    PlaybackLog,
    PlaybackState,
)
from movieclaw_db.models.base import utcnow
from movieclaw_playback import activity
from movieclaw_playback.events import ClientInfo


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    activity.reset()

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post(
            "/api/v1/auth/bootstrap",
            json={"username": "admin", "password": "s3cret-pass"},
        )
        yield c

    activity.reset()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


async def test_empty_snapshot(client: TestClient) -> None:
    """全新实例：两段实时数据都为空，但结构完整。"""
    resp = client.get("/api/v1/playback/activity")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {
        "sessions": [],
        "downloads": [],
        "hidden_session_count": 0,
        "hidden_download_count": 0,
    }


async def test_assembles_sessions(client: TestClient) -> None:
    """实时会话补齐媒体信息与文件规格；持 Jellyfin 凭据的会话标为可注销。"""
    async with get_database().session() as session:
        movie = MediaItem(
            kind="movie",
            tmdb_id=27205,
            title="盗梦空间",
            original_title="Inception",
            year=2010,
            aliases=[],
        )
        library = Library(name="电影库", kind="movie", root_paths=["/media/movies"])
        session.add_all([movie, library])
        await session.commit()
        session.add(MediaMetadata(media_item_id=movie.id, runtime_minutes=148))
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=movie.id,
                file_path="/media/movies/inception.mkv",
                size_bytes=28_000_000_000,
                container="mkv",
                resolution="2160p",
                video_codec="hevc",
                hdr="HDR10",
                bit_rate=45_000_000,
                duration_seconds=8880,
                source="scanned",
            )
        )
        session.add(
            JellyfinDevice(
                member_id=0,
                token="tok-1",
                device_id="dev-1",
                client="Infuse",
                device_name="客厅 Apple TV",
                version="8.0",
                last_seen_at=utcnow(),
            )
        )
        session.add(
            JellyfinDevice(
                member_id=0,
                token="tok-2",
                device_id="dev-2",
                client="VidHub",
                device_name="卧室 iPad",
                version="2.0",
            )
        )
        session.add(
            PlaybackState(
                member_id=0,
                media_item_id=movie.id,
                position_ms=1_000_000,
                play_count=1,
                last_played_at=utcnow(),
            )
        )
        await session.commit()
        movie_id = movie.id
        library_id = library.id

    unit = (movie_id, 0, 0)
    info = ClientInfo(
        name="Infuse", device_name="客厅 Apple TV", device_id="dev-1", version="8.0"
    )
    activity.report_start("dev-1", member_id=0, client=info, unit=unit)
    activity.report_progress(
        "dev-1", member_id=0, client=info, unit=unit, position_ms=1_200_000, paused=False
    )

    resp = client.get("/api/v1/playback/activity")
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert len(data["sessions"]) == 1
    live = data["sessions"][0]
    assert live["member_name"] == "admin"
    assert live["device_name"] == "客厅 Apple TV"
    # 持 Jellyfin 设备凭据的会话可以注销
    assert live["revocable"] is True
    assert live["media"]["title"] == "盗梦空间"
    assert live["media"]["browsable"] is True
    # 详情页落点与文件规格来自在位台账行
    assert live["media"]["library_id"] == library_id
    assert live["file"]["resolution"] == "2160p"
    assert live["file"]["hdr"] == "HDR10"
    assert live["position_ms"] == 1_200_000
    assert live["duration_ms"] == 148 * 60_000
    assert live["progress_percent"] == 14
    # 本地台账文件：即使还没看到字节也是本地直连（上报先于取流是常态），
    # 速率此刻不可测 → null，而不是伪造成 0
    assert live["play_method"] == "local"
    assert live["rate_bytes_per_second"] is None
    # 快照只装页面渲染的实时数据：不再随轮询带回设备清单与最近观看
    assert set(data) == {
        "sessions",
        "downloads",
        "hidden_session_count",
        "hidden_download_count",
    }


async def test_download_connections_aggregate_per_file(client: TestClient) -> None:
    """同设备同文件的多条 Range 连接聚合为一条：速率与字节求和，不刷屏。"""
    async with get_database().session() as session:
        movie = MediaItem(
            kind="movie",
            tmdb_id=603,
            title="黑客帝国",
            original_title="The Matrix",
            year=1999,
            aliases=[],
        )
        session.add(movie)
        await session.commit()
        movie_id = movie.id

    info = ClientInfo(name="VidHub", device_name="iPad", device_id="dev-9", version="2.0")
    meters = [
        activity.register_stream(
            device_id="dev-9",
            kind=activity.STREAM_KIND_DOWNLOAD,
            member_id=0,
            unit=(movie_id, 0, 0),
            file_id=7,
            file_name="matrix.mkv",
            size_bytes=20_000,
            client=info,
        )
        for _ in range(3)
    ]
    for meter in meters:
        meter.add(1_000)

    data = client.get("/api/v1/playback/activity").json()["data"]
    assert len(data["downloads"]) == 1
    download = data["downloads"][0]
    assert download["connections"] == 3
    assert download["bytes_sent"] == 3_000
    assert download["media"]["title"] == "黑客帝国"
    # 三条连接都从 0 开始，最远位置就是单条已传量；20_000 字节的 5%
    assert download["position_bytes"] == 1_000
    assert download["progress_percent"] == 5


async def test_session_bytes_sent_spans_reconnects(client: TestClient) -> None:
    """已传输 = 已结束连接的累计 + 在服务连接的实时增量，换连接不归零。"""
    async with get_database().session() as session:
        movie = MediaItem(
            kind="movie",
            tmdb_id=27205,
            title="盗梦空间",
            original_title="Inception",
            year=2010,
            aliases=[],
        )
        library = Library(name="电影", kind="movie", root_paths=["/media/movies"])
        session.add_all([movie, library])
        await session.commit()
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=movie.id,
                file_path="/media/movies/inception.mkv",
                source="scanned",
                size_bytes=100_000,
            )
        )
        await session.commit()
        movie_id = movie.id

    unit = (movie_id, 0, 0)
    info = ClientInfo(
        name="Infuse", device_name="客厅 Apple TV", device_id="dev-3", version="8.0"
    )
    activity.report_start("dev-3", member_id=0, client=info, unit=unit)

    def _open() -> activity.StreamMeter:
        return activity.register_stream(
            device_id="dev-3",
            kind=activity.STREAM_KIND_PLAY,
            member_id=0,
            unit=unit,
            file_id=None,
            file_name="inception.mkv",
            size_bytes=100_000,
            client=info,
        )

    # 前两条 Range 连接已经结束（播放器 seek / 续拉缓冲换的连接）
    for sent in (30_000, 20_000):
        finished = _open()
        finished.add(sent)
        activity.unregister_stream(finished)
    live = _open()
    live.add(5_000)

    data = client.get("/api/v1/playback/activity").json()["data"]
    view = data["sessions"][0]
    assert view["bytes_sent"] == 55_000
    # 连接数仍是"当下在服务的条数"，与累计字节是两个读数
    assert view["connections"] == 1


async def test_strm_session_reports_remote_play_method(client: TestClient) -> None:
    """strm 网盘条目走 302 直链、流量不经过服务器：标注 remote 而非伪造速率。"""
    async with get_database().session() as session:
        movie = MediaItem(
            kind="movie",
            tmdb_id=157336,
            title="星际穿越",
            original_title="Interstellar",
            year=2014,
            aliases=[],
        )
        library = Library(name="网盘库", kind="movie", root_paths=["/media/cloud"])
        session.add_all([movie, library])
        await session.commit()
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=movie.id,
                file_path="/media/cloud/interstellar.strm",
                source="scanned",
            )
        )
        await session.commit()
        movie_id = movie.id

    info = ClientInfo(
        name="Infuse", device_name="客厅 Apple TV", device_id="dev-5", version="8.0"
    )
    activity.report_start("dev-5", member_id=0, client=info, unit=(movie_id, 0, 0))

    data = client.get("/api/v1/playback/activity").json()["data"]
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["play_method"] == "remote"
    assert data["sessions"][0]["rate_bytes_per_second"] is None


async def test_download_progress_counts_resume_offset(client: TestClient) -> None:
    """断点续传：进度从 Range 起点续算，不把已下好的部分算回 0。"""
    async with get_database().session() as session:
        movie = MediaItem(
            kind="movie",
            tmdb_id=49026,
            title="蝙蝠侠：黑暗骑士崛起",
            original_title="The Dark Knight Rises",
            year=2012,
            aliases=[],
        )
        session.add(movie)
        await session.commit()
        movie_id = movie.id

    info = ClientInfo(name="Infuse", device_name="Apple TV", device_id="dev-7", version="8.0")
    meter = activity.register_stream(
        device_id="dev-7",
        kind=activity.STREAM_KIND_DOWNLOAD,
        member_id=0,
        unit=(movie_id, 0, 0),
        file_id=3,
        file_name="tdkr.mkv",
        size_bytes=10_000,
        client=info,
        start_offset=8_000,
    )
    meter.add(1_000)

    download = client.get("/api/v1/playback/activity").json()["data"]["downloads"][0]
    assert download["position_bytes"] == 9_000
    assert download["progress_percent"] == 90
    # 本次连接只传了 1000 字节，与"下载到哪"是两个读数
    assert download["bytes_sent"] == 1_000


async def test_revoke_device_drops_credential_and_live_session(client: TestClient) -> None:
    """注销设备：删凭据行 + 结束实时会话；再次注销给 404。"""
    async with get_database().session() as session:
        movie = MediaItem(
            kind="movie",
            tmdb_id=155,
            title="蝙蝠侠：黑暗骑士",
            original_title="The Dark Knight",
            year=2008,
            aliases=[],
        )
        session.add(movie)
        session.add(
            JellyfinDevice(
                member_id=0,
                token="tok-revoke",
                device_id="dev-revoke",
                client="Infuse",
                device_name="书房电视",
                version="8.0",
                last_seen_at=utcnow(),
            )
        )
        await session.commit()
        movie_id = movie.id

    info = ClientInfo(
        name="Infuse", device_name="书房电视", device_id="dev-revoke", version="8.0"
    )
    activity.report_start("dev-revoke", member_id=0, client=info, unit=(movie_id, 0, 0))
    assert len(client.get("/api/v1/playback/activity").json()["data"]["sessions"]) == 1

    resp = client.delete("/api/v1/playback/devices/dev-revoke")
    assert resp.status_code == 200
    assert "书房电视" in resp.json()["message"]

    data = client.get("/api/v1/playback/activity").json()["data"]
    # 凭据行与实时会话同时消失，不留一台"已注销却还在播"的幽灵设备
    assert data["sessions"] == []

    # 凭据确实失效：该 token 不再能通过 Jellyfin 设备鉴权
    async with get_database().session() as session:
        remaining = (
            await session.execute(
                select(JellyfinDevice).where(JellyfinDevice.device_id == "dev-revoke")
            )
        ).scalar_one_or_none()
    assert remaining is None

    assert client.delete("/api/v1/playback/devices/dev-revoke").status_code == 404


async def _seed_movie_in_library(
    *, title: str, tmdb_id: int, library_name: str, admin_visible: bool = True
) -> tuple[int, int]:
    """播一部电影进一个库，返回 (media_item_id, library_id)。"""
    async with get_database().session() as session:
        movie = MediaItem(
            kind="movie",
            tmdb_id=tmdb_id,
            title=title,
            original_title=title,
            year=2010,
            aliases=[],
        )
        library = Library(
            name=library_name,
            kind="movie",
            root_paths=[f"/media/{tmdb_id}"],
            admin_visible=admin_visible,
        )
        session.add_all([movie, library])
        await session.commit()
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=movie.id,
                file_path=f"/media/{tmdb_id}/{tmdb_id}.mkv",
                source="scanned",
                size_bytes=1_000,
                duration_seconds=6_000,
            )
        )
        await session.commit()
        return movie.id, library.id


async def test_web_player_progress_feeds_live_session(client: TestClient) -> None:
    """网页播放器的上报走与 Jellyfin 同一条服务：开始后立刻出现在「正在播放」，
    带浏览器推导的设备名、不可注销；停止后从实时视图消失、留在播放记录。"""
    movie_id, library_id = await _seed_movie_in_library(
        title="盗梦空间", tmdb_id=27205, library_name="电影"
    )
    ua = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1"}

    resp = client.post(
        "/api/v1/playback/progress",
        json={"media_item_id": movie_id, "event": "start", "device_id": "browser-a"},
        headers=ua,
    )
    assert resp.status_code == 200, resp.text
    client.post(
        "/api/v1/playback/progress",
        json={
            "media_item_id": movie_id,
            "event": "progress",
            "position_ms": 600_000,
            "paused": True,
            "device_id": "browser-a",
        },
        headers=ua,
    )

    data = client.get("/api/v1/playback/activity").json()["data"]
    assert len(data["sessions"]) == 1
    live = data["sessions"][0]
    assert live["device_id"] == "web-0-browser-a"
    assert live["client"] == "MovieClaw Web"
    assert live["device_name"] == "Safari · iPhone"
    assert live["member_name"] == "admin"
    assert live["position_ms"] == 600_000
    assert live["paused"] is True
    # 网页会话没有可撤销的设备凭据
    assert live["revocable"] is False
    assert live["media"]["library_id"] == library_id

    client.post(
        "/api/v1/playback/progress",
        json={
            "media_item_id": movie_id,
            "event": "stop",
            "position_ms": 700_000,
            "device_id": "browser-a",
        },
        headers=ua,
    )
    data = client.get("/api/v1/playback/activity").json()["data"]
    assert data["sessions"] == []
    history = client.get("/api/v1/playback/history").json()["data"]
    assert [e["media"]["title"] for e in history["entries"]] == ["盗梦空间"]
    assert history["entries"][0]["end_position_ms"] == 700_000


async def test_scope_folds_live_sessions_outside_browsable_range(client: TestClient) -> None:
    """默认口径：落在超管不可浏览的库里的正在播放 / 正在下载折叠成计数；
    「全部」口径全量展示，但范围外记录标 browsable=false。"""
    hidden_id, hidden_library = await _seed_movie_in_library(
        title="隐藏之作", tmdb_id=1, library_name="私密库", admin_visible=False
    )
    shown_id, _ = await _seed_movie_in_library(
        title="公开之作", tmdb_id=2, library_name="公开库"
    )
    info = ClientInfo(name="Infuse", device_name="Apple TV", device_id="dev-h", version="8.0")
    activity.report_start("dev-h", member_id=0, client=info, unit=(hidden_id, 0, 0))
    activity.report_start(
        "dev-s",
        member_id=0,
        client=ClientInfo(name="Infuse", device_name="iPad", device_id="dev-s", version="8.0"),
        unit=(shown_id, 0, 0),
    )
    activity.register_stream(
        device_id="dev-h",
        kind=activity.STREAM_KIND_DOWNLOAD,
        member_id=0,
        unit=(hidden_id, 0, 0),
        file_id=None,
        file_name="hidden.mkv",
        size_bytes=1_000,
        client=info,
    )

    data = client.get("/api/v1/playback/activity").json()["data"]
    assert [s["media"]["title"] for s in data["sessions"]] == ["公开之作"]
    assert data["downloads"] == []
    assert data["hidden_session_count"] == 1
    assert data["hidden_download_count"] == 1

    data = client.get("/api/v1/playback/activity", params={"scope": "all"}).json()["data"]
    titles = {s["media"]["title"]: s["media"] for s in data["sessions"]}
    assert set(titles) == {"隐藏之作", "公开之作"}
    assert titles["隐藏之作"]["browsable"] is False
    assert titles["隐藏之作"]["library_id"] == hidden_library
    assert titles["公开之作"]["browsable"] is True
    assert data["downloads"][0]["media"]["browsable"] is False
    assert data["hidden_session_count"] == 0
    assert data["hidden_download_count"] == 0


async def test_end_playback_drops_live_session_and_signals_the_player(client: TestClient) -> None:
    """「结束播放」：实时会话立即消失；拒绝窗口内心跳带回退出信号、不重建会话；
    用户亲手重新开始即解除。凭据与观看进度都不动。"""
    movie_id, _ = await _seed_movie_in_library(
        title="盗梦空间", tmdb_id=27205, library_name="电影"
    )
    body = {"media_item_id": movie_id, "device_id": "browser-a"}
    client.post("/api/v1/playback/progress", json={**body, "event": "start"})
    client.post(
        "/api/v1/playback/progress",
        json={**body, "event": "progress", "position_ms": 300_000},
    )
    assert len(client.get("/api/v1/playback/activity").json()["data"]["sessions"]) == 1

    # 没在播的设备：404
    assert (
        client.post("/api/v1/playback/activity/sessions/web-0-nobody/end").status_code == 404
    )
    resp = client.post("/api/v1/playback/activity/sessions/web-0-browser-a/end")
    assert resp.status_code == 200, resp.text
    assert "已结束" in resp.json()["message"]
    assert client.get("/api/v1/playback/activity").json()["data"]["sessions"] == []

    # 拒绝窗口内：心跳照常落进度，但带回退出信号，且实时会话不被重建
    beat = client.post(
        "/api/v1/playback/progress",
        json={**body, "event": "progress", "position_ms": 320_000},
    ).json()["data"]
    assert beat["ended_by_admin"] is True
    assert beat["position_ms"] == 320_000
    assert client.get("/api/v1/playback/activity").json()["data"]["sessions"] == []
    # 停止上报不算「还在播」，不带信号
    stop = client.post(
        "/api/v1/playback/progress",
        json={**body, "event": "stop", "position_ms": 330_000},
    ).json()["data"]
    assert stop["ended_by_admin"] is False

    # 用户亲手重新开始播放：窗口解除，会话回到活动页
    start = client.post("/api/v1/playback/progress", json={**body, "event": "start"}).json()[
        "data"
    ]
    assert start["ended_by_admin"] is False
    assert len(client.get("/api/v1/playback/activity").json()["data"]["sessions"]) == 1


async def test_playback_log_records_each_session_and_feeds_stats(client: TestClient) -> None:
    """播放日志：一场一行，观看时长按进度增量累加、seek 跳过的不算；
    统计与记录接口从它出。"""
    movie_id, library_id = await _seed_movie_in_library(
        title="盗梦空间", tmdb_id=27205, library_name="电影"
    )
    body = {"media_item_id": movie_id, "device_id": "browser-a"}
    ua = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/120.0"}
    client.post("/api/v1/playback/progress", json={**body, "event": "start"}, headers=ua)
    for position in (10_000, 20_000, 35_000):
        client.post(
            "/api/v1/playback/progress",
            json={**body, "event": "progress", "position_ms": position},
            headers=ua,
        )
    # 往前拖了一个小时：这段不是看过的，不计入观看时长
    client.post(
        "/api/v1/playback/progress",
        json={**body, "event": "progress", "position_ms": 3_635_000},
        headers=ua,
    )
    client.post(
        "/api/v1/playback/progress",
        json={**body, "event": "stop", "position_ms": 3_640_000},
        headers=ua,
    )
    # 同一设备紧接着再开一次同一部片：仍在同一场的保鲜期内，不另开一行
    client.post("/api/v1/playback/progress", json={**body, "event": "start"}, headers=ua)

    history = client.get("/api/v1/playback/history").json()["data"]
    assert history["hidden_count"] == 0
    assert len(history["entries"]) == 2
    latest, first = history["entries"]
    assert first["media"]["title"] == "盗梦空间"
    assert first["media"]["library_id"] == library_id
    assert first["member_name"] == "admin"
    assert first["client"] == "MovieClaw Web"
    assert first["device_name"] == "Chrome · macOS"
    assert first["ended_at"] is not None
    assert first["watched_ms"] == 40_000  # 10+10+15+5 秒；那一小时的 seek 不算
    assert first["end_position_ms"] == 3_640_000
    assert first["completed"] is False
    assert latest["ended_at"] is None  # 新一场进行中

    stats = client.get(
        "/api/v1/playback/stats/watch", params={"days": 7, "tz_offset": 480}
    ).json()["data"]
    assert stats["days"] == 7
    assert stats["current"] == {
        "plays": 2, "watched_ms": 40_000, "completed": 0, "active_members": 1
    }
    # 日志刚开始记：上一周期没有数据，前端据此显示「暂无上一周期数据」而不是 0%
    assert stats["previous_available"] is False
    assert stats["previous"]["plays"] == 0
    assert len(stats["previous_by_day"]) == 8
    assert len(stats["by_hour"]) == 7 and all(len(r) == 24 for r in stats["by_hour"])
    assert sum(sum(r) for r in stats["by_hour"]) == 40_000
    assert stats["by_member"] == [
        {"member_id": 0, "member_name": "admin", "plays": 2, "watched_ms": 40_000, "completed": 0}
    ]
    assert stats["by_client"] == [{"client": "MovieClaw Web", "plays": 2, "watched_ms": 40_000}]
    assert len(stats["by_day"]) == 8  # 7 天窗口按日补齐，含今天
    assert sum(day["plays"] for day in stats["by_day"]) == 2
    assert max(day["members"] for day in stats["by_day"]) == 1
    # 网页播放没上报过质量指标：档位分解为空，不伪造「直连 2 场」
    assert stats["by_tier"] == []
    assert len(stats["top_titles"]) == 1
    assert stats["top_titles"][0]["media"]["title"] == "盗梦空间"
    assert stats["top_titles"][0]["plays"] == 2
    assert stats["top_titles"][0]["members"] == 1
    assert [r["media"]["title"] for r in stats["favorites"]] == ["盗梦空间"]
    assert stats["previous_favorites"] == []


async def test_favorite_title_counts_members_not_hours(client: TestClient) -> None:
    """最受欢迎按看过的人数排：三个人各看一遍的电影胜过一个人刷了很久的剧。"""
    movie_id, _ = await _seed_movie_in_library(
        title="疯狂动物城", tmdb_id=269149, library_name="电影"
    )
    show_id, _ = await _seed_movie_in_library(title="长剧", tmdb_id=1396, library_name="剧集")
    now = utcnow()

    def log(member: int, item: int, watched_ms: int, *, ago: timedelta) -> PlaybackLog:
        return PlaybackLog(
            member_id=member, media_item_id=item, kind="movie", title="x", device_id=f"d{member}",
            client="Infuse", started_at=now - ago, last_seen_at=now - ago, ended_at=now - ago,
            watched_ms=watched_ms,
        )

    async with get_database().session() as session:
        for member in (1, 2, 3):
            session.add(log(member, movie_id, 3_600_000, ago=timedelta(days=1)))
        session.add(log(1, show_id, 36_000_000, ago=timedelta(days=2)))
        # 上一周期只有那部剧一个人在看
        session.add(log(1, show_id, 3_600_000, ago=timedelta(days=10)))
        await session.commit()

    stats = client.get("/api/v1/playback/stats/watch", params={"days": 7}).json()["data"]
    assert stats["top_titles"][0]["media"]["title"] == "长剧"  # 时长榜
    assert [r["media"]["title"] for r in stats["favorites"]] == ["疯狂动物城", "长剧"]  # 人气
    assert stats["favorites"][0]["members"] == 3
    assert [r["media"]["title"] for r in stats["previous_favorites"]] == ["长剧"]


async def test_playback_history_pages_by_cursor_without_duplicates(client: TestClient) -> None:
    """游标翻页：按 (started_at, id) 往前走，翻页期间新追加的记录不会让旧行重复出现。"""
    movie_id, _ = await _seed_movie_in_library(title="盗梦空间", tmdb_id=27205, library_name="电影")
    now = utcnow()
    async with get_database().session() as session:
        for i in range(5):
            session.add(
                PlaybackLog(
                    member_id=0,
                    media_item_id=movie_id,
                    kind="movie",
                    title="盗梦空间",
                    device_id="dev",
                    client="Infuse",
                    # 两行同一时刻，逼出 (started_at, id) 的复合游标
                    started_at=now - timedelta(hours=i // 2),
                    last_seen_at=now,
                    ended_at=now,
                    watched_ms=1_000 * (i + 1),
                )
            )
        await session.commit()

    first = client.get("/api/v1/playback/history", params={"limit": 2}).json()["data"]
    assert [e["watched_ms"] for e in first["entries"]] == [2_000, 1_000]
    assert first["has_more"] is True
    cursor = first["next_cursor"]
    assert cursor is not None

    # 翻页途中又开了一场：offset 翻页会把它挤进第二页并让第 2 行重复，游标不会
    async with get_database().session() as session:
        session.add(
            PlaybackLog(
                member_id=0, media_item_id=movie_id, kind="movie", title="盗梦空间",
                device_id="dev", client="Infuse", started_at=now + timedelta(minutes=1),
                last_seen_at=now, ended_at=now, watched_ms=99_000,
            )
        )
        await session.commit()

    second = client.get(
        "/api/v1/playback/history", params={"limit": 2, "before": cursor}
    ).json()["data"]
    assert [e["watched_ms"] for e in second["entries"]] == [4_000, 3_000]
    third = client.get(
        "/api/v1/playback/history", params={"limit": 2, "before": second["next_cursor"]}
    ).json()["data"]
    assert [e["watched_ms"] for e in third["entries"]] == [5_000]
    assert third["has_more"] is False
    assert third["next_cursor"] is None


async def test_playback_log_respects_visibility_scope(client: TestClient) -> None:
    """记录与作品榜里落在超管不可浏览库的片名按口径折叠；聚合数不折叠。"""
    hidden_id, _ = await _seed_movie_in_library(
        title="隐藏之作", tmdb_id=1, library_name="私密库", admin_visible=False
    )
    # 超管自己浏览不到这个库，网页端也播不了；这条记录来自成员用 Jellyfin
    # 客户端的播放，直接落一行日志
    async with get_database().session() as session:
        session.add(
            PlaybackLog(
                member_id=0,
                media_item_id=hidden_id,
                kind="movie",
                title="隐藏之作",
                device_id="dev-h",
                client="Infuse",
                device_name="Apple TV",
                ended_at=utcnow(),
                end_position_ms=60_000,
                watched_ms=60_000,
            )
        )
        await session.commit()

    history = client.get("/api/v1/playback/history").json()["data"]
    assert history["entries"] == []
    assert history["hidden_count"] == 1
    stats = client.get("/api/v1/playback/stats/watch", params={"days": 7}).json()["data"]
    assert stats["current"]["plays"] == 1
    assert stats["top_titles"] == []
    assert stats["hidden_title_count"] == 1

    history = client.get("/api/v1/playback/history", params={"scope": "all"}).json()["data"]
    assert [e["media"]["title"] for e in history["entries"]] == ["隐藏之作"]
    assert history["entries"][0]["media"]["browsable"] is False
    stats = client.get(
        "/api/v1/playback/stats/watch", params={"days": 7, "scope": "all"}
    ).json()["data"]
    assert stats["top_titles"][0]["media"]["title"] == "隐藏之作"
    assert stats["hidden_title_count"] == 0


async def test_revoke_device_keeps_watch_history(client: TestClient) -> None:
    """注销设备不动观看记录：进度按账号保存，与设备凭据无关。"""
    async with get_database().session() as session:
        movie = MediaItem(
            kind="movie",
            tmdb_id=680,
            title="低俗小说",
            original_title="Pulp Fiction",
            year=1994,
            aliases=[],
        )
        session.add(movie)
        session.add(
            JellyfinDevice(member_id=0, token="tok-x", device_id="dev-x", client="Infuse")
        )
        await session.commit()
        session.add(
            PlaybackState(
                member_id=0,
                media_item_id=movie.id,
                position_ms=900_000,
                play_count=1,
                last_played_at=utcnow(),
            )
        )
        await session.commit()

    assert client.delete("/api/v1/playback/devices/dev-x").status_code == 200
    async with get_database().session() as session:
        states = list(
            (
                await session.execute(
                    select(PlaybackState).where(PlaybackState.media_item_id == movie.id)
                )
            ).scalars()
        )
    assert len(states) == 1
    assert states[0].position_ms == 900_000


async def test_unit_contexts_fetch_only_the_played_units(client: TestClient) -> None:
    """长剧回归：补齐一场播放的集名/片长/台账只按 (条目, 季, 集) 精确取行。

    曾经按条目整表拉分集与台账，几部几百集的剧就把几千行（带简介、音轨字幕
    JSON）水合进 ORM，15 条播放记录的接口从 10 毫秒涨到 140 毫秒以上，且卡的是
    整个事件循环——活动页「最近播放」偶尔打开特别卡的根源。用 ORM 装载事件数
    守住：一个单元只该装入条目、档案、那一集的分集行与台账行。
    """
    from sqlalchemy import event

    from movieclaw_api.services.playback_activity import _load_unit_contexts
    from movieclaw_db.models import MediaEpisode

    episodes = 40
    async with get_database().session() as session:
        show = MediaItem(
            kind="tv", tmdb_id=1396, title="长剧", original_title="Long Show", year=2008, aliases=[]
        )
        library = Library(name="剧集库", kind="tv", root_paths=["/media/tv"])
        session.add_all([show, library])
        await session.commit()
        session.add(MediaMetadata(media_item_id=show.id, runtime_minutes=45))
        for e in range(1, episodes + 1):
            session.add(
                MediaEpisode(
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=e,
                    name=f"第 {e} 集",
                    runtime_minutes=40 + e,
                )
            )
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=e,
                    file_path=f"/media/tv/S01E{e:02d}.mkv",
                    source="scanned",
                    duration_seconds=2_000 + e,
                )
            )
        await session.commit()
        show_id = show.id

    unit = (show_id, 1, 7)
    loaded: list[str] = []
    async with get_database().session() as session:
        event.listen(
            session.sync_session,
            "loaded_as_persistent",
            lambda _session, instance: loaded.append(type(instance).__name__),
        )
        contexts = await _load_unit_contexts(session, {unit}, set())
    # 条目 + 档案 + 这一集的分集行 + 这一集的台账行，别的集一行都不该进来
    assert sorted(loaded) == ["LibraryFile", "MediaEpisode", "MediaItem", "MediaMetadata"]

    ctx = contexts[unit]
    assert ctx.episode_title == "第 7 集"
    assert ctx.duration_ms == 2_007 * 1000
    assert ctx.file is not None and ctx.file.file_path == "/media/tv/S01E07.mkv"
