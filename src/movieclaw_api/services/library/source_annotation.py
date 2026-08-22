"""片源人工标注（docs/design/media-source-annotation.md）。

扫描/手工入库的文件常因文件名无片源词而 ``media_source`` 为空，同分辨率下
洗版比较不可比，单元永远停在「无法确认」。本服务把用户的整季判断落库：

- **文件侧**：标注值写入 ``library_file.media_source`` 并置人工保护位
  （自动名称解析不得覆盖）。只动「未知」与「此前人工标注」的行——名称
  解析出的已知值不被整季批量覆盖（改判已知值属纠错场景，不在本入口范围）；
- **快照侧**：``wanted_item.quality`` 只在 ``NULL`` 时自动构建，事后修改
  library_file 不会传导，必须在此显式刷新。只改出处维度（``media_source``
  覆盖、``remux`` 移除——人工标注对出处是权威的），probe 实测字段一概不碰
  （§4.1 实测优先）；且只刷新「最优文件为人工标注」的单元——最优文件
  片源是名称解析出的已知值时，快照基线本来就是对的，不能被批量标注污染。

标注不触发搜索：前端标注成功后重跑一轮 upgrade-runs，由它完成排期与踢搜索。
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models import (
    LibraryFile,
    Subscription,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_matcher import USER_LOWEST_SOURCE

# 可标注值域（schema Literal 与前端选项同源）：五个真实档位 + 显式「按最低档」
ANNOTATABLE_SOURCES: tuple[str, ...] = (
    "Remux",
    "Blu-ray",
    "WEB-DL",
    "WEBRip",
    "HDTV",
    USER_LOWEST_SOURCE,
)


def _candidate_filter(media_item_id: int, season_number: int):
    """标注范围的统一口径：该季在位、且片源未知或此前人工标注的行。"""
    return (
        LibraryFile.media_item_id == media_item_id,
        LibraryFile.season_number == season_number,
        LibraryFile.in_place(),
        or_(
            LibraryFile.media_source.is_(None),
            LibraryFile.media_source_manual.is_(True),
        ),
    )


async def list_annotation_candidates(
    session: AsyncSession, *, media_item_id: int, season_number: int
) -> list[LibraryFile]:
    """弹窗预览用：将被标注的文件行（按集号排序），供用户扫一眼文件名确认。"""
    rows = (
        (
            await session.execute(
                select(LibraryFile).where(*_candidate_filter(media_item_id, season_number))
            )
        )
        .scalars()
        .all()
    )
    return sorted(rows, key=lambda f: (f.episode_number, f.file_path))


async def annotate_media_source(
    session: AsyncSession,
    *,
    media_item_id: int,
    season_number: int,
    media_source: str,
) -> dict[str, int]:
    """整季标注片源，返回 ``{"files": n, "snapshots": m}``。电影传季号
    哨兵 0（与 library_file/wanted_item 的电影单元约定一致）。

    幂等：重复标注同值无副作用；改标（如 WEB-DL → WEBRip）就是再调一次
    ——人工行始终在标注范围内。只改内存行并 commit，不触发搜索。
    """
    from movieclaw_api.services.subscription.upgrade import _file_sort_key

    files = await list_annotation_candidates(
        session, media_item_id=media_item_id, season_number=season_number
    )
    now = utcnow()
    for file in files:
        file.media_source = media_source
        file.media_source_manual = True
        file.updated_at = now

    # 快照刷新需要按单元挑最优文件（与 fill_snapshots 同一把尺），
    # 所以把该季的在位文件全部拉出来（含名称解析已知片源的行）
    unit_files = (
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.media_item_id == media_item_id,
                    LibraryFile.season_number == season_number,
                    LibraryFile.in_place(),
                )
            )
        )
        .scalars()
        .all()
    )
    by_unit: dict[tuple[int, int], list[LibraryFile]] = {}
    for file in unit_files:
        by_unit.setdefault((file.season_number, file.episode_number), []).append(file)

    wanted_rows = (
        (
            await session.execute(
                select(WantedItem)
                .join(Subscription, Subscription.id == WantedItem.subscription_id)
                .where(
                    Subscription.media_item_id == media_item_id,
                    WantedItem.status == WantedStatus.IMPORTED,
                )
            )
        )
        .scalars()
        .all()
    )
    snapshots = 0
    for wanted in wanted_rows:
        if wanted.season_number != season_number:
            continue
        # NULL 交给既有回填（会用已标注的 library_file 构建）；{} 哨兵
        # 意味着当时无在位文件，同样不在此修补
        if not wanted.quality:
            continue
        candidates = by_unit.get((wanted.season_number, wanted.episode_number))
        if not candidates:
            continue
        best = max(candidates, key=_file_sort_key)
        if not best.media_source_manual:
            continue
        quality = dict(wanted.quality)
        quality["media_source"] = best.media_source
        # 人工标注片源即否定 Remux；显式写 False 而不是删键——快照落库一律
        # 全键，删键会破坏这个不变量（§16.2）
        quality["remux"] = False
        wanted.quality = quality
        wanted.updated_at = now
        snapshots += 1

    await session.commit()
    return {"files": len(files), "snapshots": snapshots}
