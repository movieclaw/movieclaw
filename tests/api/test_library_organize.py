"""整理器（存量规范化）的测试。

覆盖：电影/剧集的规范目标路径计算、多版本按 Emby/Plex 约定加版本标签
（分辨率标签/大小编号兜底/幂等重跑/并入已有规范文件）、跳过规则（待识别/
缺集号/目标被无关文件占用/缺失文件不参与）、执行后的物理改名 + 附属文件
随迁 + 台账路径随迁 + 搬空目录清理、与扫描的双向互斥。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.organize as organize_mod
import movieclaw_api.services.library.scan as scan_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.organize import build_organize_plan, organize_library
from movieclaw_api.services.library.scan import scan_library
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, Library, LibraryFile, MediaItem, utcnow
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'organize.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _make_library(session, *, kind: MediaKind, root) -> Library:
    root.mkdir(parents=True, exist_ok=True)
    return await LibraryRepository(session).create(
        name=f"测试{kind.value}库", kind=kind.value, root_paths=[str(root)]
    )


async def _make_item(session, *, kind: MediaKind, tmdb_id: int, title: str, year: int) -> MediaItem:
    item = MediaItem(kind=kind.value, tmdb_id=tmdb_id, title=title, original_title=title, year=year)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


def _add_file(
    session, library, item, path, *, season=0, episode=0, missing=False, resolution=None
) -> LibraryFile:
    row = LibraryFile(
        library_id=library.id,
        media_item_id=item.id if item else None,
        season_number=season,
        episode_number=episode,
        file_path=str(path),
        size_bytes=path.stat().st_size if path.exists() else 0,
        resolution=resolution,
        source=FileSource.SCANNED,
        missing_since=utcnow() if missing else None,
        state=FileState.MISSING if missing else FileState.IN_PLACE,
    )
    session.add(row)
    return row


@pytest.mark.asyncio
async def test_movie_plan_targets_and_skips(db, tmp_path):
    """电影库：散乱文件 → 规范路径；多版本加分辨率标签；待识别/缺失各得其所。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root)
        movie = await _make_item(
            session, kind=MediaKind.MOVIE, tmdb_id=300, title="某电影", year=2020
        )
        tidy = await _make_item(
            session, kind=MediaKind.MOVIE, tmdb_id=301, title="规整电影", year=2019
        )
        dupe = await _make_item(
            session, kind=MediaKind.MOVIE, tmdb_id=302, title="双版本", year=2021
        )

        # 散乱命名 + 一个字幕附属文件 + 一个同名异容器视频（独立版本，不算附属）
        messy_dir = root / "Some.Movie.2020.1080p.BluRay.x264-GRP"
        messy_dir.mkdir(parents=True)
        messy = messy_dir / "some.movie.2020.1080p.mkv"
        messy.write_bytes(b"main")
        (messy_dir / "some.movie.2020.1080p.zh.srt").write_bytes(b"sub")
        (messy_dir / "some.movie.2020.1080p.mp4").write_bytes(b"alt")
        _add_file(session, library, movie, messy)

        # 已符合规范：不动
        tidy_dir = root / "规整电影 (2019)"
        tidy_dir.mkdir(parents=True)
        tidy_file = tidy_dir / "规整电影 (2019).mkv"
        tidy_file.write_bytes(b"ok")
        _add_file(session, library, tidy, tidy_file)

        # 同条目两个同容器版本 → 按 Emby/Plex 约定加分辨率标签落同一目录
        v1 = root / "双版本.v1.mkv"
        v2 = root / "双版本.v2.mkv"
        v1.write_bytes(b"v1")
        v2.write_bytes(b"v2")
        _add_file(session, library, dupe, v1, resolution="2160p")
        _add_file(session, library, dupe, v2, resolution="1080p")

        # 待识别 → 跳过；缺失 → 不参与
        unknown = root / "zzqx.mkv"
        unknown.write_bytes(b"?")
        _add_file(session, library, None, unknown)
        _add_file(session, library, movie, root / "gone.mkv", missing=True)
        await session.commit()

        plan = await build_organize_plan(session, library)

    assert plan.total == 5  # messy + tidy + v1 + v2 + unknown（missing 不计）
    assert plan.already_ok == 1
    targets = {a.source_path: a.target_path for a in plan.renames}
    assert targets[str(messy)] == str(root / "某电影 (2020)" / "某电影 (2020).mkv")
    # 多版本：同一条目目录内加 " - 分辨率" 标签，播放器归组为同片多版本
    assert targets[str(v1)] == str(root / "双版本 (2021)" / "双版本 (2021) - 2160p.mkv")
    assert targets[str(v2)] == str(root / "双版本 (2021)" / "双版本 (2021) - 1080p.mkv")
    # 字幕随迁、异容器视频不算附属
    action = next(a for a in plan.renames if a.source_path == str(messy))
    assert [s.target_path for s in action.sidecars] == [
        str(root / "某电影 (2020)" / "某电影 (2020).zh.srt")
    ]
    reasons = {s.file_path: s.reason for s in plan.skips}
    assert "认领" in reasons[str(unknown)]


