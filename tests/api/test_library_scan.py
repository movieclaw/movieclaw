"""存量扫描器（媒体库 L3）的端到端测试。

覆盖：NFO 优先识别、目录名解析 + TMDB 保守收敛、待识别落账（NULL 锚）、
忽略规则、增量重扫跳过、订阅联通（wanted 跳过库存已有 + prepare 库存概览）、
对账任务（missing 标记与文件回归清除）、改名归并（身份随迁/人工认领保留/
复制与多候选不误并）。TMDB 为假实现。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx
import pytest_asyncio
from fastapi import BackgroundTasks
from sqlmodel import select

import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.media_discover as discover_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services import jobs
from movieclaw_api.services.library.scan import preview_root_path_reconcile, scan_library
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_api.services.subtitle_gen import translate
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import JobResource, JobStatus, Library, LibraryFile, MediaItem, WantedItem
from movieclaw_db.models.library_file import IdentitySource, UnidentifiedCode
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"

_ROUTES = {
    "/3/tv/200": {
        "id": 200,
        "name": "测试剧集",
        "original_name": "Test Show",
        "first_air_date": "2024-01-01",
        "status": "Returning Series",
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "seasons": [{"season_number": 1}],
    },
    "/3/tv/200/season/1": {
        "name": "第 1 季",
        "air_date": "2024-01-01",
        "episodes": [
            {"episode_number": 1, "name": "E1", "air_date": "2024-01-01"},
            {"episode_number": 2, "name": "E2", "air_date": "2024-01-08"},
            {"episode_number": 3, "name": "E3", "air_date": "2024-01-15"},
        ],
    },
    "/3/tv/201": {
        "id": 201,
        "name": "另一部剧",
        "original_name": "Another Show",
        "first_air_date": "2024-06-01",
        "status": "Returning Series",
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "seasons": [{"season_number": 1}],
    },
    "/3/tv/201/season/1": {
        "name": "第 1 季",
        "air_date": "2024-06-01",
        "episodes": [
            {"episode_number": 1, "name": "E1", "air_date": "2024-06-01"},
            {"episode_number": 2, "name": "E2", "air_date": "2024-06-08"},
        ],
    },
    # 标记打错数字时会拉到的无关条目：与本地证据标题年份双双不符
    "/3/tv/999": {
        "id": 999,
        "name": "毫不相干的老剧",
        "original_name": "Totally Unrelated",
        "first_air_date": "1901-01-01",
        "status": "Ended",
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "seasons": [],
    },
    # 库类型选错时会拉到的无关电影（tv/81356 是剧集，movie/81356 是这部
    # 1938 年的德国老片）——类型冲突拦截生效的话它永远不该被请求
    "/3/movie/81356": {
        "id": 81356,
        "title": "13 Stühle",
        "original_title": "13 Stühle",
        "release_date": "1938-01-01",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    # 同一个数字在电影侧的另一部作品：脏 movie.nfo 被误采信时会拉到它
    "/3/movie/200": {
        "id": 200,
        "title": "毫不相干的电影",
        "original_title": "Unrelated Movie",
        "release_date": "2006-01-05",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    "/3/movie/300": {
        "id": 300,
        "title": "某电影",
        "original_title": "Some Movie",
        "release_date": "2020-05-01",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    # E.T. 原盘 badcase：NER 只抽出「外星人」，完整片名靠 Title (Year)
    # 惯例作备选查询词才追得回来
    "/3/movie/601": {
        "id": 601,
        "title": "E.T.外星人",
        "original_title": "E.T. the Extra-Terrestrial",
        "release_date": "1982-06-11",
        "runtime": 115,
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    # 神奇4侠 badcase：残留 NFO 指向的 3 分钟同年短片《4》——单字符标题
    # 让包含判定失效、年份恰好相同，只有时长轴能识破
    "/3/movie/888": {
        "id": 888,
        "title": "4",
        "original_title": "4",
        "release_date": "2025-02-26",
        "runtime": 3,
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    "/3/movie/617126": {
        "id": 617126,
        "title": "神奇4侠：初露锋芒",
        "original_title": "The Fantastic Four: First Steps",
        "release_date": "2025-07-23",
        "runtime": 115,
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
}

# 电影搜索的假结果（按查询词包含关系挑选；默认空结果）
_MOVIE_SEARCH = [
    {
        # 只有带上完整片名（备选查询词）才召回；「外星人」主词召回它但
        # 过不了标题门槛——正是 E.T. badcase 的真实形态
        "needle": "外星人",
        "result": {
            "id": 601,
            "title": "E.T.外星人",
            "original_title": "E.T. the Extra-Terrestrial",
            "release_date": "1982-06-11",
        },
    },
    {
        "needle": "神奇4侠",
        "result": {
            "id": 617126,
            "title": "神奇4侠：初露锋芒",
            "original_title": "The Fantastic Four: First Steps",
            "release_date": "2025-07-23",
        },
    },
    {
        "needle": "某电影",
        "result": {
            "id": 300,
            "title": "某电影",
            "original_title": "Some Movie",
            "release_date": "2020-05-01",
        },
    },
]


def _fake_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/3/search/tv":
            query = request.url.params.get("query", "")
            results = (
                [
                    {
                        "id": 200,
                        "name": "测试剧集",
                        "original_name": "Test Show",
                        "first_air_date": "2024-01-01",
                    }
                ]
                if "测试剧集" in query or "Test Show" in query
                else []
            )
            return httpx.Response(200, json={"results": results})
        if path == "/3/search/movie":
            query = request.url.params.get("query", "")
            results = [m["result"] for m in _MOVIE_SEARCH if m["needle"] in query]
            return httpx.Response(200, json={"results": results})
        payload = _ROUTES.get(path)
        return httpx.Response(200 if payload else 404, json=payload or {})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'scan.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    # 扫描器与认领路由取全局 TMDB 客户端：替换为假实现
    client = _fake_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    # 测试文件都是刚创建的，关掉"疑似写入中"静默窗口（该行为有专门测试覆盖）
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _make_tv_library(tmp_path):
    """剧集库样本：规范目录两集 + 一个认不出的文件 + @eaDir 干扰。"""
    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "测试剧集.S01E01.1080p.mkv").write_bytes(b"e1")
    (show / "测试剧集.S01E02.1080p.mkv").write_bytes(b"e2")
    junk = root / "未知内容目录" / "zzqx.mkv"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"junk")
    eadir = root / "测试剧集 (2024)" / "@eaDir" / "thumb.mkv"
    eadir.parent.mkdir(parents=True)
    eadir.write_bytes(b"thumb")
    return root


async def test_scan_identifies_by_name_and_flags_unknown(db, tmp_path) -> None:
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.scanned == 3  # 两集 + junk；@eaDir 被忽略
    assert summary.identified == 2
    assert summary.unidentified == 1

    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        identified = [f for f in files if f.unidentified_code is None]
        assert {(f.season_number, f.episode_number) for f in identified} == {(1, 1), (1, 2)}
        assert all(f.source == "scanned" for f in files)
        # 同轮首次发现的文件共享批次号，首页才能只摘要这轮新增内容。
        assert len({f.added_batch_id for f in files}) == 1
        assert files[0].added_batch_id is not None
        # 认不出的文件挂着**临时本地身份**（可见可播），但仍是待识别
        # （docs/design/library-other-kind.md 4.2）
        unknown = [f for f in files if f.unidentified_code is not None]
        assert len(unknown) == 1 and unknown[0].file_path.endswith("zzqx.mkv")
        assert unknown[0].media_item_id is not None
        provisional = await session.get(MediaItem, unknown[0].media_item_id)
        assert provisional is not None and provisional.source == "local"

    # 增量重扫：已识别的秒过；待识别的自动重试识别（TMDB 恢复的补救通道）
    summary2 = await scan_library(library.id)
    assert summary2.scanned == 0 and summary2.skipped_known == 2
    assert summary2.retried == 1 and summary2.unidentified == 1


async def test_scan_refreshes_persisted_library_stats(db, tmp_path) -> None:
    """扫描收尾落统计快照；missing 历史行不计入在位文件数和占用空间。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    await scan_library(library.id)
    async with db.session() as session:
        refreshed = await session.get(Library, library.id)
        assert refreshed is not None
        assert refreshed.stats_item_count == 1
        assert refreshed.stats_episode_count == 2
        assert refreshed.stats_file_count == 3
        assert refreshed.stats_total_size_bytes == 8
        assert refreshed.stats_unidentified_count == 1
        assert refreshed.stats_missing_count == 0
        assert refreshed.stats_ignored_count == 0
        assert refreshed.stats_refreshed_at is not None

    (root / "测试剧集 (2024)" / "Season 01" / "测试剧集.S01E01.1080p.mkv").unlink()
    await scan_library(library.id)
    async with db.session() as session:
        refreshed = await session.get(Library, library.id)
        assert refreshed is not None
        assert refreshed.stats_item_count == 1
        assert refreshed.stats_episode_count == 1
        assert refreshed.stats_file_count == 2
        assert refreshed.stats_total_size_bytes == 6
        assert refreshed.stats_unidentified_count == 1
        assert refreshed.stats_missing_count == 1


async def test_scan_persists_file_mtime(db, tmp_path) -> None:
    """扫描落 mtime（issue #88）：入账时随 stat 顺手记录 file_mtime_ns，
    播放接口的 ETag 由它派生，浏览请求不再对媒体文件本体做文件系统调用。
    特性上线前的旧行（NULL）由重扫的秒过分支一次性回填。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        assert files and all(f.file_mtime_ns is not None for f in files)
        for f in files:
            assert f.file_mtime_ns == Path(f.file_path).stat().st_mtime_ns
        # 模拟特性上线前的旧行：清掉 mtime
        for f in files:
            f.file_mtime_ns = None
        await session.commit()

    # 重扫：已识别行仍秒过，但 NULL 的 mtime 被顺手回填
    summary = await scan_library(library.id)
    assert summary.skipped_known == 2
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        identified = [f for f in files if f.unidentified_code is None]
        assert all(f.file_mtime_ns is not None for f in identified)


async def test_scan_survives_paths_ledgered_under_another_library(db, tmp_path) -> None:
    """事故回归：路径已挂在另一个库名下时，扫描不得整轮崩掉。

    配置层已经拒绝跨库根路径重叠，但历史数据/并发写入（扫描进行中监听导入
    恰好投递同一文件）仍可能让「本库快照里没有、数据库里却有」的路径出现。
    此前这会裸抛 UNIQUE 撞键，且失败的事务不回滚毒死会话，后续全部文件
    跟着失败（实测事故：480 个文件全灭、库计数归零）。现在 upsert 撞键
    自愈为原地更新，单文件失败也会回滚会话不再连坐。
    """
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        repo = LibraryRepository(session)
        # 直接走仓储建两个同根库，模拟配置校验上线前留下的历史配置
        first = await repo.create(name="综艺", kind="tv", root_paths=[str(root)])
        second = await repo.create(name="纪录片", kind="tv", root_paths=[str(root)])

    summary1 = await scan_library(first.id)
    assert summary1.scanned == 3 and summary1.errors == []

    # 第二个库扫描同一目录：快照为空，每条路径都会撞上第一个库的台账行
    summary2 = await scan_library(second.id)
    assert summary2.errors == []  # 不再撞键，更不再连坐全灭
    assert summary2.scanned == 3 and summary2.identified == 2

    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len(files) == 3  # 原地更新，没有产生重复行
        assert {f.library_id for f in files} == {second.id}  # 行转归后扫的库


async def test_rescan_after_root_alias_change_relocates_same_file_ledger(db, tmp_path) -> None:
    """编辑根路径为同目录的软链接入口，重扫只迁移原台账行，不重复入账。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.mkv").write_bytes(b"movie")
    (entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    alias = tmp_path / "mounted-movies"
    alias.symlink_to(root, target_is_directory=True)

    async with db.session() as session:
        repo = LibraryRepository(session)
        library = await repo.create(name="电影库", kind="movie", root_paths=[str(root)])
    await scan_library(library.id)

    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        old_id = old.id
        old_item_id = old.media_item_id
        await LibraryRepository(session).update(library.id, name="电影库", root_paths=[str(alias)])

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(root)],
    )
    assert summary.scanned == 0
    assert summary.root_relinked == 1
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 1
    assert rows[0].id == old_id
    assert rows[0].media_item_id == old_item_id
    assert rows[0].file_path == str(alias / "某电影 (2020)" / "某电影.2020.mkv")


