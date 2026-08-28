"""刮削配置的端到端有效性验证（docs/design/scrape-customization.md）。

与 `test_scrape_settings.py`（接口读写与校验）、`test_library_naming.py`
（渲染器纯函数）分工不同：**这里只问一件事——配置改了，磁盘上/库里的
结果真的跟着变了吗**。

因此每个用例都跑真实链路：真实建库、真实文件落盘、真实 `organize_library`
改名、真实 `mirror_media_dir_assets` 写目录、真实 `fetch_media_profile`
解析（TMDB 走 MockTransport，不出网）。断言落在磁盘路径与落库字段上，
不看接口回显——接口回显对了而落盘没变，正是这类配置功能最典型的假通过。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.organize import organize_library
from movieclaw_api.services.scrape_config import reset_scrape_config
from movieclaw_api.settings import MetadataScrapeSetting
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """真实 SQLite + 真实迁移（scrape_overrides 列由迁移建出来）。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()
    reset_scrape_config()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    reset_scrape_config()
    get_settings.cache_clear()


def _apply_setting(**fields) -> MetadataScrapeSetting:
    """把一份全局刮削设置灌进运行时快照（等价于设置页保存后的状态）。"""
    import movieclaw_api.services.scrape_config as cfg

    setting = MetadataScrapeSetting(**fields)
    cfg._current_scrape = setting
    return setting


async def _make_library(session, *, kind: MediaKind, root: Path, name: str, **kw):
    """建库；``kw`` 里 repo.create 不认的列（write_media_assets 等）建完再设。"""
    root.mkdir(parents=True, exist_ok=True)
    creatable = {"match_rules", "auto_clear_missing", "realtime_watch", "scrape_overrides"}
    row = await LibraryRepository(session).create(
        name=name,
        kind=kind.value,
        root_paths=[str(root)],
        **{k: v for k, v in kw.items() if k in creatable},
    )
    extras = {k: v for k, v in kw.items() if k not in creatable}
    if extras:
        for key, value in extras.items():
            setattr(row, key, value)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def _make_item(session, *, kind: MediaKind, tmdb_id: int, title: str, year: int, **kw):
    item = MediaItem(
        kind=kind.value,
        tmdb_id=tmdb_id,
        title=title,
        original_title=kw.pop("original_title", title),
        year=year,
        **kw,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


def _add_file(session, library, item, path: Path, *, season=0, episode=0, **kw):
    row = LibraryFile(
        library_id=library.id,
        media_item_id=item.id,
        season_number=season,
        episode_number=episode,
        file_path=str(path),
        size_bytes=path.stat().st_size if path.exists() else 0,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
        **kw,
    )
    session.add(row)
    return row


def _touch(path: Path, content: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ===========================================================================
# A. 命名模板：配置 → 真实整理 → 磁盘路径
# ===========================================================================


@pytest.mark.asyncio
async def test_default_templates_produce_legacy_layout(db, tmp_path):
    """不配任何模板时，整理产出的磁盘路径与模板化之前逐字节一致。

    这是升级安全的底线：默认配置下存量库不该突然"全部待整理"。
    """
    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=root, name="剧集库")
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=68035, title="风筝", year=2017)
        _add_file(session, library, item, _touch(root / "raw" / "ep3.mkv"), season=1, episode=3)
        await session.commit()
        library_id = library.id

    summary = await organize_library(library_id)
    assert summary.errors == []
    assert (root / "风筝 (2017)" / "Season 01" / "风筝 (2017) - S01E03.mkv").is_file()


@pytest.mark.asyncio
async def test_global_templates_change_disk_layout(db, tmp_path):
    """改全局模板 → 真实整理 → 磁盘上的目录名与文件名确实按新模板生成。"""
    _apply_setting(
        naming_entry_dir="{title} ({year}) [tmdbid-{tmdb_id}]",
        naming_season_dir="S{season:02d}",
        naming_episode_file="{title}.S{season:02d}E{episode:02d}",
    )
    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=root, name="剧集库")
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=68035, title="风筝", year=2017)
        _add_file(session, library, item, _touch(root / "乱名" / "x.mkv"), season=1, episode=3)
        await session.commit()
        library_id = library.id

    summary = await organize_library(library_id)
    assert summary.errors == []
    target = root / "风筝 (2017) [tmdbid-68035]" / "S01" / "风筝.S01E03.mkv"
    assert target.is_file(), sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    # 台账路径随之更新（不是只改了磁盘）
    from sqlmodel import select

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert [r.file_path for r in rows] == [str(target)]


@pytest.mark.asyncio
async def test_movie_templates_separate_dir_and_file(db, tmp_path):
    """电影：条目目录与文件名各走各的模板（模板化之前两者被迫同名）。"""
    _apply_setting(
        naming_entry_dir="{title} ({year})",
        naming_movie_file="{title}.{year}.{resolution}",
    )
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root, name="电影库")
        item = await _make_item(
            session, kind=MediaKind.MOVIE, tmdb_id=693134, title="沙丘", year=2024
        )
        _add_file(
            session,
            library,
            item,
            _touch(root / "Dune.2024.2160p" / "a.mkv"),
            resolution="2160p",
        )
        await session.commit()
        library_id = library.id

    assert (await organize_library(library_id)).errors == []
    assert (root / "沙丘 (2024)" / "沙丘.2024.2160p.mkv").is_file()


