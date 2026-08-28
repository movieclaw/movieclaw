"""订阅死种换源：真实跨站搜索、同品质试用与安全晋升。

换源不是把 ``wanted`` 简单退回队列。旧任务在新源证明能下载前仍有价值，
因此这里以 ``SubscriptionDownloadAttempt`` 保存并行事实：旧源保持
replacement_pending，新源先进入 trial；只有累计网络下载字节真实增长后才切换
wanted 的当前 infohash，并按所有权/H&R/文件重叠证据清理旧任务。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from pathlib import PurePosixPath

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.subscription.matching import (
    covered_units,
    publish_calendar_date,
    to_candidate,
    units_text,
)
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    ActivityType,
    DownloadAttemptStatus,
    DownloaderClient,
    MediaItem,
    RuleSet,
    SiteTorrent,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    SubscriptionStatus,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories import SubscriptionRepository
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader import DownloaderConfig, TorrentStatus, create_downloader
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import (
    MediaIdentity,
    RuleSetSpec,
    TorrentCandidate,
    evaluate_rules,
    match_identity,
)

logger = logging.getLogger("movieclaw_api.subscription_replacement")

REPLACEMENT_RETRY = (
    timedelta(hours=1),
    timedelta(hours=3),
    timedelta(hours=12),
    timedelta(hours=24),
)
SEARCH_FAILURE_RETRY = timedelta(minutes=15)
TRIAL_PROGRESS_BYTES = 1024 * 1024

_IN_FLIGHT = (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)
_replacement_lock = asyncio.Lock()
_RESOLUTION_RE = re.compile(r"(\d{3,4})")

# 片源序以 matcher 的统一片源档阶梯为基（quality-upgrade.md Phase 7 收编，
# 洗版与换源共用同一张表），换源侧保留两点特有语义：
# ① UHD Blu-ray 比 Blu-ray 高半档——换源的"不降级"要挡住 UHD→普通蓝光
#   （洗版语义里 UHD 由分辨率轴表达，同为 T4）；
# ② 值匹配按子串容错并带别名（存量 attempt.quality 可能存有 webdl/bluray
#   等未带连字符写法）。
# 相比旧表补全了 BDRip/HDRip/DVD 等档（此前 rank=None 被当"未知"处理）。
_SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "uhd blu-ray": ("uhd blu-ray", "uhd bluray"),
    "hd-dvd": ("hd-dvd", "hddvd"),
    "blu-ray": ("blu-ray", "bluray"),
    "web-dl": ("web-dl", "webdl"),
    "webrip": ("webrip", "web-rip"),
    "bdrip": ("bdrip",),
    "hdrip": ("hdrip",),
    "dvdrip": ("dvdrip",),
    "hdtvrip": ("hdtvrip",),
    "hdtv": ("hdtv",),
    "tvrip": ("tvrip",),
    "dvd": ("dvd",),
}


def _build_source_rank() -> dict[str, int]:
    from movieclaw_matcher import source_tier

    return {
        alias: (source_tier(canonical, False) or 0) * 10
        + (5 if canonical == "uhd blu-ray" else 0)
        for canonical, aliases in _SOURCE_ALIASES.items()
        for alias in aliases
    }


_SOURCE_RANK = _build_source_rank()


def replacement_backoff(attempts: int) -> timedelta:
    """成功搜索但无可用替代源后的专用退避；最终每天重搜一次。"""
    return REPLACEMENT_RETRY[min(attempts, len(REPLACEMENT_RETRY) - 1)]


def quality_not_lower(candidate: TorrentCandidate, old_quality: dict) -> bool:
    """候选是否达到旧源的核心画质底线。

    规则组负责用户显式要求；这里额外防止自动换源把已经选中的 2160p、Remux、
    高等级片源或 HDR/DV 静默降级。编码不存在公认单调顺序，继续交给规则组。
    """
    old = TorrentAttrs.model_validate(old_quality or {})
    new = candidate.attrs

    # 存量在途任务若无法从精确 infohash 恢复原始规格，就没有可靠的“不降级”
    # 比较基线。未知不是最低品质：宁可保留旧源继续等，也不能静默接受任意候选。
    if not any((old.resolution, old.media_source, old.remux, old.hdr)):
        return False

    def resolution(value: str | None) -> int | None:
        match = _RESOLUTION_RE.search(value or "")
        return int(match.group(1)) if match else None

    old_resolution = resolution(old.resolution)
    new_resolution = resolution(new.resolution)
    if old_resolution is not None and (new_resolution is None or new_resolution < old_resolution):
        return False
    if old.remux and not new.remux:
        return False

    old_source = _source_rank(old.media_source)
    new_source = _source_rank(new.media_source)
    if old_source is not None and (new_source is None or new_source < old_source):
        return False

    old_hdr = {value.casefold() for value in old.hdr}
    new_hdr = {value.casefold() for value in new.hdr}
    return old_hdr <= new_hdr


def _source_rank(value: str | None) -> int | None:
    normalized = (value or "").strip().casefold()
    if not normalized:
        return None
    for label, rank in sorted(_SOURCE_RANK.items(), key=lambda item: len(item[0]), reverse=True):
        if label in normalized:
            return rank
    return None


async def request_replacement(
    session: AsyncSession,
    *,
    downloader_id: int,
    info_hash: str,
) -> int:
    """用户从任务中心要求立即换源，返回将交给后台搜索的 attempt id。"""
    from movieclaw_api.exceptions import BadRequestException

    normalized = info_hash.lower()
    attempts = list(
        (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == normalized,
                    SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                        (
                            DownloadAttemptStatus.ACTIVE,
                            DownloadAttemptStatus.REPLACEMENT_PENDING,
                        )
                    ),
                )
            )
        ).scalars()
    )
    if len(attempts) > 1:
        raise BadRequestException(
            "该下载任务同时关联多个订阅，无法安全判断要替换哪一个；请先在订阅详情中处理"
        )
    attempt = attempts[0] if attempts else None
    if attempt is None or attempt.id is None:
        raise BadRequestException("该任务不是可换源的订阅主任务，或已经由其他资源接管")
    if attempt.downloader_id != downloader_id and not await _torrent_exists_on_downloader(
        session, downloader_id, normalized
    ):
        raise BadRequestException(
            "任务所在下载器已变化，且指定下载器中查不到该任务，请刷新后重试"
        )
    if utcnow() - attempt.last_progress_at < timedelta(minutes=15):
        raise BadRequestException("该任务尚未连续 15 分钟无进度，请稍后再试")
    if await _has_trial(session, attempt.id):
        raise BadRequestException("同品质替代源已经在验证中，无需重复换种")
    if not await _current_attempt_wanted(session, attempt):
        raise BadRequestException("关联单元已退出当前订阅范围，不能再发起换种")

    now = utcnow()
    # 任务中心传入的是本次实时快照实际命中的下载器；若用户迁移过任务，
    # 以这条新事实修正投递台账，后续观察和安全清理都优先查正确目标。
    result = await session.execute(
        update(SubscriptionDownloadAttempt)
        .where(
            SubscriptionDownloadAttempt.id == attempt.id,
            SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                (
                    DownloadAttemptStatus.ACTIVE,
                    DownloadAttemptStatus.REPLACEMENT_PENDING,
                )
            ),
        )
        .values(
            downloader_id=downloader_id,
            status=DownloadAttemptStatus.REPLACEMENT_PENDING,
            next_search_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    if not result.rowcount:
        raise BadRequestException("任务状态已经变化，请刷新活动页后重试")
    await session.refresh(attempt)
    await SubscriptionRepository(session).add_activity(
        SubscriptionActivity(
            subscription_id=attempt.subscription_id,
            type=ActivityType.DOWNLOAD_STALLED,
            message="用户选择立即换种，正在跨站寻找同品质或更高品质的替代源",
            payload={"info_hash": normalized, "reason": "manual_replacement"},
        )
    )
    return attempt.id


async def _torrent_exists_on_downloader(
    session: AsyncSession, downloader_id: int, info_hash: str
) -> bool:
    """验证任务中心上报的新下载器归属，防止路径参数篡改污染清理台账。"""
    downloader = await session.get(DownloaderClient, downloader_id)
    if downloader is None:
        return False
    repo = DownloaderRepository(session)
    adapter = create_downloader(
        DownloaderConfig(
            type=downloader.client_type.value,
            url=downloader.url,
            username=downloader.username,
            password=repo.decrypted_password(downloader),
        )
    )
    try:
        return await adapter.get_torrent(info_hash, include_files=False) is not None
    except Exception as exc:  # noqa: BLE001 -- 无法验证时拒绝改写归属，保留旧事实
        logger.warning("验证换源任务所在下载器失败：downloader=%s error=%s", downloader_id, exc)
        return False
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001 -- 关闭失败不改变验证结论
            logger.warning("关闭换源归属验证连接失败", exc_info=True)


async def run_replacement_search(attempt_id: int, *, force: bool = False) -> bool:
    """为一个无进度主源执行真实跨站搜索；找到并投递试用源时返回 True。"""
    async with _replacement_lock:
        db = get_database()
        async with db.session() as session:
            attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
            if attempt is None or attempt.status not in (
                DownloadAttemptStatus.ACTIVE,
                DownloadAttemptStatus.REPLACEMENT_PENDING,
            ):
                return False
            now = utcnow()
            if not force and attempt.next_search_at is not None and attempt.next_search_at > now:
                return False
            if await _has_trial(session, attempt.id):
                return False
            subscription = await session.get(Subscription, attempt.subscription_id)
            # paused 才拦：洗版源换源发生在已收齐（completed）的订阅上
            if subscription is None or subscription.status == SubscriptionStatus.PAUSED:
                return False
            item = await session.get(MediaItem, subscription.media_item_id)
            if item is None:
                return False
            if not await _current_attempt_wanted(session, attempt):
                out_of_scope = await _has_out_of_scope_attempt_wanted(session, attempt)
                attempt.status = (
                    DownloadAttemptStatus.CANCELLED
                    if out_of_scope
                    else DownloadAttemptStatus.IMPORTED
                )
                attempt.cleanup_note = (
                    "关联单元已退出当前订阅范围；保留下载器任务，不再执行换源"
                    if out_of_scope
                    else "关联工单已完成入库，不再执行死种换源"
                )
                attempt.next_search_at = None
                attempt.updated_at = now
                session.add(attempt)
                await session.commit()
                return False
            item_kind = item.kind
            keywords = list(dict.fromkeys(filter(None, (item.original_title, item.title))))
            # 网络调用前先排一个技术失败兜底，进程中途退出也不会每个 tick 重打站点。
            attempt.status = DownloadAttemptStatus.REPLACEMENT_PENDING
            attempt.last_search_at = now
            attempt.next_search_at = now + SEARCH_FAILURE_RETRY
            attempt.updated_at = now
            session.add(attempt)
            await session.commit()

        from movieclaw_api.services.site_search import search_all_sites
        from movieclaw_api.services.subscription.wanted_search import (
            _SEARCH_CATEGORIES,
            _persist_hits,
        )

        hits_by_key = {}
        sites_ok = 0
        errors: list[str] = []
        for keyword in keywords:
            try:
                response = await search_all_sites(
                    keyword,
                    categories=_SEARCH_CATEGORIES.get(item_kind),
                    # 换源同属订阅链路：受保护站点不参与自动拉种
                    exclude_protected=True,
                )
            except Exception as exc:  # noqa: BLE001 -- 全局搜索故障按技术失败短重试
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            sites_ok = max(sites_ok, sum(1 for site in response.sites if site.error is None))
            errors.extend(str(site.error) for site in response.sites if site.error)
            for hit in response.items:
                hits_by_key[(hit.site_id, hit.torrent_id)] = hit

        async with db.session() as session:
            attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
            if attempt is None or attempt.status != DownloadAttemptStatus.REPLACEMENT_PENDING:
                return False
            if not await _current_attempt_wanted(session, attempt):
                out_of_scope = await _has_out_of_scope_attempt_wanted(session, attempt)
                attempt.status = (
                    DownloadAttemptStatus.CANCELLED
                    if out_of_scope
                    else DownloadAttemptStatus.IMPORTED
                )
                attempt.next_search_at = None
                attempt.cleanup_note = (
                    "跨站搜索返回前关联单元已退出订阅范围；停止换源"
                    if out_of_scope
                    else "关联工单已完成入库，不再执行死种换源"
                )
                attempt.updated_at = utcnow()
                session.add(attempt)
                await session.commit()
                return False
            if sites_ok == 0:
                now = utcnow()
                result = await session.execute(
                    update(SubscriptionDownloadAttempt)
                    .where(
                        SubscriptionDownloadAttempt.id == attempt.id,
                        SubscriptionDownloadAttempt.status
                        == DownloadAttemptStatus.REPLACEMENT_PENDING,
                    )
                    .values(next_search_at=now + SEARCH_FAILURE_RETRY, updated_at=now)
                )
                await session.commit()
                if not result.rowcount:
                    return False
                await SubscriptionRepository(session).add_activity(
                    SubscriptionActivity(
                        subscription_id=attempt.subscription_id,
                        type=ActivityType.REPLACEMENT_SEARCHED,
                        message="替代源搜索暂时无法执行，站点连接恢复后将自动重试",
                        payload={"failed": True, "errors": errors[:5]},
                    )
                )
                return False

            rows = await _persist_hits(session, list(hits_by_key.values()))
            if await _try_candidates(session, attempt, rows, source="死种主动换源"):
                return True
            await _postpone_after_empty_search(session, attempt, result_count=len(rows))
            return False


async def try_replacement_candidates(session: AsyncSession, torrents: list[SiteTorrent]) -> int:
    """新种进入索引时给等待换源的尝试抢跑；不推进主动搜索退避。"""
    async with _replacement_lock:
        return await _try_passive_replacement_candidates(session, torrents)


async def _try_passive_replacement_candidates(
    session: AsyncSession,
    torrents: list[SiteTorrent],
) -> int:
    """锁内执行被动候选评估，避免与主动搜索同时投递两个试用源。"""
    attempts = list(
        (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.status
                    == DownloadAttemptStatus.REPLACEMENT_PENDING
                )
            )
        )
        .scalars()
        .all()
    )
    submitted = 0
    for attempt in attempts:
        if attempt.id is None or await _has_trial(session, attempt.id):
            continue
        if await _try_candidates(session, attempt, torrents, source="新种被动换源"):
            submitted += 1
    return submitted


async def _try_candidates(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
    torrents: list[SiteTorrent],
    *,
    source: str,
) -> bool:
    """评估一批候选并投递首个实际不同的同品质试用源。"""
    subscription = await session.get(Subscription, attempt.subscription_id)
    # paused 才拦：洗版源换源发生在已收齐（completed）的订阅上
    if subscription is None or subscription.status == SubscriptionStatus.PAUSED:
        return False
    item = await session.get(MediaItem, subscription.media_item_id)
    rule = await session.get(RuleSet, subscription.rule_set_id)
    if item is None or rule is None:
        return False
    spec = RuleSetSpec.model_validate(rule.spec)
    targets = await _current_attempt_wanted(session, attempt)
    if not targets:
        return False
    open_units = {(row.season_number, row.episode_number): row for row in targets}
    identity = MediaIdentity(
        kind=item.kind,
        year=item.year,
        aliases=tuple(item.aliases),
        imdb_id=item.imdb_id,
        douban_id=item.douban_id,
        season_numbers=tuple(sorted({season for season, _episode in open_units})),
    )
    excluded = await _excluded_candidates(session, attempt.subscription_id)
    accepted = []
    for row in torrents:
        candidate = to_candidate(row)
        if candidate is None:
            continue
        if (candidate.site_id, candidate.torrent_id) in excluded:
            continue
        if candidate.seeders == 0:
            continue
        match = match_identity(candidate, identity)
        if match is None:
            continue
        covered = covered_units(
            match,
            open_units,
            published=publish_calendar_date(candidate.publish_time),
        )
        if not covered:
            continue
        verdict = evaluate_rules(candidate, spec, pack_episode_count=len(covered))
        if not verdict.accepted or not quality_not_lower(candidate, attempt.quality):
            continue
        accepted.append((candidate, covered, verdict))
    accepted.sort(
        key=lambda entry: (
            entry[0].seeders is not None,
            entry[0].seeders or 0,
            len(entry[1]),
            entry[2].score,
        ),
        reverse=True,
    )
    if not accepted:
        return False

    from movieclaw_api.services.library.config import LibraryConfigService
    from movieclaw_api.services.library.routing import resolve_save_path
    from movieclaw_api.services.torrent_submit import submit_torrent

    library = await LibraryConfigService(session).resolve_for_subscription(
        subscription.library_id,
        subscription.kind,
        item=item,
    )
    decision = await resolve_save_path(
        session,
        library,
        kind=subscription.kind,
        title=item.title,
        year=item.year,
    )
    existing_hashes = set(
        (
            await session.execute(
                select(SubscriptionDownloadAttempt.info_hash).where(
                    SubscriptionDownloadAttempt.subscription_id == attempt.subscription_id
                )
            )
        ).scalars()
    )
    for candidate, covered, _verdict in accepted:
        try:
            result, downloader = await submit_torrent(
                session,
                site_id=candidate.site_id,
                download_url=candidate.download_url,
                tags=["movieclaw-sub", "movieclaw-replacement"],
                save_path=decision.path,
                subtitle=candidate.subtitle if decision.entry_level else None,
            )
        except Exception as exc:  # noqa: BLE001 -- 单个候选失败继续尝试下一名
            logger.warning(
                "替代源投递失败，继续尝试下一候选：%s/%s：%s",
                candidate.site_id,
                candidate.torrent_id,
                exc,
            )
            continue
        if not result.info_hash:
            continue
        normalized_hash = result.info_hash.lower()
        if normalized_hash == attempt.info_hash or normalized_hash in existing_hashes:
            logger.info("替代候选实际 infohash 已尝试过，跳过：%s", normalized_hash)
            if not result.already_exists and candidate.hit_and_run is False:
                await _remove_just_submitted_task(session, downloader, normalized_hash)
            continue
        # 真实网络提交期间订阅范围可能被用户调整。重新读取尝试与工单，只给
        # 仍在范围内的单元建立试用台账；可安全撤销的新任务顺手回收。
        await session.refresh(attempt)
        current_attempt = attempt
        if (
            current_attempt is None
            or current_attempt.status != DownloadAttemptStatus.REPLACEMENT_PENDING
        ):
            if not result.already_exists and candidate.hit_and_run is False:
                await _remove_just_submitted_task(session, downloader, normalized_hash)
            return False
        current_targets = await _current_attempt_wanted(session, current_attempt)
        current_ids = {row.id for row in current_targets}
        covered = [row for row in covered if row.id in current_ids]
        if not covered:
            if not result.already_exists and candidate.hit_and_run is False:
                await _remove_just_submitted_task(session, downloader, normalized_hash)
            return False
        attempt = current_attempt
        now = utcnow()
        trial = SubscriptionDownloadAttempt(
            subscription_id=attempt.subscription_id,
            downloader_id=downloader.id,
            replaces_attempt_id=attempt.id,
            info_hash=normalized_hash,
            site_id=candidate.site_id,
            torrent_id=candidate.torrent_id,
            torrent_title=candidate.title,
            download_name=result.name or None,
            save_path=decision.path,
            units=[[row.season_number, row.episode_number] for row in covered],
            quality=candidate.attrs.model_dump(exclude_defaults=True),
            hit_and_run=candidate.hit_and_run,
            owned_by_movieclaw=not result.already_exists,
            # 试用源继承被替换源的投递目的：洗版源的替代仍是洗版投递，
            # 晋升后入库验证与在途去重才能正确认领它
            purpose=attempt.purpose,
            status=DownloadAttemptStatus.TRIAL,
            baseline_completed_bytes=0 if not result.already_exists else None,
            last_completed_bytes=0 if not result.already_exists else None,
            baseline_downloaded_bytes=0 if not result.already_exists else None,
            last_downloaded_bytes=0 if not result.already_exists else None,
            last_progress_at=now,
        )
        session.add(trial)
        attempt.next_search_at = None
        attempt.updated_at = now
        session.add(attempt)
        await session.commit()
        await SubscriptionRepository(session).add_activity(
            SubscriptionActivity(
                subscription_id=attempt.subscription_id,
                wanted_item_id=covered[0].id,
                type=ActivityType.REPLACEMENT_TRIAL,
                message=(
                    f"已找到同品质替代源并开始验证{units_text(covered)}："
                    f"来自 {candidate.site_id} 的「{candidate.title[:60]}」；"
                    "新源产生真实下载进度后才会切换并清理旧源"
                ),
                payload={
                    "old_info_hash": attempt.info_hash,
                    "new_info_hash": normalized_hash,
                    "site_id": candidate.site_id,
                    "torrent_id": candidate.torrent_id,
                    "source": source,
                    "units": trial.units,
                },
            )
        )
        return True
    return False


async def _remove_just_submitted_task(
    session: AsyncSession,
    downloader: DownloaderClient,
    info_hash: str,
) -> None:
    """跨站同 hash 在取种后才能识别；安全移除刚创建的重复任务但保留数据。"""
    repo = DownloaderRepository(session)
    adapter = create_downloader(
        DownloaderConfig(
            type=downloader.client_type.value,
            url=downloader.url,
            username=downloader.username,
            password=repo.decrypted_password(downloader),
        )
    )
    try:
        await adapter.delete_torrent(info_hash, delete_files=False)
    except Exception as exc:  # noqa: BLE001 -- 重复任务清理失败只记录，不改变候选排除
        logger.warning("刚投递的重复替代源任务清理失败：hash=%s error=%s", info_hash, exc)
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001 -- 关闭失败不覆盖清理结果
            logger.warning("关闭重复替代源下载器连接失败", exc_info=True)


async def _current_attempt_wanted(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
) -> list[WantedItem]:
    allowed = {
        (int(unit[0]), int(unit[1]))
        for unit in attempt.units
        if isinstance(unit, list) and len(unit) == 2
    }
    if attempt.purpose == "upgrade":
        # 洗版 attempt 的目标工单是 imported 行（工单不重开、info_hash 指向
        # 旧版本），缺口语义的 hash 关联恒空——不分流的话死掉的洗版源
        # 永远搜得到却投不出替代源
        from movieclaw_api.services.subscription.upgrade import upgrade_attempt_wanted_rows

        return await upgrade_attempt_wanted_rows(session, attempt)
    rows = list(
        (
            await session.execute(
                select(WantedItem)
                .where(
                    WantedItem.subscription_id == attempt.subscription_id,
                    WantedItem.info_hash == attempt.info_hash,
                    WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return [row for row in rows if (row.season_number, row.episode_number) in allowed]


async def _has_out_of_scope_attempt_wanted(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
    *,
    allowed_units: set[tuple[int, int]] | None = None,
) -> bool:
    """判断尝试无活动目标是因退出订阅范围，而不是因为已经完成入库。"""
    allowed = (
        allowed_units
        if allowed_units is not None
        else {
            (int(unit[0]), int(unit[1]))
            for unit in attempt.units
            if isinstance(unit, list) and len(unit) == 2
        }
    )
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == attempt.subscription_id,
                    WantedItem.info_hash == attempt.info_hash,
                    WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                    WantedItem.in_scope.is_(False),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    return any((row.season_number, row.episode_number) in allowed for row in rows)


async def _units_have_left_scope(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
    *,
    allowed_units: set[tuple[int, int]] | None = None,
) -> bool:
    """仅在目标仍是“在途但出域”时返回 True；已入库不冒充取消订阅。"""
    allowed = (
        allowed_units
        if allowed_units is not None
        else {
            (int(unit[0]), int(unit[1]))
            for unit in attempt.units
            if isinstance(unit, list) and len(unit) == 2
        }
    )
    active = await _current_attempt_wanted(session, attempt)
    if any((row.season_number, row.episode_number) in allowed for row in active):
        return False
    return await _has_out_of_scope_attempt_wanted(
        session, attempt, allowed_units=allowed
    )


async def _excluded_candidates(
    session: AsyncSession,
    subscription_id: int,
) -> set[tuple[str, str]]:
    rows = list(
        (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == subscription_id,
                    SubscriptionDownloadAttempt.status != DownloadAttemptStatus.CANCELLED,
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        (row.site_id, row.torrent_id)
        for row in rows
        if row.site_id is not None and row.torrent_id is not None
    }


async def _has_trial(session: AsyncSession, attempt_id: int) -> bool:
    return (
        await session.execute(
            select(SubscriptionDownloadAttempt.id).where(
                SubscriptionDownloadAttempt.replaces_attempt_id == attempt_id,
                SubscriptionDownloadAttempt.status == DownloadAttemptStatus.TRIAL,
            )
        )
    ).first() is not None


async def _postpone_after_empty_search(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
    *,
    result_count: int,
) -> None:
    await session.refresh(attempt)
    if attempt.status != DownloadAttemptStatus.REPLACEMENT_PENDING:
        return
    if not await _current_attempt_wanted(session, attempt):
        out_of_scope = await _has_out_of_scope_attempt_wanted(session, attempt)
        attempt.status = (
            DownloadAttemptStatus.CANCELLED
            if out_of_scope
            else DownloadAttemptStatus.IMPORTED
        )
        attempt.next_search_at = None
        attempt.cleanup_note = (
            "关联单元已退出当前订阅范围；停止换源退避"
            if out_of_scope
            else "关联工单已完成入库，不再执行死种换源"
        )
        attempt.updated_at = utcnow()
        session.add(attempt)
        await session.commit()
        return
    now = utcnow()
    delay = replacement_backoff(attempt.search_attempts)
    assert attempt.id is not None
    result = await session.execute(
        update(SubscriptionDownloadAttempt)
        .where(
            SubscriptionDownloadAttempt.id == attempt.id,
            SubscriptionDownloadAttempt.status == DownloadAttemptStatus.REPLACEMENT_PENDING,
        )
        .values(
            search_attempts=attempt.search_attempts + 1,
            last_search_at=now,
            next_search_at=now + delay,
            updated_at=now,
        )
    )
    await session.commit()
    if not result.rowcount:
        return
    await session.refresh(attempt)
    await SubscriptionRepository(session).add_activity(
        SubscriptionActivity(
            subscription_id=attempt.subscription_id,
            type=ActivityType.REPLACEMENT_SEARCHED,
            message=(
                f"跨站搜索返回 {result_count} 个资源，但没有不同且同品质的替代源；"
                f"旧任务继续保留，约 {int(delay.total_seconds() // 3600)} 小时后再试"
            ),
            payload={
                "results": result_count,
                "search_attempts": attempt.search_attempts,
                "next_search_at": attempt.next_search_at.isoformat(),
            },
        )
    )


async def promote_trial(
    session: AsyncSession,
    trial: SubscriptionDownloadAttempt,
    status: TorrentStatus,
) -> bool:
    """试用源已证明有真实进度：切换覆盖工单，并在安全条件下清理旧任务。"""
    if trial.replaces_attempt_id is None or trial.id is None:
        return False
    await session.refresh(trial)
    if trial.status != DownloadAttemptStatus.TRIAL:
        return False
    old = await session.get(SubscriptionDownloadAttempt, trial.replaces_attempt_id)
    if old is not None:
        await session.refresh(old)
    if old is None or old.status not in (
        DownloadAttemptStatus.ACTIVE,
        DownloadAttemptStatus.REPLACEMENT_PENDING,
    ):
        return False
    allowed = {
        (int(unit[0]), int(unit[1]))
        for unit in trial.units
        if isinstance(unit, list) and len(unit) == 2
    }
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == old.subscription_id,
                    WantedItem.info_hash == old.info_hash,
                    WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    candidates = [row for row in rows if (row.season_number, row.episode_number) in allowed]
    if not candidates:
        out_of_scope = await _has_out_of_scope_attempt_wanted(
            session, old, allowed_units=allowed
        )
        trial.status = (
            DownloadAttemptStatus.CANCELLED
            if out_of_scope
            else DownloadAttemptStatus.SUPERSEDED
        )
        trial.cleanup_note = (
            "关联单元已退出当前订阅范围；保留试用任务，不再晋升或清理旧源"
            if out_of_scope
            else "原工单已被入库或其他并发流程满足"
        )
        if out_of_scope:
            old.status = DownloadAttemptStatus.CANCELLED
            old.next_search_at = None
            old.cleanup_note = "关联单元已退出当前订阅范围；保留旧下载器任务"
            old.updated_at = utcnow()
            session.add(old)
        trial.updated_at = utcnow()
        session.add(trial)
        await session.commit()
        return False
    now = utcnow()
    switched: list[WantedItem] = []
    for row in candidates:
        result = await session.execute(
            update(WantedItem)
            .where(
                WantedItem.id == row.id,
                WantedItem.info_hash == old.info_hash,
                WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            )
            .values(info_hash=trial.info_hash, grabbed_at=trial.created_at, updated_at=now)
        )
        if result.rowcount:
            switched.append(row)
    if not switched:
        # 初次读取与条件更新之间可能刚好发生取消订阅/库存认领。只有真正
        # 改写成功的工单才能驱动试用源晋升与旧任务清理。
        out_of_scope = await _has_out_of_scope_attempt_wanted(
            session, old, allowed_units=allowed
        )
        trial.status = (
            DownloadAttemptStatus.CANCELLED
            if out_of_scope
            else DownloadAttemptStatus.SUPERSEDED
        )
        trial.cleanup_note = (
            "晋升前关联单元已退出订阅范围；保留新旧下载器任务"
            if out_of_scope
            else "晋升前原工单已被其他并发流程满足"
        )
        if out_of_scope:
            old.status = DownloadAttemptStatus.CANCELLED
            old.next_search_at = None
            old.cleanup_note = "关联单元已退出当前订阅范围；保留旧下载器任务"
            old.updated_at = utcnow()
            session.add(old)
        trial.updated_at = utcnow()
        session.add(trial)
        await session.commit()
        return False
    trial.status = DownloadAttemptStatus.ACTIVE
    trial.last_downloader_state = status.state
    trial.last_observed_at = now
    trial.last_completed_bytes = status.completed_bytes
    trial.last_downloaded_bytes = status.downloaded_bytes
    trial.last_progress_at = now
    trial.updated_at = now
    session.add(trial)

    remaining = (
        await session.execute(
            select(WantedItem.id).where(
                WantedItem.subscription_id == old.subscription_id,
                WantedItem.info_hash == old.info_hash,
                WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            )
        )
    ).first()
    if remaining is None:
        # 工单切换与“待清理”必须在同一事务提交。外部下载器删除无法参与数据库
        # 事务，进程若在下一行退出，巡检会凭该状态幂等续做，不会留下幽灵旧任务。
        old.status = DownloadAttemptStatus.CLEANUP_PENDING
        old.next_search_at = None
        old.cleanup_note = "替代源已接管，等待清理旧任务"
        old.updated_at = now
        session.add(old)
    else:
        # 一个替代源可能只覆盖整包中的部分单元；剩余单元继续保留旧源并在
        # 下一轮立即寻找别的同品质候选，不能因本次局部晋升永久停住。
        old.next_search_at = utcnow()
        old.updated_at = utcnow()
        session.add(old)
    # 换源事实和工单指向/清理状态同事务落库；即使随后进程退出，时间线也不
    # 会缺失一次已经生效的自动切换。
    session.add(
        SubscriptionActivity(
            subscription_id=old.subscription_id,
            wanted_item_id=switched[0].id,
            type=ActivityType.REPLACEMENT_PROMOTED,
            message=(
                f"替代源已产生真实下载进度，已自动换种{units_text(switched)}："
                f"「{trial.torrent_title[:60]}」"
            ),
            payload={
                "old_info_hash": old.info_hash,
                "new_info_hash": trial.info_hash,
                "units": trial.units,
            },
        )
    )
    await session.commit()
    if remaining is None:
        await _cleanup_replaced_attempt(session, old, trial, status)
    return True


async def reconcile_pending_cleanup(attempt_id: int) -> bool:
    """恢复一次被进程退出/网络故障打断的旧任务清理。

    新源状态必须仍可从其下载器读取，才允许继续比较文件重叠并删除旧源；证据
    不足时保持 cleanup_pending，下一轮巡检重试，绝不为了收敛状态冒险删数据。
    """
    db = get_database()
    async with db.session() as session:
        old = await session.get(SubscriptionDownloadAttempt, attempt_id)
        if old is None or old.status != DownloadAttemptStatus.CLEANUP_PENDING:
            return False
        new = (
            await session.execute(
                select(SubscriptionDownloadAttempt)
                .where(
                    SubscriptionDownloadAttempt.replaces_attempt_id == old.id,
                    SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                        (DownloadAttemptStatus.ACTIVE, DownloadAttemptStatus.COMPLETED)
                    ),
                )
                .order_by(SubscriptionDownloadAttempt.id.desc())  # type: ignore[attr-defined]
            )
        ).scalars().first()
        if new is None or new.downloader_id is None:
            logger.warning("旧下载尝试 #%s 等待清理，但找不到仍有效的替代源台账", attempt_id)
            return False
        downloader = await session.get(DownloaderClient, new.downloader_id)
        if downloader is None:
            logger.warning("旧下载尝试 #%s 等待清理，但替代源下载器配置已不存在", attempt_id)
            return False
        repo = DownloaderRepository(session)
        adapter = create_downloader(
            DownloaderConfig(
                type=downloader.client_type.value,
                url=downloader.url,
                username=downloader.username,
                password=repo.decrypted_password(downloader),
            )
        )
        try:
            new_status = await adapter.get_torrent(new.info_hash)
        except Exception as exc:  # noqa: BLE001 -- 暂态故障留待下轮重试
            logger.warning("恢复旧任务清理时无法读取替代源：attempt=%s error=%s", attempt_id, exc)
            return False
        finally:
            try:
                await adapter.close()
            except Exception:  # noqa: BLE001 -- 关闭失败不改变状态证据
                logger.warning("关闭替代源下载器连接失败", exc_info=True)
        if new_status is None:
            logger.warning("旧下载尝试 #%s 等待清理，但替代源暂未出现在下载器中", attempt_id)
            return False
        await _cleanup_replaced_attempt(session, old, new, new_status)
        return old.status in (
            DownloadAttemptStatus.SUPERSEDED,
            DownloadAttemptStatus.RETAINED,
        )


async def fail_trial(
    session: AsyncSession,
    trial: SubscriptionDownloadAttempt,
    *,
    reason: str,
) -> None:
    """试用源也没有进度：加入失败候选，并让旧源按专用退避继续找下一源。"""
    if trial.id is None:
        return
    await session.refresh(trial)
    if trial.status != DownloadAttemptStatus.TRIAL:
        return
    old = (
        await session.get(SubscriptionDownloadAttempt, trial.replaces_attempt_id)
        if trial.replaces_attempt_id is not None
        else None
    )
    if old is not None:
        await session.refresh(old)
        trial_units = {
            (int(unit[0]), int(unit[1]))
            for unit in trial.units
            if isinstance(unit, list) and len(unit) == 2
        }
        if await _units_have_left_scope(session, old, allowed_units=trial_units):
            trial.status = DownloadAttemptStatus.CANCELLED
            trial.cleanup_note = "关联单元已退出当前订阅范围；保留试用任务，不自动删除"
            trial.next_search_at = None
            trial.updated_at = utcnow()
            if old.status in (
                DownloadAttemptStatus.ACTIVE,
                DownloadAttemptStatus.REPLACEMENT_PENDING,
            ):
                old.status = DownloadAttemptStatus.CANCELLED
                old.next_search_at = None
                old.cleanup_note = "关联单元已退出当前订阅范围；保留旧下载器任务"
                old.updated_at = utcnow()
                session.add(old)
            session.add(trial)
            await session.commit()
            return

    now = utcnow()
    result = await session.execute(
        update(SubscriptionDownloadAttempt)
        .where(
            SubscriptionDownloadAttempt.id == trial.id,
            SubscriptionDownloadAttempt.status == DownloadAttemptStatus.TRIAL,
        )
        .values(
            status=DownloadAttemptStatus.FAILED,
            cleanup_note=reason,
            updated_at=now,
        )
    )
    if not result.rowcount:
        await session.rollback()
        return
    if old is not None and old.id is not None:
        await session.execute(
            update(SubscriptionDownloadAttempt)
            .where(
                SubscriptionDownloadAttempt.id == old.id,
                SubscriptionDownloadAttempt.status
                == DownloadAttemptStatus.REPLACEMENT_PENDING,
            )
            .values(
                next_search_at=now + replacement_backoff(old.search_attempts),
                updated_at=now,
            )
        )
    await session.commit()
    await session.refresh(trial)
    await _cleanup_failed_trial(session, trial, reason)


async def _cleanup_failed_trial(
    session: AsyncSession,
    trial: SubscriptionDownloadAttempt,
    reason: str,
) -> None:
    """失败试用源只在证据充分时移除任务，数据一律保留供后续候选复用。"""
    await session.refresh(trial)
    old = (
        await session.get(SubscriptionDownloadAttempt, trial.replaces_attempt_id)
        if trial.replaces_attempt_id is not None
        else None
    )
    trial_units = {
        (int(unit[0]), int(unit[1]))
        for unit in trial.units
        if isinstance(unit, list) and len(unit) == 2
    }
    if old is not None and await _units_have_left_scope(
        session,
        old,
        allowed_units=trial_units,
    ):
        trial.status = DownloadAttemptStatus.CANCELLED
        trial.cleanup_note = "关联单元已退出当前订阅范围；保留失败试用任务，不自动删除"
        trial.updated_at = utcnow()
        session.add(trial)
        await session.commit()
        return
    if not trial.owned_by_movieclaw:
        trial.status = DownloadAttemptStatus.RETAINED
        trial.cleanup_note = f"{reason}；该任务原先已存在，未自动删除"
    elif trial.hit_and_run is not False:
        trial.status = DownloadAttemptStatus.RETAINED
        trial.cleanup_note = f"{reason}；存在 H&R 风险或考核状态未知，未自动删除"
    elif trial.downloader_id is None:
        trial.status = DownloadAttemptStatus.RETAINED
        trial.cleanup_note = f"{reason}；无法确认任务所在下载器，未自动删除"
    else:
        downloader = await session.get(DownloaderClient, trial.downloader_id)
        if downloader is None:
            trial.status = DownloadAttemptStatus.RETAINED
            trial.cleanup_note = f"{reason}；下载器配置已不存在，未自动删除"
        else:
            repo = DownloaderRepository(session)
            adapter = create_downloader(
                DownloaderConfig(
                    type=downloader.client_type.value,
                    url=downloader.url,
                    username=downloader.username,
                    password=repo.decrypted_password(downloader),
                )
            )
            try:
                if old is not None and await _units_have_left_scope(
                    session,
                    old,
                    allowed_units=trial_units,
                ):
                    trial.status = DownloadAttemptStatus.CANCELLED
                    trial.cleanup_note = (
                        "删除失败试用任务前关联单元已退出订阅范围；下载器任务保持不动"
                    )
                    trial.updated_at = utcnow()
                    session.add(trial)
                    await session.commit()
                    return
                # 试用源与旧源通常共用落点，删除数据可能破坏旧源复用的分片；
                # 因此失败试用只移除下载器任务，保守保留磁盘数据。
                await adapter.delete_torrent(trial.info_hash, delete_files=False)
                trial.cleanup_note = f"{reason}；已移除失败试用任务，数据文件保留"
            except Exception as exc:  # noqa: BLE001 -- 清理失败不影响旧源继续等待
                trial.status = DownloadAttemptStatus.RETAINED
                trial.cleanup_note = f"{reason}；移除失败试用任务失败：{exc}"
            finally:
                try:
                    await adapter.close()
                except Exception:  # noqa: BLE001 -- 关闭失败不覆盖清理结论
                    logger.warning("关闭失败试用源下载器连接失败", exc_info=True)
    trial.updated_at = utcnow()
    session.add(trial)
    await session.commit()
    await SubscriptionRepository(session).add_activity(
        SubscriptionActivity(
            subscription_id=trial.subscription_id,
            type=ActivityType.REPLACEMENT_CLEANUP,
            message=trial.cleanup_note or reason,
            payload={
                "info_hash": trial.info_hash,
                "failed_trial": True,
                "retained": trial.status == DownloadAttemptStatus.RETAINED,
            },
        )
    )


async def _stop_replacement_cleanup_if_out_of_scope(
    session: AsyncSession,
    old: SubscriptionDownloadAttempt,
    new: SubscriptionDownloadAttempt,
) -> bool:
    """换源清理的最后守门：取消订阅优先于自动删除下载器任务。"""
    if not await _units_have_left_scope(session, new):
        return False
    old.status = DownloadAttemptStatus.CANCELLED
    old.next_search_at = None
    old.cleanup_note = "关联单元已退出当前订阅范围；旧下载器任务保持不动"
    new.status = DownloadAttemptStatus.CANCELLED
    new.next_search_at = None
    new.cleanup_note = "关联单元已退出当前订阅范围；替代源停止观察"
    now = utcnow()
    old.updated_at = now
    new.updated_at = now
    session.add_all([old, new])
    await session.commit()
    return True


async def _cleanup_replaced_attempt(
    session: AsyncSession,
    old: SubscriptionDownloadAttempt,
    new: SubscriptionDownloadAttempt,
    new_status: TorrentStatus,
) -> None:
    """删除可安全控制的旧任务；任何证据不足都保留并给出中文原因。"""
    await session.refresh(old)
    await session.refresh(new)
    if old.status != DownloadAttemptStatus.CLEANUP_PENDING or new.status not in (
        DownloadAttemptStatus.ACTIVE,
        DownloadAttemptStatus.COMPLETED,
    ):
        return
    if await _stop_replacement_cleanup_if_out_of_scope(session, old, new):
        return
    if not old.owned_by_movieclaw:
        await _retain_old(session, old, "旧任务在 MovieClaw 投递前已经存在，保留用户任务")
        return
    if old.hit_and_run is not False:
        await _retain_old(session, old, "旧任务存在 H&R 风险或考核状态未知，未自动删除")
        return
    if old.downloader_id is None:
        await _retain_old(session, old, "无法确认旧任务所在下载器，未自动删除")
        return
    downloader = await session.get(DownloaderClient, old.downloader_id)
    if downloader is None:
        await _retain_old(session, old, "旧下载器配置已不存在，无法自动删除任务")
        return
    repo = DownloaderRepository(session)
    adapter = create_downloader(
        DownloaderConfig(
            type=downloader.client_type.value,
            url=downloader.url,
            username=downloader.username,
            password=repo.decrypted_password(downloader),
        )
    )
    try:
        old_status = await adapter.get_torrent(old.info_hash)
        if old_status is None:
            old.status = DownloadAttemptStatus.SUPERSEDED
            old.cleanup_note = "旧任务已不在下载器中"
            old.updated_at = utcnow()
            session.add(old)
            await session.commit()
            return
        delete_files = not _paths_overlap(
            downloader,
            old_status,
            await session.get(DownloaderClient, new.downloader_id)
            if new.downloader_id is not None
            else None,
            new_status,
        )
        # 查询文件清单期间仍可能发生取消订阅；真正发出删除命令前再次读取
        # 数据库状态，把外部副作用窗口压到最小。
        await session.refresh(old)
        await session.refresh(new)
        if old.status != DownloadAttemptStatus.CLEANUP_PENDING or new.status not in (
            DownloadAttemptStatus.ACTIVE,
            DownloadAttemptStatus.COMPLETED,
        ):
            return
        if await _stop_replacement_cleanup_if_out_of_scope(session, old, new):
            return
        await adapter.delete_torrent(old.info_hash, delete_files=delete_files)
    except Exception as exc:  # noqa: BLE001 -- 清理失败不能回滚已经成功的换源
        logger.warning("换源成功但旧任务自动清理失败：%s", exc)
        await _retain_old(session, old, f"自动清理失败：{exc}")
        return
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001 -- 关闭失败不能覆盖已完成的清理结论
            logger.warning("关闭旧任务下载器连接失败", exc_info=True)
    old.status = DownloadAttemptStatus.SUPERSEDED
    old.cleanup_note = (
        "旧任务和残留文件已删除"
        if delete_files
        else "旧任务已删除，重叠文件留给新源复用"
    )
    old.updated_at = utcnow()
    session.add(old)
    await session.commit()
    await SubscriptionRepository(session).add_activity(
        SubscriptionActivity(
            subscription_id=old.subscription_id,
            type=ActivityType.REPLACEMENT_CLEANUP,
            message=old.cleanup_note,
            payload={"info_hash": old.info_hash, "delete_files": delete_files},
        )
    )


async def _retain_old(
    session: AsyncSession,
    old: SubscriptionDownloadAttempt,
    reason: str,
) -> None:
    old.status = DownloadAttemptStatus.RETAINED
    old.cleanup_note = reason
    old.updated_at = utcnow()
    session.add(old)
    await session.commit()
    await SubscriptionRepository(session).add_activity(
        SubscriptionActivity(
            subscription_id=old.subscription_id,
            type=ActivityType.REPLACEMENT_CLEANUP,
            message=f"已完成换种，但{reason}",
            payload={"info_hash": old.info_hash, "retained": True},
        )
    )


def _paths_overlap(
    old_downloader: DownloaderClient,
    old_status: TorrentStatus,
    new_downloader: DownloaderClient | None,
    new_status: TorrentStatus,
) -> bool:
    """比较 MovieClaw 视角绝对文件路径；证据不足时按重叠处理，禁止删数据。"""
    from movieclaw_api.services.torrent_submit import translate_to_local

    if new_downloader is None:
        return True

    def paths(downloader: DownloaderClient, status: TorrentStatus) -> set[str]:
        base = translate_to_local(status.save_path, downloader.path_mappings)
        if not base or not status.files:
            return set()
        result = set()
        for torrent_file in status.files:
            relative = PurePosixPath(torrent_file.path.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            result.add(str(PurePosixPath(base).joinpath(*relative.parts)))
        return result

    old_paths = paths(old_downloader, old_status)
    new_paths = paths(new_downloader, new_status)
    return not old_paths or not new_paths or bool(old_paths & new_paths)
