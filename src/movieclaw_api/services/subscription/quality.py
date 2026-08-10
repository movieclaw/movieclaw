"""订阅连续性与洗版策略的确定性规则。

策略配置和少量运行态统一存进 ``subscription.quality_policy``：基础规则组仍
负责先拿到可接受版本；洗版目标只保留能从种子标题结构化属性稳定判断的维度。
本模块不做文件探测，候选是否达标完全以 ``TorrentAttrs`` 为准。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from movieclaw_db.models import WantedItem, WantedStatus
from movieclaw_matcher import RuleSetSpec, RuleVerdict, TorrentCandidate, evaluate_rules

LOCK_FIRST = "lock_first"
UPGRADE = "upgrade"
QUALITY_MODES = frozenset({LOCK_FIRST, UPGRADE})

_PROFILE_FIELDS = (
    "resolutions",
    "video_codecs",
    "platforms_allow",
    "platforms_block",
    "release_groups_allow",
    "release_groups_block",
    "source_match_mode",
    "hdr",
    "dv",
)


def target_profile_from_rule(spec: RuleSetSpec) -> dict[str, Any]:
    """规则组转洗版目标；分辨率列表的第一项是明确终点，其余只是基础偏好。"""
    raw = spec.model_dump(exclude_defaults=True, mode="json")
    profile = {key: raw[key] for key in _PROFILE_FIELDS if key in raw}
    if spec.resolutions:
        profile["resolutions"] = [spec.resolutions[0]]
    return RuleSetSpec.model_validate(profile).model_dump(exclude_defaults=True, mode="json")


def candidate_profile(candidate: TorrentCandidate) -> dict[str, Any]:
    """候选标题属性转成后续资源可复用的精确来源/画质约束。"""
    attrs = candidate.attrs
    profile: dict[str, Any] = {}
    if attrs.resolution:
        profile["resolutions"] = [attrs.resolution]
    if attrs.video_codec:
        profile["video_codecs"] = [attrs.video_codec]
    if attrs.platforms:
        profile["platforms_allow"] = list(attrs.platforms)
    if attrs.release_group:
        profile["release_groups_allow"] = [attrs.release_group]
    if attrs.platforms and attrs.release_group:
        profile["source_match_mode"] = "all"

    hdr_values = {value.casefold() for value in attrs.hdr}
    if hdr_values:
        profile["hdr"] = "require"
        profile["dv"] = "require" if "dv" in hdr_values else "forbid"
    else:
        profile["hdr"] = "forbid"  # 标题未标 HDR/DV，按现有命名约定锁定 SDR
    return RuleSetSpec.model_validate(profile).model_dump(exclude_defaults=True, mode="json")


def profile_verdict(candidate: TorrentCandidate, profile: object) -> RuleVerdict:
    """候选是否满足一个持久质量档案；坏档案显式拒绝而不是降级为不限。"""
    if not isinstance(profile, dict) or not profile:
        return RuleVerdict(
            accepted=False,
            reason_code="quality_profile_invalid",
            reason_text="订阅的固定版本条件为空或格式不正确",
        )
    try:
        spec = RuleSetSpec.model_validate(profile)
    except ValueError as exc:
        return RuleVerdict(
            accepted=False,
            reason_code="quality_profile_invalid",
            reason_text=f"订阅的固定版本条件无法解析：{exc}",
        )
    return evaluate_rules(candidate, spec)


def normalized_policy(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("mode") not in QUALITY_MODES:
        return None
    return deepcopy(value)


def public_policy(value: object) -> dict[str, Any] | None:
    """返回可公开的策略状态，投递候选 pending 仅供服务端对账使用。"""
    policy = normalized_policy(value)
    if policy is not None:
        policy.pop("pending", None)
    return policy


def pending_key(wanted: WantedItem) -> str:
    return f"{wanted.season_number}:{wanted.episode_number}"


def record_pending_candidate(
    policy_value: object,
    wanted_rows: list[WantedItem],
    candidate: TorrentCandidate,
) -> dict[str, Any] | None:
    """记录已投递候选的标题档案，供入库对账决定锁版或继续洗版。"""
    policy = normalized_policy(policy_value)
    if policy is None:
        return None
    profile = candidate_profile(candidate)
    target = policy.get("target")
    meets_target = bool(target) and profile_verdict(candidate, target).accepted
    pending = dict(policy.get("pending") or {})
    for wanted in wanted_rows:
        pending[pending_key(wanted)] = {
            "profile": profile,
            "meets_target": meets_target,
            "was_upgrading": wanted.status == WantedStatus.UPGRADING,
        }
    policy["pending"] = pending
    return policy


def pop_pending(
    policy_value: object, wanted: WantedItem
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    policy = normalized_policy(policy_value)
    if policy is None:
        return None, None
    pending = dict(policy.get("pending") or {})
    entry = pending.pop(pending_key(wanted), None)
    if pending:
        policy["pending"] = pending
    else:
        policy.pop("pending", None)
    return policy, entry if isinstance(entry, dict) else None


def retry_status(policy_value: object, wanted: WantedItem) -> WantedStatus:
    """下载失败时恢复投递前状态，洗版工单不能退化成普通缺集。"""
    policy = normalized_policy(policy_value)
    if policy is None:
        return WantedStatus.WANTED
    entry = (policy.get("pending") or {}).get(pending_key(wanted), {})
    return (
        WantedStatus.UPGRADING
        if isinstance(entry, dict) and entry.get("was_upgrading") is True
        else WantedStatus.WANTED
    )


def profile_summary(profile: object) -> str:
    if not isinstance(profile, dict):
        return "指定版本"
    parts: list[str] = []
    if profile.get("resolutions"):
        parts.append(str(profile["resolutions"][0]))
    hdr = profile.get("hdr")
    dv = profile.get("dv")
    if dv == "require":
        parts.append("DV")
    elif hdr == "require":
        parts.append("HDR（排除 DV）" if dv == "forbid" else "HDR")
    elif hdr == "forbid":
        parts.append("SDR")
    if profile.get("platforms_allow"):
        parts.append("/".join(profile["platforms_allow"]))
    if profile.get("release_groups_allow"):
        parts.append("/".join(profile["release_groups_allow"]))
    return " · ".join(parts) or "指定版本"