@pytest.mark.asyncio
async def test_library_override_beats_global_on_disk(db, tmp_path):
    """两个库配不同模板 → 各自按自己的模板落盘（库级覆盖真的分库生效）。"""
    _apply_setting(naming_entry_dir="{title} ({year})")
    root_a = tmp_path / "tv_a"
    root_b = tmp_path / "tv_b"
    async with db.session() as session:
        lib_a = await _make_library(session, kind=MediaKind.TV, root=root_a, name="常规剧集库")
        lib_b = await _make_library(
            session,
            kind=MediaKind.TV,
            root=root_b,
            name="动漫库",
            scrape_overrides={
                "naming_entry_dir": "{original_title} ({year})",
                "naming_season_dir": "Season {season:02d} 特别版",
            },
        )
        item = await _make_item(
            session,
            kind=MediaKind.TV,
            tmdb_id=1,
            title="中文名",
            year=2020,
            original_title="Original Name",
        )
        _add_file(session, lib_a, item, _touch(root_a / "raw" / "a.mkv"), season=1, episode=1)
        _add_file(session, lib_b, item, _touch(root_b / "raw" / "b.mkv"), season=1, episode=1)
        await session.commit()
        id_a, id_b = lib_a.id, lib_b.id

    assert (await organize_library(id_a)).errors == []
    assert (await organize_library(id_b)).errors == []
    # A 库跟全局：中文名 + 默认季目录
    assert (root_a / "中文名 (2020)" / "Season 01" / "中文名 (2020) - S01E01.mkv").is_file()
    # B 库用自己的覆盖：原名 + 自定义季目录；文件名没覆盖 → 回落全局/默认
    assert (
        root_b / "Original Name (2020)" / "Season 01 特别版" / "中文名 (2020) - S01E01.mkv"
    ).is_file()


@pytest.mark.asyncio
async def test_template_change_is_idempotent_and_reorganizes(db, tmp_path):
    """改模板 → 整理一次即全部改名；紧接着再整理一次不再产生任何动作。"""
    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=root, name="剧集库")
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=7, title="剧", year=2019)
        _add_file(session, library, item, _touch(root / "raw" / "e1.mkv"), season=2, episode=5)
        await session.commit()
        library_id = library.id

    # 先按默认模板整理到位
    assert (await organize_library(library_id)).renamed == 1
    assert (root / "剧 (2019)" / "Season 02" / "剧 (2019) - S02E05.mkv").is_file()

    # 改模板后：应当再次全部改名
    _apply_setting(naming_episode_file="{title} S{season:02d}E{episode:02d}")
    summary = await organize_library(library_id)
    assert summary.renamed == 1
    assert (root / "剧 (2019)" / "Season 02" / "剧 S02E05.mkv").is_file()

    # 幂等：同一模板再跑不动任何文件
    summary = await organize_library(library_id)
    assert summary.renamed == 0


# --- 反复调整模板时不留垃圾（条目目录级镜像资产随迁）--------------------
#
# 用户会反复改模板试效果，这是本功能的**预期用法**。海报/NFO 不跟着搬的话
# 旧条目目录永远非空、清不掉，每调一次就多留一层只剩图片的空壳目录。


def _mirror_products(entry: Path, *, kind: MediaKind) -> None:
    """在条目目录里造出一份镜像产物（等价于 mirror_media_dir_assets 写完的现场）。"""
    _touch(entry / "poster.jpg", b"POSTER")
    _touch(entry / "fanart.jpg", b"FANART")
    _touch(entry / ("movie.nfo" if kind is MediaKind.MOVIE else "tvshow.nfo"), b"<nfo/>")
    if kind is MediaKind.TV:
        _touch(entry / "season01-poster.jpg", b"S01")
        _touch(entry / "season-specials-poster.jpg", b"SP")


@pytest.mark.asyncio
async def test_entry_assets_follow_entry_dir_rename(db, tmp_path):
    """改条目目录模板 → 海报/背景/季海报/NFO 跟着搬，旧目录被清空清掉。"""
    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=root, name="剧集库")
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=31, title="剧", year=2019)
        _add_file(session, library, item, _touch(root / "raw" / "e1.mkv"), season=1, episode=1)
        await session.commit()
        library_id = library.id

    assert (await organize_library(library_id)).renamed == 1
    old_entry = root / "剧 (2019)"
    _mirror_products(old_entry, kind=MediaKind.TV)

    _apply_setting(naming_entry_dir="{title} ({year}) [tmdbid-{tmdb_id}]")
    summary = await organize_library(library_id)
    new_entry = root / "剧 (2019) [tmdbid-31]"

    # poster / fanart / tvshow.nfo / season01-poster / season-specials-poster
    assert summary.entry_assets_moved == 5
    # 旧目录彻底消失：这正是"反复调模板不留垃圾"的判据
    assert not old_entry.exists()
    assert summary.removed_dirs >= 1
    assert (new_entry / "poster.jpg").read_bytes() == b"POSTER"
    assert (new_entry / "fanart.jpg").read_bytes() == b"FANART"
    assert (new_entry / "tvshow.nfo").read_bytes() == b"<nfo/>"
    assert (new_entry / "season01-poster.jpg").read_bytes() == b"S01"
    assert (new_entry / "season-specials-poster.jpg").read_bytes() == b"SP"
    assert (new_entry / "Season 01" / "剧 (2019) - S01E01.mkv").is_file()

    # 幂等：同模板再跑一次，不再搬任何东西
    again = await organize_library(library_id)
    assert (again.renamed, again.entry_assets_moved) == (0, 0)


