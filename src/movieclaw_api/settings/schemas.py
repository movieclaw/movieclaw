from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from movieclaw_api.settings.base import SettingSchema, register_setting
from movieclaw_api.settings.store import get_setting_store
from movieclaw_tracker.models import TorrentCategory

# ---------------------------------------------------------------------------
# 系统引导状态
# ---------------------------------------------------------------------------
# 说明：这是配置内核落地的第一个具体"配置域"，同时承担两个作用：
#   1. 记录"首次引导是否完成"，让应用在空库首启时把请求导向引导页。
#   2. 作为"如何新增一个配置域"的活范例——未来接入大模型 / 下载器 / 媒体服务器
#      时，照此声明一个 SettingSchema 子类并 @register_setting 即可，零数据库迁移。


@register_setting(
    namespace="system.bootstrap",
    title="系统初始化状态",
    # 注意：它不是"向导要用户填的项"，而是向导完成后由系统写入的标记，
    # 因此 required_for_bootstrap=False（默认值）。
)
class SystemBootstrap(SettingSchema):
    """首次引导状态。

    ``initialized`` 为整个引导流程的总开关：为 False 时，应用应把业务请求
    重定向到引导页；用户在向导里完成必填配置后，由系统置为 True 放行。
    """

    initialized: bool = Field(default=False, description="是否已完成首次引导；False 时应进入引导页")
    completed_steps: list[str] = Field(
        default_factory=list, description="已完成的引导步骤标识，供向导断点续填"
    )
    version: int = Field(default=1, description="引导流程版本号，便于将来引导流程升级")


# ---------------------------------------------------------------------------
# 引导状态便捷函数（薄封装，供 API / 中间件直接调用）
# ---------------------------------------------------------------------------


async def is_initialized() -> bool:
    """系统是否已完成首次引导。空库时返回 False（触发引导页）。"""
    state = await get_setting_store().get(SystemBootstrap)
    return state.initialized


async def mark_initialized() -> None:
    """标记首次引导已完成。向导最后一步调用。"""
    store = get_setting_store()
    state = await store.get(SystemBootstrap)
    state.initialized = True
    await store.set(state)


# ---------------------------------------------------------------------------
# 超级管理员账号 + 登录会话密钥
# ---------------------------------------------------------------------------
# 全站永远只有一个超级管理员（用户明确的产品决策：即使将来出现新的用户体系，
# 也与这个超管账号无关），因此账号存配置域而非建 user 表——零迁移，与配置内核
# 的既有范式一致。
#
# 两个安全要点：
# - **密码存 argon2 单向哈希**，不注册为 secret_fields。secret_fields 是 SecretBox
#   可逆加密（为"可回显"设计，如插件令牌），而登录密码永远不需要回显，单向哈希
#   才是正确存法（哈希本身可安全落库）。
# - **会话签名密钥走 secret_fields 加密落库**：它需要原文参与签名运算，属于
#   "需回读明文"的敏感值，与插件令牌同一套保护。修改密码时轮换此密钥，
#   即可让所有已签发的登录会话立刻失效（全端下线）。


@register_setting(namespace="auth.admin", title="超级管理员账号")
class AdminAccountSetting(SettingSchema):
    """超级管理员账号。

    ``password_hash`` 为空字符串表示"系统尚未初始化"——这是首次引导建号
    接口的一次性锁依据：一旦非空，建号接口永久拒绝（409），且判断在服务端，
    前端无法绕过。
    """

    username: str = Field(default="", description="管理员用户名；空表示尚未初始化")
    password_hash: str = Field(default="", description="argon2 密码哈希；空表示尚未初始化")
    nickname: str = Field(default="", description="展示昵称；建号时默认取用户名，可在设置里修改")
    created_at: str = Field(default="", description="账号创建时间（ISO8601 字符串）")


