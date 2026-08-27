"""影视元数据服务的装配层：读配置、组装 movieclaw_media 的进程级单例。

movieclaw_media 是不感知配置来源的独立领域包（不 import movieclaw_api），
这里负责把 Settings 里的 TMDB 配置喂给它，并管理单例的生命周期
（懒加载创建、应用关闭时释放连接池、测试时重置）。
"""

from __future__ import annotations

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.network_egress import (
    effective_douban_api_base_url,
    effective_tmdb_api_base_url,
    effective_tmdb_image_base_url,
)
from movieclaw_api.services.scrape_config import effective_language, effective_region
from movieclaw_db.stores import SqlCacheStore
from movieclaw_media import (
    DoubanClient,
    DoubanDiscoverService,
    MediaDiscoverService,
    TmdbClient,
    TmdbNotConfiguredError,
)
from movieclaw_net import browser_tls_context, egress_transport

_service: MediaDiscoverService | None = None
_douban_service: DoubanDiscoverService | None = None
_tmdb_client: TmdbClient | None = None


def get_tmdb_client() -> TmdbClient:
    """取媒体身份层（订阅建档/豆瓣收敛）共用的 TmdbClient 单例。

    与发现页服务的内部 client 分开持有：发现页服务把 client 作为私有实现
    细节封装，这里不去掏它——两个连接池的代价可忽略，边界干净更重要。
    """
    global _tmdb_client
    if _tmdb_client is None:
        settings = get_settings()
        if not settings.tmdb_api_key:
            raise TmdbNotConfiguredError(
                "尚未配置 TMDB API Key，订阅功能不可用。请在 .env（或环境变量）中设置 "
                "TMDB_API_KEY 后重启服务；Key 可在 themoviedb.org 的账户设置 → API 页免费申请"
            )
        _tmdb_client = TmdbClient(
            settings.tmdb_api_key,
            base_url=effective_tmdb_api_base_url(),
            transport=egress_transport("tmdb"),
        )
    return _tmdb_client


def get_media_service() -> MediaDiscoverService:
    """取进程级单例；未配置 TMDB API Key 时抛出可读的配置引导错误。"""
    global _service
    if _service is None:
        settings = get_settings()
        if not settings.tmdb_api_key:
            raise TmdbNotConfiguredError(
                "尚未配置 TMDB API Key，发现页暂不可用。请在 .env（或环境变量）中设置 "
                "TMDB_API_KEY 后重启服务；Key 可在 themoviedb.org 的账户设置 → API 页免费申请"
            )
        _service = MediaDiscoverService(
            # 交互档位：发现页是用户在等的请求，超时/重试压小；线路不通时由
            # 出口层熔断器把后续失败压到毫秒级，前端立刻拿到引导错误
            TmdbClient(
                settings.tmdb_api_key,
                base_url=effective_tmdb_api_base_url(),
                timeout=8.0,
                max_attempts=2,
                transport=egress_transport("tmdb"),
            ),
            image_base_url=effective_tmdb_image_base_url(),
            # 语言/地区走设置页覆盖（空则回落环境变量）；设置保存后由保存
            # 接口 reset_media_service()，下次请求按新值重建本单例
            language=effective_language(),
            region=effective_region(),
            timezone=settings.scheduler_timezone,
        )
    return _service


def get_douban_media_service() -> DoubanDiscoverService:
    """取豆瓣榜单服务单例；它不依赖 TMDB API Key。"""
    global _douban_service
    if _douban_service is None:
        # 注入 SQLite 持久缓存：豆瓣榜单/详情跨重启不丢，冷启动不再突发回源
        _douban_service = DoubanDiscoverService(
            DoubanClient(
                base_url=effective_douban_api_base_url(),
                store=SqlCacheStore(),
                # 豆瓣 API 域名同样落在腾讯云节点上，防御性使用浏览器 TLS 指纹，
                # 避免部分边缘节点按 JA3 拦截 Python 默认配置（同图片代理的修复）
                transport=egress_transport("douban", verify=browser_tls_context()),
            )
        )
    return _douban_service


async def close_media_service() -> None:
    """关闭单例持有的 HTTP 连接池（应用关闭时由 lifespan 调用）。"""
    global _service, _douban_service, _tmdb_client
    if _service is not None:
        await _service.aclose()
        _service = None
    if _douban_service is not None:
        await _douban_service.aclose()
        _douban_service = None
    if _tmdb_client is not None:
        await _tmdb_client.aclose()
        _tmdb_client = None


def reset_media_service() -> None:
    """仅供测试：丢弃单例，让下个用例按新配置重建。"""
    global _service, _douban_service, _tmdb_client
    _service = None
    _douban_service = None
    _tmdb_client = None
