"""扫描的介质规格补探阶段。

这一段存在的唯一理由：ffprobe 是**后装**的时候，扫描的主循环对已识别且
在位的行整体秒过，永远走不到探测那一步——「装好 ffmpeg 再重新扫描」这个
最直觉的动作必须真的有效。ffprobe 与 TMDB 均为假实现。
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.items as items_mod
import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.media_discover as discover_mod
import movieclaw_api.services.media_probe as probe_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.scan import ScanSummary, scan_library
from movieclaw_api.services.media_probe import MediaSpec
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import LibraryFile
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"

_SPEC = MediaSpec(
    resolution="1080p",
    video_codec="hevc",
    hdr=None,
    bit_depth=10,
    duration_seconds=5400,
    bit_rate=8_000_000,
    frame_rate=23.976,
    color_space="BT.709",
    audio_streams=[{"codec": "eac3", "channels": 6, "language": "chi", "default": True}],
    subtitle_streams=[{"codec": "subrip", "language": "chi"}],
)


def _body(tmdb_id: int) -> dict:
    return {
        "id": tmdb_id,
        "title": f"影片{tmdb_id}",
        "original_title": f"M{tmdb_id}",
        "release_date": "2020-01-01",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
        "overview": "简介。",
        "vote_average": 7.0,
        "runtime": 100,
        "genres": [],
        "images": {"posters": [], "backdrops": []},
        "release_dates": {"results": []},
        "credits": {"cast": [], "crew": []},
    }


@pytest.fixture(autouse=True)
def _fresh_probe_retry_state():
    """探测失败记忆是进程级内存表，测试之间必须互不串味。"""
    probe_mod._retry_state.clear()
    yield
    probe_mod._retry_state.clear()


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()

    def handler(request: httpx.Request) -> httpx.Response:
        seg = request.url.path.rstrip("/").split("/")[-1]
        if seg.isdigit():
            return httpx.Response(200, json=_body(int(seg)))
        return httpx.Response(200, json={"results": []})

    client = TmdbClient(api_key=_KEY, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _no_ffprobe(monkeypatch) -> None:
    """模拟"系统里没装 ffmpeg"：探测一律失败，可用性检查为假。"""
    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: False)
    monkeypatch.setattr(scan_mod, "probe_media", lambda *_a, **_k: None)
    monkeypatch.setattr(items_mod, "probe_media", lambda *_a, **_k: None)


def _with_ffprobe(monkeypatch, calls: list | None = None) -> None:
    """模拟"ffmpeg 已装"：探测返回固定规格。"""

    def _probe(path, *_a, **_k):
        if calls is not None:
            calls.append(str(path))
        return _SPEC

    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: True)
    monkeypatch.setattr(scan_mod, "probe_media", _probe)
    monkeypatch.setattr(items_mod, "probe_media", _probe)


def _with_failing_ffprobe(monkeypatch, calls: list | None = None) -> None:
    """模拟"ffmpeg 已装但探测失败"（半截文件/挂载抖动/坏文件）。"""

    def _probe(path, *_a, **_k):
        if calls is not None:
            calls.append(str(path))
        return None

    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: True)
    monkeypatch.setattr(scan_mod, "probe_media", _probe)
    monkeypatch.setattr(items_mod, "probe_media", _probe)


def _rewind_retry_state(seconds: float) -> None:
    """把失败记忆的失败时刻拨到 seconds 秒之前，模拟退避到点。"""
    now = time.monotonic()
    for path, (failures, _last) in list(probe_mod._retry_state.items()):
        probe_mod._retry_state[path] = (failures, now - seconds)


async def _build(db, tmp_path, count: int = 3) -> int:
    root = tmp_path / "media" / "movies"
    for i in range(count):
        entry = root / f"影片{3000 + i} (2020)"
        entry.mkdir(parents=True)
        (entry / f"影片{3000 + i}.1080p.mkv").write_bytes(b"z" * (200 + i))
        (entry / "movie.nfo").write_text(
            f"<movie><tmdbid>{3000 + i}</tmdbid></movie>", encoding="utf-8"
        )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    return library.id


async def _rows(db) -> list[LibraryFile]:
    async with db.session() as session:
        return list((await session.execute(select(LibraryFile))).scalars().all())


async def test_rescan_backfills_specs_after_ffmpeg_is_installed(db, tmp_path, monkeypatch) -> None:
    """先在没有 ffprobe 的环境入库，装上之后重扫必须把规格补齐。

    这是本模块的正题：主循环对"已识别且在位"的行整体秒过，补探阶段是
    这些行唯一的救赎路径。
    """
    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path)
    await scan_library(library_id)

    rows = await _rows(db)
    assert len(rows) == 3
    assert all(r.audio_streams is None and r.resolution is None for r in rows)

    # 装上 ffmpeg，重新扫描
    _with_ffprobe(monkeypatch)
    summary = await scan_library(library_id)

    assert summary.skipped_known == 3, "主循环仍然应当秒过这些行——补探是独立阶段"
    assert summary.probed == 3
    rows = await _rows(db)
    assert all(r.resolution == "1080p" and r.video_codec == "hevc" for r in rows)
    assert all(r.audio_streams and r.audio_streams[0]["codec"] == "eac3" for r in rows)
    assert all(r.subtitle_streams for r in rows)


async def test_backfill_is_idempotent(db, tmp_path, monkeypatch) -> None:
    """补齐之后再扫不再重复探测——探过的行 audio_streams 非 NULL，直接出局。"""
    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path)
    await scan_library(library_id)

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    await scan_library(library_id)
    assert len(calls) == 3
    calls.clear()

    summary = await scan_library(library_id)
    assert summary.probed == 0
    assert calls == []


async def test_background_scan_skips_historical_spec_backfill(db, tmp_path, monkeypatch) -> None:
    """后台对账和目录事件只盘点台账，不能对历史文件逐个启动 ffprobe。"""
    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path)
    await scan_library(library_id)

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    summary = await scan_library(library_id, backfill_existing_specs=False)

    assert summary.probed == 0
    assert calls == []
    rows = await _rows(db)
    assert all(row.audio_streams is None for row in rows)


async def test_periodic_reconcile_skips_historical_spec_backfill(db, tmp_path, monkeypatch) -> None:
    """定时对账关闭全量补探（避免后台周期扫到全库历史规格），
    只带限量自愈参数与失败记忆的到点点名。"""
    library_id = await _build(db, tmp_path, count=1)
    calls: list[dict] = []

    async def fake_scan(current_library_id: int, **kwargs) -> ScanSummary:
        calls.append({"library_id": current_library_id, **kwargs})
        return ScanSummary(library_id=current_library_id)

    monkeypatch.setattr(scan_mod, "scan_library", fake_scan)
    await scan_mod.reconcile_libraries()

    assert len(calls) == 1
    assert calls[0]["library_id"] == library_id
    assert calls[0]["backfill_existing_specs"] is False
    assert calls[0]["probe_retry_limit"] == scan_mod._RECONCILE_PROBE_LIMIT
    assert calls[0]["reprobe_paths"] is None  # 失败记忆为空时不点名


async def test_backfill_skipped_when_ffprobe_missing(db, tmp_path, monkeypatch) -> None:
    """没装 ffprobe 时整段跳过：每轮扫描白跑一遍必然失败的探测毫无意义。"""
    calls: list[str] = []

    def _probe(path, *_a, **_k):
        calls.append(str(path))
        return None

    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: False)
    monkeypatch.setattr(scan_mod, "probe_media", _probe)
    monkeypatch.setattr(items_mod, "probe_media", _probe)

    library_id = await _build(db, tmp_path)
    await scan_library(library_id)
    calls.clear()  # 首轮入账时每个新文件探一次是正常的

    summary = await scan_library(library_id)
    assert summary.probed == 0
    assert calls == [], "可用性检查为假时不该再调 ffprobe"


async def test_backfill_skips_strm_rows(db, tmp_path, monkeypatch) -> None:
    """strm 占位行不参与补探：本体没有媒体流，探了必失败，只会污染进度分母。"""
    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path, count=1)
    entry = tmp_path / "media" / "movies" / "影片4000 (2020)"
    entry.mkdir(parents=True)
    (entry / "影片4000.strm").write_text("https://cloud.example.com/m.mkv", encoding="utf-8")
    (entry / "movie.nfo").write_text("<movie><tmdbid>4000</tmdbid></movie>", encoding="utf-8")
    await scan_library(library_id)

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    summary = await scan_library(library_id)

    assert summary.probed == 1, "只有真视频那行需要补探"
    assert all(not c.endswith(".strm") for c in calls)
    rows = {Path(r.file_path).suffix: r for r in await _rows(db)}
    assert rows[".strm"].audio_streams is None and rows[".strm"].resolution is None
    assert rows[".mkv"].audio_streams is not None


async def test_backfill_leaves_missing_and_ignored_rows_alone(db, tmp_path, monkeypatch) -> None:
    """已标记 missing 与用户忽略过的行不参与补探。

    前者文件根本不在磁盘上（探了也白探）；后者是用户明确处理完的决定，
    不该因为一次扫描又被摸一遍。
    """
    from movieclaw_db.models import utcnow

    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path)
    await scan_library(library_id)

    rows = await _rows(db)
    async with db.session() as session:
        missing = await session.get(LibraryFile, rows[0].id)
        ignored = await session.get(LibraryFile, rows[1].id)
        # 文件真的从磁盘删掉，否则重扫会把它当"文件回归"重新入账（那条路径
        # 本来就会探测，测不出补探阶段的取舍）
        Path(missing.file_path).unlink()
        missing.mark_missing()
        ignored.ignored_at = utcnow()
        await session.commit()

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    summary = await scan_library(library_id)

    assert summary.probed == 1  # 只剩第三行
    assert len(calls) == 1
    after = {r.id: r for r in await _rows(db)}
    assert after[rows[0].id].audio_streams is None
    assert after[rows[1].id].audio_streams is None
    assert after[rows[2].id].audio_streams is not None


def _clpi_with_languages() -> bytes:
    """一条中文音轨 + 一条中文字幕的最小 CLPI ProgramInfo。"""
    streams = [
        (0x1100, bytes([0x83, 0]) + b"eng"),
        (0x1200, bytes([0x90]) + b"zho" + b"\0"),
    ]
    entries = bytearray()
    for pid, coding_info in streams:
        entries.extend(pid.to_bytes(2, "big"))
        entries.append(len(coding_info))
        entries.extend(coding_info)
    body = bytearray(b"\0\1")
    body.extend((0).to_bytes(4, "big"))
    body.extend((0x100).to_bytes(2, "big"))
    body.extend(bytes([len(streams), 0]))
    body.extend(entries)
    header = bytearray(32)
    header[:8] = b"HDMV0200"
    header[12:16] = (32).to_bytes(4, "big")
    return bytes(header) + len(body).to_bytes(4, "big") + bytes(body)


async def test_backfill_enriches_existing_bluray_once_by_pid(db, tmp_path, monkeypatch) -> None:
    """已有流但 language=NULL 的 BDMV 也进入补探；版本写入后重扫不再读 m2ts。"""
    root = tmp_path / "bluray-library"
    disc = root / "1917 (2019)"
    stream_dir = disc / "BDMV" / "STREAM"
    clipinf_dir = disc / "BDMV" / "CLIPINF"
    stream_dir.mkdir(parents=True)
    clipinf_dir.mkdir(parents=True)
    stream = stream_dir / "00294.m2ts"
    stream.write_bytes(b"movie")
    (clipinf_dir / "00294.clpi").write_bytes(_clpi_with_languages())

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="蓝光库", kind="movie", root_paths=[str(root)]
        )
        session.add(
            LibraryFile(
                library_id=library.id,
                file_path=str(disc),
                size_bytes=5,
                container="bluray",
                source="scanned",
                audio_streams=[{"codec": "truehd", "language": None}],
                subtitle_streams=[{"codec": "hdmv_pgs_subtitle", "language": None}],
            )
        )
        await session.commit()

    spec = MediaSpec(
        resolution="1080p",
        video_codec="h264",
        hdr=None,
        bit_depth=8,
        duration_seconds=7200,
        bit_rate=20_000_000,
        frame_rate=24.0,
        color_space="BT.709",
        audio_streams=[{"codec": "truehd", "pid": 0x1100, "language": None}],
        subtitle_streams=[{"codec": "hdmv_pgs_subtitle", "pid": 0x1200, "language": None}],
    )
    calls: list[str] = []

    def fake_probe(path):
        calls.append(str(path))
        return spec

    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: True)
    monkeypatch.setattr(items_mod, "probe_media", fake_probe)

    async with db.session() as session:
        summary = ScanSummary(library_id=library.id)
        state = scan_mod.ScanState(phase=scan_mod.ScanPhase.WALKING)
        await scan_mod._probe_backfill(session, library.id, summary, state)
        row = (await session.execute(select(LibraryFile))).scalars().one()
        assert summary.probed == 1
        assert row.audio_streams[0]["language"] == "eng"
        assert row.subtitle_streams[0]["language"] == "zho"
        assert row.subtitle_streams[0]["language_source"] == "clpi"

    async with db.session() as session:
        second = ScanSummary(library_id=library.id)
        await scan_mod._probe_backfill(
            session,
            library.id,
            second,
            scan_mod.ScanState(phase=scan_mod.ScanPhase.WALKING),
        )
        assert second.probed == 0
    assert calls == [str(stream)]


async def test_backfill_missing_clpi_still_completes_candidate_progress(
    db, tmp_path, monkeypatch
) -> None:
    """无 CLPI 时不重读 m2ts，但该候选已检查完，进度不能停在 0/N。"""
    root = tmp_path / "bluray-library"
    disc = root / "No CLPI (2020)"
    stream_dir = disc / "BDMV" / "STREAM"
    stream_dir.mkdir(parents=True)
    (stream_dir / "00001.m2ts").write_bytes(b"movie")

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="蓝光库", kind="movie", root_paths=[str(root)]
        )
        session.add(
            LibraryFile(
                library_id=library.id,
                file_path=str(disc),
                size_bytes=5,
                container="bluray",
                source="scanned",
                audio_streams=[{"codec": "truehd", "language": None}],
                subtitle_streams=[{"codec": "hdmv_pgs_subtitle", "language": None}],
            )
        )
        await session.commit()

    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: True)

    def unexpected_probe(_path):
        raise AssertionError("没有 CLPI 时不应重读 m2ts")

    monkeypatch.setattr(items_mod, "probe_media", unexpected_probe)
    state = scan_mod.ScanState(phase=scan_mod.ScanPhase.WALKING)
    async with db.session() as session:
        summary = ScanSummary(library_id=library.id)
        await scan_mod._probe_backfill(session, library.id, summary, state)

    assert summary.probed == 0
    assert state.processed == state.total == 1


# ---------------------------------------------------------------------------
# 探测失败记忆与退避重试（media_probe 的进程内失败表）
# ---------------------------------------------------------------------------


def test_retry_backoff_escalates() -> None:
    """连续失败翻倍退避，封顶 24 小时——纯函数，直接验时间表。"""
    hour = 3600.0
    assert probe_mod._retry_due((1, 0.0), hour - 1) is False
    assert probe_mod._retry_due((1, 0.0), hour) is True
    assert probe_mod._retry_due((3, 0.0), 4 * hour - 1) is False
    assert probe_mod._retry_due((3, 0.0), 4 * hour) is True
    assert probe_mod._retry_due((10, 0.0), 24 * hour - 1) is False  # 封顶后不再翻倍
    assert probe_mod._retry_due((10, 0.0), 24 * hour) is True


async def test_manual_scan_backs_off_recent_probe_failures(db, tmp_path, monkeypatch) -> None:
    """探测失败进退避：手动重扫不再每轮为同一个坏文件全额付一次超时。"""
    calls: list[str] = []
    _with_failing_ffprobe(monkeypatch, calls)
    library_id = await _build(db, tmp_path, count=1)
    await scan_library(library_id)  # 入库探测失败，进入失败记忆
    assert len(calls) == 1
    calls.clear()

    summary = await scan_library(library_id)  # 退避未到点：补探阶段跳过该行
    assert summary.probed == 0
    assert calls == []

    _rewind_retry_state(2 * 3600)  # 退避到点，且这次文件恢复可读
    _with_ffprobe(monkeypatch, calls)
    summary = await scan_library(library_id)
    assert summary.probed == 1
    row = (await _rows(db))[0]
    assert row.audio_streams and row.resolution == "1080p"
    assert row.file_path not in probe_mod._retry_state, "成功后失败记忆应被抹掉"


async def test_reconcile_style_scan_heals_failed_probe_after_backoff(
    db, tmp_path, monkeypatch
) -> None:
    """无 watch 库的自愈路径：对账限量补探到点重试，退避未到点不动媒体盘。"""
    _with_failing_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path, count=1)
    await scan_library(library_id)

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    summary = await scan_library(library_id, backfill_existing_specs=False, probe_retry_limit=10)
    assert summary.probed == 0 and calls == [], "退避未到点，对账不该重试"

    _rewind_retry_state(2 * 3600)
    summary = await scan_library(library_id, backfill_existing_specs=False, probe_retry_limit=10)
    assert summary.probed == 1
    assert (await _rows(db))[0].audio_streams is not None


async def test_reconcile_budget_caps_probe_count(db, tmp_path, monkeypatch) -> None:
    """ffprobe 后装的存量库：对账每轮只补一小批，绝不演变成整库读盘。"""
    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path)  # 3 个文件，规格全 NULL 且无失败记录
    await scan_library(library_id)

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    summary = await scan_library(library_id, backfill_existing_specs=False, probe_retry_limit=2)
    assert summary.probed == 2 and len(calls) == 2

    summary = await scan_library(library_id, backfill_existing_specs=False, probe_retry_limit=2)
    assert summary.probed == 1, "下一轮接着补剩下的行"
    rows = await _rows(db)
    assert all(row.audio_streams is not None for row in rows)
