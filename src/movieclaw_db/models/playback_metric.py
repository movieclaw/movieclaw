from __future__ import annotations

from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class PlaybackMetric(TimestampMixin, table=True):
    """一次网页播放的质量快照（docs/design/web-player.md §8）。

    **为什么要落库**：没有它就答不出这个播放器最重要的那个问题——
    **直通率**（档 0 + 档 1 占全部播放的比例）。这一个数同时代表画质
    （没重编码 = 无损）、速度（秒开）和服务器负担（不烧 GPU），其它指标各自
    只覆盖一个侧面。降档次数则是「决策引擎判错了多少」的直接度量。

    **只落本地**（硬边界 3）：写进自己的数据库、设置页可看、可导出，
    **绝不上报任何外部服务**，也不提供「匿名统计」开关。

    一次播放一行，不存逐事件流水——统计需要的是分布，不是回放。指标口径按
    CTA-2066，不自创，这样「卡顿率」在这里和在别处是同一个东西。
    """

    __tablename__ = "playback_metric"

    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(default=0, index=True, description="归属成员；0=超管（哨兵）")
    library_file_id: int | None = Field(
        default=None, index=True, description="播的哪个文件；文件被删后置空不影响历史统计"
    )

    #: 最终采用的档位（0 直连 / 1 remux / 2 音频单转 / 3 硬件 / 4 软件）。
    #: 直通率就是 tier <= 1 的占比。
    tier: int = Field(index=True)
    #: 降档前的档位；非空即表示上一档播失败了——决策判错的直接证据。
    degraded_from: int | None = Field(default=None)
    #: 播放引擎：direct / hls.js / native-hls。iOS 与桌面的表现差异靠它区分。
    engine: str = Field(default="")
    #: 硬件后端名；软件转码或直通为空。
    hw_backend: str = Field(default="")

    ttff_ms: int | None = Field(default=None, description="点击播放→首帧渲染（毫秒）")
    rebuffer_ms: int = Field(default=0, description="卡顿总时长，不含 seek 等待")
    rebuffer_count: int = Field(default=0)
    seek_count: int = Field(default=0)
    dropped_frames: int | None = Field(default=None)
    total_frames: int | None = Field(default=None)
    watched_ms: int = Field(default=0, description="实际观看时长，卡顿率的分母")
