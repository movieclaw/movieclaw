"""回收站分区接口测试（docs/design/library-recycle-bin.md §3）：
按条目分组分页、摘要与分面计数、筛选、批量清理（按 id / 按筛选 / 失败记录）、批量恢复。"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.recycle import recycle_file
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaEpisode, MediaItem, utcnow
from movieclaw_db.repositories.library_repo import LibraryRepository

_TRIGGER = {"kind": "subscription", "id": 1, "label": "《测试》订阅洗版"}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'bin.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db):
    from movieclaw_api.api.deps import require_admin, require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    admin = Principal(kind="admin", name="tester")
    app.dependency_overrides[require_login] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _seed(db, tmp_path):
    """两个库：电影库一部电影（在位新版 + 待回收旧版）、剧集库一部剧三集待回收
    （其中一集移入回收站失败降级为原地待回收）。"""
    movies = tmp_path / "movies"
    tv = tmp_path / "tv"
    movies.mkdir()
    tv.mkdir()
    async with db.session() as session:
        repo = LibraryRepository(session)
        movie_lib = await repo.create(name="电影", kind="movie", root_paths=[str(movies)])
        tv_lib = await repo.create(name="剧集", kind="tv", root_paths=[str(tv)])
        movie = MediaItem(
            kind="movie",
            tmdb_id=1,
            title="九门",
            original_title="Nine Gates",
            year=2025,
            aliases=[],
        )
        show = MediaItem(
            kind="tv",
            tmdb_id=2,
            title="权力的游戏",
            original_title="Game of Thrones",
            year=2011,
            aliases=[],
        )
        session.add_all([movie, show])
        await session.flush()
        session.add(
            MediaEpisode(media_item_id=show.id, season_number=1, episode_number=3, name="雪诺大人")
        )

        def make(lib_id, item_id, path, size, **kw):
            path.write_bytes(b"x" * size)
            row = LibraryFile(
                library_id=lib_id,
                media_item_id=item_id,
                file_path=str(path),
                size_bytes=size,
                source=FileSource.IMPORTED,
                **kw,
            )
            session.add(row)
            return row

        new_movie = make(
            movie_lib.id,
            movie.id,
            movies / "九门.2025.2160p.Remux.mkv",
            20,
            resolution="2160p",
            media_source="Remux",
        )
        old_movie = make(
            movie_lib.id,
            movie.id,
            movies / "九门.2025.1080p.WEB-DL.mkv",
            4,
            resolution="1080p",
            media_source="WEB-DL",
            video_codec="h264",
            release_group="XXX",
            audio_streams=[{"codec": "aac", "channels": 2, "default": True}],
        )
        eps = [
            make(
                tv_lib.id,
                show.id,
                tv / f"GoT.S01E0{n}.720p.HDTV.mkv",
                1,
                season_number=1,
                episode_number=n,
                resolution="720p",
                media_source="HDTV",
                audio_streams=[
                    {"codec": "dts", "profile": "DTS-HD MA", "channel_layout": "5.1(side)"}
                ],
            )
            for n in (3, 4, 5)
        ]
        await session.commit()
        for row in [new_movie, old_movie, *eps]:
            await session.refresh(row)

        await recycle_file(
            session,
            old_movie,
            reason="upgrade_replaced",
            trigger=_TRIGGER,
            note="洗版替换：1080p WEB-DL → 2160p Remux",
        )
        old_movie.purge_after = utcnow() + timedelta(hours=6)  # 24 小时内到期
        for ep in eps[:2]:
            await recycle_file(
                session,
                ep,
                reason="upgrade_replaced",
                trigger=_TRIGGER,
                note="洗版替换：720p HDTV → 1080p BluRay",
            )
        await recycle_file(
            session, eps[2], reason="upgrade_refuted", trigger=_TRIGGER, note="洗版证伪"
        )
        # 模拟移入回收站失败的降级形态：原地待回收
        eps[2].trash_original_path = None
        await session.commit()
        return {
            "movie_lib": movie_lib.id,
            "tv_lib": tv_lib.id,
            "movie": movie.id,
            "show": show.id,
            "old_movie": old_movie.id,
            "new_movie": new_movie.id,
            "eps": [e.id for e in eps],
        }


@pytest.mark.asyncio
async def test_list_groups_by_item_with_summary_and_facets(client, db, tmp_path):
    ids = await _seed(db, tmp_path)
    res = await client.get("/api/v1/libraries/trashed-files")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    # 摘要按文件、分页按条目
    assert data["total_files"] == 4
    assert data["total_items"] == 2
    assert data["total_bytes"] == 4 + 3
    assert data["due_within_24h"] == 1
    assert data["kept_in_place"] == 1
    assert {(r["name"], r["count"]) for r in data["by_library"]} == {("电影", 1), ("剧集", 3)}
    assert {(r["reason"], r["count"]) for r in data["by_reason"]} == {
        ("upgrade_replaced", 3),
        ("upgrade_refuted", 1),
    }
    # 最早到期的条目排最上（电影 6 小时后到期）
    assert [i["media_item"]["title"] for i in data["items"]] == ["九门", "权力的游戏"]
    movie, show = data["items"]
    assert movie["file_count"] == 1 and movie["note"].startswith("洗版替换")
    assert movie["quality"]["tiers"] == {"1080p WEB-DL": 1}
    assert movie["files"][0]["audio_label"] == "AAC 2.0"
    assert movie["files"][0]["kept_in_place"] is False
    assert movie["trigger_label"] == "《测试》订阅洗版"
    # 剧集：三集一行、季号、原因混合时 note 为空、按季集排序、集名来自 media_episode
    assert show["file_count"] == 3 and show["seasons"] == [1]
    assert show["note"] is None
    assert show["reasons"] == {"upgrade_replaced": 2, "upgrade_refuted": 1}
    assert [f["episode_number"] for f in show["files"]] == [3, 4, 5]
    assert show["files"][0]["episode_title"] == "雪诺大人"
    assert show["files"][0]["audio_label"] == "DTS-HD MA 5.1"
    assert show["files"][2]["kept_in_place"] is True
    assert show["quality"]["tiers"] == {"720p HDTV": 3}
    # 在位的新版本不在回收站里
    assert ids["new_movie"] not in {f["id"] for i in data["items"] for f in i["files"]}


@pytest.mark.asyncio
async def test_list_filters_and_pagination(client, db, tmp_path):
    ids = await _seed(db, tmp_path)
    # 库筛选：摘要跟随筛选，库分面不跟随（点了「剧集」，「电影 1」仍在）
    res = await client.get("/api/v1/libraries/trashed-files", params={"library_id": ids["tv_lib"]})
    data = res.json()["data"]
    assert data["total_files"] == 3 and data["total_items"] == 1
    assert {(r["name"], r["count"]) for r in data["by_library"]} == {("电影", 1), ("剧集", 3)}
    assert {(r["reason"], r["count"]) for r in data["by_reason"]} == {
        ("upgrade_replaced", 2),
        ("upgrade_refuted", 1),
    }
    # 原因筛选：命中文件名的条目出现，展开后只列命中的文件
    res = await client.get("/api/v1/libraries/trashed-files", params={"reason": "upgrade_refuted"})
    data = res.json()["data"]
    assert data["total_files"] == 1 and len(data["items"]) == 1
    assert [f["episode_number"] for f in data["items"][0]["files"]] == [5]
    # 搜索片名
    res = await client.get("/api/v1/libraries/trashed-files", params={"q": "thrones"})
    assert [i["media_item"]["title"] for i in res.json()["data"]["items"]] == ["权力的游戏"]
    # 分页按条目
    res = await client.get("/api/v1/libraries/trashed-files", params={"limit": 1, "offset": 1})
    data = res.json()["data"]
    assert data["total_items"] == 2 and len(data["items"]) == 1
    assert data["items"][0]["media_item"]["title"] == "权力的游戏"


@pytest.mark.asyncio
async def test_purge_by_ids_and_by_filter(client, db, tmp_path):
    ids = await _seed(db, tmp_path)
    # 按 id：清理电影旧版本
    res = await client.post(
        "/api/v1/libraries/trashed-files/purge", json={"ids": [ids["old_movie"]]}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["data"] == {"done": 1, "failed": [], "remaining": 0}
    assert body["message"] == "已清理 1 个文件"
    async with db.session() as session:
        assert await session.get(LibraryFile, ids["old_movie"]) is None
        assert (await session.get(LibraryFile, ids["new_movie"])).state == FileState.IN_PLACE

    # ids 与 filter 互斥
    res = await client.post("/api/v1/libraries/trashed-files/purge", json={})
    assert res.status_code == 400
    res = await client.post(
        "/api/v1/libraries/trashed-files/purge", json={"ids": [1], "filter": {"library_id": 1}}
    )
    assert res.status_code == 400

    # 按筛选：清理剧集库全部待回收
    res = await client.post(
        "/api/v1/libraries/trashed-files/purge", json={"filter": {"library_id": ids["tv_lib"]}}
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 3
    res = await client.get("/api/v1/libraries/trashed-files")
    assert res.json()["data"]["total_files"] == 0


@pytest.mark.asyncio
async def test_purge_failure_is_recorded_and_others_proceed(client, db, tmp_path):
    ids = await _seed(db, tmp_path)
    # 让第一集的物理文件目录只读，unlink 失败 → 记录 last_error，其余照常清理
    async with db.session() as session:
        row = await session.get(LibraryFile, ids["eps"][0])
        trash_dir = os.path.dirname(row.file_path)
    os.chmod(trash_dir, 0o555)
    try:
        res = await client.post(
            "/api/v1/libraries/trashed-files/purge", json={"ids": ids["eps"][:2] + [999_999]}
        )
    finally:
        os.chmod(trash_dir, 0o755)
    if os.geteuid() == 0:
        pytest.skip("root 不受目录只读限制，无法模拟清理失败")
    data = res.json()["data"]
    assert data["done"] == 0
    assert {f["id"] for f in data["failed"]} == {ids["eps"][0], ids["eps"][1], 999_999}
    async with db.session() as session:
        row = await session.get(LibraryFile, ids["eps"][0])
        assert row.state == FileState.TRASHED
        assert "清理失败" in row.trash_context["last_error"]
    res = await client.get("/api/v1/libraries/trashed-files", params={"library_id": ids["tv_lib"]})
    files = {f["id"]: f for i in res.json()["data"]["items"] for f in i["files"]}
    assert files[ids["eps"][0]]["last_error"].startswith("清理失败")


@pytest.mark.asyncio
async def test_restore_batch(client, db, tmp_path):
    ids = await _seed(db, tmp_path)
    res = await client.post(
        "/api/v1/libraries/trashed-files/restore", json={"ids": ids["eps"][:2] + [ids["new_movie"]]}
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["done"] == 2
    assert [f["error"] for f in data["failed"]] == ["不在待回收状态"]
    async with db.session() as session:
        for fid in ids["eps"][:2]:
            row = await session.get(LibraryFile, fid)
            assert row.state == FileState.IN_PLACE and os.path.exists(row.file_path)
            assert ".movieclaw-trash" not in row.file_path
        # 恢复后库统计重算：剧集库在位文件 = 恢复的两集
        from movieclaw_db.models.library import Library

        lib = await session.get(Library, ids["tv_lib"])
        assert lib.stats_file_count == 2
    res = await client.get("/api/v1/libraries/trashed-files")
    assert res.json()["data"]["total_files"] == 2  # 电影旧版 + 证伪的一集
    res = await client.post("/api/v1/libraries/trashed-files/restore", json={"ids": []})
    assert res.status_code == 400
