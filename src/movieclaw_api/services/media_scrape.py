"""刮削管线——自足媒体库的元数据发动机（docs/design/metadata.md 第 3/4 节）。

一条管线，四个入口共用：

- **一次入库刮削**：条目建档时文本档案已随 ``ensure_media_item`` 落库（同一份
  TMDB 响应，零额外请求），图片与媒体目录镜像由 ``ensure_assets`` 补齐；
- **单条目手动刷新** / **整库刷新**：``scrape_media_item``（force=重新下载图片）；
- **定时分档保鲜**：``media_refresh`` 任务每 tick 对到期条目调 ``scrape_media_item``。

管线职责（一次完整执行）：

1. 拉 TMDB 档案（身份+展示，剧集含逐季集列表）；
2. 身份层合并（别名只增不减）+ 展示层三表 upsert（media_metadata /
   media_season / media_episode，集数据以本次档案为准增删）；
3. 订阅工单生长与档期同步（原 media_refresh 的职责，合流至此——任何入口
   写入新集都必须走同一段 diff，否则先写库的入口会"吃掉"新集信号，
   定时刷新再也发现不了它们）；
4. 图片资产下载（data/metadata/images/{条目 id}/，缺失才下，force 覆盖）；
5. 媒体目录镜像（poster.jpg/fanart.jpg/分集 thumb + 完整 NFO，Kodi/Emby
   规范，**只增不覆盖不删除**）。按库开关：``write_media_assets`` 是总闸，
   图片/NFO/分集剧照三项另可按库细分（library.scrape_overrides）。

图片与镜像失败均不阻断（保持 NULL/缺失，任一后续入口自愈）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, insert, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import jobs
from movieclaw_api.services.scrape_config import (
    effective_asset_sizes,
    effective_image_prefs,
    effective_language,
    effective_mirror_flags,
    profile_fetch_kwargs,
    resolve_scrape_library,
    scrape_setting_for_item,
)
from movieclaw_api.services.task_state import TaskState
from movieclaw_api.settings import MetadataScrapeSetting
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    ActivityType,
    Library,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaItemPerson,
    MediaMetadata,
    MediaSeason,
    Person,
    Subscription,
    SubscriptionActivity,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories import MediaItemRepository, SubscriptionRepository
from movieclaw_media.library import MediaProfile, fetch_media_profile
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbNotConfiguredError

logger = logging.getLogger("movieclaw_api.media_scrape")

_ENDED = frozenset({"Ended", "Canceled"})

# 进程内单飞 + 阶段：条目 id -> 当前阶段文案。既是"同一条目并发刮削后到者
# 直接返回"的互斥标志（与扫描 _scanning 同模式），也是详情页展示"正在做
# 什么"的数据源——单条目刷新与整库刷新因此共用同一套状态语言
_scraping: dict[int, str] = {}

# 图片下载全局并发闸：刮削可能来自扫描收尾的批量触发，别把图床打成 DoS
_asset_semaphore = asyncio.Semaphore(2)

# 阶段回调：整库刷新用它把"这部片正在做什么"实时报给前端（拉档案/写元数据/
# 下载图片/写媒体目录）。单条目刮削不传，零开销
PhaseHook = Callable[[str], None] | None


def is_scraping(media_item_id: int) -> bool:
    return media_item_id in _scraping


def scraping_phase(media_item_id: int) -> str | None:
    """该条目正在进行的刮削阶段；没在刮返回 None。"""
    return _scraping.get(media_item_id)


# ---------------------------------------------------------------------------
# 管线主入口
# ---------------------------------------------------------------------------


async def scrape_media_item(
    media_item_id: int, *, force: bool = False, on_phase: PhaseHook = None
) -> bool:
    """对一个条目执行完整刮削（自开会话，不向外抛异常；返回是否成功）。

    ``force``：图片资产已存在也重新下载（文本档案本就每次全量拉取覆盖）。
    ``on_phase``：额外的阶段回调（整库刷新用它汇总"哪几部在跑"）；无论有没有
    传，阶段都会写进 ``_scraping``，详情页据此展示与整库刷新一致的状态。
    """
    if media_item_id in _scraping:
        return False
    _scraping[media_item_id] = "排队中"

    def _record(phase: str) -> None:
        _scraping[media_item_id] = phase
        if on_phase is not None:
            on_phase(phase)

    try:
        return await _scrape(media_item_id, force=force, on_phase=_record)
    except TmdbNotConfiguredError:
        logger.warning("未配置 TMDB API Key，条目 #%s 刮削跳过", media_item_id)
        return False
    except Exception:  # noqa: BLE001 -- 后台任务兜底
        logger.exception("条目 #%s 刮削失败", media_item_id)
        return False
    finally:
        _scraping.pop(media_item_id, None)


async def _scrape(media_item_id: int, *, force: bool, on_phase: PhaseHook = None) -> bool:
    from movieclaw_api.services.media_discover import get_tmdb_client
    from movieclaw_api.services.subscription import (
        expected_units,
        recompute_subscription_status,
        refresh_release_forecasts,
    )

    def _phase(text: str) -> None:
        if on_phase is not None:
            on_phase(text)

    _phase("拉取 TMDB 档案")
    db = get_database()
    async with db.session() as session:
        repo = MediaItemRepository(session)
        item = await session.get(MediaItem, media_item_id)
        if item is None:
            return False
        kind = MediaKind(item.kind)
        # 语言/分级/选图按条目的**归属库**解析（设计文档 §14）：刷新任务没有
        # 库上下文，归属钉在条目上才不会被全局值洗回去
        fetch_kwargs = profile_fetch_kwargs(await scrape_setting_for_item(session, item))
        language = fetch_kwargs["languages"][0]
        client = get_tmdb_client()
        profile = await fetch_media_profile(client, kind, item.tmdb_id, **fetch_kwargs)

        # 本次刷新前已知的集：wanted 生长只补"新出现的集"（历史 diff 里
        # 被移除的工单不因刷新复活）
        known_keys = {
            (e.season_number, e.episode_number) for e in await repo.list_episodes(media_item_id)
        }

        _phase("写入元数据")
        _merge_identity(item, profile, await repo.get_metadata(media_item_id))
        await apply_display_profile(session, media_item_id, profile, language)

        subscription = (
            await session.execute(
                select(Subscription).where(Subscription.media_item_id == media_item_id)
            )
        ).scalar_one_or_none()

        item.metadata_refreshed_at = utcnow()
        item.next_refresh_at = utcnow() + next_refresh_delay(item, subscription)
        session.add(item)
        await session.commit()

        # 工单生长与档期同步（对 paused 订阅也生长：暂停只挡匹配与搜索）
        if subscription is not None and kind is MediaKind.TV:
            episodes = await repo.list_episodes(media_item_id)
            expected = expected_units(
                kind, episodes, list(subscription.selected_seasons), subscription.follow_future
            )
            await _grow_and_sync(session, subscription, item, expected, known_keys)
            await recompute_subscription_status(session, subscription, item)
            # 新集出现或 air_date 改档后立即重建目标快照。快照内带 target_air_date，
            # 即使本次刷新中断，读取端也不会使用旧档期的预测。
            await refresh_release_forecasts(session, media_item_ids={media_item_id})
        elif subscription is not None and kind is MediaKind.MOVIE:
            # 电影的"定档回填"：TMDB 档期出现/变化、状态翻到已上映时，把哨兵
            # 工单的搜索调度对齐最新上映信息（对应剧集 _grow_and_sync 的档期同步）
            await _sync_movie_schedule(session, subscription, item, profile.release_date)

    _phase("下载图片")
    await download_item_assets(media_item_id, force=force)
    _phase("写入媒体目录")
    await mirror_media_dir_assets(media_item_id, force=force)
    return True


async def ensure_assets(media_item_id: int) -> None:
    """建档后的资产补齐入口（图片下载 + 媒体目录镜像，不重拉文本档案）。

    文本档案在 ``ensure_media_item`` 建档事务里已经落库（同一份 TMDB 响应），
    这里只补磁盘侧的活。扫描收尾/入库管线/人工认领挂锚后调用；失败只
    记日志，任一后续刷新入口自愈。
    """
    try:
        await download_item_assets(media_item_id)
        await mirror_media_dir_assets(media_item_id)
    except Exception:  # noqa: BLE001 -- 资产是锦上添花，绝不影响主流程
        logger.exception("条目 #%s 资产补齐失败（下次刷新自愈）", media_item_id)


# ---------------------------------------------------------------------------
# 整库刷新（docs/design/metadata.md 4.3）：遍历库内全部已识别条目逐个刮削
# ---------------------------------------------------------------------------

# 并发度：TMDB 有限速、图床还有独立的下载闸，3 路是"明显快于串行、又不至于
# 撞限流"的折中（真正的瓶颈是每部片的图片下载，不是 CPU）
_REFRESH_CONCURRENCY = 3


@dataclass
class RefreshState:
    """一次整库刷新的实时状态——刷全库很慢（每部都重下图），用户需要看到
    "到哪部了、正在做什么"，而不是一个干等的百分比。"""

    total: int = 0
    processed: int = 0  # 已完成（含失败）
    failed: int = 0
    # 正在处理的条目：media_item_id -> (标题, 当前阶段)。并发 3 路，
    # 所以是列表而非单个
    active: dict[int, tuple[str, str]] = field(default_factory=dict)
    stopping: bool = False


# 进程内状态三件套（单飞 / 实时状态 / 停止请求），容器统一为 TaskState
_refresh_tasks: TaskState[RefreshState] = TaskState()


def is_library_refreshing(library_id: int) -> bool:
    return _refresh_tasks.running(library_id)


def library_refresh_state(library_id: int) -> RefreshState | None:
    """进行中整库刷新的实时状态；没在刷返回 None。"""
    return _refresh_tasks.state_of(library_id)


def request_stop_library_refresh(library_id: int) -> bool:
    """请求停止整库刷新；该库没在刷返回 False。"""
    if not _refresh_tasks.request_stop(library_id):
        return False
    state = _refresh_tasks.state_of(library_id)
    if state is not None:
        state.stopping = True
    return True


async def _library_refresh_targets(session: AsyncSession, library_id: int) -> list[tuple[int, str]]:
    """整库刷新的目标条目：库内有台账行的 ∪ 刮削归属到该库的无文件条目。

    第二类是订阅追踪中的条目（已订阅、尚无任何库存文件）：它们在库页
    「追踪中」区块带海报展示，若只按台账行取目标，改选图偏好后整库刷新
    对它们会静默失效——进度显示全量成功，用户无从察觉有条目不在范围内
    （#278）。候选面很小（有订阅或显式归属的无文件条目），逐个按
    docs/design/scrape-customization.md §14 的规则解析归属即可。
    """
    result = await session.execute(
        select(LibraryFile.media_item_id, MediaItem.title)
        .join(MediaItem, MediaItem.id == LibraryFile.media_item_id)  # type: ignore[arg-type]
        .where(LibraryFile.library_id == library_id)
        .distinct()
        .order_by(LibraryFile.media_item_id)
    )
    targets = [(i, t) for i, t in result.all() if i is not None]

    library = await session.get(Library, library_id)
    if library is None:
        return targets
    any_file = select(LibraryFile.media_item_id).where(
        LibraryFile.media_item_id.is_not(None)  # type: ignore[union-attr]
    )
    subscribed = select(Subscription.media_item_id)
    candidates = (
        (
            await session.execute(
                select(MediaItem)
                .where(
                    MediaItem.kind == library.kind,
                    MediaItem.id.not_in(any_file),  # type: ignore[union-attr]
                    (MediaItem.scrape_library_id == library_id)
                    | MediaItem.id.in_(subscribed),  # type: ignore[union-attr]
                )
                .order_by(MediaItem.id)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )
    for item in candidates:
        resolved = await resolve_scrape_library(session, item)
        if resolved is not None and resolved.id == library_id and item.id is not None:
            targets.append((item.id, item.title))
    return targets


async def refresh_library_metadata(library_id: int) -> None:
    """整库刷新（后台任务入口）：库内全部已识别条目**全量重刮**。

    全量语义（用户决策 2026-07-26）：每次点刷新都当作 force——文本档案
    重拉、图片按当前尺寸档位重下、媒体目录镜像覆盖。存量库因此不需要任何
    额外的差异机制，"想更新就点一下"即可（手动选定的图仍受锁保护）。

    代价是慢（每部片都要重下图），所以状态报得尽量细：总数/已完成/失败 +
    正在处理的每部片和它当前的阶段。并发 3 路，刮削幂等，中途停止再启动
    只是重复请求不是重复数据。
    """
    state = RefreshState()
    if not _refresh_tasks.try_start(library_id, state):
        return
    try:
        db = get_database()
        async with db.session() as session:
            targets = await _library_refresh_targets(session, library_id)
        state.total = len(targets)

        queue: asyncio.Queue = asyncio.Queue()
        for target in targets:
            queue.put_nowait(target)

        async def _worker() -> None:
            while True:
                try:
                    item_id, title = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if _refresh_tasks.stop_requested(library_id):
                    return
                state.active[item_id] = (title, "排队中")
                try:
                    ok = await scrape_media_item(
                        item_id,
                        force=True,
                        on_phase=lambda phase, _id=item_id, _t=title: state.active.__setitem__(
                            _id, (_t, phase)
                        ),
                    )
                    if not ok:
                        state.failed += 1
                finally:
                    state.active.pop(item_id, None)
                    state.processed += 1

        await asyncio.gather(*(_worker() for _ in range(_REFRESH_CONCURRENCY)))
        if _refresh_tasks.stop_requested(library_id):
            logger.info(
                "媒体库 #%s 整库元数据刷新被手动停止（已处理 %d / 共 %d）",
                library_id,
                state.processed,
                state.total,
            )
        else:
            logger.info(
                "媒体库 #%s 整库元数据刷新完成：共 %d 个条目（失败 %d）",
                library_id,
                state.total,
                state.failed,
            )
    except Exception:  # noqa: BLE001 -- 后台任务兜底
        logger.exception("媒体库 #%s 整库元数据刷新时发生未知错误", library_id)
    finally:
        _refresh_tasks.finish(library_id)


async def enqueue_library_metadata_refresh_job(
    session: AsyncSession,
    library_id: int,
    library_name: str,
    *,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "web",
) -> jobs.CreateJobResult:
    """把整库刷新固化成可恢复 Job；目标快照保证更新前后处理口径一致。"""
    targets = [
        {"media_item_id": item_id, "title": title}
        for item_id, title in await _library_refresh_targets(session, library_id)
    ]
    return await jobs.create_job(
        session,
        job_type="library.metadata.refresh",
        subject=library_name,
        input_data={"library_id": library_id, "targets": targets},
        resources=[jobs.ResourceRef("library", library_id)],
        dedupe_key=f"library.metadata.refresh:{library_id}",
        conflict_policy="return_existing",
        handler_revision="library.metadata.refresh.v1",
        max_attempts=3,
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress={
            **jobs.default_progress("等待刷新媒体库元数据"),
            "total": len(targets),
            "details": {"failed": 0, "active": []},
        },
    )


async def enqueue_item_metadata_refresh_job(
    session: AsyncSession,
    *,
    library_id: int,
    media_item_id: int,
    title: str,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "web",
) -> jobs.CreateJobResult:
    """创建单条目刷新 Job；媒体条目资源锁避免与转移或另一刷新并发。"""
    return await jobs.create_job(
        session,
        job_type="media.metadata.refresh",
        subject=title,
        input_data={
            "library_id": library_id,
            "media_item_id": media_item_id,
            "title": title,
        },
        resources=[
            jobs.ResourceRef("media_item", media_item_id),
            # 刮削末段会向媒体目录写 NFO/图片，必须与整理/转移的路径变更
            # 共用库级资源锁，不能只把 library 当展示容器。
            jobs.ResourceRef("library", library_id),
        ],
        dedupe_key=f"media.metadata.refresh:{media_item_id}",
        conflict_policy="return_existing",
        handler_revision="media.metadata.refresh.v1",
        max_attempts=3,
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress={
            **jobs.default_progress("等待刷新条目元数据"),
            "details": {"media_item_id": media_item_id, "title": title},
        },
    )


@jobs.register_job_handler("library.metadata.refresh")
async def _run_library_metadata_refresh_job(
    context: jobs.JobContext, input_data: dict[str, object]
) -> dict[str, object]:
    """整库刷新处理器：每批三个条目落一次检查点，重启最多重复当前批次。

    单条目刮削本身幂等；检查点只在一批完整收口后前移，因此服务更新发生
    在任意网络请求或图片写入阶段都不会越过尚未完成的条目。
    """
    library_id = int(input_data["library_id"])
    raw_targets = input_data.get("targets")
    targets = list(raw_targets) if isinstance(raw_targets, list) else []
    persisted = await context.current_progress()
    resume_at = min(max(int(persisted.get("current") or 0), 0), len(targets))
    details = persisted.get("details") if isinstance(persisted.get("details"), dict) else {}
    failed = int(details.get("failed") or 0)
    state = RefreshState(total=len(targets), processed=resume_at, failed=failed)
    if not _refresh_tasks.try_start(library_id, state):
        raise jobs.JobRetry("该媒体库已有元数据刷新正在收尾", delay_seconds=5)

    try:
        for start in range(resume_at, len(targets), _REFRESH_CONCURRENCY):
            await context.raise_if_cancelled()
            batch = targets[start : start + _REFRESH_CONCURRENCY]

            async def _refresh_one(raw: object) -> bool:
                if not isinstance(raw, dict):
                    return False
                item_id = int(raw["media_item_id"])
                title = str(raw.get("title") or f"条目 #{item_id}")
                state.active[item_id] = (title, "排队中")
                try:
                    return await scrape_media_item(
                        item_id,
                        force=True,
                        on_phase=lambda phase, _id=item_id, _title=title: state.active.__setitem__(
                            _id, (_title, phase)
                        ),
                    )
                finally:
                    state.active.pop(item_id, None)

            outcomes = await asyncio.gather(*(_refresh_one(raw) for raw in batch))
            failed += sum(not ok for ok in outcomes)
            state.failed = failed
            state.processed = start + len(batch)
            percent = state.processed / len(targets) * 100 if targets else 100.0
            await context.update_progress(
                mode="determinate",
                phase="refreshing",
                message=f"已刷新 {state.processed} / {len(targets)} 个条目",
                current=state.processed,
                total=len(targets),
                percent=round(percent, 1),
                details={"failed": failed, "active": []},
            )

        message = f"元数据刷新完成，共 {len(targets)} 个条目"
        if failed:
            message += f"，其中 {failed} 个未完成"
        logger.info("媒体库 #%s %s", library_id, message)
        return {
            "message": message,
            "library_id": library_id,
            "processed": len(targets),
            "total": len(targets),
            "failed": failed,
        }
    finally:
        _refresh_tasks.finish(library_id)


@jobs.register_job_handler("media.metadata.refresh")
async def _run_item_metadata_refresh_job(
    context: jobs.JobContext, input_data: dict[str, object]
) -> dict[str, object]:
    """单条目刷新处理器；阶段写入公共进度，取消与重启都可安全重跑。"""
    media_item_id = int(input_data["media_item_id"])
    title = str(input_data.get("title") or f"条目 #{media_item_id}")
    if is_scraping(media_item_id):
        raise jobs.JobRetry(f"《{title}》已有一次刮削正在收尾", delay_seconds=5)

    phase = "准备刷新"

    def _phase(value: str) -> None:
        nonlocal phase
        phase = value

    runner = asyncio.create_task(
        scrape_media_item(media_item_id, force=True, on_phase=_phase),
        name=f"metadata-refresh-{media_item_id}",
    )
    last_phase = ""
    try:
        while not runner.done():
            await context.raise_if_cancelled()
            if phase != last_phase:
                await context.update_progress(
                    mode="indeterminate",
                    phase="refreshing",
                    message=phase,
                    details={"media_item_id": media_item_id, "title": title},
                )
                last_phase = phase
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(runner), timeout=0.5)
        if not await runner:
            raise jobs.JobFailed(
                f"《{title}》元数据刷新未完成，请检查 TMDB 配置或后端日志",
                code="METADATA_REFRESH_FAILED",
                actions=[
                    {"type": "retry_job", "label": "重试"},
                    {"type": "open_settings", "label": "检查设置", "target": "metadata"},
                ],
            )
        return {
            "message": f"《{title}》元数据刷新完成",
            "library_id": int(input_data["library_id"]),
            "media_item_id": media_item_id,
        }
    finally:
        if not runner.done():
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner


# ---------------------------------------------------------------------------
# 身份层合并与展示层 upsert
# ---------------------------------------------------------------------------


def _merge_identity(
    item: MediaItem, profile: MediaProfile, meta: MediaMetadata | None = None
) -> None:
    """身份字段合并：别名只增不减（来源信息不丢），其余字段新值优先。

    图片路径受**手动选图锁**保护：用户挑过的图，选图策略重算出别的结果
    也不覆盖（docs/design/metadata.md 6.3）。
    """
    merged_aliases = list(item.aliases)
    seen = set(merged_aliases)
    for alias in profile.aliases:
        if alias not in seen:
            merged_aliases.append(alias)
            seen.add(alias)
    item.title = profile.title or item.title
    item.original_title = profile.original_title or item.original_title
    item.year = profile.year or item.year
    item.status = profile.status or item.status
    if meta is None or not meta.poster_locked:
        item.poster_path = profile.poster_path or item.poster_path
    if meta is None or not meta.backdrop_locked:
        item.backdrop_path = profile.backdrop_path or item.backdrop_path
    item.aliases = merged_aliases
    item.imdb_id = item.imdb_id or profile.imdb_id


async def apply_people_credits(
    session: AsyncSession, media_item_id: int, profile: MediaProfile, now=None
) -> None:
    """影人实体与参演/执导关系的 upsert（人物页的数据来源）。

    与集数据同样是**增删改**：本次档案里没有的关系要删掉，否则 TMDB 改了
    演员表之后，库里会残留「这个人还在这部片里」的假关系，人物页就会列出
    一部他其实没参演的片。

    ``person`` 行只增不删——同一个人往往参演多部片，删掉某部的关系不代表
    这个人该从库里消失；真正的孤儿行由 media_item 级联删除后留下，量小且
    无害（下次刮到同一个人会直接复用）。
    """
    now = now or utcnow()
    credits = profile.people
    if not credits:
        # 仍要往下走：本次没有演职员时，残留的旧关系同样必须清掉
        person_ids: dict[int, int] = {}
    else:
        # 一次 IN 查询取回已有影人，而不是逐个 SELECT。一部片最多 45 位
        # （40 演员 + 5 导演），逐个查会让整库刷新的 SQL 次数乘以两位数
        tmdb_ids = [c.tmdb_person_id for c in credits]
        rows = {
            r.tmdb_person_id: r
            for r in (
                await session.execute(select(Person).where(Person.tmdb_person_id.in_(tmdb_ids)))
            ).scalars()
        }
        for credit in credits:
            row = rows.get(credit.tmdb_person_id)
            if row is None:
                row = Person(tmdb_person_id=credit.tmdb_person_id, name=credit.name)
                session.add(row)
                rows[credit.tmdb_person_id] = row
            # 姓名/头像以最近一次刮削为准：换了刮削语言时人物页要跟着变。
            # 先比对再赋值：整库刷新时绝大多数影人一字未改，无条件写会让
            # 每轮刷新平白产生「条目数 × 演职员数」行的 UPDATE
            if (
                row.name != credit.name
                or row.original_name != credit.original_name
                or row.profile_path != credit.profile_path
            ):
                row.name = credit.name
                row.original_name = credit.original_name
                row.profile_path = credit.profile_path
                row.updated_at = now
        # 一次 flush 拿到全部新行的自增 id（逐个 flush 是上一版的性能问题所在）
        await session.flush()
        person_ids = {tid: r.id for tid, r in rows.items() if r.id is not None}

    existing = {
        (r.person_id, r.department): r
        for r in (
            await session.execute(
                select(MediaItemPerson).where(MediaItemPerson.media_item_id == media_item_id)
            )
        ).scalars()
    }
    seen: set[tuple[int, str]] = set()
    # 新关系走 Core 批量插入而不是逐行 session.add：一部片 20~45 条关系，
    # 逐行插会让整库扫描的 INSERT 次数直接等于「条目数 × 演职员数」，
    # 实测那是首次扫描里最大的一项 SQL 开销。这些行插完不需要回读对象，
    # 绕开 ORM 的单元工作正合适
    new_links: list[dict] = []
    for credit in credits:
        key = (person_ids[credit.tmdb_person_id], credit.department)
        seen.add(key)
        link = existing.get(key)
        if link is None:
            new_links.append(
                {
                    "media_item_id": media_item_id,
                    "person_id": key[0],
                    "department": credit.department,
                    "character": credit.character,
                    "credit_order": credit.credit_order,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            continue
        # 已有关系：只在真有变化时才动，避免整库刷新时白写
        if link.character != credit.character or link.credit_order != credit.credit_order:
            link.character = credit.character
            link.credit_order = credit.credit_order
            link.updated_at = now
            session.add(link)
    if new_links:
        await session.execute(insert(MediaItemPerson), new_links)

    stale = [key for key in existing if key not in seen]
    if stale:
        # 同理批量删：逐个 session.delete 会产生 N 条 DELETE
        await session.execute(
            delete(MediaItemPerson).where(
                MediaItemPerson.media_item_id == media_item_id,
                tuple_(MediaItemPerson.person_id, MediaItemPerson.department).in_(stale),
            )
        )


async def apply_display_profile(
    session: AsyncSession, media_item_id: int, profile: MediaProfile, language: str
) -> None:
    """展示层三表 upsert（不 commit，调用方掌握事务边界）。

    集数据以本次档案为准**增删改**：TMDB 偶有删集/重排，残留的旧行会污染
    期望集合展开。本地图片路径字段（*_file）不在此处碰——那是资产下载的
    地盘，文本刷新不应把已下载的图打回 NULL。
    """
    repo = MediaItemRepository(session)
    now = utcnow()

    meta = await repo.get_metadata(media_item_id)
    if meta is None:
        meta = MediaMetadata(media_item_id=media_item_id)
    meta.overview = profile.overview
    meta.tagline = profile.tagline
    meta.genres = list(profile.genres)
    meta.genre_ids = list(profile.genre_ids)
    meta.runtime_minutes = profile.runtime_minutes
    meta.release_date = profile.release_date
    meta.content_rating = profile.content_rating
    meta.original_language = profile.original_language
    meta.origin_countries = list(profile.origin_countries)
    meta.studios = list(profile.studios)
    meta.vote_average = profile.vote_average
    meta.vote_count = profile.vote_count
    meta.directors = list(profile.directors)
    meta.cast = [c.model_dump() for c in profile.cast]
    meta.scraped_at = now
    meta.scrape_language = language
    meta.updated_at = now
    session.add(meta)

    await apply_people_credits(session, media_item_id, profile, now)

    existing_seasons = {s.season_number: s for s in await repo.list_seasons(media_item_id)}
    for season_profile in profile.seasons:
        row = existing_seasons.get(season_profile.season_number)
        if row is None:
            row = MediaSeason(
                media_item_id=media_item_id, season_number=season_profile.season_number
            )
        row.name = season_profile.name
        row.air_date = season_profile.air_date
        row.episode_count = season_profile.episode_count
        row.overview = season_profile.overview
        row.poster_path = season_profile.poster_path
        row.updated_at = now
        session.add(row)

    existing_episodes = {
        (e.season_number, e.episode_number): e for e in await repo.list_episodes(media_item_id)
    }
    fresh_keys: set[tuple[int, int]] = set()
    for season_profile in profile.seasons:
        for episode in season_profile.episodes:
            key = (season_profile.season_number, episode.episode_number)
            fresh_keys.add(key)
            row = existing_episodes.get(key)
            if row is None:
                row = MediaEpisode(
                    media_item_id=media_item_id,
                    season_number=season_profile.season_number,
                    episode_number=episode.episode_number,
                )
            row.name = episode.name
            row.overview = episode.overview
            row.air_date = _parse_iso_date(episode.air_date)
            row.runtime_minutes = episode.runtime_minutes
            row.vote_average = episode.vote_average
            row.still_path = episode.still_path
            row.updated_at = now
            session.add(row)
    for key, row in existing_episodes.items():
        if key not in fresh_keys and profile.seasons:
            await session.delete(row)


def build_display_rows(
    profile: MediaProfile, language: str
) -> tuple[list[MediaSeason], list[MediaEpisode], MediaMetadata]:
    """建档路径的展示层新行（media_item_id 由 Repository 落库时补）。"""
    now = utcnow()
    seasons = [
        MediaSeason(
            media_item_id=0,
            season_number=s.season_number,
            name=s.name,
            air_date=s.air_date,
            episode_count=s.episode_count,
            overview=s.overview,
            poster_path=s.poster_path,
        )
        for s in profile.seasons
    ]
    episodes = [
        MediaEpisode(
            media_item_id=0,
            season_number=s.season_number,
            episode_number=e.episode_number,
            name=e.name,
            overview=e.overview,
            air_date=_parse_iso_date(e.air_date),
            runtime_minutes=e.runtime_minutes,
            vote_average=e.vote_average,
            still_path=e.still_path,
        )
        for s in profile.seasons
        for e in s.episodes
    ]
    metadata = MediaMetadata(
        media_item_id=0,
        overview=profile.overview,
        tagline=profile.tagline,
        genres=list(profile.genres),
        genre_ids=list(profile.genre_ids),
        runtime_minutes=profile.runtime_minutes,
        release_date=profile.release_date,
        content_rating=profile.content_rating,
        original_language=profile.original_language,
        origin_countries=list(profile.origin_countries),
        studios=list(profile.studios),
        vote_average=profile.vote_average,
        vote_count=profile.vote_count,
        directors=list(profile.directors),
        cast=[c.model_dump() for c in profile.cast],
        scraped_at=now,
        scrape_language=language,
    )
    return seasons, episodes, metadata


def _parse_iso_date(raw: str | None):
    from datetime import date

    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 订阅工单生长（原 media_refresh._grow_and_sync，随管线合流迁入）
# ---------------------------------------------------------------------------


def next_refresh_delay(item: MediaItem, subscription: Subscription | None) -> timedelta:
    """刷新分档（docs/design/subscription-p4.md 第 6 节 / metadata.md 4.4）。"""
    if subscription is None:
        return timedelta(days=30)  # 无订阅：低频保鲜档案，防条目腐烂
    if item.kind == MediaKind.TV.value and (item.status or "") not in _ENDED:
        return timedelta(hours=8)  # 在播/未完结剧：新集的发动机
    if item.kind == MediaKind.MOVIE.value and (item.status or "") != "Released":
        return timedelta(hours=24)  # 未上映电影：等定档/上映
    return timedelta(days=7)  # 已完结/已上映：低频


async def _grow_and_sync(
    session: AsyncSession,
    subscription: Subscription,
    item: MediaItem,
    expected: list,
    known_keys: set[tuple[int, int]],
) -> None:
    from movieclaw_api.services.subscription import schedule_for
    from movieclaw_db.repositories.library_file_repo import LibraryFileRepository

    repo = SubscriptionRepository(session)
    assert subscription.id is not None
    existing = {
        (w.season_number, w.episode_number): w for w in await repo.list_wanted(subscription.id)
    }
    # 库存 H：库里已有的单元不再补单（E−H 用真实的 H，媒体库 L3 联通）
    owned = await LibraryFileRepository(session).owned_units(subscription.media_item_id)

    to_add: list[WantedItem] = []
    for unit in expected:
        key = (unit.season_number, unit.episode_number)
        wanted = existing.get(key)
        if wanted is None:
            if key in known_keys:
                # 早已存在于季集数据、但不在工单里的单元属于历史 diff 结果
                # （如追新期间被移除），不因刷新复活——只有**新出现的集**才补
                continue
            if key in owned:
                continue  # 库里已经有这一集，无需追
            next_search, priority = schedule_for(subscription.kind, unit)
            to_add.append(
                WantedItem(
                    subscription_id=subscription.id,
                    media_item_id=subscription.media_item_id,
                    season_number=unit.season_number,
                    episode_number=unit.episode_number,
                    status=WantedStatus.WANTED,
                    air_date=unit.air_date,
                    priority=priority,
                    next_search_at=next_search,
                )
            )
            continue
        # 档期同步：未定档→定档回填调度；已在退避中的（attempts>0）不打扰
        if (
            wanted.status == WantedStatus.WANTED
            and wanted.air_date != unit.air_date
            and wanted.search_attempts == 0
        ):
            next_search, priority = schedule_for(subscription.kind, unit)
            wanted.air_date = unit.air_date
            wanted.next_search_at = next_search
            wanted.priority = priority
            wanted.updated_at = utcnow()
            session.add(wanted)
    if to_add:
        await repo.add_wanted(to_add)
        first = to_add[0]
        label = f"S{first.season_number:02d}E{first.episode_number:02d}"
        await repo.add_activity(
            SubscriptionActivity(
                subscription_id=subscription.id,
                type=ActivityType.WANTED_ADDED,
                message=(f"元数据刷新发现 {len(to_add)} 个新集（{label} 起），已加入追踪"),
                payload={"units": [[w.season_number, w.episode_number] for w in to_add]},
            )
        )
        logger.info("《%s》发现 %d 个新集，已补工单", item.title, len(to_add))
    else:
        await session.commit()  # 只有档期同步时也要落盘


async def _sync_movie_schedule(
    session: AsyncSession,
    subscription: Subscription,
    item: MediaItem,
    release_date,
) -> None:
    """电影订阅的档期同步：把 (0,0) 哨兵工单的搜索调度对齐最新上映信息。

    只同步既有工单、绝不补建（电影没有"生长"；文件丢失走「重新下载」）。
    改写规则：
    - 未定档 → 定档/上映：任何时候都回填——用户强制搜过（attempts>0）也不能
      让工单永远卡在不可调度；
    - 已到期（next_search_at<=now，含用户「立即搜索/重新下载」强制的 now）：
      绝不回撤——那是等待搜索管线消费的排队信号，刷新往未来/NULL 改写会把
      用户的强制悄悄吞掉；
    - 已排期在未来 → 档期变化：仅未搜过（attempts==0）时改写；退避中的不打扰。
    """
    from movieclaw_api.services.subscription import movie_schedule

    assert subscription.id is not None
    repo = SubscriptionRepository(session)
    rows = await repo.list_wanted(subscription.id)
    row = next((w for w in rows if w.season_number == 0 and w.episode_number == 0), None)
    if row is None or row.status != WantedStatus.WANTED:
        return
    next_search, priority = movie_schedule(release_date, item.status)
    now = utcnow()
    if row.next_search_at is None:
        changed = next_search is not None
    elif row.next_search_at <= now:
        changed = False  # 已到期/用户强制：让搜索管线消费，刷新不回撤
    elif next_search is None:
        changed = row.search_attempts == 0  # 撤档：真没档期就别到点空搜
    else:
        changed = row.search_attempts == 0 and next_search != row.next_search_at
    if not changed:
        return
    row.next_search_at = next_search
    row.priority = priority
    row.updated_at = now
    session.add(row)
    if next_search is not None:
        message = f"《{item.title}》档期更新，{next_search.date().isoformat()} 起开始搜索资源"
    else:
        message = f"《{item.title}》档期撤销，重新定档/上映后再安排搜索"
    await repo.add_activity(
        SubscriptionActivity(
            subscription_id=subscription.id,
            type=ActivityType.ADJUSTED,
            message=message,
            payload={
                "release_date": release_date.isoformat() if release_date else None,
                "status": item.status,
            },
        )
    )
    logger.info("电影《%s》档期同步：%s", item.title, message)


# ---------------------------------------------------------------------------
# 图片资产（事实源：data/metadata/images/{条目 id}/，docs/design/metadata.md 6.1）
# ---------------------------------------------------------------------------


def _asset_sizes(setting: MetadataScrapeSetting | None = None) -> tuple[str, str, str]:
    """生效的 (海报, 背景, 剧照) 尺寸档位（库覆盖 > 设置页 > 环境变量）。

    ``setting`` 传条目按归属库合并出来的设置；不传则全局口径。
    """
    return effective_asset_sizes(setting)


def assets_root() -> Path:
    """图片资产根目录（/images/assets 路由与刮削管线共用）。"""
    return Path(get_settings().metadata_dir) / "images"


async def cleanup_orphan_items(media_item_ids: Iterable[int]) -> int:
    """清理孤儿条目：**已不在任何媒体库、也没有订阅**的条目连同其图片资产
    一起删除，返回清理数量。

    删库/删条目之后调用。不这么做的话，条目行与 `data/metadata/images/{id}/`
    会永远留着（背景图按 original 档动辄 1~3MB/部，反复建删库会攒下可观的
    垃圾）。判定必须同时看两处引用：
    - 还在别的库里（同一部剧的集分散在两个库是常态）→ 保留；
    - 还有订阅盯着（追新中，文件迟早回来）→ 保留。
    条目删除后 media_metadata / media_season / media_episode 由外键级联清掉。

    磁盘删除单独放线程池；某个目录删不掉只记日志，不阻断其余清理
    （宁可留点垃圾，也不能让删库这类操作半途失败）。
    """
    from movieclaw_db.models import Subscription

    ids = list(dict.fromkeys(media_item_ids))
    if not ids:
        return 0
    removed: list[int] = []
    db = get_database()
    async with db.session() as session:
        for item_id in ids:
            in_library = (
                await session.execute(
                    select(LibraryFile.id).where(LibraryFile.media_item_id == item_id).limit(1)
                )
            ).scalar_one_or_none()
            if in_library is not None:
                continue
            subscribed = (
                await session.execute(
                    select(Subscription.id).where(Subscription.media_item_id == item_id).limit(1)
                )
            ).scalar_one_or_none()
            if subscribed is not None:
                continue
            item = await session.get(MediaItem, item_id)
            if item is None:
                continue
            await session.delete(item)
            removed.append(item_id)
        if removed:
            await session.commit()

    for item_id in removed:
        await asyncio.to_thread(_remove_asset_dir, item_id)
    if removed:
        logger.info("已清理 %d 个无引用条目及其图片资产（条目 %s）", len(removed), removed)
    return len(removed)


def _remove_asset_dir(media_item_id: int) -> None:
    directory = assets_root() / str(media_item_id)
    if not directory.is_dir():
        return
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        logger.warning("图片资产目录删除失败（残留不影响功能）：%s（%s）", directory, exc)


def asset_version(rel_path: str | None) -> int:
    """资产文件的版本戳（mtime 秒）——图片 URL 的缓存击穿参数。

    换图后路径不变、只有内容变（poster.jpg 原地覆盖），URL 不带版本的话
    浏览器会拿着缓存里的旧图一直显示，用户看到的就是"换了但没生效"
    （实测踩过）。mtime 变即 URL 变，浏览器自然重取。
    """
    if not rel_path:
        return 0
    try:
        return int((assets_root() / rel_path).stat().st_mtime)
    except OSError:
        return 0


def file_version(path: Path | None) -> int:
    """任意本地文件的版本戳（同 ``asset_version``，用于条目目录美术图）。"""
    if path is None:
        return 0
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


async def download_item_assets(
    media_item_id: int, *, force: bool = False, ignore_locks: bool = False
) -> None:
    """下载条目的全部图片资产（缺失才下，force 覆盖重下）。

    分集剧照只给**在库条目**下（订阅了几百集但一集未入库的剧，几百张
    剧照没有消费方，白占磁盘与请求量）。单张失败保持 NULL 不阻断，
    任一后续刷新入口自愈。手动选定（locked）的海报/背景 force 也不重下
    ——那张图就是用户要的（docs/design/metadata.md 6.3）；``ignore_locks``
    是选图动作自己的通道：刚选的图必须落盘，此时锁就是它自己加的。
    """
    db = get_database()
    async with db.session() as session:
        repo = MediaItemRepository(session)
        item = await session.get(MediaItem, media_item_id)
        if item is None:
            return
        meta = await repo.get_metadata(media_item_id)
        if meta is None:
            # 理论上建档即有档案行；兜底建一行以承载图片路径
            meta = MediaMetadata(media_item_id=media_item_id)
            session.add(meta)
        seasons = await repo.list_seasons(media_item_id)
        episodes = await repo.list_episodes(media_item_id)
        has_files = (
            await session.execute(
                select(LibraryFile.id).where(LibraryFile.media_item_id == media_item_id).limit(1)
            )
        ).scalar_one_or_none() is not None

        item_dir = assets_root() / str(media_item_id)
        base = get_settings().tmdb_image_base_url.rstrip("/")
        poster_size, backdrop_size, still_size = _asset_sizes(
            await scrape_setting_for_item(session, item)
        )
        sources = await asyncio.to_thread(_load_asset_sources, item_dir)
        saved_sources = dict(sources)

        async with _asset_semaphore:
            meta.poster_file = await _sync_asset(
                base,
                poster_size,
                item.poster_path,
                item_dir / "poster.jpg",
                meta.poster_file,
                force and (ignore_locks or not meta.poster_locked),
                sources,
                "poster",
                locked=meta.poster_locked and not ignore_locks,
            )
            meta.backdrop_file = await _sync_asset(
                base,
                backdrop_size,
                item.backdrop_path,
                item_dir / "backdrop.jpg",
                meta.backdrop_file,
                force and (ignore_locks or not meta.backdrop_locked),
                sources,
                "backdrop",
                locked=meta.backdrop_locked and not ignore_locks,
            )
            session.add(meta)
            for season in seasons:
                season.poster_file = await _sync_asset(
                    base,
                    poster_size,
                    season.poster_path,
                    item_dir / f"season-{season.season_number}.jpg",
                    season.poster_file,
                    force,
                    sources,
                    f"season-{season.season_number}",
                )
                session.add(season)
            if has_files:
                for episode in episodes:
                    key = f"s{episode.season_number:02d}e{episode.episode_number:02d}"
                    episode.still_file = await _sync_asset(
                        base,
                        still_size,
                        episode.still_path,
                        item_dir / f"{key}.jpg",
                        episode.still_file,
                        force,
                        sources,
                        key,
                    )
                    session.add(episode)
        if sources != saved_sources:
            await asyncio.to_thread(_save_asset_sources, item_dir, sources)
        await session.commit()


_SOURCES_FILE = "sources.json"


def _load_asset_sources(item_dir: Path) -> dict[str, str]:
    """条目资产目录的溯源记录：资产键 → 下载时的「档位+TMDB 路径」。

    普通刷新据此判断"上游换图了没有"——没有溯源就没法感知 TMDB 换图，
    资产会永远停在首次下载的那张（docs/design/metadata.md 6.1，2026-08-04
    完整性决策）。放文件而非数据库：与资产同生共死，且免一次迁移。
    """
    try:
        import json

        return json.loads((item_dir / _SOURCES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_asset_sources(item_dir: Path, sources: dict[str, str]) -> None:
    try:
        import json

        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / _SOURCES_FILE).write_text(
            json.dumps(sources, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("资产溯源记录写盘失败（不阻断）：%s（%s）", item_dir, exc)


async def _sync_asset(
    base: str,
    size: str,
    tmdb_path: str | None,
    dest: Path,
    current: str | None,
    force: bool,
    sources: dict[str, str],
    key: str,
    *,
    locked: bool = False,
) -> str | None:
    """下载单张图到资产目录，返回落库的相对路径（失败保留现值/None）。

    跳过条件（普通刷新）：文件在**且溯源与当前来源一致**——TMDB 换图
    （poster_path 变更）或档位配置调整都会触发重下，资产随刷新保持最新。
    ``locked``（手动选定）：只要文件还在就不动，上游换图与它无关。
    """
    if not tmdb_path:
        return current
    rel = str(dest.relative_to(assets_root()))
    want = f"{size}{tmdb_path}"
    if (
        not force
        and current == rel
        and (locked or sources.get(key) == want)
        and await asyncio.to_thread(dest.is_file)
    ):
        return current
    from movieclaw_api.services.image_proxy import get_image_proxy

    try:
        data, _content_type = await get_image_proxy().fetch(f"{base}/{size}{tmdb_path}")
    except Exception as exc:  # noqa: BLE001 -- 单张失败不阻断
        logger.warning("图片资产下载失败（保持缺失，下次刷新自愈）：%s（%s）", tmdb_path, exc)
        return current
    try:
        await asyncio.to_thread(_atomic_write, dest, data)
    except OSError as exc:
        logger.warning("图片资产写盘失败：%s（%s）", dest, exc)
        return current
    sources[key] = want
    return rel


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)


# ---------------------------------------------------------------------------
# 手动选图（docs/design/metadata.md 6.3）：自动策略挑不中口味时的人工通道
# ---------------------------------------------------------------------------


async def list_artwork_candidates(
    media_item_id: int,
) -> tuple[list[dict], list[dict], str | None, str | None]:
    """条目的候选图 (海报, 背景, 当前海报路径, 当前背景路径)。

    排序按自动选图的同一套规则；**"当前"由实际在用的路径判定**而非"列表
    第一张"——策略上线前刮的条目、手动锁定的条目、TMDB 新增了更高票的图，
    三种情况下"第一张"都不等于在用的那张（实测：星际穿越的背景对不上）。
    """
    from movieclaw_api.services.media_discover import get_tmdb_client
    from movieclaw_media.library import (
        image_language_param,
        list_image_candidates,
        resolve_image_languages,
    )

    db = get_database()
    async with db.session() as session:
        repo = MediaItemRepository(session)
        item = await session.get(MediaItem, media_item_id)
        if item is None:
            return [], [], None, None
        kind = MediaKind(item.kind)
        current_poster = item.poster_path
        current_backdrop = item.backdrop_path
        meta = await repo.get_metadata(media_item_id)
        original_language = meta.original_language if meta else None
        # 候选图的排序规则必须与自动选图完全同源，所以同样按归属库解析
        scrape_setting = await scrape_setting_for_item(session, item)
    settings = get_settings()
    prefs = effective_image_prefs(scrape_setting)
    language = effective_language(scrape_setting)
    # 候选语言集由选图偏好推导；「原始语言」此处已知（档案落库过），直接并入
    include_param = image_language_param(prefs, language)
    if original_language and original_language not in include_param.split(","):
        tokens = (*prefs.poster_langs, *prefs.backdrop_langs)
        if "orig" in tokens:
            include_param = f"{include_param},{original_language}"
    data = await get_tmdb_client().get(
        f"{kind.value}/{item.tmdb_id}/images",
        {"include_image_language": include_param},
    )
    poster_langs = resolve_image_languages(
        prefs.poster_langs, primary_language=language, original_language=original_language
    )
    backdrop_langs = resolve_image_languages(
        prefs.backdrop_langs, primary_language=language, original_language=original_language
    )
    posters, backdrops = list_image_candidates(
        {"images": data},
        poster_langs,
        backdrop_langs,
        poster_min_width=prefs.poster_min_width,
        backdrop_min_width=prefs.backdrop_min_width,
    )
    base = settings.tmdb_image_base_url.rstrip("/")

    def _view(image: dict, preview_size: str) -> dict:
        return {
            "file_path": image.get("file_path"),
            # 预览走小图档位：一屏几十张候选，用原图会把弹层拖垮
            "preview_url": f"{base}/{preview_size}{image.get('file_path')}",
            "width": image.get("width"),
            "height": image.get("height"),
            "language": image.get("iso_639_1"),
            "vote_average": image.get("vote_average"),
            "vote_count": image.get("vote_count"),
        }

    def _with_current(images: list[dict], current: str | None, size: str) -> list[dict]:
        """在用的那张若不在候选里（旧策略选的 / TMDB 下架了 / 语言被过滤掉），
        补进列表首位——否则弹层里一张都标不出"当前"，用户照样对不上。"""
        views = [_view(i, size) for i in images if i.get("file_path")]
        if current and all(v["file_path"] != current for v in views):
            views.insert(0, _view({"file_path": current}, size))
        return views

    return (
        _with_current(posters, current_poster, "w185"),
        _with_current(backdrops, current_backdrop, "w300"),
        current_poster,
        current_backdrop,
    )


async def select_artwork(media_item_id: int, *, kind: str, file_path: str | None) -> bool:
    """把用户选中的图设为该条目的海报/背景，并**加锁**（刷新不再覆盖）。

    ``file_path=None`` 表示"恢复自动"：解锁并让下次刷新按策略重选。
    选定后立即下载到资产目录并**覆盖镜像**到媒体目录——用户刚点的图要
    当场生效，包括 Emby 那侧。返回是否成功。
    """
    is_poster = kind == "poster"
    db = get_database()
    async with db.session() as session:
        repo = MediaItemRepository(session)
        item = await session.get(MediaItem, media_item_id)
        if item is None:
            return False
        meta = await repo.get_metadata(media_item_id)
        if meta is None:
            meta = MediaMetadata(media_item_id=media_item_id)
        if is_poster:
            meta.poster_locked = file_path is not None
            if file_path is not None:
                item.poster_path = file_path
        else:
            meta.backdrop_locked = file_path is not None
            if file_path is not None:
                item.backdrop_path = file_path
        meta.updated_at = utcnow()
        session.add(meta)
        session.add(item)
        await session.commit()

    if file_path is None:
        # 恢复自动：解锁即可，图片留到下次刷新按新策略重选（不立刻打 TMDB）
        return True
    # 选定的图当场落盘并覆盖镜像（force：这正是"替换现有图片"的语义；
    # ignore_locks：锁是这次选图刚加的，不能反过来挡住它自己落盘）
    await download_item_assets(media_item_id, force=True, ignore_locks=True)
    await mirror_media_dir_assets(media_item_id, force=True)
    return True


# ---------------------------------------------------------------------------
# 媒体目录镜像（Kodi/Emby 规范，只增不覆盖不删除，docs/design/metadata.md 6.2）
# ---------------------------------------------------------------------------


async def mirror_media_dir_assets(media_item_id: int, *, force: bool = False) -> None:
    """把刮削成果镜像写入媒体目录：条目图片 + 完整 NFO + 分集 thumb/NFO。

    铁律（2026-08-04 完整性决策改版，docs/design/metadata.md 6.2）：镜像随
    库内档案**保持更新**——图片"资产比镜像新才覆盖"（_copy_asset），NFO 每次
    刷新按 media_metadata 重写（内容比对，无变化不落盘）；**绝不删除**；
    写失败只告警。``force``（单条目手动刷新）：图片无条件覆盖写。

    NFO 只在身份**高置信**时写（人工认领 / 目录 tmdbid 标记 / 既有 NFO /
    入库管线锚定）：扫描的名称收敛（RESOLVED）是机器结论，写成 NFO 会被
    下次识别当权威读回——错挂经 NFO 自我固化，重识别翻案通道就失效了。
    图片不受此限（识别链不读图片）。

    **库类型与条目类型不一致的目录整个不镜像**（图片也不写）：那说明身份
    判错了——库类型选错时按错误类型拉档能拿到一部毫不相干的作品且全程
    零报错，这种身份的任何产物都不该落到用户的媒体目录里。
    """
    from movieclaw_api.services.library.layout import entry_dir_of
    from movieclaw_api.services.library.nfo import write_episode_nfo, write_full_nfo
    from movieclaw_db.models.library_file import FileSource, IdentitySource

    _TRUSTED_SOURCES = {
        IdentitySource.MANUAL.value,
        IdentitySource.PATH_TAG.value,
        IdentitySource.NFO.value,
    }

    db = get_database()
    async with db.session() as session:
        repo = MediaItemRepository(session)
        item = await session.get(MediaItem, media_item_id)
        if item is None:
            return
        meta = await repo.get_metadata(media_item_id)
        seasons = await repo.list_seasons(media_item_id)
        episodes = {
            (e.season_number, e.episode_number): e for e in await repo.list_episodes(media_item_id)
        }
        rows = list(
            (
                await session.execute(
                    select(LibraryFile, Library)
                    .join(Library, Library.id == LibraryFile.library_id)  # type: ignore[arg-type]
                    .where(
                        LibraryFile.media_item_id == media_item_id,
                        LibraryFile.in_place(),
                    )
                )
            ).all()
        )
    rows = [(f, lib) for f, lib in rows if lib.write_media_assets and lib.kind == item.kind]
    if not rows:
        return

    def _file_trusted(file: LibraryFile) -> bool:
        return (
            file.source == FileSource.IMPORTED.value
            or (file.identity_source or "") in _TRUSTED_SOURCES
        )

    item_dir = assets_root() / str(media_item_id)
    # 条目目录 → 它所属的库：镜像三件事（图片/NFO/分集剧照）各有按库开关，
    # 必须知道每个目录归谁管。库根不允许重叠（LibraryConfigService 保存时
    # 已校验），所以一个条目目录只会归属一个库
    entry_dirs: dict[Path, Library] = {}
    trusted_entries: set[Path] = set()
    for file, library in rows:
        entry = entry_dir_of([Path(p) for p in library.root_paths], Path(file.file_path))
        if entry is None:
            continue
        entry_dirs.setdefault(entry, library)
        if _file_trusted(file):
            trusted_entries.add(entry)

    def _mirror() -> None:
        for entry, library in entry_dirs.items():
            if not entry.is_dir():
                continue
            write_images, write_nfo, _ = effective_mirror_flags(library)
            if write_images:
                _copy_asset(item_dir / "poster.jpg", entry / "poster.jpg", force)
                _copy_asset(item_dir / "backdrop.jpg", entry / "fanart.jpg", force)
                if item.kind == MediaKind.TV.value:
                    for season in seasons:
                        name = (
                            "season-specials-poster.jpg"
                            if season.season_number == 0
                            else f"season{season.season_number:02d}-poster.jpg"
                        )
                        _copy_asset(
                            item_dir / f"season-{season.season_number}.jpg", entry / name, force
                        )
            if write_nfo and entry in trusted_entries:
                write_full_nfo(entry, item, meta)
        for file, library in rows:
            episode = episodes.get((file.season_number, file.episode_number))
            if episode is None or file.container in ("bluray", "dvd"):
                continue
            write_images, write_nfo, write_thumbs = effective_mirror_flags(library)
            video = Path(file.file_path)
            if write_thumbs and write_images:
                _copy_asset(
                    item_dir / f"s{episode.season_number:02d}e{episode.episode_number:02d}.jpg",
                    video.with_name(f"{video.stem}-thumb.jpg"),
                    force,
                )
            if write_nfo and _file_trusted(file):
                write_episode_nfo(video, episode)

    await asyncio.to_thread(_mirror)


def _copy_asset(src: Path, dest: Path, force: bool) -> None:
    """资产 → 媒体目录的单张镜像：**资产比镜像新（或 force）才覆盖**，绝不删除。

    2026-08-04 完整性决策：镜像随资产追新——资产因上游换图/档位调整重下后
    （mtime 变新），下次镜像即覆盖媒体目录里的旧图；用户手工放进目录的图
    （mtime 比资产新）不会被冲掉——想让手选图长期生效请走详情页选图锁
    （锁会覆盖镜像并冻结资产，见 docs/design/metadata.md 6.3）。
    """
    if not src.is_file():
        return
    if dest.exists() and not force:
        try:
            if dest.stat().st_mtime >= src.stat().st_mtime:
                return
        except OSError:
            return
    try:
        shutil.copyfile(src, dest)
    except OSError as exc:
        logger.warning("镜像图片写入媒体目录失败（不阻断）：%s（%s）", dest, exc)
