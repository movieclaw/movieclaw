"""批量搬运预检：执行前把「将要发生什么」一次算清，而且不许算错。

这里的每个用例都对应一个「算错了就会让用户吃亏」的场景：
把同盘搬运说成要占 30T 新空间（吓停一次零风险操作）、把 bind mount 的
跨盘说成同盘（搬到一半盘满）、把「同一部片的另一个版本」和「碰巧重名的
另一部片」混为一谈（处理方式完全相反）、以及全硬链接库跨盘搬时谎报源盘
会腾出空间。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library import preflight as pf
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, Library, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'preflight.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _make_entry(root: Path, name: str, size: int = 100) -> Path:
    """造一个条目目录：一个视频 + 一个字幕（字幕不参与硬链接统计）。"""
    entry = root / name
    entry.mkdir(parents=True)
    video = entry / f"{name}.mkv"
    video.write_bytes(b"x" * size)
    (entry / f"{name}.zh.srt").write_text("sub", encoding="utf-8")
    return video


async def _setup(db, tmp_path, *, entries: list[str]):
    """一个源库（电影）+ 一个目标根；每个条目一行台账。"""
    source_root = tmp_path / "src"
    target_root = tmp_path / "dst"
    source_root.mkdir(parents=True)
    target_root.mkdir(parents=True)

    members: list[tuple[int, str]] = []
    async with db.session() as session:
        repo = LibraryRepository(session)
        source = await repo.create(name="源库", kind="movie", root_paths=[str(source_root)])
        target = await repo.create(name="目标库", kind="movie", root_paths=[str(target_root)])
        assert source.id and target.id
        for index, name in enumerate(entries, start=1):
            video = _make_entry(source_root, name)
            item = MediaItem(kind="movie", tmdb_id=1000 + index, title=name, original_title=name)
            session.add(item)
            await session.flush()
            assert item.id
            session.add(
                LibraryFile(
                    library_id=source.id,
                    media_item_id=item.id,
                    file_path=str(video),
                    size_bytes=video.stat().st_size,
                    source=FileSource.SCANNED,
                )
            )
            members.append((item.id, name))
        await session.commit()
        return source.id, target.id, source_root, target_root, members


async def _run(db, source_id: int, target_root: Path, members, **kwargs) -> pf.Preflight:
    async with get_database().session() as session:
        source = await session.get(Library, source_id)
        assert source is not None
        return await pf.build_preflight(session, source, target_root, members, **kwargs)


# ---------------------------------------------------------------------------
# 同挂载探针
# ---------------------------------------------------------------------------


def test_probe_reports_same_mount_within_one_filesystem(tmp_path) -> None:
    """同一个文件系统内 rename 能成——这正是「同盘拍平目录是瞬间完成」的依据。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert pf.probe_same_mount(a, b) is True
    # 探针必须自己收尾，不能在两边留下垃圾
    assert list(a.iterdir()) == []
    assert list(b.iterdir()) == []


def test_probe_is_conservative_when_target_unusable(tmp_path) -> None:
    """探不通就判跨盘：宁可多报要复制，也不能把要复制说成秒完成。"""
    a = tmp_path / "a"
    a.mkdir()
    assert pf.probe_same_mount(a, tmp_path / "不存在") is False


# ---------------------------------------------------------------------------
# 空间：只算跨盘部分
# ---------------------------------------------------------------------------


async def test_same_disk_move_requires_no_extra_space(db, tmp_path) -> None:
    """同盘搬运是一次 rename，不占任何新空间。

    把总体积拿去和剩余空间比是错的——会把一次本来零风险的同盘归并吓停。
    """
    source_id, _, _, target_root, members = await _setup(db, tmp_path, entries=["甲 (2020)"])
    result = await _run(db, source_id, target_root, members)

    assert result.movable == 1
    assert result.cross_device_items == 0
    assert result.cross_device_bytes == 0
    assert result.target_required_bytes == 0
    assert result.blocked == []


async def test_same_disk_move_keeps_hardlinks_and_reports_nothing(db, tmp_path) -> None:
    """全硬链接库同盘搬：链接完整保留，不该在预检里报「210 部有硬链接」吓人。"""
    source_id, _, source_root, target_root, members = await _setup(
        db, tmp_path, entries=["甲 (2020)"]
    )
    seed_dir = tmp_path / "downloads"
    seed_dir.mkdir()
    video = source_root / "甲 (2020)" / "甲 (2020).mkv"
    os.link(video, seed_dir / "seed.mkv")  # 模拟做种目录的硬链接
    assert video.stat().st_nlink == 2

    result = await _run(db, source_id, target_root, members)
    assert result.cross_device_items == 0
    assert result.hardlinked_items == 0
    assert result.hardlinked_bytes == 0


