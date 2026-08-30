"""任务中心的下载器实时聚合视图。

下载进度只存在于 qBittorrent / Transmission；本服务每次并行读取实时列表，
再按 infohash 关联订阅工单、换源心跳与手动下载意图。数据库只保存完成字节
最后增长时刻等救援证据，不复制实时进度。某台下载器不可达时只把该来源标为
异常，其余来源仍正常返回。
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import NotFoundException, UpstreamServiceException
from movieclaw_api.services.network_egress import effective_tmdb_image_base_url
from movieclaw_db.models import (
    BoostTaskState,
    ConfigStatus,
    DownloadAttemptStatus,
    DownloaderClient,
    FileState,
    Library,
    LibraryFile,
    ManualDownloadIntent,
    MediaItem,
    RatioBoostTask,
    SiteTorrent,
    Subscription,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader import (
    DownloaderConfig,
    DownloaderException,
    TorrentBrief,
    TorrentStatus,
    create_downloader,
)
from movieclaw_downloader.models import DownloaderLimits

logger = logging.getLogger("movieclaw_api.download_tasks")

_IN_FLIGHT = (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)


def _site_display_name(site_id: str | None) -> str | None:
    """把站点 ID 换成用户可读的站点名；站点配置已被移除时回落原 ID。"""
    if not site_id:
        return None
    from movieclaw_tracker.exceptions import SiteNotFoundError
    from movieclaw_tracker.registry import get_site_config

    try:
        return get_site_config(site_id).display_name
    except SiteNotFoundError:
        return site_id


def _poster_url(item: MediaItem) -> str | None:
    """把已识别条目的海报收口为可直接展示的 TMDB 地址。"""
    if not item.poster_path:
        return None
    base = effective_tmdb_image_base_url().rstrip("/")
    return f"{base}/w342{item.poster_path}"


def _unit_replaced(
    *,
    info_hash: str,
    attempt_created_at: datetime | None,
    status: str | None,
    unit_hash: str | None,
    imported_at: datetime | None,
) -> bool:
    """洗版 attempt 下，这一集是否真的已经被本种子替换完成。

    判据是「工单已改指本种子」：升级确认时 upgrade.py 才会把 ``wanted.info_hash``
    改写成新种，确认前它一直指向旧版本。

    但只看 hash 会误判**混合投递**——整季包同时洗 E01–E10 又补 E11 这种缺口集
    时，缺口工单从投递那一刻起 info_hash 就等于本种子，会被当成"已替换"。这里
    沿用 upgrade.py 判定洗版目标的同一条硬判据：该集必须在 attempt 创建**之前**
    就已入库，才可能是这次洗版要替换的对象（洗版确认不改写 imported_at，缺口集
    的入库时间则必然晚于 attempt 创建）。
    """
    if unit_hash != info_hash:
        return False
    if status != WantedStatus.IMPORTED.value:
        return False
    if imported_at is None or attempt_created_at is None:
        return False
    return imported_at <= attempt_created_at


def _delivery_plan_resolver(session: AsyncSession):
    """构造带缓存的「下载完成后会发生什么」解析器。

    任务中心是轮询接口，不能逐任务重复打库；但落点推导**必须**走
    ``resolve_save_path``——它是投递、预检、体检、手动下载共用的唯一实现，
    在这里另抄一份迟早出现"卡片说好、投递却挂"的口径漂移（见
    SavePathDecision 的文档）。折中办法是按 (库, 类型, 片名, 年份) 记忆化：
    同一部作品的多条任务只算一次，组合数被在途作品数封顶。
    """
    from movieclaw_api.services.library.routing import resolve_save_path

    libraries: dict[int, Library] = {}
    cache: dict[tuple[int | None, str, str | None, int | None], dict[str, Any] | None] = {}

    async def resolve(
        library_id: int | None, kind: str | None, title: str | None, year: int | None
    ) -> dict[str, Any] | None:
        if not kind:
            return None
        key = (library_id, kind, title, year)
        if key in cache:
            return cache[key]
        if library_id is not None and library_id not in libraries:
            row = await session.get(Library, library_id)
            if row is not None:
                libraries[library_id] = row
        library = libraries.get(library_id) if library_id is not None else None
        decision = await resolve_save_path(
            session, library, kind=kind, title=title, year=year
        )
        rule = decision.rule
        # 自定义目录规则：整理改名后落规则目录、**不进任何库**，后续由库根
        # 扫描收尾——不能对用户说"入库"
        custom_dir = rule is not None and rule.target_path is not None
        if custom_dir:
            dest_path = rule.target_path
        elif decision.mode == "watch":
            # 监听导入的落点是库推导的条目目录；库未定（按收藏范围自动选库）
            # 时给不出具体路径，如实留空由前端说"按收藏范围自动选库"
            dest_path = decision.entry_dir
        else:
            dest_path = decision.path
        plan = {
            "mode": decision.mode,
            "strategy": rule.strategy if rule is not None else None,
            "library_name": None if custom_dir else (library.name if library else None),
            "dest_path": dest_path,
            "enters_library": not custom_dir,
        }
        cache[key] = plan
        return plan

    return resolve


async def _relations(
    session: AsyncSession,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """一次查询装好订阅/手动意图索引，避免按下载任务逐条查库。"""
    subscription_rows = (
        await session.execute(
            select(WantedItem, Subscription, MediaItem)
            .join(Subscription, Subscription.id == WantedItem.subscription_id)
            .join(MediaItem, MediaItem.id == WantedItem.media_item_id)
            .where(
                WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                WantedItem.info_hash.is_not(None),  # type: ignore[union-attr]
            )
        )
    ).all()
    grouped_units: dict[tuple[str, int], set[tuple[int, int]]] = defaultdict(set)
    subscription_meta: dict[tuple[str, int], dict[str, Any]] = {}
    base_meta_by_subscription: dict[int, dict[str, Any]] = {}
    scoped_units_by_subscription: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for wanted, subscription, media in subscription_rows:
        assert wanted.info_hash is not None and subscription.id is not None
        info_hash = wanted.info_hash.lower()
        key = (info_hash, subscription.id)
        grouped_units[key].add((wanted.season_number, wanted.episode_number))
        scoped_units_by_subscription[subscription.id].add(
            (wanted.season_number, wanted.episode_number)
        )
        meta = {
            "id": subscription.id,
            "media_item_id": media.id,
            "media_title": media.title,
            "media_kind": media.kind,
            "poster_url": _poster_url(media),
            # 推导"下载完成后去哪"的原料：库与类型定规则，片名与年份定条目目录
            "_library_id": subscription.library_id,
            "_kind": subscription.kind,
            "_year": media.year,
        }
        subscription_meta[key] = meta
        base_meta_by_subscription[subscription.id] = meta

    attempt_rows = (
        await session.execute(
            select(SubscriptionDownloadAttempt, Subscription, MediaItem)
            .join(
                Subscription,
                Subscription.id == SubscriptionDownloadAttempt.subscription_id,
            )
            .join(MediaItem, MediaItem.id == Subscription.media_item_id)
            .where(
                SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                    (
                        DownloadAttemptStatus.ACTIVE,
                        DownloadAttemptStatus.REPLACEMENT_PENDING,
                        DownloadAttemptStatus.TRIAL,
                        DownloadAttemptStatus.CLEANUP_PENDING,
                        DownloadAttemptStatus.RETAINED,
                        DownloadAttemptStatus.COMPLETED,
                    )
                )
            )
        )
    ).all()
    # 第二遍取数：把相关订阅的**全部**在域工单状态装进来（不限状态）。上面
    # 的 _IN_FLIGHT 过滤决定「任务是否上榜」——已入库工单不能把早已完成的老
    # 任务重新拉回列表；而卡片要展示的是种子覆盖的全集与逐集进度，已入库的
    # 集不能从列表里消失。两个用途语义相反，必须分开取数。
    related_subscription_ids = {subscription_id for _hash, subscription_id in subscription_meta} | {
        attempt.subscription_id for attempt, _subscription, _media in attempt_rows
    }
    unit_status_by_subscription: dict[int, dict[tuple[int, int], str]] = defaultdict(dict)
    # 工单当前指向的种子 + 入库时间。洗版确认成功时升级流程会把工单的 info_hash
    # 改写成新种（upgrade.py `wanted.info_hash = attempt.info_hash`），确认之前它
    # 一直指向旧版本——「工单 hash == 本 attempt hash」是洗版任务唯一可信的完成
    # 度信号。入库时间用于剔除混合投递里的缺口集，见下面 _unit_replaced
    unit_hash_by_subscription: dict[int, dict[tuple[int, int], str]] = defaultdict(dict)
    unit_imported_at_by_subscription: dict[int, dict[tuple[int, int], datetime]] = defaultdict(dict)
    if related_subscription_ids:
        for w in (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id.in_(related_subscription_ids),  # type: ignore[attr-defined]
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                )
            )
        ).scalars():
            unit = (w.season_number, w.episode_number)
            unit_status_by_subscription[w.subscription_id][unit] = str(w.status)
            if w.info_hash:
                unit_hash_by_subscription[w.subscription_id][unit] = w.info_hash.lower()
            if w.imported_at is not None:
                unit_imported_at_by_subscription[w.subscription_id][unit] = w.imported_at
    # 洗版 attempt 的关联走升级语义（quality-upgrade.md §6.2）：工单不重开、
    # info_hash 指向旧版本，缺口语义（在途工单 ∩ attempt 单元）对它恒为空——
    # 必须按「attempt 单元 ∩ 已入库 in_scope 工单」关联，否则洗版种子会被
    # 当成外部任务：无影片身份、任务中心不按影片分组（真实教训）
    imported_units_by_subscription: dict[int, set[tuple[int, int]]] = {
        subscription_id: {
            unit for unit, status in units.items() if status == WantedStatus.IMPORTED.value
        }
        for subscription_id, units in unit_status_by_subscription.items()
    }
    # 已退回重找的集：内容核验判定"种子里没有它"后工单立刻回到 wanted，
    # 于是这个种子在缺口语义下瞬间失去关联、退化成外部任务，用户再也看不到
    # "为什么等不到这一集"。这些集还没从别处补齐前，关联必须留住
    open_units_by_subscription: dict[int, set[tuple[int, int]]] = {
        subscription_id: {
            unit for unit, status in units.items() if status == WantedStatus.WANTED.value
        }
        for subscription_id, units in unit_status_by_subscription.items()
    }

    for attempt, subscription, media in attempt_rows:
        assert subscription.id is not None
        info_hash = attempt.info_hash.lower()
        key = (info_hash, subscription.id)
        attempt_units = {
            (int(unit[0]), int(unit[1]))
            for unit in attempt.units
            if isinstance(unit, list) and len(unit) == 2
        }
        content_missing_units = {
            (int(unit[0]), int(unit[1]))
            for unit in (attempt.content_missing or {}).get("units", [])
            if isinstance(unit, list) and len(unit) == 2
        }
        relevant_units = attempt_units & scoped_units_by_subscription[subscription.id]
        if attempt.purpose == "upgrade":
            relevant_units |= attempt_units & imported_units_by_subscription.get(
                subscription.id, set()
            )
        relevant_units |= content_missing_units & open_units_by_subscription.get(
            subscription.id, set()
        )
        # 尝试台账只是历史/救援状态，不能单独制造业务任务。主源直接按 hash
        # 关联；试用源和待清理旧源按覆盖单元关联，但都必须仍有入域在途目标
        # （洗版语义下"目标"即它照看的已入库单元）。
        if key not in subscription_meta and not relevant_units:
            continue
        # 展示口径以 attempt 台账为准：它记的就是"这个种子声明覆盖哪些集"，
        # 与各集是否已入库无关，因此分批入库推进时列表保持稳定。与订阅在域
        # 单元取交集，避免种子多带的集越出订阅范围。
        grouped_units[key] = attempt_units & set(
            unit_status_by_subscription.get(subscription.id, {})
        )
        # 纯洗版订阅可能没有任何缺口在途行，base_meta 里没有它——用 attempt
        # 查询自带的订阅/条目现场构造
        base_meta = base_meta_by_subscription.get(subscription.id) or {
            "id": subscription.id,
            "media_item_id": media.id,
            "media_title": media.title,
            "media_kind": media.kind,
            "poster_url": _poster_url(media),
            "_library_id": subscription.library_id,
            "_kind": subscription.kind,
            "_year": media.year,
        }
        subscription_meta[key] = {
            **base_meta,
            # 洗版任务与补缺下载在卡片上完全同形，但"已入库"对它是**前提**而非
            # 成果（要洗的集本来就在库里）——不透出目的，前端只能按补缺语义把
            # 存量入库读成任务已完成，与 0% 进度自相矛盾
            "purpose": attempt.purpose,
            "_attempt_created_at": attempt.created_at,
            "_site_id": attempt.site_id,
            "_torrent_id": attempt.torrent_id,
            "_quality": attempt.quality,
            "_attempt_status": attempt.status,
            "_attempt_downloader_id": attempt.downloader_id,
            "_last_progress_at": attempt.last_progress_at,
            "_next_search_at": attempt.next_search_at,
            "_cleanup_note": attempt.cleanup_note,
            # 内容核验证明本种子不含的集：下载完成 ≠ 这一集会到手，卡片必须
            # 说"内容不符"而不是继续挂"等待下载完成"
            "_content_missing": content_missing_units,
            "_show_when_missing": attempt.status
            in (
                DownloadAttemptStatus.ACTIVE,
                DownloadAttemptStatus.REPLACEMENT_PENDING,
                DownloadAttemptStatus.TRIAL,
                DownloadAttemptStatus.CLEANUP_PENDING,
                DownloadAttemptStatus.COMPLETED,
            ),
        }

    resolve_plan = _delivery_plan_resolver(session)
    # 洗版替换掉的旧版本去哪，由规则组的「保留共存」开关决定（默认进回收站）。
    # 复用洗版自己的批量解析器，避免另抄一份 spec 读取逻辑跑偏
    from movieclaw_api.services.subscription.upgrade import _specs_for_subscriptions

    specs = await _specs_for_subscriptions(
        session, {subscription_id for _hash, subscription_id in subscription_meta}
    )
    subscriptions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, meta in subscription_meta.items():
        info_hash, subscription_id = key
        statuses = unit_status_by_subscription.get(subscription_id, {})
        hashes = unit_hash_by_subscription.get(subscription_id, {})
        imported_ats = unit_imported_at_by_subscription.get(subscription_id, {})
        # 只有洗版任务才谈"替换完成"：补缺下载的工单 hash 从投递起就等于本
        # 种子，拿同一判据会把刚投递的集全标成已替换
        upgrade = meta.get("purpose") == "upgrade"
        attempt_created_at = meta.get("_attempt_created_at")
        spec = specs.get(subscription_id)
        subscriptions[info_hash].append(
            {
                **meta,
                "upgrade_keep_old": bool(spec.upgrade_keep_old) if spec is not None else False,
                "_plan": await resolve_plan(
                    meta.get("_library_id"),
                    meta.get("_kind") or meta.get("media_kind"),
                    meta.get("media_title"),
                    meta.get("_year"),
                ),
                "units": [
                    {
                        "season_number": season,
                        "episode_number": episode,
                        # 工单已被换源等路径清理时按"已投递"兜底：任务还在
                        # 下载器里，说明至少已经投递过
                        "status": statuses.get((season, episode), WantedStatus.GRABBED.value),
                        "replaced": upgrade
                        and _unit_replaced(
                            info_hash=info_hash,
                            attempt_created_at=attempt_created_at,
                            status=statuses.get((season, episode)),
                            unit_hash=hashes.get((season, episode)),
                            imported_at=imported_ats.get((season, episode)),
                        ),
                        "content_missing": (season, episode)
                        in (meta.get("_content_missing") or set()),
                    }
                    for season, episode in sorted(grouped_units[key])
                ],
            }
        )

    manual_rows = (
        await session.execute(
            select(ManualDownloadIntent, MediaItem).join(
                MediaItem, MediaItem.id == ManualDownloadIntent.media_item_id
            )
        )
    ).all()
    manual = {
        intent.info_hash.lower(): {
            "intent_id": intent.id,
            "intent_created_at": intent.created_at,
            "library_id": intent.library_id,
            "site_id": intent.site_id,
            "torrent_id": intent.torrent_id,
            "media_item_id": media.id,
            "media_title": media.title,
            "media_kind": media.kind,
            "poster_url": _poster_url(media),
            "_plan": await resolve_plan(
                intent.library_id, media.kind, media.title, media.year
            ),
        }
        for intent, media in manual_rows
    }

    # 反查站点种子详情页：订阅投递台账与手动身份锚都记了 site_id + torrent_id，
    # 站点快照索引里存有面向浏览器的 detail_url。查不到（快照未覆盖的老种子
    # /旧数据）就不给链接，仅少一个跳转入口。
    subscription_entries = [entry for group in subscriptions.values() for entry in group]
    site_pairs = {
        (entry["_site_id"], entry["_torrent_id"])
        for entry in subscription_entries
        if entry.get("_site_id") and entry.get("_torrent_id")
    } | {
        (entry["site_id"], entry["torrent_id"])
        for entry in manual.values()
        if entry.get("site_id") and entry.get("torrent_id")
    }
    if site_pairs:
        rows = await session.execute(
            select(SiteTorrent.site_id, SiteTorrent.torrent_id, SiteTorrent.detail_url).where(
                or_(
                    *(
                        and_(SiteTorrent.site_id == site_id, SiteTorrent.torrent_id == torrent_id)
                        for site_id, torrent_id in site_pairs
                    )
                )
            )
        )
        page_urls = {
            (site_id, torrent_id): detail_url
            for site_id, torrent_id, detail_url in rows
            if detail_url
        }
        for entry in subscription_entries:
            if entry.get("_site_id") and entry.get("_torrent_id"):
                entry["_page_url"] = page_urls.get((entry["_site_id"], entry["_torrent_id"]))
        for entry in manual.values():
            if entry.get("site_id") and entry.get("torrent_id"):
                entry["_page_url"] = page_urls.get((entry["site_id"], entry["torrent_id"]))
    return dict(subscriptions), manual


def _torrent_video_files(
    status: TorrentStatus, downloader: DownloaderClient
) -> list[tuple[str, int]]:
    """把下载器文件清单还原成 MovieClaw 视角的绝对视频路径。

    历史手动下载锚没有保存投递模式和目录，只能以下载器仍持有的事实反查。
    路径先按下载器映射翻译回来；相对文件名拒绝绝对路径与 ``..``，避免异常
    上游数据越出保存目录。这里只计算字符串用于台账对账，不触碰磁盘。
    """
    from movieclaw_api.services.library.layout import VIDEO_EXTS
    from movieclaw_api.services.torrent_submit import translate_to_local

    local_base = translate_to_local(status.save_path, downloader.path_mappings)
    if not local_base:
        return []
    base = local_base.replace("\\", "/")
    files: list[tuple[str, int]] = []
    for torrent_file in status.files:
        relative = PurePosixPath(torrent_file.path.replace("\\", "/"))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            continue
        lower_name = relative.name.lower()
        if relative.suffix.lower() not in VIDEO_EXTS or "sample" in lower_name:
            continue
        files.append(
            (
                posixpath.normpath(posixpath.join(base, *relative.parts)),
                torrent_file.size_bytes,
            )
        )
    return files


def _inventory_covers_torrent(
    *,
    status: TorrentStatus,
    downloader: DownloaderClient,
    manual: dict[str, Any],
    inventory: list[LibraryFile],
) -> bool:
    """库存是否逐文件覆盖一个已完成的手动种子。

    原地下载优先按绝对路径精确命中；监听导入可能改名，历史版本又没有在
    ``library_file`` 写 torrent_id，因此只对身份锚创建后新增的同库、同媒体
    文件开放“尺寸相同”补偿。每个库存行最多消费一次，季包只入了一部分时
    不会提前收尾；仅仅拥有该剧的旧集也不会误清新任务。
    """
    expected = _torrent_video_files(status, downloader)
    if not status.completed or not expected:
        return False

    relevant = [
        row
        for row in inventory
        if row.library_id == manual["library_id"]
        and row.media_item_id == manual["media_item_id"]
        and row.state == FileState.IN_PLACE
    ]
    unused = set(range(len(relevant)))
    for expected_path, expected_size in expected:
        matched = next(
            (
                index
                for index in unused
                if posixpath.normpath(relevant[index].file_path.replace("\\", "/"))
                == expected_path
            ),
            None,
        )
        if matched is None and expected_size > 0:
            matched = next(
                (
                    index
                    for index in unused
                    if relevant[index].size_bytes == expected_size
                    and relevant[index].created_at >= manual["intent_created_at"]
                ),
                None,
            )
        if matched is None:
            return False
        unused.remove(matched)
    return True


async def _reconcile_completed_manual_intents(
    session: AsyncSession,
    manual: dict[str, dict[str, Any]],
    details: dict[str, tuple[DownloaderClient, TorrentStatus]],
) -> set[str]:
    """用下载器文件清单与库存台账回收已完成的手动下载身份锚。

    这是对旧版本残留的读时修复：任务中心本来就要联合下载器与库存事实，
    第一次确认全部视频已经入账后删除瞬态身份锚；证据不足时保持原样等待。
    """
    candidates = {info_hash: manual[info_hash] for info_hash in details if info_hash in manual}
    if not candidates:
        return set()
    media_ids = {row["media_item_id"] for row in candidates.values()}
    library_ids = {row["library_id"] for row in candidates.values()}
    inventory = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.media_item_id.in_(media_ids),  # type: ignore[union-attr]
                    LibraryFile.library_id.in_(library_ids),  # type: ignore[union-attr]
                    LibraryFile.in_place(),
                )
            )
        )
        .scalars()
        .all()
    )

    consumed: set[str] = set()
    for info_hash, meta in candidates.items():
        downloader, status = details[info_hash]
        if not _inventory_covers_torrent(
            status=status,
            downloader=downloader,
            manual=meta,
            inventory=inventory,
        ):
            continue
        intent = await session.get(ManualDownloadIntent, meta["intent_id"])
        if intent is None:
            continue
        await session.delete(intent)
        consumed.add(info_hash)
        logger.info(
            "已按下载文件与库存台账对账，清理手动下载残留身份锚：hash=%s media_item=%s",
            info_hash,
            meta["media_item_id"],
        )
    if consumed:
        await session.commit()
    return consumed


def _task_dict(
    *,
    task_id: str,
    info_hash: str,
    torrent: TorrentBrief | None,
    downloader: DownloaderClient | None,
    subscriptions: list[dict[str, Any]],
    manual: dict[str, Any] | None,
    boost: dict[str, Any] | None = None,
    absent_state: str = "missing",
) -> dict[str, Any]:
    # 来源优先级：订阅/手动（媒体下载语义）> 刷流台账 > 外部。被订阅接管的
    # 刷流种子台账已置 missing（不在 boost 映射里），不会出现双重归属
    source = (
        "subscription"
        if subscriptions
        else "manual" if manual else "boost" if boost else "external"
    )
    media = subscriptions[0] if subscriptions else manual
    rescue = next(
        (entry for entry in subscriptions if entry.get("_attempt_status") is not None),
        None,
    )
    completed = bool(torrent and torrent.completed)
    state = absent_state if torrent is None else "completed" if completed else torrent.state
    # 站点/质量信息取自投递台账（订阅任务）、手动身份锚或刷流台账；外部任务皆无
    site_id = (
        next((entry["_site_id"] for entry in subscriptions if entry.get("_site_id")), None)
        or (manual or {}).get("site_id")
        or (boost or {}).get("site_id")
    )
    quality = next(
        (entry["_quality"] for entry in subscriptions if entry.get("_quality")), None
    ) or {}
    no_progress_seconds = None
    if rescue is not None and rescue.get("_last_progress_at") is not None:
        last_progress = rescue["_last_progress_at"]
        if last_progress.tzinfo is None:
            last_progress = last_progress.replace(tzinfo=UTC)
        no_progress_seconds = max(
            0,
            int((datetime.now(UTC) - last_progress.astimezone(UTC)).total_seconds()),
        )
    rescue_state = rescue.get("_attempt_status") if rescue is not None else None
    next_search_at = rescue.get("_next_search_at") if rescue is not None else None
    can_replace = bool(
        rescue is not None
        and rescue_state
        in (DownloadAttemptStatus.ACTIVE, DownloadAttemptStatus.REPLACEMENT_PENDING)
        and no_progress_seconds is not None
        and no_progress_seconds >= 15 * 60
        # unknown=下载器不可达，任务死活未知；missing=任务已经不在下载器里，
        # "换掉旧源"无从谈起——救援巡检确证后会把工单退回重找，这段窗口里
        # 换种只会平白多攒一个源。两种情况都不给按钮，只给解释
        and state not in ("unknown", "missing")
        and not (
            rescue_state == DownloadAttemptStatus.REPLACEMENT_PENDING
            and next_search_at is None
        )
    )
    return {
        "id": task_id,
        "info_hash": info_hash,
        "name": (
            torrent.name
            if torrent is not None
            else (media or {}).get("media_title") or (boost or {}).get("title")
        ),
        "downloader_id": (
            downloader.id
            if downloader is not None
            else rescue.get("_attempt_downloader_id") if rescue is not None else None
        ),
        "downloader_name": downloader.name if downloader is not None else None,
        "downloader_type": downloader.client_type if downloader is not None else None,
        "progress": torrent.progress if torrent is not None else None,
        "size_bytes": torrent.size_bytes if torrent is not None else None,
        "dlspeed_bytes": torrent.dlspeed_bytes if torrent is not None else None,
        "upspeed_bytes": torrent.upspeed_bytes if torrent is not None else None,
        # 累计上传/已完成字节：刷流分组的汇总原料（其它来源同样受益于展示）
        "uploaded_bytes": torrent.uploaded_bytes if torrent is not None else None,
        "completed_bytes": torrent.completed_bytes if torrent is not None else None,
        "eta_seconds": torrent.eta_seconds if torrent is not None else None,
        "state": state,
        "source": source,
        "site_id": site_id,
        "site_name": _site_display_name(site_id),
        "page_url": next(
            (entry["_page_url"] for entry in subscriptions if entry.get("_page_url")), None
        )
        or (manual or {}).get("_page_url")
        or (boost or {}).get("_page_url"),
        "resolution": quality.get("resolution"),
        "media_source": quality.get("media_source"),
        "remux": bool(quality.get("remux")),
        "media_item_id": (media or {}).get("media_item_id"),
        "media_title": (media or {}).get("media_title"),
        "media_kind": (media or {}).get("media_kind"),
        "poster_url": (media or {}).get("poster_url"),
        # 下载完成后的未发生路径：外部任务没有业务身份，推不出落点
        "plan": (media or {}).get("_plan"),
        "subscriptions": subscriptions,
        "rescue_state": rescue_state,
        "no_progress_seconds": no_progress_seconds,
        "can_replace": can_replace,
        "replacement_due_at": next_search_at,
        "rescue_message": (
            # 任务已不在下载器里时，换源文案（"可立即换种…"）会指向一个不该
            # 再点的动作；交回前端的 missing 解释文案
            None
            if state in ("unknown", "missing")
            else _rescue_message(
                rescue_state,
                no_progress_seconds,
                next_search_at,
                rescue.get("_cleanup_note") if rescue is not None else None,
            )
        ),
    }


def _rescue_message(
    state: str | None,
    no_progress_seconds: int | None,
    next_search_at,
    cleanup_note: str | None,
) -> str | None:
    """把换源状态渲染成任务中心可直接显示的中文解释。"""
    if state == DownloadAttemptStatus.TRIAL:
        return "正在验证同品质替代源；新源产生至少 1 MiB 真实进度后才会切换旧源"
    if state == DownloadAttemptStatus.REPLACEMENT_PENDING:
        if next_search_at is None:
            return "已投递同品质替代源，正在等待真实下载进度"
        if next_search_at <= datetime.now(UTC).replace(tzinfo=None):
            return "旧源仍保留，正在跨站寻找同品质替代源"
        return "暂未找到同品质替代源，旧源继续保留；系统会按退避计划重新跨站搜索"
    if state == DownloadAttemptStatus.RETAINED:
        return cleanup_note or "换源已完成；旧任务因安全条件不足而保留"
    if state == DownloadAttemptStatus.CLEANUP_PENDING:
        return cleanup_note or "替代源已接管，正在安全清理旧任务"
    if (
        state == DownloadAttemptStatus.ACTIVE
        and no_progress_seconds is not None
        and no_progress_seconds >= 15 * 60
    ):
        return "已连续 15 分钟没有下载进度；可立即换种，30 分钟时系统会自动处理"
    return None


async def _open_downloader_adapter(session: AsyncSession, downloader_id: int):
    """按 ID 取下载器配置并构造适配器，返回 (配置行, 适配器)。404 语义统一。"""
    repository = DownloaderRepository(session)
    row = await repository.get(downloader_id)
    if row is None:
        raise NotFoundException(f"下载器不存在：id={downloader_id}")
    adapter = create_downloader(
        DownloaderConfig(
            type=row.client_type.value,
            url=row.url,
            username=row.username,
            password=repository.decrypted_password(row),
        )
    )
    return row, adapter


async def get_downloader_limits(session: AsyncSession, downloader_id: int) -> DownloaderLimits:
    """读取下载器的全局限速与任务队列上限（实时读下载器，不落库）。

    队列上限对刷流用户尤其重要：做种数撞上「最大活动种子数」后新任务会
    排队，免费窗口内可能下不完——暴露出来让用户能看到并调整。
    """
    row, adapter = await _open_downloader_adapter(session, downloader_id)
    try:
        return await adapter.get_limits()
    except DownloaderException as exc:
        raise UpstreamServiceException(exc.message) from exc
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            logger.warning("关闭下载器「%s」连接失败", row.name, exc_info=True)


async def set_downloader_limits(
    session: AsyncSession, downloader_id: int, limits: DownloaderLimits
) -> DownloaderLimits:
    """写入下载器的全局限制并回读生效值（下载器可能钳制/忽略部分字段）。"""
    row, adapter = await _open_downloader_adapter(session, downloader_id)
    try:
        await adapter.set_limits(limits)
        return await adapter.get_limits()
    except DownloaderException as exc:
        raise UpstreamServiceException(exc.message) from exc
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            logger.warning("关闭下载器「%s」连接失败", row.name, exc_info=True)


async def delete_download_task(
    session: AsyncSession,
    *,
    downloader_id: int,
    info_hash: str,
    delete_files: bool = False,
) -> None:
    """从指定下载器删除一个种子任务。

    下载器 ID 与 infohash 共同定位，避免多下载器场景误删同名任务。默认只
    移除任务；用户显式选择时才同时删除数据文件。底层删除保持幂等，因此
    用户确认期间任务恰好自行消失也返回成功。
    """
    repository = DownloaderRepository(session)
    row = await repository.get(downloader_id)
    if row is None:
        raise NotFoundException(f"下载器不存在：id={downloader_id}")

    adapter = create_downloader(
        DownloaderConfig(
            type=row.client_type.value,
            url=row.url,
            username=row.username,
            password=repository.decrypted_password(row),
        )
    )
    normalized_hash = info_hash.lower()
    try:
        await adapter.delete_torrent(normalized_hash, delete_files=delete_files)
    except DownloaderException as exc:
        logger.warning(
            "从下载器「%s」删除任务失败：hash=%s error=%s",
            row.name,
            normalized_hash,
            exc.message,
        )
        raise UpstreamServiceException(exc.message) from exc
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001 -- 关闭失败不能覆盖已经完成的删除结果
            logger.warning("关闭下载器「%s」连接失败", row.name, exc_info=True)

    logger.info(
        "已从下载器「%s」删除任务%s：hash=%s",
        row.name,
        "并删除数据文件" if delete_files else "并保留数据文件",
        normalized_hash,
    )
    # 手动意图不是救援工单。用户明确删除下载器任务后，它已不可能再靠该
    # hash 驱动监听认领，必须同步回收；否则虽然界面不再显示，数据库仍要等
    # 90 天兜底窗口才清掉孤儿锚。
    intent = (
        await session.execute(
            select(ManualDownloadIntent).where(
                ManualDownloadIntent.info_hash == normalized_hash
            )
        )
    ).scalar_one_or_none()
    changed = False
    if intent is not None:
        await session.delete(intent)
        changed = True
        logger.info("已清理被删除手动任务的身份锚：hash=%s", normalized_hash)

    attempts = list(
        (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == normalized_hash,
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
        )
        .scalars()
        .all()
    )
    for attempt in attempts:
        source = attempt
        if (
            attempt.status == DownloadAttemptStatus.TRIAL
            and attempt.replaces_attempt_id is not None
        ):
            parent = await session.get(
                SubscriptionDownloadAttempt, attempt.replaces_attempt_id
            )
            if parent is not None:
                source = parent
        target_rows = list(
            (
                await session.execute(
                    select(WantedItem).where(
                        WantedItem.subscription_id == source.subscription_id,
                        WantedItem.info_hash == source.info_hash,
                        WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                        WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    )
                )
            )
            .scalars()
            .all()
        )
        allowed = {
            (int(unit[0]), int(unit[1]))
            for unit in attempt.units
            if isinstance(unit, list) and len(unit) == 2
        }
        if allowed:
            target_rows = [
                target
                for target in target_rows
                if (target.season_number, target.episode_number) in allowed
            ]
        if target_rows:
            continue
        attempt.status = DownloadAttemptStatus.CANCELLED
        attempt.next_search_at = None
        attempt.cleanup_note = "关联单元不在当前订阅范围；用户删除下载器任务后停止观察"
        attempt.updated_at = utcnow()
        session.add(attempt)
        changed = True
    if changed:
        await session.commit()


async def _boost_relations(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """刷流台账关联：info_hash → 站点/标题/详情页。

    只取 ACTIVE——被订阅/手动接管或已汰换的任务台账已是终态，不再属于
    刷流来源（接管的会经订阅/手动关联现身，双重归属天然不存在）。
    """
    rows = (
        (
            await session.execute(
                select(RatioBoostTask).where(RatioBoostTask.state == BoostTaskState.ACTIVE)
            )
        )
        .scalars()
        .all()
    )
    boost = {
        row.info_hash.lower(): {
            "site_id": row.site_id,
            "torrent_id": row.torrent_id,
            "title": row.title,
        }
        for row in rows
    }
    pairs = {(meta["site_id"], meta["torrent_id"]) for meta in boost.values()}
    if pairs:
        detail_rows = await session.execute(
            select(SiteTorrent.site_id, SiteTorrent.torrent_id, SiteTorrent.detail_url).where(
                or_(
                    *(
                        and_(SiteTorrent.site_id == site_id, SiteTorrent.torrent_id == torrent_id)
                        for site_id, torrent_id in pairs
                    )
                )
            )
        )
        page_urls = {
            (site_id, torrent_id): detail_url
            for site_id, torrent_id, detail_url in detail_rows
            if detail_url
        }
        for meta in boost.values():
            meta["_page_url"] = page_urls.get((meta["site_id"], meta["torrent_id"]))
    return boost


async def download_task_snapshot(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """并行读取下载器，返回活跃下载与仍在业务管线内的完成/缺失任务。"""
    subscriptions, manual = await _relations(session)
    boost = await _boost_relations(session)
    repository = DownloaderRepository(session)
    downloaders = await repository.list_all()

    async def read_source(
        row: DownloaderClient,
    ) -> tuple[
        dict[str, Any],
        list[TorrentBrief],
        dict[str, tuple[DownloaderClient, TorrentStatus]],
    ]:
        assert row.id is not None
        base = {
            "id": row.id,
            "name": row.name,
            "client_type": row.client_type,
            "task_count": 0,
        }
        if not row.enabled:
            return {**base, "status": "disabled", "message": "下载器已停用"}, [], {}
        if row.status != ConfigStatus.ACTIVE:
            message = row.last_error or "下载器尚未通过连接验证"
            return {**base, "status": "unavailable", "message": message}, [], {}

        adapter = None
        completed_manual: dict[str, tuple[DownloaderClient, TorrentStatus]] = {}
        try:
            # 解密凭据和构造适配器也属于单台来源边界。某条配置损坏时应在
            # 来源区给出可读错误，而不是让其它下载器的健康任务一起消失。
            adapter = create_downloader(
                DownloaderConfig(
                    type=row.client_type.value,
                    url=row.url,
                    username=row.username,
                    password=repository.decrypted_password(row),
                )
            )
            torrents = await adapter.list_torrents()
            # 轻量列表没有保存目录/文件清单。只为“已完成但仍挂手动身份锚”的
            # 少量任务补查详情，用于兼容清理旧版本留下的残留；单条失败不影响
            # 该下载器其余任务的实时列表。
            for torrent in torrents:
                info_hash = torrent.info_hash.lower()
                if not torrent.completed or info_hash not in manual:
                    continue
                try:
                    detail = await adapter.get_torrent(info_hash)
                except Exception as exc:  # noqa: BLE001 -- 对账增强失败时保守保留任务
                    logger.warning(
                        "读取已完成手动种子的文件清单失败，暂不清理身份锚："
                        "downloader=%s hash=%s error=%s",
                        row.name,
                        info_hash,
                        exc,
                    )
                    continue
                if detail is not None:
                    completed_manual[info_hash] = (row, detail)
        except Exception:  # noqa: BLE001 -- 单台失败必须降级，不能拖垮任务中心
            logger.exception("读取下载器「%s」的任务列表失败", row.name)
            return {
                **base,
                "status": "error",
                "message": "读取任务失败，请检查下载器连接",
            }, [], {}
        finally:
            if adapter is not None:
                try:
                    await adapter.close()
                except Exception:  # noqa: BLE001 -- 关闭失败不能覆盖本次来源快照
                    logger.warning("关闭下载器「%s」连接失败", row.name, exc_info=True)
        return {**base, "status": "active", "message": None}, torrents, completed_manual

    source_results = await asyncio.gather(*(read_source(row) for row in downloaders))
    completed_details = {
        info_hash: detail
        for _source, _torrents, details in source_results
        for info_hash, detail in details.items()
    }
    consumed_manual = await _reconcile_completed_manual_intents(
        session, manual, completed_details
    )
    for info_hash in consumed_manual:
        manual.pop(info_hash, None)
    items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for row, (source_view, torrents, _details) in zip(
        downloaders, source_results, strict=True
    ):
        visible_count = 0
        for index, torrent in enumerate(torrents):
            info_hash = torrent.info_hash.lower()
            linked_subscriptions = subscriptions.get(info_hash, [])
            linked_manual = manual.get(info_hash)
            linked_boost = boost.get(info_hash)
            # 做种历史可能有上千条：任务中心只看未完成任务，以及仍等待
            # MovieClaw 入库收尾的订阅/手动任务。刷流在池任务例外——做种
            # 中的才是刷流的主体，前端把它们折叠成独立分组展示汇总。
            if (
                torrent.completed
                and not linked_subscriptions
                and not linked_manual
                and linked_boost is None
            ):
                continue
            seen_hashes.add(info_hash)
            visible_count += 1
            items.append(
                _task_dict(
                    task_id=f"{row.id}:{info_hash or index}",
                    info_hash=info_hash,
                    torrent=torrent,
                    downloader=row,
                    subscriptions=linked_subscriptions,
                    manual=linked_manual,
                    boost=linked_boost,
                )
            )
        sources.append({**source_view, "task_count": visible_count})

    # 订阅已投递、但所有可用下载器都查不到时仍需提示：巡检稍后会把工单
    # 退回重新找源。手动下载意图只是入库身份锚，不是需要救援的工单；任务
    # 被明确删除后不应凭一条孤儿锚继续在任务中心显示“缺失”。
    for info_hash in sorted(set(subscriptions) - seen_hashes):
        linked_subscriptions = subscriptions.get(info_hash, [])
        if not any(row.get("_show_when_missing", True) for row in linked_subscriptions):
            continue
        linked_manual = manual.get(info_hash)
        preferred_ids = {
            row.get("_attempt_downloader_id")
            for row in linked_subscriptions
            if row.get("_attempt_downloader_id") is not None
        }
        source_by_id = {source["id"]: source for source in sources}
        preferred_unreachable = any(
            source_by_id.get(downloader_id, {}).get("status") != "active"
            for downloader_id in preferred_ids
        )
        any_reachable = any(source["status"] == "active" for source in sources)
        items.append(
            _task_dict(
                task_id=f"missing:{info_hash}",
                info_hash=info_hash,
                torrent=None,
                downloader=None,
                subscriptions=linked_subscriptions,
                manual=linked_manual,
                absent_state=(
                    "unknown" if preferred_unreachable or not any_reachable else "missing"
                ),
            )
        )

    state_order = {
        "error": 0,
        "missing": 0,
        "stalled": 1,
        "paused": 2,
        "queued": 3,
        "checking": 3,
        "downloading": 4,
        "unknown": 5,
        "completed": 6,
    }
    items.sort(key=lambda item: (state_order.get(item["state"], 9), item["name"] or ""))
    return {"items": items, "sources": sources}
