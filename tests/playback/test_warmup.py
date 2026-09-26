"""起播预热的挑选与去重逻辑（services/playback/warmup.py）。

预热本体是「调一个已有缓存的探测函数」，这里测的是**什么时候调、调什么**
——对整季剧集开闸或顺手通读整个文件，预热就从省 IO 变成烧 IO。
"""

from __future__ import annotations

import asyncio

import pytest

from movieclaw_api.services.playback import warmup
from movieclaw_db.models import FileSource, FileState, LibraryFile
from movieclaw_playback.capability import (
    AudioSupport,
    ClientCapability,
    VideoSupport,
    universal_capability,
)
from movieclaw_playback.decide import PlaybackPolicy


def make_file(tmp_path, streams, *, file_id=1, container="mkv", codec="h264") -> LibraryFile:
    path = tmp_path / f"movie{file_id}.{container}"
    path.write_bytes(b"x")
    return LibraryFile(
        id=file_id,
        library_id=1,
        media_item_id=1,
        file_path=str(path),
        size_bytes=1,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
        duration_seconds=600,
        container=container,
        video_codec=codec,
        resolution="1080p",
        bit_depth=8,
        audio_streams=[{"codec": "aac", "channels": 2, "default": True}],
        subtitle_streams=streams,
    )


#: 真实浏览器的能力档案（与 test_decide 同款）：不认 mkv，mkv/H.264 在它上面走直通。
CHROME = ClientCapability(
    video=(VideoSupport("h264"), VideoSupport("vp9"), VideoSupport("av1")),
    audio=(AudioSupport("aac"), AudioSupport("opus"), AudioSupport("flac")),
    containers=frozenset({"mp4", "hls-fmp4"}),
)
CHROME_UA = "Mozilla/5.0 (Macintosh) Chrome/140.0"
IOS_UA = "MovieClaw/1 CFNetwork/3860 Darwin/25.0"
ME = "admin:yee"


# ---------------------------------------------------------------------------
# 预热只做关键帧采样，不碰内封字幕
# ---------------------------------------------------------------------------


def test_warmup_never_extracts_embedded_subtitles(tmp_path, monkeypatch):
    """详情接口会被批量调用（UI 测试逐个打开几十部电影）。

    预热里若抽字幕，每部都是一次整文件通读——2026-09 NAS 一天因此白读几百 GB。
    字幕改为播放器请求时按需抽取，预热里不能再起 ffmpeg。
    """
    import movieclaw_api.services.media_extract as media_extract

    probed = []

    def fake_probe(path, duration):
        probed.append(path)
        return 2.0

    async def never(*_a, **_k):  # pragma: no cover
        raise AssertionError("详情页预热起了字幕抽取的 ffmpeg")

    monkeypatch.setattr(warmup, "probe_keyframe_interval", fake_probe)
    monkeypatch.setattr(media_extract.asyncio, "create_subprocess_exec", never)
    file = make_file(tmp_path, [{"codec": "subrip", "default": True}])

    asyncio.run(warmup._warm_file(file))
    assert probed == [file.file_path]


# ---------------------------------------------------------------------------
# schedule：只替「可能直通」的已知网页客户端采样；剧集不开闸、并发去重
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    warmup._in_flight.clear()
    warmup._capabilities.clear()

    async def nas_policy():  # 用户的 NAS：没有核显
        return PlaybackPolicy(hardware_available=False)

    monkeypatch.setattr(
        "movieclaw_api.services.playback.plan.load_policy", nas_policy
    )
    yield
    warmup._in_flight.clear()
    warmup._capabilities.clear()


@pytest.fixture
def probed(monkeypatch):
    """记录真正读盘采样的文件（把 ffprobe 替换成计数）。"""
    calls: list[str] = []

    def fake_probe(path, duration):
        calls.append(path)
        return 2.0

    monkeypatch.setattr(warmup, "probe_keyframe_interval", fake_probe)
    return calls


def _run_schedule(media_item_id, files, *, user_agent):
    async def run():
        warmup.schedule(media_item_id, files, identity=ME, user_agent=user_agent)
        for _ in range(20):  # 让后台任务与 to_thread 跑完
            await asyncio.sleep(0.01)

    asyncio.run(run())


