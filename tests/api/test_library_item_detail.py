"""条目详情页后端的端到端测试。

覆盖：ffprobe 音轨/字幕轨解析、NFO 展示元数据解析（容错）、详情接口
（本地美术图优先 / NFO 元数据 / 介质规格懒回填 / 外挂字幕合并）、
本地美术图接口、真实删除（整目录清除 / 混目录退化 / 库外不删 /
单文件删除：保留其他版本 / 最后一个文件升级整删 / missing 行只清账）、
单条目重新识别（错挂翻案 / NFO 权威提示 / 识别失败进待识别）。
TMDB 为假实现。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from sqlmodel import select

import movieclaw_api.services.library.items as items_mod
import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.media_discover as discover_mod
from movieclaw_api.api.routes.libraries import (
    _file_view,
    delete_library_file,
    delete_library_item,
    get_item_artwork,
    get_library_item,
    reidentify_library_item,
    set_item_scrape_library,
)
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.schemas.library import ScrapeLibraryPayload
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.nfo import read_entry_metadata, read_tmdb_id
from movieclaw_api.services.library.resolve import ResolveOutcome
from movieclaw_api.services.library.scan import scan_library
from movieclaw_api.services.media_probe import MediaSpec, _parse_probe
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileState, LibraryFile, MediaItem
from movieclaw_db.models.library_file import IdentitySource
from movieclaw_db.repositories.library_repo import LibraryRepository

_KEY = "0123456789abcdef0123456789abcdef"

# 直调路由函数时替代 require_login 依赖注入的超管主体（成员管理引入
# principal 参数后，直调必须显式传，否则 session 会错位进 principal）
_ADMIN = Principal(kind="admin", name="tester")

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
            {
                "episode_number": 1,
                "name": "E1",
                "air_date": "2024-01-01",
                "overview": "线上第一集简介",
                "still_path": "/s1e1.jpg",
            },
            {
                "episode_number": 2,
                "name": "E2",
                "air_date": "2024-01-08",
                "overview": "线上第二集简介",
                "still_path": "/s1e2.jpg",
            },
        ],
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
        # TMDB 展示兜底（无本地 NFO 时详情页拉取）用到的字段
        "overview": "一段来自 TMDB 的简介。",
        "vote_average": 7.25,
        "runtime": 118,
        "genres": [{"id": 1, "name": "剧情"}],
        "credits": {
            "cast": [
                {"id": 9101, "name": "线上演员甲", "character": "主角", "profile_path": "/a1.jpg"},
                {"id": 9102, "name": "线上演员乙", "character": "", "profile_path": None},
            ],
            "crew": [
                {
                    "id": 9001,
                    "name": "线上导演",
                    "original_name": "Online Director",
                    "job": "Director",
                    "profile_path": "/d1.jpg",
                }
            ],
        },
        # 选图策略的候选（无文字背景 / 中文海报各一张，都会被选中）
        "images": {
            "posters": [
                {
                    "file_path": "/poster.jpg",
                    "iso_639_1": "zh",
                    "width": 1000,
                    "height": 1500,
                    "vote_average": 7.0,
                    "vote_count": 50,
                }
            ],
            "backdrops": [
                {
                    "file_path": "/backdrop.jpg",
                    "iso_639_1": None,
                    "width": 1920,
                    "height": 1080,
                    "vote_average": 7.0,
                    "vote_count": 50,
                }
            ],
        },
    },
}


def _fake_tmdb():
    from movieclaw_media.tmdb import TmdbClient

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
            return httpx.Response(200, json={"results": []})
        if path == "/3/movie/300/images":
            # 「更换图片」弹层的候选来源（独立端点，与详情的 append 同数据）
            return httpx.Response(200, json=_ROUTES["/3/movie/300"]["images"])
        payload = _ROUTES.get(path)
        return httpx.Response(200 if payload else 404, json=payload or {})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'detail.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    client = _fake_tmdb()
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# ffprobe 解析
# ---------------------------------------------------------------------------


def test_file_view_parses_generated_subtitle_names() -> None:
    """详情页应把产物类型与语言拆开，不能把整段文件名误当字幕标题。"""
    row = LibraryFile(
        id=88,
        library_id=1,
        media_item_id=1,
        file_path="/media/Movie.mkv",
        source="scanned",
        subtitle_streams=[],
    )

    view = _file_view(
        row,
        ["Movie.ai-bilingual-chs-eng.chi.srt", "Movie.pgs-ocr.eng.srt"],
    )

    bilingual, ocr = view.subtitle_streams
    assert bilingual.language == "chi" and bilingual.title == "ai-bilingual-chs-eng"
    assert ocr.language == "eng" and ocr.title == "pgs-ocr"


def test_parse_probe_extracts_audio_and_subtitle_streams() -> None:
    payload = {
        "format": {"duration": "7200.5", "bit_rate": "12000000"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 3840,
                "height": 1600,
                "color_transfer": "smpte2084",
                "color_primaries": "bt2020",
                "avg_frame_rate": "24000/1001",
                "pix_fmt": "yuv420p10le",
            },
            {
                "codec_type": "audio",
                "codec_name": "dts",
                "profile": "DTS-HD MA",
                "channels": 8,
                "channel_layout": "7.1",
                "disposition": {"default": 1},
                "tags": {"language": "eng", "title": "DTS-HD MA 7.1"},
            },
            {
                "codec_type": "audio",
                "codec_name": "ac3",
                "channels": 6,
                "channel_layout": "5.1(side)",
                "disposition": {"default": 0},
                "tags": {"language": "chi"},
            },
            {
                "codec_type": "subtitle",
                "codec_name": "hdmv_pgs_subtitle",
                "disposition": {"default": 0, "forced": 1},
                "tags": {"language": "chi", "title": "简体中文"},
            },
        ],
    }
    spec = _parse_probe(payload)
    assert spec.resolution == "2160p" and spec.hdr == "HDR10" and spec.bit_depth == 10
    assert spec.frame_rate == 23.976 and spec.color_space == "BT.2020"
    assert [a["codec"] for a in spec.audio_streams] == ["dts", "ac3"]
    assert spec.audio_streams[0]["profile"] == "DTS-HD MA"
    assert spec.audio_streams[0]["channels"] == 8 and spec.audio_streams[0]["default"] is True
    assert spec.audio_streams[1]["language"] == "chi"
    assert spec.subtitle_streams == [
        {
            "codec": "hdmv_pgs_subtitle",
            "language": "chi",
            "title": "简体中文",
            "forced": True,
            "default": False,
        }
    ]


def test_parse_probe_prefers_dynamic_hdr_formats_over_pq_fallback() -> None:
    """动态 HDR side data 必须压过 PQ 的普通 HDR10 兜底标签。"""
    cases = [
        ("DOVI configuration record", "Dolby Vision"),
        ("HDR Dynamic Metadata SMPTE2094-40", "HDR10+"),
    ]
    for side_data_type, expected in cases:
        spec = _parse_probe(
            {
                "format": {},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "color_transfer": "smpte2084",
                        "side_data_list": [{"side_data_type": side_data_type}],
                    }
                ],
            }
        )
        assert spec.hdr == expected


def test_parse_probe_no_extra_streams_gives_empty_lists() -> None:
    spec = _parse_probe({"format": {}, "streams": [{"codec_type": "video", "codec_name": "h264"}]})
    assert spec.audio_streams == [] and spec.subtitle_streams == []


# ---------------------------------------------------------------------------
# NFO 展示元数据解析
# ---------------------------------------------------------------------------

_FULL_NFO = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
  <title>某电影</title>
  <plot>一段剧情简介。</plot>
  <runtime>121</runtime>
  <ratings>
    <rating name="imdb" max="10"><value>7.9</value></rating>
    <rating name="themoviedb" max="10"><value>8.25</value></rating>
  </ratings>
  <genre>剧情</genre>
  <genre>科幻</genre>
  <director>某导演</director>
  <tmdbid>300</tmdbid>
  <actor>
    <name>演员甲</name>
    <role>主角</role>
    <thumb>https://image.tmdb.org/t/p/original/a.jpg</thumb>
  </actor>
  <actor>
    <name>演员乙</name>
    <thumb>.actors/local.jpg</thumb>
  </actor>
</movie>
https://www.themoviedb.org/movie/300
"""


