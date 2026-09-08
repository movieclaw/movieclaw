from __future__ import annotations

from datetime import datetime

from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin, utcnow
from movieclaw_db.models.member_scoped import MemberScopedMixin, register_member_scoped


@register_member_scoped
class PlaybackLog(MemberScopedMixin, TimestampMixin, table=True):
    """播放日志——每一场播放一行（docs/design/activity.md「播放日志与统计」）。

    与 ``playback_state`` 的分工：状态表回答「看到哪了 / 看没看过」，同一集重看
    会覆盖，所以标不出是哪台设备、什么时候、看了多久；本表是**日志**，一次
    开始播放到停止（或超时）记一行，活动页的「播放记录」与「观看统计」都从
    这里出。两条播放入口（网页 / Jellyfin）经同一个上报服务写入。

    设计要点：
    - 条目身份用 ``media_item_id`` 数字锚，但**不做外键**，并把片名与形态
      快照进来——统计要在条目删除、刮削改名之后仍然成立；
    - 成员同 ``playback_state`` 的哨兵约定（0=超管），不做外键，删成员时由
      服务层清理；
    - ``watched_ms`` 是按进度心跳的位置增量累加的**实际观看时长**：暂停不计、
      seek 跳过的区间不计（单次增量超过心跳上限的视为 seek 丢弃），与
      ``end_position_ms - start_position_ms`` 这个「播到哪」是两个读数；
    - 一行只在开始与停止时明确开合；播放器异常退出不会发停止，``ended_at``
      留空，读侧按 ``last_seen_at`` 超过会话保鲜期视为已结束，写侧在同一设备
      下次开始播放时把旧的开行收口。
    """

    __tablename__ = "playback_log"

    id: int | None = Field(default=None, primary_key=True)
    # member_id 由 MemberScopedMixin 提供（0=超管哨兵、非外键）；本表登记进
    # 成员级注册表后，删除成员时随 delete_member 一并清理——此前 docstring
    # 写着"删成员时由服务层清理"，但 delete_member 实际漏了这张表
    media_item_id: int = Field(index=True, description="媒体条目身份锚（无外键，条目删后仍可统计）")
    kind: str = Field(default="movie", description="内容形态快照：movie / tv / video")
    title: str = Field(default="", description="片名快照")
    season_number: int = Field(default=0, description="季号；电影=0（哨兵）")
    episode_number: int = Field(default=0, description="集号；电影=0（哨兵）")

    device_id: str = Field(
        default="", index=True, description="播放设备标识（Jellyfin DeviceId / web-…）"
    )
    client: str = Field(default="", description="客户端名（Infuse / MovieClaw Web …）")
    device_name: str = Field(default="", description="设备名")

    started_at: datetime = Field(default_factory=utcnow, index=True, description="本场开始时间")
    last_seen_at: datetime = Field(default_factory=utcnow, description="最近一次进度上报时间")
    ended_at: datetime | None = Field(
        default=None, index=True, description="停止时间；NULL=未收到停止"
    )
    start_position_ms: int = Field(default=0, description="开始时的位置（毫秒）")
    end_position_ms: int = Field(default=0, description="最近上报的位置（毫秒）")
    watched_ms: int = Field(default=0, description="实际观看时长（毫秒，按进度增量累加）")
    completed: bool = Field(default=False, description="本场是否把这一集看完（翻转为已看）")
