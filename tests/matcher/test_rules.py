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
        # HDR：空列表=未标注（命名惯例缺席即否定）；hdr 轴判整个家族（含 DV）
        ({"hdr": ["HDR10", "DV"]}, {"hdr": "require"}, True, None),
        ({"hdr": ["DV"]}, {"hdr": "require"}, True, None),  # 纯 DV 也是 HDR 家族
        ({}, {"hdr": "require"}, False, "hdr_required"),
        ({"hdr": ["DV"]}, {"hdr": "forbid"}, False, "hdr_forbidden"),
        ({}, {"hdr": "forbid"}, True, None),
        # DV：独立轴，只判断杜比视界标记
        ({"hdr": ["DV"]}, {"dv": "require"}, True, None),
        ({"hdr": ["HDR10", "DV"]}, {"dv": "require"}, True, None),
        ({"hdr": ["HDR10"]}, {"dv": "require"}, False, "dv_required"),
        ({}, {"dv": "require"}, False, "dv_required"),
        ({"hdr": ["DV"]}, {"dv": "forbid"}, False, "dv_forbidden"),
        ({"hdr": ["HDR10", "DV"]}, {"dv": "forbid"}, False, "dv_forbidden"),
        ({"hdr": ["HDR10"]}, {"dv": "forbid"}, True, None),
        ({}, {"dv": "forbid"}, True, None),
        # 两轴叠加：必须 HDR 且排除 DV（PR #112 的原始诉求）
        ({"hdr": ["HDR10"]}, {"hdr": "require", "dv": "forbid"}, True, None),
        (
            {"hdr": ["HDR10", "DV"]},
            {"hdr": "require", "dv": "forbid"},
            False,
            "dv_forbidden",
        ),
        ({"hdr": ["DV"]}, {"hdr": "require", "dv": "forbid"}, False, "dv_forbidden"),
        ({}, {"hdr": "require", "dv": "forbid"}, False, "hdr_required"),
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


def test_spec_rejects_contradictory_hdr_dv_combo() -> None:
    """「排除 HDR」+「必须 DV」逻辑无解（DV 属 HDR 家族），校验层直接拒绝。"""
    with pytest.raises(ValueError, match="矛盾"):
        RuleSetSpec.model_validate({"hdr": "forbid", "dv": "require"})


def test_size_unknown_rejected_when_bounds_set() -> None:
    verdict = evaluate_rules(_candidate(), RuleSetSpec(size_max_mb=2000))
    assert verdict.accepted is False
    assert verdict.reason_code == "size_unknown"


def test_source_allow_any_accepts_platform_or_release_group() -> None:
    """国产剧来源白名单：无广告平台和 Pure@HDSWEB 是两个可替代入口。"""
    spec = RuleSetSpec(
        platforms_allow=["iqiyi", "wetv", "youku"],
        release_groups_allow=["Pure@HDSWEB"],
    )
    iq_title = (
        "凛冬下的罪恶.Frozen.Sins.S01E24.2026.2160p.IQ.WEB-DL."
        "H.265.DDP5.1.2Audios-HHWEB.mkv"
    )
    pure_title = (
        "逆时追捕.Reverse.Chase.S01E19.2026.2160p.WEB-DL."
        "DDP2.0.H265.HDR-Pure@HDSWEB.mkv"
    )

    assert evaluate_rules(_candidate(**enrich(iq_title).model_dump()), spec).accepted is True
    assert evaluate_rules(_candidate(**enrich(pure_title).model_dump()), spec).accepted is True

    rejected = evaluate_rules(
        _candidate(platforms=["netflix"], release_group="HHWEB"), spec
    )
    assert rejected.accepted is False
    assert rejected.reason_code == "source_not_allowed"


def test_source_blacklist_has_priority_and_release_group_match_is_exact() -> None:
    spec = RuleSetSpec(
        platforms_allow=["iqiyi"],
        release_groups_allow=["Pure@HDSWEB"],
        release_groups_block=["HDSWEB"],
    )

    blocked = evaluate_rules(
        _candidate(platforms=["iqiyi"], release_group="HDSWEB"), spec
    )
    assert blocked.accepted is False
    assert blocked.reason_code == "group_blocked"

    pure = evaluate_rules(_candidate(release_group="Pure@HDSWEB"), spec)
    assert pure.accepted is True


def test_source_allow_all_requires_platform_and_release_group() -> None:
    spec = RuleSetSpec(
        platforms_allow=["iqiyi"],
        release_groups_allow=["HHWEB"],
        source_match_mode="all",
    )
    assert evaluate_rules(
        _candidate(platforms=["iqiyi"], release_group="HHWEB"), spec
    ).accepted

    platform_only = evaluate_rules(
        _candidate(platforms=["iqiyi"], release_group="Other"), spec
    )
    assert platform_only.accepted is False
    assert platform_only.reason_code == "source_not_allowed"


def test_platform_filter_composes_with_hdr_and_dv_axes() -> None:
    spec = RuleSetSpec(
        platforms_allow=["iqiyi"],
        hdr="require",
        dv="forbid",
    )

    assert evaluate_rules(
        _candidate(platforms=["iqiyi"], hdr=["HDR10"]), spec
    ).accepted
    assert (
        evaluate_rules(
            _candidate(platforms=["iqiyi"], hdr=["HDR10", "DV"]), spec
        ).reason_code
        == "dv_forbidden"
    )
    assert (
        evaluate_rules(
            _candidate(platforms=["netflix"], hdr=["HDR10"]), spec
        ).reason_code
        == "platform_not_allowed"
    )


def test_platform_allow_and_block_single_dimension() -> None:
    allowed = RuleSetSpec(platforms_allow=["iqiyi"])
    assert evaluate_rules(_candidate(platforms=["iqiyi"]), allowed).accepted
    assert evaluate_rules(_candidate(), allowed).reason_code == "platform_unknown"
    assert (
        evaluate_rules(_candidate(platforms=["netflix"]), allowed).reason_code
        == "platform_not_allowed"
    )

    blocked = RuleSetSpec(platforms_block=["netflix"])
    assert (
        evaluate_rules(_candidate(platforms=["netflix"]), blocked).reason_code
        == "platform_blocked"
    )


@pytest.mark.parametrize(
    "spec",
    [
        {"platforms_allow": ["IQ"]},
        {"platforms_allow": ["iqiyi"], "platforms_block": ["IQIYI"]},
        {"release_groups_allow": ["HDSWEB"], "release_groups_block": ["hdsweb"]},
    ],
)
def test_source_lists_reject_unknown_or_overlapping_values(spec) -> None:
    with pytest.raises(ValueError):
        RuleSetSpec.model_validate(spec)


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