def test_read_entry_metadata_full_nfo(tmp_path) -> None:
    """完整 NFO：字段齐活；XML 后的裸 URL 杂质（Kodi 惯例）不影响解析；
    themoviedb 评分优先于 imdb；本地相对路径头像丢弃。"""
    nfo = tmp_path / "movie.nfo"
    nfo.write_text(_FULL_NFO, encoding="utf-8")
    meta = read_entry_metadata(nfo)
    assert meta is not None and meta.has_content()
    assert meta.plot == "一段剧情简介。"
    assert meta.rating == 8.2  # themoviedb 8.25 四舍五入一位
    assert meta.runtime_minutes == 121
    assert meta.genres == ["剧情", "科幻"] and meta.directors == ["某导演"]
    assert [(a.name, a.role) for a in meta.actors] == [("演员甲", "主角"), ("演员乙", None)]
    assert meta.actors[0].thumb == "https://image.tmdb.org/t/p/original/a.jpg"
    assert meta.actors[1].thumb is None  # 本地相对路径无服务通道


def test_read_entry_metadata_minimal_and_broken(tmp_path) -> None:
    """自家写出的最小身份 NFO 解析成功但 has_content()=False；坏文件返回 None。"""
    minimal = tmp_path / "min.nfo"
    minimal.write_text("<movie><title>X</title><tmdbid>1</tmdbid></movie>", encoding="utf-8")
    meta = read_entry_metadata(minimal)
    assert meta is not None and not meta.has_content()

    broken = tmp_path / "broken.nfo"
    broken.write_text("这不是 XML", encoding="utf-8")
    assert read_entry_metadata(broken) is None


def test_read_tmdb_id_ignores_actor_person_ids(tmp_path) -> None:
    """条目级 tmdb id 只认根元素直接子节点：<actor> 块里演员的 person id
    （Emby 刮削惯例，且排在条目 id 之前）不得污染；uniqueid 优先于裸 tmdbid。"""
    nfo = tmp_path / "tvshow.nfo"
    nfo.write_text(
        """<tvshow>
  <actor><name>Pan Yueming</name><tmdbid>138734</tmdbid></actor>
  <tmdbid>254498</tmdbid>
  <uniqueid type="tmdb">254498</uniqueid>
</tvshow>""",
        encoding="utf-8",
    )
    assert read_tmdb_id(nfo) == 254498

    # 只有演员块带 tmdbid、条目级没有 → 宁可返回 None 也不冒认演员 id
    actor_only = tmp_path / "actor_only.nfo"
    actor_only.write_text(
        "<tvshow><actor><name>某演员</name><tmdbid>138734</tmdbid></actor></tvshow>",
        encoding="utf-8",
    )
    assert read_tmdb_id(actor_only) is None

    broken = tmp_path / "broken.nfo"
    broken.write_text("这不是 XML", encoding="utf-8")
    assert read_tmdb_id(broken) is None


def test_read_tmdb_id_rejects_episode_nfo(tmp_path) -> None:
    """分集 NFO（episodedetails）根级的 uniqueid/tmdbid 是**那一集**的 id，
    不得冒认成条目身份——它是同名 .nfo 查找顺序的第一候选，一旦误读
    整部剧会被钉死到一个集上。"""
    nfo = tmp_path / "某剧 S01E01.nfo"
    nfo.write_text(
        """<episodedetails>
  <title>第 1 集</title>
  <uniqueid type="tmdb">1234567</uniqueid>
  <tmdbid>1234567</tmdbid>
</episodedetails>""",
        encoding="utf-8",
    )
    assert read_tmdb_id(nfo) is None


# ---------------------------------------------------------------------------
# 详情接口
# ---------------------------------------------------------------------------

_FAKE_SPEC = MediaSpec(
    resolution="1080p",
    video_codec="hevc",
    hdr=None,
    bit_depth=10,
    duration_seconds=7260,
    bit_rate=8_000_000,
    frame_rate=23.976,
    color_space="BT.2020",
    audio_streams=[
        {
            "codec": "eac3",
            "profile": None,
            "channels": 6,
            "channel_layout": "5.1",
            "language": "chi",
            "title": None,
            "default": True,
        }
    ],
    subtitle_streams=[
        {"codec": "subrip", "language": "chi", "title": None, "forced": False, "default": True}
    ],
)


