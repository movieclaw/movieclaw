"""存量扫描器（媒体库 L3 核心）：把库根路径下已有的文件识别并纳入台账。

识别链（docs/design/library.md M2，每个视频文件依次尝试）：
  ⓪ 类型冲突拦截——文件明显是剧集却在电影库（或 NFO 根元素与库类型相反）
     时**直接判待识别**，不进入后面任何一步：TMDB 的 movie 与 tv 是两套
     独立 id 空间，按错误类型拉档会成功拿到一部毫不相干的作品（见
     ``_kind_conflict``）；
  ① 显式身份优先——路径里的 ``[tmdbid=75956]`` 标记（Emby/Jellyfin/TMM
     的通行命名惯例，用户手写的身份声明）与 NFO 里的 tmdb id，读到即
     精确身份；但**声明不再免检**：拉到的条目与本地片名/年份严重矛盾时
     不予采信，降级走 ②（见 ``_pinned_mismatch``）；
  ② 文件名/目录名解析（enrich 复用）→ TMDB 证据验证收敛
     （标题门槛 + 年份/时长/季集数佐证，见 library_resolve 模块头注释）；
  ③ 仍无法确认 → 照样落账但 media_item_id=NULL，进"待识别"清单人工认领
     ——宁可待确认，不静默错挂。

增量语义：已识别且在位的路径直接跳过（重扫秒级）；**在位但待识别的行
每轮重走识别链**（行原地更新）——「重新扫描」因此天然是识别重试入口，
TMDB 网络故障恢复后重扫即可自动补识别；标记过 missing 的
文件再次被发现时自动清除标记（已有身份锚原样保留，不重新识别）。扫描同时感知删除：在位根路径下、台账有
但本轮没遍历到的文件标记 missing（挂载失败的根整个跳过、不误伤）——
"扫描 = 把台账与磁盘对齐"，新增与消失一次看全。扫描绝不移动/重命名/
删除任何存量文件；missing 默认只是标记、记录保留（它是「重新下载」与
跨轮次改名归并的依据），库开了 ``auto_clear_missing`` 才在收尾把已确认
丢失的记录清出台账（只删台账、且仅限本轮可信的扫描，见
``_auto_clear_missing``）。

完整性检测（库对目录用途不做任何假设——根路径完全可以同时是下载目录，
新文件可能是一个写入中的半成品）：**新文件 mtime 距今不足静默窗口的
本轮暂缓入账**（下载器的 .!qB/.part 等未完成后缀本就不是视频扩展名，
遍历天然不可见）。写入结束后不再有事件叫醒扫描，暂缓过文件的一轮扫描
会按最近到期时间挂一次性补扫。扫描的角色是盘点不是守门，因此**不做
ffprobe 门禁**——探测失败的老文件可能只是格式怪，拦下反而让台账失真。

改名归并：磁盘上被改名/移动的文件（旧路径消失 + 新路径出现）在落账前
用"尺寸 + 时长"指纹匹配旧行，唯一命中即整行随迁——身份锚（含人工
认领）无损延续，不产生幽灵 missing 行（见 _try_relink）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from itertools import islice
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services import jobs
from movieclaw_api.services.library.bluray import (
    enrich_spec_with_clpi,
    read_clpi_languages,
    read_main_playlist,
    streams_have_clpi_metadata,
)
from movieclaw_api.services.library.layout import (
    SCAN_VIDEO_EXTS,
    STRM_EXT,
    entry_dirs,
    season_from_dir,
    trailing_index_episode,
)
from movieclaw_api.services.library.nfo import (
    NfoIdentity,
    read_entry_identity,
    read_episode_metadata,
)
from movieclaw_api.services.library.resolve import (
    LocalEvidence,
    normalize_title,
    parse_total_episodes,
    resolve_with_candidates,
)
from movieclaw_api.services.library.subtitles import discover_external_subtitles_async
from movieclaw_api.services.library.units import resolve_units
from movieclaw_api.services.media_discover import get_tmdb_client
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.media_probe import MediaSpec, probe_media
from movieclaw_api.services.task_state import TaskState
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    DownloadHint,
    FileSource,
    FileState,
    Job,
    JobResource,
    JobStatus,
    Library,
    LibraryFile,
    MediaItem,
    utcnow,
)
from movieclaw_db.models.library_file import IdentitySource, UnidentifiedCode
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_enrich import enrich
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_enrich.structure import title_candidates
from movieclaw_media.models import MediaKind
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.library_scan")

# 目录级忽略：生态辅助目录（Emby/群晖/TMM）+ Emby/Jellyfin 的 extras 目录
# 惯例 + 中文圈常见叫法。一个条目目录里的花絮/预告/片段不是独立作品，拿
# 它们的文件名去 TMDB 搜必然搜出**别的影片**（issue #107）。
#
# 两个刻意不收的名字：
# - ``specials``：Emby 对电影把它当 extras，但对剧集它是 Season 0——特别篇
#   是正片内容，忽略掉等于把用户的剧集内容吞了；
# - ``shorts`` / ``other``：Emby 官方列表里有，但它们太像用户自建的内容
#   分组目录（短片收藏、杂项），误伤的是真片子。宁可漏挡不可错杀。
_IGNORE_DIRS = {
    "@eadir",
    ".deletedbytmm",
    "sample",
    "samples",
    # Emby/Jellyfin extras 目录惯例
    "behind the scenes",
    "bonus",
    "bonus disc",
    "clips",
    "deleted scenes",
    "extra",
    "extras",
    "featurettes",
    "interviews",
    "scenes",
    "trailers",
    # 中文圈常见叫法
    "花絮",
    "幕后花絮",
    "预告片",
    "制作特辑",
    "删减片段",
    "特典",
}
# 文件名含这些标记不入库（子串匹配，历来只挡样片）
_IGNORE_MARKERS = ("sample",)

# Emby/Jellyfin 的 extras **文件名后缀**惯例：``片名-trailer.mkv``。花絮常常
# 不在子目录里、就躺在正片旁边，只靠目录名挡不住。**只认后缀不认子串**：
# 《Trailer Park Boys》这类片名自带关键词的正片绝不能被误伤
_EXTRAS_SUFFIXES = (
    "-behindthescenes",
    "-clip",
    "-deleted",
    "-deletedscene",
    "-featurette",
    "-interview",
    "-scene",
    "-short",
    "-trailer",
)
# 中文圈没有后缀惯例，花絮通常直接写进名字（「花絮01.mkv」「【花絮】…」），
# 只能子串匹配。因此只取歧义最小的两个词——正片片名里几乎不会出现它们
_EXTRAS_KEYWORDS = ("花絮", "预告片")

# 路径里的显式 tmdb id 标记（Emby/Jellyfin/TMM 通行惯例，用户手写的身份声明）：
# 「风筝 (2017) [tmdbid=75956]」「Kite [tmdb-75956]」「{tmdb-75956}」
_PATH_TMDBID = re.compile(r"[\[{]\s*tmdb(?:id)?\s*[-=]\s*(\d+)\s*[\]}]", re.IGNORECASE)

# 方括号/花括号标记组（[tmdbid=N]、[1080p] 等）：匹配"Title (Year)"惯例名
# 前先剥掉，否则尾部标记会挡住惯例识别
_BRACKET_GROUP = re.compile(r"[\[{][^\]}]*[\]}]")

# 识别器版本号：识别链（NFO 解析 / 名称解析 / TMDB 证据收敛）发生**实质**
# 变更时 +1。扫描会对「已识别、但版本落后且非人工认领」的行重走识别链复核
# （身份对账）：新旧结论一致只更新版本戳；不一致写入复核建议、由用户在
# 「身份复核」清单拍板——识别器升级的红利因此能主动惠及存量库，而不是
# 只有新文件受益。
# 版本史：1 = 特性上线前的隐式版本（存量行 resolved_version 为 NULL）；
#         2 = NFO 结构化解析（修复演员 person id / 分集 id 冒认条目身份）
#         3 = 钉死身份不再免检（类型冲突检测 + 声明与本地证据的矛盾校验）
#         4 = 原盘目录名不再被 .stem 截断（E.T.外星人）；钉死身份矛盾校验
#             增加时长轴并收紧超短标题的包含判定（3 分钟短片《4》冒认
#             神奇4侠）；目录名 Title (Year) 惯例作备选查询词
#         5 = 电影的「Title (Year)」条目目录名压过文件名（issue #107：正片
#             旁边的花絮/片段按自己的文件名搜出别的影片）
RESOLVER_VERSION = 5


def conventional_title(text: str) -> tuple[str, int] | None:
    """「Title (Year)」惯例名（Emby/TMM 整理产物）→ ``(片名, 年份)``；不匹配返回 None。

    先剥掉 ``[tmdbid=N]``/``[1080p]`` 这类方括号标记组再匹配，允许年份后
    还挂着画质等尾巴。惯例名是识别链里的**高置信来源**（理由见
    ``guess_evidence``），够格压过 NER 从脏名字里抽出来的片名。
    """
    cleaned = _BRACKET_GROUP.sub(" ", text).strip()
    matched = re.match(r"^(.+?)\s*\((\d{4})\)", cleaned)
    return (matched.group(1).strip(), int(matched.group(2))) if matched else None


def extras_marker(name: str) -> str | None:
    """文件名是否命中 extras（花絮/预告/片段）惯例；命中返回命中的那个标记。

    两套判据（见 ``_EXTRAS_SUFFIXES`` / ``_EXTRAS_KEYWORDS`` 的取舍说明）：
    Jellyfin/Emby 的 ``-trailer`` 后缀惯例按后缀匹配，中文关键词按子串匹配。
    """
    stem = Path(name).stem.lower()
    for suffix in _EXTRAS_SUFFIXES:
        if stem.endswith(suffix):
            return suffix
    for keyword in _EXTRAS_KEYWORDS:
        if keyword in stem:
            return keyword
    return None


# 身份来源的中文名（日志与提示用）
_ID_SOURCE_NAMES = {
    IdentitySource.PATH_TAG: "目录名 tmdbid 标记",
    IdentitySource.NFO: "NFO",
}
_KIND_NAMES = {MediaKind.MOVIE: "电影", MediaKind.TV: "剧集"}

# 钉死身份的年份反证阈值（年）：同一部作品在 TMDB 与文件名上的年份不会差
# 这么多；超出才有资格参与"推翻用户显式声明"的判定（见 _pinned_mismatch）
_PINNED_YEAR_TOLERANCE = 2

# 钉死身份的时长反证阈值：条目片长与文件实测时长**相差 3 倍以上且绝对差
# 超过 30 分钟**才算矛盾。同一部作品的导演剪辑/加长版差不到 2 倍，3 倍
# 起步的只会是"NFO 指向了另一部作品"（实测：3 分钟短片《4》冒认 115
# 分钟的神奇4侠）；绝对差下限挡住两部都是短片时的倍数噪声
_PINNED_RUNTIME_RATIO = 3
_PINNED_RUNTIME_GAP_SECONDS = 30 * 60

# 新文件的静默窗口：mtime 距今不足该值视为"疑似写入中"，本轮暂缓入账。
# 只看 mtime 不看大小——BT 客户端预分配全尺寸文件，大小从一开始就不变
NEW_FILE_QUIET_SECONDS = 300

# 收尾补图片资产的并发路数。与整库刷新取同一个值（media_scrape.
# _REFRESH_CONCURRENCY = 3）：瓶颈是等图床响应，3 路明显快过串行，
# 真正的下载并发另有图床闸把着，不会因为这里放宽就打爆对方
_ASSET_CONCURRENCY = 3

# TMDB 档案预取窗口：逐文件串行建档时，每部新片都要干等一次 TMDB 往返
# （国内直连常在数百毫秒），一千部片就是几分钟纯等待。这里在处理每一窗
# 文件之前，把这一窗里**身份已经写死在 NFO/路径标记里**的新条目并发拉
# 回来，随后的建档直接吃缓存。
#
# 为什么是"窗口"而不是"整库一次拉完"：一份带演职员与图片列表的档案几十
# 到几百 KB，整库预取会把几百 MB 档案压在内存里。按窗推进内存恒定，
# 且不影响停止请求的响应速度（一窗之内即可响应）。
#
# 只覆盖显式身份（NFO / [tmdbid=N]）：靠文件名解析的条目要先搜一次 TMDB
# 才知道 id，预取不了；它们照常走原来的串行链路，不受影响。
_PREFETCH_WINDOW = 16

# 介质探测预取窗口：入账链路每个新文件都要等一次 ffprobe 子进程（本地盘
# 30ms 起、网络挂载常在数百毫秒），且严格串行——新库首扫的主要等待项。
# 这里为"接下来几个要走入账链的文件"提前在线程池起探测，让探测与当前
# 文件的识别/落账重叠。窗口小而有界：不放大对存储的并发压力（机械盘/
# 网络挂载经不起大并发随机读），预取过头的浪费也至多几个子进程。
_PROBE_AHEAD_WINDOW = 4


class ScanPhase(StrEnum):
    """扫描类任务的阶段。

    分阶段是**状态诚实**的要求：一次「扫描」并不是单一动作，遍历完文件
    之后还要给新挂锚的条目补齐图片资产（几百张剧照、以分钟计）。不区分
    阶段的话，进度会长时间停在「已处理 = 总数」，用户看到的是一个撞了
    墙的进度条，无从判断是慢还是死。每个阶段有自己的分子分母与文案。
    """

    WALKING = "walking"  # 盘点根路径下的文件（分母还没定）
    INGESTING = "ingesting"  # 逐文件走识别链、写台账（分母 = 待处理文件数）
    PROBING = "probing"  # 补探缺规格的在位行（分母 = 待补探的台账行数）
    ASSETS = "assets"  # 收尾补齐图片资产（分母 = 本轮新挂锚的条目数）
    REIDENTIFYING = "reidentifying"  # 单条目重识别（与扫描共用同一把库级锁）


# 阶段的中文名：接口冲突提示与日志共用，避免"正在重新识别"被说成"正在扫描"
PHASE_LABELS: dict[ScanPhase, str] = {
    ScanPhase.WALKING: "正在盘点文件",
    ScanPhase.INGESTING: "正在扫描",
    ScanPhase.PROBING: "正在补探介质规格",
    ScanPhase.ASSETS: "正在补齐图片资产",
    ScanPhase.REIDENTIFYING: "正在重新识别条目",
}

# 可以中途叫停的阶段：重识别在改身份锚，半途而废会留下不一致的台账，
# 因此不给停止入口（它本身也只处理单个条目、很快结束）
_STOPPABLE_PHASES = {
    ScanPhase.WALKING,
    ScanPhase.INGESTING,
    ScanPhase.PROBING,
    ScanPhase.ASSETS,
}


@dataclass
class ScanState:
    """一次扫描/重识别在进程内的实时状态（前端进度环的数据源）。"""

    phase: ScanPhase
    processed: int = 0
    total: int = 0  # 0 表示分母未知（遍历阶段），前端画不确定态转圈


# 进行中的扫描类任务（TaskState：库级互斥锁 + 阶段/进度 + 停止请求 +
# 最近一次结论四合一）。状态与互斥同源是刻意的——早先"互斥用 set、进度用
# 另一个 dict"的写法必须靠人肉维持两者同生共死，任何一条提前退出的路径
# 都会让「在扫描」与「有进度」对不上，接口于是开始说谎
_scan_tasks: TaskState[ScanState] = TaskState()
# 暂缓补扫任务：每库至多一个（写入结束后没有事件叫醒扫描，靠它到点补扫）
_rescan_tasks: dict[int, asyncio.Task] = {}


@dataclass
class ScanSummary:
    """一次扫描的结论（日志与接口响应共用）。"""

    library_id: int
    scanned: int = 0  # 本轮处理的新文件数
    identified: int = 0  # 成功挂上身份锚
    unidentified: int = 0  # 落账但待识别
    kind_mismatched: int = 0  # 其中因"类型与本库不符"待识别的（库类型可能选错了）
    relinked: int = 0  # 改名归并：旧台账行迁到新路径（身份延续，不算新入账）
    root_relinked: int = 0  # 改根路径后的同实体台账随迁（不算新入账）
    # 根路径已经从配置中移除时的遗留台账收口。它和普通 missing 分开计数，
    # 让用户能区分「磁盘删文件」与「容器挂载前缀改写」两类结果。
    removed_root_marked_missing: int = 0
    removed_root_cleared: int = 0
    removed_root_conflicts: int = 0
    skipped_known: int = 0  # 已在台账、直接跳过
    skipped_ignored: int = 0  # 用户忽略过的文件，不再重走识别链
    marked_missing: int = 0  # 台账有但磁盘上已消失，标记 missing
    cleared_missing: int = 0  # 已确认丢失、被自动清理出台账的行（库级开关，默认关）
    deferred: int = 0  # 疑似写入中（mtime 太新），本轮暂缓入账，稍后自动补扫
    retried: int = 0  # 识别重试：在位但待识别的台账行重走识别链（不算新入账）
    reviewed: int = 0  # 身份复核：识别器升级后重走识别链的已识别行
    review_flagged: int = 0  # 复核发现新旧结论不一致、已写入复核建议的行
    probed: int = 0  # 补探：给缺介质规格的在位行补上 ffprobe 结果
    cancelled: bool = False  # 用户手动停止：已入账的保留，未处理的留待下次扫描
    errors: list[str] = field(default_factory=list)
    # 暂缓文件的最近到期秒数（补扫的等待时长；对外接口不暴露）
    recheck_delay_seconds: float = 0.0
    # 本轮挂上身份锚的条目（去重）：扫描收尾统一补齐图片资产与媒体目录
    # 镜像（docs/design/metadata.md 4.1；对外接口不暴露）
    identified_item_ids: set[int] = field(default_factory=set)


@dataclass
class RootPathReconcilePreview:
    """显式修复入口的只读预览结果。

    历史上已经完成根路径切换的库不会再触发 ``update_library`` 的补扫，
    因此需要一个不写数据库的入口，让管理员在执行前看到会合并、会标缺失
    与因身份冲突而保留的台账。这里的统计只基于当前台账；正式执行仍会先
    扫描新根，避免把预览当成瞬时的磁盘事实。
    """

    library_id: int
    old_root: str
    new_root: str
    same_path_candidates: int = 0
    safe_merges: int = 0
    marked_missing: int = 0
    conflicts: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    old_rows_to_delete_from_ledger: int = 0
    disk_files_to_delete: int = 0


def scan_summary_payload(summary: ScanSummary) -> dict[str, object]:
    """生成可持久化的扫描结论；内部调度字段不进入 Job JSON。"""
    payload = asdict(summary)
    payload.pop("recheck_delay_seconds", None)
    payload.pop("identified_item_ids", None)
    return payload


@dataclass
class _ScanJobBridge:
    """把高频进程内扫描状态节流写入 Job，并轮询持久化取消请求。

    台账逐文件提交才是真正的恢复检查点；这里保存的是观察进度。服务更新后
    扫描重新盘点，已经入账的路径按增量语义秒过，因此不会重复建档，也不把
    一份可能已过期的全盘路径快照塞进 SQLite。
    """

    context: jobs.JobContext
    _last_phase: ScanPhase | None = None
    _last_processed: int = -1
    _last_update_at: float = 0.0
    _last_cancel_check_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def raise_if_cancelled(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_cancel_check_at < 0.5:
            return
        self._last_cancel_check_at = now
        await self.context.raise_if_cancelled()

    async def checkpoint(
        self,
        state: ScanState,
        summary: ScanSummary,
        *,
        force: bool = False,
        check_cancel: bool = True,
        before_write: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """阶段变化、约一秒或约 1% 时落一次进度，避免逐文件刷事件表。"""
        async with self._lock:
            if check_cancel:
                await self.raise_if_cancelled()
            now = time.monotonic()
            step = max(1, state.total // 100) if state.total else 128
            if (
                not force
                and state.phase == self._last_phase
                and state.processed - self._last_processed < step
                and now - self._last_update_at < 1.0
            ):
                return
            if before_write is not None:
                # 扫描主会话可能正持有 SQLite 读/写事务。先把领域数据提交成
                # 安全检查点，再用 JobContext 的独立会话写观察进度，避免
                # 两个写事务互相等待，也保证进度绝不跑到账实前面。
                await before_write()
            determinate = state.total > 0
            percent = min(100.0, state.processed / state.total * 100) if determinate else None
            phase_order = {
                ScanPhase.WALKING: 1,
                ScanPhase.INGESTING: 2,
                ScanPhase.PROBING: 3,
                ScanPhase.ASSETS: 4,
            }
            await self.context.update_progress(
                mode="determinate" if determinate else "indeterminate",
                phase=state.phase.value,
                message=(
                    f"{PHASE_LABELS[state.phase]}：{state.processed} / {state.total}"
                    if determinate
                    else f"{PHASE_LABELS[state.phase]}：已发现 {state.processed} 个文件"
                ),
                current=state.processed,
                total=state.total if determinate else None,
                percent=percent,
                phase_index=phase_order.get(state.phase),
                phase_count=4,
                details=scan_summary_payload(summary),
            )
            self._last_phase = state.phase
            self._last_processed = state.processed
            self._last_update_at = now


def is_scanning(library_id: int) -> bool:
    """该库是否有扫描类任务在跑（扫描或重识别）——库级互斥的判定入口。

    具体在做什么由 ``scan_progress(library_id).phase`` 区分：调用方要对
    用户描述状态时**必须**读阶段，不能一律说成"正在扫描"。
    """
    return _scan_tasks.running(library_id)


def busy_phase(library_id: int) -> ScanPhase | None:
    """该库进行中任务的阶段；没有任务在跑返回 None（接口提示文案用）。"""
    state = _scan_tasks.state_of(library_id)
    return state.phase if state is not None else None


def request_stop_scan(library_id: int) -> bool:
    """请求停止该库进行中的扫描；没有**可停止**的任务在跑返回 False。

    停止是"提前收尾"而非回滚：已识别入账的文件保留，还没处理到的文件
    下次扫描继续（扫描本就是增量幂等的）。遍历、入账、资产补齐三个阶段
    都在每个单位之间检查标志，单个文件的识别（含 TMDB 请求）不会被打断，
    通常几秒内停下。

    重识别阶段返回 False 而不是"假装受理"：它不看这个标志，受理了既停
    不下来，标志还会残留到下一次真扫描、让那次刚开始就被取消。
    """
    state = _scan_tasks.state_of(library_id)
    if state is None or state.phase not in _STOPPABLE_PHASES:
        return False
    return _scan_tasks.request_stop(library_id)


def _arm_rescan(library_id: int, delay: float) -> None:
    """给暂缓过文件的库挂一次性补扫（每库至多一个，落定后自然归零）。"""
    existing = _rescan_tasks.get(library_id)
    if existing is not None and not existing.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无事件循环（同步测试环境等）：由手动扫描/对账兜底
    _rescan_tasks[library_id] = loop.create_task(_rescan_later(library_id, delay))


async def _rescan_later(library_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    # 这一轮只为接住刚写完的新文件；历史规格补探必须由用户主动扫描触发，
    # 否则下载期间一次短暂的 mtime 波动也会重新扫到整库未补探的旧文件。
    await scan_library(library_id, backfill_existing_specs=False)


def last_scan(library_id: int) -> tuple | None:
    """最近一次扫描的 (完成时间, ScanSummary)；该库从未扫描过则为 None。"""
    return _scan_tasks.last(library_id)  # type: ignore[return-value]


def scan_progress(library_id: int) -> ScanState | None:
    """进行中任务的实时状态（阶段 + 已处理/总数）；没有任务在跑则为 None。

    与 ``is_scanning`` 同一份数据，不存在"在扫描却没有进度"的中间态。
    """
    return _scan_tasks.state_of(library_id)


async def _refresh_stats_snapshot(library_id: int, summary: ScanSummary) -> None:
    """用独立事务刷新库存快照；失败只记结论，不回滚已经提交的扫描成果。"""
    try:
        db = get_database()
        async with db.session() as session:
            await LibraryRepository(session).refresh_stats([library_id])
    except Exception:  # noqa: BLE001 -- 统计失败不应把已完成的入库事务回滚
        logger.exception("媒体库 #%s 库存统计刷新失败", library_id)
        message = "库存统计刷新失败，将在下次扫描时重试"
        if message not in summary.errors:
            summary.errors.append(message)


async def enqueue_scan_job(
    session: AsyncSession,
    library_id: int,
    library_name: str,
    *,
    reconcile_root_change: bool = False,
    previous_root_paths: list[str] | None = None,
    reconcile_new_root_paths: list[str] | None = None,
    actor_kind: str | None = None,
    actor_name: str | None = None,
    actor_id: str | None = None,
    origin: str = "web",
) -> jobs.CreateJobResult:
    """创建用户可观察、可取消、可跨重启恢复的媒体库扫描作业。"""
    input_data: dict[str, object] = {
        "library_id": library_id,
        "backfill_existing_specs": True,
    }
    if reconcile_root_change:
        input_data.update(
            reconcile_root_change=True,
            previous_root_paths=previous_root_paths or [],
            reconcile_new_root_paths=reconcile_new_root_paths or [],
        )
    return await jobs.create_job(
        session,
        job_type="library.scan",
        subject=library_name,
        input_data=input_data,
        resources=[jobs.ResourceRef("library", library_id)],
        dedupe_key=f"library.scan:{library_id}",
        conflict_policy="return_existing",
        handler_revision="library.scan.v1",
        max_attempts=3,
        actor_kind=actor_kind,
        actor_name=actor_name,
        actor_id=actor_id,
        origin=origin,
        progress=jobs.default_progress("等待扫描媒体库"),
    )


async def scan_library(
    library_id: int,
    *,
    backfill_existing_specs: bool = True,
    reprobe_paths: set[str] | None = None,
    scope_paths: set[str] | None = None,
    reconcile_root_change: bool = False,
    previous_root_paths: list[str] | None = None,
    reconcile_new_root_paths: list[str] | None = None,
    job_context: jobs.JobContext | None = None,
    raise_unexpected: bool = False,
) -> ScanSummary:
    """扫描一个库的全部根路径（后台任务入口；自开会话，不向外抛异常）。

    ``backfill_existing_specs`` 只应由用户主动发起的扫描保持开启。历史规格
    补探会对每个 ``audio_streams IS NULL`` 的在位文件、以及未补过 CLPI 语言的
    存量 BDMV 运行一次 ffprobe；把它绑到 watchdog、写入静默补扫或 6 小时对账，
    会让一次很小的目录事件演变为对整个机械盘媒体库的长时间读取。新入库文件
    仍在主流程中即时探测，不受此开关影响。

    ``reprobe_paths``：watchdog 点名的"内容被修改过"的视频路径集合
    （jellyfin-subtitle.md §2.4）。秒过时对名单内的行 stat 比对
    (size, mtime)，确认变化才重探——视频原地替换（洗版/重灌同路径）后
    介质规格与内封字幕轨不再永久陈旧。手动扫描（backfill_existing_specs
    开启）则对全部在位行做该比对，无需点名。

    ``scope_paths``：watch 事件限定的扫描范围（根下第一级条目的绝对路径
    集合，仅监听触发的扫描传入）。范围内的子树照常遍历入账、范围内的
    台账行照常标 missing，范围外的行完全不动；自动清理丢失记录在范围
    扫描中跳过（清理要求「整根遍历过且可信」，交给对账与手动扫描）。
    范围解析存疑时自动退回整库遍历（见 _resolve_scope）。只应与
    ``backfill_existing_specs=False`` 搭配、不与 reconcile 参数同用。

    ``reconcile_root_change`` 仅由媒体库编辑根路径后的后台扫描传入，且必须
    同时给出编辑前的 ``previous_root_paths``。它以旧/新根下相同的相对路径
    为候选，用 inode（被替换的旧入口不可访问时退回台账指纹）确认同一实体，防止
    挂载别名、软链接等换入口时重复入账。普通手动扫描/监听扫描不做这一步，
    以免把已移除根目录下的历史记录误作迁移。
    """
    from movieclaw_api.services.library.organize import is_organizing
    from movieclaw_api.services.library.transfer import is_transferring

    summary = ScanSummary(library_id=library_id)
    # watchdog 与定时对账仍是轻量直接触发，但必须服从 Job 的库级租约。
    # Job 处理器传入自己的 id 后可穿过这道检查；其他直接扫描看到锁就让路。
    db = get_database()
    async with db.session() as lock_session:
        queued_jobs = (
            [
                row
                for row in await jobs.list_jobs(
                    lock_session,
                    active_only=True,
                    resource_type="library",
                    resource_id=library_id,
                    limit=20,
                )
                if row.status is not JobStatus.BLOCKED
            ]
            if job_context is None
            else []
        )
        lock_owner = await jobs.resource_lock_owner(lock_session, "library", library_id)
    if queued_jobs or (
        lock_owner is not None and (job_context is None or lock_owner != job_context.job_id)
    ):
        summary.errors.append("该库有后台作业正在执行，扫描已顺延到下一次触发")
        return summary
    running = _scan_tasks.state_of(library_id)
    if running is not None:
        summary.errors.append(f"该库已有任务在进行中（{PHASE_LABELS[running.phase]}）")
        return summary
    # 与整理互斥：整理在批量改名，扫描此刻介入会把刚搬走的旧路径标 missing、
    # 把新路径当新文件重走识别链（人工认领可能丢失）。手动扫描、watchdog
    # 去抖、6 小时对账三个入口都收敛到这里，统一挡下（整理中台账已同步
    # 更新，无需扫描补账，漏掉的变更由下轮对账兜底）
    if is_organizing(library_id):
        summary.errors.append("该库正在整理文件名，扫描已跳过（整理完成后可重新扫描）")
        return summary
    # 与条目转移互斥，理由与整理完全相同：转移正在把整个条目目录搬进/搬出
    # 本库，扫描此刻介入会把搬走的旧路径标 missing、把搬来的新路径当新文件
    # 重走识别链。转移期间台账已同步随迁，无需扫描补账。搬运本身产生的
    # watchdog 事件也在这里被挡下
    if is_transferring(library_id):
        summary.errors.append("该库正在转移条目，扫描已跳过（转移完成后可重新扫描）")
        return summary
    # 状态先立起来再干活：从这一行到 finally 之间的任何路径，"在跑"与
    # "跑到哪了"都由同一个对象回答，接口不可能取到半截状态
    state = ScanState(phase=ScanPhase.WALKING)
    _scan_tasks.try_start(library_id, state)
    bridge = _ScanJobBridge(job_context) if job_context is not None else None
    finished_normally = False
    try:
        if bridge is not None:
            await bridge.checkpoint(state, summary, force=True)
        result = await _scan(
            library_id,
            summary,
            state,
            backfill_existing_specs=backfill_existing_specs,
            reprobe_paths=reprobe_paths or set(),
            scope_paths=scope_paths,
            reconcile_root_change=reconcile_root_change,
            previous_root_paths=previous_root_paths or [],
            reconcile_new_root_paths=reconcile_new_root_paths or [],
            bridge=bridge,
        )
        finished_normally = True
        return result
    except jobs.JobCancelled:
        summary.cancelled = True
        await _refresh_stats_snapshot(library_id, summary)
        if bridge is not None:
            # 取消结论也落进 progress.details；Job 的 cancelled 状态本身不写
            # result，重启后库详情仍能还原“扫到哪里后停止”。
            await bridge.checkpoint(state, summary, force=True, check_cancel=False)
        raise
    except Exception:  # noqa: BLE001 -- 后台任务兜底
        logger.exception("媒体库 #%s 扫描时发生未知错误", library_id)
        await _refresh_stats_snapshot(library_id, summary)
        if raise_unexpected:
            raise
        summary.errors.append("扫描中断：发生未知错误（详见后端日志）")
        finished_normally = True
        return summary
    finally:
        _scan_tasks.finish(library_id, result=(utcnow(), summary))
        # 暂缓过文件的扫描要自己安排补扫：写入结束后不会再有事件叫醒我们
        # （手动停止的不自动补扫——用户的意图是"别扫了"）
        if summary.deferred and not summary.cancelled and finished_normally:
            _arm_rescan(library_id, summary.recheck_delay_seconds)


@jobs.register_job_handler("library.scan")
async def _run_scan_job(
    context: jobs.JobContext, input_data: dict[str, object]
) -> dict[str, object]:
    """持久化扫描处理器：重启后重新盘点，逐文件台账检查点自然续跑。"""
    library_id = int(input_data["library_id"])
    db = get_database()
    async with db.session() as session:
        library = await session.get(Library, library_id)
    if library is None:
        raise jobs.JobFailed(
            "媒体库已不存在，无法继续扫描",
            code="LIBRARY_NOT_FOUND",
        )
    running = _scan_tasks.state_of(library_id)
    if running is not None:
        raise jobs.JobRetry(
            f"媒体库{PHASE_LABELS[running.phase]}，扫描作业稍后自动继续",
            delay_seconds=10,
        )
    from movieclaw_api.services.library.organize import is_organizing
    from movieclaw_api.services.library.transfer import is_transferring

    if is_organizing(library_id) or is_transferring(library_id):
        raise jobs.JobRetry("媒体库正在变更文件路径，扫描作业稍后自动继续", delay_seconds=10)
    scan_kwargs: dict[str, object] = {
        "backfill_existing_specs": bool(input_data.get("backfill_existing_specs", True)),
        "job_context": context,
        "raise_unexpected": True,
    }
    if input_data.get("reconcile_root_change") is True:
        roots = input_data.get("previous_root_paths")
        new_roots = input_data.get("reconcile_new_root_paths")
        scan_kwargs.update(
            reconcile_root_change=True,
            previous_root_paths=([str(path) for path in roots] if isinstance(roots, list) else []),
            reconcile_new_root_paths=(
                [str(path) for path in new_roots] if isinstance(new_roots, list) else []
            ),
        )
    summary = await scan_library(library_id, **scan_kwargs)
    payload = scan_summary_payload(summary)
    message = (
        f"扫描完成：新入账 {summary.scanned - summary.relinked} 个文件，"
        f"识别 {summary.identified} 个"
    )
    if summary.errors:
        message += f"，{len(summary.errors)} 个问题已记录"
    return {"message": message, **payload}


async def _resolve_scope(
    library: Library, scope_paths: set[str] | None
) -> dict[str, set[str]] | None:
    """把事件限定的绝对路径解析成「根路径 → 第一级条目名」；解析不了退回整库。

    范围路径由 watch 按建 watch 时的根推导（根下第一级条目，见
    watch._scope_for），扫描时根配置可能已经变化。只有每一条都仍是
    **现存目录**、且正好落在当前某个根的第一级时才启用范围扫描；任何
    存疑——根已换、条目已被删除/改名、根级散文件的事件——都返回 None
    走整库遍历。整库是今天的既有成本，多扫永远安全；范围判错漏扫则会
    漏标 missing、漏入账。
    """
    if not scope_paths:
        return None
    roots = {str(Path(r)) for r in library.root_paths}
    by_root: dict[str, set[str]] = {}
    for raw in sorted(scope_paths):
        target = str(Path(raw))
        parent, name = os.path.split(target)
        if parent not in roots or not name:
            return None
        by_root.setdefault(parent, set()).add(name)
    targets = sorted(scope_paths)
    all_dirs = await asyncio.to_thread(lambda: all(os.path.isdir(t) for t in targets))
    return by_root if all_dirs else None


async def _scan(
    library_id: int,
    summary: ScanSummary,
    state: ScanState,
    *,
    backfill_existing_specs: bool,
    reprobe_paths: set[str],
    scope_paths: set[str] | None,
    reconcile_root_change: bool,
    previous_root_paths: list[str],
    reconcile_new_root_paths: list[str],
    bridge: _ScanJobBridge | None,
) -> ScanSummary:
    db = get_database()
    async with db.session() as session:
        library = await session.get(Library, library_id)
        if library is None:
            summary.errors.append("媒体库不存在（可能已被删除）")
            return summary
        repo = LibraryFileRepository(session)
        known = {row.file_path: row for row in await repo.list_by_library(library_id)}
        # 显式历史修复只给出一个目标根；普通根路径编辑则默认取完整的新配置。
        # 后续两个阶段必须使用同一组根，才能正确判断「旧根对应哪个新根」。
        effective_reconcile_new_roots = reconcile_new_root_paths or list(library.root_paths)
        media_service = MediaLibraryService(
            session, get_tmdb_client(), scrape_library_id=library.id
        )
        kind = MediaKind(library.kind)
        # 每轮扫描内的收敛缓存：同一部剧同一季几十集只查一次 TMDB
        resolve_cache: dict[tuple, MediaItem | None] = {}
        # 目录 → 本地集数：同一季目录只统计一次（统计要对每个文件跑 enrich）
        episodes_cache: dict[Path, int | None] = {}
        # 下载线索：手动下载提交时锚定的「条目目录 → 副标题」（拼音名种子的救赎）
        hints = await _load_hints(session)
        # 一轮扫描里首次发现的所有文件共享批次号。已存在/回归的台账行不会
        # 覆盖原批次，因此「最近添加」只描述真正的新入账，不把重扫冒充新增。
        added_batch_id = uuid4().hex

        async def recover_failed_session() -> None:
            """单文件失败后的会话急救。失败若发生在半截事务里（如写台账时
            数据库层报错），会话会进入 pending rollback 状态，不回滚则后续
            所有文件跟着全灭（实测事故：一条撞键连坐 480 个文件）。而回滚
            又会把已加载对象全部置为过期——async 会话碰过期属性直接抛
            MissingGreenlet——所以回滚后把主循环依赖的共享对象一并重取。"""
            if session.is_active:
                return  # 失败不在事务层（探测/识别网络错误等），会话无恙
            await session.rollback()
            await session.refresh(library)
            known.clear()
            known.update({row.file_path: row for row in await repo.list_by_library(library_id)})
            resolve_cache.clear()

        # 先盘点全部待处理文件：总数定下来，进度才有分母。遍历的每一次
        # readdir 都是阻塞磁盘 IO（网络挂载上还是网络往返），分块放线程池
        # 执行、事件循环只在块间收结果——大库/慢盘遍历不再冻住全部请求
        seen_paths: set[str] = set()
        scanned_roots: list[str] = []
        # 遍历中列不动的目录（权限/IO/网络挂载抖动）：本轮的"没遍历到"因此
        # 不可信，自动清理必须整轮让路（见 _auto_clear_missing）
        unreadable_dirs: list[str] = []
        pending: list[tuple[Path, Path, bool]] = []  # (根, 文件, 是否原盘)
        # 目录 → 文件名列表：遍历时顺手截获，外挂字幕发现零额外目录 IO
        dir_files: dict[str, list[str]] = {}
        # 范围扫描（watch 事件限定）：根路径 → 允许下钻的第一级条目名；
        # None = 整库遍历。任何存疑（根已换、条目已消失、根级文件事件）都
        # 退回整库——宁可多扫，不能漏扫（漏扫会漏标 missing、漏入账）
        scope_names_by_root = await _resolve_scope(library, scope_paths)
        for root in library.root_paths:
            if bridge is not None:
                await bridge.raise_if_cancelled()
            root_path = Path(root)
            only_top: set[str] | None = None
            if scope_names_by_root is not None:
                only_top = scope_names_by_root.get(str(root_path))
                if not only_top:
                    continue  # 本根不在事件范围内：不遍历，也不参与丢失标记
            if not await asyncio.to_thread(root_path.exists):
                summary.errors.append(f"根路径不存在，已跳过：{root}")
                continue
            scanned_roots.append(str(root_path))
            walker = (
                _walk_videos(root_path, unreadable_dirs, dir_files)
                if only_top is None
                else _walk_videos(root_path, unreadable_dirs, dir_files, only_top=only_top)
            )
            while True:
                batch = await asyncio.to_thread(_take_chunk, walker)
                for file, is_disc in batch:
                    # 根路径互相嵌套时同一个文件会被遍历两次，去重后才是"每个
                    # 文件处理一次"：重复处理不只是白跑一趟识别链，第二趟还会
                    # 拿着过期的台账快照去插入已存在的路径
                    if str(file) in seen_paths:
                        continue
                    seen_paths.add(str(file))
                    pending.append((root_path, file, is_disc))
                state.processed = len(pending)
                if bridge is not None:
                    await bridge.checkpoint(state, summary, before_write=session.commit)
                if len(batch) < _WALK_CHUNK_FILES:
                    break

        # 根路径编辑后的同实体收敛：用户可能把 ``/media/movies`` 改成指向
        # 同一目录的挂载别名/软链接。此时磁盘对象没有变，台账里的旧路径却已
        # 不在当前根下；若直接按字符串入账，就会把同一文件显示成两份。
        #
        # 不把 inode 当作全局去重键：当前根里的两个硬链接可能是用户有意保留的
        # 两个版本。只有「旧台账路径脱离当前根」且「本轮扫描路径在当前根」的
        # 组合，才是根路径改写遗留的确定信号，见 _relink_legacy_root_paths。
        if reconcile_root_change:
            await _relink_legacy_root_paths(
                session,
                library,
                known,
                pending,
                summary,
                previous_root_paths=previous_root_paths,
                new_root_paths=effective_reconcile_new_roots,
            )

        assert library.id is not None
        # 分母定了，进入逐文件入账阶段
        state.phase = ScanPhase.INGESTING
        state.processed, state.total = 0, len(pending)
        if bridge is not None:
            await bridge.checkpoint(state, summary, force=True, before_write=session.commit)
        now_ts = time.time()
        min_remaining: float | None = None
        mtime_backfilled = 0
        rows_refreshed = 0  # 秒过行的外挂字幕/规格刷新计数（批量一次 commit）

        async def advance(done: int) -> None:
            state.processed = done
            if bridge is not None:
                await bridge.checkpoint(state, summary, before_write=session.commit)

        # 介质探测预取（_PROBE_AHEAD_WINDOW 的说明）：pending 索引 → 预取
        # 任务；不需要探测的位置记 None，避免每轮重复判定
        probe_ahead: dict[int, asyncio.Task[MediaSpec | None] | None] = {}
        for done, (root_path, file, is_disc) in enumerate(pending, start=1):
            if bridge is not None:
                await bridge.raise_if_cancelled()
            # 每进入新的一窗，先把这一窗要用到的 TMDB 档案并发拉回来（详见
            # _PREFETCH_WINDOW 的说明）。串行链路本身一行不改，只是轮到它
            # 建档时档案已经在手边了
            if (done - 1) % _PREFETCH_WINDOW == 0:
                await _prefetch_profiles(
                    session,
                    media_service,
                    kind,
                    pending[done - 1 : done - 1 + _PREFETCH_WINDOW],
                    known,
                )
            # 为下一窗要走入账链的文件提前起 ffprobe，与当前文件的识别/落账
            # 重叠；轮到自己时把结果领走（跳过路径直接丢弃，外壳保证无害）
            for ahead in range(done - 1, min(done - 1 + _PROBE_AHEAD_WINDOW, len(pending))):
                if ahead in probe_ahead:
                    continue
                _root_ahead, file_ahead, disc_ahead = pending[ahead]
                probe_ahead[ahead] = (
                    asyncio.create_task(_probe_quietly(file_ahead))
                    if _probe_ahead_eligible(known.get(str(file_ahead)), file_ahead, disc_ahead)
                    else None
                )
            probe_task = probe_ahead.pop(done - 1, None)
            # 用户请求停止：提前收尾。已入账的保留，剩余文件下次扫描继续
            if _scan_tasks.stop_requested(library_id):
                summary.cancelled = True
                logger.info(
                    "媒体库 #%s 扫描被手动停止（已处理 %d / 共 %d）",
                    library_id,
                    done - 1,
                    len(pending),
                )
                break
            path_str = str(file)
            existing = known.get(path_str)
            # 用户忽略过的文件秒过：花絮/预告/自录内容在 TMDB 本就没有条目，
            # 重走识别链只会再次失败、再次回到清单。忽略是**永久**承诺，
            # 「已忽略」清单里可一键恢复（消失又回来的顺手清 missing 标记，
            # 忽略状态原样保留）
            if existing is not None and existing.ignored_at is not None:
                if existing.state == FileState.MISSING:
                    assert existing.id is not None
                    await repo.clear_missing_flag(existing.id)
                summary.skipped_ignored += 1
                await advance(done)
                continue
            # 已识别且在位的行秒过。「在位但待识别」的行不跳过——重走识别链，
            # 让「重新扫描」天然成为识别重试入口（TMDB 网络故障恢复后重扫即可，
            # 不必先忽略再扫）；行原地更新，不产生新台账
            if (
                existing is not None
                and existing.state != FileState.MISSING  # 在位或待回收的已识别行都秒过
                and existing.media_item_id is not None
            ):
                # 身份对账：识别器升级后（版本戳落后）重走识别链复核——
                # 人工认领的身份永不复核；结论不一致只写建议不改身份
                if _review_due(existing):
                    try:
                        await _review_identity(
                            session,
                            media_service,
                            kind,
                            root_path,
                            file,
                            resolve_cache,
                            episodes_cache,
                            summary,
                            existing,
                            hint=_hint_for(file, hints),
                        )
                    except Exception as exc:  # noqa: BLE001 -- 单文件失败不断整轮
                        await recover_failed_session()
                        logger.exception("身份复核失败：%s", file)
                        summary.errors.append(f"「{file.name}」身份复核失败：{exc}")
                else:
                    summary.skipped_known += 1
                # mtime 一次性回填：ETag 落库特性上线前的旧行没有 mtime，
                # 趁扫描（用户主动发起）正在遍历目录顺手补上；补齐后的行
                # 重扫不再 stat，秒过语义不变
                if existing.file_mtime_ns is None:
                    try:
                        existing.file_mtime_ns = (await asyncio.to_thread(file.stat)).st_mtime_ns
                        mtime_backfilled += 1
                    except OSError:
                        pass
                # 外挂字幕重发现 + 视频变化重探（jellyfin-subtitle.md §2.4）。
                # 前者纯内存匹配 + 对命中的少数字幕文件 stat，每轮都做；
                # 后者要对视频本体 stat，只在手动扫描（backfill 开启）或
                # watchdog 点名该路径时做——6 小时对账保持零视频 stat
                if await _refresh_known_row(
                    existing,
                    file,
                    dir_files.get(str(file.parent)),
                    is_disc=is_disc,
                    check_media_change=(backfill_existing_specs or path_str in reprobe_paths),
                ):
                    rows_refreshed += 1
                await advance(done)
                continue
            # 完整性检测：mtime 太新 = 疑似写入中（下载/拷贝进行时），本轮
            # 暂缓入账、稍后补扫——库不假设目录用途，根路径完全可能同时是
            # 下载目录。mtime 在未来超出一个窗口视为时钟异常，照常入账
            try:
                age = now_ts - (await asyncio.to_thread(file.stat)).st_mtime
            except OSError:
                age = NEW_FILE_QUIET_SECONDS  # 瞬时消失/不可读：交给后续流程处理
            if -NEW_FILE_QUIET_SECONDS <= age < NEW_FILE_QUIET_SECONDS:
                summary.deferred += 1
                remaining = NEW_FILE_QUIET_SECONDS - age
                min_remaining = (
                    remaining if min_remaining is None else min(min_remaining, remaining)
                )
                await advance(done)
                continue
            try:
                await _ingest_file(
                    session,
                    repo,
                    media_service,
                    library,
                    kind,
                    root_path,
                    file,
                    resolve_cache,
                    episodes_cache,
                    summary,
                    is_disc=is_disc,
                    hint=_hint_for(file, hints),
                    existing=existing,
                    dir_names=dir_files.get(str(file.parent)),
                    added_batch_id=added_batch_id,
                    prefetched_probe=probe_task,
                )
            except Exception as exc:  # noqa: BLE001 -- 单文件失败不断整轮
                await recover_failed_session()
                logger.exception("扫描文件失败：%s", file)
                summary.errors.append(f"「{file.name}」处理失败：{exc}")
            await advance(done)

        # 手动停止/提前收尾时窗口里可能还挂着几个预取任务：取消掉，别让
        # 它们在扫描结束后继续占线程池（子进程本身很快自然结束）
        for leftover in probe_ahead.values():
            if leftover is not None:
                leftover.cancel()
        probe_ahead.clear()

        if mtime_backfilled or rows_refreshed:
            await session.commit()
        if rows_refreshed:
            logger.info(
                "媒体库 #%s：%d 个在位文件的外挂字幕/介质规格已刷新",
                library_id,
                rows_refreshed,
            )

        # ``known`` 是扫描开场的快照；新路径的行可能在逐文件入账时新建，也
        # 可能被改名归并迁入。根路径迁移的第二阶段必须重新读取它，才能看到
        # 已经存在的历史重复行，而不能只拿旧快照误判为「新路径还没有台账」。
        removed_root_result = _RemovedRootReconcileResult()
        if reconcile_root_change and not summary.cancelled:
            known.clear()
            known.update({row.file_path: row for row in await repo.list_by_library(library_id)})
            removed_root_result = await _reconcile_removed_root_ledger_rows(
                session,
                library,
                known,
                summary,
                previous_root_paths=previous_root_paths,
                new_root_paths=effective_reconcile_new_roots,
                seen_paths=seen_paths,
                roots_with_files={str(root_path) for root_path, _, _ in pending},
            )

        # 收尾感知删除：在位根路径下、台账有但本轮没遍历到 → 标记 missing。
        # 不存在的根整个不参与（挂载失败/掉盘时不误伤），文件回归时
        # upsert_by_path 会自动清除标记。判定须读行上的当前路径而非快照
        # 的旧 key：改名归并（_try_relink）会把行迁到本轮刚遍历过的新路径
        prefixes = [f"{r.rstrip('/')}/" for r in scanned_roots]
        # 范围扫描只对范围内的行做丢失判定：范围外的行"本轮没遍历到"是
        # 设计使然（根本没去看），不是丢失
        scope_prefixes: list[str] | None = None
        if scope_names_by_root is not None:
            walked_roots = set(scanned_roots)
            scope_prefixes = [
                str(Path(scoped_root) / name)
                for scoped_root, names in scope_names_by_root.items()
                if scoped_root in walked_roots
                for name in names
            ]
        # 原盘内部的行（存量嵌套/手动放入）：_walk_videos 不进原盘目录，
        # seen 恒不含它们，按 seen 判会误标缺失且永不自愈——这类行改按
        # 物理存在性对账（docs/design/disc-version-layout.md §4）。正常库
        # 这类行为零，逐行 stat 成本可忽略
        disc_prefixes = [
            f"{row.file_path.rstrip('/')}/"
            for row in known.values()
            if row.container in ("bluray", "dvd")
        ]
        now = utcnow()
        for row in known.values():
            path_str = row.file_path
            if path_str in seen_paths:
                continue
            if not any(path_str.startswith(prefix) for prefix in prefixes):
                continue
            if scope_prefixes is not None and not any(
                path_str == sp or path_str.startswith(sp + "/") for sp in scope_prefixes
            ):
                continue
            assert row.id is not None
            if any(path_str.startswith(dp) for dp in disc_prefixes):
                exists = Path(path_str).exists()
                if row.state == FileState.IN_PLACE and not exists:
                    await repo.mark_missing(row.id, since=now)
                    summary.marked_missing += 1
                elif row.state == FileState.MISSING and exists:
                    await repo.clear_missing_flag(row.id)
                continue
            if row.state != FileState.IN_PLACE:
                continue
            await repo.mark_missing(row.id, since=now)
            summary.marked_missing += 1

        # 收尾清理：开了「自动清理丢失记录」的库，把已确认丢失的行清出台账，
        # 让 file_count 与磁盘对齐（默认关；不可信的一轮整轮让路）。
        # 范围扫描整轮跳过：清理要求「整根遍历过且可信」，范围遍历给不出
        # 这个保证，确认清理交给 6 小时对账与手动扫描
        if scope_names_by_root is None:
            await _auto_clear_missing(
                repo,
                library,
                summary,
                known=list(known.values()),
                scanned_roots=scanned_roots,
                # 本轮真的遍历出文件的根：空根是"挂载掉了但挂载点还在"的典型
                # 症状，自动清理不能把它当"用户把片子删光了"（见 _auto_clear_missing）
                roots_with_files={str(root_path) for root_path, _, _ in pending},
                unreadable_dirs=unreadable_dirs,
            )
        await _auto_clear_removed_root_ledger(
            repo,
            library,
            summary,
            file_ids=removed_root_result.clearable_ids,
            unreadable_dirs=unreadable_dirs,
        )
        # 收尾后台账里仍标记丢失的条数：扫描结论要把"为什么 file_count 比
        # 磁盘上的文件多"说清楚，并给出收口路径，否则用户只能自己猜
        stale_missing = len(await repo.list_missing(library_id))

        # —— 介质规格补探 ——
        # 「扫描 = 把台账与磁盘对齐」，介质规格同样是磁盘真相的一部分。
        # 入账时每个新文件都探过一次，但**后装 ffprobe 的用户拿不到**：
        # 已识别且在位的行在上面的循环里整体秒过，永远走不到探测那一步。
        # 没有这一步，「装好 ffmpeg 再重新扫描」这个最直觉的动作就是无效的，
        # 用户只能一个条目一个条目点开、靠详情页那点限量补探慢慢磨。
        if backfill_existing_specs:
            await _probe_backfill(session, library_id, summary, state, bridge=bridge)

    # 文件台账已经收口就立刻发布统计，不等后续可能耗时数分钟的图片资产
    # 下载。扫描仍显示进行中，但首页的作品数与容量已经是本轮最新结果。
    await _refresh_stats_snapshot(library_id, summary)

    # AI 字幕自动生成挂钩（G2，subtitle-ai-translate.md §6）：开关默认关，
    # fire-and-forget 后台批次，绝不阻塞/影响扫描收尾
    try:
        from movieclaw_api.services.subtitle_gen.auto import queue_after_scan

        queue_after_scan(library_id)
    except Exception:  # noqa: BLE001 -- 自动生成不可用不能拖垮扫描
        logger.exception("自动字幕生成挂钩失败（不影响扫描结论）")

    if min_remaining is not None:
        summary.recheck_delay_seconds = max(5.0, min(min_remaining + 1.0, NEW_FILE_QUIET_SECONDS))
    if summary.marked_missing:
        logger.warning(
            "媒体库 #%s：%d 个文件已不在原位，已标记 missing（记录保留，文件回归自动恢复）",
            library_id,
            summary.marked_missing,
        )
    if stale_missing:
        # 扫完还留着丢失记录是**有意为之**（记录是重新下载与改名归并的依据），
        # 但用户看到的是"file_count 比磁盘上的文件多"。这里把原因和两条收口
        # 路径一次说清，省掉"扫完为什么还不干净"的困惑
        logger.info(
            "媒体库 #%s：台账里仍有 %d 条丢失记录（文件回归会自动恢复，也是「重新下载」"
            "的依据）。确认这些内容不再需要：在库详情「缺失」里清理，或执行 "
            "`lib missing clear`；想让以后每次扫描自动清理，打开本库的"
            "「自动清理丢失记录」开关",
            library_id,
            stale_missing,
        )
    if summary.deferred:
        logger.info(
            "媒体库 #%s：%d 个文件疑似写入中，本轮暂缓入账，约 %d 秒后自动补扫",
            library_id,
            summary.deferred,
            int(summary.recheck_delay_seconds),
        )
    if summary.kind_mismatched:
        # 库级配置问题，汇总说一次（逐文件告警会被几百集刷屏）：几乎必然是
        # 建库时类型选错了，认领单个文件解决不了。修复路径按错的范围分两条：
        # 个别文件放错挪文件即可；整库建错的正解是删库重建——删库只清台账
        # 不动磁盘（library_file 级联删除），重建后重扫即恢复，且这些文件
        # 因类型不符本来就未识别，没有任何可丢失的认领成果
        logger.warning(
            "媒体库 #%s（类型：%s）：%d 个文件的实际类型与本库不符——很可能是建库时"
            "类型选错了。这些文件不会被自动识别（按错误类型查 TMDB 会挂到毫不相干的"
            "作品上）。库类型创建后不可修改：个别文件放错了，把它们移到对应类型的库"
            "即可；整库建错了，请删除本库并以正确类型重建——删库不会动磁盘上的任何"
            "文件，重建后重新扫描即可恢复",
            library_id,
            _KIND_NAMES[kind],  # 会话已关，读本地枚举而非 ORM 属性（避免过期加载）
            summary.kind_mismatched,
        )
    logger.info(
        "媒体库 #%s 文件入账完成：新入账 %d（已识别 %d / 待识别 %d），识别重试 %d，"
        "身份复核 %d（存疑 %d），改名归并 %d，根路径随迁 %d，跳过已知 %d，跳过已忽略 %d，"
        "标记丢失 %d（旧根 %d），清理丢失 %d（旧根 %d），旧根冲突 %d，暂缓 %d，问题 %d",
        library_id,
        summary.scanned - summary.relinked,
        summary.identified,
        summary.unidentified,
        summary.retried,
        summary.reviewed,
        summary.review_flagged,
        summary.relinked,
        summary.root_relinked,
        summary.skipped_known,
        summary.skipped_ignored,
        summary.marked_missing,
        summary.removed_root_marked_missing,
        summary.cleared_missing,
        summary.removed_root_cleared,
        summary.removed_root_conflicts,
        summary.deferred,
        len(summary.errors),
    )

    # 一次入库刮削的资产补齐：文本档案在建档时已随 ensure_media_item 落库，
    # 这里给本轮新挂锚的条目补图片资产与媒体目录镜像（失败只记日志，任一
    # 后续刷新入口自愈）。
    # 这是扫描的**独立阶段**而非附带动作：一部剧几百张分集剧照能下十几
    # 分钟，必须换上自己的分子分母，否则进度会僵在"文件数/文件数"上，
    # 用户只能看到一个不动的进度条（这正是它曾经的样子）。
    #
    # 并发处理：这一阶段的时间几乎全花在等图床响应上，串行等于把几百次
    # 往返一个一个排队。与整库刷新同一套 queue + worker 写法；每个条目
    # 各自开会话、彼此无共享状态，真正的下载并发另有图床闸把着
    if summary.identified_item_ids:
        from movieclaw_api.services.media_scrape import ensure_assets

        item_ids = sorted(summary.identified_item_ids)
        state.phase = ScanPhase.ASSETS
        state.processed, state.total = 0, len(item_ids)
        if bridge is not None:
            await bridge.checkpoint(state, summary, force=True)
        logger.info("媒体库 #%s 开始补齐 %d 个条目的图片资产", library_id, len(item_ids))
        queue: asyncio.Queue[int] = asyncio.Queue()
        for item_id in item_ids:
            queue.put_nowait(item_id)

        async def _asset_worker() -> None:
            while True:
                try:
                    item_id = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                # 停止请求在这里同样生效：文件都已入账，缺的图片由任一后续
                # 刷新入口自愈，没有理由让用户点了停止还得干等
                if _scan_tasks.stop_requested(library_id):
                    return
                if bridge is not None:
                    await bridge.raise_if_cancelled()
                await ensure_assets(item_id)
                state.processed += 1
                if bridge is not None:
                    await bridge.checkpoint(state, summary)

        await asyncio.gather(*(_asset_worker() for _ in range(_ASSET_CONCURRENCY)))
        if _scan_tasks.stop_requested(library_id):
            logger.info(
                "媒体库 #%s 图片资产补齐被手动停止（已完成 %d / 共 %d，缺的图片下次刷新自愈）",
                library_id,
                state.processed,
                len(item_ids),
            )
    if bridge is not None:
        await bridge.checkpoint(state, summary, force=True)
    return summary


async def _auto_clear_missing(
    repo: LibraryFileRepository,
    library: Library,
    summary: ScanSummary,
    *,
    known: list[LibraryFile],
    scanned_roots: list[str],
    roots_with_files: set[str],
    unreadable_dirs: list[str],
) -> None:
    """扫描收尾：把已确认丢失的库存记录清出台账（库级开关，默认关）。

    用户手动删掉磁盘上的文件后，「扫完台账还没干净、得再手动清一次缺失」
    是实打实的认知负担——开了 ``auto_clear_missing`` 的库在这里一次收口，
    ``file_count`` 扫完即与磁盘对齐。

    **默认关是有理由的**：missing 行不是垃圾数据，它是缺失清单「重新下载」
    的输入、跨轮次改名归并的候选池（尺寸指纹匹配靠它），行上还带着不可
    再生的介质规格与来源种子。清理不可恢复，得由用户明确表态。

    清理的判据是「本轮扫描可不可信」，不是「丢了多久」——时间阈值既拦不住
    真正的危险（半个目录读不动时，等一天再删还是删错），又与用户的诉求
    正面冲突（删完文件立刻扫描，阈值让这一轮什么也清不掉，抱怨照旧）。
    可信的定义：

    - 遍历没有吞掉任何目录（``unreadable_dirs`` 为空）——读不动的目录底下
      的文件会被误判丢失，这一轮整轮让路；
    - 扫描没有被手动停止（用户的意图是"别扫了"，不是"顺便帮我删台账"）；
    - 只清**在位根路径下**的行：根路径不存在时整根跳过（掉盘/挂载失败），
      那些行本轮压根没被验证过，一条都不动；
    - 台账里有记录、本轮却一个文件都没遍历出来的根不参与：网络挂载掉线时
      挂载点往往还在、只是变成一个**空目录**，路径存在、目录可读、底下什么
      都没有——与"用户把这个根下的片子全删了"在磁盘上长得一模一样。两者
      只能二选一地误判，宁可少清（记录留着，下轮再清）也不能清错（删了
      回不来）。

    满足以上条件时，"台账有、磁盘上遍历不到"就是文件真的没了。改名/移动
    在这之前已由 ``_try_relink`` 整行随迁，不会走到这里。
    """
    if not library.auto_clear_missing:
        return
    if unreadable_dirs:
        logger.warning(
            "媒体库 #%s：有 %d 个目录本轮读取失败（如 %s），自动清理丢失记录已跳过"
            "——读不动的目录不等于文件不存在，误删无法恢复。请检查目录权限或挂载状态",
            library.id,
            len(unreadable_dirs),
            unreadable_dirs[0],
        )
        return
    if summary.cancelled:
        logger.info("媒体库 #%s：扫描被手动停止，本轮不做丢失记录的自动清理", library.id)
        return
    prefixes = []
    for root in scanned_roots:
        prefix = f"{root.rstrip('/')}/"
        if root in roots_with_files:
            prefixes.append(prefix)
            continue
        if any(row.file_path.startswith(prefix) for row in known):
            logger.warning(
                "媒体库 #%s：根路径「%s」本轮一个文件都没扫到，但台账里有它的记录，"
                "自动清理丢失记录已跳过这个根——网络挂载掉线时挂载点会变成空目录，"
                "与「文件被删光」分不开。若确实是你自己清空了这个目录，"
                "请在库详情「缺失」里手动清理",
                library.id,
                root,
            )
    doomed = [
        row.id
        for row in known
        if row.id is not None
        and row.library_id == library.id
        and row.state == FileState.MISSING
        and any(row.file_path.startswith(prefix) for prefix in prefixes)
    ]
    if not doomed:
        return
    summary.cleared_missing = await repo.delete_by_ids(doomed)
    logger.warning(
        "媒体库 #%s：已清理 %d 条确认丢失的库存记录（本库开启了「自动清理丢失记录」）。"
        "磁盘文件未被动过；台账记录不可恢复，这些内容如需补回请重新下载",
        library.id,
        summary.cleared_missing,
    )


@dataclass
class _RemovedRootReconcileResult:
    """已移除根路径的本轮收口结果，供安全清理阶段继续判断。"""

    clearable_ids: list[int] = field(default_factory=list)


def _normalise_root_path(root: str | Path) -> str:
    """按配置语义规范化根路径，只消除尾部斜杠，不解析软链接。

    这里刻意不用 ``resolve()``：旧根在当前容器里正是不可访问的对象，解析
    会把「配置上的挂载前缀」意外变成宿主可见路径，破坏这次迁移的边界。
    """
    value = str(Path(root))
    return value if value == "/" else value.rstrip("/")


def _removed_root_paths(previous_root_paths: list[str], new_root_paths: list[str]) -> list[Path]:
    """找出本次编辑中真正退出配置的根，重复配置只保留第一次。"""
    current = {_normalise_root_path(root) for root in new_root_paths}
    removed: list[Path] = []
    seen: set[str] = set()
    for root in previous_root_paths:
        normalised = _normalise_root_path(root)
        if normalised in current or normalised in seen:
            continue
        seen.add(normalised)
        removed.append(Path(normalised))
    return removed


def _row_under_root(row: LibraryFile, root: Path) -> Path | None:
    """返回台账路径相对某个根的部分；不在该根下时返回 ``None``。"""
    try:
        return Path(row.file_path).relative_to(root)
    except ValueError:
        return None


def _has_same_identity(old: LibraryFile, current: LibraryFile) -> bool:
    """已移除根的宽松合并仍要求两侧身份锚完整且完全相同。

    旧根不可访问时，尺寸与 mtime 已不足以作为实体证明；但若两个台账已经
    指向同一媒体条目和同一季集，再结合根迁移后的相同相对路径，才允许消除
    历史重复。任何一侧未识别都不做猜测，改为保留新行并把旧行收进 missing。
    """
    return (
        old.media_item_id is not None
        and current.media_item_id is not None
        and old.media_item_id == current.media_item_id
        and (old.season_number, old.episode_number)
        == (current.season_number, current.episode_number)
    )


def _has_identity_conflict(old: LibraryFile, current: LibraryFile) -> bool:
    """两侧都已识别却不一致时，必须留下人工可处理的冲突。"""
    return (
        old.media_item_id is not None
        and current.media_item_id is not None
        and not _has_same_identity(old, current)
    )


def _target_roots_by_old_root(
    previous_root_paths: list[str], new_root_paths: list[str]
) -> dict[str, set[str]]:
    """推导每个旧根可被自动清理时所依赖的新根。

    只有「恰好一个旧根被替换为恰好一个新根」才有足够证据自动清理。多根
    编辑中按索引猜测配对会把 A→C 的旧行错误归到仍保留的 B，最终可能误删
    台账；无法确认配对时仍可标 missing 和合并重复行，但不自动删除。
    """
    result: dict[str, set[str]] = {}
    old = [_normalise_root_path(root) for root in previous_root_paths]
    new = [_normalise_root_path(root) for root in new_root_paths]
    current = set(new)
    removed = {root for root in old if root not in current}
    added = {root for root in new if root not in set(old)}
    if len(removed) == len(added) == 1:
        result[next(iter(removed))] = {next(iter(added))}
    return result


async def _subtitle_job_running(session: AsyncSession, row: LibraryFile) -> bool:
    """合并前确认是否有运行中的字幕任务，避免删除运行中任务正在读取的行。"""
    assert row.id is not None
    return bool(
        await jobs.list_jobs(
            session,
            active_only=True,
            job_type="subtitle.generate",
            resource_type="library_file",
            resource_id=row.id,
            limit=1,
        )
    )


async def _merge_removed_root_duplicate(
    session: AsyncSession,
    old: LibraryFile,
    current: LibraryFile,
    *,
    target_path: str,
) -> tuple[LibraryFile, int] | None:
    """合并已移除旧根和当前新根的重复台账，默认保留新路径行。

    若旧行正在生成字幕，保留其主键并把路径迁到新入口；任务可无中断继续。
    两边都有运行任务时没有安全的保留方，返回 ``None`` 交给调用方报告冲突。
    """
    old_running, current_running = await asyncio.gather(
        _subtitle_job_running(session, old),
        _subtitle_job_running(session, current),
    )
    if old_running and current_running:
        return None
    survivor, duplicate = (old, current) if old_running else (current, old)
    assert duplicate.id is not None
    duplicate_id = duplicate.id
    await _merge_same_file_rows(session, survivor, duplicate, file_path=target_path)
    return survivor, duplicate_id


async def _cleanup_subtitle_checkpoints(file_ids: list[int]) -> None:
    """删除已被合并台账的旧字幕断点；持久作业引用已在事务中改写。"""
    for file_id in file_ids:
        for path in Path("data/cache/subtitle_gen").glob(f"{file_id}.*.checkpoint.json"):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("清理重复台账的字幕断点失败：file=%s %s", file_id, exc)


async def _reconcile_removed_root_ledger_rows(
    session: AsyncSession,
    library: Library,
    known: dict[str, LibraryFile],
    summary: ScanSummary,
    *,
    previous_root_paths: list[str],
    new_root_paths: list[str],
    seen_paths: set[str],
    roots_with_files: set[str],
) -> _RemovedRootReconcileResult:
    """收口根路径编辑遗留的旧前缀台账，绝不删除磁盘文件。

    此逻辑只由 ``reconcile_root_change`` 调用。严格 inode/指纹迁移已经在扫
    描前完成；这里针对的是旧容器挂载点已经消失、且历史上已经形成新旧两行
    的情形。身份完全一致才合并；无法确认时只把旧行标 missing，身份冲突则
    保留两行以免吞掉人工认领。自动清理另由下一阶段按本轮可信度决定。
    """
    selected_new_roots = new_root_paths or list(library.root_paths)
    removed_roots = _removed_root_paths(previous_root_paths, selected_new_roots)
    if not removed_roots or not selected_new_roots:
        return _RemovedRootReconcileResult()

    root_available = {
        _normalise_root_path(root): await asyncio.to_thread(root.exists) for root in removed_roots
    }
    trusted_new_roots = {_normalise_root_path(root) for root in roots_with_files}
    cleanup_targets = _target_roots_by_old_root(previous_root_paths, selected_new_roots)
    result = _RemovedRootReconcileResult()
    now = utcnow()
    changed = False
    removed_subtitle_file_ids: list[int] = []
    warned_accessible_roots: set[str] = set()

    for old in list(known.values()):
        if old.state != FileState.IN_PLACE:
            continue
        old_root = next(
            (root for root in removed_roots if _row_under_root(old, root) is not None), None
        )
        if old_root is None:
            continue
        relative = _row_under_root(old, old_root)
        assert relative is not None
        old_root_key = _normalise_root_path(old_root)
        # 根和该文件仍能访问时，不能把它当成被卸载的旧入口；保留原台账，
        # 避免用户只是暂时调整根路径配置时被误收口。文件本身已经不存在时，
        # 则仍按本次「旧根已移除」的语义标记缺失。
        if root_available[old_root_key] and await asyncio.to_thread(Path(old.file_path).exists):
            if old_root_key not in warned_accessible_roots:
                logger.warning(
                    "媒体库 #%s：已移除根「%s」仍可访问，暂不自动收口其旧路径台账",
                    library.id,
                    old_root,
                )
                warned_accessible_roots.add(old_root_key)
            continue

        candidates = [Path(root) / relative for root in selected_new_roots]
        # 只有本轮确实遍历到的候选才参与合并；Path.exists 对网络挂载的权限/IO
        # 错误会吞成 False，不把它当成「文件不存在」来合并。
        current_rows = {
            row.id: row
            for candidate in candidates
            if str(candidate) in seen_paths and str(candidate) in known
            for row in [known[str(candidate)]]
            if row is not old
        }
        existing_candidates = [
            candidate for candidate in candidates if str(candidate) in seen_paths
        ]
        if len(current_rows) == 1:
            current = next(iter(current_rows.values()))
            target_path = current.file_path
            if _has_same_identity(old, current):
                old_path = old.file_path
                merged = await _merge_removed_root_duplicate(
                    session, old, current, target_path=target_path
                )
                if merged is None:
                    summary.removed_root_conflicts += 1
                    summary.errors.append(
                        "旧根重复台账各有一个字幕任务在运行，暂不合并："
                        f"{old.file_path} ↔ {target_path}"
                    )
                    continue
                survivor, duplicate_id = merged
                known.pop(old_path, None)
                known[target_path] = survivor
                removed_subtitle_file_ids.append(duplicate_id)
                summary.root_relinked += 1
                changed = True
                logger.info("已合并已移除根的重复台账：%s → %s", old_path, target_path)
                continue
            if _has_identity_conflict(old, current):
                summary.removed_root_conflicts += 1
                summary.errors.append(
                    f"已移除根存在身份冲突台账，未自动合并：{old.file_path} ↔ {target_path}"
                )
                logger.warning(
                    "媒体库 #%s：已移除根的同相对路径台账身份冲突，已保留两行：%s ↔ %s",
                    library.id,
                    old.file_path,
                    target_path,
                )
                continue
        elif len(current_rows) > 1:
            summary.removed_root_conflicts += 1
            summary.errors.append(f"已移除根在多个新根有同相对路径记录，未自动合并：{old.file_path}")
            continue

        # 无新文件，或新行无法确认身份时，旧容器路径已不可用，不应继续被详情
        # 页、缩略图刷新等业务当作在位文件。只标台账缺失，磁盘从不触碰。
        old.mark_missing(now)
        old.updated_at = now
        summary.marked_missing += 1
        summary.removed_root_marked_missing += 1
        changed = True
        # 删除条件需要一个明确、且本轮实际扫到文件的新根。没有可确定配对时
        # 不删，保留 missing 供管理员在预览后手工处理；有同路径但身份未确认
        # 时也可删旧行——旧容器路径已确认不可用，新行仍会完整保留。唯一例外
        # 是候选已遍历却没有新台账（通常是写入静默窗暂缓）：等下轮入账后再说。
        targets = cleanup_targets.get(old_root_key, set())
        if targets and targets & trusted_new_roots and (not existing_candidates or current_rows):
            assert old.id is not None
            result.clearable_ids.append(old.id)
        if existing_candidates:
            logger.info(
                "媒体库 #%s：旧根台账无法确认与新路径为同一媒体，已标记 missing：%s",
                library.id,
                old.file_path,
            )

    if changed:
        await session.commit()
        await _cleanup_subtitle_checkpoints(removed_subtitle_file_ids)
    return result


async def _auto_clear_removed_root_ledger(
    repo: LibraryFileRepository,
    library: Library,
    summary: ScanSummary,
    *,
    file_ids: list[int],
    unreadable_dirs: list[str],
) -> None:
    """按普通自动清理同等可信条件，删除本轮确认的旧根台账。"""
    if not file_ids or not library.auto_clear_missing:
        return
    if summary.cancelled or unreadable_dirs:
        logger.info(
            "媒体库 #%s：本轮扫描未完全可信，已移除根的缺失台账只标记、不自动清理",
            library.id,
        )
        return
    cleared = await repo.delete_by_ids(file_ids)
    summary.cleared_missing += cleared
    summary.removed_root_cleared += cleared
    if cleared:
        logger.warning(
            "媒体库 #%s：已清理 %d 条已移除根的遗留台账（仅数据库记录，磁盘未动）",
            library.id,
            cleared,
        )


async def preview_root_path_reconcile(
    session: AsyncSession,
    library: Library,
    *,
    old_root: str,
    new_root: str,
) -> RootPathReconcilePreview:
    """预览历史根路径迁移修复，不扫描也不修改任何台账。"""
    old_root_path = Path(_normalise_root_path(old_root))
    new_root_path = Path(_normalise_root_path(new_root))
    rows = await LibraryFileRepository(session).list_by_library(library.id)  # type: ignore[arg-type]
    by_path = {row.file_path: row for row in rows}
    old_root_available = await asyncio.to_thread(old_root_path.exists)
    preview = RootPathReconcilePreview(
        library_id=library.id,  # type: ignore[arg-type]
        old_root=str(old_root_path),
        new_root=str(new_root_path),
    )
    for old in rows:
        if old.state != FileState.IN_PLACE:
            continue
        relative = _row_under_root(old, old_root_path)
        if relative is None:
            continue
        if old_root_available and await asyncio.to_thread(Path(old.file_path).exists):
            continue
        candidate = new_root_path / relative
        current = by_path.get(str(candidate))
        if current is None:
            preview.marked_missing += 1
            continue
        preview.same_path_candidates += 1
        if _has_same_identity(old, current):
            preview.safe_merges += 1
            preview.old_rows_to_delete_from_ledger += 1
        elif _has_identity_conflict(old, current):
            preview.conflicts.append(f"{old.file_path} ↔ {candidate}")
        else:
            preview.marked_missing += 1
            preview.unconfirmed.append(f"{old.file_path} ↔ {candidate}")
    return preview


def unit_name(file: Path, is_disc: bool) -> str:
    """识别单元的"干净名字"：普通文件去扩展名，原盘目录名**原样保留**。

    目录没有扩展名，对它用 ``Path.stem`` 会把含点片名的最后一段误当扩展名
    截掉（实测：原盘目录「E.T.外星人 (1982)」→ stem 只剩「E.T」，片名与
    年份证据全丢，整条识别链因此空转）。识别链上凡是要拿单元名字的地方
    都必须走这里，不允许直接用 ``file.stem``。"""
    return file.name if is_disc else file.stem


def scanned_media_source(attrs: TorrentAttrs) -> str | None:
    """扫描落库用的片源值：把只存在于名称里的 remux 标记折进 ``media_source``。

    ``library_file`` 没有 remux 布尔列——按既有设计，Remux 以 ``media_source``
    取值 ``"Remux"`` 表达（``decision.source_tier`` 对该值直接判 T5，人工标注
    走的也是这条路）。但 enrich 把 remux 解析成**独立布尔位**，而扫描此前只
    落 ``media_source`` 与 ``release_group``，这一位就地丢失。

    受害的是"写了 Remux、却没写片源词"的命名——Emby / MoviePilot 的整理模板
    正是这种（``片名 (年份) - 2160p Remux CHD.mkv``）：enrich 解析结果是
    ``media_source=None`` + ``remux=True``，落库后变成"片源未知"，洗版基线
    不可比。更糟的是整理改名会把这段技术串一起抹掉，之后连重新解析的机会
    都没有（``snapshot_from_file`` 正是靠 ``library_file.media_source`` 回补
    才不受改名影响，remux 没有对应列可回补）。

    只在 ``media_source`` 缺席时折入：名称同时给出片源与 remux（如
    ``UHD BluRay REMUX``）时保留更具体的片源值，不覆盖既有信息。
    """
    if attrs.remux and not attrs.media_source:
        return "Remux"
    return attrs.media_source


def disc_main_stream(disc_dir: Path) -> Path | None:
    """原盘主流文件（探测用）：蓝光优先按 MPLS 主播放列表，损坏盘降级取最大流。"""
    playlist = read_main_playlist(disc_dir)
    if playlist is not None:
        stream_dir = disc_dir / "BDMV" / "STREAM"
        try:
            by_stem = {
                path.stem: path
                for path in stream_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".m2ts"
            }
        except OSError:
            by_stem = {}
        existing = [by_stem[clip_id] for clip_id in playlist.clip_ids if clip_id in by_stem]
        if existing:
            return max(existing, key=lambda path: path.stat().st_size)
    for sub, exts in (("BDMV/STREAM", {".m2ts"}), ("VIDEO_TS", {".vob"})):
        stream_dir = disc_dir / sub
        if not stream_dir.is_dir():
            continue
        candidates = [f for f in stream_dir.iterdir() if f.is_file() and f.suffix.lower() in exts]
        if candidates:
            return max(candidates, key=lambda f: f.stat().st_size)
    return None


def _disc_total_size(disc_dir: Path) -> int:
    total = 0
    for f in disc_dir.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


# 原盘结构的标志目录名（与 layout.is_disc_dir 同一判据）：遍历时直接看
# 子目录清单里有没有它们，免去对每个目录再补两次探测 stat
_DISC_MARKER_DIRS = ("BDMV", "VIDEO_TS")

# 遍历分块大小：每块在线程池里读这么多文件再回事件循环收一次结果/检查点
_WALK_CHUNK_FILES = 500


def _take_chunk(walker, size: int = _WALK_CHUNK_FILES) -> list:
    """（线程池）从遍历生成器取一块结果——阻塞的磁盘 IO 全部发生在这里。"""
    return list(islice(walker, size))


def _walk_videos(
    root: Path,
    unreadable: list[str] | None = None,
    dir_files: dict[str, list[str]] | None = None,
    only_top: set[str] | None = None,
):
    """深度遍历，产出 (路径, 是否原盘目录)。

    基于 ``os.scandir``：目录/文件判定直接用 readdir 带回的 d_type，不再对
    每个条目单独 stat（网络挂载上每次 stat 都是一轮往返，本地大库也省一半
    系统调用）；原盘判定同样改用子目录清单推断（BDMV/VIDEO_TS 是否在列），
    不再对每个目录额外探测两次。根目录自身不做原盘判定（沿用历史行为：
    根是库不是条目）。

    原盘目录（BDMV/VIDEO_TS 结构）整体作为**一个条目**产出、不再下钻——
    盘内的几十个流文件不是独立影片。普通目录剪掉忽略/隐藏目录后继续下钻。

    ``unreadable``：调用方传入的收集器，列不动的目录（权限/IO/网络挂载抖动）
    会把路径记进来。读不动的目录只能跳过——但**跳过不等于目录是空的**，
    它底下的文件会因为"本轮没遍历到"被判丢失。标记 missing 时这无伤大雅
    （回归自动恢复），自动清理必须知情：这一轮的"没遍历到"不可信。

    ``dir_files``：调用方传入的目录清单收集器（目录路径 → 文件名列表）。
    外挂字幕发现（jellyfin-subtitle.md §2.1）靠它做零额外 IO 的前缀匹配
    ——目录本轮已经列过，不再为字幕单独列第二遍。

    ``only_top``：范围扫描（watch 事件限定，见 scan_library 的 scope_paths）
    传入的「根下第一级条目名」集合——只下钻/产出命中的第一级条目，深层
    不再限制。根目录仍完整列一次（本就只有一次 scandir 的成本），因此
    ``dir_files`` 里根层文件清单完整，外挂字幕匹配不受范围影响。
    """
    # 栈元素 (目录路径, 是否做原盘判定, 第一级名字限制)：根不做原盘判定
    # 且带范围限制；下钻的子目录都要判原盘、不再限制
    stack: list[tuple[str, bool, set[str] | None]] = [(str(root), False, only_top)]
    while stack:
        current, check_disc, restrict = stack.pop()
        try:
            with os.scandir(current) as scandir_it:
                entries = sorted(scandir_it, key=lambda e: e.name)
        except OSError:
            if unreadable is not None:
                unreadable.append(current)
            continue
        subdirs: list[os.DirEntry] = []
        files: list[os.DirEntry] = []
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False  # 判定不了的按文件走扩展名过滤（同旧行为）
            (subdirs if is_dir else files).append(entry)
        if check_disc and any(e.name in _DISC_MARKER_DIRS for e in subdirs):
            yield Path(current), True
            continue
        if dir_files is not None:
            dir_files[current] = [e.name for e in files]
        for entry in subdirs:
            name = entry.name
            if restrict is not None and name not in restrict:
                continue
            if name.startswith(".") or name.lower() in _IGNORE_DIRS:
                continue
            stack.append((entry.path, True, None))
        for entry in files:
            name = entry.name
            if restrict is not None and name not in restrict:
                continue
            lower = name.lower()
            suffix = Path(lower).suffix
            if suffix not in SCAN_VIDEO_EXTS and suffix != ".iso":
                continue
            if any(marker in lower for marker in _IGNORE_MARKERS):
                continue
            # 花絮/预告常常不在子目录里、就躺在正片旁边（BD 压制的通行做法），
            # 目录级忽略挡不住，靠文件名惯例挡住——不入库就不会错挂
            marker = extras_marker(name)
            if marker is not None:
                logger.debug("跳过花絮/预告类文件（命中「%s」）：%s", marker, entry.path)
                continue
            yield Path(entry.path), False


async def _relink_legacy_root_paths(
    session,
    library: Library,
    known: dict[str, LibraryFile],
    pending: list[tuple[Path, Path, bool]],
    summary: ScanSummary,
    *,
    previous_root_paths: list[str],
    new_root_paths: list[str],
) -> None:
    """根路径编辑后，将同一实体的旧台账路径迁到当前扫描路径。

    根编辑请求同时带来修改前后的根列表。只将同一旧根/新根组合下**相同
    相对路径**的文件配对：这既让新增软链接别名根也能复用原行，又不会把
    同库不同目录里用户有意保留的硬链接误合并。旧入口能访问时必须同 inode；
    旧挂载点已经撤掉时，则由台账中已记录的尺寸和 mtime 双重校验兜底。

    不扫描历史 ``missing`` 行：它们的路径不一定仍对应用户本次编辑的根，
    静默复活会污染缺失清单。一个新路径有多个候选时同样不处理，宁可本轮
    重新走原有入账链路，也不能猜测并吞掉人工台账。
    """
    old_roots = [Path(root) for root in previous_root_paths]
    new_roots = [Path(root) for root in (new_root_paths or list(library.root_paths))]
    if not old_roots or not new_roots:
        return

    def _stat(path: Path):
        try:
            return path.stat()
        except OSError:
            return None

    pending_by_path = {str(file): file for _root, file, _is_disc in pending}
    # 记录候选所属的旧根：旧根仍在新配置中时，跨设备号的尺寸/mtime 一致
    # 不足以证明同一实体（两个独立根也可能恰好有同名、同大小的文件）。
    candidates_by_path: dict[str, list[tuple[LibraryFile, Path]]] = {}
    for row in known.values():
        if row.state != FileState.IN_PLACE:
            continue
        old_path = Path(row.file_path)
        for old_root in old_roots:
            try:
                relative = old_path.relative_to(old_root)
            except ValueError:
                continue
            for new_root in new_roots:
                new_path = str(new_root / relative)
                if new_path != row.file_path and new_path in pending_by_path:
                    candidates_by_path.setdefault(new_path, []).append((row, old_root))
            break

    changed = False
    removed_subtitle_file_ids: list[int] = []
    for path_str, old_rows in candidates_by_path.items():
        if len(old_rows) != 1:
            continue
        old, old_root = old_rows[0]
        file = pending_by_path[path_str]
        current_stat, old_stat = await asyncio.gather(
            asyncio.to_thread(_stat, file),
            asyncio.to_thread(_stat, Path(old.file_path)),
        )
        if current_stat is None:
            continue
        if old_stat is not None and old_stat.st_dev == current_stat.st_dev:
            # 同一文件系统中 inode 不同就是不同实体，不能由尺寸/mtime 推翻。
            if old_stat.st_ino != current_stat.st_ino:
                continue
        elif old_root in new_roots or not _matches_ledger_fingerprint(old, current_stat):
            # 保留旧根又新增另一根时，跨设备号不能只凭持久化指纹合并：两个
            # 独立目录可能刚好同大小、同 mtime。仅在旧根已被替换时，才允许
            # 旧入口不可达/跨挂载命名空间时的指纹回退；它不替代同文件系统
            # 内明确的 inode 反证。
            continue
        current = known.get(path_str)
        if current is old:
            continue
        if current is None:
            # 新增的是原根的别名时，原路径仍会在这一轮被遍历。保留它作为
            # 主台账路径，并把别名映射到同一行，扫描主循环因此两边都秒过，
            # 而不是把原行迁到别名后又把原路径重新入账。
            if old.file_path in pending_by_path:
                known[path_str] = old
                summary.root_relinked += 1
                logger.info("根路径新增别名，同一文件复用原台账：%s ↔ %s", old.file_path, path_str)
                continue
            old_path = old.file_path
            _relocate_root_ledger_row(old, path_str)
            known.pop(old_path, None)
            known[path_str] = old
            summary.root_relinked += 1
            changed = True
            logger.info("根路径变更，同一文件台账已随迁：%s → %s", old_path, path_str)
            continue
        # 当前路径已经有行，说明此前一次重扫已经造成重复。只有身份完全一致
        # （或一边还未识别）才自动合并；互相冲突时宁可保留两行并记问题，不能
        # 静默吞掉用户人工认领的结论。
        if not _can_merge_same_file_rows(old, current):
            summary.errors.append(f"同一文件存在冲突台账，未自动合并：{old.file_path} ↔ {path_str}")
            logger.warning(
                "根路径变更发现同一文件的台账身份冲突，未自动合并：%s ↔ %s",
                old.file_path,
                path_str,
            )
            continue
        assert old.id is not None and current.id is not None
        old_running = bool(
            await jobs.list_jobs(
                session,
                active_only=True,
                job_type="subtitle.generate",
                resource_type="library_file",
                resource_id=old.id,
                limit=1,
            )
        )
        current_running = bool(
            await jobs.list_jobs(
                session,
                active_only=True,
                job_type="subtitle.generate",
                resource_type="library_file",
                resource_id=current.id,
                limit=1,
            )
        )
        if old_running and current_running:
            summary.errors.append(f"同一文件的重复台账有两个字幕任务在运行，暂不合并：{path_str}")
            continue
        survivor, duplicate = (current, old) if current_running else (old, current)
        assert duplicate.id is not None
        old_path = old.file_path
        old_path_is_still_scanned = old_path in pending_by_path
        await _merge_same_file_rows(
            session,
            survivor,
            duplicate,
            # 保留原根又新增别名时，两个入口本轮都会被遍历。若无运行任务，
            # 原路径仍是更早的权威台账地址；若要保留新路径的运行任务，则它
            # 本来就已指向别名路径。无论哪种情况，下面都会让两个 key 指向
            # 同一行，避免主循环把另一个入口重新入账。
            file_path=(old_path if old_path_is_still_scanned and survivor is old else path_str),
        )
        known.pop(old_path, None)
        if old_path_is_still_scanned:
            known[old_path] = survivor
        known[path_str] = survivor
        removed_subtitle_file_ids.append(duplicate.id)
        summary.root_relinked += 1
        changed = True
        logger.info(
            "根路径变更的重复台账已合并：%s → %s%s",
            old_path,
            path_str,
            "（保留正在生成字幕的当前台账行）" if current_running else "",
        )
    if changed:
        # 根路径改写往往涉及整库几千行；必须作为一次事务提交，而不是每个
        # 文件一次 SQLite fsync。任何中途异常都会由 session 上下文回滚，
        # 不会留下半迁移台账。
        await session.commit()
        # 旧版断点以 file_id 命名；持久作业已经在事务内改指向保留行，提交后
        # 才清理被删行的残留断点，避免事务回滚却提前丢失可恢复状态。
        for file_id in removed_subtitle_file_ids:
            for path in Path("data/cache/subtitle_gen").glob(f"{file_id}.*.checkpoint.json"):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("清理重复台账的字幕断点失败：file=%s %s", file_id, exc)


def _matches_ledger_fingerprint(row: LibraryFile, stat) -> bool:
    """旧根不可访问时，确认新文件仍是原台账记录的受限回退证据。

    inode 只能在同一挂载命名空间中比较；用户把 ``/mnt/media`` 换成另一个
    容器挂载点后，旧路径常已不可见、``st_dev`` 也会变化。此时只有尺寸和
    mtime 均在旧行中存在且精确一致才允许迁移，缺一项就放弃，不能用弱指纹
    把同尺寸洗版误认为同一文件。
    """
    return (
        row.size_bytes == stat.st_size
        and row.file_mtime_ns is not None
        and row.file_mtime_ns == stat.st_mtime_ns
    )


def _can_merge_same_file_rows(old: LibraryFile, current: LibraryFile) -> bool:
    """同一实体的两行台账能否无损自动合并。

    同一文件原则上身份应当相同；允许一边尚未识别，或同一 media item 的季集
    号尚有旧数据缺失。人工认领和机器结果互相指向不同单元时必须由用户处理。
    """
    if old.media_item_id is not None and current.media_item_id is not None:
        if old.media_item_id != current.media_item_id:
            return False
        return (old.season_number, old.episode_number) == (
            current.season_number,
            current.episode_number,
        )
    return True


def _relocate_root_ledger_row(row: LibraryFile, file_path: str) -> None:
    """原地改写台账路径，保留主键及全部既有识别、规格与来源信息。"""
    row.file_path = file_path
    row.revive()
    row.updated_at = utcnow()


async def _merge_same_file_rows(
    session, survivor: LibraryFile, duplicate: LibraryFile, *, file_path: str
) -> None:
    """把根路径编辑产生的重复行合并进保留行，再删除另一行。

    常规路径保留根编辑前的原台账行，Jellyfin 来源 ID 等外部引用因此不中断；
    但若新路径行正在生成字幕，保留它才能让任务继续按原 file_id 取行。重复
    行中更完整的人工身份、规格和来源信息会补回保留行；被删行关联的持久作业
    同步改指向保留行，历史任务与后续重试都不会留下悬空 file_id。
    """
    survivor_is_manual = survivor.identity_source == IdentitySource.MANUAL
    duplicate_is_manual = duplicate.identity_source == IdentitySource.MANUAL
    if duplicate.media_item_id is not None and (
        survivor.media_item_id is None or duplicate_is_manual and not survivor_is_manual
    ):
        survivor.media_item_id = duplicate.media_item_id
        survivor.season_number = duplicate.season_number
        survivor.episode_number = duplicate.episode_number
        survivor.identity_source = duplicate.identity_source
        survivor.resolved_version = duplicate.resolved_version
        survivor.review_suggestion = duplicate.review_suggestion
        survivor.unidentified_reason = None
        survivor.unidentified_code = None
        survivor.unidentified_candidates = None
    if survivor.file_mtime_ns is None:
        survivor.file_mtime_ns = duplicate.file_mtime_ns
    if survivor.container is None:
        survivor.container = duplicate.container
    for field_name in (
        "resolution",
        "video_codec",
        "hdr",
        "bit_depth",
        "duration_seconds",
        "bit_rate",
        "frame_rate",
        "color_space",
        "audio_streams",
        "subtitle_streams",
        "external_subtitles",
        "media_source",
        "release_group",
        "site_id",
        "torrent_id",
    ):
        if getattr(survivor, field_name) is None:
            setattr(survivor, field_name, getattr(duplicate, field_name))
    # 人工标注的片源优先于自动解析值（对齐上方"人工身份优先"的原则）
    if duplicate.media_source_manual and not survivor.media_source_manual:
        survivor.media_source = duplicate.media_source
        survivor.media_source_manual = True
    if survivor.source == FileSource.SCANNED and duplicate.source == FileSource.IMPORTED:
        survivor.source = duplicate.source
    if survivor.ignored_at is None:
        survivor.ignored_at = duplicate.ignored_at
    if survivor.media_item_id is None:
        for field_name in (
            "unidentified_reason",
            "unidentified_code",
            "unidentified_candidates",
        ):
            if getattr(survivor, field_name) is None:
                setattr(survivor, field_name, getattr(duplicate, field_name))
    assert survivor.id is not None and duplicate.id is not None
    await _retarget_library_file_jobs(session, duplicate.id, survivor.id)
    # file_path 有唯一索引，必须先让重复行的 DELETE 落到数据库，才能把原行
    # 迁到新路径；同一事务保证外界只会看到合并前或合并后的完整状态。
    await session.delete(duplicate)
    await session.flush()
    survivor.file_path = file_path
    survivor.revive()
    survivor.updated_at = utcnow()


async def _retarget_library_file_jobs(
    session: AsyncSession, duplicate_id: int, survivor_id: int
) -> None:
    """合并台账时同步迁移任务资源，并修正字幕任务的可重试输入。"""
    resources = list(
        (
            await session.execute(
                select(JobResource).where(
                    JobResource.resource_type == "library_file",
                    JobResource.resource_id == str(duplicate_id),
                )
            )
        ).scalars()
    )
    for resource in resources:
        existing_id = (
            await session.execute(
                select(JobResource.id).where(
                    JobResource.job_id == resource.job_id,
                    JobResource.resource_type == resource.resource_type,
                    JobResource.resource_id == str(survivor_id),
                    JobResource.relation == resource.relation,
                )
            )
        ).scalar_one_or_none()
        if existing_id is None:
            resource.resource_id = str(survivor_id)
        else:
            await session.delete(resource)

        job = await session.get(Job, resource.job_id)
        if job is None or job.job_type != "subtitle.generate":
            continue
        input_data = dict(job.input_data)
        if str(input_data.get("file_id")) != str(duplicate_id):
            continue
        input_data["file_id"] = survivor_id
        job.input_data = input_data
        prefix = f"subtitle.generate:{duplicate_id}:"
        if job.dedupe_key and job.dedupe_key.startswith(prefix):
            job.dedupe_key = f"subtitle.generate:{survivor_id}:{job.dedupe_key[len(prefix) :]}"


async def _refresh_known_row(
    row: LibraryFile,
    file: Path,
    dir_names: list[str] | None,
    *,
    is_disc: bool,
    check_media_change: bool,
) -> bool:
    """秒过行的增量刷新：外挂字幕重发现 +（点名时）视频变化重探。

    返回是否改动了行（调用方批量 commit，与 mtime 回填同一提交点）。

    - 外挂字幕：每轮都做——纯内存前缀匹配 + 对命中的少数字幕文件 stat，
      不触碰视频本体；旧行（NULL）顺带回填。原盘条目恒 []（§2.1）；
    - 视频变化重探（§2.4）：只在 ``check_media_change`` 时 stat 视频本体，
      (size, mtime) 确认变化才 ffprobe；**探测成功才回写**规格与新鲜度键
      ——半截写入的替换文件探测会失败，保留旧值让下一轮重试。strm 无
      媒体流，不参与重探。
    """
    changed = False
    if is_disc:
        discovered: list[dict] = []
    else:
        discovered = await discover_external_subtitles_async(file, dir_names)
    if row.external_subtitles != discovered:
        row.external_subtitles = discovered
        row.updated_at = utcnow()
        changed = True

    is_strm = not is_disc and file.suffix.lower() == STRM_EXT
    if check_media_change and not is_disc and not is_strm:
        try:
            st = await asyncio.to_thread(file.stat)
        except OSError:
            return changed
        if (st.st_size, st.st_mtime_ns) != (row.size_bytes, row.file_mtime_ns):
            spec = await asyncio.to_thread(probe_media, file)
            if spec is not None:
                row.size_bytes = st.st_size
                row.file_mtime_ns = st.st_mtime_ns
                row.resolution = spec.resolution
                row.video_codec = spec.video_codec
                row.hdr = spec.hdr
                row.bit_depth = spec.bit_depth
                row.duration_seconds = spec.duration_seconds
                row.bit_rate = spec.bit_rate
                row.frame_rate = spec.frame_rate
                row.color_space = spec.color_space
                row.audio_streams = list(spec.audio_streams)
                row.subtitle_streams = list(spec.subtitle_streams)
                row.updated_at = utcnow()
                changed = True
                logger.info("视频文件内容已变化，介质规格与内封字幕轨已重探：%s", file)
    return changed


def _probe_ahead_eligible(existing: LibraryFile | None, file: Path, is_disc: bool) -> bool:
    """该文件轮到时是否会需要一次 ffprobe（介质探测预取的判定）。

    原盘不预取（要先解析盘内主流文件，现场处理）；strm 不预取（无媒体流）；
    已识别且在位的秒过行与已忽略行不会走入账链，同样不预取。判定与
    ``_scan`` 主循环的秒过条件保持一致；判错的代价不对称且都可接受——
    漏预取只是退回现场探测，多预取至多白跑一个子进程。
    """
    if is_disc or file.suffix.lower() == STRM_EXT:
        return False
    if existing is not None and existing.ignored_at is not None:
        return False
    return not (
        existing is not None
        and existing.state != FileState.MISSING
        and existing.media_item_id is not None
    )


async def _probe_quietly(file: Path) -> MediaSpec | None:
    """预取探测的外壳：任何异常收敛为 None（与探测失败同语义）。

    预取任务可能因文件被跳过（暂缓入账等）而被丢弃，不能让未取回的
    异常在事件循环里报"Task exception was never retrieved"。
    """
    try:
        return await asyncio.to_thread(probe_media, file)
    except Exception:  # noqa: BLE001 -- 预取失败退化为"没探测到"，不断扫描
        logger.exception("介质探测预取失败：%s", file)
        return None


async def _ingest_file(
    session,
    repo: LibraryFileRepository,
    media_service: MediaLibraryService,
    library: Library,
    kind: MediaKind,
    root: Path,
    file: Path,
    resolve_cache: dict,
    episodes_cache: dict[Path, int | None],
    summary: ScanSummary,
    *,
    is_disc: bool = False,
    hint: _SubtitleHint | None = None,
    existing: LibraryFile | None = None,
    dir_names: list[str] | None = None,
    added_batch_id: str,
    prefetched_probe: asyncio.Task[MediaSpec | None] | None = None,
) -> None:
    """把一个文件识别并写入台账。``existing`` 是该路径已有的台账行：
    在位但待识别 → 本次是识别重试；标记过 missing → 文件回归。
    ``prefetched_probe``：主循环预取的介质探测任务（普通视频文件才有，
    见 _PROBE_AHEAD_WINDOW），没给就现场探测。"""
    if existing is None or existing.state == FileState.MISSING:
        summary.scanned += 1  # 新文件 / 回归的 missing 文件
    else:
        summary.retried += 1  # 在位但待识别：识别重试，不算新入账
    # 先探测：实测时长是电影同名候选消歧的强信号（原盘探测其主流文件）。
    # strm 是网盘播放占位文件（内容是一行 URL 的文本），没有可探测的媒体流，
    # 直接跳过——去探 strm 内部的 URL 既慢又不可靠（直链多带时效鉴权），
    # 还违背 strm"扫库零流量"的初衷，规格列留空即可
    is_strm = not is_disc and file.suffix.lower() == STRM_EXT
    probe_target = None if is_strm else (disc_main_stream(file) if is_disc else file)
    if probe_target is None:
        spec = None
    elif prefetched_probe is not None:
        # 预取只覆盖普通视频文件（probe_target 必然是文件本身），原盘仍走现场
        spec = await prefetched_probe
    else:
        spec = await asyncio.to_thread(probe_media, probe_target)
    if spec is not None and is_disc and (file / "BDMV").is_dir():
        # m2ts 常常不带语言描述符；同编号 CLPI 用 PID 补齐，已有 ffprobe
        # 语言保持不动。缺失/损坏 CLPI 只降级，不影响原盘入账。
        languages = await asyncio.to_thread(read_clpi_languages, probe_target)
        if languages is not None:
            spec = enrich_spec_with_clpi(spec, languages)
        playlist = await asyncio.to_thread(read_main_playlist, file)
        if playlist is not None and playlist.duration_seconds > 0:
            spec = replace(spec, duration_seconds=playlist.duration_seconds)
    if is_disc:
        size_bytes = await asyncio.to_thread(_disc_total_size, file)
        container = "bluray" if (file / "BDMV").is_dir() else "dvd"
        # 原盘条目 file_path 是目录，取目录 mtime——语义同样是"变了就变"
        try:
            file_mtime_ns: int | None = file.stat().st_mtime_ns
        except OSError:
            file_mtime_ns = None
    else:
        file_stat = file.stat()
        size_bytes = file_stat.st_size
        file_mtime_ns = file_stat.st_mtime_ns
        container = file.suffix.lstrip(".").lower() or None

    # 改名归并（走识别链之前）：新路径可能只是台账里某个旧文件被改了名，
    # 归并成功即结束——身份锚（含人工认领）随行迁移，免一次 TMDB 收敛。
    # 该路径已有台账行时跳过：行会原地更新，归并反而会撞路径唯一键。
    # strm 不参与归并：指纹靠"尺寸 + 时长"，而 strm 只有几百字节且无时长，
    # 同剧各集的 URL 长度往往完全相同，尺寸毫无区分度——错并会把身份锚
    # 挂到另一集头上，宁可当新文件重走识别链
    if (
        existing is None
        and not is_strm
        and await _try_relink(
            repo,
            library,
            file,
            size_bytes=size_bytes,
            container=container,
            duration_seconds=spec.duration_seconds if spec else None,
        )
    ):
        summary.relinked += 1
        return

    if existing is not None and existing.media_item_id is not None:
        # 文件回归且台账已有身份锚（可能来自人工认领）：原样保留，不重走
        # 识别链——重识别一旦失败（如 TMDB 恰好不通）会把已有身份冲掉
        item_id: int | None = existing.media_item_id
        unidentified_reason = None
        unidentified_code: str | None = None
        candidates: list[dict] = []
        season, episode = existing.season_number, existing.episode_number
        # 身份对账三件套随身份一并保留（含人工认领标记与未决的复核建议）
        identity_source = existing.identity_source
        resolved_version = existing.resolved_version
        review_suggestion = existing.review_suggestion
    else:
        identified = await _identify(
            media_service,
            kind,
            root,
            file,
            resolve_cache,
            episodes_cache,
            duration_seconds=spec.duration_seconds if spec else None,
            hint=hint,
            is_disc=is_disc,
        )
        unidentified_reason, candidates = identified.reason, identified.candidates
        unidentified_code = identified.code
        item_id = identified.item.id if identified.item is not None else None
        # 季集解析进线程池：要读分集 NFO（磁盘 IO），解析层兜底还会跑 NER
        season, episode = (
            (0, 0) if is_disc else await asyncio.to_thread(_unit_for, kind, file)
        )
        identity_source = identified.source if item_id is not None else None
        resolved_version = RESOLVER_VERSION if item_id is not None else None
        review_suggestion = None
        if item_id is not None:
            summary.identified += 1
            summary.identified_item_ids.add(item_id)
        else:
            summary.unidentified += 1
            if unidentified_code == UnidentifiedCode.KIND_MISMATCH:
                summary.kind_mismatched += 1
    attrs = enrich(unit_name(file, is_disc))
    # 外挂字幕发现（jellyfin-subtitle.md §2.1）：原盘恒 []，其余（含 strm）
    # 用遍历时截获的目录清单做前缀匹配
    external_subtitles: list[dict] = (
        [] if is_disc else await discover_external_subtitles_async(file, dir_names)
    )
    assert library.id is not None
    await repo.upsert_by_path(
        LibraryFile(
            library_id=library.id,
            media_item_id=item_id,
            season_number=season,
            episode_number=episode,
            file_path=str(file),
            size_bytes=size_bytes,
            file_mtime_ns=file_mtime_ns,
            container=container,
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
            external_subtitles=external_subtitles,
            media_source=scanned_media_source(attrs),
            release_group=attrs.release_group,
            source=FileSource.SCANNED,
            added_batch_id=added_batch_id,
            unidentified_reason=None if item_id is not None else unidentified_reason,
            unidentified_code=None if item_id is not None else unidentified_code,
            unidentified_candidates=None if item_id is not None else (candidates or None),
            identity_source=identity_source,
            resolved_version=resolved_version,
            review_suggestion=review_suggestion,
        ),
        # 同路径旧行调用方已经持有（扫描开场的整库快照），不必再查一次
        existing=existing,
    )
    if item_id is not None:
        # 库存对账：单元在库成立即关闭对应的订阅工单（订阅止于投递，
        # 完成状态由库存推导；文件回归同样适用）
        from movieclaw_api.services.subscription import close_fulfilled_wanted

        await close_fulfilled_wanted(session, item_id)


# ---------------------------------------------------------------------------
# 身份对账（识别器升级后的存量复核）
# ---------------------------------------------------------------------------


def _review_due(row: LibraryFile) -> bool:
    """该行是否需要身份复核：版本戳落后于当前识别器，且身份非人工认领。

    NULL 版本 = 特性上线前的旧数据，一律视为落后（存量库借此吃到升级红利）。
    """
    if row.identity_source == IdentitySource.MANUAL:
        return False
    return (row.resolved_version or 0) < RESOLVER_VERSION


async def _review_identity(
    session,
    media_service: MediaLibraryService,
    kind: MediaKind,
    root: Path,
    file: Path,
    resolve_cache: dict,
    episodes_cache: dict[Path, int | None],
    summary: ScanSummary,
    row: LibraryFile,
    *,
    hint: _SubtitleHint | None = None,
) -> None:
    """对一条已识别的台账行重走识别链，与现有身份对账。

    三种结局：
    - 结论一致 → 只更新版本戳与来源，皆大欢喜；
    - 结论不一致 → **不改身份**，写入复核建议进「身份复核」清单，用户拍板
      （新识别器也可能错，静默翻案会把改对的东西改错——宁可待确认）；
    - 新链给不出确定结论 → 保持原身份、盖戳了结（旧结论未必错，不打扰）；
      TMDB 网络类失败例外：不盖戳，下次扫描重试。
    """
    identified = await _identify(
        media_service,
        kind,
        root,
        file,
        resolve_cache,
        episodes_cache,
        duration_seconds=row.duration_seconds,
        hint=hint,
        is_disc=row.container in ("bluray", "dvd"),
    )
    item = identified.item
    if item is None and identified.code == UnidentifiedCode.TMDB_UNREACHABLE:
        return  # 网络类失败：本轮不盖戳，下次扫描继续复核
    if item is None and identified.code == UnidentifiedCode.KIND_MISMATCH:
        # 类型冲突：现挂的身份几乎必然是错的（按错误类型拉档拿到的是另一部
        # 作品），但没有替代条目可建议——复核清单帮不上忙。同样不盖戳：
        # 库类型的事得用户处理，盖戳等于把问题埋进版本号里；文件挪到对的
        # 库后重扫会自然走完整识别链。库级告警在扫描收尾汇总说一次
        summary.kind_mismatched += 1
        return
    summary.reviewed += 1
    row.resolved_version = RESOLVER_VERSION
    if item is not None and item.id != row.media_item_id:
        # 新旧结论不一致：身份不动，建议入清单
        row.review_suggestion = {
            "media_item_id": item.id,
            "tmdb_id": item.tmdb_id,
            "title": item.title,
            "year": item.year,
            "poster_path": item.poster_path,
        }
        summary.review_flagged += 1
        logger.warning(
            "身份复核存疑：「%s」现挂条目 #%s，新识别器认为应是《%s》（tmdb=%s），已进复核清单",
            file.name,
            row.media_item_id,
            item.title,
            item.tmdb_id,
        )
    else:
        # 结论一致（或新链给不出结论）：保持身份，清掉可能残留的旧建议
        if item is not None:
            row.identity_source = identified.source
        row.review_suggestion = None
    row.updated_at = utcnow()
    await session.commit()


# ---------------------------------------------------------------------------
# 改名归并
# ---------------------------------------------------------------------------

# 时长互证容差：同一文件两次 ffprobe 结果应一致，留 2 秒余量防版本差异
_RELINK_DURATION_TOLERANCE_SECONDS = 2


async def _probe_backfill(
    session,
    library_id: int,
    summary: ScanSummary,
    state: ScanState,
    *,
    bridge: _ScanJobBridge | None = None,
) -> None:
    """补齐未探测规格，并给存量 BDMV 回填 CLPI 语言。

    判据用 ``audio_streams IS NULL``：它是"这行从没探测成功过"的标记
    （空列表 = 探过、文件确实没有音轨，两者必须分开）。BDMV 另以流 JSON
    内的 CLPI 版本戳判断：已有数组但未读过 CLPI 的旧行也进入一次补探。

    不限量、但有自己的分子分母与停止响应——整库补探在网络挂载上可能是
    小时级的活，用户要看得到进度、也要停得下来。ffprobe 不可用时整段跳过，
    否则每轮扫描都会白跑一遍必然失败的探测。
    """
    from movieclaw_api.services.library.items import backfill_streams
    from movieclaw_api.services.media_probe import ffprobe_available

    if not ffprobe_available():
        return
    rows = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.library_id == library_id,
                    or_(
                        LibraryFile.audio_streams.is_(None),  # type: ignore[union-attr]
                        LibraryFile.container == "bluray",
                    ),
                    LibraryFile.in_place(),
                    LibraryFile.ignored_at.is_(None),  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    # 普通文件仍只补 audio_streams=NULL；蓝光旧行即使已有流数组，也要在
    # CLPI 版本戳缺失时做一次 PID 级重探。版本写进 JSON，后续手动扫描秒过。
    rows = [
        row
        for row in rows
        if row.audio_streams is None
        or (
            row.container == "bluray"
            and not streams_have_clpi_metadata(row.audio_streams, row.subtitle_streams)
        )
    ]
    # strm 占位文件永远探不出规格（本体没有媒体流），不进分母——否则
    # strm 库每轮扫描都会报"待补 N、探测 0"，像坏了一样
    rows = [row for row in rows if not row.file_path.lower().endswith(STRM_EXT)]
    if not rows:
        return
    state.phase = ScanPhase.PROBING
    state.processed, state.total = 0, len(rows)
    if bridge is not None:
        await bridge.checkpoint(state, summary, force=True, before_write=session.commit)
    logger.info("媒体库 #%s 开始补探 %d 个文件的介质规格", library_id, len(rows))

    async def _tick() -> bool:
        state.processed += 1
        if bridge is not None:
            await bridge.raise_if_cancelled()
        return not _scan_tasks.stop_requested(library_id)

    async def _checkpoint() -> None:
        if bridge is not None:
            await bridge.checkpoint(state, summary, force=True)

    summary.probed = await backfill_streams(
        session,
        rows,
        limit=None,
        on_processed=_tick,
        on_checkpoint=_checkpoint,
    )
    logger.info(
        "媒体库 #%s 介质规格补探结束：探测 %d / 待补 %d%s",
        library_id,
        summary.probed,
        len(rows),
        "（被手动停止，剩余的下次扫描继续）" if _scan_tasks.stop_requested(library_id) else "",
    )


async def _prefetch_profiles(
    session,
    media_service: MediaLibraryService,
    kind: MediaKind,
    window: list[tuple[Path, Path, bool]],
    known: dict[str, LibraryFile],
) -> None:
    """并发拉回这一窗里"身份已写死、且库里还没有"的条目档案，喂进建档缓存。

    见 ``_PREFETCH_WINDOW`` 的说明：解决的是"每部新片都要串行干等一次 TMDB
    往返"。只处理 NFO / ``[tmdbid=N]`` 声明过身份的文件——靠文件名解析的
    条目要先搜一次 TMDB 才知道 id，预取不到，它们走原来的串行链路。

    这一步纯属**加速**，失败一律吞掉：拉不到就等串行链路自己去拉，那里有
    完整的错误分类与用户可读的原因文案（TMDB 不通 / 确实找不到）。加速手段
    绝不能变成新的失败点，因此整段都在 try 里——包括算"该拉哪些"的部分。
    """
    try:
        await _collect_and_prefetch(session, media_service, kind, window, known)
    except Exception:  # noqa: BLE001 -- 预取失败等于没预取，扫描照常往下走
        logger.debug("TMDB 档案预取整窗失败（改由建档时逐个重试）", exc_info=True)


async def _collect_and_prefetch(
    session,
    media_service: MediaLibraryService,
    kind: MediaKind,
    window: list[tuple[Path, Path, bool]],
    known: dict[str, LibraryFile],
) -> None:
    """_prefetch_profiles 的正题：挑出该拉的 id 并发拉回来。"""
    wanted: set[int] = set()
    for root_path, file, _is_disc in window:
        existing = known.get(str(file))
        # 与主循环的跳过条件对齐：已忽略、以及已识别且在位的行都不会建档
        if existing is not None and (
            existing.ignored_at is not None
            or (existing.state != FileState.MISSING and existing.media_item_id is not None)
        ):
            continue
        nfo = _entry_nfo(kind, root_path, file)
        tmdb_id, source = pinned_tmdb_id(kind, root_path, file, nfo=nfo)
        if tmdb_id is not None and source is not None:
            wanted.add(tmdb_id)
    # 上一窗预取了却没被消费的档案（文件被静默窗暂缓、或钉死身份被本地证据
    # 推翻）到这里就该丢了，否则一轮长扫描会把它们一直攒在内存里
    keep = {(kind, tid) for tid in wanted}
    cache = media_service.profile_cache
    for stale_key in [key for key in cache if key not in keep]:
        del cache[stale_key]
    wanted -= {tid for _kind, tid in cache}
    if not wanted:
        return
    # 库里已有的条目建档时会走 get_by_anchor 直接复用，拉档案是白拉
    rows = await session.execute(
        select(MediaItem.tmdb_id).where(
            MediaItem.kind == kind.value,
            MediaItem.tmdb_id.in_(wanted),  # type: ignore[attr-defined]
        )
    )
    wanted -= {row[0] for row in rows.all()}
    if not wanted:
        return

    await asyncio.gather(
        *(media_service.prefetch_profile(kind, tid) for tid in sorted(wanted)),
        return_exceptions=True,  # 一个 id 拉挂了不该连累同窗其余的
    )


async def _try_relink(
    repo: LibraryFileRepository,
    library: Library,
    file: Path,
    *,
    size_bytes: int,
    container: str | None,
    duration_seconds: int | None,
) -> bool:
    """新路径落账前，先找"已消失的同尺寸旧行"迁移过来（磁盘改名检测）。

    磁盘上的改名/移动对台账来说是"旧路径消失 + 新路径出现"两个独立事件，
    直接当新文件落账会丢掉旧行的身份锚（尤其是人工认领的成果），旧行则
    沦为永久 missing 的幽灵行。这里用改名的不变量做指纹：

    - 候选：同库、尺寸精确相等、且已标记 missing 或路径已不在磁盘
      （后者覆盖 watchdog 实时触发、6 小时对账还没跑的窗口期）；
      路径仍在磁盘的同尺寸行是复制/硬链，不是改名，不参与；
    - 互证：新旧双方都有实测时长时必须一致（±2 秒），一方缺失只凭尺寸；
    - **唯一命中才归并**——多个候选宁可当新文件走识别链，不静默错挂。
    """
    assert library.id is not None
    path_str = str(file)
    candidates = []
    for row in await repo.find_by_size(library.id, size_bytes):
        if row.file_path == path_str:
            continue
        if row.state == FileState.TRASHED:
            continue  # 待回收行不参与改名归并——归并会复活它
        if (
            duration_seconds
            and row.duration_seconds
            and abs(duration_seconds - row.duration_seconds) > _RELINK_DURATION_TOLERANCE_SECONDS
        ):
            continue
        # 磁盘 stat 放最后：前面全是零成本的内存判定，先筛掉注定落选的
        # 候选，才轮到唯一需要 IO 的"路径是否仍在磁盘"（线程池，网络挂载
        # 上可能阻塞）——同尺寸候选多时省下的就是一串白付的往返
        if row.state == FileState.IN_PLACE and await asyncio.to_thread(Path(row.file_path).exists):
            continue
        candidates.append(row)
    if len(candidates) != 1:
        return False
    old = candidates[0]
    assert old.id is not None
    old_path = old.file_path
    await repo.relocate(old.id, file_path=path_str, container=container)
    logger.info("检测到文件改名，台账已随迁（身份保留）：%s → %s", old_path, path_str)
    return True


# ---------------------------------------------------------------------------
# 身份识别
# ---------------------------------------------------------------------------


@dataclass
class _Identified:
    """一次识别的结论：条目 + 失败原因/分类 + 待选候选（后三者仅失败时有值）。"""

    item: MediaItem | None
    reason: str | None = None
    candidates: list[dict] = field(default_factory=list)
    # 失败分类：清单据此给标签/配色/动作，与面向用户的 reason 文案解耦
    code: str | None = None
    # 身份来源（成功时有值）：钉死声明（path_tag/nfo）或名称收敛（resolved），
    # 落到台账 identity_source，对账机制据此分级
    source: IdentitySource | None = None


@dataclass
class _SubtitleHint:
    """``download_hint`` 行的解析形态（每轮扫描解析一次，同目录多文件复用）。"""

    save_path: str
    alt_title: str | None  # 副标题里的中文片名（enrich 提取）
    total_episodes: int | None  # 副标题「全N集」


async def _load_hints(session) -> list[_SubtitleHint]:
    """加载并解析全部下载线索；最长路径在前，嵌套目录时取最具体的一条。"""
    rows = list((await session.execute(select(DownloadHint))).scalars().all())
    hints = []
    for row in rows:
        attrs = enrich(row.subtitle)
        hints.append(
            _SubtitleHint(
                save_path=row.save_path.rstrip("/"),
                alt_title=attrs.titles_zh[0] if attrs.titles_zh else None,
                total_episodes=parse_total_episodes(row.subtitle),
            )
        )
    hints.sort(key=lambda h: len(h.save_path), reverse=True)
    return hints


def _hint_for(file: Path, hints: list[_SubtitleHint]) -> _SubtitleHint | None:
    """文件落在某条线索的目录之下 → 该线索适用（列表已按最具体优先排序）。"""
    for hint in hints:
        if Path(hint.save_path) in file.parents:
            return hint
    return None


def _local_identity_evidence(
    kind: MediaKind, root: Path, file: Path, *, is_disc: bool
) -> tuple[NfoIdentity | None, str | None, LocalEvidence | None]:
    """（线程池内运行）识别链的本地证据：NFO → 类型冲突判定 → 名称证据。

    类型冲突时不再收集名称证据（与原串行逻辑一致：冲突即早退，证据用不上）。
    """
    nfo = _entry_nfo(kind, root, file, is_disc=is_disc)
    conflict = _kind_conflict(kind, file, nfo, is_disc=is_disc)
    evidence = None if conflict is not None else guess_evidence(kind, root, file, is_disc=is_disc)
    return nfo, conflict, evidence


async def _identify(
    media_service: MediaLibraryService,
    kind: MediaKind,
    root: Path,
    file: Path,
    cache: dict,
    episodes_cache: dict[Path, int | None],
    *,
    duration_seconds: int | None = None,
    hint: _SubtitleHint | None = None,
    is_disc: bool = False,
) -> _Identified:
    """识别链主入口。

    识别成功时 reason 为 None；失败时给出用户能看懂的中文原因（落到台账的
    ``unidentified_reason``，待识别清单展示）——尤其要把"TMDB 访问失败"
    与"确实找不到匹配"区分开：前者修好网络重扫即可，后者需要人工认领。
    收敛器给出候选时一并带回（``candidates``），让用户在清单里直接点选。
    """
    # 本地证据三件套整体放线程池：NFO 查找要逐级 stat/读盘（网络挂载上
    # 可能长阻塞），guess_evidence 内含 NER 推理（CPU 数毫秒）——都不该
    # 占用事件循环；一次线程跳转拿齐，省去逐项跨线程的调度开销
    nfo, conflict, evidence = await asyncio.to_thread(
        _local_identity_evidence, kind, root, file, is_disc=is_disc
    )
    # ⓪ 类型冲突：文件实际是剧集/电影，与所在库的类型对不上（零网络成本）
    if conflict is not None:
        return _Identified(None, conflict, code=UnidentifiedCode.KIND_MISMATCH)

    # ① 显式精确身份：路径 tmdbid 标记（就近优先）→ NFO
    # 各级路径上的标记互相矛盾时全部不采信（实测：目录标着正主、文件名
    # 标着同名短片，"就近优先"会静默选中错的那个）——自相矛盾的声明不如
    # 让名称解析用物理证据（年份/时长）裁决；解析也失败时原因里说清矛盾
    rejected_pin: str | None = None
    path_ids = _path_tmdb_ids(root, file)
    if len(path_ids) > 1:
        rejected_pin = (
            f"路径上的 tmdbid 标记互相矛盾（{'、'.join(str(i) for i in path_ids)}），均不采信"
        )
        logger.warning("%s：%s", rejected_pin, file)
        tmdb_id, id_source = (
            (nfo.tmdb_id, IdentitySource.NFO)
            if nfo is not None and nfo.tmdb_id is not None
            else (None, None)
        )
    else:
        tmdb_id, id_source = pinned_tmdb_id(kind, root, file, nfo=nfo)
    if tmdb_id is not None and id_source is not None:
        try:
            item = await media_service.ensure_media_item(kind, tmdb_id)
        except Exception as exc:  # noqa: BLE001 -- 声明的 id 可能已失效，降级到解析
            logger.warning(
                "%s指定的 TMDB 条目建档失败（id=%s）：%s",
                _ID_SOURCE_NAMES[id_source],
                tmdb_id,
                exc,
            )
        else:
            # 时长轴只对电影生效：剧集的 runtime 是"单集常规时长"，特别篇/
            # 合集文件的实测时长天然偏离，拿去推翻声明会误伤
            runtime_minutes = (
                await media_service.runtime_minutes(item.id)
                if kind is MediaKind.MOVIE and item.id is not None
                else None
            )
            mismatch = _pinned_mismatch(
                item,
                evidence,
                duration_seconds=duration_seconds,
                runtime_minutes=runtime_minutes,
            )
            if mismatch is None:
                return _Identified(item, source=id_source)
            # 声明与本地证据严重矛盾：不采信，降级走名称解析（它要过完整的
            # 证据验证，此刻可信度高于一个对不上号的数字）
            rejected_pin = (
                f"{_ID_SOURCE_NAMES[id_source]} tmdbid={tmdb_id} 指向{mismatch}，与本地文件不符"
            )
            logger.warning("%s（%s），改按文件名识别", rejected_pin, file.name)

    # ② 名称解析 → TMDB 证据验证收敛（年份/时长/季集数佐证见 library_resolve）
    result = await _resolve_by_name(
        media_service,
        kind,
        file,
        cache,
        episodes_cache,
        evidence,
        duration_seconds=duration_seconds,
        hint=hint,
    )
    if rejected_pin is not None and result.item is None:
        # 声明被推翻、按名字也没认出来：原因以"声明有问题"开头——这才是
        # 用户唯一需要动手的地方（改标记/NFO 比每次都认领一遍一劳永逸）
        return replace(result, reason=f"{rejected_pin}；{result.reason}")
    return result


async def _resolve_by_name(
    media_service: MediaLibraryService,
    kind: MediaKind,
    file: Path,
    cache: dict,
    episodes_cache: dict[Path, int | None],
    evidence: LocalEvidence | None,
    *,
    duration_seconds: int | None,
    hint: _SubtitleHint | None,
) -> _Identified:
    """识别链步骤②：名称证据 → TMDB 证据验证收敛（见 library_resolve 模块头）。"""
    # 下载线索补强：副标题中文名作备选查询词，「全N集」作集数佐证；
    # 文件/目录名完全解析不出条目名时，中文名直接顶为主查询词
    if hint is not None:
        if evidence is None:
            if hint.alt_title:
                evidence = LocalEvidence(title=hint.alt_title)
        elif hint.alt_title:
            # 线索里真有中文名才顶掉备选词——guess_evidence 可能已放了
            # "Title (Year)"惯例名在这个位置，别被空线索冲掉
            evidence.alt_title = hint.alt_title
        if evidence is not None:
            evidence.total_episodes = hint.total_episodes
    if evidence is None:
        return _Identified(None, "无法从文件名/目录名解析出片名", code=UnidentifiedCode.UNPARSABLE)
    evidence.duration_seconds = duration_seconds
    # 本地实际集数（同一季目录只数一次）：同名双版本的分水岭证据
    if kind is MediaKind.TV:
        directory = file.parent
        if directory not in episodes_cache:
            # 统计要列目录 + 对每个文件名跑集号解析（含 NER），放线程池；
            # 缓存写回仍在事件循环，单飞扫描不存在并发写
            episodes_cache[directory] = await asyncio.to_thread(local_episode_count, directory)
        evidence.local_episodes = episodes_cache[directory]
    key = (
        evidence.title,
        evidence.alt_title,
        evidence.year,
        evidence.season,
        evidence.local_episodes,
    )
    if key in cache:
        return cache[key]
    year_note = f"（{evidence.year}）" if evidence.year else ""
    try:
        outcome = await resolve_with_candidates(get_tmdb_client(), kind, evidence)
        item = (
            await media_service.ensure_media_item(kind, outcome.tmdb_id, extra_aliases=[])
            if outcome.tmdb_id is not None
            else None
        )
    except Exception as exc:  # noqa: BLE001 -- TMDB 波动不该中断扫描
        logger.warning("TMDB 收敛失败（%s / %s）：%s", evidence.title, evidence.year, exc)
        # 网络类失败不写入缓存：同名文件下次扫描还有机会重试
        return _Identified(
            None,
            f"TMDB 查询失败（网络不通或接口异常）：{exc}",
            code=UnidentifiedCode.TMDB_UNREACHABLE,
        )
    if item is not None:
        result = _Identified(item, source=IdentitySource.RESOLVED)
    else:
        # 原因写清楚"卡在哪一步"：有候选说明机器是"不敢选"而非"找不到"。
        # 收尾不再写「请…」——该做什么由清单上的按钮表达，文案只说发生了什么
        detail = f"：{outcome.reason}" if outcome.reason else ""
        result = _Identified(
            None,
            f"按「{evidence.title}」{year_note}在 TMDB 未能确认身份{detail}",
            [asdict(c) for c in outcome.candidates],
            code=(UnidentifiedCode.AMBIGUOUS if outcome.candidates else UnidentifiedCode.NO_MATCH),
        )
    cache[key] = result
    return result


def pinned_tmdb_id(
    kind: MediaKind,
    root: Path,
    file: Path,
    *,
    nfo: NfoIdentity | None = None,
    is_disc: bool = False,
) -> tuple[int | None, IdentitySource | None]:
    """路径/NFO 里被**显式钉死**的 TMDB 身份，返回 (id, 来源)。

    路径标记优先于 NFO：``[tmdbid=N]`` 是用户或整理工具刚写在目录名上的
    声明，而 NFO 可能是很久以前刮削残留的。两者都没有时返回 (None, None)。
    ``nfo`` 传入已读到的条目级 NFO 可省掉一次磁盘查找（识别链就这么用）；
    需要现查时 ``kind`` 用于在多份 NFO 里挑与本库类型一致的那份。
    """
    tmdb_id = _path_tmdb_id(root, file)
    if tmdb_id is not None:
        return tmdb_id, IdentitySource.PATH_TAG
    if nfo is None:
        nfo = _entry_nfo(kind, root, file, is_disc=is_disc)
    tmdb_id = nfo.tmdb_id if nfo is not None else None
    return (tmdb_id, IdentitySource.NFO) if tmdb_id is not None else (None, None)


def _kind_conflict(
    kind: MediaKind, file: Path, nfo: NfoIdentity | None, *, is_disc: bool = False
) -> str | None:
    """文件的实际类型与所在库的类型是否冲突（冲突时返回中文原因）。

    为什么这一刀必须在识别链最前面：TMDB 的 movie 与 tv 是**两套独立的 id
    空间**，同一个数字在两边往往都能取到条目。库类型选错时，一个正确的
    ``[tmdbid=81356]`` 标记会被按电影拉档，接口 200 返回、建档成功、全程
    零报错——实测《性爱自修室》整库被建成 1938 年德国片《13 Stühle》。
    "识别失败"能进待识别清单被看见，"识别成另一部作品"则完全静默。

    只认两种**明确**信号，绝不靠猜（误判会把好好的库整个打进待识别）：
    - NFO 根元素 ``<movie>`` / ``<tvshow>``：刮削器写下的类型声明，双向可信；
    - 电影库里出现 ``Season N`` 目录或 ``SxxExx`` 文件名：季集号是剧集独有的
      结构。反方向不检测——剧集库里没有季集号的文件（特典、纪录片）很正常。
    """
    if nfo is not None and nfo.kind is not kind:
        return (
            f"同目录的 {'tvshow' if nfo.kind is MediaKind.TV else 'movie'}.nfo 表明这是"
            f"{_KIND_NAMES[nfo.kind]}，但所在媒体库的类型是「{_KIND_NAMES[kind]}」"
        )
    if kind is MediaKind.MOVIE:
        attrs = enrich(unit_name(file, is_disc))
        if season_from_dir(file.parent) is not None or (attrs.seasons and attrs.episodes):
            return "文件带季集号（是剧集），但所在媒体库的类型是「电影」"
    return None


def _pinned_mismatch(
    item: MediaItem,
    evidence: LocalEvidence | None,
    *,
    duration_seconds: int | None = None,
    runtime_minutes: int | None = None,
) -> str | None:
    """钉死身份拉到的条目是否与本地证据**严重**矛盾（矛盾时返回条目描述）。

    显式声明是用户的强意图，判定必须克制——手写 ``[tmdbid=N]`` 的主要用途
    恰恰是"机器认不出来时我告诉你"（拼音名、意译名目录），标题对不上是
    这类场景的常态，单凭标题不符推翻声明会误伤一大片。因此**标题相符就
    永不推翻**；标题不符时还须至少一条硬证据反对才判矛盾：
    - 标题：本地片名与条目主名/原名/别名归一后既不相等也无包含关系。
      包含判定要求**短边至少 2 个字符**——单字符标题（实测：短片《4》）
      几乎是任何片名的子串，包含关系在它身上零区分度；
    - 年份：两边都有年份且相差超过 2 年（同一部作品的年份偏差不会这么大）；
    - 时长（调用方只对电影传入）：条目片长与实测时长相差 3 倍以上且绝对差
      超 30 分钟（实测：NFO 里 3 分钟短片《4》冒认 115 分钟的神奇4侠，
      年份恰好同为 2025，年份轴对这类错误天然失明）。

    命中的是"标记打错数字"「NFO 是别的片子的残留」这类真错误，以及库类型
    选错时侥幸绕过类型检测的漏网之鱼。
    """
    if evidence is None:
        return None
    local = normalize_title(evidence.title)
    for text in (item.title, item.original_title, *item.aliases):
        known = normalize_title(text) if text else ""
        if not known:
            continue
        if known == local:
            return None
        if min(len(known), len(local)) >= 2 and (known in local or local in known):
            return None
    year_conflict = (
        evidence.year is not None
        and item.year is not None
        and abs(item.year - evidence.year) > _PINNED_YEAR_TOLERANCE
    )
    runtime_note = ""
    if duration_seconds and runtime_minutes:
        shorter, longer = sorted((duration_seconds, runtime_minutes * 60))
        if longer >= shorter * _PINNED_RUNTIME_RATIO and (
            longer - shorter >= _PINNED_RUNTIME_GAP_SECONDS
        ):
            runtime_note = (
                f"，片长 {runtime_minutes} 分钟 vs 本地实测约 {round(duration_seconds / 60)} 分钟"
            )
    if not year_conflict and not runtime_note:
        return None
    return f"《{item.title}》({item.year if item.year is not None else '年份未知'}{runtime_note})"


def _path_tmdb_id(root: Path, file: Path) -> int | None:
    """从文件名与各级目录名里读 ``[tmdbid=N]`` 标记（就近优先，向上到库根）。

    单集文件名上的标记比剧集目录上的更具体，因此从文件本身开始向上找。
    """
    current = file
    while True:
        match = _PATH_TMDBID.search(current.name)
        if match:
            return int(match.group(1))
        if current == root or current.parent == current:
            return None
        current = current.parent


def _path_tmdb_ids(root: Path, file: Path) -> list[int]:
    """路径各级名字上的**全部** tmdbid 标记（去重保序，就近在前）。

    超过一个说明声明自相矛盾（识别链据此整体不采信，见 ``_identify``）。
    """
    ids: dict[int, None] = {}
    current = file
    while True:
        for raw in _PATH_TMDBID.findall(current.name):
            ids.setdefault(int(raw), None)
        if current == root or current.parent == current:
            return list(ids)
        current = current.parent


def _entry_nfo(
    kind: MediaKind, root: Path, file: Path, *, is_disc: bool = False
) -> NfoIdentity | None:
    """找该文件适用的条目级 NFO：同名 .nfo → 目录级 movie/tvshow.nfo（向上到库根）。

    原盘单元（``is_disc``）的"文件"是目录：``with_suffix`` 对含点目录名
    会拼出无意义路径（「E.T.外星人 (1982)」→「E.T.nfo」），且刮削器惯例
    把 movie.nfo 写在**盘内**——因此原盘从自身目录开始向上找、不做同名。

    本函数只负责"查找顺序"这层策略；单份 NFO 的解析交给 library_nfo 的
    结构化解析器（正则扒文本会被 <actor> 块里的演员 person id 污染）。

    **带 tmdb id 的优先**：id 是 NFO 最有价值的信息，一份没写 id 的
    movie.nfo 不该挡住上层写了 id 的那份。全都没 id 时退回最近的一份——
    它声明的类型（movie/tvshow）仍然是有效信号，见 _kind_conflict。

    **同目录 movie.nfo 与 tvshow.nfo 并存时，与本库类型一致的那份优先**：
    两份类型声明自相矛盾，按固定顺序取谁都是瞎猜。实测代价：一次库类型
    选错的扫描往剧集目录里写进了 movie.nfo，库改回剧集后 movie.nfo 排在
    前面，整库每个文件都被自己写的 NFO 判成"放错库了"。只有一份声明时
    不受影响——真正放错库的文件照样判得出来。
    """
    fallback: NfoIdentity | None = None
    for nfo in entry_nfo_candidates(kind, root, file, is_disc=is_disc):
        if not nfo.is_file():
            continue
        identity = read_entry_identity(nfo)
        if identity is None:
            continue
        if identity.tmdb_id is not None:
            return identity
        if fallback is None:
            fallback = identity
    return fallback


def entry_nfo_candidates(
    kind: MediaKind, root: Path, file: Path, *, is_disc: bool = False
) -> list[Path]:
    """条目级 NFO 的查找路径序列（就近优先；``_entry_nfo`` 与认领纠错共用）。

    普通文件：同名 .nfo → 各级目录的 movie/tvshow.nfo（向上到库根）；
    原盘目录：盘内 movie/tvshow.nfo → 各级父目录（目录名不做同名拼接）。
    """
    entry_names = (
        ("movie.nfo", "tvshow.nfo") if kind is MediaKind.MOVIE else ("tvshow.nfo", "movie.nfo")
    )
    candidates = [] if is_disc else [file.with_suffix(".nfo")]
    current = file if is_disc else file.parent
    while True:
        candidates.extend(current / name for name in entry_names)
        if current == root or current.parent == current:
            break
        current = current.parent
    return candidates


def guess_evidence(
    kind: MediaKind, root: Path, file: Path, *, is_disc: bool = False
) -> LocalEvidence | None:
    """收集本地识别证据：条目名/年份 + 剧集的季集号（供收敛验证器佐证）。

    条目名：剧集优先用"剧集目录名"（比文件名干净）；电影优先用**符合
    「Title (Year)」惯例的条目目录名**，没有这样的目录才退回文件名（取舍
    见下方分支注释）。库根与文件之间的**每一层**目录都是候选（由近及远），
    第一个能解析出片名的胜出——分类分组层（``剧集/大陆/风筝 (2017)/…``）
    很常见，只认"库根的直接子目录"会把「大陆」当条目目录，白白丢掉真
    条目目录名里的年份证据。散落在库根下的裸文件退回用文件名解析。
    季集号：目录名与文件名两个来源合并取最大（季包目录带 SNN、文件名带
    SxxExx，各有一半信息）；S00 特别篇不计入季数证据。
    """
    dir_names = [d.name for d in entry_dirs(root, file)]
    own = unit_name(file, is_disc)
    if kind is MediaKind.TV:
        sources = [*dir_names, own]
    else:
        # 电影：**符合「Title (Year)」惯例的条目目录名压过文件名**。
        # 「一个电影条目目录 = 一部片」是 Emby/Plex/Jellyfin 的共同语义，而
        # 目录里除正片外常躺着花絮、片段、没被忽略名单挡住的杂项——拿它们
        # 的文件名去搜 TMDB 会高置信地搜出**另一部影片**（issue #107：
        # 「同一个文件夹下正片可以识别，bonus 等会识别成其他影片」）。惯例名
        # 带年份、是整理工具的产物，可信度高于目录内任意一个文件名。
        # 明知而为的代价：规范目录里塞了另一部片时会归到目录身份——那是
        # 用户的组织错误（Emby 同样会认成一部），且条目详情页的「修正识别
        # 结果」能当场改挂；而花絮混在正片目录里是压制片源的普遍现象。
        # 不符合惯例的分组目录（「电影/2021/」「电影/大陆/」）解析不出
        # 「Title (Year)」，照旧由文件名先说话，不受影响。
        titled_dirs = [name for name in dir_names if conventional_title(name)]
        # 去重保序：命中惯例的目录名在后半段会再出现一次，重复 enrich 是白跑
        sources = list(dict.fromkeys([*titled_dirs, own, *dir_names]))
    parsed = [enrich(text) for text in sources]

    evidence: LocalEvidence | None = None
    for text, attrs in zip(sources, parsed, strict=True):
        title = (attrs.titles_zh[0] if attrs.titles_zh else None) or (
            attrs.titles_en[0] if attrs.titles_en else None
        )
        # 短中文片名（实测《两生花》）在 NER 换代后可能整段漏抽。仅当这个
        # 干净分段也确实出现在文件自身名称里时，才把结构候选当查询词；这样
        # 「大陆/欧美」等纯分组目录不会凭空变成作品名。最终仍须通过 TMDB
        # 标题门槛与年份/时长证据验证，不会降低自动挂载标准。
        if title is None:
            fallback_titles = title_candidates(text, [])
            if fallback_titles and normalize_title(fallback_titles[0]) in normalize_title(own):
                title = fallback_titles[0]
        # "Title (Year)"惯例（Emby/TMM 整理过的目录名）：先剥掉 [tmdbid=N]
        # 这类方括号标记组再匹配，允许年份后还挂着画质等尾巴。500 库实测：
        # 惯例名是**高置信来源**——NER 面向脏乱种子名训练，对整理过的干净
        # 名字反而会截断（「知否知否应是绿肥红瘦」只抽出「绿肥红瘦」，
        # 「E.T.外星人」只剩「外星人」），截断词同年撞上别的条目就是静默
        # 错挂。因此两者并存且不同形时，惯例名当主查询词、NER 结果降为
        # 备选（收敛失败后换词重跑，混排名拆分的价值仍在）
        plain_title, plain_year = conventional_title(text) or (None, None)
        if plain_title and title and normalize_title(plain_title) != normalize_title(title):
            evidence = LocalEvidence(title=plain_title, year=plain_year, alt_title=title)
            break
        if title:
            evidence = LocalEvidence(title=title, year=attrs.year)
            if plain_year is not None and evidence.year is None:
                evidence.year = plain_year
            break
        # NER 抽不出时退回惯例名
        if plain_title:
            evidence = LocalEvidence(title=plain_title, year=plain_year)
            break
    if evidence is None:
        return None
    if kind is MediaKind.TV:
        seasons = [s for attrs in parsed for s in attrs.seasons if s > 0]
        episodes = [e for attrs in parsed for e in attrs.episodes]
        evidence.season = max(seasons) if seasons else None
        evidence.episode = max(episodes) if episodes else None
    return evidence


def local_episode_count(directory: Path) -> int | None:
    """目录里**去重后的集号个数**：同名双版本消歧的强证据。

    实测案例：《风筝》(2017) 在 TMDB 有正片（46 集）与「送审版」（51 集）
    两个同名同年条目，别名互相污染，标题门槛与年份都分不开——本地实际
    有多少集是唯一能一刀切开的物理证据（见 library_resolve 的裁决顺序）。

    只数直属本目录的视频文件；集号解析不出的不计；不足 2 集返回 None
    （单集目录零区分度，还容易被"只下了一集"误导）。
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    episodes: set[int] = set()
    for entry in entries:
        name = entry.name
        if entry.suffix.lower() not in SCAN_VIDEO_EXTS or not entry.is_file():
            continue
        if any(marker in name.lower() for marker in _IGNORE_MARKERS):
            continue
        attrs = enrich(entry.stem)
        episode = attrs.episodes[0] if attrs.episodes else trailing_index_episode(entry.stem)
        if episode:
            episodes.add(episode)
    return len(episodes) if len(episodes) >= 2 else None


