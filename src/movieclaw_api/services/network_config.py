"""网络与代理设置的业务服务（routes/network 的实现层）。

职责：
- 配置视图装配（配置本体 + 服务开关目录 + 镜像默认值 + 环境代理探测）；
- 保存编排：校验 → 落库热切换 → 镜像变更时重建媒体服务 → 闭合全部熔断；
- 连通性测试：按服务标签解析探测目标（LLM 端点解析复用 llm_config，
  不另立判据）→ 发一次最小请求 → 结果分类为中文结论。

路由层只做「取参 → 调本模块 → ok()」，不再直接接触 Repository 与密钥。
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.network import (
    EgressServiceOption,
    NetworkConfigPayload,
    NetworkConfigView,
    NetworkTestResult,
)
from movieclaw_api.services.media_discover import close_media_service
from movieclaw_api.services.network_egress import (
    current_network_setting,
    effective_douban_api_base_url,
    effective_tmdb_api_base_url,
    effective_tmdb_image_base_url,
    save_network_egress,
)
from movieclaw_api.settings import (
    BUILTIN_EGRESS_SERVICES,
    NetworkEgressSetting,
    WebhookSetting,
    get_setting_store,
)
from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository
from movieclaw_db.repositories.credential_repo import CredentialRepository
from movieclaw_net import (
    PROXY_SCHEMES,
    egress_transport,
    env_proxy_url,
    get_breaker,
    reset_all_breakers,
)
from movieclaw_tracker import get_site_config
from movieclaw_tracker.exceptions import SiteNotFoundError

logger = logging.getLogger("movieclaw_api.network_config")


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------


async def _service_catalog(session: AsyncSession) -> list[EgressServiceOption]:
    """开关目录 = 内置服务 + 已配置的 PT 站（site:<id>）。"""
    options = [EgressServiceOption(**item) for item in BUILTIN_EGRESS_SERVICES]
    for cred in await CredentialRepository(session).list_all():
        try:
            display_name = get_site_config(cred.site_id).display_name
        except SiteNotFoundError:
            display_name = cred.site_id  # 站点定义被移除时仍展示，保留用户配置
        options.append(
            EgressServiceOption(
                id=f"site:{cred.site_id}",
                label=display_name,
                description="PT 站点（国内直连通常更快，按需开启）",
            )
        )
    return options


async def build_config_view(session: AsyncSession) -> NetworkConfigView:
    """装配设置页所需的完整视图。"""
    setting = current_network_setting()
    env_settings = get_settings()
    return NetworkConfigView(
        proxy_mode=setting.proxy_mode,
        proxy_url=setting.proxy_url,
        proxy_services=setting.proxy_services,
        tmdb_api_base_url=setting.tmdb_api_base_url,
        tmdb_image_base_url=setting.tmdb_image_base_url,
        douban_api_base_url=setting.douban_api_base_url,
        services=await _service_catalog(session),
        mirror_defaults={
            "tmdb_api_base_url": env_settings.tmdb_api_base_url,
            "tmdb_image_base_url": env_settings.tmdb_image_base_url,
            "douban_api_base_url": env_settings.douban_api_base_url,
        },
        env_proxy_detected=env_proxy_url() or "",
    )


def _validate_payload(payload: NetworkConfigPayload) -> None:
    """保存前校验：代理地址协议、镜像地址格式。错误信息中文直达前端。"""
    proxy_url = payload.proxy_url.strip()
    if payload.proxy_mode == "manual":
        if not proxy_url:
            raise BadRequestException("手动代理模式必须填写代理地址")
        scheme = urlsplit(proxy_url).scheme.lower()
        if scheme not in PROXY_SCHEMES:
            raise BadRequestException(
                f"代理地址协议不支持：{scheme or '（缺失）'}，支持 {'/'.join(PROXY_SCHEMES)}"
            )
    for name, value in (
        ("TMDB 接口镜像", payload.tmdb_api_base_url),
        ("TMDB 图床镜像", payload.tmdb_image_base_url),
        ("豆瓣接口地址", payload.douban_api_base_url),
    ):
        value = value.strip()
        if value and urlsplit(value).scheme not in ("http", "https"):
            raise BadRequestException(f"{name}必须是 http(s) 地址")


async def save_config(session: AsyncSession, payload: NetworkConfigPayload) -> NetworkConfigView:
    """校验并保存网络配置，立即生效（代理热切换 + 镜像变更重建媒体服务）。"""
    _validate_payload(payload)
    mirrors_before = (
        effective_tmdb_api_base_url(),
        effective_tmdb_image_base_url(),
        effective_douban_api_base_url(),
    )
    await save_network_egress(
        NetworkEgressSetting(
            proxy_mode=payload.proxy_mode,
            proxy_url=payload.proxy_url.strip(),
            proxy_services=sorted(set(payload.proxy_services)),
            tmdb_api_base_url=payload.tmdb_api_base_url.strip(),
            tmdb_image_base_url=payload.tmdb_image_base_url.strip(),
            douban_api_base_url=payload.douban_api_base_url.strip(),
        )
    )
    # 代理路由靠 transport 的 epoch 热切换，不需要重建；但镜像地址绑死在
    # 客户端构造期，变了就重建媒体服务单例（下次请求按新地址懒加载）
    mirrors_after = (
        effective_tmdb_api_base_url(),
        effective_tmdb_image_base_url(),
        effective_douban_api_base_url(),
    )
    if mirrors_before != mirrors_after:
        await close_media_service()
        logger.info("镜像地址已变更，媒体服务将按新地址重建")
    # 网络配置变了，之前的失败统计不再有参考意义——闭合全部熔断重新试
    reset_all_breakers()
    return await build_config_view(session)


# ---------------------------------------------------------------------------
# 连通性测试
# ---------------------------------------------------------------------------


async def _probe_target(service: str, session: AsyncSession) -> tuple[str, dict[str, str]]:
    """返回某服务的探测 URL 与请求头；未配置/未知服务抛 BadRequest。"""
    if service == "tmdb":
        settings = get_settings()
        url = f"{effective_tmdb_api_base_url().rstrip('/')}/configuration"
        headers: dict[str, str] = {}
        key = settings.tmdb_api_key or ""
        if key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {key}"
        elif key:
            url += f"?api_key={key}"
        return url, headers
    if service == "douban":
        base = effective_douban_api_base_url().rstrip("/")
        return f"{base}/subject_collection/movie_hot_gaia/items?start=0&count=1", {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            "Referer": "https://m.douban.com/movie/",
        }
    if service == "image":
        return effective_tmdb_image_base_url(), {}
    if service == "llm":
        # 端点与密钥的解析复用 llm_config（唯一判据来源）；这里只做
        # 轻量连通性探测（/models），完整有效性验证仍归 verify_llm_provider
        from movieclaw_api.services.llm_config import resolve_provider_endpoint

        base, api_key, user_agent = await resolve_provider_endpoint(session)
        headers = {"Authorization": f"Bearer {api_key}"}
        # 配了自定义 UA 就一并带上：网关按 UA 放行时，探测必须与真实调用
        # 发同样的头，否则测试结论与实际能否调通不一致
        if user_agent:
            headers["User-Agent"] = user_agent
        return f"{base.rstrip('/')}/models", headers
    if service in ("telegram", "discord"):
        channel_name = "Telegram" if service == "telegram" else "Discord"
        accounts = await ChannelAccountRepository(session).list_by_channel(service)
        if not accounts:
            raise BadRequestException(f"尚未绑定 {channel_name} bot，无法测试")
        try:
            token = ChannelAccountRepository.decrypted_token(accounts[0])
        except Exception as exc:  # noqa: BLE001 -- 凭据损坏时返回可操作的中文错误
            raise BadRequestException(
                f"{channel_name} bot 凭据不可用，请重新绑定"
            ) from exc
        if service == "telegram":
            return f"https://api.telegram.org/bot{token}/getMe", {}
        return "https://discord.com/api/v10/users/@me", {"Authorization": f"Bot {token}"}
    if service == "webhook":
        setting = await get_setting_store().get(WebhookSetting)
        endpoint = next(
            (item for item in setting.endpoints if item.enabled and item.url.strip()),
            None,
        )
        if endpoint is None:
            raise BadRequestException("尚未配置可用的 Webhook endpoint，无法测试")
        return endpoint.url, dict(endpoint.headers)
    if service == "github":
        # 与应用内更新的检查请求同源（api.github.com 或 UPDATE_API_BASE_URL 反代）
        settings = get_settings()
        url = (
            f"{settings.update_api_base_url.rstrip('/')}"
            f"/repos/{settings.update_repo}/releases?per_page=1"
        )
        return url, {
            "Accept": "application/vnd.github+json",
            "User-Agent": "movieclaw-updater",
        }
    if service.startswith("site:"):
        site_id = service.removeprefix("site:")
        try:
            return get_site_config(site_id).base_url, {}
        except SiteNotFoundError as exc:
            raise BadRequestException(f"未知站点：{site_id}") from exc
    raise BadRequestException(f"未知的服务标签：{service}")


def _classify_probe(service: str, status_code: int) -> NetworkTestResult:
    """探测拿到了 HTTP 响应 = 线路通；再按服务语义细化提示。"""
    if service == "tmdb":
        if status_code == 200:
            message = "网络连通，API Key 有效"
        elif status_code == 401:
            message = "网络连通，但 TMDB API Key 无效或未配置"
        else:
            message = f"网络连通（HTTP {status_code}）"
        return NetworkTestResult(ok=True, message=message)
    if service == "llm":
        if status_code == 200:
            message = "网络连通，API Key 有效"
        elif status_code in (401, 403):
            message = "网络连通，但 API Key 无效"
        else:
            message = f"网络连通（HTTP {status_code}）"
        return NetworkTestResult(ok=True, message=message)
    if service == "github":
        if status_code == 200:
            message = "网络连通，可正常检查更新"
        elif status_code in (403, 429):
            message = "网络连通，但 GitHub 接口限流中（稍后自动恢复，不影响每日自动检查）"
        else:
            message = f"网络连通（HTTP {status_code}）"
        return NetworkTestResult(ok=True, message=message)
    if service in ("telegram", "discord") and status_code in (401, 403, 404):
        channel_name = "Telegram" if service == "telegram" else "Discord"
        return NetworkTestResult(
            ok=True, message=f"网络连通，但 {channel_name} bot Token 无效"
        )
    return NetworkTestResult(ok=True, message=f"网络连通（HTTP {status_code}）")


async def test_service(session: AsyncSession, service: str) -> NetworkTestResult:
    """按服务标签发一次最小探测请求；测通顺手闭合该服务的熔断。"""
    url, headers = await _probe_target(service, session)
    started = time.perf_counter()
    try:
        # 绕过熔断（测试就是要真发请求），跟随重定向（站点首页常见 301）
        async with httpx.AsyncClient(
            transport=egress_transport(service, use_breaker=False),
            timeout=10.0,
            follow_redirects=True,
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return NetworkTestResult(
            ok=False, message="连接超时（10 秒无响应），当前出口无法访问该服务"
        )
    except httpx.HTTPError as exc:
        logger.info("连通性测试失败：service=%s（%s）", service, exc)
        return NetworkTestResult(ok=False, message=f"连接失败：{type(exc).__name__}（{exc}）")
    latency_ms = int((time.perf_counter() - started) * 1000)
    # 测通了就闭合该服务的熔断：业务请求立刻恢复，不用等冷却期
    get_breaker(service).reset()
    result = _classify_probe(service, response.status_code)
    result.latency_ms = latency_ms
    return result
