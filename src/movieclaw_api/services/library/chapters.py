"""视频章节与场景图（docs/design/video-chapters.md）。

这是 Jellyfin / Emby「场景」（Scenes）功能的对标：详情页一排带时间戳的
截图，点一张就从那个时间点开始播；同一份章节数据还输出给 Jellyfin 协议的
``Chapters`` 字段，让 Infuse 这类播放器做章节跳转。

三条设计决定：

- **章节是文件的属性**（挂 ``library_file``），不是作品的：同条目两个版本
  章节位置可以不同，剧集每集各有各的；
- **内嵌章节优先，按时长合成兜底**：容器里 ≥2 个章节就用它（Jellyfin 的
  规则：0 或 1 个都算"没有章节"），否则按时长分档合成等距章节
  （``_SYNTH_TABLE``）。有效列表是纯函数，不落库——合成策略调档不需要
  迁移与重探；
- **图是章节的附属物**：抓不到图的章节仍在列表里（详情页显示时间戳并可
  跳播，Jellyfin 客户端仍可章节跳转）。

抓图参数照抄 Jellyfin 的 ChapterManager（第 0 章从 15s 抓、平均间隔 <1s
跳过、越界停止、只解关键帧），差异都登记在设计文档 §2.4。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("movieclaw_api.library.chapters")

# 合成章节：按时长分档定张数（设计文档 §4.2）。上限是 (时长秒, 张数)，
# 首档 <90s 不合成——短片一张主图就够，再切等于把同一画面摆三遍
_SYNTH_TABLE: tuple[tuple[int, int], ...] = (
    (90, 0),
    (10 * 60, 3),
    (40 * 60, 6),
    (90 * 60, 8),
    (180 * 60, 10),
)
_SYNTH_MAX = 12
# 合成章节落在 6% ～ 94% 之间等距：掐掉片头 logo/黑场与片尾字幕
_SYNTH_HEAD = 0.06
_SYNTH_SPAN = 0.88


@dataclass(frozen=True)
class Chapter:
    """一个有效章节。``synthetic`` 标记它是合成的（没有内嵌章节时按时长切）。"""

    index: int
    start_ms: int
    end_ms: int | None
    title: str | None
    synthetic: bool


def synth_count(duration_seconds: int | None) -> int:
    """按时长定合成章节张数；时长未知或不足 90s 为 0。"""
    if not duration_seconds or duration_seconds <= 0:
        return 0
    for limit, count in _SYNTH_TABLE:
        if duration_seconds < limit:
            return count
    return _SYNTH_MAX


def synthesize_chapters(duration_seconds: int | None) -> list[Chapter]:
    """没有内嵌章节时按时长合成等距章节（标题恒 None）。"""
    count = synth_count(duration_seconds)
    if count == 0:
        return []
    assert duration_seconds is not None
    total_ms = duration_seconds * 1000
    starts = [int(total_ms * (_SYNTH_HEAD + _SYNTH_SPAN * i / (count - 1))) for i in range(count)]
    return [
        Chapter(
            index=i,
            start_ms=start,
            end_ms=starts[i + 1] if i + 1 < count else total_ms,
            title=None,
            synthetic=True,
        )
        for i, start in enumerate(starts)
    ]


def effective_chapters(embedded: list[dict] | None, duration_seconds: int | None) -> list[Chapter]:
    """一个文件的有效章节列表：内嵌 ≥2 个用内嵌，否则按时长合成。

    ``embedded`` 是台账 ``library_file.chapters``（NULL 视为空）。内嵌章节
    起点 ≥ 片长的丢弃（Jellyfin SaveChapters 同款），丢完不足两个同样退回
    合成。
    """
    rows = [c for c in (embedded or []) if isinstance(c, dict) and "start_ms" in c]
    if duration_seconds:
        rows = [c for c in rows if int(c["start_ms"]) < duration_seconds * 1000]
    if len(rows) < 2:
        return synthesize_chapters(duration_seconds)
    rows.sort(key=lambda c: int(c["start_ms"]))
    total_ms = duration_seconds * 1000 if duration_seconds else None
    result: list[Chapter] = []
    for i, row in enumerate(rows):
        start = int(row["start_ms"])
        end = row.get("end_ms")
        if i + 1 < len(rows):
            end = int(rows[i + 1]["start_ms"])
        elif end is None:
            end = total_ms
        title = row.get("title")
        result.append(
            Chapter(
                index=i,
                start_ms=start,
                end_ms=int(end) if end is not None else None,
                title=title if isinstance(title, str) and title else None,
                synthetic=False,
            )
        )
    return result


def chapter_image_map(chapter_images: list | None) -> dict[int, dict]:
    """``library_file.chapter_images`` → {start_ms: 元素}，供有效章节 join。"""
    result: dict[int, dict] = {}
    for entry in chapter_images or []:
        if isinstance(entry, dict) and "start_ms" in entry and entry.get("image"):
            result[int(entry["start_ms"])] = entry
    return result
