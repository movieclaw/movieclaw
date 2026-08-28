"""播放决策服务层测试（docs/design/web-player.md §3）。

判定逻辑本身由 ``tests/playback/test_decide.py`` 表驱动覆盖；这里只测服务层
真正负责的三件事：**多候选择优、可见库过滤、配置读取**——以及三态结果到
响应模型的翻译。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.playback import ClientCapabilityIn
from movieclaw_api.services.playback import plan as playback_plan
from movieclaw_api.settings import PlaybackPolicySetting
from movieclaw_api.settings.store import (
    get_setting_store,
    init_setting_store,
    reset_setting_store,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_playback.capability import AudioSupport, ClientCapability, VideoSupport
from movieclaw_playback.decide import ConsentRequired, PlaybackPlan, PlaybackTier

# 只能解 H.264 + AAC 的浏览器：HEVC 一律要转码
CHROME = ClientCapability(
    video=(VideoSupport("h264"),),
    audio=(AudioSupport("aac"),),
    containers=frozenset({"mp4", "hls-fmp4"}),
)


def test_capability_from_request_preserves_native_hls_flag():
    capability = playback_plan.capability_from_request(ClientCapabilityIn(native_hls=True))

    assert capability.native_hls is True


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'decide.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    reset_setting_store()
    init_setting_store(database=get_database())
    yield get_database()
    reset_setting_store()
    await dispose_db()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def stub_probes(monkeypatch):
    """探测层打桩：测试环境没有 ffprobe，也没有真实媒体文件。

    关键帧间隔固定给 4 秒（正常 GOP），硬件加速固定不可用——这样档位差异
    完全由被测的候选择优与配置决定，不受环境影响。
    """
    monkeypatch.setattr(
        playback_plan, "probe_keyframe_interval", lambda path, duration: 4.0
    )
    monkeypatch.setattr(playback_plan, "hardware_available", lambda: False)


def _file(library_id: int, item_id: int, *, codec: str, resolution: str) -> LibraryFile:
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=0,
        episode_number=0,
        file_path=f"/lib{library_id}/{item_id}.{codec}.{resolution}.mkv",
        size_bytes=1,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
        container="mkv",
        video_codec=codec,
        resolution=resolution,
        duration_seconds=5400,
        audio_streams=[{"codec": "aac", "channels": 6, "default": True}],
    )


async def test_prefers_direct_playable_version_over_higher_resolution(db):
    """能直通的 1080p 胜过要转码的 2160p——直通率是这个播放器的北极星指标。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        item = MediaItem(kind="movie", tmdb_id=1, title="示例", original_title="Example")
        session.add(item)
        await session.flush()
        uhd = _file(library.id, item.id, codec="hevc", resolution="2160p")
        hd = _file(library.id, item.id, codec="h264", resolution="1080p")
        session.add_all([uhd, hd])
        await session.commit()
        await session.refresh(hd)

        decision = await playback_plan.decide_for_files(
            [uhd, hd], CHROME, can_self_enable=True
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX
    assert decision.file_id == hd.id


async def test_file_outside_visible_libraries_is_not_found(db):
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="私藏库", kind="movie", root_paths=["/private"]
        )
        item = MediaItem(kind="movie", tmdb_id=2, title="私藏", original_title="Private")
        session.add(item)
        await session.flush()
        row = _file(library.id, item.id, codec="h264", resolution="1080p")
        session.add(row)
        await session.commit()
        await session.refresh(row)

        assert (
            await playback_plan.decide_for_file(
                session,
                row.id,
                CHROME,
                can_self_enable=True,
                visible_library_ids=set(),
            )
            is None
        )
        # 可见时正常出计划
        decision = await playback_plan.decide_for_file(
            session,
            row.id,
            CHROME,
            can_self_enable=True,
            visible_library_ids={library.id},
        )
    assert isinstance(decision, PlaybackPlan)


