"""媒体库首页「接下来继续」：六种状态迁移，外加旧版攒下的那些不变量。

这一块只有一条规则——**从锚点起第一个没看完、且文件在位的单元**——所以测试
就照着这条规则的每一个出口各写一条：电影没看完/看完、剧集当前集没看完/看完、
全剧看完、看完之后又新入库一集。

另有四条是旧版「最近观看」用真实故障换来的，改版不能把它们丢掉：成员隔离、
同一集多版本只算一集、不可见库与缺失文件不混入、洗版刷新入库时间不误报。
"""

from __future__ import annotations

from datetime import datetime

import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.playback_up_next import up_next_items
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    PlaybackState,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

MEMBER = 7


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'up-next.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _file(
    library_id: int,
    item_id: int,
    season: int,
    episode: int,
    *,
    duration_seconds: int = 2400,
    missing: bool = False,
    added_at: datetime = datetime(2026, 8, 1),
    variant: str = "",
) -> LibraryFile:
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=season,
        episode_number=episode,
        file_path=f"/{library_id}/{item_id}/S{season:02d}E{episode:02d}{variant}.mkv",
        size_bytes=1,
        source=FileSource.SCANNED,
        duration_seconds=duration_seconds,
        missing_since=datetime(2026, 8, 1) if missing else None,
        state=FileState.MISSING if missing else FileState.IN_PLACE,
        created_at=added_at,
        updated_at=added_at,
    )


def _state(
    item_id: int,
    season: int,
    episode: int,
    *,
    played: bool = False,
    position_ms: int = 0,
    at: datetime = datetime(2026, 8, 15, 20, 0),
    member_id: int = MEMBER,
) -> PlaybackState:
    return PlaybackState(
        member_id=member_id,
        media_item_id=item_id,
        season_number=season,
        episode_number=episode,
        played=played,
        position_ms=position_ms,
        play_count=1,
        last_played_at=at,
    )


async def _cards(session, visible: set[int] | None, limit: int = 20):
    return await up_next_items(
        session, member_id=MEMBER, visible_library_ids=visible, limit=limit
    )


# ---------------------------------------------------------------------------
# 一条规则的六个出口
# ---------------------------------------------------------------------------


async def test_a_finished_movie_disappears_and_an_unfinished_one_stays(db) -> None:
    """电影看完就没了；没看完的留着接着看。

    这是改版最直接的一条：旧版「最近观看」会把"已看完"的电影一直摆在首页
    最贵的位置上，除了陈述一件用户本来就知道的事什么也做不了。
    """
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/m"]
        )
        done = MediaItem(kind="movie", tmdb_id=1, title="看完了", original_title="Done")
        half = MediaItem(kind="movie", tmdb_id=2, title="看一半", original_title="Half")
        session.add_all([done, half])
        await session.flush()
        assert library.id and done.id and half.id
        session.add_all(
            [
                _file(library.id, done.id, 0, 0),
                _file(library.id, half.id, 0, 0),
                _state(done.id, 0, 0, played=True, at=datetime(2026, 8, 16)),
                _state(half.id, 0, 0, position_ms=600_000, at=datetime(2026, 8, 15)),
            ]
        )
        await session.commit()

        rows = await _cards(session, {library.id})
        assert [row.media_item_id for row in rows] == [half.id]
        assert rows[0].progress_percent == 25
        assert rows[0].advanced is False


