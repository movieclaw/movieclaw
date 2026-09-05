"""服务器/用户身份 DTO（设计文档 3.2/4.2，字段清单逐项对照源码核实）。

UserDto/Policy/Configuration 里列出的键都是"真 Jellyfin 必然输出"的字段
（非可空或带非 null 默认值）；可空且为 null 的字段按协议约定不输出。
"""

from __future__ import annotations

from typing import Any

from movieclaw_api.services import auth as auth_service
from movieclaw_api.settings.schemas import JellyfinCompatSetting, get_jellyfin_compat
from movieclaw_db.models.member import Member
from movieclaw_jellyfin.ids import library_guid, user_guid, user_guid_for

# 对外报的 Jellyfin 版本：真实存在的 10.10 系版本号，命中客户端兼容分支
REPORTED_VERSION = "10.10.7"
PRODUCT_NAME = "Jellyfin Server"


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
        "DisplayCollectionsView": False,
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
) -> dict[str, Any]:
    """用户 Policy。超管（member=None）管理位全开；成员按权限投影：
    非管理员、不可删内容。库可见性经 EnabledFolders 下发给客户端（客户端
    据此过滤其自建的合集/快捷入口；服务端查询侧另有强制过滤）——传入
    ``visible_library_ids`` 时超管与成员一视同仁：超管把自己从某个库的浏览
    范围摘掉后，电视端也不该再列出它（docs/design/library-access.md）。"""
    if member is not None:
        policy = user_policy(visible_library_ids=visible_library_ids)  # 以超管为底，只改差异字段
        policy["IsAdministrator"] = False
        policy["IsDisabled"] = member.status != "active"
        return policy
    policy = _admin_policy()
    if visible_library_ids is not None:
        policy["EnableAllFolders"] = False
        policy["EnabledFolders"] = [library_guid(i) for i in sorted(visible_library_ids)]
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
) -> dict[str, Any]:
    """用户 DTO。member=None → 超管（原单用户形态，GUID 不变）；
    传入成员 → 以其登录名/GUID/Policy 投影（电视登录页的多头像来源）。"""
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
            "Policy": user_policy(member, visible_library_ids),
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
        "Policy": user_policy(visible_library_ids=visible_library_ids),
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
