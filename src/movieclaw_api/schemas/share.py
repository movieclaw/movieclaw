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
    media_item_id: int
    library_id: int
    title: str
    kind: MediaKind
    year: int | None
    poster_url: str | None
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
        default=None, description="被分享的条目 id；解锁之前为 null（播放页据此起播）"
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