async def test_software_transcode_switch_comes_from_settings(db):
    """无硬件 + HEVC → 要么请求同意，要么（开关已开）直接给软转计划。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        item = MediaItem(kind="movie", tmdb_id=3, title="示例", original_title="Example")
        session.add(item)
        await session.flush()
        row = _file(library.id, item.id, codec="hevc", resolution="1080p")
        session.add(row)
        await session.commit()

        first = await playback_plan.decide_for_files([row], CHROME, can_self_enable=True)
        assert isinstance(first, ConsentRequired)
        assert first.setting_namespace == "playback.policy"

        await get_setting_store().set(
            PlaybackPolicySetting(software_transcode_enabled=True)
        )
        second = await playback_plan.decide_for_files([row], CHROME, can_self_enable=True)

    assert isinstance(second, PlaybackPlan)
    assert second.tier is PlaybackTier.SOFTWARE_TRANSCODE


async def test_skips_keyframe_probe_for_candidates_that_transcode(db, monkeypatch):
    """视频已经确定要转码时，不应为 Remux 安全门槛读取源片。"""
    calls = []
    monkeypatch.setattr(
        playback_plan,
        "probe_keyframe_interval",
        lambda path, duration: calls.append((path, duration)) or 4.0,
    )

    row = _file(1, 1, codec="hevc", resolution="2160p")
    decision = await playback_plan.decide_for_files(
        [row], CHROME, can_self_enable=True
    )

    assert isinstance(decision, ConsentRequired)
    assert calls == []


async def test_keeps_keyframe_probe_for_remux_candidates(db, monkeypatch):
    """视频复制的 Remux 候选仍需采样，未知时才能安全降级转码。"""
    calls = []
    monkeypatch.setattr(
        playback_plan,
        "probe_keyframe_interval",
        lambda path, duration: calls.append((path, duration)) or 4.0,
    )

    row = _file(1, 1, codec="h264", resolution="1080p")
    decision = await playback_plan.decide_for_files(
        [row], CHROME, can_self_enable=True
    )

    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX
    assert len(calls) == 1


async def test_view_translation_covers_three_outcomes(db):
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        item = MediaItem(kind="movie", tmdb_id=4, title="示例", original_title="Example")
        session.add(item)
        await session.flush()
        playable = _file(library.id, item.id, codec="h264", resolution="1080p")
        needs_transcode = _file(library.id, item.id, codec="hevc", resolution="1080p")
        session.add_all([playable, needs_transcode])
        await session.commit()

        plan_view = playback_plan.to_view(
            await playback_plan.decide_for_files([playable], CHROME, can_self_enable=True)
        )
        consent_view = playback_plan.to_view(
            await playback_plan.decide_for_files(
                [needs_transcode], CHROME, can_self_enable=False
            )
        )

    assert plan_view.outcome == "plan"
    assert plan_view.video.action == "copy" and plan_view.audio.action == "copy"
    assert plan_view.reason  # 中文理由是一等公民字段，诊断面板与错误提示共用

    assert consent_view.outcome == "consent"
    assert consent_view.can_self_enable is False  # 普通成员：渲染说明而非按钮
    assert consent_view.cost_hint


def test_best_prefers_higher_resolution_at_equal_tier():
    """同档位并列时取更高分辨率——直通判定放开后 1080p/2160p 经常同档。

    改动前是 `min(key=tier)`，并列取数据库返回顺序的第一个：同一部片今天播
    4K 明天播 1080p，全看行序。同档位代价相同（都不重编码），画质当然取高。
    """
    from movieclaw_api.services.playback.plan import _best
    from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan

    def plan(file_id: int) -> PlaybackPlan:
        return PlaybackPlan(
            tier=PlaybackTier.REMUX,
            file_id=file_id,
            container="hls-fmp4",
            video=VideoPlan(action="copy"),
            audio=AudioPlan(action="copy"),
        )

    # 1080p 在前、4K 在后：不看高度的话会取到先来的 1080p
    picked = _best([(plan(1), 1080), (plan(2), 2160)])
    assert picked.file_id == 2

    # 高度未知的候选排最后，结果保持确定
    picked = _best([(plan(3), None), (plan(4), 1080)])
    assert picked.file_id == 4

    # 档位仍然优先于分辨率：能直通的 1080p 胜过要转码的 4K
    transcode_4k = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=5,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264"),
        audio=AudioPlan(action="copy"),
    )
    picked = _best([(transcode_4k, 2160), (plan(6), 1080)])
    assert picked.file_id == 6
