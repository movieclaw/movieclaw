from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.crypto import get_secret_box
from movieclaw_db.models.base import utcnow
from movieclaw_db.models.downloader_client import ClientType, DownloaderClient
from movieclaw_db.models.site_credential import ConfigStatus


class DownloaderRepository:
    """下载器配置表的数据访问层。

    密码的加解密统一收口在本层：
    - 写入（create/update）时用 SecretBox 加密再落库；
    - ``decrypted_password`` 按需解密，仅在真正要连下载器时调用，
      避免密码明文在各层之间随 ORM 对象到处传递。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- 查询 --------------------------------------------------------------

    async def get(self, downloader_id: int) -> DownloaderClient | None:
        """按主键查询；不存在返回 None。"""
        return await self._session.get(DownloaderClient, downloader_id)

    async def get_by_name(self, name: str) -> DownloaderClient | None:
        """按名称查询（名称全局唯一）；不存在返回 None。"""
        result = await self._session.execute(
            select(DownloaderClient).where(DownloaderClient.name == name)
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[DownloaderClient]:
        """返回所有下载器（含已停用），按 id 排序保持添加顺序。"""
        result = await self._session.execute(select(DownloaderClient).order_by(DownloaderClient.id))
        return list(result.scalars().all())

    async def get_default(self) -> DownloaderClient | None:
        """返回默认下载器；一台都没配置时返回 None。"""
        result = await self._session.execute(
            select(DownloaderClient).where(DownloaderClient.is_default == True)  # noqa: E712
        )
        return result.scalar_one_or_none()

    @staticmethod
    def decrypted_password(row: DownloaderClient) -> str | None:
        """解密某条记录的密码密文，返回明文（未设密码返回 None）。"""
        if row.password is None:
            return None
        return get_secret_box().decrypt(row.password)

    # -- 写入 --------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        client_type: ClientType,
        url: str,
        username: str | None,
        password: str | None,
        save_path: str | None,
        path_mappings: list[dict[str, str]] | None = None,
        enabled: bool = True,
    ) -> DownloaderClient:
        """新增一个下载器配置（状态置 PENDING，等待异步测试连接）。

        默认下载器不变量：当前没有默认（通常是第一台）时，新增的这台自动成为默认。
        """
        row = DownloaderClient(
            name=name,
            client_type=client_type,
            url=url,
            username=username,
            password=get_secret_box().encrypt(password) if password else None,
            save_path=save_path,
            path_mappings=path_mappings,
            enabled=enabled,
            status=ConfigStatus.PENDING,
            is_default=await self.get_default() is None,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def update(
        self,
        downloader_id: int,
        *,
        name: str,
        client_type: ClientType,
        url: str,
        username: str | None,
        password: str | None,
        save_path: str | None,
        path_mappings: list[dict[str, str]] | None = None,
        enabled: bool,
    ) -> DownloaderClient | None:
        """整体覆盖一条下载器配置；不存在返回 None。

        与站点凭据同语义：全字段覆盖（含把未提供的敏感字段重置为 None），
        连接信息一旦变更，验证状态重置为 PENDING、清空历史错误。
        """
        row = await self.get(downloader_id)
        if row is None:
            return None
        row.name = name
        row.client_type = client_type
        row.url = url
        row.username = username
        row.password = get_secret_box().encrypt(password) if password else None
        row.save_path = save_path
        row.path_mappings = path_mappings
        row.enabled = enabled
        row.status = ConfigStatus.PENDING
        row.last_error = None
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def update_status(
        self,
        downloader_id: int,
        status: ConfigStatus,
        *,
        last_error: str | None = None,
        version: str | None = None,
        path_health: list[dict[str, str]] | None = None,
    ) -> bool:
        """回写连接测试结论。返回是否命中记录。

        - 成功（ACTIVE）：清空 last_error，记录版本号、路径体检结果与检查时间。
        - 失败（FAILED）：记录 last_error 与检查时间。
        - 中间态（VERIFYING）：仅改状态。
        """
        row = await self.get(downloader_id)
        if row is None:
            return False
        now = utcnow()
        row.status = status
        if status == ConfigStatus.ACTIVE:
            row.last_error = None
            row.version = version
            row.path_health = path_health
            row.last_checked_at = now
        elif status == ConfigStatus.FAILED:
            if last_error is not None:
                row.last_error = last_error
            row.last_checked_at = now
        row.updated_at = now
        await self._session.commit()
        return True

    async def reset_stale_verifying(self) -> int:
        """把残留在 VERIFYING 的记录重置为 PENDING（进程重启自愈），返回条数。"""
        result = await self._session.execute(
            select(DownloaderClient).where(DownloaderClient.status == ConfigStatus.VERIFYING)
        )
        rows = list(result.scalars().all())
        for row in rows:
            row.status = ConfigStatus.PENDING
            row.updated_at = utcnow()
        if rows:
            await self._session.commit()
        return len(rows)

    async def set_enabled(self, downloader_id: int, enabled: bool) -> bool:
        """启用/停用某下载器。返回是否命中记录。"""
        row = await self.get(downloader_id)
        if row is None:
            return False
        row.enabled = enabled
        row.updated_at = utcnow()
        await self._session.commit()
        return True

    async def set_default(self, downloader_id: int) -> bool:
        """把某台设为默认下载器（同时清掉其他台的默认标记）。返回是否命中记录。"""
        row = await self.get(downloader_id)
        if row is None:
            return False
        now = utcnow()
        for other in await self.list_all():
            if other.is_default and other.id != downloader_id:
                other.is_default = False
                other.updated_at = now
        row.is_default = True
        row.updated_at = now
        await self._session.commit()
        return True

    async def delete(self, downloader_id: int) -> bool:
        """删除某下载器配置。返回是否命中记录。

        默认下载器不变量：删除的是默认时，自动把默认让给剩下最早添加的一台，
        保证"只要还有下载器就有默认"，一键下载永远有目标。
        """
        row = await self.get(downloader_id)
        if row is None:
            return False
        was_default = row.is_default
        await self._session.delete(row)
        await self._session.commit()
        if was_default:
            remaining = await self.list_all()
            if remaining:
                remaining[0].is_default = True
                remaining[0].updated_at = utcnow()
                await self._session.commit()
        return True
