from __future__ import annotations

from sqlalchemy import BigInteger, Column, Index
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class LibraryDirSnapshot(TimestampMixin, table=True):
    """上一轮完整遍历时每个目录的 mtime——定期对账据此只重列变过的目录。

    对账的目的是"让台账追上磁盘"，它的成本却一直是 O(整库)：每 6 小时把每个
    根路径下的每一个目录都 ``readdir`` 一遍，网络挂载上每次 readdir 都是一轮
    往返，万级媒体库一轮要跑一两分钟，而绝大多数目录从上一轮到现在根本没变。

    目录的 mtime 只在**它自己的条目**增删改名时变化：往目录里放文件、删文件、
    改名，都会让父目录的 mtime 变；反过来，mtime 没变就意味着这个目录直接
    包含的文件与子目录一个都没变。于是对账可以只做一件事——比对每个目录的
    mtime，没变的**叶子目录**（没有可下钻子目录的目录，电影目录、剧集的季目录
    都是）直接跳过，不再 readdir，它底下的台账行按"仍在原位"处理。非叶子目录
    照常列一次：一次 readdir（READDIRPLUS 顺带带回全部子项属性）比逐个 stat 它的
    几个子目录更便宜，列完就知道各子目录该不该下钻。

    正确性与全量遍历等价：任何一处增删都会让某个"会被重列"的目录 mtime 变化
    （文件变动改父目录、子目录整个消失改祖父目录），不存在"没变的目录底下
    藏着变化"。保险起见每周仍强制一轮全量（``Library.dir_snapshot_full_at``），
    用户主动发起的扫描也永远是全量。

    一个目录一行，只存 mtime 与"是否叶子"两个事实。表由每轮可信的完整遍历
    按差异维护（新增/变化的写，消失的删），不存文件清单——文件清单在台账里。
    """

    __tablename__ = "library_dir_snapshot"
    __table_args__ = (
        # 对账开场按库整表读进内存；同库同路径唯一
        Index("ix_library_dir_snapshot_library_path", "library_id", "path", unique=True),
    )

    id: int | None = Field(default=None, primary_key=True)
    library_id: int = Field(index=True, description="所属媒体库")
    path: str = Field(description="目录的绝对路径（movieclaw 视角，与台账 file_path 同一套）")
    mtime_ns: int = Field(
        sa_column=Column(BigInteger, nullable=False),
        description="上一轮列出该目录前读到的 mtime（纳秒）；相等即目录直接内容未变",
    )
    leaf: bool = Field(
        description="上一轮该目录下没有可下钻的子目录；只有叶子目录可以凭 mtime 未变跳过"
    )
