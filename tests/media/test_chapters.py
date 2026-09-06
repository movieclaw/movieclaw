"""章节的纯函数（docs/design/video-chapters.md §4.1/§4.2）：ffprobe 章节解析、
标题规范化、按时长合成、有效章节判定。都不依赖 ffprobe。"""

from __future__ import annotations

from movieclaw_api.services.library.chapters import (
    chapter_image_map,
    effective_chapters,
    synth_count,
    synthesize_chapters,
)
from movieclaw_api.services.media_probe import (
    _parse_probe,
    normalize_chapter_title,
    parse_chapters,
)


def test_parse_chapters_from_ffprobe_payload():
    raw = [
        {"id": 1, "start_time": "0.000000", "end_time": "20.000000", "tags": {"title": "Opening"}},
        {
            "id": 2,
            "start_time": "20.000000",
            "end_time": "45.000000",
            "tags": {"title": "00:00:20.000"},
        },
        {"id": 3, "start_time": "45.000000", "end_time": "60.000000"},
    ]
    assert parse_chapters(raw) == [
        {"start_ms": 0, "end_ms": 20000, "title": "Opening"},
        {"start_ms": 20000, "end_ms": 45000, "title": None},
        {"start_ms": 45000, "end_ms": 60000, "title": None},
    ]


def test_parse_chapters_sorts_and_dedupes():
    raw = [
        {"start_time": "30", "end_time": "60"},
        {"start_time": "0", "end_time": "30", "tags": {"title": "A"}},
        {"start_time": "30", "end_time": "60", "tags": {"title": "dup"}},
        {"start_time": "bad"},
        "junk",
    ]
    parsed = parse_chapters(raw)
    assert [c["start_ms"] for c in parsed] == [0, 30000]
    assert parsed[1]["title"] is None  # 首条无标题者胜出，重复起点被丢


def test_parse_probe_carries_chapters_and_defaults_empty():
    payload = {"streams": [], "format": {}, "chapters": [{"start_time": "1.5"}]}
    assert _parse_probe(payload).chapters == [{"start_ms": 1500, "end_ms": None, "title": None}]
    assert _parse_probe({"streams": [], "format": {}}).chapters == []


def test_normalize_chapter_title():
    assert normalize_chapter_title("  ") is None
    assert normalize_chapter_title(None) is None
    assert normalize_chapter_title("00:12:30") is None
    assert normalize_chapter_title("1:02:03.500") is None
    assert normalize_chapter_title("12:30") is None
    assert normalize_chapter_title(" 第 1 章 ") == "第 1 章"
    assert normalize_chapter_title("Chapter 12:30 PM") == "Chapter 12:30 PM"


def test_synth_count_table_boundaries():
    assert synth_count(None) == 0
    assert synth_count(0) == 0
    assert synth_count(89) == 0
    assert synth_count(90) == 3
    assert synth_count(10 * 60 - 1) == 3
    assert synth_count(10 * 60) == 6
    assert synth_count(40 * 60) == 8
    assert synth_count(90 * 60) == 10
    assert synth_count(180 * 60) == 12
    assert synth_count(10 * 3600) == 12


def test_synthesize_chapters_spread_between_head_and_tail():
    chapters = synthesize_chapters(100 * 60)  # 10 张
    assert len(chapters) == 10
    assert chapters[0].start_ms == int(6_000_000 * 0.06)
    assert chapters[-1].start_ms == int(6_000_000 * 0.94)
    assert all(c.synthetic and c.title is None for c in chapters)
    assert [c.index for c in chapters] == list(range(10))
    # 相邻章节的 end = 下一章 start，末章 end = 片长
    for a, b in zip(chapters, chapters[1:], strict=False):
        assert a.end_ms == b.start_ms
    assert chapters[-1].end_ms == 6_000_000


def test_effective_prefers_embedded_when_at_least_two():
    embedded = [
        {"start_ms": 0, "end_ms": 20000, "title": "Opening"},
        {"start_ms": 20000, "end_ms": 45000, "title": None},
        {"start_ms": 45000, "end_ms": None, "title": None},
    ]
    chapters = effective_chapters(embedded, 60)
    assert [c.start_ms for c in chapters] == [0, 20000, 45000]
    assert chapters[0].title == "Opening" and not chapters[0].synthetic
    assert chapters[1].end_ms == 45000
    assert chapters[-1].end_ms == 60000  # 末章 end 缺失时取片长


def test_effective_single_embedded_falls_back_to_synthetic():
    only_one = [{"start_ms": 0, "end_ms": None, "title": "All"}]
    chapters = effective_chapters(only_one, 30 * 60)
    assert len(chapters) == 6 and all(c.synthetic for c in chapters)
    assert effective_chapters(None, 30 * 60) == chapters
    assert effective_chapters([], None) == []


def test_effective_drops_chapters_beyond_runtime():
    embedded = [
        {"start_ms": 0, "end_ms": None, "title": None},
        {"start_ms": 10_000, "end_ms": None, "title": None},
        {"start_ms": 999_000, "end_ms": None, "title": None},
    ]
    chapters = effective_chapters(embedded, 60)
    assert [c.start_ms for c in chapters] == [0, 10_000]
    # 只剩一个有效内嵌章节 → 退回合成
    remaining = effective_chapters(embedded[1:], 600)  # 999s 超出 10 分钟片长
    assert len(remaining) == 6 and all(c.synthetic for c in remaining)


def test_chapter_image_map_ignores_entries_without_image():
    images = [
        {"start_ms": 0, "frame_ms": 15_000, "image": "1/chapters/2/0000000000.jpg"},
        {"start_ms": 20_000, "frame_ms": None, "image": None},
        "junk",
    ]
    assert list(chapter_image_map(images)) == [0]
    assert chapter_image_map(None) == {}