async def test_rescan_merges_duplicate_rows_left_by_root_alias_change(db, tmp_path) -> None:
    """已经由旧版本产生的同实体重复台账，下一次扫描自动收敛为一行。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.mkv").write_bytes(b"movie")
    (entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    alias = tmp_path / "mounted-movies"
    alias.symlink_to(root, target_is_directory=True)

    async with db.session() as session:
        repo = LibraryRepository(session)
        library = await repo.create(name="电影库", kind="movie", root_paths=[str(root)])
    await scan_library(library.id)

    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        old.identity_source = IdentitySource.MANUAL
        old.audio_streams = [{"codec": "truehd"}]
        await session.commit()
        old_id = old.id
        item_id = old.media_item_id
        # 模拟修复上线前第一次改根重扫已经写出的第二行：旧路径行与当前
        # 根路径行并存，且二者实际指向同一文件。
        duplicate = LibraryFile(
            library_id=library.id,
            media_item_id=item_id,
            season_number=0,
            episode_number=0,
            file_path=str(alias / "某电影 (2020)" / "某电影.2020.mkv"),
            size_bytes=5,
            source="scanned",
        )
        session.add(duplicate)
        await session.commit()
        await LibraryRepository(session).update(library.id, name="电影库", root_paths=[str(alias)])

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(root)],
    )
    assert summary.root_relinked == 1
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 1
    assert rows[0].id == old_id  # 保留原台账主键，Jellyfin 来源 ID 不变
    assert rows[0].file_path == str(alias / "某电影 (2020)" / "某电影.2020.mkv")
    assert rows[0].identity_source == IdentitySource.MANUAL
    assert rows[0].audio_streams == [{"codec": "truehd"}]


async def test_rescan_after_adding_root_alias_keeps_single_ledger_row(db, tmp_path) -> None:
    """保留原根同时新增同目录别名，两个遍历入口仍只复用一条台账。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.mkv").write_bytes(b"movie")
    (entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    alias = tmp_path / "mounted-movies"
    alias.symlink_to(root, target_is_directory=True)

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        old_id = old.id
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(root), str(alias)]
        )

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(root)],
    )
    assert summary.scanned == 0
    assert summary.root_relinked == 1
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 1
    assert rows[0].id == old_id
    assert rows[0].file_path == str(entry / "某电影.2020.mkv")


async def test_adding_root_does_not_merge_matching_fingerprint_on_another_device(
    db, tmp_path, monkeypatch
) -> None:
    """保留原根时，不能把另一设备上巧合同指纹的文件当作别名。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    original = entry / "某电影.2020.mkv"
    original.write_bytes(b"movie")
    (entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    other_root = tmp_path / "other-mount"
    other_entry = other_root / "某电影 (2020)"
    other_entry.mkdir(parents=True)
    other_file = other_entry / "某电影.2020.mkv"
    shutil.copy2(original, other_file)
    os.utime(other_file, ns=(original.stat().st_atime_ns, original.stat().st_mtime_ns))
    (other_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    real_stat = Path.stat

    def cross_device_stat(path: Path, *args, **kwargs):  # noqa: ANN002, ANN003
        stat = real_stat(path, *args, **kwargs)
        if path == other_file:
            # 临时目录通常都在同一文件系统，无法真实挂两块盘；只改 st_dev
            # 来覆盖「保留旧根 + 新增另一设备根」的安全边界。委托原结果而
            # 非手写精简对象，Path.is_dir() 等 pathlib 内部调用仍需要 st_mode。
            class CrossDeviceStat:
                st_dev = stat.st_dev + 1

                def __getattr__(self, name: str):
                    return getattr(stat, name)

            return CrossDeviceStat()
        return stat

    monkeypatch.setattr(Path, "stat", cross_device_stat)

    async with db.session() as session:
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(root), str(other_root)]
        )

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(root)],
    )
    assert summary.root_relinked == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 2


async def test_adding_root_alias_merges_historical_duplicate_without_reinserting_old_path(
    db, tmp_path
) -> None:
    """别名根已有历史重复行时，合并后原根与别名遍历均复用同一行。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.mkv").write_bytes(b"movie")
    (entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    alias = tmp_path / "mounted-movies"
    alias.symlink_to(root, target_is_directory=True)

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        old_id = old.id
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=old.media_item_id,
                file_path=str(alias / "某电影 (2020)" / "某电影.2020.mkv"),
                size_bytes=5,
                source="scanned",
            )
        )
        await session.commit()
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(root), str(alias)]
        )

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(root)],
    )
    assert summary.scanned == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 1
    assert rows[0].id == old_id
    assert rows[0].file_path == str(entry / "某电影.2020.mkv")


async def test_rescan_relinks_when_old_root_is_unavailable(db, tmp_path) -> None:
    """旧挂载点已撤掉时，以同相对路径的尺寸与 mtime 指纹延续台账。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "某电影.2020.mkv"
    old_file.write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)]
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    new_entry = new_root / "某电影 (2020)"
    new_entry.mkdir(parents=True)
    new_file = new_entry / "某电影.2020.mkv"
    shutil.copy2(old_file, new_file)
    (new_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    shutil.rmtree(old_root)

    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        old_id = old.id
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.scanned == 0
    assert summary.root_relinked == 1
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 1
    assert rows[0].id == old_id
    assert rows[0].file_path == str(new_file)


async def test_removed_root_merges_historical_duplicate_despite_mtime_mismatch(
    db, tmp_path
) -> None:
    """旧挂载点消失后，历史重复行可按完整身份锚收敛，不再依赖旧 mtime。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "某电影.2020.mkv"
    old_file.write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)]
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    new_entry = new_root / "某电影 (2020)"
    new_entry.mkdir(parents=True)
    new_file = new_entry / "某电影.2020.mkv"
    shutil.copy2(old_file, new_file)
    (new_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")

    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        old.file_mtime_ns = None  # 模拟旧版本未落 mtime 的遗留台账
        duplicate = LibraryFile(
            library_id=library.id,
            media_item_id=old.media_item_id,
            season_number=0,
            episode_number=0,
            file_path=str(new_file),
            size_bytes=old.size_bytes,
            source="scanned",
        )
        session.add(duplicate)
        await session.commit()
        new_id = duplicate.id
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )
    shutil.rmtree(old_root)

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.root_relinked == 1
    assert summary.removed_root_marked_missing == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 1
    assert rows[0].id == new_id  # 无运行任务时优先保留当前新路径台账
    assert rows[0].file_path == str(new_file)


async def test_removed_root_reconcile_preview_is_read_only_and_reports_safe_merge(
    db, tmp_path
) -> None:
    """历史修复预览只读，并按相对路径和身份锚给出实际可合并数量。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "某电影.2020.mkv"
    old_file.write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)]
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    new_entry = new_root / "某电影 (2020)"
    new_entry.mkdir(parents=True)
    new_file = new_entry / "某电影.2020.mkv"
    new_file.write_bytes(b"movie")
    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=old.media_item_id,
                season_number=old.season_number,
                episode_number=old.episode_number,
                file_path=str(new_file),
                size_bytes=old.size_bytes,
                source="scanned",
            )
        )
        await session.commit()
    shutil.rmtree(old_root)

    async with db.session() as session:
        current_library = await session.get(Library, library.id)
        assert current_library is not None
        preview = await preview_root_path_reconcile(
            session,
            current_library,
            old_root=str(old_root),
            new_root=str(new_root),
        )
        rows = list((await session.execute(select(LibraryFile))).scalars().all())

    assert preview.same_path_candidates == 1
    assert preview.safe_merges == 1
    assert preview.marked_missing == 0
    assert preview.old_rows_to_delete_from_ledger == 1
    assert preview.disk_files_to_delete == 0
    assert len(rows) == 2


async def test_removed_root_conflicting_duplicate_is_not_merged_or_hidden(db, tmp_path) -> None:
    """同相对路径但身份冲突时保留两行，并把问题写进扫描结论供人工处理。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "某电影.2020.mkv"
    old_file.write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)]
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    new_entry = new_root / "某电影 (2020)"
    new_entry.mkdir(parents=True)
    new_file = new_entry / "某电影.2020.mkv"
    shutil.copy2(old_file, new_file)
    (new_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        other = MediaItem(kind="movie", tmdb_id=301, title="另一部电影", original_title="Other")
        session.add(other)
        await session.flush()
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=other.id,
                season_number=0,
                episode_number=0,
                file_path=str(new_file),
                size_bytes=old.size_bytes,
                source="scanned",
            )
        )
        await session.commit()
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )
    shutil.rmtree(old_root)

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.removed_root_conflicts == 1
    assert any("身份冲突" in error for error in summary.errors)
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 2
    assert all(row.missing_since is None for row in rows)


async def test_removed_root_without_new_file_marks_missing_and_respects_auto_clear(
    db, tmp_path
) -> None:
    """旧根已消失且无对应新文件时仅收口台账；可信扫描且开关开启才自动删除。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    (old_entry / "某电影.2020.mkv").write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)], auto_clear_missing=True
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    # 新根必须实际扫到其他文件，才可证明它不是空挂载点。
    other = new_root / "另一部电影 (2020)"
    other.mkdir(parents=True)
    (other / "另一部电影.2020.mkv").write_bytes(b"a distinct movie payload")
    (other / "movie.nfo").write_text("<movie><tmdbid>301</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )
    shutil.rmtree(old_root)

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.removed_root_marked_missing == 1
    assert summary.removed_root_cleared == 1
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert {row.file_path for row in rows} == {str(other / "另一部电影.2020.mkv")}


async def test_removed_root_without_auto_clear_keeps_missing_ledger(db, tmp_path) -> None:
    """默认关闭自动清理时，已移除根只标记缺失，旧台账必须继续保留。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "某电影.2020.mkv"
    old_file.write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)]
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    other = new_root / "另一部电影 (2020)"
    other.mkdir(parents=True)
    other_file = other / "另一部电影.2020.mkv"
    other_file.write_bytes(b"a distinct movie payload")
    (other / "movie.nfo").write_text("<movie><tmdbid>301</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )
    shutil.rmtree(old_root)

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.removed_root_marked_missing == 1
    assert summary.removed_root_cleared == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert {row.file_path for row in rows} == {str(old_file), str(other_file)}
    old_row = next(row for row in rows if row.file_path == str(old_file))
    assert old_row.missing_since is not None


async def test_removed_root_unreadable_scan_never_auto_clears_ledger(
    db, tmp_path, monkeypatch
) -> None:
    """扫描出现不可读目录时，旧根台账即使可清理也只能标记缺失。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "某电影.2020.mkv"
    old_file.write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)], auto_clear_missing=True
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    other = new_root / "另一部电影 (2020)"
    other.mkdir(parents=True)
    other_file = other / "另一部电影.2020.mkv"
    other_file.write_bytes(b"a distinct movie payload")
    (other / "movie.nfo").write_text("<movie><tmdbid>301</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )
    shutil.rmtree(old_root)

    original_walk = scan_mod._walk_videos

    def unreadable_walk(walk_root, unreadable=None, dir_files=None, **kwargs):
        yield from original_walk(walk_root, unreadable, dir_files, **kwargs)
        if unreadable is not None:
            unreadable.append(str(walk_root / "temporarily-unreadable"))

    monkeypatch.setattr(scan_mod, "_walk_videos", unreadable_walk)
    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.removed_root_marked_missing == 1
    assert summary.removed_root_cleared == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert {row.file_path for row in rows} == {str(old_file), str(other_file)}
    old_row = next(row for row in rows if row.file_path == str(old_file))
    assert old_row.missing_since is not None


async def test_removed_root_empty_new_root_marks_missing_but_does_not_auto_clear(
    db, tmp_path
) -> None:
    """新根为空时沿用空挂载保护：旧根台账可标缺失，绝不自动删除。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    (old_entry / "某电影.2020.mkv").write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)], auto_clear_missing=True
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    new_root.mkdir()
    async with db.session() as session:
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )
    shutil.rmtree(old_root)

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.removed_root_marked_missing == 1
    assert summary.removed_root_cleared == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 1
    assert rows[0].missing_since is not None


