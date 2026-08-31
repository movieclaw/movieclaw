"""洗版判定测试（docs/design/quality-upgrade.md §2/§4/§5）。

覆盖：档位阶梯字典序、停止线、部分可比（未知维度）、分辨率虚标场景下的
快照构造（实测优先）、目标兜底缺省、spec 校验。
"""

from __future__ import annotations

import pytest

from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import (
    DISC_SOURCE,
    USER_LOWEST_SOURCE,
    QualitySnapshot,
    RuleSetSpec,
    TorrentCandidate,
    build_snapshot,
    candidate_ladder_rank,
    compare_ladder,
    compare_upgrade,
    ladder_vector,
    provably_at_cutoff,
    provably_below_cutoff,
    quality_label,
    upgrade_target_label,
)


def _candidate(**attrs) -> TorrentCandidate:
    return TorrentCandidate(
        site_id="test", torrent_id="1", title="t", subtitle="", attrs=TorrentAttrs(**attrs)
    )


def _spec(**kwargs) -> RuleSetSpec:
    return RuleSetSpec(**kwargs)


def _snap(**kwargs) -> QualitySnapshot:
    return QualitySnapshot(**kwargs)


def _ladder(snap: QualitySnapshot, spec: RuleSetSpec | None = None) -> tuple[int | None, ...]:
    """档位向量：原盘这类"不会作为候选出现"的档只能直接比向量。"""
    return ladder_vector(
        snap, spec or _spec(upgrade_source="remux", resolutions=["2160p", "1080p"])
    )


# ---------------------------------------------------------------------------
# 档位比较：分辨率严格优先，同分辨率比片源档
# ---------------------------------------------------------------------------


UPGRADE_CASES = [
    # (当前快照, 候选 attrs, spec kwargs, 期望 accepted, 期望 reason_code)
    # 片源档升级：WEB-DL → Remux
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        True,
        None,
    ),
    # 片源档升级：WEBRip → WEB-DL
    (
        dict(resolution="1080p", media_source="WEBRip"),
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(upgrade_source="remux"),
        True,
        None,
    ),
    # 同档不洗（离散档位免疫抖动）：都是 WEB-DL
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_better",
    ),
    # 片源降档拒绝：蓝光 → WEB-DL
    (
        dict(resolution="1080p", media_source="Blu-ray"),
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_better",
    ),
    # 分辨率升级不看片源：720p 蓝光 → 1080p WEBRip 也算升级（分辨率严格优先）
    (
        dict(resolution="720p", media_source="Blu-ray"),
        dict(resolution="1080p", media_source="WEBRip"),
        dict(upgrade_source="remux", cutoff_resolution=None),
        True,
        None,
    ),
    # 分辨率降档拒绝
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="720p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_better",
    ),
    # 停止线：已达目标（1080p Remux 达到 remux 目标，默认目标分辨率 1080p）
    (
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(resolution="1080p", media_source="UHD Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
        "upgrade_at_cutoff",
    ),
    # 停止线：blu-ray 目标下蓝光重编码即到顶，Remux 不再洗
    (
        dict(resolution="1080p", media_source="Blu-ray"),
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="blu-ray"),
        False,
        "upgrade_at_cutoff",
    ),
    # web-dl 目标：WEBRip → WEB-DL 仍可洗
    (
        dict(resolution="1080p", media_source="WEBRip"),
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(upgrade_source="web-dl"),
        True,
        None,
    ),
    # 候选片源未知：同分辨率下无法证明升级
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="1080p"),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_comparable",
    ),
    # 当前片源未知：同分辨率判否，但分辨率升级仍可证明（见下一条）
    (
        dict(resolution="1080p"),
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_comparable",
    ),
    # 当前片源未知 + 候选分辨率更高：分辨率维度可证明，接受
    (
        dict(resolution="1080p"),
        dict(resolution="2160p", media_source="WEB-DL"),
        dict(upgrade_source="remux", cutoff_resolution="2160p", resolutions=["2160p", "1080p"]),
        True,
        None,
    ),
    # 候选分辨率未知：不可比
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_comparable",
    ),
    # 双方片源都未知：末位平局 = 整体等价，判"不构成升级"而非"不可比"（§14.4）。
    # 分辨率低于目标才会进洗版上下文，所以这条在生产里是可达的
    (
        dict(resolution="720p"),
        dict(resolution="720p"),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_better",
    ),
    # 单侧未知仍是不可比（三态铁律不变）：当前已知、候选未知
    (
        dict(resolution="720p", media_source="HDTV"),
        dict(resolution="720p"),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_comparable",
    ),
    # 用户偏好 1080p 优先于 2160p（省空间党）：2160p 候选按偏好序判"更低"
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="2160p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux", resolutions=["1080p", "2160p"]),
        False,
        "upgrade_not_better",
    ),
]


