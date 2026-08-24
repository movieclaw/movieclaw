"""转码执行的端到端验证（docs/design/web-player.md §9.3）。

**真起 ffmpeg，真用 ffprobe 断言产物**——参数对不等于跑出来对。这里覆盖的是
「§7 的陷阱在真实 ffmpeg 上确实被挡住了」：

- hvc1 标签真的写进了 init.mp4（不加就是 hev1，Safari 静默黑屏）；
- 分片首帧真的是关键帧（切在非关键帧上会花屏）；
- 时长与源片一致、时间戳单调；
- 降混后的声道数、转码后的编码都符合计划。

标 ``integration``：需要系统里有 ffmpeg/ffprobe，CI 的 ``-m "not integration"``
会跳过。样本由 ffmpeg 自己合成（几百 KB），不依赖任何外部素材。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from movieclaw_api.services.playback.ffmpeg_args import (
    INIT_NAME,
    PLAYLIST_NAME,
    build_hls_command,
)
from movieclaw_playback.decide import (
    AudioPlan,
    PlaybackPlan,
    PlaybackTier,
    VideoPlan,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="需要系统 ffmpeg/ffprobe",
    ),
]

DURATION = 10


def _run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, timeout=180)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-2000:]


def _probe(path: Path, *entries: str) -> dict:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-2000:]
    return json.loads(proc.stdout)


def _stream(payload: dict, kind: str) -> dict:
    return next(s for s in payload["streams"] if s["codec_type"] == kind)


@pytest.fixture(scope="module")
def samples(tmp_path_factory) -> dict[str, Path]:
    """合成黄金样本：H.264+AAC 的 MKV，和 HEVC+5.1 AC3 的 MKV。

    后者正是 PT 场景的典型形态——浏览器既不认 MKV 容器，也（多半）不认 AC3，
    但视频码流可以原样直通。
    """
    root = tmp_path_factory.mktemp("player-corpus")
    h264 = root / "h264-aac.mkv"
    _run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
        "-c:a", "aac", "-shortest", "-y", str(h264),
    ])
    hevc = root / "hevc-ac3.mkv"
    _run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none",
        "-g", "50", "-keyint_min", "50",
        "-c:a", "ac3", "-ac", "6", "-shortest", "-y", str(hevc),
    ])
    return {"h264": h264, "hevc": hevc}


def _plan(tier: PlaybackTier, *, video: VideoPlan, audio: AudioPlan) -> PlaybackPlan:
    return PlaybackPlan(
        tier=tier, file_id=1, container="hls-fmp4",
        video=video, audio=audio, reason="集成测试",
    )


def _execute(plan: PlaybackPlan, source: Path, out: Path, **kwargs):
    out.mkdir(parents=True, exist_ok=True)
    cmd = build_hls_command(plan, source_path=str(source), session_dir=out, **kwargs)
    _run(cmd.argv)
    return cmd


# ---------------------------------------------------------------------------
# 档 1 Remux 直通
# ---------------------------------------------------------------------------


def test_remux_mkv_to_fmp4_preserves_streams_and_duration(samples, tmp_path):
    """MKV → fMP4 分片，码流不重编码，时长不变。"""
    plan = _plan(
        PlaybackTier.REMUX,
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
    )
    cmd = _execute(plan, samples["h264"], tmp_path / "remux")

    assert cmd.playlist_path.exists() and cmd.init_path.exists()
    payload = _probe(cmd.playlist_path)
    assert _stream(payload, "video")["codec_name"] == "h264"
    assert _stream(payload, "audio")["codec_name"] == "aac"
    assert abs(float(payload["format"]["duration"]) - DURATION) < 1.0


def test_hevc_remux_writes_hvc1_tag(samples, tmp_path):
    """§7-①：不打 hvc1 标签，Safari 是静默黑屏——没有 error、没有日志。"""
    plan = _plan(
        PlaybackTier.REMUX,
        video=VideoPlan(action="copy", codec="hevc"),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
    )
    cmd = _execute(plan, samples["hevc"], tmp_path / "hevc")
    assert _stream(_probe(cmd.init_path), "video")["codec_tag_string"] == "hvc1"


def test_every_segment_starts_on_a_keyframe(samples, tmp_path):
    """§7-②：分片切在非关键帧上会花屏黑屏。"""
    plan = _plan(
        PlaybackTier.REMUX,
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
    )
    cmd = _execute(plan, samples["h264"], tmp_path / "kf")
    init = cmd.init_path.read_bytes()
    segments = sorted(cmd.playlist_path.parent.glob("seg*.m4s"))
    assert segments, "没有产出任何分片"
    for segment in segments:
        joined = tmp_path / "joined.mp4"
        joined.write_bytes(init + segment.read_bytes())
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "packet=flags", "-read_intervals", "%+#1",
                "-of", "csv=p=0", str(joined),
            ],
            capture_output=True, timeout=60,
        )
        first = proc.stdout.decode().strip().splitlines()[0]
        assert first.startswith("K"), f"{segment.name} 首帧不是关键帧：{first}"


def test_seek_produces_shorter_output_starting_at_zero(samples, tmp_path):
    """会话时间轴恒从 0 起（实测取舍，见 ffmpeg_args 模块文档）。

    关键帧回退让实际起点可能早于请求值——方向是「稍早」而非「跳过内容」。
    """
    plan = _plan(
        PlaybackTier.REMUX,
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
    )
    cmd = _execute(plan, samples["h264"], tmp_path / "seek", start_ms=4000)
    payload = _probe(cmd.playlist_path)
    assert float(payload["format"]["start_time"]) == pytest.approx(0.0, abs=0.2)
    remaining = float(payload["format"]["duration"])
    assert 4.0 < remaining <= DURATION  # 掐掉了开头，但不会掐过头


# ---------------------------------------------------------------------------
# 档 2 音频单转：视频原样直通，只动音轨
# ---------------------------------------------------------------------------


def test_audio_only_transcode_keeps_video_bitstream(samples, tmp_path):
    plan = _plan(
        PlaybackTier.AUDIO_TRANSCODE,
        video=VideoPlan(action="copy", codec="hevc"),
        audio=AudioPlan(
            action="transcode", track_ref="embedded:0",
            codec="aac", channels=2, downmix=True,
        ),
    )
    cmd = _execute(plan, samples["hevc"], tmp_path / "audio")
    payload = _probe(cmd.playlist_path)
    video = _stream(payload, "video")
    audio = _stream(payload, "audio")
    assert video["codec_name"] == "hevc"  # 视频没被重编码
    assert _stream(_probe(cmd.init_path), "video")["codec_tag_string"] == "hvc1"
    assert audio["codec_name"] == "aac"
    assert audio["channels"] == 2  # 5.1 已降混
    assert abs(float(payload["format"]["duration"]) - DURATION) < 1.0


# ---------------------------------------------------------------------------
# 档 4 软件转码
# ---------------------------------------------------------------------------


def test_software_transcode_scales_and_reencodes(samples, tmp_path):
    plan = _plan(
        PlaybackTier.SOFTWARE_TRANSCODE,
        video=VideoPlan(action="transcode", codec="h264", height=120),
        audio=AudioPlan(action="transcode", track_ref="embedded:0", codec="aac", channels=2),
    )
    cmd = _execute(plan, samples["hevc"], tmp_path / "sw")
    payload = _probe(cmd.playlist_path)
    video = _stream(payload, "video")
    assert video["codec_name"] == "h264"
    assert video["height"] == 120
    assert _stream(payload, "audio")["codec_name"] == "aac"


def test_software_transcode_segments_match_target(samples, tmp_path):
    """转码档自己控制 GOP，分片能精确对齐目标时长（直通档做不到）。"""
    plan = _plan(
        PlaybackTier.SOFTWARE_TRANSCODE,
        video=VideoPlan(action="transcode", codec="h264", height=120),
        audio=AudioPlan(action="transcode", track_ref="embedded:0", codec="aac", channels=2),
    )
    cmd = _execute(plan, samples["hevc"], tmp_path / "sw2")
    durations = [
        float(line.split(":")[1].rstrip(","))
        for line in cmd.playlist_path.read_text().splitlines()
        if line.startswith("#EXTINF")
    ]
    assert durations
    # 上限 = SEGMENT_SECONDS + 半秒容差（末段除外）
    assert all(d <= 4.5 for d in durations[:-1]), durations


# ---------------------------------------------------------------------------
# 硬边界 1：字幕绝不进容器
# ---------------------------------------------------------------------------


def test_output_never_contains_subtitle_stream(samples, tmp_path):
    with_subs = tmp_path / "with-subs.mkv"
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:05,000\n测试字幕\n\n", encoding="utf-8")
    _run([
        "ffmpeg", "-v", "error", "-i", str(samples["h264"]), "-i", str(srt),
        "-map", "0", "-map", "1", "-c", "copy", "-c:s", "srt", "-y", str(with_subs),
    ])
    assert any(s["codec_type"] == "subtitle" for s in _probe(with_subs)["streams"])

    plan = _plan(
        PlaybackTier.REMUX,
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
    )
    cmd = _execute(plan, with_subs, tmp_path / "nosubs")
    assert not any(s["codec_type"] == "subtitle" for s in _probe(cmd.playlist_path)["streams"])


# ---------------------------------------------------------------------------
# 多音轨：按中性引用选对轨
# ---------------------------------------------------------------------------


def test_selects_requested_audio_track(samples, tmp_path):
    """第二条音轨是 48kHz 单声道，用它和第一条区分开。"""
    multi = tmp_path / "multi-audio.mkv"
    _run([
        "ffmpeg", "-v", "error",
        "-i", str(samples["h264"]),
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={DURATION}",
        "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a:0", "copy", "-c:a:1", "aac", "-ac:a:1", "1",
        "-shortest", "-y", str(multi),
    ])
    plan = _plan(
        PlaybackTier.REMUX,
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref="embedded:1"),
    )
    cmd = _execute(plan, multi, tmp_path / "track1")
    assert _stream(_probe(cmd.playlist_path), "audio")["channels"] == 1


# ---------------------------------------------------------------------------
# 产物形态：playlist 结构与 fMP4
# ---------------------------------------------------------------------------


def test_playlist_is_fmp4_event_playlist(samples, tmp_path):
    plan = _plan(
        PlaybackTier.REMUX,
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
    )
    cmd = _execute(plan, samples["h264"], tmp_path / "shape")
    text = cmd.playlist_path.read_text()
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in text
    assert f'#EXT-X-MAP:URI="{INIT_NAME}"' in text
    assert cmd.playlist_path.name == PLAYLIST_NAME
    assert ".m4s" in text and ".ts" not in text  # CMAF，不是 MPEG-TS
