"""匹配内核的输入/输出契约。

- ``RuleSetSpec``：``rule_set.spec`` JSON 列的 schema，纯参数包（判断在 rules.py）。
  所有维度可缺省=不限，加维度只需加带默认值的字段，无需迁移。
- ``TorrentCandidate`` / ``MediaIdentity``：内核输入，由 services 层分别从
  ``site_torrent`` 与 ``media_item`` 行构造——内核不碰数据库。
- ``IdentityMatch`` / ``RuleVerdict``：两级判定的输出。

契约冻结于 docs/design/subscription-p4.md 第 1 节，P4 编排层按此消费。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from movieclaw_enrich.models import TorrentAttrs


class HdrPolicy(StrEnum):
    """HDR/DV 过滤策略。

    ``require`` / ``forbid`` 是已有规则组的持久化值，继续分别表示
    “必须 HDR（且不含 DV）”与“排除 HDR”，避免升级后改写用户配置。
    """

    ANY = "any"  # 不限（默认）
    REQUIRE = "require"  # 必须是非 DV 的 HDR，且资源不能同时带 DV
    REQUIRE_DV = "require_dv"  # 必须包含 Dolby Vision，可同时带 HDR
    FORBID = "forbid"  # 排除非 DV 的 HDR，不影响纯 DV
    FORBID_DV = "forbid_dv"  # 排除 Dolby Vision，不影响 HDR


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
    video_codecs: list[str] = Field(
        default_factory=list, description="允许的视频编码（如 x265/x264）；空=不限"
    )
    release_groups_allow: list[str] = Field(
        default_factory=list, description="制作组白名单；空=不限"
    )
    release_groups_block: list[str] = Field(
        default_factory=list, description="制作组黑名单"
    )
    hdr: HdrPolicy = Field(default=HdrPolicy.ANY, description="HDR/DV 过滤策略")
    free_only: bool = Field(default=False, description="只接受当前免费（free）的种子")
    min_seeders: int | None = Field(default=None, description="做种数下限；None=不限")
    # 体积区间按"每集均摊"评估：整季包用总体积 ÷ 集数比较，避免整季包被误杀
    size_min_mb: int | None = Field(default=None, description="单集体积下限（MB）；None=不限")
    size_max_mb: int | None = Field(default=None, description="单集体积上限（MB）；None=不限")
    exclude_hr: bool = Field(default=False, description="排除 H&R 考核种子")
    hr_unknown_policy: HrUnknownPolicy = Field(
        default=HrUnknownPolicy.LENIENT, description="H&R 未知时的策略"
    )

    # -- 预留（本期不消费，字段先占位保证 spec 向前兼容）--------------------
    sites: list[str] = Field(
        default_factory=list, description="[预留] 站点白名单；空=全部启用站点"
    )
    cutoff_resolution: str | None = Field(
        default=None, description="[预留] 洗版上限（P6 启用）"
    )


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
