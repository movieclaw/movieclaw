"""本地 NFO 的「入库吸收」契约（docs/design/metadata.md 第 5 节）。

守的是这次改版的产品口径：

- **入库时**读一次条目 NFO，有值的字段压过 TMDB 写进 ``media_metadata``；
- **之后读路径只读库**——详情页不再回媒体盘碰一个 NFO 字节；
- 用户**主动刷新元数据**才重读 NFO（改了 NFO 想立刻生效走这条路）；
- 媒体目录镜像写出的是档案自己的副本，**不能**在下一次刷新时被当成
  用户的 NFO 重新吸收，否则会拿上一轮的旧内容盖掉新拉回来的 TMDB 数据。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.nfo as nfo_mod
import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.media_discover as discover_mod
from movieclaw_api.api.routes.libraries import get_library_item
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.nfo import forget_parsed_nfo
from movieclaw_api.services.library.scan import scan_library
from movieclaw_api.services.media_scrape import scrape_media_item
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import MediaItem, MediaMetadata
from movieclaw_db.repositories import MediaItemRepository
from movieclaw_db.repositories.library_repo import LibraryRepository

_KEY = "0123456789abcdef0123456789abcdef"
_ADMIN = Principal(kind="admin", name="tester")

# 可变的 TMDB 档案：用例改它来模拟"上游更新了简介/评分"
_MOVIE = {
    "id": 300,
    "title": "某电影",
    "original_title": "Some Movie",
    "release_date": "2020-05-01",
    "status": "Released",
    "external_ids": {},
    "alternative_titles": {"titles": []},
    "translations": {"translations": []},
    "overview": "TMDB 初版简介。",
    "vote_average": 7.0,
    "runtime": 100,
    "genres": [{"id": 1, "name": "剧情"}],
    "credits": {
        "cast": [
            {"id": 9101, "name": "线上演员甲", "character": "主角", "profile_path": "/a1.jpg"}
        ],
        "crew": [],
    },
    "images": {"posters": [], "backdrops": []},
}

_RICH_NFO = (
    "<movie><title>某电影</title><tmdbid>300</tmdbid>"
    "<plot>手写的中文简介。</plot><runtime>121</runtime>"
    "<ratings><rating name='themoviedb' max='10'><value>8.8</value></rating></ratings>"
    "<actor><name>某演员</name><role>主角</role></actor></movie>"
)


def _never(message: str):
    def _boom(*_args, **_kwargs):
        raise AssertionError(message)

    return _boom


def _fake_tmdb():
    from movieclaw_media.tmdb import TmdbClient

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/3/movie/300":
            return httpx.Response(200, json=_MOVIE)
        if path.startswith("/3/search/"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'absorb.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    client = _fake_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    _MOVIE["overview"] = "TMDB 初版简介。"
    _MOVIE["vote_average"] = 7.0
    _MOVIE["runtime"] = 100
    forget_parsed_nfo()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db, tmp_path, nfo_text: str) -> tuple[int, int, Path]:
    """建一个带 NFO 的电影条目目录并扫描入库，返回 (库 id, 条目 id, NFO 路径)。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.1080p.mkv").write_bytes(b"m")
    nfo = entry / "movie.nfo"
    nfo.write_text(nfo_text, encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        return library.id, item.id, nfo


async def _meta(db, item_id: int) -> MediaMetadata:
    async with db.session() as session:
        row = await MediaItemRepository(session).get_metadata(item_id)
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_entry_nfo_is_absorbed_at_ingest(db, tmp_path) -> None:
    """入库即吸收：NFO 有值的字段压过 TMDB，没写的字段保留 TMDB 那份。"""
    _library_id, item_id, _nfo = await _seed(db, tmp_path, _RICH_NFO)

    meta = await _meta(db, item_id)
    assert meta.overview == "手写的中文简介。"  # NFO 压过 TMDB
    assert meta.vote_average == 8.8 and meta.runtime_minutes == 121
    assert [a["name"] for a in meta.cast] == ["某演员"]
    assert meta.genres == ["剧情"]  # NFO 没写 <genre> → 保留 TMDB 的
    assert meta.nfo_name == "movie.nfo" and meta.nfo_fingerprint


@pytest.mark.asyncio
async def test_detail_never_reads_nfo_from_disk(db, tmp_path, monkeypatch) -> None:
    """详情页零 NFO 读盘：改了盘上的 NFO 而不刷新，页面纹丝不动。"""
    library_id, item_id, nfo = await _seed(db, tmp_path, _RICH_NFO)

    nfo.write_text(
        "<movie><tmdbid>300</tmdbid><plot>盘上改过的简介。</plot></movie>", encoding="utf-8"
    )
    forget_parsed_nfo()
    # 解析器直接拆掉：读路径但凡碰一次 NFO 就会炸在这里
    monkeypatch.setattr(
        nfo_mod, "read_entry_metadata", _never("详情页不该读条目 NFO"), raising=True
    )
    monkeypatch.setattr(
        nfo_mod, "read_episode_metadata", _never("详情页不该读分集 NFO"), raising=True
    )

    async with db.session() as session:
        view = (await get_library_item(library_id, item_id, _ADMIN, session)).data

    assert view.local_meta is not None
    assert view.local_meta.plot == "手写的中文简介。"  # 仍是入库时吸收的那份


@pytest.mark.asyncio
async def test_user_refresh_rereads_a_changed_nfo(db, tmp_path) -> None:
    """用户改了 NFO 再点「刷新元数据」→ 重读并吸收（这是重读的唯一入口）。"""
    _library_id, item_id, nfo = await _seed(db, tmp_path, _RICH_NFO)

    nfo.write_text(
        "<movie><tmdbid>300</tmdbid><plot>改过的简介。</plot><runtime>130</runtime></movie>",
        encoding="utf-8",
    )
    forget_parsed_nfo()
    await scrape_media_item(item_id)

    meta = await _meta(db, item_id)
    assert meta.overview == "改过的简介。" and meta.runtime_minutes == 130


@pytest.mark.asyncio
async def test_mirrored_nfo_never_masks_new_tmdb_data(db, tmp_path) -> None:
    """镜像写出的自家 NFO 不得挡住 TMDB 新数据（2026-08-04 决策的守卫）。

    没有指纹台账时，刷新会读到上一轮镜像写出去的 NFO，把刚拉回来的新简介/
    新评分原样盖回旧值——这正是当年"NFO 挡住新数据"的复现路径。
    """
    # 入库时没有可吸收内容（最小身份 NFO），档案 = TMDB 初版；
    # 扫描收尾的镜像会把它升级成带 plot/rating 的完整 NFO
    _library_id, item_id, nfo = await _seed(
        db, tmp_path, "<movie><title>某电影</title><tmdbid>300</tmdbid></movie>"
    )
    assert "TMDB 初版简介。" in nfo.read_text(encoding="utf-8")  # 镜像已对齐

    # 上游更新
    _MOVIE["overview"] = "TMDB 更新后的简介。"
    _MOVIE["vote_average"] = 9.1
    forget_parsed_nfo()
    await scrape_media_item(item_id)

    meta = await _meta(db, item_id)
    assert meta.overview == "TMDB 更新后的简介。"
    assert meta.vote_average == 9.1


@pytest.mark.asyncio
async def test_no_nfo_is_recorded_so_backfill_converges(db, tmp_path) -> None:
    """条目目录里确实没有 NFO 时台账记空串——存量回填据此收敛，不反复重挑。"""
    from movieclaw_api.services.library.nfo_backfill import backfill_nfo_absorption

    # 目录名 tmdbid 标记挂锚（没有 NFO 可读，正是本用例要的形态）
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020) [tmdbid=300]"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.1080p.mkv").write_bytes(b"m")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        item_id = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
            .id
        )

    meta = await _meta(db, item_id)
    assert meta.nfo_name is None
    assert meta.nfo_fingerprint is not None  # 查过了：空串或镜像写出的指纹

    # 回填任务对已登记的条目空转（选不出待办）
    async with db.session() as session:
        pending = (
            (
                await session.execute(
                    select(MediaMetadata.id).where(MediaMetadata.nfo_fingerprint.is_(None))
                )
            )
            .scalars()
            .all()
        )
    assert list(pending) == []
    await backfill_nfo_absorption()  # 空转不报错


