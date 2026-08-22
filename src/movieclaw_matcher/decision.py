"""选优与洗版比较（内核第三步）。

选优：同一单元的多个通过候选投谁——**整季包优先于单集**（已确认决策），
同类内按规则评分降序，评分相同做种多者优先（下得快）。

洗版（docs/design/quality-upgrade.md §2/§5）：候选是否构成对现有版本的升级。
核心是一条**二元组字典序**的档位阶梯：

    rank = (分辨率位次, 片源档)     # 分辨率严格优先，字典序比较

- 分辨率位次取规则组 ``resolutions`` 的偏好顺序（第一位最高）——复用用户
  已经表达过的偏好；未配置时用内置默认序（高清优先）；
- 片源档内置固定不暴露配置：Remux > 蓝光重编码 > WEB-DL > Rip 类 > TV 录制类；
- **未知不可比，但只在未知的维度上**（部分可比）：分辨率双方已知即可定
  分辨率维度的序；片源未知时同分辨率的比较判否——只在能证明的维度上行动；
- 升级要求**严格更高**（rank 相等不洗），离散档位天然免疫微小差异抖动；
- 评分公式（免费/做种）不参与"是否构成升级"——那是"现在下谁划算"，
  不是"这个版本更好"，两件事分开。
"""

from __future__ import annotations

from functools import partial

from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher.models import (
    IdentityMatch,
    QualitySnapshot,
    RuleSetSpec,
    RuleVerdict,
    TorrentCandidate,
    UpgradeVerdict,
)

Entry = tuple[TorrentCandidate, IdentityMatch, RuleVerdict]


def pick_best(entries: list[Entry]) -> Entry | None:
    """从"已通过规则"的候选里选一个投递目标；空列表/全拒绝返回 None。"""
    accepted = [e for e in entries if e[2].accepted]
    if not accepted:
        return None
    return max(
        accepted,
        key=lambda e: (e[1].is_pack, e[2].score, e[0].seeders or 0),
    )


# ---------------------------------------------------------------------------
# 片源档阶梯（内置，不暴露配置；值域对齐 movieclaw_enrich.vocab.MEDIA_SOURCE）
# ---------------------------------------------------------------------------

# Remux 是封装方式不是片源，单独用 remux 布尔判定为最高档 T5
_REMUX_TIER = 5

# 人工标注「不确定，按最低档」的哨兵值（docs/design/media-source-annotation.md
# §2.2）。只有片源标注 API 能写入——enrich 词表永远解析不出它，因此
# 「系统未知（None，不可比）」与「用户判定最低（T0，可比）」严格分离。
USER_LOWEST_SOURCE = "user-lowest"

# 片源 → 档位。键为 casefold 后的归一值；不在表中/None = 片源未知（不可比）。
# 与换源 replacement._SOURCE_RANK 相比补全了 BDRip/HDRip/DVD 等档
# （Phase 7 会把换源迁移到本表，消除两套片源序）。
_SOURCE_TIER: dict[str, int] = {
    # T5：人工标注的 Remux 存为 media_source 值（library_file 无 remux 布尔列，
    # 快照出处维度由 file.media_source 覆盖，走值比走布尔位更省一列）
    "remux": _REMUX_TIER,
    # T4 光盘重编码
    "uhd blu-ray": 4,
    "blu-ray": 4,
    "hd-dvd": 4,
    # T3 流媒体原流
    "web-dl": 3,
    # T2 二压 Rip 类
    "webrip": 2,
    "bdrip": 2,
    "hdrip": 2,
    "dvdrip": 2,
    # T1 TV 录制类与 DVD
    "hdtv": 1,
    "hdtvrip": 1,
    "tvrip": 1,
    "dvd": 1,
    # T0 用户判定最低档：低于一切已知档，可证明低于任何洗版目标
    USER_LOWEST_SOURCE: 0,
}

# 未配置 resolutions 时的内置默认偏好序（高清优先），与 rules.py 的
# _DEFAULT_RESOLUTION_SCORE 同方向、覆盖面更全（快照可能出现 576p 等低档值）
_DEFAULT_RESOLUTION_LADDER = ["4320p", "2160p", "1440p", "1080p", "720p", "576p", "480p"]

# 洗版目标分辨率的兜底缺省：resolutions 与 cutoff_resolution 都缺省时用它。
# 保守取 1080p——不因为开了洗版就把用户意外带进 4K 的磁盘占用
# （调研里 44GB Forrest Gump 的教训，quality-upgrade-research.md §3 主题 E）。
_FALLBACK_CUTOFF_RESOLUTION = "1080p"

