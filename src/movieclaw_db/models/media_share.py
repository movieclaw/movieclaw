from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, ForeignKey, Integer
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class MediaShare(TimestampMixin, table=True):
    """影片分享（docs/design/media-share.md §3）：一条链接 = 一个条目对外可看。

    - 范围永远是一个条目：电影一部、剧集整部（含以后新入库的集）。``library_id``
      记的是从哪个库的详情页分享出去的，访客的可浏览库集合就只有这一个库；
    - ``slug`` 是链接里唯一的身份，96 位随机，不可猜；过期 / 取消后不复用，
      重新分享得到新 slug；
    - 「一部影片同一时间只有一条有效分享」由服务层保证（创建前先查有效行），
      不做数据库级约束——SQLite 的部分唯一索引写不了「未过期」这种带当前
      时间的条件；
    - 密码用 Fernet 可逆加密落库而不是哈希：创建者再次打开分享对话框要能
      看到密码原文；``password_version`` 在改密码 / 取消时递增，签在解锁
      Cookie 里的旧版本号即刻失效；
    - 过期与取消的行保留（只是不再有效），不做清理任务。

    条目 / 库删除时行级联删除（FK ondelete=CASCADE，引擎已开 foreign_keys）。
    """

    __tablename__ = "media_share"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True, max_length=32, description="链接里的分享标识")
    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="被分享的条目",
    )
    library_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("library.id", ondelete="CASCADE"),
            nullable=False,
        ),
        description="从哪个库分享出去的；访客只能看这个库里该条目的文件",
    )
    created_by_member_id: int = Field(default=0, description="创建者；0=超管（哨兵）")
    password_encrypted: str | None = Field(
        default=None, description="访问密码的 Fernet 密文；NULL=无密码"
    )
    password_version: int = Field(default=1, description="密码版本；变更即作废旧解锁 Cookie")
    expires_at: datetime = Field(index=True, description="到期时间（naive UTC）")
    revoked_at: datetime | None = Field(default=None, description="取消时间；NULL=未取消")
    view_count: int = Field(default=0, description="影片页成功打开的次数")
    last_accessed_at: datetime | None = Field(default=None, description="最近一次打开影片页")
