"""重复文件的端到端验证（docs/design/library-duplicate-files.md 验收）。

与 ``test_library_duplicates.py`` 的分工：那一份手搓台账行验证判定逻辑；**这一份
不手搓任何一行台账**——文件真实落盘（含真硬链接、真复制），走**真实扫描管线**与
**真实监听入库管线**入库，再打真实接口，最后核对**磁盘**。

它守护的是单测覆盖不到的三件事：

1. ``origin`` 的四个写入点是否真的在落账现场生效（单测里那些 origin 是手写的）；
2. 清理是否真的把文件搬进了 ``<库根>/.movieclaw-trash``、原路径是否真的空了，
   恢复是否真的搬得回来（``recycle_file`` 的物理行为 + 本特性的调用姿势）；
3. 「都留着」之后新文件进来是否真的让单元重新出现（闭环）。

TMDB 与 ffprobe 都是假实现：前者用 MockTransport，后者按文件名给规格——本机没有
ffprobe，而真实部署里它是有的，桩掉才能测到"探测成功"这条主路径。另有一个用例
**故意让探测失败**，验证无时长时同尺寸文件的保守归堆。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

import movieclaw_api.services.library.ingest as ingest_mod
import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.media_discover as discover_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services import jobs
from movieclaw_api.services.library.recycle import TRASH_DIR_NAME
from movieclaw_api.services.library.scan import scan_library
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileState, ImportWatch, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"

_MOVIES = {
    300: ("某电影", "Some Movie", "2020-05-01"),
    301: ("另一部电影", "Another Movie", "2021-03-01"),
    302: ("第三部电影", "Third Movie", "2022-08-01"),
}

_ROUTES: dict[str, dict] = {
    f"/3/movie/{mid}": {
        "id": mid,
        "title": title,
        "original_title": original,
        "release_date": date,
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    }
    for mid, (title, original, date) in _MOVIES.items()
}
_ROUTES["/3/tv/200"] = {
    "id": 200,
    "name": "测试剧集",
    "original_name": "Test Show",
    "first_air_date": "2024-01-01",
    "status": "Returning Series",
    "external_ids": {},
    "alternative_titles": {"results": []},
    "translations": {"translations": []},
    "seasons": [{"season_number": 1}],
}
_ROUTES["/3/tv/200/season/1"] = {
    "name": "第 1 季",
    "air_date": "2024-01-01",
    "episodes": [
        {"episode_number": n, "name": f"E{n}", "air_date": "2024-01-0{n}"} for n in (1, 2, 3)
    ],
}


def _fake_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = request.url.params.get("query", "")
        if path == "/3/search/movie":
            hits = [
                {"id": mid, "title": t, "original_title": o, "release_date": d}
                for mid, (t, o, d) in _MOVIES.items()
                if t in query
            ]
            return httpx.Response(200, json={"results": hits})
        if path == "/3/search/tv":
            hits = (
                [
                    {
                        "id": 200,
                        "name": "测试剧集",
                        "original_name": "Test Show",
                        "first_air_date": "2024-01-01",
                    }
                ]
                if "测试剧集" in query
                else []
            )
            return httpx.Response(200, json={"results": hits})
        payload = _ROUTES.get(path)
        return httpx.Response(200 if payload else 404, json=payload or {})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


def _spec(resolution: str, duration: int, bit_rate: int, *, hdr: str | None = None):
    """假 ffprobe 结论：本机没有 ffprobe，而真实部署里有，桩掉才测得到主路径。"""
    return SimpleNamespace(
        resolution=resolution,
        video_codec="hevc",
        hdr=hdr,
        bit_depth=10,
        duration_seconds=duration,
        bit_rate=bit_rate,
        frame_rate=23.976,
        color_space="BT.709",
        audio_streams=[{"codec": "eac3", "channels": 6, "default": True}],
        subtitle_streams=[],
        chapters=[],
        tag_date=None,
        creation_time=None,
    )


def _probe_by_name(path):
    """按文件名给规格：2160p / 1080p / 720p，时长由所属作品决定（同一部片各版本一致）。

    ``no-probe`` 出现在名字里时返回 None，模拟探测失败（半截文件 / 怪格式）。
    """
    name = Path(path).name
    if "no-probe" in name:
        return None
    duration = 3000 if "S01E" in name or "剧集" in name else 7200
    if "2160p" in name:
        return _spec("2160p", duration, 20_000_000, hdr="DV" if "DV" in name else None)
    if "720p" in name:
        return _spec("720p", duration, 2_500_000)
    return _spec("1080p", duration, 8_000_000)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'dup-e2e.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    client = _fake_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "probe_media", _probe_by_name)
    monkeypatch.setattr(ingest_mod, "probe_media", _probe_by_name)
    # 刚创建的文件不走"疑似写入中"静默窗口
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    monkeypatch.setattr(ingest_mod, "QUIET_SECONDS", 0)
    monkeypatch.setattr(ingest_mod, "_stability", {})
    monkeypatch.setattr(ingest_mod, "_deferred", {})
    monkeypatch.setattr(ingest_mod, "_last_swept", {})
    monkeypatch.setattr(ingest_mod, "_briefs_cache", (float("-inf"), None))
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db):
    from movieclaw_api.api.deps import require_admin, require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    admin = Principal(kind="admin", name="管理员")
    app.dependency_overrides[require_login] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


async def _make_library(db, *, name: str, kind: str, root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with db.session() as session:
        row = await LibraryRepository(session).create(name=name, kind=kind, root_paths=[str(root)])
        return row.id


def _by_title(payload: dict) -> dict[str, dict]:
    return {it["media_item"]["title"]: it for it in payload["items"]}


async def _build_scanned_library(db, tmp_path) -> tuple[int, int, Path, Path]:
    """真实落盘 + 真实扫描：电影库三部片、剧集库一季三集，各自带不同形态的重复。"""
    movies = tmp_path / "media" / "movies"
    tv = tmp_path / "media" / "tv"
    movie_lib = await _make_library(db, name="电影", kind="movie", root=movies)
    tv_lib = await _make_library(db, name="剧集", kind="tv", root=tv)

    # 1) 某电影：2160p 与 1080p 两个真版本 → 不同版本
    _write(movies / "某电影 (2020)" / "某电影.2020.2160p.WEB-DL.mkv", b"A" * 4096)
    _write(movies / "某电影 (2020)" / "某电影.2020.1080p.WEB-DL.mkv", b"B" * 2048)
    # 2) 另一部电影：一个文件 + 一份真复制（同尺寸、同时长）→ 一模一样
    src = _write(movies / "另一部电影 (2021)" / "另一部电影.2021.1080p.WEB-DL.mkv", b"C" * 3000)
    _write(movies / "另一部电影 (2021)" / "另一部电影 (2021) - 1080p.mkv", src.read_bytes())
    # 3) 第三部电影：一个文件 + 一个真硬链接（同 inode）→ 一模一样
    origin = _write(movies / "第三部电影 (2022)" / "第三部电影.2022.1080p.WEB-DL.mkv", b"D" * 2500)
    os.link(origin, movies / "第三部电影 (2022)" / "第三部电影.2022.1080p.副本.mkv")
    # 4) 测试剧集 S01：三集各两个版本（1080p GROUPA / 720p GROUPB）→ 不同版本 · 同构
    season = tv / "测试剧集 (2024)" / "Season 01"
    for n in (1, 2, 3):
        _write(season / f"测试剧集.S01E0{n}.1080p.WEB-DL-GROUPA.mkv", b"E" * (1000 + n))
        _write(season / f"测试剧集.S01E0{n}.720p.HDTV-GROUPB.mkv", b"F" * (500 + n))

    for lib_id in (movie_lib, tv_lib):
        summary = await scan_library(lib_id)
        assert summary.errors == [], summary.errors
    return movie_lib, tv_lib, movies, tv


@pytest.mark.asyncio
async def test_scan_pipeline_writes_origin_and_detects_every_shape(client, db, tmp_path):
    """真实扫描入库 → 来源快照由扫描代码写入 → 三种重复形态各就各位。"""
    movie_lib, tv_lib, movies, _tv = await _build_scanned_library(db, tmp_path)

    # —— 来源快照确实是扫描管线写的（不是测试手搓的）——
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars())
    assert len(rows) == 12  # 三部电影各 2 个 + 剧集三集各 2 个
    assert {r.origin["kind"] for r in rows} == {"scan"}
    assert {r.origin["label"] for r in rows} == {"存量扫描发现（非本系统入库）"}
    # scan_library 默认是用户主动扫描（backfill_existing_specs=True）→ 手动扫描
    assert {r.origin["detail"] for r in rows} == {"手动扫描发现"}
    assert all(r.kept_at is None for r in rows)

    res = await client.get("/api/v1/libraries/duplicate-files")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    items = _by_title(data)
    assert set(items) == {"某电影", "另一部电影", "第三部电影", "测试剧集"}

    # 一模一样：真复制（尺寸+时长）与真硬链接（inode）各一个单元
    assert data["identical"]["units"] == 2
    assert data["identical"]["files"] == 2
    assert items["另一部电影"]["seasons"][0]["bucket"] == "identical"
    assert items["第三部电影"]["seasons"][0]["bucket"] == "identical"
    # 硬链接那对磁盘上只有一份，但仍然值得清掉一行台账
    assert data["identical"]["bytes"] > 0

    # 不同版本：某电影（2160p vs 1080p）+ 剧集三集
    assert data["versions"]["units"] == 4
    assert data["versions"]["files"] == 4

    # 电影：建议保留 2160p，依据是档位最高；两侧来源都是扫描
    movie = items["某电影"]["seasons"][0]
    assert movie["bucket"] == "versions" and movie["uniform"] is False
    files = {f["quality_label"]: f for f in movie["units"][0]["files"]}
    assert set(files) == {"2160p WEB-DL", "1080p WEB-DL"}
    assert files["2160p WEB-DL"]["suggested"] is True
    assert files["2160p WEB-DL"]["suggest_reason"] == "档位最高"
    assert files["2160p WEB-DL"]["origin"]["label"] == "存量扫描发现（非本系统入库）"
    # 规格来自真实（桩）探测，不是文件名猜的
    assert files["2160p WEB-DL"]["bit_rate"] == 20_000_000
    assert files["2160p WEB-DL"]["audio_label"] == "EAC3 5.1"

    # 剧集：按季折叠成两个版本行，各覆盖三集
    season = items["测试剧集"]["seasons"][0]
    assert season["season_number"] == 1 and season["uniform"] is True
    assert len(season["units"]) == 3
    versions = {v["quality_label"]: v for v in season["versions"]}
    assert set(versions) == {"1080p WEB-DL", "720p HDTV"}
    assert versions["1080p WEB-DL"]["suggested"] is True
    assert versions["1080p WEB-DL"]["episodes"] == [1, 2, 3]
    assert versions["720p HDTV"]["episodes"] == [1, 2, 3]

    # 条目详情页也能读到来源（文件区「来源」行的数据源）
    async with db.session() as session:
        item_id = (
            await session.execute(select(MediaItem.id).where(MediaItem.title == "某电影"))
        ).scalar_one()
    detail = await client.get(f"/api/v1/libraries/{movie_lib}/items/{item_id}")
    assert detail.status_code == 200, detail.text
    origins = {f["file_name"]: f["origin"] for f in detail.json()["data"]["files"]}
    assert len(origins) == 2
    assert all(o["label"] == "存量扫描发现（非本系统入库）" for o in origins.values())
    assert tv_lib and movies  # 供后续用例复用的句柄，这里只做存在性断言


@pytest.mark.asyncio
async def test_keep_one_moves_file_to_trash_dir_and_restore_brings_it_back(client, db, tmp_path):
    """「留这个」→ 文件真的进了库根回收站、原路径真的空了；恢复 → 真的搬回原位。"""
    movie_lib, _tv_lib, movies, _tv = await _build_scanned_library(db, tmp_path)
    keep_path = movies / "某电影 (2020)" / "某电影.2020.2160p.WEB-DL.mkv"
    gone_path = movies / "某电影 (2020)" / "某电影.2020.1080p.WEB-DL.mkv"
    gone_bytes = gone_path.read_bytes()

    data = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    movie = _by_title(data)["某电影"]
    keep = next(f for f in movie["seasons"][0]["units"][0]["files"] if f["suggested"])
    assert keep["file_path"] == str(keep_path)

    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={
            "media_item_id": movie["media_item"]["id"],
            "season_number": 0,
            "episode_number": 0,
            "keep_file_id": keep["id"],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 1
    assert res.json()["data"]["failed"] == []

    # —— 磁盘事实 ——
    assert keep_path.exists(), "保留的文件必须原地不动"
    assert not gone_path.exists(), "被清理的文件必须离开原路径"
    trash = movies / TRASH_DIR_NAME
    moved = list(trash.iterdir())
    assert [p.name for p in moved] == ["某电影.2020.1080p.WEB-DL.mkv"]
    assert moved[0].read_bytes() == gone_bytes, "内容必须完好（只是搬家，不是重写）"

    # —— 台账事实 ——
    async with db.session() as session:
        row = (
            await session.execute(
                select(LibraryFile).where(LibraryFile.trash_original_path == str(gone_path))
            )
        ).scalar_one()
        assert row.state == FileState.TRASHED
        assert row.file_path == str(moved[0])  # file_path 恒为当前物理位置
        assert row.purge_after is not None  # 7 天倒计时
        assert row.trash_context["reason"] == "duplicate_cleanup"
        assert row.trash_context["trigger"]["label"] == "管理员"
        assert "留下「某电影.2020.2160p.WEB-DL.mkv」（2160p WEB-DL）" in row.trash_context["note"]
        trashed_id = row.id

    # 回收站列表看得到它，原因胶囊是「重复清理」
    bin_data = (
        await client.get("/api/v1/libraries/trashed-files?reason=duplicate_cleanup")
    ).json()["data"]
    assert bin_data["total_files"] == 1
    assert bin_data["items"][0]["media_item"]["title"] == "某电影"

    # 这个单元不再是重复
    after = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    assert "某电影" not in _by_title(after)

    # —— 恢复：文件搬回原路径，单元重新变成重复 ——
    restored = await client.post(
        "/api/v1/libraries/trashed-files/restore", json={"ids": [trashed_id]}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["done"] == 1
    assert gone_path.exists() and gone_path.read_bytes() == gone_bytes
    assert not any(trash.iterdir()), "回收站目录应被清空"
    back = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    assert "某电影" in _by_title(back)
    assert back["versions"]["files"] == 4
    assert movie_lib  # 句柄存在性


@pytest.mark.asyncio
async def test_keep_season_version_and_resolve_all_move_real_files(client, db, tmp_path):
    """整季留一个版本 + 整堆按建议清理：磁盘上该走的都走了，该留的一个没动。"""
    _movie_lib, _tv_lib, movies, tv = await _build_scanned_library(db, tmp_path)
    season_dir = tv / "测试剧集 (2024)" / "Season 01"

    data = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    show = _by_title(data)["测试剧集"]
    season = show["seasons"][0]
    low = next(v for v in season["versions"] if v["quality_label"] == "720p HDTV")

    # 整季留低码率那版（用户口味）：三集的 1080p 全部进回收站
    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={
            "media_item_id": show["media_item"]["id"],
            "season_number": 1,
            "keep_version": low["key"],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 3

    for n in (1, 2, 3):
        assert (season_dir / f"测试剧集.S01E0{n}.720p.HDTV-GROUPB.mkv").exists()
        assert not (season_dir / f"测试剧集.S01E0{n}.1080p.WEB-DL-GROUPA.mkv").exists()
    tv_trash = tv / TRASH_DIR_NAME
    assert sorted(p.name for p in tv_trash.iterdir()) == [
        f"测试剧集.S01E0{n}.1080p.WEB-DL-GROUPA.mkv" for n in (1, 2, 3)
    ]

    # 整堆按建议清理「一模一样」：复制品与硬链接副本各清一个
    copy_extra = movies / "另一部电影 (2021)" / "另一部电影 (2021) - 1080p.mkv"
    hardlink_extra = movies / "第三部电影 (2022)" / "第三部电影.2022.1080p.副本.mkv"
    assert copy_extra.exists() and hardlink_extra.exists()
    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve-all", json={"bucket": "identical"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 2

    # 建议保留的是命名规范、非退让名的那个；被清掉的两个离开原路径
    assert (movies / "另一部电影 (2021)" / "另一部电影.2021.1080p.WEB-DL.mkv").exists()
    assert (movies / "第三部电影 (2022)" / "第三部电影.2022.1080p.WEB-DL.mkv").exists()
    assert not copy_extra.exists() and not hardlink_extra.exists()
    assert sorted(p.name for p in (movies / TRASH_DIR_NAME).iterdir()) == [
        "另一部电影 (2021) - 1080p.mkv",
        "第三部电影.2022.1080p.副本.mkv",
    ]

    after = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    assert after["identical"]["units"] == 0
    # 只剩某电影那一个不同版本单元
    assert set(_by_title(after)) == {"某电影"}


@pytest.mark.asyncio
async def test_keep_all_survives_rescan_and_new_file_relists(client, db, tmp_path):
    """「都留着」是对单元的长期决定：重扫不复活，新文件进来才重新列出。"""
    movie_lib, _tv_lib, movies, _tv = await _build_scanned_library(db, tmp_path)
    data = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    movie = _by_title(data)["某电影"]

    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={
            "media_item_id": movie["media_item"]["id"],
            "season_number": 0,
            "episode_number": 0,
            "keep_all": True,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 2
    assert "都留着" in res.json()["message"]

    # 文件一个没动
    assert (movies / "某电影 (2020)" / "某电影.2020.2160p.WEB-DL.mkv").exists()
    assert (movies / "某电影 (2020)" / "某电影.2020.1080p.WEB-DL.mkv").exists()
    assert not (movies / TRASH_DIR_NAME).exists() or not list((movies / TRASH_DIR_NAME).iterdir())
    assert "某电影" not in _by_title(
        (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    )

    # 重扫（真实管线再跑一遍）：标记必须活下来——upsert 不得把 kept_at 洗掉
    summary = await scan_library(movie_lib)
    assert summary.errors == []
    assert "某电影" not in _by_title(
        (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    )

    # 新版本进来 + 重扫 → 单元重新列出，旧两个带「你留下的」
    _write(movies / "某电影 (2020)" / "某电影.2020.720p.WEB-DL.mkv", b"G" * 900)
    assert (await scan_library(movie_lib)).errors == []
    relisted = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    unit = _by_title(relisted)["某电影"]["seasons"][0]["units"][0]
    kept = [f for f in unit["files"] if f["kept_at"]]
    fresh = [f for f in unit["files"] if not f["kept_at"]]
    assert len(kept) == 2 and len(fresh) == 1
    assert fresh[0]["quality_label"] == "720p WEB-DL"
    # 只有新来的那个会被清掉；已标记的不计入
    assert relisted["versions"]["files"] == 1 + 3  # 新文件 + 剧集三集


@pytest.mark.asyncio
async def test_watch_import_pipeline_writes_its_own_origin(client, db, tmp_path, monkeypatch):
    """真实监听入库管线：来源快照写成「监听目录自动识别入库」，并带上目录与搬运方式。"""
    root = tmp_path / "media" / "movies"
    watch = tmp_path / "downloads"
    watch.mkdir(parents=True)
    library_id = await _make_library(db, name="电影", kind="movie", root=root)
    async with db.session() as session:
        item = MediaItem(
            kind="movie", tmdb_id=300, title="某电影", original_title="Some Movie", year=2020
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)

    async def _identify(session, kind, watch_root, main, spec):
        return item

    monkeypatch.setattr(ingest_mod, "_identify", _identify)

    entry = watch / "Some.Movie.2020.1080p.WEB-DL"
    entry.mkdir()
    _write(entry / "some.movie.2020.1080p.mkv", b"H" * 2048)

    rule = ImportWatch(source_path=str(watch), strategy="hardlink", library_id=library_id)
    for _ in range(2):  # 第一轮记指纹，第二轮确认静默后处理
        async with db.session() as session:
            library = await session.get(
                type(await LibraryRepository(session).get(library_id)), library_id
            )
        await ingest_mod._sweep_dir(rule, library, execute_inline=True)

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars())
    assert len(rows) == 1, [r.file_path for r in rows]
    origin = rows[0].origin
    assert origin["kind"] == "watch_import"
    assert origin["label"] == "监听目录自动识别入库"
    assert origin["detail"] == f"{watch} · 硬链接入库 · 未匹配到任何下载任务"

    # 详情页能读到它
    detail = await client.get(f"/api/v1/libraries/{library_id}/items/{item.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["files"][0]["origin"]["kind"] == "watch_import"


@pytest.mark.asyncio
async def test_probe_failure_keeps_same_size_files_in_versions_bucket(client, db, tmp_path):
    """探测失败时的保守归堆：只有尺寸相同、没有实测时长，不敢判「一模一样」。

    这是三态铁律在本特性里的落点——无从判定就交给人，而不是猜成"没区别"后
    进批量清理。代价是列表里多一个要人拍板的单元，比误删一个文件划算。
    """
    movies = tmp_path / "media" / "movies"
    library_id = await _make_library(db, name="电影", kind="movie", root=movies)
    payload = b"I" * 1500
    _write(movies / "某电影 (2020)" / "某电影.2020.1080p.no-probe.mkv", payload)
    _write(movies / "某电影 (2020)" / "某电影.2020.1080p.no-probe.副本.mkv", payload)
    assert (await scan_library(library_id)).errors == []

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars())
    assert len(rows) == 2
    assert {r.duration_seconds for r in rows} == {None}
    assert {r.size_bytes for r in rows} == {len(payload)}

    data = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    assert data["identical"]["units"] == 0
    assert data["versions"]["units"] == 1
    unit = _by_title(data)["某电影"]["seasons"][0]["units"][0]
    # 两边规格都未知 → 档位比不出来，建议保留者要说明依据而不是假装有把握
    suggested = next(f for f in unit["files"] if f["suggested"])
    assert suggested["suggest_reason"] is not None
    assert "无法比较" in suggested["suggest_reason"] or "同档" in suggested["suggest_reason"]


@pytest.mark.asyncio
async def test_resolve_all_versions_keeps_one_per_unit(client, db, tmp_path):
    """整堆按建议清理「不同版本」：每个单元只剩建议保留的那个，其余全进回收站。

    这是页面上唯一可能一次清掉用户想留的版本的按钮，所以它的作用域必须精确：
    只动「不同版本」堆，一模一样那堆一个不碰。
    """
    _movie_lib, _tv_lib, movies, tv = await _build_scanned_library(db, tmp_path)
    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve-all", json={"bucket": "versions"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 4  # 某电影 1 + 剧集三集各 1

    # 不同版本堆：每单元只剩最优的那个
    assert (movies / "某电影 (2020)" / "某电影.2020.2160p.WEB-DL.mkv").exists()
    assert not (movies / "某电影 (2020)" / "某电影.2020.1080p.WEB-DL.mkv").exists()
    season_dir = tv / "测试剧集 (2024)" / "Season 01"
    for n in (1, 2, 3):
        assert (season_dir / f"测试剧集.S01E0{n}.1080p.WEB-DL-GROUPA.mkv").exists()
        assert not (season_dir / f"测试剧集.S01E0{n}.720p.HDTV-GROUPB.mkv").exists()

    # 一模一样那堆完全没动
    assert (movies / "另一部电影 (2021)" / "另一部电影 (2021) - 1080p.mkv").exists()
    assert (movies / "第三部电影 (2022)" / "第三部电影.2022.1080p.副本.mkv").exists()
    after = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    assert after["versions"]["units"] == 0
    assert after["identical"]["units"] == 2


@pytest.mark.asyncio
async def test_file_deleted_outside_is_converged_not_errored(client, db, tmp_path):
    """用户在文件系统里先把多余版本删了，再点「留这个」：台账收敛，不报错。

    ``recycle_file`` 对已消失的文件返回 ``already_gone``、由调用方删行——行留着
    会和磁盘长期不一致（列表里挂一个点不动的幽灵）。
    """
    _movie_lib, _tv_lib, movies, _tv = await _build_scanned_library(db, tmp_path)
    data = (await client.get("/api/v1/libraries/duplicate-files")).json()["data"]
    movie = _by_title(data)["某电影"]
    unit = movie["seasons"][0]["units"][0]
    keep = next(f for f in unit["files"] if f["suggested"])
    doomed = next(f for f in unit["files"] if not f["suggested"])

    Path(doomed["file_path"]).unlink()  # 绕过 movieclaw 直接删

    res = await client.post(
        "/api/v1/libraries/duplicate-files/resolve",
        json={
            "media_item_id": movie["media_item"]["id"],
            "season_number": 0,
            "episode_number": 0,
            "keep_file_id": keep["id"],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["done"] == 1
    assert res.json()["data"]["failed"] == []

    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.file_path == doomed["file_path"])
                )
            ).scalars()
        )
    assert rows == [], "磁盘上已经没有的文件不该在台账里留一行"
    assert Path(keep["file_path"]).exists()
    # 回收站里也不该凭空多出一条（没有东西可回收）
    bin_data = (await client.get("/api/v1/libraries/trashed-files")).json()["data"]
    assert bin_data["total_files"] == 0
