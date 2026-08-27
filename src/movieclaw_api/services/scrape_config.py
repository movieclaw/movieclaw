"""刮削与发现页偏好的运行时装配：配置域 ↔ 刮削管线/发现页服务的桥。

与 ``network_egress`` 同一套模式（docs/design/scrape-customization.md §1）：

- 启动时（lifespan）与设置保存后，把 ``MetadataScrapeSetting`` /
  ``DiscoverPreferencesSetting`` 同步一份到模块态快照——settings store 是
  async 的，而刮削管线里的部分读取点（``_asset_sizes`` 等）是同步函数；
- 向消费方提供"生效值"（``effective_*``）：设置页的值优先，空则回落到
  环境变量/内置默认——保证只配 env 的老部署行为完全不变。

院线地区（region）改动会影响发现页服务单例（region 绑死在构造期），
保存接口在调用 ``save_discover_prefs`` 后自行 ``reset_media_service``。
"""

from __future__ import annotations

import logging

from movieclaw_api.core.config import get_settings
from movieclaw_api.settings import DiscoverPreferencesSetting, MetadataScrapeSetting
from movieclaw_api.settings.store import get_setting_store
from movieclaw_media import ImagePrefs

logger = logging.getLogger("movieclaw_api.scrape_config")

_current_scrape: MetadataScrapeSetting | None = None
_current_discover: DiscoverPreferencesSetting | None = None


async def load_scrape_runtime() -> None:
    """从配置域加载刮削/发现页偏好到快照。应用启动与设置保存后各调一次。"""
    global _current_scrape, _current_discover
    store = get_setting_store()
    _current_scrape = await store.get(MetadataScrapeSetting)
    _current_discover = await store.get(DiscoverPreferencesSetting)


async def save_scrape_setting(setting: MetadataScrapeSetting) -> MetadataScrapeSetting:
    """保存刮削偏好并立即生效，返回保存后的值。"""
    global _current_scrape
    await get_setting_store().set(setting)
    _current_scrape = setting
    return setting


async def save_discover_prefs(setting: DiscoverPreferencesSetting) -> DiscoverPreferencesSetting:
    """保存发现页偏好并立即生效（发现页服务单例由调用方按需重建）。"""
    global _current_discover
    await get_setting_store().set(setting)
    _current_discover = setting
    return setting


def current_scrape_setting() -> MetadataScrapeSetting:
    """当前生效的刮削偏好快照；启动加载前调用则按默认值处理。"""
    return _current_scrape or MetadataScrapeSetting()


def current_discover_prefs() -> DiscoverPreferencesSetting:
    return _current_discover or DiscoverPreferencesSetting()


def reset_scrape_config() -> None:
    """仅供测试：清空快照，回到未加载状态。"""
    global _current_scrape, _current_discover
    _current_scrape = None
    _current_discover = None


# ---------------------------------------------------------------------------
# 生效值：设置页覆盖 > 环境变量/内置默认
# ---------------------------------------------------------------------------


def effective_languages() -> list[str]:
    """元数据语言优先级（非空）。未配置时跟随 TMDB_LANGUAGE 并以 en-US 兜底
    ——正是历史写死行为（主语言请求 + 简介英文兜底）的优先级表达。"""
    configured = current_scrape_setting().language_priority
    if configured:
        return list(configured)
    primary = get_settings().tmdb_language
    return [primary] if primary.lower().startswith("en") else [primary, "en-US"]


def effective_language() -> str:
    """主语言（请求语言），替代散落各处的 ``get_settings().tmdb_language``。"""
    return effective_languages()[0]


def effective_cert_countries() -> list[str]:
    return list(current_scrape_setting().cert_country_priority)


def effective_asset_sizes() -> tuple[str, str, str]:
    """(海报, 背景, 剧照) 档位：设置页的值优先，空则跟随环境变量。"""
    setting = current_scrape_setting()
    env = get_settings()
    return (
        setting.poster_size or env.tmdb_poster_size,
        setting.backdrop_size or env.tmdb_backdrop_size,
        setting.still_size or env.tmdb_still_size,
    )


def effective_region() -> str:
    """发现页院线地区：设置页的值优先，空则跟随 TMDB_REGION。"""
    return current_discover_prefs().region or get_settings().tmdb_region


def effective_image_prefs() -> ImagePrefs:
    """选图偏好 → 领域层 ``ImagePrefs``。"""
    setting = current_scrape_setting()
    return ImagePrefs(
        poster_mode=setting.poster_mode,
        poster_langs=tuple(setting.poster_language_priority),
        backdrop_langs=tuple(setting.backdrop_language_priority),
        poster_min_width=setting.poster_min_width,
        backdrop_min_width=setting.backdrop_min_width,
    )


def profile_fetch_kwargs() -> dict:
    """``fetch_media_profile`` 的偏好参数包（建档与刷新共用，口径一致）。"""
    return {
        "languages": effective_languages(),
        "image_prefs": effective_image_prefs(),
        "cert_countries": effective_cert_countries(),
    }
