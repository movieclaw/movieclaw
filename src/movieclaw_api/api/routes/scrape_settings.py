"""刮削与整理设置接口（「设置 → 刮削与整理」页的后端）。

- GET  /scrape/config —— 当前配置 + 各"跟随环境变量"字段的生效默认值；
- PUT  /scrape/config —— **只更新请求体里出现的字段**，其余保持原样，
  保存后立即生效（快照热更新；对存量条目生效需整库刷新，前端保存后
  给出引导）。

为什么 PUT 是"局部更新"而不是整体替换：这是一份**十几个字段的单例配置**，
Web 端总是整体提交（行为无差别），但 CLI 与 Agent 天然是"只改一项"的用法
——``mclaw scrape set --poster-mode language`` 如果把没提到的字段一并按
默认值写回去，用户精心配好的语言优先级和命名模板会被一条命令悄悄抹掉。
想把某项恢复默认，显式传空值（``""`` / ``[]``）即可。
- GET  /scrape/language-options —— 完整语种表（TMDB configuration/languages，
  进程内缓存；TMDB 不可用时回落内置常用表）；
- GET  /scrape/country-options —— 完整地区表（configuration/countries，同上）。

配置的运行时装配见 ``services/scrape_config.py``。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.media_discover import get_tmdb_client, reset_media_service
from movieclaw_api.services.scrape_config import (
    current_scrape_setting,
    effective_asset_sizes,
    effective_cert_countries,
    effective_languages,
    save_scrape_setting,
)
from movieclaw_api.settings import MetadataScrapeSetting

logger = logging.getLogger("movieclaw_api.scrape_settings")

router = APIRouter(prefix="/scrape", tags=["scrape"])


class ScrapeEffectiveView(BaseModel):
    """ "跟随环境变量"字段当前的生效值（前端展示"跟随中：xxx"用）。"""

    language_priority: list[str]
    cert_country_priority: list[str]
    poster_size: str
    backdrop_size: str
    still_size: str


class ScrapeConfigView(BaseModel):
    setting: MetadataScrapeSetting
    effective: ScrapeEffectiveView


class LanguageOption(BaseModel):
    code: str = Field(description="ISO 639-1 语言码（zh / en / ja …）")
    name: str = Field(description="该语言的本族名（TMDB name，缺失回落英文名）")
    english_name: str = ""


class CountryOption(BaseModel):
    code: str = Field(description="ISO 3166-1 地区码（CN / US …）")
    name: str = Field(description="地区中文名（TMDB native_name，缺失回落英文名）")


def _config_view() -> ScrapeConfigView:
    return ScrapeConfigView(
        setting=current_scrape_setting(),
        effective=ScrapeEffectiveView(
            language_priority=effective_languages(),
            cert_country_priority=effective_cert_countries(),
            poster_size=effective_asset_sizes()[0],
            backdrop_size=effective_asset_sizes()[1],
            still_size=effective_asset_sizes()[2],
        ),
    )


@router.get(
    "/config",
    response_model=ApiResponse[ScrapeConfigView],
    summary="读取刮削与整理配置",
    operation_id="scrape.show",
)
async def get_scrape_config() -> ApiResponse[ScrapeConfigView]:
    """返回 ``setting``（用户配置，留空表示跟随环境变量）与 ``effective``
    （留空字段当前实际生效的值）。改配置前先读一遍，避免把留空当成没配。"""
    return ok(_config_view())


@router.put(
    "/config",
    response_model=ApiResponse[ScrapeConfigView],
    summary="修改刮削与整理配置：只更新给出的字段，没给的保持原样（立即生效）",
    operation_id="scrape.set",
)
async def save_scrape_config(payload: MetadataScrapeSetting) -> ApiResponse[ScrapeConfigView]:
    """只改你给出的那几项，没给的保持原样；想把某项恢复默认就显式传空值（"" 或 []）。

    优先级类字段是有序列表，越靠前越优先。language_priority 的首位就是向
    TMDB 请求的语言，后面的用于该语言缺文本时逐级回落。图片语言里三个特殊
    取值：meta = 跟随元数据主语言，orig = 影片发行时的原始语言，null = 无
    文字的纯画面版本。

    例：只把海报改成按语言挑，其余配置不动——
    mclaw scrape set --poster-mode language

    改动只影响之后的刮削与整理，不会追改存量内容。改了语言、选图或图片档位，
    用 library metadata refresh 重刷存量条目；改了命名模板，用 library
    organize-files 把存量文件改名归位（先 --dry-run 看计划，确认后加 --yes）。
    模板可以反复改、反复整理，条目目录改名时海报和 NFO 会一起搬走，不留空目录。
    """
    # model_fields_set 区分"显式传了默认值"和"根本没传"：前者按用户意图写入
    # （这正是"恢复默认"的表达方式），后者原样保留
    merged = MetadataScrapeSetting.model_validate(
        {**current_scrape_setting().model_dump(), **payload.model_dump(exclude_unset=True)}
    )
    await save_scrape_setting(merged)
    # 主语言也喂给发现页服务（构造期绑定），重建单例让新语言下次请求生效
    reset_media_service()
    return ok(_config_view())


# ---------------------------------------------------------------------------
# 完整语种/地区表：TMDB configuration 接口 + 进程内缓存 + 内置回落
# ---------------------------------------------------------------------------

# 内置常用表：TMDB 不可用（未配 Key / 断网）时设置页仍能工作的最小集合
_BUILTIN_LANGUAGES = [
    {"code": "zh", "name": "中文", "english_name": "Chinese"},
    {"code": "en", "name": "English", "english_name": "English"},
    {"code": "ja", "name": "日本語", "english_name": "Japanese"},
    {"code": "ko", "name": "한국어", "english_name": "Korean"},
    {"code": "fr", "name": "Français", "english_name": "French"},
    {"code": "de", "name": "Deutsch", "english_name": "German"},
    {"code": "es", "name": "Español", "english_name": "Spanish"},
    {"code": "it", "name": "Italiano", "english_name": "Italian"},
    {"code": "ru", "name": "Pусский", "english_name": "Russian"},
    {"code": "pt", "name": "Português", "english_name": "Portuguese"},
    {"code": "th", "name": "ภาษาไทย", "english_name": "Thai"},
    {"code": "hi", "name": "हिन्दी", "english_name": "Hindi"},
]
_BUILTIN_COUNTRIES = [
    {"code": "CN", "name": "中国"},
    {"code": "US", "name": "美国"},
    {"code": "JP", "name": "日本"},
    {"code": "KR", "name": "韩国"},
    {"code": "GB", "name": "英国"},
    {"code": "FR", "name": "法国"},
    {"code": "DE", "name": "德国"},
    {"code": "HK", "name": "香港"},
    {"code": "TW", "name": "台湾"},
    {"code": "IN", "name": "印度"},
]

# 进程内缓存：语种/地区表几乎不变，进程生命周期内取一次即可
_language_cache: list[LanguageOption] | None = None
_country_cache: list[CountryOption] | None = None


@router.get(
    "/language-options",
    response_model=ApiResponse[list[LanguageOption]],
    summary="完整语种表（供「更多语言」搜索面板）",
    operation_id="scrape.languages",
)
async def list_language_options() -> ApiResponse[list[LanguageOption]]:
    global _language_cache
    if _language_cache is None:
        try:
            raw = await get_tmdb_client().get("configuration/languages", {})
            options = [
                LanguageOption(
                    code=entry["iso_639_1"],
                    name=(entry.get("name") or "").strip()
                    or (entry.get("english_name") or "").strip()
                    or entry["iso_639_1"],
                    english_name=(entry.get("english_name") or "").strip(),
                )
                for entry in raw
                if isinstance(entry, dict) and entry.get("iso_639_1")
            ]
            _language_cache = sorted(options, key=lambda o: o.code)
        except Exception as exc:  # noqa: BLE001 -- 全量表拉不到回落内置常用表
            logger.warning("TMDB 语种表拉取失败，回落内置常用表：%s", exc)
            return ok([LanguageOption(**entry) for entry in _BUILTIN_LANGUAGES])
    return ok(_language_cache)


@router.get(
    "/country-options",
    response_model=ApiResponse[list[CountryOption]],
    summary="完整地区表（供「更多地区」搜索面板）",
    operation_id="scrape.countries",
)
async def list_country_options() -> ApiResponse[list[CountryOption]]:
    global _country_cache
    if _country_cache is None:
        try:
            raw = await get_tmdb_client().get("configuration/countries", {"language": "zh-CN"})
            options = [
                CountryOption(
                    code=entry["iso_3166_1"],
                    name=(entry.get("native_name") or "").strip()
                    or (entry.get("english_name") or "").strip()
                    or entry["iso_3166_1"],
                )
                for entry in raw
                if isinstance(entry, dict) and entry.get("iso_3166_1")
            ]
            _country_cache = sorted(options, key=lambda o: o.code)
        except Exception as exc:  # noqa: BLE001 -- 同语种表：回落内置常用表
            logger.warning("TMDB 地区表拉取失败，回落内置常用表：%s", exc)
            return ok([CountryOption(**entry) for entry in _BUILTIN_COUNTRIES])
    return ok(_country_cache)
