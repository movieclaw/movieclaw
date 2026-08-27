"""入库自动生成（G2，docs/design/subtitle-ai-translate.md §6）。

扫描收尾触发：对该库缺目标语言字幕、且有可用参考源的在位文件，按每日
额度串行生成。四重护栏：

1. 开关**默认关**（SubtitleGenSetting.auto_generate），可限定库集合；
2. 每日额度熔断（daily_limit，进程内按天计数——自托管单机语义下重启
   清零可接受，额度是防批量失控不是硬计费）;
3. 进程内已尝试集：同一文件本进程只自动尝试一次（失败不无限重试烧钱，
   手动触发不受此限）;
4. 持久静音台账（``subtitle_auto_mute``）：用户在任务中心忽略失败任务时
   可以选择"不再自动生成"，这条决定跨重启有效。第 3 条是内存集合，重启
   即清零——只有它，用户忽略掉的任务会在下次扫描时原地复活（issue #221）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services.subtitle_gen import source, tasks
from movieclaw_api.services.subtitle_gen.source import normalize_language
from movieclaw_db.engine import get_database
from movieclaw_db.models import Job, LibraryFile, SubtitleAutoMute

logger = logging.getLogger("movieclaw_api.subtitle_gen")

# 每日额度计数（进程内按天）与已尝试集
_quota_day: date | None = None
_quota_used = 0
_attempted: set[int] = set()
# 每库单飞：一轮扫描一个自动批次
_running_libraries: set[int] = set()


def _quota_take(limit: int) -> bool:
    global _quota_day, _quota_used
    today = date.today()
    if _quota_day != today:
        _quota_day, _quota_used = today, 0
    if _quota_used >= limit:
        return False
    _quota_used += 1
    return True


def _has_target_subtitle(row: LibraryFile, target: str | None) -> bool:
    """已有目标语言字幕（内封或外挂，含此前生成的 AI 字幕）就不再生成。"""
    for entry in row.external_subtitles or []:
        if normalize_language(entry.get("language")) == target:
            return True
    for raw in row.subtitle_streams or []:
        if normalize_language(raw.get("language")) == target:
            return True
    return False


def _has_reference(row: LibraryFile, target_language: str) -> bool:
    ranked = source.rank_candidates(row, original_language=None, target_language=target_language)
    return any(not c.excluded for c in ranked)


# --------------------------------------------------------------------------
# 「不再自动生成」台账
#
# 用户在任务中心忽略一条失败的字幕任务时，可以顺手勾上"不再自动生成"。
# 没有这一层，忽略只在当前进程内有效：``_attempted`` 一重启就清零，下次
# 扫描收尾又给同一个文件建一条新任务、再失败一次（issue #221）。
# --------------------------------------------------------------------------


async def _muted_file_ids(target_language: str) -> set[int]:
    """读取该目标语言下被静音的文件 id 集合。"""
    token = tasks.ensure_language_token(target_language)
    db = get_database()
    async with db.session() as session:
        rows = (
            await session.execute(
                select(SubtitleAutoMute.library_file_id).where(
                    SubtitleAutoMute.target_language == token
                )
            )
        ).scalars()
        return set(rows)


def _mute_scope(job: Job) -> tuple[int, str] | None:
    """从一条字幕任务里取出静音坐标：``(文件 id, 目标语言)``。

    只认 ``subtitle.generate``——静音防的是"系统自动反复重建同一件事"，
    而这是目前唯一会自动重建的任务类型。别的任务类型返回 None，调用方据此
    如实告诉用户"这类任务没有可静音的自动来源"，而不是假装静音成功。
    """
    if job.job_type != "subtitle.generate":
        return None
    file_id = (job.input_data or {}).get("file_id")
    target = (job.input_data or {}).get("target_language")
    if not isinstance(file_id, int) or not isinstance(target, str):
        return None
    try:
        return file_id, tasks.ensure_language_token(target)
    except BadRequestException:
        # 历史任务 input_data 里的脏语言值不该让忽略动作整个失败——用户要的是
        # 把这条任务收走，静音不了就如实不静音。
        return None


async def mute_from_job(
    session: AsyncSession, job: Job, *, muted_by: str | None = None
) -> bool:
    """按任务落一条静音记录；任务类型不支持静音时返回 False（不抛）。

    幂等：同一 (文件, 语言) 重复静音直接返回 True，不制造第二行。
    """
    scope = _mute_scope(job)
    if scope is None:
        return False
    file_id, token = scope
    existing = (
        await session.execute(
            select(SubtitleAutoMute).where(
                SubtitleAutoMute.library_file_id == file_id,
                SubtitleAutoMute.target_language == token,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            SubtitleAutoMute(
                library_file_id=file_id, target_language=token, muted_by=muted_by
            )
        )
        await session.commit()
        logger.info(
            "已静音自动字幕生成：文件 #%s 的 %s 字幕（来自任务 %s）", file_id, token, job.id
        )
    return True


async def unmute_from_job(session: AsyncSession, job: Job) -> bool:
    """撤销忽略时同步解除静音——否则"撤销"只撤了一半，自动生成仍然不会恢复。"""
    scope = _mute_scope(job)
    if scope is None:
        return False
    file_id, token = scope
    result = await session.execute(
        delete(SubtitleAutoMute).where(
            SubtitleAutoMute.library_file_id == file_id,
            SubtitleAutoMute.target_language == token,
        )
    )
    await session.commit()
    if result.rowcount:
        # 内存集合也要放开，否则本进程内仍然拒绝再次自动尝试。
        _attempted.discard(file_id)
        logger.info("已解除自动字幕生成静音：文件 #%s 的 %s 字幕", file_id, token)
    return True


def queue_after_scan(library_id: int) -> None:
    """扫描收尾的挂钩（同步、绝不抛）：开关开启才起后台批次。"""
    if library_id in _running_libraries:
        return
    # 必须在 create_task 前同步占位；若等协程首次运行、读完设置后才登记，
    # 同一事件循环连续两次扫描收尾会同时创建两个批次并重复消费额度。
    loop = asyncio.get_running_loop()
    _running_libraries.add(library_id)
    try:
        loop.create_task(_run_batch(library_id))
    except Exception:
        _running_libraries.discard(library_id)
        raise


async def _run_batch(library_id: int) -> None:
    from movieclaw_api.settings.schemas import get_subtitle_gen_setting

    try:
        setting = await get_subtitle_gen_setting()
        if not setting.auto_generate:
            return
        if setting.library_ids and library_id not in setting.library_ids:
            return
        target = normalize_language(setting.target_language)

        db = get_database()
        async with db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(LibraryFile).where(
                            LibraryFile.library_id == library_id,
                            LibraryFile.media_item_id.is_not(None),
                            LibraryFile.in_place(),
                        )
                    )
                ).scalars()
            )

        # 台账短路必须发生在筛选里，而不是等到建任务时才拦：被静音的文件
        # 连"待处理"都不该算，否则日志会报出一个永远不会执行的数字。
        muted = await _muted_file_ids(setting.target_language)
        pending = [
            row
            for row in rows
            if row.id not in _attempted
            and row.id not in muted
            and not _has_target_subtitle(row, target)
            and _has_reference(row, setting.target_language)
        ]
        if not pending:
            return
        logger.info(
            "媒体库 #%s 自动字幕生成批次：%d 个文件缺 %s 字幕（每日额度剩余 %d）",
            library_id,
            len(pending),
            setting.target_language,
            max(0, setting.daily_limit - _quota_used),
        )
        for row in pending:
            assert row.id is not None
            if not _quota_take(setting.daily_limit):
                logger.warning(
                    "自动字幕生成已达每日额度上限（%d），其余文件明日或手动生成",
                    setting.daily_limit,
                )
                return
            _attempted.add(row.id)
            # 自动入口与 Web / CLI / Agent 共用持久化 Job。这里只负责按额度
            # 创建任务，执行并发、恢复、取消与状态展示统一交给 dispatcher。
            async with db.session() as session:
                try:
                    await tasks.enqueue_generation_job(
                        session,
                        row.id,
                        setting.target_language,
                        origin="scheduler",
                        actor_kind="scheduler",
                        actor_name="入库后自动生成字幕",
                    )
                except Exception as exc:  # noqa: BLE001 -- 单文件不可行不断批次
                    logger.info("自动字幕生成跳过 %s：%s", row.file_path, exc)
                    continue
    except Exception:  # noqa: BLE001 -- 自动批次绝不影响扫描主流程
        logger.exception("自动字幕生成批次失败：库 #%s", library_id)
    finally:
        _running_libraries.discard(library_id)
