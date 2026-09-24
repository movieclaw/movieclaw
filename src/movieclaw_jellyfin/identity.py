"""服务器/用户身份 DTO（设计文档 3.2/4.2，字段清单逐项对照源码核实）。

UserDto/Policy/Configuration 里列出的键都是"真 Jellyfin 必然输出"的字段
（非可空或带非 null 默认值）；可空且为 null 的字段按协议约定不输出。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from movieclaw_api.services import auth as auth_service
from movieclaw_api.settings.schemas import JellyfinCompatSetting, get_jellyfin_compat
from movieclaw_db.models.member import Member
from movieclaw_jellyfin.ids import (
    collection_view_guid,
    library_guid,
    user_guid,
    user_guid_for,
)
from movieclaw_jellyfin.security import read_authorization

logger = logging.getLogger("movieclaw_jellyfin.identity")

PRODUCT_NAME = "Jellyfin Server"

# ---------------------------------------------------------------------------
# 对外自报的 Jellyfin 版本号（issue #445）
#
# 上游 10.11 之后直接跳到了 12.0，客户端的"最低服务器版本"门槛随之抬高：
# Flow 要求 >= 12.0、官方 Android TV（Kotlin SDK 1.8.12）要求 >= 10.11，
# Kotlin/Swift SDK 主干已是 12.0——一律报 10.10.7 会被它们直接拒连。
# 调研过的客户端都只设下限、不设上限，且我们实现的接口在 10.10.7→12.1 之间
# 协议无破坏性差异，因此默认报 12.1.0。
#
# 但 Infuse / VidHub 这类闭源播放器是否按版本号走不同分支无从查证，它们在
# 10.10.7 下已长期验证可用，所以按来访客户端区分：识别出它们就维持 10.10.7，
# 行为与改动前完全一致；其余一律 12.1.0。无需用户配置。
#
# 版本号必须是三段式：Kotlin/Swift SDK 解析 "12.1" 这种两段式会失败，
# 反而把 Android 系客户端全部拒掉（真 Jellyfin 也是 ToString(3) 输出三段）。
# ---------------------------------------------------------------------------

#: 默认版本：与上游最新发布版一致
REPORTED_VERSION = "12.1.0"
#: 老牌播放器沿用的版本：兼容层最初就是对照 10.10.7 源码实现并实测的
LEGACY_REPORTED_VERSION = "10.10.7"
#: 维持旧版本号的客户端标识（小写子串）。Infuse 官方文档公开了 UA
#: ``Infuse-Direct/<ver>`` 与 ``Client="Infuse-Direct"``（另有 Infuse-Library、
#: Infuse-Download 两种连接类型）；VidHub 按名称匹配。
_LEGACY_CLIENT_MARKERS = ("infuse", "vidhub")


def reported_version(request: Request) -> str:
    """按来访客户端决定自报的 Jellyfin 版本号。

    同时看 User-Agent 与 Authorization / X-Emby-Authorization 里的 ``Client=``：
    匿名探测阶段不同客户端带的头不一样，任一处命中即视为老牌播放器。
    同一客户端每次请求带的标识不变，所以 /System/Info/Public 与 /System/Info
    拿到的版本号始终一致。
    """
    user_agent = request.headers.get("User-Agent", "")
    client = read_authorization(request).client
    fingerprint = f"{user_agent} {client}".lower()
    legacy = any(marker in fingerprint for marker in _LEGACY_CLIENT_MARKERS)
    version = LEGACY_REPORTED_VERSION if legacy else REPORTED_VERSION
    # debug 级别：探测请求很频繁；排查"某播放器连不上"时打开即可看到它的标识
    logger.debug(
        "Jellyfin 兼容层自报版本 %s（User-Agent=%r, Client=%r）", version, user_agent, client
    )
    return version


def _now_iso() -> str:
    from movieclaw_db.models.base import utcnow

    return format_datetime(utcnow())


def format_datetime(value) -> str:
    """ISO8601 UTC 7 位小数 + Z（协议约定的日期形态，统一收敛）。"""
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond:06d}0Z"


async def get_compat_settings() -> JellyfinCompatSetting:
    return await get_jellyfin_compat()


def user_configuration() -> dict[str, Any]:
    return {
        "PlayDefaultAudioTrack": True,
        "SubtitleLanguagePreference": "",
        "DisplayMissingEpisodes": False,
        "GroupedFolders": [],
        "SubtitleMode": "Default",
        # 合集视图（docs/design/library-collections.md 4.3）：服务端在有可见合集时
        # 才把它放进 /UserViews，这里如实声明"这个服务器支持合集视图"——有些
        # 客户端只在配置为真时才渲染那个入口
        "DisplayCollectionsView": True,
        "EnableLocalPassword": False,
        "OrderedViews": [],
        "LatestItemsExcludes": [],
        "MyMediaExcludes": [],
        "HidePlayedInLatest": True,
        "RememberAudioSelections": True,
        "RememberSubtitleSelections": True,
        "EnableNextEpisodeAutoPlay": True,
    }


def user_policy(
    member: Member | None = None,
    visible_library_ids: set[int] | None = None,
    visible_collection_ids: list[int] | None = None,
) -> dict[str, Any]:
    """用户 Policy。超管（member=None）管理位全开；成员按权限投影：
    非管理员、不可删内容。库可见性经 EnabledFolders 下发给客户端（客户端
    据此过滤其自建的合集/快捷入口；服务端查询侧另有强制过滤）——传入
    ``visible_library_ids`` 时超管与成员一视同仁：超管把自己从某个库的浏览
    范围摘掉后，电视端也不该再列出它（docs/design/library-access.md）。

    ``visible_collection_ids`` 是伪装成媒体库的那些合集（钉了首页，
    docs/design/library-collections.md 4.11）。**它们必须一起进 EnabledFolders**：
    这份清单一旦非空，客户端就拿它当白名单过滤自己看到的库，虚拟库的 GUID 不在
    里面就会被客户端自己藏掉——``/UserViews`` 下发了也没用，而且这种"少了一个库"
    的表现没有任何报错，最难查。"""
    if member is not None:
        # 以超管为底，只改差异字段
        policy = user_policy(
            visible_library_ids=visible_library_ids,
            visible_collection_ids=visible_collection_ids,
        )
        policy["IsAdministrator"] = False
        policy["IsDisabled"] = member.status != "active"
        return policy
    policy = _admin_policy()
    if visible_library_ids is not None:
        policy["EnableAllFolders"] = False
        policy["EnabledFolders"] = [library_guid(i) for i in sorted(visible_library_ids)] + [
            collection_view_guid(i) for i in (visible_collection_ids or [])
        ]
    return policy


def _admin_policy() -> dict[str, Any]:
    return {
        "IsAdministrator": True,
        "IsHidden": False,  # /Users/Public 按 hidden 过滤，true 会让登录页空列表
        "IsDisabled": False,
        "BlockedTags": [],
        "AllowedTags": [],
        "EnableUserPreferenceAccess": True,
        "AccessSchedules": [],
        "BlockUnratedItems": [],
        "EnableRemoteControlOfOtherUsers": False,
        "EnableSharedDeviceControl": True,
        "EnableRemoteAccess": True,
        "EnableLiveTvManagement": True,
        "EnableLiveTvAccess": True,
        "EnableMediaPlayback": True,
        "EnableAudioPlaybackTranscoding": True,
        "EnableVideoPlaybackTranscoding": True,
        "EnablePlaybackRemuxing": True,
        # 置 true 会逼客户端对远程源（strm）走转码，击穿"不转码"硬边界
        "ForceRemoteSourceTranscoding": False,
        "EnableContentDeletion": False,
        "EnableContentDeletionFromFolders": [],
        "EnableContentDownloading": True,
        "EnableSyncTranscoding": True,
        "EnableMediaConversion": True,
        "EnabledDevices": [],
        "EnableAllDevices": True,
        "EnabledChannels": [],
        "EnableAllChannels": True,
        "EnabledFolders": [],
        "EnableAllFolders": True,
        "InvalidLoginAttemptCount": 0,
        "LoginAttemptsBeforeLockout": -1,
        "MaxActiveSessions": 0,
        "EnablePublicSharing": True,
        "BlockedMediaFolders": [],
        "BlockedChannels": [],
        "RemoteClientBitrateLimit": 0,
        "AuthenticationProviderId": (
            "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider"
        ),
        "PasswordResetProviderId": (
            "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider"
        ),
        "SyncPlayAccess": "CreateAndJoinGroups",
        "EnableCollectionManagement": False,
        "EnableSubtitleManagement": False,
        "EnableLyricManagement": False,
    }


async def user_dto(
    server_id: str,
    member: Member | None = None,
    visible_library_ids: set[int] | None = None,
    visible_collection_ids: list[int] | None = None,
) -> dict[str, Any]:
    """用户 DTO。member=None → 超管（原单用户形态，GUID 不变）；
    传入成员 → 以其登录名/GUID/Policy 投影（电视登录页的多头像来源）。

    ``visible_collection_ids``：伪装成媒体库的合集，见 ``user_policy``。"""
    if member is not None:
        return {
            "Name": member.username,
            "ServerId": server_id,
            "Id": user_guid_for(member.id),
            "HasPassword": True,
            "HasConfiguredPassword": True,
            "HasConfiguredEasyPassword": False,
            "EnableAutoLogin": False,
            "Configuration": user_configuration(),
            "Policy": user_policy(member, visible_library_ids, visible_collection_ids),
        }
    account = await auth_service.get_admin_account()
    return {
        "Name": account.username,
        "ServerId": server_id,
        "Id": user_guid(),
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "HasConfiguredEasyPassword": False,
        "EnableAutoLogin": False,
        "Configuration": user_configuration(),
        "Policy": user_policy(
            visible_library_ids=visible_library_ids,
            visible_collection_ids=visible_collection_ids,
        ),
    }


def session_info_dto(
    server_id: str,
    session_id: str,
    *,
    client: str,
    device_id: str,
    device_name: str,
    version: str,
    user_name: str,
    member_id: int = 0,
) -> dict[str, Any]:
    now = _now_iso()
    return {
        "Id": session_id,
        "UserId": user_guid_for(member_id),
        "UserName": user_name,
        "Client": client,
        "DeviceId": device_id,
        "DeviceName": device_name,
        "ApplicationVersion": version,
        "ServerId": server_id,
        "PlayableMediaTypes": [],
        "SupportedCommands": [],
        "LastActivityDate": now,
        "LastPlaybackCheckIn": now,
        "IsActive": True,
        "SupportsMediaControl": False,
        "SupportsRemoteControl": False,
        "HasCustomDeviceName": False,
    }