def _unit_for(kind: MediaKind, file: Path) -> tuple[int, int]:
    """期望单元：电影 (0,0)；剧集优先信分集 NFO，其次走确定性季集解析层。

    证据优先级：
    1. 视频同名 NFO 的 <season>/<episode>——刮削器写盘的定位，最强证据；
    2. ``units.resolve_units``：显式 SxxEyy → 裸 E/EP 标记 → 季目录 → 模型，
       模型排在最后且带「季号 == 集号」幻觉指纹的去噪（见该模块 docstring）。
       NAS 实测《妻子的浪漫旅行》第 9 季文件名同时含「第一季」与 S09 两个季
       信号、整季错挂到第 1 季的病例，由「确定性来源压过模型」直接覆盖——
       S09 是显式标记、Season 9 是目录声明，模型的「第一季」根本进不了决赛。

    与监听导入的区别：这里逐文件走（库扫描按目录遍历，没有「包」的概念），
    不传条目名与台账；季号求解失败时回落 0（特别季）——``library_file``
    的季号列非空，扫描必须给出一个值。
    """
    if kind is MediaKind.MOVIE:
        return 0, 0
    episode_nfo = read_episode_metadata(file.with_suffix(".nfo"))
    if episode_nfo and episode_nfo.season is not None and episode_nfo.episode is not None:
        return episode_nfo.season, episode_nfo.episode
    unit = resolve_units([file])[file]
    return unit.season if unit.season is not None else 0, unit.episode


