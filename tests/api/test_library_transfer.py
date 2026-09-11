"""条目转移：分错库的作品换库时，磁盘目录与台账必须一起搬到位。

覆盖用户场景「韩剧被路由进了大陆华语剧库」的完整补救路径：预览算得对、
执行后目录真的搬走了、台账改挂了新库、订阅跟着改挂、目标已有同名目录时
被拦住不覆盖。跨盘分支（复制而非改名）没法在单个 tmp_path 里造出来，
用直接调用 ``_move`` 的方式不做覆盖——它的 EXDEV 分支靠代码审阅保证。

「其他」库（本地内容库，一文件一条目）单列一节：那里没有"条目目录"这回事，
搬的是文件本身，且必须把本地身份锚一并改到新库——两条都是与影视库不同的
语义，各自有独立的回归。
"""

from __future__ import annotations

import asyncio
import errno

import pytest
import pytest_asyncio

from movieclaw_api.api.routes.libraries import (
    preview_transfer,
    transfer_library_item,
)
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.library import TransferPayload
from movieclaw_api.services import jobs
from movieclaw_api.services.library import transfer as transfer_svc
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, JobStatus, LibraryFile, MediaItem, RuleSet, Subscription
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'transfer.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    await jobs.init_job_dispatcher(max_parallel=1)
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_media_server_notify(monkeypatch):
    """转移收尾会通知下游媒体服务器刷新——测试里没配置，直接短路。"""

    async def _noop() -> None:
        return None

    monkeypatch.setattr(
        "movieclaw_api.services.media_server_notify.notify_media_server_refresh", _noop
    )


async def _drain_transfer(source_id: int, target_id: int) -> transfer_svc.TransferSummary:
    """等后台转移任务跑完并取回结论（最多等 5 秒，正常是毫秒级）。"""
    for _ in range(500):
        async with get_database().session() as session:
            latest = await jobs.latest_job_for_resource(
                session, "library", source_id, job_type="library.transfer"
            )
        if latest is not None and latest.status is JobStatus.SUCCEEDED:
            result = dict(latest.result or {})
            result.pop("message", None)
            return transfer_svc.TransferSummary(**result)
        await asyncio.sleep(0.01)
    raise AssertionError("转移作业没有在时限内留下结论")


def _make_series(root, name: str, episodes: int = 2):
    """在库根下造一个条目目录：Season 01 + 若干集 + NFO/海报/字幕。"""
    entry = root / name
    season = entry / "Season 01"
    season.mkdir(parents=True)
    (entry / "poster.jpg").write_bytes(b"poster")
    (entry / "tvshow.nfo").write_text("<tvshow/>", encoding="utf-8")
    paths = []
    for i in range(1, episodes + 1):
        video = season / f"{name} - S01E{i:02d}.mkv"
        video.write_bytes(b"x" * 100)
        (season / f"{name} - S01E{i:02d}.zh.srt").write_text("sub", encoding="utf-8")
        paths.append(video)
    return entry, paths


async def _setup(db, tmp_path, *, with_subscription: bool = False):
    """两个剧集库（源=大陆华语剧、目标=韩剧）+ 一部分错库的剧。"""
    source_root = tmp_path / "media" / "大陆"
    target_root = tmp_path / "media" / "韩剧"
    source_root.mkdir(parents=True)
    target_root.mkdir(parents=True)
    entry, videos = _make_series(source_root, "机智的医生生活 (2020)")

    async with db.session() as session:
        repo = LibraryRepository(session)
        source = await repo.create(name="大陆华语剧", kind="tv", root_paths=[str(source_root)])
        target = await repo.create(name="韩剧", kind="tv", root_paths=[str(target_root)])
        item = MediaItem(kind="tv", tmdb_id=96162, title="机智的医生生活", original_title="K")
        session.add(item)
        await session.flush()
        assert source.id and target.id and item.id
        for i, video in enumerate(videos, start=1):
            session.add(
                LibraryFile(
                    library_id=source.id,
                    media_item_id=item.id,
                    season_number=1,
                    episode_number=i,
                    file_path=str(video),
                    size_bytes=100,
                    source=FileSource.SCANNED,
                )
            )
        if with_subscription:
            rule_set = RuleSet(name="默认规则组", is_default=True, spec={})
            session.add(rule_set)
            await session.flush()
            assert rule_set.id
            session.add(
                Subscription(
                    media_item_id=item.id,
                    kind="tv",
                    library_id=source.id,
                    rule_set_id=rule_set.id,
                    status="active",
                )
            )
        await session.commit()
        return source.id, target.id, item.id, entry, target_root