async def test_finishing_an_episode_moves_the_card_to_the_next_one(db) -> None:
    """看完一集，卡片换成下一集——这正是旧版要点三步才能做到的事。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        show = MediaItem(kind="tv", tmdb_id=10, title="示例剧", original_title="Show")
        session.add(show)
        await session.flush()
        assert library.id and show.id
        session.add_all(
            [
                _file(library.id, show.id, 1, 1),
                _file(library.id, show.id, 1, 2),
                _file(library.id, show.id, 1, 3),
                MediaEpisode(
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=2,
                    name="第二集",
                    still_path="/e2.jpg",
                ),
                _state(show.id, 1, 1, played=True),
            ]
        )
        await session.commit()

        rows = await _cards(session, {library.id})
        assert len(rows) == 1
        card = rows[0]
        assert (card.season_number, card.episode_number) == (1, 2)
        # 标题、剧照、时长都要跟着换成**卡片这一集**的，不能还留在上一集上
        assert card.episode_title == "第二集"
        assert card.episode_still_url == "https://image.tmdb.org/t/p/w500/e2.jpg"
        assert card.advanced is True
        assert card.position_ms == 0
        # 角标相对卡片这一集算：E02 之后还剩 E03 一集
        assert card.unwatched_ahead_count == 1


async def test_a_half_watched_episode_is_not_skipped(db) -> None:
    """当前集没看完就是它，不跳下一集。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        show = MediaItem(kind="tv", tmdb_id=11, title="示例剧", original_title="Show")
        session.add(show)
        await session.flush()
        assert library.id and show.id
        session.add_all(
            [
                _file(library.id, show.id, 1, 1),
                _file(library.id, show.id, 1, 2),
                _state(show.id, 1, 1, position_ms=600_000),
            ]
        )
        await session.commit()

        card = (await _cards(session, {library.id}))[0]
        assert (card.season_number, card.episode_number) == (1, 1)
        assert card.advanced is False
        assert card.progress_percent == 25


async def test_going_back_to_finish_an_earlier_episode_keeps_the_half_watched_one(db) -> None:
    """先看了 E02 一半、又回头补完 E01，卡片必须回到 E02——不能跳到 E03。

    旧版判"看过"用的是 ``played OR last_played_at 非空``。那条口径放在这里会
    把 E02 当成"碰过 = 看过"跳掉，用户看了一半的那一集反而回不去了。所以这里
    只认 ``played``。
    """
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        show = MediaItem(kind="tv", tmdb_id=12, title="示例剧", original_title="Show")
        session.add(show)
        await session.flush()
        assert library.id and show.id
        session.add_all(
            [
                _file(library.id, show.id, 1, 1),
                _file(library.id, show.id, 1, 2),
                _file(library.id, show.id, 1, 3),
                # 先看 E02 一半
                _state(show.id, 1, 2, position_ms=600_000, at=datetime(2026, 8, 15)),
                # 再回头把 E01 补完——它更近，于是成了锚点
                _state(show.id, 1, 1, played=True, at=datetime(2026, 8, 16)),
            ]
        )
        await session.commit()

        card = (await _cards(session, {library.id}))[0]
        assert (card.season_number, card.episode_number) == (1, 2)
        assert card.position_ms == 600_000, "看了一半的那一集要能原地接着看"
        assert card.advanced is True


async def test_a_finished_show_disappears_until_a_new_episode_lands(db) -> None:
    """整部剧看完就消失；之后新入库一集，它自己回来。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        show = MediaItem(kind="tv", tmdb_id=13, title="追平了", original_title="Caught Up")
        session.add(show)
        await session.flush()
        assert library.id and show.id
        session.add_all(
            [
                _file(library.id, show.id, 1, 1),
                _file(library.id, show.id, 1, 2),
                _state(show.id, 1, 1, played=True, at=datetime(2026, 8, 14)),
                _state(show.id, 1, 2, played=True, at=datetime(2026, 8, 15)),
            ]
        )
        await session.commit()
        assert await _cards(session, {library.id}) == []

        # 新一集入库——不需要用户做任何事，它就该回到首页
        session.add(_file(library.id, show.id, 1, 3, added_at=datetime(2026, 9, 1)))
        await session.commit()

        card = (await _cards(session, {library.id}))[0]
        assert (card.season_number, card.episode_number) == (1, 3)
        assert card.advanced is True
        assert card.unwatched_ahead_count == 0


async def test_a_missing_file_is_not_something_you_can_continue(db) -> None:
    """下一集的文件失联就跳过它——"接下来继续"的前提是真能放。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        show = MediaItem(kind="tv", tmdb_id=14, title="示例剧", original_title="Show")
        session.add(show)
        await session.flush()
        assert library.id and show.id
        session.add_all(
            [
                _file(library.id, show.id, 1, 1),
                _file(library.id, show.id, 1, 2, missing=True),
                _file(library.id, show.id, 1, 3),
                _state(show.id, 1, 1, played=True),
            ]
        )
        await session.commit()

        card = (await _cards(session, {library.id}))[0]
        assert (card.season_number, card.episode_number) == (1, 3)
        assert card.unwatched_ahead_count == 0