# ---------------------------------------------------------------------------
# 修正识别结果（条目详情页）第一阶段：重走识别链但只出结论、不写台账
# ---------------------------------------------------------------------------


@dataclass
class ReidentifyOutcome:
    """预览里一组文件的识别结论：命中某条目，或没命中（带原因与候选）。"""

    media_item_id: int | None = None
    tmdb_id: int | None = None
    title: str | None = None
    year: int | None = None
    poster_path: str | None = None
    source: str | None = None  # path_tag / nfo / resolved（命中时有值）
    reason: str | None = None  # 没命中时的中文原因
    code: str | None = None  # 没命中时的失败分类
    candidates: list[dict] = field(default_factory=list)


@dataclass
class ReidentifyGroup:
    """预览的一组：识别结论相同的文件聚在一起，用户按组拍板。

    按结论而非按文件分组，是这个面板能用的前提——一部剧几十集重跑出同一个
    结论，逐个确认既刷屏又折磨人；真出现"38 个文件归 A、2 个归 B"的分裂，
    分组恰好把它如实摆出来。
    """

    key: str
    outcome: ReidentifyOutcome
    file_ids: list[int] = field(default_factory=list)
    file_count: int = 0
    total_size_bytes: int = 0
    sample_names: list[str] = field(default_factory=list)  # 前几个文件名，认得出是哪些


