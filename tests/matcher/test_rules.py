"""规则过滤与评分的表驱动测试：每个维度覆盖 接受/拒绝/未知 三态。"""

from __future__ import annotations

import logging

import pytest

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
        # 片源档（白名单按档，词表里的多种写法归一到同一档）
        ({"media_source": "Blu-ray"}, {"media_sources": ["blu-ray"]}, True, None),
        ({"media_source": "UHD Blu-ray"}, {"media_sources": ["blu-ray"]}, True, None),
        ({"remux": True}, {"media_sources": ["remux"]}, True, None),
        (
            {"media_source": "HDTV"},
            {"media_sources": ["blu-ray", "web-dl"]},
            False,
            "media_source_not_allowed",
        ),
        (
            {"media_source": "WEBRip"},
            {"media_sources": ["rip"]},
            True,
            None,
        ),
        ({}, {"media_sources": ["web-dl"]}, False, "media_source_unknown"),
        ({"media_source": "HDTV"}, {}, True, None),  # 不限片源时任意都放行
        # 编码（大小写不敏感）
        ({"video_codec": "H.265"}, {"video_codecs": ["h.265"]}, True, None),
        ({"video_codec": "H.264"}, {"video_codecs": ["H.265"]}, False, "codec_not_allowed"),
        # 编码按族比较：x265 / H.265 / HEVC 是同一编码的三种写法，写哪个都互通
        ({"video_codec": "HEVC"}, {"video_codecs": ["x265"]}, True, None),
        ({"video_codec": "x265"}, {"video_codecs": ["HEVC"]}, True, None),
        ({"video_codec": "AVC"}, {"video_codecs": ["x264"]}, True, None),
        # 跨族仍然拒绝，族归一没有放宽到"什么都收"
        ({"video_codec": "AV1"}, {"video_codecs": ["x265"]}, False, "codec_not_allowed"),
        # 族表外的编码各自成族：自己命中自己，不误伤也不互通
        ({"video_codec": "VC-1"}, {"video_codecs": ["VC-1"]}, True, None),
        ({"video_codec": "VP9"}, {"video_codecs": ["x265"]}, False, "codec_not_allowed"),
        # 流媒体平台：与制作组正交，两者都配则都要满足
        ({"platforms": ["netflix"]}, {"platforms": ["netflix"]}, True, None),
        ({"platforms": ["netflix"]}, {"platforms": ["Netflix"]}, True, None),  # 大小写不敏感
        (
            {"platforms": ["iqiyi"]},
            {"platforms": ["netflix"]},
            False,
            "platform_not_allowed",
        ),
        # 平台未识别 + 规则配了白名单 → 按不合格（与分辨率/编码的未知语义一致）
        ({}, {"platforms": ["netflix"]}, False, "platform_unknown"),
        ({"platforms": ["netflix"]}, {"platforms_block": ["netflix"]}, False, "platform_blocked"),
        ({}, {"platforms_block": ["netflix"]}, True, None),  # 黑名单不误伤未知
        # 平台与制作组各自独立：平台过了但组不在白名单，仍拒且原因指向组
        (
            {"platforms": ["netflix"], "release_group": "Other"},
            {"platforms": ["netflix"], "release_groups_allow": ["FLUX"]},
            False,
            "group_not_allowed",
        ),
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
        ({}, {"hdr": "require"}, False, "hdr_not_allowed"),
        ({"hdr": ["DV"]}, {"hdr": "forbid"}, False, "hdr_not_allowed"),
        ({}, {"hdr": "forbid"}, True, None),
        # DV：独立轴，只判断杜比视界标记
        ({"hdr": ["DV"]}, {"dv": "require"}, True, None),
        ({"hdr": ["HDR10", "DV"]}, {"dv": "require"}, True, None),
        ({"hdr": ["HDR10"]}, {"dv": "require"}, False, "hdr_not_allowed"),
        ({}, {"dv": "require"}, False, "hdr_not_allowed"),
        ({"hdr": ["DV"]}, {"dv": "forbid"}, False, "hdr_blocked"),
        ({"hdr": ["HDR10", "DV"]}, {"dv": "forbid"}, False, "hdr_blocked"),
        ({"hdr": ["HDR10"]}, {"dv": "forbid"}, True, None),
        ({}, {"dv": "forbid"}, True, None),
        # 两轴叠加：必须 HDR 且排除 DV（PR #112 的原始诉求）
        ({"hdr": ["HDR10"]}, {"hdr": "require", "dv": "forbid"}, True, None),
        (
            {"hdr": ["HDR10", "DV"]},
            {"hdr": "require", "dv": "forbid"},
            False,
            "hdr_blocked",
        ),
        ({"hdr": ["DV"]}, {"hdr": "require", "dv": "forbid"}, False, "hdr_blocked"),
        ({}, {"hdr": "require", "dv": "forbid"}, False, "hdr_not_allowed"),
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