# 洗版目标片源档的展示名（upgrade_source 值 → 中文语境标签）
_TARGET_SOURCE_LABEL = {"web-dl": "WEB-DL", "blu-ray": "蓝光", "remux": "Remux"}
_TARGET_SOURCE_TIER = {"web-dl": 3, "blu-ray": 4, "remux": _REMUX_TIER}


def source_tier(media_source: str | None, remux: bool) -> int | None:
    """片源 → 档位；未知片源返回 None（不可比，绝不当最低档处理）。"""
    if remux:
        return _REMUX_TIER
    if not media_source:
        return None
    return _SOURCE_TIER.get(media_source.casefold())


def _resolution_ladder(spec: RuleSetSpec) -> list[str]:
    """生效的分辨率偏好序（casefold）。配置了 resolutions 就完全以它为准——
    不在列表中的分辨率位次未知（如 1080p-only 规则下的手工 480p 文件），
    宁可让该单元安静，也不做数值猜测导致意外下载。"""
    ladder = spec.resolutions or _DEFAULT_RESOLUTION_LADDER
    return [r.casefold() for r in ladder]


def resolution_rank(resolution: str | None, spec: RuleSetSpec) -> int | None:
    """分辨率位次，越大越优；未知/不在偏好序中返回 None。"""
    if not resolution:
        return None
    ladder = _resolution_ladder(spec)
    key = resolution.casefold()
    if key not in ladder:
        return None
    return len(ladder) - ladder.index(key)


# 档位阶梯的位序（quality-upgrade.md §2.1）。§14 会把它变成规则组可配的
# ``upgrade_ladder``；在那之前写死为二元组，语义与位次来源都不变。
_LADDER_LABELS: tuple[str, ...] = ("分辨率", "片源")


def ladder_vector(
    item: QualitySnapshot | TorrentAttrs, spec: RuleSetSpec
) -> tuple[int | None, ...]:
    """档位向量：逐位的位次，``None`` = 该维度未知（不可比，见 §2.4）。"""
    return (
        resolution_rank(item.resolution, spec),
        source_tier(item.media_source, item.remux),
    )


def compare_ladder_at(
    left: tuple[int | None, ...], right: tuple[int | None, ...]
) -> tuple[int | None, int]:
    """字典序逐位比较两个档位向量，返回 (结果, 定序位下标)。

    结果：``1`` = left 更优，``-1`` = right 更优，``0`` = 逐位等价，
    ``None`` = 不可比（某位单侧未知，比较被截断，后续位够不着）。
    逐位等价时下标是向量长度（没有任何一位定序）。

    三条规则（quality-upgrade.md §2.4 + §14.4）：
    - 双方都已知且不同 → 该位定序，结束；
    - **双方都未知 → 该位平局，继续比下一位**（两边都没标片源是命名习惯的
      常态，不是数据质量问题）；
    - 单侧未知 → 不可比。三态铁律：未知永远不当已知用。
    """
    for index, (a, b) in enumerate(zip(left, right, strict=True)):
        if a is None and b is None:
            continue
        if a is None or b is None:
            return None, index
        if a != b:
            return (1 if a > b else -1), index
    return 0, len(left)


def compare_ladder(
    left: tuple[int | None, ...], right: tuple[int | None, ...]
) -> int | None:
    """``compare_ladder_at`` 只取结果——三个洗版谓词的共同底座。"""
    return compare_ladder_at(left, right)[0]


def candidate_ladder_rank(
    attrs: QualitySnapshot | TorrentAttrs, spec: RuleSetSpec
) -> tuple[int, ...]:
    """候选自身的绝对档位，供**洗版选优排序**使用（不参与升级判定）。

    与 ``compare_ladder`` 的"未知即不可比"不同：这里未知按最低（-1）处理。
    两者不矛盾——升级判定回答"要不要投"，必须只在能证明的维度上行动；
    本函数只回答"同样已经判定合格的候选谁先投"，把未知排在后面不会
    产生任何额外投递。

    用途见 quality-upgrade.md §15.4：洗版候选按档位而非评分选优，否则
    免费加分（_FREE_SCORE=100）会压过一档分辨率（30），系统可能先抓一个
    "免费但只高半档"的版本，同一单元付两次下载。
    """
    return tuple(-1 if rank is None else rank for rank in ladder_vector(attrs, spec))


