"""订阅首页「刚刚入库」：最近整理入库、而当前账号还没看完的订阅内容。

这一块回答订阅的回报时刻——**"我追的东西到了，现在就能看"**。它和媒体库首页的
「接下来继续」（``services/playback_up_next.py``）是一对：那边从**看过的**单元往后
找下一集，这边从**刚到的**一批里找第一集还没看完的。两边"看完"的口径相同，只认
``playback_state.played``；"能看"的口径也相同，只认在位、且对当前身份可见的库文件。

规则只有三条：

- 取当前身份可见订阅里、窗口期内（默认 7 天）整理入库的工单（``imported_at``），
  同一部作品只出一张卡——整季包一次到 8 集，首页也只占一个位置；
- 这一批里按季集正序，第一个**没看完**且**文件在位**的单元就是卡片的播放入口；
  整批都看完了就不出卡：它已经不"新"了，下一集入库时会自己回来；
- 卡片按这一批最近一次入库的时间倒序。

窗口取 7 天而不是 48 小时：周更剧一周只到一集，窗口短于更新周期时，用户隔两天
打开 App 就看不到那一集了；而看完即消失的规则保证了窗口放宽不会让首页变旧。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.media_scrape import asset_version

# 「看完了吗 / 文件在不在位」与接下来继续同一套判定，直接复用那边的批量查询，
# 不另写第二份口径
from movieclaw_api.services.playback_up_next import (
    _in_place_units,
    _progress_percent,
    _runtime_ms,
    _states,
)
from movieclaw_db.models import (
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    Subscription,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_media.models import MediaKind

#: 季集单元：``(季, 集)``，电影是哨兵 ``(0, 0)``
Unit = tuple[int, int]


@dataclass(frozen=True)
class RecentArrival:
    """一张「刚刚入库」卡片的领域结果（路由层再组装成视图）。"""

    subscription: Subscription
    media: MediaItem
    #: 这一批里还没看完、文件在位的单元（季集正序）；第一个就是播放入口
    units: list[Unit]
    #: 这一批最近一次整理入库的时间
    imported_at: datetime
    #: 播放入口那一集的集名 / 剧照（电影与缺档案时为 None）
    episode_name: str | None
    still_url: str | None
    #: 播放入口看了一半时的进度（1~99）；没看过为 None
    progress_percent: int | None

    @property
    def display(self) -> Unit:
        return self.units[0]


async def recent_arrivals(
    session: AsyncSession,
    *,
    subscriptions: list[tuple[Subscription, MediaItem]],
    member_id: int,
    visible_library_ids: set[int] | None,
    days: int = 7,
    limit: int = 12,
    now: datetime | None = None,
) -> list[RecentArrival]:
    """当前身份「刚刚入库」的卡片，最近到的在前。

    ``subscriptions`` 由调用方按订阅可见边界给出（成员 = 自己发起 + 自己关注，
    管理员 = 全部），本函数不再做订阅级权限判断；``member_id`` 只用于读观看进度。
    """
    by_subscription = {sub.id: (sub, media) for sub, media in subscriptions if sub.id is not None}
    if not by_subscription or visible_library_ids == set():
        return []
    cutoff = (now or utcnow()) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                WantedItem.subscription_id,
                WantedItem.season_number,
                WantedItem.episode_number,
                WantedItem.imported_at,
            ).where(
                WantedItem.subscription_id.in_(list(by_subscription)),  # type: ignore[attr-defined]
                WantedItem.status == WantedStatus.IMPORTED,
                # 移出期望集合的单元只留作历史，用户已经表态不要它了
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                WantedItem.imported_at >= cutoff,  # type: ignore[operator]
            )
        )
    ).all()
    batches: dict[int, dict[Unit, datetime]] = {}
    for subscription_id, season, episode, imported_at in rows:
        if imported_at is None:
            continue
        batch = batches.setdefault(int(subscription_id), {})
        batch[(int(season), int(episode))] = imported_at
    if not batches:
        return []

    item_ids = sorted({by_subscription[sid][1].id for sid in batches if by_subscription[sid][1].id})
    available = await _in_place_units(session, item_ids, visible_library_ids)
    states = await _states(session, member_id, item_ids)

    picks: list[tuple[Subscription, MediaItem, list[Unit], datetime]] = []
    for subscription_id, batch in batches.items():
        subscription, media = by_subscription[subscription_id]
        if media.id is None:
            continue
        in_place = available.get(media.id, {})
        pending = sorted(
            unit
            for unit in batch
            if unit in in_place and not states.get((media.id, unit), (False, 0))[0]
        )
        if pending:
            picks.append((subscription, media, pending, max(batch.values())))
    picks.sort(key=lambda pick: (pick[3], pick[1].id or 0), reverse=True)
    picks = picks[:limit]
    if not picks:
        return []
    return await _hydrate(session, picks, states)


async def _hydrate(
    session: AsyncSession,
    picks: list[tuple[Subscription, MediaItem, list[Unit], datetime]],
    states: dict[tuple[int, Unit], tuple[bool, int]],
) -> list[RecentArrival]:
    """补齐播放入口的集名、剧照与进度：各批一次查询，不按卡片逐张往返。"""
    item_ids = [media.id for _sub, media, _units, _at in picks if media.id is not None]
    targets = {(media.id, units[0]) for _sub, media, units, _at in picks}
    episodes = {
        (int(i), (int(s), int(e))): (name, runtime, still_file, still_path)
        for i, s, e, name, runtime, still_file, still_path in (
            await session.execute(
                select(
                    MediaEpisode.media_item_id,
                    MediaEpisode.season_number,
                    MediaEpisode.episode_number,
                    MediaEpisode.name,
                    MediaEpisode.runtime_minutes,
                    MediaEpisode.still_file,
                    MediaEpisode.still_path,
                ).where(MediaEpisode.media_item_id.in_(item_ids))  # type: ignore[attr-defined]
            )
        ).all()
        if (int(i), (int(s), int(e))) in targets
    }
    durations = {
        (int(i), (int(s), int(e))): int(d or 0)
        for i, s, e, d in (
            await session.execute(
                select(
                    LibraryFile.media_item_id,
                    LibraryFile.season_number,
                    LibraryFile.episode_number,
                    func.max(LibraryFile.duration_seconds),
                )
                .where(
                    LibraryFile.media_item_id.in_(item_ids),  # type: ignore[attr-defined]
                    LibraryFile.in_place(),
                )
                .group_by(
                    LibraryFile.media_item_id,
                    LibraryFile.season_number,
                    LibraryFile.episode_number,
                )
            )
        ).all()
    }
    item_runtimes = {
        int(i): runtime
        for i, runtime in (
            await session.execute(
                select(MediaMetadata.media_item_id, MediaMetadata.runtime_minutes).where(
                    MediaMetadata.media_item_id.in_(item_ids)  # type: ignore[attr-defined]
                )
            )
        ).all()
    }

    image_base = get_settings().tmdb_image_base_url.rstrip("/")
    result: list[RecentArrival] = []
    for subscription, media, units, imported_at in picks:
        assert media.id is not None
        display = units[0]
        is_tv = media.kind == MediaKind.TV.value
        name, episode_runtime, still_file, still_path = episodes.get(
            (media.id, display), (None, None, None, None)
        )
        still_url = None
        if is_tv:
            # 本地剧照资产优先（刮削落盘过的图不再绕 TMDB），其次 TMDB 原图路径
            if still_file:
                still_url = f"/images/assets/{still_file}?v={asset_version(still_file)}"
            elif still_path:
                still_url = f"{image_base}/w780{still_path}"
        position_ms = states.get((media.id, display), (False, 0))[1]
        duration_ms = _runtime_ms(
            durations.get((media.id, display)),
            episode_runtime if is_tv else None,
            item_runtimes.get(media.id),
        )
        result.append(
            RecentArrival(
                subscription=subscription,
                media=media,
                units=units,
                imported_at=imported_at,
                episode_name=(name or None) if is_tv else None,
                still_url=still_url,
                progress_percent=_progress_percent(position_ms, duration_ms),
            )
        )
    return result
