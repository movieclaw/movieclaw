"""视频章节与场景图（docs/design/video-chapters.md）。

这是 Jellyfin / Emby「场景」（Scenes）功能的对标：详情页一排带时间戳的
截图，点一张就从那个时间点开始播；同一份章节数据还输出给 Jellyfin 协议的
``Chapters`` 字段，让 Infuse 这类播放器做章节跳转。

三条设计决定：

- **章节是文件的属性**（挂 ``library_file``），不是作品的：同条目两个版本
  章节位置可以不同，剧集每集各有各的；
- **内嵌章节优先，按时长合成兜底**：容器里 ≥2 个章节就用它（Jellyfin 的
  规则：0 或 1 个都算"没有章节"），否则按时长分档合成等距章节
  （``_SYNTH_TABLE``）。有效列表是纯函数，不落库——合成策略调档不需要
  迁移与重探；
- **图是章节的附属物**：抓不到图的章节仍在列表里（详情页显示时间戳并可
  跳播，Jellyfin 客户端仍可章节跳转）。

抓图参数照抄 Jellyfin 的 ChapterManager（第 0 章从 15s 抓、平均间隔 <1s
跳过、越界停止、只解关键帧），差异都登记在设计文档 §2.4。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.schemas.library import ChapterJobView
from movieclaw_api.services import jobs
from movieclaw_api.services.library.layout import STRM_EXT
from movieclaw_api.services.library.thumbs import FRAME_GRAB_GATE, build_filter_chains
from movieclaw_api.services.media_probe import VideoColor, probe_chapters, video_color_for
from movieclaw_db.engine import get_database
from movieclaw_db.models import Library, LibraryFile, utcnow
from movieclaw_db.models.job import Job

logger = logging.getLogger("movieclaw_api.library.chapters")

# 合成章节：按时长分档定张数（设计文档 §4.2）。上限是 (时长秒, 张数)，
# 首档 <90s 不合成——短片一张主图就够，再切等于把同一画面摆三遍
_SYNTH_TABLE: tuple[tuple[int, int], ...] = (
    (90, 0),
    (10 * 60, 3),
    (40 * 60, 6),
    (90 * 60, 8),
    (180 * 60, 10),
)
_SYNTH_MAX = 12
# 合成章节落在 6% ～ 94% 之间等距：掐掉片头 logo/黑场与片尾字幕
_SYNTH_HEAD = 0.06
_SYNTH_SPAN = 0.88


@dataclass(frozen=True)
class Chapter:
    """一个有效章节。``synthetic`` 标记它是合成的（没有内嵌章节时按时长切）。"""

    index: int
    start_ms: int
    end_ms: int | None
    title: str | None
    synthetic: bool


def synth_count(duration_seconds: int | None) -> int:
    """按时长定合成章节张数；时长未知或不足 90s 为 0。"""
    if not duration_seconds or duration_seconds <= 0:
        return 0
    for limit, count in _SYNTH_TABLE:
        if duration_seconds < limit:
            return count
    return _SYNTH_MAX


def synthesize_chapters(duration_seconds: int | None) -> list[Chapter]:
    """没有内嵌章节时按时长合成等距章节（标题恒 None）。"""
    count = synth_count(duration_seconds)
    if count == 0:
        return []
    assert duration_seconds is not None
    total_ms = duration_seconds * 1000
    starts = [int(total_ms * (_SYNTH_HEAD + _SYNTH_SPAN * i / (count - 1))) for i in range(count)]
    return [
        Chapter(
            index=i,
            start_ms=start,
            end_ms=starts[i + 1] if i + 1 < count else total_ms,
            title=None,
            synthetic=True,
        )
        for i, start in enumerate(starts)
    ]


def effective_chapters(embedded: list[dict] | None, duration_seconds: int | None) -> list[Chapter]:
    """一个文件的有效章节列表：内嵌 ≥2 个用内嵌，否则按时长合成。

    ``embedded`` 是台账 ``library_file.chapters``（NULL 视为空）。内嵌章节
    起点 ≥ 片长的丢弃（Jellyfin SaveChapters 同款），丢完不足两个同样退回
    合成。
    """
    rows = [c for c in (embedded or []) if isinstance(c, dict) and "start_ms" in c]
    if duration_seconds:
        rows = [c for c in rows if int(c["start_ms"]) < duration_seconds * 1000]
    if len(rows) < 2:
        return synthesize_chapters(duration_seconds)
    rows.sort(key=lambda c: int(c["start_ms"]))
    total_ms = duration_seconds * 1000 if duration_seconds else None
    result: list[Chapter] = []
    for i, row in enumerate(rows):
        start = int(row["start_ms"])
        end = row.get("end_ms")
        if i + 1 < len(rows):
            end = int(rows[i + 1]["start_ms"])
        elif end is None:
            end = total_ms
        title = row.get("title")
        result.append(
            Chapter(
                index=i,
                start_ms=start,
                end_ms=int(end) if end is not None else None,
                title=title if isinstance(title, str) and title else None,
                synthetic=False,
            )
        )
    return result


def chapter_image_map(chapter_images: list | None) -> dict[int, dict]:
    """``library_file.chapter_images`` → {start_ms: 元素}，供有效章节 join。"""
    result: dict[int, dict] = {}
    for entry in chapter_images or []:
        if isinstance(entry, dict) and "start_ms" in entry and entry.get("image"):
            result[int(entry["start_ms"])] = entry
    return result


# ---------------------------------------------------------------------------
# 抓图（设计文档 §4.4）
# ---------------------------------------------------------------------------

#: 场景图宽度：横排卡 480 变体够用，灯箱放大 960 不糊；比主图 1280 小是
#: 因为一部片 8～12 张
_STILL_WIDTH = 960
_JPEG_QUALITY = "4"  # ffmpeg -q:v；960 宽一张约 60～90KB
_FFMPEG_TIMEOUT = 60.0  # 单章
_FILE_BUDGET_SECONDS = 300.0  # 单文件总预算：耗尽把已抓到的落库
#: 有效章节超过这个数只记列表不抓图（KTV 合集这类，横排也放不下）
_MAX_STILLS = 48
#: 章节平均间隔低于 1s 视为碎片化章节，不抓图（Jellyfin 同款阈值）
_MIN_AVG_GAP_MS = 1000
#: 第 0 章从 15s 处抓，避开片头黑场（Jellyfin _firstChapterTicks）
_FIRST_CHAPTER_OFFSET_S = 15.0
_SHOWINFO_PTS = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")

#: 详情页懒触发的去重：正在抓的条目 id（与 trickplay._in_flight 同款）
_in_flight: set[int] = set()


def image_rel_path(media_item_id: int, file_id: int, start_ms: int) -> str:
    """场景图相对资产根目录的路径。首段是条目 id，``/images/assets`` 的可见性
    校验与条目删除时的目录清理都靠它。"""
    return f"{media_item_id}/chapters/{file_id}/{start_ms:010d}.jpg"


def stills_eligible(row: LibraryFile) -> bool:
    """这一行要不要抓图：在位、非原盘、非 strm。"""
    return (
        row.state == "in_place"
        and (row.container or "") not in ("bluray", "dvd", "iso")
        and not row.file_path.lower().endswith(STRM_EXT)
    )


def _filter_chains(color: VideoColor) -> list[list[str]]:
    """滤镜链：隔行化 → 5 个关键帧里选代表帧 → 缩放 → [色调映射] → showinfo。

    色彩决策与主图抓帧共用 ``thumbs.build_filter_chains``（那里有取舍说明）——
    两处出的图必须观感一致，各写一遍迟早会漂。
    ``showinfo`` 放最后：thumbnail 选完帧它只打印被选中那一帧的 pts_time。
    """
    return build_filter_chains(
        color,
        f"scale='min({_STILL_WIDTH},iw)':-2",
        thumbnail="thumbnail=n=5",
        tail=("showinfo",),
    )


def grab_chapter_still(
    video: Path, dest: Path, *, seek_seconds: float, color: VideoColor
) -> int | None:
    """同步版：在 ``seek_seconds`` 附近只解关键帧抓一帧到 ``dest``，返回图上
    那一帧的真实时间（毫秒）；失败返回 None。

    输入侧 ``-ss`` 会把输出时间戳归零，showinfo 报的 ``pts_time`` 是相对定位点
    的偏移，真实时间 = 定位点 + 偏移。**不用 ``-copyts``**：那样报的是容器
    绝对时间戳，MPEG-TS 这类 start_time 不为 0 的文件会整体偏掉，而播放器的
    ``start_ms`` 与 ``-ss`` 一样是相对文件开头的。解析不出 pts_time 时退回
    定位点本身，图照样可用。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    for chain in _filter_chains(color):
        cmd = [
            "ffmpeg",
            "-v",
            "info",
            "-y",
            "-skip_frame",
            "nokey",
            "-ss",
            f"{seek_seconds:.3f}",
            "-i",
            str(video),
            "-an",
            "-sn",
            "-vf",
            ",".join(chain),
            "-frames:v",
            "1",
            "-q:v",
            _JPEG_QUALITY,
            str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT)
        if proc.returncode != 0 or not dest.is_file():
            logger.debug("章节抓帧失败：%s", proc.stderr.decode(errors="replace")[-300:])
            continue
        match = _SHOWINFO_PTS.findall(proc.stderr.decode(errors="replace"))
        offset = max(0.0, float(match[-1])) if match else 0.0
        return int(round((seek_seconds + offset) * 1000))
    return None


