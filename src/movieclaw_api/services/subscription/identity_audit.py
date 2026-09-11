"""识别存疑的观察台账：把几条 shadow 判定的真实触发率读出来。

``docs/design/identity-confidence.md`` §10.2 要求那几个拍脑袋的阈值先以 shadow
模式上线，"跑满一个观察期后回答三个问题再决定是否开成实际生效"：触发率多少、
触发样本里有几个是真错配、§0 那个 case 能不能复现。

问题是判定虽然都落了库，**全项目没有任何一处读它们**——``library_file.
identity_doubt`` 零读取点，投递活动 payload 里的 ``shadow`` 键同样零读取点。
于是灰度永远收敛不了：既不知道该不该点灯，出了事也只能靠人工翻日志（那条
113 MB 的假「正片」就是用户自己发现的，而系统连着两层都"看见"了）。

本模块只做一件事：**只读聚合**，不改任何行为、不产生告警。四组数字对应
§10.2 与 §10.4 要回答的问题：

- ``confidence``：只靠片名+年份认的投递占比——§10.4 点名的核心指标，本方案
  要压低的就是它；
- ``bitrate_shadow``：体积÷片长 的**可疑档**命中（记录但未拦截）——它的触发率
  与误报率决定这一档要不要点灯；
- ``absurd_rejected``：**极端档**真正拦下的次数——这一档已经生效，看的是它有
  没有过度开火；
- ``runtime_doubt``：入库实测片长与影片信息不符的文件。

样本按时间倒序给最近若干条，让人能直接去核对"这几条到底是不是真错配"。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models import (
    ActivityType,
    LibraryFile,
    MediaItem,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    utcnow,
)

DEFAULT_WINDOW_DAYS = 30
SAMPLE_LIMIT = 20  # 每组样本条数：够人工抽查，不至于把响应撑爆


@dataclass
class AuditSample:
    """一条可直接人工核对的样本。``at`` 是 ISO 时间串（前端不必再做时区换算）。"""

    at: str
    title: str
    note: str
    site_id: str | None = None
    torrent_id: str | None = None


@dataclass
class AuditGroup:
    """一组观察：命中次数 + 最近样本。``hits`` 是窗口内全量，``samples`` 有上限。"""

    hits: int = 0
    samples: list[AuditSample] = field(default_factory=list)


@dataclass
class ConfidenceStat:
    """投递的身份证据强度分布。``guess`` 是"只靠片名+年份认的"那部分。"""

    dispatched: int = 0
    guess: int = 0
    ratio: float = 0.0


async def identity_audit(
    session: AsyncSession, *, window_days: int = DEFAULT_WINDOW_DAYS
) -> dict:
    """汇总窗口内的识别存疑观察；纯只读。"""
    since = utcnow() - timedelta(days=max(window_days, 1))
    # 条目量级是媒体库规模，一次取完比逐条 join 便宜
    titles = dict((await session.execute(select(MediaItem.id, MediaItem.title))).all())

    confidence = await _confidence_stat(session, since)
    bitrate, absurd = await _activity_groups(session, since, await _subscription_titles(session))
    doubt = await _runtime_doubt_group(session, since, titles)
    return {
        "window_days": window_days,
        "confidence": asdict(confidence),
        "bitrate_shadow": asdict(bitrate),
        "absurd_rejected": asdict(absurd),
        "runtime_doubt": asdict(doubt),
    }


async def _confidence_stat(session: AsyncSession, since) -> ConfidenceStat:
    """投递台账里 exact_id 与"只靠片名+年份"的比例。

    NULL 不计入分母：那是本特性上线前的旧数据，当成猜测会把历史噪音算进指标。
    """
    rows = (
        (
            await session.execute(
                select(SubscriptionDownloadAttempt.identity_confidence).where(
                    SubscriptionDownloadAttempt.created_at >= since,  # type: ignore[operator]
                    SubscriptionDownloadAttempt.identity_confidence.isnot(None),  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    dispatched = len(rows)
    guess = sum(1 for value in rows if value != "exact_id")
    return ConfidenceStat(
        dispatched=dispatched,
        guess=guess,
        ratio=round(guess / dispatched, 4) if dispatched else 0.0,
    )


async def _subscription_titles(session: AsyncSession) -> dict[int, str]:
    """订阅 → 条目标题。活动只挂 ``subscription_id``，样本里的片名得绕这一跳。"""
    from movieclaw_db.models import Subscription

    rows = (
        await session.execute(
            select(Subscription.id, MediaItem.title).join(
                MediaItem,
                MediaItem.id == Subscription.media_item_id,  # type: ignore[arg-type]
            )
        )
    ).all()
    return dict(rows)


async def _activity_groups(
    session: AsyncSession, since, titles: dict[int, str]
) -> tuple[AuditGroup, AuditGroup]:
    """从订阅活动里取两组：投递时的 shadow 记录，与极端档的拒绝。

    payload 是 JSON 列，跨 SQLite/其他后端做条件下推不可移植，所以按类型 +
    时间窗口取回后在 Python 里筛——窗口内的活动量级对单机部署完全可接受。
    """
    bitrate, absurd = AuditGroup(), AuditGroup()
    rows = (
        (
            await session.execute(
                select(SubscriptionActivity)
                .where(
                    SubscriptionActivity.created_at >= since,  # type: ignore[operator]
                    SubscriptionActivity.type.in_(  # type: ignore[union-attr]
                        (ActivityType.GRABBED, ActivityType.MATCH_REJECTED)
                    ),
                )
                .order_by(SubscriptionActivity.created_at.desc())  # type: ignore[union-attr]
            )
        )
        .scalars()
        .all()
    )
    for activity in rows:
        payload = activity.payload or {}
        label = titles.get(activity.subscription_id) or "（订阅已删除）"
        if activity.type == ActivityType.GRABBED:
            note = (payload.get("shadow") or {}).get("bitrate_reject")
            group = bitrate
        elif payload.get("reason_code") == "size_absurd_for_runtime":
            note = activity.message
            group = absurd
        else:
            continue
        if not note:
            continue
        group.hits += 1
        if len(group.samples) < SAMPLE_LIMIT:
            group.samples.append(
                AuditSample(
                    at=activity.created_at.isoformat(),
                    title=label,
                    note=note,
                    site_id=payload.get("site_id"),
                    torrent_id=payload.get("torrent_id"),
                )
            )
    return bitrate, absurd


async def _runtime_doubt_group(
    session: AsyncSession, since, titles: dict[int, str]
) -> AuditGroup:
    """入库时长体检留下的存疑台账（``library_file.identity_doubt``）。"""
    group = AuditGroup()
    rows = (
        (
            await session.execute(
                select(LibraryFile)
                .where(
                    LibraryFile.identity_doubt.isnot(None),  # type: ignore[union-attr]
                    LibraryFile.created_at >= since,  # type: ignore[operator]
                )
                .order_by(LibraryFile.created_at.desc())  # type: ignore[union-attr]
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        doubt = row.identity_doubt or {}
        group.hits += 1
        if len(group.samples) < SAMPLE_LIMIT:
            group.samples.append(
                AuditSample(
                    at=row.created_at.isoformat(),
                    title=titles.get(row.media_item_id or 0) or "（条目已删除）",
                    note=(
                        f"实测 {doubt.get('actual_minutes')} 分钟，"
                        f"影片信息标注 {doubt.get('expected_minutes')} 分钟"
                    ),
                    site_id=row.site_id,
                    torrent_id=row.torrent_id,
                )
            )
    return group