async def test_removed_root_unconfirmed_new_row_is_cleaned_when_safe(db, tmp_path) -> None:
    """新根已有同相对路径但旧行身份未知时，保留新行并按开关清理旧台账。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "未知内容"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "mystery.mkv"
    old_file.write_bytes(b"movie")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)], auto_clear_missing=True
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    new_entry = new_root / "未知内容"
    new_entry.mkdir(parents=True)
    new_file = new_entry / "mystery.mkv"
    # 尺寸不同，不能被普通「改名归并」抢先处理，必须走已移除根收口分支。
    new_file.write_bytes(b"a newer, different media payload")
    (new_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        old.file_mtime_ns = None  # 阻止严格指纹迁移，进入本次收口分支
        await session.commit()
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )
    shutil.rmtree(old_root)

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.removed_root_marked_missing == 1
    assert summary.removed_root_cleared == 1
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 1
    assert rows[0].file_path == str(new_file)
    assert rows[0].media_item_id is not None


async def test_removed_root_still_accessible_is_not_marked_missing(db, tmp_path) -> None:
    """旧根仍可读时不触发收口，避免临时保留独立目录被误判为已移除挂载。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "某电影.2020.mkv"
    old_file.write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)]
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    new_entry = new_root / "某电影 (2020)"
    new_entry.mkdir(parents=True)
    (new_entry / "某电影.2020.mkv").write_bytes(b"a distinct replacement payload")
    (new_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.removed_root_marked_missing == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 2
    assert all(row.missing_since is None for row in rows)


async def test_regular_scan_does_not_reconcile_removed_root_ledger(db, tmp_path) -> None:
    """普通手动扫描绝不触碰已退出配置的旧根台账，避免跨历史路径误收口。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "某电影.2020.mkv"
    old_file.write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)]
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    new_entry = new_root / "某电影 (2020)"
    new_entry.mkdir(parents=True)
    (new_entry / "某电影.2020.mkv").write_bytes(b"a distinct replacement payload")
    (new_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )
    shutil.rmtree(old_root)

    summary = await scan_library(library.id)
    assert summary.removed_root_marked_missing == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 2
    assert all(row.missing_since is None for row in rows)


async def test_rescan_does_not_merge_same_fingerprint_different_inode(db, tmp_path) -> None:
    """旧入口仍可访问且 inode 已不同，不能被尺寸/mtime 指纹误合并。"""
    old_root = tmp_path / "old-mount"
    old_entry = old_root / "某电影 (2020)"
    old_entry.mkdir(parents=True)
    old_file = old_entry / "某电影.2020.mkv"
    old_file.write_bytes(b"movie")
    (old_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(old_root)]
        )
    await scan_library(library.id)

    new_root = tmp_path / "new-mount"
    new_entry = new_root / "某电影 (2020)"
    new_entry.mkdir(parents=True)
    new_file = new_entry / "某电影.2020.mkv"
    shutil.copy2(old_file, new_file)
    os.utime(new_file, ns=(old_file.stat().st_atime_ns, old_file.stat().st_mtime_ns))
    (new_entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")

    async with db.session() as session:
        await LibraryRepository(session).update(
            library.id, name="电影库", root_paths=[str(new_root)]
        )

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(old_root)],
    )
    assert summary.root_relinked == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 2


async def test_reconcile_duplicate_clears_idle_subtitle_job_state(
    db, tmp_path, monkeypatch
) -> None:
    """合并重复行时迁移字幕历史与重试输入，并清理被删行的旧断点。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.mkv").write_bytes(b"movie")
    (entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    alias = tmp_path / "mounted-movies"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        assert old.id is not None
        old_id = old.id
        duplicate = LibraryFile(
            library_id=library.id,
            media_item_id=old.media_item_id,
            file_path=str(alias / "某电影 (2020)" / "某电影.2020.mkv"),
            size_bytes=5,
            source="scanned",
        )
        session.add(duplicate)
        await session.commit()
        assert duplicate.id is not None
        duplicate_id = duplicate.id
        created = await jobs.create_job(
            session,
            job_type="subtitle.generate",
            subject="历史字幕任务",
            input_data={"file_id": duplicate_id, "target_language": "chs"},
            resources=[jobs.ResourceRef("library_file", duplicate_id)],
            dedupe_key=f"subtitle.generate:{duplicate_id}:chs",
        )
        created.job.status = JobStatus.FAILED
        await session.commit()
        job_id = created.job.id
        await LibraryRepository(session).update(library.id, name="电影库", root_paths=[str(alias)])

    checkpoint = translate.Checkpoint(duplicate_id, "chs", "test")
    checkpoint.save()

    await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(root)],
    )
    assert not checkpoint.path.exists()
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        job = await jobs.get_job(session, job_id)
        resource = (
            await session.execute(select(JobResource).where(JobResource.job_id == job_id))
        ).scalar_one()
    assert len(rows) == 1
    assert rows[0].id == old_id
    assert job is not None and job.input_data["file_id"] == old_id
    assert resource.resource_id == str(old_id)


async def test_reconcile_duplicate_preserves_running_subtitle_task(db, tmp_path) -> None:
    """重复行有关联的活跃持久字幕作业时保留它的 id。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.mkv").write_bytes(b"movie")
    (entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    alias = tmp_path / "mounted-movies"
    alias.symlink_to(root, target_is_directory=True)

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        duplicate = LibraryFile(
            library_id=library.id,
            media_item_id=old.media_item_id,
            file_path=str(alias / "某电影 (2020)" / "某电影.2020.mkv"),
            size_bytes=5,
            source="scanned",
        )
        session.add(duplicate)
        await session.commit()
        assert duplicate.id is not None
        duplicate_id = duplicate.id
        created = await jobs.create_job(
            session,
            job_type="subtitle.generate",
            subject="运行中的字幕任务",
            input_data={"file_id": duplicate_id, "target_language": "chs"},
            resources=[jobs.ResourceRef("library_file", duplicate_id)],
            dedupe_key=f"subtitle.generate:{duplicate_id}:chs",
        )
        await LibraryRepository(session).update(library.id, name="电影库", root_paths=[str(alias)])

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(root)],
    )
    assert summary.errors == []
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        job = await jobs.get_job(session, created.job.id)
    assert len(rows) == 1
    assert rows[0].id == duplicate_id
    assert job is not None and job.status == JobStatus.QUEUED


async def test_reconcile_duplicate_waits_when_both_subtitle_tasks_are_running(db, tmp_path) -> None:
    """两条重复台账各有活跃持久作业时，不删任一行，留待任务结束后合并。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.mkv").write_bytes(b"movie")
    (entry / "movie.nfo").write_text("<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8")
    alias = tmp_path / "mounted-movies"
    alias.symlink_to(root, target_is_directory=True)

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        old = (await session.execute(select(LibraryFile))).scalar_one()
        duplicate = LibraryFile(
            library_id=library.id,
            media_item_id=old.media_item_id,
            file_path=str(alias / "某电影 (2020)" / "某电影.2020.mkv"),
            size_bytes=5,
            source="scanned",
        )
        session.add(duplicate)
        await session.commit()
        assert old.id is not None and duplicate.id is not None
        for file_id in (old.id, duplicate.id):
            await jobs.create_job(
                session,
                job_type="subtitle.generate",
                subject=f"字幕任务 {file_id}",
                input_data={"file_id": file_id, "target_language": "chs"},
                resources=[jobs.ResourceRef("library_file", file_id)],
                dedupe_key=f"subtitle.generate:{file_id}:chs",
            )
        await LibraryRepository(session).update(library.id, name="电影库", root_paths=[str(alias)])

    summary = await scan_library(
        library.id,
        reconcile_root_change=True,
        previous_root_paths=[str(root)],
    )
    assert any("两个字幕任务" in error for error in summary.errors)
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(rows) == 2


async def test_scan_prefers_nfo_identity(db, tmp_path) -> None:
    """电影库：文件名认不出，但目录里的 movie.nfo 带 tmdbid → 精确识别。"""
    root = tmp_path / "media" / "movies"
    folder = root / "乱七八糟的目录名"
    folder.mkdir(parents=True)
    (folder / "abcxyz.mkv").write_bytes(b"movie")
    (folder / "movie.nfo").write_text(
        "<movie><title>某电影</title><tmdbid>300</tmdbid></movie>", encoding="utf-8"
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1 and summary.unidentified == 0

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id is not None
        assert (row.season_number, row.episode_number) == (0, 0)


async def test_scan_ingests_strm_placeholders(db, tmp_path, monkeypatch) -> None:
    """strm 占位文件（网盘场景）与普通视频同权入库：按文件名识别、规格留空。

    覆盖两种常见生成器命名：纯 strm 与保留原容器后缀的双后缀（foo.mkv.strm）。
    strm 本体是一行 URL 的文本，ffprobe 探测必须整个跳过——包括入账时的
    首探与后续扫描的补探阶段。
    """
    probed: list[str] = []
    monkeypatch.setattr(scan_mod, "probe_media", lambda path, *_a, **_k: probed.append(str(path)))

    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "测试剧集.S01E01.1080p.strm").write_text(
        "https://cloud.example.com/e1.mkv", encoding="utf-8"
    )
    (show / "测试剧集.S01E02.1080p.mkv.strm").write_text(
        "https://cloud.example.com/e2.mkv", encoding="utf-8"
    )
    (show / "测试剧集.S01E03.1080p.mkv").write_bytes(b"e3")  # 混排的真视频照常探测
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="网盘剧集库", kind="tv", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.scanned == 3 and summary.identified == 3
    assert probed == [str(show / "测试剧集.S01E03.1080p.mkv")], "strm 不该被 ffprobe 摸到"

    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        assert {(f.season_number, f.episode_number) for f in files} == {(1, 1), (1, 2), (1, 3)}
        strm_rows = [f for f in files if f.file_path.endswith(".strm")]
        assert len(strm_rows) == 2
        for row in strm_rows:
            assert row.container == "strm"
            assert row.resolution is None and row.audio_streams is None

    # 增量重扫：strm 行与普通行同样秒过，补探阶段也不再回头摸 strm
    probed.clear()
    summary2 = await scan_library(library.id)
    assert summary2.skipped_known == 3
    assert probed == []


