"""存量条目的 NFO 吸收回填（docs/design/metadata.md 第 5 节）。

NFO 改为「刮削时吸收进库内档案、读路径只读库」之后，**升级前就入库的条目**
档案里没有那次吸收——如果不管，用 TMM 精心刮过的用户升级完会发现详情页的
简介/评分/演职员从自己的 NFO 换成了 TMDB 那份。这不可接受，所以有这个回填。

判据是 ``media_metadata.nfo_fingerprint IS NULL``（从未吸收过）。吸收完写上
指纹（没有 NFO 的写空串），于是**跑完自然停**，也天然可中断可重入——中途
停了下次接着跑，不靠状态机记录跑到哪。

纯本地读盘、零 TMDB 请求：只读条目目录已有的 NFO，不重刮、不重下图。
每轮一小批、5 分钟一轮，不跟用户正在等的刮削抢线程池。
"""

from __future__ import annotations

import logging

from sqlmodel import select

from movieclaw_db.engine import get_database
from movieclaw_db.models import MediaItem, MediaMetadata, MediaSource
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.nfo_backfill")

#: 每轮吸收几个条目。读盘是线程池里的阻塞操作，一批别太大——
#: 回填是升级后的一次性补课，慢几分钟没人会察觉
_BATCH = 40


@register_task(
    "backfill_nfo_absorption",
    title="本地 NFO 吸收回填",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=300,
    description=(
        "把升级前入库条目的本地 NFO（简介/评分/片长/类型/演职员）读进库内档案。"
        "此前这些字段是每次打开详情页时现读 NFO 得到的，现在改为入库时读一次、"
        "之后只读库。纯本地读盘，不联网、不重下图；全部补完后本任务自动空转。"
    ),
)
async def backfill_nfo_absorption() -> None:
    from movieclaw_api.services.media_scrape import absorb_local_nfo

    db = get_database()
    async with db.session() as session:
        item_ids = list(
            (
                await session.execute(
                    select(MediaItem.id)
                    .join(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)
                    .where(
                        MediaItem.source == MediaSource.TMDB,
                        MediaMetadata.nfo_fingerprint.is_(None),  # type: ignore[union-attr]
                    )
                    .order_by(MediaItem.id)
                    .limit(_BATCH)
                )
            )
            .scalars()
            .all()
        )
        if not item_ids:
            return
        absorbed = 0
        for item_id in item_ids:
            try:
                if await absorb_local_nfo(session, item_id):
                    absorbed += 1
            except Exception:  # noqa: BLE001 -- 单条目失败不断整批
                logger.exception("NFO 吸收回填失败：media_item_id=%s", item_id)
        await session.commit()
    logger.info("NFO 吸收回填：本轮处理 %d 个条目，其中 %d 个读到了 NFO", len(item_ids), absorbed)
