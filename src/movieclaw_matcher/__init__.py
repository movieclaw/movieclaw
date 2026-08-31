"""公共匹配内核（movieclaw_matcher）——订阅与自定义规则共用的资源匹配层。

架构定位
--------
与 movieclaw_enrich 平级的纯逻辑包：给定候选种子（含 enrich 属性）、媒体身份
与规则组，判定"是不是这个内容"（身份匹配）与"这个载体可不可接受"（规则过滤），
并给出评分与可读的中文拒绝原因。**纯函数、无 IO、不依赖数据库**——所有读写由
movieclaw_api 的 services 层编排（docs/design/subscription.md 第 3 节）。

组成
----
- models.py   输入/输出契约：RuleSetSpec、TorrentCandidate、IdentityMatch 等
- identity.py 第一级：身份匹配（ID 精确 / 别名+年份，短别名守卫）
- rules.py    第二级：规则过滤 + 内置评分（三态保守处理）
- decision.py 选优：整季包优先，同类按评分（认领等有状态操作在 services 层）
"""

from movieclaw_matcher.decision import (
    DISC_SOURCE,
    USER_LOWEST_SOURCE,
    build_snapshot,
    candidate_ladder_rank,
    compare_ladder,
    compare_upgrade,
    ladder_vector,
    media_source_rank,
    pick_best,
    provably_at_cutoff,
    provably_below_cutoff,
    quality_label,
    resolution_rank,
    source_tier,
    upgrade_target_label,
)
from movieclaw_matcher.identity import match_identity, normalize_title
from movieclaw_matcher.models import (
    SNAPSHOT_VERSION,
    DvPolicy,
    HdrPolicy,
    HrUnknownPolicy,
    IdentityMatch,
    MediaIdentity,
    MediaSourceTier,
    QualitySnapshot,
    RuleSetSpec,
    RuleVerdict,
    TorrentCandidate,
    UpgradeSource,
    UpgradeVerdict,
)
from movieclaw_matcher.rules import evaluate_rules

__all__ = [
    "DISC_SOURCE",
    "USER_LOWEST_SOURCE",
    "DvPolicy",
    "HdrPolicy",
    "HrUnknownPolicy",
    "IdentityMatch",
    "MediaIdentity",
    "MediaSourceTier",
    "QualitySnapshot",
    "SNAPSHOT_VERSION",
    "RuleSetSpec",
    "RuleVerdict",
    "TorrentCandidate",
    "UpgradeSource",
    "UpgradeVerdict",
    "match_identity",
    "normalize_title",
    "evaluate_rules",
    "pick_best",
    "build_snapshot",
    "candidate_ladder_rank",
    "compare_ladder",
    "compare_upgrade",
    "ladder_vector",
    "media_source_rank",
    "provably_at_cutoff",
    "provably_below_cutoff",
    "quality_label",
    "resolution_rank",
    "source_tier",
    "upgrade_target_label",
]
