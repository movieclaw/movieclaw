"""MCP 端点的增删改查与鉴权（docs/design/mcp-server.md §4.4 / §5）。

端点是「管理员配的几条记录」，落在 ``mcp.endpoints`` 配置域里。本模块是它唯一的
写入口，三件事必须在这里保证：

1. **令牌只存哈希**。明文只在创建/轮换那一次返回给调用方，服务端此后取不出来
   （与 ``ApiTokenRecord`` 同一立场）——丢了只能轮换，不能找回。
2. **地址标识唯一且可安全入 URL**。它会成为 ``/mcp/<slug>`` 的一段。
3. **服务集合必须是 spec 里真实存在的域**。写入时校验，读取时对已消失的域静默
   忽略——升级后某个域被移除，不该让整个端点连同其余服务一起坏掉。
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from datetime import UTC, datetime

from movieclaw_api.exceptions import BadRequestException, ConflictException, NotFoundException
from movieclaw_api.settings import get_setting_store
from movieclaw_api.settings.mcp import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    SLUG_PATTERN,
    McpEndpoint,
    McpEndpointsSetting,
    generate_token,
    hash_token,
    token_hint,
)
from movieclaw_mcp.catalog import available_services

logger = logging.getLogger("movieclaw_api.mcp_endpoints")

#: 「最近调用」的落盘节流：精度够回答「这个端点还有人在用吗」，又不至于让每次
#: 工具调用都写一次设置项（与 API 令牌的 _touch 同款做法）。
_TOUCH_INTERVAL_S = 60
_touched_at: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def get_config() -> McpEndpointsSetting:
    return await get_setting_store().get(McpEndpointsSetting)


async def _save(config: McpEndpointsSetting) -> None:
    await get_setting_store().set(config)


def _validate_slug(slug: str, *, existing: list[McpEndpoint], exclude_id: str = "") -> str:
    slug = slug.strip().lower()
    if not SLUG_PATTERN.match(slug):
        raise BadRequestException(
            "地址标识只能用小写字母、数字和连字符，且不能以连字符开头或结尾（如 home-assistant）"
        )
    if any(e.slug == slug and e.id != exclude_id for e in existing):
        raise ConflictException(f"地址标识「{slug}」已被其他端点占用，换一个")
    return slug


def _validate_services(services: list[str]) -> list[str]:
    known = set(available_services())
    unknown = [s for s in services if s not in known]
    if unknown:
        raise BadRequestException(f"这些服务不存在：{', '.join(unknown)}")
    if not services:
        raise BadRequestException("至少选择一个服务，否则这个端点没有任何工具")
    return sorted(set(services))


def _validate_timeout(seconds: int) -> int:
    if not 5 <= seconds <= MAX_TIMEOUT_SECONDS:
        raise BadRequestException(f"超时秒数必须在 5 到 {MAX_TIMEOUT_SECONDS} 之间")
    return seconds


async def create_endpoint(
    *,
    name: str,
    slug: str,
    services: list[str],
    description: str = "",
    expand_tools: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[McpEndpoint, str]:
    """新建端点，返回 (记录, 令牌明文)。明文仅此一次。"""
    config = await get_config()
    name = name.strip()
    if not name:
        raise BadRequestException("端点名称不能为空")
    endpoint = McpEndpoint(
        id=secrets.token_hex(8),
        slug=_validate_slug(slug, existing=config.endpoints),
        name=name,
        description=description.strip(),
        services=_validate_services(services),
        expand_tools=expand_tools,
        timeout_seconds=_validate_timeout(timeout_seconds),
        created_at=_now_iso(),
    )
    plaintext = generate_token()
    endpoint.token_hash = hash_token(plaintext)
    endpoint.token_hint = token_hint(plaintext)
    config.endpoints.append(endpoint)
    await _save(config)
    logger.info(
        "已创建 MCP 端点「%s」（/mcp/%s，%d 个服务）", name, endpoint.slug, len(endpoint.services)
    )
    return endpoint, plaintext


def _find(config: McpEndpointsSetting, endpoint_id: str) -> McpEndpoint:
    for endpoint in config.endpoints:
        if endpoint.id == endpoint_id:
            return endpoint
    raise NotFoundException("端点不存在或已被删除")


async def update_endpoint(
    endpoint_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    services: list[str] | None = None,
    expand_tools: bool | None = None,
    enabled: bool | None = None,
    timeout_seconds: int | None = None,
) -> McpEndpoint:
    """更新端点。只改传进来的字段——地址标识建成后不可改（改了等于换个端点，
    已接入的客户端会静默失联，不如让用户显式删了重建）。"""
    config = await get_config()
    endpoint = _find(config, endpoint_id)
    if name is not None:
        if not name.strip():
            raise BadRequestException("端点名称不能为空")
        endpoint.name = name.strip()
    if description is not None:
        endpoint.description = description.strip()
    if services is not None:
        endpoint.services = _validate_services(services)
    if expand_tools is not None:
        endpoint.expand_tools = expand_tools
    if enabled is not None:
        endpoint.enabled = enabled
    if timeout_seconds is not None:
        endpoint.timeout_seconds = _validate_timeout(timeout_seconds)
    await _save(config)
    logger.info("已更新 MCP 端点「%s」（/mcp/%s）", endpoint.name, endpoint.slug)
    return endpoint


async def rotate_token(endpoint_id: str) -> tuple[McpEndpoint, str]:
    """轮换令牌：旧令牌立即失效，新明文仅本次返回。"""
    config = await get_config()
    endpoint = _find(config, endpoint_id)
    plaintext = generate_token()
    endpoint.token_hash = hash_token(plaintext)
    endpoint.token_hint = token_hint(plaintext)
    await _save(config)
    logger.info("已轮换 MCP 端点「%s」的令牌，旧令牌即刻失效", endpoint.name)
    return endpoint, plaintext


async def delete_endpoint(endpoint_id: str) -> None:
    config = await get_config()
    endpoint = _find(config, endpoint_id)
    config.endpoints = [e for e in config.endpoints if e.id != endpoint_id]
    await _save(config)
    _touched_at.pop(endpoint_id, None)
    logger.info("已删除 MCP 端点「%s」（/mcp/%s）", endpoint.name, endpoint.slug)


async def set_global_enabled(enabled: bool) -> McpEndpointsSetting:
    """MCP 总开关。关闭后所有端点一律 404——对外完全隐身，不是 403。"""
    config = await get_config()
    config.enabled = enabled
    await _save(config)
    logger.info("MCP 服务总开关已%s", "开启" if enabled else "关闭")
    return config


async def lookup_endpoint(slug: str) -> McpEndpoint | None:
    """按地址标识找一个**可服务**的端点：总开关开着、端点自身也启用。

    找不到时调用方回 404（不是 403）——停用的端点与不存在的地址对外表现一致，
    等于对外完全隐身。
    """
    config = await get_config()
    if not config.enabled:
        return None
    return next((e for e in config.endpoints if e.slug == slug and e.enabled), None)


def verify_token(endpoint: McpEndpoint, token: str | None) -> bool:
    """常量时间比对端点令牌。"""
    if not token:
        return False
    return hmac.compare_digest(hash_token(token), endpoint.token_hash)


async def touch(endpoint_id: str) -> None:
    """记录端点的最近调用时间（按分钟粒度落盘）。"""
    now = time.monotonic()
    if now - _touched_at.get(endpoint_id, 0.0) < _TOUCH_INTERVAL_S:
        return
    _touched_at[endpoint_id] = now
    config = await get_config()
    for endpoint in config.endpoints:
        if endpoint.id == endpoint_id:
            endpoint.last_used_at = _now_iso()
            await _save(config)
            return
