"""匹配内核的输入/输出契约。

- ``RuleSetSpec``：``rule_set.spec`` JSON 列的 schema，纯参数包（判断在 rules.py）。
  所有维度可缺省=不限，加维度只需加带默认值的字段，无需迁移。
- ``TorrentCandidate`` / ``MediaIdentity``：内核输入，由 services 层分别从
  ``site_torrent`` 与 ``media_item`` 行构造——内核不碰数据库。
- ``IdentityMatch`` / ``RuleVerdict``：两级判定的输出。

契约冻结于 docs/design/subscription-p4.md 第 1 节，P4 编排层按此消费。
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from movieclaw_enrich.models import TorrentAttrs
from movieclaw_enrich.vocab import PLATFORM_IDS

logger = logging.getLogger(__name__)

# HDR 值域，按"越靠前越高"的内置顺序排列（仅作编辑器初始顺序与旧字段换算用，
# 实际偏好序以用户列表为准）。"SDR" 是哨兵值，对应资源未标注任何 HDR 格式。
HDR_LEVELS: tuple[str, ...] = ("DV", "HDR10+", "HDR10", "HLG", "SDR")

# 高格式蕴含低格式：DV Profile 8 自带 HDR10 基础层，HDR10+ 是 HDR10 的动态
# 元数据扩展。不建模这层关系的话，"偏好 HDR10 胜过 DV"的用户（播放设备对 DV
# 支持差）会把一个**已经含 HDR10 基础层**的文件判成低位次，白洗一次
# （quality-upgrade.md §14.8）。DV Profile 5 确实不含 HDR10 基础层，但标题与
# probe 都区分不出 profile——按"能播 HDR10"这侧保守建模，错的方向是少洗一次。
_HDR_IMPLIES: dict[str, set[str]] = {"DV": {"HDR10"}, "HDR10+": {"HDR10"}}

# 别名：解析器与用户都可能给出值域里没有的写法，归一到最接近的那一档。
# 泛指 `HDR`（`2160p WEB-DL … HDR H.265` 是 4K WEB 最常见的命名之一，enrich
# 会原样产出）既不是"未标注"也不属于任何具体格式，正好落在 SDR 与具体格式的
# 缝里：不归一的话它是一个白名单**永远装不下**的值，非空白名单会把这一大类
# 4K 资源无条件拒掉且用户无从放行。HDR10 是 HDR 的基线格式，绝大多数只标
# `HDR` 的发布就是 HDR10，按它算是无感的正确行为（issue #244）。
_HDR_ALIASES: dict[str, str] = {"HDR": "HDR10"}


def _canonical_hdr(value: str) -> str:
    """HDR 值归一：统一大写并展开别名。"""
    upper = value.upper()
    return _HDR_ALIASES.get(upper, upper)


def hdr_values(tags: list[str]) -> set[str]:
    """资源**声明**的 HDR 格式集合；未标注任何格式 = {"SDR"}。过滤用。"""
    return {_canonical_hdr(tag) for tag in tags} if tags else {"SDR"}


def hdr_effective_values(tags: list[str], allowed: Collection[str]) -> set[str]:
    """资源"能当什么格式用"的集合（含蕴含展开）。**排序用**。

    与 ``hdr_values`` 分开是因为过滤与排序回答的是两个问题：
    - 要不要（过滤）：**你列出的就是你接受的全部格式**，资源带了没列出的格式
      就不要——白名单对多值属性必须这么判，否则「排除 DV」会被一个同时标了
      HDR10 的 DV 资源绕过去（"只要 A"表达不了"排除 B"）；
    - 谁更好（排序）：DV 文件自带 HDR10 基础层，按它**能播的最高格式**算位次，
      免得偏好 HDR10 的用户为一个已经含 HDR10 的文件白洗一次（§14.8）。
    展开只对用户本来就接受的格式生效，排序因此不会把被排除的格式抬进来。
    """
    values = hdr_values(tags)
    accepted = {value.upper() for value in allowed}
    for tag in list(values):
        if tag in accepted:
            values |= _HDR_IMPLIES.get(tag, set())
    return values


# 洗版阶梯的值域与缺省。缺省二元组 = quality-upgrade.md §2 的原样行为，
# 存量规则组因此逐位等价、零迁移。
_LADDER_DIMENSION_VALUES: frozenset[str] = frozenset(
    {"resolution", "source", "hdr", "video_codec", "platform"}
)
_DEFAULT_UPGRADE_LADDER: tuple[str, ...] = ("resolution", "source")

# 片源档白名单的值域（MediaSourceTier 的字符串值；定义在下方枚举之后不便引用，
# 这里按值列出，两者由 test_models 锁死一致）
_MEDIA_SOURCE_TIER_VALUES: frozenset[str] = frozenset(
    {"remux", "blu-ray", "web-dl", "rip", "tv"}
)


def _known_hdr_levels(values: list[str]) -> list[str]:
    """保序去重地筛出值域内的 HDR 值（大小写不敏感，别名先归一）。

    值域外的值仍然丢弃（读取路径永远宽容，与平台/片源档同一取向），但要留一条
    日志：静默吞掉会让用户按文档配出来的东西和自己以为的不一样，且毫无提示。
    """
    known = {v.casefold(): v for v in HDR_LEVELS}
    out: list[str] = []
    for value in values:
        canon = known.get(_canonical_hdr(value).casefold())
        if canon is None:
            logger.warning(
                "规则组里的 HDR 值 %r 不在值域（%s）内，已忽略——该值不会产生任何约束",
                value,
                "/".join(HDR_LEVELS),
            )
        elif canon not in out:
            out.append(canon)
    return out


def _hdr_from_legacy(hdr: HdrPolicy, dv: DvPolicy) -> tuple[list[str], list[str]]:
    """旧三态组合 → (白名单, 黑名单)（quality-upgrade.md §14.8 的对照表）。

    白/黑两份而不是一份：HDR 是**多值属性**，"必须含 DV"（任一命中）与
    "排除 DV"（命中即拒）是两个方向，单靠白名单表达不了其中之一——这也正是
    平台维度当初就分白/黑两个字段的原因，两者形状一致。

    ``hdr=forbid`` 与 ``dv=require`` 的历史矛盾组合不再报错（读取路径必须
    永远宽容，§16.4）：排除项更强，按 ``hdr=forbid`` 解释为「只要 SDR」。
    """
    family = [v for v in HDR_LEVELS if v != "SDR"]
    if hdr is HdrPolicy.FORBID:
        return ["SDR"], []
    if dv is DvPolicy.REQUIRE:
        return ["DV"], []
    if hdr is HdrPolicy.REQUIRE:
        if dv is DvPolicy.FORBID:
            return [v for v in family if v != "DV"], ["DV"]
        return family, []
    if dv is DvPolicy.FORBID:
        return [], ["DV"]  # 不限 HDR，只排除 DV
    return [], []


def _legacy_from_hdr(levels: list[str], block: list[str]) -> tuple[HdrPolicy, DvPolicy]:
    """(白名单, 黑名单) → 最接近的旧三态组合（仅供回退兼容，丢失顺序信息）。"""
    dv = (
        DvPolicy.FORBID
        if "DV" in block
        else DvPolicy.REQUIRE
        if levels == ["DV"]
        else DvPolicy.ANY
    )
    if not levels:
        return HdrPolicy.ANY, dv
    if levels == ["SDR"]:
        return HdrPolicy.FORBID, DvPolicy.ANY
    if "SDR" in levels:
        return HdrPolicy.ANY, dv  # 既收 SDR 又收 HDR：旧字段表达不了，退回不限
    return HdrPolicy.REQUIRE, dv


def _known_platforms(values: list[str]) -> list[str]:
    """保序去重地筛出词表里认识的平台规范值（大小写不敏感）。"""
    seen: list[str] = []
    for value in values:
        canon = value.casefold()
        if canon in PLATFORM_IDS and canon not in seen:
            seen.append(canon)
    return seen


class HdrPolicy(StrEnum):
    """HDR 三态要求（判断整个 HDR 家族：HDR10/HDR10+/HLG/DV/…）。

    DV（杜比视界）也属于 HDR 家族，因此本轴对它同样生效——这是历史语义，
    存量规则组依赖它（如"排除 HDR"的 SDR 用户不该收到纯 DV 资源）。
    需要单独控制 DV 时用正交的 ``DvPolicy``（``RuleSetSpec.dv``），
    如"必须 HDR 但不要 DV" = ``hdr=require + dv=forbid``。
    """

    ANY = "any"  # 不限（默认）
    REQUIRE = "require"  # 必须是 HDR（任意 HDR 家族标记，含 DV）
    FORBID = "forbid"  # 必须不是 HDR（任何 HDR 家族标记都排除，含 DV）


class DvPolicy(StrEnum):
    """DV（杜比视界）三态要求，与 ``HdrPolicy`` 正交的独立轴。

    发布名常见 ``HDR.DV`` 双标（DV Profile 8 自带 HDR10 基础层），单一枚举
    无法同时表达"要不要 HDR"与"要不要 DV"，故拆成两轴各自判断。
    """

    ANY = "any"  # 不限（默认）
    REQUIRE = "require"  # 必须含 DV 标记（可同时带 HDR10 等基础层）
    FORBID = "forbid"  # 排除含 DV 标记的资源（不影响其他 HDR 格式）


class UpgradeSource(StrEnum):
    """洗版目标片源档（规则组的"洗到哪一档"选项，docs/design/quality-upgrade.md §3.2）。

    只暴露三个用户语言里的档位；完整的片源档阶梯（含 Rip/TV 录制类）内置在
    decision.py，不暴露配置。
    """

    WEB_DL = "web-dl"  # 洗到 WEB-DL
    BLU_RAY = "blu-ray"  # 洗到蓝光重编码
    REMUX = "remux"  # 洗到原盘 Remux


class MediaSourceTier(StrEnum):
    """片源档（规则组片源白名单的值域，docs/design/quality-upgrade.md §2.1）。

    单位是**档**而不是词表原值（Blu-ray / UHD Blu-ray / BDRip …）：用户谈论
    版本时说的就是"蓝光""WEB-DL"，档内的写法差异（UHD Blu-ray 与 Blu-ray）
    实际由分辨率那一维表达；按档白名单还天然免疫词表演进——新增的写法自动
    落进已有档，不需要用户回来补勾。

    前三个值与 ``UpgradeSource`` **刻意同值**：洗版终点必须是白名单里的一档，
    两个字段说同一种语言才能互相校验（``_validate_upgrade_target``）。
    """

    REMUX = "remux"  # 原盘 Remux（T5）
    BLU_RAY = "blu-ray"  # 光盘重编码：Blu-ray / UHD Blu-ray / HD-DVD（T4）
    WEB_DL = "web-dl"  # 流媒体原流（T3）
    RIP = "rip"  # 二压 Rip：WEBRip / BDRip / HDRip / DVDRip（T2）
    TV = "tv"  # 电视录制与 DVD：HDTV / HDTVRip / TVRip / DVD（T1）


class HrUnknownPolicy(StrEnum):
    """H&R 状态未知（NULL）时的处理策略。

    H&R 是三态：True=有考核，False=站点标注无考核，NULL=站点不提供/未适配。
    缺席不代表没有考核，因此把未知当什么处理必须显式选择。
    """

    LENIENT = "lenient"  # 宽松：未知视作无考核，放行（默认）
    STRICT = "strict"  # 保守：未知视作有考核，拒绝


class RuleSetSpec(BaseModel):
    """规则组参数包（``rule_set.spec`` 的 JSON schema）。

    过滤维度全部可缺省（空列表/None/False = 不限该维度）；
    ``resolutions`` 的**列表顺序即偏好顺序**——排在前面的分辨率评分更高，
    这是第一版唯一暴露给用户的评分参数（评分公式内置，见 rules.py）。
    """

    # -- 硬性过滤 ----------------------------------------------------------
    resolutions: list[str] = Field(
        default_factory=list, description="允许的分辨率（如 2160p/1080p）；顺序即偏好；空=不限"
    )
    # 片源与分辨率是用户谈论"版本好坏"时实际使用的两个主轴（§2.2），因此
    # 两者的偏好序都参与候选选优（rules._score）；其余维度只做过滤，顺序仅在
    # 洗版阶梯上有意义。
    media_sources: list[str] = Field(
        default_factory=list,
        description="允许的片源档（remux/blu-ray/web-dl/rip/tv）；顺序即偏好；空=不限",
    )
    video_codecs: list[str] = Field(
        default_factory=list, description="允许的视频编码（如 x265/x264）；空=不限"
    )
    # 平台与制作组是正交的两个来源维度，各自独立判断（都配 = 都要满足）：
    # 不引入"任一命中即可"的组合开关——每个维度独立才能一句话解释拒绝原因。
    platforms: list[str] = Field(
        default_factory=list,
        description="流媒体平台白名单（规范值，如 netflix/disney_plus）；空=不限。"
        "列表顺序即偏好序，为洗版位次预留（§14），当前不参与候选评分",
    )
    platforms_block: list[str] = Field(
        default_factory=list, description="流媒体平台黑名单（规范值）"
    )
    release_groups_allow: list[str] = Field(
        default_factory=list, description="制作组白名单；空=不限"
    )
    release_groups_block: list[str] = Field(
        default_factory=list, description="制作组黑名单"
    )
    # HDR：硬过滤与偏好合并成**一条有序列表**（列表即白名单，顺序即偏好，
    # 首项即洗版终点）。旧的 hdr/dv 两个三态字段降级为兼容层——读取时若
    # hdr_levels 为空则由它们推导，写入时反向回填等价值，让"应用内更新回退到
    # 旧版本"仍能读懂（不能原地改 hdr 的类型：同名不同型会让旧代码
    # model_validate 直接抛错、规则组全线解析失败，§14.8）。
    hdr_levels: list[str] = Field(
        default_factory=list,
        description='HDR 白名单兼偏好序（值域 DV/HDR10+/HDR10/HLG/SDR，"SDR"=未标注，'
        "资源只标注泛指 HDR 时按 HDR10 计）；任一命中即过，顺序即偏好；空=不限",
    )
    hdr_block: list[str] = Field(
        default_factory=list, description="HDR 黑名单；资源带其中任一格式即排除"
    )
    hdr: HdrPolicy = Field(
        default=HdrPolicy.ANY, description="[兼容层] HDR 三态；hdr_levels 为空时才消费"
    )
    dv: DvPolicy = Field(
        default=DvPolicy.ANY, description="[兼容层] DV 三态；hdr_levels 为空时才消费"
    )
    free_only: bool = Field(default=False, description="只接受当前免费（free）的种子")
    min_seeders: int | None = Field(default=None, description="做种数下限；None=不限")
    # 体积区间按"每集均摊"评估：整季包用总体积 ÷ 集数比较，避免整季包被误杀
    size_min_mb: int | None = Field(default=None, description="单集体积下限（MB）；None=不限")
    size_max_mb: int | None = Field(default=None, description="单集体积上限（MB）；None=不限")
    exclude_hr: bool = Field(default=False, description="排除 H&R 考核种子")
    hr_unknown_policy: HrUnknownPolicy = Field(
        default=HrUnknownPolicy.LENIENT, description="H&R 未知时的策略"
    )
    # 字幕/音轨语言要求（BCP 47，前缀匹配：要求 "zh" 命中 zh/zh-Hans/zh-Hant，
    # 要求 "zh-Hans" 只命中简体）。声明式匹配：任一命中即过；规则非空而资源
    # **未声明**该轴时按不合格处理——与 resolution/codec 的未知语义一致，
    # 自动选种精确率优先。识别来源见 movieclaw_enrich.lang_decl（torrent-ner v2）
    subtitle_languages_require: list[str] = Field(
        default_factory=list,
        description="要求的字幕语言（任一命中即过，如 [\"zh\"]=要中文字幕）；空=不限",
    )
    audio_languages_require: list[str] = Field(
        default_factory=list,
        description="要求的音轨语言（任一命中即过，如 [\"cmn\"]=要国语）；空=不限",
    )

    # -- 洗版（docs/design/quality-upgrade.md §3.2）--------------------------
    # 两个字段都缺省 = 不洗版。洗版目标 = (cutoff_resolution 或 resolutions
    # 首选或 1080p, upgrade_source)，比较用 decision.py 的档位阶梯。
    upgrade_source: UpgradeSource | None = Field(
        default=None,
        description="洗版目标片源档（web-dl/blu-ray/remux）；None=不洗版",
    )
    cutoff_resolution: str | None = Field(
        default=None,
        description="洗版目标分辨率；None=取 resolutions 首选（都缺省则 1080p，"
        "保守缺省避免把用户意外带进 4K 的磁盘占用）",
    )
    upgrade_ladder: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_UPGRADE_LADDER),
        description="参与洗版比较的维度及其优先级（顺序即位次）；"
        "值域 resolution/source/video_codec/platform，缺省即 §2 的二元组",
    )
    upgrade_keep_old: bool = Field(
        default=False,
        description="洗到新版本后保留旧版本（多版本共存，收藏家模式）；"
        "缺省 False=旧版本进回收站（library-file-recycle.md §9）",
    )

    # -- 预留（本期不消费，字段先占位保证 spec 向前兼容）--------------------
    sites: list[str] = Field(
        default_factory=list, description="[预留] 站点白名单；空=全部启用站点"
    )

    @model_validator(mode="after")
    def _normalize_upgrade_ladder(self) -> RuleSetSpec:
        """阶梯维度归一：丢弃未知值与重复项；全被丢弃时回落到缺省二元组。

        与平台白名单同一取向——读取路径永远宽容（§16.4）。回落而不是留空是
        因为**空阶梯没有任何一位可比**：比较恒等价，于是没有候选构成升级、
        所有单元又都算"已达目标"，洗版静默全停。回落到缺省至少行为正确。
        """
        seen: list[str] = []
        for dim in self.upgrade_ladder:
            if dim in _LADDER_DIMENSION_VALUES and dim not in seen:
                seen.append(dim)
        self.upgrade_ladder = seen or list(_DEFAULT_UPGRADE_LADDER)
        return self

    @model_validator(mode="after")
    def _normalize_platforms(self) -> RuleSetSpec:
        """平台值归一到规范值；词表外的值**丢弃而非报错**。

        规则组 spec 的读取路径必须永远宽容：词表演进（合并平台、改规范值）
        或人工写错一个值，都不该让 ``model_validate`` 抛错——那会让整个规则组
        解析失败、匹配管线全线停摆，波及的远不止平台这一个维度。
        丢弃的后果只是该值不再产生约束，量级与"少配了一个条件"相同。
        """
        self.platforms = _known_platforms(self.platforms)
        self.platforms_block = _known_platforms(self.platforms_block)
        return self

    @model_validator(mode="after")
    def _normalize_hdr(self) -> RuleSetSpec:
        """HDR 归一与双写：新键优先，同时回填等价旧键。

        旧的互斥校验器（hdr=forbid + dv=require）随之删除——矛盾组合在列表
        语义下根本写不出来，这正是合并成一个字段的收益之一。
        """
        levels = _known_hdr_levels(self.hdr_levels)
        block = _known_hdr_levels(self.hdr_block)
        if not levels and not block:
            levels, block = _hdr_from_legacy(self.hdr, self.dv)
        self.hdr_levels, self.hdr_block = levels, block
        # 反向回填：能精确表达就精确写，表达不了（顺序、混合集合）就写最宽松的
        # 等价值。回退到旧版本后行为退化为粗粒度，**不精确但不崩、不危险**。
        self.hdr, self.dv = _legacy_from_hdr(levels, block)
        return self

    @model_validator(mode="after")
    def _normalize_media_sources(self) -> RuleSetSpec:
        """片源档归一：丢弃未知值与重复项（与平台白名单同一取向，读取路径永远宽容）。"""
        seen: list[str] = []
        for value in self.media_sources:
            key = value.casefold()
            if key in _MEDIA_SOURCE_TIER_VALUES and key not in seen:
                seen.append(key)
        self.media_sources = seen
        return self

    @model_validator(mode="after")
    def _validate_upgrade_target(self) -> RuleSetSpec:
        """洗版目标必须是规则组自己接受的档，否则永远洗不到。"""
        if (
            self.cutoff_resolution is not None
            and self.resolutions
            and self.cutoff_resolution.casefold()
            not in {r.casefold() for r in self.resolutions}
        ):
            raise ValueError(
                f"洗版目标分辨率 {self.cutoff_resolution} 不在允许的分辨率列表中，"
                "规则组自己都不接受的分辨率无法作为洗版目标"
            )
        if (
            self.upgrade_source is not None
            and self.media_sources
            and self.upgrade_source.value not in self.media_sources
        ):
            raise ValueError(
                f"洗版目标片源 {self.upgrade_source.value} 不在允许的片源列表中，"
                "规则组自己都不接受的片源无法作为洗版目标"
            )
        return self


# ---------------------------------------------------------------------------
# 内核输入（services 层构造，内核零 IO）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TorrentCandidate:
    """候选种子：``site_torrent`` 行在内核视角下的只读切片。"""

    site_id: str
    torrent_id: str
    title: str
    subtitle: str
    attrs: TorrentAttrs
    imdb_id: str | None = None
    douban_id: str | None = None
    size_bytes: int | None = None
    seeders: int | None = None
    is_free: bool | None = None
    hit_and_run: bool | None = None
    download_url: str | None = None
    # 发布时间是整季包覆盖范围的物理上限：种子不可能包含发布之后才播出的集。
    # 真实教训——2025-12 发布的同名他剧整季包，曾把 2026-06 才开播订阅的
    # 全季 10 集（含 4 集未播出）标记为已投递。None=未知（按评估时刻处理）。
    publish_time: datetime | None = None


@dataclass(frozen=True)
class MediaIdentity:
    """媒体条目在内核视角下的身份切片（来自 ``media_item`` + 季清单）。"""

    kind: str  # movie / tv（MediaKind 的字符串值）
    year: int | None
    aliases: tuple[str, ...]
    imdb_id: str | None = None
    douban_id: str | None = None
    # 已知季号（含特别季 0）：单季剧可为"无季号集"兜底推断季，整季包展开时校验
    season_numbers: tuple[int, ...] = ()
    # 已知季名（media_season.name，如"南洋拾光季"）：国综惯例把季名并进片名
    # （"中餐厅·南洋拾光季"），"别名+季名"的组合等式是零歧义的身份信号
    season_titles: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 内核输出
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityMatch:
    """身份匹配结果：这个种子是这个条目的哪些单元。

    - ``episodes``：明确的 (season, episode) 集合；电影恒为 {(0, 0)}；
    - ``pack_seasons``：整季包覆盖的季号（种子只标季不标集），消费方展开成
      该季全部未满足工单；
    - ``is_complete_series``：全集包（连季号都没有、仅标注"全集/合集"），
      消费方展开到所有勾选/追踪中的季；
    - ``is_pack``：选优优先级输入（整季包优先于单集——已确认决策）。
      集列表被完整枚举的合集（如"全 40 集"）也算 pack。
    """

    episodes: frozenset[tuple[int, int]] = frozenset()
    pack_seasons: frozenset[int] = frozenset()
    is_complete_series: bool = False
    is_pack: bool = False
    confidence: str = "title_year"  # exact_id / title_year / title_only（观察用）
    matched_alias: str | None = None


@dataclass(frozen=True)
class RuleVerdict:
    """规则过滤结果。拒绝时 reason_text 是完整中文句子，直接进活动流水。"""

    accepted: bool
    score: int = 0
    reason_code: str | None = None
    reason_text: str | None = None


# 快照结构版本：新增可比维度时 +1，驱动存量快照重算（quality-upgrade.md §16.3）。
# v1 = 历史快照（无版本键，只有 resolution/media_source/remux/release_group/hdr/bit_rate）
# v2 = 增加 video_codec 与 platforms
# v3 = 片源维度新增原盘档 T6（DISC_SOURCE）。不是新增维度，但存量快照里的
#      原盘单元此前被记成 Blu-ray(T4)/未知，重算才能把它们抬到正确的档位
#      ——不重算的话，一个 Remux 候选仍会被判成升级并把原盘洗掉（issue #163）
SNAPSHOT_VERSION = 3


class QualitySnapshot(BaseModel):
    """单元当前版本的质量快照（``wanted_item.quality`` JSON 列的 schema）。

    洗版比较的唯一基线（docs/design/quality-upgrade.md §4）。取值原则：
    **能实测的维度以 ffprobe 为准，实测不出的出处维度采信名称解析**——
    resolution/hdr/bit_rate 来自 probe（值域归一到 enrich 词表），
    media_source/remux/release_group 来自种子名或文件名 enrich。
    所有字段可缺省 = 未知（三态铁律：未知不当已知用，见 decision.py 的
    部分可比规则）。
    """

    # 结构版本；**缺省是 1 而不是当前版本**——老行没有这个键，读出来必须
    # 显示为"落后"，回填任务才会把新维度补上（写入方 build_snapshot 负责盖章）
    v: int = 1
    resolution: str | None = None  # 归一化分辨率（2160p/1080p/…）；probe 优先
    media_source: str | None = None  # 片源（WEB-DL/Blu-ray/…）；名称来源
    remux: bool = False  # 是否原盘 Remux；名称来源（缺席即否定，同 TorrentAttrs）
    release_group: str | None = None  # 制作组；仅展示
    hdr: list[str] = Field(default_factory=list)  # HDR 格式（DV/HDR10/…）；仅展示
    video_codec: str | None = None  # 视频编码；probe 定族、名称定写法（见 build_snapshot）
    platforms: list[str] = Field(default_factory=list)  # 流媒体平台；名称来源
    bit_rate: int | None = None  # 实测码率（bps）；留证据供详情页展示，不参与档位


@dataclass(frozen=True)
class UpgradeVerdict:
    """洗版判定结果（docs/design/quality-upgrade.md §5）。

    拒绝码只有三个：``upgrade_not_comparable``（关键维度无法识别）、
    ``upgrade_at_cutoff``（当前版本已达洗版目标）、``upgrade_not_better``
    （候选档位不高于当前版本）。reason_text 与 RuleVerdict 同约定——
    完整中文句子；接受时 current_label/candidate_label 供"从 X 洗到 Y"
    的活动文案使用。
    """

    accepted: bool
    reason_code: str | None = None
    reason_text: str | None = None
    current_label: str | None = None  # 当前版本档位中文标签（如 "1080p WEB-DL"）
    candidate_label: str | None = None  # 候选档位中文标签（如 "1080p Remux"）
