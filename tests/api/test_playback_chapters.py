"""进度条章节刻度（docs/design/player-feel.md §2.C1）。

只验一件事：**下发给播放器的刻度里没有合成章节**。合成章节是详情页凑场景图
用的等距切分，画到进度条上就是一排没有信息量的竖条。
"""

from __future__ import annotations

from movieclaw_api.api.routes.playback import _chapter_marks
from movieclaw_db.models import LibraryFile


def _file(chapters, duration_seconds: int | None) -> LibraryFile:
    return LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path="/x.mkv",
        chapters=chapters,
        duration_seconds=duration_seconds,
    )


def test_内嵌章节原样下发():
    marks = _chapter_marks(
        _file(
            [
                {"start_ms": 0, "title": "片头"},
                {"start_ms": 90_000, "title": "第一场"},
                {"start_ms": 300_000, "title": None},
            ],
            duration_seconds=600,
        )
    )
    assert [m.start_ms for m in marks] == [0, 90_000, 300_000]
    assert marks[0].title == "片头"
    assert marks[2].title is None


def test_没有内嵌章节时不下发合成的等距刻度():
    # effective_chapters 在这种情况下会按时长合成一串等距章节——那是给详情页
    # 用的，进度条上画出来只是噪音
    assert _chapter_marks(_file([], duration_seconds=3600)) == []
    assert _chapter_marks(_file([{"start_ms": 0}], duration_seconds=3600)) == []


def test_章节未探测的旧台账行返回空表():
    assert _chapter_marks(_file(None, duration_seconds=3600)) == []