def extract_file_stills(
    video: Path,
    *,
    media_item_id: int,
    file_id: int,
    chapters: list[Chapter],
    duration_seconds: int | None,
    hdr: str | None,
    assets_root: Path,
    existing: dict[int, dict],
    force: bool = False,
) -> list[dict] | None:
    """同步版：给一个文件的有效章节逐章抓图，返回 ``chapter_images`` 的元素列表。

    - 已有且文件仍在的图直接复用（``force`` 重抓）；
    - 起点 ≥ 片长的章节停止；平均间隔 <1s 或章节数超上限整体跳过；
    - 单文件预算耗尽返回已抓到的（部分产物也落库，设计文档 §4.4）；
    - 有效列表里对不上的旧图（策略调档、章节变了）当死图删掉；
    - 系统里没有 ffmpeg 返回 None：调用方保持 NULL，装好后下次自动补，
      不用手动 force。
    """
    result: list[dict] = []
    image_dir = assets_root / str(media_item_id) / "chapters" / str(file_id)
    keep: set[str] = set()
    if len(chapters) == 0 or len(chapters) > _MAX_STILLS:
        _delete_dead_images(image_dir, keep)
        return result
    if len(chapters) >= 2:
        gaps = [b.start_ms - a.start_ms for a, b in zip(chapters, chapters[1:], strict=False)]
        if sum(gaps) / len(gaps) < _MIN_AVG_GAP_MS:
            logger.info("章节平均间隔不足 1 秒，跳过场景图：%s", video)
            _delete_dead_images(image_dir, keep)
            return result
    total_ms = duration_seconds * 1000 if duration_seconds else None
    # 色彩特征整个文件探一次就够：DOVI 配置在流的 side data 里，与抓哪一帧无关。
    # 放在预算计时之前——它是一次轻量 ffprobe，不该算进逐章抓图的预算。
    color = video_color_for(video, fallback_hdr=hdr)
    deadline = time.monotonic() + _FILE_BUDGET_SECONDS
    try:
        for chapter in chapters:
            if total_ms is not None and chapter.start_ms >= total_ms:
                break
            rel = image_rel_path(media_item_id, file_id, chapter.start_ms)
            dest = assets_root / rel
            previous = existing.get(chapter.start_ms)
            if not force and previous and previous.get("image") == rel and dest.is_file():
                result.append(previous)
                keep.add(dest.name)
                continue
            if time.monotonic() > deadline:
                logger.warning("章节场景图超出单文件预算，已抓到的先落库：%s", video)
                break
            seek = chapter.start_ms / 1000
            if chapter.start_ms == 0:
                # 第 0 章避开片头黑场；时长未知时也按 15s，抓不到再退回起点由
                # ffmpeg 自己兜（越界定位它会取最后一个关键帧）
                seek = min(_FIRST_CHAPTER_OFFSET_S, float(duration_seconds or 0)) or (
                    _FIRST_CHAPTER_OFFSET_S if duration_seconds is None else 0.0
                )
            try:
                frame_ms = grab_chapter_still(video, dest, seek_seconds=seek, color=color)
            except subprocess.TimeoutExpired:
                logger.warning("章节抓帧超时（%s 秒）：%s @ %ss", _FFMPEG_TIMEOUT, video, seek)
                frame_ms = None
            if frame_ms is None:
                continue
            result.append({"start_ms": chapter.start_ms, "frame_ms": frame_ms, "image": rel})
            keep.add(dest.name)
    except FileNotFoundError:
        logger.warning("系统中未找到 ffmpeg，章节场景图已跳过（装好后自动补）：%s", video)
        return None
    except OSError as exc:
        logger.warning("生成章节场景图失败：%s（%s）", video, exc)
    _delete_dead_images(image_dir, keep)
    return result


