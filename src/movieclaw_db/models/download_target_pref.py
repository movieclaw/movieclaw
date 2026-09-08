"""下载保存位置记忆：每人每种子分类记住一次「文件放哪」的选择。

设计见 ``docs/design/download-target-memory.md``。三个要点：

- **桶键是 ``TorrentCategory``**（movie/tv/documentary/anime/music/game/av/other），
  即搜索页标签栏本身的维度、由各站 YAML 声明式映射而来。刻意不用 enrich 的
  ``media_type`` / ``content_type``：那两个是模型推断值，会 null、同一部剧的
  两条种子可能判得不一致，还会随模型版本漂移——拿会漂移的值当**持久化偏好的
  键**，意味着某次模型升级后用户的记忆桶会悄悄换位置。
- **记的是「目标」不是「路径」**：``kind="smart"`` 存的是「走智能入库」这个策略，
  每次提交前重跑预检算出当次路径；只有 ``kind="dir"`` 才有固定的 ``save_path``。
- **写入是自动的**：提交下载即 upsert，不需要用户勾选任何东西。原实现用
  「记住本次选择」复选框且默认不勾，等于要求用户在第一次下载时预判「以后还会
  不会下同类的」——这正是它在实际使用中几乎从不生效的原因。
"""

from __future__ import annotations

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin
from movieclaw_db.models.member_scoped import MemberScopedMixin, register_member_scoped


@register_member_scoped
class DownloadTargetPref(MemberScopedMixin, TimestampMixin, table=True):
    """一行 = 一个人 × 一个种子分类的保存位置记忆。

    ``member_id`` 由 ``MemberScopedMixin`` 提供（0=超管哨兵，非外键）。眼下能
    产生记忆的其实只有超管——成员被服务端强制自动路由，选不了目录也选不了
    下载器（见 ``api/routes/downloaders.py`` 的成员分支）。仍然带上成员维度，
    是因为这个列现在加进去零成本，将来若放开成员自选目录，表结构、清理逻辑、
    隔离语义都不用再动。
    """

    __tablename__ = "download_target_pref"
    __table_args__ = (
        UniqueConstraint("member_id", "category", name="uq_download_target_pref"),
    )

    id: int | None = Field(default=None, primary_key=True)
    category: str = Field(
        sa_column=Column(Text, nullable=False),
        description="种子分类（TorrentCategory 值）；站点未映射分类时归到 other",
    )
    kind: str = Field(
        sa_column=Column(Text, nullable=False),
        description="smart=智能入库 / dir=固定目录 / default=下载器默认目录",
    )
    save_path: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="固定目录（movieclaw 视角绝对路径）；仅 kind=dir 有值",
    )
    downloader_id: int | None = Field(
        default=None, description="指定的下载器；None=默认下载器"
    )