async def test_preview_lists_entry_dir_and_size(db, tmp_path) -> None:
    """预览把「整个条目目录搬到目标库主根下」这件事讲清楚：一个目录单元、
    体积等于台账之和、目标路径在目标主根下、没有阻断问题。"""
    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)
    async with get_database().session() as session:
        resp = await preview_transfer(source_id, item_id, target_id, session=session)
    view = resp.data
    assert view.blocked == []
    assert len(view.moves) == 1
    move = view.moves[0]
    assert move.is_dir is True
    assert move.source_path == str(entry)
    assert move.target_path == str(target_root / entry.name)
    assert move.file_count == 2
    assert view.total_bytes == 200
    assert view.cross_device is False  # 同一个 tmp_path，必然同盘


async def test_transfer_moves_directory_and_reassigns_ledger(db, tmp_path) -> None:
    """执行后：源目录消失、目标目录含全部刮削产物、台账改挂目标库且路径随迁。"""
    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)
    async with get_database().session() as session:
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )
    summary = await _drain_transfer(source_id, target_id)

    assert summary.errors == []
    assert summary.files_relocated == 2
    assert summary.bytes_moved == 200

    moved = target_root / entry.name
    assert not entry.exists(), "源条目目录应已搬走"
    assert (moved / "poster.jpg").is_file()
    assert (moved / "tvshow.nfo").is_file()
    assert (moved / "Season 01" / "机智的医生生活 (2020) - S01E01.mkv").is_file()
    assert (moved / "Season 01" / "机智的医生生活 (2020) - S01E01.zh.srt").is_file()

    async with get_database().session() as session:
        from sqlmodel import select

        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        item = await session.get(MediaItem, item_id)
    assert {r.library_id for r in rows} == {target_id}
    assert all(r.file_path.startswith(str(moved)) for r in rows)
    assert all(r.media_item_id == item_id for r in rows), "身份锚不该被转移动过"
    # TMDB 来源的锚与库、路径都无关，转移绝不能碰它
    assert item is not None and item.external_id == "96162"


async def test_transfer_reassigns_subscription(db, tmp_path) -> None:
    """订阅一并改挂目标库——否则下一集下载完又按旧库投递，白搬一场。"""
    source_id, target_id, item_id, _entry, _root = await _setup(
        db, tmp_path, with_subscription=True
    )
    async with get_database().session() as session:
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )
    summary = await _drain_transfer(source_id, target_id)

    assert summary.subscription_moved is True
    async with get_database().session() as session:
        from sqlmodel import select

        sub = (await session.execute(select(Subscription))).scalar_one()
    assert sub.library_id == target_id


async def test_blocked_when_target_has_same_directory(db, tmp_path) -> None:
    """目标库里已有同名目录 → 判为阻断，绝不覆盖也绝不合并。"""
    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)
    (target_root / entry.name).mkdir()

    async with get_database().session() as session:
        resp = await preview_transfer(source_id, item_id, target_id, session=session)
        assert resp.data.blocked, "同名目录必须被预览标为阻断"
        assert resp.data.moves == []

        with pytest.raises(Exception) as excinfo:  # ConflictException
            await transfer_library_item(
                source_id, item_id, TransferPayload(target_library_id=target_id), session=session
            )
        assert "已存在同名目录" in excinfo.value.message  # type: ignore[attr-defined]
    assert entry.exists(), "被阻断时源目录必须原封不动"


