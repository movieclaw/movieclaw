"""「图片」类型媒体库（docs/design/library-photo-kind.md）的回归测试。

覆盖的硬约束：
- 能力档案多一行 ``("photo","local")``，新增能力位 ``media_exts`` /
  ``playable`` / ``jellyfin_exposed``，建库默认本地来源；
- 扫描图片目录：**一文件一条目**、零 ffprobe 子进程、零 TMDB 请求，EXIF
  拍摄时间成为 ``release_date``，方向标签为旋转时尺寸对调，相册工具的
  缩略图目录 / sample / 视频文件都不入账；``audio_streams`` 是空列表而非
  NULL（否则对账补探会无限重探）；
- 影视库目录里的 jpg 不入账（它们是海报 sidecar）；
- 缩略图走 Pillow：纠正方向、长边 ≤720、透明图合成到底色、不起 ffmpeg；
- 列表按内容时间倒序、同日按文件名；月份索引与分页同口径；
- 原图路由按台账行推导路径、按库可见性鉴权，非图片文件 404；
- 实时监听按库的扩展名口径判定事件；
- Jellyfin 库视图与最新媒体不含图片库。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import select

import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.library.thumbs as thumbs_mod
import movieclaw_api.services.media_discover as discover_mod
import movieclaw_api.services.media_probe as probe_mod
import movieclaw_api.services.media_scrape as scrape_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.library import LibraryView
from movieclaw_api.services import jobs
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.library import items as items_mod
from movieclaw_api.services.library.config import LibraryConfigService
from movieclaw_api.services.library.layout import IMAGE_EXTS, SCAN_VIDEO_EXTS
from movieclaw_api.services.library.profile import (
    capabilities_of,
    jellyfin_hidden_kinds,
    library_kind_options,
    profile_for,
)
from movieclaw_api.services.library.scan import scan_library
from movieclaw_api.services.library.thumbs import (
    build_backdrop,
    build_thumbnail,
    ensure_local_assets,
)
from movieclaw_api.services.library.watch import _is_relevant_event
from movieclaw_api.services.media_probe import probe_image, probe_media
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import Library, LibraryFile, MediaItem, MediaMetadata, MediaSource
from movieclaw_db.models.library_file import IdentitySource
from movieclaw_jellyfin import catalog as jf_catalog
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_EXIF_ORIENTATION = 0x0112
_EXIF_IFD = 0x8769
_EXIF_DATETIME_ORIGINAL = 0x9003


def _save_jpeg(
    path: Path, size: tuple[int, int], *, taken: str | None = None, orientation: int | None = None
) -> None:
    """写一张带 EXIF 的 JPEG：拍摄时间进 Exif 子 IFD，方向进主 IFD（相机的写法）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, (120, 80, 40))
    exif = Image.Exif()
    if orientation is not None:
        exif[_EXIF_ORIENTATION] = orientation
    if taken is not None:
        exif.get_ifd(_EXIF_IFD)[_EXIF_DATETIME_ORIGINAL] = taken
    img.save(path, exif=exif)


class _NoTmdb:
    calls: list[str] = []


def _strict_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        _NoTmdb.calls.append(str(request.url))
        raise AssertionError(f"图片库不该请求 TMDB：{request.url}")

    return TmdbClient("test-key", transport=httpx.MockTransport(handler))