@pytest.mark.parametrize("snap_kw, cand_kw, spec_kw, accepted, code", UPGRADE_CASES)
def test_compare_upgrade_table(snap_kw, cand_kw, spec_kw, accepted, code) -> None:
    verdict = compare_upgrade(_candidate(**cand_kw), _snap(**snap_kw), _spec(**spec_kw))
    assert verdict.accepted is accepted
    assert verdict.reason_code == code
    if not accepted:
        assert verdict.reason_text  # 拒绝必须带完整中文句


def test_compare_upgrade_requires_enabled_spec() -> None:
    """未配置洗版目标时调用是编排层 bug，直接抛错而非静默判否。"""
    with pytest.raises(ValueError):
        compare_upgrade(
            _candidate(resolution="1080p"), _snap(resolution="720p"), _spec()
        )


def test_accepted_verdict_carries_labels() -> None:
    """接受时带 from/to 档位标签，供"从 X 洗到 Y"活动文案。"""
    verdict = compare_upgrade(
        _candidate(resolution="1080p", media_source="Blu-ray", remux=True),
        _snap(resolution="1080p", media_source="WEB-DL"),
        _spec(upgrade_source="remux"),
    )
    assert verdict.accepted
    assert verdict.current_label == "1080p WEB-DL"
    assert verdict.candidate_label == "1080p Remux"


# ---------------------------------------------------------------------------
# 调度口径：只有可证明低于目标的单元参与洗版
# ---------------------------------------------------------------------------


BELOW_CUTOFF_CASES = [
    # (快照, spec kwargs, 期望)
    # WEB-DL 低于 remux 目标 → 参与
    (dict(resolution="1080p", media_source="WEB-DL"), dict(upgrade_source="remux"), True),
    # 已是 Remux → 到顶
    (
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
    ),
    # 分辨率低于目标：片源即使未知也可证明"还差着"
    (
        dict(resolution="720p"),
        dict(upgrade_source="remux", resolutions=["1080p", "720p"], cutoff_resolution="1080p"),
        True,
    ),
    # 同分辨率片源未知 → 证明不了差距，安静
    (dict(resolution="1080p"), dict(upgrade_source="remux"), False),
    # 快照分辨率未知 → 不参与
    (dict(media_source="WEB-DL"), dict(upgrade_source="remux"), False),
    # 分辨率不在偏好序（1080p-only 规则下的手工 480p 文件）→ 位次未知，安静
    (
        dict(resolution="480p", media_source="WEB-DL"),
        dict(upgrade_source="remux", resolutions=["1080p"]),
        False,
    ),
    # 分辨率已超目标（2160p 文件、目标 1080p）→ 到顶
    (
        dict(resolution="2160p", media_source="WEB-DL"),
        dict(
            upgrade_source="remux",
            resolutions=["2160p", "1080p"],
            cutoff_resolution="1080p",
        ),
        False,
    ),
    # 未配置洗版 → 永不参与
    (dict(resolution="720p", media_source="HDTV"), dict(), False),
]


@pytest.mark.parametrize("snap_kw, spec_kw, expected", BELOW_CUTOFF_CASES)
def test_provably_below_cutoff(snap_kw, spec_kw, expected) -> None:
    assert provably_below_cutoff(_snap(**snap_kw), _spec(**spec_kw)) is expected


