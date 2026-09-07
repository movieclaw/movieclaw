"""扫描入库快路径的行为守卫。

这批优化（TMDB 档案预取、复用已持有的台账行、去掉提交后的多余读回、
根路径去重）都是**只该改快慢、不该改结果**的改动。这里锁住的正是"结果
不变"：识别结论、影人关系、台账行数在预取命中、预取失败、根路径互相
嵌套三种情形下都必须与串行链路一致。TMDB 为假实现。
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.media_discover as discover_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.scan import scan_library
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import LibraryFile, MediaItem, MediaItemPerson
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.tmdb import TmdbClient

_ADMIN = Principal(kind="admin", name="tester")

_KEY = "0123456789abcdef0123456789abcdef"
_MOVIES = 5


def _body(tmdb_id: int) -> dict:
    return {
        "id": tmdb_id,
        "title": f"影片{tmdb_id}",
        "original_title": f"M{tmdb_id}",
        "release_date": "2020-01-01",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
        "overview": "简介。",
        "vote_average": 7.0,
        "runtime": 100,
        "genres": [],
        "images": {"posters": [], "backdrops": []},
        "release_dates": {"results": []},
        "credits": {
            "cast": [
                {
                    "id": 700,
                    "name": "演员甲",
                    "character": "角色甲",
                    "order": 0,
                    "profile_path": None,
                }
            ],
            "crew": [{"id": 800, "name": "导演乙", "job": "Director"}],
        },
    }


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'perf.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()

    def handler(request: httpx.Request) -> httpx.Response:
        seg = request.url.path.rstrip("/").split("/")[-1]
        if seg.isdigit():
            return httpx.Response(200, json=_body(int(seg)))
        return httpx.Response(200, json={"results": []})

    client = TmdbClient(api_key=_KEY, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _make_movies(tmp_path, count: int = _MOVIES):
    """count 部电影，每部一个目录 + 一份写死 tmdbid 的 NFO（预取覆盖的形态）。"""
    root = tmp_path / "media" / "movies"
    for i in range(count):
        entry = root / f"影片{1000 + i} (2020)"
        entry.mkdir(parents=True)
        (entry / f"影片{1000 + i}.1080p.mkv").write_bytes(b"x" * (100 + i))
        (entry / "movie.nfo").write_text(
            f"<movie><title>影片{1000 + i}</title><tmdbid>{1000 + i}</tmdbid></movie>",
            encoding="utf-8",
        )
    return root


async def _scan(db, root, *, extra_roots: list[str] | None = None, name: str = "电影库") -> int:
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name=name, kind="movie", root_paths=[str(root), *(extra_roots or [])]
        )
    await scan_library(library.id)
    return library.id


async def _snapshot(db) -> tuple[int, int, set[str]]:
    async with db.session() as session:
        files = (await session.execute(select(LibraryFile))).scalars().all()
        links = (await session.execute(select(MediaItemPerson))).scalars().all()
        titles = {i.title for i in (await session.execute(select(MediaItem))).scalars()}
    return len(files), len(links), titles


@pytest.mark.parametrize("window", [1, 3, 64])
async def test_prefetch_window_does_not_change_outcome(db, tmp_path, monkeypatch, window) -> None:
    """预取窗大小只影响并发度，入库结果必须逐字节一致。

    窗口 1 = 退化成串行；3 = 跨窗；64 = 一窗吃下全部。三者结果相同才能说明
    预取是纯加速——否则就是"快了但结果变了"，比慢更糟。
    """
    monkeypatch.setattr(scan_mod, "_PREFETCH_WINDOW", window)
    await _scan(db, _make_movies(tmp_path))

    files, links, titles = await _snapshot(db)
    assert files == _MOVIES
    # 每部片 1 位演员 + 1 位导演
    assert links == _MOVIES * 2
    assert titles == {f"影片{1000 + i}" for i in range(_MOVIES)}


async def test_prefetch_failure_falls_back_to_serial(db, tmp_path, monkeypatch) -> None:
    """预取整体拉不到（比如临时断网）时，串行链路照常把片入库。

    预取只是把往返提前，绝不能成为新的失败点：拉不到就当没预取过。
    """
    from movieclaw_api.services.media_library import MediaLibraryService

    async def _boom(self, kind, tmdb_id):
        raise RuntimeError("模拟预取阶段网络抖动")

    monkeypatch.setattr(MediaLibraryService, "prefetch_profile", _boom)
    await _scan(db, _make_movies(tmp_path))

    files, links, _titles = await _snapshot(db)
    assert files == _MOVIES
    assert links == _MOVIES * 2


async def test_nested_root_paths_ingest_each_file_once(db, tmp_path) -> None:
    """两个根路径互相嵌套时，同一个文件只入账一次。

    重复遍历不只是白跑一趟识别链——第二趟拿的是扫描开场的台账快照，
    照着"这条路径还没有行"去插入，会撞 file_path 唯一键。
    """
    root = _make_movies(tmp_path)
    library_id = await _scan(db, root, extra_roots=[str(root / "影片1000 (2020)")])

    async with db.session() as session:
        rows = (
            (await session.execute(select(LibraryFile).where(LibraryFile.library_id == library_id)))
            .scalars()
            .all()
        )
    assert len(rows) == _MOVIES
    assert len({r.file_path for r in rows}) == _MOVIES


async def test_library_wall_query_count_is_flat(db, tmp_path) -> None:
    """海报墙查询的 SQL 条数不随条目数增长——N+1 的守卫。

    一旦有人在聚合循环里补一句 ``await session.get(...)``，这里立刻变红。
    条数本身不是重点，「5 部片与 15 部片一样多」才是。
    """
    from sqlalchemy import event

    from movieclaw_api.api.routes.libraries import list_library_items

    counts = []
    for count in (_MOVIES, _MOVIES * 3):
        # 每轮换一个干净的库根，条目数不同、其余一致
        root = tmp_path / f"lib{count}"
        root.mkdir()
        for i in range(count):
            entry = root / f"影片{2000 + count * 10 + i} (2020)"
            entry.mkdir()
            (entry / f"影片{2000 + count * 10 + i}.mkv").write_bytes(b"y" * (500 + i))
            (entry / "movie.nfo").write_text(
                f"<movie><tmdbid>{2000 + count * 10 + i}</tmdbid></movie>", encoding="utf-8"
            )
        library_id = await _scan(db, root, name=f"电影库{count}")

        seen = []
        engine = db._engine.sync_engine

        @event.listens_for(engine, "before_cursor_execute")
        def _tally(conn, cur, stmt, params, ctx, many, _sink=seen):
            _sink.append(stmt)

        async with db.session() as session:
            resp = await list_library_items(library_id, session=session, principal=_ADMIN)
        event.remove(engine, "before_cursor_execute", _tally)

        assert len(resp.data) == count
        counts.append(len(seen))

    assert counts[0] == counts[1], f"查询条数随条目数变了：{counts}，多半是引入了 N+1"


async def test_rescan_is_incremental(db, tmp_path) -> None:
    """全部已识别在位时，重扫不产生任何 TMDB 请求与新台账行。"""
    root = _make_movies(tmp_path)
    library_id = await _scan(db, root)
    summary = await scan_library(library_id)

    assert summary.skipped_known == _MOVIES
    assert summary.scanned == 0
    files, links, _ = await _snapshot(db)
    assert (files, links) == (_MOVIES, _MOVIES * 2)
