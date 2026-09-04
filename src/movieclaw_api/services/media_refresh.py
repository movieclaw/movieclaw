"""元数据刷新（F3）：定时分档保鲜的任务壳。

每 tick 捞出到期条目，逐个交给刮削管线 ``media_scrape.scrape_media_item``
执行（拉 TMDB 档案 → 身份/展示层落库 → 订阅补新工单与档期同步 → 图片
资产自愈）。分档间隔见 ``media_scrape.next_refresh_delay``
（docs/design/subscription-p4.md 第 6 节 / metadata.md 4.4）。

管线本身不向外抛异常；这里只负责失败条目的重试推迟，防止坏条目每 tick
都占住队首。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlmodel import select

from movieclaw_api.services.media_scrape import scrape_media_item
from movieclaw_api.services.subscription import REFRESH_PER_TICK
from movieclaw_db.engine import get_database
from movieclaw_db.models import MediaItem, MediaSource, utcnow
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.media_refresh")

_RETRY_DELAY = timedelta(hours=1)  # 单条目刷新失败的重试间隔


@register_task(
    "refresh_media_metadata",
    title="元数据定时刷新",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=900,
    description=(
        "定期从 TMDB 刷新已建档条目的完整档案（季集/状态/简介/评分/演职员/图片）："
        "发现新集给追新订阅补工单、同步改档期。在播剧 8 小时一刷，"
        "完结/无订阅条目低频保鲜。"
    ),
)
async def refresh_media_metadata() -> None:
    db = get_database()
    async with db.session() as session:
        result = await session.execute(
            select(MediaItem)
            .where(
                # 本地来源条目没有上游可刷（docs/design/library-other-kind.md 4.8
                # 的 source 守卫）：它们的档案来自 sidecar NFO/文件本身，扫描时同步
                MediaItem.source == MediaSource.TMDB,
                (MediaItem.next_refresh_at.is_(None))  # type: ignore[union-attr]
                | (MediaItem.next_refresh_at <= utcnow()),  # type: ignore[operator]
            )
            .order_by(MediaItem.next_refresh_at)  # type: ignore[arg-type]
            .limit(REFRESH_PER_TICK)
        )
        due = [item.id for item in result.scalars().all() if item.id is not None]
    for media_item_id in due:
        if not await scrape_media_item(media_item_id):
            await _postpone(media_item_id)


async def _postpone(media_item_id: int) -> None:
    db = get_database()
    async with db.session() as session:
        item = await session.get(MediaItem, media_item_id)
        if item is None:
            return
        item.next_refresh_at = utcnow() + _RETRY_DELAY
        session.add(item)
        await session.commit()
    logger.warning("条目 #%s 刷新失败（详见上方日志），%s 后重试", media_item_id, _RETRY_DELAY)
