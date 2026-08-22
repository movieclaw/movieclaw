"""播放质量指标的落库与聚合（docs/design/web-player.md §8）。

**北极星指标是直通率**（档 0 + 档 1 占全部播放的比例）：这一个数同时代表
画质（没重编码 = 无损）、速度（秒开）和服务器负担（不烧 GPU），其它指标各自
只覆盖一个侧面。它也是「这个软件对我的库适配得好不好」的直观答案，所以要给
用户看，不只是给开发者看。

指标口径按 CTA-2066，不自创。遥测**只落本地**（硬边界 3）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_db.models import PlaybackMetric

#: 档 0（原文件直出）与档 1（remux 直通）都不重编码，合起来就是「直通」。
DIRECT_TIERS = (0, 1)


@dataclass(frozen=True)
class PlaybackStats:
    """一段时间内的播放质量汇总。样本不足时各项为 None，不编数字。"""

    sessions: int
    direct_ratio: float | None
    degraded_ratio: float | None
    ttff_p50_ms: int | None
    ttff_p95_ms: int | None
    rebuffer_ratio: float | None
    dropped_ratio: float | None
    tier_counts: dict[int, int]


async def record(session: AsyncSession, metric: PlaybackMetric) -> PlaybackMetric:
    session.add(metric)
    await session.commit()
    await session.refresh(metric)
    return metric


async def summarize(session: AsyncSession, *, limit: int = 500) -> PlaybackStats:
    """最近 ``limit`` 次播放的汇总。

    取最近 N 次而不是最近 N 天：自建媒体库的播放频率差异极大，按天算会让
    低频用户永远看到「样本不足」。
    """
    rows = list(
        (
            await session.execute(
                select(PlaybackMetric).order_by(PlaybackMetric.id.desc()).limit(limit)
            )
        ).scalars()
    )
    if not rows:
        return PlaybackStats(
            sessions=0, direct_ratio=None, degraded_ratio=None,
            ttff_p50_ms=None, ttff_p95_ms=None, rebuffer_ratio=None,
            dropped_ratio=None, tier_counts={},
        )

    total = len(rows)
    tier_counts: dict[int, int] = {}
    for row in rows:
        tier_counts[row.tier] = tier_counts.get(row.tier, 0) + 1

    ttffs = sorted(r.ttff_ms for r in rows if r.ttff_ms is not None)
    watched = sum(r.watched_ms for r in rows)
    rebuffered = sum(r.rebuffer_ms for r in rows)
    frames = sum(r.total_frames or 0 for r in rows)
    dropped = sum(r.dropped_frames or 0 for r in rows)

    return PlaybackStats(
        sessions=total,
        direct_ratio=sum(1 for r in rows if r.tier in DIRECT_TIERS) / total,
        degraded_ratio=sum(1 for r in rows if r.degraded_from is not None) / total,
        ttff_p50_ms=_percentile(ttffs, 0.50),
        ttff_p95_ms=_percentile(ttffs, 0.95),
        # 卡顿率 = 卡顿时长 / (观看时长 + 卡顿时长)，与 CTA-2066 同口径
        rebuffer_ratio=(rebuffered / (watched + rebuffered)) if watched + rebuffered else None,
        dropped_ratio=(dropped / frames) if frames else None,
        tier_counts=tier_counts,
    )


async def purge_older_than(session: AsyncSession, *, keep: int = 2000) -> int:
    """只保留最近 ``keep`` 行。

    指标是趋势数据，不是台账——攒到几十万行只会拖慢 data 卷上的 SQLite，
    而更早的数据对「现在适配得好不好」没有意义。
    """
    threshold = (
        await session.execute(
            select(PlaybackMetric.id).order_by(PlaybackMetric.id.desc()).offset(keep).limit(1)
        )
    ).scalar_one_or_none()
    if threshold is None:
        return 0
    result = await session.execute(
        PlaybackMetric.__table__.delete().where(PlaybackMetric.id <= threshold)
    )
    await session.commit()
    return result.rowcount or 0


async def count(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(PlaybackMetric))).scalar_one()
    )


def _percentile(ordered: list[int], q: float) -> int | None:
    """已排序序列的分位数（最近秩法）。空序列返回 None，不编数字。"""
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]
