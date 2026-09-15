"""订阅投递的文件级选择性下载（docs/design/subscription.md 推论二的落地）。

整季包覆盖的单元远多于本次认领的缺口时（"只缺 S01E08，包里却有整季"），
投递以暂停态进入下载器，把包里不需要的文件标记为不下载，再恢复下载——
省流量，也避免重复版本混入库。本模块是链路的**大脑**，两段职责：

1. :func:`selective_units_for`——投递前判定是否值得启用（候选呈包形态、
   且是剧集），返回要保障的单元集合；单集资源返回 None，最常见的追新
   路径完全不进入暂停→恢复流程。
2. :func:`plan_file_selection`——拿到种子内文件清单后做纯规划：解析每个
   文件的季集号，产出保留/跳过的文件索引。**不做任何 IO**，真正写下载器
   在 torrent_submit 编排。

规划铁律是**不确定即全量**：任何要保障的单元映射不到文件、或文件解析
不出确定的季集号，宁可直接放弃选择让种子全量下载——跳错文件会让工单
以 GRABBED 永久挂起（文件清单里"看得到"、磁盘上却没有，库存对账既不
退回也不关单），比多下几个 GB 严重得多。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from movieclaw_api.services.library.layout import VIDEO_EXTS
from movieclaw_api.services.library.units import resolve_units


@dataclass(frozen=True)
class FileSelectionPlan:
    """一次选择性下载的文件取舍结论（索引对应文件清单的下标）。"""

    keep_indices: list[int]
    skip_indices: list[int]
    # 被跳过文件的总字节数，活动文案里换算成"省下多少"
    skip_bytes: int


def selective_units_for(
    match,  # noqa: ANN001 -- movieclaw_matcher.IdentityMatch（避免运行期硬依赖其导入路径）
    targets: Sequence[tuple[int, int]],
    kind: str,
) -> set[tuple[int, int]] | None:
    """按候选的覆盖形态判定要不要启用选择性下载。

    只有三条路都满足才启用：剧集、身份匹配在结果里、且候选明显覆盖了
    多于本次要下载的单元（整季包/全集包/多集包）。返回要保障的单元集合
    （缺口 + 洗版单元），None = 不启用、按原样整包投递。
    """
    if kind != "tv" or match is None or not targets:
        return None
    needed = set(targets)
    pack_like = bool(match.pack_seasons) or match.is_complete_series
    if not pack_like and len(match.episodes) <= len(needed):
        return None
    return needed


def plan_file_selection(
    file_paths: Sequence[str],
    needed_units: set[tuple[int, int]],
    *,
    file_sizes: Sequence[int] | None = None,
    known_seasons: Collection[int] | None = None,
) -> FileSelectionPlan | None:
    """规划种子内文件的取舍：跳过确定不需要的，保留其余一切。

    :param file_paths: 种子内相对路径（下载器文件清单顺序，即文件索引）。
    :param needed_units: 要保障的 (季, 集) 集合——这些单元对应的文件必须
        全部保留，且每个单元至少要有一个文件可下载。
    :param file_sizes: 与 file_paths 对齐的字节数，用于估算省下的体积。
    :param known_seasons: 条目真实存在的季号（订阅勾选的季），借给
        resolve_units 做季号推断守卫。
    :returns: 取舍结论；``None`` = 不做选择（没有任何可跳过的文件，或
        保障单元映射不全，调用方应直接恢复全量下载）。

    可跳过的只有一种文件：**视频文件 且 确定解析出 (季, 集) 且不在保障
    集合里**。非视频文件（nfo/封面等，体积极小）、季号挂起、集号解析
    失败、E00 特辑一律保留——多选的代价是几 MB，选错的代价是工单挂死。
    """
    if not file_paths or not needed_units:
        return None
    paths = [Path(p) for p in file_paths]
    units = resolve_units(paths, known_seasons=known_seasons)

    keep: list[int] = []
    skip: list[int] = []
    skip_bytes = 0
    for index, path in enumerate(paths):
        unit = units.get(path)
        skippable = (
            path.suffix.lower() in VIDEO_EXTS
            and unit is not None
            and unit.season is not None
            and unit.episode > 0
            and not unit.explicit_pilot
            and (unit.season, unit.episode) not in needed_units
        )
        if skippable:
            skip.append(index)
            if file_sizes is not None and index < len(file_sizes):
                skip_bytes += int(file_sizes[index] or 0)
        else:
            keep.append(index)

    if not skip:
        return None  # 没有可跳过的文件，选择没有意义
    # 安全阀：每个保障单元都必须在保留文件里有落点，否则全量下载
    resolved = {
        (unit.season, unit.episode)
        for unit in units.values()
        if unit.season is not None and unit.episode > 0
    }
    if not needed_units <= resolved:
        return None
    return FileSelectionPlan(
        keep_indices=keep, skip_indices=skip, skip_bytes=skip_bytes
    )