@register_setting(
    namespace="auth.session",
    title="登录会话签名密钥",
    secret_fields=["secret"],
)
class SessionSecretSetting(SettingSchema):
    """登录会话令牌的 HMAC 签名密钥。

    首次需要签发会话时自动生成并加密落库（用户无感）；重新生成即"轮换"，
    所有旧会话立即失效。
    """

    secret: str = Field(default="", description="HMAC 签名密钥；空表示尚未生成")


# ---------------------------------------------------------------------------
# 浏览器插件同步令牌
# ---------------------------------------------------------------------------
# 配套 Chrome 插件（apps/extension）使用：插件把浏览器里的站点 Cookie 推送到本
# 服务时，需要携带一个"同步令牌"作为身份凭证。设计取舍见下：
#
# - **一次配置、长期有效**：令牌不设自动过期。用户在插件里填一次即可，之后连接
#   即视为可信。只有用户主动"重新生成"时，旧令牌立刻失效（即"强制过期"）。
# - **加密落库**：token 声明为敏感字段，由 SettingStore 落库前加密、读取后解密，
#   与站点 api_key/password 同一套保护。
# - **可回显**：Web 后台需要把令牌显示给用户复制进插件，故采用"可解密回显"而非
#   "只存哈希"。这是自托管单用户场景下的合理取舍，与全站现有凭据存法保持一致。


@register_setting(
    namespace="extension.sync",
    title="浏览器插件同步",
    secret_fields=["token"],
)
class ExtensionSyncSetting(SettingSchema):
    """浏览器插件同步配置。

    ``token`` 为空表示"尚未启用同步"——此时所有插件侧接口一律拒绝（401），
    直到用户在后台生成令牌。
    """

    token: str = Field(default="", description="插件同步令牌；空字符串表示未启用同步")
    created_at: str = Field(default="", description="令牌生成时间（ISO8601 字符串），供后台展示")


async def get_sync_setting() -> ExtensionSyncSetting:
    """读取当前插件同步配置（从未配置则返回禁用态默认值）。"""
    return await get_setting_store().get(ExtensionSyncSetting)


