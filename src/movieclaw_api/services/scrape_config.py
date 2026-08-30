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


# 下面这组 ``effective_*`` 都接受一个可选的 ``setting``：不传 = 全局快照
# （发现页、收藏范围路由等"选库之前"的读取点），传入 = 某条目按其**归属库**
# 合并出来的设置（见 ``scrape_setting_for_item``）。参数化而不是靠隐式上下文，
# 是为了让每个读取点用的是全局口径还是条目口径在调用处一眼可见。


def effective_languages(setting: MetadataScrapeSetting | None = None) -> list[str]:
    """元数据语言优先级（非空）。未配置时跟随 TMDB_LANGUAGE 并以 en-US 兜底
    ——正是历史写死行为（主语言请求 + 简介英文兜底）的优先级表达。"""
    configured = (setting or current_scrape_setting()).language_priority
    if configured:
        return list(configured)
    primary = get_settings().tmdb_language
    return [primary] if primary.lower().startswith("en") else [primary, "en-US"]


def effective_language(setting: MetadataScrapeSetting | None = None) -> str:
    """主语言（请求语言），替代散落各处的 ``get_settings().tmdb_language``。"""
    return effective_languages(setting)[0]


def effective_cert_countries(setting: MetadataScrapeSetting | None = None) -> list[str]:
    return list((setting or current_scrape_setting()).cert_country_priority)


def effective_asset_sizes(setting: MetadataScrapeSetting | None = None) -> tuple[str, str, str]:
    """(海报, 背景, 剧照) 档位：设置页的值优先，空则跟随环境变量。"""
    setting = setting or current_scrape_setting()
    env = get_settings()
    return (
        setting.poster_size or env.tmdb_poster_size,
        setting.backdrop_size or env.tmdb_backdrop_size,
        setting.still_size or env.tmdb_still_size,
    )


def effective_region() -> str:
    """发现页院线地区：设置页的值优先，空则跟随 TMDB_REGION。"""
    return current_discover_prefs().region or get_settings().tmdb_region


def effective_image_prefs(setting: MetadataScrapeSetting | None = None) -> ImagePrefs:
    """选图偏好 → 领域层 ``ImagePrefs``。

    选出来的图存在 media_item / 条目资产目录里、跨库共享一份，所以按库配
    的口味必须先经**归属库**收敛成条目的唯一答案（``scrape_setting_for_item``）
    ——不传 setting 就是全局口径。
    """
    setting = setting or current_scrape_setting()
    return ImagePrefs(
        poster_mode=setting.poster_mode,
        poster_langs=tuple(setting.poster_language_priority),
        backdrop_langs=tuple(setting.backdrop_language_priority),
        poster_min_width=setting.poster_min_width,
        backdrop_min_width=setting.backdrop_min_width,
    )


# ---------------------------------------------------------------------------
# 库级覆盖（docs/design/scrape-customization.md §1 / P3；§14 扩容到全部字段）
# ---------------------------------------------------------------------------

# 所有字段都可以按库覆盖，但**解析路径**分两类。判据不变，还是那一条：
# 这个设置的产物是不是按库各存一份。
#
# - ``DIR_SCOPED``：产物落在各库自己的目录树里（路径、写进目录的图片/NFO）
#   → 按**文件所在库**解析。同一条目的文件在两个库，两边各按各的模板命名、
#   各按各的开关写 NFO，互不冲突，所以逐目录取所属库即可（现状不变）；
# - ``ITEM_SCOPED``：产物挂**全局条目**（media_metadata 一行、
#   media_item.poster_path、按条目 id 存一份的图片资产）→ 按条目的
#   **归属库**解析（``resolve_scrape_library``）。这类设置没法像命名那样
#   两边各来一套：同一条目分散在两个库时，两库不同口味会轮流覆盖同一行、
#   同一个文件。P3 由此判定它们"不可库级"；P4 换了个问法——不问"哪个库
#   说了算"，而是先给条目定一个归属库，答案就唯一了（设计文档 §14）。
#
#   另有一条 P3 没写、但更硬的理由：后台刷新 ``refresh_media_metadata()``
#   是纯按条目全表扫的，**根本没有库上下文**。不把归属钉在条目上，就算
#   开成库级也会被下一轮刷新用全局值洗回去。
ITEM_SCOPED_OVERRIDABLE = frozenset(
    {
        # 语言与分级（产物是同一份 media_metadata）
        "language_priority",
        "cert_country_priority",
        # 选图偏好与图片档位（产物是 poster_path/backdrop_path 与条目资产目录）
        "poster_mode",
        "poster_language_priority",
        "backdrop_language_priority",
        "poster_min_width",
        "backdrop_min_width",
        "poster_size",
        "backdrop_size",
        "still_size",
    }
)

DIR_SCOPED_OVERRIDABLE = frozenset(
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

LIBRARY_OVERRIDABLE = ITEM_SCOPED_OVERRIDABLE | DIR_SCOPED_OVERRIDABLE


def sanitize_overrides(raw: object, *, fields: frozenset[str] = LIBRARY_OVERRIDABLE) -> dict:
    """把任意来源的覆盖对象收敛为「只含可覆盖字段」的干净字典。

    保存入口与读取端都过这一道：读取端也过是因为库行里可能残留旧版本
    写入的字段（字段被移出可覆盖集合后），静默忽略比报错合适。
    ``fields`` 让两条解析路径各取自己那一半——目录态的读取点不该看见
    条目态的覆盖，反之亦然。
    """
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in fields}


