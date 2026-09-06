"""人物页（/people/{tmdb_person_id}）的端到端测试。

覆盖：入库刮削写出 person / media_item_person 关系、按 TMDB 影人 ID 反查库内
作品、演员与导演两种身份、库内没有的人返回 404、TMDB 改演员表后残留关系被清理。
TMDB 为假实现。
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.media_discover as discover_mod
from movieclaw_api.api.routes.people import get_person
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.scan import scan_library
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import MediaItem, MediaItemPerson, Person
from movieclaw_db.repositories.library_repo import LibraryRepository

_KEY = "0123456789abcdef0123456789abcdef"
# 路由直调时的请求主体：超管会话，库默认对超管可见，作品不受可见范围过滤
_ADMIN = Principal(kind="admin", name="admin", member_id=0, is_admin=True)


def _movie(tmdb_id: int, title: str, *, cast: list[dict], directors: list[dict]) -> dict:
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": f"{title} EN",
        "release_date": "2012-11-29",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
        "overview": "简介。",
        "vote_average": 8.4,
        "runtime": 146,
        "genres": [{"id": 1, "name": "剧情"}],
        "credits": {
            "cast": cast,
            "crew": [{**d, "job": "Director"} for d in directors],
        },
        "images": {"posters": [], "backdrops": []},
        "release_dates": {"results": []},
    }


# 张国立（901）演两部片；冯小刚（902）在片 1 是导演；李雪健（903）只在片 1
_CAST_1 = [
    {"id": 901, "name": "张国立", "character": "老东家", "order": 0, "profile_path": "/z.jpg"},
    {"id": 903, "name": "李雪健", "character": "李培基", "order": 1, "profile_path": None},
    # 无 person id：关系表以 id 为身份，这种条目必须被跳过而不是拿姓名硬凑
    {"name": "无名氏", "character": "路人", "order": 2, "profile_path": None},
]
_CAST_2 = [
    {"id": 901, "name": "张国立", "character": "康熙", "order": 0, "profile_path": "/z.jpg"},
]

_ROUTES: dict[str, dict] = {
    "/3/movie/401": _movie(
        401, "一九四二", cast=_CAST_1, directors=[{"id": 902, "name": "冯小刚"}]
    ),
    "/3/movie/402": _movie(402, "康熙王朝", cast=_CAST_2, directors=[]),
}


def _fake_tmdb() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _ROUTES.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"status_message": "not found"})
        return httpx.Response(200, json=body)

    from movieclaw_media.tmdb import TmdbClient

    return TmdbClient(api_key=_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'person.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    client = _fake_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _scan_two_movies(db, tmp_path) -> int:
    """建库 + 放两部片 + 扫描入库；返回 library_id。"""
    root = tmp_path / "media" / "movies"
    for name, _tmdb in (("一九四二 (2012)", 401), ("康熙王朝 (2001)", 402)):
        entry = root / name
        entry.mkdir(parents=True)
        (entry / f"{name}.1080p.mkv").write_bytes(b"m")
        (entry / "movie.nfo").write_text(
            f"<movie><title>{name}</title><tmdbid>{_tmdb}</tmdbid></movie>", encoding="utf-8"
        )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    return library.id


async def test_person_page_lists_library_credits(db, tmp_path) -> None:
    """入库即写出影人关系；人物页按影人 ID 反查出他在库里的全部作品。"""
    library_id = await _scan_two_movies(db, tmp_path)

    async with db.session() as session:
        view = (await get_person(901, session, _ADMIN)).data  # 张国立

    assert view.name == "张国立"
    assert view.avatar_url and view.avatar_url.endswith("/w300/z.jpg")
    # 两部片都在，且都带着可跳转的库 id 与饰演角色
    assert {(c.title, c.character, c.department) for c in view.credits} == {
        ("一九四二", "老东家", "cast"),
        ("康熙王朝", "康熙", "cast"),
    }
    assert all(c.library_id == library_id for c in view.credits)


async def test_person_page_covers_director(db, tmp_path) -> None:
    """导演也进关系表（department=director），人物页对导演同样成立。"""
    await _scan_two_movies(db, tmp_path)

    async with db.session() as session:
        view = (await get_person(902, session, _ADMIN)).data  # 冯小刚

    assert view.name == "冯小刚"
    assert [(c.title, c.department, c.character) for c in view.credits] == [
        ("一九四二", "director", None)
    ]


async def test_cast_without_tmdb_person_id_is_skipped(db, tmp_path) -> None:
    """没有 TMDB person id 的演员不进关系表——姓名不是身份，硬凑会把同名不同人合并。

    但他仍然留在 media_metadata.cast 里（详情页照常展示姓名与角色），
    只是没有人物页可点。
    """
    await _scan_two_movies(db, tmp_path)

    async with db.session() as session:
        names = {p.name for p in (await session.execute(select(Person))).scalars()}
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 401)))
            .scalars()
            .one()
        )
        from movieclaw_db.repositories import MediaItemRepository

        meta = await MediaItemRepository(session).get_metadata(item.id)

    assert "无名氏" not in names
    assert names == {"张国立", "李雪健", "冯小刚"}
    # 展示层不受影响：三位演员都还在档案里
    assert [c["name"] for c in meta.cast] == ["张国立", "李雪健", "无名氏"]


async def test_person_not_in_library_returns_404(db, tmp_path) -> None:
    """库里没有这个人（或存量库还没刷新过元数据）→ 404，而不是空页面。"""
    await _scan_two_movies(db, tmp_path)

    async with db.session() as session:
        with pytest.raises(NotFoundException):
            await get_person(999999, session, _ADMIN)


async def test_stale_credit_is_removed_on_refresh(db, tmp_path) -> None:
    """TMDB 改了演员表后，残留的旧关系必须被删掉。

    否则人物页会列出一部他其实没参演的片——比缺数据更糟。
    """
    await _scan_two_movies(db, tmp_path)

    # 片 402 的演员表换人：张国立(901) 换成陈道明(904)
    _ROUTES["/3/movie/402"]["credits"]["cast"] = [
        {"id": 904, "name": "陈道明", "character": "康熙", "order": 0, "profile_path": None}
    ]
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 402)))
            .scalars()
            .one()
        )
        item_id = item.id
    from movieclaw_api.services.media_scrape import scrape_media_item

    await scrape_media_item(item_id, force=True)

    async with db.session() as session:
        view = (await get_person(901, session, _ADMIN)).data
        links = (
            await session.execute(
                select(MediaItemPerson).where(MediaItemPerson.media_item_id == item_id)
            )
        ).scalars().all()

    # 张国立只剩片 401；片 402 的关系整体换成了陈道明
    assert [c.title for c in view.credits] == ["一九四二"]
    assert len(links) == 1


async def test_credits_persist_without_caller_commit(db, tmp_path) -> None:
    """建档路径必须自己提交影人数据，不能指望调用方。

    ``create_with_seasons`` 已经提交过，影人关系写的是**新事务**，而 get_session
    明确不提交（事务边界交给上层）。订阅 prepare 那条链路只读不提交——建档服务
    不自己收口的话，通过订阅建出来的条目会静默丢掉全部影人数据。
    """
    from movieclaw_api.services.media_library import MediaLibraryService
    from movieclaw_media.models import MediaKind

    # 只调 ensure_media_item 并让会话关闭，全程不显式 commit
    async with db.session() as session:
        svc = MediaLibraryService(session, _fake_tmdb())
        await svc.ensure_media_item(MediaKind.MOVIE, 401)

    async with db.session() as session:
        people = (await session.execute(select(Person))).scalars().all()
        links = (await session.execute(select(MediaItemPerson))).scalars().all()

    assert {p.name for p in people} == {"张国立", "李雪健", "冯小刚"}
    assert len(links) == 3


async def test_person_without_any_credit_returns_404(db, tmp_path) -> None:
    """影人还在、但他的片都已从库里删掉 → 404，而不是「库内 0 部」的空壳页。

    person 行只增不删（一个人参演多部，删一部不代表这个人该消失），所以这种
    孤儿行一定会出现；页面的前提「他在我库里的作品」此时不成立。
    """
    from movieclaw_db.models import MediaItem as _MediaItem

    await _scan_two_movies(db, tmp_path)
    async with db.session() as session:
        for item in (await session.execute(select(_MediaItem))).scalars().all():
            await session.delete(item)
        await session.commit()

    async with db.session() as session:
        # 影人行仍在（只增不删），但已经没有任何作品
        assert (await session.execute(select(Person))).scalars().first() is not None
        with pytest.raises(NotFoundException):
            await get_person(901, session, _ADMIN)
