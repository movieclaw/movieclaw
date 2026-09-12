"""路由层共享助手：参数解析、DtoContext 构造、开关检查。"""

from __future__ import annotations

from typing import Any

from movieclaw_api.settings.schemas import get_jellyfin_compat
from movieclaw_jellyfin.catalog import DtoContext, DtoOptions
from movieclaw_jellyfin.errors import not_found


def parse_comma(value: str | None) -> list[str]:
    """逗号分隔列表；空段丢弃（对齐 CommaDelimitedCollectionModelBinder）。"""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def parse_pipe(value: str | None) -> list[str]:
    """管道符分隔列表（genres/studios/tags/officialRatings 专用）。"""
    if not value:
        return []
    return [p for p in value.split("|") if p]


def parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() == "true"


def dto_options(
    fields: str | None,
    *,
    enable_user_data: str | None = None,
    enable_images: str | None = None,
    all_fields: bool = False,
) -> DtoOptions:
    return DtoOptions(
        fields=set(parse_comma(fields)),
        enable_user_data=parse_bool(enable_user_data) is not False,
        enable_images=parse_bool(enable_images) is not False,
        all_fields=all_fields,
    )


async def dto_context() -> DtoContext:
    setting = await get_jellyfin_compat()
    return DtoContext(server_id=setting.server_id)


async def require_enabled() -> None:
    """兼容层总开关：关闭时所有 Jellyfin 路由 404。"""
    setting = await get_jellyfin_compat()
    if not setting.enabled:
        raise not_found()


def query_result(items: list[dict[str, Any]], total: int, start_index: int = 0) -> dict[str, Any]:
    return {"Items": items, "TotalRecordCount": total, "StartIndex": start_index}