async def test_reject_cross_kind_and_same_library(db, tmp_path) -> None:
    """跨类型（剧集→电影库）与转到自己都在校验层拒掉，不进搬运。"""
    source_id, _target_id, item_id, _entry, _root = await _setup(db, tmp_path)
    movie_root = tmp_path / "media" / "电影"
    movie_root.mkdir()
    async with get_database().session() as session:
        movie_lib = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(movie_root)]
        )
        await session.commit()
        assert movie_lib.id

        with pytest.raises(BadRequestException, match="同类型"):
            await preview_transfer(source_id, item_id, movie_lib.id, session=session)
        with pytest.raises(BadRequestException, match="无需转移"):
            await preview_transfer(source_id, item_id, source_id, session=session)


async def test_mixed_directory_falls_back_to_per_file(db, tmp_path) -> None:
    """条目目录里混着另一个条目的文件时，退化为只搬本片的文件 + 字幕，
    目录本身与别人的文件留在原地。"""
    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)
    intruder = entry / "Season 01" / "别人的片子.mkv"
    intruder.write_bytes(b"other")
    async with get_database().session() as session:
        other = MediaItem(kind="tv", tmdb_id=999, title="别人的片子", original_title="O")
        session.add(other)
        await session.flush()
        session.add(
            LibraryFile(
                library_id=source_id,
                media_item_id=other.id,
                season_number=1,
                episode_number=1,
                file_path=str(intruder),
                size_bytes=5,
                source=FileSource.SCANNED,
            )
        )
        await session.commit()

        resp = await preview_transfer(source_id, item_id, target_id, session=session)
    assert resp.data.blocked == []
    assert len(resp.data.moves) == 2, "两个视频文件各自成为一个搬运单元"
    assert all(not m.is_dir for m in resp.data.moves)
    assert any("其他条目" in s.reason for s in resp.data.skips)

    async with get_database().session() as session:
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )
    summary = await _drain_transfer(source_id, target_id)
    assert summary.errors == []
    moved_season = target_root / entry.name / "Season 01"
    assert (moved_season / "机智的医生生活 (2020) - S01E01.mkv").is_file()
    assert (moved_season / "机智的医生生活 (2020) - S01E01.zh.srt").is_file(), "字幕应跟着走"
    assert intruder.is_file(), "别人的文件必须留在原地"
    assert (entry / "poster.jpg").is_file(), "混合目录不整搬，刮削产物留在原目录"


# ---------------------------------------------------------------------------
# 「其他」库（本地内容库）：条目就是文件本身，锚随库随路径改写
# ---------------------------------------------------------------------------


async def _setup_video(db, tmp_path):
    """两个「其他」库（源=家庭录像A、目标=家庭录像B）+ 一段放错库的录像。

    源库刻意造成真实形态：``2024旅行/`` 是用户自己的分组目录，下面还躺着
    另一段互不相干的录像——本地内容库一文件一条目，这个目录不是谁的条目
    目录。目标库也有一个同名的 ``2024旅行/``（换库不换分组习惯，很常见）。
    """
    from movieclaw_api.services.library.local_identity import local_external_id
    from movieclaw_media.models import MediaKind

    source_root = tmp_path / "media" / "家庭A"
    target_root = tmp_path / "media" / "家庭B"
    group = source_root / "2024旅行"
    group.mkdir(parents=True)
    (target_root / "2024旅行").mkdir(parents=True)

    video = group / "婚礼.mp4"
    video.write_bytes(b"v" * 100)
    (group / "婚礼.nfo").write_text("<movie><title>婚礼</title></movie>", encoding="utf-8")
    (group / "婚礼.zh.srt").write_text("sub", encoding="utf-8")
    neighbour = group / "生日.mp4"
    neighbour.write_bytes(b"o" * 50)

    async with db.session() as session:
        repo = LibraryRepository(session)
        source = await repo.create(
            name="家庭录像A", kind="video", source="local", root_paths=[str(source_root)]
        )
        target = await repo.create(
            name="家庭录像B", kind="video", source="local", root_paths=[str(target_root)]
        )
        assert source.id and target.id
        items = []
        for path, title in ((video, "婚礼"), (neighbour, "生日")):
            item = MediaItem(
                kind="video",
                source="local",
                external_id=local_external_id(
                    source.id, MediaKind.VIDEO, source_root, path, None, scraped=False
                ),
                title=title,
                original_title=title,
                scrape_library_id=source.id,
            )
            session.add(item)
            await session.flush()
            assert item.id
            items.append(item)
            session.add(
                LibraryFile(
                    library_id=source.id,
                    media_item_id=item.id,
                    season_number=0,
                    episode_number=0,
                    file_path=str(path),
                    size_bytes=path.stat().st_size,
                    source=FileSource.SCANNED,
                )
            )
        await session.commit()
        return source.id, target.id, items[0].id, source_root, target_root