def _delete_dead_images(image_dir: Path, keep: set[str]) -> None:
    if not image_dir.is_dir():
        return
    for path in image_dir.iterdir():
        if path.suffix.lower() == ".jpg" and path.name not in keep:
            with contextlib.suppress(OSError):
                path.unlink()
    with contextlib.suppress(OSError):
        if not any(image_dir.iterdir()):
            image_dir.rmdir()


def cleanup_orphan_dirs(media_item_id: int, live_file_ids: set[int], assets_root: Path) -> None:
    """删掉条目下不再对应任何台账行的 chapters 子目录（文件被删/洗版替换后的孤儿）。"""
    base = assets_root / str(media_item_id) / "chapters"
    if not base.is_dir():
        return
    for sub in base.iterdir():
        if sub.is_dir() and sub.name.isdigit() and int(sub.name) not in live_file_ids:
            shutil.rmtree(sub, ignore_errors=True)


# ---------------------------------------------------------------------------
# 单文件 / 单条目刷新（设计文档 §4.5）
# ---------------------------------------------------------------------------


async def refresh_file_chapter_images(
    session: AsyncSession, row: LibraryFile, *, force: bool = False
) -> bool:
    """给一行台账补探章节（NULL 时）并抓图，写回 ``chapter_images``。

    返回是否有写入。库开关由调用方判断；这里只管资格（在位/非原盘/非 strm）。
    抓帧失败不抛：写 ``[]`` 并记日志，force 可重试；章节补探失败或 ffmpeg
    缺失则什么都不写（保持 NULL），下次入口自动再来。
    """
    from movieclaw_api.services.media_scrape import assets_root

    if row.id is None or row.media_item_id is None or not stills_eligible(row):
        return False
    if not force and row.chapter_images is not None:
        return False
    video = Path(row.file_path)
    if not await asyncio.to_thread(video.is_file):
        return False
    if row.chapters is None:
        # 存量行没探过章节：只读容器头，毫秒级。探不出来就不抓图——否则会按
        # 合成章节抓一套图落库，而这一行的章节事实永远停在 NULL
        probed = await asyncio.to_thread(probe_chapters, video)
        if probed is None:
            logger.warning("章节探测失败，场景图暂缓（下次自动重试）：%s", video)
            return False
        row.chapters = probed
    chapters = effective_chapters(row.chapters, row.duration_seconds)
    async with FRAME_GRAB_GATE:
        images = await asyncio.to_thread(
            extract_file_stills,
            video,
            media_item_id=row.media_item_id,
            file_id=row.id,
            chapters=chapters,
            duration_seconds=row.duration_seconds,
            hdr=row.hdr,
            assets_root=assets_root(),
            existing=chapter_image_map(row.chapter_images),
            force=force,
        )
    if images is None:
        return False  # ffmpeg 缺失：保持 NULL，装好后自动补
    row.chapter_images = images
    row.updated_at = utcnow()
    session.add(row)
    await session.commit()
    return True


