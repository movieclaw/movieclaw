"""Jellyfin 兼容层对「其他」库（homevideos）的输出（docs/design/library-other-kind.md 5.1）。

- 库视图 CollectionType=homevideos；
- 条目是可播叶子 ``Video``（不是 Movie，也不是 Series/Episode 层级），
  ProviderIds 里没有 Tmdb；
- 有本地抓帧尺寸时输出真实 PrimaryImageAspectRatio（16:9），TMDB 海报 2:3；
- Latest 把 Video 当叶子直接列出；Counts 只计入 ItemCount；
- 勾了「从首页排除」的库不进首页级 Latest。
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
    MediaItem,
    MediaMetadata,
    MediaSource,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_jellyfin.ids import library_guid


@pytest.fixture
def seeded_video(tmp_path: Path, monkeypatch) -> dict:
    """播种：一个电影库（TMDB 条目）+ 一个其他库（两个本地条目，一个有抓帧尺寸）。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jf-video.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()

    movie_root = tmp_path / "movies"
    home_root = tmp_path / "home"
    movie_file = movie_root / "Inception (2010)" / "Inception.2010.mkv"
    movie_file.parent.mkdir(parents=True)
    movie_file.write_bytes(b"A" * 1024)
    clip_a = home_root / "2019" / "春节团圆饭.mp4"
    clip_b = home_root / "旅行.mkv"
    clip_a.parent.mkdir(parents=True)
    clip_a.write_bytes(b"B" * 1024)
    clip_b.write_bytes(b"C" * 1024)
    # 本地抓帧缩略图落在资产目录（与 TMDB 海报同一路径约定）
    from PIL import Image

    assets = tmp_path / "metadata" / "images"
    (assets / "2").mkdir(parents=True)
    Image.new("RGB", (1280, 720), "#335577").save(assets / "2" / "poster.jpg", "JPEG")
    (assets / "1").mkdir(parents=True)
    Image.new("RGB", (100, 150), "#4a6fa5").save(assets / "1" / "poster.jpg", "JPEG")

    ids: dict = {}

    async def _seed() -> None:
        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        async with get_database().session() as session:
            movie_lib = Library(name="电影", kind="movie", root_paths=[str(movie_root)])
            home_lib = Library(
                name="家庭录像", kind="video", source="local", root_paths=[str(home_root)]
            )
            session.add_all([movie_lib, home_lib])
            await session.flush()
            movie = MediaItem(
                kind="movie", tmdb_id=27205, title="盗梦空间", original_title="Inception", year=2010
            )
            clip1 = MediaItem(
                kind="video",
                source=MediaSource.LOCAL,
                external_id=f"{home_lib.id}:path:aaaa",
                title="春节团圆饭",
                original_title="",
                year=2019,
                aliases=[],
            )
            clip2 = MediaItem(
                kind="video",
                source=MediaSource.LOCAL,
                external_id=f"{home_lib.id}:path:bbbb",
                title="旅行",
                original_title="",
                year=None,
                aliases=[],
            )
            session.add_all([movie, clip1, clip2])
            await session.flush()
            session.add_all(
                [
                    MediaMetadata(media_item_id=movie.id, poster_file="1/poster.jpg"),
                    MediaMetadata(
                        media_item_id=clip1.id,
                        poster_file="2/poster.jpg",
                        poster_width=1280,
                        poster_height=720,
                        runtime_minutes=12,
                    ),
                    MediaMetadata(media_item_id=clip2.id),
                    LibraryFile(
                        library_id=movie_lib.id,
                        media_item_id=movie.id,
                        file_path=str(movie_file),
                        size_bytes=1024,
                        container="mkv",
                        duration_seconds=8880,
                        source=FileSource.SCANNED,
                    ),
                    LibraryFile(
                        library_id=home_lib.id,
                        media_item_id=clip1.id,
                        file_path=str(clip_a),
                        size_bytes=1024,
                        container="mp4",
                        duration_seconds=720,
                        source=FileSource.SCANNED,
                    ),
                    LibraryFile(
                        library_id=home_lib.id,
                        media_item_id=clip2.id,
                        file_path=str(clip_b),
                        size_bytes=1024,
                        container="mkv",
                        duration_seconds=95,
                        source=FileSource.SCANNED,
                    ),
                ]
            )
            await session.commit()
            await LibraryRepository(session).refresh_stats([movie_lib.id, home_lib.id])
            ids.update(
                {
                    "movie_lib": movie_lib.id,
                    "home_lib": home_lib.id,
                    "movie": movie.id,
                    "clip1": clip1.id,
                    "clip2": clip2.id,
                }
            )
        await dispose_db()

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    asyncio.run(_seed())
    return ids


@pytest.fixture
def client(seeded_video, monkeypatch):
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


