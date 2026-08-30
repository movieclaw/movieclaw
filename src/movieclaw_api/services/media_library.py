"""媒体身份层的落库编排：任何入口的订阅先在这里收敛成统一媒体条目。

分工（docs/design/subscription.md 第 1 节）：
- ``movieclaw_media.library``：纯 TMDB 拉取与收敛判定（不碰数据库）；
- ``MediaItemRepository``：原始存取；
- 本服务：把两者接起来——建档幂等、豆瓣入口收敛、来源信息回填。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.scrape_config import ITEM_SCOPED_OVERRIDABLE, merge_for_library
from movieclaw_api.settings import MetadataScrapeSetting
from movieclaw_db.models import (
    Library,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    MediaSeason,
    utcnow,
)
from movieclaw_db.repositories import MediaItemRepository
from movieclaw_media.library import (
    DoubanResolution,
    MediaProfile,
    ResolveStatus,
    fetch_media_profile,
    resolve_douban_to_tmdb,
)
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

logger = logging.getLogger("movieclaw_api.media_library")


class MediaLibraryService:
    """媒体条目服务：``ensure_media_item`` 是订阅创建链路的第一步。"""

    def __init__(
        self,
        session: AsyncSession,
        tmdb_client: TmdbClient,
        *,
        languages: Sequence[str] | None = None,
        scrape_library_id: int | None = None,
    ) -> None:
        self._session = session
        self._repo = MediaItemRepository(session)
        self._client = tmdb_client
        # 本实例服务于哪个库：扫描、整库重识别这类"整个过程都围着一个库转"
        # 的链路在构造期给一次，就不必把 library_id 顺着四层识别函数往下传。
        # ``ensure_media_item`` 的显式入参优先级更高（设计文档 §14）
        self._scrape_library_id = scrape_library_id
        # None = 跟随「设置 → 刮削与整理」的语言优先级（读取时取快照，
        # 设置保存立即生效）；显式传入仅供测试固定语言
        self._languages = list(languages) if languages else None
        # 预取档案缓存：(kind, tmdb_id) -> 已经拉回来的 TMDB 档案。
        # 扫描器会在处理每一窗文件前并发填充它（见 library_scan._prefetch_profiles），
        # 建档时命中即省掉一次 TMDB 往返。取用即弹出，不做长期缓存——
        # 档案很大，且"元数据保鲜"是刷新任务的职责，不该有第二份陈旧副本
        self.profile_cache: dict[tuple[MediaKind, int], MediaProfile] = {}

    async def ensure_media_item(
        self,
        kind: MediaKind,
        tmdb_id: int,
        *,
        douban_id: str | None = None,
        extra_aliases: Sequence[str] = (),
        library_id: int | None = None,
    ) -> MediaItem:
        """按锚建档或复用媒体条目（幂等）。

        - 已存在：不重复请求 TMDB（元数据保鲜是刷新任务的职责），只回填
          调用方带来的新信息（douban_id、入口标题等别名、刮削归属库）；
        - 不存在：一次拉齐 TMDB 档案落库——身份信息（media_item + 季集）
          与展示元数据（media_metadata / media_episode）同一事务写入，
          这就是"一次入库刮削"的文本部分（图片资产由 ensure_assets 补齐，
          docs/design/metadata.md 4.1）。

        ``library_id`` 是条目的**刮削归属库**（设计文档 §14）：入库、扫描、
        人工认领、手动下载四条链路本就握着目标库，传下来首次刮削就用对
        该库的语言/选图设置，不必等下一轮刷新才变脸。不传（如订阅弹层打开
        时用户还没选库）则留空，由读取端惰性推断。
        """
        library = await self._scrape_library(
            kind, library_id if library_id is not None else self._scrape_library_id
        )
        setting = merge_for_library(library, fields=ITEM_SCOPED_OVERRIDABLE)

        existing = await self._repo.get_by_anchor(kind.value, tmdb_id)
        if existing is not None:
            self.profile_cache.pop((kind, tmdb_id), None)  # 用不上了，别占内存
            return await self._backfill(existing, douban_id, extra_aliases, library)

        profile = self.profile_cache.pop((kind, tmdb_id), None)
        if profile is None:
            profile = await fetch_media_profile(
                self._client, kind, tmdb_id, **self._fetch_kwargs(setting)
            )
        item, seasons, episodes, metadata = self._to_rows(
            profile, douban_id, extra_aliases, setting
        )
        if library is not None:
            item.scrape_library_id = library.id
        try:
            item = await self._repo.create_with_seasons(item, seasons, episodes, metadata)
        except IntegrityError:
            # 并发建档撞唯一锚：让出胜者，读回对方落库的条目
            await self._repo.rollback()
            logger.info("媒体条目并发建档冲突，复用已有条目：%s/%s", kind.value, tmdb_id)
            existing = await self._repo.get_by_anchor(kind.value, tmdb_id)
            if existing is None:  # 理论不可达：冲突意味着对方已提交
                raise
            return await self._backfill(existing, douban_id, extra_aliases, library)
        # 影人关系要等 media_item.id 落库后才能建（人物页的数据来源）。
        # 与刷新路径（media_scrape.apply_display_profile）同一个函数，口径一致。
        #
        # 必须自己 commit：create_with_seasons 已经提交过，这里写的是**新事务**，
        # 而 get_session 明确不提交（事务边界交给上层）。调用方里只有扫描链路
        # 后续恰好会提交，订阅 prepare 那条只读不提交——不自己收口的话，
        # 通过订阅建档的条目会静默丢掉全部影人数据。
        from movieclaw_api.services.media_scrape import apply_people_credits

        if item.id is not None:
            await apply_people_credits(self._session, item.id, profile)
            await self._session.commit()
        logger.info(
            "媒体条目已建档：%s/%s《%s》(%s)，别名 %d 个，季 %d 个",
            kind.value,
            tmdb_id,
            item.title,
            item.year or "年份未知",
            len(item.aliases),
            len(seasons),
        )
        return item

    async def prefetch_profile(self, kind: MediaKind, tmdb_id: int) -> None:
        """把一份 TMDB 档案提前拉进缓存，供随后的 ``ensure_media_item`` 直接取用。

        扫描器用它把"每部新片串行干等一次往返"变成"每窗并发拉一批"
        （见 library_scan._prefetch_profiles）。纯属加速，失败一律吞掉——
        没预取到就等建档时自己去拉，那里才有完整的错误分类与用户可读文案。
        """
        key = (kind, tmdb_id)
        if key in self.profile_cache:
            return
        try:
            self.profile_cache[key] = await fetch_media_profile(
                self._client, kind, tmdb_id, **self._fetch_kwargs()
            )
        except Exception as exc:  # noqa: BLE001 -- 预取失败不是错误，建档时会重来
            logger.debug("TMDB 档案预取失败（改由建档时重试）：%s/%s：%s", kind.value, tmdb_id, exc)

    async def runtime_minutes(self, media_item_id: int) -> int | None:
        """条目档案里的片长（电影）/单集常规时长（剧集），分钟；未知为 None。

        识别链用它校验钉死身份：NFO 声明的条目片长与文件实测时长悬殊时是
        "声明指向了另一部作品"的硬证据（见 library_scan._pinned_mismatch）。
        """
        row = (
            await self._session.execute(
                select(MediaMetadata.runtime_minutes).where(
                    MediaMetadata.media_item_id == media_item_id
                )
            )
        ).scalar_one_or_none()
        return row

    async def resolve_douban(
        self,
        kind: MediaKind,
        title: str,
        *,
        year: int | None = None,
        douban_id: str | None = None,
        aliases: Sequence[str] = (),
        released: str = "",
    ) -> tuple[DoubanResolution, MediaItem | None]:
        """豆瓣入口收敛：命中即建档并回填豆瓣身份，歧义/未找到交给调用方处理。

        返回 (收敛结果, 条目)；仅 status=MATCHED 时条目非 None。
        歧义时由前端弹层让用户从 candidates 里确认，确认后走
        ``ensure_media_item(kind, tmdb_id, douban_id=..., extra_aliases=[豆瓣标题])``。

        ``aliases``（豆瓣又名）与 ``released``（豆瓣首播/上映日期串）是收敛的
        证据来源，调用方拿到豆瓣详情后应一并传入——缺省时只走标题+年份通路，
        豆瓣的季条目（「中餐厅 第十季」）会因此收敛不到。
        """
        resolution = await resolve_douban_to_tmdb(
            self._client,
            kind,
            title,
            year=year,
            language=self._primary_language(),
            aliases=aliases,
            released=released,
        )
        if resolution.status is not ResolveStatus.MATCHED:
            return resolution, None
        assert resolution.tmdb_id is not None
        item = await self.ensure_media_item(
            kind,
            resolution.tmdb_id,
            douban_id=douban_id,
            extra_aliases=[title],
        )
        return resolution, item

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _backfill(
        self,
        item: MediaItem,
        douban_id: str | None,
        extra_aliases: Sequence[str],
        library: Library | None = None,
    ) -> MediaItem:
        """把调用方带来的新信息合并进已有条目；无变化则不产生写。"""
        changed = False
        if douban_id and not item.douban_id:
            item.douban_id = douban_id
            changed = True
        # 刮削归属库只**补空不改判**：条目已有归属（推断固化或用户手动指定）
        # 时不动它——同一条目进第二个库，不该悄悄换掉它的刮削口味
        if library is not None and item.scrape_library_id is None:
            item.scrape_library_id = library.id
            changed = True
        merged = self._merge_aliases(item.aliases, extra_aliases)
        if merged is not None:
            item.aliases = merged
            changed = True
        return await self._repo.save(item) if changed else item

    @staticmethod
    def _merge_aliases(current: list, extra: Sequence[str]) -> list | None:
        """追加新别名（保序精确去重）；没有新增返回 None。"""
        seen = set(current)
        additions = []
        for text in extra:
            cleaned = text.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                additions.append(cleaned)
        return [*current, *additions] if additions else None

    def _to_rows(
        self,
        profile: MediaProfile,
        douban_id: str | None,
        extra_aliases: Sequence[str],
        setting: MetadataScrapeSetting | None = None,
    ) -> tuple[MediaItem, list[MediaSeason], list[MediaEpisode], MediaMetadata]:
        """传输模型 → ORM 行（身份 + 展示层）。入口带来的别名合并进别名集合。"""
        from movieclaw_api.services.media_scrape import build_display_rows

        aliases = list(profile.aliases)
        merged = self._merge_aliases(aliases, extra_aliases)
        if merged is not None:
            aliases = merged
        item = MediaItem(
            kind=profile.kind.value,
            tmdb_id=profile.tmdb_id,
            imdb_id=profile.imdb_id,
            douban_id=douban_id,
            title=profile.title,
            original_title=profile.original_title,
            year=profile.year,
            aliases=aliases,
            status=profile.status,
            poster_path=profile.poster_path,
            backdrop_path=profile.backdrop_path,
            metadata_refreshed_at=utcnow(),
            # NULL=立即到期：刷新任务首个 tick 会处理并按 status 分档重排
            next_refresh_at=None,
        )
        seasons, episodes, metadata = build_display_rows(profile, self._primary_language(setting))
        return item, seasons, episodes, metadata

    def _fetch_kwargs(self, setting: MetadataScrapeSetting | None = None) -> dict:
        """``fetch_media_profile`` 的偏好参数：显式语言（测试）或生效设置。

        ``setting`` 由归属库合并而来（见 ``ensure_media_item``）；不传则全局口径。
        """
        if self._languages is not None:
            return {"languages": self._languages}
        from movieclaw_api.services.scrape_config import profile_fetch_kwargs

        return profile_fetch_kwargs(setting)

    def _primary_language(self, setting: MetadataScrapeSetting | None = None) -> str:
        return self._fetch_kwargs(setting)["languages"][0]

    async def _scrape_library(self, kind: MediaKind, library_id: int | None) -> Library | None:
        """归属库行；库不存在或类型不符时按"没有归属"处理（绝不拿类型不对的
        库的设置去刮），不报错——调用方给的库 id 只是个偏好提示。"""
        if library_id is None:
            return None
        library = await self._session.get(Library, library_id)
        if library is None or library.kind != kind.value:
            return None
        return library
