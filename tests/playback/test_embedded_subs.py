"""内封字幕轨抽取测试（docs/design/web-player.md §6.2）。

PT 片源的字幕绝大多数是内封的，只服务外挂等于对大部分片子没字幕——所以这条
路径的正确性直接决定「能不能看」。

格式判定与缓存新鲜度是纯逻辑，进 CI 默认门禁；真抽取标 ``integration``，
用 ffmpeg 合成带字幕的样本，验证 **ASS 保住样式、文本轨转 SRT**。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

import movieclaw_api.services.playback.embedded_subs as embedded_subs
from movieclaw_api.services.playback.embedded_subs import (
    embedded_subtitle_format,
    embedded_track_codec,
    extract_embedded_subtitle,
    extract_embedded_subtitle_async,
)
from movieclaw_db.models import FileSource, FileState, LibraryFile


def make_file(tmp_path: Path, streams: list[dict] | None, *, path: Path | None = None):
    return LibraryFile(
        id=7,
        library_id=1,
        media_item_id=1,
        file_path=str(path or (tmp_path / "movie.mkv")),
        size_bytes=1,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
        container="mkv",
        subtitle_streams=streams,
    )


# ---------------------------------------------------------------------------
# 格式判定：ASS 保原样，文本转 SRT，图形轨一律拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("codec", ["ass", "ssa", "ASS"])
def test_ass_tracks_keep_their_format(codec):
    """转成 VTT 就丢掉特效与排版，番剧字幕直接崩。"""
    assert embedded_subtitle_format(codec) == "ass"


@pytest.mark.parametrize("codec", ["subrip", "srt", "mov_text", "text", "webvtt"])
def test_text_tracks_normalize_to_srt(codec):
    assert embedded_subtitle_format(codec) == "srt"


@pytest.mark.parametrize("codec", ["hdmv_pgs_subtitle", "pgssub", "PGS"])
def test_pgs_tracks_extract_as_sup(codec):
    """蓝光位图轨抽成 .sup 交前端 libbitsub 渲染（Jellyfin 10.9+ 同路）。"""
    assert embedded_subtitle_format(codec) == "sup"


@pytest.mark.parametrize("codec", ["dvd_subtitle", "", None, "xyz"])
def test_unknown_tracks_are_refused(codec):
    """宁可少一条轨也不烧录——烧录会把任何档位拖进全转码。"""
    assert embedded_subtitle_format(codec) is None


# ---------------------------------------------------------------------------
# 轨定位：数组下标即 ffmpeg 的 0:s:<k>
# ---------------------------------------------------------------------------


def test_track_codec_comes_from_the_ledger(tmp_path):
    file = make_file(tmp_path, [{"codec": "ass"}, {"codec": "subrip"}])
    assert embedded_track_codec(file, 0) == "ass"
    assert embedded_track_codec(file, 1) == "subrip"


@pytest.mark.parametrize("index", [-1, 2, 99])
def test_out_of_range_track_is_none(tmp_path, index):
    file = make_file(tmp_path, [{"codec": "ass"}, {"codec": "subrip"}])
    assert embedded_track_codec(file, index) is None


def test_unprobed_file_has_no_tracks(tmp_path):
    """subtitle_streams=None 表示没探测过，不能当成「没有字幕」。"""
    assert embedded_track_codec(make_file(tmp_path, None), 0) is None


def test_malformed_ledger_entry_does_not_crash(tmp_path):
    file = make_file(tmp_path, ["不是字典", {"codec": "ass"}])
    assert embedded_track_codec(file, 0) is None
    assert embedded_track_codec(file, 1) == "ass"


def test_unsupported_track_never_touches_ffmpeg(tmp_path, monkeypatch):
    """格式判定要在起进程之前完成——VobSub 轨不该白白读一遍整个容器。"""
    called = []
    monkeypatch.setattr(
        "movieclaw_api.services.playback.embedded_subs.subprocess.run",
        lambda *a, **k: called.append(a),
    )
    file = make_file(tmp_path, [{"codec": "dvd_subtitle"}])
    assert extract_embedded_subtitle(file, 0) is None
    assert called == []


def test_missing_video_file_is_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    file = make_file(tmp_path, [{"codec": "subrip"}], path=tmp_path / "gone.mkv")
    assert extract_embedded_subtitle(file, 0) is None


@pytest.mark.asyncio
async def test_async_extraction_cancels_ffmpeg_and_removes_partial_file(tmp_path, monkeypatch):
    """播放器放弃字幕请求时，PGS 抽取进程和临时文件都必须回收。"""
    monkeypatch.chdir(tmp_path)
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"source")
    file = make_file(tmp_path, [{"codec": "hdmv_pgs_subtitle"}], path=video)

    started = asyncio.Event()
    killed = asyncio.Event()
    commands: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class HangingProcess:
        pid = 12345
        returncode: int | None = None

        async def communicate(self):
            started.set()
            await killed.wait()
            self.returncode = -15
            return b"", b""

    process = HangingProcess()

    async def fake_create_subprocess_exec(*argv, **kwargs):
        commands.append((argv, kwargs))
        return process

    def fake_signal(_pid, _sig):
        process.returncode = -15
        killed.set()

    monkeypatch.setattr(embedded_subs.shutil, "which", lambda _name: "/fake/ffmpeg")
    monkeypatch.setattr(
        embedded_subs.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(embedded_subs, "_signal_process_group", fake_signal)

    pending = asyncio.create_task(extract_embedded_subtitle_async(file, 0))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    await asyncio.wait_for(killed.wait(), timeout=1)
    for _ in range(3):
        await asyncio.sleep(0)
    cache = tmp_path / "data" / "cache" / "playback-subs"
    leftovers = list(cache.glob("*")) if cache.exists() else []
    assert leftovers == []
    assert commands and "-nostdin" in commands[0][0]


# ---------------------------------------------------------------------------
# 真抽取
# ---------------------------------------------------------------------------

integration = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="需要系统 ffmpeg"
)


def _run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, timeout=180)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-1500:]


@pytest.fixture
def video_with_subs(tmp_path):
    """合成一个内封 ASS + 内封 SRT 双字幕轨的 MKV。"""
    ass = tmp_path / "styled.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour\n"
        "Style: Karaoke,Arial,72,&H00FF00FF\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:00.50,0:00:03.00,Karaoke,{\\pos(320,240)}特效字幕\n",
        encoding="utf-8",
    )
    srt = tmp_path / "plain.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:03,000\n纯文本字幕\n\n", encoding="utf-8")
    video = tmp_path / "movie.mkv"
    _run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=4",
        "-i", str(ass), "-i", str(srt),
        "-map", "0:v", "-map", "1:s", "-map", "2:s",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:s:0", "ass", "-c:s:1", "srt",
        "-y", str(video),
    ])
    return video


@integration
@pytest.mark.integration
def test_extracts_ass_track_preserving_styles(tmp_path, monkeypatch, video_with_subs):
    """ASS 必须原样 copy 出来——样式段和定位标签都得在，否则 JASSUB 渲染出的
    就是一行没有排版的白字。"""
    monkeypatch.chdir(tmp_path)
    file = make_file(tmp_path, [{"codec": "ass"}, {"codec": "subrip"}], path=video_with_subs)
    ref = extract_embedded_subtitle(file, 0)
    assert ref is not None and ref.format == "ass"
    text = ref.path.read_text(encoding="utf-8")
    assert "[V4+ Styles]" in text
    assert "Karaoke" in text
    assert "\\pos(320,240)" in text
    assert "特效字幕" in text


@integration
@pytest.mark.integration
def test_extracts_text_track_as_srt(tmp_path, monkeypatch, video_with_subs):
    monkeypatch.chdir(tmp_path)
    file = make_file(tmp_path, [{"codec": "ass"}, {"codec": "subrip"}], path=video_with_subs)
    ref = extract_embedded_subtitle(file, 1)
    assert ref is not None and ref.format == "srt"
    text = ref.path.read_text(encoding="utf-8")
    assert "纯文本字幕" in text
    assert "-->" in text


@integration
@pytest.mark.integration
def test_second_call_reuses_the_cache(tmp_path, monkeypatch, video_with_subs):
    """抽取要通读整个容器，不能每次点开字幕都重来一遍。"""
    monkeypatch.chdir(tmp_path)
    file = make_file(tmp_path, [{"codec": "ass"}], path=video_with_subs)
    first = extract_embedded_subtitle(file, 0)
    assert first is not None
    stamp = first.path.stat().st_mtime_ns

    calls = []
    real_run = subprocess.run
    monkeypatch.setattr(
        "movieclaw_api.services.playback.embedded_subs.subprocess.run",
        lambda *a, **k: (calls.append(a), real_run(*a, **k))[1],
    )
    second = extract_embedded_subtitle(file, 0)
    assert second is not None and second.path == first.path
    assert calls == [], "命中缓存却又起了一次 ffmpeg"
    assert second.path.stat().st_mtime_ns == stamp


@integration
@pytest.mark.integration
def test_failed_extraction_leaves_no_partial_file(tmp_path, monkeypatch, video_with_subs):
    """失败留下的非空残片会被下次的新鲜度判断误认成成功产物。"""
    monkeypatch.chdir(tmp_path)
    # 台账说有第 3 条轨，实际没有——ffmpeg 会以非零码退出
    file = make_file(
        tmp_path,
        [{"codec": "ass"}, {"codec": "subrip"}, {"codec": "subrip"}],
        path=video_with_subs,
    )
    assert extract_embedded_subtitle(file, 2) is None
    cache = tmp_path / "data" / "cache" / "playback-subs"
    leftovers = list(cache.glob("*")) if cache.exists() else []
    assert leftovers == [], f"留下了残片：{leftovers}"


# ---------------------------------------------------------------------------
# 内嵌字体：ASS 特效字幕的另一半
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SourceHanSans.ttf", "SourceHanSans.ttf"),
        ("Font Name.otf", "Font Name.otf"),
        ("字体.ttf", "字体.ttf"),
        # 文件名来自媒体文件本体，是不可信输入——只取 basename
        ("../../etc/passwd.ttf", "passwd.ttf"),
        ("/abs/evil.ttf", "evil.ttf"),
        ("..\\\\windows\\\\evil.ttf", None),  # 反斜杠不在白名单里
        ("script.sh", None),  # 不是字体扩展名
        ("payload.ttf.sh", None),
        ("", None),
        ("x" * 200 + ".ttf", None),  # 超长
    ],
)
def test_font_name_sanitizing(raw, expected):
    from movieclaw_api.services.playback.embedded_subs import safe_font_name

    assert safe_font_name(raw) == expected


def test_font_extraction_without_ffmpeg_is_empty(tmp_path, monkeypatch):
    from movieclaw_api.services.playback.embedded_subs import extract_embedded_fonts

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "movieclaw_api.services.playback.embedded_subs.shutil.which", lambda _n: None
    )
    assert extract_embedded_fonts(make_file(tmp_path, None)) == []


@integration
@pytest.mark.integration
def test_extracts_attached_fonts(tmp_path, monkeypatch):
    """番剧的 ASS 把字体作为附件放在 MKV 里；不抽出来字幕会回退成默认字体。"""
    from movieclaw_api.services.playback.embedded_subs import (
        extract_embedded_fonts,
        font_cache_dir,
    )

    system_font = next(Path("/usr/share/fonts").rglob("*.ttf"), None)
    if system_font is None:
        pytest.skip("本机没有可用作附件的 ttf 字体")

    video = tmp_path / "anime.mkv"
    _run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=2",
        "-attach", str(system_font),
        "-metadata:s:t:0", "mimetype=application/x-truetype-font",
        "-map", "0:v", "-c:v", "libx264", "-preset", "ultrafast",
        "-y", str(video),
    ])
    monkeypatch.chdir(tmp_path)
    file = make_file(tmp_path, [{"codec": "ass"}], path=video)
    names = extract_embedded_fonts(file)
    assert names == [system_font.name]
    dumped = font_cache_dir(file.id) / names[0]
    assert dumped.is_file()
    # 抽出来的必须是有效字体，否则 JASSUB 加载会静默失败
    assert dumped.read_bytes()[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")


@integration
@pytest.mark.integration
def test_second_font_extraction_reuses_cache(tmp_path, monkeypatch):
    from movieclaw_api.services.playback.embedded_subs import extract_embedded_fonts

    system_font = next(Path("/usr/share/fonts").rglob("*.ttf"), None)
    if system_font is None:
        pytest.skip("本机没有可用作附件的 ttf 字体")
    video = tmp_path / "anime.mkv"
    _run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=2",
        "-attach", str(system_font),
        "-metadata:s:t:0", "mimetype=application/x-truetype-font",
        "-map", "0:v", "-c:v", "libx264", "-preset", "ultrafast",
        "-y", str(video),
    ])
    monkeypatch.chdir(tmp_path)
    file = make_file(tmp_path, [{"codec": "ass"}], path=video)
    assert extract_embedded_fonts(file)

    calls = []
    monkeypatch.setattr(
        "movieclaw_api.services.playback.embedded_subs.subprocess.run",
        lambda *a, **k: calls.append(a),
    )
    assert extract_embedded_fonts(file)
    assert calls == [], "命中缓存却又读了一遍容器"


@integration
@pytest.mark.integration
def test_video_without_attachments_is_remembered(tmp_path, monkeypatch):
    """没有字体的片子也要记住结论，否则每次点开 ASS 都白读一遍整个容器。"""
    from movieclaw_api.services.playback.embedded_subs import extract_embedded_fonts

    video = tmp_path / "plain.mkv"
    _run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=2",
        "-c:v", "libx264", "-preset", "ultrafast", "-y", str(video),
    ])
    monkeypatch.chdir(tmp_path)
    file = make_file(tmp_path, [{"codec": "ass"}], path=video)
    assert extract_embedded_fonts(file) == []

    calls = []
    monkeypatch.setattr(
        "movieclaw_api.services.playback.embedded_subs.subprocess.run",
        lambda *a, **k: calls.append(a),
    )
    assert extract_embedded_fonts(file) == []
    assert calls == []


@pytest.fixture
def video_with_pgs(tmp_path):
    """合成带真 PGS 位图轨的 MKV。ffmpeg 没有 PGS 编码器，字幕流由
    pgs_sup 按段格式手工拼出，muxer 只做流拷贝。"""
    from pgs_sup import make_sup

    sup = tmp_path / "sample.sup"
    sup.write_bytes(make_sup())
    video = tmp_path / "pgs-movie.mkv"
    _run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=6",
        "-f", "sup", "-i", str(sup),
        "-map", "0:v", "-map", "1:s",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:s", "copy",
        "-y", str(video),
    ])
    return video


@integration
@pytest.mark.integration
def test_extracts_pgs_track_as_binary_sup(tmp_path, monkeypatch, video_with_pgs):
    """PGS 轨抽成 .sup：二进制原样拷贝，段魔数在、ffprobe 认得出。"""
    monkeypatch.chdir(tmp_path)
    file = make_file(tmp_path, [{"codec": "hdmv_pgs_subtitle"}], path=video_with_pgs)
    ref = extract_embedded_subtitle(file, 0)
    assert ref is not None and ref.format == "sup"
    raw = ref.path.read_bytes()
    assert raw.startswith(b"PG"), "sup 段魔数丢失——不是合法的 HDMV PGS 流"
    assert len(raw) > 100
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "sup", "-show_streams", str(ref.path)],
        capture_output=True, timeout=60,
    )
    assert b"hdmv_pgs_subtitle" in probe.stdout