async def _item_rows(session: AsyncSession, media_item_id: int) -> list[tuple[LibraryFile, bool]]:
    rows = (
        await session.execute(
            select(LibraryFile, Library.extract_chapter_images)
            .join(Library, Library.id == LibraryFile.library_id)  # type: ignore[arg-type]
            .where(LibraryFile.media_item_id == media_item_id)
            .order_by(LibraryFile.season_number, LibraryFile.episode_number, LibraryFile.id)
        )
    ).all()
    return [(row, bool(enabled)) for row, enabled in rows]


async def refresh_chapter_images(media_item_id: int, *, force: bool = False) -> int:
    """一个条目全部在位文件的章节场景图（单条目刷新/懒触发入口），返回处理的文件数。

    所在库关了开关的文件跳过。顺手清掉条目下的孤儿 chapters 目录。
    """
    from movieclaw_api.services.media_scrape import assets_root

    done = 0
    db = get_database()
    async with db.session() as session:
        rows = await _item_rows(session, media_item_id)
        live_ids = {row.id for row, _ in rows if row.id is not None}
        for row, enabled in rows:
            if not enabled:
                continue
            try:
                if await refresh_file_chapter_images(session, row, force=force):
                    done += 1
            except Exception:  # noqa: BLE001 -- 一个文件坏了不影响同条目其他文件
                logger.exception("条目 #%s 文件 #%s 章节场景图生成失败", media_item_id, row.id)
    await asyncio.to_thread(cleanup_orphan_dirs, media_item_id, live_ids, assets_root())
    return done