async def test_tv_files_in_movie_library_refuse_to_identify(db, tmp_path) -> None:
    """事故回归：剧集文件落在**电影库**里，绝不能按电影建档。

    TMDB 的 movie 与 tv 是两套独立 id 空间——tv/81356 是《性爱自修室》，
    movie/81356 是 1938 年的德国片《13 Stühle》。库类型选错时，一个完全
    正确的 [tmdbid=81356] 标记会被按电影拉档并"成功"，整部剧静默挂到一部
    毫不相干的老电影上（实测事故：整个库 26 部剧全军覆没）。识别不出来能
    进待识别清单被看见，识别成另一部作品则完全静默——必须在建档前拦下。
    """
    root = tmp_path / "media" / "movies"
    show = root / "性爱自修室 (2019) [tmdbid=81356]" / "Season 3"
    show.mkdir(parents=True)
    (show / "性爱自修室 S03E01 - 1080p x265 10bit.mkv").write_bytes(b"e1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="欧美剧", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 0
    assert summary.unidentified == 1 and summary.kind_mismatched == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id is None  # 建档没有发生
        assert row.unidentified_code == UnidentifiedCode.KIND_MISMATCH
        # movie/81356 那部无关老片压根不该被建出来
        assert (await session.execute(select(MediaItem))).scalars().all() == []


async def test_nfo_root_tag_conflicting_with_library_kind(db, tmp_path) -> None:
    """NFO 根元素是刮削器写下的明确类型声明：<tvshow> 出现在电影库里，
    哪怕文件名毫无季集号，也判类型冲突（放错库了）。"""
    root = tmp_path / "media" / "movies"
    folder = root / "乱七八糟的目录名"
    folder.mkdir(parents=True)
    (folder / "abcxyz.mkv").write_bytes(b"movie")
    (folder / "tvshow.nfo").write_text(
        "<tvshow><title>某剧</title><tmdbid>300</tmdbid></tvshow>", encoding="utf-8"
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 0 and summary.kind_mismatched == 1
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.unidentified_code == UnidentifiedCode.KIND_MISMATCH


async def test_same_dir_dual_nfo_prefers_library_kind(db, tmp_path) -> None:
    """同一目录里 tvshow.nfo 与 movie.nfo 并存时，取与本库类型一致的那份。

    实测事故：一次库类型选错的扫描往 27 个剧集目录写进了 movie.nfo，库改回
    剧集后 movie.nfo 排在查找顺序前面，444 个文件全被自己写的 NFO 判成
    "放错库了"。两份类型声明自相矛盾，按固定顺序取谁都是瞎猜；按库类型取
    至少不会凭空把整库判死，而**只有一份声明时的冲突检测照常成立**
    （见 test_nfo_root_tag_conflicting_with_library_kind）。
    """
    root = tmp_path / "media" / "tv"
    entry = root / "测试剧集 (2024)"
    season = entry / "Season 01"
    season.mkdir(parents=True)
    (season / "测试剧集.S01E01.1080p.mkv").write_bytes(b"e1")
    (entry / "tvshow.nfo").write_text(
        "<tvshow><title>测试剧集</title><tmdbid>200</tmdbid></tvshow>", encoding="utf-8"
    )
    # 历史遗留的脏 NFO：库类型曾被选成「电影」，扫描把电影身份写回了磁盘
    (entry / "movie.nfo").write_text(
        "<movie><title>毫不相干的电影</title><tmdbid>200</tmdbid></movie>", encoding="utf-8"
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="欧美剧", kind="tv", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1 and summary.kind_mismatched == 0

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id is not None
        item = await session.get(MediaItem, row.media_item_id)
        assert (item.kind, item.tmdb_id) == (MediaKind.TV.value, 200)


async def test_pinned_id_contradicted_by_local_evidence_falls_back(db, tmp_path) -> None:
    """标记里的数字打错了：tmdbid=999 拉到的是 1901 年的《毫不相干的老剧》，
    与本地《测试剧集》(2024) 标题年份双双不符 → 不采信声明，降级走名称
    解析（它要过完整证据验证，此刻比一个对不上号的数字更可信）。"""
    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "测试剧集.S01E01 [tmdbid=999].mkv").write_bytes(b"e1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item = await session.get(MediaItem, row.media_item_id)
        assert item is not None and item.tmdb_id == 200  # 名称解析的结论胜出
        assert row.identity_source == IdentitySource.RESOLVED


async def test_pinned_id_survives_title_mismatch_when_year_agrees(db, tmp_path) -> None:
    """不误伤：手写 tmdbid 标记的**主要用途**就是"机器认不出来时我告诉你"
    ——拼音名/意译名目录的标题必然对不上 TMDB。只要年份吻合就照样采信，
    绝不能因为标题不同就推翻用户的显式声明。"""
    root = tmp_path / "media" / "tv"
    show = root / "Qiang Qiang Shi Yi (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "QQSY.S01E01 [tmdbid=201].mkv").write_bytes(b"e1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    await scan_library(library.id)
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item = await session.get(MediaItem, row.media_item_id)
        assert item is not None and item.tmdb_id == 201  # 《另一部剧》2024，年份吻合
        assert row.identity_source == IdentitySource.PATH_TAG


async def test_identity_review_lifecycle(db, tmp_path, monkeypatch) -> None:
    """身份对账全链路：入账盖版本戳 → 识别器升级后扫描自动复核 →
    结论一致只更新戳、不一致进复核清单（身份不动）→ 用户拍板后转人工、
    永不再打扰。"""
    from movieclaw_api.api.routes.libraries import (
        list_identity_review,
        resolve_identity_review,
    )
    from movieclaw_api.schemas.library import ReviewResolvePayload

    root = tmp_path / "media" / "tv"
    show = root / "某剧 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "某剧.S01E01.mkv").write_bytes(b"e1")
    (show / "某剧.S01E02.mkv").write_bytes(b"e2")
    nfo = show.parent / "tvshow.nfo"
    nfo.write_text("<tvshow><tmdbid>200</tmdbid></tvshow>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    # ① 首扫：NFO 钉死 200，身份来源与版本戳落账
    await scan_library(library.id)
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert all(r.identity_source == IdentitySource.NFO for r in rows)
        assert all(r.resolved_version == scan_mod.RESOLVER_VERSION for r in rows)

    # ② 识别器升级、结论没变：复核后只更新版本戳，不产生建议
    monkeypatch.setattr(scan_mod, "RESOLVER_VERSION", scan_mod.RESOLVER_VERSION + 1)
    summary = await scan_library(library.id)
    assert summary.reviewed == 2 and summary.review_flagged == 0
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert all(r.resolved_version == scan_mod.RESOLVER_VERSION for r in rows)
        assert all(r.review_suggestion is None for r in rows)

    # ③ 识别器再升级、结论变了（NFO 修正为 201）：进复核清单，身份不动
    nfo.write_text("<tvshow><tmdbid>201</tmdbid></tvshow>", encoding="utf-8")
    monkeypatch.setattr(scan_mod, "RESOLVER_VERSION", scan_mod.RESOLVER_VERSION + 1)
    summary = await scan_library(library.id)
    assert summary.reviewed == 2 and summary.review_flagged == 2
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        old_item = await session.get(MediaItem, rows[0].media_item_id)
        assert old_item is not None and old_item.tmdb_id == 200  # 身份未被翻案
        assert all((r.review_suggestion or {}).get("tmdb_id") == 201 for r in rows)

    # ④ 建议未决期间再扫：版本已齐平，不重复复核也不冲掉建议
    summary = await scan_library(library.id)
    assert summary.reviewed == 0 and summary.skipped_known == 2
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert all(r.review_suggestion is not None for r in rows)

        # ⑤ 复核清单：同目录同分歧聚成一组
        groups = (await list_identity_review(None, session)).data
        assert len(groups) == 1
        group = groups[0]
        assert group.file_count == 2
        assert group.current.tmdb_id == 200 and group.suggestion.tmdb_id == 201

        # ⑥ 采纳建议：改挂新条目并转人工
        await resolve_identity_review(
            ReviewResolvePayload(file_ids=group.file_ids, decision="accept_suggestion"),
            BackgroundTasks(),
            session,
        )
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        new_item = await session.get(MediaItem, rows[0].media_item_id)
        assert new_item is not None and new_item.tmdb_id == 201
        assert all(r.identity_source == IdentitySource.MANUAL for r in rows)
        assert all(r.review_suggestion is None for r in rows)

    # ⑦ 人工身份从此免疫：识别器再升级也不复核
    monkeypatch.setattr(scan_mod, "RESOLVER_VERSION", scan_mod.RESOLVER_VERSION + 1)
    summary = await scan_library(library.id)
    assert summary.reviewed == 0 and summary.skipped_known == 2


async def test_identity_review_reject_keeps_identity(db, tmp_path, monkeypatch) -> None:
    """复核拍板选「维持现状」：身份不变、建议清除、转人工不再打扰。"""
    from movieclaw_api.api.routes.libraries import resolve_identity_review
    from movieclaw_api.schemas.library import ReviewResolvePayload

    root = tmp_path / "media" / "tv"
    show = root / "某剧 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "某剧.S01E01.mkv").write_bytes(b"e1")
    nfo = show.parent / "tvshow.nfo"
    nfo.write_text("<tvshow><tmdbid>200</tmdbid></tvshow>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    nfo.write_text("<tvshow><tmdbid>201</tmdbid></tvshow>", encoding="utf-8")
    monkeypatch.setattr(scan_mod, "RESOLVER_VERSION", scan_mod.RESOLVER_VERSION + 1)
    summary = await scan_library(library.id)
    assert summary.review_flagged == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        await resolve_identity_review(
            ReviewResolvePayload(file_ids=[row.id], decision="keep_current"),
            BackgroundTasks(),
            session,
        )
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item = await session.get(MediaItem, row.media_item_id)
        assert item is not None and item.tmdb_id == 200  # 身份未动
        assert row.identity_source == IdentitySource.MANUAL
        assert row.review_suggestion is None


def test_nfo_tmdbid_ignores_actor_person_ids(tmp_path) -> None:
    """Emby 刮削的 tvshow.nfo 里 <actor> 块自带演员的 <tmdbid>（person id），
    且排在剧集级 id 之前——必须剔除演员块，否则第一个演员的 person id
    会被误当条目身份（实测：潘粤明 138734 → 误配成同 id 的 1993 年动画）。"""
    root = tmp_path / "tv"
    show = root / "黑夜告白 (2026)" / "Season 1"
    show.mkdir(parents=True)
    file = show / "黑夜告白 S01E01.mkv"
    file.write_bytes(b"ep")
    (show.parent / "tvshow.nfo").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<tvshow>
  <title>黑夜告白</title>
  <actor>
    <name>Pan Yueming</name>
    <type>Actor</type>
    <tmdbid>138734</tmdbid>
  </actor>
  <tmdbid>254498</tmdbid>
  <uniqueid type="tmdb">254498</uniqueid>
</tvshow>
""",
        encoding="utf-8",
    )
    tmdb_id, source = scan_mod.pinned_tmdb_id(MediaKind.TV, root, file)
    assert (tmdb_id, source) == (254498, IdentitySource.NFO)


async def test_owned_units_skip_wanted_and_show_in_prepare(db, tmp_path) -> None:
    """库存联通：订阅创建只为缺的集建工单；prepare 返回每季已有集数。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        service = SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))
        # prepare：S1 已播 3 集，库里有 2 集
        item, seasons, _existing = await service.prepare(MediaKind.TV, 200)
        from movieclaw_db.repositories.library_file_repo import LibraryFileRepository

        owned = await LibraryFileRepository(session).owned_units(item.id)
        assert owned == {(1, 1), (1, 2)}

        sub = await service.create(MediaKind.TV, 200, selected_seasons=[1])
        rows = list(
            (await session.execute(select(WantedItem).where(WantedItem.subscription_id == sub.id)))
            .scalars()
            .all()
        )
        # 只有 E03 缺——E01/E02 库里已有，不建工单
        assert {(w.season_number, w.episode_number) for w in rows} == {(1, 3)}


async def test_scan_progress_observable_and_cleared(db, tmp_path, monkeypatch) -> None:
    """扫描进行中能轮询到 (已处理, 总数)，结束后进度清空——前端进度环的数据源。"""
    import asyncio

    import movieclaw_api.services.library.scan as scan_mod
    from movieclaw_api.services.library.scan import scan_progress

    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    # 给每个文件的探测加一点耗时，让"进行中"窗口足够被采样到
    original_probe = scan_mod.probe_media

    def slow_probe(path):
        import time

        time.sleep(0.05)
        return original_probe(path)

    monkeypatch.setattr(scan_mod, "probe_media", slow_probe)

    task = asyncio.create_task(scan_library(library.id))
    sampled: list[tuple[str, int, int]] = []
    while not task.done():
        state = scan_progress(library.id)
        if state is not None:
            sampled.append((state.phase, state.processed, state.total))
        # 状态与"在跑"必须同生同灭：任一时刻取到 is_scanning 就一定取得到进度
        assert scan_mod.is_scanning(library.id) == (state is not None)
        await asyncio.sleep(0.01)
    await task

    assert sampled, "扫描期间必须能采样到进度"
    ingesting = [s for s in sampled if s[0] == scan_mod.ScanPhase.INGESTING]
    assert ingesting, "必须能采样到逐文件入账阶段"
    assert ingesting[-1][2] == 3  # 入账阶段的分母 = 两集 + junk
    assert scan_progress(library.id) is None  # 结束后清空


async def test_asset_phase_reports_its_own_phase_and_denominator(db, tmp_path, monkeypatch) -> None:
    """文件入账跑完后进入资产补齐阶段，阶段名与分子分母都换成资产自己的。

    回归：这一段曾经挂在扫描阶段下静默执行，进度长时间僵在"文件数/文件数"
    ——一部剧几百张分集剧照能下十几分钟，用户看到的是一个撞了墙的进度条。
    """
    import movieclaw_api.services.media_scrape as scrape_mod

    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    library_id = library.id

    seen: list[tuple[str, int, int]] = []

    async def watching_ensure_assets(item_id: int) -> None:
        state = scan_mod.scan_progress(library_id)
        assert state is not None
        seen.append((state.phase, state.processed, state.total))

    monkeypatch.setattr(scrape_mod, "ensure_assets", watching_ensure_assets)

    summary = await scan_library(library_id)

    assert seen, "本轮有条目挂上身份锚，就必须走资产补齐阶段"
    assert all(phase == scan_mod.ScanPhase.ASSETS for phase, _, _ in seen)
    # 分母是条目数（1 部剧）而不是文件数（3）——阶段换了分母就得跟着换
    assert {total for _, _, total in seen} == {len(summary.identified_item_ids)}
    assert seen[0][1] == 0  # 第一个条目开工时已完成 0 个
    assert scan_mod.scan_progress(library_id) is None  # 收尾后状态清空


async def test_asset_phase_honors_stop_request(db, tmp_path, monkeypatch) -> None:
    """资产补齐阶段同样响应「停止扫描」。

    回归：停止标志过去只在逐文件循环里检查，进入资产阶段后按钮就成了摆设
    ——界面还显示"扫描中"，点停止毫无反应。文件此时已全部入账，缺的图片
    由任一后续刷新入口自愈，没有理由让用户干等。
    """
    import movieclaw_api.services.media_scrape as scrape_mod

    root = tmp_path / "media" / "tv"
    # 两部剧 → 两个条目：第一个补齐时喊停，第二个必须不再开工
    show_a = root / "测试剧集 (2024)" / "Season 01"
    show_a.mkdir(parents=True)
    (show_a / "测试剧集.S01E01.1080p.mkv").write_bytes(b"a1")
    show_b = root / "另一部剧 (2024) [tmdbid=201]" / "Season 01"
    show_b.mkdir(parents=True)
    (show_b / "另一部剧.S01E01.1080p.mkv").write_bytes(b"b1")

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    library_id = library.id

    calls: list[int] = []

    async def stopping_ensure_assets(item_id: int) -> None:
        calls.append(item_id)
        assert scan_mod.request_stop_scan(library_id) is True  # 资产阶段可停

    monkeypatch.setattr(scrape_mod, "ensure_assets", stopping_ensure_assets)

    summary = await scan_library(library_id)

    assert len(summary.identified_item_ids) == 2, "样本要有两个条目才谈得上提前收尾"
    assert len(calls) == 1, "喊停之后不该再给下一个条目补资产"
    # 文件都已入账，停的只是图片下载——不该被记成"扫描没扫完"
    assert summary.cancelled is False
    assert not scan_mod._scan_tasks.stop_requested(library_id)  # 标志随扫描收尾清干净


async def test_reidentify_is_not_disguised_as_scanning(db, tmp_path, monkeypatch) -> None:
    """重识别与扫描共用库级锁，但阶段如实标成 REIDENTIFYING。

    回归两处：
    1. 接口过去一律回"正在扫描"，用户看到的状态与实际动作对不上；
    2. 「停止扫描」当时会被受理，可重识别根本不看这个标志——既停不下来，
       残留的标志还会让**下一次真扫描**刚开始就被取消。
    """
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    library_id = library.id
    await scan_library(library_id)

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    item_id = next(r.media_item_id for r in rows if r.media_item_id is not None)

    observed: list[str] = []
    original_identify = scan_mod._identify

    async def watching_identify(*args, **kwargs):
        state = scan_mod.scan_progress(library_id)
        assert state is not None
        observed.append(state.phase)
        # 重识别期间请求停止：必须被拒绝，且不留下任何痕迹
        assert scan_mod.request_stop_scan(library_id) is False
        assert not scan_mod._scan_tasks.stop_requested(library_id)
        return await original_identify(*args, **kwargs)

    monkeypatch.setattr(scan_mod, "_identify", watching_identify)
    summary = await scan_mod.reidentify_item(library_id, item_id)
    assert not summary.errors

    assert observed, "重识别必须走到识别链"
    assert all(phase == scan_mod.ScanPhase.REIDENTIFYING for phase in observed)
    assert scan_mod.scan_progress(library_id) is None  # 结束后状态清空
    # 下一次真扫描不能被上一轮的残留标志取消
    next_summary = await scan_library(library_id)
    assert next_summary.cancelled is False


async def test_reconcile_marks_missing_and_rescan_restores(db, tmp_path) -> None:
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    victim = root / "测试剧集 (2024)" / "Season 01" / "测试剧集.S01E01.1080p.mkv"
    payload = victim.read_bytes()
    victim.unlink()
    # 删除感知已并入扫描本身：重扫即标记 missing（无需独立对账步骤）
    summary = await scan_library(library.id)
    assert summary.marked_missing == 1
    # 最近扫描结论要留档（前端"点了有反应"的反馈数据源）
    from movieclaw_api.services.library.scan import last_scan

    record = last_scan(library.id)
    assert record is not None and record[1].marked_missing == 1

    async with db.session() as session:
        row = (
            (await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(victim))))
            .scalars()
            .one()
        )
        assert row.missing_since is not None  # 标记而非删除

    # 文件回归 → 重扫清除 missing
    victim.write_bytes(payload)
    await scan_library(library.id)
    async with db.session() as session:
        row = (
            (await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(victim))))
            .scalars()
            .one()
        )
        assert row.missing_since is None