@dataclass
class ReidentifyPreview:
    """一次「修正识别结果」的预览结论（不含任何写操作）。"""

    library_id: int
    media_item_id: int
    movie: bool
    groups: list[ReidentifyGroup] = field(default_factory=list)
    skipped_missing: int = 0  # missing 文件没有磁盘实体，不参与
    pinned_identity: bool = False  # 身份被目录名 tmdbid 标记/NFO 钉死
    unreachable: bool = False  # 有文件因 TMDB 不通而无结论：此刻别让用户拍板
    search_seed: str = ""  # 「都不对，我自己搜」的预填词（识别链解析出的片名）


async def preview_reidentify(
    session, library: Library, media_item_id: int, rows: list[LibraryFile]
) -> ReidentifyPreview:
    """重走识别链、按结论分组返回，**一行台账都不改**。

    与 ``reidentify_item`` 的分工：那个是"重跑并直接落库"（CLI/自动化的
    一键翻案入口）；这个是界面上「修正识别结果」的第一阶段。识别错挂时
    机器往往是**高置信地错**——同一条链、同样的输入，重跑大概率复现同一个
    错答案，所以结论必须先给人看、由人拍板，落库走人工认领通道
    （``claim_files``，身份记为 manual、顺带纠正矛盾的 NFO）。

    只读的边界说清楚：**不改任何 library_file**，用户取消 = 台账零改动。
    唯一的副作用是结论条目会在 ``media_item`` 建档（识别链要靠它拿标题与
    海报来展示），没被采纳的那些是孤儿条目，由既有的孤儿清理兜底。

    不占库级锁：纯读不会和扫描打架；结论过时了也无妨，拍板落库时以那一刻
    的台账为准。
    """
    kind = MediaKind(library.kind)
    assert library.id is not None
    preview = ReidentifyPreview(
        library_id=library.id, media_item_id=media_item_id, movie=kind is MediaKind.MOVIE
    )
    media_service = MediaLibraryService(session, get_tmdb_client(), scrape_library_id=library.id)
    resolve_cache: dict[tuple, tuple] = {}
    episodes_cache: dict[Path, int | None] = {}
    hints = await _load_hints(session)
    roots = [Path(p) for p in library.root_paths]
    grouped: dict[tuple[str, object], ReidentifyGroup] = {}

    for row in rows:
        if row.state != FileState.IN_PLACE:
            preview.skipped_missing += 1
            continue
        file = Path(row.file_path)
        # 文件所在的库根：识别链用它界定条目目录与 NFO 向上查找的边界
        root = next((r for r in roots if r in file.parents), file.parent)
        is_disc = row.container in ("bluray", "dvd")
        if pinned_tmdb_id(kind, root, file, is_disc=is_disc)[0] is not None:
            preview.pinned_identity = True
        if not preview.search_seed:
            evidence = guess_evidence(kind, root, file, is_disc=is_disc)
            preview.search_seed = evidence.title if evidence is not None else ""
        identified = await _identify(
            media_service,
            kind,
            root,
            file,
            resolve_cache,
            episodes_cache,
            duration_seconds=row.duration_seconds,
            hint=_hint_for(file, hints),
            is_disc=is_disc,
        )
        item = identified.item
        if item is None and identified.code == UnidentifiedCode.TMDB_UNREACHABLE:
            preview.unreachable = True
        key: tuple[str, object] = (
            ("item", item.id) if item is not None else ("none", identified.code or "")
        )
        group = grouped.get(key)
        if group is None:
            group = ReidentifyGroup(
                key=f"{key[0]}:{key[1]}",
                outcome=ReidentifyOutcome(
                    media_item_id=item.id if item is not None else None,
                    tmdb_id=item.tmdb_id if item is not None else None,
                    title=item.title if item is not None else None,
                    year=item.year if item is not None else None,
                    poster_path=item.poster_path if item is not None else None,
                    source=identified.source.value if identified.source is not None else None,
                    reason=identified.reason,
                    code=identified.code,
                    candidates=identified.candidates or [],
                ),
            )
            grouped[key] = group
        assert row.id is not None
        group.file_ids.append(row.id)
        group.file_count += 1
        group.total_size_bytes += row.size_bytes
        if len(group.sample_names) < 3:
            group.sample_names.append(file.name)

    # 文件多的组排前面：分裂时主结论先出现，零星文件不抢视线
    preview.groups = sorted(grouped.values(), key=lambda g: -g.file_count)
    logger.info(
        "媒体库 #%s 条目 #%s 修正识别结果预览：%d 个文件收敛为 %d 组结论%s",
        library.id,
        media_item_id,
        sum(g.file_count for g in preview.groups),
        len(preview.groups),
        "（身份由 tmdbid 标记/NFO 指定）" if preview.pinned_identity else "",
    )
    return preview


