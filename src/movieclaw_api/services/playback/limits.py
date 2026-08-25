"""播放资源上限的自动推导（替代原「设置 → 播放」的四个数字项）。

这些上限决定播放最多能占多少机器资源，设错的代价（转码把 CPU 吃满拖死全站、
分片写满磁盘毒死 SQLite）由整个应用承担，但普通用户并不知道该怎么设——
独立设置页大概率没人会调、调了也未必更好。因此 2026-08-25 起撤下设置页，
全部按机器实际规格自动取值；``software_transcode_enabled`` 仍是配置项
（它是「要不要接受软转的代价」的**意愿**问题，由播放页弹窗征求同意）。

三个推导各自的依据都写在函数注释里。纯函数 + 显式入参，便于表驱动单测。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: 转码输出高度上限。4K 转码的算力开销是 1080p 的四倍以上，家用设备几乎
#: 都撑不住实时；而「需要转码」本身就意味着浏览器播不了原始流，此时 1080p
#: 已是观感与算力的最优折中（Jellyfin/Emby 的默认转码档同样落在 1080p）。
MAX_TRANSCODE_HEIGHT = 1080

#: 直通（remux / 只转音轨)会话上限。瓶颈在磁盘 IO 不在算力——机械盘上
#: 两路 4K remux 就能打满随机读，而 CPU 几乎是空的。磁盘能力没有便宜的
#: 探测手段（跑基准测试比设错更扰民），取一个 SSD 富余、机械盘不至于
#: 互相拖死的定值。
MAX_REMUX_CONCURRENCY = 4

_QUOTA_FLOOR_BYTES = 2 * 1024**3
_QUOTA_CEIL_BYTES = 100 * 1024**3
_QUOTA_FALLBACK_BYTES = 10 * 1024**3


def auto_transcode_concurrency(*, hardware: bool, cores: int | None = None) -> int:
    """同时转码的会话上限。

    - 有硬件加速：编码在 GPU 上，定值 3——消费级显卡（NVENC 会话上限、
      核显 QSV 实测）同时 3 路 1080p 是普遍安全值，再多所有会话一起掉出
      实时速度，不如把后来者拒之门外说清楚。
    - 纯软件：每路 1080p superfast 软转约吃 4 个核心才能保住实时，按核数
      折算并封顶 4——软转再多路也会把搜索、扫描、订阅这些后台任务挤死。
    """
    if hardware:
        return 3
    n = cores if cores is not None else (os.cpu_count() or 4)
    return max(1, min(4, n // 4))


def auto_quota_bytes(cache_root: Path) -> int:
    """转码分片缓存的占盘上限。

    分片与数据库同在 data/ 卷上，盘满会让 SQLite 写不进去、整个应用不可用。
    取当前剩余空间的四分之一（钳在 2–100 GB）：盘越紧张配额越收缩，与会话
    层的最低水位检查、SIGSTOP 急停共同构成三道防线。查不到磁盘信息时回退
    10 GB（原默认值），宁可保守也不放开。
    """
    try:
        free = shutil.disk_usage(cache_root).free
    except OSError:
        return _QUOTA_FALLBACK_BYTES
    return max(_QUOTA_FLOOR_BYTES, min(_QUOTA_CEIL_BYTES, free // 4))