def test_contradictory_legacy_hdr_dv_combo_resolves_instead_of_raising() -> None:
    """「排除 HDR」+「必须 DV」这个历史矛盾组合**不再抛错**。

    读取路径必须永远宽容（§16.4）：抛错会让整个规则组解析失败、匹配管线全线
    停摆。排除项更强，按「排除 HDR」解释为「只要 SDR」。合并成一个列表之后，
    这种组合在新形态里根本写不出来。
    """
    spec = RuleSetSpec.model_validate({"hdr": "forbid", "dv": "require"})
    assert spec.hdr_levels == ["SDR"]
    assert spec.hdr_block == []


def test_size_unknown_rejected_when_bounds_set() -> None:
    verdict = evaluate_rules(_candidate(), RuleSetSpec(size_max_mb=2000))
    assert verdict.accepted is False
    assert verdict.reason_code == "size_unknown"


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


def test_score_media_source_preference_follows_spec_order() -> None:
    """media_sources 列表顺序即偏好：省流党把 WEB-DL 排前面，选优就按他说的算。"""
    spec = RuleSetSpec(media_sources=["web-dl", "blu-ray"])  # 刻意 WEB-DL 优先
    web = evaluate_rules(_candidate(media_source="WEB-DL"), spec)
    disc = evaluate_rules(_candidate(media_source="Blu-ray"), spec)
    assert web.score > disc.score


def test_score_resolution_outranks_media_source() -> None:
    """两个主轴同时生效时分辨率先说话（与洗版阶梯缺省位序"分辨率 › 片源"一致）。"""
    spec = RuleSetSpec(resolutions=["2160p", "1080p"], media_sources=["remux", "web-dl"])
    better_resolution = evaluate_rules(
        _candidate(resolution="2160p", media_source="WEB-DL"), spec
    )
    better_source = evaluate_rules(
        _candidate(resolution="1080p", media_source="Remux", remux=True), spec
    )
    assert better_resolution.score > better_source.score


def test_score_ignores_media_source_when_unset() -> None:
    """没配片源白名单 = 不限片源：任何内置片源序都不该悄悄改变存量规则组的选优。"""
    spec = RuleSetSpec()
    remux = evaluate_rules(_candidate(media_source="Remux", remux=True), spec)
    hdtv = evaluate_rules(_candidate(media_source="HDTV"), spec)
    assert remux.score == hdtv.score