def item_pending(media_item_id: int) -> bool:
    """该条目是否正在后台抓图（详情页据此轮询）。"""
    return media_item_id in _in_flight


def schedule_item_chapter_images(media_item_id: int, *, force: bool = False) -> bool:
    """详情页懒触发：单条目后台补图（去重），返回是否在抓。

    只补没抓过的（升级后第一次打开旧条目不用等整库作业排到它），前端靠
    ``chapters_pending`` 轮询把图补上。**故意不做成 Job**：用户没有发起
    任何动作，一次次打开详情页却在任务中心堆出一串条目作业，既是噪音也
    违背"Job 只承载用户可感知、需要追踪的异步业务"（persistent-jobs.md）。
    重启把它丢了也无妨——台账没写回，下次打开详情页原地再触发一次，已经
    抓好的文件不会重做。

    条目菜单「重新生成章节」是用户明确发起、要能看进度也要能续跑的动作，
    走持久化 Job（``enqueue_item_chapter_images_job``），不走这里。
    """
    if media_item_id in _in_flight:
        return True
    _in_flight.add(media_item_id)

    async def _run() -> None:
        try:
            await refresh_chapter_images(media_item_id, force=force)
        except Exception:  # noqa: BLE001 -- 锦上添花的图，绝不影响详情页
            logger.exception("条目 #%s 章节场景图后台生成失败", media_item_id)
        finally:
            _in_flight.discard(media_item_id)

    asyncio.create_task(_run(), name=f"chapter-images-{media_item_id}")
    return True


# ---------------------------------------------------------------------------
# 整库作业（设计文档 §4.5）：扫描结束自动入队，也可从库菜单手动触发
# ---------------------------------------------------------------------------

JOB_TYPE = "library.chapter_images"


async def enqueue_library_chapter_images_job(
    session: AsyncSession,
    library_id: int,
    library_name: str,
    *,
    force: bool = False,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "system",
) -> jobs.CreateJobResult:
    """把整库抓图固化成可恢复 Job；同库同时只跑一份。"""
    return await jobs.create_job(
        session,
        job_type=JOB_TYPE,
        subject=library_name,
        input_data={"library_id": library_id, "force": bool(force)},
        resources=[jobs.ResourceRef("library", library_id)],
        dedupe_key=f"{JOB_TYPE}:{library_id}",
        conflict_policy="return_existing",
        handler_revision=f"{JOB_TYPE}.v1",
        max_attempts=2,
        priority=-10,  # 低优先级：让扫描/刷新这类用户等着看结果的作业先跑
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress=jobs.default_progress("等待生成章节"),
    )


#: Job 的未完成态：这些状态下管理页要把作业当"在跑"显示
_ACTIVE_JOB_STATUSES = frozenset(
    {"queued", "running", "retry_wait", "cancelling", "waiting", "blocked"}
)


