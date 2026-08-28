"""下载监听导入：监听目录里下载完成的内容 → 识别 → 硬链/复制到目标。

目标三态（docs/design/strm-workflow.md）：指定库/自动路由进库主根；
**自定义目录**规则落规则声明的目录——识别改名后即完成，不进任何库
（文件是"过客"：外部流转——上传网盘、转存、人工确认——后出现在某个
库根时由扫描收尾入账）。

与既有两条入库路径的关系（docs/design/library.md 语境）：
- 订阅管线靠**下载器 API 轮询**确认完成后硬链入库（download_progress）；
- 手动推送直接把 save_path 指到库内条目目录，靠库根监听发现；
- 本模块补第三条：**任何来源**落进监听导入源目录的文件（用户直接在
  qB 里加的种、网盘/浏览器下载等），完成后自动按规范命名搬进库。
  配置是独立的「监听导入规则」（import_watch 表：源目录 → 目标库 +
  策略），媒体库本身只有一套目录体系、不承载下载语义。

完成检测（本功能的核心难点——下载开始时目录结构就已建立）：
0. **下载器权威信号优先**：条目名能匹配到已配置下载器中的种子
   （比对种子名与落盘根名——**按名称匹配免疫容器路径映射**，save_path
   两侧视角不同不可靠）时，以下载器报告的完成状态为准：整种完成即处理；
   整种未完成时按单文件完成字节数构造安全白名单，已完成且没有其他种子仍在
   写同一路径的文件先入库，其余继续等待——这是暂停种子误判与同目录长尾
   阻塞的共同解法；
   匹配不到且没有持久化投递锚（网盘/浏览器等非种子来源）时才退回启发式；
   启用中的下载器不可达/未验证完成时保持等待，绝不猜测完成：
1. **进行中标记排除**：条目树内存在下载器的未完成标记文件
   （qBittorrent ``.!qB`` / aria2 ``.aria2`` / 浏览器 ``.crdownload`` 等）
   即视为下载中，并重置静默计时（标记与权威信号矛盾时从严，按下载中处理）；
2. **静默窗口**：条目全树指纹（总大小:文件数:最大 mtime）连续稳定
   ``QUIET_SECONDS`` 才算落定。**不能只看大小**——BT 客户端普遍预分配
   全尺寸文件，大小从一开始就不变，mtime 才是写入活动的真实信号；
3. **ffprobe 终检**：入库前视频文件逐个探测，失败的不入库，挡住"暂停的
   种子恰好静默够久"的残缺文件（moov 在尾部的 mp4 未下完必然探测失败）；
   ffprobe 未安装时此门禁自动放行（与扫描器的降级行为一致）；
4. **指纹变化自动重试**：结论落 ``ingest_entry`` 台账，指纹变化（下载
   还在继续/季包补了新集/用户改名）自动重新处理——这同时给了"边下边
   补集"的增量导入能力。

结论分档（与库扫描"宁可待确认，不静默错挂"同一哲学，见 IngestStatus）：
- **识别不出 = 待处理（pending）**：不告警、不定时重试——重试改变不了
  信息不足，等的是人（清单里认领/忽略）或输入变化（改名 → 指纹变化）；
- **环境故障 = 失败（failed）**：探测失败/搬运出错/TMDB 不可达，时间
  驱动的重试有意义，小时级退避 + 告警；告警按**源目录聚合**成一条
  （"某目录 N 个条目失败"），逐条目刷红灯在存量场景是灾难；
- **部分入库不冒充完成**：季包只要还有文件因季集解析失败而跳过，就保留
  pending；只要还有探测/搬运故障，就保留 failed。升级（代码或 NER 模型
  任一半，见 _ingest_handler_revision）后的首轮补扫会自动重试旧版误标成
  imported 的同类记录，正常完成记录仍按指纹短路；有过成功搬运的自定义
  目录记录不自动补偿（重复上传风险，见 _parser_gap_auto_retry）；
- 识别顺序：订阅工单认领（info_hash）→ NFO 身份声明（第三方整理过的
  内容常自带 tmdbid，与扫描器同一解析器）→ 名称解析 + TMDB 收敛。

存量基线（规则的「跳过存量」选项）：首轮见到源目录时把当时已完成的
条目批量标记为已忽略（ignored 台账行，清单中可恢复），之后只处理新增；
正在下载中的条目不进基线。基线打在首轮巡检而非保存规则时——保存时
目录可能尚未挂载，空快照会让挂载后出现的存量全被当成新增。

触发机制（事件驱动，尽量不主动扫——监听目录里的保种源会永远留着，
主动全量 stat 会让 NAS 磁盘永远无法休眠）：
- 正常路径：``IngestWatcher`` 对监听目录挂 watchdog，有事件才去抖巡检
  对应目录（只读事件不算：做种上传/ffprobe 读文件不改变结论，见
  ``_is_ingest_relevant``）；下载写入持续产生事件，事件停止本身就是
  静默的开端；
- 静默到点自检：事件停了之后没人会再叫醒我们，每轮巡检后若仍有条目在
  等静默窗口或等失败退避（失败结论落账后不会再有事件，退避重试全靠
  自检/兜底回来看），按最近到期时间挂一次性自检，全部落定即归零；
- 下载器状态轮询：被权威信号判为「未完成」的条目记入挂起表——完成的
  一瞬 fs 事件已停、下载器 API 又慢半拍（最后分片校验完 progress 才翻 1，
  概览另有短缓存），事件路径必然错过这次翻转。自检先用 API 核对；只有
  下载器给出新的单文件完成证据时才 stat 这些候选文件并触发分批入库，暂停
  的种子仍可无限期挂着而不反复扫描目录；
- 唯一的主动扫：目录**初次纳入监听**时补扫该目录一次——监听建立之前
  完成的下载（停机期间/目录刚就绪/刚加进配置）不会再产生事件，只有
  这一次能接住；之后该目录全靠事件驱动；
- 兜底：每小时重建一次失效监听，并巡检**监听覆盖不到**的目录
  （目录不存在/watchdog 缺失/挂载不产生 fs 事件）；正被实时监听的目录
  只在仍有挂起/待静默/待失败重试条目时纳入巡检（自检链异常死亡的最后保险，
  否则条目会无限期静默——线上实证过入库延迟 1 小时+且无告警），
  其余情况绝不重复主动扫。

幂等与安全：
- 源文件**永不改动**：硬链保种零占用（需与主根同盘），复制适合跨盘；
- 搬运落位统一「备好内容 → rename 发布」（fsops.rename_no_replace，原子
  防覆盖，同 library_organize；直接 link 到最终名在极空间的 FUSE 文件
  系统上不产生索引事件，极影视会永远看不到该文件，见 fsops 模块头）；
  目标已存在且内容不同时按多版本约定加 " - 版本标签" 后缀，仍冲突则
  跳过不覆盖；
- 台账逐条目收口：源文件留在监听目录，没有台账每轮都会重复处理。

与扫描/整理的并发：巡检只创建持久化 ``library.ingest`` 作业，不在监听协程
里直接搬运。指定库在入队时声明库资源，自动路由在识别出目标后动态补充同一
资源并取得租约锁，因此扫描、整理、迁移与监听入库共享一套库级互斥。复制使用
隐藏续传文件与源版本边车，服务更新后从已确认字节继续，最终仍以原子 rename
发布；硬链接保持一次性原子发布。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from uuid import uuid4

from sqlalchemy import String, cast, or_, update
from sqlmodel import select

from movieclaw_api.services import jobs
from movieclaw_api.services.import_watch_config import rule_target_label
from movieclaw_api.services.library.bluray import (
    enrich_spec_with_clpi,
    read_clpi_languages,
    read_main_playlist,
)
from movieclaw_api.services.library.config import (
    derive_entry_dir,
    derive_save_path,
    sanitize_folder_name,
)
from movieclaw_api.services.library.fsops import rename_no_replace
from movieclaw_api.services.library.layout import (
    IN_PROGRESS_MARKERS,
    VIDEO_EXTS,
    is_disc_dir,
)
from movieclaw_api.services.library.naming import (
    episode_file_name,
    movie_file_name,
    season_dir_name,
)
from movieclaw_api.services.library.resolve import verify_resolve
from movieclaw_api.services.library.scan import disc_main_stream, guess_evidence
from movieclaw_api.services.library.units import FileUnit, resolve_units
from movieclaw_api.services.media_discover import get_tmdb_client
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.media_probe import ffprobe_available, probe_media
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    ACTIVE_JOB_STATUSES,
    DownloaderClient,
    FileSource,
    ImportWatch,
    IngestEntry,
    IngestStatus,
    Job,
    JobResource,
    JobStatus,
    Library,
    LibraryFile,
    ManualDownloadIntent,
    MediaItem,
    MediaSeason,
    NoticeSeverity,
    utcnow,
)
from movieclaw_db.models.manual_download_intent import MANUAL_DOWNLOAD_INTENT_TTL
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_enrich import enrich
from movieclaw_enrich.inference import model_release_tag
from movieclaw_media.models import MediaKind
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.library_ingest")

# 兜底巡检节奏：正常路径是 watchdog 事件驱动，这里的低频全量巡检只兜底
# 网络挂载不产生 fs 事件、进程停机期间错过事件两种场景
FALLBACK_SWEEP_SECONDS = 3600
# 静默窗口：指纹连续稳定 5 分钟才认为下载落定（写入中 mtime 持续变化）
QUIET_SECONDS = 300
# 挂起条目（下载器报未完成）的状态轮询节奏：优先 API 核对，只有出现新的
# 单文件完成候选时才 stat 对应文件，不扫描整棵目录
DEFERRED_POLL_SECONDS = 300
# 失败条目的重试退避：指纹没变化时每小时才重试一次（避免反复打 TMDB）
FAILED_RETRY_SECONDS = 3600
# 下载器种子概览的缓存：一轮事件风暴中的多个目录巡检共享一次 API 调用
_BRIEFS_TTL_SECONDS = 15.0
# Job 复制每个安全停止点最多写 64 MiB。块太小会让 SQLite 进度事件膨胀，
# 太大又会让取消/更新等待过久；NAS 上 64 MiB 通常在数秒内完成。
_INGEST_COPY_CHUNK_BYTES = 64 * 1024 * 1024

# Job 处理逻辑修订号不仅用于审计，也作为“升级后是否值得自动重试”的边界。
# 修改季集解析/部分入库收口逻辑时必须递增；同一修订内的待处理条目不反复重跑。
# v4：显式 SxxEyy 标记确定性解析压过模型（torrent-ner-v2 对单集文件名漏抽
#     集号的线上病例），存量「解析不出集号」的挂起记录升级后自动补偿
# v5：显式 E00（先导/特辑占位）不再误报「解析不出集号」钉住记录，按占位
#     跳过并如实说明——含 E00 的季包不再每次都进待处理
# v6：季集号改为整批解析（library/units.py）——裸 E/EP 标记确定性出集号、
#     季号按批次仲裁、推断季号过 TMDB 台账守卫。存量因 issue #185 挂起的
#     「解析不出季号/集号」记录升级后自动补偿重跑
_INGEST_HANDLER_REVISION = "library.ingest.v6"


def _ingest_handler_revision() -> str:
    """当前入库处理能力的完整修订号：代码修订 + NER 模型版本。

    季集解析能力一半在代码、一半在独立发布的 NER 模型（设置页可单独一键
    更新模型，tag 形如 torrent-ner-vN）——只有两者拼在一起才能回答"这条
    记录值不值得再试一次"。只用代码常量会漏掉纯模型升级：用户先装新镜像
    （补偿机会当场消耗、任务重新 blocked）再更新模型时就永远不会重试了。
    模型更新必须全量重启才生效，读到的 tag 在进程生命周期内不变。
    """
    tag = model_release_tag()
    return f"{_INGEST_HANDLER_REVISION}+{tag}" if tag else _INGEST_HANDLER_REVISION


# v0.10.0 以前，季包只要成功搬运一个文件就会被记成 imported，即使 message
# 已明确写着其余文件因季集号解析失败而未入库。保留稳定中文片段用于识别这类
# 历史脏状态；两种原因都不是作品身份歧义，解析能力升级后应自动补偿。
_PARSER_GAP_MARKERS = ("解析不出集号，未入库", "解析不出季号，未入库")

# 文件名含这些标记的视频不入库（与入库管线同口径）
_IGNORE_MARKERS = ("sample",)

# 进程内的静默观察：条目路径 -> (指纹, 首次见到该指纹的单调时钟)。
# 重启丢失只是重新等一个静默窗口，无需持久化
_stability: dict[str, tuple[str, float]] = {}
# 进程内的挂起表：被下载器权威信号判为「未完成」的条目路径 -> 首次挂起的
# 单调时钟。挂起条目由状态轮询自检 + 每小时兜底巡检共同照看——绝不能
# 「弹出即失忆」：完成瞬间 API 常慢半拍，误判一次就再没有事件叫醒它了。
# 重启丢失无妨：启动时新纳入监听的目录会补扫一次，条目重新入表
_deferred: dict[str, float] = {}
# 进程内的失败重试表：结论落 FAILED 的条目路径 -> 落账时的单调时钟。
# 这是第三个唤醒源：失败结论落账时静默表/挂起表都已弹出该条目，之后
# 不会再有 fs 事件（下载早已结束），若不在此留痕，到点自检与兜底巡检
# 都看不见它，「自动退避重试」的承诺永远无法兑现。重启丢失由
# _process_entry 的退避短路分支补记（见该处注释）
_failed_retry: dict[str, float] = {}
# 巡检串行锁：事件驱动与兜底巡检可能同时到达同一目录，串行化防同条目双处理
_sweep_lock = asyncio.Lock()
# 下载器种子概览缓存：(取样时刻, 概览列表或 None=不可用)
_briefs_cache: tuple[float, list | None] = (float("-inf"), None)


class IngestError(Exception):
    """单个文件搬运失败。message 是完整中文句子，直接进台账 message。"""


class IngestConflictError(IngestError):
    """目标同名文件冲突（内容不同、多版本退让名也被占）。

    与环境故障（网络盘瞬断、复制失败）本质不同：冲突是确定性的，退避重试
    永远得到同一结果，只会在任务中心刷出无限「等待重试」。处理分两级：
    该单元在库中已有同档或更高版本 → 重复内容，跳过即是完成
    （见 _covered_by_library）；判不了档位或来件更优 → 待处理等人。
    """


async def _covered_by_library(
    session,
    media_item_id: int,
    season: int,
    episode: int,
    *,
    resolution: str | None,
    media_source: str | None,
    remux: bool,
) -> bool:
    """同名冲突的去重判据：该单元在位文件里是否已有同档或更高版本。

    典型场景：季包只为补 E14–E15 的缺口投递，附带的 E05 与此前换源包
    收编的版本同档不同尺寸——基础名与版本退让名全被占，但重复内容本就
    不值得入库。档位口径与洗版裁决同源（分辨率位次 > 片源档字典序，见
    upgrade._file_sort_key）；来件分辨率未知（探测不可用）时不判定——
    宁可进待处理，不做猜测性丢弃。真升级（档位更高）落的是不同版本名，
    不会走进同名冲突，仍由洗版验证确认后把旧版送待回收。
    """
    from movieclaw_matcher import RuleSetSpec
    from movieclaw_matcher.decision import resolution_rank, source_tier

    neutral = RuleSetSpec()
    incoming = (
        resolution_rank(resolution, neutral) or 0,
        source_tier(media_source, remux) or 0,
    )
    if incoming[0] <= 0:
        return False
    rows = (
        await session.execute(
            select(LibraryFile).where(
                LibraryFile.media_item_id == media_item_id,
                LibraryFile.season_number == season,
                LibraryFile.episode_number == episode,
                LibraryFile.in_place(),
                # 意外消失的文件不算覆盖：同档重新投递正是找回它的路径
                LibraryFile.missing_since.is_(None),  # type: ignore[union-attr]
            )
        )
    ).scalars()
    keys = [
        (
            resolution_rank(row.resolution, neutral) or 0,
            source_tier(row.media_source, False) or 0,
        )
        for row in rows
    ]
    return bool(keys) and max(keys) >= incoming


class IngestSourceChanged(Exception):
    """复制期间源文件继续变化；保留作业意图，等下载稳定后重新执行。"""


def _has_parser_gap(record: IngestEntry | None) -> bool:
    """台账是否仍有因季集解析失败而未入库的文件。"""
    return bool(record and any(marker in (record.message or "") for marker in _PARSER_GAP_MARKERS))


def _parser_gap_auto_retry(rule: ImportWatch, record: IngestEntry | None) -> bool:
    """升级补偿能否自动重跑该记录（区别于用户主动重试）。

    库目标靠落点文件天然幂等（同内容已在库的搬运会静默短路），可放心自动
    重跑；自定义目录（中转）无法按落点去重——此前搬运过、已被外部工具
    消费（上传后删除）但尚未回流入库的文件会被重新搬进中转、重复上传。
    因此有过成功搬运的中转记录不自动补偿（保持升级前的状态，用户改名/
    恢复时自行触发重跑）；从未搬运过文件的中转记录没有这个风险，照常补偿。
    """
    if not _has_parser_gap(record):
        return False
    assert record is not None
    return rule.target_path is None or not record.imported_count


@dataclass
class _EntrySnapshot:
    """条目的一次快照：指纹 + 完成检测所需的观察结果。"""

    fingerprint: str  # 整树为大小/数量/mtime；文件批次为相对路径版本哈希
    has_marker: bool  # 树内存在下载中标记文件
    has_disc: bool  # 树内存在原盘结构（BDMV/VIDEO_TS）
    videos: list[Path] = field(default_factory=list)  # 可入库的视频文件
    disc_roots: list[Path] = field(default_factory=list)  # 完整原盘根（其下含标志目录）


@dataclass(frozen=True)
class _ReadyDownloadFile:
    """下载器确认可安全入库的单个视频及其来源证据。"""

    relative_path: str
    size_bytes: int
    info_hashes: tuple[str, ...]


@dataclass(frozen=True)
class _DownloadFileBatch:
    """同目录仍在下载时可先行处理的一批文件。"""

    files: tuple[_ReadyDownloadFile, ...]
    consumable_hashes: tuple[str, ...]


def _snapshot(entry: Path) -> _EntrySnapshot:
    """遍历条目（文件或目录）产出快照。纯 stat，线程池内运行。"""
    total, count, max_mtime = 0, 0, 0.0
    has_marker = False
    has_disc = False
    videos: list[Path] = []
    disc_roots: set[Path] = set()

    def visit(file: Path, *, inside_disc: bool = False) -> None:
        nonlocal total, count, max_mtime, has_marker
        try:
            stat = file.stat()
        except OSError:
            return
        total += stat.st_size
        count += 1
        max_mtime = max(max_mtime, stat.st_mtime)
        lower = file.name.lower()
        if any(lower.endswith(marker) for marker in IN_PROGRESS_MARKERS):
            has_marker = True
            return
        if (
            not inside_disc
            and Path(lower).suffix in VIDEO_EXTS
            and not any(m in lower for m in _IGNORE_MARKERS)
        ):
            videos.append(file)

    if entry.is_file():
        visit(entry)
    else:
        stack = [(entry, False)]
        while stack:
            current, inside_disc = stack.pop()
            if current.name.upper() in ("BDMV", "VIDEO_TS"):
                has_disc = True
                inside_disc = True
                disc_roots.add(current.parent)
            try:
                children = sorted(current.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if not child.name.startswith("."):
                        stack.append((child, inside_disc))
                elif child.is_file():
                    visit(child, inside_disc=inside_disc)
    return _EntrySnapshot(
        fingerprint=f"{total}:{count}:{int(max_mtime)}",
        has_marker=has_marker,
        has_disc=has_disc,
        videos=sorted(videos),
        disc_roots=sorted(disc_roots),
    )


def _entry_file(entry: Path, relative_path: str) -> Path | None:
    """把持久化相对路径安全还原到监听条目内，拒绝绝对路径与 ``..``。"""
    if relative_path == ".":
        return entry if entry.is_file() else None
    relative = PurePosixPath(relative_path.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None
    return entry.joinpath(*relative.parts)


def _relative_entry_file(entry: Path, file: Path) -> str | None:
    """把下载器映射后的文件路径收敛成条目内相对路径。"""
    entry_path = Path(os.path.normpath(str(entry)))
    file_path = Path(os.path.normpath(str(file)))
    if entry.is_file():
        return "." if file_path == entry_path else None
    try:
        relative = file_path.relative_to(entry_path)
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else None


def _disc_relative_path(relative_path: str) -> bool:
    """下载器文件清单里的路径是否属于蓝光/DVD 原盘结构。

    整树快照会在下钻时识别 ``BDMV`` / ``VIDEO_TS``，但边下边入库只拿到
    下载器放行的文件白名单，不能靠目录遍历补判。这里直接检查相对路径的
    目录段，既覆盖单片原盘 ``BDMV/STREAM/...``，也覆盖合集里嵌套的
    ``影片名/BDMV/STREAM/...``。
    """
    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    return any(part.upper() in {"BDMV", "VIDEO_TS"} for part in parts)


def _snapshot_ready_files(entry: Path, ready_files: list[_ReadyDownloadFile]) -> _EntrySnapshot:
    """只对下载器放行的文件取快照，目录内其他写入不再阻塞本批作业。"""
    fingerprint_parts: list[str] = []
    videos: list[Path] = []
    for ready in ready_files:
        file = _entry_file(entry, ready.relative_path)
        if file is None or not file.is_file():
            raise IngestSourceChanged(f"已完成文件已不存在：{ready.relative_path}")
        try:
            stat = file.stat()
        except OSError as exc:
            raise IngestSourceChanged(f"无法读取已完成文件：{file.name}") from exc
        if stat.st_size != ready.size_bytes:
            raise IngestSourceChanged(f"已完成文件大小发生变化：{file.name}")
        lower = file.name.lower()
        if any(lower.endswith(marker) for marker in IN_PROGRESS_MARKERS):
            raise IngestSourceChanged(f"文件仍带下载中标记：{file.name}")
        if file.suffix.lower() not in VIDEO_EXTS or any(
            marker in lower for marker in _IGNORE_MARKERS
        ):
            continue
        fingerprint_parts.append(f"{ready.relative_path}\0{stat.st_size}\0{stat.st_mtime_ns}")
        videos.append(file)
    fingerprint = hashlib.sha256("\n".join(fingerprint_parts).encode()).hexdigest()
    return _EntrySnapshot(
        fingerprint=f"ready:{fingerprint}",
        has_marker=False,
        has_disc=False,
        videos=sorted(videos),
    )


def _ingest_path_id(entry_path: str) -> str:
    """把绝对路径收敛成稳定、无敏感目录信息的资源 id。"""
    return hashlib.sha256(entry_path.encode("utf-8")).hexdigest()


def _ingest_dedupe_key(entry_path: str, fingerprint: str | None = None) -> str:
    """条目的入库去重键；边下边入库的每一批 ready 文件再按指纹分桶。

    整树入库沿用纯路径键（一个条目同时只该有一个作业）。分批入库必须分桶：
    blocked 属于活跃状态，若各批共用一个键，某一批因识别失败挂起后，
    ``return_existing`` 会把后续每一批都并回那个挂起作业，同条目剩余各集就
    永久堵死——而任务中心只显示那一个受阻任务，用户根本看不出还有几集被
    连坐。分桶后挂起的老批次留在原地等人工，新批次自己往前走。
    """
    key = f"library.ingest:{_ingest_path_id(entry_path)}"
    return f"{key}:{fingerprint}" if fingerprint else key


async def _latest_ingest_job(session, entry_path: str):
    """返回同一监听条目的最近 Job（含各分批入库批次），供巡检去重与终态意图。"""
    from movieclaw_db.models import Job

    prefix = _ingest_dedupe_key(entry_path)
    return (
        (
            await session.execute(
                select(Job)
                .where(
                    or_(
                        Job.dedupe_key == prefix,
                        Job.dedupe_key.startswith(f"{prefix}:"),  # type: ignore[union-attr]
                    )
                )
                .order_by(Job.created_at.desc())
            )
        )
        .scalars()
        .first()
    )


async def enqueue_ingest_job(
    session,
    rule: ImportWatch,
    entry: Path,
    snap: _EntrySnapshot,
    *,
    matched_hashes: list[str] | None,
    ready_files: tuple[_ReadyDownloadFile, ...] | None = None,
    consumable_hashes: tuple[str, ...] | None = None,
    keep_deferred: bool = False,
    record: IngestEntry | None = None,
    origin: str = "system",
) -> jobs.CreateJobResult:
    """把一个已通过完成检测的监听条目固化为可恢复 Job。

    路径只作为处理器输入，任务中心用条目名展示；资源索引保存路径哈希，
    避免把 NAS 挂载结构暴露给无关视图。下载 infohash 作为来源资源关联，
    让下载卡片能投影后续入库阶段，但不参与执行锁。
    """
    if rule.id is None:
        raise RuntimeError("监听导入规则尚未落库，不能创建后台作业")
    hashes = sorted({value.lower() for value in matched_hashes or [] if value})
    resources = [
        jobs.ResourceRef("import_watch", rule.id),
        jobs.ResourceRef("ingest_path", _ingest_path_id(str(entry))),
    ]
    if rule.library_id is not None:
        resources.append(jobs.ResourceRef("library", rule.library_id))
    if record is not None and record.id is not None:
        resources.append(jobs.ResourceRef("ingest_entry", record.id, relation="context"))
    resources.extend(jobs.ResourceRef("download", value, relation="source") for value in hashes)
    total_bytes = sum(path.stat().st_size for path in snap.videos if path.exists())
    input_data: dict[str, object] = {
        "rule_id": rule.id,
        "entry_path": str(entry),
        "detected_fingerprint": snap.fingerprint,
        "info_hashes": hashes,
    }
    if ready_files is not None:
        input_data["ready_files"] = [
            {
                "path": ready.relative_path,
                "size_bytes": ready.size_bytes,
                "info_hashes": list(ready.info_hashes),
            }
            for ready in ready_files
        ]
        input_data["consume_info_hashes"] = list(consumable_hashes or ())
        input_data["keep_deferred"] = keep_deferred
    return await jobs.create_job(
        session,
        job_type="library.ingest",
        subject=entry.name,
        input_data=input_data,
        resources=resources,
        # 分批入库按本批 ready 指纹分桶，各批互不牵连（见 _ingest_dedupe_key）
        dedupe_key=_ingest_dedupe_key(
            str(entry), snap.fingerprint if ready_files is not None else None
        ),
        conflict_policy="return_existing",
        handler_revision=_ingest_handler_revision(),
        # 下载落地后的环境故障按小时退避；给足一天自动自愈窗口，超过后
        # 明确失败等待用户处理，不能无限占住活跃任务。
        max_attempts=24,
        origin=origin,
        correlation_id=hashes[0] if hashes else None,
        progress={
            **jobs.default_progress("等待自动整理入库"),
            "total": total_bytes or None,
            "details": {
                "rule_id": rule.id,
                "entry_name": entry.name,
                "info_hashes": hashes,
            },
        },
    )


# ---------------------------------------------------------------------------
# 巡检任务
# ---------------------------------------------------------------------------


async def _load_rules() -> list[tuple[ImportWatch, Library | None]]:
    """全部监听导入规则及其目标库；auto 规则（library_id=NULL）的库为 None
    （识别出作品后按收藏范围路由）。指定库被删除的规则由外键级联清掉。"""
    db = get_database()
    async with db.session() as session:
        result = await session.execute(
            select(ImportWatch, Library)
            .outerjoin(Library, ImportWatch.library_id == Library.id)  # type: ignore[arg-type]
            .order_by(ImportWatch.id)
        )
        return [(rule, library) for rule, library in result.all()]


@register_task(
    "library_ingest",
    title="监听导入（兜底巡检）",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=FALLBACK_SWEEP_SECONDS,
    description=(
        "低频兜底：重建失效的目录监听，并巡检监听覆盖不到的源目录；"
        "被实时监听的目录只在仍有等待中的条目（等静默窗口/等下载器完成）时"
        "纳入巡检，其余由事件驱动、不主动扫。"
    ),
)
async def ingest_tick() -> None:
    # 先重建监听：目录此前未就绪（挂载晚于启动）或监听失效时借此恢复；
    # 新纳入监听的目录由 refresh_watches 触发一次该目录的补扫
    watcher = get_ingest_watcher()
    if watcher is not None:
        await watcher.refresh_watches()
    watched = watcher.watched_keys() if watcher is not None else frozenset()

    for rule, library in await _load_rules():
        if rule.source_path in watched and not _has_pending(rule.source_path):
            continue  # watchdog 在实时盯着且没有等待中的条目：事件路径负责
        try:
            await _sweep_dir(rule, library)
        except Exception:  # noqa: BLE001 -- 单目录失败不拖垮整轮
            logger.exception(
                "监听导入巡检失败（→ %s）：%s",
                rule_target_label(rule, library.name if library else None),
                rule.source_path,
            )


def _has_pending(source_path: str) -> bool:
    """该源目录下是否还有等待中的条目（等静默窗口 / 等下载器状态翻转 /
    等失败退避到点重试）。

    兜底巡检对这类目录不再跳过——这是自检链意外死亡时的最后一道保险：
    目录里有货没人管时兜底必须看得见，最坏一小时后条目仍会被接住。
    """
    prefix = source_path.rstrip("/") + "/"
    return any(
        any(p.startswith(prefix) for p in table) for table in (_stability, _deferred, _failed_retry)
    )


async def _sweep_dir(
    rule: ImportWatch,
    library: Library | None,
    *,
    execute_inline: bool = False,
) -> None:
    """巡检一个监听目录：顶层每个文件/目录是一个条目（一次下载的产物）。

    ``library`` 为 None 即自动路由规则——目标库在识别出作品后按收藏范围
    决定（_ingest_entry 内），kind 先验取自规则本身。生产路径只负责创建
    ``library.ingest`` Job；``execute_inline`` 仅供领域单测与离线维护脚本复用
    同一套处理原语，Web、监听器和调度器都不得开启。
    """
    root = Path(rule.source_path)
    if not root.is_dir():
        return  # 目录未就绪（挂载中/配置超前）：不告警刷屏，下轮再看
    async with _sweep_lock:
        briefs = await _downloader_briefs()
        try:
            entries = sorted(e for e in root.iterdir() if not e.name.startswith("."))
        except OSError as exc:
            logger.warning("读取监听目录失败（%s）：%s", rule.source_path, exc)
            return
        if not rule.process_existing and not rule.baseline_done:
            # 「跳过存量」基线：本轮只盖章不处理，下轮起只有基线外的新条目
            # 会被消费（正在下载中的条目不进基线，完成后按新增处理）
            if briefs is None:
                # 无法确认哪些条目仍在下载时不能盖「存量已完成」基线。
                for entry in entries:
                    _deferred.setdefault(str(entry), time.monotonic())
                return
            await _baseline_existing(rule, library, entries, briefs)
            return
        seen = {str(e) for e in entries}
        for entry in entries:
            try:
                await _process_entry(
                    rule,
                    library,
                    root,
                    entry,
                    briefs,
                    execute_inline=execute_inline,
                )
            except Exception:  # noqa: BLE001 -- 单条目失败不断整轮
                logger.exception("处理监听条目失败：%s", entry)
        # 条目从监听目录消失（用户删源）后清掉它的静默观察与挂起记录，
        # 防字典无界增长；它的失败告警（若有）也一并熄灭——源没了，问题
        # 就不存在了
        prefix = str(root).rstrip("/") + "/"
        for table in (_stability, _deferred, _failed_retry):
            for key in [k for k in table if k.startswith(prefix) and k not in seen]:
                table.pop(key, None)
        from movieclaw_api.services.system_notice import resolve_notices

        db = get_database()
        async with db.session() as session:
            # 旧版逐条目告警（dedupe_key "ingest:{条目路径}"）已被按目录聚合
            # 取代：见到即全部熄灭，不管条目还在不在
            await resolve_notices(session, prefix=f"ingest:{prefix}")
            await refresh_source_notice(session, rule.source_path, present=seen)
    # 巡检收尾必挂自检：仍有条目在等静默窗口/等下载器翻转时，保证有人
    # 回来看——无论这轮巡检由事件、自检还是兜底触发（集中在此处挂，
    # 任何一条触发路径都不会漏）
    watcher = get_ingest_watcher()
    if watcher is not None:
        watcher._arm_recheck(rule.source_path)


async def _baseline_existing(
    rule: ImportWatch, library: Library | None, entries: list[Path], briefs: list | None
) -> None:
    """「跳过存量」基线：把源目录当前**已完成**的条目批量标记为已忽略。

    实现即"预写台账"：与正常处理共用同一张 ``ingest_entry`` 和同一个
    跳过查询，零新机制；忽略行躺在清单里，用户可逐条恢复处理。已有结论
    的条目（此前处理过/手动拍板过）不覆盖。
    """
    db = get_database()
    stamped = 0
    async with db.session() as session:
        # 基线旗标先做**条件置位**（仅当库里仍为 False 时更新）：事件巡检与
        # 兜底巡检各自在锁外加载的 rule 对象可能已过期，若另一路刚打完基线，
        # 这里过期对象的 baseline_done 仍是 False——不加条件会把基线重打一遍，
        # 把两轮之间刚下载完成的新条目错误盖章为已忽略。rowcount 为 0 说明
        # 基线已被别人打过，直接返回（本轮不盖章）
        result = await session.execute(
            update(ImportWatch)
            .where(ImportWatch.id == rule.id)  # type: ignore[arg-type]
            .where(ImportWatch.baseline_done == False)  # type: ignore[arg-type]  # noqa: E712
            .values(baseline_done=True)
        )
        if result.rowcount == 0:
            rule.baseline_done = True
            return
        for entry in entries:
            snap = await asyncio.to_thread(_snapshot, entry)
            verdict = _torrent_verdict(_match_briefs(entry.name, briefs))
            if snap.has_marker or verdict == "downloading":
                continue  # 还在下载：语义上是"新增"，完成后正常处理
            existing = (
                await session.execute(
                    select(IngestEntry).where(IngestEntry.entry_path == str(entry))
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            session.add(
                IngestEntry(
                    library_id=library.id if library is not None else None,
                    entry_path=str(entry),
                    fingerprint=snap.fingerprint,
                    status=IngestStatus.IGNORED,
                    message="规则生效时已存在于源目录，按「跳过存量」设置未处理；可在清单中恢复处理",
                )
            )
            stamped += 1
        await session.commit()
    rule.baseline_done = True
    logger.info(
        "监听导入基线（%s）：按「跳过存量」标记 %d 个存量条目为已忽略，之后只处理新增",
        rule.source_path,
        stamped,
    )


async def refresh_source_notice(session, source_path: str, present: set[str] | None = None) -> None:
    """按源目录聚合失败告警：N 个条目失败 → 一条全局通知，清零即熄灭。

    逐条目告警在存量场景会瞬间刷出上百条红灯，聚合成"这个目录有 N 个
    失败"才是可行动的信号。待处理（pending）不是错误、不进通知——监听
    导入清单上的数字承载它。``present`` 给定时只统计仍在源目录里的条目
    （巡检方已有在场清单）；缺省按磁盘现查（用户拍板等低频路径）。
    """
    from movieclaw_api.services.system_notice import resolve_notices, upsert_notice

    source = source_path.rstrip("/")
    prefix = source + "/"
    rows = (
        (
            await session.execute(
                select(IngestEntry).where(
                    IngestEntry.entry_path.startswith(prefix),  # type: ignore[attr-defined]
                    IngestEntry.status == IngestStatus.FAILED,
                )
            )
        )
        .scalars()
        .all()
    )
    failed = [
        r
        for r in rows
        # 只算本目录的顶层条目（防嵌套源目录串数），且源仍在场（删源即问题消失）
        if str(Path(r.entry_path).parent) == source
        and (r.entry_path in present if present is not None else Path(r.entry_path).exists())
    ]
    key = f"ingest-dir:{source}"
    if not failed:
        await resolve_notices(session, dedupe_key=key)
        return
    names = "、".join(f"「{Path(r.entry_path).name}」" for r in failed[:3])
    extra = f" 等 {len(failed)} 个条目" if len(failed) > 3 else ""
    await upsert_notice(
        session,
        dedupe_key=key,
        severity=NoticeSeverity.ERROR,
        source="ingest",
        title=f"监听目录 {source} 有 {len(failed)} 个条目导入失败",
        message=(f"{names}{extra}处理失败，将自动退避重试；具体原因见「设置 → 监听导入」的清单。"),
        payload={"source_path": source, "failed": len(failed)},
    )


async def _downloader_briefs() -> list | None:
    """全部可用下载器的种子概览（带短缓存）。

    ``[]`` 表示没有配置下载器或可达下载器确实没有任务；``None`` 只表示
    配置过下载器但本轮全部不可达。后者必须 fail closed，不能拿静默窗口
    猜完成——API 短暂失联正是未完成文件被提前入库的根因。
    """
    global _briefs_cache
    now = time.monotonic()
    cached_at, cached = _briefs_cache
    if now - cached_at < _BRIEFS_TTL_SECONDS:
        return cached
    # 局部导入：复用订阅管线的"可用下载器"口径，避免模块加载期的重依赖
    from movieclaw_api.services.download_progress import _usable_downloaders
    from movieclaw_downloader import create_downloader

    db = get_database()
    async with db.session() as session:
        enabled_exists = (
            await session.execute(
                select(DownloaderClient.id).where(DownloaderClient.enabled.is_(True))  # type: ignore[union-attr]
            )
        ).first() is not None
        downloaders = await _usable_downloaders(session)
    if not downloaders:
        # 没有启用任何下载器才表示“确实没有权威状态源”。启用中的配置若仍在
        # pending/verifying/failed，则只是当前不可用，必须和连接异常一样
        # fail closed；否则服务启动的验证窗口会把预分配文件误当成已完成。
        result = None if enabled_exists else []
        _briefs_cache = (now, result)
        return result
    briefs: list = []
    all_ok = True
    for row, config in downloaders:
        adapter = create_downloader(config)
        try:
            briefs.extend(await adapter.list_torrents())
        except Exception as exc:  # noqa: BLE001 -- 继续关闭/检查其余连接后整体降级
            all_ok = False
            logger.warning("列出下载器「%s」的种子失败：%s", row.name, exc)
        finally:
            await adapter.close()
    # 任一台缺席时，这份列表都不能证明“某目录不属于下载任务”。宁可让所有
    # 未决条目暂等，也不能用另一台的成功响应替失联下载器作否定判断。
    result = briefs if all_ok else None
    _briefs_cache = (now, result)
    return result


def _claim_path_matches(entry: Path, save_path: str | None) -> bool:
    """持久化投递路径是否覆盖当前监听条目；旧行无路径时按真实名称认领。"""
    if not save_path:
        return True
    expected = Path(os.path.normpath(save_path))
    actual = Path(os.path.normpath(str(entry)))
    return expected in {actual, actual.parent}


def _download_name_matches(entry_name: str, recorded_name: str | None) -> bool:
    """兼容站点标题与下载器内容名的轻微差异，不做宽松片名猜测。"""
    if not recorded_name:
        return False
    if entry_name == recorded_name:
        return True
    compact_entry = re.sub(r"[^a-z0-9]+", "", entry_name.lower())
    compact_recorded = re.sub(r"[^a-z0-9]+", "", recorded_name.lower())
    if compact_entry == compact_recorded:
        return True
    if min(len(compact_entry), len(compact_recorded)) < 20:
        return False
    years_entry = set(re.findall(r"(?:19|20)\d{2}", compact_entry))
    years_recorded = set(re.findall(r"(?:19|20)\d{2}", compact_recorded))
    if years_entry and years_recorded and not years_entry.intersection(years_recorded):
        return False
    return SequenceMatcher(None, compact_entry, compact_recorded).ratio() >= 0.90


async def _has_managed_download_claim(session, entry: Path) -> bool:
    """条目是否仍由 MovieClaw 投递台账管理，即使下载器概览暂时漏掉它。"""
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        SiteTorrent,
        SubscriptionDownloadAttempt,
    )

    manual = list(
        (
            await session.execute(
                select(ManualDownloadIntent, SiteTorrent.title)
                .outerjoin(
                    SiteTorrent,
                    (SiteTorrent.site_id == ManualDownloadIntent.site_id)
                    & (SiteTorrent.torrent_id == ManualDownloadIntent.torrent_id),
                )
                .where(
                    ManualDownloadIntent.created_at
                    >= utcnow() - MANUAL_DOWNLOAD_INTENT_TTL,  # type: ignore[arg-type]
                    or_(
                        ManualDownloadIntent.download_name == entry.name,
                        ManualDownloadIntent.download_name.is_(None),  # type: ignore[union-attr]
                        ManualDownloadIntent.download_name == "",
                    )
                )
            )
        ).all()
    )
    if any(
        _claim_path_matches(entry, intent.save_path)
        and (
            _download_name_matches(entry.name, intent.download_name)
            or _download_name_matches(entry.name, site_title)
        )
        for intent, site_title in manual
    ):
        return True

    active = (
        DownloadAttemptStatus.ACTIVE,
        DownloadAttemptStatus.REPLACEMENT_PENDING,
        DownloadAttemptStatus.TRIAL,
        DownloadAttemptStatus.CLEANUP_PENDING,
        DownloadAttemptStatus.COMPLETED,
    )
    attempts = list(
        (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.status.in_(active),  # type: ignore[union-attr]
                    or_(
                        SubscriptionDownloadAttempt.download_name == entry.name,
                        SubscriptionDownloadAttempt.download_name.is_(None),  # type: ignore[union-attr]
                        SubscriptionDownloadAttempt.download_name == "",
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return any(
        _claim_path_matches(entry, row.save_path)
        and (
            _download_name_matches(entry.name, row.download_name)
            or _download_name_matches(entry.name, row.torrent_title)
        )
        for row in attempts
    )


def _match_briefs(entry_name: str, briefs: list | None) -> list:
    """按名称把条目匹配到下载器种子。

    条目名就是种子的落盘根目录/文件名，比对种子名与 content_name 两个口径
    ——名称匹配免疫容器路径映射。
    """
    return [b for b in briefs or [] if entry_name in (b.name, b.content_name)]


def _torrent_verdict(matches: list) -> str | None:
    """匹配结果 → "complete" / "downloading" / None（未匹配）。

    同名多个种子从严：任一未完成即视为下载中。
    """
    if not matches:
        return None
    return "complete" if all(b.completed for b in matches) else "downloading"


async def _matched_torrent_statuses(matches: list) -> list[tuple[object, object]] | None:
    """补查同名种子的文件状态；任一详情缺失时返回 None，调用方保守等待。"""
    from movieclaw_api.services.download_progress import _query_torrent, _usable_downloaders

    if any(not brief.info_hash for brief in matches):
        return None  # 没有 hash 的轻量记录无法形成可复核的文件证据
    hashes = sorted({str(brief.info_hash).lower() for brief in matches if brief.info_hash})
    if len(hashes) != len(matches):
        return None  # 重复 hash 的概览不应产生互相矛盾的文件写入证据
    db = get_database()
    async with db.session() as session:
        downloaders = await _usable_downloaders(session)
    if not downloaders:
        return None
    details: list[tuple[object, object]] = []
    for info_hash in hashes:
        found = await _query_torrent(info_hash, downloaders)
        if found is None:
            return None
        details.append(found)
    return details


def _torrent_file_source(downloader, status, torrent_file) -> Path | None:  # noqa: ANN001
    """把下载器文件路径翻译到 MovieClaw 视角；异常相对路径直接拒绝。"""
    from movieclaw_api.services.torrent_submit import translate_to_local

    local_dir = translate_to_local(status.save_path, downloader.path_mappings)
    if not local_dir:
        return None
    relative = PurePosixPath(str(torrent_file.path).replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None
    return Path(local_dir).joinpath(*relative.parts)


def _torrent_file_completed(status, torrent_file) -> bool:  # noqa: ANN001
    """只认下载器完成字节；旧适配器缺字段时仅整种完成可兜底。"""
    if status.completed:
        return True
    completed_bytes = torrent_file.completed_bytes
    return completed_bytes is not None and completed_bytes >= torrent_file.size_bytes


async def _completed_file_batch(entry: Path, matches: list) -> _DownloadFileBatch:
    """计算同目录下载中的安全文件交集。

    一个路径可能被多个同名种子引用；只有所有仍会写它的任务都确认该文件
    完成，并且磁盘大小与每份下载器证据一致，才进入本批。详情读取失败时
    返回空批，继续沿用原来的整目录等待语义。
    """
    details = await _matched_torrent_statuses(matches)
    if not details:
        return _DownloadFileBatch(files=(), consumable_hashes=())

    writers: dict[str, list[tuple[object, object]]] = {}
    video_paths_by_hash: dict[str, set[str]] = {}
    status_by_hash: dict[str, object] = {}
    for downloader, status in details:
        info_hash = str(status.info_hash).lower()
        status_by_hash[info_hash] = status
        mapped_selected = 0
        for torrent_file in status.files:
            if not torrent_file.selected:
                continue
            source = _torrent_file_source(downloader, status, torrent_file)
            if source is None:
                continue
            relative = _relative_entry_file(entry, source)
            if relative is None:
                continue
            mapped_selected += 1
            # 原盘的 m2ts/vob 是播放列表引用的传输流分段，不是可独立入库的
            # 正片。下载中若按“已完成文件”放行，会把几秒钟的小分段误当电影，
            # 还会随每批分段完成不断制造新作业。原盘沿用既有整树语义：等整种
            # 完成后由 _snapshot 标记 has_disc，并只生成一条“暂不支持”结论。
            if _disc_relative_path(relative):
                return _DownloadFileBatch(files=(), consumable_hashes=())
            lower = source.name.lower()
            if source.suffix.lower() not in VIDEO_EXTS or any(
                marker in lower for marker in _IGNORE_MARKERS
            ):
                continue
            writers.setdefault(relative, []).append((status, torrent_file))
            video_paths_by_hash.setdefault(info_hash, set()).add(relative)
        # 同名未完成种子存在、却无法还原它在本条目内写哪些文件时，不能证明
        # 任何路径与它不重叠，整批保守等待。
        if not status.completed and mapped_selected == 0:
            return _DownloadFileBatch(files=(), consumable_hashes=())

    ready: list[_ReadyDownloadFile] = []
    for relative, references in sorted(writers.items()):
        source = _entry_file(entry, relative)
        if source is None or not source.is_file():
            continue
        try:
            actual_size = source.stat().st_size
        except OSError:
            continue
        expected_sizes = {int(file.size_bytes) for _status, file in references}
        if len(expected_sizes) != 1 or actual_size not in expected_sizes:
            continue
        if not all(_torrent_file_completed(status, file) for status, file in references):
            continue
        ready.append(
            _ReadyDownloadFile(
                relative_path=relative,
                size_bytes=actual_size,
                info_hashes=tuple(
                    sorted({str(status.info_hash).lower() for status, _file in references})
                ),
            )
        )

    ready_paths = {file.relative_path for file in ready}
    consumable = tuple(
        sorted(
            info_hash
            for info_hash, paths in video_paths_by_hash.items()
            if status_by_hash[info_hash].completed and paths and paths <= ready_paths
        )
    )
    return _DownloadFileBatch(files=tuple(ready), consumable_hashes=consumable)


async def _claimed_by_file_batches(session, entry_path: str) -> frozenset[str]:
    """所有既有分批入库作业已经认领的文件（条目内相对路径）。

    文件白名单一经持久化就归属于原作业：blocked 等人工，succeeded 已入库，
    failed/cancelled 只能显式重试原作业。后续批次若再次带上这些旧文件，不仅
    重做无用功，还会让一个历史坏文件反复拖住整批；因此新作业只接真正新增的
    ready 文件。原作业自身的重试直接使用已保存白名单，不经过这里，不受影响。
    """
    from movieclaw_db.models import Job

    prefix = _ingest_dedupe_key(entry_path)
    rows = (
        await session.execute(
            select(Job).where(
                Job.dedupe_key.startswith(f"{prefix}:"),  # type: ignore[union-attr]
            )
        )
    ).scalars()
    return frozenset(
        str(ready["path"])
        for job in rows
        for ready in (job.input_data or {}).get("ready_files") or ()
        if isinstance(ready, dict) and ready.get("path")
    )


async def _deferred_flipped(prefix: str) -> bool:
    """挂起条目是否出现整种完成或新的单文件安全批次。

    任一条目的种子翻转成完成、或从下载器消失（巡检将结合持久化投递锚
    再判定）都算翻转；下载器整体不可达时继续等待，绝不退回启发式。
    仍在下载时只补查下载器文件状态并
    stat 已被确认完成的少量文件；与台账指纹不同才唤醒完整巡检。
    """
    paths = [p for p in _deferred if p.startswith(prefix)]
    if not paths:
        return False
    briefs = await _downloader_briefs()
    if briefs is None:
        return False
    db = get_database()
    for path in paths:
        entry = Path(path)
        matches = _match_briefs(entry.name, briefs)
        if _torrent_verdict(matches) != "downloading":
            return True
        batch = await _completed_file_batch(entry, matches)
        if not batch.files:
            continue
        try:
            snap = await asyncio.to_thread(_snapshot_ready_files, entry, list(batch.files))
        except IngestSourceChanged:
            continue
        async with db.session() as session:
            record = (
                await session.execute(select(IngestEntry).where(IngestEntry.entry_path == path))
            ).scalar_one_or_none()
        if record is None or record.fingerprint != snap.fingerprint:
            return True
    return False


async def _maybe_wake_blocked_job(
    session,
    entry: Path,
    record: IngestEntry | None,
    job,
    parser_retry: bool,
) -> None:
    """blocked 任务等的是人，也等两种系统输入变化，各自到来时原地唤醒：

    1. **解析能力升级**（代码或 NER 模型任一半更新，见 _ingest_handler_revision）：
       同一修订号只唤醒一次，避免每轮巡检/每次重启反复消耗 NAS IO；
    2. **条目内容变化**（用户换上修好的文件 / 季包补了新集）：blocked 前
       落账的指纹与当前快照不一致即唤醒——任务化之前这类记录靠指纹对比
       自动重入库，blocked 挡在快照之前会把这条承诺变成空话。
       唤醒只是重新入队，任务自己会重新走完成检测（下载中标记/静默窗口/
       下载器状态），不会复制在途文件。

    快照成本与任务化之前巡检对待处理记录的树遍历相当，且只发生在本就被
    事件/兜底触发的巡检里，不新增主动扫描。
    """
    if parser_retry and job.handler_revision != _ingest_handler_revision():
        # 旧处理器因季集解析能力不足而 blocked：新能力只自动唤醒一次。
        job.handler_revision = _ingest_handler_revision()
        await jobs.retry_job(
            session,
            job,
            actor_kind="system",
            actor_name="MovieClaw 升级补偿",
            origin="system",
        )
        logger.info("升级后重新尝试部分入库条目：%s（job=%s）", entry.name, job.id)
        return
    if "ready_files" in (job.input_data or {}):
        # 文件级批次作业的输入钉死了白名单，其 ready: 指纹与全树指纹永不
        # 相等——按指纹对比唤醒只会让它反复按旧白名单重跑再挂起（无限
        # 唤醒）。批次挂起的解锁只走上面的解析能力升级或人工处理。
        return
    snap = await asyncio.to_thread(_snapshot, entry)
    blocked_fingerprint = (
        record.fingerprint
        if record is not None
        else str(job.input_data.get("detected_fingerprint") or "")
    )
    if blocked_fingerprint and snap.fingerprint != blocked_fingerprint:
        await jobs.retry_job(
            session,
            job,
            actor_kind="system",
            actor_name="监听导入巡检",
            origin="system",
        )
        logger.info("监听条目内容变化，重新唤醒等待中的任务：%s（job=%s）", entry.name, job.id)


def _superseded_blocked_batch(
    job,
    snap: _EntrySnapshot | None,
    partial_batch: _DownloadFileBatch | None,
) -> bool:
    """挂起的分批入库作业是否已被新的一轮处理取代。

    分批作业的白名单已经钉死，只能等人工或解析能力升级——但同条目剩下的集与
    它无关：后续批次、以及整种下完后的整树入库，都必须能绕过它继续推进。只有
    "又是同一批"才让它继续拦着（否则会并发重复搬同一批文件）。

    整树作业挂起时不适用：它覆盖整个条目，没有"下一批"的说法，重复放行只会
    每轮重建同一个作业。
    """
    if job.status != JobStatus.BLOCKED:
        return False
    input_data = job.input_data or {}
    if "ready_files" not in input_data:
        return False
    if partial_batch is None:
        return True  # 整种已下完：整树入库不该被某一批的挂起连坐
    if snap is None:
        return False
    return str(input_data.get("detected_fingerprint") or "") != snap.fingerprint


async def _process_entry(
    rule: ImportWatch,
    library: Library | None,
    watch_root: Path,
    entry: Path,
    briefs: list | None,
    *,
    execute_inline: bool,
) -> None:
    path_str = str(entry)
    if briefs is None:
        # 下载器配置存在但本轮全部不可达：绝不能回退到静默时间猜测。把条目
        # 留在挂起表，API 恢复后由状态轮询重新判定，不读整棵目录。
        _stability.pop(path_str, None)
        _deferred.setdefault(path_str, time.monotonic())
        return
    # 权威信号优先：能匹配到下载器种子时以下载器状态为准。报未完成的条目
    # 先尝试按文件完成证据拆出安全批次；没有可放行文件时只记入挂起表，
    # 状态轮询期间不做目录全树快照——绝不能
    # 一忘了之：下载完成的一瞬 fs 事件已经停了，下载器 API 又常慢半拍
    # （最后分片校验完 progress 才翻 1，概览另有短缓存），此处若不留痕，
    # 事件/自检/兜底三条触发路径会同时失灵，条目无限期卡住且无告警
    # （线上实证 bug）。挂起表由状态轮询自检 + 兜底巡检共同照看
    matches = _match_briefs(entry.name, briefs)
    verdict = _torrent_verdict(matches)
    partial_batch: _DownloadFileBatch | None = None
    snap: _EntrySnapshot | None = None
    if verdict == "downloading":
        _stability.pop(path_str, None)
        if path_str not in _deferred:
            _deferred[path_str] = time.monotonic()
            logger.info(
                "「%s」匹配到未完成的下载器种子，挂起等待其完成（约每 %d 秒核对一次状态）",
                entry.name,
                DEFERRED_POLL_SECONDS,
            )
        partial_batch = await _completed_file_batch(entry, matches)
        if not partial_batch.files:
            return
        try:
            snap = await asyncio.to_thread(_snapshot_ready_files, entry, list(partial_batch.files))
        except IngestSourceChanged:
            return
        if not snap.videos:
            return
        logger.info(
            "「%s」仍有下载写入，先处理 %d 个已完成且路径无冲突的文件",
            entry.name,
            len(snap.videos),
        )
    else:
        _deferred.pop(path_str, None)

    db = get_database()
    async with db.session() as session:
        record = (
            await session.execute(select(IngestEntry).where(IngestEntry.entry_path == path_str))
        ).scalar_one_or_none()
        # 台账短路必须在快照**之前**：忽略条目（「跳过存量」目录里可能有
        # 成百上千行）每轮都做全树 stat 会让 NAS 磁盘永远无法休眠——一次
        # 索引查询远比树遍历便宜（模块头声明的磁盘休眠目标）
        if record is not None and record.status == IngestStatus.IGNORED:
            _failed_retry.pop(path_str, None)  # 失败后被人工忽略：不再退避重试
            return  # 用户拍板（或存量基线）永久忽略：指纹变化也不复活，恢复走接口
        if verdict is None and await _has_managed_download_claim(session, entry):
            # 可达 API 偶尔在重启/恢复窗口返回空列表，或展示名尚未同步。真实
            # 投递名与落点已持久化时继续 fail closed，避免第二条误放行路径。
            _stability.pop(path_str, None)
            _deferred.setdefault(path_str, time.monotonic())
            return
        latest_job = None if execute_inline else await _latest_ingest_job(session, path_str)
        parser_retry = _parser_gap_auto_retry(rule, record)
        if latest_job is not None and latest_job.status in ACTIVE_JOB_STATUSES:
            if latest_job.status == JobStatus.BLOCKED:
                await _maybe_wake_blocked_job(session, entry, record, latest_job, parser_retry)
            # 挂起的分批入库不能连坐后面的集：该批的白名单已经钉死，靠指纹
            # 唤醒只会让它按旧白名单反复重跑，因此它只能等人工——但剩下的集
            # 跟它无关，必须允许按新指纹另立作业继续往前走。
            if not _superseded_blocked_batch(latest_job, snap, partial_batch):
                # Job 已接管后不再反复遍历条目树（blocked 例外，见唤醒助手）；
                # 运行、等待重试与待人工认领都以统一状态机为事实源。
                _stability.pop(path_str, None)
                if partial_batch is None:
                    _deferred.pop(path_str, None)
                _failed_retry.pop(path_str, None)
                return

        if partial_batch is not None:
            # 每轮都避开所有历史分批作业已认领的文件，而不只在“最新作业恰好
            # blocked”时做。真实坏例是：大批次挂起后，一个新文件单独成功，
            # 最新作业随即变成 succeeded；旧实现下一轮便忘掉更早的 blocked，
            # 也再次带上已成功文件，把上千个旧文件连同新文件再建一批，任务
            # 中心因此指数式膨胀。
            claimed = await _claimed_by_file_batches(session, path_str)
            remaining = tuple(
                ready for ready in partial_batch.files if ready.relative_path not in claimed
            )
            if not remaining:
                return  # 本轮完成的文件全在挂起批次里：等人工，无事可做
            if len(remaining) != len(partial_batch.files):
                # 仍有文件卡在挂起批次里，种子不能算消费完毕
                partial_batch = _DownloadFileBatch(files=remaining, consumable_hashes=())
                try:
                    snap = await asyncio.to_thread(_snapshot_ready_files, entry, list(remaining))
                except IngestSourceChanged:
                    return
                if not snap.videos:
                    return

        if snap is None:
            snap = await asyncio.to_thread(_snapshot, entry)
        disc_retry = bool(
            snap.has_disc
            and record is not None
            and record.status == IngestStatus.SKIPPED
            and "原盘目录（BDMV/VIDEO_TS）暂不支持自动入库" in (record.message or "")
        )
        if latest_job is not None and latest_job.status in {
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }:
            reconciled_disc_job = bool(
                disc_retry
                and latest_job.status == JobStatus.CANCELLED
                and latest_job.cancel_requested_by == "system:disc-batch-reconcile"
            )
            # 用户取消或重试耗尽后，同一磁盘版本不能被启动补扫悄悄复活；
            # 唯一例外是升级对账主动取消的旧版原盘分段 Job——那不是用户意图，
            # 必须允许新处理器按完整原盘重新建立一次作业。
            # 已取消代表用户明确停止，只能显式重试；失败任务可由人工“恢复”
            # 清空台账指纹，或在源内容真的变化后由监听器建立一条新作业。
            terminal_fingerprint = (
                record.fingerprint
                if record is not None
                else str(latest_job.input_data.get("detected_fingerprint") or "")
            )
            if not reconciled_disc_job and (
                latest_job.status == JobStatus.CANCELLED
                or terminal_fingerprint == snap.fingerprint
            ):
                return
        # 标记文件与权威信号矛盾（说完成却还有 .!qB 等）说明匹配可疑，从严按
        # 下载中处理。必须在静默观察表留痕：CIFS/NFS 等不产生 fs 事件的挂载
        # 上，标记消失不会有事件叫醒我们，一忘了之条目就永远卡死；留痕后
        # _has_pending 看得见它，到点自检与每小时兜底会持续回来看（标记文件
        # 计入指纹，它消失后指纹必变，自然重新起算静默窗口）
        if snap.has_marker:
            _stability[path_str] = (snap.fingerprint, time.monotonic())
            return

        if record is not None and record.fingerprint == snap.fingerprint:
            if record.status != IngestStatus.FAILED and not parser_retry and not disc_retry:
                return  # 已处理且没变化（pending 等的是人工拍板，不是时间）
            if record.status == IngestStatus.FAILED:
                elapsed = (utcnow() - record.attempted_at).total_seconds()
                if elapsed < FAILED_RETRY_SECONDS:
                    # 环境故障退避中。进程重启会丢失失败重试表——在此按已过的
                    # 退避时间补记，保证退避到点仍有第三唤醒源接得住
                    _failed_retry.setdefault(path_str, time.monotonic() - elapsed)
                    return
            elif parser_retry:
                # 只在真正决定重跑时说话：FAILED 记录本来就按小时退避重试，
                # 不属于升级补偿，不能让退避中的新记录每轮巡检都刷这条日志
                logger.info("升级后重新检查旧版部分入库记录：%s", entry.name)

        if verdict == "complete" or partial_batch is not None:
            # 下载器确认完成：整种或单文件证据都跳过静默窗口，立即处理。
            _stability.pop(path_str, None)
        else:
            # 启发式兜底（没有权威任务或持久化投递锚的非种子来源）：
            # 同一指纹连续稳定 QUIET_SECONDS 才认为下载落定
            now = time.monotonic()
            previous = _stability.get(path_str)
            if previous is None or previous[0] != snap.fingerprint:
                _stability[path_str] = (snap.fingerprint, now)
                return
            if now - previous[1] < QUIET_SECONDS:
                return

        matched_hashes = (
            sorted({info_hash for ready in partial_batch.files for info_hash in ready.info_hashes})
            if partial_batch is not None
            else [b.info_hash for b in matches if b.info_hash]
        )
        consumable_hashes = (
            list(partial_batch.consumable_hashes) if partial_batch is not None else None
        )
        if execute_inline:
            await _ingest_entry(
                session,
                rule,
                library,
                watch_root,
                entry,
                snap,
                record,
                matched_hashes,
                consumable_hashes,
            )
            if partial_batch is not None:
                _deferred[path_str] = time.monotonic()
            return
        created = await enqueue_ingest_job(
            session,
            rule,
            entry,
            snap,
            matched_hashes=matched_hashes,
            ready_files=partial_batch.files if partial_batch is not None else None,
            consumable_hashes=(
                partial_batch.consumable_hashes if partial_batch is not None else None
            ),
            keep_deferred=partial_batch is not None,
            record=record,
        )
        _stability.pop(path_str, None)
        if partial_batch is None:
            _deferred.pop(path_str, None)
        else:
            _deferred[path_str] = time.monotonic()
        _failed_retry.pop(path_str, None)
        if created.created:
            logger.info("监听条目已进入后台作业：%s（job=%s）", entry.name, created.job.id)


# ---------------------------------------------------------------------------
# 单条目入库
# ---------------------------------------------------------------------------


async def _ingest_entry(
    session,
    rule: ImportWatch,
    library: Library | None,
    watch_root: Path,
    entry: Path,
    snap: _EntrySnapshot,
    record: IngestEntry | None,
    matched_hashes: list[str] | None = None,
    consumable_hashes: list[str] | None = None,
    forced_item: MediaItem | None = None,
    job_context: jobs.JobContext | None = None,
) -> IngestEntry:
    strategy = rule.strategy
    # 目标库：指定库规则即入参；auto 规则在识别出作品后才决定（见下），
    # conclude 闭包读的是当下的 dest_library——选定目标前失败的条目落账无归属库。
    # 自定义目录规则（staging 非空）不涉及任何库：识别改名后落该目录即完成，
    # 文件是"过客"（外部流转后由库根扫描收尾），不写库台账、不生成资产
    staging = rule.target_path
    dest_library: Library | None = library
    # 与 dest_library 同理：conclude 读的是当下的 item——定出身份前失败/跳过的
    # 条目落账无作品归属，摘要行的作品去重自然不会把它算进任何一部
    item: MediaItem | None = None
    # 手动下载的 hash 锚只服务于这次在途导入：一旦文件已成功整理，和台账
    # 一起提交删除。删除依据是本次条目匹配到的全部 hash + 最终媒体身份，
    # 不能只删“身份识别实际选中的那一行”：同一 hash 若先被订阅工单认领，
    # 手动锚虽然没有参与识别，也已经随同一批文件完成了使命。
    manual_intent: ManualDownloadIntent | None = None
    # 只保存本次作业实际新增的条目内相对文件名，供任务中心展示本轮成果；
    # 已在库而跳过的旧文件不计入，也不暴露监听目录或媒体库的绝对路径。
    imported_files: list[str] = []
    # 同一个监听条目本轮成功搬入的文件属于一个用户可理解的入库批次；增量
    # 季包下一轮再补进来的集会拿新批次号，首页因此只摘要最后一次变化。
    added_batch_id = uuid4().hex

    async def conclude(status: IngestStatus, message: str, imported: int = 0) -> IngestEntry:
        if status is IngestStatus.IMPORTED and not snap.fingerprint.startswith("ready:"):
            # 整树结论成功 = 条目当前所有文件都已处理：仍挂着的分批 blocked
            # 作业（白名单钉死、等人工）已被彻底取代，就地收口——否则它们会
            # 以「需要处理」永远挂在任务中心（升级补偿只唤醒每个条目最新的
            # 作业，轮不到这些历史批次；NAS 实测滞留 4 条）
            stale_batches = (
                (
                    await session.execute(
                        select(Job).where(
                            Job.dedupe_key.startswith(  # type: ignore[union-attr]
                                f"{_ingest_dedupe_key(str(entry))}:"
                            ),
                            Job.status == JobStatus.BLOCKED,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for stale in stale_batches:
                if job_context is not None and stale.id == job_context.job_id:
                    continue  # 防御：绝不取消当前正在执行的作业
                await jobs.request_cancel(
                    session,
                    stale.id,
                    requested_by="system:ingest-superseded",
                    reason="该条目所有文件已完整入库，本批次任务被新一轮入库取代，系统自动收口",
                )
                logger.info(
                    "整树入库完成，收口被取代的分批挂起作业：%s（job=%s）",
                    entry.name,
                    stale.id,
                )
        if status is IngestStatus.IMPORTED and item is not None and item.id is not None:
            source_hashes = matched_hashes if consumable_hashes is None else consumable_hashes
            hashes = sorted({value.lower() for value in source_hashes or [] if value})
            if hashes:
                intents = list(
                    (
                        await session.execute(
                            select(ManualDownloadIntent).where(
                                ManualDownloadIntent.info_hash.in_(hashes),  # type: ignore[union-attr]
                                ManualDownloadIntent.media_item_id == item.id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for intent in intents:
                    await session.delete(intent)
                    logger.info("手动下载身份锚已随成功入库消费：hash=%s", intent.info_hash)
        saved = await _save_record(
            session,
            dest_library,
            str(entry),
            snap.fingerprint,
            record,
            status,
            message,
            imported,
            item,
            schedule_retry=job_context is None,
        )
        if status is IngestStatus.IMPORTED and job_context is not None and imported_files:
            await job_context.update_progress(
                mode="determinate",
                phase="finalizing",
                message=message,
                percent=100.0,
                phase_index=4,
                phase_count=4,
                details={
                    "entry_name": entry.name,
                    "rule_id": rule.id,
                    "media_item_id": item.id if item is not None else None,
                    "library_id": dest_library.id if dest_library is not None else None,
                    "imported_files": list(dict.fromkeys(imported_files)),
                },
            )
        return saved

    # kind 先验：指定库取库类型；auto 规则取规则声明（识别链按 movie/tv 分叉）
    kind = MediaKind(library.kind if library is not None else rule.kind)
    disc_root: Path | None = None
    if snap.has_disc:
        if kind is not MediaKind.MOVIE:
            return await conclude(IngestStatus.PENDING, "剧集原盘暂不能确定季集边界，请人工整理")
        if any(root == watch_root for root in snap.disc_roots):
            return await conclude(
                IngestStatus.PENDING,
                "BDMV/VIDEO_TS 不能直接作为监听目录的顶层条目；"
                "请先放进单独的「标题 (年份)」目录，避免越界处理同目录其他下载",
            )
        if len(snap.disc_roots) != 1 or snap.videos:
            return await conclude(
                IngestStatus.PENDING,
                f"条目包含 {len(snap.disc_roots)} 个原盘或混合视频，"
                "无法安全判定电影边界，请人工整理",
            )
        disc_root = snap.disc_roots[0]
        main = await asyncio.to_thread(disc_main_stream, disc_root)
        if main is None:
            return await conclude(
                IngestStatus.FAILED,
                "原盘缺少可读取的主播放流，可能尚未下载完整；文件变化后自动重试",
            )
    else:
        if not snap.videos:
            return await conclude(IngestStatus.SKIPPED, "条目中没有视频文件，已跳过")
        main = max(snap.videos, key=lambda f: f.stat().st_size)
    if job_context is not None:
        await job_context.update_progress(
            mode="indeterminate",
            phase="probing",
            message=f"正在检查「{main.name}」是否完整",
            phase_index=1,
            phase_count=4,
            details={"entry_name": entry.name, "rule_id": rule.id},
        )
    spec = await asyncio.to_thread(probe_media, main)
    if spec is None and ffprobe_available():
        return await conclude(
            IngestStatus.FAILED,
            f"主视频「{main.name}」探测失败——可能尚未下载完成或已损坏，文件变化后自动重试",
        )
    if spec is not None and disc_root is not None and (disc_root / "BDMV").is_dir():
        languages = await asyncio.to_thread(read_clpi_languages, main)
        if languages is not None:
            spec = enrich_spec_with_clpi(spec, languages)
        playlist = await asyncio.to_thread(read_main_playlist, disc_root)
        if playlist is not None and playlist.duration_seconds > 0:
            spec = replace(spec, duration_seconds=playlist.duration_seconds)

    # 身份优先级：人工认领（forced_item，用户拍板最高权威）→ 订阅工单认领
    # （info_hash 命中在途投递 → 继承投递时锚定的精确身份，零猜测）→
    # NFO/名称解析识别链（第三方下载的兜底）。自定义目录规则同样认领——
    # 身份与落点分离：认领只取身份（免重新识别、命名更准），落点始终由
    # 规则声明决定（docs/design/strm-workflow.md 2.3）
    if job_context is not None:
        await job_context.update_progress(
            mode="indeterminate",
            phase="identifying",
            message=f"正在识别「{entry.name}」",
            phase_index=2,
            phase_count=4,
            details={"entry_name": entry.name, "rule_id": rule.id},
        )
    if forced_item is None and record is not None and record.claimed_tmdb_id is not None:
        # 台账里钉过认领身份：认领当轮环境故障（探测/搬运/TMDB 网络）失败
        # 后，自动重试凭这颗钉子还原用户拍板的身份，绝不回退重新识别——
        # 用户拍板是最高权威，不能被一次网络抖动作废
        claimed_kind = MediaKind(record.claimed_kind) if record.claimed_kind else kind
        try:
            forced_item = await MediaLibraryService(session, get_tmdb_client()).ensure_media_item(
                claimed_kind, record.claimed_tmdb_id
            )
        except Exception as exc:  # noqa: BLE001 -- TMDB 不可达等环境故障，退避重试
            return await conclude(
                IngestStatus.FAILED, f"按认领身份重建条目失败（{exc}）；将自动重试"
            )
    if forced_item is not None:
        item, pinned_library_id = forced_item, None
        identity_source: str | None = None
    else:
        item, pinned_library_id = await _wanted_identity(session, matched_hashes or [])
        identity_source = "subscription" if item is not None else None
        if item is None:
            item, pinned_library_id, manual_intent = await _manual_download_identity(
                session, matched_hashes or []
            )
            identity_source = "manual" if item is not None else None
        if item is None:
            try:
                item = await _identify(session, kind, watch_root, disc_root or main, spec)
            except IdentifyUnavailable as exc:
                # 环境故障：网络恢复后重试就能过，不该钉进待处理清单
                return await conclude(IngestStatus.FAILED, f"识别中断：{exc}；将自动重试")
    if item is None:
        return await conclude(
            IngestStatus.PENDING,
            f"无法自动识别「{entry.name}」对应的影视条目；"
            "可在监听导入清单中搜索认领，或把条目改名为「标题 (年份)」形式自动重试",
        )

    # 来源戳：入库文件行必须带上 (site, torrent)，洗版验证的 _file_from_attempt
    # 才能精确匹配「文件 ↔ 投递」。缺了它只能退化到时间兜底——两个包在同一
    # 时间窗交错完成时，会把别家包的文件记到自己账上（NAS 实测 HDKWeb 包的
    # 文件被记成 CHDWEB 投递，qb 里的 CHDWEB 任务白下、清理证据链也锚错）
    prov_site: str | None = None
    prov_torrent: str | None = None
    if manual_intent is not None:
        prov_site, prov_torrent = manual_intent.site_id, manual_intent.torrent_id
    elif matched_hashes:
        prov_site, prov_torrent = await _delivery_provenance(session, matched_hashes)

    # auto 规则：识别后决定目标库（docs/design/library-routing.md 2.3）。
    # 订阅/手动下载的已确认身份均沿用提交时定格的库——粘性 + 不让规则
    # 后续变化改写已在下载任务的归属；名称识别的身份才重新走收藏范围路由。
    route_note: str | None = None
    if staging is None and dest_library is None:
        if identity_source is not None and pinned_library_id is not None:
            dest_library = await session.get(Library, pinned_library_id)
            if dest_library is not None:
                if identity_source == "subscription":
                    route_note = f"入库到订阅指定的「{dest_library.name}」"
                else:
                    route_note = f"入库到手动下载时确认的「{dest_library.name}」"
        if dest_library is None:
            from movieclaw_api.services.library.routing import route_for_item

            decision = await route_for_item(session, kind.value, item)
            dest_library = decision.library
            route_note = decision.reason
        if dest_library is None:
            return await conclude(
                IngestStatus.FAILED,
                f"没有可用的{'电影' if kind is MediaKind.MOVIE else '剧集'}媒体库，"
                "无法自动路由入库；请先到「媒体库」创建",
            )

    if job_context is not None and dest_library is not None:
        assert dest_library.id is not None
        while not await job_context.acquire_target_resource("library", dest_library.id):
            await job_context.raise_if_cancelled()
            await job_context.update_progress(
                mode="waiting",
                phase="waiting_library",
                message=f"媒体库「{dest_library.name}」正在执行其他作业，等待安全入库",
                phase_index=3,
                phase_count=4,
                details={
                    "entry_name": entry.name,
                    "rule_id": rule.id,
                    "library_id": dest_library.id,
                },
            )
            await asyncio.sleep(2)

    # 发布信息以条目名为准（比单集文件名完整），与入库管线的种子名口径一致
    release_attrs = enrich(entry.name if entry.is_dir() else entry.stem)

    if staging is not None:
        # 自定义目录与库入库共用同一命名规范——回流文件名天然规范，
        # 库根扫描识别精准命中，这就是两段链路的全部衔接
        dest_dir = derive_entry_dir(staging, title=item.title, year=item.year, item=item)
    else:
        assert dest_library is not None
        dest_dir = derive_save_path(dest_library, title=item.title, year=item.year, item=item)
        if dest_dir is None:
            return await conclude(
                IngestStatus.FAILED, f"媒体库「{dest_library.name}」没有配置根路径，无法入库"
            )
        # 入库落点避让（docs/design/disc-version-layout.md §3）：条目目录
        # 本身已是原盘时改落同级版本目录。标签与 _transfer 的版本标签同源
        version_label = (spec.resolution if spec else None) or release_attrs.media_source or "V2"
        # 原盘整体入库时目录就是条目本身，不做多版本退让（main 的原盘支持）
        if disc_root is None:
            dest_dir = _avoid_disc_entry_dir(dest_dir, version_label)
    repo = LibraryFileRepository(session)
    assert item.id is not None
    assert staging is not None or (dest_library is not None and dest_library.id is not None)

    if disc_root is not None:
        label = (spec.resolution if spec else None) or release_attrs.media_source or "原盘"
        try:
            if job_context is None:
                final, created = await asyncio.to_thread(
                    _transfer_disc_tree, disc_root, Path(dest_dir), strategy, label
                )
            else:

                async def report_disc_copy(copied: int, total: int) -> None:
                    percent = copied / total * 100 if total else 100.0
                    await job_context.update_progress(
                        mode="determinate",
                        phase="transferring",
                        message=(
                            f"正在{'复制' if strategy == 'copy' else '硬链接'}完整原盘"
                        ),
                        current=copied,
                        total=total,
                        percent=round(percent, 1),
                        phase_index=3,
                        phase_count=4,
                        details={
                            "entry_name": entry.name,
                            "library_id": dest_library.id if dest_library else None,
                            "bytes_copied": copied,
                        },
                    )

                final, created = await _transfer_disc_tree_for_job(
                    disc_root,
                    Path(dest_dir),
                    strategy,
                    label,
                    job_context,
                    report_disc_copy,
                )
        except (IngestConflictError, IngestError) as exc:
            return await conclude(IngestStatus.FAILED, str(exc))
        imported_files.append(final.name)
        if staging is not None:
            verb = "硬链接" if strategy == "hardlink" else "复制"
            return await conclude(
                IngestStatus.IMPORTED,
                f"已识别为《{item.title}》，完整原盘已{verb}到 {final}；"
                "文件进入媒体库根目录后将自动入账",
                1 if created else 0,
            )
        assert dest_library is not None and dest_library.id is not None
        stat = final.stat()
        await repo.upsert_by_path(
            LibraryFile(
                library_id=dest_library.id,
                media_item_id=item.id,
                season_number=0,
                episode_number=0,
                file_path=str(final),
                size_bytes=await asyncio.to_thread(_disc_total_size, final),
                file_mtime_ns=stat.st_mtime_ns,
                container="bluray" if (final / "BDMV").is_dir() else "dvd",
                resolution=spec.resolution if spec else None,
                video_codec=spec.video_codec if spec else None,
                hdr=spec.hdr if spec else None,
                bit_depth=spec.bit_depth if spec else None,
                duration_seconds=spec.duration_seconds if spec else None,
                bit_rate=spec.bit_rate if spec else None,
                frame_rate=spec.frame_rate if spec else None,
                color_space=spec.color_space if spec else None,
                audio_streams=list(spec.audio_streams) if spec else None,
                subtitle_streams=list(spec.subtitle_streams) if spec else None,
                media_source=release_attrs.media_source,
                release_group=release_attrs.release_group,
                source=FileSource.IMPORTED,
                site_id=prov_site,
                torrent_id=prov_torrent,
                added_batch_id=added_batch_id,
            )
        )
        await LibraryRepository(session).refresh_stats([dest_library.id])
        from movieclaw_api.services.library.nfo import write_entry_nfo
        from movieclaw_api.services.subscription import close_fulfilled_wanted

        await asyncio.to_thread(write_entry_nfo, final, item)
        await close_fulfilled_wanted(session, item.id)
        from movieclaw_api.services.media_scrape import ensure_assets

        if job_context is None:
            asyncio.get_running_loop().create_task(ensure_assets(item.id))
        else:
            await job_context.update_progress(
                mode="indeterminate",
                phase="finalizing",
                message=f"正在补齐《{item.title}》的图片与媒体目录资产",
                phase_index=4,
                phase_count=4,
                details={
                    "entry_name": entry.name,
                    "media_item_id": item.id,
                    "library_id": dest_library.id,
                },
            )
            await ensure_assets(item.id)
        message = (
            f"已识别为《{item.title}》"
            + (f"，{route_note}" if route_note else "")
            + f"，完整原盘已{'硬链接' if strategy == 'hardlink' else '复制'}到 {final}"
        )
        return await conclude(IngestStatus.IMPORTED, message, 1 if created else 0)

    files = [main] if kind is MediaKind.MOVIE else list(snap.videos)
    # 季集号按**整批**解析（设计见 library/units.py）：同一个包里的文件季号
    # 必然相同，逐文件各猜各的会解出一堆互相矛盾的季号（issue #185：36 集
    # 单季包散进 25 个不存在的季目录）。台账一并查好传进去，作为推断季号的
    # 守卫——幻觉季在这里被挡住，绝不落盘建目录
    units: dict[Path, FileUnit] = {}
    if kind is not MediaKind.MOVIE:
        known_seasons = set(
            (
                await session.execute(
                    select(MediaSeason.season_number).where(MediaSeason.media_item_id == item.id)
                )
            ).scalars()
        )
        units = resolve_units(
            files,
            entry_name=entry.name if entry.is_dir() else entry.stem,
            known_seasons=known_seasons,
        )
    notes: list[str] = []
    # 逐文件的失败按性质分档：环境故障（探测失败/搬运出错）→ failed 退避
    # 重试；解析不出季集、目标同名冲突 → pending 等人（重试改变不了结果）
    env_error = False
    parser_gap = False
    conflict = False
    dup_skipped = 0
    pilot_skipped: list[str] = []
    if kind is MediaKind.MOVIE and len(snap.videos) > 1:
        notes.append(f"已取最大文件为正片，忽略其余 {len(snap.videos) - 1} 个视频")

    # 自定义目录的幂等靠库存：中转文件上传后会被删除，无法像库目标那样按
    # 落点文件去重——季包补集触发的重处理会把旧集重新搬进中转、被外部工具
    # 重复上传。已回流入库（任一库在位）的单元视为闭环完成，跳过
    owned_units = await repo.owned_units(item.id) if staging is not None else set()

    imported = 0
    skipped_owned = 0
    total_bytes = sum(file.stat().st_size for file in files)
    completed_bytes = 0
    for file in files:
        if job_context is not None:
            await job_context.raise_if_cancelled()
        if kind is MediaKind.MOVIE:
            season, episode = 0, 0
        else:
            unit = units[file]
            season, episode = unit.season, unit.episode
            if not episode:
                if unit.explicit_pilot:
                    # 显式 E00：先导/特辑占位。集号 0 是管线的「无集号」哨兵，
                    # 按设计不自动入库；这不是解析缺口——重试和模型升级都改变
                    # 不了结果，不能把记录钉在待处理，如实说明后跳过即可
                    pilot_skipped.append(file.name)
                    completed_bytes += file.stat().st_size
                    continue
                notes.append(f"「{file.name}」解析不出集号，未入库")
                parser_gap = True
                completed_bytes += file.stat().st_size
                continue
            if season is None:
                # 台账守卫否决时补上具体原因——「解出第 36 季但 TMDB 没有这一季」
                # 比含混的「解析不出季号」可操作得多。前半句原样保留：
                # _PARSER_GAP_MARKERS 靠它识别解析缺口并在能力升级后自动补偿
                because = f"（{unit.rejected_reason}）" if unit.rejected_reason else ""
                notes.append(f"「{file.name}」解析不出季号，未入库{because}")
                parser_gap = True
                completed_bytes += file.stat().st_size
                continue
        if staging is not None and (season, episode) in owned_units:
            skipped_owned += 1
            completed_bytes += file.stat().st_size
            continue
        ext = file.suffix.lower()
        # 探测提前到命名之前：分辨率等文件属性是命名模板的可用占位符
        # （{resolution}/{media_source}/{release_group}），拿不到值的话
        # 用户配了这些占位符只会渲染成空——探测本身是纯读取，前移无副作用
        file_spec = spec if file == main else await asyncio.to_thread(probe_media, file)
        attrs = {
            "resolution": file_spec.resolution if file_spec else None,
            "media_source": release_attrs.media_source,
            "release_group": release_attrs.release_group,
        }
        # 文件名走命名模板（默认即 ``标题 (年份)`` / ``… - SxxEyy``）；
        # 版本标签等冲突后缀仍由 _transfer 追加，不进模板
        if kind is MediaKind.MOVIE:
            target = Path(dest_dir) / f"{movie_file_name(item, library=dest_library, **attrs)}{ext}"
        else:
            target = (
                Path(dest_dir)
                / season_dir_name(season, item, library=dest_library)
                / f"{episode_file_name(item, season, episode, library=dest_library, **attrs)}{ext}"
            )
        # 门禁逐文件生效：暂停的季包可能前几集完整、后几集残缺，主文件
        # 探测通过不代表每个文件都完整
        if file_spec is None and ffprobe_available():
            notes.append(f"「{file.name}」探测失败（可能不完整），未入库；文件变化后自动重试")
            env_error = True
            continue
        # 订阅投递的同档预检：单元已有同档或更高版本在库时不再落盘。同名
        # 幂等只挡得住同扩展名——换组包的 .mp4 会绕过 .mkv 的名字，落成
        # 一个"同档不构成升级、verify 也不会替换"的多余版本（NAS 实测
        # 洗版 E04/E06 的 CHDWEB 包把 E01 又入了一份）。真升级档位更高，
        # 预检拦不住；订阅之外的路径（手工监听/自定义目录）不受影响
        if (
            staging is None
            and identity_source == "subscription"
            and await _covered_by_library(
                session,
                item.id,
                season,
                episode,
                resolution=file_spec.resolution if file_spec else None,
                media_source=release_attrs.media_source,
                remux=release_attrs.remux,
            )
        ):
            dup_skipped += 1
            completed_bytes += file.stat().st_size
            continue
        label = (file_spec.resolution if file_spec else None) or release_attrs.media_source or "V2"
        try:
            if job_context is None:
                final = await asyncio.to_thread(_transfer, file, target, strategy, label)
            else:

                async def report_copy(
                    copied: int,
                    _total: int,
                    *,
                    _file=file,
                    _completed=completed_bytes,
                    _total_bytes=total_bytes,
                ) -> None:
                    current = min(_completed + copied, _total_bytes)
                    percent = current / _total_bytes * 100 if _total_bytes else 100.0
                    await job_context.update_progress(
                        mode="determinate",
                        phase="transferring",
                        message=(
                            f"正在{'复制' if strategy == 'copy' else '硬链接'}「{_file.name}」"
                        ),
                        current=current,
                        total=_total_bytes,
                        percent=round(percent, 1),
                        phase_index=3,
                        phase_count=4,
                        details={
                            "entry_name": entry.name,
                            "file_name": _file.name,
                            "library_id": dest_library.id if dest_library else None,
                            "bytes_copied": current,
                        },
                    )

                final = await _transfer_for_job(
                    file,
                    target,
                    strategy,
                    label,
                    job_context,
                    report_copy,
                )
        except IngestConflictError as exc:
            # 冲突先做同档去重裁决：库里已有同档或更高版本时跳过即是完成，
            # 不阻塞任务；判不了或来件更优才留给人工
            if staging is None and await _covered_by_library(
                session,
                item.id,
                season,
                episode,
                resolution=file_spec.resolution if file_spec else None,
                media_source=release_attrs.media_source,
                remux=release_attrs.remux,
            ):
                dup_skipped += 1
                completed_bytes += file.stat().st_size
                continue
            notes.append(str(exc))
            conflict = True
            continue
        except IngestError as exc:
            notes.append(str(exc))
            env_error = True
            continue
        completed_bytes += file.stat().st_size
        if final is None:
            continue  # 同一内容已在目标（重复处理/增量重扫），静默幂等
        relative_name = _relative_entry_file(entry, file)
        imported_files.append(file.name if relative_name in {None, "."} else relative_name)
        if staging is not None:
            # 自定义目录：文件是"过客"，搬到位即完成，不写库台账
            imported += 1
            continue
        assert dest_library is not None and dest_library.id is not None
        # stat 落位后的目标文件（跨盘复制时 mtime 与源不同），size/mtime 一次拿全
        final_stat = final.stat()
        await repo.upsert_by_path(
            LibraryFile(
                library_id=dest_library.id,
                media_item_id=item.id,
                season_number=season,
                episode_number=episode,
                file_path=str(final),
                size_bytes=final_stat.st_size,
                file_mtime_ns=final_stat.st_mtime_ns,
                container=final.suffix.lstrip(".").lower() or None,
                resolution=file_spec.resolution if file_spec else None,
                video_codec=file_spec.video_codec if file_spec else None,
                hdr=file_spec.hdr if file_spec else None,
                bit_depth=file_spec.bit_depth if file_spec else None,
                duration_seconds=file_spec.duration_seconds if file_spec else None,
                bit_rate=file_spec.bit_rate if file_spec else None,
                frame_rate=file_spec.frame_rate if file_spec else None,
                color_space=file_spec.color_space if file_spec else None,
                audio_streams=list(file_spec.audio_streams) if file_spec else None,
                subtitle_streams=list(file_spec.subtitle_streams) if file_spec else None,
                media_source=release_attrs.media_source,
                release_group=release_attrs.release_group,
                source=FileSource.IMPORTED,
                site_id=prov_site,
                torrent_id=prov_torrent,
                added_batch_id=added_batch_id,
            )
        )
        imported += 1

    if imported and staging is None:
        assert dest_library is not None and dest_library.id is not None
        # 监听入库不经过存量扫描；条目文件全部落账后在同一写路径刷新一次
        # 快照，避免首页要等下一轮定时扫描才看到新库存。
        await LibraryRepository(session).refresh_stats([dest_library.id])
        # NFO 身份档案：Emby 零歧义、自家重扫免收敛（已存在不覆盖，失败不阻断）
        from movieclaw_api.services.library.nfo import write_entry_nfo

        await asyncio.to_thread(write_entry_nfo, Path(dest_dir), item)
        # 库存对账：新入库的单元关闭对应的订阅工单（订阅止于投递）。
        # 自定义目录不在此列——"库里有"才算完成，工单在文件外部流转后
        # 由库根扫描入账时关闭
        from movieclaw_api.services.subscription import close_fulfilled_wanted

        await close_fulfilled_wanted(session, item.id)
        # 一次入库刮削的资产补齐：图片资产 + 媒体目录镜像（完整 NFO/海报/
        # 分集 thumb），后台执行不阻塞入库结论
        from movieclaw_api.services.media_scrape import ensure_assets

        assert item.id is not None
        if job_context is None:
            asyncio.get_running_loop().create_task(ensure_assets(item.id))
        else:
            await job_context.update_progress(
                mode="indeterminate",
                phase="finalizing",
                message=f"正在补齐《{item.title}》的图片与媒体目录资产",
                phase_index=4,
                phase_count=4,
                details={
                    "entry_name": entry.name,
                    "media_item_id": item.id,
                    "library_id": dest_library.id if dest_library else None,
                },
            )
            await ensure_assets(item.id)

    verb = "硬链接" if strategy == "hardlink" else "复制"
    if imported:
        message = f"已识别为《{item.title}》"
        if route_note:
            message += f"，{route_note}"
        message += f"，{verb} {imported} 个文件到 {dest_dir}"
        if staging is not None:
            message += "；文件进入媒体库根目录后将自动入账"
        if skipped_owned:
            message += f"；{skipped_owned} 个文件的内容已在媒体库，跳过"
        if dup_skipped:
            message += f"；{dup_skipped} 个文件在库中已有同档或更高版本，跳过"
        if pilot_skipped:
            message += f"；「{'」「'.join(pilot_skipped)}」是第 0 集（先导/特辑），暂不自动入库"
        if notes:
            message += "；" + "；".join(notes)
        status = (
            IngestStatus.FAILED
            if env_error
            else IngestStatus.PENDING
            if parser_gap or conflict
            else IngestStatus.IMPORTED
        )
        return await conclude(status, message, imported)
    elif notes:
        # 一个文件都没进：环境故障退避重试；纯解析问题进待处理等人拍板/改名
        message = "；".join(notes)
        if pilot_skipped:
            message += f"；「{'」「'.join(pilot_skipped)}」是第 0 集（先导/特辑），暂不自动入库"
        if record is not None and record.imported_count:
            # 重跑零新增（此前搬运过的文件都在原位被幂等短路，不计数）时
            # 不能丢掉已入库事实：message 是台账里唯一的人读摘要，只剩失败
            # 说明会让用户误以为整个条目一无所成
            message = (
                f"已识别为《{item.title}》，此前已入库 {record.imported_count} 个文件；{message}"
            )
        return await conclude(IngestStatus.FAILED if env_error else IngestStatus.PENDING, message)
    elif dup_skipped or pilot_skipped:
        # 季包附带内容全部与库存同档重复（换源后两个包先后投递最常见），
        # 或只剩 E00 先导占位：正常闭环，不是异常。
        # 同档跳过=单元内容已在库，必须与正常入库一样做库存对账关闭已
        # 满足的订阅工单——否则"整包都被同档跳过"的投递（工单集早已由
        # 别的源入库）会让下载任务永远挂在"等待入库"（NAS 实测 S06E11）
        if dup_skipped and staging is None:
            from movieclaw_api.services.subscription import close_fulfilled_wanted

            assert item.id is not None
            await close_fulfilled_wanted(session, item.id)
        parts = []
        if dup_skipped:
            parts.append(f"《{item.title}》的内容在库中已有同档或更高版本")
        if pilot_skipped:
            prefix = "" if dup_skipped else f"《{item.title}》的"
            parts.append(
                f"{prefix}「{'」「'.join(pilot_skipped)}」是第 0 集（先导/特辑），暂不自动入库"
            )
        return await conclude(IngestStatus.IMPORTED, "；".join(parts) + "，无需整理")
    elif skipped_owned:
        # 自定义目录条目的全部单元都已回流入库：链路闭环完成，不再搬运
        return await conclude(IngestStatus.IMPORTED, f"《{item.title}》的内容已在媒体库，无需整理")
    elif staging is not None:
        return await conclude(
            IngestStatus.IMPORTED, f"《{item.title}》的内容已全部在目标目录，无需搬运"
        )
    else:
        # 全部文件都已在库（此前处理过/多下载器重复下载）：结论记 imported
        return await conclude(IngestStatus.IMPORTED, f"《{item.title}》的内容已全部在库，无需搬运")


async def _wanted_identity(session, info_hashes: list[str]) -> tuple[MediaItem | None, int | None]:
    """按 info_hash 反查订阅工单，继承投递时锚定的精确身份。

    返回 (条目, 订阅定格的 library_id)——auto 规则用后者沿用订阅的入库目标
    （粘性：不对认领内容重新路由）；查不到返回 (None, None)。
    """
    if not info_hashes:
        return None, None
    from movieclaw_db.models import Subscription, SubscriptionDownloadAttempt, WantedItem

    result = await session.execute(
        select(MediaItem, Subscription.library_id)
        .join(Subscription, MediaItem.id == Subscription.media_item_id)  # type: ignore[arg-type]
        .join(WantedItem, WantedItem.subscription_id == Subscription.id)  # type: ignore[arg-type]
        .where(WantedItem.info_hash.in_(info_hashes))  # type: ignore[union-attr]
    )
    row = result.first()
    if row is None:
        # 自动换源后 wanted 只指向新主源；旧源可能因 H&R/用户所有权被保留，
        # 它之后若自行完成仍应继承原订阅身份，而不是退回模糊的文件名识别。
        result = await session.execute(
            select(MediaItem, Subscription.library_id)
            .join(Subscription, MediaItem.id == Subscription.media_item_id)  # type: ignore[arg-type]
            .join(
                SubscriptionDownloadAttempt,
                SubscriptionDownloadAttempt.subscription_id == Subscription.id,  # type: ignore[arg-type]
            )
            .where(SubscriptionDownloadAttempt.info_hash.in_(info_hashes))  # type: ignore[union-attr]
        )
        row = result.first()
    if row is None:
        return None, None
    item, library_id = row
    logger.info("条目按 info_hash 认领了订阅身份：《%s》", item.title)
    return item, library_id


async def _delivery_provenance(session, info_hashes: list[str]) -> tuple[str | None, str | None]:
    """按 info_hash 反查订阅投递记录的 (site_id, torrent_id) 来源戳。

    条目匹配到的 hash 就是这个种子本身，任何同 hash 的投递记录都是它的
    来源——与身份认领走哪条链无关。查不到（外部种子）返回 (None, None)。
    """
    from movieclaw_db.models import SubscriptionDownloadAttempt

    rows = (
        await session.execute(
            select(SubscriptionDownloadAttempt).where(
                SubscriptionDownloadAttempt.info_hash.in_(info_hashes)  # type: ignore[union-attr]
            )
        )
    ).scalars()
    for attempt in rows:
        if attempt.site_id:
            return attempt.site_id, attempt.torrent_id
    return None, None


async def _manual_download_identity(
    session, info_hashes: list[str]
) -> tuple[MediaItem | None, int | None, ManualDownloadIntent | None]:
    """按 info_hash 反查手动下载在提交时确认的身份与目标库。

    监听目录常被多个库复用，文件名不足以重新识别时，这颗锚把「搜索结果
    已确认的 TMDB 身份 → 收藏范围选库 → 实际投递目录」完整接到完成后的
    整理流程。订阅工单优先级更高，调用方只会在它未命中时走这里。
    """
    if not info_hashes:
        return None, None, None
    result = await session.execute(
        select(MediaItem, ManualDownloadIntent)
        .join(
            ManualDownloadIntent,
            MediaItem.id == ManualDownloadIntent.media_item_id,  # type: ignore[arg-type]
        )
        .where(ManualDownloadIntent.info_hash.in_(info_hashes))  # type: ignore[union-attr]
    )
    row = result.first()
    if row is None:
        return None, None, None
    item, intent = row
    logger.info("条目按 info_hash 认领了手动下载身份：《%s》", item.title)
    return item, intent.library_id, intent


class IdentifyUnavailable(Exception):
    """识别所需的外部依赖（TMDB）暂时不可达。

    与"识别不出"（返回 None）必须区分：前者是环境故障，交退避重试自愈；
    后者是信息不足，交人工拍板。混为一谈会让一次网络抖动把条目错误地
    钉进待处理清单——那里没有定时重试。message 是中文句子，直接进台账。
    """


def _entry_nfo_paths(kind: MediaKind, watch_root: Path, main: Path) -> list[Path]:
    """条目内条目级 NFO 的查找序列（就近优先，与本 kind 同名的先看）。

    只在**条目自身范围**内找：主视频同名 .nfo → 主视频各级父目录（向上
    到监听目录为止，不含监听目录本身——顶层一份游离的 movie.nfo 会把
    目录里每个条目都毒成同一部片）的 movie/tvshow.nfo。
    """
    names = ["movie.nfo", "tvshow.nfo"] if kind is MediaKind.MOVIE else ["tvshow.nfo", "movie.nfo"]
    paths = [main / names[0], main / names[1]] if main.is_dir() else [main.with_suffix(".nfo")]
    current = main.parent
    while current != watch_root and current != current.parent:
        paths.extend(current / n for n in names)
        current = current.parent
    return paths


async def _identify(
    session, kind: MediaKind, watch_root: Path, main: Path, spec
) -> MediaItem | None:
    """识别条目身份：NFO 身份声明优先 → 条目名/文件名解析 → TMDB 证据收敛。

    第三方工具整理过的内容常自带刮削档案（NFO 内含 tmdbid），是免猜测的
    精确身份；解析器与扫描器同源（read_entry_identity：只认条目级根元素，
    演员块里的 person id 天然隔离）。类型与本规则先验不符的 NFO 不采信
    （TMDB 的 movie/tv 是两套 id 空间），交后续识别链与人工拍板。
    返回 None = 证据不足以收敛；TMDB 不可达抛 ``IdentifyUnavailable``。
    """
    from movieclaw_api.services.library.nfo import read_entry_identity

    for nfo in _entry_nfo_paths(kind, watch_root, main):
        if not nfo.is_file():
            continue
        identity = read_entry_identity(nfo)
        if identity is None or identity.tmdb_id is None or identity.kind is not kind:
            continue
        try:
            return await MediaLibraryService(session, get_tmdb_client()).ensure_media_item(
                kind, identity.tmdb_id
            )
        except Exception as exc:
            raise IdentifyUnavailable(f"读到 NFO 身份但 TMDB 建档失败（{exc}）") from exc

    evidence = guess_evidence(kind, watch_root, main, is_disc=main.is_dir())
    if evidence is None:
        return None
    evidence.duration_seconds = spec.duration_seconds if spec else None
    try:
        tmdb_id = await verify_resolve(get_tmdb_client(), kind, evidence)
        if tmdb_id is None:
            return None
        return await MediaLibraryService(session, get_tmdb_client()).ensure_media_item(
            kind, tmdb_id
        )
    except Exception as exc:
        raise IdentifyUnavailable(f"TMDB 暂时不可达（{exc}）") from exc


# ---------------------------------------------------------------------------
# 搬运（线程池内运行）
# ---------------------------------------------------------------------------


def _same_payload(a: Path, b: Path) -> bool:
    """两个路径是否同一内容：同一 inode（硬链过）或尺寸相同（复制过）。"""
    try:
        if os.path.samefile(a, b):
            return True
        return a.stat().st_size == b.stat().st_size
    except OSError:
        return False


def _disc_total_size(disc_dir: Path) -> int:
    """原盘整树体积；不可读文件由后续搬运报错，不在这里伪造体积。"""
    return sum(path.stat().st_size for path in disc_dir.rglob("*") if path.is_file())


def _same_disc_payload(source: Path, target: Path) -> bool:
    """整树是否已落位：相对路径一致，文件为同一 inode 或尺寸与 mtime 一致。

    普通单文件历史逻辑只比尺寸；原盘往往有大量固定尺寸的流分段，单凭尺寸
    更容易把不同盘面误判成同一内容。复制路径保留源 mtime，因此无需读取数百
    GB 内容，也能比“路径 + 尺寸”多一道低成本身份校验。
    """
    try:
        source_files = {
            path.relative_to(source): path for path in source.rglob("*") if path.is_file()
        }
        target_files = {
            path.relative_to(target): path for path in target.rglob("*") if path.is_file()
        }
        if source_files.keys() != target_files.keys():
            return False
        for relative, path in source_files.items():
            other = target_files[relative]
            if os.path.samefile(path, other):
                continue
            source_stat = path.stat()
            target_stat = other.stat()
            if (
                source_stat.st_size != target_stat.st_size
                or source_stat.st_mtime_ns != target_stat.st_mtime_ns
            ):
                return False
        return True
    except OSError:
        return False


def _disc_destination(source: Path, base: Path, version_label: str) -> tuple[Path, bool]:
    """选择完整原盘目录落点，返回（目录，是否已存在同内容）。"""
    if not base.exists():
        return base, False
    if is_disc_dir(base) and _same_disc_payload(source, base):
        return base, True
    label = sanitize_folder_name(f"{version_label}原盘")
    candidate = base.with_name(f"{base.name} - {label}")
    serial = 2
    while candidate.exists():
        if is_disc_dir(candidate) and _same_disc_payload(source, candidate):
            return candidate, True
        candidate = base.with_name(f"{base.name} - {label} ({serial})")
        serial += 1
    return candidate, False


def _transfer_disc_tree(
    source: Path, base: Path, strategy: str, version_label: str
) -> tuple[Path, bool]:
    """完整保留 BDMV/VIDEO_TS 树并一次发布；不 remux、不拼接、不修改保种源。"""
    final, already_present = _disc_destination(source, base, version_label)
    if already_present:
        return final, False
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IngestError(f"创建原盘目标目录失败（{exc.strerror}）：{final.parent}") from exc
    stage = final.parent / f".{final.name}.{uuid4().hex[:8]}.part"
    try:
        stage.mkdir()
        copied = 0
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            target = stage / relative
            if path.is_symlink():
                raise IngestError(f"原盘包含符号链接，拒绝自动搬运：{relative}")
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not path.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if strategy == "copy":
                shutil.copy2(path, target)
            else:
                try:
                    os.link(path, target)
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        raise IngestError(
                            "原盘硬链接失败：监听目录与目标目录不在同一文件系统；"
                            "请把该监听规则改为「复制」"
                        ) from exc
                    raise
            copied += 1
        if copied == 0:
            raise IngestError("原盘目录中没有可搬运文件")
        rename_no_replace(stage, final)
        return final, True
    except FileExistsError as exc:
        raise IngestConflictError(f"原盘目标目录已被并发占用：{final.name}") from exc
    except IngestError:
        raise
    except OSError as exc:
        verb = "复制" if strategy == "copy" else "硬链接"
        raise IngestError(f"原盘{verb}失败（{exc.strerror}）：{source.name} → {final}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _disc_ingest_paths(final: Path) -> tuple[Path, Path]:
    """完整原盘 Job 的隐藏续传目录与源版本边车。"""
    stage = final.parent / f".{final.name}.movieclaw-ingest.part"
    return stage, stage.with_name(stage.name + ".json")


def _prepare_disc_ingest(
    source: Path, stage: Path, state_path: Path
) -> tuple[list[Path], list[tuple[Path, Path, int, int]], int]:
    """校验原盘源版本并准备稳定续传目录，返回文件清单与总字节数。"""
    directories: list[Path] = []
    files: list[tuple[Path, Path, int, int]] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise IngestError(f"原盘包含符号链接，拒绝自动搬运：{relative}")
        if path.is_dir():
            directories.append(relative)
            continue
        if not path.is_file():
            continue
        stat = path.stat()
        files.append((path, relative, stat.st_size, stat.st_mtime_ns))
    if not files:
        raise IngestError("原盘目录中没有可搬运文件")
    expected = {
        "source": str(source),
        "directories": [relative.as_posix() for relative in directories],
        "files": [
            [relative.as_posix(), size, mtime_ns]
            for _path, relative, size, mtime_ns in files
        ],
    }
    valid = False
    if stage.is_dir() and state_path.is_file():
        try:
            valid = json.loads(state_path.read_text(encoding="utf-8")) == expected
        except (OSError, json.JSONDecodeError):
            valid = False
    if not valid:
        if stage.exists():
            if stage.is_dir():
                shutil.rmtree(stage)
            else:
                stage.unlink()
        state_path.unlink(missing_ok=True)
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir(exist_ok=True)
    if not state_path.exists():
        temp = state_path.with_name(state_path.name + ".tmp")
        temp.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, state_path)
    return directories, files, sum(size for _path, _relative, size, _mtime_ns in files)


async def _transfer_disc_tree_for_job(
    source: Path,
    base: Path,
    strategy: str,
    version_label: str,
    context: jobs.JobContext,
    on_progress: Callable[[int, int], Awaitable[None]],
) -> tuple[Path, bool]:
    """Job 专用完整原盘搬运：复制可续传，整树完成后才原子发布。"""
    final, already_present = await asyncio.to_thread(
        _disc_destination, source, base, version_label
    )
    if already_present:
        stage, state_path = _disc_ingest_paths(final)
        if stage.exists():
            await asyncio.to_thread(shutil.rmtree, stage, True)
        state_path.unlink(missing_ok=True)
        return final, False
    stage, state_path = _disc_ingest_paths(final)
    try:
        directories, files, total = await asyncio.to_thread(
            _prepare_disc_ingest, source, stage, state_path
        )
        for relative in directories:
            await asyncio.to_thread((stage / relative).mkdir, parents=True, exist_ok=True)
        completed = 0
        for path, relative, size, mtime_ns in files:
            await context.raise_if_cancelled()
            target = stage / relative
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            if strategy == "hardlink":
                same_link = False
                if target.exists():
                    try:
                        same_link = os.path.samefile(path, target)
                    except OSError:
                        same_link = False
                    if not same_link:
                        target.unlink()
                if not same_link:
                    try:
                        await asyncio.to_thread(os.link, path, target)
                    except OSError as exc:
                        if exc.errno == errno.EXDEV:
                            raise IngestError(
                                "原盘硬链接失败：监听目录与目标目录不在同一文件系统；"
                                "请把该监听规则改为「复制」"
                            ) from exc
                        raise
                completed += size
                await on_progress(completed, total)
                continue

            target_complete = False
            if target.is_file():
                stat = target.stat()
                target_complete = stat.st_size == size and stat.st_mtime_ns == mtime_ns
                if not target_complete:
                    target.unlink()
            if target_complete:
                completed += size
                await on_progress(completed, total)
                continue

            partial = target.with_name(f".{target.name}.part")
            if partial.exists() and partial.stat().st_size > size:
                partial.unlink()
            if size == 0:
                partial.touch(exist_ok=True)
            copied = partial.stat().st_size if partial.exists() else 0
            await on_progress(completed + copied, total)
            while copied < size:
                await context.raise_if_cancelled()
                copied = await asyncio.to_thread(
                    _copy_ingest_chunk,
                    path,
                    partial,
                    expected_size=size,
                    expected_mtime_ns=mtime_ns,
                )
                await on_progress(completed + copied, total)
            await asyncio.to_thread(rename_no_replace, partial, target)
            target_stat = target.stat()
            await asyncio.to_thread(
                os.utime,
                target,
                ns=(target_stat.st_atime_ns, mtime_ns),
            )
            completed += size

        await context.raise_if_cancelled()
        try:
            await asyncio.to_thread(rename_no_replace, stage, final)
        except FileExistsError as exc:
            if await asyncio.to_thread(_same_disc_payload, source, final):
                await asyncio.to_thread(shutil.rmtree, stage, True)
                state_path.unlink(missing_ok=True)
                return final, False
            raise IngestConflictError(f"原盘目标目录已被并发占用：{final.name}") from exc
        state_path.unlink(missing_ok=True)
        return final, True
    except IngestSourceChanged:
        if stage.exists():
            await asyncio.to_thread(shutil.rmtree, stage, True)
        state_path.unlink(missing_ok=True)
        raise
    except (jobs.JobCancelled, asyncio.CancelledError, IngestError):
        # 更新/取消与暂态错误保留已确认字节；下一次执行按源版本边车续传。
        raise
    except OSError as exc:
        verb = "复制" if strategy == "copy" else "硬链接"
        raise IngestError(f"原盘{verb}暂未完成（{exc}）：{source.name} → {final}") from exc


def _avoid_disc_entry_dir(dest_dir: str, version_label: str) -> str:
    """入库落点避让：目标条目目录本身已是原盘目录（BDMV/VIDEO_TS）时，
    改落同级版本目录 ``标题 (年份) - {标签}``（docs/design/disc-version-layout.md §3）。

    生态规范不支持"原盘目录内部再放文件"：嵌套会让下游播放器丢版本、
    库扫描误标缺失（扫描不进原盘目录）、旧盘清理殃及新文件。判物理不判
    台账——用户刚手动摆好原盘、扫描还没收编的窗口期也必须避让。
    """
    base = Path(dest_dir)
    if not is_disc_dir(base):
        return dest_dir
    label = sanitize_folder_name(version_label)
    candidate = base.with_name(f"{base.name} - {label}")
    serial = 2
    while is_disc_dir(candidate):
        # 同名版本目录也被手动摆了原盘：追加序号，绝不落进任何原盘内部
        candidate = base.with_name(f"{base.name} - {label} ({serial})")
        serial += 1
    logger.info("目标条目目录是原盘，入库落点避让到同级版本目录：%s → %s", base, candidate)
    return str(candidate)


def _resolve_transfer_target(src: Path, dst: Path, version_label: str) -> Path | None:
    """按既有多版本规则选择最终路径；None 表示内容已经落位。"""
    final = dst
    if final.exists():
        if _same_payload(src, final):
            return None
        final = dst.with_name(f"{dst.stem} - {version_label}{dst.suffix}")
        if final.exists():
            if _same_payload(src, final):
                return None
            raise IngestConflictError(f"目标已存在同名文件，跳过以免覆盖：{final.name}")
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IngestError(f"创建目标目录失败（{exc.strerror}）：{final.parent}") from exc
    return final


def _transfer(src: Path, dst: Path, strategy: str, version_label: str) -> Path | None:
    """把源文件按策略搬到目标；返回最终落位路径，None = 同内容已在库。

    目标已存在且内容不同时按多版本约定退让到 ``… - 版本标签.ext``；
    落位一律「先在目标目录写 .part 临时名（复制=copyfile、硬链=os.link），
    再 rename_no_replace 发布到最终名」——rename 保证极空间等 FUSE 存储的
    索引能看到新文件（见 fsops 模块头），NOREPLACE 保证不静默覆盖；
    库根的 watchdog/扫描不认 .part 后缀，不会看到半成品。
    """
    final = _resolve_transfer_target(src, dst, version_label)
    if final is None:
        return None

    if strategy == "copy":
        part = final.with_name(final.name + ".part")
        try:
            shutil.copyfile(src, part)
            rename_no_replace(part, final)
        except FileExistsError as exc:
            raise IngestConflictError(f"目标已存在同名文件，跳过以免覆盖：{final.name}") from exc
        except OSError as exc:
            raise IngestError(f"复制失败（{exc.strerror}）：{src.name} → {final}") from exc
        finally:
            part.unlink(missing_ok=True)
        return final

    # 硬链临时名带随机段：同名条目的失败重试/并发不会互踩残留；崩溃残留的
    # .part 对扫描不可见且不占空间（硬链接），下次同条目重试不受其影响
    tmp = final.with_name(f"{final.name}.{uuid4().hex[:8]}.part")
    try:
        os.link(src, tmp)
        rename_no_replace(tmp, final)
    except FileExistsError as exc:
        raise IngestConflictError(f"目标已存在同名文件，跳过以免覆盖：{final.name}") from exc
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise IngestError(
                f"硬链接失败：监听目录与目标目录不在同一文件系统（{src.name}）。"
                "请把该监听目录的策略改为「复制」，或把两者放到同一存储卷"
            ) from exc
        raise IngestError(f"硬链接失败（{exc.strerror}）：{src.name} → {final}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return final


def _ingest_copy_paths(final: Path) -> tuple[Path, Path]:
    """返回 Job 复制的隐藏续传文件与源版本边车。"""
    partial = final.with_name(f".{final.name}.movieclaw-ingest.part")
    return partial, partial.with_name(partial.name + ".json")


def _prepare_ingest_copy(source: Path, partial: Path, state_path: Path) -> tuple[int, int, int]:
    """校验/初始化续传点，返回（源大小、源 mtime_ns、已复制字节）。"""
    stat = source.stat()
    expected = {
        "source": str(source),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    valid = False
    if partial.exists() and state_path.is_file():
        try:
            valid = json.loads(state_path.read_text(encoding="utf-8")) == expected
        except (OSError, json.JSONDecodeError):
            valid = False
    if not valid:
        partial.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
    partial.parent.mkdir(parents=True, exist_ok=True)
    if not state_path.exists():
        temp = state_path.with_name(state_path.name + ".tmp")
        temp.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, state_path)
    copied = partial.stat().st_size if partial.exists() else 0
    if copied > stat.st_size:
        partial.unlink(missing_ok=True)
        copied = 0
    return stat.st_size, stat.st_mtime_ns, copied


def _copy_ingest_chunk(
    source: Path,
    partial: Path,
    *,
    expected_size: int,
    expected_mtime_ns: int,
) -> int:
    """从现有长度续写一个块，源文件变化时拒绝发布混合内容。"""
    before = source.stat()
    if before.st_size != expected_size or before.st_mtime_ns != expected_mtime_ns:
        raise IngestSourceChanged(f"源文件仍在变化：{source.name}")
    copied = partial.stat().st_size if partial.exists() else 0
    if copied >= expected_size:
        partial.touch(exist_ok=True)
        return copied
    with source.open("rb") as source_file:
        source_file.seek(copied)
        chunk = source_file.read(_INGEST_COPY_CHUNK_BYTES)
    with partial.open("ab" if copied else "wb") as target_file:
        target_file.write(chunk)
        target_file.flush()
    after = source.stat()
    if after.st_size != expected_size or after.st_mtime_ns != expected_mtime_ns:
        raise IngestSourceChanged(f"源文件仍在变化：{source.name}")
    return copied + len(chunk)


async def _transfer_for_job(
    source: Path,
    target: Path,
    strategy: str,
    version_label: str,
    context: jobs.JobContext,
    on_copy_progress: Callable[[int, int], Awaitable[None]],
) -> Path | None:
    """Job 专用搬运：硬链接保持原子语义，复制按隐藏文件断点续传。"""
    final = await asyncio.to_thread(_resolve_transfer_target, source, target, version_label)
    if final is None:
        # 上次执行可能已发布最终文件、却在清理边车前被强制终止。恢复时既要
        # 以最终文件为幂等事实，也要清掉不再可能使用的隐藏续传状态。
        candidates = [target, target.with_name(f"{target.stem} - {version_label}{target.suffix}")]
        for candidate in candidates:
            if candidate.exists() and _same_payload(source, candidate):
                partial, state_path = _ingest_copy_paths(candidate)
                partial.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
        return None
    if strategy != "copy":
        return await asyncio.to_thread(_transfer, source, target, strategy, version_label)

    partial, state_path = _ingest_copy_paths(final)
    try:
        total, mtime_ns, copied = await asyncio.to_thread(
            _prepare_ingest_copy, source, partial, state_path
        )
        await on_copy_progress(copied, total)
        while copied < total:
            await context.raise_if_cancelled()
            copied = await asyncio.to_thread(
                _copy_ingest_chunk,
                source,
                partial,
                expected_size=total,
                expected_mtime_ns=mtime_ns,
            )
            await on_copy_progress(copied, total)
        await context.raise_if_cancelled()
        try:
            await asyncio.to_thread(rename_no_replace, partial, final)
        except FileExistsError as exc:
            if _same_payload(source, final):
                partial.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
                return None
            raise IngestConflictError(f"目标已存在同名文件，跳过以免覆盖：{final.name}") from exc
        state_path.unlink(missing_ok=True)
        return final
    except IngestSourceChanged:
        # 续传副本属于旧源版本，继续使用会把两个版本拼在一起；删除后短延迟
        # 重试，重新走下载完成检测。用户的 Job 意图本身继续保留。
        partial.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
        raise
    except jobs.JobCancelled:
        raise
    except asyncio.CancelledError:
        raise
    except IngestError:
        raise
    except OSError as exc:
        # 网络盘瞬断时刻意保留校验过的隐藏副本，下次从实际长度继续。
        raise IngestError(f"复制暂未完成（{exc}）：{source.name} → {final}") from exc


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------


async def _save_record(
    session,
    library: Library | None,
    entry_path: str,
    fingerprint: str,
    record: IngestEntry | None,
    status: IngestStatus,
    message: str,
    imported: int,
    item: MediaItem | None = None,
    *,
    schedule_retry: bool = True,
) -> IngestEntry:
    now = utcnow()
    if record is None:
        record = IngestEntry(
            library_id=library.id if library is not None else None,
            media_item_id=item.id if item is not None else None,
            entry_path=entry_path,
            fingerprint=fingerprint,
            status=status,
            message=message,
            imported_count=imported,
            attempted_at=now,
        )
        session.add(record)
    else:
        record.fingerprint = fingerprint
        record.status = status
        record.message = message
        record.imported_count += imported
        record.attempted_at = now
        record.updated_at = now
        if library is not None:
            record.library_id = library.id  # auto 条目此前失败无归属，路由成功后补上
        if item is not None:
            # 同理：识别不出而挂起的条目后来认领成功，这里补上身份
            record.media_item_id = item.id
    await session.commit()
    _stability.pop(entry_path, None)
    _deferred.pop(entry_path, None)
    # 失败结论必须记入失败重试表（第三个唤醒源）：此刻静默表/挂起表都已
    # 弹出该条目，下载早已结束不会再有 fs 事件，若不留痕，_arm_recheck 挂
    # 不上自检、兜底巡检的 _has_pending 也看不见它——「自动退避重试」就
    # 成了一句空话（回归：FAILED 条目落账后被彻底遗忘，永不重试）
    if status is IngestStatus.FAILED and schedule_retry:
        _failed_retry[entry_path] = time.monotonic()
    else:
        _failed_retry.pop(entry_path, None)
    # 全局红灯只留给环境故障（failed），且由 refresh_source_notice 按源目录
    # 聚合（巡检收尾/人工拍板后刷新）——识别不出（pending）不是错误，
    # 清单上的数字承载它，不在这里刷屏
    if status is IngestStatus.FAILED:
        logger.warning("监听导入未完成（%s）：%s", entry_path, message)
    else:
        logger.info("监听导入（%s）：%s", entry_path, message)
    return record


async def _attach_ingest_entry_resource(context: jobs.JobContext, record: IngestEntry) -> None:
    """处理结论落账后把 Job 关联到清单行，供认领/忽略动作精确接管。"""
    if record.id is None:
        return
    await context.attach_resource("ingest_entry", record.id)


async def _ensure_job_snapshot_stable(
    context: jobs.JobContext,
    snap: _EntrySnapshot,
    detected_fingerprint: str,
    *,
    rule_id: int,
    entry_name: str,
) -> None:
    """入队后磁盘版本变化时重新建立静默窗口，避免恢复后复制在途文件。"""
    if not detected_fingerprint or snap.fingerprint == detected_fingerprint or QUIET_SECONDS <= 0:
        return
    progress = await context.current_progress()
    details = dict(progress.get("details") or {})
    observed_fingerprint = str(details.get("stability_fingerprint") or "")
    try:
        observed_at = float(details.get("stability_observed_at") or 0)
    except (TypeError, ValueError):
        observed_at = 0
    now = time.time()
    elapsed = now - observed_at if observed_fingerprint == snap.fingerprint else 0
    if observed_fingerprint == snap.fingerprint and elapsed >= QUIET_SECONDS:
        return
    if observed_fingerprint != snap.fingerprint:
        observed_at = now
        elapsed = 0
    details.update(
        {
            "rule_id": rule_id,
            "entry_name": entry_name,
            "stability_fingerprint": snap.fingerprint,
            "stability_observed_at": observed_at,
        }
    )
    remaining = max(1, int(QUIET_SECONDS - elapsed))
    await context.update_progress(
        mode="waiting",
        phase="waiting_stable",
        message=f"「{entry_name}」在排队期间发生变化，等待下载完全停止",
        phase_index=1,
        phase_count=4,
        details=details,
    )
    raise jobs.JobRetry(
        "源条目仍在变化，静默后继续入库",
        delay_seconds=remaining,
    )


def _ready_files_from_input(value: object) -> list[_ReadyDownloadFile]:
    """解析持久化文件白名单；坏路径交后续安全还原拒绝，不静默扩权。"""
    if not isinstance(value, list):
        return []
    ready: list[_ReadyDownloadFile] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        size = item.get("size_bytes")
        if not isinstance(path, str) or not isinstance(size, int) or size < 0:
            continue
        hashes = item.get("info_hashes")
        ready.append(
            _ReadyDownloadFile(
                relative_path=path,
                size_bytes=size,
                info_hashes=tuple(
                    sorted(
                        {
                            str(info_hash).lower()
                            for info_hash in hashes
                            if isinstance(info_hash, str) and info_hash
                        }
                    )
                )
                if isinstance(hashes, list)
                else (),
            )
        )
    return ready


async def _execute_ingest_job(
    context: jobs.JobContext, input_data: dict[str, object]
) -> dict[str, object]:
    """监听入库 Job：重新核对磁盘现场后执行，领域台账作为幂等检查点。

    Job 输入表达“处理这个监听条目”的稳定意图，而不是把一次目录快照当作
    永久真相。更新期间下载目录发生变化时会重新快照；复制原语另用源大小与
    mtime 边车保护续传，绝不把两个文件版本拼接在一起。
    """
    rule_id = int(input_data["rule_id"])
    entry = Path(str(input_data["entry_path"]))
    stored_hashes = [
        str(value).lower()
        for value in input_data.get("info_hashes", [])
        if isinstance(value, str) and value
    ]
    file_scoped = "ready_files" in input_data
    ready_files = _ready_files_from_input(input_data.get("ready_files"))
    if file_scoped and not ready_files:
        raise jobs.JobFailed(
            "文件级入库白名单无效，已停止以避免扩大处理范围",
            code="INGEST_READY_FILES_INVALID",
        )
    consume_hashes = [
        str(value).lower()
        for value in input_data.get("consume_info_hashes", [])
        if isinstance(value, str) and value
    ]
    db = get_database()
    async with db.session() as session:
        pair = (
            await session.execute(
                select(ImportWatch, Library)
                .outerjoin(Library, ImportWatch.library_id == Library.id)  # type: ignore[arg-type]
                .where(ImportWatch.id == rule_id)
            )
        ).first()
    if pair is None:
        return {"message": "监听导入规则已删除，本次作业已停止", "rule_id": rule_id}
    rule, library = pair
    if not _is_entry_of(str(entry), rule.source_path):
        raise jobs.JobFailed(
            "监听条目已不属于原规则，已停止处理",
            code="INGEST_ENTRY_OUTSIDE_RULE",
            details={"rule_id": rule_id},
        )
    if not entry.exists():
        return {
            "message": "源条目已被移除，无需继续入库",
            "rule_id": rule_id,
            "entry_name": entry.name,
        }

    persisted_progress = await context.current_progress()
    persisted_details = dict(persisted_progress.get("details") or {})
    await context.update_progress(
        mode="indeterminate",
        phase="preparing",
        message=f"正在核对「{entry.name}」的下载完成状态",
        phase_index=1,
        phase_count=4,
        details={**persisted_details, "rule_id": rule_id, "entry_name": entry.name},
    )
    briefs = await _downloader_briefs()
    if briefs is None:
        raise jobs.JobRetry(
            "下载器当前不可达，无法确认条目已经完整；恢复连接后自动继续",
            delay_seconds=DEFERRED_POLL_SECONDS,
        )
    matches = _match_briefs(entry.name, briefs)
    verdict = _torrent_verdict(matches)
    if verdict is None and stored_hashes:
        raise jobs.JobRetry(
            "下载器暂未返回该受管任务，等待状态同步后自动继续",
            delay_seconds=DEFERRED_POLL_SECONDS,
        )
    if verdict is None:
        async with db.session() as session:
            if await _has_managed_download_claim(session, entry):
                raise jobs.JobRetry(
                    "下载器暂未返回该受管任务，等待状态同步后自动继续",
                    delay_seconds=DEFERRED_POLL_SECONDS,
                )
    if file_scoped and verdict == "downloading":
        current_batch = await _completed_file_batch(entry, matches)
        current_ready = {(ready.relative_path, ready.size_bytes) for ready in current_batch.files}
        if any(
            (ready.relative_path, ready.size_bytes) not in current_ready for ready in ready_files
        ):
            raise jobs.JobRetry(
                "已完成文件的下载器证据发生变化，稍后重新核对",
                delay_seconds=DEFERRED_POLL_SECONDS,
            )
    elif verdict == "downloading":
        raise jobs.JobRetry(
            "下载器仍在写入该条目，稍后继续检查",
            delay_seconds=DEFERRED_POLL_SECONDS,
        )
    try:
        snap = (
            await asyncio.to_thread(_snapshot_ready_files, entry, ready_files)
            if file_scoped
            else await asyncio.to_thread(_snapshot, entry)
        )
    except IngestSourceChanged as exc:
        raise jobs.JobRetry(str(exc), delay_seconds=30) from exc
    if not file_scoped and snap.has_marker:
        raise jobs.JobRetry(
            "条目仍有下载中标记，稍后继续检查",
            delay_seconds=DEFERRED_POLL_SECONDS,
        )
    await _ensure_job_snapshot_stable(
        context,
        snap,
        str(input_data.get("detected_fingerprint") or ""),
        rule_id=rule_id,
        entry_name=entry.name,
    )
    current_hashes = (
        [] if file_scoped else [brief.info_hash.lower() for brief in matches if brief.info_hash]
    )
    matched_hashes = sorted(set(stored_hashes) | set(current_hashes))

    async with db.session() as session:
        record = (
            await session.execute(select(IngestEntry).where(IngestEntry.entry_path == str(entry)))
        ).scalar_one_or_none()
        if record is not None and record.status == IngestStatus.IGNORED:
            await _attach_ingest_entry_resource(context, record)
            return {
                "message": "条目已被忽略，不再自动入库",
                "entry_id": record.id,
                "entry_name": entry.name,
            }
        if record is not None and record.fingerprint == snap.fingerprint:
            await _attach_ingest_entry_resource(context, record)
            # 中转记录的补偿限制见 _parser_gap_auto_retry；被限制的记录在此
            # 照旧短路/挂起，等用户改名或恢复时以新指纹走完整重跑
            parser_retry = _parser_gap_auto_retry(rule, record)
            disc_retry = bool(
                snap.has_disc
                and record.status == IngestStatus.SKIPPED
                and "原盘目录（BDMV/VIDEO_TS）暂不支持自动入库"
                in (record.message or "")
            )
            if (
                record.status in {IngestStatus.IMPORTED, IngestStatus.SKIPPED}
                and not parser_retry
                and not disc_retry
            ):
                return {
                    "message": record.message or "监听条目已处理",
                    "entry_id": record.id,
                    "status": record.status,
                }
            if (
                record.status == IngestStatus.PENDING
                and record.claimed_tmdb_id is None
                and not parser_retry
            ):
                parser_gap = _has_parser_gap(record)
                raise jobs.JobBlocked(
                    record.message or "无法自动识别该条目，请到监听导入清单认领",
                    code="INGEST_EPISODE_PARSE_REQUIRED"
                    if parser_gap
                    else "INGEST_IDENTITY_REQUIRED",
                    actions=[
                        {
                            "type": "open_settings",
                            "label": "查看监听导入" if parser_gap else "去监听导入认领",
                            "target": "import-watch",
                        }
                    ],
                    details={"entry_id": record.id, "rule_id": rule_id},
                )
        try:
            record = await _ingest_entry(
                session,
                rule,
                library,
                Path(rule.source_path),
                entry,
                snap,
                record,
                matched_hashes,
                consume_hashes if file_scoped else None,
                job_context=context,
            )
        except IngestSourceChanged as exc:
            raise jobs.JobRetry(str(exc), delay_seconds=30) from exc
        await _attach_ingest_entry_resource(context, record)
        await refresh_source_notice(session, rule.source_path)

    if record.status == IngestStatus.PENDING:
        parser_gap = _has_parser_gap(record)
        raise jobs.JobBlocked(
            record.message or "监听条目需要人工确认",
            code="INGEST_EPISODE_PARSE_REQUIRED" if parser_gap else "INGEST_IDENTITY_REQUIRED",
            actions=[
                {
                    "type": "open_settings",
                    "label": "查看监听导入" if parser_gap else "去监听导入认领",
                    "target": "import-watch",
                }
            ],
            details={"entry_id": record.id, "rule_id": rule_id},
        )
    if record.status == IngestStatus.FAILED:
        raise jobs.JobRetry(
            record.message or "监听入库暂未完成",
            delay_seconds=FAILED_RETRY_SECONDS,
            actions=[
                {
                    "type": "open_settings",
                    "label": "查看监听导入",
                    "target": "import-watch",
                }
            ],
        )
    return {
        "message": record.message or "监听条目已完成入库",
        "entry_id": record.id,
        "entry_name": entry.name,
        "status": record.status,
        "imported_count": record.imported_count,
        "media_item_id": record.media_item_id,
    }


@jobs.register_job_handler("library.ingest")
async def _run_ingest_job(
    context: jobs.JobContext, input_data: dict[str, object]
) -> dict[str, object]:
    """执行持久化入库；分批作业结束后继续轮询同目录剩余下载。"""
    entry = Path(str(input_data["entry_path"]))
    try:
        return await _execute_ingest_job(context, input_data)
    finally:
        if bool(input_data.get("keep_deferred")) and entry.exists():
            _deferred[str(entry)] = time.monotonic()


# ---------------------------------------------------------------------------
# 台账清单与人工拍板（routes/import_watch 的实现层）
# ---------------------------------------------------------------------------


def _is_entry_of(entry_path: str, source_path: str) -> bool:
    """条目是否属于某源目录（只认顶层直系子项，防嵌套源目录串数）。"""
    return str(Path(entry_path).parent) == source_path.rstrip("/")


class LedgerStats(NamedTuple):
    """一条规则的台账汇总（规则卡片摘要行的数据源）。"""

    counts: dict[str, int]
    """状态 → 条目数（imported/pending/failed/skipped/ignored）。"""
    imported_files: int
    """已入库条目累计入库的文件数。"""
    imported_works: int
    """已入库条目覆盖的作品数（按 media_item_id 去重，同剧多版本算一部）。"""


async def entry_stats(session, rules: list[ImportWatch]) -> dict[int, LedgerStats]:
    """各规则的台账汇总：状态 → 条目数，外加已入库的作品数与文件数。

    条目 = 源目录顶层项（一次下载任务的产物），这是台账的记账单位，但不是
    用户认知的单位：一部剧的 S01、S02，以及同一季的多个版本（不同发布组、
    DV 与 HDR 各一个种子）都是独立条目，却同属一部作品。因此「已入库」报
    作品数（media_item_id 去重）——用户问"入库了几部剧"要的就是这个数；
    条目数与它不同时，前端补注文件数说清实际规模。
    """
    rows = (
        await session.execute(
            select(
                IngestEntry.entry_path,
                IngestEntry.status,
                IngestEntry.imported_count,
                IngestEntry.media_item_id,
            )
        )
    ).all()
    stats: dict[int, LedgerStats] = {}
    for rule in rules:
        assert rule.id is not None
        counts = {s.value: 0 for s in IngestStatus}
        imported_files = 0
        works: set[int] = set()
        # 老台账（本次升级前入库）没有身份，去重无从下手：这些条目按条目数
        # 计入，宁可少合并也不能凭标题猜——回填见迁移脚本
        legacy = 0
        for path, status, imported_count, media_item_id in rows:
            if not _is_entry_of(path, rule.source_path) or status not in counts:
                continue
            counts[status] += 1
            if status == IngestStatus.IMPORTED:
                imported_files += imported_count or 0
                if media_item_id is None:
                    legacy += 1
                else:
                    works.add(media_item_id)
        stats[rule.id] = LedgerStats(
            counts=counts,
            imported_files=imported_files,
            imported_works=len(works) + legacy,
        )
    return stats


async def list_entries(session, rule: ImportWatch, status: str | None = None) -> list[IngestEntry]:
    """一条规则的台账清单（可按状态过滤），最近处理的在前。"""
    prefix = rule.source_path.rstrip("/") + "/"
    rows = (
        (
            await session.execute(
                select(IngestEntry)
                .where(IngestEntry.entry_path.startswith(prefix))  # type: ignore[attr-defined]
                .order_by(IngestEntry.updated_at.desc())  # type: ignore[attr-defined]
            )
        )
        .scalars()
        .all()
    )
    rows = [r for r in rows if _is_entry_of(r.entry_path, rule.source_path)]
    if status:
        rows = [r for r in rows if r.status == status]
    return rows


async def ignore_entry(session, entry_id: int) -> IngestEntry:
    """人工忽略：永久跳过（指纹变化也不复活），清单中可恢复。"""
    from movieclaw_api.exceptions import BadRequestException, NotFoundException

    record = await session.get(IngestEntry, entry_id)
    if record is None:
        raise NotFoundException(f"监听导入台账记录不存在：id={entry_id}")
    if record.status == IngestStatus.IMPORTED:
        raise BadRequestException("已入库的条目不需要忽略")
    record.status = IngestStatus.IGNORED
    record.message = "已手动忽略；如需处理可在清单中恢复"
    record.updated_at = utcnow()
    await session.commit()
    await _cancel_active_entry_jobs(
        session,
        entry_id=entry_id,
        requested_by="监听导入清单",
        reason="条目已在监听导入清单中手动忽略，任务随之取消；如需处理可在清单中恢复",
    )
    await refresh_source_notice(session, str(Path(record.entry_path).parent))
    return record


async def _cancel_active_entry_jobs(
    session,
    *,
    entry_id: int | None = None,
    requested_by: str,
    reason: str,
) -> int:
    """取消条目关联的全部活跃作业；``entry_id=None`` 时对账所有已忽略条目。

    同一条目可能因增量下载、重试或升级补偿留下多个作业。只取 ``latest`` 会
    让较早的 blocked 作业永远留在任务中心。这里按资源关系一次找全，但不改
    succeeded/failed/cancelled 等终态历史；重复运行时查不到活跃行，天然幂等。
    """
    statement = select(Job).join(JobResource, JobResource.job_id == Job.id)
    statement = statement.where(
        JobResource.resource_type == "ingest_entry",
        # cancelling 已经收到过取消请求；重复对账不再追加同义事件。
        Job.status.in_(
            [status.value for status in ACTIVE_JOB_STATUSES if status is not JobStatus.CANCELLING]
        ),
    )
    if entry_id is None:
        statement = statement.join(
            IngestEntry,
            cast(IngestEntry.id, String) == JobResource.resource_id,
        ).where(IngestEntry.status == IngestStatus.IGNORED)
    else:
        statement = statement.where(JobResource.resource_id == str(entry_id))

    active_jobs = list((await session.execute(statement)).scalars().unique())
    cancelled = 0
    for active in active_jobs:
        _, changed = await jobs.request_cancel(
            session,
            active.id,
            requested_by=requested_by,
            reason=reason,
        )
        cancelled += int(changed)
    return cancelled


async def reconcile_ignored_entry_jobs() -> int:
    """启动时收口旧版遗留作业；失败不阻断服务，下次启动继续幂等重试。"""
    try:
        async with get_database().session() as session:
            cancelled = await _cancel_active_entry_jobs(
                session,
                requested_by="system:ignored-ingest-reconcile",
                reason="监听导入条目已被忽略，系统在启动对账时自动收口遗留任务",
            )
    except Exception:  # noqa: BLE001 -- 历史清理失败不能让整个服务拒绝启动
        logger.warning("已忽略条目的遗留作业对账失败（不影响启动，下次启动将重试）", exc_info=True)
        return 0
    if cancelled:
        logger.info("启动对账已收口 %d 个已忽略条目的遗留作业", cancelled)
    return cancelled


async def reconcile_disc_batch_jobs() -> int:
    """启动时收口旧版把原盘流分段当视频建立的活跃作业。

    只依据作业已经持久化的文件白名单判定，不扫描 NAS 目录；普通电影、剧集
    批次和无关终态作业都不受影响。升级后这一步会同时清掉任务中心里的历史
    红项与监听页残留的 ``pending`` 台账；新的原盘下载则由
    ``_completed_file_batch`` 在建作业前直接拦住。
    """
    try:
        async with get_database().session() as session:
            active_statuses = [
                status.value for status in ACTIVE_JOB_STATUSES if status is not JobStatus.CANCELLING
            ]
            rows = list(
                (
                    await session.execute(
                        select(Job).where(
                            Job.job_type == "library.ingest",
                            or_(
                                Job.status.in_(active_statuses),
                                Job.cancel_requested_by == "system:disc-batch-reconcile",
                            ),
                        )
                    )
                ).scalars()
            )
            cancelled = 0
            disc_job_ids: set[str] = set()
            for job in rows:
                ready_files = (job.input_data or {}).get("ready_files") or ()
                if not any(
                    isinstance(ready, dict)
                    and isinstance(ready.get("path"), str)
                    and _disc_relative_path(ready["path"])
                    for ready in ready_files
                ):
                    continue
                disc_job_ids.add(job.id)
                if job.status not in active_statuses:
                    continue
                _, changed = await jobs.request_cancel(
                    session,
                    job.id,
                    requested_by="system:disc-batch-reconcile",
                    reason="原盘流分段不是独立视频，本批次由升级后的入库逻辑自动收口",
                )
                cancelled += int(changed)

            # v0.18.0 的 Job 与监听台账是两套持久化状态。只取消 Job 会让前端
            # 继续显示旧的“已取最大文件、忽略其余 N 个视频”。资源关系能精确
            # 找回这些 Job 对应的台账，不需要扫描 NAS，也不会碰普通 pending。
            reconciled_entries = 0
            if disc_job_ids:
                resource_ids = (
                    await session.execute(
                        select(JobResource.resource_id).where(
                            JobResource.job_id.in_(disc_job_ids),  # type: ignore[union-attr]
                            JobResource.resource_type == "ingest_entry",
                        )
                    )
                ).scalars()
                entry_ids = {
                    int(resource_id) for resource_id in resource_ids if str(resource_id).isdigit()
                }
                if entry_ids:
                    records = list(
                        (
                            await session.execute(
                                select(IngestEntry).where(
                                    IngestEntry.id.in_(entry_ids),  # type: ignore[union-attr]
                                    IngestEntry.status == IngestStatus.PENDING,
                                )
                            )
                        ).scalars()
                    )
                    for record in records:
                        record.status = IngestStatus.SKIPPED
                        record.message = (
                            "旧版原盘流分段的待处理记录已自动收口；"
                            "原盘目录（BDMV/VIDEO_TS）暂不支持自动入库，请手动整理"
                        )
                        record.updated_at = utcnow()
                    reconciled_entries = len(records)
                    if records:
                        await session.commit()
    except Exception:  # noqa: BLE001 -- 历史清理失败不能让整个服务拒绝启动
        logger.warning(
            "原盘分批入库的遗留作业对账失败（不影响启动，下次启动将重试）", exc_info=True
        )
        return 0
    if cancelled:
        logger.info("启动对账已收口 %d 个原盘分批入库遗留作业", cancelled)
    if reconciled_entries:
        logger.info("启动对账已收口 %d 个原盘分批监听台账", reconciled_entries)
    return cancelled


async def restore_entry(session, entry_id: int) -> IngestEntry:
    """恢复处理：置空指纹让下轮巡检必然重处理，并立即请求巡检。

    适用忽略行（存量基线/手动忽略的反悔）与待处理行（TMDB 补录条目后
    的主动重试——不用改名也能再走一遍识别链）。
    """
    from movieclaw_api.exceptions import BadRequestException, NotFoundException

    record = await session.get(IngestEntry, entry_id)
    if record is None:
        raise NotFoundException(f"监听导入台账记录不存在：id={entry_id}")
    if record.status == IngestStatus.IMPORTED:
        raise BadRequestException("该条目已入库，无需恢复")
    record.status = IngestStatus.PENDING
    record.fingerprint = ""  # 与任何真实指纹都不同 → 下轮巡检必然重处理
    record.message = "已恢复，等待处理（需先通过下载完成检测）"
    record.updated_at = utcnow()
    await session.commit()
    source = str(Path(record.entry_path).parent)
    await refresh_source_notice(session, source)
    active = await jobs.latest_job_for_resource(session, "ingest_entry", entry_id)
    if active is not None and active.status == JobStatus.BLOCKED:
        await jobs.retry_job(
            session,
            active,
            actor_kind="user",
            actor_name="监听导入清单",
            origin="web",
        )
    else:
        request_sweep(source)
    return record


async def claim_entry(
    session,
    entry_id: int,
    tmdb_id: int,
    *,
    execute_inline: bool = False,
) -> IngestEntry:
    """人工认领：把条目钉到指定 TMDB 身份并恢复同一个后台入库作业。

    用户拍板是最高权威——跳过工单认领与识别链；kind 先验仍取自规则
    （指定库=库类型，自动路由/自定义目录=规则声明）。生产路径只保存认领
    事实并原地解除 blocked Job，稳定 job id、事件时间线与复制断点都不变；
    ``execute_inline`` 仅供领域单测复用处理原语。
    """
    from movieclaw_api.exceptions import BadRequestException, NotFoundException

    record = await session.get(IngestEntry, entry_id)
    if record is None:
        raise NotFoundException(f"监听导入台账记录不存在：id={entry_id}")
    if record.status == IngestStatus.IMPORTED:
        raise BadRequestException("该条目已入库，无需认领")
    entry = Path(record.entry_path)
    source = str(entry.parent)
    pair = (
        await session.execute(
            select(ImportWatch, Library)
            .outerjoin(Library, ImportWatch.library_id == Library.id)  # type: ignore[arg-type]
            .where(ImportWatch.source_path == source)
        )
    ).first()
    if pair is None:
        raise BadRequestException(f"该条目所属的监听导入规则已删除（{source}），无法认领")
    rule, library = pair
    if not entry.exists():
        raise BadRequestException("条目已不在源目录（可能已被删除），无法认领")
    snap = await asyncio.to_thread(_snapshot, entry)
    briefs = await _downloader_briefs()
    matches = _match_briefs(entry.name, briefs)
    if snap.has_marker or _torrent_verdict(matches) == "downloading":
        raise BadRequestException("条目似乎还在下载中（存在未完成标记文件），请等下载完成再认领")
    kind = MediaKind(library.kind if library is not None else rule.kind)
    item = await MediaLibraryService(session, get_tmdb_client()).ensure_media_item(kind, tmdb_id)
    # 认领身份先持久化再处理：用户拍板是最高权威。本轮处理若因环境故障
    # 失败（探测/搬运/TMDB 网络 → failed），后续自动退避重试会凭台账里的
    # 这颗钉子还原身份（见 _ingest_entry），不会回退到重新识别落回
    # pending、更不需要用户再认领一次
    record.claimed_tmdb_id = tmdb_id
    record.claimed_kind = kind.value
    record.status = IngestStatus.PENDING
    record.message = f"已认领为《{item.title}》，等待后台整理入库"
    record.updated_at = utcnow()
    await session.commit()
    if not execute_inline:
        created = await enqueue_ingest_job(
            session,
            rule,
            entry,
            snap,
            matched_hashes=[brief.info_hash for brief in matches if brief.info_hash],
            record=record,
            origin="web",
        )
        if created.job.status == JobStatus.BLOCKED:
            await jobs.retry_job(
                session,
                created.job,
                actor_kind="user",
                actor_name="监听导入清单",
                origin="web",
            )
        await session.refresh(record)
        await refresh_source_notice(session, source)
        return record
    async with _sweep_lock:
        await _ingest_entry(
            session,
            rule,
            library,
            entry.parent,
            entry,
            snap,
            record,
            matched_hashes=None,
            forced_item=item,
        )
    await session.refresh(record)
    await refresh_source_notice(session, source)
    return record


def request_sweep(source_path: str) -> None:
    """请求尽快巡检一个源目录：监听器在时走事件通道（秒级），否则等兜底巡检。"""
    watcher = get_ingest_watcher()
    if watcher is not None:
        watcher.poke(source_path)


# ---------------------------------------------------------------------------
# 事件驱动：监听目录的 watchdog 观察者（进程级单例，模式同 library_watch）
# ---------------------------------------------------------------------------

# 事件去抖：首个事件后等安静 3 秒再巡检；持续有事件时最长 30 秒必巡检一次
_EVENT_QUIET_SECONDS = 3.0
_EVENT_MAX_WAIT_SECONDS = 30.0

# 观察者线程侧的同目录合并窗口：下载写入的 modified 风暴每秒可达上千条，
# 逐条唤醒事件循环本身就是开销。窗口必须远小于 _EVENT_QUIET_SECONDS，
# 否则消费者会把"事件被合并掉"误判成"已经安静"
_EVENT_COALESCE_SECONDS = 0.5


def _is_ingest_relevant(event) -> bool:  # noqa: ANN001
    """该文件事件是否值得触发巡检（观察者线程调用，只做纯判定）。

    只读事件（打开 / 未写关闭）忽略：做种上传、入库前的 ffprobe 终检、
    外部工具浏览都会**读**监听目录里的文件，读不改变条目指纹与完成结论，
    触发巡检只是无谓的磁盘扫描和下载器查询（库根监听在同一问题上吃过
    无限自激的亏，见 library/watch.py 的 _is_relevant_event）。其余事件
    （增删改/移动，含目录与标记文件）都放行——完成检测靠条目全树指纹，
    任何写动作都可能改变结论，不做扩展名过滤。
    """
    from watchdog.events import EVENT_TYPE_CLOSED_NO_WRITE, EVENT_TYPE_OPENED

    return event.event_type not in (EVENT_TYPE_OPENED, EVENT_TYPE_CLOSED_NO_WRITE)


class IngestWatcher:
    """下载监听目录的文件事件观察者。

    事件驱动是本功能的既定形态（不做常驻轮询）：下载写入持续产生 fs
    事件，事件停止即静默的开端；没有下载活动时零开销，NAS 磁盘可以休眠。
    静默窗口的"到点检查"没有事件会叫醒——每轮巡检后若仍有条目在等静默
    或在挂起表里等下载器完成，挂一个一次性自检任务（前者按最近到期时间，
    后者按固定轮询节奏且醒来先通过 API 核对文件进度），全部落定后归零。
    """

    def __init__(self) -> None:
        self._observer = None
        # 源目录 → (handler, ObservedWatch)：差量重建的台账。
        # 仅在持有 _refresh_lock 的工作线程里读写
        self._entries: dict[str, tuple[object, object]] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._consumer: asyncio.Task | None = None
        self._catchup: asyncio.Task | None = None
        # 初始建 watch 的后台任务：不阻塞应用启动（同 library_watch，issue #162）
        self._startup: asyncio.Task | None = None
        self._available = True
        self._closed = False
        # 当前实际在监听的源目录集合：兜底巡检据此跳过它们（事件路径已负责）
        self._watched: set[str] = set()
        # 同目录合并的最近投递时刻（观察者的单一 dispatch 线程读写，无需锁）
        self._recent_keys: dict[str, float] = {}
        # 静默到点自检任务：每个源目录至多挂一个
        self._rechecks: dict[str, asyncio.Task] = {}
        # 串行化重建：并发的规则编辑各自触发 refresh，并发重建会互踩 _observer
        self._refresh_lock = asyncio.Lock()

    def watched_keys(self) -> frozenset[str]:
        """实际在监听的源目录集合——兜底巡检只扫不在此列的目录。"""
        return frozenset(self._watched)

    # -- 生命周期 ----------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._consumer = asyncio.create_task(self._consume())
        # 初始建 watch 放后台：网络挂载上的递归建 watch 可达分钟级，不能
        # 拖住 FastAPI startup（entrypoint 有就绪超时，issue #162）
        self._startup = asyncio.create_task(self._startup_refresh())

    async def _startup_refresh(self) -> None:
        # refresh_watches 会把初次纳入监听的目录逐个投队列补扫（覆盖停机
        # 期间完成的下载），不做独立的全量扫描
        try:
            await self.refresh_watches()
        except Exception:  # noqa: BLE001 -- 后台任务无人 await，异常必须就地落日志
            logger.exception("监听导入初始化失败（每小时兜底巡检仍会发现完成的下载）")
            return
        if not self._available:
            # watchdog 缺失：事件路径不存在，开机全量补扫一次顶上
            self._catchup = asyncio.create_task(ingest_tick())

    async def stop(self) -> None:
        self._closed = True
        for task in (self._consumer, self._catchup, self._startup, *self._rechecks.values()):
            if task is not None:
                task.cancel()
        self._consumer = None
        self._catchup = None
        self._startup = None
        self._rechecks.clear()
        # 与后台重建互斥：重建正在工作线程里装配新观察者时直接停旧引用，
        # 会漏掉刚装好的那个（关停竞态）；拿到锁再停则必停到最终的观察者
        async with self._refresh_lock:
            self._stop_observer()

    def _stop_observer(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        self._entries.clear()
        self._watched = set()

    async def refresh_watches(self) -> None:
        """按当前监听导入规则**差量**调整监听（规则增删改后调用）。

        磁盘部分（recursive 建 watch、网络挂载上的 is_dir、拆 watch 的
        join）都是阻塞调用，放线程池执行，不冻住事件循环（同 library_watch）。
        差量意味着只有新增/移除/失效的源目录付建 watch 的成本；每小时的
        兜底巡检借同一入口恢复失效监听，未变的目录零开销。
        """
        if not self._available or self._closed:
            return
        try:
            import watchdog  # noqa: F401 -- 仅探测可用性，实际使用在 _apply_watches
        except ImportError:
            self._available = False
            logger.warning("未安装 watchdog，监听导入不实时——完成的下载靠每小时兜底巡检发现")
            return

        rules = await _load_rules()
        source_paths = [rule.source_path for rule, _library in rules]
        async with self._refresh_lock:
            newly_watched = await asyncio.to_thread(self._apply_watches, source_paths)
        # 初次纳入监听的目录补扫一次：监听建立之前完成的下载（停机期间/
        # 目录刚就绪/刚加进配置）不会再产生事件，只有这一次主动扫能接住；
        # 之后全靠事件驱动，不再主动扫它。队列操作回到事件循环线程再做
        for key in sorted(newly_watched):
            self._queue.put_nowait(key)

    def _apply_watches(self, source_paths: list[str]) -> set[str]:
        """（工作线程）差量调整监听；返回**初次**纳入监听的目录。

        失效后恢复的目录不算"初次"——原目录早已做过基线补扫，重复补扫
        会把存量当新增重处理。失活检测同 library_watch：读观察者内部表，
        拿不到就当作存活（宁可漏检由兜底巡检接住，不能误拆好监听）。
        """
        from watchdog.observers import Observer

        if self._closed:
            return set()
        if self._observer is None:
            observer = Observer()
            observer.daemon = True
            observer.start()
            self._observer = observer
        observer = self._observer

        # key 必须保持 rule.source_path 原样：消费侧回查规则、兜底巡检的
        # watched_keys 对比都按原字符串匹配，归一化会让两头失配
        desired = dict.fromkeys(source_paths)  # 去重且保序
        previously_watched = set(self._entries)
        for key, (handler, watch) in list(self._entries.items()):
            if key in desired and self._watch_alive(observer, watch):
                continue
            self._entries.pop(key, None)
            # KeyError 一律吞掉：watch 可能已随 emitter 死亡被 watchdog 清理
            with contextlib.suppress(KeyError):
                observer.remove_handler_for_watch(handler, watch)
            with contextlib.suppress(KeyError):
                observer.unschedule(watch)
        added = 0
        for source_path in desired:
            if self._closed:
                break
            if source_path in self._entries:
                continue
            if not os.path.isdir(source_path):
                continue  # 目录未就绪：兜底巡检持续兜着，不告警刷屏
            handler = self._make_handler(source_path)
            try:
                # recursive 建 watch 在本线程同步走完整棵树（watchdog 在
                # 调用线程里跑 on_thread_start），大树/网络挂载慢在这里；
                # 超 inotify watch 上限（ENOSPC）也在这里抛，按目录隔离
                watch = observer.schedule(handler, source_path, recursive=True)
            except OSError as exc:
                logger.warning("监听源目录失败（%s）：%s", source_path, exc)
                continue
            self._entries[source_path] = (handler, watch)
            added += 1
        self._watched = set(self._entries)
        if added:
            logger.info("监听导入已更新：新增 %d 个，共监听 %d 个源目录", added, len(self._entries))
        return self._watched - previously_watched

    @staticmethod
    def _watch_alive(observer, watch) -> bool:  # noqa: ANN001
        try:
            emitter = observer._emitter_for_watch.get(watch)  # noqa: SLF001
        except AttributeError:  # watchdog 内部结构变化：当作存活
            return True
        return emitter is not None and emitter.is_alive()

    def _make_handler(self, source_path: str):  # noqa: ANN202
        from watchdog.events import FileSystemEventHandler

        watcher = self

        class _Handler(FileSystemEventHandler):
            """事件回调（观察者 dispatch 线程）：判定 + 合并 + 投递，不做业务。"""

            def on_any_event(self, event) -> None:  # noqa: ANN001
                if _is_ingest_relevant(event) and watcher._should_enqueue(source_path):
                    watcher._enqueue_threadsafe(source_path)

        return _Handler()

    # -- 事件通道 ----------------------------------------------------------

    def poke(self, source_path: str) -> None:
        """外部请求巡检某源目录（人工恢复处理后的立即重看）。事件循环线程调用。"""
        self._queue.put_nowait(source_path)

    def _should_enqueue(self, key: str) -> bool:
        """（观察者 dispatch 线程）同目录合并：短窗口内不重复跨线程投递。

        所有 handler 都在观察者唯一的 dispatch 线程回调，无需加锁。
        """
        now = time.monotonic()
        last = self._recent_keys.get(key)
        if last is not None and now - last < _EVENT_COALESCE_SECONDS:
            return False
        if len(self._recent_keys) > 512:
            self._recent_keys = {
                k: t for k, t in self._recent_keys.items() if now - t < _EVENT_COALESCE_SECONDS
            }
        self._recent_keys[key] = now
        return True

    def _enqueue_threadsafe(self, key: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._queue.put_nowait, key)

    async def _consume(self) -> None:
        """去抖消费：首事件后等安静窗口，汇总本批涉及的监听目录逐个巡检。"""
        while True:
            first = await self._queue.get()
            pending = {first}
            deadline = asyncio.get_running_loop().time() + _EVENT_MAX_WAIT_SECONDS
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                timeout = min(_EVENT_QUIET_SECONDS, max(remaining, 0))
                try:
                    pending.add(await asyncio.wait_for(self._queue.get(), timeout))
                except TimeoutError:
                    break  # 安静窗口达成
                if asyncio.get_running_loop().time() >= deadline:
                    break  # 兜底：持续有事件也要巡检
            for source_path in sorted(pending):
                try:
                    await self._sweep(source_path)
                except Exception:  # noqa: BLE001 -- 监听消费绝不崩
                    logger.exception("事件触发的监听目录巡检失败：%s", source_path)

    async def _sweep(self, source_path: str) -> None:
        db = get_database()
        async with db.session() as session:
            result = await session.execute(
                select(ImportWatch, Library)
                .outerjoin(Library, ImportWatch.library_id == Library.id)  # type: ignore[arg-type]
                .where(ImportWatch.source_path == source_path)
            )
            pair = result.first()
        if pair is None:
            return  # 规则已删除（refresh_watches 稍后会拆掉监听）
        rule, library = pair
        await _sweep_dir(rule, library)  # 收尾会重挂自检（_sweep_dir 统一负责）

    def _arm_recheck(self, source_path: str) -> None:
        """仍有条目在等静默窗口、等下载器状态翻转或等失败退避时，挂一次性自检。

        三个唤醒源：静默窗口按最近到期时间挂；挂起条目按固定节奏轮询——
        自检醒来先通过 API 核对状态（见 _recheck_later），仅在出现新的单文件
        完成候选时 stat 对应文件，不扫描整棵目录；
        **失败条目按退避到期时间挂**——失败结论落账时静默表/挂起表都已弹出
        该条目、fs 事件也不会再来（下载早已结束），没有这第三个源就没人
        回来重试。取三者中最近的到期时间挂一次即可（自检收尾会重挂）。
        """
        prefix = source_path.rstrip("/") + "/"
        now = time.monotonic()
        quiet = [since for path, (_fp, since) in _stability.items() if path.startswith(prefix)]
        failed = [since for path, since in _failed_retry.items() if path.startswith(prefix)]
        has_deferred = any(path.startswith(prefix) for path in _deferred)
        if not quiet and not has_deferred and not failed:
            return
        existing = self._rechecks.get(source_path)
        if existing is not None and not existing.done():
            return
        candidates: list[float] = []
        if quiet:
            delay = min(quiet) + QUIET_SECONDS - now + 1.0
            candidates.append(max(5.0, min(delay, QUIET_SECONDS + 5.0)))
        if has_deferred:
            candidates.append(DEFERRED_POLL_SECONDS)
        if failed:
            candidates.append(max(5.0, min(failed) + FAILED_RETRY_SECONDS - now + 1.0))
        self._rechecks[source_path] = asyncio.create_task(
            self._recheck_later(source_path, min(candidates))
        )

    async def _recheck_later(self, key: str, delay: float) -> None:
        await asyncio.sleep(delay)
        prefix = key.rstrip("/") + "/"
        now = time.monotonic()
        # 失败条目退避到点：直接巡检重试（不走下面的纯 API 核对——失败与
        # 下载器状态无关）
        failed_due = any(
            now - since >= FAILED_RETRY_SECONDS
            for path, since in _failed_retry.items()
            if path.startswith(prefix)
        )
        if not failed_due and not any(path.startswith(prefix) for path in _stability):
            # 只剩挂起条目：先通过 API 核对。没有新的单文件完成证据就只
            # 重挂下一轮；有候选时也只 stat 候选，不扫描整棵目录
            try:
                flipped = await _deferred_flipped(prefix)
            except Exception:  # noqa: BLE001 -- 核对失败就去巡检，交给巡检的降级链
                logger.exception("挂起条目的下载器状态核对失败，转为巡检：%s", key)
                flipped = True
            if not flipped:
                self._rechecks.pop(key, None)
                self._arm_recheck(key)
                return
        self._queue.put_nowait(key)


_watcher: IngestWatcher | None = None


def get_ingest_watcher() -> IngestWatcher | None:
    return _watcher


async def init_ingest_watcher() -> None:
    """启动进程级监听单例（lifespan 调用）。"""
    global _watcher
    # v0.18.0 及更早版本只取消同一条目的最新作业，旧 blocked 批次会一直
    # 挂在任务中心。监听启动前做一次纯数据库对账，升级镜像即可自动修复。
    await reconcile_ignored_entry_jobs()
    # v0.18.0 的边下边入库会把 BDMV/VIDEO_TS 流分段误当独立视频并持续建
    # 作业；同样在监听启动前只读作业白名单并收口，不触碰媒体文件。
    await reconcile_disc_batch_jobs()
    _watcher = IngestWatcher()
    await _watcher.start()


async def close_ingest_watcher() -> None:
    global _watcher
    if _watcher is not None:
        await _watcher.stop()
        _watcher = None