# ---------------------------------------------------------------------------
# 旧版攒下的不变量：这些都是真实故障换来的，改版一条都不能丢
# ---------------------------------------------------------------------------


async def test_member_scoped_and_visibility_narrowed(db) -> None:
    """别人的进度不混进来；不可见库里的作品一张卡都不出。"""
    async with db.session() as session:
        repo = LibraryRepository(session)
        shared = await repo.create(name="电影库", kind="movie", root_paths=["/m"])
        private = await repo.create(name="私藏库", kind="movie", root_paths=["/p"])
        mine = MediaItem(kind="movie", tmdb_id=20, title="我在看", original_title="Mine")
        hidden = MediaItem(kind="movie", tmdb_id=21, title="私藏", original_title="Hidden")
        theirs = MediaItem(kind="movie", tmdb_id=22, title="别人在看", original_title="Theirs")
        session.add_all([mine, hidden, theirs])
        await session.flush()
        assert shared.id and private.id and mine.id and hidden.id and theirs.id
        session.add_all(
            [
                _file(shared.id, mine.id, 0, 0),
                _file(private.id, hidden.id, 0, 0),
                _file(shared.id, theirs.id, 0, 0),
                _state(mine.id, 0, 0, position_ms=60_000),
                _state(hidden.id, 0, 0, position_ms=60_000),
                _state(theirs.id, 0, 0, position_ms=60_000, member_id=MEMBER + 1),
            ]
        )
        await session.commit()

        rows = await _cards(session, {shared.id})
        assert [row.media_item_id for row in rows] == [mine.id]
        # 一个库都看不见时直接空表，不是"全都看得见"
        assert await _cards(session, set()) == []


