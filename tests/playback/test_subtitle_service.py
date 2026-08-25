"""字幕播放领域服务（B 层，docs/design/jellyfin-subtitle.md §3）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from movieclaw_db.models import LibraryFile
from movieclaw_playback.subtitles import (
    SUBTITLE_OFF,
    SubtitleRef,
    SubtitleServeError,
    embedded_track,
    external_track,
    is_ai_generated,
    parse_embedded_track,
    parse_external_track,
    pick_default_subtitle,
    resolve_default_audio,
    resolve_default_subtitle,
    resolve_external_subtitle,
    serve_subtitle,
)

SRT = "1\n00:00:01,000 --> 00:00:02,500\n简体中文字幕测试\n\n"


# ---------------------------------------------------------------------------
# 中性轨引用（§3.1）
# ---------------------------------------------------------------------------


def test_track_ref_roundtrip() -> None:
    assert parse_embedded_track(embedded_track(2)) == 2
    assert parse_external_track(external_track("Movie.chs.srt")) == "Movie.chs.srt"


def test_ai_subtitle_detection() -> None:
    # subtitle_gen 的三种命名（单语 / 指定语言 / 双语）与 v2.1 旧版命名
    assert is_ai_generated("ai") is True
    assert is_ai_generated("ai-cht") is True
    assert is_ai_generated("ai-bilingual-chs-eng") is True
    assert is_ai_generated("AI") is True


def test_ai_subtitle_detection_ignores_publisher_tracks() -> None:
    # 判据是台账解析出的 title 段（视频 stem 之后的 token），不是整个文件名：
    # 拿文件名去找会把片名叫《A.I.》《AI》的片子全标成 AI 生成
    assert is_ai_generated(None) is False
    assert is_ai_generated("") is False
    assert is_ai_generated("SubHD") is False
    assert is_ai_generated("air") is False
    assert is_ai_generated("A.I.Artificial.Intelligence") is False


def test_track_ref_rejects_garbage() -> None:
    assert parse_embedded_track("external:x.srt") is None
    assert parse_embedded_track("embedded:notanint") is None
    assert parse_embedded_track("embedded:-1") is None
    assert parse_external_track("embedded:0") is None
    assert parse_external_track("external:") is None


# ---------------------------------------------------------------------------
# 内容服务（§3.2：编码归一 + srt↔vtt）
# ---------------------------------------------------------------------------


def _file_with_subs(path: str, externals: list[dict]) -> LibraryFile:
    return LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path=path,
        external_subtitles=externals,
        source="scanned",
    )


def test_resolve_external_subtitle_builds_path(tmp_path: Path) -> None:
    f = _file_with_subs(
        str(tmp_path / "Movie.mkv"),
        [{"filename": "Movie.chs.srt", "format": "srt"}],
    )
    ref = resolve_external_subtitle(f, "external:Movie.chs.srt")
    assert ref == SubtitleRef(path=tmp_path / "Movie.chs.srt", format="srt")


def test_resolve_external_subtitle_rejects_embedded_and_unknown(tmp_path: Path) -> None:
    f = _file_with_subs(str(tmp_path / "Movie.mkv"), [])
    assert resolve_external_subtitle(f, "embedded:0") is None
    assert resolve_external_subtitle(f, "external:nope.srt") is None


def test_serve_gbk_srt_normalized_to_utf8(tmp_path: Path) -> None:
    """GBK 中文字幕 → UTF-8 输出（中文用户核心价值）。"""
    sub = tmp_path / "Movie.chs.srt"
    sub.write_bytes(SRT.encode("gbk"))
    content, mime = serve_subtitle(SubtitleRef(path=sub, format="srt"), "srt")
    assert mime == "application/x-subrip"
    assert "简体中文字幕测试" in content.decode("utf-8")


def test_serve_big5_normalized_to_utf8(tmp_path: Path) -> None:
    text = "1\n00:00:01,000 --> 00:00:02,000\n繁體中文字幕測試繁體字幕\n\n"
    sub = tmp_path / "Movie.cht.srt"
    sub.write_bytes(text.encode("big5"))
    content, _ = serve_subtitle(SubtitleRef(path=sub, format="srt"), None)
    assert "繁體中文字幕測試" in content.decode("utf-8")


def test_serve_srt_to_vtt(tmp_path: Path) -> None:
    sub = tmp_path / "Movie.srt"
    sub.write_text(SRT, encoding="utf-8")
    content, mime = serve_subtitle(SubtitleRef(path=sub, format="srt"), "vtt")
    text = content.decode("utf-8")
    assert mime == "text/vtt"
    assert text.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in text  # vtt 用点不用逗号
    assert "简体中文字幕测试" in text


def test_serve_ass_passthrough_but_no_cross_conversion(tmp_path: Path) -> None:
    ass = tmp_path / "Movie.ass"
    ass.write_text(
        "[Script Info]\nTitle: t\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n",
        encoding="utf-8",
    )
    content, mime = serve_subtitle(SubtitleRef(path=ass, format="ass"), "ass")
    assert mime == "text/x-ssa"
    assert "你好" in content.decode("utf-8")
    # 白名单外的组合仍要拒绝（ass→vtt 已为画中画放行，见 _CONVERTIBLE）
    with pytest.raises(SubtitleServeError):
        serve_subtitle(SubtitleRef(path=ass, format="ass"), "srt")


def test_serve_empty_format_means_source_format(tmp_path: Path) -> None:
    sub = tmp_path / "Movie.srt"
    sub.write_text(SRT, encoding="utf-8")
    _, mime = serve_subtitle(SubtitleRef(path=sub, format="srt"), None)
    assert mime == "application/x-subrip"


def test_serve_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SubtitleServeError):
        serve_subtitle(SubtitleRef(path=tmp_path / "gone.srt", format="srt"), "srt")


# ---------------------------------------------------------------------------
# 默认字幕轨选择（§3.4：外挂 ↓ → default ↓ → 非 forced ↓，forced 沉底）
# ---------------------------------------------------------------------------


def _file_tracks(
    embedded: list[dict] | None, external: list[dict] | None
) -> LibraryFile:
    return LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path="/media/Movie.mkv",
        subtitle_streams=embedded,
        external_subtitles=external,
        source="scanned",
    )


def test_pick_external_wins_over_embedded_default() -> None:
    f = _file_tracks(
        [{"codec": "subrip", "default": True}],
        [{"filename": "Movie.chs.srt", "format": "srt"}],
    )
    assert pick_default_subtitle(f) == "external:Movie.chs.srt"


def test_pick_default_flag_over_plain_embedded() -> None:
    f = _file_tracks(
        [{"codec": "subrip"}, {"codec": "ass", "default": True}],
        [],
    )
    assert pick_default_subtitle(f) == "embedded:1"


def test_pick_forced_sinks_below_full_subtitle() -> None:
    # forced 是外语片段部分字幕：有完整字幕可选时绝不选它（v4 终审修正项）
    f = _file_tracks(
        [{"codec": "subrip", "forced": True}, {"codec": "subrip", "default": True}],
        [],
    )
    assert pick_default_subtitle(f) == "embedded:1"


def test_pick_forced_only_when_nothing_else() -> None:
    f = _file_tracks([{"codec": "subrip", "forced": True}], [])
    assert pick_default_subtitle(f) == "embedded:0"


def test_pick_none_when_no_flags_and_no_external() -> None:
    # 只有无旗标的内封轨：不自动开字幕（Default 模式过滤条件不命中）
    f = _file_tracks([{"codec": "subrip"}], [])
    assert pick_default_subtitle(f) is None


def test_pick_external_default_flag_first_among_externals() -> None:
    f = _file_tracks(
        [],
        [
            {"filename": "Movie.eng.srt", "format": "srt"},
            {"filename": "Movie.chs.default.srt", "format": "srt", "default": True},
        ],
    )
    assert pick_default_subtitle(f) == "external:Movie.chs.default.srt"


def test_pick_ai_external_first_among_externals() -> None:
    """产品拍板（2026-08-25）：外挂里有 AI 生成的字幕就最优先——它是为
    这部片现生成的，比随片源顺来的外挂更可能是用户想看的那条。
    AI 优先级压过外挂的 default 旗标。"""
    f = _file_tracks(
        [{"codec": "subrip", "default": True}],
        [
            {"filename": "Movie.chs.default.srt", "format": "srt", "default": True},
            {"filename": "Movie.ai-zh.srt", "format": "srt", "title": "ai-zh"},
        ],
    )
    assert pick_default_subtitle(f) == "external:Movie.ai-zh.srt"


def test_pick_ai_marker_reads_title_not_filename() -> None:
    """判 AI 只认解析出来的 title 段：片名叫《AI》的片子不能被误标。"""
    f = _file_tracks(
        [],
        [
            {"filename": "AI.2024.chs.srt", "format": "srt", "title": "chs"},
            {"filename": "AI.2024.eng.srt", "format": "srt", "title": "eng"},
        ],
    )
    # 没有真 AI 轨：照旧外挂第一条
    assert pick_default_subtitle(f) == "external:AI.2024.chs.srt"


# ---------------------------------------------------------------------------
# 记忆裁决（§3.3/§3.4：off 恒生效、失效回落）
# ---------------------------------------------------------------------------


def test_resolve_default_off_always_wins() -> None:
    f = _file_tracks([], [{"filename": "Movie.chs.srt", "format": "srt"}])
    assert resolve_default_subtitle(f, SUBTITLE_OFF) == SUBTITLE_OFF


def test_resolve_default_remembered_valid() -> None:
    f = _file_tracks([{"codec": "subrip"}], [])
    assert resolve_default_subtitle(f, "embedded:0") == "embedded:0"


def test_resolve_default_stale_falls_back_to_policy() -> None:
    f = _file_tracks([], [{"filename": "Movie.chs.srt", "format": "srt"}])
    # 记忆指向已删除的字幕文件 → 回落选择策略（选现存外挂）
    assert resolve_default_subtitle(f, "external:gone.srt") == "external:Movie.chs.srt"


def test_resolve_default_audio_validity() -> None:
    f = LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path="/m.mkv",
        audio_streams=[{"codec": "aac"}, {"codec": "dts"}],
        source="scanned",
    )
    assert resolve_default_audio(f, "embedded:1") == 1
    assert resolve_default_audio(f, "embedded:5") is None  # 悬空
    assert resolve_default_audio(f, None) is None


# ---------------------------------------------------------------------------
# 架构守护：B 层不 import 协议层（jellyfin-subtitle.md §6）
# ---------------------------------------------------------------------------


def test_playback_domain_never_imports_jellyfin_layer() -> None:
    import ast

    src = Path(__file__).resolve().parents[2] / "src" / "movieclaw_playback"
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith("movieclaw_jellyfin"), (
                    f"领域层文件 {py.name} import 了协议层 {name}——"
                    "Jellyfin 只是翻译皮，方言不得渗入领域层"
                )


def test_ass_converts_to_vtt_for_pip(tmp_path):
    """ass→vtt 降级（画中画用）：丢样式、保文本与时间轴。"""
    ass = tmp_path / "Movie.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR,"
        " MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\b1}你好{\\b0}\n",
        encoding="utf-8",
    )
    body, mime = serve_subtitle(SubtitleRef(path=ass, format="ass"), "vtt")
    text = body.decode("utf-8")
    assert text.startswith("WEBVTT")
    assert "你好" in text
    assert mime == "text/vtt"


def test_vtt_cues_get_default_bottom_safe_line(tmp_path):
    """VTT 输出给无定位的 cue 加 line:84%（系统层字幕位置的唯一控制手段）；
    作者显式定位过的 cue 原样尊重。"""
    sub = tmp_path / "Movie.vtt"
    sub.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n你好\n\n"
        "00:00:03.000 --> 00:00:04.000 line:10%\n顶置注释\n",
        encoding="utf-8",
    )
    body, _ = serve_subtitle(SubtitleRef(path=sub, format="vtt"), "vtt")
    text = body.decode("utf-8")
    assert "00:00:01.000 --> 00:00:02.000 line:84%" in text
    assert "00:00:03.000 --> 00:00:04.000 line:10%" in text