@pytest.mark.asyncio
async def test_strm_renames_independently_not_as_sidecar(db, tmp_path):
    """strm 占位文件独立参与整理（保留 .strm 后缀），不被同名视频当附属拖走。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root)
        movie = await _make_item(
            session, kind=MediaKind.MOVIE, tmdb_id=310, title="云上电影", year=2022
        )
        messy_dir = root / "Cloud.Movie.2022.1080p"
        messy_dir.mkdir(parents=True)
        video = messy_dir / "cloud.movie.2022.mkv"
        video.write_bytes(b"v")
        # 双后缀是 strm 生成器的常见命名——正好构成"以主文件名. 开头"的附属形态
        strm = messy_dir / "cloud.movie.2022.mkv.strm"
        strm.write_text("https://cloud.example.com/m.mkv", encoding="utf-8")
        _add_file(session, library, movie, video, resolution="1080p")
        _add_file(session, library, movie, strm)
        await session.commit()

        plan = await build_organize_plan(session, library)

    targets = {a.source_path: a.target_path for a in plan.renames}
    assert targets[str(video)] == str(root / "云上电影 (2022)" / "云上电影 (2022).mkv")
    assert targets[str(strm)] == str(root / "云上电影 (2022)" / "云上电影 (2022).strm")
    video_action = next(a for a in plan.renames if a.source_path == str(video))
    assert video_action.sidecars == [], "strm 是独立版本，不该作为附属被视频拖走"


@pytest.mark.asyncio
async def test_tv_plan_targets(db, tmp_path):
    """剧集库：季集号 → Season 目录 + SxxEyy 规范名；集号缺失跳过。"""
    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=root)
        show = await _make_item(
            session, kind=MediaKind.TV, tmdb_id=200, title="测试剧集", year=2024
        )
        messy_dir = root / "乱七八糟剧" / "第二季"
        messy_dir.mkdir(parents=True)
        ep = messy_dir / "xx.s02e03.1080p.mkv"
        ep.write_bytes(b"e3")
        _add_file(session, library, show, ep, season=2, episode=3)
        noep = root / "乱七八糟剧" / "花絮.mkv"
        noep.write_bytes(b"x")
        _add_file(session, library, show, noep, season=0, episode=0)
        await session.commit()

        plan = await build_organize_plan(session, library)

    assert [a.target_path for a in plan.renames] == [
        str(root / "测试剧集 (2024)" / "Season 02" / "测试剧集 (2024) - S02E03.mkv")
    ]
    assert any("集号" in s.reason for s in plan.skips)


@pytest.mark.asyncio
async def test_organize_moves_files_and_relocates_ledger(db, tmp_path):
    """执行：物理改名 + 字幕随迁 + 台账路径随迁 + 搬空的目录被清理。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root)
        movie = await _make_item(
            session, kind=MediaKind.MOVIE, tmdb_id=300, title="某电影", year=2020
        )
        messy_dir = root / "Some.Movie.2020" / "disc1"
        messy_dir.mkdir(parents=True)
        messy = messy_dir / "some.movie.mkv"
        messy.write_bytes(b"main")
        (messy_dir / "some.movie.zh.srt").write_bytes(b"sub")
        _add_file(session, library, movie, messy)
        await session.commit()
        library_id = library.id

    summary = await organize_library(library_id)

    assert summary.errors == []
    assert summary.renamed == 1
    assert summary.sidecars_renamed == 1
    target = root / "某电影 (2020)" / "某电影 (2020).mkv"
    assert target.read_bytes() == b"main"
    assert (root / "某电影 (2020)" / "某电影 (2020).zh.srt").read_bytes() == b"sub"
    # 搬空的 disc1 与 Some.Movie.2020 两级目录都被清掉，库根保留
    assert not (root / "Some.Movie.2020").exists()
    assert summary.removed_dirs == 2
    assert root.exists()

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert [r.file_path for r in rows] == [str(target)]
    assert rows[0].missing_since is None


