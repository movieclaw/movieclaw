"""投递（F5）：选定的候选 → 认领工单 → （取种 → 提交下载器）→ 台账与状态推进。

幂等三层防线的第一层在这里：**条件更新认领**——被动匹配与主动搜索并发命中
同一工单时，数据库保证只有一个赢家（docs/design/subscription-p4.md 第 5/7 节）。

真实投递（2026-07-24 起默认）：取种 → 保存目录过下载器路径映射翻译 →
提交默认下载器（编排收口 torrent_submit）。``SUBSCRIPTION_DISPATCH_DRY_RUN``
设为 true 可切回模拟投递（短路取种与提交、纯日志、照常推进状态机，
活动标注"模拟投递"），供调试匹配规则用。
"""

from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.quality import record_pending_candidate
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
from movieclaw_matcher import RuleVerdict, TorrentCandidate

logger = logging.getLogger("movieclaw_api.download_dispatch")


async def dispatch(
    session: AsyncSession,
    *,
    subscription: Subscription,
    item: MediaItem,
    wanted_rows: list[WantedItem],
    candidate: TorrentCandidate,
    verdict: RuleVerdict,
    source: str,
) -> bool:
    """把候选投递给下载器，满足给定的一批工单。返回是否有实际投递发生。"""
    from movieclaw_api.services.subscription.core import recompute_subscription_status
    from movieclaw_api.services.subscription.matching import (
        DISPATCH_RETRY_DELAY,
        units_text,
    )

    claimed = await _claim(session, wanted_rows)
    if not claimed:
        return False  # 全部被另一条路径抢先，本候选无事可做

    repo = SubscriptionRepository(session)
    assert subscription.id is not None
    dry_run = get_settings().subscription_dispatch_dry_run
    units_label = units_text(claimed)
    spec_text = _describe(candidate)

    # 入库目标：订阅指定的库（缺省该类型默认库）→ 投递目录三级兜底走全仓
    # 唯一实现 resolve_save_path（口径与预检/体检/手动下载同源）。
    # 完成后的搬运/入账仍由监听导入或库扫描接管，工单由库存对账关闭——
    # 订阅不亲自跟踪下载，但投递必须把种子送到那两个机制看得见的地方。
    from movieclaw_api.services.library.config import LibraryConfigService
    from movieclaw_api.services.library.routing import resolve_save_path

    # library_id 常态下已在创建时定格（收藏范围路由，library-routing 2.1）；
    # NULL 仅剩老订阅/定格库被删的回落——带上 item 让回落也走路由而非裸默认库
    library = await LibraryConfigService(session).resolve_for_subscription(
        subscription.library_id, subscription.kind, item=item
    )
    decision = await resolve_save_path(
        session, library, kind=subscription.kind, title=item.title, year=item.year
    )
    dispatch_dir = decision.path
    # entry_level = 投递目录就是库内条目目录：可以安全锚定副标题线索，
    # 帮扫描器收敛拼音命名的种子内容（监听目录/默认目录锚线索会波及无关内容）
    entry_level = decision.entry_level
    # 自定义目录规则：落点由规则声明决定（不入库），文案陈述事实——
    # 整理到该目录后由外部流转，文件回到库根时才入账关单
    staging = getattr(decision.rule, "target_path", None)
    if library is not None and decision.mode == "watch" and staging:
        target_text = (
            f"；已投递到监听导入目录，下载完成后将整理到 {staging}，文件进入媒体库根目录后自动入账"
        )
    elif library is not None and decision.mode == "watch":
        target_text = (
            f"；已投递到监听导入目录，下载完成后将整理入库到「{library.name}」："
            f"{decision.entry_dir}"
        )
    elif library is not None and decision.mode == "inplace":
        target_text = f"；将直接下载到「{library.name}」库内目录：{decision.path}，完成后自动入账"
    elif library is not None:
        target_text = f"；媒体库「{library.name}」未配置根路径，将落至下载器默认目录，不会自动入库"
    else:
        target_text = "；未配置媒体库，下载完成后不会自动整理入库"

    if not dry_run:
        try:
            submit_result = await _submit_real(
                session,
                candidate,
                save_path=dispatch_dir,
                subtitle=candidate.subtitle if entry_level else None,
            )
        except Exception as exc:  # noqa: BLE001 -- 投递失败退回调度通道重试
            reason = f"{type(exc).__name__}: {exc}"
            await _rollback_claim(session, claimed, retry_delay=DISPATCH_RETRY_DELAY)
            await repo.add_activity(
                SubscriptionActivity(
                    subscription_id=subscription.id,
                    wanted_item_id=claimed[0].id,
                    type=ActivityType.DISPATCH_FAILED,
                    message=(
                        f"{units_label}投递失败：{reason}；已退回队列，"
                        f"约 {int(DISPATCH_RETRY_DELAY.total_seconds() // 60)} 分钟后重试"
                    ),
                    payload={
                        "site_id": candidate.site_id,
                        "torrent_id": candidate.torrent_id,
                        "source": source,
                    },
                )
            )
            logger.warning(
                "投递失败（%s）：《%s》%s ← %s/%s：%s",
                source,
                item.title,
                units_label,
                candidate.site_id,
                candidate.torrent_id,
                reason,
            )
            return False
        # 记录 infohash：完成轮询任务据此追踪下载进度并触发入库整理
        if submit_result.info_hash:
            now = utcnow()
            for wanted in claimed:
                await session.execute(
                    update(WantedItem)
                    .where(WantedItem.id == wanted.id)
                    .values(info_hash=submit_result.info_hash, updated_at=now)
                )
            await session.commit()

    mode = "【模拟投递】" if dry_run else ""
    pending_policy = record_pending_candidate(
        subscription.quality_policy, claimed, candidate
    )
    if pending_policy is not None:
        subscription.quality_policy = pending_policy
        await repo.save(subscription)
    logger.info(
        "%s已投递（%s）：《%s》%s ← %s 的「%s」（%s）",
        mode,
        source,
        item.title,
        units_label,
        candidate.site_id,
        candidate.title[:80],
        spec_text,
    )
    await repo.add_activity(
        SubscriptionActivity(
            subscription_id=subscription.id,
            wanted_item_id=claimed[0].id,
            type=ActivityType.GRABBED,
            message=(
                f"已投递{units_label}：来自 {candidate.site_id} 的"
                f"「{candidate.title[:60]}」（{spec_text}）"
                + target_text
                + ("——模拟投递，未真实提交下载器" if dry_run else "")
            ),
            payload={
                "site_id": candidate.site_id,
                "torrent_id": candidate.torrent_id,
                "score": verdict.score,
                "source": source,
                "dry_run": dry_run,
                "units": [[w.season_number, w.episode_number] for w in claimed],
                "library_id": library.id if library else None,
                "save_path": decision.entry_dir,
                "staging_path": staging,
                "dispatch_dir": dispatch_dir,
            },
        )
    )
    await recompute_subscription_status(session, subscription, item)

    # IM 通道推送(微信/TG/Discord;fire-and-forget,失败不影响投递链路)
    if not dry_run:
        from movieclaw_api.services.channel_push import notify_channels, tmdb_push_image_url

        year_text = f"({item.year}) " if item.year else ""
        notify_channels(
            f"📥 开始下载:《{item.title}》{year_text}{units_label}\n"
            f"来自 {candidate.site_id} 的「{candidate.title[:60]}」\n"
            f"{spec_text}",
            event="dispatch",
            image_url=tmdb_push_image_url(item.backdrop_path, item.poster_path),
        )
        # 事件 Webhook(与 IM 推送同点位:种子已真实提交,事件即事实)
        from movieclaw_api.services.subscription.events import build_download_started_event
        from movieclaw_api.services.webhook import emit_events

        emit_events(
            [
                build_download_started_event(
                    item,
                    [(w.season_number, w.episode_number) for w in claimed],
                    site_id=candidate.site_id,
                    torrent_title=candidate.title,
                    spec=spec_text,
                )
            ]
        )
    return True