@pytest.mark.asyncio
async def test_episode_thumb_follows_rename(db, tmp_path):
    """分集剧照 ``xxx-thumb.jpg`` 随分集文件改名（它不带"主文件名."前缀）。"""
    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=root, name="剧集库")
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=32, title="剧", year=2020)
        _add_file(session, library, item, _touch(root / "raw" / "e1.mkv"), season=1, episode=3)
        await session.commit()
        library_id = library.id

    assert (await organize_library(library_id)).renamed == 1
    season_dir = root / "剧 (2020)" / "Season 01"
    _touch(season_dir / "剧 (2020) - S01E03-thumb.jpg", b"THUMB")
    _touch(season_dir / "剧 (2020) - S01E03.nfo", b"<ep/>")

    _apply_setting(naming_episode_file="{title} S{season:02d}E{episode:02d}")
    summary = await organize_library(library_id)
    assert summary.sidecars_renamed == 2
    assert (season_dir / "剧 S01E03-thumb.jpg").read_bytes() == b"THUMB"
    assert (season_dir / "剧 S01E03.nfo").read_bytes() == b"<ep/>"
    assert not (season_dir / "剧 (2020) - S01E03-thumb.jpg").exists()


@pytest.mark.asyncio
async def test_entry_assets_stay_when_other_video_remains(db, tmp_path):
    """旧目录里还有别的在位视频（不在本次计划里）→ 图留给它们，不搬。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root, name="电影库")
        item = await _make_item(session, kind=MediaKind.MOVIE, tmdb_id=33, title="片", year=2020)
        _add_file(session, library, item, _touch(root / "raw" / "a.mkv"))
        await session.commit()
        library_id = library.id

    assert (await organize_library(library_id)).renamed == 1
    old_entry = root / "片 (2020)"
    _mirror_products(old_entry, kind=MediaKind.MOVIE)
    # 台账不认识的视频（别的作品，或还没扫到）躺在同一个条目目录里
    _touch(old_entry / "另一部片.mkv", b"other")

    _apply_setting(naming_entry_dir="{title}.{year}")
    summary = await organize_library(library_id)
    assert summary.entry_assets_moved == 0
    assert (old_entry / "poster.jpg").is_file()
    assert (root / "片.2020" / "片 (2020).mkv").is_file()


@pytest.mark.asyncio
async def test_duplicate_entry_asset_removed_but_different_one_kept(db, tmp_path):
    """目标已有同名资产：内容相同删掉重复份，内容不同保留并告警。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root, name="电影库")
        item = await _make_item(session, kind=MediaKind.MOVIE, tmdb_id=34, title="片", year=2021)
        _add_file(session, library, item, _touch(root / "raw" / "a.mkv"))
        await session.commit()
        library_id = library.id

    assert (await organize_library(library_id)).renamed == 1
    old_entry = root / "片 (2021)"
    _mirror_products(old_entry, kind=MediaKind.MOVIE)
    # 新目录里预先放好：poster 与旧的逐字节相同，fanart 不同（用户自己换过图）
    new_entry = root / "片.2021"
    _touch(new_entry / "poster.jpg", b"POSTER")
    _touch(new_entry / "fanart.jpg", b"USER-ART")

    _apply_setting(naming_entry_dir="{title}.{year}")
    summary = await organize_library(library_id)

    assert not (old_entry / "poster.jpg").exists()  # 重复副本清掉
    assert (new_entry / "poster.jpg").read_bytes() == b"POSTER"
    assert (old_entry / "fanart.jpg").read_bytes() == b"FANART"  # 内容不同：原样保留
    assert (new_entry / "fanart.jpg").read_bytes() == b"USER-ART"  # 绝不覆盖
    assert any("内容不同" in e for e in summary.errors)
    assert old_entry.exists()  # 还留着那张图，目录自然清不掉（不删用户可能在意的东西）


@pytest.mark.asyncio
async def test_entry_assets_stay_put_when_renames_fail(db, tmp_path, monkeypatch):
    """改名全都失败时资产必须原地不动——否则图被抽走丢进一个空目录。

    计划阶段的守门看的是"计划里会不会搬空"，管不了执行时改名真的失败
    （权限不足、并发占用）；执行前必须按源目录再复核一次。
    """
    import movieclaw_api.services.library.organize as org

    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root, name="电影库")
        item = await _make_item(session, kind=MediaKind.MOVIE, tmdb_id=36, title="片", year=2023)
        _add_file(session, library, item, _touch(root / "raw" / "a.mkv"))
        await session.commit()
        library_id = library.id

    assert (await organize_library(library_id)).renamed == 1
    old_entry = root / "片 (2023)"
    _mirror_products(old_entry, kind=MediaKind.MOVIE)

    def boom(src, dst, *, missing_message):
        raise org._MoveError(f"改名失败：{src}")

    monkeypatch.setattr(org, "_resolve_and_move", boom)
    _apply_setting(naming_entry_dir="{title}.{year}")
    summary = await organize_library(library_id)

    assert (summary.renamed, summary.entry_assets_moved) == (0, 0)
    assert (old_entry / "片 (2023).mkv").is_file()  # 视频还在原地
    assert (old_entry / "poster.jpg").is_file()  # 图就得留在它身边
    assert not (root / "片.2023").exists()  # 不凭空造一个只有图的目录
    assert any("仍有视频文件" in e for e in summary.errors)


