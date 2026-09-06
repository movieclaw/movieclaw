"""Jellyfin 兼容层的章节输出（docs/design/video-chapters.md §4.7）。

- ``Chapters`` 受 fields 门控：不传不出，传了出；单条目接口全字段语义带出；
- 内嵌章节按起点输出，合成章节输出图上那一帧的真实时间；无标题补「第 N 章」；
- 有图才给 ImageTag / ImageDateModified，``ImagePath`` 省略（偏离⑫）；
- 章节图路由 ``/Items/{id}/Images/Chapter/{index}``：有图 200、无图/越界 404；
- VirtualFolders 的 EnableChapterImageExtraction 如实反映库开关。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jellyfin.helpers import ADMIN, jf_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    Library,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    MediaSeason,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_jellyfin.ids import episode_guid, item_guid, library_guid

_EMBEDDED = [
    {"start_ms": 0, "end_ms": 600_000, "title": "Opening"},
    {"start_ms": 600_000, "end_ms": 3_000_000, "title": None},
    {"start_ms": 3_000_000, "end_ms": None, "title": "Finale"},
]


@pytest.fixture
def seeded_chapters(tmp_path: Path, monkeypatch) -> dict:
    """电影：三段内嵌章节，第 2/3 章有图；剧集一集：无内嵌章节（47 分钟 → 合成 8 段），
    首段有图。电影库开了章节开关，剧集库关了。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jf-ch.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()

    media = tmp_path / "media"
    movie_file = media / "Inception (2010)" / "Inception.2010.mkv"
    movie_file.parent.mkdir(parents=True)
    movie_file.write_bytes(b"A" * 1024)
    ep_file = media / "Breaking (2008)" / "Season 01" / "S01E01.mkv"
    ep_file.parent.mkdir(parents=True)
    ep_file.write_bytes(b"B" * 1024)

    from PIL import Image

    ids: dict = {}

    async def _seed() -> None:
        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        async with get_database().session() as session:
            movie_lib = Library(name="电影", kind="movie", root_paths=[str(media)])
            tv_lib = Library(
                name="剧集", kind="tv", root_paths=[str(media)], extract_chapter_images=False
            )
            session.add_all([movie_lib, tv_lib])
            await session.flush()
            movie = MediaItem(
                kind="movie", tmdb_id=27205, title="盗梦空间", original_title="Inception", year=2010
            )
            show = MediaItem(
                kind="tv", tmdb_id=1396, title="绝命毒师", original_title="Breaking Bad", year=2008
            )
            session.add_all([movie, show])
            await session.flush()
            session.add_all(
                [
                    MediaMetadata(media_item_id=movie.id, runtime_minutes=148),
                    MediaMetadata(media_item_id=show.id),
                    MediaSeason(media_item_id=show.id, season_number=1, name="第 1 季"),
                    MediaEpisode(
                        media_item_id=show.id, season_number=1, episode_number=1, name="Pilot"
                    ),
                ]
            )
            movie_row = LibraryFile(
                library_id=movie_lib.id,
                media_item_id=movie.id,
                file_path=str(movie_file),
                size_bytes=1024,
                container="mkv",
                duration_seconds=148 * 60,
                chapters=_EMBEDDED,
                source=FileSource.SCANNED,
            )
            ep_row = LibraryFile(
                library_id=tv_lib.id,
                media_item_id=show.id,
                season_number=1,
                episode_number=1,
                file_path=str(ep_file),
                size_bytes=1024,
                container="mkv",
                duration_seconds=47 * 60,
                chapters=[],
                source=FileSource.SCANNED,
            )
            session.add_all([movie_row, ep_row])
            await session.flush()
            assets = tmp_path / "metadata" / "images"
            movie_row.chapter_images = []
            for start in (600_000, 3_000_000):
                rel = f"{movie.id}/chapters/{movie_row.id}/{start:010d}.jpg"
                (assets / rel).parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (960, 540), "#335577").save(assets / rel, "JPEG")
                movie_row.chapter_images.append(
                    {"start_ms": start, "frame_ms": start + 2000, "image": rel}
                )
            # 合成首段起点 = 47*60*1000*0.06 = 169200ms；图上那一帧在 171000ms
            rel = f"{show.id}/chapters/{ep_row.id}/{169_200:010d}.jpg"
            (assets / rel).parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (960, 540), "#553377").save(assets / rel, "JPEG")
            ep_row.chapter_images = [{"start_ms": 169_200, "frame_ms": 171_000, "image": rel}]
            await session.commit()
            await LibraryRepository(session).refresh_stats([movie_lib.id, tv_lib.id])
            ids.update(
                {
                    "movie_lib": movie_lib.id,
                    "tv_lib": tv_lib.id,
                    "movie": movie.id,
                    "show": show.id,
                }
            )
        await dispose_db()

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    asyncio.run(_seed())
    return ids