def test_chrome_opening_a_remux_candidate_is_warmed(tmp_path, probed):
    """网页端播过片（上报过能力），打开一部 mkv/H.264：直通候选，值得提前采样。"""
    warmup.remember_capability(ME, CHROME_UA, CHROME)
    movie = make_file(tmp_path, [])
    _run_schedule(1, [movie], user_agent=CHROME_UA)
    assert probed == [movie.file_path]


def test_unknown_client_is_never_warmed(tmp_path, probed):
    """没上报过能力的客户端（App、脚本、UI 测试批量打开详情）一律不读盘。

    2026-09 NAS 实测：iOS 打开一部 15 GB 的 mkv 详情，预热白读 171 MB。
    """
    _run_schedule(1, [make_file(tmp_path, [])], user_agent=IOS_UA)
    _run_schedule(1, [make_file(tmp_path, [])], user_agent=None)
    assert probed == []


def test_same_account_on_another_device_does_not_borrow_capability(tmp_path, probed):
    """同一账号在 Chrome 播过，不代表它在 iOS App 上打开详情也要替网页采样。"""
    warmup.remember_capability(ME, CHROME_UA, CHROME)
    _run_schedule(1, [make_file(tmp_path, [])], user_agent=IOS_UA)
    assert probed == []


def test_full_decode_clients_are_never_warmed(tmp_path, probed):
    """全解码客户端直读原文件，关键帧密度对它毫无用处。"""
    warmup.remember_capability(ME, IOS_UA, universal_capability())
    _run_schedule(1, [make_file(tmp_path, [])], user_agent=IOS_UA)
    assert probed == []


def test_direct_play_and_must_transcode_files_are_skipped(tmp_path, probed):
    """网页端直放的 mp4、注定转码的 HEVC（Chrome 不解），采样结论都用不上。"""
    warmup.remember_capability(ME, CHROME_UA, CHROME)
    mp4 = make_file(tmp_path, [], file_id=1, container="mp4")
    hevc = make_file(tmp_path, [], file_id=2, codec="hevc")
    _run_schedule(1, [mp4, hevc], user_agent=CHROME_UA)
    assert probed == []


def test_series_with_many_files_is_skipped(tmp_path, probed):
    """整季剧集详情页猜不到用户要播哪集，全预热是几 GB 的无谓 IO。"""
    warmup.remember_capability(ME, CHROME_UA, CHROME)
    files = [make_file(tmp_path, [], file_id=i) for i in range(1, 7)]
    _run_schedule(1, files, user_agent=CHROME_UA)
    assert probed == []
    assert 1 not in warmup._in_flight


def test_concurrent_schedule_runs_once(tmp_path, probed):
    """详情页反复刷新不能叠加探测 IO。"""
    warmup.remember_capability(ME, CHROME_UA, CHROME)
    files = [make_file(tmp_path, [], file_id=1)]

    async def run():
        warmup.schedule(1, files, identity=ME, user_agent=CHROME_UA)
        warmup.schedule(1, files, identity=ME, user_agent=CHROME_UA)  # 第一次还没跑完
        for _ in range(20):
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert len(probed) == 1
    assert 1 not in warmup._in_flight  # 结束后放行下一次


def test_warm_failure_never_leaks_inflight(tmp_path, monkeypatch):
    """预热炸了要放行下一次，否则这个条目在进程生命周期里永远不再预热。"""

    def boom(f):
        raise RuntimeError("存储抖了一下")

    monkeypatch.setattr(warmup, "_warm_file", boom)
    warmup.remember_capability(ME, CHROME_UA, CHROME)
    _run_schedule(1, [make_file(tmp_path, [], file_id=1)], user_agent=CHROME_UA)
    assert 1 not in warmup._in_flight


def test_remembered_clients_are_bounded(tmp_path):
    """能力表按客户端计、有上限：长期运行不会因 User-Agent 五花八门而无限增长。"""
    for i in range(warmup._MAX_CLIENTS + 10):
        warmup.remember_capability(ME, f"UA-{i}", CHROME)
    assert len(warmup._capabilities) == warmup._MAX_CLIENTS
    assert warmup._known_capability(ME, "UA-0") is None
    assert warmup._known_capability(ME, f"UA-{warmup._MAX_CLIENTS + 9}") is CHROME
