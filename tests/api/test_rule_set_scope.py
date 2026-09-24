"""规则组适用范围（docs/design/rule-set-scope.md）的测试。

覆盖：
- validate_scope 写入校验：kind 取值收紧、电影+剧集等于不限（丢弃）、其余
  字段复用库收藏范围的校验且报错文案用「适用范围」；
- scope_matches：kind 与其他条件 AND、只写 kind 时元数据缺失也能命中、
  其余条件沿用保守语义；
- pick：特异性优先、打平取创建更早、未命中/无声明落默认组，理由可读；
- 订阅创建：不指定规则组时按适用范围选组并写进创建活动；显式指定压倒自动；
- API：CRUD 携带 match_rules，更新时缺省不改、[] 清空。
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services.library.routing import RoutingFacts
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.rule_sets import RuleSetService, scope_matches, validate_scope
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import SubscriptionActivity
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_MOVIE = {"field": "kind", "op": "any_of", "values": ["movie"]}
_TV = {"field": "kind", "op": "any_of", "values": ["tv"]}
_ANIME = {"field": "genres", "op": "any_of", "values": [16]}
_JP_KR = {"field": "origin_countries", "op": "any_of", "values": ["JP", "KR"]}

_ANIME_JP = RoutingFacts(genre_ids=(16,), origin_countries=("JP",))
_DRAMA_US = RoutingFacts(genre_ids=(18,), origin_countries=("US",))


# ---------------------------------------------------------------------------
# 纯函数：校验与求值
# ---------------------------------------------------------------------------


def test_validate_scope_normalizes() -> None:
    cleaned = validate_scope(
        [{"field": "kind", "values": ["tv"]}, {"field": "origin_countries", "values": ["kr", "jp"]}]
    )
    assert cleaned == [
        {"field": "kind", "op": "any_of", "values": ["tv"]},
        {"field": "origin_countries", "op": "any_of", "values": ["JP", "KR"]},
    ]
    # 电影+剧集等于不限类型：丢弃，不白白抬高特异性
    assert validate_scope([{"field": "kind", "values": ["movie", "tv"]}]) == []
    assert validate_scope(None) == []


def test_validate_scope_rejects_bad_input() -> None:
    with pytest.raises(BadRequestException, match="movie（电影）或 tv（剧集）"):
        validate_scope([{"field": "kind", "values": ["anime"]}])
    with pytest.raises(BadRequestException, match="不能为空"):
        validate_scope([{"field": "kind", "values": []}])
    with pytest.raises(BadRequestException, match="只能出现一次"):
        validate_scope([_TV, _MOVIE])
    # 其余字段走库收藏范围的校验，报错名词换成「适用范围」
    with pytest.raises(BadRequestException, match="适用范围条件包含不支持的字段"):
        validate_scope([{"field": "directors", "values": [1]}])


def test_scope_matches_semantics() -> None:
    # 只写 kind：类型对就命中，元数据拿不到也命中（kind 永远已知）
    assert scope_matches([_TV], "tv", None) is True
    assert scope_matches([_TV], "movie", _ANIME_JP) is False
    # kind 与其他条件 AND
    assert scope_matches([_TV, _ANIME, _JP_KR], "tv", _ANIME_JP) is True
    assert scope_matches([_TV, _ANIME, _JP_KR], "movie", _ANIME_JP) is False
    assert scope_matches([_TV, _JP_KR], "tv", _DRAMA_US) is False
    # 不写 kind：电影剧集都适用；其余条件事实缺失时保守不命中
    assert scope_matches([_JP_KR], "movie", _ANIME_JP) is True
    assert scope_matches([_JP_KR], "tv", None) is False
    # 空声明不参与命中
    assert scope_matches([], "tv", _ANIME_JP) is False


# ---------------------------------------------------------------------------
# pick：选组
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'scope.db'}")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def test_pick_without_scopes_uses_default(db) -> None:
    """没有任何组声明适用范围时行为与升级前一致：一律默认组。"""
    async with db.session() as session:
        service = RuleSetService(session)
        default = await service.ensure_default()
        pick = await service.pick("tv", _ANIME_JP)
        assert pick.rule_set.id == default.id
        assert pick.matched is False
        assert pick.reason == f"使用默认规则组「{default.name}」"


async def test_pick_specificity_and_tiebreak(db) -> None:
    async with db.session() as session:
        service = RuleSetService(session)
        default = await service.ensure_default()
        movies = await service.create("电影 4K", {"resolutions": ["2160p"]}, [_MOVIE])
        shows = await service.create("剧集 1080p", {"resolutions": ["1080p"]}, [_TV])
        anime = await service.create("日韩动画", {}, [_TV, _ANIME, _JP_KR])
        asia = await service.create("日韩", {}, [_JP_KR])

        # 按类型分默认：只写 kind 的组承接该类型
        pick = await service.pick("movie", _DRAMA_US)
        assert (pick.rule_set.id, pick.matched) == (movies.id, True)
        assert pick.reason == "按适用范围选用「电影 4K」：电影"
        pick = await service.pick("tv", _DRAMA_US)
        assert pick.rule_set.id == shows.id

        # 越具体越优先：三条件的「日韩动画」压过只写类型的「剧集 1080p」
        pick = await service.pick("tv", _ANIME_JP)
        assert pick.rule_set.id == anime.id
        assert pick.reason == "按适用范围选用「日韩动画」：剧集、类型=动画、区域=日本"

        # 条件数打平（「电影 4K」与「日韩」各一条）取创建更早的
        pick = await service.pick("movie", _ANIME_JP)
        assert pick.rule_set.id == movies.id
        assert asia.id > movies.id

        # 元数据拿不到：只写类型的组照样命中
        pick = await service.pick("tv", None)
        assert pick.rule_set.id == shows.id

        # 删掉类型组后，未命中落默认组并写明原因
        await service.delete(movies.id)
        pick = await service.pick("movie", _DRAMA_US)
        assert (pick.rule_set.id, pick.matched) == (default.id, False)
        assert "未命中任何规则组的适用范围" in pick.reason
        pick = await service.pick("movie", None)
        assert "作品元数据暂不可得" in pick.reason


async def test_update_keeps_scope_when_omitted(db) -> None:
    async with db.session() as session:
        service = RuleSetService(session)
        row = await service.create("剧集", {}, [_TV])
        row = await service.update(row.id, name="剧集", spec={"free_only": True})
        assert row.match_rules == [_TV]  # 缺省 = 不改
        row = await service.update(row.id, name="剧集", spec={}, match_rules=[])
        assert row.match_rules == []  # [] = 清空


# ---------------------------------------------------------------------------
# 订阅创建：自动选组 + 活动理由
# ---------------------------------------------------------------------------

_KEY = "0123456789abcdef0123456789abcdef"
_TMDB = {
    "/3/movie/300": {
        "id": 300,
        "title": "美国剧情片",
        "original_title": "US Drama",
        "release_date": "2020-01-01",
        "status": "Released",
        "genres": [{"id": 18, "name": "剧情"}],
        "production_countries": [{"iso_3166_1": "US"}],
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    "/3/tv/400": {
        "id": 400,
        "name": "日本动画",
        "original_name": "JP Anime",
        "first_air_date": "2020-01-01",
        "status": "Ended",
        "genres": [{"id": 16, "name": "动画"}],
        "origin_country": ["JP"],
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "seasons": [{"season_number": 1}],
    },
    "/3/tv/400/season/1": {
        "name": "第 1 季",
        "air_date": "2020-01-01",
        "episodes": [{"episode_number": 1, "name": "E1", "air_date": "2020-01-01"}],
    },
}


def _service(session) -> SubscriptionService:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _TMDB.get(request.url.path)
        return httpx.Response(200, json=payload) if payload else httpx.Response(404, json={})

    tmdb = TmdbClient(_KEY, transport=httpx.MockTransport(handler))
    return SubscriptionService(session, MediaLibraryService(session, tmdb))


async def _created_message(session, subscription_id: int) -> str:
    rows = await session.execute(
        select(SubscriptionActivity).where(SubscriptionActivity.subscription_id == subscription_id)
    )
    return next(a.message for a in rows.scalars().all() if a.type == "created")


async def test_create_picks_rule_set_by_scope(db) -> None:
    async with db.session() as session:
        rules = RuleSetService(session)
        await rules.ensure_default()
        movies = await rules.create("电影 4K", {"resolutions": ["2160p"]}, [_MOVIE])
        anime = await rules.create("日韩动画", {}, [_TV, _ANIME, _JP_KR])
        service = _service(session)

        movie_sub = await service.create(MediaKind.MOVIE, 300)
        assert movie_sub.rule_set_id == movies.id
        assert "按适用范围选用「电影 4K」" in await _created_message(session, movie_sub.id)

        tv_sub = await service.create(MediaKind.TV, 400, selected_seasons=[1])
        assert tv_sub.rule_set_id == anime.id
        assert "按适用范围选用「日韩动画」：剧集、类型=动画、区域=日本" in (
            await _created_message(session, tv_sub.id)
        )


async def test_explicit_rule_set_wins_over_scope(db) -> None:
    async with db.session() as session:
        rules = RuleSetService(session)
        default = await rules.ensure_default()
        await rules.create("电影 4K", {}, [_MOVIE])
        sub = await _service(session).create(MediaKind.MOVIE, 300, rule_set_id=default.id)
        assert sub.rule_set_id == default.id
        assert "按适用范围" not in await _created_message(session, sub.id)


async def test_dispatch_preview_reports_picked_rule_set(db) -> None:
    """订阅弹窗与「模拟一单」的预检带出自动选组结论（与库路由同一时机）。"""
    from movieclaw_api.services.subscription import preview_dispatch_route

    async with db.session() as session:
        rules = RuleSetService(session)
        anime = await rules.create("日韩动画", {}, [_TV, _ANIME, _JP_KR])
        await _service(session).prepare(MediaKind.TV, 400)  # 建档（弹窗打开即建）

        preview = await preview_dispatch_route(session, kind="tv", library_id=None, tmdb_id=400)
        assert preview["rule_set_id"] == anime.id
        assert preview["rule_set_name"] == "日韩动画"
        assert preview["rule_set_matched"] is True
        assert "区域=日本" in preview["rule_set_reason"]

        # 显式选库（用户在弹窗里改库）不走路由，也不重复给选组结论
        explicit = await preview_dispatch_route(session, kind="tv", library_id=1, tmdb_id=400)
        assert "rule_set_id" not in explicit


# ---------------------------------------------------------------------------
# API：CRUD 携带适用范围
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()

    from movieclaw_api.api.deps import require_admin, require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    admin = Principal(kind="admin", name="tester")
    app.dependency_overrides[require_login] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_rule_set_api_carries_match_rules(client) -> None:
    r = client.post(
        "/api/v1/rule-sets",
        json={"name": "剧集", "spec": {}, "match_rules": [{"field": "kind", "values": ["tv"]}]},
    )
    assert r.status_code == 200, r.text
    row = r.json()["data"]
    assert row["match_rules"] == [_TV]

    # 更新不带 match_rules：保留
    r = client.put(f"/api/v1/rule-sets/{row['id']}", json={"name": "剧集", "spec": {}})
    assert r.json()["data"]["match_rules"] == [_TV]

    # 非法取值给 400 中文报错
    r = client.put(
        f"/api/v1/rule-sets/{row['id']}",
        json={"name": "剧集", "spec": {}, "match_rules": [{"field": "kind", "values": ["x"]}]},
    )
    assert r.status_code == 400
    assert "作品类型" in r.json()["message"]

    listed = client.get("/api/v1/rule-sets").json()["data"]
    assert {r["name"]: r["match_rules"] for r in listed}["剧集"] == [_TV]