def test_below_cutoff_none_snapshot() -> None:
    assert provably_below_cutoff(None, _spec(upgrade_source="remux")) is False


AT_CUTOFF_CASES = [
    # (快照, spec kwargs, 期望)——与 provably_below 成对但不互补：
    # 两者都 False 的是第三态"无法确认"（体检报告口径）
    # 已是 Remux → 可证明达标
    (
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        True,
    ),
    # 分辨率已超目标 → 达标（片源档不再比较）
    (
        dict(resolution="2160p", media_source="WEB-DL"),
        dict(upgrade_source="remux", resolutions=["2160p", "1080p"], cutoff_resolution="1080p"),
        True,
    ),
    # WEB-DL 低于 remux 目标 → 未达标
    (dict(resolution="1080p", media_source="WEB-DL"), dict(upgrade_source="remux"), False),
    # 同分辨率片源未知 → 证明不了达标（第三态，below 同样证明不了）
    (dict(resolution="1080p"), dict(upgrade_source="remux"), False),
    # 分辨率未知 → 证明不了
    (dict(media_source="Blu-ray", remux=True), dict(upgrade_source="remux"), False),
    # 未配置洗版 → 无目标可达
    (dict(resolution="1080p", media_source="Blu-ray", remux=True), dict(), False),
]


@pytest.mark.parametrize("snap_kw, spec_kw, expected", AT_CUTOFF_CASES)
def test_provably_at_cutoff(snap_kw, spec_kw, expected) -> None:
    assert provably_at_cutoff(_snap(**snap_kw), _spec(**spec_kw)) is expected


# ---------------------------------------------------------------------------
# 快照构造：实测优先，出处采信名称（§4.1）
# ---------------------------------------------------------------------------


def test_snapshot_probe_overrides_name_resolution() -> None:
    """分辨率虚标场景：种子标称 2160p，实测 1080p——实测说了算。"""
    name = TorrentAttrs(resolution="2160p", media_source="WEB-DL", release_group="FAKE")
    snap = build_snapshot(
        name, probed=True, probe_resolution="1080p", probe_hdr_label=None, probe_bit_rate=8_000_000
    )
    assert snap.resolution == "1080p"
    assert snap.media_source == "WEB-DL"  # 出处采信名称
    assert snap.release_group == "FAKE"
    assert snap.hdr == []  # probe 测得 SDR 是已知空，不是未知
    assert snap.bit_rate == 8_000_000


def test_snapshot_probe_hdr_normalized_to_vocab() -> None:
    """probe 的 "Dolby Vision" 归一为词表值 "DV"，两侧命名空间在此消化。"""
    name = TorrentAttrs(resolution="2160p", media_source="Blu-ray", remux=True, hdr=["HDR10"])
    snap = build_snapshot(
        name, probed=True, probe_resolution="2160p", probe_hdr_label="Dolby Vision"
    )
    assert snap.hdr == ["DV"]
    assert snap.remux is True


def test_snapshot_probe_resolution_missing_falls_back_to_name() -> None:
    """探测失败拿不到分辨率时回落名称值（名称是仅剩的信息）。"""
    name = TorrentAttrs(resolution="1080p", media_source="WEB-DL")
    snap = build_snapshot(name, probed=True, probe_resolution=None)
    assert snap.resolution == "1080p"


def test_snapshot_name_only_path() -> None:
    """纯名称路径（无 probe）：全部取名称解析值。"""
    name = TorrentAttrs(resolution="1080p", media_source="WEB-DL", hdr=["HDR10"])
    snap = build_snapshot(name)
    assert snap.resolution == "1080p" and snap.hdr == ["HDR10"]


