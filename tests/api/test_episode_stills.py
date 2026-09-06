"""分集剧照抓帧兜底（docs/design/metadata.md 6.1）。

TMDB 没给剧照的在库分集从视频抓一帧写进同一资产位；TMDB 有剧照时始终
用 TMDB 的，后来补了图刷新即覆盖。抓帧本身（ffmpeg）在文件末尾单独用
真 ffmpeg 验证（环境没有时跳过），链路用例把它打成桩。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library import thumbs
from movieclaw_api.services.library.items import build_season_episodes
from movieclaw_api.services.media_scrape import assets_root, download_item_assets
from movieclaw_api.services.scrape_config import reset_scrape_config
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
)
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'stills.db'}")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()
    reset_scrape_config()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    reset_scrape_config()
    get_settings.cache_clear()


class _FakeGrab:
    """替身抓帧：记下调用并写一张假图，返回尺寸。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path, int | None, str | None]] = []

    def __call__(self, video, dest, *, duration_seconds, hdr=None):
        self.calls.append((video, dest, duration_seconds, hdr))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"frame")
        return (640, 360)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return path


async def _seed_show(session, root: Path, *, tmdb_id: int = 5, generate_thumbnails: bool = True):
    """一部三集剧：E1 TMDB 有剧照、E2/E3 没有；E1/E2 在库、E3 缺集。"""
    library = await LibraryRepository(session).create(
        name=f"剧集库 {tmdb_id}",
        kind="tv",
        root_paths=[str(root)],
        generate_thumbnails=generate_thumbnails,
    )
    item = MediaItem(kind="tv", tmdb_id=tmdb_id, title="剧", original_title="Show", year=2020)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    session.add(MediaMetadata(media_item_id=item.id))
    for number, still in ((1, "/s1e1.jpg"), (2, None), (3, None)):
        session.add(
            MediaEpisode(
                media_item_id=item.id, season_number=1, episode_number=number, still_path=still
            )
        )
    files = {}
    for number in (1, 2):
        path = _touch(root / "剧 (2020)" / "Season 01" / f"剧.S01E{number:02d}.mkv")
        row = LibraryFile(
            library_id=library.id,
            media_item_id=item.id,
            season_number=1,
            episode_number=number,
            file_path=str(path),
            size_bytes=5,
            source=FileSource.SCANNED,
            state=FileState.IN_PLACE,
            duration_seconds=1200,
            hdr="HDR10" if number == 2 else None,
        )
        session.add(row)
        files[number] = path
    await session.commit()
    return library, item, files


async def _episode(session, item_id: int, number: int) -> MediaEpisode:
    return (
        await session.execute(
            select(MediaEpisode).where(
                MediaEpisode.media_item_id == item_id, MediaEpisode.episode_number == number
            )
        )
    ).scalar_one()


