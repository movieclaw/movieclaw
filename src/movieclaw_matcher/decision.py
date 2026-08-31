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
from movieclaw_enrich.vocab import codec_family
from movieclaw_matcher.models import (
    SNAPSHOT_VERSION,
    IdentityMatch,
    QualitySnapshot,
    RuleSetSpec,
    RuleVerdict,
    TorrentCandidate,
    UpgradeVerdict,
    hdr_effective_values,
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

# 原盘（BDMV / VIDEO_TS / ISO 完整盘）：片源阶梯的顶档 T6，高于 Remux。
# Remux 本来就是**从原盘剥出来的**——把原盘压在 Remux 之下（此前只能标成
# Blu-ray T4）会让一个 Remux 候选被判成升级，而默认 upgrade_keep_old=False
# 会把原盘送进回收站，这是把更好的文件洗掉（issue #163 评论区实测：
# 918 部电影的库里 339 个原盘，其中 199 个 BDMV 目录已探测出分辨率、
# 真的暴露在这条路径上）。
#
# 该值只由**结构证据**写入（台账 container 为 bluray/dvd/iso，见
# LibraryFile.is_disc），enrich 词表永远解析不出它——种子标题里的
# "Blu-ray" 说的是压制来源，不是"这是一张完整的盘"。
DISC_SOURCE = "Disc"
_DISC_TIER = 6

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
    # T6 原盘：完整盘结构（BDMV / VIDEO_TS / ISO），高于从它剥出来的 Remux
    DISC_SOURCE.casefold(): _DISC_TIER,
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

# 片源档选项（MediaSourceTier 的值）→ 内置档位 / 展示名。前三个与洗版目标同值，
# 多出的 rip/tv 两档只在片源白名单里出现（洗版终点没人会设成"洗到 Rip"）。
_MEDIA_SOURCE_CHOICE_TIER = {**_TARGET_SOURCE_TIER, "rip": 2, "tv": 1}
MEDIA_SOURCE_TIER_LABEL = {
    "remux": "Remux",
    "blu-ray": "蓝光",
    "web-dl": "WEB-DL",
    "rip": "Rip 类",
    "tv": "电视录制类",
}


def media_source_rank(
    media_source: str | None, remux: bool, spec: RuleSetSpec
) -> int | None:
    """片源位次（越大越优）；未知或不在白名单内返回 ``None``（不可比）。

    两种模式，与分辨率那一维完全同构：
    - **没配 ``media_sources``**：用内置片源档（Remux T5 > 光盘 T4 > WEB-DL T3
      > Rip T2 > 录制 T1），即本模块一直以来的行为；
    - **配了**：位次由**用户的列表顺序**决定——省流党把 WEB-DL 排在蓝光前面，
      洗版与选优就都按他说的算（"偏好即优先级"，§14.3 对编码/平台/HDR 也是
      这个待遇）。

    不在白名单内的档返回 ``None`` 而不是"最低"，与 ``_resolution_ladder`` 的
    取向一字不差：位次未知时宁可让该单元安静，也不做数值猜测——把"白名单外"
    一律当最低，会让 [WEB-DL, 蓝光] 这种倒序偏好把已入库的 Remux 判成最低档
    而白洗一次，那是删掉更好的文件。两个例外是阶梯的两端哨兵：T0（人工标注
    "按最低档"）语义就是"低于一切"，T6（原盘）语义就是"高于一切"，两者都
    保留可比——原盘不在白名单里（白名单是"愿意下载哪类资源"，没人会去下载
    一张 40GB 的盘），但它必须能证明自己已经高于任何洗版目标，否则配了白名单
    的规则组会让原盘退回"不可比"，白白等在那里。
    """
    tier = source_tier(media_source, remux)
    if tier is None:
        return None
    if not spec.media_sources:
        return tier
    if tier == 0:  # USER_LOWEST_SOURCE：显式的"低于白名单里的一切"
        return 0
    if tier == _DISC_TIER:  # 原盘：显式的"高于白名单里的一切"
        return len(spec.media_sources) + 1
    ladder = [_MEDIA_SOURCE_CHOICE_TIER[value] for value in spec.media_sources]
    if tier not in ladder:
        return None
    return len(ladder) - ladder.index(tier)


def source_tier(media_source: str | None, remux: bool) -> int | None:
    """片源 → 档位；未知片源返回 None（不可比，绝不当最低档处理）。

    原盘（T6）压过 ``remux`` 布尔位：那个布尔位来自**名称解析**，而原盘值
    来自台账的结构证据（BDMV/VIDEO_TS/ISO）。一张目录名里带 "Remux" 的
    完整原盘仍然是原盘，不能因为名字里的一个词降到 T5。
    """
    tier = _SOURCE_TIER.get(media_source.casefold()) if media_source else None
    if tier == _DISC_TIER:
        return tier
    if remux:
        return _REMUX_TIER
    return tier


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
# 档位阶梯的维度（quality-upgrade.md §14.3）。位序由规则组的 ``upgrade_ladder``
# 决定，缺省 ("resolution", "source") 即 §2 的二元组。
_LADDER_LABELS: dict[str, str] = {
    "resolution": "分辨率",
    "source": "片源",
    "hdr": "HDR",
    "video_codec": "编码",
    "platform": "平台",
}

# 与 models._DEFAULT_UPGRADE_LADDER 同值：生效维度为空时的兜底
_DEFAULT_LADDER: tuple[str, ...] = ("resolution", "source")


def _rank_in(ladder: list[str], value: str | None) -> int | None:
    """值在偏好序里的位次（越大越优）；不在序列里 = 未知（不可比）。"""
    if not value:
        return None
    try:
        return len(ladder) - ladder.index(value)
    except ValueError:
        return None


def _codec_ladder(spec: RuleSetSpec) -> list[str]:
    """编码白名单 → **按族去重**的偏好序。

    UI 选一族会把三种等价写法都写进 video_codecs（x265/H.265/HEVC），它们
    必须塌缩成阶梯上的**一个**位次，否则同一族的不同写法会互相"升级"。
    """
    ladder: list[str] = []
    for value in spec.video_codecs:
        family = codec_family(value)
        if family and family not in ladder:
            ladder.append(family)
    return ladder


def effective_ladder(spec: RuleSetSpec) -> tuple[str, ...]:
    """实际参与比较的维度：**偏好列表为空的位自动跳过**（§14.3）。

    跳过而不是"当作未知"是关键——空列表当未知会让整条阶梯在该位截断、
    洗版全线哑火。resolution / source 恒有效：前者有内置默认偏好序，
    后者有内置片源档，都不依赖用户配置。
    """
    dims = tuple(
        dim
        for dim in spec.upgrade_ladder
        if not (dim == "hdr" and not spec.hdr_levels)
        and not (dim == "video_codec" and not spec.video_codecs)
        and not (dim == "platform" and not spec.platforms)
    )
    # 全被跳过时回落缺省二元组。空阶梯下没有任何一位可比：比较恒等价 ⇒
    # 没有候选构成升级，而所有单元又都算"已达目标"——720p HDTV 会被报成
    # 「已达 Remux 目标」，洗版静默全停且详情页还在误导用户。
    # 校验层的同名保护只挡得住"列表本身为空"，挡不住"列表里的维度全没配偏好"
    # （用户在洗版优先级里点掉分辨率和片源、只留一个没配的平台就是这样）
    return dims or _DEFAULT_LADDER


def _dimension_rank(
    item: QualitySnapshot | TorrentAttrs, dim: str, spec: RuleSetSpec
) -> int | None:
    if dim == "resolution":
        return resolution_rank(item.resolution, spec)
    if dim == "source":
        return media_source_rank(item.media_source, item.remux, spec)
    if dim == "hdr":
        # 取"能播的最高格式"位次：DV 文件自带 HDR10 基础层，偏好 HDR10 的
        # 规则组不该为它白洗一次（hdr_effective_values 的展开只对已接受的格式生效）
        values = hdr_effective_values(item.hdr, spec.hdr_levels)
        hdr_ranks = [r for v in values if (r := _rank_in(spec.hdr_levels, v)) is not None]
        return max(hdr_ranks) if hdr_ranks else None
    if dim == "video_codec":
        return _rank_in(_codec_ladder(spec), codec_family(item.video_codec))
    if dim == "platform":
        # 一个资源可带多个平台标记，取其中位次最高的那个
        ranks = [r for p in item.platforms if (r := _rank_in(spec.platforms, p)) is not None]
        return max(ranks) if ranks else None
    # 值域由 models._LADDER_DIMENSION_VALUES 把关，走到这里说明两者漂移了；
    # 显式炸掉好过悄悄按最后一个分支算（那会让新维度看起来"生效了"却全错）
    raise ValueError(f"未知的洗版阶梯维度：{dim}")


def _dimension_text(item: QualitySnapshot | TorrentAttrs, dim: str) -> str:
    """该资源在某维度上的原始标注值，供"无法识别 X（…）"文案使用。"""
    if dim == "resolution":
        return item.resolution or "未标注"
    if dim == "source":
        return item.media_source or "未标注"
    if dim == "hdr":
        return "/".join(item.hdr) or "未标注 HDR（按 SDR 处理）"
    if dim == "video_codec":
        return item.video_codec or "未标注"
    return "/".join(item.platforms) or "未标注"


def ladder_vector(
    item: QualitySnapshot | TorrentAttrs, spec: RuleSetSpec
) -> tuple[int | None, ...]:
    """档位向量：逐位的位次，``None`` = 该维度未知（不可比，见 §2.4）。"""
    return tuple(_dimension_rank(item, dim, spec) for dim in effective_ladder(spec))


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

    逐位取值（§14.3）：resolution 取 ``cutoff_resolution``（缺省=偏好首选），
    source 取 ``upgrade_source``，**其余维度直接取偏好列表首项**——只有
    分辨率与片源会成数量级地改变磁盘占用，才需要"接受但不主动洗"的区分。

    返回 None 表示"这个目标无法比较"（配置矛盾，校验器已拦，此处防御）——
    调用方一律按"证明不了"处理，不猜。
    """
    if spec.upgrade_source is None:
        return None
    vector: list[int] = []
    for dim in effective_ladder(spec):
        if dim == "resolution":
            target_resolution, _ = _target(spec)
            resolution = resolution_rank(target_resolution, spec)
            if resolution is None:
                return None
            vector.append(resolution)
        elif dim == "source":
            # 配了白名单就按用户序取位次（校验层保证终点档在白名单内）
            target_tier = _TARGET_SOURCE_TIER[spec.upgrade_source.value]
            if spec.media_sources:
                ladder = [_MEDIA_SOURCE_CHOICE_TIER[v] for v in spec.media_sources]
                if target_tier not in ladder:
                    return None
                vector.append(len(ladder) - ladder.index(target_tier))
            else:
                vector.append(target_tier)
        elif dim == "hdr":
            vector.append(len(spec.hdr_levels))  # 首项位次
        elif dim == "video_codec":
            vector.append(len(_codec_ladder(spec)))  # 首项位次
        else:
            vector.append(len(spec.platforms))
    return tuple(vector)


def _target(spec: RuleSetSpec) -> tuple[str, int]:
    """洗版目标 (分辨率, 片源档)。调用前提：spec.upgrade_source 已配置。"""
    resolution = (
        spec.cutoff_resolution
        or (spec.resolutions[0] if spec.resolutions else None)
        or _FALLBACK_CUTOFF_RESOLUTION
    )
    return resolution, _TARGET_SOURCE_TIER[spec.upgrade_source.value]


def quality_label(
    snapshot: QualitySnapshot | TorrentAttrs, spec: RuleSetSpec | None = None
) -> str:
    """档位的人话标签（"1080p Remux" / "2160p WEB-DL · x265 · Netflix"）。

    ``spec`` 给出时按生效阶梯补上编码/平台位——多维阶梯下，两个候选可能
    分辨率与片源都一样、只差在编码上，标签不带那一位就没法解释拒绝理由。
    """
    resolution = snapshot.resolution or "分辨率未知"
    if (snapshot.media_source or "").casefold() == DISC_SOURCE.casefold():
        head = f"{resolution} 原盘"
    elif snapshot.remux:
        head = f"{resolution} Remux"
    elif (snapshot.media_source or "").casefold() == USER_LOWEST_SOURCE:
        head = f"{resolution} 最低档（人工标注）"
    else:
        head = f"{resolution} {snapshot.media_source or '片源未知'}"
    if spec is None:
        return head
    parts = [head]
    dims = effective_ladder(spec)
    if "hdr" in dims and snapshot.hdr:
        parts.append("/".join(snapshot.hdr))
    if "video_codec" in dims and snapshot.video_codec:
        parts.append(snapshot.video_codec)
    if "platform" in dims and snapshot.platforms:
        parts.append("/".join(snapshot.platforms))
    return " · ".join(parts)


def upgrade_target_label(spec: RuleSetSpec) -> str | None:
    """洗版目标的人话标签（"2160p Remux · x265"）；未配置洗版返回 None。"""
    if spec.upgrade_source is None:
        return None
    resolution, _ = _target(spec)
    parts = [f"{resolution} {_TARGET_SOURCE_LABEL[spec.upgrade_source.value]}"]
    dims = effective_ladder(spec)
    if "hdr" in dims and spec.hdr_levels:
        parts.append(spec.hdr_levels[0])
    if "video_codec" in dims and spec.video_codecs:
        parts.append(spec.video_codecs[0])
    if "platform" in dims and spec.platforms:
        parts.append(spec.platforms[0])
    return " · ".join(parts)


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
    "在哪一位上定的序"翻译成人话——每次拒绝永远只解释一个维度（§14.7）。
    """
    if spec.upgrade_source is None:
        raise ValueError("规则组未配置洗版目标，不应调用洗版比较")

    current_label = quality_label(snapshot, spec)
    candidate_label = quality_label(candidate.attrs, spec)
    reject = partial(
        _upgrade_reject, current_label=current_label, candidate_label=candidate_label
    )

    dimensions = effective_ladder(spec)
    snapshot_vector = ladder_vector(snapshot, spec)
    candidate_vector = ladder_vector(candidate.attrs, spec)

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
    if result == 0:
        return reject(
            "upgrade_not_better",
            f"候选 {candidate_label} 与当前版本 {current_label} 在可比维度上完全相同，不构成升级",
        )
    dimension = dimensions[position]
    label = _LADDER_LABELS[dimension]
    # 定序/截断发生在第 position 位，意味着它之前的位全部打平——说出来，
    # 用户才知道"差就差在这一个维度上"
    prior = (
        f"{'、'.join(_LADDER_LABELS[d] for d in dimensions[:position])}相同，"
        if position
        else ""
    )
    if result is None:
        # 截断位上是哪一侧未知，文案就点名哪一侧——用户据此知道该修数据还是等资源
        if candidate_vector[position] is None:
            return reject(
                "upgrade_not_comparable",
                f"{prior}无法识别候选的{label}"
                f"（{_dimension_text(candidate.attrs, dimension)}），无法证明是升级",
            )
        return reject(
            "upgrade_not_comparable",
            f"{prior}当前版本{label}无法识别，无法证明候选更好",
        )
    return reject(
        "upgrade_not_better",
        f"{prior}候选 {candidate_label} 的{label}位次不高于当前版本 {current_label}，"
        "不构成升级",
    )


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
    probe_video_codec: str | None = None,
    probe_bit_rate: int | None = None,
) -> QualitySnapshot:
    """按"可实测性分层"构造质量快照。

    - ``name_attrs``：名称解析来源（投递时的 TorrentAttrs 快照，或文件名
      enrich 结果）——出处维度（media_source/remux/release_group）的唯一来源；
    - ``probed=True`` 表示文件经过 ffprobe 实测：resolution/hdr 以实测为准
      （probe 的 hdr=None 是**测得 SDR**，不是未知，因此覆盖为空列表）；
      实测拿不到分辨率（探测失败）时回落名称值；
    - ``probed=False``（纯名称路径）：resolution/hdr 也取名称值；
    - ``video_codec``：**实测定族、名称定写法**——probe 的 codec_name
      （hevc/h264）与标题写法（x265/HEVC）同族时保留名称值（信息量更大、
      与规则组配置同一命名空间），不同族或名称缺失时以实测为准（实测推翻
      名称）。两侧比较统一走 ``vocab.codec_family``，不需要额外映射层；
    - ``platforms``：名称唯一来源（文件本体测不出发行平台）。
    """
    resolution = name_attrs.resolution if name_attrs else None
    hdr = list(name_attrs.hdr) if name_attrs else []
    video_codec = name_attrs.video_codec if name_attrs else None
    if probed:
        resolution = probe_resolution or resolution
        mapped = _PROBE_HDR_TO_VOCAB.get((probe_hdr_label or "").casefold())
        hdr = [mapped] if mapped else []
        if probe_video_codec and (
            video_codec is None
            or codec_family(video_codec) != codec_family(probe_video_codec)
        ):
            video_codec = probe_video_codec
    return QualitySnapshot(
        v=SNAPSHOT_VERSION,
        resolution=resolution,
        media_source=name_attrs.media_source if name_attrs else None,
        remux=bool(name_attrs.remux) if name_attrs else False,
        release_group=name_attrs.release_group if name_attrs else None,
        hdr=hdr,
        video_codec=video_codec,
        platforms=list(name_attrs.platforms) if name_attrs else [],
        bit_rate=probe_bit_rate,
    )