def test_snapshot_from_attempt_quality_dict() -> None:
    """attempt.quality（TorrentAttrs 的 exclude_defaults dump）可直接
    validate 成快照——多余字段被忽略，同名字段值域一致。"""
    dumped = TorrentAttrs(
        resolution="1080p", media_source="WEB-DL", seasons=[1], episodes=[2]
    ).model_dump(exclude_defaults=True)
    snap = QualitySnapshot.model_validate(dumped)
    assert snap.resolution == "1080p" and snap.media_source == "WEB-DL"


def test_snapshot_none_name_attrs() -> None:
    snap = build_snapshot(None, probed=True, probe_resolution="1080p")
    assert snap.resolution == "1080p" and snap.media_source is None and snap.remux is False


# ---------------------------------------------------------------------------
# 标签与目标
# ---------------------------------------------------------------------------


def test_labels_and_target() -> None:
    assert quality_label(_snap(resolution="1080p", media_source="WEB-DL")) == "1080p WEB-DL"
    assert quality_label(_snap(resolution="1080p", remux=True)) == "1080p Remux"
    assert quality_label(_snap(resolution="1080p")) == "1080p 片源未知"
    assert quality_label(_snap(media_source="WEB-DL")) == "分辨率未知 WEB-DL"

    assert upgrade_target_label(_spec()) is None
    # 目标分辨率：cutoff > resolutions 首选 > 1080p 兜底
    assert upgrade_target_label(_spec(upgrade_source="remux")) == "1080p Remux"
    assert (
        upgrade_target_label(_spec(upgrade_source="blu-ray", resolutions=["2160p", "1080p"]))
        == "2160p 蓝光"
    )
    assert (
        upgrade_target_label(
            _spec(
                upgrade_source="web-dl",
                resolutions=["2160p", "1080p"],
                cutoff_resolution="1080p",
            )
        )
        == "1080p WEB-DL"
    )


def test_spec_rejects_cutoff_resolution_outside_allowed() -> None:
    """洗版目标分辨率必须在允许列表内，否则永远洗不到。"""
    with pytest.raises(ValueError):
        _spec(resolutions=["1080p"], cutoff_resolution="2160p", upgrade_source="remux")


def test_spec_old_json_reads_as_upgrade_disabled() -> None:
    """旧 spec JSON（无洗版字段）读出即"不洗版"——向前兼容。"""
    spec = RuleSetSpec.model_validate({"resolutions": ["1080p"], "free_only": True})
    assert spec.upgrade_source is None and spec.cutoff_resolution is None


# ---------------------------------------------------------------------------
# 片源白名单（media_sources）：配了之后阶梯的片源位次改由用户序决定
# ---------------------------------------------------------------------------


def test_media_source_whitelist_order_overrides_builtin_tiers() -> None:
    """省流党把 WEB-DL 排在蓝光前面：洗版就认他的序，蓝光→WEB-DL 才叫升级。"""
    spec = _spec(
        resolutions=["1080p"],
        media_sources=["web-dl", "blu-ray"],
        upgrade_source="web-dl",
    )
    verdict = compare_upgrade(
        _candidate(resolution="1080p", media_source="WEB-DL"),
        _snap(resolution="1080p", media_source="Blu-ray"),
        spec,
    )
    assert verdict.accepted is True


def test_media_source_outside_whitelist_stays_not_comparable() -> None:
    """白名单外的档位次未知（不可比），与"不在偏好序里的分辨率"同一取向——
    绝不当最低档，否则倒序偏好会把已入库的 Remux 判成最低而白洗一次。"""
    spec = _spec(
        resolutions=["1080p"],
        media_sources=["web-dl", "blu-ray"],
        upgrade_source="web-dl",
    )
    snap = _snap(resolution="1080p", media_source="Remux")
    assert provably_below_cutoff(snap, spec) is False
    assert provably_at_cutoff(snap, spec) is False


def test_user_lowest_still_comparable_under_whitelist() -> None:
    """T0 哨兵的语义是"低于一切"，白名单不该把它变回不可比。"""
    spec = _spec(
        resolutions=["1080p"], media_sources=["blu-ray"], upgrade_source="blu-ray"
    )
    snap = _snap(resolution="1080p", media_source=USER_LOWEST_SOURCE)
    assert provably_below_cutoff(snap, spec) is True


