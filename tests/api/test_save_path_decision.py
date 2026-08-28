"""投递目录三级兜底统一入口（library_routing.resolve_save_path）的专项测试。

覆盖：三级兜底逐级断言、entry_level 只在条目级目录为真，以及最关键的
**口径对拍**——体检（pipeline_health）与预检（preview_dispatch_route）对
同一配置给出的 mode/path 必须与统一决策一致，消灭"体检说好、投递却挂"。
"""
from __future__ import annotations

import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.import_watch_config import ImportWatchConfigService
from movieclaw_api.services.library.routing import resolve_save_path
from movieclaw_api.services.subscription.dispatch import preview_dispatch_route
from movieclaw_api.services.subscription.health import pipeline_health
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'route.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _make_library(db, *, name: str, root=None, kind="movie"):
    async with db.session() as session:
        row = await LibraryRepository(session).create(
            name=name, kind=kind, root_paths=[str(root)] if root else [], match_rules=[]
        )
        await session.refresh(row)
        return row


async def test_three_level_fallback(db, tmp_path):
    """① 监听规则 → ② 条目目录/库主根 → ③ 下载器默认目录。"""
    root = tmp_path / "movies"
    root.mkdir()
    library = await _make_library(db, name="电影库", root=root)

    async with db.session() as session:
        # ② 无规则 + 给 title → 条目目录，entry_level=True
        d = await resolve_save_path(session, library, kind="movie", title="流浪地球", year=2019)
        assert d.mode == "inplace"
        assert d.path == f"{root}/流浪地球 (2019)"
        assert d.entry_level is True
        assert d.entry_dir == d.path

        # ② 无规则 + 不给 title（预检口径）→ 库主根，entry_level=False
        d = await resolve_save_path(session, library, kind="movie")
        assert d.mode == "inplace"
        assert d.path == str(root)
        assert d.entry_level is False

        # ① 配了监听规则 → 规则源目录优先，entry_dir 仍是条目目录（供文案）
        watch = tmp_path / "watch"
        watch.mkdir()
        await ImportWatchConfigService(session).create(
            source_path=str(watch), strategy="copy", library_id=library.id
        )
        d = await resolve_save_path(session, library, kind="movie", title="流浪地球", year=2019)
        assert d.mode == "watch"
        assert d.path == str(watch)
        assert d.entry_level is False
        assert d.entry_dir == f"{root}/流浪地球 (2019)"
        assert d.rule is not None

        # ③ 没有库 → 下载器默认目录
        d = await resolve_save_path(session, None, kind="tv")
        assert d.mode == "downloader_default"
        assert d.path is None


async def test_no_root_library_falls_to_downloader_default(db):
    """库没配根路径：给不出条目目录，落下载器默认目录。"""
    library = await _make_library(db, name="空库")
    async with db.session() as session:
        d = await resolve_save_path(session, library, kind="movie", title="某片", year=2020)
        assert d.mode == "downloader_default"
        assert d.path is None
        assert d.entry_level is False


async def test_health_and_preview_agree_with_decision(db, tmp_path):
    """对拍：体检与预检的 mode/path 与统一决策一致（口径同源的回归锚）。"""
    root = tmp_path / "tv"
    root.mkdir()
    library = await _make_library(db, name="剧集库", root=root, kind="tv")

    async with db.session() as session:
        decision = await resolve_save_path(session, library, kind="tv")

        preview = await preview_dispatch_route(session, kind="tv", library_id=library.id)
        assert (preview["mode"], preview["path"]) == (decision.mode, decision.path)

        health = await pipeline_health(session)
        pipe = next(p for p in health["libraries"] if p["library_id"] == library.id)
        assert (pipe["mode"], pipe["path"]) == (decision.mode, decision.path)

    # 换成 watch 模式再对拍一次（同一配置改动同时反映在三处）
    watch = tmp_path / "watch"
    watch.mkdir()
    async with db.session() as session:
        await ImportWatchConfigService(session).create(
            source_path=str(watch), strategy="copy", library_id=library.id
        )
        decision = await resolve_save_path(session, library, kind="tv")
        assert decision.mode == "watch"

        preview = await preview_dispatch_route(session, kind="tv", library_id=library.id)
        assert (preview["mode"], preview["path"]) == (decision.mode, decision.path)

        health = await pipeline_health(session)
        pipe = next(p for p in health["libraries"] if p["library_id"] == library.id)
        assert (pipe["mode"], pipe["path"]) == (decision.mode, decision.path)


