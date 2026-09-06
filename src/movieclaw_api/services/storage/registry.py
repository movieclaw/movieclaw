"""运行期数据目录登记表（docs/design/cache-management.md §3）。

data/ 下的每一个目录都在 ``DATA_DIRS`` 里有且仅有一条登记：它是什么、能不能删、
删了会怎样、怎样判断一个条目是孤儿或正在被使用。缓存管理面板、清理接口、
启动期的「未登记目录」告警，以及 CI 守卫测试（tests/api/test_storage.py）
全部只读这一张表——**业务功能新增一种落盘产物时，只需要在这里加一条**，
面板与清理逻辑不用动。

维护规范（三条守卫保证不漏）：

1. 源码里不允许手写 ``data/...`` 字面量：路径一律在 core/config.py 声明成
   Settings 字段，登记表通过 ``resolve`` 从配置取路径。CI 守卫会扫描 src/ 里
   所有 ``data/`` 字面量，每一个都必须被某条登记的 ``default`` 前缀覆盖。
2. 登记项之间不允许嵌套（一个目录只能被统计一次），守卫测试断言。
3. 运行时兜底：启动时与面板加载时都会列出 data/ 根下没有任何登记覆盖的条目
   并告警——即使有人绕过前两条守卫，管理员也能在面板上看到「未登记目录」。

清理动作只删登记目录**里面的条目**（直接子项），不删目录本身；因此生产方
写入前照常 ``mkdir(parents=True, exist_ok=True)`` 即可，不需要感知清理。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from movieclaw_api.core.config import Settings, get_settings


class Group(StrEnum):
    """面板分组：cache = 派生物（可清理）；data = 用户数据/系统状态（只展示）。"""

    CACHE = "cache"
    DATA = "data"


class RebuildCost(StrEnum):
    """删掉之后重建的代价——面板据此措辞并决定是否二次确认。"""

    #: 不需要重建，或下次访问自动重建、几乎无感（图片回源、字幕抽轨）
    CHEAP = "cheap"
    #: 重建要跑重活（通读整部片抽帧）或消耗外网配额，面板要明确警示
    EXPENSIVE = "expensive"
    #: 用户资产或系统状态，没有「重建」一说
    NONE = "none"


#: 批量探测：输入登记目录的直接子项列表，返回其中「是孤儿」或「正在使用」的子集。
#: 用批量而不是逐项谓词，是为了一次数据库查询覆盖整目录（几千个条目不该查几千次）。
EntryProbe = Callable[[list[Path]], Awaitable[set[Path]]]


@dataclass(frozen=True)
class DataDir:
    """一条目录登记。

    - ``key``：稳定标识，接口与前端用它指代目录，改名字不改 key；
    - ``summary`` 行内一句话，``description`` 完整说明（悬停与确认时展示）；
    - ``default``：相对项目根的默认路径（如 ``data/cache/images``），CI 守卫用它
      做前缀匹配；``resolve`` 才是运行期真正的位置（用户可能用环境变量改到别处）；
    - ``clearable``：是否允许「全部清空」；``orphans`` 非空即提供「清理孤儿」；
    - ``busy``：正在被使用、任何清理都必须跳过的条目（活跃转码会话、运行中的
      字幕任务断点、生成到一半的 staging 目录）。
    """

    key: str
    title: str
    #: 一句话用途（面板行内显示，十几个字）；``description`` 是完整说明与清理后果
    summary: str
    description: str
    default: str
    resolve: Callable[[Settings], Path]
    group: Group = Group.DATA
    rebuild_cost: RebuildCost = RebuildCost.NONE
    clearable: bool = False
    orphans: EntryProbe | None = None
    busy: EntryProbe | None = None

    @property
    def orphan_aware(self) -> bool:
        return self.orphans is not None


# ---------------------------------------------------------------------------
# 探测函数（数据库/内存状态查询都放这里，登记项本身保持声明式）
# ---------------------------------------------------------------------------


def _leading_int(name: str) -> int | None:
    """条目名开头的整数：``123``、``123.zh.checkpoint.json``、``123.s2.srt`` 都取 123。"""
    head = name.split(".", 1)[0]
    return int(head) if head.isdigit() else None


async def _existing_ids(model_name: str) -> set[int]:
    """取某张表的全部主键（库文件/媒体条目量级为万，内存集合足够）。"""
    from sqlmodel import select

    from movieclaw_db import models
    from movieclaw_db.engine import get_database

    model = getattr(models, model_name)
    async with get_database().session() as session:
        rows = await session.exec(select(model.id))
        return {int(i) for i in rows.all() if i is not None}


def _orphans_by_id(model_name: str) -> EntryProbe:
    """按「条目名前缀整数 ∉ 表主键」判孤儿；名字不是整数开头的条目不动。"""

    async def probe(entries: list[Path]) -> set[Path]:
        ids = await _existing_ids(model_name)
        return {e for e in entries if (i := _leading_int(e.name)) is not None and i not in ids}

    return probe


async def _staging_dirs(entries: list[Path]) -> set[Path]:
    """生成到一半的 staging（``.<name>.<uuid>.part``）：正在写，不能碰。"""
    return {e for e in entries if e.name.endswith(".part")}


async def _fonts_with_staging(entries: list[Path]) -> set[Path]:
    """字体附件按 ``fonts/<file_id>/`` 落盘，抽取中的 staging 在它下面：有 staging
    就整个 ``fonts/`` 跳过，避免删掉正在写的目录让半套字体被当成完整结果。"""
    busy: set[Path] = set()
    for e in entries:
        if e.name == "fonts" and any(c.name.endswith(".part") for c in _children(e)):
            busy.add(e)
    return busy


def _children(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except OSError:
        return []


async def _active_transcode_sessions(entries: list[Path]) -> set[Path]:
    """正在播放的转码会话目录（删了正在看的人立刻黑屏）。"""
    from movieclaw_api.services.playback.session import get_session_manager

    live = {s.id for s in get_session_manager().active()}
    return {e for e in entries if e.name in live}


async def _running_subtitle_jobs(entries: list[Path]) -> set[Path]:
    """运行中的 AI 字幕任务所属文件的中间品：断点删了任务要从头翻译。"""
    from sqlmodel import select

    from movieclaw_db.engine import get_database
    from movieclaw_db.models import ACTIVE_JOB_STATUSES, Job, JobResource

    async with get_database().session() as session:
        rows = await session.exec(
            select(JobResource.resource_id)
            .join(Job, Job.id == JobResource.job_id)
            .where(
                Job.job_type == "subtitle.generate",
                Job.status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
                JobResource.resource_type == "library_file",
            )
        )
        running = {int(r) for r in rows.all() if str(r).isdigit()}
    return {e for e in entries if _leading_int(e.name) in running}


# ---------------------------------------------------------------------------
# 登记表本体——新增 data/ 下的目录只改这里
# ---------------------------------------------------------------------------

DATA_DIRS: tuple[DataDir, ...] = (
    # ---- 派生物缓存：随时可删，按需重建 -------------------------------------
    DataDir(
        key="cache.images",
        title="图片缓存",
        summary="远程图片的本地副本与缩略图",
        description=(
            "海报、剧照、站点图片等远程图片的本地副本与缩略图（含 Jellyfin 客户端的"
            "缩放变体）。超过容量上限会自动淘汰最久未访问的条目；清空后下次访问重新"
            "从外网下载。"
        ),
        default="data/cache/images",
        resolve=lambda s: Path(s.image_cache_dir),
        group=Group.CACHE,
        rebuild_cost=RebuildCost.CHEAP,
        clearable=True,
    ),
    DataDir(
        key="cache.playback_subs",
        title="播放字幕缓存",
        summary="播放器抽取的内封字幕与字体",
        description=(
            "网页播放器从视频内封轨抽取出来的字幕与字体文件。清空后下次播放会重新"
            "抽取，首次开播稍慢几秒。"
        ),
        default="data/cache/playback-subs",
        resolve=lambda s: Path(s.playback_subs_cache_dir),
        group=Group.CACHE,
        rebuild_cost=RebuildCost.CHEAP,
        clearable=True,
        orphans=_orphans_by_id("LibraryFile"),
        busy=_fonts_with_staging,
    ),
    DataDir(
        key="cache.subtitle_gen",
        title="AI 字幕中间品",
        summary="AI 字幕生成的抽取产物与翻译断点",
        description=(
            "AI 字幕生成过程中的抽取产物、PGS 图片与翻译断点。正在运行的字幕任务"
            "所属文件会被跳过；已完成任务的中间品可放心清理。"
        ),
        default="data/cache/subtitle_gen",
        resolve=lambda s: Path(s.subtitle_gen_cache_dir),
        group=Group.CACHE,
        rebuild_cost=RebuildCost.CHEAP,
        clearable=True,
        orphans=_orphans_by_id("LibraryFile"),
        busy=_running_subtitle_jobs,
    ),
    DataDir(
        key="cache.trickplay",
        title="进度条预览图",
        summary="播放器拖动进度条的缩略图",
        description=(
            "网页播放器拖动进度条时显示的缩略图。重新生成需要通读整部影片抽帧，"
            "建议只清理媒体库里已不存在的文件对应的孤儿目录。"
        ),
        default="data/cache/playback-trickplay",
        resolve=lambda s: Path(s.trickplay_cache_dir),
        group=Group.CACHE,
        rebuild_cost=RebuildCost.EXPENSIVE,
        clearable=True,
        orphans=_orphans_by_id("LibraryFile"),
        busy=_staging_dirs,
    ),
    DataDir(
        key="transcodes",
        title="转码分片",
        summary="网页播放器实时转码的 HLS 分片",
        description=(
            "网页播放器实时转码产生的 HLS 分片，会话结束即删、重启时清残留，正常"
            "情况下不应有大量占用。正在播放的会话会被跳过。"
        ),
        default="data/transcodes",
        resolve=lambda s: Path(s.transcode_dir),
        group=Group.CACHE,
        rebuild_cost=RebuildCost.CHEAP,
        clearable=True,
        busy=_active_transcode_sessions,
    ),
    DataDir(
        key="metadata.covers",
        title="媒体库封面",
        summary="服务端渲染的媒体库封面拼贴",
        description="服务端渲染的媒体库封面拼贴，清空后下次访问自动重新渲染。",
        default="data/metadata/library-covers",
        resolve=lambda s: Path(s.metadata_dir) / "library-covers",
        group=Group.CACHE,
        rebuild_cost=RebuildCost.CHEAP,
        clearable=True,
    ),
    DataDir(
        key="metadata.images",
        title="刮削图片资产",
        summary="刮削下载的海报、背景与剧照",
        description=(
            "刮削下载的海报、背景与剧照，是媒体库展示的事实源。整体重建等于整库"
            "刷新元数据（大量外网流量并受 TMDB 限速），因此只提供清理孤儿条目。"
        ),
        default="data/metadata/images",
        resolve=lambda s: Path(s.metadata_dir) / "images",
        group=Group.CACHE,
        rebuild_cost=RebuildCost.EXPENSIVE,
        clearable=False,
        orphans=_orphans_by_id("MediaItem"),
    ),
    # ---- 用户数据与系统状态：只展示占用，面板不提供删除 -----------------------
    DataDir(
        key="database",
        title="数据库",
        summary="SQLite 主库与 WAL 日志",
        description="SQLite 主库（含 WAL 日志），所有配置、媒体库台账与观看记录。",
        default="data/movieclaw.db",
        resolve=lambda s: _sqlite_path(s),
    ),
    DataDir(
        key="logs",
        title="运行日志",
        summary="按天写入的后端日志，30 天自动轮转",
        description="按天写入的后端日志，超过保留天数自动删除（LOG_RETENTION_DAYS，默认 30 天）。",
        default="data/logs",
        resolve=lambda s: Path(s.log_dir),
    ),
    DataDir(
        key="uploads",
        title="上传文件",
        summary="成员头像与首页背景图",
        description="成员头像与首页背景图库，用户上传的原件，无法重建。",
        default="data/uploads",
        resolve=lambda s: Path(s.media_dir),
    ),
    DataDir(
        key="updates",
        title="应用更新",
        summary="应用内更新的版本代码与数据库备份",
        description=(
            "应用内更新下载的版本代码、启动状态标记与更新前的数据库自动备份。"
            "版本数量在「版本与更新」标签的「本地保留版本数」里调整；备份是回退时"
            "恢复数据的来源，不提供删除。"
        ),
        default="data/updates",
        resolve=lambda s: Path(s.updates_dir),
    ),
    DataDir(
        key="models",
        title="模型文件",
        summary="NER 模型与语音检测模型",
        description="种子命名识别（NER）模型与字幕同步用的语音检测模型，由应用内更新维护。",
        default="data/models",
        resolve=lambda s: Path(s.data_dir) / "models",
    ),
    DataDir(
        key="site_configs",
        title="站点配置",
        summary="用户自行适配的站点 YAML",
        description="用户自行适配的站点 YAML，无法重建。",
        default="data/site-configs",
        resolve=lambda s: Path(s.site_configs_dir),
    ),
    DataDir(
        key="agent.workspace",
        title="Agent 工作区",
        summary="Agent 文件操作的工作目录",
        description="Agent 执行文件操作的工作目录，可能包含它替你生成的文件。",
        default="data/agent-workspace",
        resolve=lambda s: Path(s.agent_workspace_dir),
    ),
    DataDir(
        key="agent.sessions",
        title="Agent 会话记录",
        summary="Agent 对话转录与附件",
        description="Agent 对话转录与附件，是会话历史的事实源，删除即丢失对话。",
        default="data/agent-sessions",
        resolve=lambda s: Path(s.agent_sessions_dir),
    ),
    DataDir(
        key="agent.skills",
        title="Agent 技能",
        summary="管理员放入的自定义技能",
        description="管理员放入的自定义 Agent 技能。",
        default="data/agent-skills",
        resolve=lambda s: Path(s.agent_skills_dir),
    ),
    DataDir(
        key="config",
        title="应用配置文件",
        summary="对外端口等数据库之外的设置文件",
        description="对外端口等需要在数据库之外读取的设置文件。",
        default="data/config",
        resolve=lambda s: Path(s.web_port_file).parent,
    ),
    DataDir(
        key="secret_key",
        title="配置加密主密钥",
        summary="保护敏感配置的主密钥",
        description="保护站点凭据等敏感配置的主密钥，删除后所有密文永久无法恢复。",
        default="data/.secret_key",
        resolve=lambda s: Path(s.secret_key_file),
    ),
)


def _sqlite_path(settings: Settings) -> Path:
    """从 DATABASE_URL 取 SQLite 文件路径；非 SQLite 时返回一个不存在的占位路径。"""
    url = settings.database_url
    marker = "sqlite+aiosqlite:///"
    if not url.startswith(marker):
        return Path(settings.data_dir) / "movieclaw.db"
    return Path(url[len(marker) :])


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------


def data_root(settings: Settings | None = None) -> Path:
    return Path((settings or get_settings()).data_dir)


def find(key: str) -> DataDir | None:
    return next((d for d in DATA_DIRS if d.key == key), None)


def resolved(settings: Settings | None = None) -> list[tuple[DataDir, Path]]:
    """登记项 → 运行期绝对路径（未 resolve 的相对路径以进程 cwd 为基准，与各生产方一致）。"""
    settings = settings or get_settings()
    return [(d, d.resolve(settings).resolve()) for d in DATA_DIRS]


def sqlite_sidecars(path: Path) -> list[Path]:
    """SQLite 的 -wal/-shm 伴生文件：数据库条目的体积要把它们算进去。"""
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]


def unregistered_entries(settings: Settings | None = None) -> list[Path]:
    """data/ 根下没有任何登记覆盖的条目（运行时兜底守卫）。

    登记路径可能位于根的深层（``data/cache/images``），因此沿途的祖先目录
    （``data/cache``）被视为「容器」：只往下继续检查它的子项，本身不算未登记。
    数据库的 -wal/-shm 伴生文件视作已登记。
    """
    settings = settings or get_settings()
    root = data_root(settings).resolve()
    registered = {p for _d, p in resolved(settings)}
    for d, p in resolved(settings):
        if d.key == "database":
            registered.update(x.resolve() for x in sqlite_sidecars(p))
    containers = {
        anc for p in registered for anc in p.parents if anc == root or root in anc.parents
    }

    unknown: list[Path] = []

    def walk(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        for child in children:
            resolved_child = child.resolve()
            if resolved_child in registered:
                continue
            if resolved_child in containers:
                walk(child)
                continue
            unknown.append(child)

    if root.is_dir():
        walk(root)
    return unknown


def iter_data_literals(source: str) -> Iterable[str]:
    """从一段源码里抽出所有 ``data/...`` 路径字面量（CI 守卫用）。"""
    import re

    for match in re.finditer(r"""["'](?:\./)?(data/[A-Za-z0-9_./\-]*)["']""", source):
        yield match.group(1).rstrip("/")


def covers(literal: str) -> bool:
    """某个 ``data/...`` 字面量是否被登记表覆盖。

    覆盖的两种情形：位于某条登记目录之下（``data/cache/images/ab``），或本身是
    登记目录的祖先容器（``data/metadata`` 之下的 images/ 与 library-covers/ 分别
    登记，配置里的 ``data/metadata`` 字面量因此合法）。祖先容器下若再长出新的
    子目录，会被运行时的 ``unregistered_entries`` 兜底发现。
    """
    normalized = literal.rstrip("/")
    for d in DATA_DIRS:
        default = d.default.rstrip("/")
        if normalized == default or normalized.startswith(default + "/"):
            return True
        if default.startswith(normalized + "/"):
            return True
    return False


__all__ = [
    "DATA_DIRS",
    "DataDir",
    "EntryProbe",
    "Group",
    "RebuildCost",
    "covers",
    "data_root",
    "find",
    "iter_data_literals",
    "resolved",
    "sqlite_sidecars",
    "unregistered_entries",
]
