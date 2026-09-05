from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Column
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin
from movieclaw_db.models.site_credential import ConfigStatus


class ClientType(StrEnum):
    """下载器类型。取值与 ``movieclaw_downloader.DownloaderType`` 一一对应。

    此处独立定义而非直接 import —— movieclaw_db 是纯存储层，
    不反向依赖领域库（与 SiteCredential 不依赖 tracker 同理）。
    """

    QBITTORRENT = "qbittorrent"
    TRANSMISSION = "transmission"


class DownloaderClient(TimestampMixin, table=True):
    """下载器配置表：保存用户接入的下载软件连接信息。

    用户可以接入多个下载器（如家里一台 qBittorrent、NAS 一台 Transmission），
    每条记录一个实例，``name`` 由用户命名用于区分。

    验证状态机与站点配置完全一致（复用 ConfigStatus）：保存后置 PENDING，
    异步测试连接，成功 ACTIVE / 失败 FAILED（原因见 last_error）。
    「可用」= ``enabled=True`` 且 ``status=ACTIVE``。

    安全：``password`` 经 SecretBox 加密后落库（带 ``enc::`` 前缀的密文），
    加解密统一在 Repository 层完成，模型自身不感知。
    """

    __tablename__ = "downloader_client"

    id: int | None = Field(default=None, primary_key=True)
    # 用户起的名字（如"家里的 qBittorrent"），用于列表区分，全局唯一
    name: str = Field(index=True, unique=True, description="用户命名的下载器名称")
    client_type: ClientType = Field(description="下载器类型")
    # 下载器 Web 服务完整地址：qBittorrent 为 WebUI 地址，Transmission 为 RPC 地址
    url: str = Field(description="下载器地址，如 http://192.168.1.10:8080")
    username: str | None = Field(default=None, description="登录用户名（未开鉴权可留空）")
    password: str | None = Field(default=None, description="登录密码（SecretBox 加密密文）")
    # 提交下载时的默认保存目录；None 表示使用下载器自己的默认目录。
    # movieclaw 视角的路径（界面上弹窗选择），提交前经 path_mappings 翻译
    save_path: str | None = Field(default=None, description="默认保存目录（movieclaw 视角）")

    # 路径映射：movieclaw 与下载器不在同一容器/主机、同一块盘两边路径不同时，
    # 声明挂载对照 [{"local": "/data/downloads", "remote": "/downloads"}]。
    # 提交下载前按最长前缀把 movieclaw 视角的保存目录翻译成下载器视角；
    # None/空 = 两边视角一致，路径原样提交（绝大多数直装部署）
    path_mappings: list[dict[str, str]] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="路径映射（movieclaw 路径 → 下载器路径）",
    )

    # 是否启用；停用后不出现在"提交下载"的可选目标里，但保留配置便于恢复
    enabled: bool = Field(default=True, description="用户启用开关")

    # 是否为默认下载器（一键下载不选目标时投给它）。
    # 不变量（由 Repository 维护）：只要存在下载器，就有且只有一个默认 ——
    # 第一台自动成为默认；删除默认时自动把默认让给剩下的一台。
    is_default: bool = Field(default=False, description="是否为默认下载器")

    # 验证状态机（语义见 ConfigStatus）
    status: ConfigStatus = Field(default=ConfigStatus.PENDING, description="连接验证状态")
    last_error: str | None = Field(default=None, description="最近一次测试失败原因（中文）")
    last_checked_at: datetime | None = Field(default=None, description="最近一次测试时间")
    # 最近一次连接成功时获取的下载器版本号（如 v5.0.2），供管理页展示
    version: str | None = Field(default=None, description="下载器版本号")

    # 路径映射的可达性体检结果（movieclaw_api.services.downloader_paths.PathProbe
    # 的扁平列表）。「API 通、路径瞎」是真实发生过的组合：容器 bind mount 静默
    # 失效成空目录、配置看着完全正常、下载器绿灯常亮，而所有下载都无法入库。
    # 连接测试通过后顺带体检每条映射的 local 侧，让"可用"不再只等于"API 通"。
    # NULL = 尚未体检（存量行或未配置映射），不参与任何判定
    path_health: list[dict[str, str]] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="路径映射体检结果（每条映射的可达状态与说明）",
    )

    # 刷流带宽哨兵学到的安全下载包络（字节/秒）：AIMD 闭环的持久化状态。
    # 上行受压时乘性回退、健康时加性恢复，收敛到"该环境下不伤上传的刷流
    # 下载总速度"。NULL = 尚未观测到拥塞（不限速）——对称大带宽环境会
    # 一直保持 NULL，零配置零行为变化。机制见 boost_bandwidth 模块注释
    boost_dl_envelope_bytes: int | None = Field(
        default=None, description="刷流安全下载包络（字节/秒）；NULL=不限"
    )
