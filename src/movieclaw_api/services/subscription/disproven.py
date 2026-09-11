"""条目级的"证伪来源"台账：这个发布被实测证明满足不了这个单元。

**为什么不继续只用 ``SubscriptionDownloadAttempt.content_missing``**：那张表的
``subscription_id`` 是 ON DELETE CASCADE，订阅一删，它名下所有投递记录连同负面
记忆一起消失。真实教训——用户抓到一条 113 MB 的假「正片」，手动删掉库里的文件、
删掉订阅重建，系统把同一条种子原样又抓了一遍：证据明明产生过，只是挂在了一个
比它短命的东西上。

记忆的正确归属是**条目**：「这个发布里没有这部电影」是关于内容的事实，与用户
订没订、订了几次无关。

``content_missing`` 保留不动，选种阶段**两边都读**（见 ``matching``）：存量安装
里的旧记忆不必数据迁移就继续生效，新证据同时落到本表。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models import MediaDisprovenSource

logger = logging.getLogger("movieclaw_api.subscription.disproven")

# 证伪成因（``MediaDisprovenSource.reason`` 的取值）
REASON_CONTENT_MISSING = "content_missing"  # 下完后文件清单里没有这个单元
REASON_RUNTIME_MISMATCH = "runtime_mismatch"  # 下完后实测片长远短于影片信息


async def remember_disproven_sources(
    session: AsyncSession,
    *,
    media_item_id: int,
    sources: Iterable[tuple[str, str]],
    units: Iterable[tuple[int, int]],
    reason: str,
    note: str | None = None,
) -> int:
    """把「这些来源满足不了这些单元」记到条目上；返回新增行数。

    幂等：已经记过的 (来源, 单元) 不重复插入，也**不覆盖**原有的 reason/note
    ——第一次证伪的成因才是最贴近现场的那个，后续重复观测没有新信息。

    调用方自行 commit（写入通常与工单退回、入库落账在同一事务里）。
    """
    wanted_sources = {(str(site), str(torrent)) for site, torrent in sources if site and torrent}
    wanted_units = {(int(season), int(episode)) for season, episode in units}
    if not wanted_sources or not wanted_units:
        return 0

    known = {
        (row.site_id, row.torrent_id, row.season_number, row.episode_number)
        for row in (
            await session.execute(
                select(MediaDisprovenSource).where(
                    MediaDisprovenSource.media_item_id == media_item_id
                )
            )
        ).scalars()
    }
    added = 0
    for site_id, torrent_id in sorted(wanted_sources):
        for season, episode in sorted(wanted_units):
            if (site_id, torrent_id, season, episode) in known:
                continue
            session.add(
                MediaDisprovenSource(
                    media_item_id=media_item_id,
                    site_id=site_id,
                    torrent_id=torrent_id,
                    season_number=season,
                    episode_number=episode,
                    reason=reason,
                    note=note,
                )
            )
            added += 1
    if added:
        logger.info(
            "条目 #%s 记下 %d 条证伪来源（%s）：%s",
            media_item_id,
            added,
            reason,
            note or "无说明",
        )
    return added


async def disproven_by_media(
    session: AsyncSession, media_item_ids: Iterable[int]
) -> dict[int, dict[tuple[int, int], set[tuple[str, str]]]]:
    """批量取条目的证伪来源：{条目: {(季, 集): {(站点, 种子ID), ...}}}。

    形状与 ``MediaContext.content_missing`` 一致，调用方直接并进去即可。
    """
    ids = list(dict.fromkeys(media_item_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(MediaDisprovenSource).where(
                MediaDisprovenSource.media_item_id.in_(ids)  # type: ignore[union-attr]
            )
        )
    ).scalars()
    memory: dict[int, dict[tuple[int, int], set[tuple[str, str]]]] = {}
    for row in rows:
        memory.setdefault(row.media_item_id, {}).setdefault(
            (row.season_number, row.episode_number), set()
        ).add((row.site_id, row.torrent_id))
    return memory
