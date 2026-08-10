"""规则过滤与评分的表驱动测试：每个维度覆盖 接受/拒绝/未知 三态。"""

from __future__ import annotations

import pytest

from movieclaw_enrich import enrich
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import RuleSetSpec, TorrentCandidate, evaluate_rules


def _candidate(**kwargs) -> TorrentCandidate:
    attr_fields = TorrentAttrs.model_fields.keys()
    attrs = {k: v for k, v in kwargs.items() if k in attr_fields}
    rest = {k: v for k, v in kwargs.items() if k not in attr_fields}
    return TorrentCandidate(
        site_id="test", torrent_id="1", title="t", subtitle="", attrs=TorrentAttrs(**attrs), **rest
    )


# ---------------------------------------------------------------------------
# 硬性过滤：接受 / 拒绝 / 未知三态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate_kwargs", "spec_kwargs", "expect_accept", "expect_code"),
    [
        # 分辨率
        ({"resolution": "2160p"}, {"resolutions": ["2160p", "1080p"]}, True, None),
        (
            {"resolution": "720p"},
            {"resolutions": ["2160p", "1080p"]},
            False,
            "resolution_not_allowed",
        ),
        ({}, {"resolutions": ["1080p"]}, False, "resolution_unknown"),
        ({"resolution": "720p"}, {}, True, None),  # 不限时未知/任意都放行
        # 编码（大小写不敏感）
        ({"video_codec": "H.265"}, {"video_codecs": ["h.265"]}, True, None),
        ({"video_codec": "H.264"}, {"video_codecs": ["H.265"]}, False, "codec_not_allowed"),
        # 制作组黑白名单
        ({"release_group": "CHD"}, {"release_groups_block": ["chd"]}, False, "group_blocked"),
        ({"release_group": "OurTV"}, {"release_groups_allow": ["OurTV"]}, True, None),
        (
            {"release_group": "Other"},
            {"release_groups_allow": ["OurTV"]},
            False,
            "group_not_allowed",
        ),
        ({}, {"release_groups_allow": ["OurTV"]}, False, "group_unknown"),
        # HDR/DV 分开判断；必须 HDR 明确拒绝纯 DV 与 HDR+DV 双标资源
        ({"hdr": ["HDR10"]}, {"hdr": "require"}, True, None),
        ({"hdr": ["DV"]}, {"hdr": "require"}, False, "dv_not_allowed_for_hdr"),
        (
            {"hdr": ["HDR10", "DV"]},
            {"hdr": "require"},
            False,
            "dv_not_allowed_for_hdr",
        ),
        ({}, {"hdr": "require"}, False, "hdr_required"),
        ({"hdr": ["DV"]}, {"hdr": "require_dv"}, True, None),
        ({"hdr": ["HDR10", "DV"]}, {"hdr": "require_dv"}, True, None),
        ({"hdr": ["HDR10"]}, {"hdr": "require_dv"}, False, "dv_required"),
        ({"hdr": ["HDR10"]}, {"hdr": "forbid"}, False, "hdr_forbidden"),
        ({"hdr": ["HDR10", "DV"]}, {"hdr": "forbid"}, False, "hdr_forbidden"),
        ({"hdr": ["DV"]}, {"hdr": "forbid"}, True, None),
        ({}, {"hdr": "forbid"}, True, None),
        ({"hdr": ["DV"]}, {"hdr": "forbid_dv"}, False, "dv_forbidden"),
        ({"hdr": ["HDR10", "DV"]}, {"hdr": "forbid_dv"}, False, "dv_forbidden"),
        ({"hdr": ["HDR10"]}, {"hdr": "forbid_dv"}, True, None),
        ({"hdr": ["HDR10", "DV"]}, {"hdr": "any"}, True, None),
        # 仅免费：None=未知按非免费
        ({"is_free": True}, {"free_only": True}, True, None),
        ({"is_free": False}, {"free_only": True}, False, "not_free"),
        ({"is_free": None}, {"free_only": True}, False, "not_free"),
        # 做种下限：未知保守拒绝
        ({"seeders": 10}, {"min_seeders": 5}, True, None),
        ({"seeders": 3}, {"min_seeders": 5}, False, "seeders_too_few"),
        ({"seeders": None}, {"min_seeders": 5}, False, "seeders_unknown"),
        # H&R 三态
        ({"hit_and_run": True}, {"exclude_hr": True}, False, "hit_and_run"),
        ({"hit_and_run": False}, {"exclude_hr": True}, True, None),
        ({"hit_and_run": None}, {"exclude_hr": True}, True, None),  # 默认 lenient
        (
            {"hit_and_run": None},
            {"exclude_hr": True, "hr_unknown_policy": "strict"},
            False,
            "hit_and_run_unknown",
        ),
    ],
)
def test_filter_dimensions(candidate_kwargs, spec_kwargs, expect_accept, expect_code) -> None:
    verdict = evaluate_rules(
        _candidate(**candidate_kwargs), RuleSetSpec.model_validate(spec_kwargs)
    )
    assert verdict.accepted is expect_accept
    assert verdict.reason_code == expect_code
    if not expect_accept:
        assert verdict.reason_text  # 拒绝必须带可读中文原因


def test_size_amortized_for_season_pack() -> None:
    """体积区间按每集均摊：40GB 的 40 集合集 = 单集 1GB，不被单集上限误杀。"""
    spec = RuleSetSpec(size_min_mb=500, size_max_mb=2000)
    pack = _candidate(size_bytes=40 * 1024**3)

    assert evaluate_rules(pack, spec, pack_episode_count=40).accepted is True
    single = evaluate_rules(pack, spec, pack_episode_count=1)
    assert single.accepted is False
    assert single.reason_code == "size_too_large"


def test_size_unknown_rejected_when_bounds_set() -> None:
    verdict = evaluate_rules(_candidate(), RuleSetSpec(size_max_mb=2000))
    assert verdict.accepted is False
    assert verdict.reason_code == "size_unknown"


def test_require_hdr_rejects_real_title_containing_hdr_and_dv() -> None:
    """真实资源名里的 HDR+DV 双标不能被“必须 HDR”误收。"""
    title = "Movie.2026.2160p.WEB-DL.HDR.DV.HEVC"
    candidate = TorrentCandidate(
        site_id="test",
        torrent_id="hdr-dv",
        title=title,
        subtitle="",
        attrs=enrich(title),
    )

    assert set(candidate.attrs.hdr) == {"HDR", "DV"}
    verdict = evaluate_rules(candidate, RuleSetSpec(hdr="require"))
    assert verdict.accepted is False
    assert verdict.reason_code == "dv_not_allowed_for_hdr"


# ---------------------------------------------------------------------------
# 评分：免费 > 做种（封顶） > 分辨率偏好
# ---------------------------------------------------------------------------


def test_score_free_beats_seeders() -> None:
    spec = RuleSetSpec()
    free_few_seeders = evaluate_rules(_candidate(is_free=True, seeders=2), spec)
    paid_many_seeders = evaluate_rules(_candidate(is_free=False, seeders=500), spec)
    assert free_few_seeders.score > paid_many_seeders.score  # 做种计分封顶生效


def test_score_resolution_preference_follows_spec_order() -> None:
    """resolutions 列表顺序即偏好：排在前面的分辨率评分更高。"""
    spec = RuleSetSpec(resolutions=["1080p", "2160p"])  # 刻意 1080p 优先
    v1080 = evaluate_rules(_candidate(resolution="1080p"), spec)
    v2160 = evaluate_rules(_candidate(resolution="2160p"), spec)
    assert v1080.score > v2160.score
