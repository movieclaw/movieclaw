"""刮削与发现页偏好配置域（docs/design/scrape-customization.md）。

两个配置域：

- ``MetadataScrapeSetting``（「设置 → 刮削与整理」）：元数据语言优先级、
  分级地区、选图偏好、图片质量档位——把原先写死在代码里的刮削细节
  开放为可配置项，**默认值 = 现状行为**（跟随环境变量或代码内置）。
- ``DiscoverPreferencesSetting``（发现页页脚就地设置）：院线地区。

与环境变量的关系（设计文档 §1）：空值/空列表表示"跟随环境变量"，
运行时的合并读取在 ``services/scrape_config.py``（``effective_*``），
与 ``network_egress`` 的镜像地址覆盖同一套模式——保证只配 env 的
老部署行为完全不变。
"""

from __future__ import annotations

import re

from pydantic import Field, field_validator

from movieclaw_api.settings.base import SettingSchema, register_setting

# 语言标签：ISO 639-1，可带 ISO 3166-1 地区（zh-CN / en-US / ja）
_LANG_TAG = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
# 图片语言 token：具体语言码之外的三个特殊项（设计文档 §2.2）——
# meta=跟随元数据主语言；orig=条目的原声语言（original_language，随条目解析）；
# null=无文字（TMDB 把未烧录文字的图标记为语言 null）
_IMAGE_TOKEN = re.compile(r"^(meta|orig|null|[a-z]{2})$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")

# TMDB 图床合法档位（configuration 接口的稳定集合）；空串 = 跟随环境变量
POSTER_SIZES = {"", "w92", "w154", "w185", "w342", "w500", "w780", "original"}
BACKDROP_SIZES = {"", "w300", "w780", "w1280", "original"}
STILL_SIZES = {"", "w92", "w185", "w300", "original"}


