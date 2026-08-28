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
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_db.models import (
    ActivityType,
    DownloadAttemptStatus,
    MediaItem,
    SiteTorrent,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories import SubscriptionRepository
from movieclaw_matcher import RuleVerdict, TorrentCandidate

logger = logging.getLogger("movieclaw_api.download_dispatch")


def _utc_text(value: datetime | None) -> str | None:
    """把数据库 naive UTC / 接口 aware 时间统一冻结为带偏移 ISO 文本。"""
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.isoformat()


async def dispatch(
    session: AsyncSession,
    *,
    subscription: Subscription,
    item: MediaItem,
    wanted_rows: list[WantedItem],
    candidate: TorrentCandidate,
    verdict: RuleVerdict,
    source: str,
    upgrade_rows: list[WantedItem] | None = None,
    upgrade_labels: tuple[str, str] | None = None,
    manual: bool = False,
) -> bool:
    """把候选投递给下载器，满足给定的一批工单。返回是否有实际投递发生。

    洗版（quality-upgrade.md §6.2）：``upgrade_rows`` 是候选要洗版的已入库
    单元——**不认领、不改 status、不动 info_hash**（保持 imported、指向旧
    版本，直到入库验证确认升级），只进 attempt 的 units 台账；attempt 标记
    ``purpose="upgrade"`` 供在途去重与旧版清理定位。``upgrade_labels`` 是
    (当前档位, 候选档位) 的展示标签，进活动文案。

    ``manual``：手动选种投递（用户显式选择）。落在 attempt.manual 上，
    洗版验证据此在"未能证明更优"时保留共存而不是证伪（§13.8）。
    """
    from movieclaw_api.services.subscription.core import recompute_subscription_status
    from movieclaw_api.services.subscription.matching import (
        DISPATCH_RETRY_DELAY,
        units_text,
    )

    upgrade_rows = upgrade_rows or []
    if upgrade_rows:
        # 洗版没有工单认领这道 DB 防线（不改 status），用投递前复查兜住
        # 被动匹配与搜索 worker 的并发窗口：单元已有在途洗版 attempt 即剔除
        upgrade_rows = await _filter_upgrade_in_flight(session, subscription, upgrade_rows)
    claimed = await _claim(session, wanted_rows)
    if not claimed and not upgrade_rows:
        return False  # 全部被另一条路径抢先，本候选无事可做

    repo = SubscriptionRepository(session)
    assert subscription.id is not None
    dry_run = get_settings().subscription_dispatch_dry_run
    submitted_info_hash: str | None = None
    all_targets = claimed + upgrade_rows
    units_label = units_text(all_targets)
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
        session, library, kind=subscription.kind, title=item.title, year=item.year, item=item
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
            submit_result, downloader_row = await _submit_real(
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
                    wanted_item_id=all_targets[0].id,
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
            normalized_hash = submit_result.info_hash.lower()
            submitted_info_hash = normalized_hash
            for wanted in claimed:
                await session.execute(
                    update(WantedItem)
                    .where(
                        WantedItem.id == wanted.id,
                        WantedItem.status == WantedStatus.GRABBED,
                    )
                    .values(info_hash=normalized_hash, updated_at=now)
                )
            # 网络提交期间用户可能刚好取消季订阅。infohash 仍要写入以保存真实
            # 投递历史，但只有仍在范围内的目标才能让尝试保持 active。
            # 洗版单元不写 info_hash（保持指向旧版本），其在途性由 attempt 表达。
            active_targets = list(
                (
                    await session.execute(
                        select(WantedItem).where(
                            WantedItem.subscription_id == subscription.id,
                            WantedItem.info_hash == normalized_hash,
                            WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                            WantedItem.status.in_(  # type: ignore[attr-defined]
                                (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            attempt_alive = bool(active_targets) or any(w.in_scope for w in upgrade_rows)
            existing_attempt = (
                await session.execute(
                    select(SubscriptionDownloadAttempt).where(
                        SubscriptionDownloadAttempt.subscription_id == subscription.id,
                        SubscriptionDownloadAttempt.info_hash == normalized_hash,
                    )
                )
            ).scalar_one_or_none()
            # 所有权：全新提交归我们；「已存在」通常是用户自己的任务（不碰），
            # 但刷流引擎抢下后被接管的（reclaimed_from_boost）本来就是
            # movieclaw 的，数据也已迁到目标目录——按自有任务记
            owned = not submit_result.already_exists or submit_result.reclaimed_from_boost
            values = {
                "downloader_id": downloader_row.id,
                "site_id": candidate.site_id,
                "torrent_id": candidate.torrent_id,
                "torrent_title": candidate.title,
                "download_name": submit_result.name or None,
                "save_path": dispatch_dir,
                "units": [[w.season_number, w.episode_number] for w in all_targets],
                "quality": candidate.attrs.model_dump(exclude_defaults=True),
                "hit_and_run": candidate.hit_and_run,
                "owned_by_movieclaw": owned,
                # 一旦承担过洗版语义就保持 upgrade（入库验证与旧版清理据此定位）
                "purpose": (
                    "upgrade"
                    if upgrade_rows
                    or (existing_attempt is not None and existing_attempt.purpose == "upgrade")
                    else "download"
                ),
                # 同理粘性：承担过手动选种语义就保持（验证裁决据此分流）
                "manual": manual or (existing_attempt is not None and existing_attempt.manual),
                "status": (
                    DownloadAttemptStatus.ACTIVE
                    if attempt_alive
                    else DownloadAttemptStatus.CANCELLED
                ),
                "baseline_completed_bytes": 0 if owned else None,
                "last_completed_bytes": 0 if owned else None,
                "baseline_downloaded_bytes": 0 if owned else None,
                "last_downloaded_bytes": 0 if owned else None,
                "last_progress_at": now,
                "stalled_notified_at": None,
                "missing_observations": 0,
                "next_search_at": None,
                "cleanup_note": (
                    None
                    if attempt_alive
                    else "网络投递完成前关联单元已退出订阅范围；保留下载器任务"
                ),
                "updated_at": now,
            }
            if existing_attempt is None:
                session.add(
                    SubscriptionDownloadAttempt(
                        subscription_id=subscription.id,
                        info_hash=normalized_hash,
                        **values,
                    )
                )
            else:
                existing_units = {
                    (int(unit[0]), int(unit[1]))
                    for unit in existing_attempt.units
                    if isinstance(unit, list) and len(unit) == 2
                }
                existing_units.update(
                    (wanted.season_number, wanted.episode_number) for wanted in all_targets
                )
                values["units"] = [[season, episode] for season, episode in sorted(existing_units)]
                # 同一 hash 是同一份内容的续用，不抹掉首次投递时已经证明的
                # 所有权/来源/品质历史；旧字段缺失时才用本次更完整的证据回填。
                values["owned_by_movieclaw"] = existing_attempt.owned_by_movieclaw or owned
                if existing_attempt.site_id:
                    values["site_id"] = existing_attempt.site_id
                    values["torrent_id"] = existing_attempt.torrent_id
                    values["torrent_title"] = existing_attempt.torrent_title
                if existing_attempt.download_name:
                    values["download_name"] = existing_attempt.download_name
                if existing_attempt.save_path:
                    values["save_path"] = existing_attempt.save_path
                if existing_attempt.quality:
                    values["quality"] = existing_attempt.quality
                if existing_attempt.hit_and_run is not None:
                    values["hit_and_run"] = existing_attempt.hit_and_run
                # 证伪过的洗版内容可能以同一 info_hash 在别的站点再现
                # （排除清单按 site/torrent 记，拦不住跨站同种）：实测已经
                # 证明它不构成升级，绝不复活为在途任务
                if (
                    existing_attempt.purpose == "upgrade"
                    and existing_attempt.status == DownloadAttemptStatus.FAILED
                ):
                    values["status"] = DownloadAttemptStatus.FAILED
                    values["purpose"] = "upgrade"
                for key, value in values.items():
                    setattr(existing_attempt, key, value)
                session.add(existing_attempt)
            await session.commit()

    # 活动是投递事实的永久台账：这里把资源发布→首次索引→提交下载器的时间链
    # 一并冻结。不能只在详情页临时查 site_torrent——用户移除站点会清索引，
    # 历史耗时仍应保留。手动选种不落索引，first_seen_at 合理为空。
    torrent_row = (
        await session.execute(
            select(SiteTorrent).where(
                SiteTorrent.site_id == candidate.site_id,
                SiteTorrent.torrent_id == candidate.torrent_id,
            )
        )
    ).scalar_one_or_none()
    resource_publish_time = candidate.publish_time or (
        torrent_row.publish_time if torrent_row is not None else None
    )
    resource_first_seen_at = torrent_row.created_at if torrent_row is not None else None
    submitted_at = utcnow()

    mode = "【模拟投递】" if dry_run else ""
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
    # 洗版投递用专属活动类型与"从 X 洗到 Y"文案；混合投递（缺口+洗版）
    # 仍是 GRABBED，文案附注洗版单元数
    if upgrade_rows and not claimed:
        activity_type = ActivityType.UPGRADE_GRABBED
        label_text = (
            f"{upgrade_labels[1]}（当前 {upgrade_labels[0]}）" if upgrade_labels else spec_text
        )
        message = (
            f"{units_label}发现更高版本，已提交洗版下载：来自 {candidate.site_id} 的"
            f"「{candidate.title[:60]}」（{label_text}）"
            + target_text
            + ("——模拟投递，未真实提交下载器" if dry_run else "")
        )
    else:
        activity_type = ActivityType.GRABBED
        message = (
            f"已投递{units_label}：来自 {candidate.site_id} 的"
            f"「{candidate.title[:60]}」（{spec_text}）"
            + (f"；其中 {units_text(upgrade_rows)}为洗版" if upgrade_rows else "")
            + target_text
            + ("——模拟投递，未真实提交下载器" if dry_run else "")
        )
    # 单集履历注解：把「这集靠哪个种子拿到」冻结在工单上，详情页里程碑链的
    # 投递站直接读，不再回活动流水里按季集捞。只写认领的缺口单元——洗版
    # 单元仍指向在库旧版本，升级确认前候选种子不是它的「来源」。
    # 提交失败在上方已 return，走到这里必然是投递成立（含 dry-run 模拟投递）；
    # 随下方 add_activity 的 commit 一起落库。
    if claimed:
        grab_note = f"{candidate.site_id} · {candidate.title[:120]}"
        grab_ts = utcnow()
        for wanted in claimed:
            await session.execute(
                update(WantedItem)
                .where(WantedItem.id == wanted.id)
                .values(grab_title=grab_note, updated_at=grab_ts)
            )
    await repo.add_activity(
        SubscriptionActivity(
            subscription_id=subscription.id,
            wanted_item_id=all_targets[0].id,
            type=activity_type,
            message=message,
            payload={
                "site_id": candidate.site_id,
                "torrent_id": candidate.torrent_id,
                "score": verdict.score,
                "source": source,
                "dry_run": dry_run,
                "info_hash": submitted_info_hash,
                "units": [[w.season_number, w.episode_number] for w in claimed],
                "upgrade_units": [[w.season_number, w.episode_number] for w in upgrade_rows],
                "resource_publish_time": _utc_text(resource_publish_time),
                "resource_first_seen_at": _utc_text(resource_first_seen_at),
                "submitted_at": _utc_text(submitted_at),
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
        push_verb = "开始洗版下载" if upgrade_rows and not claimed else "开始下载"
        notify_channels(
            f"📥 {push_verb}:《{item.title}》{year_text}{units_label}\n"
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
                    [(w.season_number, w.episode_number) for w in all_targets],
                    site_id=candidate.site_id,
                    torrent_title=candidate.title,
                    spec=spec_text,
                )
            ]
        )
    return True


async def preview_dispatch_route(
    session: AsyncSession,
    *,
    kind: str,
    library_id: int | None,
    tmdb_id: int | None = None,
    downloader_id: int | None = None,
    title: str | None = None,
    year: int | None = None,
) -> dict:
    """预演一次投递的路由结论（订阅弹窗/下载弹窗的预检数据源）。

    与 dispatch() 的三级兜底同源：监听规则源目录 → 库主根（条目目录的
    基底）→ 下载器默认目录；再叠加 submit_torrent 的映射覆盖守门判定。
    ``downloader_id`` 缺省时维持默认下载器语义，显式传入时则用该台的
    可用状态与路径映射预演，供手动下载弹窗的多下载器选择复用。
    只读不投，返回结构化结论让前端在**订阅那一刻**就把问题亮给用户，
    而不是等投递失败/落点告警才发现。

    ``library_id`` 缺省且给了 ``tmdb_id`` 时按收藏范围路由选库，并返回
    路由结论（route_matched/route_reason）供前端预选与展示理由——规则
    只决定默认值，用户在弹窗里改库即显式指定，下次预检不再带路由徽标。

    ``title``/``year`` 只影响展示用的 ``entry_dir``（条目目录预览），不参与
    mode/path 的推导——预检给出的是投递基底目录，条目目录到投递时才推导。

    返回字段：mode（watch/inplace/downloader_default）、path（movieclaw
    视角的投递基底目录）、entry_dir（条目目录的完整路径预览，见下）、
    library_id/library_name、downloader_name、
    route_matched/route_reason（走了路由才有）、ok、warning
    （不 ok 时的中文指引）。
    """
    from movieclaw_api.services.library.config import LibraryConfigService, derive_save_path
    from movieclaw_api.services.library.routing import resolve_save_path
    from movieclaw_api.services.torrent_submit import mapping_covers
    from movieclaw_db.models.downloader_client import DownloaderClient
    from movieclaw_db.models.site_credential import ConfigStatus

    route_matched: bool | None = None
    route_reason: str | None = None
    routed_item = None
    if library_id is None and tmdb_id is not None:
        from movieclaw_api.services.library.routing import route_for_tmdb

        route_decision = await route_for_tmdb(session, kind, tmdb_id)
        library = route_decision.library
        route_matched = route_decision.matched
        route_reason = route_decision.reason
        routed_item = route_decision.item
    else:
        library = await LibraryConfigService(session).resolve_for_subscription(library_id, kind)
    # 投递目录口径与真实投递同源（预检不给 title：条目目录到投递时才推导）
    decision = await resolve_save_path(session, library, kind=kind)
    base = decision.path
    # 条目目录预览：模板化之后前端不能再自己拼「标题 (年份)」，落点长什么样
    # 只能由后端用同一套模板渲染（命名同源，见 library/naming.py）。
    # 标题取值：已建档条目的权威标题优先，未建档（临时条目标题是 tmdb#id
    # 占位符）时用调用方传入的识别标题。
    if routed_item is None and tmdb_id is not None:
        # 显式选库时不走路由，但条目可能已建档——命名模板里的
        # {original_title}/{tmdb_id} 需要条目才渲染得出，补一次轻量查询
        from movieclaw_db.models import MediaItem

        routed_item = (
            await session.execute(
                select(MediaItem).where(MediaItem.kind == kind, MediaItem.tmdb_id == tmdb_id)
            )
        ).scalar_one_or_none()
    if routed_item is not None and routed_item.id is not None:
        entry_title, entry_year = routed_item.title, routed_item.year
        entry_item: object | None = routed_item
    else:
        entry_title, entry_year, entry_item = title, year, None
    entry_dir = (
        derive_save_path(library, title=entry_title, year=entry_year, item=entry_item)
        if library is not None and entry_title
        else None
    )

    if downloader_id is not None:
        # 手动下载弹窗可以显式选第二台下载器。预检必须以该台的路径映射
        # 判断，不能仍偷看默认下载器，否则「预检可投、提交被拒」会重新出现。
        downloader = await session.get(DownloaderClient, downloader_id)
    else:
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
        warning = (
            f"指定下载器 #{downloader_id} 不存在"
            if downloader_id is not None
            else "没有可用的默认下载器，请先在「设置 → 下载器」添加并确保连接测试通过"
        )
    elif not downloader.enabled or downloader.status != ConfigStatus.ACTIVE:
        ok = False
        warning = f"下载器「{downloader.name}」当前不可用（已停用或连接验证未通过）"
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
        "entry_dir": entry_dir,
        "staging_path": getattr(decision.rule, "target_path", None),
        "library_id": library.id if library else None,
        "library_name": library.name if library else None,
        "downloader_name": downloader.name if downloader else None,
        "route_matched": route_matched,
        "route_reason": route_reason,
        "ok": ok,
        "warning": warning,
    }


async def _filter_upgrade_in_flight(
    session: AsyncSession,
    subscription: Subscription,
    upgrade_rows: list[WantedItem],
) -> list[WantedItem]:
    """剔除已有在途洗版 attempt 的单元（洗版投递的并发防线）。"""
    in_flight: set[tuple[int, int]] = set()
    attempts = (
        await session.execute(
            select(SubscriptionDownloadAttempt).where(
                SubscriptionDownloadAttempt.subscription_id == subscription.id,
                SubscriptionDownloadAttempt.purpose == "upgrade",
                SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                    (
                        DownloadAttemptStatus.ACTIVE,
                        DownloadAttemptStatus.REPLACEMENT_PENDING,
                        DownloadAttemptStatus.TRIAL,
                        DownloadAttemptStatus.CLEANUP_PENDING,
                        DownloadAttemptStatus.COMPLETED,
                    )
                ),
            )
        )
    ).scalars()
    for attempt in attempts:
        in_flight.update(
            (int(u[0]), int(u[1])) for u in attempt.units if isinstance(u, list) and len(u) == 2
        )
    return [w for w in upgrade_rows if (w.season_number, w.episode_number) not in in_flight]


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
                WantedItem.status == WantedStatus.WANTED,
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
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
            .where(
                WantedItem.id == wanted.id,
                WantedItem.status == WantedStatus.GRABBED,
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            )
            .values(
                status=WantedStatus.WANTED,
                grabbed_at=None,
                next_search_at=now + retry_delay,
                updated_at=now,
            )
        )
        # 网络失败期间若用户取消了该范围，认领仍应回滚，但不能留下一个会在
        # 重新勾选前触发的退避时间。历史没有真实投递，所以回到 wanted 合理。
        await session.execute(
            update(WantedItem)
            .where(
                WantedItem.id == wanted.id,
                WantedItem.status == WantedStatus.GRABBED,
                WantedItem.in_scope.is_(False),  # type: ignore[attr-defined]
            )
            .values(
                status=WantedStatus.WANTED,
                grabbed_at=None,
                next_search_at=None,
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

    result, row = await submit_torrent(
        session,
        site_id=candidate.site_id,
        download_url=candidate.download_url,
        tags=["movieclaw-sub"],
        save_path=save_path,
        subtitle=subtitle,
    )
    return result, row


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
