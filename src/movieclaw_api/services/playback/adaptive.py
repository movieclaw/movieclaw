"""按实测线路带宽收紧转码目标码率（docs/design/player-pipeline-optimization.md §C）。

单变体 HLS 没有 ABR：master 列表只有一路，播放器不能逐片换码率。外网带宽不够
时它只会停下来等，45 秒后被判「供流中断」走降档——而降档降的是**编码档**，
解决不了带宽问题。多变体等于多路转码，NAS 承担不起；所以在单变体的前提下
能做的最大努力是：**开会话（或重开会话）时就按线路选对码率**。

判定是纯函数，输入只有两样：决策引擎给出的计划、前端量到的下行速度
（``bandwidth.ts`` 的传输期口径，Resource Timing 记时）。

三条规则：

1. **只对转码视频生效**。直通档（``-c:v copy``）的码率改不了，那条路由前端
   提示用户手动换画质。
2. **码率与高度一起降**。线路只够 2 Mbps 时把 1080p 的 maxrate 压到 2M 是
   糊的，降到 720p 反而清楚。但也不能线路一紧就降高度：1080p 给 5 Mbps 仍
   比 720p 给 3 Mbps 清楚。所以目标高度的阶梯值打七五折仍装得下就**只压码率
   不动高度**；装不下才在阶梯里找线路装得下的最高一档。
3. **上限量化到 250 kbps**。转码缓存按计划指纹复用（§B），码率进指纹；
   不量化的话每次实测差几 kbps 就是一份新缓存，命中率归零。
"""

from __future__ import annotations

from dataclasses import replace

from movieclaw_playback.decide import PlaybackDecision, PlaybackPlan

#: 线路余量：实测下行的八成给视频，剩下的留给音频、分片边界抖动与并发请求。
DOWNLINK_SAFETY = 0.8
#: 目标高度的阶梯值打这个折扣仍装得下，就保住高度只压码率；装不下才降高度。
KEEP_HEIGHT_RATIO = 0.75
#: 再差的线路也不把码率压到这以下——再低画面已经没有信息量，不如直接等。
MIN_CAP_BPS = 400_000
#: 码率上限的量化步长（bps）。
CAP_STEP_BPS = 250_000
#: 分辨率阶梯（高度 → 该档「够清晰又不虚胖」的码率），与 ffmpeg_args 的
#: BITRATE_LADDER 同一组数，这里用 bps 表示便于比较。
LADDER_BPS: dict[int, int] = {
    2160: 16_000_000,
    1440: 10_000_000,
    1080: 6_000_000,
    720: 3_000_000,
    480: 1_500_000,
}


def quantize_cap(cap_bps: int) -> int:
    """把码率上限量化到步长的整数倍（向下取），并钳在 MIN_CAP_BPS 之上。"""
    return max(MIN_CAP_BPS, (cap_bps // CAP_STEP_BPS) * CAP_STEP_BPS)


def adapt_to_downlink(
    decision: PlaybackDecision, downlink_bps: int | None
) -> PlaybackDecision:
    """给转码计划套上线路能装下的码率上限，必要时连高度一起降。

    非计划（consent / rejected）、直通视频、没有带宽读数：原样返回。
    """
    if not isinstance(decision, PlaybackPlan):
        return decision
    if downlink_bps is None or downlink_bps <= 0:
        return decision
    video = decision.video
    if video.action != "transcode":
        return decision

    budget = int(downlink_bps * DOWNLINK_SAFETY)
    target_height = video.height
    height = target_height
    if target_height is not None:
        # 目标高度对应的阶梯档：向上取整，与 ffmpeg_args.maxrate_for_height 同一规则
        steps = sorted(LADDER_BPS)
        top = next((h for h in steps if target_height <= h), steps[-1])
        if budget >= LADDER_BPS[top] * KEEP_HEIGHT_RATIO:
            height = target_height
            ladder = LADDER_BPS[top]
        else:
            # 往下找线路装得下的最高一档；一档都装不下就取最低档，再由下面的
            # 上限把码率压下去
            lower = [h for h in steps if h < top and LADDER_BPS[h] <= budget]
            height = max(lower) if lower else steps[0]
            ladder = LADDER_BPS[height]
    else:
        ladder = LADDER_BPS[1080]
    cap = quantize_cap(min(ladder, budget))
    if cap >= ladder and height == target_height:
        # 线路吃得下阶梯值：不设上限，与没有读数时的行为完全一致
        return decision

    mbps = downlink_bps / 1_000_000
    note = f"；实测线路约 {mbps:.1f} Mbps，码率限到 {cap / 1_000_000:.2f} Mbps"
    if height != target_height:
        note += f"，画质降到 {height}p"
    return replace(
        decision,
        video=replace(video, height=height, bitrate_cap_bps=cap),
        reason=decision.reason + note,
    )
