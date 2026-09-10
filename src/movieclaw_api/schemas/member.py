"""成员管理的请求 / 响应模型（docs/design/member-management.md §3.9.1）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from movieclaw_api.schemas.base import BaseModel


class MemberCreateRequest(BaseModel):
    """新建成员：用户名 + 初始密码（前端默认一键生成随机密码）。"""

    username: str = Field(min_length=3, max_length=32, description="登录名，创建后不可改")
    password: str = Field(min_length=8, max_length=128, description="初始密码，至少 8 位")
    nickname: str = Field(default="", max_length=32, description="展示昵称，可留空")


class MemberUpdateRequest(BaseModel):
    """编辑成员：None 字段不改动；白名单为整体覆盖式保存。"""

    nickname: str | None = Field(default=None, max_length=32)
    allow_subscribe: bool | None = None
    allow_search: bool | None = None
    allow_direct_download: bool | None = None
    all_libraries: bool | None = Field(
        default=None, description="True=全部库可见（含未来新建）；False=按 library_ids 白名单"
    )
    library_ids: list[int] | None = Field(
        default=None, description="可见库白名单（整体覆盖）；仅 all_libraries=False 时生效"
    )
    all_sites: bool | None = Field(
        default=None, description="True=全部站点可用；False=按 site_ids 白名单"
    )
    site_ids: list[str] | None = Field(
        default=None, description="可用站点白名单（整体覆盖）；仅 all_sites=False 时生效"
    )
    content_age_limit: int | None = Field(
        default=None,
        ge=-1,
        le=21,
        description=(
            "内容年龄上限（岁）。设了之后，超过这个年龄分级的作品在海报墙、搜索、"
            "合集、Jellyfin、条目详情与起播六处一律不可见。"
            "**传 -1 表示取消上限**（传 null 是「不改动」，两者不是一回事）"
        ),
    )
    allow_unrated: bool | None = Field(
        default=None,
        description=(
            "设了年龄上限时，未分级的作品是否仍可见。默认关闭——大量中文影片在 TMDB 上"
            "没有分级信息，「我不确定的一律不给看」才是家长要的默认值"
        ),
    )


class MemberStatusRequest(BaseModel):
    """启用 / 停用成员。停用即时踢下线，数据全部保留。"""

    enabled: bool


class MemberView(BaseModel):
    """成员详情（管理页列表与详情共用）。"""

    id: int
    username: str
    nickname: str
    avatar_url: str | None = None
    status: str
    last_login_at: datetime | None = Field(
        default=None, description="最近登录时间；None=从未登录"
    )
    allow_subscribe: bool
    allow_search: bool
    allow_direct_download: bool
    all_libraries: bool
    library_ids: list[int] = Field(default_factory=list)
    all_sites: bool
    site_ids: list[str] = Field(default_factory=list)
    content_age_limit: int | None = Field(
        default=None, description="内容年龄上限（岁）；null=不限"
    )
    allow_unrated: bool = Field(default=False, description="设了上限时未分级的作品是否可见")
    created_at: datetime


class MemberPasswordResetView(BaseModel):
    """重置密码的返回体：新密码明文仅此一次，请立即复制发给成员。"""

    id: int
    username: str
    password: str = Field(description="新密码明文；服务端只存哈希，之后无法再次查看")
