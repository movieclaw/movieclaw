"""ORM 表模型集中导出。

⚠️ 所有 ``table=True`` 的模型都必须在此导入，原因有二：
1. 只有被导入，模型才会注册到 ``SQLModel.metadata``，Alembic 自动生成迁移、
   以及 create_all 才能感知到这些表。
2. 给上层提供统一的导入入口：``from movieclaw_db.models import SiteCredential``。
"""

from __future__ import annotations

from movieclaw_db.models.agent_session import AgentSession
from movieclaw_db.models.app_setting import AppSetting
from movieclaw_db.models.base import TimestampMixin, utcnow
from movieclaw_db.models.cache_entry import CacheEntry
from movieclaw_db.models.channel_account import ChannelAccount, ChannelAccountStatus
from movieclaw_db.models.download_hint import DownloadHint
from movieclaw_db.models.downloader_client import ClientType, DownloaderClient
from movieclaw_db.models.import_watch import ImportWatch
from movieclaw_db.models.ingest_entry import IngestEntry, IngestStatus
from movieclaw_db.models.jellyfin_device import JellyfinDevice
from movieclaw_db.models.job import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    Job,
    JobEvent,
    JobLock,
    JobResource,
    JobStatus,
)
from movieclaw_db.models.library import Library
from movieclaw_db.models.library_file import FileSource, FileState, LibraryFile
from movieclaw_db.models.llm_provider import LlmProvider
from movieclaw_db.models.manual_download_intent import ManualDownloadIntent
from movieclaw_db.models.media_item import MediaItem, MediaSeason
from movieclaw_db.models.media_metadata import MediaEpisode, MediaMetadata
from movieclaw_db.models.member import Member, MemberLibraryAccess, MemberSiteAccess
from movieclaw_db.models.person import MediaItemPerson, Person
from movieclaw_db.models.playback_metric import PlaybackMetric
from movieclaw_db.models.playback_state import PlaybackState
from movieclaw_db.models.ratio_boost_stat import RatioBoostStat
from movieclaw_db.models.ratio_boost_task import BoostTaskState, RatioBoostTask
from movieclaw_db.models.ratio_boost_task_sample import RatioBoostTaskSample
from movieclaw_db.models.rule_set import RuleSet
from movieclaw_db.models.scheduled_task import (
    ScheduledTask,
    TaskRun,
    TaskRunStatus,
    TriggerType,
)
from movieclaw_db.models.search_history import SearchHistory
from movieclaw_db.models.site_cookie import SiteCookie
from movieclaw_db.models.site_credential import AuthType, ConfigStatus, SiteCredential
from movieclaw_db.models.site_torrent import (
    SiteSyncCursor,
    SiteTorrent,
    TorrentSource,
)
from movieclaw_db.models.site_torrent_swarm_sample import SiteTorrentSwarmSample
from movieclaw_db.models.site_user_profile import SiteUserProfile
from movieclaw_db.models.subscription import (
    DownloadAttemptStatus,
    Subscription,
    SubscriptionDownloadAttempt,
    SubscriptionFollower,
    SubscriptionStatus,
    WantedItem,
    WantedStatus,
)
from movieclaw_db.models.subscription_activity import ActivityType, SubscriptionActivity
from movieclaw_db.models.subtitle_auto_mute import SubtitleAutoMute
from movieclaw_db.models.system_notice import NoticeSeverity, NoticeStatus, SystemNotice

__all__ = [
    "TimestampMixin",
    "utcnow",
    "AgentSession",
    "CacheEntry",
    "ChannelAccount",
    "ChannelAccountStatus",
    "SiteCookie",
    "SiteCredential",
    "AuthType",
    "ConfigStatus",
    "ScheduledTask",
    "TaskRun",
    "TaskRunStatus",
    "TriggerType",
    "AppSetting",
    "ClientType",
    "DownloadHint",
    "DownloaderClient",
    "FileSource",
    "FileState",
    "ImportWatch",
    "IngestEntry",
    "IngestStatus",
    "JellyfinDevice",
    "Job",
    "JobEvent",
    "JobLock",
    "JobResource",
    "JobStatus",
    "ACTIVE_JOB_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "Library",
    "LibraryFile",
    "LlmProvider",
    "ManualDownloadIntent",
    "MediaEpisode",
    "MediaItemPerson",
    "MediaItem",
    "MediaMetadata",
    "Member",
    "MemberLibraryAccess",
    "MemberSiteAccess",
    "Person",
    "PlaybackMetric",
    "PlaybackState",
    "MediaSeason",
    "BoostTaskState",
    "RatioBoostStat",
    "RatioBoostTask",
    "RatioBoostTaskSample",
    "RuleSet",
    "Subscription",
    "SubscriptionDownloadAttempt",
    "DownloadAttemptStatus",
    "SubscriptionFollower",
    "SubscriptionStatus",
    "WantedItem",
    "WantedStatus",
    "ActivityType",
    "SubscriptionActivity",
    "SubtitleAutoMute",
    "SearchHistory",
    "SiteTorrent",
    "SiteTorrentSwarmSample",
    "SiteSyncCursor",
    "TorrentSource",
    "SiteUserProfile",
    "SystemNotice",
    "NoticeSeverity",
    "NoticeStatus",
]