async def preview_dispatch_route(
    session: AsyncSession, *, kind: str, library_id: int | None, tmdb_id: int | None = None
) -> dict:
    """预演一次投递的路由结论（订阅弹窗/下载弹窗的预检数据源）。

    与 dispatch() 的三级兜底同源：监听规则源目录 → 库主根（条目目录的
    基底）→ 下载器默认目录；再叠加 submit_torrent 的映射覆盖守门判定。
    只读不投，返回结构化结论让前端在**订阅那一刻**就把问题亮给用户，
    而不是等投递失败/落点告警才发现。

    ``library_id`` 缺省且给了 ``tmdb_id`` 时按收藏范围路由选库，并返回
    路由结论（route_matched/route_reason）供前端预选与展示理由——规则
    只决定默认值，用户在弹窗里改库即显式指定，下次预检不再带路由徽标。

    返回字段：mode（watch/inplace/downloader_default）、path（movieclaw
    视角的投递基底目录）、library_id/library_name、downloader_name、
    route_matched/route_reason（走了路由才有）、ok、warning
    （不 ok 时的中文指引）。
    """
    from movieclaw_api.services.library.config import LibraryConfigService
    from movieclaw_api.services.library.routing import resolve_save_path
    from movieclaw_api.services.torrent_submit import mapping_covers
    from movieclaw_db.models.downloader_client import DownloaderClient
    from movieclaw_db.models.site_credential import ConfigStatus

    route_matched: bool | None = None
    route_reason: str | None = None
    if library_id is None and tmdb_id is not None:
        from movieclaw_api.services.library.routing import route_for_tmdb

        route_decision = await route_for_tmdb(session, kind, tmdb_id)
        library = route_decision.library
        route_matched = route_decision.matched
        route_reason = route_decision.reason
    else:
        library = await LibraryConfigService(session).resolve_for_subscription(library_id, kind)
    # 投递目录口径与真实投递同源（预检不给 title：条目目录到投递时才推导）
    decision = await resolve_save_path(session, library, kind=kind)
    base = decision.path

    result = await session.execute(
        select(DownloaderClient).where(
            DownloaderClient.is_default.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.enabled.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.status == ConfigStatus.ACTIVE,
        )
    )
    downloader = result.scalars().first()

    mode = decision.mode
    ok = True
    warning: str | None = None
    if downloader is None:
        ok = False
        warning = "没有可用的默认下载器，请先在「设置 → 下载器」添加并确保连接测试通过"
    elif base is None:
        ok = False
        warning = "没有可用的媒体库（或库未配置根路径），下载会落到下载器默认目录且不会自动入库"
    elif library is None:
        # 无库但存在同类型 auto/路径监听规则：种子有目录可投，但订阅闭环
        # 需要库承接——不能因为"投得出去"就报可行
        ok = False
        if getattr(decision.rule, "target_path", None):
            warning = (
                "没有可用的媒体库——下载完成后会整理到自定义目录，但没有库可承接"
                "回流入账，订阅无法闭环；请先到「媒体库」创建"
            )
        else:
            warning = (
                "没有可用的媒体库——种子会投到监听导入目录，但下载完成后无法自动入库；"
                "请先到「媒体库」创建"
            )
    elif downloader.path_mappings and not mapping_covers(base, downloader.path_mappings):
        ok = False
        warning = (
            f"目录 {base} 不在下载器「{downloader.name}」的路径映射覆盖范围内，"
            "届时投递会被拒绝——请补一条覆盖该目录的路径映射（映射按前缀匹配，"
            "映射公共父目录即可覆盖其下所有库），或为这个库配置监听导入规则"
        )
    return {
        "mode": mode,
        "path": base,
        "staging_path": getattr(decision.rule, "target_path", None),
        "library_id": library.id if library else None,
        "library_name": library.name if library else None,
        "downloader_name": downloader.name if downloader else None,
        "route_matched": route_matched,
        "route_reason": route_reason,
        "ok": ok,
        "warning": warning,
    }


