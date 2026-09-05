from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.media_item import MediaItem, MediaSeason, MediaSource
from movieclaw_db.models.media_metadata import MediaEpisode, MediaMetadata


class MediaItemRepository:
    """媒体条目（``media_item`` / ``media_season`` / ``media_episode`` /
    ``media_metadata``）的数据访问层。

    职责边界：只做按锚存取与整体落库，不理解 TMDB 数据结构、不做收敛判定——
    这些语义在 ``movieclaw_api.services.media_library`` 与 ``movieclaw_media.library``。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_anchor(self, kind: str, tmdb_id: int) -> MediaItem | None:
        """按 TMDB 锚 (kind, tmdb_id) 读取条目；不存在返回 None。

        是 ``get_by_external`` 的 TMDB 便捷形式；本地条目没有 tmdb_id，
        按 ``(source, kind, external_id)`` 走 ``get_by_external``。
        """
        return await self.get_by_external(MediaSource.TMDB, kind, str(tmdb_id))

    async def get_by_external(self, source: str, kind: str, external_id: str) -> MediaItem | None:
        """按唯一锚 (source, kind, external_id) 读取条目；不存在返回 None。"""
        result = await self._session.execute(
            select(MediaItem).where(
                MediaItem.source == source,
                MediaItem.kind == kind,
                MediaItem.external_id == external_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_seasons(self, media_item_id: int) -> list[MediaSeason]:
        """返回条目的全部季，按季号升序（特别季 0 在最前）。"""
        result = await self._session.execute(
            select(MediaSeason)
            .where(MediaSeason.media_item_id == media_item_id)
            .order_by(MediaSeason.season_number)
        )
        return list(result.scalars().all())

    async def list_seasons_many(self, media_item_ids: list[int]) -> dict[int, list[MediaSeason]]:
        """批量返回多个条目的季骨架，供海报墙统计使用，避免逐订阅查询。"""
        if not media_item_ids:
            return {}
        result = await self._session.execute(
            select(MediaSeason)
            .where(MediaSeason.media_item_id.in_(media_item_ids))  # type: ignore[attr-defined]
            .order_by(MediaSeason.media_item_id, MediaSeason.season_number)
        )
        grouped: dict[int, list[MediaSeason]] = {}
        for season in result.scalars().all():
            grouped.setdefault(season.media_item_id, []).append(season)
        return grouped

    async def aired_units_many(
        self, media_item_ids: list[int], *, include_specials: bool = False
    ) -> dict[int, set[tuple[int, int]]]:
        """批量返回各条目**已播出**的 (季, 集) 单元集合（口径的唯一实现）。

        已播 = air_date 非空且不晚于今天。订阅预检的"已播 x 集"与海报墙的
        缺集统计（已播 − 在位 = 缺）都必须经本方法取数，不允许各自手写查询。
        ``include_specials=False`` 时排除特别季（season 0）——缺集统计与
        订阅默认口径一致；预检的季选择器要显示特别季，传 True。

        只取三列，不整行取 ORM 对象：分集行带着剧照路径与简介，整行取等于
        把整库的分集文案都读进内存，而这里只是在数「哪些集已经播了」。
        """
        if not media_item_ids:
            return {}
        statement = select(
            MediaEpisode.media_item_id,
            MediaEpisode.season_number,
            MediaEpisode.episode_number,
        ).where(
            MediaEpisode.media_item_id.in_(media_item_ids),  # type: ignore[attr-defined]
            MediaEpisode.air_date.is_not(None),  # type: ignore[union-attr]
            MediaEpisode.air_date <= utcnow().date(),
        )
        if not include_specials:
            statement = statement.where(MediaEpisode.season_number != 0)
        result = await self._session.execute(statement)
        aired: dict[int, set[tuple[int, int]]] = {}
        for media_item_id, season_number, episode_number in result.all():
            aired.setdefault(media_item_id, set()).add((season_number, episode_number))
        return aired

    async def list_episodes(
        self, media_item_id: int, season_number: int | None = None
    ) -> list[MediaEpisode]:
        """返回条目的集（可限定一季），按 (季号, 集号) 升序。"""
        statement = select(MediaEpisode).where(MediaEpisode.media_item_id == media_item_id)
        if season_number is not None:
            statement = statement.where(MediaEpisode.season_number == season_number)
        statement = statement.order_by(
            MediaEpisode.season_number, MediaEpisode.episode_number  # type: ignore[arg-type]
        )
        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def get_metadata(self, media_item_id: int) -> MediaMetadata | None:
        """读取条目的展示元数据档案；未刮削过返回 None。"""
        result = await self._session.execute(
            select(MediaMetadata).where(MediaMetadata.media_item_id == media_item_id)
        )
        return result.scalar_one_or_none()

    async def create_with_seasons(
        self,
        item: MediaItem,
        seasons: list[MediaSeason],
        episodes: list[MediaEpisode] | None = None,
        metadata: MediaMetadata | None = None,
    ) -> MediaItem:
        """建档：条目、季、集与展示档案一次事务落库，返回带 id 的条目。"""
        self._session.add(item)
        await self._session.flush()  # 先拿到 item.id 供子表行引用
        for season in seasons:
            season.media_item_id = item.id
            self._session.add(season)
        for episode in episodes or []:
            episode.media_item_id = item.id
            self._session.add(episode)
        if metadata is not None:
            metadata.media_item_id = item.id
            self._session.add(metadata)
        # 提交后不 refresh：会话是 expire_on_commit=False，提交不会让属性过期，
        # 而全部列（含 id 与时间戳）都是应用侧赋的值，没有库端默认值要读回来。
        # 每建一档白搭一次全表列的 SELECT，首次扫描上千部片就是上千次
        await self._session.commit()
        return item

    async def rollback(self) -> None:
        """回滚当前事务（并发建档撞唯一锚后，会话须先回滚才能继续查询）。"""
        await self._session.rollback()

    async def save(self, item: MediaItem) -> MediaItem:
        """保存对已加载条目的修改（如回填 douban_id / 合并别名）。"""
        item.updated_at = utcnow()
        self._session.add(item)
        await self._session.commit()
        return item
