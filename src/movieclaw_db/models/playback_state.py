from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin
from movieclaw_db.models.member_scoped import MemberScopedMixin, register_member_scoped


@register_member_scoped
class PlaybackState(MemberScopedMixin, TimestampMixin, table=True):
    """观看状态——"看到哪了/看没看过"的领域事实（docs/design/jellyfin-compat.md 5.4/8.5）。

    协议无关的领域层数据：Jellyfin 兼容层与未来的网页端播放器共用同一张表、
    同一套读写服务。因此进度单位是**毫秒**而非 Jellyfin 的 100ns ticks——
    ticks 换算收敛在协议边界做。

    键沿用全局约定的 (media_item_id, season_number, episode_number) 数字对
    （电影 (0,0) 哨兵，与 wanted_item / library_file 同源），不锚 media_episode
    行 id（集行随元数据刷新可增删重建）。

    成员维度（docs/design/member-management.md §3.4）：``member_id`` 进唯一
    约束，每人各一行——进度、已看、收藏全部按人隔离。**哨兵值 0 = 超管**，
    刻意不用 NULL：SQLite 的 UNIQUE 视 NULL 互不相等，可空列进唯一约束会
    让同一单元插出无限行。也因此本列不是外键——删除成员时由服务层显式
    清理其状态行。存量数据迁移后 member_id=0（历史进度归超管，语义正确）。
    """

    __tablename__ = "playback_state"
    __table_args__ = (
        UniqueConstraint(
            "member_id",
            "media_item_id",
            "season_number",
            "episode_number",
            name="uq_playback_state_unit",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    # member_id 由 MemberScopedMixin 提供（0=超管哨兵、非外键、删除成员时显式清理）

    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="媒体条目身份锚",
    )
    season_number: int = Field(default=0, description="季号；电影=0（哨兵）")
    episode_number: int = Field(default=0, description="集号；电影=0（哨兵）")

    position_ms: int = Field(default=0, description="续播位置（毫秒）；0=无续播点")
    # 轨选择记忆（docs/design/jellyfin-subtitle.md §3.3）：存协议无关的
    # 中性轨引用（"embedded:<k>" / "external:<文件名>" / 字幕特有 "off"），
    # 不存 Jellyfin 的合成流序号——序号随补探回填漂移，中性引用天然免疫。
    # NULL=从未上报过轨选择。
    audio_track: str | None = Field(default=None, description="记忆的音轨（中性引用）")
    subtitle_track: str | None = Field(
        default=None, description="记忆的字幕轨（中性引用；off=明确关闭）"
    )
    played: bool = Field(default=False, index=True, description="是否已看完")
    play_count: int = Field(default=0, description="播放次数（开始播放时 +1）")
    is_favorite: bool = Field(default=False, description="是否收藏")
    # 收藏**这一次**的时间。不能用 updated_at 代替：那一列任何写入都会动
    # （进度上报、标记已看、记忆轨选择），于是"两年前收藏、昨晚看过一遍"的片
    # 会排到收藏列表最前面——那不是用户理解的"最近收藏"。
    # 只在 is_favorite 由非真变真时刷新；重复收藏是幂等的，不该改写时间。
    # NULL=这一行从未被收藏过，或收藏发生在本列落地之前（迁移回填成 updated_at）。
    favorited_at: datetime | None = Field(
        default=None, index=True, description="收藏时间；NULL=从未收藏"
    )
    last_played_at: datetime | None = Field(
        default=None, index=True, description="最近一次播放活动时间；NULL=从未播放"
    )