def test_spec_rejects_upgrade_source_outside_whitelist() -> None:
    """洗版终点必须是规则组自己接受的片源档，否则永远洗不到。"""
    with pytest.raises(ValueError):
        _spec(media_sources=["web-dl"], upgrade_source="remux")


# ---------------------------------------------------------------------------
# 片源人工标注（docs/design/media-source-annotation.md §2.2）：
# T0「用户判定最低档」哨兵与 Remux 存为 media_source 值的两条路径
# ---------------------------------------------------------------------------


def test_user_lowest_is_provably_below_any_target() -> None:
    """标了「不确定，按最低档」（T0）：同分辨率下可证明低于任何目标，进排期。"""
    snap = _snap(resolution="2160p", media_source=USER_LOWEST_SOURCE)
    spec = _spec(upgrade_source="web-dl", resolutions=["2160p", "1080p"])
    assert provably_below_cutoff(snap, spec) is True
    assert provably_at_cutoff(snap, spec) is False


def test_system_unknown_stays_not_comparable() -> None:
    """系统未知（None）语义不变：既证明不了低于目标也证明不了达标（第三态）。"""
    snap = _snap(resolution="2160p")
    spec = _spec(upgrade_source="web-dl", resolutions=["2160p", "1080p"])
    assert provably_below_cutoff(snap, spec) is False
    assert provably_at_cutoff(snap, spec) is False


def test_any_known_tier_upgrades_user_lowest() -> None:
    """任何已知档（哪怕 HDTV T1）都构成对 T0 的严格升级。"""
    verdict = compare_upgrade(
        _candidate(resolution="2160p", media_source="HDTV"),
        _snap(resolution="2160p", media_source=USER_LOWEST_SOURCE),
        _spec(upgrade_source="web-dl", resolutions=["2160p", "1080p"]),
    )
    assert verdict.accepted


def test_unknown_candidate_never_upgrades_user_lowest() -> None:
    """候选片源未知仍不可比——T0 不给未知候选开口子。"""
    verdict = compare_upgrade(
        _candidate(resolution="2160p"),
        _snap(resolution="2160p", media_source=USER_LOWEST_SOURCE),
        _spec(upgrade_source="web-dl", resolutions=["2160p", "1080p"]),
    )
    assert not verdict.accepted
    assert verdict.reason_code == "upgrade_not_comparable"


def test_annotated_remux_value_reaches_cutoff() -> None:
    """标注 Remux 存为 media_source 值（library_file 无 remux 布尔列）：
    档位表按值给 T5，达到任何目标停洗，标签自然显示。"""
    snap = _snap(resolution="1080p", media_source="Remux")
    assert provably_at_cutoff(snap, _spec(upgrade_source="remux")) is True
    assert quality_label(snap) == "1080p Remux"


def test_user_lowest_label_is_human_readable() -> None:
    """哨兵值不能把 user-lowest 原样亮给用户。"""
    snap = _snap(resolution="2160p", media_source=USER_LOWEST_SOURCE)
    assert quality_label(snap) == "2160p 最低档（人工标注）"


# ---------------------------------------------------------------------------
# 原盘档 T6（issue #163 / quality-upgrade.md §2.1.1）：Remux 是从原盘剥出来的，
# 拿 Remux 洗原盘是降级——而默认 upgrade_keep_old=False 会把原盘送进回收站
# ---------------------------------------------------------------------------


def test_remux_never_upgrades_a_disc() -> None:
    """同分辨率下 Remux 候选不构成对原盘的升级——这条是本档位存在的理由。"""
    verdict = compare_upgrade(
        _candidate(resolution="2160p", media_source="Blu-ray", remux=True),
        _snap(resolution="2160p", media_source=DISC_SOURCE),
        _spec(upgrade_source="remux", resolutions=["2160p", "1080p"]),
    )
    assert verdict.accepted is False


