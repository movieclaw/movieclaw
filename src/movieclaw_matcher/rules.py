"""规则过滤与评分（内核第二级）：这个载体可不可接受、多个可接受的谁更好。

三态处理原则（与 site_torrent 的三态铁律呼应）：**未知永远不当已知用**。
- 有明确要求的维度上，"未知"按保守方向处理（分辨率未知 + 要求 1080p → 拒），
  拒绝原因如实说"无法识别"而非假装它不达标；
- H&R 未知按规则组的显式策略（lenient/strict）处理，绝不静默塌缩成 False。

评分公式第一版内置不暴露权重（已确认决策）：免费 > 做种数（封顶）> 分辨率偏好。
"""

from __future__ import annotations

from movieclaw_matcher.models import (
    HdrPolicy,
    HrUnknownPolicy,
    RuleSetSpec,
    RuleVerdict,
    TorrentCandidate,
)

# 未配置分辨率偏好时的内置默认（高清优先，权重温和）
_DEFAULT_RESOLUTION_SCORE = {"2160p": 30, "1080p": 20, "720p": 10}

_FREE_SCORE = 100  # 免费是 PT 场景的第一偏好
_SEEDERS_CAP = 50  # 做种数计分封顶，防止爆种老资源碾压一切偏好


def evaluate_rules(
    candidate: TorrentCandidate, spec: RuleSetSpec, *, pack_episode_count: int = 1
) -> RuleVerdict:
    """按规则组评估候选。第一条不通过即返回（拒绝原因单一明确）。

    ``pack_episode_count``：整季包的体积按每集均摊评估，避免 40 集合集
    被"单集体积上限"误杀。
    """
    attrs = candidate.attrs

    if spec.resolutions:
        allowed = {r.casefold() for r in spec.resolutions}
        if attrs.resolution is None:
            return _reject("resolution_unknown", "无法识别分辨率，规则要求明确分辨率时按不合格处理")
        if attrs.resolution.casefold() not in allowed:
            return _reject(
                "resolution_not_allowed",
                f"分辨率 {attrs.resolution} 不在允许范围（{'/'.join(spec.resolutions)}）",
            )

    if spec.video_codecs:
        allowed = {c.casefold() for c in spec.video_codecs}
        if attrs.video_codec is None:
            return _reject("codec_unknown", "无法识别视频编码，规则要求明确编码时按不合格处理")
        if attrs.video_codec.casefold() not in allowed:
            return _reject(
                "codec_not_allowed",
                f"视频编码 {attrs.video_codec} 不在允许范围（{'/'.join(spec.video_codecs)}）",
            )

    group = (attrs.release_group or "").casefold()
    if spec.release_groups_block and group in {g.casefold() for g in spec.release_groups_block}:
        return _reject("group_blocked", f"制作组 {attrs.release_group} 在黑名单中")
    if spec.release_groups_allow:
        if not group:
            return _reject("group_unknown", "无法识别制作组，规则设置了白名单时按不合格处理")
        if group not in {g.casefold() for g in spec.release_groups_allow}:
            return _reject("group_not_allowed", f"制作组 {attrs.release_group} 不在白名单中")

    # HDR 与 DV 必须分开判断。发布名常见 ``DV.HDR`` 双标记，若只判断 hdr
    # 列表非空，“必须 HDR”会误收 DV 版本。这里把 DV 单独剥离：必须 HDR 时
    # 先拒绝任何含 DV 的候选，再要求至少一个非 DV HDR 标记。
    hdr_formats = {value.casefold() for value in attrs.hdr}
    has_dv = "dv" in hdr_formats
    has_hdr = bool(hdr_formats - {"dv"})
    if spec.hdr is HdrPolicy.REQUIRE:
        if has_dv:
            return _reject("dv_not_allowed_for_hdr", "规则要求 HDR（不含 DV），该资源带有 DV 标记")
        if not has_hdr:
            return _reject("hdr_required", "规则要求 HDR，该资源未标注 HDR 格式")
    if spec.hdr is HdrPolicy.REQUIRE_DV and not has_dv:
        return _reject("dv_required", "规则要求 DV，该资源未标注 DV")
    if spec.hdr is HdrPolicy.FORBID and has_hdr:
        return _reject("hdr_forbidden", f"规则排除 HDR，该资源标注了 {'/'.join(attrs.hdr)}")
    if spec.hdr is HdrPolicy.FORBID_DV and has_dv:
        return _reject("dv_forbidden", f"规则排除 DV，该资源标注了 {'/'.join(attrs.hdr)}")

    if spec.free_only and candidate.is_free is not True:
        state = "非免费" if candidate.is_free is False else "促销状态未知（按非免费处理）"
        return _reject("not_free", f"规则要求仅免费资源，该资源当前{state}")

    if spec.min_seeders is not None:
        if candidate.seeders is None:
            return _reject("seeders_unknown", "做种数未知，规则设置了做种下限时按不合格处理")
        if candidate.seeders < spec.min_seeders:
            return _reject(
                "seeders_too_few",
                f"做种数 {candidate.seeders} 低于下限 {spec.min_seeders}",
            )

    if spec.size_min_mb is not None or spec.size_max_mb is not None:
        if candidate.size_bytes is None:
            return _reject("size_unknown", "体积未知，规则设置了体积区间时按不合格处理")
        per_episode_mb = candidate.size_bytes / max(pack_episode_count, 1) / 1024 / 1024
        if spec.size_min_mb is not None and per_episode_mb < spec.size_min_mb:
            return _reject(
                "size_too_small",
                f"单集均摊体积 {per_episode_mb:.0f}MB 低于下限 {spec.size_min_mb}MB",
            )
        if spec.size_max_mb is not None and per_episode_mb > spec.size_max_mb:
            return _reject(
                "size_too_large",
                f"单集均摊体积 {per_episode_mb:.0f}MB 超过上限 {spec.size_max_mb}MB",
            )

    if spec.exclude_hr:
        if candidate.hit_and_run is True:
            return _reject("hit_and_run", "该资源有 H&R 考核，规则已排除考核种子")
        if candidate.hit_and_run is None and spec.hr_unknown_policy is HrUnknownPolicy.STRICT:
            return _reject(
                "hit_and_run_unknown",
                "该站点未提供 H&R 信息，规则按保守策略视作有考核而排除",
            )

    return RuleVerdict(accepted=True, score=_score(candidate, spec))


def _score(candidate: TorrentCandidate, spec: RuleSetSpec) -> int:
    """内置评分：免费 > 做种数（封顶） > 分辨率偏好（配置序或内置默认序）。"""
    score = 0
    if candidate.is_free is True:
        score += _FREE_SCORE
    score += min(candidate.seeders or 0, _SEEDERS_CAP)
    resolution = (candidate.attrs.resolution or "").casefold()
    if spec.resolutions:
        ordered = [r.casefold() for r in spec.resolutions]
        if resolution in ordered:
            score += (len(ordered) - ordered.index(resolution)) * 30
    else:
        score += _DEFAULT_RESOLUTION_SCORE.get(resolution, 0)
    return score


def _reject(code: str, text: str) -> RuleVerdict:
    return RuleVerdict(accepted=False, reason_code=code, reason_text=text)
