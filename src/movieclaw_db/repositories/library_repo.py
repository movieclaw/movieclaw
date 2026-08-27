from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import and_, case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.library import Library
from movieclaw_db.models.library_file import FileState, LibraryFile


class LibraryRepository:
    """媒体库表的数据访问层。

    默认库不变量（按 kind 各自维护，与下载器的全局默认同理）：
    同 kind 只要存在库，就有且只有一个默认——第一个自动成为默认；
    删除默认库时默认让给同 kind 剩下最早创建的一个。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- 查询 --------------------------------------------------------------

    async def get(self, library_id: int) -> Library | None:
        """按主键查询；不存在返回 None。"""
        return await self._session.get(Library, library_id)

    async def get_by_name(self, name: str) -> Library | None:
        """按名称查询（名称全局唯一）；不存在返回 None。"""
        result = await self._session.execute(select(Library).where(Library.name == name))
        return result.scalar_one_or_none()

    async def list_all(self, *, kind: str | None = None) -> list[Library]:
        """返回全部库（可按类型过滤），按用户排定的展示顺序（sort_order 升序，
        id 兜底）——媒体库首页的卡片区与「最近添加」分区都按这个顺序排。"""
        stmt = select(Library).order_by(Library.sort_order, Library.id)
        if kind is not None:
            stmt = stmt.where(Library.kind == kind)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_default(self, kind: str) -> Library | None:
        """返回某类型的默认库；该类型一个库都没有时返回 None。"""
        result = await self._session.execute(
            select(Library).where(
                Library.kind == kind,
                Library.is_default == True,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()

    async def count(self) -> int:
        """库总数（首启种子判空用）。"""
        result = await self._session.execute(select(Library.id))
        return len(result.scalars().all())

    async def refresh_stats(self, library_ids: Collection[int]) -> None:
        """重算指定媒体库的库存统计快照。

        聚合只在台账发生变化的写路径收尾时执行，媒体库列表/详情的高频读
        路径直接读取 ``library.stats_*``，查询成本不再随库存文件数增长。
        一批库只发固定两条聚合查询：条目数按库内在位文件的
        ``media_item_id`` 去重，分集数按（条目、季、集）去重；文件数与
        容量同样排除 missing 历史记录。这两条查询只在写路径的批次
        收尾执行，不会放大 Jellyfin 和管理端的高频读请求。
        """
        ids = sorted(set(library_ids))
        if not ids:
            return

        present = LibraryFile.in_place()  # type: ignore[union-attr]
        identified_item = case(
            (
                and_(
                    present,
                    LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
                ),
                LibraryFile.media_item_id,
            ),
            else_=None,
        )
        aggregate_rows = (
            await self._session.execute(
                select(
                    LibraryFile.library_id,
                    func.count(func.distinct(identified_item)).label("item_count"),
                    func.sum(case((present, 1), else_=0)).label("file_count"),
                    func.sum(case((present, LibraryFile.size_bytes), else_=0)).label(
                        "total_size_bytes"
                    ),
                    func.sum(
                        case(
                            (
                                and_(
                                    present,
                                    LibraryFile.media_item_id.is_(None),  # type: ignore[union-attr]
                                    LibraryFile.ignored_at.is_(None),  # type: ignore[union-attr]
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("unidentified_count"),
                    func.sum(
                        case(
                            (LibraryFile.state == FileState.MISSING, 1),
                            else_=0,
                        )
                    ).label("missing_count"),
                    func.sum(
                        case(
                            (
                                and_(
                                    present,
                                    LibraryFile.media_item_id.is_(None),  # type: ignore[union-attr]
                                    LibraryFile.ignored_at.is_not(None),  # type: ignore[union-attr]
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("ignored_count"),
                )
                .where(LibraryFile.library_id.in_(ids))  # type: ignore[attr-defined]
                .group_by(LibraryFile.library_id)
            )
        ).mappings()
        aggregates = {int(values["library_id"]): values for values in aggregate_rows}

        # SQLite 不支持 COUNT(DISTINCT col1, col2, col3)，先用子查询
        # 对分集单元去重，再按库计数。全程在数据库内聚合，不把
        # 大量台账行拉回 Python。
        distinct_units = (
            select(
                LibraryFile.library_id.label("library_id"),
                LibraryFile.media_item_id.label("media_item_id"),
                LibraryFile.season_number.label("season_number"),
                LibraryFile.episode_number.label("episode_number"),
            )
            .join(Library, Library.id == LibraryFile.library_id)
            .where(
                LibraryFile.library_id.in_(ids),  # type: ignore[attr-defined]
                Library.kind == "tv",
                present,
                LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
            )
            .distinct()
            .subquery()
        )
        episode_rows = (
            await self._session.execute(
                select(
                    distinct_units.c.library_id,
                    func.count().label("episode_count"),
                ).group_by(distinct_units.c.library_id)
            )
        ).mappings()
        episode_counts = {
            int(values["library_id"]): int(values["episode_count"] or 0)
            for values in episode_rows
        }
        libraries = list(
            (
                await self._session.execute(select(Library).where(Library.id.in_(ids)))  # type: ignore[attr-defined]
            )
            .scalars()
            .all()
        )
        refreshed_at = utcnow()
        for library in libraries:
            values = aggregates.get(library.id or -1)
            library.stats_item_count = int(values["item_count"] or 0) if values else 0
            library.stats_episode_count = episode_counts.get(library.id or -1, 0)
            library.stats_file_count = int(values["file_count"] or 0) if values else 0
            library.stats_total_size_bytes = (
                int(values["total_size_bytes"] or 0) if values else 0
            )
            library.stats_unidentified_count = (
                int(values["unidentified_count"] or 0) if values else 0
            )
            library.stats_missing_count = int(values["missing_count"] or 0) if values else 0
            library.stats_ignored_count = int(values["ignored_count"] or 0) if values else 0
            library.stats_refreshed_at = refreshed_at
        await self._session.commit()

    # -- 写入 --------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        kind: str,
        root_paths: list[str],
        match_rules: list | None = None,
        auto_clear_missing: bool = False,
        realtime_watch: bool = True,
        scrape_overrides: dict | None = None,
    ) -> Library:
        """新增一个库。该 kind 尚无默认库时，新库自动成为默认。新库置于
        展示顺序末尾（现有最大 sort_order + 1，不打乱用户排好的顺序）。"""
        existing = await self.list_all()
        row = Library(
            name=name,
            kind=kind,
            root_paths=list(root_paths),
            match_rules=list(match_rules or []),
            auto_clear_missing=auto_clear_missing,
            realtime_watch=realtime_watch,
            scrape_overrides=scrape_overrides or None,
            is_default=await self.get_default(kind) is None,
            sort_order=max((lib.sort_order for lib in existing), default=0) + 1,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def reorder(self, ordered_ids: list[int]) -> None:
        """按给定的 id 顺序重写全部库的展示顺序（1 起步的连续序号）。
        调用方（service 层）保证 ordered_ids 与现存库集合完全一致。"""
        rows = {row.id: row for row in await self.list_all()}
        now = utcnow()
        for position, library_id in enumerate(ordered_ids, start=1):
            row = rows[library_id]
            if row.sort_order != position:
                row.sort_order = position
                row.updated_at = now
        await self._session.commit()

    async def update(
        self,
        library_id: int,
        *,
        name: str,
        root_paths: list[str],
        match_rules: list | None = None,
        auto_clear_missing: bool | None = None,
        realtime_watch: bool | None = None,
        scrape_overrides: dict | None = None,
    ) -> Library | None:
        """更新名称/根路径/收藏范围（kind 创建后不可改）；不存在返回 None。

        ``auto_clear_missing`` 传 None 表示不改动——它是个危险开关（清理
        不可恢复），不能因为某个客户端的请求体里没带这个字段就被静默关掉。
        ``realtime_watch`` 同理：None = 保持原值，避免老客户端不带字段
        就把用户关掉的监控又悄悄打开。
        """
        row = await self.get(library_id)
        if row is None:
            return None
        row.name = name
        row.root_paths = list(root_paths)
        row.match_rules = list(match_rules or [])
        if auto_clear_missing is not None:
            row.auto_clear_missing = auto_clear_missing
        if realtime_watch is not None:
            row.realtime_watch = realtime_watch
        if scrape_overrides is not None:
            # 空字典 = 显式清空覆盖（回到全跟全局）；None = 不改动
            row.scrape_overrides = scrape_overrides or None
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def set_default(self, library_id: int) -> bool:
        """把某库设为其类型的默认库（同时清掉同 kind 其他库的默认标记）。"""
        row = await self.get(library_id)
        if row is None:
            return False
        now = utcnow()
        for other in await self.list_all(kind=row.kind):
            if other.is_default and other.id != library_id:
                other.is_default = False
                other.updated_at = now
        row.is_default = True
        row.updated_at = now
        await self._session.commit()
        return True

    async def delete(self, library_id: int) -> bool:
        """删除某库。返回是否命中记录。

        - 引用它的订阅经外键 SET NULL 回落到"用该类型默认库"；
        - 删除的是默认库时，把默认让给同 kind 剩下最早创建的一个。
        """
        row = await self.get(library_id)
        if row is None:
            return False
        was_default, kind = row.is_default, row.kind
        await self._session.delete(row)
        await self._session.commit()
        if was_default:
            remaining = await self.list_all(kind=kind)
            if remaining:
                remaining[0].is_default = True
                remaining[0].updated_at = utcnow()
                await self._session.commit()
        return True
