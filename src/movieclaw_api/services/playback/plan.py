"""播放决策的服务胶水（docs/design/web-player.md §3）。

职责严格限定为「取数 + 组装 + 翻译」：装配决策输入、调用协议无关的判定引擎、
把结果翻成响应模型。**任何档位判断都不许写在这里**——判定一旦散进服务层，
就没法用表驱动单测覆盖了（转码没法在 CI 跑真硬件，判定与执行分离是唯一支点）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.media_probe import probe_keyframe_interval
from movieclaw_api.services.playback.hwprobe import hardware_available
from movieclaw_api.services.playback.limits import MAX_TRANSCODE_HEIGHT
from movieclaw_api.settings import PlaybackPolicySetting
from movieclaw_api.settings.store import get_setting_store
from movieclaw_db.models import FileState, LibraryFile
from movieclaw_playback.capability import ClientCapability
from movieclaw_playback.decide import (
    ConsentRequired,
    PlaybackDecision,
    PlaybackPlan,
    PlaybackPolicy,
    decide_playback,
    needs_keyframe_probe,
)
from movieclaw_playback.decide import PlaybackTier as Tier
from movieclaw_playback.profile import media_profile_from_file

if TYPE_CHECKING:  # 仅类型标注需要，运行时不导入（避免 schemas ↔ services 循环）
    from movieclaw_api.schemas.playback import PlaybackDecisionView


logger = logging.getLogger("movieclaw_api.playback.plan")


async def decide_for_file(
    session: AsyncSession,
    file_id: int,
    capability: ClientCapability,
    *,
    can_self_enable: bool,
    failed_tiers: frozenset[Tier] = frozenset(),
    preferred_audio: str | None = None,
    preferred_subtitle: str | None = None,
    max_height: int | None = None,
    visible_library_ids: set[int] | None = None,
) -> PlaybackDecision | None:
    """对一个台账文件出播放决策；文件不存在或当前账号不可见时返回 None。

    ``visible_library_ids`` 为 None 表示不限制（管理员）——与库列表接口同一约定。
    """
    file = await session.get(LibraryFile, file_id)
    if file is None:
        return None
    if visible_library_ids is not None and file.library_id not in visible_library_ids:
        return None
    return await decide_for_files(
        [file],
        capability,
        can_self_enable=can_self_enable,
        failed_tiers=failed_tiers,
        preferred_audio=preferred_audio,
        preferred_subtitle=preferred_subtitle,
        max_height=max_height,
    )


async def decide_for_files(
    files: list[LibraryFile],
    capability: ClientCapability,
    *,
    can_self_enable: bool,
    failed_tiers: frozenset[Tier] = frozenset(),
    preferred_audio: str | None = None,
    preferred_subtitle: str | None = None,
    max_height: int | None = None,
) -> PlaybackDecision | None:
    """多候选择优（§3.5）：同一条目常有 1080p/2160p 两个版本，能直通的
    1080p 胜过要转码的 2160p。

    接口形状是「候选集合 → 最优计划」而不是逐文件判档——形状定死在这里，
    调用方以后加多版本选择不用改签名。
    """
    if not files:
        return None
    policy = await _load_policy()
    decisions = []
    for file in files:
        # 关键帧密度只服务于档 1/2 的视频复制。先用不需要源片 IO 的纯判定
        # 判断候选是否可能落在这两个档位；iOS 4K 硬转、软件转码和 Direct
        # Play 都直接跳过，避免为最终不会使用的安全信息等待慢存储。
        profile = media_profile_from_file(file)
        interval = None
        if needs_keyframe_probe(
            profile,
            capability,
            policy,
            failed_tiers=failed_tiers,
            preferred_audio=preferred_audio,
            preferred_subtitle=preferred_subtitle,
            max_height=max_height,
        ):
            probe_started = time.perf_counter()
            interval = await asyncio.to_thread(
                probe_keyframe_interval, file.file_path, file.duration_seconds
            )
            probe_s = time.perf_counter() - probe_started
            if probe_s > 1.0:
                # 慢的几乎都是存储：三段采样约 90 秒码流的读取，网络挂载上会到
                # 几十秒。条目详情页的起播预热（warmup.py）本该把它提前做掉——
                # 这条日志出现说明预热没盖住（剧集多文件 / 分享直达播放页）。
                logger.warning(
                    "关键帧采样耗时 %.1f 秒（%s）——存储较慢，首播延迟主要来自这里",
                    probe_s, file.file_path,
                )
            profile = media_profile_from_file(file, keyframe_interval_s=interval)
        decisions.append(
            (
                decide_playback(
                    profile,
                    capability,
                    policy,
                    can_self_enable=can_self_enable,
                    failed_tiers=failed_tiers,
                    preferred_audio=preferred_audio,
                    preferred_subtitle=preferred_subtitle,
                    max_height=max_height,
                ),
                profile.height,
            )
        )
    return _best(decisions)


async def _load_policy() -> PlaybackPolicy:
    """把持久化配置翻成引擎输入。

    ``hardware_available`` 不来自配置而来自实测——用户改不了自己有没有显卡，
    把它做成开关只会让人误配。转码高度上限不再是配置项，按最佳定值取
    （limits.py，独立播放设置页已于 2026-08-25 撤下）。
    """
    stored = await get_setting_store().get(PlaybackPolicySetting)
    return PlaybackPolicy(
        software_transcode_enabled=stored.software_transcode_enabled,
        hardware_available=await asyncio.to_thread(hardware_available),
        max_transcode_height=MAX_TRANSCODE_HEIGHT,
    )


def _best(decisions: list[tuple[PlaybackDecision, int | None]]) -> PlaybackDecision:
    """择优：能出计划的按档位取最小，**同档位取更高分辨率**；全都出不了计划
    时，优先把「要用户同意」这种可挽救的结果交给前端，而不是直接报拒绝。

    同档位决胜为什么必须有：直通判定放开后（§12.15），1080p 与 2160p 版本
    经常**同时**落在档 0/1，此时播哪个取决于数据库返回顺序——同一部片今天
    4K 明天 1080p。既然同档位代价相同（都不重编码），当然给用户画质最好的
    那份；高度未知的候选排最后（None 当 0），至少保证结果确定。"""
    plans = [(d, height) for d, height in decisions if isinstance(d, PlaybackPlan)]
    if plans:
        return min(plans, key=lambda pair: (pair[0].tier, -(pair[1] or 0)))[0]
    consents = [d for d, _ in decisions if isinstance(d, ConsentRequired)]
    return consents[0] if consents else decisions[0][0]


async def library_files_for_unit(
    session: AsyncSession,
    media_item_id: int,
    season_number: int,
    episode_number: int,
    *,
    visible_library_ids: set[int] | None = None,
) -> list[LibraryFile]:
    """取一个播放单元（电影或某一集）在位的全部版本文件，供多候选择优。"""
    stmt = select(LibraryFile).where(
        LibraryFile.media_item_id == media_item_id,
        LibraryFile.season_number == season_number,
        LibraryFile.episode_number == episode_number,
        LibraryFile.state == FileState.IN_PLACE,
    )
    if visible_library_ids is not None:
        stmt = stmt.where(LibraryFile.library_id.in_(visible_library_ids))
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# 领域结果 → 响应模型
# ---------------------------------------------------------------------------


def to_view(decision: PlaybackDecision) -> PlaybackDecisionView:
    """把三态决策翻成对外响应模型。

    翻译放在服务层而不是领域层：``movieclaw_playback`` 要保持协议无关，
    Jellyfin 兼容层与网页端各自翻各自的（与观看状态、字幕轨同一分层原则）。
    """
    from movieclaw_api.schemas.playback import (  # 局部导入避免 schemas ↔ services 循环
        AudioPlanView,
        AudioTrackView,
        PlaybackDecisionView,
        SubtitlePlanView,
        VideoPlanView,
    )

    if isinstance(decision, PlaybackPlan):
        return PlaybackDecisionView(
            outcome="plan",
            tier=int(decision.tier),
            file_id=decision.file_id,
            container=decision.container,
            video=VideoPlanView(**vars(decision.video)),
            audio=AudioPlanView(**vars(decision.audio)),
            audio_tracks=[AudioTrackView(**vars(t)) for t in decision.audio_tracks],
            subtitles=[SubtitlePlanView(**vars(s)) for s in decision.subtitles],
            degraded_from=(
                int(decision.degraded_from) if decision.degraded_from is not None else None
            ),
            reason=decision.reason,
        )
    if isinstance(decision, ConsentRequired):
        return PlaybackDecisionView(
            outcome="consent",
            tier=int(decision.tier),
            reason=decision.reason,
            cost_hint=decision.cost_hint,
            can_self_enable=decision.can_self_enable,
            setting_namespace=decision.setting_namespace,
            setting_key=decision.setting_key,
        )
    return PlaybackDecisionView(
        outcome="rejected", reason=decision.reason, suggestion=decision.suggestion
    )


def capability_from_request(payload) -> ClientCapability:  # noqa: ANN001
    """请求体 → 领域值对象。字段同名同义，逐项搬运即可。"""
    from movieclaw_playback.capability import AudioSupport, VideoSupport

    return ClientCapability(
        video=tuple(VideoSupport(**vars(v)) for v in payload.video),
        audio=tuple(AudioSupport(**vars(a)) for a in payload.audio),
        containers=frozenset(payload.containers),
        hdr_passthrough=payload.hdr_passthrough,
        mse=payload.mse,
        is_mobile=payload.is_mobile,
        native_hls=payload.native_hls,
    )