async def test_auto_clear_missing_off_keeps_ledger(db, tmp_path) -> None:
    """默认（开关关）：扫描只标记不删——记录是「重新下载」与改名归并的依据。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
        assert library.auto_clear_missing is False  # 危险开关默认关
    await scan_library(library.id)

    (root / "测试剧集 (2024)" / "Season 01" / "测试剧集.S01E01.1080p.mkv").unlink()
    summary = await scan_library(library.id)
    assert summary.marked_missing == 1 and summary.cleared_missing == 0

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len([r for r in rows if r.missing_since is not None]) == 1


async def test_auto_clear_missing_removes_confirmed_lost(db, tmp_path) -> None:
    """开了自动清理：扫完台账即与磁盘对齐，不用用户再手动清一次缺失。

    同时验证在位文件一条不动——清理只针对"本轮遍历确认不在磁盘上"的行。
    """
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)], auto_clear_missing=True
        )
    await scan_library(library.id)
    async with db.session() as session:
        before = len(list((await session.execute(select(LibraryFile))).scalars().all()))

    victim = root / "测试剧集 (2024)" / "Season 01" / "测试剧集.S01E01.1080p.mkv"
    victim.unlink()
    summary = await scan_library(library.id)
    assert summary.marked_missing == 1
    assert summary.cleared_missing == 1  # 标记与清理在同一轮完成（"扫完就干净"）

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len(rows) == before - 1  # file_count 与磁盘对齐
        assert all(r.file_path != str(victim) for r in rows)
        assert all(r.missing_since is None for r in rows)


async def test_auto_clear_missing_skips_unreadable_dirs(db, tmp_path, monkeypatch) -> None:
    """遍历吞了目录（权限/掉盘/网络挂载抖动）的一轮：整轮不清理。

    读不动的目录不等于底下的文件不存在，此时的"没遍历到"不可信——标记
    missing 无伤大雅（回归自动恢复），删记录则不可挽回。
    """
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)], auto_clear_missing=True
        )
    await scan_library(library.id)

    season = root / "测试剧集 (2024)" / "Season 01"
    original_walk = scan_mod._walk_videos

    def flaky_walk(walk_root, unreadable=None, dir_files=None, **kwargs):
        """整个季目录列不动：底下两集本轮都遍历不到。"""
        for entry, is_disc in original_walk(walk_root, unreadable, dir_files, **kwargs):
            if not str(entry).startswith(str(season)):
                yield entry, is_disc
        if unreadable is not None:
            unreadable.append(str(season))

    monkeypatch.setattr(scan_mod, "_walk_videos", flaky_walk)
    summary = await scan_library(library.id)
    assert summary.marked_missing == 2  # 标记照旧（回归即恢复）
    assert summary.cleared_missing == 0  # 但一条都不能删

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len([r for r in rows if r.missing_since is not None]) == 2

    # 目录恢复可读 → 文件回归，标记自动清除（没有任何东西被删掉）
    monkeypatch.setattr(scan_mod, "_walk_videos", original_walk)
    await scan_library(library.id)
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert all(r.missing_since is None for r in rows)


async def test_auto_clear_missing_spares_emptied_root(db, tmp_path) -> None:
    """挂载掉线的典型症状：挂载点还在、目录可读、底下空了。

    这与"用户把这个根下的片子全删了"在磁盘上完全一样，只能二选一地误判——
    宁可少清（记录留着，下轮再清）也不能清错（删了回不来）。
    """
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)], auto_clear_missing=True
        )
    await scan_library(library.id)
    async with db.session() as session:
        before = len(list((await session.execute(select(LibraryFile))).scalars().all()))
    assert before > 0

    for child in list(root.iterdir()):  # 根还在、可读，但一个文件都没了
        shutil.rmtree(child)
    summary = await scan_library(library.id)
    assert summary.marked_missing == before  # 标记照旧（回归即恢复）
    assert summary.cleared_missing == 0  # 但一条都不清

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len(rows) == before


async def test_auto_clear_missing_spares_offline_root(db, tmp_path) -> None:
    """掉盘/挂载失败的根整个跳过：它底下的记录一条都不清（也不标记）。"""
    root_a = _make_tv_library(tmp_path)
    root_b = tmp_path / "media" / "tv2"
    (root_b / "另一部剧 (2024)" / "Season 01").mkdir(parents=True)
    (root_b / "另一部剧 (2024)" / "Season 01" / "另一部剧.S01E01.mkv").write_bytes(b"b1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库",
            kind="tv",
            root_paths=[str(root_a), str(root_b)],
            auto_clear_missing=True,
        )
    await scan_library(library.id)
    async with db.session() as session:
        before = len(list((await session.execute(select(LibraryFile))).scalars().all()))

    # 扩展根整个不可达（模拟掉盘）：它下面的文件既不该被标记，更不该被清理
    root_b.rename(tmp_path / "media" / "tv2-offline")
    summary = await scan_library(library.id)
    assert summary.marked_missing == 0 and summary.cleared_missing == 0
    assert any("根路径不存在" in e for e in summary.errors)

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len(rows) == before


async def test_scan_relinks_renamed_file(db, tmp_path) -> None:
    """改名归并：已识别文件在磁盘被改成认不出的名字 → 台账行随迁，身份无损。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    old = root / "测试剧集 (2024)" / "Season 01" / "测试剧集.S01E01.1080p.mkv"
    new = old.with_name("完全认不出的名字.mkv")
    async with db.session() as session:
        row = (
            (await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(old))))
            .scalars()
            .one()
        )
        old_id, old_item = row.id, row.media_item_id
        old_unit = (row.season_number, row.episode_number)
    old.rename(new)

    summary = await scan_library(library.id)
    assert summary.relinked == 1
    # 待识别计数只来自 junk 文件的例行识别重试；改名文件没有当新文件进待识别
    assert summary.unidentified == summary.retried == 1

    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len(files) == 3  # 行数不变：没有幽灵行 + 新行
        moved = next(f for f in files if f.file_path == str(new))
        assert moved.id == old_id  # 同一行随迁而非重建
        assert moved.media_item_id == old_item
        assert (moved.season_number, moved.episode_number) == old_unit
        assert moved.missing_since is None


async def test_scan_relink_preserves_manual_claim(db, tmp_path) -> None:
    """人工认领过的待识别文件被改名 → 认领成果随行保留，不用重新认领。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    from movieclaw_db.repositories.library_file_repo import LibraryFileRepository

    junk = root / "未知内容目录" / "zzqx.mkv"
    async with db.session() as session:
        repo = LibraryFileRepository(session)
        row = (
            (await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(junk))))
            .scalars()
            .one()
        )
        media_service = MediaLibraryService(session, _fake_tmdb())
        item = await media_service.ensure_media_item(MediaKind.TV, 200)
        await repo.claim_identity(row.id, media_item_id=item.id, season_number=1, episode_number=3)

    junk.rename(junk.with_name("还是认不出的新名字.mkv"))
    summary = await scan_library(library.id)
    assert summary.relinked == 1

    async with db.session() as session:
        moved = (
            (
                await session.execute(
                    select(LibraryFile).where(
                        LibraryFile.file_path.endswith("还是认不出的新名字.mkv")
                    )
                )
            )
            .scalars()
            .one()
        )
        assert moved.media_item_id is not None  # 认领结果延续
        assert (moved.season_number, moved.episode_number) == (1, 3)


async def test_scan_copy_is_not_relink(db, tmp_path) -> None:
    """复制（旧路径仍在磁盘）不是改名：不归并，按新文件落账。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    src = root / "测试剧集 (2024)" / "Season 01" / "测试剧集.S01E01.1080p.mkv"
    copy = src.with_name("副本.mkv")
    copy.write_bytes(src.read_bytes())

    summary = await scan_library(library.id)
    assert summary.relinked == 0  # 旧路径仍在磁盘：不是改名，不归并

    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len(files) == 4  # 副本按新文件落了新行（经目录名照常识别）
        assert {str(src), str(copy)} <= {f.file_path for f in files}