def chapter_job_view(job: Job | None) -> ChapterJobView | None:
    """把该库最近一个章节作业投影成随库下发的状态；跑完/失败/取消的不再带出。

    进度字段沿用 JobContext.update_progress 写进 progress 的口径
    （current/total/percent，失败数在 details.failed）。
    """
    if job is None or str(job.status) not in _ACTIVE_JOB_STATUSES:
        return None
    progress = job.progress or {}
    details = progress.get("details") or {}
    return ChapterJobView(
        job_id=job.id,
        status=str(job.status),
        processed=int(progress.get("current") or 0),
        total=int(progress.get("total") or 0),
        failed=int(details.get("failed") or 0),
        percent=progress.get("percent"),
        stopping=job.cancel_requested_at is not None or str(job.status) == "cancelling",
    )


async def _job_targets(session: AsyncSession, library_id: int, *, force: bool) -> list[int]:
    """待处理的台账行 id：在位、非原盘/strm、没抓过图（force 时全部）。
    新入库的排前面——刚入库最可能被点开看。"""
    query = (
        select(LibraryFile.id, LibraryFile.file_path, LibraryFile.container)
        .where(
            LibraryFile.library_id == library_id,
            LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
            LibraryFile.in_place(),
        )
        .order_by(LibraryFile.created_at.desc(), LibraryFile.id.desc())
    )
    if not force:
        query = query.where(LibraryFile.chapter_images.is_(None))  # type: ignore[union-attr]
    rows = (await session.execute(query)).all()
    return [
        int(file_id)
        for file_id, path, container in rows
        if (container or "") not in ("bluray", "dvd", "iso")
        and not str(path).lower().endswith(STRM_EXT)
    ]


async def _run_targets(
    context: jobs.JobContext, targets: list[int], *, force: bool, subject: str
) -> tuple[int, int]:
    """逐文件抓图的公共循环（整库作业与条目作业共用），返回
    ``(累计已处理, 累计失败)``——两个数都是整轮作业的口径，跨重启累加。

    应用内更新与重启会把 Job 退回队列、再把处理器整体重跑一遍，所以两种
    断点都要有：

    - **补缺**（``force=False``）：逐文件写回台账即是检查点，重新取目标时
      ``chapter_images IS NULL`` 自然把已完成的行排除在外；
    - **重抓**（``force=True``）：每一行都要重做，台账不再是检查点，因此把
      "上一轮最后一个处理完的文件"记进进度的 ``details.cursor``，恢复时按
      同一份排序跳过它之前的行。进度写入有节流（1 秒），所以最多重做节流
      窗口里的那一两个文件——重抓一次是幂等的，代价只是几秒。

    进度也从断点接着走：重启后要是从 0 重新数到"剩下的文件数"，用户看到的
    就是又从头跑了一遍。
    """
    db = get_database()
    persisted = await context.current_progress()
    persisted_details = persisted.get("details")
    persisted_details = persisted_details if isinstance(persisted_details, dict) else {}
    processed = max(int(persisted.get("current") or 0), 0)
    failed = max(int(persisted_details.get("failed") or 0), 0)
    if force:
        # 游标那一行已经不在目标里（被删除/移走）时只能从头重抓，累计数一并
        # 归零，免得分母虚高
        cursor = persisted_details.get("cursor")
        position = targets.index(cursor) + 1 if cursor in targets else 0
        if position == 0:
            processed = failed = 0
        targets = targets[position:]
    if processed:
        logger.info(
            "%s的章节生成从断点继续：已完成 %s 个文件，还剩 %s 个",
            subject,
            processed,
            len(targets),
        )
    total = processed + len(targets)
    for file_id in targets:
        await context.raise_if_cancelled()
        async with db.session() as session:
            row = await session.get(LibraryFile, file_id)
            if row is not None:
                try:
                    await refresh_file_chapter_images(session, row, force=force)
                except Exception:  # noqa: BLE001 -- 单个文件失败不打断整批
                    failed += 1
                    logger.exception("文件 #%s 章节场景图生成失败", file_id)
        processed += 1
        if processed == total or context.progress_due():
            await context.update_progress(
                mode="determinate",
                phase="extracting",
                message=f"正在生成章节 {processed}/{total}",
                current=processed,
                total=total,
                percent=round(processed * 100 / total, 1) if total else 100.0,
                details={"failed": failed, "cursor": file_id},
            )
    return processed, failed