async def test_video_library_moves_the_file_not_the_group_dir(db, tmp_path) -> None:
    """其他库：搬的是这一个文件（连同 NFO/字幕），分组目录结构原样保留；
    目标库已有同名分组目录不是冲突——那正是它该落进去的地方。"""
    source_id, target_id, item_id, source_root, target_root = await _setup_video(db, tmp_path)

    async with get_database().session() as session:
        resp = await preview_transfer(source_id, item_id, target_id, session=session)
    assert resp.data.blocked == [], "同名分组目录不该被判成冲突"
    assert len(resp.data.moves) == 1
    move = resp.data.moves[0]
    assert move.is_dir is False
    assert move.source_path == str(source_root / "2024旅行" / "婚礼.mp4")
    assert move.target_path == str(target_root / "2024旅行" / "婚礼.mp4")

    async with get_database().session() as session:
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )
    summary = await _drain_transfer(source_id, target_id)

    assert summary.errors == []
    assert summary.files_relocated == 1
    moved_dir = target_root / "2024旅行"
    assert (moved_dir / "婚礼.mp4").is_file()
    assert (moved_dir / "婚礼.nfo").is_file(), "NFO 元数据要跟着走"
    assert (moved_dir / "婚礼.zh.srt").is_file(), "字幕要跟着走"
    assert (source_root / "2024旅行" / "生日.mp4").is_file(), "同目录里别人的条目必须留在原地"
    assert not (source_root / "2024旅行" / "婚礼.mp4").exists()


async def test_video_library_transfer_reanchors_local_identity(db, tmp_path) -> None:
    """本地锚由「库 id + 相对库根路径」派生，转移必须一并改写。

    不改的后果是源库里后来出现在同一相对路径的另一段录像会被扫描认成同一
    条目（一张卡片下面躺着两个库里两段不相干的视频），刮削归属也还指着一个
    已经不存放它的库。
    """
    from movieclaw_api.services.library.local_identity import local_external_id
    from movieclaw_media.models import MediaKind

    source_id, target_id, item_id, source_root, target_root = await _setup_video(db, tmp_path)
    async with get_database().session() as session:
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )
    summary = await _drain_transfer(source_id, target_id)
    assert summary.errors == []

    moved = target_root / "2024旅行" / "婚礼.mp4"
    async with get_database().session() as session:
        item = await session.get(MediaItem, item_id)
    assert item is not None
    assert item.external_id == local_external_id(
        target_id, MediaKind.VIDEO, target_root, moved, None, scraped=False
    ), "锚应指向目标库里的新位置"
    assert item.scrape_library_id == target_id, "刮削归属跟着条目走"
    # 源库里同一相对路径重新出现的文件，算出来的锚必须与搬走的这条不同
    assert item.external_id != local_external_id(
        source_id,
        MediaKind.VIDEO,
        source_root,
        source_root / "2024旅行" / "婚礼.mp4",
        None,
        scraped=False,
    )


