from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.library_file import FileState, IdentitySource, LibraryFile

logger = logging.getLogger("movieclaw_db.library_file")

# 哨兵：区分「调用方没提供同路径旧行」与「调用方已确认该路径没有旧行」。
# 用 None 当默认值就没法区分这两种情况，前者必须自己去查，后者查了是白查
_LOOKUP: LibraryFile = object()  # type: ignore[assignment]


class LibraryFileRepository:
    """库存台账的数据访问层。

    ``file_path`` 是全局唯一键：入库/扫描的写入统一走 ``upsert_by_path``
    ——同一路径重复发现（重扫、重复入库）更新而非重复插入。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- 查询 --------------------------------------------------------------

    async def get_by_path(self, file_path: str) -> LibraryFile | None:
        result = await self._session.execute(
            select(LibraryFile).where(LibraryFile.file_path == file_path)
        )
        return result.scalar_one_or_none()

    async def list_by_library(self, library_id: int) -> list[LibraryFile]:
        """某库的全部台账（含 missing 行，展示层自行标注）。"""
        result = await self._session.execute(
            select(LibraryFile).where(LibraryFile.library_id == library_id).order_by(LibraryFile.id)
        )
        return list(result.scalars().all())

    async def list_unidentified(self, *, library_id: int | None = None) -> list[LibraryFile]:
        """待识别清单：**在位**、没挂上身份锚、且用户没忽略过的文件。

        在位口径不能省：文件已经不在磁盘上了还催人去认领，是让用户对着
        一个认领完也没有片源的条目做决定；那类行属于「缺失」清单。少了这
        个条件，清单还会与库卡片上的 ``stats_unidentified_count`` 角标对不上
        （后者一直只算在位，见 LibraryRepository.refresh_stats）——同一件事
        两个数，用户没法判断该信哪个。
        """
        stmt = select(LibraryFile).where(
            LibraryFile.media_item_id.is_(None),  # type: ignore[union-attr]
            LibraryFile.ignored_at.is_(None),  # type: ignore[union-attr]
            LibraryFile.in_place(),
        )
        if library_id is not None:
            stmt = stmt.where(LibraryFile.library_id == library_id)
        result = await self._session.execute(stmt.order_by(LibraryFile.file_path))
        return list(result.scalars().all())

    async def list_review(self, *, library_id: int | None = None) -> list[LibraryFile]:
        """身份复核清单：识别器升级后新旧结论不一致、等用户拍板的行。"""
        stmt = select(LibraryFile).where(LibraryFile.review_suggestion.is_not(None))  # type: ignore[union-attr]
        if library_id is not None:
            stmt = stmt.where(LibraryFile.library_id == library_id)
        result = await self._session.execute(stmt.order_by(LibraryFile.file_path))
        return list(result.scalars().all())

    async def list_ignored(self, *, library_id: int | None = None) -> list[LibraryFile]:
        """已忽略清单：用户说过"别再问"的文件（可一键恢复重新参与识别）。"""
        stmt = select(LibraryFile).where(LibraryFile.ignored_at.is_not(None))  # type: ignore[union-attr]
        if library_id is not None:
            stmt = stmt.where(LibraryFile.library_id == library_id)
        result = await self._session.execute(stmt.order_by(LibraryFile.file_path))
        return list(result.scalars().all())

    async def list_missing(
        self, library_id: int, *, media_item_id: int | None = None
    ) -> list[LibraryFile]:
        """缺失清单：文件已不在磁盘（missing_since 非空）的台账行。"""
        stmt = select(LibraryFile).where(
            LibraryFile.library_id == library_id,
            LibraryFile.state == FileState.MISSING,
        )
        if media_item_id is not None:
            stmt = stmt.where(LibraryFile.media_item_id == media_item_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def delete_missing(
        self, library_id: int, *, media_item_id: int | None = None
    ) -> int:
        """删除缺失记录（只删台账，绝不动磁盘），返回删除条数。"""
        rows = await self.list_missing(library_id, media_item_id=media_item_id)
        for row in rows:
            await self._session.delete(row)
        if rows:
            await self._session.commit()
        return len(rows)

    async def delete_by_ids(self, file_ids: list[int]) -> int:
        """按 id 批量删除台账行（扫描的自动清理用），返回删除条数。

        与 ``delete_missing`` 的区别只在选行方式：清理谁由调用方判定
        （扫描要按"本轮遍历确认未回归"筛，那是它才知道的事实）。
        同样**只删台账，绝不动磁盘**。
        """
        deleted = 0
        for file_id in file_ids:
            row = await self._session.get(LibraryFile, file_id)
            if row is None:
                continue
            await self._session.delete(row)
            deleted += 1
        if deleted:
            await self._session.commit()
        return deleted

    async def find_by_size(self, library_id: int, size_bytes: int) -> list[LibraryFile]:
        """同库同尺寸的台账行——改名归并的候选池（尺寸是改名/移动的不变量）。"""
        result = await self._session.execute(
            select(LibraryFile).where(
                LibraryFile.library_id == library_id,
                LibraryFile.size_bytes == size_bytes,
            )
        )
        return list(result.scalars().all())

    async def owned_units(self, media_item_id: int) -> set[tuple[int, int]]:
        """某条目在库的期望单元集合（库存 H）——wanted 生成跳过已有的依据。

        只算"在位"的文件（missing 的不算拥有）。
        """
        result = await self._session.execute(
            select(LibraryFile.season_number, LibraryFile.episode_number)
            .where(
                LibraryFile.media_item_id == media_item_id,
                LibraryFile.in_place(),
            )
            .distinct()
        )
        return {(row[0], row[1]) for row in result.all()}

    async def owned_units_many(
        self, media_item_ids: list[int]
    ) -> dict[int, set[tuple[int, int]]]:
        """批量版 owned_units（海报墙等列表页一次查完避免 N+1）。

        口径与 owned_units 完全一致：**跨库**统计（同一部剧的集可能分散在
        多个媒体库，任一库在位就算拥有）、missing 的文件不算。列表页的
        缺集提示与订阅生成工单必须同口径，否则会出现「海报卡提示补齐缺集，
        点开订阅弹窗却说整季已在库」的自相矛盾。
        """
        if not media_item_ids:
            return {}
        result = await self._session.execute(
            select(
                LibraryFile.media_item_id,
                LibraryFile.season_number,
                LibraryFile.episode_number,
            )
            .where(
                LibraryFile.media_item_id.in_(media_item_ids),  # type: ignore[attr-defined]
                LibraryFile.in_place(),
            )
            .distinct()
        )
        owned: dict[int, set[tuple[int, int]]] = {}
        for media_item_id, season_number, episode_number in result.all():
            owned.setdefault(media_item_id, set()).add((season_number, episode_number))
        return owned

    # -- 写入 --------------------------------------------------------------

    async def upsert_by_path(
        self, row: LibraryFile, *, existing: LibraryFile | None = _LOOKUP
    ) -> LibraryFile:
        """按 file_path 幂等写入：已有则整体更新（文件回归时清 missing 标记）。

        ``existing`` 是调用方已经持有的同路径台账行（扫描开场就把整库台账读进
        内存了，逐文件再查一次纯属重复）。不传则自己查；显式传 None 表示调用
        方已确认该路径没有旧行。
        """
        if existing is _LOOKUP:
            existing = await self.get_by_path(row.file_path)
        if existing is None:
            try:
                # INSERT 圈在 SAVEPOINT 里：撞键只回滚保存点自身，不会把整个
                # 会话置为失败态、也不会把已加载对象全部过期（async 会话下
                # 过期属性一碰就抛 MissingGreenlet，等于毒死调用方的后续工作）
                async with self._session.begin_nested():
                    self._session.add(row)
            except IntegrityError:
                # 调用方的「没有旧行」结论过期了：file_path 全局唯一，同路径
                # 行可能刚被另一条链路写入（扫描进行中监听导入恰好投递同一
                # 文件），也可能挂在另一个库名下（跨库根路径重叠的历史配置）。
                # 撞键不是终局——全局重查、转为原地更新，保住幂等语义
                existing = await self.get_by_path(row.file_path)
                if existing is None:
                    raise  # 不是路径撞键，是别的完整性问题：如实上抛
                if existing.library_id != row.library_id:
                    logger.warning(
                        "文件已挂在另一个媒体库（#%d）名下，本次写入将其转归媒体库 #%d"
                        "——若两个库的根路径互相重叠，请调整库配置：%s",
                        existing.library_id,
                        row.library_id,
                        row.file_path,
                    )
            else:
                # 不 refresh：expire_on_commit=False，提交后属性仍在，
                # id 由 INSERT 回填，没有库端默认值需要读回
                await self._session.commit()
                return row
        existing.library_id = row.library_id
        existing.media_item_id = row.media_item_id
        existing.season_number = row.season_number
        existing.episode_number = row.episode_number
        existing.size_bytes = row.size_bytes
        existing.file_mtime_ns = row.file_mtime_ns
        existing.container = row.container
        existing.resolution = row.resolution
        existing.video_codec = row.video_codec
        existing.hdr = row.hdr
        existing.bit_depth = row.bit_depth
        existing.duration_seconds = row.duration_seconds
        existing.bit_rate = row.bit_rate
        existing.frame_rate = row.frame_rate
        existing.color_space = row.color_space
        existing.audio_streams = row.audio_streams
        existing.subtitle_streams = row.subtitle_streams
        existing.external_subtitles = row.external_subtitles
        # 人工标注的片源不被自动解析覆盖（docs/design/media-source-annotation.md
        # §3.2）；扫描/入库构造的 row 永远非人工，标记位无需从 row 继承
        if not existing.media_source_manual:
            existing.media_source = row.media_source
        existing.release_group = row.release_group
        existing.source = row.source
        existing.site_id = row.site_id
        existing.torrent_id = row.torrent_id
        existing.unidentified_reason = row.unidentified_reason
        existing.unidentified_code = row.unidentified_code
        existing.unidentified_candidates = row.unidentified_candidates
        existing.identity_source = row.identity_source
        existing.resolved_version = row.resolved_version
        existing.review_suggestion = row.review_suggestion
        existing.revive()  # 再次发现即在位；待回收行不复活，只更新探测属性
        existing.updated_at = utcnow()
        await self._session.commit()
        return existing

    async def relocate(self, file_id: int, *, file_path: str, container: str | None) -> None:
        """改名归并：把台账行迁到新路径，身份锚与介质信息原样保留。"""
        row = await self._session.get(LibraryFile, file_id)
        if row is None:
            return
        row.file_path = file_path
        row.container = container
        row.revive()
        row.updated_at = utcnow()
        await self._session.commit()

    async def relocate_to_library(
        self,
        file_id: int,
        *,
        library_id: int,
        file_path: str,
        keep_missing: bool = False,
    ) -> None:
        """条目转移：把台账行改挂到另一个库并迁到新路径。

        与 ``relocate`` 的区别只在多改一个 ``library_id``——身份锚、季集、
        介质规格、来源追溯一概原样保留（转移改的是"这份拷贝归哪个库管"，
        不是"这是哪部片子"）。

        ``keep_missing``：缺失行（磁盘上没有实体）只做逻辑随迁，不能顺手
        清掉 missing 标记——那会让一个并不存在的文件在新库里显示为在位。
        """
        row = await self._session.get(LibraryFile, file_id)
        if row is None:
            return
        row.library_id = library_id
        row.file_path = file_path
        if not keep_missing:
            row.revive()
        row.updated_at = utcnow()
        await self._session.commit()

    async def claim_identity(
        self,
        file_id: int,
        *,
        media_item_id: int,
        season_number: int,
        episode_number: int,
        resolved_version: int | None = None,
    ) -> LibraryFile | None:
        """人工认领：给未识别文件挂上身份锚。不存在返回 None。

        认领即人工拍板，``identity_source`` 记为 manual——身份对账机制永不
        自动翻案人工结论。``resolved_version`` 由调用方传当前识别器版本。
        """
        row = await self._session.get(LibraryFile, file_id)
        if row is None:
            return None
        row.media_item_id = media_item_id
        row.season_number = season_number
        row.episode_number = episode_number
        row.unidentified_reason = None  # 已有身份，失败原因/分类/候选随之失义
        row.unidentified_code = None
        row.unidentified_candidates = None
        row.identity_source = IdentitySource.MANUAL
        row.resolved_version = resolved_version
        row.review_suggestion = None
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def detach_and_ignore(self, file_ids: list[int]) -> tuple[int, set[int]]:
        """「这不是独立作品」：摘掉身份锚 + 打忽略标记，返回 ``(处理数, 被腾空前挂着的条目 id)``。

        花絮、预告、片段这类文件被识别链高置信错挂到别的影片时，用户要
        表达的不是"改挂到条目 Y"，而是"它根本不该是个条目"。单打忽略
        标记不够——``ignored_at`` 只让扫描不再过问，身份锚还在，条目照旧
        出现在库存里；必须连锚一起摘掉，这一行才真正从库存中消失。

        与「忽略待识别文件」同样是**可反悔**的：行始终保留，在「已忽略」
        里恢复即可重新参与识别（磁盘文件自始至终未被触碰）。
        """
        now = utcnow()
        detached = 0
        previous: set[int] = set()
        for file_id in file_ids:
            row = await self._session.get(LibraryFile, file_id)
            if row is None:
                continue
            if row.media_item_id is not None:
                previous.add(row.media_item_id)
            row.media_item_id = None
            # 身份没了，围绕身份的三件套（失败原因/分类/候选）与复核建议随之失义
            row.unidentified_reason = None
            row.unidentified_code = None
            row.unidentified_candidates = None
            row.identity_source = None
            row.resolved_version = None
            row.review_suggestion = None
            row.ignored_at = row.ignored_at or now
            row.updated_at = now
            detached += 1
        await self._session.commit()
        return detached, previous

    async def mark_missing(self, file_id: int, *, since: datetime | None = None) -> None:
        """对账：标记文件消失（不删记录）。"""
        row = await self._session.get(LibraryFile, file_id)
        if row is None:
            return
        row.mark_missing(since)  # 待回收行不受缺失检测覆盖（复活防线）
        row.updated_at = utcnow()
        await self._session.commit()

    async def mark_ignored(self, file_ids: list[int]) -> int:
        """待识别清单的「忽略」：打标记而非删行，返回实际标记的条数。

        **不能删行**——扫描器判定"新文件"的唯一依据就是台账里有没有这条
        路径，删了下轮扫描就当新文件重走识别链，认不出照样回清单（对活跃
        的库连几分钟都撑不住）。打标记后扫描直接秒过，且行还在、可恢复。
        """
        marked = 0
        now = utcnow()
        for file_id in file_ids:
            row = await self._session.get(LibraryFile, file_id)
            if row is None or row.ignored_at is not None:
                continue
            row.ignored_at = now
            row.updated_at = now
            marked += 1
        await self._session.commit()
        return marked

    async def restore_ignored(self, file_ids: list[int]) -> int:
        """恢复已忽略的文件：清标记，重新参与识别与待识别清单。"""
        restored = 0
        now = utcnow()
        for file_id in file_ids:
            row = await self._session.get(LibraryFile, file_id)
            if row is None or row.ignored_at is None:
                continue
            row.ignored_at = None
            row.updated_at = now
            restored += 1
        await self._session.commit()
        return restored

    async def clear_missing_flag(self, file_id: int) -> None:
        """清除 missing 标记（已忽略的文件回归时用：忽略状态原样保留）。"""
        row = await self._session.get(LibraryFile, file_id)
        if row is None or row.state != FileState.MISSING:
            return
        row.revive()
        row.updated_at = utcnow()
        await self._session.commit()
