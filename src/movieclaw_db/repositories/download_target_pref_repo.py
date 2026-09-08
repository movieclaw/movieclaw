"""下载保存位置记忆的数据访问层。

只做「按 (成员, 分类) 读一条 / 覆盖一条 / 删一条」，不理解业务语义——目标是否
仍然有效（目录还在不在、下载器还能不能用）由服务层判定，见
``docs/design/download-target-memory.md`` §5.3。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.download_target_pref import DownloadTargetPref


class DownloadTargetPrefRepository:
    """``download_target_pref`` 的仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, member_id: int, category: str) -> DownloadTargetPref | None:
        """读某人某分类的记忆；没有返回 None（= 走完整弹窗）。"""
        result = await self._session.execute(
            select(DownloadTargetPref).where(
                DownloadTargetPref.member_id == member_id,
                DownloadTargetPref.category == category,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_member(self, member_id: int) -> list[DownloadTargetPref]:
        """读某人的全部记忆，按分类排序（搜索结果随行下发用）。"""
        result = await self._session.execute(
            select(DownloadTargetPref)
            .where(DownloadTargetPref.member_id == member_id)
            .order_by(DownloadTargetPref.category)
        )
        return list(result.scalars().all())

    async def upsert(
        self,
        member_id: int,
        category: str,
        *,
        kind: str,
        save_path: str | None,
        downloader_id: int | None,
    ) -> DownloadTargetPref:
        """写入或覆盖某分类的记忆。

        目标与已存记忆完全一致时**不写**（只读一次、不产生 UPDATE）：批量下载
        同一分类连点十几次，没必要每次都刷 ``updated_at`` 和写盘。但注意
        ``updated_at`` 因此表示「这条记忆最后一次被**改变**的时间」，确认条上
        展示为「上次用过」——语义上略有出入但对用户更有用：他关心的是「这个
        默认是什么时候定下来的」，而不是「我上次点确认是几点」。
        """
        row = await self.get(member_id, category)
        if row is not None:
            if (
                row.kind == kind
                and row.save_path == save_path
                and row.downloader_id == downloader_id
            ):
                return row
            row.kind = kind
            row.save_path = save_path
            row.downloader_id = downloader_id
            row.updated_at = utcnow()
        else:
            row = DownloadTargetPref(
                member_id=member_id,
                category=category,
                kind=kind,
                save_path=save_path,
                downloader_id=downloader_id,
            )
            self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def delete(self, member_id: int, category: str) -> bool:
        """清除某分类的记忆（确认条的「不再记住」）。返回是否命中。"""
        row = await self.get(member_id, category)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True