def target_vector(spec: RuleSetSpec) -> tuple[int | None, ...] | None:
    """洗版目标的档位向量；目标分辨率不在偏好序里时返回 ``None``。

    返回 None 表示"这个目标无法比较"（配置矛盾，校验器已拦，此处防御）——
    调用方一律按"证明不了"处理，不猜。
    """
    if spec.upgrade_source is None:
        return None
    target_resolution, target_tier = _target(spec)
    resolution = resolution_rank(target_resolution, spec)
    if resolution is None:
        return None
    return (resolution, target_tier)


def _target(spec: RuleSetSpec) -> tuple[str, int]:
    """洗版目标 (分辨率, 片源档)。调用前提：spec.upgrade_source 已配置。"""
    resolution = (
        spec.cutoff_resolution
        or (spec.resolutions[0] if spec.resolutions else None)
        or _FALLBACK_CUTOFF_RESOLUTION
    )
    return resolution, _TARGET_SOURCE_TIER[spec.upgrade_source.value]


def quality_label(snapshot: QualitySnapshot | TorrentAttrs) -> str:
    """档位的人话标签（"1080p Remux" / "2160p WEB-DL" / "1080p 片源未知"），
    供活动文案与详情页展示。"""
    resolution = snapshot.resolution or "分辨率未知"
    if snapshot.remux:
        return f"{resolution} Remux"
    if (snapshot.media_source or "").casefold() == USER_LOWEST_SOURCE:
        return f"{resolution} 最低档（人工标注）"
    return f"{resolution} {snapshot.media_source or '片源未知'}"


def upgrade_target_label(spec: RuleSetSpec) -> str | None:
    """洗版目标的人话标签（"2160p Remux"）；未配置洗版返回 None。"""
    if spec.upgrade_source is None:
        return None
    resolution, _ = _target(spec)
    return f"{resolution} {_TARGET_SOURCE_LABEL[spec.upgrade_source.value]}"

def provably_below_cutoff(snapshot: QualitySnapshot | None, spec: RuleSetSpec) -> bool:
    """该单元是否**可证明**低于洗版目标（调度口径，quality-upgrade.md §2.4）。

    只有可证明"还差着"的单元才参与洗版排期——证明不了的（快照缺失、
    分辨率位次未知、同分辨率但片源未知）一律安静，不打扰站点。
    """
    target = target_vector(spec)
    if target is None or snapshot is None:
        return False
    return compare_ladder(ladder_vector(snapshot, spec), target) == -1


def provably_at_cutoff(snapshot: QualitySnapshot | None, spec: RuleSetSpec) -> bool:
    """该单元是否**可证明**已达（或超过）洗版目标（体检报告口径，§13.2）。

    与 provably_below_cutoff 成对但不互补：两者都证明不了的第三态
    （分辨率位次未知、同分辨率但片源未知）体检报告要如实展示为
    「无法确认」，不能冒充"已达目标"。
    """
    target = target_vector(spec)
    if target is None or snapshot is None:
        return False
    return compare_ladder(ladder_vector(snapshot, spec), target) in (0, 1)


