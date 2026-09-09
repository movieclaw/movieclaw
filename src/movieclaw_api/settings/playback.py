"""网页播放器策略配置域（docs/design/web-player.md §3.6 / §4.5 / §4.6）。

这里只声明与持久化策略，真正决定档位的判定逻辑在协议无关的
``movieclaw_playback.decide``（纯函数），执行转码的在会话层——三者分开，
是为了让判定逻辑能被表驱动单测完整覆盖（转码没法在 CI 跑真硬件）。

原「设置 → 播放」页的四个数字上限（转码/直通并发、输出高度、缓存配额）
已于 2026-08-25 撤下，改为按机器规格自动推导（services/playback/limits.py）：
用户不知道怎么设，设错的代价却由整个应用承担。这里只放「要不要接受某种
代价」的意愿开关——机器替用户答不了的那种。
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
            "询问并永久保存。"
        ),
    )
    trickplay_enabled: bool = Field(
        default=True,
        description=(
            "是否为影片生成进度条预览缩略图。生成要通读整部影片抽帧（4 倍速"
            "限速下两小时片约 30 分钟），低配设备上会有可感知的 CPU 与磁盘"
            "占用；关闭后不再生成新预览，已生成的照常可用，重新打开即恢复。"
        ),
    )