async def _claim(session: AsyncSession, wanted_rows: list[WantedItem]) -> list[WantedItem]:
    """条件更新认领（防线①）：只把仍是 wanted 态的工单推进到 grabbed。

    逐条执行拿到精确的"谁被我认领了"；工单数量级小（整季包也就几十条），
    不值得为省几次 UPDATE 引入批量+回读的复杂度。
    """
    claimed: list[WantedItem] = []
    now = utcnow()
    for wanted in wanted_rows:
        result = await session.execute(
            update(WantedItem)
            .where(
                WantedItem.id == wanted.id,
                WantedItem.status.in_(  # type: ignore[attr-defined]
                    (WantedStatus.WANTED, WantedStatus.UPGRADING)
                ),
            )
            .values(status=WantedStatus.GRABBED, grabbed_at=now, updated_at=now)
        )
        if result.rowcount:
            claimed.append(wanted)
    await session.commit()
    return claimed


async def _rollback_claim(session: AsyncSession, claimed: list[WantedItem], *, retry_delay) -> None:
    """投递失败：认领回滚，退回调度通道（next_search_at 短冷却）择机重试。"""
    now = utcnow()
    for wanted in claimed:
        await session.execute(
            update(WantedItem)
            .where(WantedItem.id == wanted.id, WantedItem.status == WantedStatus.GRABBED)
            .values(
                status=(
                    WantedStatus.UPGRADING
                    if wanted.status == WantedStatus.UPGRADING
                    else WantedStatus.WANTED
                ),
                grabbed_at=None,
                next_search_at=now + retry_delay,
                updated_at=now,
            )
        )
    await session.commit()


async def _submit_real(
    session: AsyncSession,
    candidate: TorrentCandidate,
    *,
    save_path: str | None = None,
    subtitle: str | None = None,
):
    """真实投递：委托公共编排（站点取种 → 默认下载器提交，幂等判重）。

    save_path 按三级兜底解析（监听规则源目录 / 库条目目录 / None 退下载器
    默认目录），完成后的搬运/入账由监听导入或库扫描接管，库存对账关闭工单。
    subtitle 仅在投递目录为**条目级**时传入（download_hint 线索只能锚条目
    目录——锚到监听目录/默认目录会波及目录下全部内容）。
    """
    from movieclaw_api.services.torrent_submit import submit_torrent

    result, _row = await submit_torrent(
        session,
        site_id=candidate.site_id,
        download_url=candidate.download_url,
        tags=["movieclaw-sub"],
        save_path=save_path,
        subtitle=subtitle,
    )
    return result


def _describe(candidate: TorrentCandidate) -> str:
    """候选的一句话规格描述，进活动与日志。"""
    parts: list[str] = []
    if candidate.attrs.resolution:
        parts.append(candidate.attrs.resolution)
    if candidate.is_free is True:
        parts.append("free")
    if candidate.seeders is not None:
        parts.append(f"{candidate.seeders} 做种")
    return " · ".join(parts) if parts else "规格未知"