def compare_upgrade(
    candidate: TorrentCandidate, snapshot: QualitySnapshot, spec: RuleSetSpec
) -> UpgradeVerdict:
    """洗版判定：候选是否构成对当前版本的**严格**升级（quality-upgrade.md §5）。

    前提：候选已通过规则组硬过滤（evaluate_rules）。三个否定出口 + 一个
    肯定出口，reason_text 为完整中文句子。

    判定链与 ``provably_*`` 共用 ``compare_ladder``，本函数只额外负责把
    "在哪一位上定的序"翻译成人话——每次拒绝永远只解释一个维度。
    """
    if spec.upgrade_source is None:
        raise ValueError("规则组未配置洗版目标，不应调用洗版比较")

    current_label = quality_label(snapshot)
    candidate_label = quality_label(candidate.attrs)
    reject = partial(
        _upgrade_reject, current_label=current_label, candidate_label=candidate_label
    )

    snapshot_vector = ladder_vector(snapshot, spec)
    candidate_vector = ladder_vector(candidate.attrs, spec)

    # 首位（分辨率）任一侧未知 → 整条比较不成立。单列出来是因为它的两句
    # 文案要分别点名是基线还是候选，且基线未知在语义上先于候选未知
    if snapshot_vector[0] is None:
        return reject("upgrade_not_comparable", "无法识别当前版本的分辨率，洗版比较不成立")
    if candidate_vector[0] is None:
        return reject(
            "upgrade_not_comparable",
            f"无法识别候选的分辨率（{candidate.attrs.resolution or '未标注'}），洗版比较不成立",
        )

    # 停止线：当前版本已达洗版目标
    target = target_vector(spec)
    if target is not None and compare_ladder(snapshot_vector, target) in (0, 1):
        return reject(
            "upgrade_at_cutoff",
            f"当前版本 {current_label} 已达到洗版目标（{upgrade_target_label(spec)}），不再洗版",
        )

    # 升级判定：字典序严格更优才算升级
    result, position = compare_ladder_at(candidate_vector, snapshot_vector)
    if result == 1:
        return UpgradeVerdict(
            accepted=True, current_label=current_label, candidate_label=candidate_label
        )
    dimension = _LADDER_LABELS[position] if position < len(_LADDER_LABELS) else ""
    # 截断/定序发生在第 position 位，意味着它之前的位全部打平——说出来，
    # 用户才知道"差就差在这一个维度上"
    prior = f"{'、'.join(_LADDER_LABELS[:position])}相同，" if position else ""
    if result is None:
        # 截断位上是哪一侧未知，文案就点名哪一侧——用户据此知道该修数据还是等资源
        if candidate_vector[position] is None:
            return reject(
                "upgrade_not_comparable",
                f"{prior}无法识别候选的{dimension}"
                f"（{_dimension_text(candidate.attrs, position)}），无法证明是升级",
            )
        return reject(
            "upgrade_not_comparable",
            f"{prior}当前版本{dimension}无法识别，无法证明候选更好",
        )
    if result == 0:
        return reject(
            "upgrade_not_better",
            f"候选 {candidate_label} 与当前版本 {current_label} 在可比维度上完全相同，不构成升级",
        )
    return reject(
        "upgrade_not_better",
        f"{prior}候选 {candidate_label} 的{dimension}位次不高于当前版本 {current_label}，"
        "不构成升级",
    )


def _dimension_text(attrs: QualitySnapshot | TorrentAttrs, position: int) -> str:
    """截断位上该资源的原始标注值，供"无法识别 X（未标注）"文案使用。"""
    if position == 0:
        return attrs.resolution or "未标注"
    return attrs.media_source or "未标注"


def _upgrade_reject(
    code: str, text: str, current_label: str, candidate_label: str
) -> UpgradeVerdict:
    return UpgradeVerdict(
        accepted=False,
        reason_code=code,
        reason_text=text,
        current_label=current_label,
        candidate_label=candidate_label,
    )


# ---------------------------------------------------------------------------
# 质量快照构造（quality-upgrade.md §4.1：实测优先，出处采信名称）
# ---------------------------------------------------------------------------

# probe 的 HDR 标签 → enrich 词表值域（两侧命名空间在此消化并单测锁死）
_PROBE_HDR_TO_VOCAB = {
    "dolby vision": "DV",
    "hdr10+": "HDR10+",
    "hdr10": "HDR10",
    "hlg": "HLG",
}


def build_snapshot(
    name_attrs: TorrentAttrs | QualitySnapshot | None,
    *,
    probed: bool = False,
    probe_resolution: str | None = None,
    probe_hdr_label: str | None = None,
    probe_bit_rate: int | None = None,
) -> QualitySnapshot:
    """按"可实测性分层"构造质量快照。

    - ``name_attrs``：名称解析来源（投递时的 TorrentAttrs 快照，或文件名
      enrich 结果）——出处维度（media_source/remux/release_group）的唯一来源；
    - ``probed=True`` 表示文件经过 ffprobe 实测：resolution/hdr 以实测为准
      （probe 的 hdr=None 是**测得 SDR**，不是未知，因此覆盖为空列表）；
      实测拿不到分辨率（探测失败）时回落名称值；
    - ``probed=False``（纯名称路径）：resolution/hdr 也取名称值。
    """
    resolution = name_attrs.resolution if name_attrs else None
    hdr = list(name_attrs.hdr) if name_attrs else []
    if probed:
        resolution = probe_resolution or resolution
        mapped = _PROBE_HDR_TO_VOCAB.get((probe_hdr_label or "").casefold())
        hdr = [mapped] if mapped else []
    return QualitySnapshot(
        resolution=resolution,
        media_source=name_attrs.media_source if name_attrs else None,
        remux=bool(name_attrs.remux) if name_attrs else False,
        release_group=name_attrs.release_group if name_attrs else None,
        hdr=hdr,
        bit_rate=probe_bit_rate,
    )
