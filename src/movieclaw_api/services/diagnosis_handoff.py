"""待处理事项 → Agent 诊断工单：把"发生了什么、系统已判定什么、已自动做过什么、
你能做什么"一次性组装成给 Agent 的输入。

## 为什么后端组装

前端拼 prompt 只能写标题（任务 ID、错误码、一句"请先查详情"），等于把一张只有
标题的工单扔给同事。而 Agent 的诊断上限取决于它能感知到什么：真实事故里决定性的
证据（容器内目录是空的）根本不在任何接口里，Agent 看到的和用户看到的一模一样，
只会得出同样错误的结论。所以工单在服务端生成，**生成那一刻就把自检跑掉**（路径
体检、下载器里的实时状态、落点是否可见），把证据而不是症状端给 Agent。

## 工单结构（每类待办固定六段）

1. 发生了什么：告警/任务原文与涉及的实体；
2. 系统已判定的事实：实时自检结果，带时间戳，Agent 应先复核再采信；
3. 系统已经自动做过的事：换源几次、退避到何时、刹车是否生效——没有这段 Agent
   会重复系统试过的事；
4. 相关时间线：只取这条工单前后的少量条目；
5. 你可以执行的动作：与界面上的按钮**同一份清单**，标出破坏性动作；
6. 请你做什么：先核实、再判断、只读自由、破坏性先问、解决不了就直说。

Agent 能做的动作集合等于界面的动作集合：多了会失控，少了只能"建议"。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services.downloader_paths import diagnose_landing, probe_mappings
from movieclaw_db.models import (
    ImportWatch,
    IngestEntry,
    Job,
    JobEvent,
    JobResource,
    LibraryFile,
    MediaItem,
    SiteCredential,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    SystemNotice,
    WantedItem,
    utcnow,
)
from movieclaw_db.models.downloader_client import DownloaderClient
from movieclaw_db.models.system_notice import NoticeStatus

logger = logging.getLogger("movieclaw_api.diagnosis_handoff")

HandoffKind = Literal["notice", "download", "job"]

_TIMELINE_LIMIT = 20
_ENTRY_LIMIT = 10


@dataclass(frozen=True)
class HandoffPrompt:
    title: str
    prompt: str


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "—"


def _gb(size: int | None) -> str:
    return f"{(size or 0) / 2**30:.2f} GB"


def _brief(data: object, limit: int = 600) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _units(units: list) -> str:
    if not units:
        return "—"
    return "、".join(f"S{int(s):02d}E{int(e):02d}" for s, e in units[:12]) + (
        f" 等 {len(units)} 个" if len(units) > 12 else ""
    )


# ---------------------------------------------------------------------------
# 各领域的取证段落（返回行列表）
# ---------------------------------------------------------------------------


async def _downloader_lines(session: AsyncSession, downloader_id: int | None) -> list[str]:
    """下载器配置 + 此刻的路径体检（不读缓存，现场 stat）。"""
    if downloader_id is None:
        return ["- 未关联下载器"]
    row = await session.get(DownloaderClient, downloader_id)
    if row is None:
        return [f"- 下载器 #{downloader_id} 已不存在"]
    lines = [
        f"- 下载器「{row.name}」#{row.id}：{row.client_type.value} {row.version or ''} @ {row.url}",
        f"  连接状态 {row.status.value}，启用={row.enabled}，最近测试 {_dt(row.last_checked_at)}"
        + (f"，最近错误：{row.last_error}" if row.last_error else ""),
        f"  路径映射：{_brief(row.path_mappings or [])}",
    ]
    probes = probe_mappings(row.path_mappings)
    if probes:
        lines.append(f"  路径体检（{_dt(utcnow())} 现场执行）：")
        for p in probes:
            lines.append(f"    · {p.local} → {p.remote}：{p.state.value}，{p.detail}")
    else:
        lines.append("  未配置路径映射（两边视角一致）")
    return lines


async def _torrent_lines(session: AsyncSession, info_hash: str) -> list[str]:
    """一个种子的全部事实：投递台账、工单、下载器实时状态、落点是否可见。"""
    from movieclaw_api.services.download_progress import _query_torrent, _usable_downloaders
    from movieclaw_api.services.torrent_submit import translate_to_local

    info_hash = info_hash.lower()
    lines: list[str] = [f"- 种子 info_hash：{info_hash}"]
    attempts = (
        (
            await session.execute(
                select(SubscriptionDownloadAttempt)
                .where(SubscriptionDownloadAttempt.info_hash == info_hash)
                .order_by(SubscriptionDownloadAttempt.id.desc())  # type: ignore[attr-defined]
            )
        )
        .scalars()
        .all()
    )
    for a in attempts[:3]:
        lines.append(
            f"- 投递台账 #{a.id}：状态 {a.status}，目的 {a.purpose}，下载器 #{a.downloader_id}，"
            f"投递目录 {a.save_path or '—'}，覆盖 {_units(a.units)}，"
            f"movieclaw 投递={a.owned_by_movieclaw}，H&R={a.hit_and_run}"
        )
        lines.append(
            f"  最近进度 {_dt(a.last_progress_at)}，完成于 {_dt(a.completed_at)}，"
            f"搜索次数 {a.search_attempts}，下次换源搜索 {_dt(a.next_search_at)}"
            + (f"，备注：{a.cleanup_note}" if a.cleanup_note else "")
            + (f"，替换自 #{a.replaces_attempt_id}" if a.replaces_attempt_id else "")
        )
        if a.content_missing:
            lines.append(f"  实测缺失：{_brief(a.content_missing)}")
    wanted = (
        await session.execute(
            select(WantedItem, MediaItem)
            .join(MediaItem, MediaItem.id == WantedItem.media_item_id)
            .where(WantedItem.info_hash == info_hash)
        )
    ).all()
    for w, item in wanted[:12]:
        lines.append(
            f"- 工单 #{w.id}《{item.title}》S{w.season_number:02d}E{w.episode_number:02d}："
            f"状态 {w.status}，在范围={w.in_scope}，"
            f"投递于 {_dt(w.grabbed_at)}，入库于 {_dt(w.imported_at)}"
        )

    downloaders = await _usable_downloaders(session)
    match = await _query_torrent(info_hash, downloaders)
    if match is None:
        lines.append(
            f"- 下载器实时状态：{len(downloaders)} 台可用下载器中都查不到该种子（可能已被删除）"
        )
        return lines
    downloader, status = match
    lines.append(
        f"- 下载器「{downloader.name}」实时状态（{_dt(utcnow())}）：{status.state}，"
        f"进度 {status.progress * 100:.1f}%，已完成={status.completed}，"
        f"体积 {_gb(status.size_bytes)}，保存目录 {status.save_path}"
        + (
            f"，错误：{getattr(status, 'error_message', None)}"
            if getattr(status, "error_message", None)
            else ""
        )
    )
    files = list(status.files or [])
    if files:
        shown = "、".join(f.path for f in files[:6]) + (
            f" 等 {len(files)} 个文件" if len(files) > 6 else ""
        )
        lines.append(f"  文件清单：{shown}")
    local_dir = translate_to_local(status.save_path, downloader.path_mappings)
    root = files[0].path.split("/")[0] if files else status.name
    probe = diagnose_landing(local_dir, downloader.path_mappings)
    visible = bool(local_dir and root and (Path(local_dir) / root).exists())
    lines.append(
        f"- 落点核验（现场）：下载器视角 {status.save_path} → "
        f"movieclaw 视角 {local_dir or '无法翻译'}；"
        f"目录体检 {probe.state.value}（{probe.detail}）；内容「{root}」在 movieclaw 侧"
        + ("可见" if visible else "**不可见**")
    )
    return lines


async def _activity_lines(session: AsyncSession, subscription_id: int) -> list[str]:
    rows = (
        (
            await session.execute(
                select(SubscriptionActivity)
                .where(SubscriptionActivity.subscription_id == subscription_id)
                .order_by(SubscriptionActivity.id.desc())  # type: ignore[attr-defined]
                .limit(_TIMELINE_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return ["- 无"]
    return [f"- {_dt(r.created_at)} [{r.type}] {r.message}" for r in reversed(rows)]


async def _subscription_head(session: AsyncSession, subscription_id: int) -> list[str]:
    sub = await session.get(Subscription, subscription_id)
    if sub is None:
        return [f"- 订阅 #{subscription_id} 已不存在"]
    item = await session.get(MediaItem, sub.media_item_id)
    return [
        f"- 订阅 #{sub.id}《{item.title if item else '?'}》（{sub.kind}）：状态 {sub.status}，"
        f"选季 {_brief(sub.selected_seasons)}，追新={sub.follow_future}，库 #{sub.library_id}"
    ]


async def _braked_attempts(session: AsyncSession, downloader_id: int, local_dir: str) -> list[str]:
    """目录级落点故障期间被刹车的换源尝试。"""
    rows = (
        (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.downloader_id == downloader_id,
                    SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                        ("active", "replacement_pending", "completed")
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    target = local_dir.rstrip("/")
    hit = [
        r
        for r in rows
        if r.save_path
        and (
            r.save_path.rstrip("/").startswith(target) or target.startswith(r.save_path.rstrip("/"))
        )
    ]
    if not hit:
        return ["- 该目录下没有在途的投递"]
    return [
        f"- #{r.id} 订阅 #{r.subscription_id}「{r.torrent_title[:50]}」状态 {r.status}，"
        f"下次换源搜索 {_dt(r.next_search_at)}"
        for r in hit[:12]
    ]


# ---------------------------------------------------------------------------
# 动作清单：与界面按钮同一份
# ---------------------------------------------------------------------------

_ACTIONS: dict[str, list[str]] = {
    "downloader": [
        "「设置 → 下载器」重新测试连接（dl.verify）：连接与路径体检都会重跑",
        "「设置 → 下载器」修改路径映射（dl.update）",
        "重启 movieclaw 容器 —— 这在宿主机上做，你做不了，只能建议用户去做",
        "忽略这条告警（notices.dismiss）—— 问题不会消失，只是不再提醒",
    ],
    "torrent": [
        "活动页「删除下载任务」（**破坏性**，会把工单退回重新寻找资源）",
        "活动页「立即换种」（仅在连续 15 分钟无进度时可用）",
        "订阅详情「立即搜索」/「手动选种」",
        "「设置 → 下载器」重新测试连接与路径体检",
    ],
    "job": [
        "重新执行（retry）",
        "取消（仅进行中/阻塞的任务）",
        "忽略（仅失败任务；阻塞任务占着资源锁不能忽略，要么处理要么取消）",
        "「设置 → 监听导入」清单里认领身份或忽略条目（入库阻塞时）",
        "「设置 → AI 模型」配置模型（字幕任务阻塞时）",
    ],
    "ingest": [
        "「设置 → 监听导入」清单：搜索 TMDB 认领（claim）、忽略（ignore）、恢复处理（restore）",
        "改文件名让指纹变化，系统会自动重跑识别",
    ],
    "site": [
        "「设置 → 站点」更新凭据并重新验证（site.verify）",
        "检查站点是否开启了「站点保护」（订阅链路会绕开它）",
    ],
    "subscription": [
        "订阅详情：季标题「标注片源」、手动选种、调整规则组、暂停/恢复追踪",
        "库详情：查看该集文件的实测规格（分辨率/编码/HDR/片源）",
        "忽略这条告警（notices.dismiss）",
    ],
}

_RULES = [
    "工单里的「现场」数据是生成那一刻的快照。先用只读操作复核关键事实，再下结论。",
    '给出：① 根因判断（一句话）② 证据 ③ 最短处理路径。判断要确定，不要写"可能是 A 或 B"——'
    "如果证据不足以确定，说清楚还缺哪条证据、怎么取。",
    "只读与非破坏性动作可以直接执行。删除下载任务、删除文件、清理记录、忽略告警这类动作"
    "先说明后果，等用户确认再做。",
    "系统已经自动做过的事不要重复做。",
    "如果上面列出的动作都解决不了，明确说出来并解释为什么，不要编造一个看起来可行的步骤。",
]


def _render(title: str, sections: list[tuple[str, list[str]]], actions: list[str]) -> str:
    out = [f"# MovieClaw 诊断工单：{title}", ""]
    for heading, lines in sections:
        out.append(f"## {heading}")
        out.extend(lines or ["- 无"])
        out.append("")
    out.append("## 你可以执行的动作（与界面一致，不要越过这份清单）")
    out.extend(f"- {a}" for a in actions)
    out.append("")
    out.append("## 请你做什么")
    out.extend(f"{i}. {r}" for i, r in enumerate(_RULES, 1))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 三类入口
# ---------------------------------------------------------------------------


async def _for_notice(session: AsyncSession, notice_id: int) -> HandoffPrompt:
    notice = await session.get(SystemNotice, notice_id)
    if notice is None:
        raise NotFoundException(f"待处理事项不存在：id={notice_id}")
    key = notice.dedupe_key
    payload = notice.payload or {}
    happened = [
        f"- 告警：{notice.title}",
        f"- 说明：{notice.message}",
        f"- 严重度 {notice.severity}，来源 {notice.source}，状态 {notice.status}，"
        f"首次 {_dt(notice.created_at)}，最近刷新 {_dt(notice.updated_at)}",
        f"- 关联数据：{_brief(payload)}",
    ]
    sections: list[tuple[str, list[str]]] = [("1. 发生了什么", happened)]
    actions = _ACTIONS["subscription"]

    if key.startswith(("downloader:", "downloader.paths:", "downloader.landing:")):
        did = payload.get("downloader_id")
        facts = await _downloader_lines(session, did)
        done: list[str] = []
        if key.startswith("downloader.landing:") and did is not None:
            local_dir = str(payload.get("local_dir") or "")
            children = (
                (
                    await session.execute(
                        select(SystemNotice).where(
                            SystemNotice.status == NoticeStatus.ACTIVE.value,
                            SystemNotice.dedupe_key.startswith("subscription.landing:"),  # type: ignore[attr-defined]
                        )
                    )
                )
                .scalars()
                .all()
            )
            mine = [c for c in children if c.payload.get("grouped_under") == key]
            facts.append(f"- 被收编的单种子告警 {len(mine)} 条：")
            facts.extend(
                f"  · {c.title}（{_gb(c.payload.get('size_bytes'))}，"
                f"订阅 #{c.payload.get('subscription_id')}）"
                for c in mine[:_ENTRY_LIMIT]
            )
            done.append(
                "- 目录级红灯活跃期间，落在该目录的订阅**自动换源已刹车**（一小时复查一次）；"
                "手动换种不受限"
            )
            done.extend(await _braked_attempts(session, did, local_dir))
        sections.append(("2. 系统已判定的事实（现场自检）", facts))
        sections.append(
            (
                "3. 系统已经自动做过的事",
                done or ["- 连接失败时订阅投递与完成检测全部停摆，等待重测通过"],
            )
        )
        actions = _ACTIONS["downloader"]

    elif key.startswith("subscription.landing:"):
        _, sub_id, info_hash = key.split(":", 2)
        facts = await _subscription_head(session, int(sub_id)) + await _torrent_lines(
            session, info_hash
        )
        sections.append(("2. 系统已判定的事实（现场自检）", facts))
        sections.append(
            (
                "3. 系统已经自动做过的事",
                [
                    "- 落点核验失败**不会**退回工单重找"
                    "（数据真实存在，重找只会重复下载到同一个黑洞）",
                    "- 用户在活动页删掉该任务时工单立即退回、红灯熄灭",
                ],
            )
        )
        sections.append(("4. 相关时间线", await _activity_lines(session, int(sub_id))))
        actions = _ACTIONS["torrent"]

    elif key.startswith(("subscription.spec_mismatch:", "subscription.upgrade:")):
        _, sub_id, season, episode = key.split(":", 3)
        facts = await _subscription_head(session, int(sub_id))
        wanted = (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == int(sub_id),
                    WantedItem.season_number == int(season),
                    WantedItem.episode_number == int(episode),
                )
            )
        ).scalar_one_or_none()
        if wanted is not None:
            facts.append(
                f"- 工单 #{wanted.id} S{int(season):02d}E{int(episode):02d}：状态 {wanted.status}，"
                f"质量快照 {_brief(wanted.quality)}，"
                f"洗版证伪次数 {wanted.upgrade_verify_failures}，"
                f"最近拒绝原因：{wanted.last_reject_reason or '—'}"
            )
            files = (
                (
                    await session.execute(
                        select(LibraryFile).where(
                            LibraryFile.media_item_id == wanted.media_item_id,
                            LibraryFile.season_number == int(season),
                            LibraryFile.episode_number == int(episode),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for f in files[:5]:
                facts.append(
                    f"- 库内文件：{f.file_path}（{_gb(f.size_bytes)}，"
                    f"{f.resolution or '?'} {f.video_codec or '?'} "
                    f"{f.hdr or 'SDR'} {f.media_source or '?'}，状态 {f.state}，"
                    f"片源手动标注={f.media_source_manual}）"
                )
        sections.append(("2. 系统已判定的事实", facts))
        sections.append(
            (
                "3. 系统已经自动做过的事",
                [
                    "- 洗版连续证伪后该单元转入 30 天冷却"
                    if "upgrade" in key
                    else "- 只记录，不自动替换；该告警没有自动熄灭路径，需要用户复查后忽略"
                ],
            )
        )
        sections.append(("4. 相关时间线", await _activity_lines(session, int(sub_id))))

    elif key.startswith("ingest:"):
        source = str(payload.get("source_path") or key.removeprefix("ingest:"))
        rule = (
            await session.execute(select(ImportWatch).where(ImportWatch.source_path == source))
        ).scalar_one_or_none()
        facts = []
        if rule is None:
            facts.append(f"- 监听规则 {source} 已不存在")
        else:
            facts.append(
                f"- 监听规则 #{rule.id}：{rule.source_path} → "
                f"库 #{rule.library_id}（{rule.kind or '自动'}），"
                f"策略 {rule.strategy}，处理存量={rule.process_existing}"
            )
            entries = (
                (
                    await session.execute(
                        select(IngestEntry)
                        .where(IngestEntry.entry_path.startswith(source))  # type: ignore[attr-defined]
                        .where(IngestEntry.status.in_(("failed", "pending")))  # type: ignore[attr-defined]
                        .order_by(IngestEntry.updated_at.desc())  # type: ignore[attr-defined]
                        .limit(_ENTRY_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
            for e in entries:
                facts.append(
                    f"- 条目 #{e.id} [{e.status}] {Path(e.entry_path).name}：{e.message or '—'}"
                    f"（最近尝试 {_dt(e.attempted_at)}，认领 TMDB={e.claimed_tmdb_id}）"
                )
        sections.append(("2. 系统已判定的事实", facts))
        sections.append(
            (
                "3. 系统已经自动做过的事",
                [
                    "- failed 条目每小时退避重试一次；"
                    "pending 条目不重试、不告警，只等人工认领或文件改名"
                ],
            )
        )
        actions = _ACTIONS["ingest"]

    elif key.startswith("site:"):
        site_id = key.removeprefix("site:")
        cred = (
            await session.execute(select(SiteCredential).where(SiteCredential.site_id == site_id))
        ).scalar_one_or_none()
        facts = (
            [f"- 站点 {site_id} 未配置凭据"]
            if cred is None
            else [
                f"- 站点 {site_id}：认证 {cred.auth_type.value}，启用={cred.enabled}，"
                f"站点保护={cred.protected}，状态 {cred.status.value}，"
                f"最近验证成功 {_dt(cred.last_verified_at)}，"
                f"最近尝试 {_dt(cred.last_checked_at)}"
                + (f"，最近错误：{cred.last_error}" if cred.last_error else "")
            ]
        )
        sections.append(("2. 系统已判定的事实", facts))
        sections.append(
            ("3. 系统已经自动做过的事", ["- 该站点已从搜索与投递中摘除，直到重新验证通过"])
        )
        actions = _ACTIONS["site"]

    return HandoffPrompt(title=notice.title, prompt=_render(notice.title, sections, actions))


async def _for_download(session: AsyncSession, info_hash: str) -> HandoffPrompt:
    facts = await _torrent_lines(session, info_hash)
    sections: list[tuple[str, list[str]]] = [
        ("1. 发生了什么", ["- 活动页上这条下载任务被标为「需要处理」，用户要求诊断"]),
        ("2. 系统已判定的事实（现场自检）", facts),
    ]
    jobs = (
        (
            await session.execute(
                select(Job)
                .join(JobResource, JobResource.job_id == Job.id)
                .where(
                    JobResource.resource_type == "download",
                    JobResource.resource_id == info_hash.lower(),
                )
                .order_by(Job.created_at.desc())  # type: ignore[attr-defined]
                .limit(3)
            )
        )
        .scalars()
        .all()
    )
    done = [
        f"- 关联入库任务 {j.id}（{j.job_type}）：状态 {j.status}，"
        f"{(j.error or {}).get('code') or ''} "
        f"{(j.error or {}).get('message') or j.progress.get('message') or ''}".rstrip()
        for j in jobs
    ] or ["- 尚未触发入库任务"]
    sections.append(("3. 系统已经自动做过的事", done))
    sub_ids = sorted(
        {
            a
            for (a,) in (
                await session.execute(
                    select(SubscriptionDownloadAttempt.subscription_id).where(
                        SubscriptionDownloadAttempt.info_hash == info_hash.lower()
                    )
                )
            ).all()
        }
    )
    timeline: list[str] = []
    for sid in sub_ids[:2]:
        timeline.extend(await _activity_lines(session, sid))
    sections.append(("4. 相关时间线", timeline))
    return HandoffPrompt(
        title=f"下载任务 {info_hash[:8]}",
        prompt=_render(f"下载任务 {info_hash[:8]}", sections, _ACTIONS["torrent"]),
    )


async def _for_job(session: AsyncSession, job_id: str) -> HandoffPrompt:
    job = await session.get(Job, job_id)
    if job is None:
        raise NotFoundException(f"任务不存在：{job_id}")
    error = job.error or {}
    happened = [
        f"- 任务 {job.id}（{job.job_type}）：{job.subject or '—'}",
        f"- 状态 {job.status}，尝试 {job.attempt}/{job.max_attempts}，来源 {job.origin}"
        + (f"（{job.actor_name}）" if job.actor_name else ""),
        f"- 错误码 {error.get('code') or '—'}："
        f"{error.get('message') or job.progress.get('message') or '—'}",
        f"- 系统给出的动作：{_brief(error.get('actions') or [])}",
        f"- 输入：{_brief(job.input_data, 500)}",
        f"- 进度：{_brief(job.progress, 300)}",
        f"- 创建 {_dt(job.created_at)}，开始 {_dt(job.started_at)}，结束 {_dt(job.finished_at)}",
    ]
    sections: list[tuple[str, list[str]]] = [("1. 发生了什么", happened)]
    facts: list[str] = []
    resources = (
        (await session.execute(select(JobResource).where(JobResource.job_id == job.id)))
        .scalars()
        .all()
    )
    for r in resources:
        if r.resource_type == "ingest_entry" and r.resource_id.isdigit():
            e = await session.get(IngestEntry, int(r.resource_id))
            if e is not None:
                facts.append(
                    f"- 监听条目 #{e.id} [{e.status}] {e.entry_path}：{e.message or '—'}"
                    f"（认领 TMDB={e.claimed_tmdb_id}/{e.claimed_kind}，"
                    f"已入库 {e.imported_count} 个文件）"
                )
        elif r.resource_type == "download":
            facts.extend(await _torrent_lines(session, r.resource_id))
        else:
            facts.append(f"- 关联资源 {r.resource_type}:{r.resource_id}（{r.relation}）")
    sections.append(("2. 系统已判定的事实", facts))
    events = (
        (
            await session.execute(
                select(JobEvent)
                .where(JobEvent.job_id == job.id)
                .order_by(JobEvent.id.desc())  # type: ignore[attr-defined]
                .limit(_TIMELINE_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    timeline = [
        f"- {_dt(ev.created_at)} [{ev.event_type}] {_brief(ev.payload, 200)}"
        for ev in reversed(events)
    ]
    sections.append(
        (
            "3. 系统已经自动做过的事",
            [f"- 已自动重试 {job.attempt} 次" if job.attempt else "- 无自动重试"],
        )
    )
    sections.append(("4. 任务事件时间线", timeline))
    title = f"{job.job_type} {job.subject or job.id}"
    return HandoffPrompt(title=title, prompt=_render(title, sections, _ACTIONS["job"]))


async def build_handoff_prompt(session: AsyncSession, kind: HandoffKind, ref: str) -> HandoffPrompt:
    """按待办类型生成诊断工单。``ref``：notice 为 id，download 为 info_hash，job 为 job id。"""
    if kind == "notice":
        return await _for_notice(session, int(ref))
    if kind == "download":
        return await _for_download(session, ref)
    return await _for_job(session, ref)
