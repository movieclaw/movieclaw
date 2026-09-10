from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class Member(TimestampMixin, table=True):
    """成员账号（docs/design/member-management.md §3.1）。

    超管**不在**本表——超管是 ``auth.admin`` 配置域里的一条配置（明确的产品
    决策：超管账号与成员体系互不混用），成员体系在其之外另立。登录时先匹配
    超管再查本表，建成员时校验用户名与超管互斥（大小写不敏感）。

    会话失效不靠轮换全局签名密钥（那会踢掉所有人），而靠 ``token_version``：
    改密/停用/重置密码时 +1，会话令牌负载里携带签发时的版本号，校验时不匹配
    即 401——只踢这一个人。

    能力开关与资源白名单遵循"三件套范式"（设计文档 §2.1）：开关在本表，
    库/站点白名单在关联表，判定收口在服务层。
    """

    __tablename__ = "member"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True, description="登录名，与超管用户名互斥")
    password_hash: str = Field(description="argon2 哈希（与超管同一套 pwdlib 配置）")
    nickname: str = Field(default="", description="展示昵称；空串时前端回退展示用户名")
    avatar_path: str | None = Field(default=None, description="头像文件相对 data/ 的路径")
    status: str = Field(default="active", index=True, description="active / disabled")
    token_version: int = Field(default=0, description="会话版本号，+1 即踢掉该成员全部会话")
    last_login_at: datetime | None = Field(
        default=None, description="最近登录时间；成员列表用它回答「这个号还有人用吗」"
    )

    # ---- 能力开关（设计文档 §2.2 的三问准则筛出的 v1 全集）----
    allow_subscribe: bool = Field(default=True, description="发起/关注订阅")
    allow_search: bool = Field(default=False, description="站点聚合搜索")
    allow_direct_download: bool = Field(
        default=False, description="搜索结果一键下载（依赖 allow_search，服务端独立校验）"
    )

    # ---- 资源白名单模式开关（True=全部可见含未来新建；False=查关联表）----
    all_libraries: bool = Field(default=True, description="库可见性：是否不受白名单限制")
    all_sites: bool = Field(default=True, description="站点可用性：是否不受白名单限制")

    # ---- 内容分级约束（儿童档案，docs/design/library-filtering.md F5）----
    # 与「能力开关」和「库白名单」的分别：那两者管的是**能不能进这个门**，
    # 这一条管的是**进门之后能看见哪些片**——一个混着 R 级片的电影库，
    # 光靠库白名单是保护不了小孩的（除非专门给他建一个库，那是另一件事）。
    #
    # 它是**强制收窄**，不是筛选：用户自己选的筛选条件可以清空、可以存成合集，
    # 这一条不行——它在海报墙、搜索、合集、Jellyfin、条目详情与起播六处一律
    # 生效，判定收口在 services/library/access.content_limit_for()。
    content_age_limit: int | None = Field(
        default=None,
        description="内容年龄上限（岁）；NULL=不限。分级串→年龄的映射见 library/content_rating.py",
    )
    # 大量中文影片在 TMDB 上**没有分级信息**。设了年龄上限之后，未分级的片
    # 默认**一并隐藏**——这是一条安全开关，"我不确定的一律不给看"才是家长要的
    # 默认值。觉得太狠的可以把它打开，界面上会如实说明代价
    allow_unrated: bool = Field(
        default=False, description="设了年龄上限时，未分级的作品是否仍可见"
    )

    # ---- 个人界面偏好（P2）----
    # 整体覆盖式 JSON（结构同 ui.preferences 配置域）；None = 尚未设置，
    # 读取时回退默认值。超管继续用全局配置域，成员各存各的。
    ui_prefs: dict | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="成员的界面偏好；None=默认",
    )


class MemberLibraryAccess(TimestampMixin, table=True):
    """成员 × 库 的可见性白名单，仅 ``member.all_libraries=False`` 时生效。

    用关联表而非 member 上的 JSON 列表（设计文档 §3.6）：反向查询可索引、
    库删除靠外键级联清理、未来"库 × 成员"的新属性（如权限等级）加列即可。
    """

    __tablename__ = "member_library_access"
    __table_args__ = (
        UniqueConstraint("member_id", "library_id", name="uq_member_library_access"),
    )

    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("member.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    library_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("library.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )


class MemberSiteAccess(TimestampMixin, table=True):
    """成员 × 站点 的可用性白名单，仅 ``member.all_sites=False`` 时生效。

    ``site_id`` 是 YAML 注册表标识（字符串，如 ``mteam``），**非外键**——
    站点不是数据库实体，从注册表移除后留下的悬空行在判定时自然失效
    （等价于不可用），无需清理。
    """

    __tablename__ = "member_site_access"
    __table_args__ = (
        UniqueConstraint("member_id", "site_id", name="uq_member_site_access"),
    )

    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("member.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    site_id: str = Field(index=True, description="站点注册表标识，如 mteam")