def test_disc_upgrades_a_remux() -> None:
    """反向成立：同分辨率下原盘候选高于已入库的 Remux（阶梯是个全序）。"""
    assert (
        compare_ladder(
            _ladder(_snap(resolution="2160p", media_source=DISC_SOURCE)),
            _ladder(_snap(resolution="2160p", media_source="Remux")),
        )
        == 1
    )


def test_disc_reaches_any_cutoff() -> None:
    """原盘已在顶档：可证明达标，洗版就此停下，不再排期。"""
    snap = _snap(resolution="2160p", media_source=DISC_SOURCE)
    spec = _spec(upgrade_source="remux", resolutions=["2160p", "1080p"])
    assert provably_at_cutoff(snap, spec) is True
    assert provably_below_cutoff(snap, spec) is False


def test_disc_beats_name_parsed_remux_flag() -> None:
    """目录名里带 Remux 的原盘仍是原盘：结构证据压过名称解析的布尔位，
    否则 T6 会被那个词拉回 T5，Remux 候选又变成"平档"。"""
    snap = _snap(resolution="2160p", media_source=DISC_SOURCE, remux=True)
    assert (
        compare_ladder(
            _ladder(snap), _ladder(_snap(resolution="2160p", media_source="Remux"))
        )
        == 1
    )


def test_disc_still_comparable_under_whitelist() -> None:
    """配了片源白名单时，T6 的语义是"高于白名单里的一切"（与 T0 对称）。

    原盘本身不在白名单值域里——白名单说的是"愿意下载哪类资源"——但它不能
    因此退回不可比，否则这类单元会永远停在"无法确认"上等人工介入。
    """
    spec = _spec(
        resolutions=["2160p"], media_sources=["web-dl", "blu-ray"], upgrade_source="blu-ray"
    )
    snap = _snap(resolution="2160p", media_source=DISC_SOURCE)
    assert provably_at_cutoff(snap, spec) is True
    assert provably_below_cutoff(snap, spec) is False
    verdict = compare_upgrade(
        _candidate(resolution="2160p", media_source="Blu-ray"), snap, spec
    )
    assert verdict.accepted is False


def test_disc_label_is_human_readable() -> None:
    """档位值不能把内部的 "Disc" 原样亮给用户。"""
    assert quality_label(_snap(resolution="2160p", media_source=DISC_SOURCE)) == "2160p 原盘"


# ---------------------------------------------------------------------------
# 选优排序用的绝对档位（§15.4）：与升级判定的"未知即不可比"是两套语义
# ---------------------------------------------------------------------------


def test_candidate_ladder_rank_orders_by_tier() -> None:
    """同分辨率下档位更高的候选排序位次更大——洗版按它选优而非按评分。"""
    spec = _spec(upgrade_source="remux")
    webdl = candidate_ladder_rank(TorrentAttrs(resolution="1080p", media_source="WEB-DL"), spec)
    remux = candidate_ladder_rank(
        TorrentAttrs(resolution="1080p", media_source="Blu-ray", remux=True), spec
    )
    assert remux > webdl


def test_candidate_ladder_rank_unknown_sorts_lowest() -> None:
    """未知维度按最低（-1）排序：排序只决定谁先投，不会因此多投一个候选。"""
    spec = _spec(upgrade_source="remux")
    assert candidate_ladder_rank(TorrentAttrs(), spec) == (-1, -1)
    assert candidate_ladder_rank(TorrentAttrs(resolution="1080p"), spec)[1] == -1


def test_candidate_ladder_rank_follows_user_resolution_order() -> None:
    """分辨率位次跟随规则组偏好序（省空间党的 1080p 优先同样生效）。"""
    spec = _spec(upgrade_source="remux", resolutions=["1080p", "2160p"])
    r1080 = candidate_ladder_rank(TorrentAttrs(resolution="1080p", media_source="WEB-DL"), spec)
    r2160 = candidate_ladder_rank(TorrentAttrs(resolution="2160p", media_source="WEB-DL"), spec)
    assert r1080 > r2160