async def generate_sync_token() -> ExtensionSyncSetting:
    """生成（或重新生成）同步令牌并保存，返回新配置。

    重新生成会整体覆盖旧记录，因此旧令牌**立即失效**——这正是"强制过期"的实现。
    """
    setting = ExtensionSyncSetting(
        token=secrets.token_urlsafe(32),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    await get_setting_store().set(setting)
    return setting


async def revoke_sync_token() -> None:
    """撤销同步令牌（写回禁用态默认值），关闭插件同步。"""
    await get_setting_store().set(ExtensionSyncSetting())


# ---------------------------------------------------------------------------
# CLI API 令牌（PAT）
# ---------------------------------------------------------------------------
# mclaw 等远程客户端的长期凭证（docs/design/cli.md §8.1）。与插件同步令牌的
# 关键差异：**只存哈希、不可回显**——PAT 面向脚本/定时任务，明文只在创建
# 时返回一次，用户抄走后服务端无法（也无需）再展示；泄露疑虑时按 id 单独
# 吊销即可，不影响其他令牌。


class ApiTokenRecord(BaseModel):
    """单枚 API 令牌的落库记录（仅哈希与元信息，无明文）。

    记录里存的是「谁批准的、给哪种客户端」，**不是「能干什么」**——权限在每次
    验签时按批准者装配（docs/design/device-auth.md §4）。这样管理员事后调整
    权限能立刻对令牌生效，不会出现「令牌里冻结的旧权限」。

    新增字段全部带默认值：老记录读出来就是「超管批准的手工令牌、长期有效」，
    与升级前语义一致，零迁移。
    """

    id: str = Field(description="令牌 id（吊销时使用）")
    name: str = Field(description="用户起的名字，如 'nas-cron'")
    token_hash: str = Field(description="令牌明文的 sha256 十六进制哈希")
    created_at: str = Field(description="创建时间（ISO8601 字符串）")
    client_type: str = Field(
        default="manual",
        description="客户端形态：worker（只能转码）/ cli / manual（网页手工创建）",
    )
    owner_kind: str = Field(
        default="admin",
        description="批准者身份。当前只有超管能批准设备，恒为 admin；"
        "保留字段是为了将来开放成员批准时能按它分流",
    )
    expires_at: str | None = Field(
        default=None,
        description="过期时间（ISO8601）。None = 长期有效，当前签发的令牌都不设过期",
    )
    last_used_at: str | None = Field(
        default=None,
        description="最近一次使用时间（按分钟粒度落盘）。设备列表靠它回答"
        "「这台机器还活着吗」，也是吊销决策的依据",
    )


@register_setting(
    namespace="auth.api_tokens",
    title="CLI API 令牌",
)
class ApiTokensSetting(SettingSchema):
    """全部有效 API 令牌的清单；吊销即从清单移除。"""

    tokens: list[ApiTokenRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 搜索偏好（标签栏：内置分类 + 自定义分类，统一混排）
# ---------------------------------------------------------------------------
# 搜索面板的分类栏是一个用户自定义的「标签列表」，两种标签统一排序、统一显隐：
#   - 内置分类（type=category）：单个一级分类 × 全部站点，不可删除只可隐藏；
#   - 自定义分类（type=preset）：任意分类组合 × 任意站点组合，可增删改。
#     categories 为空表示「不限分类」，site_ids 为空表示「全部可用站点」
#     （新配置的站点自动纳入）。
# 存整份**有序列表**，列表顺序即展示顺序；「全部」标签固定在首位、不在此列表中。
# 设置存服务端（配置域，零迁移），跨设备/浏览器一次保存处处生效。
# 未来的搜索设定（默认排序、结果条数……）继续在 SearchPreferencesSetting 上加字段。


class SearchCategoryTab(BaseModel):
    """内置分类标签；它在列表中的位置即搜索面板里标签的顺序。"""

    type: Literal["category"] = "category"
    id: str = Field(description="分类标识，对应 TorrentCategory 的取值（movie/tv/…）")
    visible: bool = Field(default=True, description="是否在搜索分类栏中展示")


class SearchPresetTab(BaseModel):
    """自定义分类标签：一组「分类组合 × 站点组合」的命名预设。"""

    type: Literal["preset"] = "preset"
    id: str = Field(description="预设标识（创建时生成的随机短 id），历史与前端引用它")
    name: str = Field(description="用户设定的展示名称")
    visible: bool = Field(default=True, description="是否在搜索分类栏中展示")
    categories: list[str] = Field(default_factory=list, description="勾选的一级分类；空 = 不限分类")
    site_ids: list[str] = Field(default_factory=list, description="勾选的站点；空 = 全部可用站点")
    poster_mode: bool = Field(
        default=False,
        description="图览模式：用该分类搜索时，结果页默认以图墙展示（结果页可临时切换）",
    )
    skip_history: bool = Field(
        default=False,
        description="无痕搜索：用该分类搜索时不写入搜索历史（隐私敏感场景的开关）",
    )


SearchTab = Annotated[SearchCategoryTab | SearchPresetTab, Field(discriminator="type")]

# 自定义分类数量上限：防止标签栏无限膨胀，也约束单条配置记录的体积
MAX_SEARCH_PRESETS = 20

# 默认可见的四个常用分类（顺序即默认展示顺序），与历史版本前端硬编码的
# 「全部/电影/剧集/纪录片/动漫」保持一致——升级后老用户的搜索面板不变。
_DEFAULT_VISIBLE_CATEGORIES: tuple[TorrentCategory, ...] = (
    TorrentCategory.MOVIE,
    TorrentCategory.TV,
    TorrentCategory.DOCUMENTARY,
    TorrentCategory.ANIME,
)


def default_search_tabs() -> list[SearchTab]:
    """默认标签列表：常用四类可见，其余（音乐/游戏/成人/其他）隐藏、排在末尾。"""
    visible = set(_DEFAULT_VISIBLE_CATEGORIES)
    ordered = list(_DEFAULT_VISIBLE_CATEGORIES) + [c for c in TorrentCategory if c not in visible]
    return [SearchCategoryTab(id=c.value, visible=c in visible) for c in ordered]


@register_setting(namespace="search.preferences", title="搜索偏好")
class SearchPreferencesSetting(SettingSchema):
    """搜索相关的用户偏好。当前只有标签栏配置，后续搜索设定在此扩展。"""

    tabs: list[SearchTab] = Field(
        default_factory=default_search_tabs,
        description="标签栏配置（有序）：列表顺序即搜索面板中的标签顺序",
    )


def normalize_search_tabs(tabs: list[SearchTab]) -> list[SearchTab]:
    """把任意来源的标签列表校正为「内置全量、无重复、无未知项」的规范形态。

    - 内置分类：未知值丢弃、重复保留首个、缺失的按默认可见性补到末尾——
      保证内置分类永远是完整集合，枚举演进无需数据修复；
    - 自定义分类：按预设 id 去重（保留首个），预设内的分类值去掉未知项与重复；
      site_ids 只做去重，不校验存在性（站点目录演进/站点被删时保留原值，
      搜索时自动跳过不可用站点即可，存在性校验在 API 保存入口做）。

    读与写都过这一道，混排顺序原样保留。
    """
    valid_categories = {c.value for c in TorrentCategory}
    default_visible = {c.value for c in _DEFAULT_VISIBLE_CATEGORIES}
    result: list[SearchTab] = []
    seen_categories: set[str] = set()
    seen_presets: set[str] = set()
    for tab in tabs:
        if isinstance(tab, SearchCategoryTab):
            if tab.id in valid_categories and tab.id not in seen_categories:
                seen_categories.add(tab.id)
                result.append(tab)
        else:
            if not tab.id or tab.id in seen_presets:
                continue
            seen_presets.add(tab.id)
            result.append(
                tab.model_copy(
                    update={
                        "categories": _dedup(c for c in tab.categories if c in valid_categories),
                        "site_ids": _dedup(tab.site_ids),
                    }
                )
            )
    for cat in TorrentCategory:
        if cat.value not in seen_categories:
            result.append(SearchCategoryTab(id=cat.value, visible=cat.value in default_visible))
    return result


def _dedup(values) -> list[str]:
    """列表去重（保序）。"""
    return list(dict.fromkeys(values))


# ---------------------------------------------------------------------------
# 界面偏好（按页面分组的样式设定）
# ---------------------------------------------------------------------------
# 用户自定义的页面元素/样式设定统一放这一个配置域，**按页面分组嵌套**：
#   UiPreferencesSetting
#   └─ sidebar: 侧边栏玻璃面板；（未来）discover / detail …… 加一个嵌套模型即可，零迁移
#
# 历史：搜索结果页的「图览模式」曾是这里的全局设定（search.poster_mode），
# 已改为跟随自定义分类各自的 poster_mode（见 SearchPresetTab）+ 结果页临时开关；
# 基类 extra="ignore"，旧存量数据里的 search 分组读取时自动忽略，无需迁移。
#
# 为什么单独一个域、而不是塞进 search.preferences：
#   - search.preferences 是「搜索行为」（标签栏、范围），这里是「界面样式」，
#     两者的消费者与变更节奏不同（样式设定会跨页面膨胀）；
#   - 单域 = 前端一次 GET 拿到全站样式设定（应用启动时拉一次、Context 共享），
#     未来加设定不增加请求数。
# 读写就是配置域的整体读写，没有业务校验，因此 API 层直接复用本模型作为
# 请求/响应体（纯用户偏好透传，无敏感字段，无需再抄一份 schema）。


class SidebarUiPrefs(BaseModel):
    """侧边栏（液态玻璃面板）的样式偏好。

    两个值直接对应前端 WebGL 着色器的参数（见 apps/web/lib/glass.ts）：
    侧栏玻璃的基底是 LiquidGlassCard 同款材质（见 apps/web/lib/glass.ts），
    三个值是在其上微调的滑杆，默认值即 Card 出厂观感：
    - ``transparency``：玻璃透明程度。0 = Card 标准玻璃，1 = 玻璃完全
      隐去；对应 shader 的 u_opacity（材质整体淡出）。
    - ``brightness``：玻璃明暗。-1 最暗 ~ 1 最亮，0 = 不加暗不提亮；
      对应 shader 的 tint 参数。
    - ``depth``：玻璃厚度（边缘曲率带宽度，px）。越大越像厚玻璃、边缘折射带
      越宽；对应 shader 的 u_zRadius，过小会使高度场退化，故下限取 10。
    默认值是实际调校后确定的出厂观感（半透 + 略压暗 + 偏薄的边缘折射带），
    而非 Card 材质的原始参数；必须与前端 DEFAULT_UI_PREFS 保持一致。
    """

    transparency: float = Field(
        default=0.49, ge=0.0, le=1.0, description="玻璃透明程度：0 标准玻璃，1 完全隐去"
    )
    brightness: float = Field(
        default=-0.36, ge=-1.0, le=1.0, description="玻璃明暗：-1 最暗，1 最亮"
    )
    depth: float = Field(
        default=28.0, ge=10.0, le=90.0, description="玻璃厚度（边缘曲率带宽度，px）"
    )


class ScrimUiPrefs(BaseModel):
    """全站背景蒙版（.page-scrim）的样式偏好。

    蒙版是铺在背景大图之上、页面内容之下的一层深色模糊层，压住背景、
    突出内容（见 apps/web/app/globals.css 的 .page-scrim）。全站只有这
    一档蒙版（除「新任务」首页大图直出外，所有页面统一），两个可调项
    分别驱动前端 CSS 变量 ``--scrim-blur`` / ``--scrim-dark``：
    - ``blur``：高斯模糊半径（px）。0 = 不模糊、背景大图清晰透出；越大背景
      越朦胧。
    - ``dark``：压暗程度（蒙版底色的不透明度）。0 = 完全不压暗，1 = 全黑。
    默认值是实际调校后确定的出厂观感：中等模糊 + 近七成压暗，背景大图化为
    朦胧色块托住内容、又不至于抢走注意力；必须与前端 DEFAULT_UI_PREFS
    以及 globals.css 里 .page-scrim 的变量兜底值保持一致。
    """

    blur: float = Field(
        default=13.0,
        ge=0.0,
        le=40.0,
        description="蒙版高斯模糊半径（px）：0 不模糊，越大背景越朦胧",
    )
    dark: float = Field(
        default=0.69,
        ge=0.0,
        le=1.0,
        description="蒙版压暗程度：0 完全不压暗，1 全黑",
    )


class NavUiPrefs(BaseModel):
    """侧边栏主导航的个人排序。

    只存**顺序**（导航项 id 的列表），不存导航项本身——导航有哪些项、叫什么、
    什么权限可见，全部由前端与权限决定，这里存下来的仅仅是"这个人希望它们按
    什么次序排"。

    因此本字段是提示而非契约，前端按"排过的按此顺序在前，没排过的按内置默认
    顺序追加在后"合并（见 apps/web/lib/sidebar-nav.ts）：
    - 版本升级新增的导航入口，在存过排序的老用户那里也一定会出现（追加在后），
      不会因为不在这个列表里而永远消失——这是这类"存死一份顺序"功能最常见的事故；
    - 已经不存在的 id（导航项被删）读取时直接忽略，无需迁移。

    上限 32 只是防脏数据无限增长的安全阀，不是产品限制（主导航实际只有个位数项）。
    """

    order: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="侧栏主导航的展示顺序（导航项 id）；空列表 = 用内置默认顺序",
    )


@register_setting(namespace="ui.preferences", title="界面偏好")
class UiPreferencesSetting(SettingSchema):
    """全站界面样式偏好，按页面分组。新页面的设定加嵌套模型字段即可。"""

    sidebar: SidebarUiPrefs = Field(default_factory=SidebarUiPrefs, description="侧边栏玻璃面板")
    scrim: ScrimUiPrefs = Field(default_factory=ScrimUiPrefs, description="全站背景蒙版")
    nav: NavUiPrefs = Field(default_factory=NavUiPrefs, description="侧边栏主导航排序")


async def get_ui_preferences() -> UiPreferencesSetting:
    """读取界面偏好（从未配置则返回各页面的默认值）。"""
    return await get_setting_store().get(UiPreferencesSetting)


async def save_ui_preferences(prefs: UiPreferencesSetting) -> UiPreferencesSetting:
    """整体覆盖式保存界面偏好，返回保存后的值。"""
    await get_setting_store().set(prefs)
    return prefs


# ---------------------------------------------------------------------------
# 更新检查结果快照
# ---------------------------------------------------------------------------
# 为什么要落库：更新提醒的载体是「侧栏常驻的环境徽标」（见 apps/web 的
# app-update-badge），徽标要在**任意页面、任意时刻**都能立刻知道「有没有新
# 版本」，不能每次渲染都去打 GitHub。定时任务（每小时）与用户手动点「检查
# 更新」都把结果写进这里，前端只读这份快照——既零网络开销，也让重启容器后
# 徽标立刻恢复（NAS 用户重启很频繁，等下一轮检查才亮灯是明显的倒退）。
#
# 没有新版本时写回全空实例（而不是删记录）：语义是「已检查过，结论是最新」，
# 与「从未检查过」区分开，前端才能安心地按快照渲染。


@register_setting(namespace="app.update_state", title="更新检查结果快照")
class AppUpdateStateSetting(SettingSchema):
    """最近一次更新检查的结果快照（应用与 NER 模型各一组，互相独立）。"""

    checked_at: str = Field(
        default="", description="最近一次检查完成时间（ISO8601）；空 = 从未检查过"
    )
    app_latest_version: str = Field(default="", description="可更新的应用版本号；空 = 无可用更新")
    app_compatible: bool = Field(
        default=False, description="False = 含依赖变化，需重拉 Docker 镜像"
    )
    app_changelog: str = Field(default="", description="该版本的 Release 说明（Markdown 原文）")
    app_published_at: str = Field(default="", description="该版本的发布时间（ISO8601）")
    model_latest_tag: str = Field(default="", description="可更新的 NER 模型 tag；空 = 无可用更新")
    # 镜像基线版本：跑基线代码时（MOVIECLAW_CODE_SOURCE=baseline）由启动流程
    # 记下 __version__。overlay 运行期读不到基线代码的版本号，回退列表要向
    # 用户明示「回落镜像内置版本 = 回到 v 几」，只能靠这份历史记录
    baseline_version: str = Field(default="", description="最近一次以镜像基线运行时的版本号")


async def get_app_update_state() -> AppUpdateStateSetting:
    """读取更新检查结果快照（从未检查过则返回全空实例）。"""
    return await get_setting_store().get(AppUpdateStateSetting)


async def save_app_update_state(state: AppUpdateStateSetting) -> None:
    """整体覆盖式保存更新检查结果快照。"""
    await get_setting_store().set(state)


@register_setting(namespace="app.update_prefs", title="应用更新偏好")
class AppUpdatePrefsSetting(SettingSchema):
    """应用内更新的用户偏好。

    ``keep_versions``：本地保留的历史版本目录数（含当前运行版本）。保留得越多
    可回退的范围越大，占用磁盘越多（每个版本一整份前后端产物）。范围 2~20：
    下限 2 保证「当前 + 上一版」的 A/B 兜底永远成立。
    """

    keep_versions: int = Field(
        default=5, ge=2, le=20, description="本地保留的版本目录数（含当前版本），默认 5"
    )


async def get_app_update_prefs() -> AppUpdatePrefsSetting:
    """读取应用更新偏好（从未配置则返回默认值）。"""
    return await get_setting_store().get(AppUpdatePrefsSetting)


async def save_app_update_prefs(prefs: AppUpdatePrefsSetting) -> None:
    """整体覆盖式保存应用更新偏好。"""
    await get_setting_store().set(prefs)


async def get_search_tabs() -> list[SearchTab]:
    """读取当前搜索标签配置（从未配置则返回默认值），已规范化。"""
    setting = await get_setting_store().get(SearchPreferencesSetting)
    return normalize_search_tabs(setting.tabs)


async def save_search_tabs(tabs: list[SearchTab]) -> list[SearchTab]:
    """保存搜索标签配置（整体覆盖），返回规范化后的最新列表。"""
    normalized = normalize_search_tabs(tabs)
    await get_setting_store().set(SearchPreferencesSetting(tabs=normalized))
    return normalized


# ---------------------------------------------------------------------------
# Jellyfin 兼容播放接口（docs/design/jellyfin-compat.md）
# ---------------------------------------------------------------------------


@register_setting(namespace="jellyfin.compat", title="Jellyfin 兼容播放接口")
class JellyfinCompatSetting(SettingSchema):
    """Jellyfin 兼容层配置。默认开启（2026-08-03 用户决策，设计文档 8.4）。

    ``server_id`` 首启自动生成并持久化——它是播放器识别"这台服务器"的
    身份锚，换了会导致所有已配对客户端要求重新登录，永不轻易重置。
    """

    enabled: bool = Field(default=True, description="是否对外提供 Jellyfin 兼容接口")
    server_id: str = Field(default="", description="服务器 ID（32 位 hex，首启自动生成）")
    published_server_url: str = Field(
        default="",
        description=(
            "对外发布地址（如 http://192.168.1.10:3000）；"
            "Docker 桥接部署下自动发现的唯一可靠答案，空=自动探测"
        ),
    )
    server_name: str = Field(default="MovieClaw", description="对播放器展示的服务器名")


async def get_jellyfin_compat() -> JellyfinCompatSetting:
    """读取 Jellyfin 兼容层配置；首次读取时生成并固化 server_id。"""
    store = get_setting_store()
    setting = await store.get(JellyfinCompatSetting)
    if not setting.server_id:
        setting.server_id = secrets.token_hex(16)
        await store.set(setting)
    return setting


@register_setting(namespace="subtitle.gen", title="AI 字幕生成")
class SubtitleGenSetting(SettingSchema):
    """AI 字幕生成配置（docs/design/subtitle-ai-translate.md §6）。

    ``auto_generate`` **默认关**：批量入库=批量 LLM 消费（真金白银），
    必须用户显式授权；开启后由 ``daily_limit`` 每日额度熔断兜底。
    ``library_ids`` 空列表 = 对全部库生效（库级粒度，不动库表结构）。
    """

    auto_generate: bool = Field(default=False, description="入库后自动生成缺失的目标语言字幕")
    library_ids: list[int] = Field(
        default_factory=list, description="自动生成生效的库 id 列表；空=全部库"
    )
    target_language: str = Field(default="chs", description="目标语言 token（文件名用）")
    daily_limit: int = Field(default=5, description="每日自动生成任务上限（额度熔断）")


async def get_subtitle_gen_setting() -> SubtitleGenSetting:
    return await get_setting_store().get(SubtitleGenSetting)