@pytest.mark.asyncio
async def test_organize_skips_occupied_target(db, tmp_path):
    """目标路径已被磁盘上的其他文件占用：跳过，绝不覆盖。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root)
        movie = await _make_item(
            session, kind=MediaKind.MOVIE, tmdb_id=300, title="某电影", year=2020
        )
        messy = root / "some.movie.mkv"
        messy.write_bytes(b"main")
        _add_file(session, library, movie, messy)
        occupied = root / "某电影 (2020)"
        occupied.mkdir(parents=True)
        (occupied / "某电影 (2020).mkv").write_bytes(b"already-here")
        await session.commit()

        plan = await build_organize_plan(session, library)

    assert plan.renames == []
    assert any("覆盖" in s.reason for s in plan.skips)
    assert (occupied / "某电影 (2020).mkv").read_bytes() == b"already-here"


@pytest.mark.asyncio
async def test_multi_version_fallback_labels_and_idempotent_rerun(db, tmp_path):
    """探测信息全缺的多版本按大小 V1/V2 编号；整理后重算计划全部已规范（幂等）。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root)
        dupe = await _make_item(
            session, kind=MediaKind.MOVIE, tmdb_id=302, title="双版本", year=2021
        )
        big = root / "双版本.big.mkv"
        small = root / "双版本.small.mkv"
        big.write_bytes(b"bigger-content")
        small.write_bytes(b"s")
        _add_file(session, library, dupe, big)
        _add_file(session, library, dupe, small)
        await session.commit()
        library_id = library.id

    summary = await organize_library(library_id)

    assert summary.errors == []
    assert summary.renamed == 2
    entry = root / "双版本 (2021)"
    assert (entry / "双版本 (2021) - V1.mkv").read_bytes() == b"bigger-content"
    assert (entry / "双版本 (2021) - V2.mkv").read_bytes() == b"s"

    # 幂等：标签推导确定性，重算计划不再产生任何改名
    async with db.session() as session:
        library = await session.get(Library, library_id)
        plan = await build_organize_plan(session, library)
    assert plan.renames == []
    assert plan.already_ok == 2