class TestSubtitleAudioLanguages:
    """字幕/音轨语言维度：BCP 47 前缀匹配 + 声明式未声明即拒。"""

    def test_generic_zh_requirement_matches_any_chinese(self):
        spec = RuleSetSpec(subtitle_languages_require=["zh"])
        assert evaluate_rules(_candidate(subtitle_languages=["zh-Hans"]), spec).accepted
        assert evaluate_rules(_candidate(subtitle_languages=["zh-Hant"]), spec).accepted
        assert evaluate_rules(_candidate(subtitle_languages=["zh"]), spec).accepted

    def test_specific_script_requirement_rejects_generic(self):
        spec = RuleSetSpec(subtitle_languages_require=["zh-Hans"])
        assert evaluate_rules(_candidate(subtitle_languages=["zh-Hans"]), spec).accepted
        verdict = evaluate_rules(_candidate(subtitle_languages=["zh"]), spec)
        assert not verdict.accepted
        assert verdict.reason_code == "subtitle_language_missing"

    def test_undeclared_subtitle_rejected_when_required(self):
        spec = RuleSetSpec(subtitle_languages_require=["zh"])
        verdict = evaluate_rules(_candidate(subtitle_languages=[]), spec)
        assert not verdict.accepted
        assert verdict.reason_code == "subtitle_language_missing"

    def test_any_of_semantics(self):
        spec = RuleSetSpec(subtitle_languages_require=["zh-Hans", "en"])
        assert evaluate_rules(_candidate(subtitle_languages=["en"]), spec).accepted

    def test_audio_language_requirement(self):
        spec = RuleSetSpec(audio_languages_require=["cmn"])
        assert evaluate_rules(_candidate(audio_languages=["cmn", "ja"]), spec).accepted
        verdict = evaluate_rules(_candidate(audio_languages=["yue"]), spec)
        assert not verdict.accepted
        assert verdict.reason_code == "audio_language_missing"

    def test_unset_dimensions_do_not_filter(self):
        assert evaluate_rules(_candidate(subtitle_languages=[], audio_languages=[]),
                              RuleSetSpec()).accepted


def test_unknown_platform_value_is_dropped_not_raised() -> None:
    """规则组 spec 的读取路径必须永远宽容：词表外的平台值丢弃而非抛错。

    抛错会让整个规则组解析失败、匹配管线全线停摆（PR #120 的写法正是如此），
    波及范围远大于"少配了一个条件"。
    """
    spec = RuleSetSpec.model_validate({"platforms": ["netflix", "no_such_platform"]})
    assert spec.platforms == ["netflix"]
    # 全部是未知值 → 该维度不产生约束，等价于没配
    assert RuleSetSpec.model_validate({"platforms": ["nope"]}).platforms == []


# ---------------------------------------------------------------------------
# HDR 有序列表（§14.8 方案 A）：硬过滤与偏好合并成一个字段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "levels", "accepted"),
    [
        (["DV"], ["DV", "HDR10"], True),
        (["HDR10"], ["DV", "HDR10"], True),
        ([], ["SDR"], True),  # 未标注 = SDR
        ([], ["DV", "HDR10"], False),  # 缺席即否定，SDR 不在白名单
        (["DV"], ["HDR10"], False),  # 没列 DV 就不收 DV
        (["HLG"], ["SDR", "HDR10"], False),
        (["HDR10", "DV"], ["DV", "HDR10+", "HDR10", "HLG"], True),
    ],
)
def test_hdr_levels_filtering(tags, levels, accepted) -> None:
    verdict = evaluate_rules(_candidate(hdr=tags), RuleSetSpec(hdr_levels=levels))
    assert verdict.accepted is accepted
    if not accepted:
        assert verdict.reason_code == "hdr_not_allowed"


def test_bare_hdr_tag_is_accepted_as_hdr10() -> None:
    """泛指 ``HDR`` 归一成 HDR10：``2160p WEB-DL … HDR …`` 是 4K WEB 最常见的
    命名之一，enrich 会原样产出裸 ``HDR``。不做这层归一，它就落在"值域装不下"
    的缝里——白名单非空时这类资源被无条件拒绝，而用户没有任何办法放行。
    """
    bare = _candidate(hdr=["HDR"])
    assert evaluate_rules(bare, RuleSetSpec(hdr_levels=["HDR10+", "HDR10", "HLG", "SDR"])).accepted
    assert evaluate_rules(bare, RuleSetSpec(hdr="require")).accepted
    # 归一是双向的：只收 SDR 的用户不该因此收到 HDR 资源
    sdr_only = evaluate_rules(bare, RuleSetSpec(hdr_levels=["SDR"]))
    assert not sdr_only.accepted
    assert sdr_only.reason_code == "hdr_not_allowed"
    # 黑名单同构：排除 HDR10 就该连泛指 HDR 一起排除
    assert evaluate_rules(bare, RuleSetSpec(hdr_block=["HDR10"])).reason_code == "hdr_blocked"