async def test_scan_relink_ambiguous_candidates_bail_out(db, tmp_path) -> None:
    """多个同尺寸旧行都消失时无法确定对应关系：不归并（宁缺毋滥）。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    season_dir = root / "测试剧集 (2024)" / "Season 01"
    # E01/E02 内容同尺寸：一个删除、一个改名 → 新文件对应哪行无法确定
    (season_dir / "测试剧集.S01E01.1080p.mkv").unlink()
    (season_dir / "测试剧集.S01E02.1080p.mkv").rename(season_dir / "不知道是哪集.mkv")

    summary = await scan_library(library.id)
    assert summary.relinked == 0  # 两个候选二义：不归并

    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        # 新文件落新行（经目录名照常识别），两条旧行原地保留（留给对账标记 missing）
        assert len(files) == 4


async def test_scan_records_unidentified_reason_and_claim_clears(db, tmp_path) -> None:
    """认不出的文件要在台账上留下"为什么认不出"（前端待识别清单展示）；
    人工认领后原因随之清除。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        from movieclaw_db.repositories.library_file_repo import LibraryFileRepository

        repo = LibraryFileRepository(session)
        unknown = (await repo.list_unidentified(library_id=library.id))[0]
        assert unknown.unidentified_reason  # 有可读的失败原因
        identified = [
            f for f in await repo.list_by_library(library.id) if f.unidentified_code is None
        ]
        assert all(f.unidentified_reason is None for f in identified)

        # 认领后原因失义，应清除
        item_id = identified[0].media_item_id
        claimed = await repo.claim_identity(
            unknown.id, media_item_id=item_id, season_number=1, episode_number=9
        )
        assert claimed is not None and claimed.unidentified_reason is None


async def test_scan_stop_request_cancels_early(db, tmp_path) -> None:
    """停止请求让扫描提前收尾：cancelled 标记置位、剩余文件不入账；
    没有扫描在跑时 request_stop_scan 返回 False。"""
    from movieclaw_api.services.library.scan import request_stop_scan

    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    assert request_stop_scan(library.id) is False  # 没在扫

    # 预置停止标志：扫描循环在第一个文件前即检查到并提前收尾
    scan_mod._scan_tasks._stops.add(library.id)
    summary = await scan_library(library.id)
    assert summary.cancelled is True
    assert summary.scanned == 0  # 一个文件都没处理
    assert not scan_mod._scan_tasks.stop_requested(library.id)  # 收尾时清除标志

    # 停止不破坏增量语义：再扫一次照常完成
    summary2 = await scan_library(library.id)
    assert summary2.cancelled is False and summary2.identified == 2


async def test_rescan_retries_unidentified_after_tmdb_recovery(db, tmp_path, monkeypatch) -> None:
    """TMDB 故障期间落账的待识别文件，网络恢复后重扫自动补识别——
    行原地更新（不新建台账），失败原因随之清除。"""
    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "测试剧集.S01E01.1080p.mkv").write_bytes(b"e1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    # 第一轮：TMDB "网络不通"
    real_verify = scan_mod.resolve_with_candidates

    async def boom(*args, **kwargs):
        raise RuntimeError("网络不可达")

    monkeypatch.setattr(scan_mod, "resolve_with_candidates", boom)
    summary = await scan_library(library.id)
    assert summary.identified == 0 and summary.unidentified == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        # TMDB 不通也先挂临时本地身份（文件可见可播），失败原因照记
        assert row.media_item_id is not None
        assert row.unidentified_reason and "TMDB 查询失败" in row.unidentified_reason
        row_id = row.id
        provisional_id = row.media_item_id

    # 网络恢复后重扫：同一行补上身份锚，原因清除
    monkeypatch.setattr(scan_mod, "resolve_with_candidates", real_verify)
    summary2 = await scan_library(library.id)
    assert summary2.retried == 1 and summary2.identified == 1
    assert summary2.scanned == 0 and summary2.unidentified == 0

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.id == row_id  # 原地更新，不是删了重建
        assert row.media_item_id is not None and row.media_item_id != provisional_id
        assert row.unidentified_reason is None
        # 转正后临时条目被孤儿清理收走
        assert await session.get(MediaItem, provisional_id) is None


async def test_missing_file_return_keeps_identity_without_reidentify(
    db, tmp_path, monkeypatch
) -> None:
    """标记 missing 的已识别文件回归时，身份锚原样保留、不重走识别链——
    即使此刻 TMDB 不通也不会把已有身份冲掉。"""
    root = _make_tv_library(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    # E01 消失 → 对账标记 missing
    e01 = root / "测试剧集 (2024)" / "Season 01" / "测试剧集.S01E01.1080p.mkv"
    e01.unlink()
    await scan_library(library.id)
    async with db.session() as session:
        row = (
            (await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(e01))))
            .scalars()
            .one()
        )
        assert row.missing_since is not None and row.media_item_id is not None
        anchor = row.media_item_id

    # 文件回归，但 TMDB 全挂：身份必须保留
    e01.write_bytes(b"e1")
    import os
    import time as time_mod

    old = time_mod.time() - 3600
    os.utime(e01, (old, old))  # 绕过写入静默窗口

    async def boom(*args, **kwargs):
        raise RuntimeError("网络不可达")

    monkeypatch.setattr(scan_mod, "resolve_with_candidates", boom)
    summary = await scan_library(library.id)
    assert summary.scanned == 1  # 回归文件按原语义计入
    async with db.session() as session:
        row = (
            (await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(e01))))
            .scalars()
            .one()
        )
        assert row.missing_since is None  # 在位标记恢复
        assert row.media_item_id == anchor  # 身份未被冲掉


async def test_scan_prefers_path_tmdbid_tag(db, tmp_path) -> None:
    """目录名的 [tmdbid=N] 标记（Emby/Jellyfin 惯例）是显式身份声明：
    文件名/片名在 TMDB 搜不到也照样精确识别，不进待识别。"""
    root = tmp_path / "media" / "movies"
    folder = root / "无从搜起的片名 (2020) [tmdbid=300]"
    folder.mkdir(parents=True)
    (folder / "zzqx.mkv").write_bytes(b"movie")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1 and summary.unidentified == 0
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id is not None


