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
    """选图偏好 → 领域层 ``ImagePrefs``。

    **全局口径，不按库**：选出来的图存在 media_item / 条目资产目录里，
    跨库共享一份（见 LIBRARY_OVERRIDABLE 的说明）。
    """
    setting = current_scrape_setting()
    return ImagePrefs(
        poster_mode=setting.poster_mode,
        poster_langs=tuple(setting.poster_language_priority),
        backdrop_langs=tuple(setting.backdrop_language_priority),
        poster_min_width=setting.poster_min_width,
        backdrop_min_width=setting.backdrop_min_width,
    )


# ---------------------------------------------------------------------------
# 库级覆盖（docs/design/scrape-customization.md §1 / P3）
# ---------------------------------------------------------------------------

# 允许按库覆盖的字段。判据只有一条：**这个设置的产物是不是按库各存一份**。
#
# - 命名：产物是路径，落在各库自己的目录树里 → 可库级（电影库与剧集库、
#   动漫库各用一套模板正是收藏玩家的诉求）；
# - 目录写入细项：产物是写进各库目录的图片/NFO 文件 → 可库级；
# - **选图与图片档位不可库级**（2026-08-27 实现期修正，见设计文档 §9）：
#   poster_path / backdrop_path 存在 media_item 上、图片资产也按条目 id
#   存一份，**跨库共享**。同一条目分散在两个库是本项目的常态，两库配了
#   不同选图偏好就会轮流覆盖同一行、同一个文件——这与设计文档给"语言
#   不可库级"列的理由是同一个，草案把它标〔可库级〕是漏看了数据模型；
# - 语言与分级同理不可库级（同一份 media_metadata）。
LIBRARY_OVERRIDABLE = frozenset(
    {
        # 命名（产物落在各库自己的目录树）
        "naming_entry_dir",
        "naming_movie_file",
        "naming_season_dir",
        "naming_episode_file",
        # 目录写入细项（产物是写进各库目录的文件）
        "mirror_images",
        "mirror_nfo",
        "mirror_episode_thumbs",
    }
)


def sanitize_overrides(raw: object) -> dict:
    """把任意来源的覆盖对象收敛为「只含可覆盖字段」的干净字典。

    保存入口与读取端都过这一道：读取端也过是因为库行里可能残留旧版本
    写入的字段（字段被移出可覆盖集合后），静默忽略比报错合适。
    """
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in LIBRARY_OVERRIDABLE}


def merge_for_library(library: object | None) -> MetadataScrapeSetting:
    """全局设置 + 该库的显式覆盖 → 合并后的有效设置。

    ``library`` 为 None 或没有覆盖时直接返回全局快照（零拷贝，热路径常态）。
    非法覆盖值不会让整库刮削崩掉——校验失败就退回全局，只记一条告警。
    """
    global_setting = current_scrape_setting()
    overrides = sanitize_overrides(getattr(library, "scrape_overrides", None))
    if not overrides:
        return global_setting
    try:
        # 走一次完整校验而不是 model_copy：覆盖值来自库行的 JSON，可能是
        # 用户手改或旧版本残留，必须与保存入口同一套规则把关
        return MetadataScrapeSetting.model_validate({**global_setting.model_dump(), **overrides})
    except Exception as exc:  # noqa: BLE001 -- 脏覆盖不能拖垮刮削
        name = getattr(library, "name", "?")
        logger.warning("媒体库「%s」的刮削覆盖不合法，本次按全局设置处理：%s", name, exc)
        return global_setting


def effective_naming_templates(library: object | None = None):
    """该库生效的命名模板（内置默认 → 全局设置 → 库覆盖，逐字段回落）。"""
    from dataclasses import replace

    from movieclaw_api.services.library.naming import DEFAULT_TEMPLATES

    setting = merge_for_library(library)
    overrides = {
        field: value
        for field in ("entry_dir", "movie_file", "season_dir", "episode_file")
        if (value := getattr(setting, f"naming_{field}", "").strip())
    }
    return replace(DEFAULT_TEMPLATES, **overrides) if overrides else DEFAULT_TEMPLATES


def effective_mirror_flags(library: object | None) -> tuple[bool, bool, bool]:
    """(写图片, 写 NFO, 写分集剧照)。库上的 ``write_media_assets`` 是总闸，
    关掉则三项全关；细项默认全开（= 模板化之前的行为）。"""
    if library is not None and not getattr(library, "write_media_assets", True):
        return (False, False, False)
    setting = merge_for_library(library)
    return (setting.mirror_images, setting.mirror_nfo, setting.mirror_episode_thumbs)


def profile_fetch_kwargs() -> dict:
    """``fetch_media_profile`` 的偏好参数包（建档与刷新共用，口径一致）。"""
    return {
        "languages": effective_languages(),
        "image_prefs": effective_image_prefs(),
        "cert_countries": effective_cert_countries(),
    }
