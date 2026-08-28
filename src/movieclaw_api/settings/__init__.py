"""应用配置内核。

本包是"运行时业务配置"的统一管理层，为未来持续膨胀的集成配置（大模型、
BT 下载器、媒体服务器……）提供基础支撑。分层与职责：

- ``base``    —— 配置域模型基类 ``SettingSchema`` 与注册表（声明"有哪些可配置域"）。
- ``store``   —— ``SettingStore`` 内核：串起校验、加密、缓存、持久化。
- ``schemas`` —— 内置配置域（当前是系统引导状态），也是新增配置域的范例。

⚠️ 与 ``movieclaw_db.models`` 同理：任何用 ``@register_setting`` 声明的配置域，
都必须确保其所在模块被导入，注册才会生效。内置域在此集中导入；未来新增的业务
配置模块（如 ``schemas`` 拆分出的 llm/downloader 等）也应在此登记，或由各自模块
在应用启动时导入。

三层配置边界备忘（避免混淆）：
1. 引导层（env / 主密钥）：见 ``core.config`` 与 ``movieclaw_db.crypto``，不进 DB。
2. 内置定义（随镜像发布的只读 yaml）：见 ``movieclaw_tracker`` 站点配置。
3. 运行时业务配置：即本包，落 DB（``app_setting`` 表）。
"""

from __future__ import annotations

from movieclaw_api.settings.app_server import AppServerSetting
from movieclaw_api.settings.base import (
    SettingDescriptor,
    SettingSchema,
    get_descriptor,
    get_descriptor_by_model,
    list_bootstrap_required,
    list_descriptors,
    register_setting,
)
from movieclaw_api.settings.channel_push import (
    ChannelPushSetting,
)
from movieclaw_api.settings.metadata import (
    DiscoverPreferencesSetting,
    MetadataScrapeSetting,
)
from movieclaw_api.settings.network import (
    BUILTIN_EGRESS_SERVICES,
    NetworkEgressSetting,
)
from movieclaw_api.settings.playback import PlaybackPolicySetting
from movieclaw_api.settings.remote_transcode import (
    DEFAULT_REMOTE_TRANSCODE_MAX_ARTIFACT_BYTES,
    MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES,
    RemoteTranscodeSetting,
)
from movieclaw_api.settings.schemas import (
    AdminAccountSetting,
    ApiTokenRecord,
    ApiTokensSetting,
    ExtensionSyncSetting,
    SessionSecretSetting,
    SystemBootstrap,
    generate_sync_token,
    get_sync_setting,
    is_initialized,
    mark_initialized,
    revoke_sync_token,
)
from movieclaw_api.settings.store import (
    SettingStore,
    get_setting_store,
    init_setting_store,
    reset_setting_store,
)
from movieclaw_api.settings.webhook import (
    WebhookEndpoint,
    WebhookSetting,
    generate_webhook_secret,
    mask_webhook_secret,
)

__all__ = [
    # 基类与注册表
    "SettingSchema",
    "SettingDescriptor",
    "register_setting",
    "get_descriptor",
    "get_descriptor_by_model",
    "list_descriptors",
    "list_bootstrap_required",
    # 内核
    "SettingStore",
    "init_setting_store",
    "get_setting_store",
    "reset_setting_store",
    # 内置配置域与引导助手
    "SystemBootstrap",
    "is_initialized",
    "mark_initialized",
    # 浏览器插件同步令牌
    "ExtensionSyncSetting",
    "get_sync_setting",
    "generate_sync_token",
    "revoke_sync_token",
    # CLI API 令牌（PAT）
    "ApiTokenRecord",
    "ApiTokensSetting",
    # 超级管理员账号与登录会话
    "AdminAccountSetting",
    "SessionSecretSetting",
    # 刮削与发现页偏好
    "MetadataScrapeSetting",
    "DiscoverPreferencesSetting",
    # 网络与代理
    "NetworkEgressSetting",
    "PlaybackPolicySetting",
    "RemoteTranscodeSetting",
    "DEFAULT_REMOTE_TRANSCODE_MAX_ARTIFACT_BYTES",
    "MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES",
    "ChannelPushSetting",
    "BUILTIN_EGRESS_SERVICES",
    # 应用服务（外部访问地址）
    "AppServerSetting",
    # 事件 Webhook
    "WebhookSetting",
    "WebhookEndpoint",
    "generate_webhook_secret",
    "mask_webhook_secret",
]