# ---------------------------------------------------------------------------
# 档位向量比较：三个洗版谓词的共同底座（§14.4）
# ---------------------------------------------------------------------------


LADDER_CASES = [
    # (左, 右, 期望)  —— 1 = 左更优，-1 = 右更优，0 = 逐位等价，None = 不可比
    ((4, 3), (4, 2), 1),
    ((4, 2), (4, 3), -1),
    ((4, 2), (3, 5), 1),  # 首位定序，后续位够不着（分辨率严格优先）
    ((4, 3), (4, 3), 0),
    # 双方都未知 = 该位平局：末位平局即整体等价
    ((4, None), (4, None), 0),
    # 单侧未知 = 截断，后续位一律够不着
    ((4, None), (4, 3), None),
    ((4, 3), (4, None), None),
    ((None, 5), (4, 1), None),
    # 首位就定序时，次位的未知不影响结论——未知只截断它**之后**的位
    ((5, None), (4, 3), 1),
]


@pytest.mark.parametrize("left, right, expected", LADDER_CASES)
def test_compare_ladder_table(left, right, expected) -> None:
    assert compare_ladder(left, right) == expected


def test_compare_ladder_is_antisymmetric() -> None:
    """交换两侧则结果取反（不可比与等价对称不变）——比较关系自洽的底线。"""
    for left, right, expected in LADDER_CASES:
        flipped = compare_ladder(right, left)
        assert flipped == (None if expected is None else -expected)


# ---------------------------------------------------------------------------
# 多维阶梯（§14.3）：维度顺序即优先级，偏好列表顺序即位次
# ---------------------------------------------------------------------------


def _multi_spec(**kwargs) -> RuleSetSpec:
    base = dict(
        upgrade_source="web-dl",
        resolutions=["2160p", "1080p"],
        # UI 选"H.265 一族"会把三种等价写法都写进来
        video_codecs=["x265", "H.265", "HEVC", "x264"],
        platforms=["netflix", "amazon"],
        upgrade_ladder=["resolution", "source", "video_codec", "platform"],
    )
    return RuleSetSpec(**{**base, **kwargs})


_MULTI_BASE = dict(
    resolution="2160p", media_source="WEB-DL", video_codec="x264", platforms=["amazon"]
)


def test_codec_position_upgrades_within_same_resolution_and_source() -> None:
    verdict = compare_upgrade(
        _candidate(**{**_MULTI_BASE, "video_codec": "x265"}), _snap(**_MULTI_BASE), _multi_spec()
    )
    assert verdict.accepted


def test_same_codec_family_written_differently_is_a_tie() -> None:
    """x265 与 HEVC 是同一族的两种写法，必须塌缩成阶梯上的同一个位次——
    否则同族的不同写法会互相"升级"，来回重下。"""
    snapshot = {**_MULTI_BASE, "video_codec": "x265"}
    verdict = compare_upgrade(
        _candidate(**{**snapshot, "video_codec": "hevc"}), _snap(**snapshot), _multi_spec()
    )
    assert not verdict.accepted
    assert verdict.reason_code == "upgrade_not_better"


def test_platform_position_decides_when_higher_positions_tie() -> None:
    snapshot = {**_MULTI_BASE, "video_codec": "x265"}
    verdict = compare_upgrade(
        _candidate(**{**snapshot, "platforms": ["netflix"]}), _snap(**snapshot), _multi_spec()
    )
    assert verdict.accepted


def test_dimension_order_changes_the_verdict() -> None:
    """把平台提到编码之前，"编码更好但平台更差"的候选从升级变成降级——
    维度顺序就是用户表达偏好的地方。"""
    snapshot = {
        "resolution": "2160p",
        "media_source": "WEB-DL",
        "video_codec": "x264",
        "platforms": ["netflix"],
    }
    candidate = {**snapshot, "video_codec": "x265", "platforms": ["amazon"]}
    codec_first = compare_upgrade(_candidate(**candidate), _snap(**snapshot), _multi_spec())
    platform_first = compare_upgrade(
        _candidate(**candidate),
        _snap(**snapshot),
        _multi_spec(upgrade_ladder=["resolution", "source", "platform", "video_codec"]),
    )
    assert codec_first.accepted
    assert not platform_first.accepted


