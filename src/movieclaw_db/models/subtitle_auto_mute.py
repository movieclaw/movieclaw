from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class SubtitleAutoMute(TimestampMixin, table=True):
    """「不再自动生成字幕」台账：让用户的"忽略"能真正生效一次以上。

    为什么必须落库：入库后自动生成（``subtitle_gen.auto``）的"同一文件本进程
    只自动尝试一次"是**进程内**集合，容器一重启就清零。用户在任务中心忽略掉
    一条失败的字幕任务后，下一次扫描收尾又会为同一个文件建一条新任务、再失败
    一次——忽略变成了西西弗斯。台账把这个决定写进数据库，扫描前置查表短路。

    与「监听导入清单」的人工忽略同构（services/library/ingest.py）：用户拍板
    的忽略是持久的，恢复走显式动作（任务中心「撤销忽略」），不靠系统自愈。

    - 手动触发不受静音限制。静音防的是系统**自动**反复重试烧钱，不是禁止
      用户自己再试一次；这与静音之前 ``_attempted`` 的语义完全一致。
    - 外键级联删除：文件从库里消失，静音记录随之消失。不能只按 file_id 存
      裸整数——SQLite 的行 id 会在删除后被复用，那会让一个新文件凭空继承
      前任的静音。
    """

    __tablename__ = "subtitle_auto_mute"
    __table_args__ = (
        UniqueConstraint(
            "library_file_id", "target_language", name="uq_subtitle_auto_mute_file_language"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    library_file_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("library_file.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="被静音的媒体库文件",
    )
    target_language: str = Field(
        index=True,
        description="被静音的目标语言 token（与任务 input_data.target_language 同口径，如 chs）",
    )
    muted_by: str | None = Field(default=None, description="操作者（审计用）")
