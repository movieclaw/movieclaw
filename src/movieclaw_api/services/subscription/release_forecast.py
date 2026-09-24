"""基于既有种子索引推导追新资源发布时间，并生成临时站点探测计划。

这里刻意不建立 observation / hint 表：``site_torrent`` 已经保存了站点、种子
发布时间与首次发现时间，是不可变的原始观测；本模块只把可重算的调度结论保存到
``wanted_item.release_forecast``。站点探测是否已经执行则由
``SiteSyncCursor.last_sync_at`` 判断，普通轮询也会自然消费已经经过的探测点。

首版不用机器学习，只使用同季较早单集的发布时间：把历史单集按目标播出日期平移，
再取加权中位数。一个样本即可从 E2 开始给出宽窗口，样本增多后用滚动回测误差
收紧窗口；误差过大则标记 volatile，仅展示而不额外刷站。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.subscription.matching import (
    load_season_titles,
    publish_calendar_date,
    to_candidate,
)
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    ConfigStatus,
    MediaEpisode,
    MediaItem,
    SiteCredential,
    SiteSyncCursor,
    SiteTorrent,
    Subscription,
    SubscriptionStatus,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_matcher import MediaIdentity, TorrentCandidate, match_identity

logger = logging.getLogger("movieclaw_api.subscription.release_forecast")

FORECAST_VERSION = 1
FORECAST_MIN_INTERVAL = timedelta(minutes=15)
_OBSERVATION_LOOKBACK = timedelta(days=90)
# 种子索引分批取回的批大小（见 _load_recent_candidates）
_LOAD_CHUNK = 500
_MAX_HISTORY_EPISODES = 4
_MAX_WINDOW = timedelta(hours=12)
_EARLY_RELEASE_TOLERANCE = timedelta(days=2)
_LATE_RELEASE_TOLERANCE = timedelta(days=7)


@dataclass(frozen=True)
class _ObservedRelease:
    """一个站点对某个明确单集的发布时间观测。"""

    season_number: int
    episode_number: int
    air_date: date
    site_id: str
    torrent_row_id: int
    publish_time: datetime


@dataclass(frozen=True)
class _HistoryUnit:
    """历史单集及其跨站最早发布时间。"""

    season_number: int
    episode_number: int
    air_date: date
    publish_time: datetime


def _as_utc_text(value: datetime) -> str:
    """数据库中的 naive UTC 转为带偏移的 ISO 文本，避免前端按本地时间误读。"""
    return value.replace(tzinfo=UTC).isoformat()


def _parse_utc_text(value: object) -> datetime | None:
    """预测 JSON 时间转回数据库约定的 naive UTC；坏快照直接视为不可用。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _weighted_median(values: list[datetime]) -> datetime:
    """按历史新旧赋予 1..N 权重，取中位点；近期发布节奏对预测影响更大。"""
    weighted: list[datetime] = []
    for weight, value in enumerate(values, start=1):
        weighted.extend([value] * weight)
    return sorted(weighted)[(len(weighted) - 1) // 2]


def _project(history: list[_HistoryUnit], target_air_date: date) -> datetime:
    projected = [
        unit.publish_time + timedelta(days=(target_air_date - unit.air_date).days)
        for unit in history
    ]
    return _weighted_median(projected)


def _window_offsets(history: list[_HistoryUnit]) -> tuple[str, timedelta, timedelta]:
    """按样本量和滚动回测误差给出置信等级及预测窗口偏移。"""
    if len(history) == 1:
        return "bootstrap", timedelta(hours=-2), timedelta(hours=8)
    if len(history) == 2:
        return "growing", timedelta(minutes=-90), timedelta(hours=6)

    residuals: list[timedelta] = []
    for index in range(1, len(history)):
        current = history[index]
        predicted = _project(history[:index], current.air_date)
        residuals.append(current.publish_time - predicted)

    lower = min(min(residuals) - timedelta(minutes=30), timedelta(hours=-1))
    upper = max(max(residuals) + timedelta(minutes=30), timedelta(hours=1))
    if upper - lower > _MAX_WINDOW:
        # 发布时刻不稳定时仍保留预测供观察，但不生成额外站点探测，避免无谓刷站。
        return "volatile", timedelta(hours=-6), timedelta(hours=6)
    return "stable", lower, upper


def _probe_times(
    start: datetime, end: datetime, fractions: tuple[float, ...]
) -> list[str]:
    span = end - start
    return [_as_utc_text(start + span * fraction) for fraction in fractions]


def _first_snapshot(forecast: dict[str, Any]) -> dict[str, Any]:
    """冻结首次预测，后续可直接计算命中率而不被滚动修正覆盖。"""
    return {
        "generated_at": forecast["generated_at"],
        "predicted_at": forecast["predicted_at"],
        "window_start": forecast["window_start"],
        "window_end": forecast["window_end"],
        "confidence": forecast["confidence"],
        "sample_count": forecast["sample_count"],
    }


def _same_forecast(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """忽略生成时间与冻结副本，只比较会影响调度的预测结论。"""
    ignored = {"generated_at", "first"}
    return (
        {key: value for key, value in left.items() if key not in ignored}
        == {key: value for key, value in right.items() if key not in ignored}
    )


# 观测只用得上这几列（to_candidate 的输入 + 观测的 id/站点/发布时间）：按列取 Core 行
# 而不是装配整行 ORM 对象——同样 4 万行，装配 3 秒缩到 1 秒，垃圾回收要扫的对象也少一大截
_CANDIDATE_COLUMNS = (
    SiteTorrent.id,
    SiteTorrent.site_id,
    SiteTorrent.torrent_id,
    SiteTorrent.title,
    SiteTorrent.subtitle,
    SiteTorrent.category,
    SiteTorrent.attrs,
    SiteTorrent.imdb_id,
    SiteTorrent.douban_id,
    SiteTorrent.size_bytes,
    SiteTorrent.seeders,
    SiteTorrent.is_free,
    SiteTorrent.hit_and_run,
    SiteTorrent.download_url,
    SiteTorrent.publish_time,
)


async def _load_recent_candidates(
    session: AsyncSession, *, now: datetime
) -> list[tuple[Row, TorrentCandidate]]:
    """一次读取并解析近期种子，供本轮全部活跃剧集复用。"""
    statement = select(*_CANDIDATE_COLUMNS).where(
        SiteTorrent.publish_time.is_not(None),  # type: ignore[union-attr]
        SiteTorrent.publish_time >= now - _OBSERVATION_LOOKBACK,
    )
    # 分批取回而不是一次 .all()：几万行的装配只能在事件循环线程上做（会话不能跨
    # 线程），一口气取回会把整个循环卡住好几秒；分批之后每批之间都把控制权还给
    # 事件循环，其他请求最多只等一批的工夫（实测一次性取回卡 2 秒多，分批后不到 0.2 秒）
    rows: list[Row] = []
    result = await session.stream(statement.execution_options(yield_per=_LOAD_CHUNK))
    async for partition in result.partitions(_LOAD_CHUNK):
        rows.extend(partition)

    def _parse() -> tuple[list[tuple[Row, TorrentCandidate]], int]:
        parsed: list[tuple[Row, TorrentCandidate]] = []
        invalid = 0
        for row in rows:
            try:
                candidate = to_candidate(row)
            except ValueError:
                invalid += 1
                continue
            if candidate is not None:
                parsed.append((row, candidate))
        return parsed, invalid

    # 逐行 pydantic 校验是纯 CPU：站点同步了几个月的索引有几万行，放在事件循环
    # 里跑会把同一时刻的所有 HTTP 请求一起卡住。行已经读进内存、列都已加载，
    # 线程里只做只读属性访问，不碰会话
    candidates, invalid_attrs = await asyncio.to_thread(_parse)
    if invalid_attrs:
        logger.warning(
            "生成资源发布时间预测时跳过 %s 条属性格式异常的种子索引，请检查富化数据",
            invalid_attrs,
        )
    return candidates


def _observations_for_item(
    *,
    item: MediaItem,
    episodes: list[MediaEpisode],
    candidates: list[tuple[Row, TorrentCandidate]],
    season_titles: tuple[str, ...] = (),
) -> list[_ObservedRelease]:
    """从 site_torrent 提取本条目的明确单集观测；整季包与多集包不参与训练。"""
    season_numbers = tuple(sorted({episode.season_number for episode in episodes}))
    identity = MediaIdentity(
        kind=item.kind,
        year=item.year,
        aliases=tuple(item.aliases),
        imdb_id=item.imdb_id,
        douban_id=item.douban_id,
        season_numbers=season_numbers,
        season_titles=season_titles,
    )
    air_dates = {
        (episode.season_number, episode.episode_number): episode.air_date
        for episode in episodes
        if episode.air_date is not None
    }
    if not air_dates:
        return []

    earliest: dict[tuple[int, int, str], _ObservedRelease] = {}
    for row, candidate in candidates:
        match = match_identity(candidate, identity)
        if match is None or match.is_pack or len(match.episodes) != 1:
            continue
        season_number, episode_number = next(iter(match.episodes))
        air_date = air_dates.get((season_number, episode_number))
        if air_date is None or row.publish_time is None or row.id is None:
            continue
        # 搜索结果可能带回播出数月后的重发/洗版；它属于该集资源，却不是首发
        # 节奏样本。给跨时区提前发布留两天、站点延迟留七天，超出即排除。
        if not (
            air_date - _EARLY_RELEASE_TOLERANCE
            <= publish_calendar_date(row.publish_time)
            <= air_date + _LATE_RELEASE_TOLERANCE
        ):
            continue
        observation = _ObservedRelease(
            season_number=season_number,
            episode_number=episode_number,
            air_date=air_date,
            site_id=row.site_id,
            torrent_row_id=row.id,
            publish_time=row.publish_time,
        )
        key = (season_number, episode_number, row.site_id)
        previous = earliest.get(key)
        if previous is None or observation.publish_time < previous.publish_time:
            earliest[key] = observation

    return list(earliest.values())


def _build_forecast(
    *,
    wanted: WantedItem,
    observations: list[_ObservedRelease],
    now: datetime,
) -> dict[str, Any] | None:
    """为一个未来单集生成可解释预测；没有同季前序单集样本时返回 None。"""
    if wanted.air_date is None:
        return None

    previous = [
        observation
        for observation in observations
        if observation.season_number == wanted.season_number
        and observation.episode_number < wanted.episode_number
        and observation.air_date < wanted.air_date
    ]
    if not previous:
        return None

    by_unit: dict[tuple[int, int], list[_ObservedRelease]] = defaultdict(list)
    for observation in previous:
        by_unit[(observation.season_number, observation.episode_number)].append(observation)

    history = [
        _HistoryUnit(
            season_number=unit_observations[0].season_number,
            episode_number=unit_observations[0].episode_number,
            air_date=unit_observations[0].air_date,
            publish_time=min(item.publish_time for item in unit_observations),
        )
        for unit_observations in by_unit.values()
    ]
    history.sort(key=lambda unit: (unit.air_date, unit.episode_number))
    history = history[-_MAX_HISTORY_EPISODES:]

    predicted_at = _project(history, wanted.air_date)
    confidence, lower, upper = _window_offsets(history)
    window_start = predicted_at + lower
    window_end = predicted_at + upper
    history_keys = {(unit.season_number, unit.episode_number) for unit in history}
    selected_observations = [
        observation
        for observation in previous
        if (observation.season_number, observation.episode_number) in history_keys
    ]

    global_publish = {
        (unit.season_number, unit.episode_number): unit.publish_time for unit in history
    }
    site_history: dict[str, list[_ObservedRelease]] = defaultdict(list)
    for observation in selected_observations:
        site_history[observation.site_id].append(observation)

    latest_key = (history[-1].season_number, history[-1].episode_number)
    # 快照保留完整站点排名；读取时再从“当前启用站点”中取前两个。这样站点被
    # 停用后可自动回落到下一名，不必另建提示表或等待预测重算。
    ranked_sites = sorted(
        site_history,
        key=lambda site_id: (
            latest_key not in {
                (item.season_number, item.episode_number) for item in site_history[site_id]
            },
            -len(site_history[site_id]),
            median(
                (
                    item.publish_time
                    - global_publish[(item.season_number, item.episode_number)]
                ).total_seconds()
                for item in site_history[site_id]
            ),
            site_id,
        ),
    )

    sites: list[dict[str, Any]] = []
    for site_id in ranked_sites:
        site_observations = site_history[site_id]
        lag_seconds = median(
            (
                item.publish_time
                - global_publish[(item.season_number, item.episode_number)]
            ).total_seconds()
            for item in site_observations
        )
        site_predicted = predicted_at + timedelta(seconds=lag_seconds)
        site_start = site_predicted + lower
        site_end = site_predicted + upper
        probes: list[str] = []
        if confidence != "volatile":
            # 每站都保存完整三点；读取端从当前启用站点中选出前两名后，第二名
            # 再裁成后两点。这样排名靠前的站点停用时，下一名仍能成为首探站。
            probes = _probe_times(site_start, site_end, (0.2, 0.5, 0.8))
        sites.append(
            {
                "site_id": site_id,
                "predicted_at": _as_utc_text(site_predicted),
                "window_start": _as_utc_text(site_start),
                "window_end": _as_utc_text(site_end),
                "lag_minutes": round(lag_seconds / 60),
                "coverage_count": len(site_observations),
                "probe_times": probes,
            }
        )

    forecast: dict[str, Any] = {
        "version": FORECAST_VERSION,
        "generated_at": _as_utc_text(now),
        "target_air_date": wanted.air_date.isoformat(),
        "predicted_at": _as_utc_text(predicted_at),
        "window_start": _as_utc_text(window_start),
        "window_end": _as_utc_text(window_end),
        "confidence": confidence,
        "sample_count": len(history),
        "cadence_days": (wanted.air_date - history[-1].air_date).days,
        "basis_units": [
            [unit.season_number, unit.episode_number] for unit in history
        ],
        "basis_torrent_row_ids": sorted(
            {observation.torrent_row_id for observation in selected_observations}
        ),
        "sites": sites,
    }
    existing = wanted.release_forecast
    if (
        isinstance(existing, dict)
        and existing.get("version") == FORECAST_VERSION
        and existing.get("target_air_date") == wanted.air_date.isoformat()
        and isinstance(existing.get("first"), dict)
    ):
        forecast["first"] = existing["first"]
    else:
        forecast["first"] = _first_snapshot(forecast)
    return forecast


async def refresh_release_forecasts(
    session: AsyncSession, *, media_item_ids: set[int] | None = None
) -> int:
    """刷新活跃剧集订阅范围内的单集预测快照，返回实际变化的工单数。

    只处理 ``wanted`` 状态且已定档的剧集工单；暂停、完成、已投递工单不会生成
    新探测。``in_scope`` 已表达用户是否订阅该集，自动续订只影响未来范围扩张，
    不得影响现有范围的预测与探测。已过期工单的旧快照无需删除，调度读取时会按
    窗口截止时间忽略。
    """
    statement = (
        select(WantedItem, Subscription)
        .join(Subscription, WantedItem.subscription_id == Subscription.id)  # type: ignore[arg-type]
        .where(
            WantedItem.status == WantedStatus.WANTED,
            WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            WantedItem.air_date.is_not(None),  # type: ignore[union-attr]
            Subscription.kind == "tv",
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )
    if media_item_ids is not None:
        if not media_item_ids:
            return 0
        statement = statement.where(WantedItem.media_item_id.in_(media_item_ids))  # type: ignore[union-attr]

    rows = (await session.execute(statement)).all()
    if not rows:
        return 0

    now = utcnow()
    today = now.date()
    grouped: dict[int, list[WantedItem]] = defaultdict(list)
    for wanted, _subscription in rows:
        # 昨日之前的窗口必然已经结束，不再花代价重算；旧快照会被读取端忽略。
        if wanted.air_date is not None and wanted.air_date >= today - timedelta(days=1):
            grouped[wanted.media_item_id].append(wanted)

    if not grouped:
        return 0
    changed = 0
    candidates = await _load_recent_candidates(session, now=now)
    for media_item_id, wanted_items in grouped.items():
        item = await session.get(MediaItem, media_item_id)
        if item is None:
            continue
        episodes = list(
            (
                await session.execute(
                    select(MediaEpisode).where(MediaEpisode.media_item_id == media_item_id)
                )
            )
            .scalars()
            .all()
        )
        # 身份匹配同样是纯 CPU（每个候选跑一遍 match_identity），与解析同理进线程
        observations = await asyncio.to_thread(
            _observations_for_item,
            item=item,
            episodes=episodes,
            candidates=candidates,
            season_titles=await load_season_titles(session, media_item_id),
        )
        for wanted in wanted_items:
            forecast = _build_forecast(wanted=wanted, observations=observations, now=now)
            existing = wanted.release_forecast
            if forecast is None:
                if existing is not None:
                    wanted.release_forecast = None
                    wanted.updated_at = now
                    session.add(wanted)
                    changed += 1
                continue
            if isinstance(existing, dict) and _same_forecast(existing, forecast):
                continue
            wanted.release_forecast = forecast
            wanted.updated_at = now
            session.add(wanted)
            changed += 1

    if changed:
        await session.commit()
        logger.info("已更新 %s 个订阅单集的资源发布时间预测", changed)
    return changed


# fire-and-forget 任务的强引用集合：asyncio 只持弱引用，不留强引用的话
# 任务可能在执行前被垃圾回收（与 wanted_search._kick_tasks 同一套路）。
_refresh_tasks: set[asyncio.Task] = set()
# 排队中（还没开始执行）的条目：同一条目连续触发（创建→调整→恢复）只跑一次。
# 开始执行后就从这里移除——执行中再触发要重新排一次，本次可能读不到之后的变化
_queued_ids: set[int] = set()
# 串行执行：每次刷新都要把整个索引解析一遍，两份并行只会让事件循环更挤，不会更快
_refresh_lock = asyncio.Lock()


async def _refresh_once(media_item_ids: set[int]) -> None:
    """后台刷新的执行体：自开会话，失败只记日志（定时任务会兜底全量重算）。"""
    async with _refresh_lock:
        _queued_ids.difference_update(media_item_ids)
        try:
            async with get_database().session() as session:
                await refresh_release_forecasts(session, media_item_ids=media_item_ids)
        except Exception:  # noqa: BLE001 -- 环境未就绪（如关停中）时静默降级
            logger.warning(
                "后台刷新条目 %s 的资源发布时间预测失败，等待定时任务兜底",
                sorted(media_item_ids),
                exc_info=True,
            )


def refresh_release_forecasts_soon(media_item_ids: set[int]) -> None:
    """把预测刷新挪出请求路径（订阅创建/调整/恢复共用的唯一触发点）。

    预测快照是可重算的派生值，晚几秒落库对功能没有影响：首页预告在快照缺席时
    按播出日期兜底，站点探测计划也只在到点时才读取它。而同步刷新要把近 90 天的
    全部种子索引读出来逐行解析、匹配，索引一大就是秒级——用户点一下「订阅」
    没有理由等这个。请求会话在响应后即关闭，所以这里必须自开会话。
    没有运行中的事件循环时（如同步脚本）静默跳过，交给定时任务兜底。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    new_ids = set(media_item_ids) - _queued_ids
    if not new_ids:
        return  # 同条目的刷新已在排队，合并进那一次
    _queued_ids.update(new_ids)
    task = loop.create_task(_refresh_once(new_ids))
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)


def _forecast_site_probes(
    wanted: WantedItem, *, site_ids: set[str], now: datetime
) -> dict[str, list[datetime]]:
    """读取一个工单在当前有效站点上的原始探测点，保持与调度器相同的站点取舍。"""
    forecast = wanted.release_forecast
    if not isinstance(forecast, dict):
        return {}
    if forecast.get("version") != FORECAST_VERSION:
        return {}
    if wanted.air_date is None or forecast.get("target_air_date") != wanted.air_date.isoformat():
        return {}
    if forecast.get("confidence") == "volatile":
        return {}
    window_end = _parse_utc_text(forecast.get("window_end"))
    if window_end is None or window_end < now:
        return {}
    sites = forecast.get("sites")
    if not isinstance(sites, list):
        return {}

    result: dict[str, list[datetime]] = {}
    eligible_sites = [
        site
        for site in sites
        if isinstance(site, dict) and site.get("site_id") in site_ids
    ][:2]
    for rank, site in enumerate(eligible_sites):
        site_id = str(site["site_id"])
        probes = site.get("probe_times")
        if not isinstance(probes, list):
            continue
        selected_probes = probes if rank == 0 else probes[-2:]
        parsed = [probe for value in selected_probes if (probe := _parse_utc_text(value))]
        if parsed:
            result[site_id] = sorted(set(parsed))
    return result


def effective_forecast_probe_at(
    probes: list[datetime], *, last_sync_at: datetime | None
) -> datetime | None:
    """把原始预测点换算成站点真正可执行的下一探测时间。

    上次同步之后的探测点才算未消费；即使预测点已到，也必须遵守单站 15 分钟
    礼貌间隔。调度器与页面共用本函数，避免两边分别计算后发生时间漂移。
    """
    pending = sorted(
        probe for probe in probes if last_sync_at is None or probe > last_sync_at
    )
    if not pending:
        return None
    if last_sync_at is None:
        return pending[0]
    return max(pending[0], last_sync_at + FORECAST_MIN_INTERVAL)


async def forecast_probe_times_by_site(
    session: AsyncSession, *, site_ids: set[str]
) -> dict[str, list[datetime]]:
    """读取当前有效的站点探测点；返回值可直接合并到既有同步规划器。

    这里只筛选业务状态与快照有效性，不记录“已消费”状态。同步规划器用站点游标的
    ``last_sync_at`` 跳过旧探测点，因此一次普通同步同样能够消费它们。是否自动续订
    与这里无关：只要工单仍在当前订阅范围内，就必须继续探测。
    """
    if not site_ids:
        return {}
    rows = await session.execute(
        select(WantedItem)
        .join(Subscription, WantedItem.subscription_id == Subscription.id)  # type: ignore[arg-type]
        .where(
            WantedItem.status == WantedStatus.WANTED,
            WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
            WantedItem.release_forecast.is_not(None),  # type: ignore[union-attr]
            Subscription.kind == "tv",
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )
    now = utcnow()
    result: dict[str, set[datetime]] = defaultdict(set)
    for wanted in rows.scalars().all():
        for site_id, probes in _forecast_site_probes(
            wanted, site_ids=site_ids, now=now
        ).items():
            result[site_id].update(probes)
    return {site_id: sorted(values) for site_id, values in result.items()}


async def next_forecast_probe_times_by_wanted(
    session: AsyncSession, *, wanted_items: list[WantedItem]
) -> dict[int, datetime]:
    """按工单返回与调度器一致的下一有效探测时间，供首页计算预计入库时间。"""
    if not wanted_items:
        return {}
    credentials = (
        (
            await session.execute(
                select(SiteCredential).where(
                    SiteCredential.enabled.is_(True),  # type: ignore[attr-defined]
                    SiteCredential.status == ConfigStatus.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    active_site_ids = {credential.site_id for credential in credentials}
    if not active_site_ids:
        return {}
    cursors = (
        (
            await session.execute(
                select(SiteSyncCursor).where(
                    SiteSyncCursor.site_id.in_(active_site_ids)  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    cursor_by_site = {cursor.site_id: cursor for cursor in cursors}
    now = utcnow()
    result: dict[int, datetime] = {}
    for wanted in wanted_items:
        if wanted.id is None:
            continue
        effective: list[datetime] = []
        for site_id, probes in _forecast_site_probes(
            wanted, site_ids=active_site_ids, now=now
        ).items():
            cursor = cursor_by_site.get(site_id)
            due_at = effective_forecast_probe_at(
                probes,
                last_sync_at=cursor.last_sync_at if cursor is not None else None,
            )
            if due_at is not None:
                effective.append(due_at)
        if effective:
            # 已到期但尚未等到下一轮全局 tick 时，从“现在”起估算，不回传过去时间。
            result[wanted.id] = max(min(effective), now)
    return result


__all__ = [
    "FORECAST_MIN_INTERVAL",
    "FORECAST_VERSION",
    "effective_forecast_probe_at",
    "forecast_probe_times_by_site",
    "next_forecast_probe_times_by_wanted",
    "refresh_release_forecasts",
]