async def test_grabs_frame_only_for_episodes_without_tmdb_still(db, tmp_path, monkeypatch) -> None:
    """E2（TMDB 无剧照、在库）抓帧落资产位；E1（TMDB 有图但图床离线）不抓，
    留给图床自愈；E3（缺集）没文件可抓。抓帧结果直接被分集区消费。"""
    grab = _FakeGrab()
    monkeypatch.setattr(thumbs, "build_episode_still", grab)
    async with db.session() as session:
        library, item, files = await _seed_show(session, tmp_path / "tv")
        item_id = item.id

    await download_item_assets(item_id)  # conftest 默认图床离线：E1 下载失败

    assert [(c[0], c[2], c[3]) for c in grab.calls] == [(files[2], 1200, "HDR10")]
    item_dir = assets_root() / str(item_id)
    assert grab.calls[0][1] == item_dir / "s01e02.jpg"
    async with db.session() as session:
        assert (await _episode(session, item_id, 1)).still_file is None
        assert (await _episode(session, item_id, 2)).still_file == f"{item_id}/s01e02.jpg"
        assert (await _episode(session, item_id, 3)).still_file is None
    sources = json.loads((item_dir / "sources.json").read_text(encoding="utf-8"))
    assert sources["s01e02"] == f"frame:{files[2]}"

    # 分集区拿到的就是这张资产（带版本戳），而不是集号占位
    async with db.session() as session:
        item = await session.get(MediaItem, item_id)
        rows = (await session.execute(select(LibraryFile))).scalars().all()
        infos = {e.episode_number: e for e in await build_season_episodes(session, item, rows, 1)}
    assert infos[2].still_url and infos[2].still_url.startswith(
        f"/images/assets/{item_id}/s01e02.jpg?v="
    )
    assert infos[3].still_url is None

    # 幂等：资产已在，再跑不重复抓
    await download_item_assets(item_id)
    assert len(grab.calls) == 1

    # TMDB 后来补了 E2 的剧照：溯源对不上 → 重下覆盖抓帧，TMDB 优先
    async with db.session() as session:
        ep2 = await _episode(session, item_id, 2)
        ep2.still_path = "/s1e2.jpg"
        session.add(ep2)
        await session.commit()

    class _Proxy:
        async def fetch(self, url: str):
            return b"tmdb-still", "image/jpeg"

    monkeypatch.setattr("movieclaw_api.services.image_proxy.get_image_proxy", lambda: _Proxy())
    await download_item_assets(item_id)
    assert (item_dir / "s01e02.jpg").read_bytes() == b"tmdb-still"
    sources = json.loads((item_dir / "sources.json").read_text(encoding="utf-8"))
    assert sources["s01e02"] == "w300/s1e2.jpg"
    assert len(grab.calls) == 1  # 有 TMDB 剧照的集不再抓帧
    # E1 的剧照这轮也下到了（图床恢复即自愈）
    async with db.session() as session:
        assert (await _episode(session, item_id, 1)).still_file == f"{item_id}/s01e01.jpg"


async def test_grab_skips_sidecar_disc_and_disabled_library(db, tmp_path, monkeypatch) -> None:
    """视频旁已有 -thumb.jpg 不抓；原盘目录不抓；库关了抓帧开关不抓。"""
    grab = _FakeGrab()
    monkeypatch.setattr(thumbs, "build_episode_still", grab)

    async with db.session() as session:
        _library, item, files = await _seed_show(session, tmp_path / "tv")
        item_id = item.id
        files[2].with_name(files[2].stem + "-thumb.jpg").write_bytes(b"user thumb")
    await download_item_assets(item_id)
    assert grab.calls == []  # E2 有 sidecar
    async with db.session() as session:
        assert (await _episode(session, item_id, 2)).still_file is None

    async with db.session() as session:
        files[2].with_name(files[2].stem + "-thumb.jpg").unlink()
        row = (
            await session.execute(select(LibraryFile).where(LibraryFile.episode_number == 2))
        ).scalar_one()
        row.container = "bluray"
        session.add(row)
        await session.commit()
    await download_item_assets(item_id)
    assert grab.calls == []  # 原盘

    async with db.session() as session:
        _library, item2, _files = await _seed_show(
            session, tmp_path / "tv-off", tmdb_id=6, generate_thumbnails=False
        )
        item2_id = item2.id
    await download_item_assets(item2_id)
    assert grab.calls == []  # 库开关关闭


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要系统 ffmpeg")
def test_build_episode_still_grabs_frame(tmp_path) -> None:
    """真 ffmpeg：抓到帧、宽不超过 640；旁边的剧海报 sidecar 不会被误当剧照。"""
    from PIL import Image

    video = tmp_path / "ep.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=1280x720:rate=10",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(video),
        ],
        check=True,
        timeout=60,
    )
    Image.new("RGB", (200, 300), "gray").save(tmp_path / "poster.jpg")
    dest = tmp_path / "assets" / "1" / "s01e01.jpg"
    assert thumbs.build_episode_still(video, dest, duration_seconds=3) == (640, 360)
    assert dest.is_file() and dest.stat().st_size > 0