def merge_for_library(
    library: object | None, *, fields: frozenset[str] = LIBRARY_OVERRIDABLE
) -> MetadataScrapeSetting:
    """全局设置 + 该库的显式覆盖 → 合并后的有效设置。

    ``library`` 为 None 或没有覆盖时直接返回全局快照（零拷贝，热路径常态）。
    非法覆盖值不会让整库刮削崩掉——校验失败就退回全局，只记一条告警。
    """
    global_setting = current_scrape_setting()
    overrides = sanitize_overrides(getattr(library, "scrape_overrides", None), fields=fields)
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

    setting = merge_for_library(library, fields=DIR_SCOPED_OVERRIDABLE)
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
    setting = merge_for_library(library, fields=DIR_SCOPED_OVERRIDABLE)
    return (setting.mirror_images, setting.mirror_nfo, setting.mirror_episode_thumbs)


def profile_fetch_kwargs(setting: MetadataScrapeSetting | None = None) -> dict:
    """``fetch_media_profile`` 的偏好参数包（建档与刷新共用，口径一致）。"""
    return {
        "languages": effective_languages(setting),
        "image_prefs": effective_image_prefs(setting),
        "cert_countries": effective_cert_countries(setting),
    }


# ---------------------------------------------------------------------------
# 刮削归属库（docs/design/scrape-customization.md §14 / P4）
# ---------------------------------------------------------------------------


async def resolve_scrape_library(session, item):
    """条目的刮削归属库；``None`` = 无归属（跟全局设置）。

    ``media_item.scrape_library_id`` 为空时按下面的顺序推断，并把结果**回填
    固化**到条目上——固化之后第 1 步就直接返回，推断逻辑不会再改写它
    （用户在详情页手动指定过的同样受这条保护）：

    1. 列非空且库仍在、类型相符 → 直接用；
    2. 在位文件所属的同类型库：文件数最多者，并列取库 id 最小——只在首次
       推断时算一次然后固化，所以后续增删文件不会让口味漂移；
    3. 订阅的目标库；订阅没指定库时取该类型的默认库（与订阅的落盘目标
       一致：条目最终会进那个库，用它的设置是对的）；
    4. 都没有（纯发现页浏览过、既无文件也无订阅的条目）→ None，跟全局。
       **不回落默认库**：那会让默认库的口味悄悄套到所有无归属条目上。

    调用方负责提交事务（本函数只改 ORM 对象，不 commit）。
    """
    from movieclaw_db.models import Library

    if item.scrape_library_id is not None:
        library = await session.get(Library, item.scrape_library_id)
        if library is not None and library.kind == item.kind:
            return library
        # 库删了（FK 已置 NULL，这里兜的是类型被改这类异常）或类型不符：
        # 清空重新推断，绝不拿一个类型不对的库的设置去刮
        item.scrape_library_id = None

    library = await _infer_scrape_library(session, item)
    if library is not None:
        item.scrape_library_id = library.id
        session.add(item)
    return library


async def _infer_scrape_library(session, item):
    """推断归属库（``resolve_scrape_library`` 的 2~4 步）。"""
    from sqlalchemy import func, select

    from movieclaw_db.models import Library, LibraryFile, Subscription

    # 2) 在位文件所属的同类型库，文件数最多者优先
    rows = (
        await session.execute(
            select(Library, func.count(LibraryFile.id).label("n"))
            .join(LibraryFile, LibraryFile.library_id == Library.id)
            .where(
                LibraryFile.media_item_id == item.id,
                LibraryFile.in_place(),
                Library.kind == item.kind,
            )
            .group_by(Library.id)
            .order_by(func.count(LibraryFile.id).desc(), Library.id.asc())
            .limit(1)
        )
    ).first()
    if rows is not None:
        return rows[0]

    # 3) 订阅的目标库；订阅未指定则该类型的默认库
    subscription = (
        await session.execute(
            select(Subscription).where(Subscription.media_item_id == item.id).limit(1)
        )
    ).scalar_one_or_none()
    if subscription is None:
        return None
    if subscription.library_id is not None:
        library = await session.get(Library, subscription.library_id)
        if library is not None and library.kind == item.kind:
            return library
    return (
        (
            await session.execute(
                select(Library)
                .where(Library.kind == item.kind, Library.is_default.is_(True))
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def assign_scrape_library(item, library) -> bool:
    """把归属库补到条目上（**只补空不改判**），返回是否真的写了。

    给那些"目标库要等条目建好才路由得出来"的链路用——入库管线与手动下载的
    智能入库都是识别完再按作品特征选库（docs/design/library-routing.md），
    建档那一刻还不知道会进哪个库。条目已有归属时不动它：同一条目进第二个库，
    不该悄悄换掉它既有的刮削口味。

    调用方负责提交事务。
    """
    if library is None or library.id is None or item.scrape_library_id is not None:
        return False
    if library.kind != item.kind:  # 类型不符的库，其设置不该套到本条目上
        return False
    item.scrape_library_id = library.id
    return True


async def scrape_setting_for_item(session, item) -> MetadataScrapeSetting:
    """该条目生效的刮削设置：全局设置 + 归属库的**条目态**覆盖。

    刮削管线（档案拉取、选图、图片档位）统一走这一个入口，后台刷新也一样
    ——归属钉在条目上，刷新不需要库上下文就能保持口味。
    """
    library = await resolve_scrape_library(session, item)
    return merge_for_library(library, fields=ITEM_SCOPED_OVERRIDABLE)
