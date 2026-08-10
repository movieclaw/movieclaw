"""规则过滤与评分（内核第二级）：这个载体可不可接受、多个可接受的谁更好。

三态处理原则（与 site_torrent 的三态铁律呼应）：**未知永远不当已知用**。
- 有明确要求的维度上，"未知"按保守方向处理（分辨率未知 + 要求 1080p → 拒），
  拒绝原因如实说"无法识别"而非假装它不达标；
- H&R 未知按规则组的显式策略（lenient/strict）处理，绝不静默塌缩成 False。

评分公式第一版内置不暴露权重（已确认决策）：免费 > 做种数（封顶）> 分辨率偏好。
"""

from __future__ import annotations

from movieclaw_matcher.models import (
    DvPolicy,
    HdrPolicy,
    HrUnknownPolicy,
    RuleSetSpec,
    RuleVerdict,
    SourceMatchMode,
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

    # 来源黑名单是全局否决项，且平台与制作组严格分字段判断。制作组使用完整值
    # 匹配，HDSWEB 不会误伤 Pure@HDSWEB；反过来 Pure 也不会命中后者。
    platforms = {value.casefold() for value in attrs.platforms}
    blocked_platforms = platforms & {value.casefold() for value in spec.platforms_block}
    if blocked_platforms:
        return _reject("platform_blocked", f"流媒体平台 {sorted(blocked_platforms)[0]} 在黑名单中")

    group = (attrs.release_group or "").casefold()
    if spec.release_groups_block and group in {g.casefold() for g in spec.release_groups_block}:
        return _reject("group_blocked", f"制作组 {attrs.release_group} 在黑名单中")

    platform_match = bool(
        platforms & {value.casefold() for value in spec.platforms_allow}
    )
    group_match = bool(
        group and group in {value.casefold() for value in spec.release_groups_allow}
    )
    source_checks = []
    if spec.platforms_allow:
        source_checks.append(platform_match)
    if spec.release_groups_allow:
        source_checks.append(group_match)
    if source_checks:
        accepted_source = (
            all(source_checks)
            if spec.source_match_mode is SourceMatchMode.ALL
            else any(source_checks)
        )
        if not accepted_source:
            if spec.platforms_allow and not spec.release_groups_allow:
                if not platforms:
                    return _reject(
                        "platform_unknown",
                        "无法识别流媒体平台，规则设置了白名单时按不合格处理",
                    )
                return _reject("platform_not_allowed", "流媒体平台不在白名单中")
            if spec.release_groups_allow and not spec.platforms_allow:
                if not group:
                    return _reject(
                        "group_unknown",
                        "无法识别制作组，规则设置了白名单时按不合格处理",
                    )
                return _reject("group_not_allowed", f"制作组 {attrs.release_group} 不在白名单中")
            relation = "且" if spec.source_match_mode is SourceMatchMode.ALL else "或"
            return _reject(
                "source_not_allowed",
                f"资源未满足平台{relation}制作组白名单条件",
            )

    # hdr 空列表 = 未提取到 HDR 标记；命名惯例里 HDR 属"缺席即否定"的强标记，
    # 因此 require 时空列表按"非 HDR"拒绝，forbid 时空列表放行。
    # hdr 轴判断整个 HDR 家族（含 DV），dv 轴单独判断杜比视界，两轴正交：
    # 如"必须 HDR 但不要 DV"= hdr=require + dv=forbid，组合语义自然叠加。
    if spec.hdr is HdrPolicy.REQUIRE and not attrs.hdr:
        return _reject("hdr_required", "规则要求 HDR，该资源未标注任何 HDR 格式")
    if spec.hdr is HdrPolicy.FORBID and attrs.hdr:
        return _reject("hdr_forbidden", f"规则排除 HDR，该资源标注了 {'/'.join(attrs.hdr)}")

    # 词表已把 DoVi/Dolby Vision 等写法归一化为 "DV"，直接查成员即可
    has_dv = "DV" in attrs.hdr
    if spec.dv is DvPolicy.REQUIRE and not has_dv:
        return _reject("dv_required", "规则要求杜比视界（DV），该资源未标注 DV")
    if spec.dv is DvPolicy.FORBID and has_dv:
        return _reject("dv_forbidden", "规则排除杜比视界（DV），该资源标注了 DV")

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
