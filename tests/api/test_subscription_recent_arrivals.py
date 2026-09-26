"""订阅首页「刚刚入库」：一部作品一张卡、指向第一集没看完的、整批看完就消失。

规则见 ``services/subscription/recent_arrivals.py``。每条测试对应规则的一个出口，
另有两条守住与「接下来继续」共用的口径：文件失联/库不可见不出卡、只认 played。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription import recent_arrivals
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    PlaybackState,
    RuleSet,
    Subscription,
    WantedItem,
    WantedStatus,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

MEMBER = 7
NOW = datetime(2026, 9, 26, 12, 0)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'recent.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _library(session, kind: str = "tv") -> int:
    library = await LibraryRepository(session).create(
        name=f"{kind} 库", kind=kind, root_paths=[f"/{kind}"]
    )
    assert library.id is not None
    return library.id


async def _subscribe(session, *, kind: str, tmdb_id: int, title: str) -> tuple[Subscription, MediaItem]:
    rule = RuleSet(name=f"规则 {tmdb_id}")
    item = MediaItem(
        kind=kind,
        tmdb_id=tmdb_id,
        title=title,
        original_title=title,
        backdrop_path=f"/backdrop-{tmdb_id}.jpg",
    )
    session.add_all([rule, item])
    await session.flush()
    assert rule.id is not None and item.id is not None
    sub = Subscription(media_item_id=item.id, kind=kind, rule_set_id=rule.id)
    session.add(sub)
    await session.flush()
    return sub, item


def _imported(sub: Subscription, season: int, episode: int, *, at: datetime) -> WantedItem:
    assert sub.id is not None
    return WantedItem(
        subscription_id=sub.id,
        media_item_id=sub.media_item_id,
        season_number=season,
        episode_number=episode,
        status=WantedStatus.IMPORTED,
        imported_at=at,
    )


def _file(library_id: int, item_id: int, season: int, episode: int, *, missing: bool = False) -> LibraryFile:
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=season,
        episode_number=episode,
        file_path=f"/{library_id}/{item_id}/S{season:02d}E{episode:02d}.mkv",
        size_bytes=1,
        source=FileSource.SCANNED,
        duration_seconds=2400,
        missing_since=datetime(2026, 9, 1) if missing else None,
        state=FileState.MISSING if missing else FileState.IN_PLACE,
    )


def _watched(item_id: int, season: int, episode: int, *, played: bool = True, position_ms: int = 0) -> PlaybackState:
    return PlaybackState(
        member_id=MEMBER,
        media_item_id=item_id,
        season_number=season,
        episode_number=episode,
        played=played,
        position_ms=position_ms,
        play_count=1,
        last_played_at=NOW - timedelta(hours=1),
    )


async def _cards(session, subs, visible: set[int] | None):
    return await recent_arrivals(
        session,
        subscriptions=subs,
        member_id=MEMBER,
        visible_library_ids=visible,
        now=NOW,
    )


async def test_one_card_per_show_pointing_at_the_first_unwatched_episode(db) -> None:
    """整季包一次到三集也只占一张卡；看过的那集跳过，入口是下一集，集名与剧照跟着入口走。"""
    async with db.session() as session:
        library = await _library(session)
        sub, show = await _subscribe(session, kind="tv", tmdb_id=10, title="示例剧")
        assert show.id is not None
        session.add_all(
            [
                *(_imported(sub, 1, e, at=NOW - timedelta(hours=5)) for e in (1, 2, 3)),
                *(_file(library, show.id, 1, e) for e in (1, 2, 3)),
                _watched(show.id, 1, 1),
                MediaEpisode(
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=2,
                    name="第二集",
                    still_path="/still-e2.jpg",
                ),
            ]
        )
        await session.commit()

        cards = await _cards(session, [(sub, show)], {library})

    assert len(cards) == 1
    card = cards[0]
    assert card.display == (1, 2)
    assert card.units == [(1, 2), (1, 3)]
    assert card.episode_name == "第二集"
    assert card.still_url is not None and card.still_url.endswith("/w780/still-e2.jpg")
    assert card.progress_percent is None


async def test_a_fully_watched_batch_disappears_and_old_imports_are_out_of_window(db) -> None:
    """整批看完就不再"新"；窗口外（默认 7 天）的入库也不再算刚刚入库。"""
    async with db.session() as session:
        library = await _library(session)
        done, done_item = await _subscribe(session, kind="tv", tmdb_id=11, title="看完了")
        old, old_item = await _subscribe(session, kind="tv", tmdb_id=12, title="很久以前")
        assert done_item.id is not None and old_item.id is not None
        session.add_all(
            [
                _imported(done, 1, 1, at=NOW - timedelta(days=1)),
                _file(library, done_item.id, 1, 1),
                _watched(done_item.id, 1, 1),
                _imported(old, 1, 1, at=NOW - timedelta(days=10)),
                _file(library, old_item.id, 1, 1),
            ]
        )
        await session.commit()

        cards = await _cards(session, [(done, done_item), (old, old_item)], {library})

    assert cards == []


async def test_missing_files_and_invisible_libraries_never_make_a_card(db) -> None:
    """放不了的不出卡：文件失联、或落在当前身份看不见的库里（同「接下来继续」口径）。"""
    async with db.session() as session:
        visible = await _library(session)
        hidden = await _library(session, kind="movie")
        lost, lost_item = await _subscribe(session, kind="tv", tmdb_id=13, title="文件失联")
        private, private_item = await _subscribe(session, kind="movie", tmdb_id=14, title="看不见")
        assert lost_item.id is not None and private_item.id is not None
        session.add_all(
            [
                _imported(lost, 1, 1, at=NOW - timedelta(hours=2)),
                _file(visible, lost_item.id, 1, 1, missing=True),
                _imported(private, 0, 0, at=NOW - timedelta(hours=2)),
                _file(hidden, private_item.id, 0, 0),
            ]
        )
        await session.commit()

        cards = await _cards(session, [(lost, lost_item), (private, private_item)], {visible})

    assert cards == []


async def test_half_watched_movie_keeps_its_progress_and_cards_sort_by_latest_import(db) -> None:
    """电影是哨兵单元 (0,0)，看了一半照样在（只认 played）；卡片按最近入库倒序。"""
    async with db.session() as session:
        library = await _library(session)
        movie, movie_item = await _subscribe(session, kind="movie", tmdb_id=15, title="看一半的电影")
        fresh, fresh_item = await _subscribe(session, kind="tv", tmdb_id=16, title="刚到的剧")
        assert movie_item.id is not None and fresh_item.id is not None
        session.add_all(
            [
                _imported(movie, 0, 0, at=NOW - timedelta(days=2)),
                _file(library, movie_item.id, 0, 0),
                _watched(movie_item.id, 0, 0, played=False, position_ms=600_000),
                _imported(fresh, 2, 1, at=NOW - timedelta(minutes=30)),
                _file(library, fresh_item.id, 2, 1),
            ]
        )
        await session.commit()

        cards = await _cards(session, [(movie, movie_item), (fresh, fresh_item)], None)

    assert [card.media.title for card in cards] == ["刚到的剧", "看一半的电影"]
    movie_card = cards[1]
    assert movie_card.display == (0, 0)
    assert movie_card.progress_percent == 25
    assert movie_card.still_url is None and movie_card.episode_name is None