def test_dimension_without_preference_list_is_skipped_not_unknown() -> None:
    """偏好列表为空的位自动跳过：当成"未知"会截断整条阶梯，洗版全线哑火。"""
    spec = _multi_spec(platforms=[], video_codecs=[])
    verdict = compare_upgrade(
        _candidate(resolution="2160p", media_source="Blu-ray"),
        _snap(resolution="2160p", media_source="WEBRip"),
        spec,
    )
    assert verdict.accepted  # 只剩分辨率与片源两位，片源 T2 → T4 成立


def test_unknown_low_position_does_not_block_higher_position_verdict() -> None:
    """未知只截断它**之后**的位：编码已定序时，平台未标注不影响结论。"""
    verdict = compare_upgrade(
        _candidate(resolution="2160p", media_source="WEB-DL", video_codec="x265"),
        _snap(**_MULTI_BASE),
        _multi_spec(),
    )
    assert verdict.accepted


def test_ladder_normalization_drops_unknown_and_duplicates() -> None:
    spec = RuleSetSpec.model_validate(
        {"upgrade_ladder": ["platform", "nope", "platform", "resolution"]}
    )
    assert spec.upgrade_ladder == ["platform", "resolution"]


def test_empty_ladder_falls_back_to_default_pair() -> None:
    """空阶梯没有任何一位可比 ⇒ 没有候选构成升级、所有单元又都算已达目标，
    洗版静默全停。回落到缺省二元组至少行为正确。"""
    assert RuleSetSpec.model_validate({"upgrade_ladder": ["nope"]}).upgrade_ladder == [
        "resolution",
        "source",
    ]


def test_target_label_renders_every_effective_dimension() -> None:
    assert upgrade_target_label(_multi_spec()) == "2160p WEB-DL · x265 · netflix"
    # 未进阶梯的维度不出现在目标里
    assert upgrade_target_label(_multi_spec(upgrade_ladder=["resolution", "source"])) == (
        "2160p WEB-DL"
    )


def test_ladder_dimension_domains_do_not_drift() -> None:
    """值域（models 校验用）与标签表（decision 文案用）必须一一对应。

    两份定义分居两个模块是循环导入所迫；漂移的后果很隐蔽——校验层放行了一个
    维度，排序层却算不出它的位次。这条测试是它们之间唯一的粘合。
    """
    from movieclaw_matcher.decision import _LADDER_LABELS
    from movieclaw_matcher.models import _LADDER_DIMENSION_VALUES

    assert set(_LADDER_LABELS) == set(_LADDER_DIMENSION_VALUES)


def test_all_dimensions_skipped_falls_back_to_default_pair() -> None:
    """阶梯里的维度**全部因未配偏好而被跳过**时，回落缺省二元组。

    校验层的同名保护只挡得住"列表本身为空"。用户在界面上点掉分辨率和片源、
    只留一个没配偏好的平台，列表非空但生效维度为零——那时比较恒等价：
    没有候选构成升级，所有单元又都算"已达目标"，720p HDTV 会被报成
    「已达 Remux 目标」，洗版静默全停而详情页还在误导人。
    """
    spec = RuleSetSpec.model_validate(
        {"upgrade_source": "remux", "upgrade_ladder": ["platform"]}
    )
    snap = _snap(resolution="720p", media_source="HDTV")
    assert provably_below_cutoff(snap, spec) is True
    assert provably_at_cutoff(snap, spec) is False


def test_default_ladder_constants_agree() -> None:
    """两个模块各自持有一份缺省二元组（循环导入所迫），值必须一致。"""
    from movieclaw_matcher.decision import _DEFAULT_LADDER
    from movieclaw_matcher.models import _DEFAULT_UPGRADE_LADDER

    assert tuple(_DEFAULT_LADDER) == tuple(_DEFAULT_UPGRADE_LADDER)
