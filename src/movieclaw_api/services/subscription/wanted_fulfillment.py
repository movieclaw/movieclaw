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
    # 洗版入库验证先行（quality-upgrade.md §6.3）：已 imported 的单元出现
    # 新文件不会产生可关闭工单，但需要在同一钩子点做实测裁决（确认/证伪）
    from movieclaw_api.services.subscription.upgrade import verify_upgrades

    await verify_upgrades(session, media_item_id)

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

    now = utcnow()
    by_subscription: dict[int, list[WantedItem]] = {}
    for wanted in fulfilled:
        wanted.status = WantedStatus.IMPORTED
        wanted.imported_at = now
        # 缺口时代的搜索排期就此作废——不清掉的话，imported 单元会带着旧的
        # next_search_at 进入洗版搜索队列，触发无谓的站点搜索（洗版排期由
        # arm_upgrade_candidates 按需重挂）
        wanted.next_search_at = None
        wanted.updated_at = now
        by_subscription.setdefault(wanted.subscription_id, []).append(wanted)
    # 洗版基线：入库即落质量快照（与规则组是否开洗版无关——数据此刻最热，
    # 规则组日后开洗版时立即可用）；规则组已配洗版目标且未到顶的单元顺带
    # 进入洗版搜索排期（见 services/subscription/upgrade.py）
    from movieclaw_api.services.subscription.upgrade import (
        arm_upgrade_candidates,
        fill_snapshots,
    )

    await fill_snapshots(session, media_item_id, fulfilled)
    await arm_upgrade_candidates(session, fulfilled)
    await session.commit()

    # 时间线与派生状态：逐订阅补记（对账可能一次关闭多个订阅的工单）
    from movieclaw_api.services.subscription import recompute_subscription_status
    from movieclaw_api.services.subscription.matching import units_text

    item = await session.get(MediaItem, media_item_id)
    repo = SubscriptionRepository(session)
    for subscription_id, wanted_rows in by_subscription.items():
        subscription = await session.get(Subscription, subscription_id)
        if subscription is None or item is None:
            continue
        await repo.add_activity(
            SubscriptionActivity(
                subscription_id=subscription_id,
                wanted_item_id=wanted_rows[0].id,
                type=ActivityType.IMPORTED,
                message=f"{units_text(wanted_rows)}已入库（媒体库对账确认）",
                payload={"units": [[w.season_number, w.episode_number] for w in wanted_rows]},
            )
        )
        await recompute_subscription_status(session, subscription, item)
        # IM 通道推送(微信/TG/Discord;fire-and-forget,失败不影响对账链路)
        from movieclaw_api.services.channel_push import notify_channels, tmdb_push_image_url

        year_text = f"({item.year}) " if item.year else ""
        notify_channels(
            f"🎬 已入库:《{item.title}》{year_text}{units_text(wanted_rows)}",
            event="imported",
            image_url=tmdb_push_image_url(item.backdrop_path, item.poster_path),
        )
        # 事件 Webhook(与 IM 推送同点位:入库已由库存对账确认,事件即事实)
        from movieclaw_api.services.subscription.events import build_fulfilled_event
        from movieclaw_api.services.webhook import emit_events

        emit_events(
            [
                build_fulfilled_event(
                    item, [(w.season_number, w.episode_number) for w in wanted_rows]
                )
            ]
        )
        # 内容已进库，该订阅在途种子的落点告警（若有）自动熄灭
        from movieclaw_api.services.system_notice import resolve_notices

        await resolve_notices(session, prefix=f"subscription.landing:{subscription_id}:")
    # 入库规格核验：此刻实测值（快照）与种子声称值（attempt.quality）都在手上，
    # "声称 1080p / 实测 540p"这类货不对板不该静默（services/subscription/spec_audit.py）
    from movieclaw_api.services.subscription.spec_audit import audit_ingest_specs

    await audit_ingest_specs(session, media_item_id, fulfilled)
    logger.info("库存对账：条目 #%s 关闭了 %d 个工单", media_item_id, len(fulfilled))

    # L4：通知媒体服务器刷新（未配置为 no-op；失败只告警）
    from movieclaw_api.services.media_server_notify import notify_media_server_refresh

    await notify_media_server_refresh()
    return len(fulfilled)