@pytest.fixture
def client(seeded_chapters, monkeypatch):
    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        resp = c.post("/api/v1/auth/bootstrap", json=ADMIN)
        assert resp.status_code == 200, resp.text
        yield c
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def test_chapters_are_field_gated_and_full_on_single_item(
    client: TestClient, seeded_chapters: dict
) -> None:
    auth = {"ApiKey": jf_login(client)}
    parent = library_guid(seeded_chapters["movie_lib"])
    plain = client.get("/Items", params={**auth, "parentId": parent}).json()["Items"][0]
    assert "Chapters" not in plain

    with_fields = client.get(
        "/Items", params={**auth, "parentId": parent, "fields": "Chapters"}
    ).json()["Items"][0]
    chapters = with_fields["Chapters"]
    assert [c["StartPositionTicks"] for c in chapters] == [0, 600_000 * 10_000, 3_000_000 * 10_000]
    assert [c["Name"] for c in chapters] == ["Opening", "第 2 章", "Finale"]
    # 有图才给 ImageTag；ImagePath 永远不出（偏离⑫）
    assert "ImageTag" not in chapters[0]
    assert chapters[1]["ImageTag"] and chapters[1]["ImageDateModified"].endswith("Z")
    assert all("ImagePath" not in c for c in chapters)

    single = client.get(f"/Items/{item_guid(seeded_chapters['movie'])}", params=auth).json()
    assert [c["Name"] for c in single["Chapters"]] == ["Opening", "第 2 章", "Finale"]


def test_synthetic_chapters_use_frame_time_and_are_output(
    client: TestClient, seeded_chapters: dict
) -> None:
    auth = {"ApiKey": jf_login(client)}
    guid = episode_guid(seeded_chapters["show"], 1, 1)
    episode = client.get(f"/Items/{guid}", params=auth).json()
    chapters = episode["Chapters"]
    assert len(chapters) == 8 and chapters[0]["Name"] == "第 1 章"
    # 首段有图：起点用图上那一帧（171s）；其余没图：名义起点
    assert chapters[0]["StartPositionTicks"] == 171_000 * 10_000
    assert chapters[0]["ImageTag"]
    assert chapters[1]["StartPositionTicks"] == int(47 * 60 * 1000 * (0.06 + 0.88 / 7)) * 10_000
    assert "ImageTag" not in chapters[1]


def test_chapter_image_route(client: TestClient, seeded_chapters: dict) -> None:
    token = jf_login(client)
    guid = item_guid(seeded_chapters["movie"])
    no_image = client.get(f"/Items/{guid}/Images/Chapter/0", params={"ApiKey": token})
    assert no_image.status_code == 404
    ok = client.get(f"/Items/{guid}/Images/Chapter/1", params={"ApiKey": token, "tag": "t1"})
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("image/jpeg")
    assert ok.headers["ETag"] == '"t1"'
    scaled = client.get(
        f"/Items/{guid}/Images/Chapter/2", params={"ApiKey": token, "maxWidth": 320}
    )
    assert scaled.status_code == 200
    assert (
        client.head(f"/Items/{guid}/Images/Chapter/2", params={"ApiKey": token}).status_code == 200
    )
    out_of_range = client.get(f"/Items/{guid}/Images/Chapter/9", params={"ApiKey": token})
    assert out_of_range.status_code == 404
    # 剧集单元也走同一路由：首段有图
    ep = episode_guid(seeded_chapters["show"], 1, 1)
    assert client.get(f"/Items/{ep}/Images/Chapter/0", params={"ApiKey": token}).status_code == 200
    assert client.get(f"/Items/{ep}/Images/Chapter/1", params={"ApiKey": token}).status_code == 404


def test_virtual_folders_reflect_chapter_switch(client: TestClient, seeded_chapters: dict) -> None:
    auth = {"ApiKey": jf_login(client)}
    folders = {f["Name"]: f for f in client.get("/Library/VirtualFolders", params=auth).json()}
    assert folders["电影"]["LibraryOptions"]["EnableChapterImageExtraction"] is True
    assert folders["剧集"]["LibraryOptions"]["EnableChapterImageExtraction"] is False