async def test_scan_sees_through_grouping_dirs(db, tmp_path) -> None:
    """库根与条目目录之间的分类分组层（剧集/大陆/…）不该挡住识别：
    条目目录名要能被逐层找到，否则「大陆」会被当条目名、年份白丢。"""
    root = tmp_path / "media" / "tv"
    show = root / "大陆" / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "测试剧集.S01E01.1080p.mkv").write_bytes(b"e1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1 and summary.unidentified == 0


def test_local_episode_count(tmp_path) -> None:
    """本地集数统计：按集号去重（同集多版本只算一集），不足 2 集不作数。"""
    season = tmp_path / "Season 01"
    season.mkdir()
    for n in (1, 2, 3):
        (season / f"测试剧集.S01E{n:02d}.1080p.mkv").write_bytes(b"x")
    (season / "测试剧集.S01E03.2160p.mkv").write_bytes(b"x")  # 同集另一版本
    (season / "测试剧集.S01E04.sample.mkv").write_bytes(b"x")  # sample 不计
    (season / "海报.jpg").write_bytes(b"x")  # 非视频不计
    assert scan_mod.local_episode_count(season) == 3

    single = tmp_path / "single"
    single.mkdir()
    (single / "测试剧集.S01E01.mkv").write_bytes(b"x")
    assert scan_mod.local_episode_count(single) is None


# ---------------------------------------------------------------------------
# 无法抉择时：候选落账 → 清单分组 → 整组认领（《风筝》2017 实测案例的闭环）
# ---------------------------------------------------------------------------


def _ambiguous_tmdb() -> TmdbClient:
    """同名同年双版本的假 TMDB：正片 46 集 / 送审版 51 集（别名也叫「风筝」）。"""
    detail = {
        "status": "Ended",
        "external_ids": {},
        "translations": {"translations": []},
    }
    routes = {
        "/3/tv/900": {
            "id": 900,
            "name": "风筝",
            "original_name": "风筝",
            "first_air_date": "2017-12-17",
            "number_of_seasons": 1,
            "seasons": [{"season_number": 1, "episode_count": 46}],
            "alternative_titles": {"results": []},
            **detail,
        },
        "/3/tv/901": {
            "id": 901,
            "name": "风筝·送审版",
            "original_name": "风筝·送审版",
            "first_air_date": "2017-12-17",
            "number_of_seasons": 1,
            "seasons": [{"season_number": 1, "episode_count": 51}],
            "alternative_titles": {"results": [{"title": "风筝"}]},
            **detail,
        },
        "/3/tv/900/season/1": {
            "name": "第 1 季",
            "air_date": "2017-12-17",
            "episodes": [
                {"episode_number": n, "name": f"E{n}", "air_date": "2017-12-17"}
                for n in range(1, 47)
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/3/search/tv":
            hit = "风筝" in request.url.params.get("query", "")
            return httpx.Response(
                200,
                json={
                    "results": [
                        {k: routes[f"/3/tv/{i}"][k] for k in ("id", "name", "original_name")}
                        | {"first_air_date": routes[f"/3/tv/{i}"]["first_air_date"]}
                        for i in (900, 901)
                    ]
                    if hit
                    else []
                },
            )
        payload = routes.get(path)
        return httpx.Response(200 if payload else 404, json=payload or {})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


def _make_ambiguous_show(tmp_path):
    """本地只有 2 集：集数既对不上 46 也对不上 51 → 机器无从抉择。"""
    root = tmp_path / "media" / "tv2"
    season = root / "大陆" / "风筝 (2017)" / "Season 1"
    season.mkdir(parents=True)
    for n in (1, 2):
        (season / f"风筝 S01E{n:02d} - 2160p.mkv").write_bytes(b"x")
    return root


async def test_ambiguous_scan_records_candidates_and_groups(db, tmp_path, monkeypatch) -> None:
    """判不了时：原因说清"为什么判不了" + 候选落账 + 清单按条目目录成一组。"""
    from movieclaw_api.api.routes.libraries import list_unidentified

    client = _ambiguous_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    root = _make_ambiguous_show(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 0 and summary.unidentified == 2

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert all("同样可信的候选" in (r.unidentified_reason or "") for r in rows)
        ids = {c["tmdb_id"] for c in rows[0].unidentified_candidates}
        assert ids == {900, 901}
        # 候选带上各自的集数：46 / 51 正是用户肉眼区分的依据
        counts = {c["tmdb_id"]: c["episode_count"] for c in rows[0].unidentified_candidates}
        assert counts == {900: 46, 901: 51}

        resp = await list_unidentified(library.id, session)
    groups = resp.data
    assert len(groups) == 1  # 两集聚成一组，不再逐集刷屏
    assert groups[0].label == "风筝 (2017)" and groups[0].file_count == 2
    assert len(groups[0].candidates) == 2


async def test_claim_batch_fixes_whole_group_at_once(db, tmp_path, monkeypatch) -> None:
    """整组认领：一次点击全组生效，每个文件沿用自己已解析的季集号。"""
    import movieclaw_api.api.routes.libraries as routes_mod
    from movieclaw_api.api.routes.libraries import claim_files_batch
    from movieclaw_api.schemas.library import ClaimBatchPayload

    client = _ambiguous_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(routes_mod, "get_tmdb_client", lambda: client)
    root = _make_ambiguous_show(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        payload = ClaimBatchPayload(file_ids=[r.id for r in rows], title_ref="tmdb:tv:900")
        resp = await claim_files_batch(payload, BackgroundTasks(), session)
    assert resp.data["claimed"] == 2

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert all(r.media_item_id is not None for r in rows)
        assert all(r.unidentified_reason is None for r in rows)
        assert all(r.unidentified_candidates is None for r in rows)
        # 季集号来自各自的文件名，不是前端指定的
        assert {(r.season_number, r.episode_number) for r in rows} == {(1, 1), (1, 2)}


# ---------------------------------------------------------------------------
# 忽略是永久的：忽略过的文件不再重走识别链、不回清单，但可恢复
# ---------------------------------------------------------------------------


async def test_ignored_file_stays_ignored_across_rescans(db, tmp_path, monkeypatch) -> None:
    """忽略过的文件重扫时秒过：不重走识别链（连 TMDB 都不该打），不回清单。

    这是「忽略」曾经的硬伤——早先实现是删台账行，而扫描器判定"新文件"
    就看台账有没有这条路径，删了下轮扫描原样再来一遍。
    """
    from movieclaw_api.api.routes.libraries import ignore_file, list_unidentified

    root = tmp_path / "media" / "tv"
    junk = root / "无从辨认的花絮" / "zzqx.mkv"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"junk")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.unidentified == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        await ignore_file(row.id, session)
        assert (await list_unidentified(library.id, session)).data == []

    # 识别链此刻若被触碰就炸——忽略过的文件不该再产生任何识别开销
    async def boom(*args, **kwargs):
        raise AssertionError("忽略过的文件不应重走识别链")

    monkeypatch.setattr(scan_mod, "resolve_with_candidates", boom)
    summary2 = await scan_library(library.id)
    assert summary2.skipped_ignored == 1
    assert summary2.scanned == 0 and summary2.unidentified == 0 and summary2.retried == 0

    async with db.session() as session:
        # 台账行仍在（否则下次扫描又会把它当新文件），清单里仍然看不到
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.ignored_at is not None and row.media_item_id is None
        assert (await list_unidentified(library.id, session)).data == []


# ---------------------------------------------------------------------------
# 原盘目录与钉死身份矛盾校验的 badcase 回归（RESOLVER_VERSION 4）
# ---------------------------------------------------------------------------


def _make_disc_dir(parent: Path, name: str) -> Path:
    """造一个最小蓝光原盘结构：{name}/BDMV/STREAM/00000.m2ts。"""
    disc = parent / name
    stream = disc / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    (stream / "00000.m2ts").write_bytes(b"bd")
    return disc


async def test_disc_dir_with_dotted_title_identifies(db, tmp_path) -> None:
    """badcase 回归：原盘目录「E.T.外星人 (1982)」曾被 Path.stem 截成
    「E.T」，片名与年份证据全丢、直接进待识别（unparsable）。修复后：
    目录名原样参与解析；NER 只抽出「外星人」过不了标题门槛时，
    Title (Year) 惯例名作备选查询词追回完整片名。"""
    root = tmp_path / "media" / "movies"
    _make_disc_dir(root / "欧美", "E.T.外星人 (1982)")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1 and summary.unidentified == 0

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.container == "bluray"
        assert (row.season_number, row.episode_number) == (0, 0)
        item = await session.get(MediaItem, row.media_item_id)
        assert item is not None and item.tmdb_id == 601
        assert row.identity_source == IdentitySource.RESOLVED


async def test_disc_dir_reads_nfo_inside_disc(db, tmp_path) -> None:
    """原盘的条目 NFO 惯例位置在**盘内**（movie.nfo 与 BDMV 平级）；
    旧代码对目录做 with_suffix 拼出「E.T.nfo」这类无意义路径且从父目录
    起找，盘内 NFO 会被漏掉。"""
    root = tmp_path / "media" / "movies"
    disc = _make_disc_dir(root, "某电影 (2020)")
    (disc / "movie.nfo").write_text(
        "<movie><title>某电影</title><tmdbid>300</tmdbid></movie>", encoding="utf-8"
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item = await session.get(MediaItem, row.media_item_id)
        assert item is not None and item.tmdb_id == 300
        assert row.identity_source == IdentitySource.NFO


def _spec(duration_seconds: int):
    from movieclaw_api.services.media_probe import MediaSpec

    return MediaSpec(
        resolution="2160p",
        video_codec="hevc",
        hdr=None,
        bit_depth=10,
        duration_seconds=duration_seconds,
        bit_rate=None,
    )


async def test_pinned_id_overturned_by_runtime_conflict(db, tmp_path, monkeypatch) -> None:
    """badcase 回归：残留 NFO 把 115 分钟的《神奇4侠：初露锋芒》(2025)
    指向同年的 3 分钟短片《4》——年份轴失明（恰好同年）、标题包含判定
    被单字符标题击穿（"4"⊂"神奇4侠"）。时长轴（3 倍且差 30 分钟以上）
    识破声明后，降级名称解析追回正主。"""
    monkeypatch.setattr(scan_mod, "probe_media", lambda p: _spec(6875))
    root = tmp_path / "media" / "movies"
    entry = root / "神奇4侠：初露锋芒 (2025)"
    entry.mkdir(parents=True)
    (entry / "神奇4侠：初露锋芒 (2025) - 2160p H.265 Atmos CHDWEB.mkv").write_bytes(b"ff")
    (entry / "movie.nfo").write_text(
        "<movie><title>4</title><year>2025</year><tmdbid>888</tmdbid></movie>",
        encoding="utf-8",
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item = await session.get(MediaItem, row.media_item_id)
        assert item is not None and item.tmdb_id == 617126  # 名称解析的结论胜出
        assert row.identity_source == IdentitySource.RESOLVED


async def test_pinned_id_survives_runtime_agreement(db, tmp_path, monkeypatch) -> None:
    """不误伤：NFO 指向的条目片长与实测吻合（意译名目录标题必然不符），
    照样采信声明——时长轴只识破"悬殊"，不苛求精确。"""
    monkeypatch.setattr(scan_mod, "probe_media", lambda p: _spec(115 * 60 + 200))
    root = tmp_path / "media" / "movies"
    entry = root / "Yi Yi De Mu Lu (1982)"
    entry.mkdir(parents=True)
    (entry / "yydml.mkv").write_bytes(b"m")
    (entry / "movie.nfo").write_text("<movie><tmdbid>601</tmdbid></movie>", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item = await session.get(MediaItem, row.media_item_id)
        assert item is not None and item.tmdb_id == 601
        assert row.identity_source == IdentitySource.NFO


def test_pinned_mismatch_short_title_containment() -> None:
    """包含判定的短边下限：单字符标题（《4》）不认包含，两字符（《24》
    ⊂「24小时」）照常认——那是真实的中英片名对应关系。"""
    from movieclaw_api.services.library.resolve import LocalEvidence

    short = MediaItem(kind="movie", tmdb_id=888, title="4", original_title="4", year=2020)
    # 单字符包含不作数：年份矛盾时该推翻就推翻
    assert (
        scan_mod._pinned_mismatch(short, LocalEvidence(title="神奇4侠：初露锋芒", year=2025))
        is not None
    )
    series = MediaItem(kind="tv", tmdb_id=1, title="24", original_title="24", year=2001)
    # 两字符包含关系仍然有效：哪怕年份对不上也不推翻（别名常缺年份对齐）
    assert scan_mod._pinned_mismatch(series, LocalEvidence(title="24小时", year=2010)) is None


async def test_claim_rewrites_poisoned_nfo(db, tmp_path) -> None:
    """认领反哺盘面：认领结论与目录里 NFO 声明的 tmdbid 矛盾时，NFO 被
    原地改写为认领身份——否则下次全量重扫会把毒 NFO 原样读回（识别链
    NFO 优先是自我固化的），Emby/Jellyfin 也继续错挂。"""
    from movieclaw_api.services.library.claim import claim_files
    from movieclaw_api.services.library.nfo import read_tmdb_id

    root = tmp_path / "media" / "movies"
    entry = root / "神奇4侠：初露锋芒 (2025)"
    entry.mkdir(parents=True)
    video = entry / "神奇4侠：初露锋芒 (2025) - 2160p.mkv"
    video.write_bytes(b"ff")
    poisoned = "<movie><title>4</title><year>2025</year><tmdbid>888</tmdbid></movie>"
    (entry / "movie.nfo").write_text(poisoned, encoding="utf-8")
    same_name = video.with_suffix(".nfo")
    same_name.write_text(poisoned, encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    await scan_library(library.id)  # 无实测时长，毒 NFO 此时仍会被采信
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item, claimed, _displaced = await claim_files(session, [row.id], tmdb_id=617126)
        assert claimed == 1 and item.tmdb_id == 617126

    # 两份毒 NFO（同名 + 目录级）都被改写为认领身份
    assert read_tmdb_id(entry / "movie.nfo") == 617126
    assert read_tmdb_id(same_name) == 617126

    # 重扫不再中毒：身份保持认领结论
    await scan_library(library.id)
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item = await session.get(MediaItem, row.media_item_id)
        assert item is not None and item.tmdb_id == 617126


def test_guess_evidence_prefers_curated_name_over_ner(tmp_path) -> None:
    """500 库实测：整理过的「Title (Year)」目录名是高置信来源，NER 面向
    脏乱种子名训练、对干净长片名反而截断（截断词同年撞上别的条目就是
    静默错挂）。两者不同形时惯例名当主查询词、NER 结果降为备选。"""
    root = tmp_path / "tv"
    file = root / "知否知否应是绿肥红瘦 (2018)" / "Season 01" / "E01.mkv"
    evidence = scan_mod.guess_evidence(MediaKind.TV, root, file)
    assert evidence is not None
    assert evidence.title == "知否知否应是绿肥红瘦" and evidence.year == 2018
    # NER 的抽取结果降级为备选查询词（具体形态随模型版本浮动，不钉死）
    if evidence.alt_title is not None:
        assert evidence.alt_title != evidence.title


def test_guess_evidence_strips_bracket_tags_before_convention(tmp_path) -> None:
    """「1917 (2019) [tmdbid=530915]」这类名字：纯数字片名 NER 全灭，
    尾部标记组又挡住惯例正则——剥掉方括号组、允许年份后带尾巴之后，
    惯例名照常抽出（标记本身另有 _path_tmdb_id 消费，互不干扰）。"""
    root = tmp_path / "movies"
    file = root / "1917 (2019) [tmdbid=530915]" / "1917 (2019) [tmdbid=530915].mkv"
    evidence = scan_mod.guess_evidence(MediaKind.MOVIE, root, file)
    assert evidence is not None
    assert evidence.title == "1917" and evidence.year == 2019

    tail = root / "饥饿站台 (2019) [tmdbid=619264] - 1080p Remux FLAC.mkv"
    evidence = scan_mod.guess_evidence(MediaKind.MOVIE, root, tail)
    assert evidence is not None
    assert evidence.title == "饥饿站台" and evidence.year == 2019


# ---------------------------------------------------------------------------
# 花絮/预告不再冒充影片（issue #107）
# ---------------------------------------------------------------------------


def test_movie_entry_dir_beats_file_name(tmp_path) -> None:
    """issue #107：正片旁边的花絮按自己的文件名去搜 TMDB 会搜出别的影片。
    「一个电影条目目录 = 一部片」，符合 Title (Year) 惯例的目录名压过文件名。"""
    root = tmp_path / "movies"
    bonus = root / "某电影 (2020)" / "神奇4侠 幕后制作特辑.mkv"
    evidence = scan_mod.guess_evidence(MediaKind.MOVIE, root, bonus)
    assert evidence is not None
    assert evidence.title == "某电影" and evidence.year == 2020


def test_movie_grouping_dir_does_not_hijack_file_name(tmp_path) -> None:
    """反向保证：不符合惯例的分组目录（「电影/欧美/」这类）不参与压制，
    照旧由文件名先说话——否则一个分类目录会把底下所有片子归成一部。"""
    root = tmp_path / "movies"
    file = root / "某电影" / "E.T.外星人 (1982).mkv"
    evidence = scan_mod.guess_evidence(MediaKind.MOVIE, root, file)
    assert evidence is not None
    assert evidence.title == "E.T.外星人" and evidence.year == 1982


async def test_extras_dirs_and_files_never_enter_ledger(db, tmp_path) -> None:
    """花絮/预告不入库：Emby 惯例目录、``-trailer`` 文件名后缀、中文关键词
    三条都要挡住。挡不住的下场是它们各自去搜 TMDB，搜出一堆莫名其妙的条目。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    (entry / "Featurettes").mkdir(parents=True)
    (entry / "花絮").mkdir()
    (entry / "某电影 (2020).mkv").write_bytes(b"main")  # 唯一该入库的
    (entry / "Featurettes" / "making-of.mkv").write_bytes(b"x")
    (entry / "花絮" / "01.mkv").write_bytes(b"x")
    (entry / "某电影 (2020)-trailer.mkv").write_bytes(b"x")
    (entry / "花絮片段 01.mkv").write_bytes(b"x")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.scanned == 1 and summary.identified == 1

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert [Path(r.file_path).name for r in rows] == ["某电影 (2020).mkv"]


def test_extras_marker_does_not_touch_titles_containing_keywords() -> None:
    """后缀惯例只认后缀：《Trailer Park Boys》这类片名自带关键词的正片
    不能被当成花絮丢掉（漏挡可以补，错杀无从发现）。"""
    assert scan_mod.extras_marker("Trailer Park Boys S01E01.mkv") is None
    assert scan_mod.extras_marker("某电影 (2020) - 2160p.mkv") is None
    assert scan_mod.extras_marker("某电影 (2020)-trailer.mkv") == "-trailer"
    assert scan_mod.extras_marker("幕后花絮 01.mkv") == "花絮"


def test_unit_for_trailing_index_episode(tmp_path) -> None:
    """裸尾号命名（「走向共和01」）：常规 SxxExx 全灭时以结尾数字为集号，
    否则整目录几十集坍缩成同一个 E0 单元（NAS 实测 70 个特别篇合并成一格）。
    技术尾巴（x264、4 位年份）不得误吃。"""
    from movieclaw_api.services.library.layout import trailing_index_episode

    specials = tmp_path / "走向共和 (2003)" / "Specials"
    assert scan_mod._unit_for(MediaKind.TV, specials / "走向共和01.mp4") == (0, 1)
    assert scan_mod._unit_for(MediaKind.TV, specials / "走向共和59.mp4") == (0, 59)
    # 常规标记优先，兜底不抢跑
    assert scan_mod._unit_for(MediaKind.TV, specials / "某剧 S02E03.mkv")[1] == 3
    # 技术尾巴与年份不误吃
    assert trailing_index_episode("Movie.x264") is None
    assert trailing_index_episode("纪录片 2003") is None
    assert trailing_index_episode("00") is None
    assert trailing_index_episode("01") == 1


def test_unit_for_explicit_sxxeyy(tmp_path) -> None:
    """显式 SxxEyy 标记确定性解析、压过模型：NAS 实测 torrent-ner-v2 对
    《十三邀》纯场景命名的单集文件名漏抽集号（"S06E01" 的 01 被错标成季号），
    整个季包挂成「解析不出集号」。显式标记在时季集号不再依赖模型。"""
    from movieclaw_api.services.library.layout import explicit_unit

    entry = tmp_path / "十三邀第六季.Thirteen.Talks.S06.2021.2160p.WEB-DL.H265.AAC-HHWEB"
    assert scan_mod._unit_for(
        MediaKind.TV, entry / "Thirteen.Talks.S06E01.2021.2160p.WEB-DL.H265.AAC-HHWEB.mp4"
    ) == (6, 1)
    assert scan_mod._unit_for(
        MediaKind.TV, entry / "十三邀.第五季.Thirteen.Talks.S05E10.2020.2160p.WEB-DL.mp4"
    ) == (5, 10)
    # 大小写/分隔符变体与特别篇季（S00）
    assert explicit_unit("some.show.s01e02") == (1, 2)
    assert explicit_unit("Show S01.E02 1080p") == (1, 2)
    assert explicit_unit("Show.S00E03.Special") == (0, 3)
    # E00 原样带出（先导/特辑占位）：入库层据此按占位跳过而非误报解析失败
    assert explicit_unit("Thirteen.Talks.S06E00.2021.2160p") == (6, 0)
    # 词内片段与数字粘连不误吃，无标记回落原链路
    assert explicit_unit("XS06E01") is None
    assert explicit_unit("Show.S02E051080p") is None
    assert explicit_unit("走向共和01") is None


def test_unit_for_season_conflict(tmp_path) -> None:
    """文件名同时含「第一季」与 S09 两个季信号（NAS 实测《妻子的浪漫旅行》
    第 9 季整季错挂到第 1 季）：分集 NFO 的 <season> 是最强证据须优先；
    没有 NFO 时父目录（Season 9）在冲突季号中仲裁。"""
    season_dir = tmp_path / "妻子的浪漫旅行 第一季 (2018)" / "Season 9"
    season_dir.mkdir(parents=True)
    stem = "妻子的浪漫旅行 第一季 S09E01 - 2160p H.265 AAC CHDWEB"
    (season_dir / f"{stem}.nfo").write_text(
        "<episodedetails><title>第 1 集</title>"
        "<season>9</season><episode>1</episode></episodedetails>",
        encoding="utf-8",
    )
    # 有分集 NFO：直接采信 NFO 的季/集号
    assert scan_mod._unit_for(MediaKind.TV, season_dir / f"{stem}.mkv") == (9, 1)
    # 无 NFO：显式 S09E02 标记直接定案（「第一季」是片名片段，不参与）
    stem2 = "妻子的浪漫旅行 第一季 S09E02 - 2160p H.265 AAC CHDWEB"
    assert scan_mod._unit_for(MediaKind.TV, season_dir / f"{stem2}.mkv") == (9, 2)
    # 无父目录仲裁时显式标记同样定案（旧行为取模型首个季号会错挂第 1 季）
    plain_dir = tmp_path / "某剧"
    assert scan_mod._unit_for(MediaKind.TV, plain_dir / "某剧 第一季 S09E03.mkv") == (9, 3)
    # 单一季号不受影响
    assert scan_mod._unit_for(MediaKind.TV, plain_dir / "某剧 S02E03.mkv") == (2, 3)


def test_path_tmdb_ids_collects_conflicts(tmp_path) -> None:
    """路径各级标记全收集（就近在前、去重）：超过一个即声明自相矛盾。"""
    root = tmp_path / "movies"
    file = root / "某电影 (2020) [tmdbid=888]" / "某电影 (2020) [tmdbid=300] - 2160p.mkv"
    assert scan_mod._path_tmdb_ids(root, file) == [300, 888]
    same = root / "某电影 (2020) [tmdbid=300]" / "某电影 (2020) [tmdbid=300].mkv"
    assert scan_mod._path_tmdb_ids(root, same) == [300]


async def test_conflicting_path_tags_fall_back_to_resolution(db, tmp_path) -> None:
    """badcase 回归：目录标着正主、文件名标着另一个 id（实测 Coda 正片
    目录 + 同名短片文件名标记），"就近优先"会静默选中错的。互相矛盾的
    声明全部不采信，交给名称解析用证据裁决。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020) [tmdbid=888]"
    entry.mkdir(parents=True)
    (entry / "某电影 (2020) [tmdbid=300] - 2160p.mkv").write_bytes(b"mm")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    summary = await scan_library(library.id)
    assert summary.identified == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        item = await session.get(MediaItem, row.media_item_id)
        # 名称解析的证据裁决胜出：既不是就近的 300 也不是 888 说了算，
        # 而是搜索 + 年份佐证收敛出的条目（此处恰为 300——但来源是解析）
        assert item is not None and item.tmdb_id == 300
        assert row.identity_source == IdentitySource.RESOLVED


async def test_restore_ignored_returns_to_unidentified(db, tmp_path) -> None:
    """恢复：清掉忽略标记，文件回到待识别清单并重新参与识别。"""
    from movieclaw_api.api.routes.libraries import (
        ignore_file,
        list_ignored,
        list_unidentified,
        restore_ignored_files,
    )
    from movieclaw_api.schemas.library import RestorePayload

    root = tmp_path / "media" / "tv"
    junk = root / "无从辨认的花絮" / "zzqx.mkv"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"junk")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        await ignore_file(row.id, session)
        groups = (await list_ignored(library.id, session)).data
        assert len(groups) == 1 and groups[0].label == "无从辨认的花絮"

        resp = await restore_ignored_files(RestorePayload(file_ids=[row.id]), session)
        assert resp.data["restored"] == 1
        assert (await list_ignored(library.id, session)).data == []
        assert len((await list_unidentified(library.id, session)).data) == 1

    # 恢复后重扫会重新尝试识别（不再秒过）
    summary = await scan_library(library.id)
    assert summary.skipped_ignored == 0 and summary.retried == 1


async def test_ignored_file_survives_disappear_and_return(db, tmp_path) -> None:
    """忽略过的文件消失又回来：忽略状态保留，missing 标记自动清掉。"""
    from movieclaw_api.api.routes.libraries import ignore_file

    root = tmp_path / "media" / "tv"
    junk = root / "无从辨认的花絮" / "zzqx.mkv"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"junk")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        await ignore_file(row.id, session)

    junk.unlink()
    summary = await scan_library(library.id)
    assert summary.marked_missing == 1
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.missing_since is not None and row.ignored_at is not None

    junk.write_bytes(b"junk")
    await scan_library(library.id)
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.missing_since is None and row.ignored_at is not None


async def test_unidentified_code_classifies_failures(db, tmp_path, monkeypatch) -> None:
    """失败分类落库：清单靠 code 决定标签/配色/动作，不能靠猜 reason 文案。

    覆盖三种：认不出片名 / TMDB 搜不到 / TMDB 不可达（ambiguous 见
    test_ambiguous_scan_records_candidates_and_groups）。
    """
    root = tmp_path / "media" / "tv"
    (root / "---").mkdir(parents=True)
    (root / "---" / "---.mkv").write_bytes(b"x")  # 纯分隔符，确定解析不出片名
    (root / "查无此剧 (2019)").mkdir(parents=True)
    (root / "查无此剧 (2019)" / "查无此剧.S01E01.mkv").write_bytes(b"y")  # 搜不到
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )

    await scan_library(library.id)
    async with db.session() as session:
        by_name = {
            Path(r.file_path).name: r
            for r in (await session.execute(select(LibraryFile))).scalars().all()
        }
    assert by_name["---.mkv"].unidentified_code == "unparsable"
    assert by_name["查无此剧.S01E01.mkv"].unidentified_code == "no_match"
    # 原因整句不再以「请…」收尾——该做什么由清单上的按钮表达
    assert not (by_name["---.mkv"].unidentified_reason or "").endswith("请人工认领")

    # TMDB 不可达：与「确实找不到」必须分开，前者重扫可自愈
    async def boom(*args, **kwargs):
        raise RuntimeError("网络不可达")

    monkeypatch.setattr(scan_mod, "resolve_with_candidates", boom)
    (root / "查无此剧 (2019)" / "查无此剧.S01E02.mkv").write_bytes(b"z")
    await scan_library(library.id)
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    fresh = next(r for r in rows if r.file_path.endswith("S01E02.mkv"))
    assert fresh.unidentified_code == "tmdb_unreachable"


async def test_scan_reconciles_rows_inside_disc_dir_by_stat(db, tmp_path) -> None:
    """原盘内部的存量行（落点避让上线前的嵌套/手动放入）按物理存在性对账：
    在盘不误标缺失、真消失标缺失、回归自动复活——扫描不进原盘目录，
    seen 集合恒不含这类行，按 seen 判会误标且永不自愈
    （docs/design/disc-version-layout.md §4）。"""
    from movieclaw_db.models import FileSource, FileState

    root = tmp_path / "movies"
    disc = root / "某电影 (2020)"
    (disc / "BDMV" / "STREAM").mkdir(parents=True)
    (disc / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"disc")
    inner = disc / "某电影 (2020).mkv"
    inner.write_bytes(b"inner")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    await scan_library(library.id)  # 原盘按目录整体收编
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert [r.file_path for r in rows] == [str(disc)]  # 扫描不进原盘内部
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=rows[0].media_item_id,
                file_path=str(inner),
                size_bytes=5,
                source=FileSource.IMPORTED,
            )
        )
        await session.commit()

    await scan_library(library.id)  # 在盘：不因缺席 seen 集合被误标缺失
    async with db.session() as session:
        row = (
            await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(inner)))
        ).scalar_one()
        assert row.state == FileState.IN_PLACE

    inner.unlink()
    await scan_library(library.id)  # 真消失：标缺失
    async with db.session() as session:
        row = (
            await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(inner)))
        ).scalar_one()
        assert row.state == FileState.MISSING

    inner.write_bytes(b"inner")
    await scan_library(library.id)  # 回归：自动复活（upsert 看不见它，靠对账）
    async with db.session() as session:
        row = (
            await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(inner)))
        ).scalar_one()
        assert row.state == FileState.IN_PLACE


async def test_scan_merges_sibling_version_dirs_into_one_item(db, tmp_path) -> None:
    """同级版本目录规范（docs/design/disc-version-layout.md §2）：
    ``标题 (年份) - 标签`` 版本目录（含原盘版本目录）与条目目录识别归并
    到同一条目——版本共存的单位是条目行集合，与目录无关。"""
    root = tmp_path / "movies"
    (root / "某电影 (2020)").mkdir(parents=True)
    (root / "某电影 (2020)" / "某电影 (2020).mkv").write_bytes(b"a")
    version = root / "某电影 (2020) - 2160p"
    version.mkdir()
    (version / "某电影 (2020).mkv").write_bytes(b"b")
    disc_version = root / "某电影 (2020) - 4K原盘"
    (disc_version / "BDMV" / "STREAM").mkdir(parents=True)
    (disc_version / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"c")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )

    await scan_library(library.id)
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        items = list((await session.execute(select(MediaItem))).scalars().all())
    assert len(rows) == 3
    assert all(r.media_item_id is not None for r in rows), [
        (r.file_path, r.unidentified_reason) for r in rows
    ]
    assert len({r.media_item_id for r in rows}) == 1  # 三个版本归并同一条目
    assert [i.tmdb_id for i in items] == [300]
