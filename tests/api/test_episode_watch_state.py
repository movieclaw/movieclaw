"""分集清单附带观看进度：分集卡进度条/已看对勾的数据源。

口径必须与首页「最近观看」一致：同一张 ``playback_state`` 表（网页播放器与
Jellyfin 客户端写的是同一张），percent 1~99、完成态由 played 单独表达。
"""

from __future__ import annotations

from datetime import datetime

import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.items import build_season_episodes, episode_view
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    PlaybackState,
)
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'episodes.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _file(
    library_id: int, item_id: int, episode: int, *, duration_seconds: int | None = 2400
) -> LibraryFile:
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=1,
        episode_number=episode,
        file_path=f"/tv/{item_id}/S01E{episode:02d}.mkv",
        size_bytes=1,
        source=FileSource.SCANNED,
        duration_seconds=duration_seconds,
        state=FileState.IN_PLACE,
    )


async def test_episodes_carry_member_watch_state(db) -> None:
    """E1 已看完 → played；E2 看到 1/4 → percent=25；E3 没看过 → 全零。
    别的成员的状态绝不能混进来。"""
    async with db.session() as session:
        repo = LibraryRepository(session)
        library = await repo.create(name="剧集库", kind="tv", root_paths=["/tv"])
        show = MediaItem(kind="tv", tmdb_id=201, title="进度剧", original_title="P")
        session.add(show)
        await session.flush()
        assert library.id and show.id

        files = [_file(library.id, show.id, e) for e in (1, 2, 3)]
        session.add_all(files)
        session.add_all(
            # 有分集元数据 → 装配器不会走 TMDB 实时兜底（测试不出网）
            MediaEpisode(media_item_id=show.id, season_number=1, episode_number=e)
            for e in (1, 2, 3)
        )
        session.add_all(
            [
                PlaybackState(
                    member_id=7,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=1,
                    played=True,
                    position_ms=0,
                    play_count=2,
                    last_played_at=datetime(2026, 8, 20, 20, 0),
                ),
                PlaybackState(
                    member_id=7,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=2,
                    position_ms=600_000,  # 2400s 文件的 25%
                    play_count=1,
                    last_played_at=datetime(2026, 8, 21, 20, 0),
                ),
                # 另一位成员看完了 E3——不能泄露给成员 7
                PlaybackState(
                    member_id=8,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=3,
                    played=True,
                    last_played_at=datetime(2026, 8, 21, 21, 0),
                ),
            ]
        )
        await session.commit()

        episodes = await build_season_episodes(session, show, files, 1, member_id=7)
        by_number = {e.episode_number: e for e in episodes}
        assert by_number[1].played is True
        assert by_number[1].progress_percent is None  # 完成态由 played 表达
        assert by_number[2].played is False
        assert by_number[2].position_ms == 600_000
        assert by_number[2].progress_percent == 25
        assert by_number[3].played is False
        assert by_number[3].position_ms == 0
        assert by_number[3].progress_percent is None

        # 不传 member_id（匿名装配）不附观看状态
        plain = await build_season_episodes(session, show, files, 1)
        assert all(not e.played and e.position_ms == 0 for e in plain)

        # 视图转换原样带出（两个端点共用的那一层）
        view = episode_view(by_number[2])
        assert (view.position_ms, view.played, view.progress_percent) == (600_000, False, 25)


async def test_percent_falls_back_to_episode_runtime(db) -> None:
    """文件没探到时长时，分母退回分集刮削时长（与最近观看同口径）。"""
    async with db.session() as session:
        repo = LibraryRepository(session)
        library = await repo.create(name="剧集库", kind="tv", root_paths=["/tv"])
        show = MediaItem(kind="tv", tmdb_id=202, title="无时长剧", original_title="N")
        session.add(show)
        await session.flush()
        assert library.id and show.id
        files = [_file(library.id, show.id, 1, duration_seconds=None)]
        session.add_all(files)
        session.add(
            MediaEpisode(
                media_item_id=show.id, season_number=1, episode_number=1, runtime_minutes=40
            )
        )
        session.add(
            PlaybackState(
                member_id=7,
                media_item_id=show.id,
                season_number=1,
                episode_number=1,
                position_ms=1_200_000,  # 40 分钟的一半
                play_count=1,
                last_played_at=datetime(2026, 8, 22, 20, 0),
            )
        )
        await session.commit()

        episodes = await build_season_episodes(session, show, files, 1, member_id=7)
        assert episodes[0].progress_percent == 50
