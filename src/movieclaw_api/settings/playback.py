"""网页播放器策略配置域（docs/design/web-player.md §3.6 / §4.5 / §4.6）。

对应「设置 → 播放」页。这里只声明与持久化策略，真正决定档位的判定逻辑在
协议无关的 ``movieclaw_playback.decide``（纯函数），执行转码的在会话层——
三者分开，是为了让判定逻辑能被表驱动单测完整覆盖（转码没法在 CI 跑真硬件）。
"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting


@register_setting(namespace="playback.policy", title="播放")
class PlaybackPolicySetting(SettingSchema):
    """网页播放器的服务端策略。全部字段都有默认值——从未配置过也能直接播。"""

    software_transcode_enabled: bool = Field(
        default=False,
        description=(
            "是否允许软件转码（无可用硬件加速时的兜底）。默认关闭：低配 NAS 上"
            "一路 1080p 软转就能吃满 CPU，连带拖慢搜索、扫描、订阅——用户感知到的"
            "是「整个应用变卡」，却不会联想到是自己点了播放。需要时由播放页弹窗"
            "询问并在此永久保存。"
        ),
    )
    max_transcode_concurrency: int = Field(
        default=2,
        ge=1,
        le=8,
        description="同时进行的转码会话上限（按 CPU/GPU 算）。",
    )
    max_remux_concurrency: int = Field(
        default=4,
        ge=1,
        le=16,
        description=(
            "同时进行的直通（remux/音频单转）会话上限。与转码分开计数：机械盘"
            "上两路 4K remux 就能打满随机读，而 CPU 几乎是空的——瓶颈在 IO 不在算力。"
        ),
    )
    max_transcode_height: int = Field(
        default=1080,
        ge=480,
        le=2160,
        description="转码输出的高度上限，超出则降分辨率。",
    )
    transcode_cache_quota_gb: int = Field(
        default=10,
        ge=1,
        le=500,
        description=(
            "转码分片缓存的占盘上限（GB）。分片与数据库同在 data/ 卷上，盘满会"
            "让 SQLite 写不进去、整个应用不可用，因此写入前会先检查剩余空间。"
        ),
    )