def _make_movie_entry(tmp_path):
    """电影库样本：规范条目目录 + 完整 NFO + 本地海报/剧照 + 外挂字幕。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    video = entry / "某电影.2020.1080p.WEB-DL.mkv"
    video.write_bytes(b"movie-bytes")
    (entry / "movie.nfo").write_text(_FULL_NFO, encoding="utf-8")
    (entry / "poster.jpg").write_bytes(b"poster")
    (entry / "fanart.jpg").write_bytes(b"fanart")
    (entry / "某电影.2020.1080p.WEB-DL.chs&eng.srt").write_text("1", encoding="utf-8")
    return root, entry, video


async def test_item_detail_assembles_local_scrape(db, tmp_path, monkeypatch) -> None:
    root, entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    summary = await scan_library(library.id)
    assert summary.identified == 1  # NFO tmdbid=300 精确锚定

    # 扫描时 ffprobe 对假字节探测失败（audio_streams=NULL）→ 等扫描补探回填
    monkeypatch.setattr(items_mod, "probe_media", lambda _path: _FAKE_SPEC)

    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        item.douban_id = "1292052"
        session.add(item)
        await session.commit()
        resp = await get_library_item(library.id, item.id, _ADMIN, session)
        view = resp.data

    assert view.title == "某电影" and view.kind == "movie" and view.tmdb_id == 300
    assert view.douban_id == "1292052"
    # 本地美术图优先：走 artwork 接口相对路径而非 TMDB 图床
    # URL 带 ?v=<mtime> 版本戳：换图是原地覆盖同一路径，不带版本浏览器
    # 会一直显示缓存里的旧图（实测踩过）
    art_base = f"/libraries/{library.id}/items/{item.id}/artwork"
    assert view.poster_url.startswith(f"{art_base}?kind=poster&v=")
    assert view.backdrop_url.startswith(f"{art_base}?kind=fanart&v=")
    assert int(view.poster_url.rsplit("v=", 1)[1]) > 0
    assert view.entry_dirs == [str(entry)]
    # NFO 元数据——2026-08-04 对齐语义：扫描收尾的资产镜像已把第三方 NFO
    # 重写为与库内档案一致（rating 8.2/121 的旧值被 TMDB 档案 7.2/118 取代），
    # 详情读到的 NFO 层与 media_metadata 同源（docs/design/metadata.md 6.2）
    assert view.local_meta is not None
    assert view.local_meta.rating == 7.2 and view.local_meta.runtime_minutes == 118
    assert [a.name for a in view.local_meta.actors] == ["线上演员甲", "线上演员乙"]
    # 详情页不再触发补探：音轨保持"尚未探测"（前端据此提示重新扫描），
    # 浏览不碰媒体文件本体（云盘挂载上读文件就是流量与延迟）
    file = view.files[0]
    assert file.file_name == "某电影.2020.1080p.WEB-DL.mkv"
    assert file.audio_streams is None
    embedded = [s for s in file.subtitle_streams if not s.external]
    external = [s for s in file.subtitle_streams if s.external]
    assert embedded == []  # 内封轨要等扫描补探
    assert external[0].file_name == "某电影.2020.1080p.WEB-DL.chs&eng.srt"
    assert external[0].title == "chs&eng"

    # 扫描的补探阶段回填 → 落库 → 再取即有
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        await items_mod.backfill_streams(session, rows)
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.audio_streams and row.audio_streams[0]["codec"] == "eac3"
        assert row.subtitle_streams and row.subtitle_streams[0]["codec"] == "subrip"
        resp2 = await get_library_item(library.id, item.id, _ADMIN, session)
        file2 = resp2.data.files[0]
        assert file2.audio_streams is not None and file2.audio_streams[0].codec == "eac3"
        assert file2.audio_streams[0].channels == 6
        assert file2.frame_rate == 23.976 and file2.color_space == "BT.2020"
        embedded2 = [s for s in file2.subtitle_streams if not s.external]
        assert embedded2[0].codec == "subrip" and embedded2[0].language == "chi"


async def test_wall_reports_probe_pending_count(db, tmp_path, monkeypatch) -> None:
    """海报墙的待补探计数：扫描后没探出规格的条目 >0，补探回填后归零。
    扫描补探阶段前端据此把「还在处理」的条目排到墙前面并点亮。"""
    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    summary = await scan_library(library.id)
    assert summary.identified == 1

    # 扫描时 ffprobe 对假字节探测失败 → audio_streams=NULL → 计入待补探
    async with db.session() as session:
        wall = await items_mod.build_library_wall(session, library.id)
        assert [v.probe_pending_count for v in wall] == [1]

    # 补探回填成功后归零
    monkeypatch.setattr(items_mod, "probe_media", lambda _path: _FAKE_SPEC)
    async with db.session() as session:
        rows = list((await session.execute(select(LibraryFile))).scalars().all())
        await items_mod.backfill_streams(session, rows)
        wall = await items_mod.build_library_wall(session, library.id)
        assert [v.probe_pending_count for v in wall] == [0]


async def test_strm_rows_never_count_as_probe_pending(db, tmp_path) -> None:
    """strm 占位行不算「待补探」：墙上计数为零，详情页也不排后台补探。

    strm 永远探不出规格（本体没有媒体流），算进待补探会让网盘库的海报墙
    每轮扫描都全墙点亮「正在读取规格」且永不消失。
    """
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.1080p.WEB-DL.strm").write_text(
        "https://cloud.example.com/m.mkv", encoding="utf-8"
    )
    (entry / "movie.nfo").write_text(_FULL_NFO, encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="网盘电影库", kind="movie", root_paths=[str(root)]
        )
    summary = await scan_library(library.id)
    assert summary.identified == 1

    async with db.session() as session:
        wall = await items_mod.build_library_wall(session, library.id)
        assert [v.probe_pending_count for v in wall] == [0]
        item_id = (await session.execute(select(MediaItem))).scalars().one().id
        # 详情页可正常打开（不再有任何补探副作用）
        await get_library_item(library.id, item_id, _ADMIN, session)


async def test_item_detail_selfsufficient_after_scan(db, tmp_path) -> None:
    """一次入库刮削（docs/design/metadata.md）：扫描挂锚即展示档案落库、
    最小身份 NFO 原地升级为完整版。详情页读路径 NFO > DB——删掉 NFO 后
    档案照样从库内 media_metadata 出（断网可用），出处标注 db。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.1080p.mkv").write_bytes(b"m")
    # 我们自己写出的最小身份 NFO：只有 tmdbid，没有展示信息
    (entry / "movie.nfo").write_text(
        "<movie><title>某电影</title><tmdbid>300</tmdbid></movie>", encoding="utf-8"
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        resp = await get_library_item(library.id, item.id, _ADMIN, session)
        view = resp.data

    # NFO 身份高置信（identity_source=nfo）→ 最小 NFO 被升级为完整版并回读
    assert view.local_meta is not None and view.local_meta.source == "nfo"
    assert view.local_meta.plot == "一段来自 TMDB 的简介。"
    assert view.local_meta.rating == 7.2 and view.local_meta.runtime_minutes == 118
    assert view.local_meta.directors == ["线上导演"]
    assert [d.name for d in view.local_meta.director_credits] == ["线上导演"]
    assert view.local_meta.director_credits[0].tmdb_person_id == 9001
    assert view.local_meta.director_credits[0].thumb_url and view.local_meta.director_credits[
        0
    ].thumb_url.endswith("/w300/d1.jpg")
    assert [(a.name, a.role) for a in view.local_meta.actors] == [
        ("线上演员甲", "主角"),
        ("线上演员乙", None),
    ]
    assert view.local_meta.actors[0].thumb_url and view.local_meta.actors[0].thumb_url.endswith(
        "/w300/a1.jpg"
    )

    # 删掉 NFO：读路径落到第二层——库内刮削档案（media_metadata），断网可用
    (entry / "movie.nfo").unlink()
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        resp = await get_library_item(library.id, item.id, _ADMIN, session)
        view = resp.data

    assert view.local_meta is not None and view.local_meta.source == "db"
    assert view.local_meta.plot == "一段来自 TMDB 的简介。"
    assert view.local_meta.rating == 7.2 and view.local_meta.runtime_minutes == 118
    assert view.local_meta.directors == ["线上导演"]
    assert view.local_meta.actors[0].thumb_url and view.local_meta.actors[0].thumb_url.endswith(
        "/w300/a1.jpg"
    )


async def test_item_detail_fills_missing_actor_thumbs_from_archive(db, tmp_path) -> None:
    """NFO 只写了演员姓名（很多刮削器如此，本项目早期版本也是）时，头像按
    姓名从库内档案回填——否则详情页的演职员条是一排空占位。档案里也没有
    头像的演员保持为空，不瞎编。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.1080p.mkv").write_bytes(b"m")
    (entry / "movie.nfo").write_text(
        "<movie><title>某电影</title><tmdbid>300</tmdbid></movie>", encoding="utf-8"
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)  # 刮削落库：档案里的演员带头像路径

    # 换成一份"有内容但演员没头像"的 NFO（第三方刮削器/旧版本写出的形态）
    (entry / "movie.nfo").write_text(
        "<movie><title>某电影</title><tmdbid>300</tmdbid>"
        "<plot>一段剧情简介。</plot><runtime>121</runtime>"
        "<actor><name>线上演员甲</name><role>主角</role></actor>"
        "<actor><name>查无此人</name></actor></movie>",
        encoding="utf-8",
    )
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        resp = await get_library_item(library.id, item.id, _ADMIN, session)
        view = resp.data

    assert view.local_meta is not None and view.local_meta.source == "nfo"
    actors = view.local_meta.actors
    assert [(a.name, a.role) for a in actors] == [("线上演员甲", "主角"), ("查无此人", None)]
    assert actors[0].thumb_url and actors[0].thumb_url.endswith("/w300/a1.jpg")
    assert actors[1].thumb_url is None


async def test_item_detail_fills_person_ids_even_when_thumbs_complete(db, tmp_path) -> None:
    """NFO 头像全齐但没有 <actor><tmdbid>（TMM/Emby 与本项目旧版本写出的
    典型形态）时，影人 id 仍要按姓名从库内档案回填——曾因"头像齐了就早退"
    把 id 回填一起跳掉，演职员卡全部不可点（人物页链接依赖这个 id）。"""
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.1080p.mkv").write_bytes(b"m")
    (entry / "movie.nfo").write_text(
        "<movie><title>某电影</title><tmdbid>300</tmdbid></movie>", encoding="utf-8"
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)  # 刮削落库：档案里的演员带 tmdb_person_id

    # 换成"头像全有、影人 id 全无"的 NFO
    (entry / "movie.nfo").write_text(
        "<movie><title>某电影</title><tmdbid>300</tmdbid>"
        "<plot>一段剧情简介。</plot>"
        "<actor><name>线上演员甲</name><role>主角</role>"
        "<thumb>https://image.tmdb.org/t/p/w300/a1.jpg</thumb></actor>"
        "<actor><name>线上演员乙</name>"
        "<thumb>https://image.tmdb.org/t/p/w300/a2.jpg</thumb></actor></movie>",
        encoding="utf-8",
    )
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        resp = await get_library_item(library.id, item.id, _ADMIN, session)
        view = resp.data

    assert view.local_meta is not None and view.local_meta.source == "nfo"
    actors = view.local_meta.actors
    assert [a.tmdb_person_id for a in actors] == [9101, 9102]
    # 头像以 NFO 为准，不被回填覆盖
    assert actors[0].thumb_url and actors[0].thumb_url.endswith("/w300/a1.jpg")


async def test_manual_artwork_selection_locks_against_refresh(db, tmp_path) -> None:
    """手动选图即加锁：自动选图策略与 force 刷新都不再覆盖这张图
    （docs/design/metadata.md 6.3）——精挑的图被下次刷新冲掉比选不了更伤。"""
    from movieclaw_api.services.media_scrape import scrape_media_item, select_artwork
    from movieclaw_db.repositories import MediaItemRepository

    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        item_id = item.id

    await select_artwork(item_id, kind="backdrop", file_path="/my-pick.jpg")
    async with db.session() as session:
        item = await session.get(MediaItem, item_id)
        meta = await MediaItemRepository(session).get_metadata(item_id)
        assert item.backdrop_path == "/my-pick.jpg"
        assert meta.backdrop_locked is True
        assert meta.poster_locked is False  # 只锁选过的那一种

    # force 刷新（"以 TMDB 最新为准"）也不能动锁定的背景，海报照常按策略走
    await scrape_media_item(item_id, force=True)
    async with db.session() as session:
        item = await session.get(MediaItem, item_id)
        assert item.backdrop_path == "/my-pick.jpg"
        assert item.poster_path == "/poster.jpg"

    # 恢复自动：解锁后刷新重新按策略选图
    await select_artwork(item_id, kind="backdrop", file_path=None)
    async with db.session() as session:
        meta = await MediaItemRepository(session).get_metadata(item_id)
        assert meta.backdrop_locked is False
    await scrape_media_item(item_id, force=True)
    async with db.session() as session:
        item = await session.get(MediaItem, item_id)
        assert item.backdrop_path == "/backdrop.jpg"


async def test_library_refresh_is_full_and_reports_progress(db, tmp_path, monkeypatch) -> None:
    """整库刷新=全量重刷（force），并把"到哪部了、在做什么"实时报出来
    ——全量很慢，只给百分比等于让用户干等（用户决策 2026-07-26）。"""
    from movieclaw_api.services import media_scrape

    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    seen_force: list[bool] = []
    seen_phases: list[str] = []
    real_scrape = media_scrape.scrape_media_item

    async def _spy(media_item_id, *, force=False, on_phase=None):
        seen_force.append(force)
        # 阶段回调必须把状态写进 active，前端才有"在做什么"可展示
        return await real_scrape(
            media_item_id,
            force=force,
            on_phase=lambda phase: (
                (seen_phases.append(phase), on_phase(phase))[1] if on_phase else None
            ),
        )

    monkeypatch.setattr(media_scrape, "scrape_media_item", _spy)
    await media_scrape.refresh_library_metadata(library.id)

    assert seen_force == [True]  # 全量：每部都按 force 走
    assert "拉取 TMDB 档案" in seen_phases and "下载图片" in seen_phases
    # 跑完即清状态（前端据此收起进度面板）
    assert media_scrape.library_refresh_state(library.id) is None
    assert media_scrape.is_library_refreshing(library.id) is False


async def test_orphan_cleanup_removes_unreferenced_items_and_assets(db, tmp_path) -> None:
    """删库/删条目后的孤儿清理：条目在所有库都没文件、也没订阅 → 连同
    图片资产一起删；仍被订阅或别的库引用的条目原样保留（同一部剧的集
    分散在两个库是常态，删一个库不能把锚删了）。"""
    from movieclaw_api.services.media_scrape import assets_root, cleanup_orphan_items
    from movieclaw_db.models import Subscription

    async with db.session() as session:
        orphan = MediaItem(kind="movie", tmdb_id=901, title="孤儿片", original_title="O")
        subscribed = MediaItem(kind="movie", tmdb_id=902, title="在订阅", original_title="S")
        session.add_all([orphan, subscribed])
        await session.flush()
        from movieclaw_db.models import RuleSet

        rule_set = RuleSet(name="默认规则")
        session.add(rule_set)
        await session.flush()
        session.add(
            Subscription(
                media_item_id=subscribed.id,
                kind="movie",
                selected_seasons=[],
                follow_future=False,
                rule_set_id=rule_set.id,
                status="active",
            )
        )
        orphan_id, subscribed_id = orphan.id, subscribed.id
        await session.commit()

    # 两个条目各造一份图片资产目录
    for item_id in (orphan_id, subscribed_id):
        directory = assets_root() / str(item_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "poster.jpg").write_bytes(b"x")

    assert await cleanup_orphan_items([orphan_id, subscribed_id]) == 1
    async with db.session() as session:
        assert await session.get(MediaItem, orphan_id) is None
        assert await session.get(MediaItem, subscribed_id) is not None
    assert not (assets_root() / str(orphan_id)).exists()
    assert (assets_root() / str(subscribed_id) / "poster.jpg").is_file()
    # 清理干净后重复调用是幂等的（删库/删条目可能重复触发）
    assert await cleanup_orphan_items([orphan_id]) == 0


async def test_item_detail_reports_scraping_state(db, tmp_path) -> None:
    """刮削状态由服务端给：用户离开页面、刷新浏览器、换台设备打开，
    都能看到"这部还在刮"——不依赖发起刷新的那个标签页还开着。"""
    from movieclaw_api.services import media_scrape

    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        item_id = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        ).id
        resp = await get_library_item(library.id, item_id, _ADMIN, session)
        assert resp.data.scraping is False
        assert resp.data.scraping_phase is None

        # 模拟后台正在刮：单飞字典（id → 阶段）是状态的唯一事实源，
        # 阶段文案与整库刷新同一套，两处状态语言因此一致
        media_scrape._scraping[item_id] = "下载图片"
        try:
            resp = await get_library_item(library.id, item_id, _ADMIN, session)
            assert resp.data.scraping is True
            assert resp.data.scraping_phase == "下载图片"
        finally:
            media_scrape._scraping.pop(item_id, None)


async def test_artwork_candidates_mark_current_by_path(db, tmp_path) -> None:
    """「当前」按实际在用的路径判定，不能用"列表第一张"推断——策略升级前
    刮的条目、锁定的条目、TMDB 新增更高票的图都会让第一张不是在用的那张
    （实测：星际穿越的背景对不上）。在用的图不在候选里时补进列表首位。"""
    from movieclaw_api.services.media_scrape import list_artwork_candidates

    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        item_id = item.id

    _p, backdrops, _cp, current_backdrop = await list_artwork_candidates(item_id)
    assert current_backdrop == "/backdrop.jpg"
    assert [b["file_path"] for b in backdrops].count("/backdrop.jpg") == 1

    # 在用的图不在 TMDB 候选里（旧策略选的 / 已下架）：补进首位，仍标得出「当前」
    await select_artwork_for_test(item_id, "/legacy-pick.jpg")
    _p, backdrops, _cp, current_backdrop = await list_artwork_candidates(item_id)
    assert current_backdrop == "/legacy-pick.jpg"
    assert backdrops[0]["file_path"] == "/legacy-pick.jpg"


async def select_artwork_for_test(item_id: int, file_path: str) -> None:
    """直接改身份层的图片路径（绕过下载，只为构造"在用图不在候选里"的局面）。"""
    from movieclaw_db.engine import get_database

    async with get_database().session() as session:
        item = await session.get(MediaItem, item_id)
        item.backdrop_path = file_path
        session.add(item)
        await session.commit()


async def test_item_artwork_serves_local_file(db, tmp_path) -> None:
    root, entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        resp = await get_item_artwork(library.id, item.id, "fanart", session)
        assert str(resp.path) == str(entry / "fanart.jpg")


# ---------------------------------------------------------------------------
# 真实删除
# ---------------------------------------------------------------------------


async def test_delete_item_removes_whole_entry_dir(db, tmp_path) -> None:
    """删除的语义：整个刮削目录（视频+NFO+海报+字幕）一起清掉，台账行删除；
    库里其他条目不受影响。"""
    root, entry, _video = _make_movie_entry(tmp_path)
    bystander = root / "无关目录"
    bystander.mkdir()
    (bystander / "别的东西.txt").write_text("x", encoding="utf-8")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        resp = await delete_library_item(library.id, item.id, BackgroundTasks(), session)
        result = resp.data

    assert result.rows_deleted == 1 and not result.errors
    assert result.removed_paths == [str(entry)]
    assert not entry.exists()  # 目录整个没了，不留刮削残渣
    assert bystander.exists()  # 无关目录纹丝不动
    async with db.session() as session:
        assert (await session.execute(select(LibraryFile))).scalars().all() == []
        resp404 = None
        try:
            await get_library_item(library.id, item.id, _ADMIN, session)
        except NotFoundException as exc:
            resp404 = exc
        assert resp404 is not None


async def test_delete_item_degrades_when_dir_shared(db, tmp_path) -> None:
    """条目目录里混着其他条目的文件时不删目录，只删本条目文件及同名附属。"""
    root = tmp_path / "media" / "movies"
    shared = root / "合集目录"
    shared.mkdir(parents=True)
    mine = shared / "我的电影.mkv"
    mine.write_bytes(b"a")
    (shared / "我的电影.nfo").write_text("<movie><title>我</title></movie>", encoding="utf-8")
    others = shared / "别人的电影.mkv"
    others.write_bytes(b"b")

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
        item_a = MediaItem(kind="movie", tmdb_id=901, title="我的电影", original_title="Mine")
        item_b = MediaItem(kind="movie", tmdb_id=902, title="别人的电影", original_title="Other")
        session.add_all([item_a, item_b])
        await session.flush()
        session.add_all(
            [
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item_a.id,
                    season_number=0,
                    episode_number=0,
                    file_path=str(mine),
                    size_bytes=1,
                    source="scanned",
                ),
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item_b.id,
                    season_number=0,
                    episode_number=0,
                    file_path=str(others),
                    size_bytes=1,
                    source="scanned",
                ),
            ]
        )
        await session.commit()
        item_a_id = item_a.id

    async with db.session() as session:
        resp = await delete_library_item(library.id, item_a_id, BackgroundTasks(), session)
        assert not resp.data.errors

    assert shared.exists() and others.exists()  # 目录与他人文件保留
    assert not mine.exists()
    assert not (shared / "我的电影.nfo").exists()  # 同名附属一起清
    async with db.session() as session:
        remaining = (await session.execute(select(LibraryFile))).scalars().all()
        assert [r.file_path for r in remaining] == [str(others)]


async def test_delete_single_file_keeps_other_version(db, tmp_path) -> None:
    """双版本电影删一个版本：仅该文件+同名附属被删，另一版本与条目目录、
    NFO/海报等刮削产物完好，条目仍在库中。"""
    root, entry, video = _make_movie_entry(tmp_path)
    other = entry / "某电影.2020.2160p.WEB-DL.mkv"
    other.write_bytes(b"movie-4k-bytes")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    summary = await scan_library(library.id)
    assert summary.identified == 2

    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        target = (
            (await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(video))))
            .scalars()
            .one()
        )
        resp = await delete_library_file(library.id, item.id, target.id, BackgroundTasks(), session)
        result = resp.data

    assert result.rows_deleted == 1 and not result.errors
    assert not video.exists()
    assert not (entry / "某电影.2020.1080p.WEB-DL.chs&eng.srt").exists()  # 同名附属一起清
    assert other.exists() and entry.exists()  # 另一版本与条目目录不动
    assert (entry / "movie.nfo").exists() and (entry / "poster.jpg").exists()
    async with db.session() as session:
        detail = (await get_library_item(library.id, item.id, _ADMIN, session)).data
        assert detail.file_count == 1
        assert detail.files[0].file_path == str(other)


async def test_delete_single_file_last_file_upgrades_to_item_delete(db, tmp_path) -> None:
    """条目在本库的最后一个文件：升级为整条目删除，刮削目录整个清掉，
    不留只剩 NFO/海报的空壳。"""
    root, entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        row = (await session.execute(select(LibraryFile))).scalars().one()
        resp = await delete_library_file(library.id, item.id, row.id, BackgroundTasks(), session)

    assert resp.data.rows_deleted == 1 and not resp.data.errors
    assert resp.data.removed_paths == [str(entry)]
    assert not entry.exists()
    assert "最后一个文件" in resp.message
    async with db.session() as session:
        assert (await session.execute(select(LibraryFile))).scalars().all() == []


async def test_delete_single_file_episode_keeps_show_assets(db, tmp_path) -> None:
    """剧集删一集：该集文件与同名附属（分集 NFO/缩略图）被删，其他集与
    剧集级刮削产物（tvshow.nfo/海报）不动。"""
    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)"
    season = show / "Season 01"
    season.mkdir(parents=True)
    (show / "tvshow.nfo").write_text("<tvshow><title>测试剧集</title></tvshow>", encoding="utf-8")
    (show / "poster.jpg").write_bytes(b"poster")
    e1 = season / "测试剧集.S01E01.1080p.mkv"
    e1.write_bytes(b"e1")
    (season / "测试剧集.S01E01.1080p.nfo").write_text(
        "<episodedetails><title>E1</title></episodedetails>", encoding="utf-8"
    )
    (season / "测试剧集.S01E01.1080p-thumb.jpg").write_bytes(b"thumb")
    e2 = season / "测试剧集.S01E02.1080p.mkv"
    e2.write_bytes(b"e2")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    summary = await scan_library(library.id)
    assert summary.identified == 2

    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 200)))
            .scalars()
            .one()
        )
        target = (
            (await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(e1))))
            .scalars()
            .one()
        )
        resp = await delete_library_file(library.id, item.id, target.id, BackgroundTasks(), session)

    assert resp.data.rows_deleted == 1 and not resp.data.errors
    assert not e1.exists()
    assert not (season / "测试剧集.S01E01.1080p.nfo").exists()
    assert not (season / "测试剧集.S01E01.1080p-thumb.jpg").exists()
    assert e2.exists()  # 其他集不动
    assert (show / "tvshow.nfo").exists() and (show / "poster.jpg").exists()
    async with db.session() as session:
        remaining = (await session.execute(select(LibraryFile))).scalars().all()
        assert [r.file_path for r in remaining] == [str(e2)]


async def test_delete_single_file_missing_row_clears_ledger_only(db, tmp_path) -> None:
    """missing 行没有磁盘实体：删除只清台账，不碰磁盘上的任何东西。"""
    from datetime import datetime

    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    present = entry / "某电影.2020.1080p.mkv"
    present.write_bytes(b"a")

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
        item = MediaItem(kind="movie", tmdb_id=903, title="某电影", original_title="Some")
        session.add(item)
        await session.flush()
        session.add_all(
            [
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item.id,
                    season_number=0,
                    episode_number=0,
                    file_path=str(present),
                    size_bytes=1,
                    source="scanned",
                ),
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item.id,
                    season_number=0,
                    episode_number=0,
                    file_path=str(entry / "某电影.2020.2160p.mkv"),
                    size_bytes=1,
                    source="scanned",
                    missing_since=datetime(2024, 1, 1),
                    state=FileState.MISSING,
                ),
            ]
        )
        await session.commit()
        item_id = item.id

    async with db.session() as session:
        missing_row = (
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.missing_since.is_not(None))  # type: ignore[union-attr]
                )
            )
            .scalars()
            .one()
        )
        resp = await delete_library_file(
            library.id, item_id, missing_row.id, BackgroundTasks(), session
        )

    assert resp.data.rows_deleted == 1 and not resp.data.errors
    assert resp.data.removed_paths == []  # 没碰磁盘
    assert present.exists()
    async with db.session() as session:
        remaining = (await session.execute(select(LibraryFile))).scalars().all()
        assert [r.file_path for r in remaining] == [str(present)]


# ---------------------------------------------------------------------------
# 剧集分集
# ---------------------------------------------------------------------------


async def test_tv_episodes_merge_local_and_tmdb(db, tmp_path) -> None:
    """分集三源合并：E01 本地分集 NFO/缩略图优先，E02 由 TMDB 分季兜底，
    元数据有而库里没有的集照样列出（owned=false）。"""
    from movieclaw_api.api.routes.libraries import get_file_thumb, list_item_episodes

    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    e1 = show / "测试剧集.S01E01.1080p.mkv"
    e1.write_bytes(b"e1")
    (show / "测试剧集.S01E01.1080p.nfo").write_text(
        "<episodedetails><title>本地集名</title><plot>本地分集简介</plot>"
        "<aired>2024-01-01</aired></episodedetails>\n"
        "<episodedetails><title>第二段</title></episodedetails>",
        encoding="utf-8",
    )
    (show / "测试剧集.S01E01.1080p-thumb.jpg").write_bytes(b"thumb")
    (show / "测试剧集.S01E02.1080p.mkv").write_bytes(b"e2")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    summary = await scan_library(library.id)
    assert summary.identified == 2

    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 200)))
            .scalars()
            .one()
        )
        detail = (await get_library_item(library.id, item.id, _ADMIN, session)).data
        assert detail.seasons == [1]

        # 路由带上了 principal（分集随观看者带回进度/已看，member_id 由它来）——
        # 直调时按签名补齐，别再位置传参把 session 顶进 principal 槽
        resp = await list_item_episodes(library.id, item.id, 1, _ADMIN, session)
        episodes = {e.episode_number: e for e in resp.data.episodes}

    # 元数据有 E1/E2（TMDB 建档带回）；库里恰好也只有这两集
    ep1, ep2 = episodes[1], episodes[2]
    assert ep1.name == "本地集名" and ep1.overview == "本地分集简介"  # 本地 NFO 优先
    assert ep1.owned and len(ep1.file_ids) == 1
    assert ep1.still_url == f"/libraries/files/{ep1.file_ids[0]}/thumb"  # 本地缩略图
    assert ep2.name == "E2" and ep2.overview == "线上第二集简介"  # TMDB 兜底
    assert ep2.still_url and ep2.still_url.endswith("/w300/s1e2.jpg")

    # 本地缩略图接口按台账行回吐文件
    async with db.session() as session:
        thumb_resp = await get_file_thumb(ep1.file_ids[0], _ADMIN, session)
        assert str(thumb_resp.path).endswith("-thumb.jpg")


async def test_tv_episodes_list_missing_episode_from_metadata(db, tmp_path) -> None:
    """库里只有 E01 时，元数据里的 E02 也要列出（owned=false，前端置灰提示缺集）。"""
    from movieclaw_api.api.routes.libraries import list_item_episodes

    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "测试剧集.S01E01.1080p.mkv").write_bytes(b"e1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 200)))
            .scalars()
            .one()
        )
        # 路由带上了 principal（分集随观看者带回进度/已看，member_id 由它来）——
        # 直调时按签名补齐，别再位置传参把 session 顶进 principal 槽
        resp = await list_item_episodes(library.id, item.id, 1, _ADMIN, session)
    numbers = {(e.episode_number, e.owned) for e in resp.data.episodes}
    assert (1, True) in numbers and (2, False) in numbers


# ---------------------------------------------------------------------------
# 重新识别
# ---------------------------------------------------------------------------


async def test_reidentify_corrects_wrong_anchor(db, tmp_path) -> None:
    """错挂翻案：条目被挂错身份后重新识别，识别链重新收敛到正确条目，
    行原地改挂、原条目detail入口随之失效。"""
    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "测试剧集.S01E01.1080p.mkv").write_bytes(b"e1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)

    # 人为制造错挂：把行改锚到一个错误条目
    async with db.session() as session:
        wrong = MediaItem(kind="tv", tmdb_id=999, title="错挂剧", original_title="Wrong")
        session.add(wrong)
        await session.flush()
        row = (await session.execute(select(LibraryFile))).scalars().one()
        correct_id = row.media_item_id
        row.media_item_id = wrong.id
        await session.commit()
        wrong_id = wrong.id

    async with db.session() as session:
        resp = await reidentify_library_item(library.id, wrong_id, session)
        result = resp.data

    assert result.total == 1 and result.identified == 1
    assert result.changed is True
    assert result.new_media_item_id == correct_id
    assert result.new_title == "测试剧集"
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id == correct_id and row.unidentified_reason is None


async def test_reidentify_reports_pinned_identity(db, tmp_path) -> None:
    """身份被 NFO 钉死时重识别结果必然一致——响应要明说，引导用户改 NFO/认领。"""
    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        resp = await reidentify_library_item(library.id, item.id, session)
    assert resp.data.changed is False and resp.data.pinned_identity is True
    assert "NFO" in resp.data.message


async def test_reidentify_network_failure_keeps_identity(db, tmp_path, monkeypatch) -> None:
    """TMDB 网络不通时重识别不冲掉现有身份——用户要的是"重新刮削"，
    不是"断网就清档"；修好网络再点一次即可。"""
    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "测试剧集.S01E01.1080p.mkv").write_bytes(b"e1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        anchor = row.media_item_id

    async def boom(*args, **kwargs):
        raise RuntimeError("网络不可达")

    monkeypatch.setattr(scan_mod, "resolve_with_candidates", boom)
    async with db.session() as session:
        resp = await reidentify_library_item(library.id, anchor, session)
    assert resp.data.kept_on_error == 1 and resp.data.unidentified == 0
    assert "保留当前身份" in resp.data.message
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id == anchor  # 身份未被冲掉


async def test_reidentify_failure_moves_to_unidentified(db, tmp_path, monkeypatch) -> None:
    """识别不出时不静默保持旧身份：行进待识别清单（原因照记），可人工认领。"""
    root = tmp_path / "media" / "tv"
    show = root / "测试剧集 (2024)" / "Season 01"
    show.mkdir(parents=True)
    (show / "测试剧集.S01E01.1080p.mkv").write_bytes(b"e1")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        anchor = row.media_item_id

    async def none_resolve(*args, **kwargs):
        return ResolveOutcome()

    monkeypatch.setattr(scan_mod, "resolve_with_candidates", none_resolve)
    async with db.session() as session:
        resp = await reidentify_library_item(library.id, anchor, session)
    assert resp.data.identified == 0 and resp.data.unidentified == 1
    assert resp.data.new_media_item_id is None
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id is None
        assert row.unidentified_reason and "未能确认身份" in row.unidentified_reason


# ---------------------------------------------------------------------------
# 修正识别结果：预览只出结论、拍板才落库（issue #107）
# ---------------------------------------------------------------------------


async def _wrongly_anchored_movie(db, tmp_path) -> tuple[int, int, int]:
    """造一个错挂现场：正确身份 tmdb=300 的电影被挂到一个无关条目上。

    返回 ``(库 id, 错挂条目 id, 正确条目 id)``。
    """
    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)
    async with db.session() as session:
        wrong = MediaItem(kind="movie", tmdb_id=555, title="张冠李戴", original_title="Wrong")
        session.add(wrong)
        await session.flush()
        row = (await session.execute(select(LibraryFile))).scalars().one()
        correct_id = row.media_item_id
        row.media_item_id = wrong.id
        await session.commit()
        return library.id, wrong.id, correct_id


async def test_reidentify_preview_gives_verdict_without_touching_ledger(db, tmp_path) -> None:
    """第一阶段只出结论：预览跑完，错挂的行**仍然原样挂着错身份**。

    这是这个面板成立的前提——识别错挂时机器往往是高置信地错，重跑大概率
    复现同一个错答案，所以绝不能像旧的「重新识别」那样跑完直接改身份锚。
    """
    from movieclaw_api.api.routes.libraries import preview_reidentify_item

    library_id, wrong_id, correct_id = await _wrongly_anchored_movie(db, tmp_path)

    async with db.session() as session:
        view = (await preview_reidentify_item(library_id, wrong_id, session)).data

    assert view.current.media_item_id == wrong_id and view.current.title == "张冠李戴"
    assert len(view.groups) == 1
    group = view.groups[0]
    assert group.file_count == 1 and group.sample_names
    assert group.outcome.media_item_id == correct_id and group.outcome.tmdb_id == 300
    assert group.outcome.same_as_current is False
    # 身份由 NFO 钉死：面板要如实告知，否则用户改完不明白为何又被改回去
    assert view.pinned_identity is True
    # 台账零改动——这一条是本特性的硬约束
    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id == wrong_id


async def test_reidentify_preview_then_claim_applies_and_clears_orphan(db, tmp_path) -> None:
    """第二阶段：拍板走人工认领通道——身份转 manual，腾空的旧条目不留空壳。"""
    from movieclaw_api.api.routes.libraries import claim_files_batch
    from movieclaw_api.schemas.library import ClaimBatchPayload

    library_id, wrong_id, _correct_id = await _wrongly_anchored_movie(db, tmp_path)

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        resp = await claim_files_batch(
            ClaimBatchPayload(file_ids=[row.id], title_ref="tmdb:movie:300"),
            BackgroundTasks(),
            session,
        )
    assert resp.data["claimed"] == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id != wrong_id
        item = await session.get(MediaItem, row.media_item_id)
        assert item is not None and item.tmdb_id == 300
        # 人工拍板过的身份，对账机制永不自动翻案
        assert row.identity_source == IdentitySource.MANUAL
        # 旧条目已被腾空，孤儿清理的名单里应当有它（后台任务，这里只验语义前提）
        remaining = (
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.media_item_id == wrong_id)
                )
            )
            .scalars()
            .all()
        )
        assert not remaining


async def test_detach_marks_non_work_and_frees_the_slot(db, tmp_path) -> None:
    """「这不是独立作品」：花絮被高置信错挂时的出口——摘掉身份锚 + 忽略。

    只打忽略标记是不够的：``ignored_at`` 只让扫描不再过问，身份锚还在、
    条目照旧出现在库存里。锚必须一起摘掉，磁盘则一个字节都不动。
    """
    from movieclaw_api.api.routes.libraries import detach_files, list_unidentified
    from movieclaw_api.schemas.library import DetachPayload

    library_id, wrong_id, _correct_id = await _wrongly_anchored_movie(db, tmp_path)

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        file_path = row.file_path
        resp = await detach_files(DetachPayload(file_ids=[row.id]), BackgroundTasks(), session)
    assert resp.data["detached"] == 1

    async with db.session() as session:
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert row.media_item_id is None  # 不再占用库存
        assert row.ignored_at is not None  # 扫描不再过问
        # 不进待识别清单（用户已经表过态，别再来问一遍）
        assert (await list_unidentified(library_id, session)).data == []
    assert Path(file_path).is_file()  # 磁盘文件未受影响


async def test_actor_thumb_missing_only_when_tmdb_has_no_profile(db, tmp_path) -> None:
    """演职员头像为空的唯一成因是 TMDB 没有这个人的 profile_path，不是读取/入库丢数据。

    夹具里「线上演员甲」有 profile_path、「线上演员乙」没有。三条读路径
    （NFO / 库内档案 / TMDB 兜底）都必须：有图的拿到 w300 地址，没图的为 None
    且**仍然保留这一行**——姓名与角色本身就是信息，不能因为缺图就把人丢掉。
    """
    root = tmp_path / "media" / "movies"
    entry = root / "某电影 (2020)"
    entry.mkdir(parents=True)
    (entry / "某电影.2020.1080p.mkv").write_bytes(b"m")
    (entry / "movie.nfo").write_text(
        "<movie><title>某电影</title><tmdbid>300</tmdbid></movie>", encoding="utf-8"
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    await scan_library(library.id)

    async def actors_now():
        async with db.session() as session:
            item = (
                (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
                .scalars()
                .one()
            )
            resp = await get_library_item(library.id, item.id, _ADMIN, session)
        return resp.data.local_meta

    # 1) NFO 路径（我们自己写出的完整 NFO：有图的写 <thumb>，没图的不写）
    meta = await actors_now()
    assert meta is not None and meta.source == "nfo"
    assert [(a.name, bool(a.thumb_url)) for a in meta.actors] == [
        ("线上演员甲", True),
        ("线上演员乙", False),
    ]

    # 2) 库内档案路径（删掉 NFO，断网可用的第二层）
    (entry / "movie.nfo").unlink()
    meta = await actors_now()
    assert meta is not None and meta.source == "db"
    assert [(a.name, bool(a.thumb_url)) for a in meta.actors] == [
        ("线上演员甲", True),
        ("线上演员乙", False),
    ]


# ---------------------------------------------------------------------------
# 刮削归属库（docs/design/scrape-customization.md §14）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_detail_reports_and_pins_scrape_library(db, tmp_path) -> None:
    """详情页给出的归属库是**推断并固化后**的真实生效值，不是"列为空所以跟全局"。"""
    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    assert (await scan_library(library.id)).identified == 1

    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        # 扫描链路建档时就该把归属钉上；即便没钉，详情页读取也会推断出同一个库
        view = (await get_library_item(library.id, item.id, _ADMIN, session)).data

    assert view.scrape_library_id == library.id
    assert view.scrape_library_name == "电影库"

    async with db.session() as session:
        pinned = await session.get(MediaItem, view.media_item_id)
        assert pinned.scrape_library_id == library.id


@pytest.mark.asyncio
async def test_set_item_scrape_library_switches_and_resets(db, tmp_path) -> None:
    """切换归属库：改到同类型的另一个库可以，类型不符拒绝，null 恢复自动。"""
    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
        other = await LibraryRepository(session).create(
            name="4K 电影库", kind="movie", root_paths=[str(tmp_path / "uhd")]
        )
        tv = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(tmp_path / "tv")]
        )
    assert (await scan_library(library.id)).identified == 1

    async with db.session() as session:
        item = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .one()
        )
        item_id = item.id

        resp = await set_item_scrape_library(
            library.id, item_id, ScrapeLibraryPayload(target_library_id=other.id), session
        )
        assert resp.data["scrape_library_id"] == other.id

    async with db.session() as session:
        assert (await session.get(MediaItem, item_id)).scrape_library_id == other.id

    # 类型不符的库不能作为归属（电影库的口味不该套到剧集上，反之亦然）
    async with db.session() as session:
        with pytest.raises(BadRequestException):
            await set_item_scrape_library(
                library.id, item_id, ScrapeLibraryPayload(target_library_id=tv.id), session
            )

    # null = 恢复自动：清空后由推断接管（文件都在电影库，于是又回到它）
    async with db.session() as session:
        await set_item_scrape_library(
            library.id, item_id, ScrapeLibraryPayload(target_library_id=None), session
        )
    async with db.session() as session:
        assert (await session.get(MediaItem, item_id)).scrape_library_id is None
        view = (await get_library_item(library.id, item_id, _ADMIN, session)).data
    assert view.scrape_library_id == library.id


# ---------------------------------------------------------------------------
# 订阅追踪中（无库存文件）的条目：纯元数据入口不再被"没有库存文件"挡住（#278）
# ---------------------------------------------------------------------------


async def _make_tracked_fileless_item(db, library_id: int, *, tmdb_id: int = 300) -> int:
    """建一个"已订阅、尚无任何库存文件"的条目（媒体库页「追踪中」区块的形态）。"""
    from movieclaw_db.models import RuleSet, Subscription

    async with db.session() as session:
        item = MediaItem(
            kind="movie", tmdb_id=tmdb_id, title="某电影", original_title="Some Movie", year=2020
        )
        session.add(item)
        rule_set = RuleSet(name="默认规则组", is_default=True, spec={})
        session.add(rule_set)
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        session.add(
            Subscription(
                media_item_id=item.id,
                kind="movie",
                rule_set_id=rule_set.id,
                library_id=library_id,
            )
        )
        await session.commit()
        return item.id


async def test_fileless_tracked_item_opens_metadata_routes(db, tmp_path) -> None:
    """无库存文件的追踪中条目：重刮 / 候选图 / 选图三个入口都能用，选图不因
    没有条目目录而失败；刮削归属不是该库的照旧 404（#278）。"""
    from movieclaw_api.api.routes.libraries import (
        list_artwork_candidates_route,
        refresh_item_metadata,
        select_artwork_route,
    )
    from movieclaw_api.schemas.library import ArtworkSelectPayload
    from movieclaw_db.repositories import MediaItemRepository

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(tmp_path / "movies")]
        )
        other = await LibraryRepository(session).create(
            name="4K 电影库", kind="movie", root_paths=[str(tmp_path / "uhd")]
        )
    item_id = await _make_tracked_fileless_item(db, library.id)

    # 候选图：TMDB 候选照常返回
    async with db.session() as session:
        resp = await list_artwork_candidates_route(library.id, item_id, session)
        assert "/poster.jpg" in [p.file_path for p in resp.data.posters]

    # 重刮：能正常入队后台作业
    async with db.session() as session:
        resp = await refresh_item_metadata(library.id, item_id, client_name=None, session=session)
        assert resp.data["started"] is True

    # 选图：更新条目图片字段并加锁；镜像落盘对无文件条目是安全 no-op
    async with db.session() as session:
        await select_artwork_route(
            library.id,
            item_id,
            ArtworkSelectPayload(kind="poster", file_path="/poster.jpg"),
            session,
        )
    async with db.session() as session:
        refreshed = await session.get(MediaItem, item_id)
        assert refreshed.poster_path == "/poster.jpg"
        meta = await MediaItemRepository(session).get_metadata(item_id)
        assert meta.poster_locked is True
        # 校验时按订阅目标库推断出的归属已固化
        assert refreshed.scrape_library_id == library.id

    # 刮削归属不是该库 → 条目不属于那个库，仍然 404
    async with db.session() as session:
        with pytest.raises(NotFoundException):
            await list_artwork_candidates_route(other.id, item_id, session)


