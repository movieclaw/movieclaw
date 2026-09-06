"""订阅链路体检：对每个库预演「投递 → 转移 → 入库」全链路，联合约束一次亮清。

为什么放在订阅名下（用户决策 2026-07-27）：下载器路径映射、监听导入、库根
路径分散在三个配置页，各自保存时都有前置校验，但**正确性是联合约束**
（映射要覆盖投递目录、硬链要求源目录与库主根同盘、投递目录又取决于库有
没有监听规则）。用户真正的问题是"订阅后能不能自动下载并入库"——体检以
这个问题为锚，把每个库的链路逐段陈述事实，红项给修复去处。

口径同源原则：判定全部复用真实投递/搬运用的同一批原语（library_routing.
resolve_save_path 的三级兜底、torrent_submit.mapping_covers 的映射覆盖、
同盘检测的 st_dev 比对），不另写一套"体检版"逻辑——否则迟早出现
"体检说好、投递却挂"。

三档结论：
- ok：这一段没有问题；
- warn：能转但降级（如 watchdog 缺失退化为每小时兜底巡检、目录未就绪
  暂无法检测）——不阻断入库，值得知道；
- error：投递会被拒绝或内容不会自动入库，必须修。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models import ImportWatch, Library
from movieclaw_db.models.downloader_client import DownloaderClient
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.repositories.library_repo import LibraryRepository

# 状态严重度：聚合时取最坏
_SEVERITY = {"ok": 0, "warn": 1, "error": 2}


@dataclass
class HealthCheck:
    """链路上的一段检查结论。"""

    key: str  # downloader / mapping / transfer_disk / watch_active / ingest
    label: str  # 段落名（前端左列）
    status: str  # ok / warn / error
    detail: str  # 中文事实陈述（前端右列，直接展示）
    fix_section: str | None = None  # 修复去处：settings 分区 id 或 "libraries"


@dataclass
class LibraryPipeline:
    """一个库的完整链路结论。"""

    library_id: int
    library_name: str
    kind: str
    is_default: bool
    mode: str  # watch / inplace / downloader_default
    path: str | None  # 投递基底目录（movieclaw 视角）
    library_root: str | None  # 库主根（入库节点的落点展示）
    status: str  # 全链路最坏状态
    # 命中自定义目录规则时的整理落点（strm-workflow）：非空时转移段落到
    # 该目录、之后是"外部流转"节点，入账靠库根扫描收尾
    staging_path: str | None = None
    narrative: str = ""  # 「订阅命中本库会发生什么」的一句话叙事（全绿时的正向可预期）
    checks: list[HealthCheck] = field(default_factory=list)


@dataclass
class FixOption:
    """修复卡里的一个可选修法：做什么 / 为什么 / 跳到哪。

    fix_section 沿用 HealthCheck 的词表（前端负责映射到路由），fix_params
    是跳转要携带的预填参数（如建议的映射路径）——目标设置页读取后自动
    填好表单，用户不必凭记忆誊写路径。
    """

    title: str  # 选项标题（如「补一条公共父目录映射」）
    why: str  # 为什么这么做 / 适合谁（帮用户二选一）
    steps: str  # 具体做什么，含建议值
    fix_section: str  # 修复去处（sites / downloaders / import-watch / libraries）
    fix_label: str  # 跳转按钮文案
    fix_params: dict[str, str] | None = None  # 跳转预填参数


@dataclass
class HealthIssue:
    """按根因聚合的问题卡：一个根因 = 一张卡，不随受影响的库数膨胀。

    体检按库逐条报告事实（``LibraryPipeline.checks``，保持不变），但同一个
    根因（如下载器映射没覆盖媒体库）会在每个库上各亮一条红项——用户看到
    的是 N 个问题，实际要修的只有一处。这里把红黄项按根因归并，给出
    受影响的库和结构化的修复选项，前端置顶展示为「需要处理 N 件事」。
    ``key`` 与被归并检查项的 ``HealthCheck.key`` 一致，前端据此在库卡片里
    隐藏已被聚合的红项文字、只保留状态点。
    """

    key: str  # 与聚合来源的 HealthCheck.key 同词表
    status: str  # error / warn
    title: str  # 一句话根因
    detail: str  # 根因的事实陈述与后果
    affected_libraries: list[str] = field(default_factory=list)
    options: list[FixOption] = field(default_factory=list)


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _SEVERITY.get(s, 0)) if statuses else "ok"


async def _default_downloader(session: AsyncSession) -> DownloaderClient | None:
    """可用的默认下载器（与投递预检同一查询口径）。"""
    result = await session.execute(
        select(DownloaderClient).where(
            DownloaderClient.is_default.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.enabled.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.status == ConfigStatus.ACTIVE,
        )
    )
    return result.scalars().first()


async def _site_check(session: AsyncSession) -> tuple[HealthCheck, bool]:
    """资源搜索段（全局，链路第一环）。

    返回 (检查结论, 是否配置过站点)。"配置过"与"当前可用"必须分开：
    前者决定前端展示开局清单（从未配置）还是体检红项（配了但坏了，如
    cookie 过期）——老用户的站点失效不该被当成新手对待。
    """
    from movieclaw_db.models import SiteCredential

    rows = list((await session.execute(select(SiteCredential))).scalars().all())
    active = [r for r in rows if r.enabled and r.status == ConfigStatus.ACTIVE]
    if active:
        return (
            HealthCheck(
                key="sites",
                label="资源搜索",
                status="ok",
                detail=f"已接入 {len(active)} 个可用站点",
            ),
            True,
        )
    detail = (
        "没有可用的资源站点——订阅搜不到任何资源"
        if not rows
        else "已配置的站点当前都不可用（验证失败或已停用）——订阅搜不到任何资源"
    )
    return (
        HealthCheck(
            key="sites", label="资源搜索", status="error", detail=detail, fix_section="sites"
        ),
        bool(rows),
    )


def _watched_dirs() -> frozenset[str]:
    """实时监听中的源目录集合；监听器未启动（如测试环境）返回空集。"""
    from movieclaw_api.services.library.ingest import get_ingest_watcher

    watcher = get_ingest_watcher()
    return watcher.watched_keys() if watcher is not None else frozenset()


def _check_transfer(
    rule: ImportWatch, library: Library, watched: frozenset[str]
) -> list[HealthCheck]:
    """watch 模式的「转移」段：硬链同盘 + 监听是否实际生效。

    自定义目录规则的整理落点是 ``target_path`` 而非库主根——同盘检测的
    锚点跟着落点走（口径与真实搬运同源）。
    """
    checks: list[HealthCheck] = []
    # 硬链同盘的锚点 = 整理落点：自定义目录规则为 target_path，否则库主根
    anchor = rule.target_path or library.primary_root
    anchor_label = "整理目标目录" if rule.target_path else "库主根"
    if rule.strategy == "hardlink" and anchor:
        try:
            same = os.stat(rule.source_path).st_dev == os.stat(anchor).st_dev
        except OSError:
            checks.append(
                HealthCheck(
                    key="transfer_disk",
                    label="硬链接同盘",
                    status="warn",
                    detail=(
                        f"目录暂不可达（{rule.source_path} 或{anchor_label}未挂载），无法检测；"
                        "挂载就绪后重新体检"
                    ),
                    fix_section="import-watch",
                )
            )
        else:
            if same:
                checks.append(
                    HealthCheck(
                        key="transfer_disk",
                        label="硬链接同盘",
                        status="ok",
                        detail=f"源目录与{anchor_label}在同一文件系统，硬链接零占用可用",
                    )
                )
            else:
                target_text = (
                    f"整理目标目录 {rule.target_path}"
                    if rule.target_path
                    else f"「{library.name}」的主根"
                )
                checks.append(
                    HealthCheck(
                        key="transfer_disk",
                        label="硬链接同盘",
                        status="error",
                        detail=(
                            f"源目录与{target_text}不在同一文件系统，"
                            "硬链接会失败——请把监听导入规则的策略改为「复制」，"
                            "或调整存储布局"
                        ),
                        fix_section="import-watch",
                    )
                )
    if rule.source_path in watched:
        checks.append(
            HealthCheck(
                key="watch_active",
                label="目录监听",
                status="ok",
                detail="源目录正在实时监听，下载完成即处理",
            )
        )
    else:
        checks.append(
            HealthCheck(
                key="watch_active",
                label="目录监听",
                status="warn",
                detail=(
                    "源目录未在实时监听（watchdog 缺失或目录未就绪），"
                    "由每小时的兜底巡检接手——能入库，但不及时"
                ),
                fix_section="import-watch",
            )
        )
    return checks


def _narrative(mode: str, base: str | None, library: Library, rule: ImportWatch | None) -> str:
    """「订阅命中本库会发生什么」的一句话叙事。

    体检的红黄项讲"哪里会坏"，这句话讲"正常时会发生什么"——把模拟一单
    的结论常驻到每个库卡片上，全绿时用户也能对订阅后的行为有明确预期。
    """
    root = library.primary_root
    if mode == "watch" and rule is not None and rule.target_path:
        verb = "硬链接" if rule.strategy == "hardlink" else "复制"
        tail = (
            f"；文件后续出现在 {root} 时自动扫描入账" if root else "；库没有根路径，回流后无法入账"
        )
        return (
            f"订阅命中本库的影片会投递到 {base}，下载完成后自动识别改名并{verb}到 "
            f"{rule.target_path}（不直接入库，外部流转由你的工具完成）{tail}。"
        )
    if mode == "watch" and rule is not None:
        if not root:
            return f"投递到监听导入目录 {base}，但库没有根路径，下载完成后无法入库。"
        if rule.strategy == "hardlink":
            return (
                f"订阅命中本库的影片会投递到 {base}，下载完成后自动识别并硬链接进 "
                f"{root} 的「片名 (年份)」目录——源文件留在下载目录继续做种，删种不删片。"
            )
        return (
            f"订阅命中本库的影片会投递到 {base}，下载完成后自动识别并复制进 "
            f"{root} 的「片名 (年份)」目录——源文件保留在下载目录。"
        )
    if mode == "inplace":
        return (
            f"订阅命中本库的影片会以「片名 (年份)」目录直接下载进 {base}，"
            "下载完成即在库内，扫描自动入账。"
        )
    return "库没有根路径：下载会落到下载器默认目录，不会自动入库。"


# 修复去处 → 默认跳转按钮文案（与前端 fixTarget 的词表一致）
_FIX_LABELS = {
    "sites": "去接入站点",
    "downloaders": "去下载器设置",
    "import-watch": "去监听导入",
    "libraries": "去媒体库",
}


def _aggregate_issues(
    pipelines: list[LibraryPipeline],
    site_check: HealthCheck,
    downloader: DownloaderClient | None,
    common_root: str | None,
) -> list[HealthIssue]:
    """把逐库的红黄项按根因归并成修复卡（error 在前，warn 在后）。

    专门聚合的两类：全局段（站点/下载器，本就与库无关）与路径映射
    （同一个根因逐库开花的重灾区，给出二选一的结构化修法）；其余红黄项
    按（key, detail）归并——detail 相同即同一根因，受影响库合并列出。
    """
    issues: list[HealthIssue] = []

    if site_check.status != "ok":
        issues.append(
            HealthIssue(
                key="sites",
                status=site_check.status,
                title="资源站点不可用",
                detail=site_check.detail,
                options=[
                    FixOption(
                        title="接入或修复资源站点",
                        why="订阅的一切从站点搜索开始，没有可用站点就搜不到任何资源。",
                        steps="到「资源站点」接入站点，或修复失效的登录态（如更新 Cookie）。",
                        fix_section="sites",
                        fix_label=_FIX_LABELS["sites"],
                    )
                ],
            )
        )

    if downloader is None:
        issues.append(
            HealthIssue(
                key="downloader",
                status="error",
                title="没有可用的默认下载器",
                detail="订阅只能记录想要的内容，无法真实下载。",
                options=[
                    FixOption(
                        title="接入下载器并设为默认",
                        why="找到的资源要交给 qBittorrent / Transmission 完成下载。",
                        steps="到「下载器」添加实例，确保连接测试通过并设为默认。",
                        fix_section="downloaders",
                        fix_label=_FIX_LABELS["downloaders"],
                    )
                ],
            )
        )

    # —— 路径映射：同一根因逐库开花的重灾区，归并成一张二选一的修复卡
    mapping_hits = [
        (p, c) for p in pipelines for c in p.checks if c.key == "mapping" and c.status != "ok"
    ]
    if mapping_hits and downloader is not None:
        names = [p.library_name for p, _ in mapping_hits]
        # 受影响的类型（去重保序）：监听导入选项据此建议建几条 auto 规则
        kinds = list(dict.fromkeys(p.kind for p, _ in mapping_hits))
        kind_labels = "、".join("电影" if k == "movie" else "剧集" for k in kinds)
        anchor = common_root or mapping_hits[0][0].path or ""
        issues.append(
            HealthIssue(
                key="mapping",
                status="error",
                title=f"下载器「{downloader.name}」的路径映射没有覆盖媒体库目录",
                detail=(
                    f"{len(names)} 个库的投递目录都不在映射覆盖范围内——这是同一个"
                    "原因，修一处即可，不需要逐库配置。修复前投递会被拒绝"
                    "（工单不会丢，修好后自动重试）。"
                ),
                affected_libraries=names,
                options=[
                    FixOption(
                        title="补一条公共父目录映射（改动最小）",
                        why=(
                            "映射按目录前缀覆盖：一条公共父目录的映射即可覆盖其下"
                            "所有库。适合下载器能直接访问媒体库目录、愿意让下载"
                            "文件直接落在库内的部署。"
                        ),
                        steps=(
                            f"添加映射：本机 {anchor} → 下载器视角的对应路径"
                            "（下载器可直达同名路径时，两边填相同的即可）。"
                        ),
                        fix_section="downloaders",
                        fix_label="去补映射",
                        fix_params={"suggest_mapping": anchor} if anchor else None,
                    ),
                    FixOption(
                        title="改用监听导入（下载区与库分离）",
                        why=(
                            "投递改走独立的下载目录，映射只需覆盖下载目录，媒体库"
                            "分得再细也不影响下载器配置；下载完成后硬链接/复制进库，"
                            "源文件继续做种、删种不删片——PT 保种推荐。"
                        ),
                        steps=(
                            f"为{kind_labels}各建一条「自动路由」规则，源目录选"
                            "下载器的下载目录（不能在媒体库根路径之下）。"
                        ),
                        fix_section="import-watch",
                        fix_label="去建规则",
                        fix_params={"suggest": "auto", "kinds": ",".join(kinds)},
                    ),
                ],
            )
        )

    # —— 其余红黄项：detail 相同即同一根因，受影响库合并成一张单选项卡。
    # 标题按 (key, status) 给人话版本，兜底退回检查项的段落名
    titles = {
        ("dispatch_dir", "error"): "媒体库没有根路径，无法自动入库",
        ("transfer_disk", "error"): "硬链接无法跨文件系统工作",
        ("transfer_disk", "warn"): "硬链接同盘暂时无法检测",
        ("watch_active", "warn"): "目录监听未生效，入库会不及时",
    }
    grouped: dict[tuple[str, str], list[tuple[LibraryPipeline, HealthCheck]]] = {}
    for pipeline in pipelines:
        for check in pipeline.checks:
            if check.status == "ok" or check.key in ("mapping", "downloader"):
                continue
            grouped.setdefault((check.key, check.detail), []).append((pipeline, check))
    for (key, detail), hits in grouped.items():
        check = hits[0][1]
        section = check.fix_section or "libraries"
        issues.append(
            HealthIssue(
                key=key,
                status=check.status,
                title=titles.get((key, check.status), check.label),
                detail=detail,
                affected_libraries=[p.library_name for p, _ in hits],
                options=[
                    FixOption(
                        title=f"处理「{check.label}」",
                        why="",
                        steps=detail,
                        fix_section=section,
                        fix_label=_FIX_LABELS.get(section, "去处理"),
                    )
                ],
            )
        )

    issues.sort(key=lambda i: -_SEVERITY.get(i.status, 0))
    return issues


async def pipeline_health(session: AsyncSession) -> dict:
    """全部库的链路体检。返回 dict（路由层直接进响应模型）。"""
    from movieclaw_api.services.library.profile import profile_of
    from movieclaw_api.services.library.routing import resolve_save_path
    from movieclaw_api.services.torrent_submit import mapping_covers

    downloader = await _default_downloader(session)
    site_check, sites_configured = await _site_check(session)
    # 下载器同理区分"配置过"与"当前可用"（验证中/失效 ≠ 从未接入）
    downloaders_configured = (
        await session.execute(select(DownloaderClient))
    ).scalars().first() is not None
    watched = _watched_dirs()
    # 只演练能作为订阅目标的库（能力位 subscribable，不按 kind 字面分叉）：
    # 图片/其他这类本地内容库不会被订阅命中——路由只在同 kind 的影视库里选，
    # 订阅侧也拒绝把它们设为入库目标——它们的根路径没配进下载器映射是常态，
    # 拿来演练投递链路只会报出一条永远修不掉也不需要修的红项
    libraries = [
        lib for lib in await LibraryRepository(session).list_all() if profile_of(lib).subscribable
    ]

    # 映射修复建议的锚点：全部库根的公共父目录。映射按前缀覆盖，一条公共
    # 父目录的映射即可覆盖其下所有库——建议里给出这个目录，避免用户把
    # 逐库报警误读成"每个库都要单独配一条映射"
    all_roots = [root for lib in libraries for root in lib.root_paths if root]
    try:
        common_root: str | None = os.path.commonpath(all_roots) if all_roots else None
    except ValueError:  # 根路径混入了相对路径等异常形态：放弃建议锚点即可
        common_root = None
    if common_root == "/":
        common_root = None  # 公共父目录退化到根：建议映射 / 没有意义

    pipelines: list[LibraryPipeline] = []
    for library in libraries:
        assert library.id is not None
        checks: list[HealthCheck] = []

        # —— 段 1：投递（下载器 + 目录 + 映射守门，与 dispatch 三级兜底同源）
        if downloader is None:
            checks.append(
                HealthCheck(
                    key="downloader",
                    label="下载器",
                    status="error",
                    detail="没有可用的默认下载器——订阅只能记录意愿，无法真实下载",
                    fix_section="downloaders",
                )
            )
        else:
            checks.append(
                HealthCheck(
                    key="downloader",
                    label="下载器",
                    status="ok",
                    detail=f"默认下载器「{downloader.name}」已就绪",
                )
            )

        # 投递目录三级兜底走全仓唯一实现（与真实投递/预检/手动下载同源）
        decision = await resolve_save_path(session, library, kind=library.kind)
        rule = decision.rule
        base = decision.path
        mode = decision.mode

        if base is None:
            checks.append(
                HealthCheck(
                    key="dispatch_dir",
                    label="投递目录",
                    status="error",
                    detail="库没有根路径，下载会落到下载器默认目录且不会自动入库",
                    fix_section="libraries",
                )
            )
        elif mode == "watch":
            staging = getattr(rule, "target_path", None)
            detail = (
                f"投递到监听导入目录 {base}，下载完成后整理到 {staging}（外部流转后回库根入账）"
                if staging
                else f"投递到监听导入目录 {base}，下载完成后自动整理入库"
            )
            checks.append(
                HealthCheck(
                    key="dispatch_dir",
                    label="投递目录",
                    status="ok",
                    detail=detail,
                )
            )
        else:
            checks.append(
                HealthCheck(
                    key="dispatch_dir",
                    label="投递目录",
                    status="ok",
                    detail=f"直接下载进库根 {base}，完成后库扫描自动入账",
                )
            )

        if downloader is not None and base is not None and downloader.path_mappings:
            if mapping_covers(base, downloader.path_mappings):
                checks.append(
                    HealthCheck(
                        key="mapping",
                        label="路径映射",
                        status="ok",
                        detail=f"投递目录已被「{downloader.name}」的路径映射覆盖",
                    )
                )
            else:
                checks.append(
                    HealthCheck(
                        key="mapping",
                        label="路径映射",
                        status="error",
                        detail=(
                            f"目录 {base} 不在下载器「{downloader.name}」的路径映射"
                            "覆盖范围内，投递会被拒绝。修复二选一：① 补一条路径"
                            "映射——映射按目录前缀覆盖，映射公共父目录（本机 "
                            f"{common_root or base} → 下载器视角的对应路径，下载器"
                            "可直达同名路径时两边填相同的）即可一条覆盖其下所有库，"
                            "无需逐库配置；② 为库配置监听导入规则（自动路由）——"
                            "投递会改走独立的下载目录，路径映射只需覆盖那个下载"
                            "目录，媒体库分得再细也不影响下载器配置"
                        ),
                        fix_section="downloaders",
                    )
                )

        # —— 段 2：转移（仅 watch 模式：硬链同盘 + 监听生效）
        if rule is not None:
            checks.extend(_check_transfer(rule, library, watched))

        status = _worst([c.status for c in checks])
        pipelines.append(
            LibraryPipeline(
                library_id=library.id,
                library_name=library.name,
                kind=library.kind,
                is_default=library.is_default,
                mode=mode,
                path=base,
                library_root=library.primary_root,
                status=status,
                staging_path=getattr(rule, "target_path", None),
                narrative=_narrative(mode, base, library, rule),  # type: ignore[arg-type]
                checks=checks,
            )
        )

    # 全局段（站点/下载器）计入整体状态：没有站点或下载器时即使各库自身
    # 无恙，订阅也跑不起来——横幅与开局清单都依赖这个口径
    overall = _worst([p.status for p in pipelines] + [site_check.status])
    if downloader is None:
        overall = "error"
    issues = _aggregate_issues(pipelines, site_check, downloader, common_root)
    return {
        "status": overall,
        "error_count": sum(1 for p in pipelines if p.status == "error"),
        "warn_count": sum(1 for p in pipelines if p.status == "warn"),
        "site_check": asdict(site_check),
        "downloader_ok": downloader is not None,
        "sites_configured": sites_configured,
        "downloaders_configured": downloaders_configured,
        "issues": [asdict(i) for i in issues],
        "libraries": [asdict(p) for p in pipelines],
    }