# ---------------------------------------------------------------------------
# 单条目重新识别（条目详情页）：识别链在升级，错挂的条目要有翻案通道
# ---------------------------------------------------------------------------


@dataclass
class ReidentifySummary:
    """一次单条目重识别的结论（同步返回给前端，用户当场看到结果）。"""

    library_id: int
    media_item_id: int  # 重识别前的身份锚
    total: int = 0  # 参与重识别的在位文件数
    identified: int = 0
    unidentified: int = 0
    skipped_missing: int = 0  # missing 文件没有磁盘实体，保持原身份不参与
    kept_on_error: int = 0  # TMDB 网络类失败：保留原身份的文件数（修好网络再重试）
    new_media_item_id: int | None = None  # 全部文件收敛到的新身份（分裂时为 None）
    new_title: str | None = None
    changed: bool = False  # 识别结果是否与原身份不同
    # 身份被显式钉死（目录名 tmdbid 标记或 NFO）：结果不满意得先改标记/NFO
    pinned_identity: bool = False
    errors: list[str] = field(default_factory=list)


async def reidentify_item(library_id: int, media_item_id: int) -> ReidentifySummary:
    """对某条目在库内的全部在位文件**重走完整识别链**（NFO → 名称解析 →
    TMDB 证据收敛），行原地更新。

    与「重新扫描」的区别：扫描只重试待识别的行，已识别的秒过；这里是
    用户对**已识别结果**不满意时的翻案通道——识别器随版本升级在变强，
    老的错挂应该有机会被纠正。识别失败的行会进待识别清单（原因照记），
    用户仍可人工认领，绝不静默保持错误身份。
    """
    summary = ReidentifySummary(library_id=library_id, media_item_id=media_item_id)
    from movieclaw_api.services.library.organize import is_organizing
    from movieclaw_api.services.library.transfer import is_transferring

    running = _scan_tasks.state_of(library_id)
    if running is not None:
        summary.errors.append(f"该库{PHASE_LABELS[running.phase]}，请等当前任务完成后再重新识别")
        return summary
    if is_organizing(library_id):
        summary.errors.append("该库正在整理文件名，请等整理完成后再重新识别")
        return summary
    if is_transferring(library_id):
        summary.errors.append("该库正在转移条目，请等转移完成后再重新识别")
        return summary
    # 与扫描互斥：重识别正在改身份锚，此刻扫描介入会打架。占的是同一把库级
    # 锁，但阶段标成 REIDENTIFYING——接口据此如实说"正在重新识别"，不会
    # 冒充扫描（也因此不会给出一个按了没用的"停止扫描"入口）
    state = ScanState(phase=ScanPhase.REIDENTIFYING)
    _scan_tasks.try_start(library_id, state)
    try:
        return await _reidentify(library_id, media_item_id, summary, state)
    except Exception:  # noqa: BLE001 -- 面向用户的操作，兜底转成可读错误
        logger.exception("媒体库 #%s 条目 #%s 重新识别时发生未知错误", library_id, media_item_id)
        summary.errors.append("重新识别中断：发生未知错误（详见后端日志）")
        return summary
    finally:
        _scan_tasks.finish(library_id)