def test_video_library_view_is_homevideos(client: TestClient, seeded_video: dict) -> None:
    auth = {"ApiKey": jf_login(client)}
    body = client.get("/UserViews", params=auth).json()
    by_name = {v["Name"]: v for v in body["Items"]}
    assert by_name["家庭录像"]["CollectionType"] == "homevideos"
    assert by_name["家庭录像"]["ChildCount"] == 2
    assert by_name["家庭录像"]["RecursiveItemCount"] == 2
    assert by_name["电影"]["CollectionType"] == "movies"
    # VirtualFolders 同一口径
    folders = client.get("/Library/VirtualFolders", params=auth).json()
    kinds = {f["Name"]: f["CollectionType"] for f in folders}
    assert kinds["家庭录像"] == "homevideos"


def test_video_items_are_playable_leaves(client: TestClient, seeded_video: dict) -> None:
    auth = {"ApiKey": jf_login(client)}
    body = client.get(
        "/Items",
        params={
            **auth,
            "parentId": library_guid(seeded_video["home_lib"]),
            "fields": "ProviderIds,Path,MediaSources",
            "sortBy": "SortName",
        },
    ).json()
    assert body["TotalRecordCount"] == 2
    by_name = {i["Name"]: i for i in body["Items"]}
    clip = by_name["春节团圆饭"]
    assert clip["Type"] == "Video" and clip["MediaType"] == "Video"
    assert clip["IsFolder"] is False and clip["ProductionYear"] == 2019
    assert "Tmdb" not in clip.get("ProviderIds", {})
    # 抓帧尺寸 → 真实比例；TMDB 海报按 2:3
    assert clip["ImageTags"].get("Primary")
    assert clip["PrimaryImageAspectRatio"] == pytest.approx(16 / 9, abs=1e-3)
    assert clip["RunTimeTicks"] == 720 * 1000 * 10_000
    assert clip["MediaSources"][0]["Path"].endswith("春节团圆饭.mp4")
    # 没有缩略图的条目：无 Primary 图，也就不出比例
    other = by_name["旅行"]
    assert other["Type"] == "Video" and "Primary" not in other["ImageTags"]

    # includeItemTypes=Video 过滤命中；按 Movie 过滤则一个都没有
    only_video = client.get(
        "/Items",
        params={
            **auth,
            "parentId": library_guid(seeded_video["home_lib"]),
            "includeItemTypes": "Video",
        },
    ).json()
    assert only_video["TotalRecordCount"] == 2
    none = client.get(
        "/Items",
        params={
            **auth,
            "parentId": library_guid(seeded_video["home_lib"]),
            "includeItemTypes": "Movie",
        },
    ).json()
    assert none["TotalRecordCount"] == 0

    # 单条目取详情也是 Video
    detail = client.get(f"/Users/{'0' * 32}/Items/{clip['Id']}", params=auth).json()
    assert detail["Type"] == "Video" and detail["Name"] == "春节团圆饭"

    movie = client.get(
        "/Items",
        params={**auth, "parentId": library_guid(seeded_video["movie_lib"])},
    ).json()["Items"][0]
    assert movie["Type"] == "Movie"
    assert movie["PrimaryImageAspectRatio"] == pytest.approx(2 / 3, abs=1e-3)


def test_video_items_in_latest_and_counts(client: TestClient, seeded_video: dict) -> None:
    auth = {"ApiKey": jf_login(client)}
    latest = client.get("/Items/Latest", params={**auth, "limit": 10}).json()
    assert {i["Type"] for i in latest} == {"Movie", "Video"}
    assert sum(1 for i in latest if i["Type"] == "Video") == 2

    counts = client.get("/Items/Counts", params=auth).json()
    assert counts["MovieCount"] == 1 and counts["SeriesCount"] == 0
    assert counts["ItemCount"] == 3  # 其他库只计入总数


def test_exclude_from_home_hides_library_from_latest(
    client: TestClient, seeded_video: dict
) -> None:
    auth = {"ApiKey": jf_login(client)}
    # 通过管理接口把其他库标成「从首页排除」
    login = client.post(
        "/api/v1/auth/login",
        json={"username": ADMIN["username"], "password": ADMIN["password"]},
    )
    assert login.status_code == 200, login.text
    lib = client.get(f"/api/v1/libraries/{seeded_video['home_lib']}").json()["data"]
    assert lib["capabilities"]["jellyfin_collection"] == "homevideos"
    resp = client.put(
        f"/api/v1/libraries/{seeded_video['home_lib']}",
        json={
            "name": lib["name"],
            "kind": lib["kind"],
            "root_paths": lib["root_paths"],
            "exclude_from_home": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["exclude_from_home"] is True

    latest = client.get("/Items/Latest", params={**auth, "limit": 10}).json()
    assert {i["Type"] for i in latest} == {"Movie"}
    # 指定库看仍然有
    in_lib = client.get(
        "/Items/Latest",
        params={**auth, "limit": 10, "parentId": library_guid(seeded_video["home_lib"])},
    ).json()
    assert len(in_lib) == 2
