"""取消订阅的联动清理：删种子任务 + 回收媒体库文件（异步后台任务）。

取消订阅本身只是"不再追了"，默认不碰任何已有内容——这是订阅删除一直以来的
承诺，不能改。但用户真正想"这部片从我的机器上消失"时，此前要分别去下载器删
任务、去媒体库删文件，两处都得自己找。本模块把这两件事收成取消订阅时的两个
显式开关，动作本身仍走各自领域已有的唯一通道：

- 种子：``download_tasks.delete_download_task``（连同下载目录里的数据文件）；
- 媒体库文件：``library.recycle.recycle_file``（进回收站，保留期内可恢复）。
  用回收站而不是真删，是因为这是一次从订阅页发起的批量破坏性操作，
  给 7 天后悔窗口的成本为零（见 docs/design/library-file-recycle.md）。

为什么必须是后台任务：删种子要逐个连下载器（慢且可能超时），回收文件要逐个
移动磁盘文件（几十 GB 的目录也可能跨目录搬），而用户点的是"取消订阅"——
接口必须立刻返回、订阅立刻消失。

为什么清理计划要在删订阅**之前**快照进任务输入：``subscription_download_attempt``
随订阅级联删除，订阅一没，"这条订阅投递过哪些种子"就永远查不回来了。因此
``build_cleanup_plan`` 先把 infohash、下载器、台账文件 id 全部取出来塞进
``input_data``，任务此后只认这份快照，不再依赖订阅存在（也因此崩溃重启后
仍可继续）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import AppException
from movieclaw_api.services import jobs
from movieclaw_api.services.download_tasks import delete_download_task
from movieclaw_api.services.library.recycle import recycle_file
from movieclaw_api.services.media_server_notify import notify_media_server_refresh
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    DownloaderClient,
    FileState,
    LibraryFile,
    MediaItem,
    Subscription,
    SubscriptionDownloadAttempt,
)
from movieclaw_db.repositories import LibraryRepository

logger = logging.getLogger("movieclaw_api.subscription_cleanup")

JOB_TYPE = "subscription.cleanup"


@dataclass(frozen=True)
class TorrentTarget:
    """计划里的一个种子任务（下载器 + infohash 唯一定位，避免误删同名任务）。"""

    info_hash: str
    downloader_id: int
    downloader_name: str
    title: str
    hit_and_run: bool | None


@dataclass(frozen=True)
class FileTarget:
    """计划里的一个媒体库台账文件。"""

    file_id: int
    library_id: int
    path: str
    size_bytes: int


@dataclass(frozen=True)
class CleanupPlan:
    """取消订阅可联动清理的全部内容；两条清单互相独立，用户逐项选择。"""

    media_item_id: int
    title: str
    torrents: list[TorrentTarget]
    files: list[FileTarget]

    @property
    def hit_and_run_count(self) -> int:
        """H&R 考核中/状态未知的种子数——删了可能被站点判为未完成考核。"""
        return sum(1 for t in self.torrents if t.hit_and_run is not False)

    @property
    def library_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


async def build_cleanup_plan(session: AsyncSession, subscription: Subscription) -> CleanupPlan:
    """快照一条订阅可联动清理的种子与媒体库文件（只读，不改任何状态）。

    - 种子：该订阅历次投递（含换源试用、洗版）留下的尝试，按 infohash 去重；
      下载器配置已删除（downloader_id 为空）的无从定位，不进计划；
    - 媒体库文件：该条目在**所有**库里的台账行。订阅与条目一一对应，用户说的
      "把媒体库里的资源也删掉"就是这部作品的全部文件；已在回收站的行跳过
      （无事可做）。缺失行（state=missing）留在计划里——回收会把账一并清干净。
    """
    assert subscription.id is not None
    item_title = await _item_title(session, subscription.media_item_id)

    attempts = list(
        (
            await session.execute(
                select(SubscriptionDownloadAttempt)
                .where(SubscriptionDownloadAttempt.subscription_id == subscription.id)
                .order_by(SubscriptionDownloadAttempt.id)
            )
        )
        .scalars()
        .all()
    )
    downloader_names = dict(
        (
            await session.execute(select(DownloaderClient.id, DownloaderClient.name))
        ).all()
    )
    torrents: list[TorrentTarget] = []
    seen: set[tuple[int, str]] = set()
    for attempt in attempts:
        if attempt.downloader_id is None:
            continue
        key = (attempt.downloader_id, attempt.info_hash.lower())
        if key in seen:
            continue
        seen.add(key)
        torrents.append(
            TorrentTarget(
                info_hash=attempt.info_hash.lower(),
                downloader_id=attempt.downloader_id,
                downloader_name=downloader_names.get(attempt.downloader_id) or "下载器",
                title=attempt.torrent_title or attempt.download_name or attempt.info_hash,
                hit_and_run=attempt.hit_and_run,
            )
        )

    rows = list(
        (
            await session.execute(
                select(LibraryFile)
                .where(
                    LibraryFile.media_item_id == subscription.media_item_id,
                    LibraryFile.state != FileState.TRASHED,
                )
                .order_by(LibraryFile.season_number, LibraryFile.episode_number, LibraryFile.id)
            )
        )
        .scalars()
        .all()
    )
    files = [
        FileTarget(
            file_id=row.id,  # type: ignore[arg-type]
            library_id=row.library_id,
            path=row.file_path,
            size_bytes=row.size_bytes,
        )
        for row in rows
        if row.id is not None
    ]
    return CleanupPlan(
        media_item_id=subscription.media_item_id, title=item_title, torrents=torrents, files=files
    )


async def _item_title(session: AsyncSession, media_item_id: int) -> str:
    """作品名——任务名与所有用户可读文案都用它（订阅删掉后条目仍在）。"""
    item = await session.get(MediaItem, media_item_id)
    return item.title if item is not None else f"条目 #{media_item_id}"


async def enqueue_cleanup_job(
    session: AsyncSession,
    plan: CleanupPlan,
    *,
    delete_torrents: bool,
    delete_library_files: bool,
    origin: str = "web",
) -> jobs.CreateJobResult | None:
    """把选中的清理内容入队；两项都没选或都无内容时返回 None（不建空任务）。

    ``commit=False``：与"删除订阅"共用一个事务，要么订阅删掉且清理已入队，
    要么两件事都没发生——绝不会出现"订阅还在但文件已被回收"。
    """
    torrents = list(plan.torrents) if delete_torrents else []
    files = list(plan.files) if delete_library_files else []
    if not torrents and not files:
        return None
    return await jobs.create_job(
        session,
        job_type=JOB_TYPE,
        subject=f"《{plan.title}》取消订阅清理",
        input_data={
            "media_item_id": plan.media_item_id,
            "title": plan.title,
            "torrents": [
                {
                    "info_hash": t.info_hash,
                    "downloader_id": t.downloader_id,
                    "downloader_name": t.downloader_name,
                    "title": t.title,
                }
                for t in torrents
            ],
            "files": [
                {"file_id": f.file_id, "library_id": f.library_id, "path": f.path}
                for f in files
            ],
        },
        resources=[jobs.ResourceRef("media_item", plan.media_item_id)],
        # 不设 dedupe_key：每次取消订阅都带着自己那份快照，重新订阅后再次取消
        # 必须独立执行——按条目去重会让第二份计划被静默丢弃。订阅在同一事务里
        # 已经删掉，同一条订阅本来也不可能被取消两次。
        handler_revision="subscription.cleanup.v1",
        # 外部副作用（删种子、搬文件）不适合盲目重试：失败原因基本是下载器
        # 不可达或权限问题，重试只会重复报同一个错。失败留在任务中心由用户重试。
        max_attempts=1,
        origin=origin,
        progress={
            **jobs.default_progress("等待清理订阅关联内容"),
            "details": {
                "torrent_total": len(torrents),
                "file_total": len(files),
                "title": plan.title,
            },
        },
        commit=False,
    )


@jobs.register_job_handler(JOB_TYPE)
async def _run_subscription_cleanup_job(
    context: jobs.JobContext, input_data: dict[str, Any]
) -> dict[str, Any]:
    """先删种子再回收文件；单项失败只计数，不让整条清理半途而废。

    顺序有意如此：种子先走，下载器就不会在文件被搬进回收站后把任务判为
    "文件丢失"而报错，做种也不会在中途断在一个找不到文件的状态上。
    """
    title = str(input_data.get("title") or "订阅")
    torrents = [t for t in input_data.get("torrents") or [] if isinstance(t, dict)]
    files = [f for f in input_data.get("files") or [] if isinstance(f, dict)]
    total = len(torrents) + len(files)
    done = 0
    torrent_removed = 0
    file_recycled = 0
    failures: list[str] = []

    async def _tick(phase: str, message: str) -> None:
        await context.update_progress(
            mode="determinate",
            phase=phase,
            message=message,
            current=done,
            total=total,
            percent=round(done / total * 100, 1) if total else 100.0,
            details={
                "title": title,
                "torrent_removed": torrent_removed,
                "file_recycled": file_recycled,
                "failed": len(failures),
            },
        )

    db = get_database()
    for entry in torrents:
        await context.raise_if_cancelled()
        info_hash = str(entry.get("info_hash") or "")
        downloader_id = entry.get("downloader_id")
        name = str(entry.get("title") or info_hash)
        if not info_hash or not isinstance(downloader_id, int):
            done += 1
            continue
        try:
            async with db.session() as session:
                await delete_download_task(
                    session,
                    downloader_id=downloader_id,
                    info_hash=info_hash,
                    delete_files=True,
                )
            torrent_removed += 1
        except AppException as exc:
            # 下载器不可达 / 任务已不存在都不该中断其余清理：用户的诉求是
            # "尽量清干净"，剩下的失败项在任务结果里逐条讲明白
            failures.append(f"种子「{name}」删除失败：{exc.message}")
            logger.warning("取消订阅清理：删除种子失败 hash=%s：%s", info_hash, exc.message)
        except Exception as exc:  # noqa: BLE001 -- 同上，单项异常不终止整条清理
            failures.append(f"种子「{name}」删除失败：{exc}")
            logger.warning("取消订阅清理：删除种子异常 hash=%s", info_hash, exc_info=True)
        done += 1
        if done == total or context.progress_due():
            await _tick("torrents", f"正在删除下载任务（{torrent_removed}/{len(torrents)}）")

    library_ids: set[int] = set()
    for entry in files:
        await context.raise_if_cancelled()
        file_id = entry.get("file_id")
        path = str(entry.get("path") or "")
        if not isinstance(file_id, int):
            done += 1
            continue
        try:
            async with db.session() as session:
                recycled = await _recycle_one(session, file_id, title)
            if recycled is not None:
                file_recycled += 1
                library_ids.add(recycled)
        except Exception as exc:  # noqa: BLE001 -- 单个文件失败不影响其余
            failures.append(f"文件「{path}」回收失败：{exc}")
            logger.warning("取消订阅清理：回收文件失败 id=%s", file_id, exc_info=True)
        done += 1
        if done == total or context.progress_due():
            await _tick("library", f"正在回收媒体库文件（{file_recycled}/{len(files)}）")

    if library_ids:
        async with db.session() as session:
            await LibraryRepository(session).refresh_stats(library_ids)
        # 库存统计与下游媒体服务器都要知道这些文件没了；通知未配置时空转
        await notify_media_server_refresh()

    parts: list[str] = []
    if torrents:
        parts.append(f"删除 {torrent_removed} 个下载任务")
    if files:
        parts.append(f"回收 {file_recycled} 个媒体库文件")
    message = f"《{title}》清理完成：" + "、".join(parts or ["无可清理内容"])
    if failures:
        message += f"；{len(failures)} 项失败，详见任务结果"
    logger.info("%s", message)
    return {
        "message": message,
        "torrent_removed": torrent_removed,
        "file_recycled": file_recycled,
        "failed": len(failures),
        "failures": failures[:20],  # 结果只留前 20 条，完整原因在服务日志里
    }


async def _recycle_one(session: AsyncSession, file_id: int, title: str) -> int | None:
    """回收单个台账行；返回受影响的库 id（无事可做时返回 None）。

    行已不在（并发删除）或已在回收站的直接跳过——取消订阅清理必须可以重复
    执行而不产生第二次搬运。
    """
    row = await session.get(LibraryFile, file_id)
    if row is None or row.state == FileState.TRASHED:
        return None
    library_id = row.library_id
    outcome = await recycle_file(
        session,
        row,
        reason="subscription_cancelled",
        trigger={"kind": "subscription", "id": None, "label": f"《{title}》取消订阅"},
        note="取消订阅时选择了一并删除媒体库资源",
    )
    if outcome == "already_gone":
        await session.delete(row)  # 文件早没了：行删掉与磁盘一致
    await session.commit()
    return library_id
