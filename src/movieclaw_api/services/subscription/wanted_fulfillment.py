"""库存对账：library_file 的在位单元关闭对应的订阅工单（订阅止于投递的另一半）。

订阅的目标本来就是"库里有"——工单的完成状态从**库存**推导，而不是从
管线事件推导：任何路径（监听导入搬运、库扫描原地入账、人工认领）让某个
(条目, 季, 集) 单元出现在库里，对应的开放工单即关闭、订阅派生状态重算、
时间线补记"已入库"。这让入库引擎（扫描/监听导入）无需知道订阅的存在，
订阅也无需亲自跟踪下载与搬运。

调用点：library_scan 识别入账后、library_ingest 搬运入库后、待识别
人工认领后。函数幂等：没有可关闭的工单时是纯查询。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.subscription.quality import (
    LOCK_FIRST,
    UPGRADE,
    normalized_policy,
    pending_key,
    pop_pending,
    profile_summary,
)
from movieclaw_db.models import (
    ActivityType,
    MediaItem,
    Subscription,
    SubscriptionActivity,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories import SubscriptionRepository
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository

logger = logging.getLogger("movieclaw_api.wanted_fulfillment")


async def close_fulfilled_wanted(session: AsyncSession, media_item_id: int) -> int:
    """把某条目已在库的单元对应的开放工单标记为已入库。返回关闭数。"""
    owned = await LibraryFileRepository(session).owned_units(media_item_id)
    if not owned:
        return 0
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.media_item_id == media_item_id,
                    WantedItem.status != WantedStatus.IMPORTED,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    fulfilled = [w for w in rows if (w.season_number, w.episode_number) in owned]
    if not fulfilled:
        return 0

    # 时间线与派生状态：逐订阅处理（对账可能一次关闭多个订阅的工单）
    from movieclaw_api.services.subscription import recompute_subscription_status
    from movieclaw_api.services.subscription.matching import units_text

    now = utcnow()
    by_subscription: dict[int, list[WantedItem]] = {}
    for wanted in fulfilled:
        by_subscription.setdefault(wanted.subscription_id, []).append(wanted)

    item = await session.get(MediaItem, media_item_id)
    repo = SubscriptionRepository(session)
    for subscription_id, wanted_rows in by_subscription.items():
        subscription = await session.get(Subscription, subscription_id)
        if subscription is None or item is None:
            continue
        policy = normalized_policy(subscription.quality_policy)
        processed_rows: list[WantedItem] = []
        upgrading_rows: list[WantedItem] = []
        lock_activated = False
        for wanted in wanted_rows:
            policy, entry = pop_pending(policy, wanted)
            # 已有旧版本、仍在等待目标候选的洗版工单会持续命中库存 H；只有
            # 新投递候选真正入库（pending 存在）时才重新判定，避免被旧文件关单。
            if wanted.status == WantedStatus.UPGRADING and entry is None:
                continue
            processed_rows.append(wanted)
            next_status = WantedStatus.IMPORTED
            if policy is not None and entry is not None:
                if policy.get("mode") == LOCK_FIRST and not policy.get("locked"):
                    profile = entry.get("profile")
                    if isinstance(profile, dict):
                        policy["locked"] = profile
                        lock_activated = True
                elif policy.get("mode") == UPGRADE:
                    key = pending_key(wanted)
                    anchor = policy.get("anchor_unit")
                    if entry.get("meets_target") is True:
                        if (
                            subscription.kind == "tv"
                            and not policy.get("locked")
                            and (anchor is None or anchor == key)
                        ):
                            policy["locked"] = dict(policy.get("target") or {})
                            lock_activated = True
                        if anchor == key:
                            policy.pop("anchor_unit", None)
                    elif subscription.kind == "movie":
                        next_status = WantedStatus.UPGRADING
                    else:
                        if anchor is None:
                            policy["anchor_unit"] = key
                            next_status = WantedStatus.UPGRADING
                        elif anchor == key:
                            next_status = WantedStatus.UPGRADING

            wanted.status = next_status
            wanted.imported_at = now
            wanted.updated_at = now
            if next_status == WantedStatus.UPGRADING:
                wanted.info_hash = None
                wanted.grabbed_at = None
                wanted.downloaded_at = None
                wanted.next_search_at = now
                wanted.search_attempts = 0
                wanted.last_search_at = None
                upgrading_rows.append(wanted)
            else:
                wanted.next_search_at = None
            session.add(wanted)

        if not processed_rows:
            continue

        subscription.quality_policy = policy
        subscription.updated_at = now
        session.add(subscription)
        await session.commit()

        message = f"{units_text(processed_rows)}已入库（媒体库对账确认）"
        if upgrading_rows:
            message += f"；其中 {units_text(upgrading_rows)} 未达到洗版目标，继续寻找"
        if lock_activated and policy is not None:
            message += f"；后续剧集已固定为 {profile_summary(policy.get('locked'))}"
        await repo.add_activity(
            SubscriptionActivity(
                subscription_id=subscription_id,
                wanted_item_id=processed_rows[0].id,
                type=ActivityType.IMPORTED,
                message=message,
                payload={
                    "units": [[w.season_number, w.episode_number] for w in processed_rows],
                    "upgrading_units": [
                        [w.season_number, w.episode_number] for w in upgrading_rows
                    ],
                    "quality_locked": lock_activated,
                },
            )
        )
        await recompute_subscription_status(session, subscription, item)
        # IM 通道推送(微信/TG/Discord;fire-and-forget,失败不影响对账链路)
        from movieclaw_api.services.channel_push import notify_channels, tmdb_push_image_url

        year_text = f"({item.year}) " if item.year else ""
        notify_channels(
            f"🎬 已入库:《{item.title}》{year_text}{units_text(processed_rows)}",
            event="imported",
            image_url=tmdb_push_image_url(item.backdrop_path, item.poster_path),
        )
        # 事件 Webhook(与 IM 推送同点位:入库已由库存对账确认,事件即事实)
        from movieclaw_api.services.subscription.events import build_fulfilled_event
        from movieclaw_api.services.webhook import emit_events

        emit_events(
            [
                build_fulfilled_event(
                    item, [(w.season_number, w.episode_number) for w in processed_rows]
                )
            ]
        )
        # 内容已进库，该订阅在途种子的落点告警（若有）自动熄灭
        from movieclaw_api.services.system_notice import resolve_notices

        await resolve_notices(session, prefix=f"subscription.landing:{subscription_id}:")
        if upgrading_rows:
            from movieclaw_api.services.subscription.wanted_search import kick_search_soon

            kick_search_soon()
    logger.info("库存对账：条目 #%s 关闭了 %d 个工单", media_item_id, len(fulfilled))

    # L4：通知媒体服务器刷新（未配置为 no-op；失败只告警）
    from movieclaw_api.services.media_server_notify import notify_media_server_refresh

    await notify_media_server_refresh()
    return len(fulfilled)
