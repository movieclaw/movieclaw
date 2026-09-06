from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.crypto import get_secret_box
from movieclaw_db.models.base import utcnow
from movieclaw_db.models.llm_provider import LlmProvider
from movieclaw_db.models.site_credential import ConfigStatus


class LlmProviderRepository:
    """LLM 供应商实例配置表的数据访问层（多实例）。

    - 与 DownloaderRepository 同构：按 id 读写、按 name 查重、维护
      「只要还有实例就有且仅有一个默认」的不变量；
    - api_key 的加解密统一收口在本层。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- 查询 --------------------------------------------------------------

    async def get(self, provider_id: int) -> LlmProvider | None:
        """按主键查询；不存在返回 None。"""
        return await self._session.get(LlmProvider, provider_id)

    async def get_by_name(self, name: str) -> LlmProvider | None:
        """按实例名查询（名称全局唯一）；不存在返回 None。"""
        result = await self._session.execute(select(LlmProvider).where(LlmProvider.name == name))
        return result.scalar_one_or_none()

    async def list_all(self) -> list[LlmProvider]:
        """返回全部实例，默认实例排最前，其余按添加顺序。

        顺序即路由优先级：裸模型 id 在多个实例目录里都有时，LlmRouter 按
        这个顺序命中（默认实例优先）。
        """
        result = await self._session.execute(
            select(LlmProvider).order_by(LlmProvider.is_default.desc(), LlmProvider.id)
        )
        return list(result.scalars().all())

    async def get_default(self) -> LlmProvider | None:
        """返回默认实例；一个都没配置时返回 None。"""
        result = await self._session.execute(
            select(LlmProvider).where(LlmProvider.is_default == True).limit(1)  # noqa: E712
        )
        return result.scalar_one_or_none()

    async def has_any(self) -> bool:
        """是否至少接入了一个实例（各 AI 入口的「已配置」判据）。"""
        result = await self._session.execute(select(LlmProvider.id).limit(1))
        return result.scalar_one_or_none() is not None

    @staticmethod
    def decrypted_api_key(row: LlmProvider) -> str:
        """解密 API Key 密文，仅在真正要调模型时使用。"""
        return get_secret_box().decrypt(row.api_key)

    # -- 写入 --------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        provider_type: str,
        base_url: str | None,
        api_key: str,
        default_model: str,
        extra_models: list[dict] | None = None,
        user_agent: str | None = None,
    ) -> LlmProvider:
        """新增实例。第一个接入的实例自动成为默认（不变量：有实例就有默认）。"""
        row = LlmProvider(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key=get_secret_box().encrypt(api_key),
            default_model=default_model,
            extra_models=extra_models,
            user_agent=user_agent,
            is_default=not await self.has_any(),
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def update(
        self,
        provider_id: int,
        *,
        name: str,
        provider_type: str,
        base_url: str | None,
        api_key: str,
        default_model: str,
        extra_models: list[dict] | None = None,
        user_agent: str | None = None,
    ) -> LlmProvider | None:
        """整体覆盖一个实例的接入配置；不存在返回 None。

        连接信息变更后验证状态重置为 PENDING、清空历史错误与模型列表。
        is_default 不在此处改动（走 set_default）。
        """
        row = await self.get(provider_id)
        if row is None:
            return None
        row.name = name
        row.provider_type = provider_type
        row.base_url = base_url
        row.api_key = get_secret_box().encrypt(api_key)
        row.default_model = default_model
        row.extra_models = extra_models
        row.user_agent = user_agent
        row.status = ConfigStatus.PENDING
        row.last_error = None
        row.available_models = None
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def update_status(
        self,
        provider_id: int,
        status: ConfigStatus,
        *,
        last_error: str | None = None,
        available_models: list[str] | None = None,
    ) -> bool:
        """回写验证结论。返回是否命中记录。

        - 成功（ACTIVE）：清空 last_error，记录可用模型列表与检查时间；
        - 失败（FAILED）：记录 last_error 与检查时间；
        - 中间态（VERIFYING）：仅改状态。
        """
        row = await self.get(provider_id)
        if row is None:
            return False
        now = utcnow()
        row.status = status
        if status == ConfigStatus.ACTIVE:
            row.last_error = None
            if available_models is not None:
                row.available_models = available_models
            row.last_checked_at = now
        elif status == ConfigStatus.FAILED:
            if last_error is not None:
                row.last_error = last_error
            row.last_checked_at = now
        row.updated_at = now
        await self._session.commit()
        return True

    async def set_default(self, provider_id: int) -> bool:
        """把某实例设为全局默认（同时清掉其它实例的默认标记）。返回是否命中记录。"""
        row = await self.get(provider_id)
        if row is None:
            return False
        now = utcnow()
        for other in await self.list_all():
            if other.is_default and other.id != provider_id:
                other.is_default = False
                other.updated_at = now
        row.is_default = True
        row.updated_at = now
        await self._session.commit()
        return True

    async def reset_stale_verifying(self) -> int:
        """把残留在 VERIFYING 的实例重置为 PENDING（进程重启自愈），返回条数。"""
        result = await self._session.execute(
            select(LlmProvider).where(LlmProvider.status == ConfigStatus.VERIFYING)
        )
        rows = list(result.scalars().all())
        now = utcnow()
        for row in rows:
            row.status = ConfigStatus.PENDING
            row.updated_at = now
        if rows:
            await self._session.commit()
        return len(rows)

    async def delete(self, provider_id: int) -> bool:
        """删除某实例。返回是否命中记录。

        删除的是默认实例时，自动把默认让给剩下最早添加的一个，保证
        「只要还有实例就有默认」——IM 通道与字幕翻译永远有模型可用。
        """
        row = await self.get(provider_id)
        if row is None:
            return False
        was_default = row.is_default
        await self._session.delete(row)
        await self._session.flush()
        if was_default:
            result = await self._session.execute(
                select(LlmProvider).order_by(LlmProvider.id).limit(1)
            )
            successor = result.scalar_one_or_none()
            if successor is not None:
                successor.is_default = True
                successor.updated_at = utcnow()
        await self._session.commit()
        return True