def test_stale_asset_plan_is_silent_on_rerun(tmp_path):
    """持久化作业重跑：上一轮已搬完、旧目录已清掉，这一轮应静默无事。

    不能因为"目录不在了"就报「仍有视频文件」——那会让一次成功的整理带着
    一串假问题收尾，用户以为出了错。
    """
    from movieclaw_api.services.library.organize import EntryAssetMove, _move_entry_assets

    gone = tmp_path / "已清掉的旧目录"
    moved, errors = _move_entry_assets(
        [EntryAssetMove(str(gone / "poster.jpg"), str(tmp_path / "新目录" / "poster.jpg"))]
    )
    assert (moved, errors) == (0, [])


@pytest.mark.asyncio
async def test_user_files_in_entry_dir_are_never_touched(db, tmp_path):
    """白名单之外的文件（用户自己放的）一律不搬不删——旧目录因此保留。"""
    root = tmp_path / "movies"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.MOVIE, root=root, name="电影库")
        item = await _make_item(session, kind=MediaKind.MOVIE, tmdb_id=35, title="片", year=2022)
        _add_file(session, library, item, _touch(root / "raw" / "a.mkv"))
        await session.commit()
        library_id = library.id

    assert (await organize_library(library_id)).renamed == 1
    old_entry = root / "片 (2022)"
    _mirror_products(old_entry, kind=MediaKind.MOVIE)
    _touch(old_entry / "我的观影笔记.txt", b"note")

    _apply_setting(naming_entry_dir="{title}.{year}")
    await organize_library(library_id)
    assert (old_entry / "我的观影笔记.txt").read_bytes() == b"note"
    assert not (old_entry / "poster.jpg").exists()  # 镜像产物照常搬走
    assert (root / "片.2022" / "poster.jpg").is_file()


@pytest.mark.asyncio
async def test_sidecars_follow_template_rename(db, tmp_path):
    """字幕等附属文件随新模板一起改名（否则外挂字幕会与视频失联）。"""
    _apply_setting(naming_episode_file="{title}.S{season:02d}E{episode:02d}")
    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=root, name="剧集库")
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=9, title="剧", year=2021)
        video = _touch(root / "raw" / "old.mkv")
        _touch(root / "raw" / "old.zh.srt", b"sub")
        _add_file(session, library, item, video, season=1, episode=2)
        await session.commit()
        library_id = library.id

    summary = await organize_library(library_id)
    assert summary.sidecars_renamed == 1
    entry = root / "剧 (2021)" / "Season 01"
    assert (entry / "剧.S01E02.mkv").is_file()
    assert (entry / "剧.S01E02.zh.srt").read_bytes() == b"sub"


@pytest.mark.asyncio
async def test_save_path_follows_template(db, tmp_path):
    """投递给下载器的 save_path 与整理目标同源：模板改了，投递目录也改。"""
    from movieclaw_api.services.library.config import derive_save_path

    _apply_setting(naming_entry_dir="{title} ({year}) [tmdbid-{tmdb_id}]")
    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=root, name="剧集库")
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=68035, title="风筝", year=2017)

    path = derive_save_path(library, title=item.title, year=item.year, item=item)
    assert path == f"{root}/风筝 (2017) [tmdbid-68035]"
    # 与整理侧算出的目录名逐字相同（命名同源）
    from movieclaw_api.services.library.naming import entry_dir_name_of

    assert Path(path).name == entry_dir_name_of(item, library=library)


# ===========================================================================
# B. 目录写入细分：配置 → 真实镜像 → 媒体目录里到底有哪些文件
# ===========================================================================


async def _prepare_mirror_case(db, tmp_path, *, library_kw=None, entry="风筝 (2017)"):
    """铺一个可镜像的最小现场：库 + 条目 + 档案 + 在位分集文件 + 资产图片。

    资产目录（data/metadata/images/{id}/）里放真图，镜像才有东西可拷。
    identity_source=MANUAL 让 NFO 通过"高置信身份"的门槛。
    """
    from movieclaw_api.services.media_scrape import assets_root
    from movieclaw_db.models import MediaEpisode, MediaMetadata, MediaSeason
    from movieclaw_db.models.library_file import IdentitySource

    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(
            session, kind=MediaKind.TV, root=root, name="剧集库", **(library_kw or {})
        )
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=68035, title="风筝", year=2017)
        session.add(MediaMetadata(media_item_id=item.id, overview="简介"))
        session.add(MediaSeason(media_item_id=item.id, season_number=1, name="第 1 季"))
        session.add(
            MediaEpisode(media_item_id=item.id, season_number=1, episode_number=3, name="第三集")
        )
        video = _touch(root / entry / "Season 01" / "风筝 (2017) - S01E03.mkv")
        _add_file(
            session,
            library,
            item,
            video,
            season=1,
            episode=3,
            identity_source=IdentitySource.MANUAL.value,
        )
        await session.commit()
        item_id = item.id

    # 资产目录：条目图 + 季海报 + 分集剧照
    item_dir = assets_root() / str(item_id)
    item_dir.mkdir(parents=True, exist_ok=True)
    for name in ("poster.jpg", "backdrop.jpg", "season-1.jpg", "s01e03.jpg"):
        (item_dir / name).write_bytes(b"img")
    return item_id, root / entry, root / entry / "Season 01"