@pytest.mark.asyncio
async def test_backfill_absorbs_legacy_entries(db, tmp_path) -> None:
    """升级前入库的条目（台账为 NULL）由回填任务补上吸收，一轮跑完即收敛。"""
    from movieclaw_api.services.library.nfo_backfill import backfill_nfo_absorption

    _library_id, item_id, _nfo = await _seed(db, tmp_path, _RICH_NFO)

    # 把档案退回"升级前"的样子：台账清空、展示列换成 TMDB 那份
    async with db.session() as session:
        row = await MediaItemRepository(session).get_metadata(item_id)
        row.overview, row.vote_average, row.runtime_minutes = "TMDB 初版简介。", 7.0, 100
        row.nfo_name = None
        row.nfo_fingerprint = None
        session.add(row)
        await session.commit()

    forget_parsed_nfo()
    await backfill_nfo_absorption()

    meta = await _meta(db, item_id)
    assert meta.nfo_fingerprint is not None
    # 镜像已按档案重写过 movie.nfo，回填吸收的是那份（内容与档案一致），
    # 关键是台账补上了、回填不会再挑中它
    async with db.session() as session:
        left = (
            (
                await session.execute(
                    select(MediaMetadata.id).where(MediaMetadata.nfo_fingerprint.is_(None))
                )
            )
            .scalars()
            .all()
        )
    assert list(left) == []