async def test_cross_device_counts_hardlinks_and_reclaimable_space(
    db, tmp_path, monkeypatch
) -> None:
    """跨盘搬全硬链接库：目标盘要吃全量，源盘一字节都不释放。

    用户最容易误解的一点——以为搬完源盘腾出 30T，实际下载目录还引用着，
    「删源」只删掉一个链接。source_reclaimable_bytes 就是为了说清这件事。
    """
    source_id, _, source_root, target_root, members = await _setup(
        db, tmp_path, entries=["甲 (2020)"]
    )
    seed_dir = tmp_path / "downloads"
    seed_dir.mkdir()
    os.link(source_root / "甲 (2020)" / "甲 (2020).mkv", seed_dir / "seed.mkv")
    monkeypatch.setattr(pf, "probe_same_mount", lambda *_: False)

    result = await _run(db, source_id, target_root, members)
    assert result.cross_device_items == 1
    assert result.hardlinked_items == 1
    assert result.hardlinked_bytes == 100
    # 全硬链接 → 源盘可释放为 0，而目标盘仍要吃下全量（含余量下限）
    assert result.source_reclaimable_bytes == 0
    assert result.target_required_bytes == pf.headroom_for(result.cross_device_bytes)
    assert result.target_required_bytes > result.cross_device_bytes


async def test_blocked_when_target_disk_is_short(db, tmp_path, monkeypatch) -> None:
    """空间不够是整批阻断，不是逐条跳过——没有哪一条能在满盘上搬成功。"""
    source_id, _, _, target_root, members = await _setup(db, tmp_path, entries=["甲 (2020)"])
    monkeypatch.setattr(pf, "probe_same_mount", lambda *_: False)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _: shutil._ntuple_diskusage(100, 100, 1),  # type: ignore[attr-defined]
    )

    result = await _run(db, source_id, target_root, members)
    assert any("空间不足" in item for item in result.blocked)


# ---------------------------------------------------------------------------
# 冲突：按锚分类，不按目录名
# ---------------------------------------------------------------------------


async def test_conflicts_are_classified_by_anchor_not_by_name(db, tmp_path) -> None:
    """同名的三种情况处理方式完全不同，预检必须分开报。

    - 同一部作品的其他版本 → 可以合并；
    - 目录撞名但不是同一部片 → 只能跳过；
    - 目标目录没有台账行（用户手放的、正在下载的）→ 身份不明，只能跳过。
    """
    source_id, target_id, _, target_root, members = await _setup(
        db, tmp_path, entries=["同锚 (2020)", "异锚 (2021)", "无主 (2022)"]
    )
    same_id, other_id, _orphan_id = (mid for mid, _ in members)

    # 目标根下先造出三个同名目录，并给前两个安上不同归属的台账行
    async with get_database().session() as session:
        for name in ("同锚 (2020)", "异锚 (2021)", "无主 (2022)"):
            _make_entry(target_root, name)
        stranger = MediaItem(
            kind="movie", tmdb_id=90001, title="另一部碰巧同名的片", original_title="Other"
        )
        session.add(stranger)
        await session.flush()
        assert stranger.id
        session.add(
            LibraryFile(
                library_id=target_id,
                media_item_id=same_id,  # 同一部作品的另一个版本
                file_path=str(target_root / "同锚 (2020)" / "同锚 (2020).mkv"),
                size_bytes=100,
                source=FileSource.SCANNED,
            )
        )
        session.add(
            LibraryFile(
                library_id=target_id,
                media_item_id=stranger.id,  # 完全不同的作品，只是目录重名
                file_path=str(target_root / "异锚 (2021)" / "异锚 (2021).mkv"),
                size_bytes=100,
                source=FileSource.SCANNED,
            )
        )
        await session.commit()

    result = await _run(db, source_id, target_root, members)

    by_id = {m.media_item_id: m for m in result.members}
    assert by_id[same_id].conflict == pf.CONFLICT_SAME_ANCHOR
    assert by_id[other_id].conflict == pf.CONFLICT_DIFFERENT_ANCHOR
    assert by_id[_orphan_id].conflict == pf.CONFLICT_UNKNOWN
    assert result.movable == 0
    assert result.mergeable_conflicts == 1
    # 冲突不是阻断：其余成员照搬，整批不停
    assert result.blocked == []


async def test_seeding_in_place_is_unknown_when_downloader_unreachable(db, tmp_path) -> None:
    """下载器连不上时如实报「无法确认」，不能报 0 让用户以为查过了。"""
    source_id, _, _, target_root, members = await _setup(db, tmp_path, entries=["甲 (2020)"])

    silent = await _run(db, source_id, target_root, members, seeding_names=None)
    assert silent.seeding_in_place_items is None

    hit = await _run(db, source_id, target_root, members, seeding_names={"甲 (2020)"})
    assert hit.seeding_in_place_items == 1
    assert hit.members[0].seeding_in_place is True
