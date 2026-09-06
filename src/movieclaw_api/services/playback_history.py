"""清除观看记录（docs/design/library-access.md 2.6）。

观看记录由两张表构成，删的时候要一起删：

- ``playback_state``：续播点、已看标记、播放次数、记忆的音轨/字幕——键是
  成员 × 条目 × 季 × 集；
- ``playback_metric``：每次播放的质量指标（起播耗时、卡顿、观看时长），按
  成员 × 台账文件记录，虽然不带片名，但「某成员在某时间看了某文件」本身
  就是观看痕迹。

只作用于**一个成员自己**的记录（超管 = 哨兵 0），跨成员清除不在这里提供。
三种范围：按条目（该条目全部季集）、按库（该库台账里出现过的全部条目）、
全部。任一范围都可再叠一个时间窗口 ``since``（首页「清空今天 / 最近一周」
用它）：只删最近一次播放落在窗口内的状态行与窗口内产生的指标行——一部片
上周看到一半、今天又接着看了，它的续播点也会被清掉，这正是「清空今天看过
的」应有的含义。同一事务内落盘，删了一半的状态不会出现。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.schemas.playback import PlaybackHistoryClearView
from movieclaw_db.models.library_file import LibraryFile
from movieclaw_db.models.playback_metric import PlaybackMetric
from movieclaw_db.models.playback_state import PlaybackState


async def clear(
    session: AsyncSession,
    member_id: int,
    *,
    media_item_id: int | None = None,
    library_id: int | None = None,
    since: datetime | None = None,
) -> PlaybackHistoryClearView:
    """删除 ``member_id`` 在给定范围内的全部观看记录；两个范围参数都不给 = 全部。

    ``since``（UTC 朴素时间）非空时只删这个时刻之后有播放活动的记录。
    """
    state_stmt = sa_delete(PlaybackState).where(PlaybackState.member_id == member_id)  # type: ignore[arg-type]
    metric_stmt = sa_delete(PlaybackMetric).where(PlaybackMetric.member_id == member_id)  # type: ignore[arg-type]

    if since is not None:
        # 状态行按「最近一次播放时间」判定：从未播放过（只被标记已看/收藏）的
        # 行没有时间可比，不在时间窗口的语义里，留下
        state_stmt = state_stmt.where(PlaybackState.last_played_at >= since)  # type: ignore[arg-type]
        metric_stmt = metric_stmt.where(PlaybackMetric.created_at >= since)  # type: ignore[arg-type]

    if media_item_id is not None:
        state_stmt = state_stmt.where(PlaybackState.media_item_id == media_item_id)  # type: ignore[arg-type]
        file_ids = select(LibraryFile.id).where(LibraryFile.media_item_id == media_item_id)
        metric_stmt = metric_stmt.where(PlaybackMetric.library_file_id.in_(file_ids))  # type: ignore[union-attr]
    elif library_id is not None:
        # 按库：该库台账里出现过的条目（含已标记缺失的行——记录是关于条目的，
        # 文件暂时不在不代表记录不该清）
        item_ids = (
            select(LibraryFile.media_item_id)
            .where(
                LibraryFile.library_id == library_id,
                LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
            )
            .distinct()
        )
        state_stmt = state_stmt.where(PlaybackState.media_item_id.in_(item_ids))  # type: ignore[union-attr]
        file_ids = select(LibraryFile.id).where(LibraryFile.library_id == library_id)
        metric_stmt = metric_stmt.where(PlaybackMetric.library_file_id.in_(file_ids))  # type: ignore[union-attr]

    deleted_states = (await session.execute(state_stmt)).rowcount or 0
    deleted_metrics = (await session.execute(metric_stmt)).rowcount or 0
    await session.commit()
    return PlaybackHistoryClearView(
        deleted_states=int(deleted_states), deleted_metrics=int(deleted_metrics)
    )