def _dedup(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


@register_setting(namespace="metadata.scrape", title="刮削与整理")
class MetadataScrapeSetting(SettingSchema):
    """刮削管线的用户偏好。所有字段的默认值即当前写死的行为。"""

    # —— STEP 1 元数据 ——————————————————————————————
    # 空列表 = 跟随环境变量 TMDB_LANGUAGE（并以 en-US 兜底），保证老部署不变
    language_priority: list[str] = Field(
        default_factory=list,
        description="元数据语言优先级（1~3 项）；首位为主语言（请求语言），"
        "空 = 跟随环境变量 TMDB_LANGUAGE + en-US 兜底",
    )
    cert_country_priority: list[str] = Field(
        default_factory=lambda: ["CN", "US"],
        description="内容分级的地区优先级：按顺序取第一个有分级数据的地区",
    )

    # —— STEP 2 图片 ————————————————————————————————
    poster_mode: str = Field(
        default="default",
        description="海报选择：default=TMDB 默认（与发现页一致）；language=按语言优先级挑选",
    )
    poster_language_priority: list[str] = Field(
        default_factory=lambda: ["meta", "en", "null"],
        description="海报语言优先级（poster_mode=language 时生效）",
    )
    backdrop_language_priority: list[str] = Field(
        default_factory=lambda: ["null", "meta", "en"],
        description="背景图语言优先级；「无文字」排首位即无文字优先（现状）",
    )
    poster_min_width: int = Field(
        default=500, ge=0, le=10000, description="海报最低宽度门槛；0 = 不限制"
    )
    backdrop_min_width: int = Field(
        default=1920, ge=0, le=10000, description="背景图最低宽度门槛；0 = 不限制"
    )
    # 空串 = 跟随环境变量 TMDB_POSTER_SIZE / TMDB_BACKDROP_SIZE / TMDB_STILL_SIZE
    poster_size: str = Field(default="", description="海报档位；空 = 跟随环境变量")
    backdrop_size: str = Field(default="", description="背景档位；空 = 跟随环境变量")
    still_size: str = Field(default="", description="分集剧照档位；空 = 跟随环境变量")

    # —— STEP 3 命名与整理 ————————————————————————————
    # 空串 = 用内置默认模板（即模板化之前的写死行为）。模板语法与校验见
    # services/library/naming.py；层级结构固定，模板只描述一段名字
    naming_entry_dir: str = Field(
        default="", description="条目目录模板；空 = 默认 {title} ({year})"
    )
    naming_movie_file: str = Field(
        default="", description="电影文件名模板；空 = 默认 {title} ({year})"
    )
    naming_season_dir: str = Field(
        default="", description="季目录模板；空 = 默认 Season {season:02d}"
    )
    naming_episode_file: str = Field(
        default="",
        description="剧集文件名模板；空 = 默认 {title} ({year}) - S{season:02d}E{episode:02d}",
    )

    # —— STEP 4 目录写入（细项；库上的 write_media_assets 是总闸）——————
    # 默认全开 = 拆分之前的行为：总闸开着就三样都写
    mirror_images: bool = Field(
        default=True, description="镜像条目图片到媒体目录（poster/fanart/季海报）"
    )
    mirror_nfo: bool = Field(default=True, description="镜像 NFO 元数据到媒体目录")
    mirror_episode_thumbs: bool = Field(
        default=True, description="镜像分集剧照（长剧集写入量最大，可单独关）"
    )

    # —— 校验 ————————————————————————————————————————
    @field_validator("language_priority")
    @classmethod
    def _check_languages(cls, value: list[str]) -> list[str]:
        value = _dedup([v.strip() for v in value if v.strip()])
        if len(value) > 3:
            raise ValueError("元数据语言优先级最多 3 项")
        for tag in value:
            if not _LANG_TAG.match(tag):
                raise ValueError(f"语言标签格式不正确：{tag}（应为 zh-CN / en-US / ja 形式）")
        return value

    @field_validator("cert_country_priority")
    @classmethod
    def _check_cert_countries(cls, value: list[str]) -> list[str]:
        value = _dedup([v.strip().upper() for v in value if v.strip()])
        if not value:
            raise ValueError("分级地区至少保留一项")
        if len(value) > 6:
            raise ValueError("分级地区最多 6 项")
        for code in value:
            if not _COUNTRY.match(code):
                raise ValueError(f"地区码格式不正确：{code}（应为 CN / US 形式）")
        return value

    @field_validator("poster_mode")
    @classmethod
    def _check_poster_mode(cls, value: str) -> str:
        if value not in {"default", "language"}:
            raise ValueError("海报选择只能是 default（TMDB 默认）或 language（按语言优先级）")
        return value

    @field_validator("poster_language_priority", "backdrop_language_priority")
    @classmethod
    def _check_image_tokens(cls, value: list[str]) -> list[str]:
        value = _dedup([v.strip() for v in value if v.strip()])
        if not value:
            raise ValueError("图片语言优先级至少保留一项")
        if len(value) > 4:
            raise ValueError("图片语言优先级最多 4 项")
        for token in value:
            if not _IMAGE_TOKEN.match(token):
                raise ValueError(f"图片语言项不合法：{token}（应为语言码或 meta/orig/null 特殊项）")
        return value

    @field_validator(
        "naming_entry_dir", "naming_movie_file", "naming_season_dir", "naming_episode_file"
    )
    @classmethod
    def _check_naming(cls, value: str, info) -> str:
        """空串 = 用默认模板；非空则按该字段的可用占位符与防重名规则校验。

        校验逻辑放在 naming.py（渲染与校验同源，规则改一处），此处延迟导入
        避开 settings → naming → scrape_config → settings 的导入环。
        """
        value = value.strip()
        if not value:
            return value
        from movieclaw_api.services.library.naming import validate_template

        field = info.field_name.removeprefix("naming_")
        error = validate_template(field, value)
        if error is not None:
            raise ValueError(error)
        return value

    @field_validator("poster_size")
    @classmethod
    def _check_poster_size(cls, value: str) -> str:
        if value not in POSTER_SIZES:
            raise ValueError(f"海报档位不合法：{value}（可选 {sorted(POSTER_SIZES - {''})}）")
        return value

    @field_validator("backdrop_size")
    @classmethod
    def _check_backdrop_size(cls, value: str) -> str:
        if value not in BACKDROP_SIZES:
            raise ValueError(f"背景档位不合法：{value}（可选 {sorted(BACKDROP_SIZES - {''})}）")
        return value

    @field_validator("still_size")
    @classmethod
    def _check_still_size(cls, value: str) -> str:
        if value not in STILL_SIZES:
            raise ValueError(f"剧照档位不合法：{value}（可选 {sorted(STILL_SIZES - {''})}）")
        return value


@register_setting(namespace="discover.preferences", title="发现页偏好")
class DiscoverPreferencesSetting(SettingSchema):
    """发现页的展示口径。院线地区就地内联在发现页页脚，不进设置页。"""

    # 空 = 跟随环境变量 TMDB_REGION
    region: str = Field(
        default="",
        description="「正在热映/即将上映」的院线地区；空 = 跟随环境变量 TMDB_REGION",
    )

    @field_validator("region")
    @classmethod
    def _check_region(cls, value: str) -> str:
        value = value.strip().upper()
        if value and not _COUNTRY.match(value):
            raise ValueError(f"地区码格式不正确：{value}（应为 CN / US 形式）")
        return value