async def _reidentify(
    library_id: int, media_item_id: int, summary: ReidentifySummary, state: ScanState
) -> ReidentifySummary:
    db = get_database()
    async with db.session() as session:
        library = await session.get(Library, library_id)
        if library is None:
            summary.errors.append("媒体库不存在（可能已被删除）")
            return summary
        repo = LibraryFileRepository(session)
        rows = [
            row
            for row in await repo.list_by_library(library_id)
            if row.media_item_id == media_item_id
        ]
        if not rows:
            summary.errors.append("该条目在本库已没有台账文件")
            return summary
        media_service = MediaLibraryService(
            session, get_tmdb_client(), scrape_library_id=library.id
        )
        kind = MediaKind(library.kind)
        resolve_cache: dict[tuple, tuple] = {}
        episodes_cache: dict[Path, int | None] = {}
        hints = await _load_hints(session)
        roots = [Path(p) for p in library.root_paths]

        state.total = len(rows)
        new_ids: set[int] = set()
        for done, row in enumerate(rows, start=1):
            state.processed = done
            if row.state != FileState.IN_PLACE:
                summary.skipped_missing += 1
                continue
            summary.total += 1
            file = Path(row.file_path)
            # 文件所在的库根：识别链用它界定条目目录与 NFO 向上查找的边界；
            # 根已被移出配置时退回父目录（仍能按文件名识别）
            root = next((r for r in roots if r in file.parents), file.parent)
            is_disc = row.container in ("bluray", "dvd")
            if pinned_tmdb_id(kind, root, file, is_disc=is_disc)[0] is not None:
                summary.pinned_identity = True
            identified = await _identify(
                media_service,
                kind,
                root,
                file,
                resolve_cache,
                episodes_cache,
                duration_seconds=row.duration_seconds,
                hint=_hint_for(file, hints),
                is_disc=is_disc,
            )
            item, reason = identified.item, identified.reason
            # 网络类失败（TMDB 不通）不冲掉现有身份——用户要的是"重新刮削"，
            # 不是"断网就清档"；修好网络再点一次即可。与"确实匹配不到"区分：
            # 后者才应该进待识别（不静默保持可疑身份）
            if item is None and identified.code == UnidentifiedCode.TMDB_UNREACHABLE:
                summary.kept_on_error += 1
                continue
            row.media_item_id = item.id if item is not None else None
            row.unidentified_reason = None if item is not None else reason
            row.unidentified_code = None if item is not None else identified.code
            row.unidentified_candidates = (
                None if item is not None else (identified.candidates or None)
            )
            row.identity_source = identified.source if item is not None else None
            row.resolved_version = RESOLVER_VERSION if item is not None else None
            row.review_suggestion = None  # 重识别就是用户主动翻案，旧建议随之失义
            if not is_disc:
                row.season_number, row.episode_number = _unit_for(kind, file)
            row.updated_at = utcnow()
            if item is not None:
                assert item.id is not None
                summary.identified += 1
                new_ids.add(item.id)
            else:
                summary.unidentified += 1
        await session.commit()

        if len(new_ids) == 1:
            new_id = next(iter(new_ids))
            summary.new_media_item_id = new_id
            summary.changed = new_id != media_item_id
            new_item = await session.get(MediaItem, new_id)
            summary.new_title = new_item.title if new_item is not None else None
            # 库存对账：单元归属的新条目若有订阅工单，在库成立即关闭
            from movieclaw_api.services.subscription import close_fulfilled_wanted

            await close_fulfilled_wanted(session, new_id)
        elif new_ids:
            # 分裂成多个条目（如剧集目录混入了别的剧）：不给单一跳转目标
            summary.changed = True
        await LibraryRepository(session).refresh_stats([library_id])
    logger.info(
        "媒体库 #%s 条目 #%s 重新识别完成：%d 个文件（识别 %d / 待识别 %d），新身份 %s%s",
        library_id,
        media_item_id,
        summary.total,
        summary.identified,
        summary.unidentified,
        summary.new_media_item_id,
        "（身份由 tmdbid 标记/NFO 指定）" if summary.pinned_identity else "",
    )
    # 改锚后的新条目补齐资产（图片 + 媒体目录镜像），后台执行不拖住响应
    if new_ids:
        from movieclaw_api.services.media_scrape import ensure_assets

        loop = asyncio.get_running_loop()
        for new_id in new_ids:
            loop.create_task(ensure_assets(new_id))
    return summary


