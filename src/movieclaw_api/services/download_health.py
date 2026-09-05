"""下载健康：落点故障按根因分组、换源刹车、停滞事件折叠。

三件事服务同一个目标——**让一个根因在页面上只表现为一件事**，并且在修好
之前不让自动化继续制造代价。它们来自同一次真实事故的三个教训：

1. 一个挂载故障影响 3 部剧、16 个种子，用户看到的是 16 条"《某剧》下载完成
   但无法入库"，没有任何一条说"你的下载目录整个不可见"。归纳症状到根因是
   专家才做得到的事，系统必须替用户做。→ ``upsert_landing_group``
2. 红灯亮着的 45 小时里，系统照常换源重下，白烧 90GB，用户全程不知道。
   告警是状态展示，不是控制信号；目录都看不见，换再多源也只是往同一个黑洞
   里倒。→ ``landing_brake``
3. 每次换源新建一个 attempt、各记一条"停滞 15 分钟"，34 条流水把 2 条真正
   的 import_failed 淹没。高频例行事件不能盖住低频真信号。→ ``record_stalled``

放在独立模块：巡检（download_progress）与换源（subscription.replacement）
都要用，而两者之间刻意不互相 import。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.downloader_paths import PathProbe
from movieclaw_api.services.system_notice import resolve_notices, upsert_notice
from movieclaw_db.models import ActivityType, SubscriptionActivity, utcnow
from movieclaw_db.models.downloader_client import DownloaderClient
from movieclaw_db.models.system_notice import NoticeSeverity, NoticeStatus, SystemNotice

logger = logging.getLogger("movieclaw_api.download_health")

# 目录级落点红灯的 key 前缀；子告警（单个种子）沿用 subscription.landing:
LANDING_GROUP_PREFIX = "downloader.landing:"
CHILD_PREFIX = "subscription.landing:"
# 下载器路径体检（「测试连接」时发现映射本地侧不可达）的红灯前缀；它比目录级
# 落点红灯更上游：同一个挂载故障，先被测试连接发现就是它，先被完成种子撞上
# 就是目录级红灯。两者同时活跃时目录级红灯收编到它下面，用户仍只看到一条
PATHS_PREFIX = "downloader.paths:"

# 同一订阅、同一批单元的停滞事件在此窗口内折叠成一条（超过则视为新一轮）
_FOLD_WINDOW = timedelta(hours=24)


def landing_group_key(downloader_id: int, local_dir: str) -> str:
    return f"{LANDING_GROUP_PREFIX}{downloader_id}:{local_dir.rstrip('/') or '/'}"


def paths_key(downloader_id: int) -> str:
    return f"{PATHS_PREFIX}{downloader_id}"


async def _notice_active(session: AsyncSession, dedupe_key: str) -> bool:
    row = (
        await session.execute(select(SystemNotice).where(SystemNotice.dedupe_key == dedupe_key))
    ).scalar_one_or_none()
    return row is not None and row.status == NoticeStatus.ACTIVE.value


def _same_dir(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.rstrip("/") == b.rstrip("/")


async def _active_children(
    session: AsyncSession, downloader_id: int, local_dir: str
) -> list[SystemNotice]:
    """同一下载器、同一落点目录下仍活跃的单种子告警。表很小，Python 过滤即可。"""
    rows = (
        (
            await session.execute(
                select(SystemNotice).where(
                    SystemNotice.dedupe_key.startswith(CHILD_PREFIX),  # type: ignore[attr-defined]
                    SystemNotice.status == NoticeStatus.ACTIVE.value,
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        r
        for r in rows
        if r.payload.get("downloader_id") == downloader_id
        and _same_dir(r.payload.get("local_dir"), local_dir)
    ]


def _gb(size: int) -> str:
    return f"{size / 2**30:.1f} GB"


async def upsert_landing_group(
    session: AsyncSession,
    downloader: DownloaderClient,
    local_dir: str,
    probe: PathProbe,
) -> None:
    """目录级根因确认 → 升格为一条目录级红灯，把同目录的单种子告警收进去。

    子告警不消退：任务中心靠它们把 ``landing_error`` 挂回对应任务卡片（#294），
    卡片上的"无法入库"红标与删除出口都依赖它。这里只在子告警 payload 打上
    ``grouped_under``，告警中心据此折叠展示——用户看到一条根因，任务卡片
    仍各自亮着。

    代价累计写进标题正文：把"某部剧没更新"变成"正在持续烧盘"，紧迫感完全不同。
    """
    children = await _active_children(session, downloader.id, local_dir)  # type: ignore[arg-type]
    key = landing_group_key(downloader.id, local_dir)  # type: ignore[arg-type]
    wasted = sum(int(c.payload.get("size_bytes") or 0) for c in children)
    subscriptions = sorted({c.payload.get("subscription_id") for c in children} - {None})
    tagged = 0
    for child in children:
        if child.payload.get("grouped_under") != key:
            child.payload = {**child.payload, "grouped_under": key}
            tagged += 1
    if tagged:
        await session.commit()
    count = len(children)
    await upsert_notice(
        session,
        dedupe_key=key,
        severity=NoticeSeverity.ERROR,
        source="downloader",
        title=f"下载器「{downloader.name}」的目录 {local_dir} movieclaw 看不到",
        message=(
            f"{probe.detail}。"
            f"已有 {count} 个下载完成的任务卡在这个目录里无法入库（共 {_gb(wasted)}），"
            f"涉及 {len(subscriptions)} 个订阅。修好之前这些订阅的自动换源已暂停——"
            "目录都看不见，换再多源也只是重复下载同一内容"
        ),
        payload={
            "group_key": key,
            # 路径体检红灯活跃时收编到它下面：同一挂载故障只亮一张卡
            **(
                {"grouped_under": paths_key(downloader.id)}  # type: ignore[arg-type]
                if await _notice_active(session, paths_key(downloader.id))  # type: ignore[arg-type]
                else {}
            ),
            "downloader_id": downloader.id,
            "local_dir": local_dir,
            "state": probe.state.value,
            "count": count,
            "wasted_bytes": wasted,
            "subscription_ids": subscriptions,
        },
    )
    logger.warning(
        "落点目录级故障：下载器「%s」的 %s（%s），%d 个任务无法入库，合计 %s，自动换源已刹车",
        downloader.name,
        local_dir,
        probe.state.value,
        count,
        _gb(wasted),
    )


async def adopt_landing_groups(session: AsyncSession, downloader_id: int) -> int:
    """路径体检红灯点亮后，把该下载器已活跃的目录级落点红灯收编到它下面。

    两盏灯谁先亮取决于故障先被哪条路径撞上（测试连接 vs 完成种子核验），
    因此两个方向都要处理：这里管"路径红灯后亮"，upsert_landing_group 管"先亮"。
    返回收编条数。
    """
    parent = paths_key(downloader_id)
    rows = (
        (
            await session.execute(
                select(SystemNotice).where(
                    SystemNotice.dedupe_key.startswith(LANDING_GROUP_PREFIX),  # type: ignore[attr-defined]
                    SystemNotice.status == NoticeStatus.ACTIVE.value,
                )
            )
        )
        .scalars()
        .all()
    )
    adopted = 0
    for row in rows:
        if row.payload.get("downloader_id") != downloader_id:
            continue
        if row.payload.get("grouped_under") != parent:
            row.payload = {**row.payload, "grouped_under": parent}
            adopted += 1
    if adopted:
        await session.commit()
    return adopted


async def resolve_landing_group(session: AsyncSession, downloader_id: int, local_dir: str) -> None:
    """目录重新可见 → 目录级红灯熄灭。子告警各自在下一轮核验时熄灭。"""
    await resolve_notices(session, dedupe_key=landing_group_key(downloader_id, local_dir))


async def landing_brake(
    session: AsyncSession, downloader_id: int | None, save_path: str | None
) -> SystemNotice | None:
    """该投递目录是否处在目录级落点故障中。是则返回那条红灯（供文案引用）。

    只看**活跃**的目录级告警：用户手动忽略（dismissed）意味着"我知道，别管"，
    刹车随之松开——否则一个被忽略的红灯会悄悄冻结所有换源，用户找不到原因。
    """
    if downloader_id is None or not save_path:
        return None
    rows = (
        (
            await session.execute(
                select(SystemNotice).where(
                    SystemNotice.dedupe_key.startswith(LANDING_GROUP_PREFIX),  # type: ignore[attr-defined]
                    SystemNotice.status == NoticeStatus.ACTIVE.value,
                )
            )
        )
        .scalars()
        .all()
    )
    target = save_path.rstrip("/")
    for row in rows:
        if row.payload.get("downloader_id") != downloader_id:
            continue
        local_dir = str(row.payload.get("local_dir") or "").rstrip("/")
        # 投递目录与落点目录任一方是另一方的前缀即视为同一片区域
        if local_dir and (target.startswith(local_dir) or local_dir.startswith(target)):
            return row
    return None


async def record_stalled(
    session: AsyncSession,
    *,
    subscription_id: int,
    info_hash: str,
    units: list,
    reason: str,
    message: str,
) -> None:
    """记一条停滞事件，同订阅、同单元、同原因在 24 小时内折叠为一条并计数。

    时间线活动原则上只增不改（历史事实）。此处刻意例外：换源每换一次源就新建
    一个 attempt、各记一条"停滞 15 分钟"，一部剧一晚上能刷出几十条，把真正
    需要用户看的 import_failed 淹没。折叠后一条读作"已停滞 N 次"，信息量
    只增不减，且首条的时间戳保留了"从什么时候开始卡"。
    """
    now = utcnow()
    unit_key = sorted(tuple(u) for u in units)
    since = now - _FOLD_WINDOW
    recent = (
        (
            await session.execute(
                select(SubscriptionActivity)
                .where(
                    SubscriptionActivity.subscription_id == subscription_id,
                    SubscriptionActivity.type == ActivityType.DOWNLOAD_STALLED,
                    SubscriptionActivity.created_at >= since,
                )
                .order_by(SubscriptionActivity.id.desc())  # type: ignore[attr-defined]
            )
        )
        .scalars()
        .all()
    )
    for row in recent:
        payload = row.payload or {}
        if payload.get("reason") != reason:
            continue
        if sorted(tuple(u) for u in payload.get("units") or []) != unit_key:
            continue
        occurrences = int(payload.get("occurrences") or 1) + 1
        base = payload.get("base_message") or row.message
        row.message = f"{base}（24 小时内已发生 {occurrences} 次）"
        row.payload = {
            **payload,
            "info_hash": info_hash,
            "occurrences": occurrences,
            "base_message": base,
        }
        row.updated_at = now
        session.add(row)
        await session.commit()
        return
    session.add(
        SubscriptionActivity(
            subscription_id=subscription_id,
            type=ActivityType.DOWNLOAD_STALLED,
            message=message,
            payload={
                "info_hash": info_hash,
                "reason": reason,
                "units": [list(u) for u in unit_key],
                "occurrences": 1,
                "base_message": message,
            },
        )
    )
    await session.commit()