@pytest.mark.asyncio
async def test_mirror_all_on_writes_everything(db, tmp_path):
    """三项细项默认全开（= 拆分之前的行为）：图片、NFO、分集剧照都落地。"""
    from movieclaw_api.services.media_scrape import mirror_media_dir_assets

    item_id, entry, season_dir = await _prepare_mirror_case(db, tmp_path)
    await mirror_media_dir_assets(item_id)

    assert (entry / "poster.jpg").is_file()
    assert (entry / "fanart.jpg").is_file()
    assert (entry / "season01-poster.jpg").is_file()
    assert (entry / "tvshow.nfo").is_file()
    assert (season_dir / "风筝 (2017) - S01E03-thumb.jpg").is_file()
    assert (season_dir / "风筝 (2017) - S01E03.nfo").is_file()


@pytest.mark.asyncio
async def test_mirror_images_off_keeps_nfo(db, tmp_path):
    """关掉「条目图片」：图片一张不写，NFO 照写（有人只要我们的 NFO）。"""
    from movieclaw_api.services.media_scrape import mirror_media_dir_assets

    _apply_setting(mirror_images=False)
    item_id, entry, season_dir = await _prepare_mirror_case(db, tmp_path)
    await mirror_media_dir_assets(item_id)

    assert not (entry / "poster.jpg").exists()
    assert not (entry / "fanart.jpg").exists()
    assert not (entry / "season01-poster.jpg").exists()
    assert not (season_dir / "风筝 (2017) - S01E03-thumb.jpg").exists()
    assert (entry / "tvshow.nfo").is_file()
    assert (season_dir / "风筝 (2017) - S01E03.nfo").is_file()


@pytest.mark.asyncio
async def test_mirror_nfo_off_keeps_images(db, tmp_path):
    """关掉「NFO」：图片照写，一个 nfo 都不写（用 Emby 自己刮元数据的玩法）。"""
    from movieclaw_api.services.media_scrape import mirror_media_dir_assets

    _apply_setting(mirror_nfo=False)
    item_id, entry, season_dir = await _prepare_mirror_case(db, tmp_path)
    await mirror_media_dir_assets(item_id)

    assert (entry / "poster.jpg").is_file()
    assert (season_dir / "风筝 (2017) - S01E03-thumb.jpg").is_file()
    assert not (entry / "tvshow.nfo").exists()
    assert not (season_dir / "风筝 (2017) - S01E03.nfo").exists()


@pytest.mark.asyncio
async def test_mirror_episode_thumbs_off_only(db, tmp_path):
    """只关「分集剧照」：条目图片与两级 NFO 都在，唯独 thumb 不写
    （长剧集这是最大的写入量）。"""
    from movieclaw_api.services.media_scrape import mirror_media_dir_assets

    _apply_setting(mirror_episode_thumbs=False)
    item_id, entry, season_dir = await _prepare_mirror_case(db, tmp_path)
    await mirror_media_dir_assets(item_id)

    assert (entry / "poster.jpg").is_file()
    assert (entry / "season01-poster.jpg").is_file()
    assert (entry / "tvshow.nfo").is_file()
    assert (season_dir / "风筝 (2017) - S01E03.nfo").is_file()
    assert not (season_dir / "风筝 (2017) - S01E03-thumb.jpg").exists()


@pytest.mark.asyncio
async def test_library_master_switch_blocks_everything(db, tmp_path):
    """库上的 write_media_assets 是总闸：关掉则三项全不写（细项开着也没用）。"""
    from movieclaw_api.services.media_scrape import mirror_media_dir_assets

    _apply_setting(mirror_images=True, mirror_nfo=True, mirror_episode_thumbs=True)
    item_id, entry, season_dir = await _prepare_mirror_case(
        db, tmp_path, library_kw={"write_media_assets": False}
    )
    await mirror_media_dir_assets(item_id)

    assert not (entry / "poster.jpg").exists()
    assert not (entry / "tvshow.nfo").exists()
    assert not (season_dir / "风筝 (2017) - S01E03-thumb.jpg").exists()


@pytest.mark.asyncio
async def test_library_override_mirror_flags(db, tmp_path):
    """库级覆盖镜像细项：全局开着，这个库单独关掉 NFO。"""
    from movieclaw_api.services.media_scrape import mirror_media_dir_assets

    _apply_setting(mirror_nfo=True)
    item_id, entry, season_dir = await _prepare_mirror_case(
        db, tmp_path, library_kw={"scrape_overrides": {"mirror_nfo": False}}
    )
    await mirror_media_dir_assets(item_id)

    assert (entry / "poster.jpg").is_file()  # 图片跟全局，照写
    assert not (entry / "tvshow.nfo").exists()  # NFO 被该库单独关掉
    assert not (season_dir / "风筝 (2017) - S01E03.nfo").exists()