async def test_one_episode_counts_once_no_matter_how_many_versions(db) -> None:
    """同一集的 1080p 与 2160p 是一集，不是两集——角标不能翻倍。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        show = MediaItem(kind="tv", tmdb_id=30, title="示例剧", original_title="Show")
        session.add(show)
        await session.flush()
        assert library.id and show.id
        session.add_all(
            [
                _file(library.id, show.id, 1, 1),
                _file(library.id, show.id, 1, 2),
                _file(library.id, show.id, 1, 2, variant="-2160p"),
                _file(library.id, show.id, 1, 3),
                _file(library.id, show.id, 1, 3, variant="-2160p"),
                _state(show.id, 1, 1, played=True),
            ]
        )
        await session.commit()

        card = (await _cards(session, {library.id}))[0]
        assert (card.season_number, card.episode_number) == (1, 2)
        assert card.unwatched_ahead_count == 1, "E03 的两个版本只能算一集"


async def test_a_rewritten_added_time_does_not_resurrect_a_watched_episode(db) -> None:
    """洗版会把老集的入库时间刷成今天——那不是"新内容"，不该被当成可看的下一集。

    角标口径因此是"季集排在卡片之后 + 没看完"，与入库时间无关。
    """
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        show = MediaItem(kind="tv", tmdb_id=31, title="示例剧", original_title="Show")
        session.add(show)
        await session.flush()
        assert library.id and show.id
        session.add_all(
            [
                # E01 洗版：入库时间被刷新成最近，但它早就看完了
                _file(library.id, show.id, 1, 1, added_at=datetime(2026, 9, 1)),
                _file(library.id, show.id, 1, 2),
                _state(show.id, 1, 1, played=True, at=datetime(2026, 8, 1)),
                _state(show.id, 1, 2, played=True, at=datetime(2026, 8, 2)),
            ]
        )
        await session.commit()

        assert await _cards(session, {library.id}) == [], "全看完了，洗版不该把它捞回来"


async def test_cross_library_card_lands_in_the_first_visible_library(db) -> None:
    """同一部片躺在两个库里时，落点取媒体库首页顺序的第一个可见库。"""
    async with db.session() as session:
        repo = LibraryRepository(session)
        first = await repo.create(name="电影库", kind="movie", root_paths=["/m1"])
        second = await repo.create(name="4K 电影库", kind="movie", root_paths=["/m2"])
        movie = MediaItem(kind="movie", tmdb_id=40, title="两处都有", original_title="Both")
        session.add(movie)
        await session.flush()
        assert first.id and second.id and movie.id
        session.add_all(
            [
                _file(second.id, movie.id, 0, 0),
                _file(first.id, movie.id, 0, 0),
                _state(movie.id, 0, 0, position_ms=60_000),
            ]
        )
        await session.commit()

        # 只看得见第二个库时，落点必须是它——否则卡片点进去是 404
        only_second = (await _cards(session, {second.id}))[0]
        assert only_second.library_id == second.id
        both = (await _cards(session, {first.id, second.id}))[0]
        assert both.library_id == first.id


async def test_limit_caps_the_row(db) -> None:
    """limit 卡的是卡片数，不是扫描深度——中间夹着看完的作品也要凑满。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/m"]
        )
        for index in range(6):
            movie = MediaItem(
                kind="movie", tmdb_id=50 + index, title=f"片{index}", original_title=f"M{index}"
            )
            session.add(movie)
            await session.flush()
            assert library.id and movie.id
            session.add_all(
                [
                    _file(library.id, movie.id, 0, 0),
                    # 偶数号看完了：它们不出卡，但不该让后面的片被 limit 挤掉
                    _state(
                        movie.id,
                        0,
                        0,
                        played=index % 2 == 0,
                        position_ms=0 if index % 2 == 0 else 60_000,
                        at=datetime(2026, 8, 1 + index),
                    ),
                ]
            )
        await session.commit()

        assert len(await _cards(session, {library.id})) == 3
        assert len(await _cards(session, {library.id}, limit=2)) == 2


async def test_a_movie_with_metadata_carries_its_runtime_and_aspect(db) -> None:
    """时长优先取真实文件；本地 16:9 封面要把宽高比带出来（卡片靠它决定铺法）。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/m"]
        )
        movie = MediaItem(
            kind="movie",
            tmdb_id=60,
            title="示例电影",
            original_title="Movie",
            backdrop_path="/backdrop.jpg",
        )
        session.add(movie)
        await session.flush()
        assert library.id and movie.id
        session.add_all(
            [
                _file(library.id, movie.id, 0, 0, duration_seconds=7200),
                MediaMetadata(
                    media_item_id=movie.id,
                    runtime_minutes=130,
                    poster_width=1280,
                    poster_height=720,
                ),
                _state(movie.id, 0, 0, position_ms=3_600_000),
            ]
        )
        await session.commit()

        card = (await _cards(session, {library.id}))[0]
        assert card.duration_ms == 7_200_000, "真实文件时长优先于档案里的 130 分钟"
        assert card.progress_percent == 50
        assert card.poster_aspect == 1.7778
        assert card.backdrop_url == "https://image.tmdb.org/t/p/w780/backdrop.jpg"
        assert card.episode_still_url is None
        assert card.unwatched_ahead_count == 0