async def test_library_refresh_targets_include_fileless_tracked_items(db, tmp_path) -> None:
    """整库刷新的目标 = 有台账行的条目 ∪ 刮削归属到该库的无文件条目——
    追踪中条目从此进刷新范围，改选图偏好后不再静默失效（#278）。"""
    from movieclaw_api.services import media_scrape

    root, _entry, _video = _make_movie_entry(tmp_path)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    assert (await scan_library(library.id)).identified == 1
    tracked_id = await _make_tracked_fileless_item(db, library.id, tmdb_id=969681)

    async with db.session() as session:
        # 无订阅也无归属的纯浏览条目不进名单（它不在任何库的界面上展示）
        stray = MediaItem(
            kind="movie", tmdb_id=301, title="路过的电影", original_title="Passerby", year=2021
        )
        session.add(stray)
        await session.commit()
        await session.refresh(stray)
        stray_id = stray.id

        created = await media_scrape.enqueue_library_metadata_refresh_job(
            session, library.id, "电影库"
        )
        target_ids = {t["media_item_id"] for t in created.job.input_data["targets"]}

    async with db.session() as session:
        scanned = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 300)))
            .scalars()
            .all()
        )
    scanned_ids = {i.id for i in scanned}
    assert tracked_id in target_ids
    assert scanned_ids & target_ids  # 有文件的条目照旧在名单里
    assert stray_id not in target_ids