# ===========================================================================
# C. 刮削链路：配置 → 真实档案解析（MockTransport）→ 落库/选中的值
# ===========================================================================

_MOVIE_ID = 693134


def _images(*, posters=(), backdrops=()):
    def _one(path, lang, width, avg, count):
        return {
            "file_path": path,
            "iso_639_1": lang,
            "width": width,
            "height": int(width * 1.5),
            "vote_average": avg,
            "vote_count": count,
        }

    return {
        "posters": [_one(*p) for p in posters],
        "backdrops": [_one(*b) for b in backdrops],
    }


def _movie_payload(**overrides) -> dict:
    payload = {
        "id": _MOVIE_ID,
        "title": "沙丘2",
        "original_title": "Dune: Part Two",
        "original_language": "en",
        "release_date": "2024-02-27",
        "status": "Released",
        "overview": "中文简介",
        "tagline": "中文标语",
        "poster_path": "/tmdb-default.jpg",
        "backdrop_path": "/tmdb-default-bd.jpg",
        "genres": [{"id": 878, "name": "科幻"}],
        "external_ids": {"imdb_id": "tt15239678"},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
        "credits": {"cast": [], "crew": []},
        "release_dates": {"results": []},
        "images": _images(),
    }
    payload.update(overrides)
    return payload


