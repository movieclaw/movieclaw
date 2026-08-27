from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Column, ForeignKey, Integer, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin

# 手动下载锚只用于覆盖“投递成功到入库完成”的在途窗口。超窗仍未被消费的
# 记录通常对应已从下载器删除的孤儿任务；查询与写入清理必须共用同一口径，
# 否则旧锚会让监听条目永久停在“等待下载器”。
MANUAL_DOWNLOAD_INTENT_TTL = timedelta(days=90)


class ManualDownloadIntent(TimestampMixin, table=True):
    """手动下载的身份锚：把下载器任务与已确认的媒体条目连起来。

    手动搜索没有订阅工单可记录 infohash。种子若投到共享监听目录，完成后
    只能看目录名；拼音名或同名资源会再次识别失败，也无法还原提交时选出的
    目标库。本表在提交成功后以 infohash 保存「媒体条目 + 路由后的库」，
    让监听导入直接认领，不重新猜测。

    同一种子重复提交返回相同 hash，因此 hash 唯一：保留最先确认的意图，
    避免后一次点击静默改写正在下载任务的入库归属。
    """

    __tablename__ = "manual_download_intent"

    id: int | None = Field(default=None, primary_key=True)
    info_hash: str = Field(
        sa_column=Column(Text, nullable=False, unique=True, index=True),
        description="下载器任务的 infohash（小写十六进制）",
    )
    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="手动下载时确认的媒体条目",
    )
    library_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("library.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="按收藏范围路由后确认的目标媒体库",
    )
    downloader_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("downloader_client.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        description="实际承载任务的下载器；NULL=旧数据",
    )
    download_name: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True, index=True),
        description="下载器返回的真实任务/内容名；用于监听目录可靠关联",
    )
    save_path: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="MovieClaw 视角的投递路径；用于同名任务消歧",
    )
    site_id: str | None = Field(default=None, description="来源站点（追溯用）")
    # 站点内种子 ID：与 site_id 一起反查 site_torrent 的详情页，供任务中心
    # 提供「打开种子页」。旧数据/前端未传时为 NULL，仅少一个跳转入口
    torrent_id: str | None = Field(default=None, description="站点内种子 ID；NULL=未知")