# ---------------------------------------------------------------------------
# 定期对账（L3.4）：兜底巡检
# ---------------------------------------------------------------------------

# 对账节奏：低频兜底即可——新增/删除的即时感知靠手动扫描和目录监听
# （scan_library 本身已同时处理入账与 missing 标记），定时任务只兜底
# 监听失效（如网络挂载不产生 fs 事件）的场景
RECONCILE_INTERVAL_SECONDS = 6 * 3600


@register_task(
    "library_reconcile",
    title="媒体库对账",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=RECONCILE_INTERVAL_SECONDS,
    description=(
        "定期把媒体库台账与磁盘对齐：新文件增量入账，消失的文件标记 missing"
        "（默认只标记不删记录；开了「自动清理丢失记录」的库会把已确认丢失的"
        "记录清出台账）。兜底目录监听覆盖不到的场景。"
    ),
)
async def reconcile_libraries() -> None:
    db = get_database()
    async with db.session() as session:
        libraries = list((await session.execute(select(Library))).scalars().all())
    for library in libraries:
        assert library.id is not None
        # 对账负责让台账追上文件新增/删除；规格补探是可长达数小时的用户主动
        # 维护任务，不能在空闲后台周期里抢占媒体盘。
        summary = await scan_library(library.id, backfill_existing_specs=False)
        if summary.errors:
            logger.warning(
                "媒体库「%s」对账补扫存在问题：%s", library.name, "；".join(summary.errors)
            )
