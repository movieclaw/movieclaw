"""监听导入规则的接口：源目录 → 目标库 搬运配置的 CRUD。

媒体库之上的独立功能（详见 services.import_watch_config 与
services.library.ingest 模块头）：媒体库只有一套目录体系；把外部目录里
下载完成的内容搬进库，由这里配置的规则驱动。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.base import BaseModel
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.import_watch_config import ImportWatchConfigService
from movieclaw_api.services.library import ingest
from movieclaw_api.services.library.config import LibraryConfigService
from movieclaw_api.services.library.profile import kind_label
from movieclaw_db.engine import get_session
from movieclaw_db.models import ImportWatch, IngestEntry, IngestStatus

router = APIRouter(prefix="/import-watch", tags=["import-watch"])


class ImportWatchPayload(BaseModel):
    """创建/更新监听导入规则的请求体。

    目标三态：``library_id`` 指定库；``target_path`` 自定义目录（movie/tv
    识别改名、video 原样搬运后落该目录，不进任何媒体库——整理结果需外部
    流转再进库的场景，此时 ``kind`` 必填）；两者都为 null 即**自动路由**（识别出作品后按各库收藏
    范围选库，``kind`` 同样必填）。``library_id`` 与 ``target_path`` 互斥。
    """

    source_path: str = Field(description="源目录（绝对路径，不得与任何库根路径重叠）")
    strategy: Literal["hardlink", "copy"] = Field(
        description="搬运策略：hardlink（零占用需与落点同盘）/ copy（可跨盘）"
    )
    library_id: int | None = Field(
        default=None, description="目标媒体库；null=自动路由或自定义目录"
    )
    target_path: str | None = Field(
        default=None,
        description="自定义目录目标（绝对路径，不得与库根/监听源重叠）；与 library_id 互斥",
    )
    kind: Literal["movie", "tv", "video"] | None = Field(
        default=None,
        description=(
            "自动路由/自定义目录的媒体类型；指定库时忽略"
            "（video：不识别不改名，自动路由落默认其他库、自定义目录原样落该目录）"
        ),
    )
    process_existing: bool = Field(
        default=True,
        description=(
            "是否整理源目录里已有的存量内容；false=跳过存量只处理新增"
            "（存量条目在首轮巡检被标记为已忽略，清单中可恢复）"
        ),
    )


class ImportWatchView(BaseModel):
    """一条监听导入规则（带目标展示信息）。"""

    id: int
    source_path: str
    strategy: Literal["hardlink", "copy"]
    library_id: int | None = Field(default=None, description="null=自动路由或自定义目录")
    library_name: str | None = None
    target_path: str | None = Field(default=None, description="自定义目录目标（其余目标为 null）")
    kind: Literal["movie", "tv", "video"] | None = Field(
        default=None, description="自动路由/自定义目录的媒体类型（指定库时为 null）"
    )
    target_label: str = Field(
        description="目标展示名：库名 /「自动路由（电影/剧集）」/「自定义目录 …」"
    )
    process_existing: bool = Field(description="是否整理存量（false=跳过存量只处理新增）")
    stats: dict[str, int] = Field(
        default_factory=dict,
        description="台账状态计数（imported/pending/failed/skipped/ignored → 条目数）",
    )
    imported_files: int = Field(
        default=0,
        description="已入库条目累计入库的文件数（剧集一条目是一个季包，条目数说不清入了几集）",
    )
    imported_works: int = Field(
        default=0,
        description=(
            "已入库的作品数（同一部剧的多季、多版本合并为一部）——"
            "「已入库」展示的就是这个数，stats.imported 是记账条目数"
        ),
    )
    created_at: datetime

    @classmethod
    def from_model(
        cls,
        row: ImportWatch,
        *,
        library_name: str | None,
        stats: ingest.LedgerStats | None = None,
    ) -> ImportWatchView:
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if row.library_id is not None:
            label = library_name or "?"
        elif row.target_path:
            label = f"自定义目录（{kind_label(row.kind or '')} · {row.target_path}）"
        else:
            label = f"自动路由（{kind_label(row.kind or '')}）"
        return cls(
            id=row.id,  # type: ignore[arg-type]
            source_path=row.source_path,
            strategy=row.strategy,  # type: ignore[arg-type]
            library_id=row.library_id,
            library_name=library_name,
            target_path=row.target_path,
            kind=row.kind,  # type: ignore[arg-type]
            target_label=label,
            process_existing=row.process_existing,
            stats=stats.counts if stats else {},
            imported_files=stats.imported_files if stats else 0,
            imported_works=stats.imported_works if stats else 0,
            created_at=created,
        )


async def _views(session: AsyncSession, rows: list[ImportWatch]) -> list[ImportWatchView]:
    names = {lib.id: lib.name for lib in await LibraryConfigService(session).list_all()}
    stats = await ingest.entry_stats(session, rows)
    return [
        ImportWatchView.from_model(
            r, library_name=names.get(r.library_id), stats=stats.get(r.id or 0)
        )
        for r in rows
    ]


@router.get(
    "",
    response_model=ApiResponse[list[ImportWatchView]],
    summary="列出全部监听导入规则",
    operation_id="watch.list",
)
async def list_rules(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ImportWatchView]]:
    service = ImportWatchConfigService(session)
    return ok(await _views(session, await service.list_all()))


@router.post(
    "",
    response_model=ApiResponse[ImportWatchView],
    summary="创建监听导入规则（硬链接策略保存即做同盘检测）",
    operation_id="watch.create",
)
async def create_rule(
    payload: ImportWatchPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ImportWatchView]:
    service = ImportWatchConfigService(session)
    row = await service.create(
        source_path=payload.source_path,
        strategy=payload.strategy,
        library_id=payload.library_id,
        kind=payload.kind,
        target_path=payload.target_path,
        process_existing=payload.process_existing,
    )
    views = await _views(session, [row])
    return ok(views[0], message=f"已创建监听导入规则：{row.source_path}")


@router.put(
    "/{rule_id}",
    response_model=ApiResponse[ImportWatchView],
    summary="更新监听导入规则",
    operation_id="watch.update",
)
async def update_rule(
    rule_id: int,
    payload: ImportWatchPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ImportWatchView]:
    service = ImportWatchConfigService(session)
    row = await service.update(
        rule_id,
        source_path=payload.source_path,
        strategy=payload.strategy,
        library_id=payload.library_id,
        kind=payload.kind,
        target_path=payload.target_path,
        process_existing=payload.process_existing,
    )
    views = await _views(session, [row])
    return ok(views[0], message="已更新")


@router.delete(
    "/{rule_id}",
    response_model=ApiResponse[dict],
    summary="删除监听导入规则（不动磁盘，仅停止监听）",
    operation_id="watch.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_rule(
    rule_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = ImportWatchConfigService(session)
    await service.delete(rule_id)
    return ok({}, message="已删除（源目录与已导入的文件均未受影响）")


# —— 台账清单与人工拍板（实现层：services.library.ingest）——————————


class IngestEntryView(BaseModel):
    """监听目录一个条目的处理台账行（清单展示）。"""

    id: int
    name: str = Field(description="条目名（源目录顶层的文件/目录名）")
    entry_path: str
    status: Literal["imported", "pending", "failed", "skipped", "ignored"]
    message: str | None = Field(default=None, description="处理结论（中文）")
    imported_count: int
    attempted_at: datetime

    @classmethod
    def from_model(cls, row: IngestEntry) -> IngestEntryView:
        attempted = row.attempted_at
        if attempted.tzinfo is None:
            attempted = attempted.replace(tzinfo=UTC)
        return cls(
            id=row.id,  # type: ignore[arg-type]
            name=PurePosixPath(row.entry_path).name,
            entry_path=row.entry_path,
            status=row.status,  # type: ignore[arg-type]
            message=row.message,
            imported_count=row.imported_count,
            attempted_at=attempted,
        )


class IngestEntriesView(BaseModel):
    """一条规则的台账清单 + 各状态计数。"""

    counts: dict[str, int]
    entries: list[IngestEntryView]


class ClaimPayload(BaseModel):
    """人工认领请求：把条目钉到指定 TMDB 条目并恢复后台入库作业。"""

    tmdb_id: int = Field(description="TMDB 条目 id（类型按规则先验：库类型或规则声明）")


@router.get(
    "/{rule_id}/entries",
    response_model=ApiResponse[IngestEntriesView],
    summary="一条规则的台账清单（待处理/失败/已忽略/已入库）",
    operation_id="watch.entries",
)
async def list_rule_entries(
    rule_id: int,
    status: Literal["imported", "pending", "failed", "skipped", "ignored"] | None = None,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[IngestEntriesView]:
    rule = await ImportWatchConfigService(session).get(rule_id)
    ledger = (await ingest.entry_stats(session, [rule])).get(rule_id)
    rows = await ingest.list_entries(session, rule, status)
    return ok(
        IngestEntriesView(
            counts=ledger.counts if ledger else {},
            entries=[IngestEntryView.from_model(r) for r in rows],
        )
    )


@router.post(
    "/entries/{entry_id}/ignore",
    response_model=ApiResponse[IngestEntryView],
    summary="忽略条目：永久跳过，不再处理（可恢复）",
    operation_id="watch.entries.ignore",
)
async def ignore_entry(
    entry_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[IngestEntryView]:
    row = await ingest.ignore_entry(session, entry_id)
    return ok(IngestEntryView.from_model(row), message="已忽略（清单中可随时恢复）")


@router.post(
    "/entries/{entry_id}/restore",
    response_model=ApiResponse[IngestEntryView],
    summary="恢复条目：重新进入处理流程",
    operation_id="watch.entries.restore",
)
async def restore_entry(
    entry_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[IngestEntryView]:
    row = await ingest.restore_entry(session, entry_id)
    return ok(IngestEntryView.from_model(row), message="已恢复，将在下轮巡检时重新处理")


@router.post(
    "/entries/{entry_id}/claim",
    response_model=ApiResponse[IngestEntryView],
    summary="认领条目：钉到指定 TMDB 身份并恢复后台入库作业",
    operation_id="watch.entries.claim",
)
async def claim_entry(
    entry_id: int,
    payload: ClaimPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[IngestEntryView]:
    row = await ingest.claim_entry(session, entry_id, payload.tmdb_id)
    view = IngestEntryView.from_model(row)
    if row.status == IngestStatus.IMPORTED:
        return ok(view, message=row.message or "已入库")
    # 认领只持久化身份并唤醒原 Job；入库进度与故障继续由任务中心呈现。
    return ok(view, message=row.message or "已认领，等待后台整理入库")