def _mock_client(payload: dict, captured: list | None = None, extra_routes: dict | None = None):
    """按 path 返回固定 JSON 的假 TMDB（不出网）。"""
    import httpx

    from movieclaw_media.tmdb import TmdbClient

    routes = {f"/3/movie/{_MOVIE_ID}": payload, **(extra_routes or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404, json={})

    return TmdbClient("0123456789abcdef0123456789abcdef", transport=httpx.MockTransport(handler))


async def _fetch(payload, **setting_fields):
    """按给定配置跑一次真实的档案解析，返回 MediaProfile。"""
    from movieclaw_api.services.scrape_config import profile_fetch_kwargs
    from movieclaw_media.library import fetch_media_profile

    _apply_setting(**setting_fields)
    client = _mock_client(payload)
    return await fetch_media_profile(client, MediaKind.MOVIE, _MOVIE_ID, **profile_fetch_kwargs())


@pytest.mark.asyncio
async def test_poster_mode_default_keeps_tmdb_default(db):
    """poster_mode=default（默认配置）：以 TMDB 默认海报为准，
    即便候选里有"更符合口味"的图也不跳变（订阅前后一致）。"""
    payload = _movie_payload(
        images=_images(posters=[("/zh-poster.jpg", "zh", 1000, 7.0, 50)]),
    )
    profile = await _fetch(payload)
    assert profile.poster_path == "/tmdb-default.jpg"


@pytest.mark.asyncio
async def test_poster_mode_language_picks_by_priority(db):
    """poster_mode=language：按语言优先级挑，第一档有候选就用它。"""
    payload = _movie_payload(
        images=_images(
            posters=[
                ("/en-poster.jpg", "en", 2000, 9.5, 900),  # 分更高
                ("/zh-poster.jpg", "zh", 1000, 6.0, 40),
            ]
        ),
    )
    # 中文优先 → 选中文版（尽管英文版加权分更高）
    profile = await _fetch(payload, poster_mode="language", poster_language_priority=["meta", "en"])
    assert profile.poster_path == "/zh-poster.jpg"

    # 把 en 排到前面 → 改选英文版（同一份候选，只有配置变了）
    profile = await _fetch(payload, poster_mode="language", poster_language_priority=["en", "meta"])
    assert profile.poster_path == "/en-poster.jpg"


@pytest.mark.asyncio
async def test_poster_original_language_token(db):
    """「原始语言」按条目的 original_language 动态解析（这里是 en）。"""
    payload = _movie_payload(
        original_language="ko",
        images=_images(
            posters=[
                ("/ko-poster.jpg", "ko", 1000, 5.0, 10),
                ("/en-poster.jpg", "en", 2000, 9.0, 800),
            ]
        ),
    )
    profile = await _fetch(payload, poster_mode="language", poster_language_priority=["orig", "en"])
    assert profile.poster_path == "/ko-poster.jpg"


@pytest.mark.asyncio
async def test_backdrop_textless_first_by_default(db):
    """背景默认「无文字优先」：干净图分更低也压过带字图（现状行为）。"""
    payload = _movie_payload(
        images=_images(
            backdrops=[
                ("/logo-en.jpg", "en", 3840, 9.5, 900),
                ("/clean.jpg", None, 1920, 6.0, 30),
            ]
        ),
    )
    profile = await _fetch(payload)
    assert profile.backdrop_path == "/clean.jpg"


@pytest.mark.asyncio
async def test_backdrop_language_first_when_configured(db):
    """把语言排到「无文字」前面 → 改选带片名 logo 的横图（收藏口味）。"""
    payload = _movie_payload(
        images=_images(
            backdrops=[
                ("/logo-en.jpg", "en", 1920, 6.0, 40),
                ("/clean.jpg", None, 3840, 9.0, 500),
            ]
        ),
    )
    profile = await _fetch(payload, backdrop_language_priority=["en", "null"])
    assert profile.backdrop_path == "/logo-en.jpg"


@pytest.mark.asyncio
async def test_min_width_threshold_filters_small_images(db):
    """分辨率门槛真的过滤候选：调高后小图被排除，调到 0 则不设限。"""
    payload = _movie_payload(
        images=_images(
            posters=[
                ("/small-but-loved.jpg", "zh", 500, 9.9, 900),
                ("/big.jpg", "zh", 2000, 6.0, 50),
            ]
        ),
    )
    # 门槛 1000：小图被过滤，只剩大图
    profile = await _fetch(
        payload,
        poster_mode="language",
        poster_language_priority=["meta"],
        poster_min_width=1000,
    )
    assert profile.poster_path == "/big.jpg"

    # 门槛 0：不设限，加权分最高的小图胜出
    profile = await _fetch(
        payload,
        poster_mode="language",
        poster_language_priority=["meta"],
        poster_min_width=0,
    )
    assert profile.poster_path == "/small-but-loved.jpg"


@pytest.mark.asyncio
async def test_language_priority_changes_request_and_fallback(db):
    """语言优先级：首位决定请求语言；主语言缺失的字段从 translations 本地
    回落（**不产生额外请求**）。"""
    payload = _movie_payload(
        title="Dune: Part Two",  # 无 zh 翻译时 TMDB 静默退回原名
        overview="",
        tagline="",
        translations={
            "translations": [
                {
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "data": {
                        "title": "Dune: Part Two",
                        "overview": "EN overview",
                        "tagline": "EN tagline",
                    },
                }
            ]
        },
    )
    from movieclaw_api.services.scrape_config import profile_fetch_kwargs
    from movieclaw_media.library import fetch_media_profile

    _apply_setting(language_priority=["zh-CN", "en-US"])
    captured: list = []
    client = _mock_client(payload, captured)
    profile = await fetch_media_profile(
        client, MediaKind.MOVIE, _MOVIE_ID, **profile_fetch_kwargs()
    )
    assert profile.overview == "EN overview"
    assert profile.tagline == "EN tagline"
    assert len(captured) == 1, "回落必须走同一次请求已拉回的 translations"
    assert dict(captured[0].url.params)["language"] == "zh-CN"

    # 首位换成日语 → 请求语言随之改变
    _apply_setting(language_priority=["ja-JP", "en-US"])
    captured.clear()
    client = _mock_client(payload, captured)
    await fetch_media_profile(client, MediaKind.MOVIE, _MOVIE_ID, **profile_fetch_kwargs())
    assert dict(captured[0].url.params)["language"] == "ja-JP"


@pytest.mark.asyncio
async def test_cert_country_priority_changes_rating(db):
    """分级地区优先级：同一份档案，按序取第一个有数据的地区。"""
    payload = _movie_payload(
        release_dates={
            "results": [
                {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
                {"iso_3166_1": "JP", "release_dates": [{"certification": "G"}]},
            ]
        },
    )
    assert (await _fetch(payload, cert_country_priority=["US", "JP"])).content_rating == "PG-13"
    assert (await _fetch(payload, cert_country_priority=["JP", "US"])).content_rating == "G"
    # 配的地区都没数据 → 取第一个非空（不至于丢掉分级）
    assert (await _fetch(payload, cert_country_priority=["CN"])).content_rating in ("PG-13", "G")


@pytest.mark.asyncio
async def test_include_image_language_derived_from_prefs(db):
    """候选图请求的 include_image_language 由选图偏好推导，而非写死。"""
    from movieclaw_api.services.scrape_config import profile_fetch_kwargs
    from movieclaw_media.library import fetch_media_profile

    _apply_setting(
        language_priority=["zh-CN"],
        poster_language_priority=["meta", "ja", "null"],
        backdrop_language_priority=["null", "ko"],
    )
    captured: list = []
    client = _mock_client(_movie_payload(), captured)
    await fetch_media_profile(client, MediaKind.MOVIE, _MOVIE_ID, **profile_fetch_kwargs())
    langs = dict(captured[0].url.params)["include_image_language"].split(",")
    assert "null" in langs and "zh" in langs  # 无文字 + 主语言
    assert "ja" in langs and "ko" in langs  # 偏好里出现的具体语种


# ===========================================================================
# D. 图片档位与资产落盘：配置 → 真实下载请求 URL → 资产文件
# ===========================================================================


@pytest.mark.asyncio
async def test_asset_size_config_changes_download_url(db, tmp_path, monkeypatch):
    """图片档位改了 → 真实下载走的 URL 用新档位（不是只改了显示）。"""
    from movieclaw_api.services.media_scrape import assets_root, download_item_assets
    from movieclaw_db.models import MediaMetadata

    _apply_setting(poster_size="w342", backdrop_size="w780", still_size="w185")
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=tmp_path / "tv", name="库")
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=5, title="剧", year=2020)
        item.poster_path = "/p.jpg"
        item.backdrop_path = "/b.jpg"
        session.add(item)
        session.add(MediaMetadata(media_item_id=item.id))
        _add_file(
            session,
            library,
            item,
            _touch(tmp_path / "tv" / "剧 (2020)" / "x.mkv"),
            season=1,
            episode=1,
        )
        await session.commit()
        item_id = item.id

    urls: list[str] = []

    class _FakeProxy:
        async def fetch(self, url: str):
            urls.append(url)
            return b"img", "image/jpeg"

    monkeypatch.setattr("movieclaw_api.services.image_proxy.get_image_proxy", lambda: _FakeProxy())
    await download_item_assets(item_id)

    assert any(u.endswith("/w342/p.jpg") for u in urls), urls
    assert any(u.endswith("/w780/b.jpg") for u in urls), urls
    # 资产真的落盘了
    assert (assets_root() / str(item_id) / "poster.jpg").is_file()

    # 换档位后 force 重下：溯源记录发现来源变了，URL 跟着变
    _apply_setting(poster_size="w780", backdrop_size="original", still_size="w300")
    urls.clear()
    await download_item_assets(item_id)
    assert any(u.endswith("/w780/p.jpg") for u in urls), urls
    assert any(u.endswith("/original/b.jpg") for u in urls), urls