# ---------------------------------------------------------------------------
# 条目目录预览（entry_dir）：前端文案的唯一来源
# ---------------------------------------------------------------------------


async def test_preview_entry_dir_follows_naming_template(db, tmp_path, monkeypatch):
    """预检返回的 entry_dir 必须由命名模板渲染，而不是写死「标题 (年份)」。

    这条是给**前端文案**兜底的：模板化之前，下载弹窗/订阅弹窗/模拟一单
    都自己拼 ``${title} (${year})``，用户一改模板预览就与真实落点对不上。
    改成后端下发 entry_dir 后，这里断言"改模板 → 预览跟着变"。
    """
    from movieclaw_api.services import scrape_config
    from movieclaw_api.settings.metadata import MetadataScrapeSetting

    root = tmp_path / "movies"
    root.mkdir()
    library = await _make_library(db, name="电影库", root=root)

    async with db.session() as session:
        # 默认模板：与改动前的写死行为逐字节一致（年份括号前有空格）
        preview = await preview_dispatch_route(
            session, kind="movie", library_id=library.id, title="流浪地球", year=2019
        )
        assert preview["entry_dir"] == f"{root}/流浪地球 (2019)"

        # 换一套模板：预览必须跟着变（证明它走的是模板而非写死拼接）
        monkeypatch.setattr(
            scrape_config,
            "_current_scrape",
            MetadataScrapeSetting(naming_entry_dir="{year} - {title}"),
        )
        preview = await preview_dispatch_route(
            session, kind="movie", library_id=library.id, title="流浪地球", year=2019
        )
        assert preview["entry_dir"] == f"{root}/2019 - 流浪地球"


async def test_preview_entry_dir_prefers_persisted_item(db, tmp_path, monkeypatch):
    """已建档条目：标题取库里的权威标题，且 {tmdb_id} 之类的占位符渲染得出。

    手动下载弹窗传的是种子识别出来的标题，真实投递却是按 TMDB 身份建档后
    推导目录的——两者不一致时以条目为准，预览才等于真实落点。
    """
    from movieclaw_api.services import scrape_config
    from movieclaw_api.settings.metadata import MetadataScrapeSetting
    from movieclaw_db.models import MediaItem

    root = tmp_path / "movies"
    root.mkdir()
    library = await _make_library(db, name="电影库", root=root)

    async with db.session() as session:
        session.add(
            MediaItem(
                kind="movie",
                tmdb_id=529203,
                title="疯狂动物城",
                original_title="Zootopia",
                year=2016,
                aliases=[],
            )
        )
        await session.commit()

    monkeypatch.setattr(
        scrape_config,
        "_current_scrape",
        MetadataScrapeSetting(naming_entry_dir="{title} ({year}) [tmdbid-{tmdb_id}]"),
    )
    async with db.session() as session:
        preview = await preview_dispatch_route(
            session,
            kind="movie",
            library_id=library.id,
            tmdb_id=529203,
            title="Zootopia.2016.1080p",  # 种子标题：应当被条目标题覆盖
            year=2016,
        )
        assert preview["entry_dir"] == f"{root}/疯狂动物城 (2016) [tmdbid-529203]"


async def test_preview_entry_dir_none_without_identity(db, tmp_path):
    """没有身份（体检/纯选库预检）时不臆造条目目录，entry_dir 为空。"""
    root = tmp_path / "movies"
    root.mkdir()
    library = await _make_library(db, name="电影库", root=root)
    async with db.session() as session:
        preview = await preview_dispatch_route(session, kind="movie", library_id=library.id)
        assert preview["entry_dir"] is None
