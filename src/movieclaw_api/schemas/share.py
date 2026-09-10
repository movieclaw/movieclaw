"""影片分享（docs/design/media-share.md）的接口模型。

两侧两套视图：

- 管理侧 ``ShareView``：给创建者看，带密码原文与链接；
- 访客侧 ``SharePublicView`` / ``SharedItemView``：只给访客看得到的东西——
  详情视图里的落盘路径、库归属、待处理 / 回收站 / 管理相关字段一律不进。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel
from movieclaw_api.schemas.library import (
    AudioStreamView,
    ChapterView,
    LocalMetaView,
    SubtitleStreamView,
)
from movieclaw_media.models import MediaKind


class ShareCreateRequest(BaseModel):
    """创建分享。有效期只有四档，没有「永久」。"""

    expires_in_days: int = Field(default=7, description="有效期（天）：1 / 3 / 7 / 30")
    password: str | None = Field(default=None, max_length=64, description="访问密码；空 = 不设密码")


class ShareView(BaseModel):
    """一条分享（创建者视角）。"""

    id: int
    slug: str
    url: str = Field(description="分享链接；未配置外部访问地址时为相对路径 /s/{slug}")
    #: 范围二选一：条目分享给 media_item_id，合集分享给 collection_id
    media_item_id: int | None = None
    collection_id: int | None = None
    library_id: int | None = None
    title: str
    kind: MediaKind | None = Field(
        default=None, description="被分享条目的形态；合集分享为 null"
    )
    year: int | None = None
    poster_url: str | None = None
    item_count: int | None = Field(
        default=None, description="合集分享此刻有几部；条目分享为 null"
    )
    password: str | None = Field(default=None, description="访问密码原文；无密码为 null")
    expires_at: datetime
    created_at: datetime
    view_count: int
    last_accessed_at: datetime | None


class SharePublicView(BaseModel):
    """访客打开链接时的探针：只说「要不要密码」，不露片名海报。"""

    requires_password: bool
    unlocked: bool = Field(description="无密码恒为 true；有密码时表示本浏览器已解锁")
    expires_at: datetime
    media_item_id: int | None = Field(
        default=None, description="被分享的条目 id；解锁之前、或分享的是合集时为 null"
    )
    collection_id: int | None = Field(
        default=None, description="被分享的合集 id；解锁之前、或分享的是条目时为 null"
    )


class ShareUnlockRequest(BaseModel):
    password: str = Field(max_length=64)


class SharedFileView(BaseModel):
    """访客能看到的一个文件：只有规格与章节，没有路径、文件名、生命周期细节。"""

    id: int
    size_bytes: int
    container: str | None
    resolution: str | None
    video_codec: str | None
    hdr: str | None
    bit_depth: int | None
    duration_seconds: int | None
    media_source: str | None
    season_number: int
    episode_number: int
    missing: bool
    state: str
    audio_streams: list[AudioStreamView] | None = None
    subtitle_streams: list[SubtitleStreamView] = Field(default_factory=list)
    chapters: list[ChapterView] | None = None


class SharedItemView(BaseModel):
    """分享页的影片信息：详情视图的浏览面投影。"""

    media_item_id: int
    kind: MediaKind
    #: 外部词条锚点（TMDB / IMDb / 豆瓣），分享页底部「相关链接」；公开信息
    tmdb_id: int | None
    imdb_id: str | None
    douban_id: str | None
    title: str
    original_title: str
    year: int | None
    poster_url: str | None
    backdrop_url: str | None
    primary_aspect: float
    local_meta: LocalMetaView | None
    files: list[SharedFileView]
    seasons: list[int]
    expires_at: datetime


class SharedCollectionItemView(BaseModel):
    """合集分享页上的一格：只有认得出这部片所需的最少信息。"""

    media_item_id: int
    title: str
    year: int | None = None
    kind: MediaKind
    poster_url: str | None = None


class SharedCollectionView(BaseModel):
    """分享页的合集信息：名字 + 此刻的成员卡片。

    成员是**每次访问现算**的（走 resolve_members）：规则驱动的合集会自己长，
    分享出去之后新入库的片也会出现在里面——这正是分享一个合集而不是一串
    条目的意义。反过来，被移出去的片立刻打不开，不需要任何撤销动作。
    """

    name: str
    item_count: int
    items: list[SharedCollectionItemView] = Field(default_factory=list)