async def test_transfer_carries_in_place_trashed_file(db, tmp_path) -> None:
    """原地待回收行（做种保护形态）的实体在条目目录里：必须随目录物理搬运、
    路径改写、状态保持待回收——否则搬运后行路径陈旧被清理任务收敛，新库里
    的无台账文件会被扫描重新收编（证伪版本借转移复活，回收机制的红线）。"""
    from sqlmodel import select

    from movieclaw_db.models import FileState, utcnow

    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)
    old_version = entry / "Season 01" / "旧版本.S01E01.mkv"
    old_version.write_bytes(b"o" * 10)
    async with get_database().session() as session:
        session.add(
            LibraryFile(
                library_id=source_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(old_version),
                size_bytes=10,
                source=FileSource.SCANNED,
                state=FileState.TRASHED,
                trashed_at=utcnow(),
                trash_context={"reason": "upgrade_replaced", "trigger": {}, "note": "洗版替换"},
            )
        )
        await session.commit()
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )
    summary = await _drain_transfer(source_id, target_id)

    assert summary.errors == []
    moved = target_root / entry.name / "Season 01" / "旧版本.S01E01.mkv"
    assert moved.is_file(), "原地待回收文件应随条目目录搬到目标库"
    async with get_database().session() as session:
        row = (
            await session.execute(
                select(LibraryFile).where(LibraryFile.file_path == str(moved))
            )
        ).scalar_one()
        assert row.state == FileState.TRASHED  # 路径改写但状态不被搬运复活
        assert row.library_id == target_id


# ---------------------------------------------------------------------------
# 失败分级：错因是「这一条」还是「环境」，处理方式必须不同
# ---------------------------------------------------------------------------


def test_classify_os_error_splits_per_path_from_environment() -> None:
    """errno 决定跳过还是停下：盘满/只读/掉线是环境性的，权限是这一条的。"""
    halt = transfer_svc._classify_os_error(
        OSError(errno.ENOSPC, "No space left on device"), "盘满了"
    )
    assert isinstance(halt, transfer_svc._MoveHalt)
    for code in (errno.EROFS, errno.EIO, errno.ESTALE):
        assert isinstance(
            transfer_svc._classify_os_error(OSError(code, "x"), "y"), transfer_svc._MoveHalt
        )
    for code in (errno.EACCES, errno.ENOENT, errno.EEXIST):
        assert isinstance(
            transfer_svc._classify_os_error(OSError(code, "x"), "y"), transfer_svc._MoveError
        )


async def test_disk_full_halts_the_whole_run_instead_of_skipping(db, tmp_path, monkeypatch) -> None:
    """盘满时整轮停下并退避重试，而不是把同一个错误在每个路径上重复一遍。

    没有分级的话，一次整库转移会挨个尝试剩下的几百个条目、挨个失败，跑
    几个小时、刷出几百条一样的错误，还在目标盘留下几百个半截续传文件。
    """
    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)

    attempts = 0

    def _no_space(src, dst):
        nonlocal attempts
        attempts += 1
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(transfer_svc, "_rename_no_replace_with_parent", _no_space)

    async with get_database().session() as session:
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )

    for _ in range(500):
        async with get_database().session() as session:
            latest = await jobs.latest_job_for_resource(
                session, "library", source_id, job_type="library.transfer"
            )
        if latest is not None and latest.status is JobStatus.RETRY_WAIT:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("盘满没有让作业进入自动重试，而是被当成了普通跳过")

    # 作业退避重试（不是"成功但有问题"），错误里保留可操作的中文原因
    assert latest.status is JobStatus.RETRY_WAIT
    assert "空间" in (latest.error or {}).get("message", "")
    # 目录仍在原位：停下的语义是"什么都没搬走"，不是"搬了一半"
    assert entry.exists()
    assert not (target_root / entry.name).exists()