@pytest.mark.asyncio
async def test_asset_sizes_follow_env_when_unset(db, tmp_path, monkeypatch):
    """档位留空 = 跟随环境变量（老部署只配 env 的行为完全不变）。"""
    from movieclaw_api.services.scrape_config import effective_asset_sizes

    _apply_setting()  # 全默认（三个档位都是空串）
    assert effective_asset_sizes() == ("w780", "original", "w300")

    monkeypatch.setenv("TMDB_POSTER_SIZE", "w500")
    get_settings.cache_clear()
    assert effective_asset_sizes()[0] == "w500"
    # 设置页显式配了值 → 压过 env
    _apply_setting(poster_size="w342")
    assert effective_asset_sizes()[0] == "w342"
    get_settings.cache_clear()


# ===========================================================================
# E. 防假通过：配置**没配**时不得意外改变行为
# ===========================================================================


@pytest.mark.asyncio
async def test_unset_overrides_do_not_leak_between_libraries(db, tmp_path):
    """一个库配了覆盖，不能影响另一个库——库级覆盖必须真的按库隔离。"""
    from movieclaw_api.services.scrape_config import effective_naming_templates

    _apply_setting(naming_entry_dir="{title} 全局")
    async with db.session() as session:
        lib_a = await _make_library(
            session,
            kind=MediaKind.TV,
            root=tmp_path / "a",
            name="A",
            scrape_overrides={"naming_entry_dir": "{title} 覆盖"},
        )
        lib_b = await _make_library(session, kind=MediaKind.TV, root=tmp_path / "b", name="B")

    assert effective_naming_templates(lib_a).entry_dir == "{title} 覆盖"
    assert effective_naming_templates(lib_b).entry_dir == "{title} 全局"
    assert effective_naming_templates(None).entry_dir == "{title} 全局"


@pytest.mark.asyncio
async def test_dirty_override_falls_back_to_global_without_crashing(db, tmp_path):
    """库行里的脏覆盖（手改数据库/旧版本残留）不能拖垮整理：退回全局。"""
    from movieclaw_api.services.scrape_config import effective_naming_templates

    _apply_setting(naming_entry_dir="{title} ({year})")
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=tmp_path / "d", name="脏库")
        # 绕过保存校验直接写脏值（模拟手改 DB）
        library.scrape_overrides = {"naming_entry_dir": "{title}/{year}"}
        session.add(library)
        await session.commit()
        await session.refresh(library)

    assert effective_naming_templates(library).entry_dir == "{title} ({year})"


@pytest.mark.asyncio
async def test_dirty_override_still_organizes_with_global_template(db, tmp_path):
    """接上一条：脏覆盖下整理仍能跑完，落盘用全局模板（而不是报错中断）。"""
    root = tmp_path / "tv"
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=root, name="脏库")
        library.scrape_overrides = {"naming_episode_file": "{title}"}  # 缺季集号，非法
        session.add(library)
        item = await _make_item(session, kind=MediaKind.TV, tmdb_id=11, title="剧", year=2022)
        _add_file(session, library, item, _touch(root / "raw" / "a.mkv"), season=1, episode=4)
        await session.commit()
        library_id = library.id

    summary = await organize_library(library_id)
    assert summary.errors == []
    assert (root / "剧 (2022)" / "Season 01" / "剧 (2022) - S01E04.mkv").is_file()


@pytest.mark.asyncio
async def test_non_overridable_field_in_row_is_ignored(db, tmp_path):
    """库行里残留了不可覆盖的字段（如选图）时静默忽略，不影响全局选图。"""
    from movieclaw_api.services.scrape_config import effective_image_prefs, merge_for_library

    _apply_setting(poster_mode="default")
    async with db.session() as session:
        library = await _make_library(session, kind=MediaKind.TV, root=tmp_path / "x", name="X")
        library.scrape_overrides = {"poster_mode": "language", "naming_season_dir": "S{season:02d}"}
        session.add(library)
        await session.commit()
        await session.refresh(library)

    # 选图字段被忽略（跨库共享一份，不允许按库改）
    assert merge_for_library(library).poster_mode == "default"
    assert effective_image_prefs().poster_mode == "default"
    # 同一份覆盖里合法的命名字段照常生效
    from movieclaw_api.services.scrape_config import effective_naming_templates

    assert effective_naming_templates(library).season_dir == "S{season:02d}"
