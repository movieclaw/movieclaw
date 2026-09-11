"""存量库的系列回填（docs/design/library-series-collections.md 第 7 节）。

新列默认 NULL，**已经刮削过的库一部都不会自动成系列**。不能要求用户整库重刮
——大库要跑很久，还会把图重下一遍。

所以有这个轻量作业：只对 ``tmdb_id 非空 且 series_key 还没查过`` 的电影发一次
``GET /movie/{id}``（**不带 append_to_response**，比完整刮削便宜一个量级），
只取 ``belongs_to_collection`` 落列 + ensure 合集。

**按刮削设置的元数据语言问**：不带 ``language`` 时 TMDB 一律回英文，合集页就会
整片是「If You Are the One (Collection)」而不是「非诚勿扰（系列）」。语言按条目的
归属库解析（与刮削管线同一个入口 ``scrape_setting_for_item``），库级覆盖了语言的
库各按各的来。

**可中断可重入**：每 tick 处理一小批，判据是"这一列还是 NULL"，所以中途停了
下次接着跑，跑完自然停（查过没有系列的写空串，不会被反复问）。这里没有做成
带进度条的作业中心任务——同样的用户价值（升级完自己就补齐了），少一大截
样板代码，而且中断恢复是**构造上**成立的，不靠状态机记录跑到哪了。

对 TMDB 温和：每 tick 至多 ``_BATCH`` 部，5 分钟一轮。300 部的库大约十几分钟
补完，用户不需要做任何事。
"""

from __future__ import annotations

import logging

from sqlmodel import select

from movieclaw_api.services.library.series import (
    SERIES_KEY_NONE,
    build_series_key,
    ensure_series_collections_for_item,
    rename_series_collections,
)
from movieclaw_api.services.scrape_config import effective_language, scrape_setting_for_item
from movieclaw_db.engine import get_database
from movieclaw_db.models import MediaItem, MediaMetadata, MediaSource, utcnow
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_media.models import MediaKind
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.series_backfill")

#: 每轮问几部。TMDB 没有硬性限速，但存量回填是后台的锦上添花，
#: 不该跟用户正在等的刮削抢配额
_BATCH = 50


@register_task(
    "backfill_media_series",
    title="作品系列回填",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=300,
    description=(
        "给升级前就刮过的电影补上「所属系列」（《哈利·波特》这种），"
        "补完后合集页自动出现系列合集。只发一次轻量详情请求，不重刮档案、不重下图；"
        "全部补完后本任务自动空转。"
    ),
)
async def backfill_media_series() -> None:
    db = get_database()
    async with db.session() as session:
        items = (
            (
                await session.execute(
                    select(MediaItem)
                    .join(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)
                    .where(
                        MediaItem.source == MediaSource.TMDB,
                        MediaItem.kind == MediaKind.MOVIE.value,
                        MediaItem.tmdb_id.is_not(None),  # type: ignore[union-attr]
                        # NULL = 还没查过。查过没系列的写空串，不会再被捞出来
                        MediaMetadata.series_key.is_(None),  # type: ignore[union-attr]
                    )
                    .limit(_BATCH)
                )
            )
            .scalars()
            .all()
        )
        batch: list[tuple[int, int, str]] = []
        for item in items:
            setting = await scrape_setting_for_item(session, item)
            batch.append((item.id, item.tmdb_id, effective_language(setting)))
        # 解析归属库时可能把推断结果固化到条目上（resolve_scrape_library），一并落盘
        await session.commit()
    if not batch:
        return

    from movieclaw_api.services.media_discover import get_tmdb_client

    client = get_tmdb_client()
    filled = 0
    for media_item_id, tmdb_id, language in batch:
        try:
            data = await client.get(f"movie/{tmdb_id}", {"language": language})
        except Exception:  # noqa: BLE001 -- 单条失败不该让整轮回填停下
            logger.warning("作品系列回填：条目 %s 的 TMDB 详情读取失败，下轮重试", media_item_id)
            continue
        summary = data.get("belongs_to_collection") or {}
        async with db.session() as session:
            meta = (
                await session.execute(
                    select(MediaMetadata).where(MediaMetadata.media_item_id == media_item_id)
                )
            ).scalar_one_or_none()
            if meta is None:
                continue
            previous_name = meta.series_name
            meta.series_key = build_series_key(summary.get("id"), summary.get("name"))
            meta.series_name = summary.get("name") or None
            meta.updated_at = utcnow()
            session.add(meta)
            await session.flush()
            if meta.series_key != SERIES_KEY_NONE:
                # 系列名换了语言：用户没改过名的合集跟着换（判据见 rename_series_collections）
                await rename_series_collections(
                    session, meta.series_key, previous_name, meta.series_name
                )
                await ensure_series_collections_for_item(session, media_item_id)
                filled += 1
            await session.commit()
    if filled:
        logger.info("作品系列回填：本轮为 %d 部影片补上了所属系列", filled)