@pytest.mark.asyncio
async def test_extra_version_joins_canonical_file(db, tmp_path):
    """规范名被同条目的在位文件占用：新文件加版本标签并入，而不是跳过。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root)
        movie = await _make_item(
            session, kind=MediaKind.MOVIE, tmdb_id=300, title="某电影", year=2020
        )
        entry = root / "某电影 (2020)"
        entry.mkdir(parents=True)
        canonical = entry / "某电影 (2020).mkv"
        canonical.write_bytes(b"1080p-version")
        _add_file(session, library, movie, canonical, resolution="1080p")
        extra = root / "某电影.4k.mkv"
        extra.write_bytes(b"4k-version")
        _add_file(session, library, movie, extra, resolution="2160p")
        await session.commit()

        plan = await build_organize_plan(session, library)

    # 无标签的规范文件保持不动（Emby/Plex 按相同基础名归组），新版本带标签并入
    assert plan.already_ok == 1
    assert [a.target_path for a in plan.renames] == [str(entry / "某电影 (2020) - 2160p.mkv")]


@pytest.mark.asyncio
async def test_scan_and_organize_are_mutually_exclusive(db, tmp_path):
    """整理与扫描双向互斥：任一方在跑，另一方立即让路不动磁盘。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root)
        library_id = library.id

    scan_mod._scan_tasks.try_start(
        library_id, scan_mod.ScanState(phase=scan_mod.ScanPhase.INGESTING)
    )
    try:
        summary = await organize_library(library_id)
        assert any("扫描" in e for e in summary.errors)
        assert summary.renamed == 0
    finally:
        scan_mod._scan_tasks.finish(library_id)

    organize_mod._organize_tasks.try_start(library_id, (0, 0))
    try:
        scan_summary = await scan_library(library_id)
        assert any("整理" in e for e in scan_summary.errors)
        assert scan_summary.scanned == 0
    finally:
        organize_mod._organize_tasks.finish(library_id)


@pytest.mark.asyncio
async def test_organize_job_progress_is_throttled_but_conclusion_exact(db, tmp_path):
    """进度持久化按秒节流：事件数远小于文件数，但结论与终态进度必须精确。

    逐文件写进度会触发“事件 → SSE → 每个任务中心客户端拉两次列表”的
    放大链（大库整理被拖慢一个数量级的元凶之一）。节流只该丢中间观察值：
    改名结果、最终 current/total 一个都不能差。
    """
    import asyncio

    from movieclaw_api.services import jobs as jobs_svc
    from movieclaw_db.models import JobStatus

    root = tmp_path / "movies"
    file_count = 24
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root)
        for i in range(file_count):
            movie = await _make_item(
                session, kind=MediaKind.MOVIE, tmdb_id=9000 + i, title=f"影片{i}", year=2020
            )
            messy = root / f"Movie.{i}.2020.1080p" / f"movie.{i}.mkv"
            messy.parent.mkdir(parents=True)
            messy.write_bytes(b"x")
            _add_file(session, library, movie, messy)
        await session.commit()
        plan = await organize_mod.build_organize_plan(session, library)
        created = await organize_mod.enqueue_organize_job(session, library, plan)

    await jobs_svc.init_job_dispatcher(max_parallel=1)
    try:
        deadline = asyncio.get_running_loop().time() + 10.0
        while True:
            async with db.session() as session:
                row = await jobs_svc.get_job(session, created.job.id)
                assert row is not None
                if row.status is JobStatus.SUCCEEDED:
                    break
                assert row.status not in (JobStatus.FAILED, JobStatus.BLOCKED), row.error
            assert asyncio.get_running_loop().time() < deadline, "整理作业未在期限内完成"
            await asyncio.sleep(0.05)

        async with db.session() as session:
            row = await jobs_svc.get_job(session, created.job.id)
            events = await jobs_svc.job_events(session, created.job.id)
        assert row is not None and row.result is not None
        assert row.result["renamed"] == file_count
        assert row.progress["percent"] == 100.0
        progress_events = [e for e in events if e.event_type == "progress"]
        # 节流后事件数应当只与耗时（秒级）相关，而不是与文件数同阶
        assert len(progress_events) < file_count / 2
        # 终态前最后一条进度必须是精确的 total/total（最后一个文件强制落盘）
        final = [e for e in progress_events if e.payload.get("phase") == "organizing"][-1]
        assert final.payload["current"] == file_count
        assert final.payload["total"] == file_count
    finally:
        await jobs_svc.close_job_dispatcher()