@jobs.register_job_handler(JOB_TYPE)
async def _run_chapter_images_job(
    context: jobs.JobContext, input_data: dict[str, Any]
) -> dict[str, Any]:
    """整库抓图处理器：目标是库内待处理的台账行，断点见 ``_run_targets``。"""
    library_id = int(input_data["library_id"])
    force = bool(input_data.get("force", False))
    db = get_database()
    async with db.session() as session:
        library = await session.get(Library, library_id)
        if library is None:
            raise jobs.JobFailed("媒体库已不存在，无法生成章节", code="LIBRARY_NOT_FOUND")
        if not library.extract_chapter_images:
            return {"message": f"「{library.name}」已关闭章节生成，本次未生成", "processed": 0}
        targets = await _job_targets(session, library_id, force=force)
    processed, failed = await _run_targets(
        context, targets, force=force, subject=f"媒体库 #{library_id}"
    )
    message = f"章节生成完成：处理 {processed} 个文件"
    if failed:
        message += f"，{failed} 个失败（可在库菜单重新生成）"
    return {"message": message, "processed": processed, "failed": failed}


# ---------------------------------------------------------------------------
# 条目作业（设计文档 §4.5 入口 3）：条目菜单「重新生成章节」
# ---------------------------------------------------------------------------

ITEM_JOB_TYPE = "media.chapter_images"


async def enqueue_item_chapter_images_job(
    session: AsyncSession,
    media_item_id: int,
    title: str,
    *,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "system",
) -> jobs.CreateJobResult:
    """把单条目重抓固化成可恢复 Job；同条目同时只跑一份。

    做成 Job 而不是裸协程（详情页懒触发那种）有三个理由：用户明确点了菜单，
    要在任务中心看到它、能停它；一部剧几十集重抓要跑很久，重启不该白跑；
    整库作业已经是 Job，两者共用同一套断点与进度口径。
    """
    return await jobs.create_job(
        session,
        job_type=ITEM_JOB_TYPE,
        subject=title,
        input_data={"media_item_id": media_item_id, "force": True},
        resources=[jobs.ResourceRef("media_item", media_item_id)],
        dedupe_key=f"{ITEM_JOB_TYPE}:{media_item_id}",
        conflict_policy="return_existing",
        handler_revision=f"{ITEM_JOB_TYPE}.v1",
        max_attempts=2,
        # 不压低优先级：用户正站在详情页等这一部的图，与整库那份（-10）不同
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress=jobs.default_progress(f"等待重新生成《{title}》的章节"),
    )


async def item_job_active(session: AsyncSession, media_item_id: int) -> bool:
    """该条目是否有未完成的重抓作业（详情页 ``chapters_pending`` 的另一半）。

    重启后裸协程没了、Job 还在队列里，只看内存里的 ``_in_flight`` 会让前端
    以为没在跑、停止轮询，图补上了也不刷新。
    """
    job = await jobs.latest_job_for_resource(
        session, "media_item", media_item_id, job_type=ITEM_JOB_TYPE
    )
    return job is not None and str(job.status) in _ACTIVE_JOB_STATUSES


@jobs.register_job_handler(ITEM_JOB_TYPE)
async def _run_item_chapter_images_job(
    context: jobs.JobContext, input_data: dict[str, Any]
) -> dict[str, Any]:
    """单条目重抓处理器：该条目全部在位文件按当前策略重抓，断点见 ``_run_targets``。"""
    from movieclaw_api.services.media_scrape import assets_root

    media_item_id = int(input_data["media_item_id"])
    force = bool(input_data.get("force", True))
    db = get_database()
    async with db.session() as session:
        rows = await _item_rows(session, media_item_id)
        if not rows:
            raise jobs.JobFailed("条目已不存在，无法生成章节", code="MEDIA_ITEM_NOT_FOUND")
        live_ids = {row.id for row, _ in rows if row.id is not None}
        targets = [
            row.id
            for row, enabled in rows
            if enabled and row.id is not None and stills_eligible(row)
        ]
    processed, failed = await _run_targets(
        context, targets, force=force, subject=f"条目 #{media_item_id}"
    )
    await asyncio.to_thread(cleanup_orphan_dirs, media_item_id, live_ids, assets_root())
    message = f"章节生成完成：处理 {processed} 个文件"
    if failed:
        message += f"，{failed} 个失败（可再次重新生成）"
    return {"message": message, "processed": processed, "failed": failed}