def test_bare_hdr_in_rule_levels_normalizes_instead_of_being_dropped() -> None:
    """规则组里写 ``HDR`` 也按 HDR10 存下，而不是被静默丢掉。"""
    spec = RuleSetSpec.model_validate({"hdr_levels": ["HDR10+", "HDR10", "HDR", "SDR", "HLG"]})
    assert spec.hdr_levels == ["HDR10+", "HDR10", "SDR", "HLG"]  # 与已有的 HDR10 合并
    assert RuleSetSpec.model_validate({"hdr_levels": ["HDR"]}).hdr_levels == ["HDR10"]


def test_unknown_hdr_value_is_dropped_with_a_warning(caplog) -> None:
    """值域外的值仍然丢弃（读取路径永远宽容），但要留一条日志——静默吞掉
    会让用户配出来的东西和自己以为的不一样，且毫无提示。"""
    with caplog.at_level(logging.WARNING, logger="movieclaw_matcher.models"):
        spec = RuleSetSpec.model_validate({"hdr_levels": ["HDR10", "HDR42"]})
    assert spec.hdr_levels == ["HDR10"]
    assert any("HDR42" in record.getMessage() for record in caplog.records)


def test_hdr_block_is_needed_to_exclude_a_format() -> None:
    """多值属性上"只要 A"表达不了"排除 B"——这正是 HDR 与平台一样分白/黑
    两份的原因：白名单是"任一命中即过"，双标资源靠 HDR10 就能过，
    要挡住 DV 只能靠黑名单。"""
    dual = _candidate(hdr=["HDR10", "DV"])
    allow_only = RuleSetSpec(hdr_levels=["HDR10+", "HDR10", "HLG", "SDR"])
    assert evaluate_rules(dual, allow_only).accepted
    with_block = RuleSetSpec(hdr_levels=["HDR10+", "HDR10", "HLG", "SDR"], hdr_block=["DV"])
    verdict = evaluate_rules(dual, with_block)
    assert not verdict.accepted
    assert verdict.reason_code == "hdr_blocked"


@pytest.mark.parametrize(
    ("legacy", "levels", "block"),
    [
        ({}, [], []),
        ({"hdr": "require"}, ["DV", "HDR10+", "HDR10", "HLG"], []),
        ({"hdr": "require", "dv": "forbid"}, ["HDR10+", "HDR10", "HLG"], ["DV"]),
        ({"hdr": "forbid"}, ["SDR"], []),
        ({"dv": "require"}, ["DV"], []),
        ({"dv": "forbid"}, [], ["DV"]),  # 不限 HDR，只排除 DV
    ],
)
def test_legacy_hdr_fields_derive_levels(legacy, levels, block) -> None:
    """旧三态组合读进来即换算成新形态——存量规则组不迁移也照常工作。"""
    spec = RuleSetSpec.model_validate(legacy)
    assert (spec.hdr_levels, spec.hdr_block) == (levels, block)


def test_new_levels_backfill_legacy_fields_for_rollback() -> None:
    """写入时反向回填旧键：应用内更新回退到旧版本后，旧代码仍读得懂。

    表达不了顺序或混合集合时退回最宽松的等价值——不精确，但不崩、不危险。
    """
    spec = RuleSetSpec.model_validate({"hdr_levels": ["DV"]})
    assert (spec.hdr.value, spec.dv.value) == ("require", "require")
    mixed = RuleSetSpec.model_validate({"hdr_levels": ["SDR", "HDR10"]})
    assert mixed.hdr.value == "any"


def test_hdr_spec_roundtrip_is_stable() -> None:
    """读→写→读 必须稳定，否则每次保存都会把规则组改一点点。"""
    first = RuleSetSpec.model_validate({"hdr": "require", "dv": "forbid"})
    dumped = first.model_dump(exclude_defaults=True, mode="json")
    assert RuleSetSpec.model_validate(dumped).model_dump(
        exclude_defaults=True, mode="json"
    ) == dumped
