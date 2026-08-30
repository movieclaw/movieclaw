"""订阅下载心跳与死种换源巡检。

下载器是实时状态事实源，本表只持久化“完成字节最后一次增长”的心跳：连续
15 分钟不增长时提醒用户可立即换种，30 分钟时自动执行真实跨站搜索。旧源在
替代源证明有真实进度前始终保留；暂停、排队、校验不计入死种时间，下载器
不可达也只表示状态未知，不能误判成任务消失。

完成后的搬运仍由监听导入/库扫描负责，库存对账负责关闭 wanted；本任务只
观察投递、驱动换源，并核验已完成任务的落点与实际集数。若种子声明覆盖的
剧集不在文件清单中，只把缺失部分退回重新寻找，已存在的集数照常等待入库。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.subscription import units_text
from movieclaw_api.services.system_notice import resolve_notices, upsert_notice
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    ActivityType,
    DownloadAttemptStatus,
    MediaItem,
    SiteTorrent,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.models.downloader_client import DownloaderClient
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.models.system_notice import NoticeSeverity
from movieclaw_db.repositories import SubscriptionRepository
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader import DownloaderConfig, TorrentStatus, create_downloader
from movieclaw_media.models import MediaKind
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.download_progress")

# 五分钟采样一次；三次可达但查无任务，恰好对应 15 分钟提醒窗口。
PROGRESS_TICK_SECONDS = 300
STALLED_WARNING_MINUTES = 15
AUTO_REPLACEMENT_MINUTES = 30
TRIAL_TIMEOUT_MINUTES = 30
MISSING_CONFIRMATIONS = 3

_tick_lock = asyncio.Lock()

# 在途状态：GRABBED 为主；DOWNLOADED 是旧版管线的遗留中间态（新架构不再
# 写入），存量行按同样语义照看直至库存对账关闭
_IN_FLIGHT = (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)


@register_task(
    "check_download_progress",
    title="订阅下载换源巡检",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=PROGRESS_TICK_SECONDS,
    description=(
        "按完成字节心跳照看订阅种子：15 分钟无进度提醒，30 分钟自动跨站寻找"
        "同品质替代源；旧源保留到新源证明可下载。已完成的同时核验落点，"
        "movieclaw 看不到文件时告警，实际内容缺集时只退回缺失部分。"
        "下载完成后的入库由监听导入/库扫描完成，工单由库存对账关闭。"
    ),
)
async def check_download_progress() -> None:
    async with _tick_lock:
        db = get_database()
        async with db.session() as session:
            groups = await _pipeline_groups(session)
            await _ensure_attempts(session, groups)
            attempts = list(
                (
                    await session.execute(
                        select(SubscriptionDownloadAttempt).where(
                            SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                                (
                                    DownloadAttemptStatus.ACTIVE,
                                    DownloadAttemptStatus.REPLACEMENT_PENDING,
                                    DownloadAttemptStatus.TRIAL,
                                    DownloadAttemptStatus.CLEANUP_PENDING,
                                    DownloadAttemptStatus.COMPLETED,
                                )
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            # 孤儿在途组：attempt 已终态（imported/failed/superseded/retained，
            # cancelled 已被 _ensure_attempts 复活）但名下工单仍在途——心跳
            # 不再照看它们，完成核验轮不到，覆盖虚标的缺集会永久挂起
            watched_keys = {
                (a.subscription_id, (a.info_hash or "").lower()) for a in attempts
            }
            orphaned = sorted(
                {(sub_id, h.lower()) for (sub_id, h) in groups} - watched_keys
            )
            if not attempts and not orphaned:
                return
            downloaders = await _usable_downloaders(session)
        if not downloaders:
            logger.warning(
                "有 %d 个订阅下载等待照看，但没有可用的下载器",
                len(attempts) + len(orphaned),
            )
            return
        for attempt in attempts:
            if attempt.id is None:
                continue
            try:
                if attempt.status == DownloadAttemptStatus.CLEANUP_PENDING:
                    from movieclaw_api.services.subscription import (
                        reconcile_pending_cleanup,
                    )

                    await reconcile_pending_cleanup(attempt.id)
                    continue
                should_search = await _observe_attempt(attempt.id, downloaders)
                if should_search:
                    from movieclaw_api.services.subscription import run_replacement_search

                    await run_replacement_search(attempt.id)
            except Exception:  # noqa: BLE001 -- 单组失败不拖垮整轮
                logger.exception("订阅下载尝试 #%s 的换源巡检失败", attempt.id)
        for sub_id, info_hash in orphaned:
            try:
                await _reconcile_orphaned_group(sub_id, info_hash, downloaders)
            except Exception:  # noqa: BLE001 -- 单组失败不拖垮整轮
                logger.exception("孤儿在途工单对账失败：订阅 #%s 种子 %s", sub_id, info_hash)


async def _pipeline_groups(
    session: AsyncSession,
) -> dict[tuple[int, str], list[WantedItem]]:
    """在途工单，按（订阅, 种子）分组。"""
    result = await session.execute(
        select(WantedItem).where(
            WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
            WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            WantedItem.info_hash.is_not(None),  # type: ignore[union-attr]
        )
    )
    groups: dict[tuple[int, str], list[WantedItem]] = {}
    for row in result.scalars().all():
        assert row.info_hash is not None
        groups.setdefault((row.subscription_id, row.info_hash), []).append(row)
    return groups


async def _usable_downloaders(
    session: AsyncSession,
) -> list[tuple[DownloaderClient, DownloaderConfig]]:
    """全部可用（启用 + 连接验证通过）的下载器及其连接配置。"""
    repo = DownloaderRepository(session)
    rows = await repo.list_all()
    usable = []
    for row in rows:
        if not row.enabled or row.status != ConfigStatus.ACTIVE:
            continue
        usable.append(
            (
                row,
                DownloaderConfig(
                    type=row.client_type.value,
                    url=row.url,
                    username=row.username,
                    password=repo.decrypted_password(row),
                ),
            )
        )
    return usable


@dataclass(frozen=True)
class _TorrentLookup:
    """一次跨下载器查询结果；可达数量用于区分“缺失”和“状态未知”。"""

    match: tuple[DownloaderClient, TorrentStatus] | None
    reachable_count: int


async def _lookup_torrent(
    info_hash: str,
    downloaders: list[tuple[DownloaderClient, DownloaderConfig]],
    *,
    include_files: bool = True,
    preferred_downloader_id: int | None = None,
) -> _TorrentLookup:
    """查询种子并保留可达性证据；优先查尝试实际投递到的下载器。"""
    ordered = sorted(
        downloaders,
        key=lambda entry: 0 if entry[0].id == preferred_downloader_id else 1,
    )
    reachable_count = 0
    for row, config in ordered:
        adapter = create_downloader(config)
        try:
            status = await adapter.get_torrent(info_hash, include_files=include_files)
            reachable_count += 1
        except Exception as exc:  # noqa: BLE001 -- 单台不可达降级继续
            logger.warning("查询下载器「%s」失败：%s", row.name, exc)
            continue
        finally:
            try:
                await adapter.close()
            except Exception:  # noqa: BLE001 -- 关闭失败不改变本次查询事实
                logger.warning("关闭下载器「%s」连接失败", row.name, exc_info=True)
        if status is not None:
            return _TorrentLookup((row, status), reachable_count)
    return _TorrentLookup(None, reachable_count)


async def _query_torrent(
    info_hash: str,
    downloaders: list[tuple[DownloaderClient, DownloaderConfig]],
    *,
    include_files: bool = True,
) -> tuple[DownloaderClient, TorrentStatus] | None:
    """在全部可用下载器中查找种子（先到先得；单台故障不影响其余）。

    连同命中的下载器记录一起返回——落点核验需要它的路径映射做反向翻译。
    进度快照类的高频轮询传 ``include_files=False``，省掉文件清单的获取开销。
    """
    lookup = await _lookup_torrent(info_hash, downloaders, include_files=include_files)
    return lookup.match


async def _ensure_attempts(
    session: AsyncSession,
    groups: dict[tuple[int, str], list[WantedItem]],
) -> None:
    """给升级前已在途的 wanted 补安全保守的尝试台账。"""
    if not groups:
        return
    normalized_groups = {
        (subscription_id, info_hash.lower()): rows
        for (subscription_id, info_hash), rows in groups.items()
    }
    existing_attempts = list(
        (
            await session.execute(select(SubscriptionDownloadAttempt))
        ).scalars().all()
    )
    existing_rows = [
        (row.subscription_id, row.info_hash, row) for row in existing_attempts
    ]
    existing = {
        (subscription_id, info_hash.lower()): row
        for subscription_id, info_hash, row in existing_rows
    }
    for key, rows in normalized_groups.items():
        old = existing.get(key)
        if old is None or old.status != DownloadAttemptStatus.CANCELLED:
            continue
        old.status = (
            DownloadAttemptStatus.COMPLETED
            if all(row.status == WantedStatus.DOWNLOADED for row in rows)
            else DownloadAttemptStatus.ACTIVE
        )
        old.last_progress_at = utcnow()
        old.missing_observations = 0
        old.next_search_at = None
        old.cleanup_note = None
        session.add(old)
    existing_keys = set(existing)
    for subscription_id, info_hash in set(normalized_groups) - existing_keys:
        rows = normalized_groups[(subscription_id, info_hash)]
        now = utcnow()
        started_at = max((row.grabbed_at or row.updated_at) for row in rows)
        row_units = {(row.season_number, row.episode_number) for row in rows}
        grabbed_activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity)
                    .where(
                        SubscriptionActivity.subscription_id == subscription_id,
                        SubscriptionActivity.type == ActivityType.GRABBED,
                    )
                    .order_by(SubscriptionActivity.id.desc())  # type: ignore[attr-defined]
                )
            )
            .scalars()
            .all()
        )
        source_row = None
        for activity in grabbed_activities:
            payload = activity.payload or {}
            # 旧版活动没有 infohash，多个并行种子覆盖相同单元时无法知道活动
            # 属于哪一个。只有精确 hash 证据才允许恢复品质底线；否则保持未知，
            # 后续 quality_not_lower 会禁止自动降级换源。
            if str(payload.get("info_hash") or "").lower() != info_hash:
                continue
            activity_units = {
                (int(unit[0]), int(unit[1]))
                for unit in payload.get("units", [])
                if isinstance(unit, list) and len(unit) == 2
            }
            if activity_units and not (activity_units & row_units):
                continue
            site_id = payload.get("site_id")
            torrent_id = payload.get("torrent_id")
            if not site_id or not torrent_id:
                continue
            source_row = (
                await session.execute(
                    select(SiteTorrent).where(
                        SiteTorrent.site_id == str(site_id),
                        SiteTorrent.torrent_id == str(torrent_id),
                    )
                )
            ).scalar_one_or_none()
            if source_row is not None:
                break
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=subscription_id,
                info_hash=info_hash,
                site_id=source_row.site_id if source_row is not None else None,
                torrent_id=source_row.torrent_id if source_row is not None else None,
                torrent_title=source_row.title if source_row is not None else "",
                units=[[row.season_number, row.episode_number] for row in rows],
                quality=(source_row.attrs or {}) if source_row is not None else {},
                # 存量任务无法证明由 MovieClaw 首次创建，也无法证明没有 H&R；
                # 两项都按未知处理，换源后只会保留，不会自动删用户数据。
                owned_by_movieclaw=False,
                hit_and_run=source_row.hit_and_run if source_row is not None else None,
                status=DownloadAttemptStatus.ACTIVE,
                last_progress_at=min(started_at, now),
            )
        )
    await session.commit()


async def _observe_attempt(
    attempt_id: int,
    downloaders: list[tuple[DownloaderClient, DownloaderConfig]],
) -> bool:
    """观察一次完成字节心跳；返回本轮是否应发起真实替代源搜索。"""
    db = get_database()
    async with db.session() as session:
        attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
        if attempt is None or attempt.status not in (
            DownloadAttemptStatus.ACTIVE,
            DownloadAttemptStatus.REPLACEMENT_PENDING,
            DownloadAttemptStatus.TRIAL,
            DownloadAttemptStatus.COMPLETED,
        ):
            return False
        if attempt.status == DownloadAttemptStatus.TRIAL:
            if not await _trial_has_open_target(session, attempt):
                if await _cancel_attempt_if_out_of_scope(session, attempt):
                    return False
                from movieclaw_api.services.subscription import fail_trial

                await fail_trial(session, attempt, reason="原订阅工单已由入库或其他流程满足")
                return False
        elif attempt.purpose == "upgrade":
            # 洗版 attempt 的目标单元本来就是 imported（工单不重开），下方
            # 缺口语义的"工单已闭合→退出观察"判据对它恒真，会在首个巡检
            # tick 把刚投递的洗版任务错误完结。洗版的完成/证伪由入库验证
            # 裁决（verify_upgrades 置 IMPORTED/FAILED）；这里只做两件事：
            # 单元全部退出范围时止损取消，否则继续心跳观察与死种换源
            from movieclaw_api.services.subscription import upgrade_attempt_wanted_rows

            tracked = await upgrade_attempt_wanted_rows(session, attempt, in_scope_only=False)
            if tracked and not any(row.in_scope for row in tracked):
                attempt.status = DownloadAttemptStatus.CANCELLED
                attempt.next_search_at = None
                attempt.cleanup_note = (
                    "关联单元已退出当前订阅范围；保留下载器任务，不再观察或换源"
                )
                attempt.updated_at = utcnow()
                session.add(attempt)
                await session.commit()
                return False
        elif not await _attempt_wanted_rows(session, attempt):
            if await _cancel_attempt_if_out_of_scope(session, attempt):
                return False
            # 库存对账已经关闭工单后，下载任务可继续由用户保种，但不再属于
            # “需要救援的缺口”，否则会无意义地跨站重复下载已入库内容。
            promoted_child = (
                await session.execute(
                    select(SubscriptionDownloadAttempt.id).where(
                        SubscriptionDownloadAttempt.replaces_attempt_id == attempt.id,
                        SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                            (DownloadAttemptStatus.ACTIVE, DownloadAttemptStatus.COMPLETED)
                        ),
                    )
                )
            ).first()
            attempt.status = (
                DownloadAttemptStatus.CLEANUP_PENDING
                if promoted_child is not None
                else DownloadAttemptStatus.IMPORTED
            )
            attempt.cleanup_note = (
                "替代源已接管，等待恢复旧任务清理"
                if promoted_child is not None
                else "关联工单已完成入库，不再执行死种换源"
            )
            attempt.updated_at = utcnow()
            session.add(attempt)
            await session.commit()
            return False
        info_hash = attempt.info_hash
        preferred_id = attempt.downloader_id

    lookup = await _lookup_torrent(
        info_hash,
        downloaders,
        preferred_downloader_id=preferred_id,
    )
    async with db.session() as session:
        attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
        if attempt is None or attempt.status not in (
            DownloadAttemptStatus.ACTIVE,
            DownloadAttemptStatus.REPLACEMENT_PENDING,
            DownloadAttemptStatus.TRIAL,
            DownloadAttemptStatus.COMPLETED,
        ):
            return False
        if await _cancel_attempt_if_out_of_scope(session, attempt):
            return False
        now = utcnow()
        attempt.last_observed_at = now

        if lookup.reachable_count == 0:
            # 所有下载器都不可达时没有“任务已消失”的证据，不动心跳和计数。
            attempt.last_downloader_state = "unknown"
            attempt.updated_at = now
            session.add(attempt)
            await session.commit()
            return False

        if lookup.match is None:
            attempt.missing_observations += 1
            attempt.last_downloader_state = "missing"
            attempt.updated_at = now
            session.add(attempt)
            await session.commit()
            if (
                attempt.status == DownloadAttemptStatus.TRIAL
                and attempt.missing_observations >= MISSING_CONFIRMATIONS
            ):
                from movieclaw_api.services.subscription import fail_trial

                await fail_trial(session, attempt, reason="替代源连续三次未在可达下载器中找到")
                return False
            if attempt.missing_observations >= MISSING_CONFIRMATIONS:
                if attempt.status == DownloadAttemptStatus.COMPLETED:
                    # 已完成任务若在入库前连任务本身也消失，不能永久藏在 completed。
                    # 文件可能仍在磁盘，因此仍沿用 15/30 分钟窗口寻找替代源，
                    # 晋升时旧任务查询不到只会幂等收口，不会删除未知文件。
                    attempt.completed_at = attempt.completed_at or now
                    attempt.status = DownloadAttemptStatus.ACTIVE
                elif await _requeue_missing_attempt(session, attempt, now):
                    return False
                return await _handle_stalled_attempt(session, attempt, now)
            return False

        downloader, status = lookup.match
        attempt.downloader_id = downloader.id
        attempt.missing_observations = 0
        attempt.last_downloader_state = status.state
        current_bytes = status.completed_bytes

        # 下载器在“完成”后可能因重新校验或文件损坏退回未完成；恢复成主源状态
        # 重新计时，不能卡在只做落点核验的 completed。
        if attempt.status == DownloadAttemptStatus.COMPLETED and not status.completed:
            attempt.status = DownloadAttemptStatus.ACTIVE
            attempt.last_progress_at = now
            attempt.stalled_notified_at = None
            attempt.next_search_at = None

        if status.state in {"paused", "queued", "checking"}:
            # 用户暂停、下载器排队和校验都不是死种；不断刷新计时基准，恢复后
            # 必须重新经历完整 15/30 分钟窗口。
            attempt.last_progress_at = now
            attempt.stalled_notified_at = None
            attempt.updated_at = now
            session.add(attempt)
            await session.commit()
            return False

        progressed = False
        if current_bytes is not None:
            if attempt.last_completed_bytes is None:
                attempt.last_completed_bytes = current_bytes
                if attempt.status != DownloadAttemptStatus.TRIAL:
                    attempt.last_progress_at = now
            elif current_bytes > attempt.last_completed_bytes:
                attempt.last_completed_bytes = current_bytes
                if attempt.status != DownloadAttemptStatus.TRIAL:
                    attempt.last_progress_at = now
                    attempt.stalled_notified_at = None
                    progressed = True
            elif current_bytes < attempt.last_completed_bytes:
                # 下载器重新校验/重置数据后从新基线计时，不能沿用旧完成字节。
                attempt.last_completed_bytes = current_bytes
                attempt.baseline_completed_bytes = current_bytes
                if attempt.status != DownloadAttemptStatus.TRIAL:
                    attempt.last_progress_at = now
                    attempt.stalled_notified_at = None

        if attempt.status == DownloadAttemptStatus.TRIAL:
            current_downloaded = getattr(status, "downloaded_bytes", None)
            network_progressed = False
            if current_downloaded is not None:
                if attempt.last_downloaded_bytes is None:
                    attempt.last_downloaded_bytes = current_downloaded
                    if attempt.baseline_downloaded_bytes is None:
                        attempt.baseline_downloaded_bytes = current_downloaded
                elif current_downloaded > attempt.last_downloaded_bytes:
                    attempt.last_downloaded_bytes = current_downloaded
                    attempt.last_progress_at = now
                    network_progressed = True
                elif current_downloaded < attempt.last_downloaded_bytes:
                    # 下载器重置统计计数后重新建立基线，不能把跨重置差值当流量。
                    attempt.last_downloaded_bytes = current_downloaded
                    attempt.baseline_downloaded_bytes = current_downloaded
                    attempt.last_progress_at = now
            baseline = attempt.baseline_downloaded_bytes
            proven = (not attempt.owned_by_movieclaw and status.completed) or (
                network_progressed
                and baseline is not None
                and current_downloaded is not None
                and current_downloaded - baseline >= 1024 * 1024
            )
            attempt.updated_at = now
            session.add(attempt)
            await session.commit()
            if proven:
                from movieclaw_api.services.subscription import promote_trial

                await promote_trial(session, attempt, status)
                return False
            if now - attempt.last_progress_at >= timedelta(minutes=TRIAL_TIMEOUT_MINUTES):
                from movieclaw_api.services.subscription import fail_trial

                await fail_trial(session, attempt, reason="替代源试用 30 分钟仍无真实下载进度")
            return False

        if status.completed:
            attempt.status = DownloadAttemptStatus.COMPLETED
            attempt.completed_at = attempt.completed_at or now
            attempt.last_completed_bytes = current_bytes
            attempt.updated_at = now
            session.add(attempt)
            await session.commit()
            pending_trials = list(
                (
                    await session.execute(
                        select(SubscriptionDownloadAttempt).where(
                            SubscriptionDownloadAttempt.replaces_attempt_id == attempt.id,
                            SubscriptionDownloadAttempt.status == DownloadAttemptStatus.TRIAL,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if pending_trials:
                from movieclaw_api.services.subscription import fail_trial

                for trial in pending_trials:
                    await fail_trial(
                        session,
                        trial,
                        reason="旧源已恢复并下载完成，无需继续试用替代源",
                    )
            rows = await _attempt_wanted_rows(session, attempt)
            if rows:
                await _verify_completed_download(
                    session,
                    rows,
                    attempt.info_hash,
                    downloader,
                    status,
                )
            return False

        attempt.updated_at = now
        if progressed and attempt.status == DownloadAttemptStatus.REPLACEMENT_PENDING:
            trial_exists = (
                await session.execute(
                    select(SubscriptionDownloadAttempt.id).where(
                        SubscriptionDownloadAttempt.replaces_attempt_id == attempt.id,
                        SubscriptionDownloadAttempt.status == DownloadAttemptStatus.TRIAL,
                    )
                )
            ).first()
            if trial_exists is None:
                attempt.status = DownloadAttemptStatus.ACTIVE
                attempt.next_search_at = None
                attempt.search_attempts = 0
        session.add(attempt)
        await session.commit()
        if progressed:
            logger.debug("订阅种子 %s 完成字节继续增长", attempt.info_hash)
            return False
        return await _handle_stalled_attempt(session, attempt, now)


async def _handle_stalled_attempt(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
    now,
) -> bool:
    """应用 15 分钟提醒和 30 分钟自动换源边界。"""
    elapsed = now - attempt.last_progress_at
    repo = SubscriptionRepository(session)
    if (
        elapsed >= timedelta(minutes=STALLED_WARNING_MINUTES)
        and attempt.stalled_notified_at is None
    ):
        attempt.stalled_notified_at = now
        attempt.updated_at = now
        session.add(attempt)
        await session.commit()
        await repo.add_activity(
            SubscriptionActivity(
                subscription_id=attempt.subscription_id,
                type=ActivityType.DOWNLOAD_STALLED,
                message=(
                    "下载已连续 15 分钟没有进度，可在活动页立即换种；"
                    "30 分钟时将自动寻找同品质替代源"
                ),
                payload={"info_hash": attempt.info_hash, "reason": "no_byte_progress"},
            )
        )
    if elapsed < timedelta(minutes=AUTO_REPLACEMENT_MINUTES):
        return False
    if attempt.status == DownloadAttemptStatus.ACTIVE:
        attempt.status = DownloadAttemptStatus.REPLACEMENT_PENDING
        attempt.next_search_at = now
        attempt.updated_at = now
        session.add(attempt)
        await session.commit()
    return bool(attempt.next_search_at is not None and attempt.next_search_at <= now)


async def _requeue_missing_attempt(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
    now,
) -> bool:
    """主源确证从所有可达下载器消失时，把工单退回 wanted 让常规搜索接手。

    换源策略保守地"等新源产生真实进度才切换"，前提是旧源还在下载、值得保护；
    源本身已经不在了的时候这个前提不成立。此时工单继续挂 grabbed，业务层会
    把它当"在途"，用户的「立即搜索」「手动选种」都以"没有缺口"被拒，能点的
    只剩「立即换种」——每点一次多攒一个同样下不动的源（issue #238）。退回
    工单既恢复人工介入入口，也让常规缺口搜索重新接手。

    调用方已保证：下载器可达（否则只是状态未知）且已连续 MISSING_CONFIRMATIONS
    次查无任务。这里再守三条，任一不满足都维持原有的换源路径：

    - 洗版投递（purpose=upgrade）的目标工单本就是 imported，不存在"退回缺口"
      的语义，其成败由入库验证裁决；
    - 名下还有试用替代源时先让试用裁决（晋升接管 / 超时失败）——工单此刻仍
      指向旧源，退回会让试用源失去目标被判失败，连数据一起清掉；
    - 曾经完成过的任务文件很可能已落盘等入库，退回只会重复下载同一份内容。

    返回是否已退回。
    """
    if attempt.purpose != "download" or attempt.completed_at is not None:
        return False
    trial_exists = (
        await session.execute(
            select(SubscriptionDownloadAttempt.id).where(
                SubscriptionDownloadAttempt.replaces_attempt_id == attempt.id,
                SubscriptionDownloadAttempt.status == DownloadAttemptStatus.TRIAL,
            )
        )
    ).first()
    if trial_exists is not None:
        return False
    rows = await _attempt_wanted_rows(session, attempt)
    if not rows:
        return False
    item = await session.get(MediaItem, rows[0].media_item_id)
    if item is None:
        return False

    attempt.status = DownloadAttemptStatus.FAILED
    attempt.next_search_at = None
    attempt.cleanup_note = "任务已从所有可达下载器中消失，关联工单已退回重新寻找"
    attempt.updated_at = now
    session.add(attempt)
    await _requeue(
        session,
        SubscriptionRepository(session),
        item,
        rows,
        attempt.info_hash,
        message=(
            f"原下载任务已从所有可达的下载器中消失（连续 {MISSING_CONFIRMATIONS} 次确认，"
            "可能被手动删除或被下载器清理），"
            f"{units_text(rows)} 已退回重新寻找资源；也可在订阅详情页立即搜索或手动选种"
        ),
        reason="source_missing",
    )
    return True


async def _attempt_wanted_rows(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
) -> list[WantedItem]:
    return list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == attempt.subscription_id,
                    WantedItem.info_hash == attempt.info_hash,
                    WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )


async def _cancel_attempt_if_out_of_scope(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
) -> bool:
    """并发兜底：网络观察返回时目标若已退出范围，原地止损且不碰下载器。"""
    source = attempt
    if (
        attempt.status == DownloadAttemptStatus.TRIAL
        and attempt.replaces_attempt_id is not None
    ):
        parent = await session.get(SubscriptionDownloadAttempt, attempt.replaces_attempt_id)
        if parent is None:
            return False
        source = parent
    allowed = {
        (int(unit[0]), int(unit[1]))
        for unit in attempt.units
        if isinstance(unit, list) and len(unit) == 2
    }
    historical = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == source.subscription_id,
                    WantedItem.info_hash == source.info_hash,
                    WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    if allowed:
        historical = [
            row
            for row in historical
            if (row.season_number, row.episode_number) in allowed
        ]
    if not historical or any(row.in_scope for row in historical):
        return False
    attempt.status = DownloadAttemptStatus.CANCELLED
    attempt.next_search_at = None
    attempt.cleanup_note = "关联单元已退出当前订阅范围；保留下载器任务，不再观察或换源"
    attempt.updated_at = utcnow()
    session.add(attempt)
    await session.commit()
    return True


async def _trial_has_open_target(
    session: AsyncSession,
    trial: SubscriptionDownloadAttempt,
) -> bool:
    if trial.replaces_attempt_id is None:
        return False
    old = await session.get(SubscriptionDownloadAttempt, trial.replaces_attempt_id)
    if old is None:
        return False
    allowed = {
        (int(unit[0]), int(unit[1]))
        for unit in trial.units
        if isinstance(unit, list) and len(unit) == 2
    }
    if old.purpose == "upgrade":
        # 洗版换源的目标判定走洗版语义：单元仍 imported 且在范围内、
        # 且尚未被验证收口（旧源仍处等待替代的活跃态）即为开放
        from movieclaw_api.services.subscription import upgrade_attempt_wanted_rows

        if old.status not in (
            DownloadAttemptStatus.ACTIVE,
            DownloadAttemptStatus.REPLACEMENT_PENDING,
            DownloadAttemptStatus.COMPLETED,
        ):
            return False
        rows = await upgrade_attempt_wanted_rows(session, old)
        return any((row.season_number, row.episode_number) in allowed for row in rows)
    rows = await _attempt_wanted_rows(session, old)
    return any((row.season_number, row.episode_number) in allowed for row in rows)


async def _rescue_group(
    subscription_id: int,
    info_hash: str,
    downloaders: list[tuple[DownloaderClient, DownloaderConfig]],
) -> None:
    """兼容旧调用入口：完成任务先核验内容，其余走统一心跳状态机。

    定时巡检直接观察持久化尝试；这个入口仍供旧调用方和回归测试使用。
    先经 ``_query_torrent`` 查询可保留旧桩接口，实际实现仍复用
    ``_lookup_torrent``，不会形成第二套下载器语义。
    """
    found = await _query_torrent(info_hash, downloaders)
    db = get_database()
    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(WantedItem).where(
                        WantedItem.subscription_id == subscription_id,
                        WantedItem.info_hash == info_hash,
                        WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                        WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return
        if found is not None and found[1].completed:
            downloader, status = found
            await _verify_completed_download(
                session,
                rows,
                info_hash,
                downloader,
                status,
            )
            return
        await _ensure_attempts(session, {(subscription_id, info_hash): rows})
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == subscription_id,
                    SubscriptionDownloadAttempt.info_hash == info_hash.lower(),
                )
            )
        ).scalar_one()
        assert attempt.id is not None
        attempt_id = attempt.id
    should_search = await _observe_attempt(attempt_id, downloaders)
    if should_search:
        from movieclaw_api.services.subscription import run_replacement_search
        await run_replacement_search(attempt_id)


# 落点核验宽限期：完成后给下载器归位文件/网络盘可见性留出的窗口
_LANDING_GRACE_MINUTES = 10
_MISSING_RETRY_MINUTES = 30


async def _reconcile_orphaned_group(
    subscription_id: int,
    info_hash: str,
    downloaders: list[tuple[DownloaderClient, DownloaderConfig]],
) -> None:
    """孤儿在途工单的完成对账：attempt 终态、工单还挂着的组。

    洗版的混合投递会在种子仍在下载时就被验证收口——分批入库先交付了洗版
    目标集，attempt 置 imported、离开心跳照看集合；种子几小时后才下载完成，
    落点/内容核验永远轮不到它，包里根本不存在的集（覆盖虚标）就以 grabbed
    永久挂起（真实案例：十三邀 S06 包声明整季、实际只有 E00–E10，E11 工单
    悬挂一天无人退回）。

    只处理证据充分的一种情况：种子在下载器里且已完成 → 补跑与活跃路径
    完全相同的落点/内容核验（缺失单元退回重找 + 写负面记忆，存在的单元
    照常等库存对账关单）。种子不在或未完成一律不动——删种救援、死种换源
    等语义归活跃尝试的心跳状态机，这里证据不足不猜。
    """
    found = await _query_torrent(info_hash, downloaders)
    if found is None or not found[1].completed:
        return
    downloader, status = found
    db = get_database()
    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(WantedItem).where(
                        WantedItem.subscription_id == subscription_id,
                        WantedItem.info_hash == info_hash,
                        WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                        WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return
        await _verify_completed_download(session, rows, info_hash, downloader, status)


async def _verify_completed_download(
    session: AsyncSession,
    rows: list[WantedItem],
    info_hash: str,
    downloader: DownloaderClient,
    status: TorrentStatus,
) -> None:
    """完成任务先确认落点，再以物理文件清单核验承诺的季集范围。"""
    repo = SubscriptionRepository(session)
    landed = await _verify_landing(session, repo, rows, info_hash, downloader, status)
    if not landed:
        return
    item = await session.get(MediaItem, rows[0].media_item_id)
    if item is not None:
        await _verify_content(session, repo, item, rows, info_hash, status)


async def _verify_landing(
    session: AsyncSession,
    repo: SubscriptionRepository,
    rows: list[WantedItem],
    info_hash: str,
    downloader: DownloaderClient,
    status,
) -> bool:
    """核验已完成种子的落点：movieclaw 侧看不到内容则记告警活动（去重）。

    判定：下载器上报的实际保存目录反向过路径映射翻译回 movieclaw 视角，
    在本地 stat 种子内容根（首个文件的顶层段，或任务名）。看不到就说明
    文件落在 movieclaw 不可达的位置——映射缺失/卷未挂载/被人工移动，
    监听导入和库扫描永远等不到它，必须把问题亮到时间线上。
    不退回重找：数据真实存在，重找只会重复下载到同一个黑洞。

    返回落点是否可见——只有确认可见（True）才有资格进入后续的内容核验；
    宽限期内或证据不全一律返回 False（本轮不判，不代表落点有问题）。
    """
    from movieclaw_api.services.torrent_submit import translate_to_local

    # 宽限期内不判：刚完成的种子可能还在归位（qB 临时目录搬移等）
    threshold = utcnow() - timedelta(minutes=_LANDING_GRACE_MINUTES)
    if any((w.grabbed_at or w.updated_at) > threshold for w in rows):
        return False

    local_dir = translate_to_local(status.save_path, downloader.path_mappings)
    root = status.files[0].path.split("/")[0] if status.files else status.name
    if not local_dir or not root:
        return False
    if (Path(local_dir) / root).exists():
        # 落点可见，等监听导入/库扫描接管即可；此前若报过"看不到"（映射
        # 刚修好/卷刚挂上），问题已消失，红灯就地熄灭
        await resolve_notices(
            session,
            dedupe_key=f"subscription.landing:{rows[0].subscription_id}:{info_hash}",
        )
        return True

    message = (
        f"「{status.name}」已下载完成，但 movieclaw 在 {local_dir} 看不到它——"
        f"下载器「{downloader.name}」可能无法访问该路径（路径映射缺失或卷未挂载），"
        "或文件已在下载器中被移动。请检查「设置 → 下载器」的路径映射，"
        "或改用监听导入规则后把文件移入监听目录"
    )
    # 全局红灯（幂等 upsert，问题在就常亮）：时间线活动埋在单个订阅详情页里，
    # 这类"不修就永远卡着"的错误必须在任何页面都能被感受到
    await upsert_notice(
        session,
        dedupe_key=f"subscription.landing:{rows[0].subscription_id}:{info_hash}",
        severity=NoticeSeverity.ERROR,
        source="subscription",
        title=f"「{status.name}」下载完成但无法入库",
        message=message,
        payload={"subscription_id": rows[0].subscription_id, "info_hash": info_hash},
    )

    # 去重：同一（订阅, 种子）只告警一次，避免每 5 分钟刷屏
    existing = (
        (
            await session.execute(
                select(SubscriptionActivity).where(
                    SubscriptionActivity.subscription_id == rows[0].subscription_id,
                    SubscriptionActivity.type == ActivityType.IMPORT_FAILED,
                )
            )
        )
        .scalars()
        .all()
    )
    for activity in existing:
        payload = activity.payload or {}
        if payload.get("info_hash") == info_hash and payload.get("reason") == "path_unreachable":
            return False

    await repo.add_activity(
        SubscriptionActivity(
            subscription_id=rows[0].subscription_id,
            wanted_item_id=rows[0].id,
            type=ActivityType.IMPORT_FAILED,
            message=message,
            payload={
                "info_hash": info_hash,
                "reason": "path_unreachable",
                "downloader_save_path": status.save_path,
                "local_dir": local_dir,
            },
        )
    )
    logger.warning(
        "落点核验失败：《种子 %s》完成于 %s（下载器视角 %s），movieclaw 侧不可见",
        info_hash,
        local_dir,
        status.save_path,
    )
    return False


# 种子内文件的季集声明：场景命名 SxxEyy。第二组抓整段集号串——连写
# （E05E06）与区间简写（E05-E08）都收进来，展开规则见 _observed_units。
# 裸数字段必须有分隔符引导（防止把 E05.1080p 的技术串吞进集号）
_FILE_UNIT_RE = re.compile(r"[Ss](\d{1,2})((?:[Ee]\d{1,4})(?:(?:\s*[-~&+]\s*[Ee]?|[Ee])\d{1,4})*)")
_FILE_EP_RE = re.compile(r"\d{1,4}")


def _observed_units(files) -> set[tuple[int, int]]:
    """种子文件清单里能认出的 (季, 集) 集合。

    只认场景命名的 SxxEyy 高置信声明；恰好两个递增集号视为区间展开
    （E05-E08 → 5..8，连写 E05E06 展开结果与列举一致），其余按列举。
    季集识别的完整口径在库扫描侧（NER 模型），这里刻意不复用——救援
    巡检要在无模型环境同样工作，而轻量正则的漏认只会让判定更保守
    （认不出 → 不动），不会造成误退。
    """
    observed: set[tuple[int, int]] = set()
    for file in files:
        for match in _FILE_UNIT_RE.finditer(file.path):
            season = int(match.group(1))
            numbers = [int(n) for n in _FILE_EP_RE.findall(match.group(2))]
            if len(numbers) == 2 and numbers[0] < numbers[1] <= numbers[0] + 200:
                numbers = list(range(numbers[0], numbers[1] + 1))
            observed.update((season, episode) for episode in numbers)
    return observed


async def _verify_content(
    session: AsyncSession,
    repo: SubscriptionRepository,
    item: MediaItem,
    rows: list[WantedItem],
    info_hash: str,
    status: TorrentStatus,
) -> None:
    """核验已完成种子的内容：承诺的集数不在文件清单里 → 缺失部分退回重找。

    库存对账只认领真实存在的文件——种子里根本没有的集，扫多少轮都不会
    入库，工单会以 grabbed 永远挂着，任务中心那条"等待入库"永不消失
    （真实案例：全集包被判定覆盖特别篇 7 集，62 个文件全是正剧）。

    判定刻意保守，证据不足一律不动：
    - 电影不判（单元没有集号语义）；
    - 文件清单为空（旧适配器不上报）不判；
    - 清单里一个集数都认不出（原盘目录/非常规命名）不判——只有清单
      "明确认得出集数、且明确没有这一集"时才断言缺失；
    - 只退回缺失的工单，清单里存在的集数继续等对账正常关单。
    """
    if MediaKind(item.kind) is MediaKind.MOVIE:
        return
    if not status.files:
        return
    observed = _observed_units(status.files)
    if not observed:
        return
    missing = [w for w in rows if (w.season_number, w.episode_number) not in observed]
    if not missing:
        return
    await _requeue(
        session,
        repo,
        item,
        missing,
        info_hash,
        message=(
            f"「{status.name}」已下载完成，但内容里没有 {units_text(missing)} 对应的"
            f"文件（声明的覆盖范围与实际内容不符），这部分退回重新寻找资源；"
            "种子内实际存在的集数不受影响，照常入库"
        ),
        reason="content_missing",
    )


async def _record_content_missing(
    session: AsyncSession,
    repo: SubscriptionRepository,
    rows: list[WantedItem],
    info_hash: str,
) -> None:
    """把"这份内容里没有这些集"写进 attempt 的负面记忆。

    只退回工单是不够的：下一轮搜索会再次选中同一个种子，而它早已躺在下载器
    里完成，于是"秒完成 → 再核验 → 再退回"无限循环（真实教训：十三邀 S04
    全集包不含 TMDB 编号为 S04E14 的夏日特别版，工单每 30 分钟空转一轮）。

    来源清单取自本 hash 的历次投递活动——同一份内容常在多站镜像，attempt 上
    的 site/torrent 只保留首次投递者，拦不住换个站点的同一发布。
    """
    attempt = (
        await session.execute(
            select(SubscriptionDownloadAttempt).where(
                SubscriptionDownloadAttempt.subscription_id == rows[0].subscription_id,
                SubscriptionDownloadAttempt.info_hash == info_hash.lower(),
            )
        )
    ).scalar_one_or_none()
    if attempt is None:  # 手动投递等无台账路径：没有可挂载记忆的行，跳过
        return
    memory = dict(attempt.content_missing or {})
    units = {
        (int(unit[0]), int(unit[1]))
        for unit in memory.get("units", [])
        if isinstance(unit, list) and len(unit) == 2
    }
    units.update((row.season_number, row.episode_number) for row in rows)
    sources = {
        (str(source[0]), str(source[1]))
        for source in memory.get("sources", [])
        if isinstance(source, list) and len(source) == 2
    }
    if attempt.site_id and attempt.torrent_id:
        sources.add((attempt.site_id, attempt.torrent_id))
    for activity in await repo.list_activities(rows[0].subscription_id, limit=200):
        payload = activity.payload or {}
        if payload.get("info_hash") != info_hash.lower():
            continue
        if payload.get("site_id") and payload.get("torrent_id"):
            sources.add((str(payload["site_id"]), str(payload["torrent_id"])))
    attempt.content_missing = {
        "units": [[season, episode] for season, episode in sorted(units)],
        "sources": [[site_id, torrent_id] for site_id, torrent_id in sorted(sources)],
    }
    session.add(attempt)


async def _requeue(
    session: AsyncSession,
    repo: SubscriptionRepository,
    item: MediaItem,
    rows: list[WantedItem],
    info_hash: str,
    *,
    message: str,
    reason: str,
) -> None:
    """只把物理内容缺失的工单退回 wanted，保留同种子中实际存在的单元。"""
    now = utcnow()
    if reason == "content_missing":
        await _record_content_missing(session, repo, rows, info_hash)
    retry_at = now + timedelta(minutes=_MISSING_RETRY_MINUTES)
    for row in rows:
        await session.execute(
            update(WantedItem)
            .where(WantedItem.id == row.id)
            .values(
                status=WantedStatus.WANTED,
                info_hash=None,
                grabbed_at=None,
                downloaded_at=None,
                next_search_at=retry_at,
                updated_at=now,
            )
        )
    await session.commit()
    await resolve_notices(
        session,
        dedupe_key=f"subscription.landing:{rows[0].subscription_id}:{info_hash}",
    )
    await repo.add_activity(
        SubscriptionActivity(
            subscription_id=rows[0].subscription_id,
            wanted_item_id=rows[0].id,
            type=ActivityType.DISPATCH_FAILED,
            message=message,
            payload={
                "info_hash": info_hash,
                "reason": reason,
                "units": [[row.season_number, row.episode_number] for row in rows],
            },
        )
    )
    logger.warning("《%s》的种子 %s 已退回缺失单元：%s", item.title, info_hash, reason)


async def subscription_download_snapshot(session: AsyncSession, subscription_id: int) -> list[dict]:
    """订阅详情页的实时下载进度快照。

    把该订阅的在途工单按种子分组，逐个到可用下载器里查当前状态（速度/ETA/
    进度），返回展示用字典列表。纯读操作：不落库、不改工单——工单的死活
    仍归上面的救援巡检管，这里只回答"此刻下到哪了"。

    种子在所有下载器中都查不到时 state="missing"（可能刚被手动删除，
    救援巡检连续三次确认后会把工单退回重找），前端据此给出解释而不是显示 0%。
    """
    result = await session.execute(
        select(WantedItem).where(
            WantedItem.subscription_id == subscription_id,
            WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
            WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            WantedItem.info_hash.is_not(None),  # type: ignore[union-attr]
        )
    )
    groups: dict[str, list[WantedItem]] = {}
    for row in result.scalars().all():
        assert row.info_hash is not None
        groups.setdefault(row.info_hash, []).append(row)
    if not groups:
        return []

    downloaders = await _usable_downloaders(session)
    snapshots: list[dict] = []
    for info_hash, rows in sorted(groups.items()):
        units = [
            {"season_number": w.season_number, "episode_number": w.episode_number}
            for w in sorted(rows, key=lambda w: (w.season_number, w.episode_number))
        ]
        # 快照只用 info 字段（进度/速度/ETA），不取文件清单——5 秒一轮的
        # 轮询没必要每次都把整包文件列表拉回来
        found = (
            await _query_torrent(info_hash, downloaders, include_files=False)
            if downloaders
            else None
        )
        if found is None:
            snapshots.append(
                {
                    "info_hash": info_hash,
                    "name": None,
                    "progress": None,
                    "size_bytes": None,
                    "dlspeed_bytes": None,
                    "eta_seconds": None,
                    "state": "missing",
                    "downloader_name": None,
                    "units": units,
                }
            )
            continue
        downloader_row, status = found
        snapshots.append(
            {
                "info_hash": info_hash,
                "name": status.name,
                "progress": status.progress,
                "size_bytes": status.size_bytes,
                "dlspeed_bytes": status.dlspeed_bytes,
                "eta_seconds": status.eta_seconds,
                "state": status.state,
                "downloader_name": downloader_row.name,
                "units": units,
            }
        )
    return snapshots