def _no_subprocess(*_args, **_kwargs):
    raise AssertionError("图片探测/缩略图不该起 ffprobe/ffmpeg 子进程")


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'photo.db'}")
    monkeypatch.setenv("MOVIECLAW_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    _NoTmdb.calls.clear()
    client = _strict_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    # 图片库全程不得起 ffprobe：探测走 Pillow
    monkeypatch.setattr(probe_mod.subprocess, "run", _no_subprocess)
    # 扫描收尾的资产阶段单独测（ensure_local_assets 用例）
    monkeypatch.setattr(scrape_mod, "ensure_assets", _noop_assets)
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


async def _noop_assets(_media_item_id: int, **_kwargs) -> None:
    return None


def _make_photo_root(tmp_path: Path) -> Path:
    """相册样本：带 EXIF 的竖拍 JPEG、透明 PNG、WebP，外加相册工具目录 / sample / 视频干扰。"""
    root = tmp_path / "media" / "photos"
    # 相机竖拍：像素 4:3 横放 + 方向标签 6 → 显示尺寸 300x400
    _save_jpeg(
        root / "2024" / "IMG_0001.jpg", (400, 300), taken="2024:05:01 10:20:30", orientation=6
    )
    _save_jpeg(root / "2024" / "IMG_0002.jpg", (400, 300), taken="2024:05:01 09:00:00")
    Image.new("RGBA", (100, 50), (255, 0, 0, 0)).save(root / "IMG_0003.png")
    Image.new("RGB", (64, 64), "blue").save(root / "IMG_0004.webp")
    (root / ".thumbnails").mkdir()
    Image.new("RGB", (32, 32), "gray").save(root / ".thumbnails" / "IMG_0001.jpg")
    Image.new("RGB", (32, 32), "gray").save(root / "sample.jpg")
    (root / "clip.mp4").write_bytes(b"not a photo")
    (root / "@eaDir").mkdir()
    Image.new("RGB", (32, 32), "gray").save(root / "@eaDir" / "x.jpg")
    return root


async def _make_photo_library(db, root: Path, **kwargs) -> Library:
    async with db.session() as session:
        return await LibraryConfigService(session).create(
            name=kwargs.pop("name", "相册"),
            kind=MediaKind.PHOTO,
            root_paths=[str(root)],
            **kwargs,
        )


# ---------------------------------------------------------------------------
# 能力档案
# ---------------------------------------------------------------------------


def test_photo_profile_is_capability_driven() -> None:
    photo = profile_for(MediaKind.PHOTO)
    assert photo.source == MediaSource.LOCAL
    assert (photo.scraped, photo.naming, photo.subscribable, photo.write_nfo) == (
        False,
        False,
        False,
        False,
    )
    assert photo.playable is False and photo.jellyfin_exposed is False
    assert photo.media_exts == frozenset(IMAGE_EXTS)
    assert photo.jellyfin_collection == "photos" and photo.jellyfin_type == "Photo"
    assert photo.default_aspect == pytest.approx(4 / 3, abs=1e-3)
    # 存量三行：收视频、可播、对 Jellyfin 暴露
    for kind in (MediaKind.MOVIE, MediaKind.TV, MediaKind.VIDEO):
        profile = profile_for(kind)
        assert profile.media_exts == frozenset(SCAN_VIDEO_EXTS)
        assert profile.playable is True and profile.jellyfin_exposed is True
    assert capabilities_of(photo)["playable"] is False
    assert jellyfin_hidden_kinds() == {"photo"}
    assert [o["label"] for o in library_kind_options()] == ["电影", "剧集", "其他", "图片"]


async def test_create_photo_library_defaults_to_local_source(db, tmp_path) -> None:
    library = await _make_photo_library(db, _make_photo_root(tmp_path))
    assert library.source == "local" and library.kind == "photo"
    view = LibraryView.from_model(library)
    assert view.capabilities.playable is False and view.capabilities.scraped is False
    assert view.capabilities.default_aspect == pytest.approx(4 / 3, abs=1e-3)


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------


def test_probe_image_reads_size_exif_and_orientation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(probe_mod.subprocess, "run", _no_subprocess)
    photo = tmp_path / "a.jpg"
    _save_jpeg(photo, (400, 300), taken="2024:05:01 10:20:30", orientation=6)
    spec = probe_media(photo)  # 按后缀分派到 probe_image，不起 ffprobe
    assert spec is not None
    assert spec.resolution == "300x400"  # 方向 6：显示时宽高对调
    assert spec.tag_date == "2024:05:01 10:20:30"
    assert spec.audio_streams == [] and spec.subtitle_streams == []  # 空列表，不是 None
    assert spec.duration_seconds is None and spec.video_codec is None

    plain = tmp_path / "b.png"
    Image.new("RGB", (64, 32), "white").save(plain)
    spec2 = probe_image(plain)
    assert spec2 is not None and spec2.resolution == "64x32" and spec2.tag_date is None

    # 坏文件 / 非图片：探测失败返回 None，不抛
    (tmp_path / "c.jpg").write_bytes(b"not an image")
    assert probe_image(tmp_path / "c.jpg") is None

    # 解压炸弹按失败处理
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    assert probe_image(photo) is None


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


async def test_scan_photo_library_one_item_per_image(db, tmp_path) -> None:
    root = _make_photo_root(tmp_path)
    library = await _make_photo_library(db, root)

    summary = await scan_library(library.id)
    assert _NoTmdb.calls == []
    assert summary.identified == 4 and summary.unidentified == 0

    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        by_name = {Path(r.file_path).name: r for r in rows}
        # 相册工具目录、@eaDir、sample、视频都不入账
        assert sorted(by_name) == ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.png", "IMG_0004.webp"]
        items = {i.id: i for i in (await session.execute(select(MediaItem))).scalars().all()}
        # 一文件一条目（不是同目录合成一张卡）
        assert len(items) == 4
        assert len({r.media_item_id for r in rows}) == 4
        assert all(
            i.source == "local" and i.tmdb_id is None and i.kind == "photo" for i in items.values()
        )
        assert all(r.identity_source == IdentitySource.LOCAL.value for r in rows)
        assert all(r.unidentified_code is None for r in rows)
        # 规格：原图尺寸（方向已对调）、无时长、音轨是空列表（补探条件是 IS NULL）
        first = by_name["IMG_0001.jpg"]
        assert first.resolution == "300x400" and first.duration_seconds is None
        assert first.audio_streams == [] and first.container == "jpg"
        assert by_name["IMG_0003.png"].resolution == "100x50"
        # EXIF 拍摄时间 → release_date 与年份；没有 EXIF 的回落到 mtime（今天）
        item1 = items[first.media_item_id]
        assert item1.title == "IMG_0001" and item1.year == 2024
        meta1 = (
            await session.execute(
                select(MediaMetadata).where(MediaMetadata.media_item_id == item1.id)
            )
        ).scalar_one()
        assert str(meta1.release_date) == "2024-05-01"
        fresh = await session.get(Library, library.id)
        assert fresh.stats_item_count == 4 and fresh.stats_unidentified_count == 0

        # 海报墙：按内容时间倒序；同一天（两张 2024-05-01）按文件名倒序
        wall = await items_mod.build_library_wall(session, library.id, sort="release_date")
        titles = [v.title for v in wall]
        assert titles[-2:] == ["IMG_0002", "IMG_0001"]
        assert set(titles[:2]) == {"IMG_0003", "IMG_0004"}
        view1 = next(v for v in wall if v.title == "IMG_0001")
        assert str(view1.release_date) == "2024-05-01"
        assert view1.primary_file_id == first.id
        assert view1.resolutions == ["300x400"]
        # 缩略图还没生成：比例来自入账时探到的原图尺寸（300x400），墙一开始就是
        # 最终布局；只有连尺寸都没有的才按档案兜底 4:3
        assert view1.primary_aspect == pytest.approx(300 / 400, abs=1e-3)
        assert view1.poster_blur is None

        # 月份索引与分页同口径：最新的月份（mtime 回落）在前，2024-05 两张在后
        buckets = await items_mod.build_library_index(session, library.id, "release_date")
        assert buckets[-1] == ("2024-05", 2, 2)
        assert sum(count for _, count, _ in buckets) == 4

    # 增量：再扫一次不重复建条目、不重探（规格已齐）
    again = await scan_library(library.id)
    assert again.identified == 0 and again.probed == 0 and _NoTmdb.calls == []
    async with db.session() as session:
        assert len((await session.execute(select(MediaItem))).scalars().all()) == 4


async def test_video_library_ignores_images(db, tmp_path, monkeypatch) -> None:
    """影视/其他库目录里的 jpg 是海报 sidecar，不是内容。"""
    from movieclaw_api.services.media_probe import MediaSpec

    root = tmp_path / "media" / "home"
    root.mkdir(parents=True)
    (root / "vlog.mp4").write_bytes(b"v")
    Image.new("RGB", (200, 300), "gray").save(root / "vlog-poster.jpg")
    Image.new("RGB", (200, 300), "gray").save(root / "random.jpg")
    spec = MediaSpec(
        resolution="1080p",
        video_codec="h264",
        hdr=None,
        bit_depth=8,
        duration_seconds=10,
        bit_rate=None,
    )
    monkeypatch.setattr(scan_mod, "probe_media", lambda _p: spec)
    async with db.session() as session:
        library = await LibraryConfigService(session).create(
            name="家庭录像", kind=MediaKind.VIDEO, root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert [Path(r.file_path).name for r in rows] == ["vlog.mp4"]


# ---------------------------------------------------------------------------
# 缩略图
# ---------------------------------------------------------------------------


def test_build_thumbnail_for_photo_uses_pillow(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(thumbs_mod.subprocess, "run", _no_subprocess)
    photo = tmp_path / "IMG_1.jpg"
    _save_jpeg(photo, (2000, 1000), orientation=6)  # 显示为竖版 1000x2000
    # 旁边放一张同名 sidecar 风格的图：图片就是内容本身，不能被当成海报
    Image.new("RGB", (10, 10), "red").save(tmp_path / "IMG_1-poster.jpg")
    dest = tmp_path / "assets" / "1" / "poster.jpg"
    size = build_thumbnail(photo, dest, duration_seconds=None)
    assert size == (360, 720) and dest.is_file()
    with Image.open(dest) as out:
        assert out.size == (360, 720) and out.mode == "RGB"
        assert out.getexif().get(_EXIF_ORIENTATION) is None  # 缩略图不带 EXIF

    # 透明 PNG 合成到卡片底色
    png = tmp_path / "IMG_2.png"
    Image.new("RGBA", (100, 50), (255, 0, 0, 0)).save(png)
    dest2 = tmp_path / "assets" / "2" / "poster.jpg"
    assert build_thumbnail(png, dest2, duration_seconds=None) == (100, 50)
    with Image.open(dest2) as out:
        assert out.getpixel((5, 5)) == pytest.approx((0x14, 0x18, 0x24), abs=3)

    # 图片没有背景图；坏文件返回 None
    assert build_backdrop(photo, tmp_path / "assets" / "1" / "backdrop.jpg") is False
    (tmp_path / "bad.jpg").write_bytes(b"nope")
    assert (
        build_thumbnail(
            tmp_path / "bad.jpg", tmp_path / "assets" / "3" / "poster.jpg", duration_seconds=None
        )
        is None
    )


async def test_ensure_local_assets_for_photo_records_real_aspect(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(thumbs_mod.subprocess, "run", _no_subprocess)
    root = tmp_path / "media" / "photos"
    _save_jpeg(root / "IMG_9.jpg", (1600, 1200))
    library = await _make_photo_library(db, root)
    await scan_library(library.id)
    async with db.session() as session:
        item = (await session.execute(select(MediaItem))).scalar_one()
    await ensure_local_assets(item.id)
    async with db.session() as session:
        meta = (await session.execute(select(MediaMetadata))).scalar_one()
        assert meta.poster_file == f"{item.id}/poster.jpg"
        assert (meta.poster_width, meta.poster_height) == (720, 540)
        assert meta.backdrop_file is None
        # 微缩占位图：16px 宽的 JPEG data URI，几百字节
        assert meta.poster_blur and meta.poster_blur.startswith("data:image/jpeg;base64,")
        assert len(meta.poster_blur) < 1200
        wall = await items_mod.build_library_wall(session, library.id, sort="release_date")
        assert wall[0].primary_aspect == pytest.approx(1600 / 1200, abs=1e-3)
        assert wall[0].poster_url.startswith(f"/images/assets/{item.id}/poster.jpg")
        assert wall[0].poster_blur == meta.poster_blur

    # 关掉缩略图开关：不生成
    async with db.session() as session:
        await LibraryConfigService(session).update(
            library.id,
            name=library.name,
            root_paths=list(library.root_paths),
            generate_thumbnails=False,
        )
    from movieclaw_api.services.media_scrape import assets_root

    poster = assets_root() / str(item.id) / "poster.jpg"
    assert poster.is_file()
    poster.unlink()
    await ensure_local_assets(item.id, force=True)
    assert not poster.exists()


async def test_delete_then_recreate_same_root_keeps_every_photo(db, tmp_path) -> None:
    """删库后立刻用同一目录重建：SQLite 复用库 id，新库的本地条目键与旧条目同键。
    删库接口须等孤儿条目的数据库清理做完再放行，否则后台清理会删掉新库刚认领的
    条目（端到端模拟时暴露：外键报错 + 一批照片从墙上消失）。"""
    from movieclaw_api.api.routes.libraries import delete_library
    from movieclaw_api.services.media_scrape import cleanup_orphan_items

    root = _make_photo_root(tmp_path)
    first = await _make_photo_library(db, root)
    await scan_library(first.id)
    async with db.session() as session:
        await delete_library(first.id, session=session)
    # 库 id 被复用（SQLite rowid），条目键因此与旧库相同
    second = await _make_photo_library(db, root)
    assert second.id == first.id
    summary = await scan_library(second.id)
    assert summary.errors == [] and summary.identified == 4
    # 旧条目在删库时已同步清干净，新库的 4 张各有条目、条目各有台账行
    async with db.session() as session:
        items = list((await session.execute(select(MediaItem))).scalars().all())
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        assert len(items) == 4 and len(rows) == 4
        assert {r.media_item_id for r in rows} == {i.id for i in items}
        fresh = await session.get(Library, second.id)
        assert fresh.stats_item_count == 4 and fresh.stats_file_count == 4
    # 清理是幂等的：再跑一次不会误删有台账的条目
    assert await cleanup_orphan_items([i.id for i in items]) == 0


def test_photo_variants_fit_without_cropping(tmp_path) -> None:
    """瓦片与屏幕适配派生图：等比装进盒子、不裁切、不放大（照片的比例就是内容）。"""
    from io import BytesIO

    from movieclaw_api.services.image_variants import _PRESETS, ImageVariant, _render_webp

    photo = tmp_path / "wide.jpg"
    _save_jpeg(photo, (1600, 1000), orientation=6)  # 显示为 1000x1600 竖版
    tile = Image.open(BytesIO(_render_webp(photo, _PRESETS[ImageVariant.PHOTO_TILE])))
    assert tile.format == "WEBP" and tile.size == (300, 480)  # 长边 480、方向已纠正、不裁
    screen = Image.open(BytesIO(_render_webp(photo, _PRESETS[ImageVariant.PHOTO_SCREEN])))
    assert screen.size == (1000, 1600)  # 小于 2048 不放大
    # 卡片预设同样是等比装框：竖版照片以 492 高为界、宽按原比例算，不裁成 2:3
    card = Image.open(BytesIO(_render_webp(photo, _PRESETS[ImageVariant.POSTER_CARD])))
    assert card.size == (308, 492)


# ---------------------------------------------------------------------------
# 监听 / Jellyfin
# ---------------------------------------------------------------------------


def test_watch_event_relevance_follows_library_extensions() -> None:
    photo_watched = frozenset(IMAGE_EXTS | {".srt"})
    video_watched = frozenset(SCAN_VIDEO_EXTS | {".srt"})
    jpg = SimpleNamespace(
        event_type="created", is_directory=False, src_path="/lib/IMG_1.jpg", dest_path=""
    )
    mp4 = SimpleNamespace(
        event_type="created", is_directory=False, src_path="/lib/a.mp4", dest_path=""
    )
    assert _is_relevant_event(jpg, photo_watched) is True
    assert _is_relevant_event(mp4, photo_watched) is False
    # 影视库目录里出现 jpg（刮削器写海报）不触发扫描——否则刷新期间自激
    assert _is_relevant_event(jpg, video_watched) is False
    assert _is_relevant_event(mp4, video_watched) is True
    assert _is_relevant_event(mp4) is True  # 缺省按视频口径


async def test_jellyfin_hides_photo_libraries(db, tmp_path) -> None:
    root = _make_photo_root(tmp_path)
    photo_lib = await _make_photo_library(db, root)
    video_root = tmp_path / "media" / "home"
    video_root.mkdir(parents=True)
    async with db.session() as session:
        video_lib = await LibraryConfigService(session).create(
            name="家庭录像", kind=MediaKind.VIDEO, root_paths=[str(video_root)]
        )
    await scan_library(photo_lib.id)
    async with db.session() as session:
        libs = await jf_catalog.list_libraries(session)
        assert [lib.id for lib in libs] == [video_lib.id]
        assert jf_catalog.item_type_of("photo") == "Photo"
        assert "Photo" not in jf_catalog.PLAYABLE_TYPES


# ---------------------------------------------------------------------------
# 原图路由：鉴权与内容
# ---------------------------------------------------------------------------

_AUTH = "/api/v1/auth"
_MEMBERS = "/api/v1/members"
_LIBS = "/api/v1/libraries"
_ADMIN = {"username": "admin", "password": "s3cret-pass"}
_MEMBER = {"username": "family", "password": "family-pass-1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        resp = c.post(f"{_AUTH}/bootstrap", json=_ADMIN)
        assert resp.status_code == 200, resp.text
        yield c
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


async def _seed_photo(library_id: int, path: Path) -> int:
    """直接播种一条图片台账行，返回 file id。"""
    async with get_database().session() as session:
        item = MediaItem(
            kind="photo",
            source="local",
            external_id=f"{library_id}:path:{path.name}",
            tmdb_id=None,
            title=path.stem,
            original_title=path.stem,
            year=2024,
            aliases=[],
        )
        session.add(item)
        await session.commit()
        row = LibraryFile(
            library_id=library_id,
            media_item_id=item.id,
            file_path=str(path),
            source="scanned",
            size_bytes=path.stat().st_size if path.exists() else 0,
        )
        session.add(row)
        await session.commit()
        assert row.id is not None
        return row.id


async def test_original_route_serves_images_by_visibility(client: TestClient, tmp_path) -> None:
    root = tmp_path / "media" / "photos"
    _save_jpeg(root / "IMG_1.jpg", (40, 30))
    (root / "clip.mp4").write_bytes(b"v")
    # 只对指定成员开放的图片库；成员不在名单里
    resp = client.post(
        _LIBS,
        json={
            "name": "相册",
            "kind": "photo",
            "root_paths": [str(root)],
            "access_mode": "selected",
            "member_ids": [],
        },
    )
    assert resp.status_code == 200, resp.text
    library_id = resp.json()["data"]["id"]
    assert resp.json()["data"]["capabilities"]["playable"] is False
    photo_id = await _seed_photo(library_id, root / "IMG_1.jpg")
    video_id = await _seed_photo(library_id, root / "clip.mp4")
    missing_id = await _seed_photo(library_id, root / "gone.jpg")

    admin_cookie = client.cookies.get("movieclaw_session")
    got = client.get(f"{_LIBS}/files/{photo_id}/original")
    assert got.status_code == 200, got.text
    assert got.headers["content-type"].startswith("image/jpeg")
    assert (
        "last-modified" in got.headers and got.headers["cache-control"] == "private, max-age=3600"
    )
    assert got.content[:2] == b"\xff\xd8"  # JPEG 魔数：给的是原图本身
    down = client.get(f"{_LIBS}/files/{photo_id}/original", params={"download": "1"})
    assert down.status_code == 200
    assert down.headers["content-disposition"].startswith("attachment; filename*=UTF-8''IMG_1.jpg")
    # 屏幕适配图：按原图派生的 WebP，走图片缓存
    screen = client.get(f"{_LIBS}/files/{photo_id}/original", params={"size": "screen"})
    assert screen.status_code == 200, screen.text
    assert screen.headers["content-type"].startswith("image/webp")
    assert screen.content[:4] == b"RIFF"
    not_image = client.get(f"{_LIBS}/files/{video_id}/original", params={"size": "screen"})
    assert not_image.status_code == 404
    # 非图片文件与磁盘上不存在的文件都是 404
    assert client.get(f"{_LIBS}/files/{video_id}/original").status_code == 404
    assert client.get(f"{_LIBS}/files/{missing_id}/original").status_code == 404
    assert client.get(f"{_LIBS}/files/999999/original").status_code == 404

    # 不在可见范围内的成员：与不存在同样 404
    created = client.post(_MEMBERS, json=_MEMBER)
    assert created.status_code == 200, created.text
    client.cookies.clear()
    login = client.post(
        f"{_AUTH}/login", json={"username": _MEMBER["username"], "password": _MEMBER["password"]}
    )
    assert login.status_code == 200, login.text
    assert client.get(f"{_LIBS}/files/{photo_id}/original").status_code == 404
    # 未登录 401
    client.cookies.clear()
    assert client.get(f"{_LIBS}/files/{photo_id}/original").status_code == 401
    client.cookies.set("movieclaw_session", admin_cookie)
    assert client.get(f"{_LIBS}/files/{photo_id}/original").status_code == 200
