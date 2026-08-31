"""洗版基线快照：构建、写入与存量回填（docs/design/quality-upgrade.md §4）。

快照取值原则（§4.1）：**能实测的维度以 ffprobe 为准，实测不出的出处维度采信
名称解析**——resolution/hdr/bit_rate 来自 library_file 的 probe 列，
media_source/remux/release_group 优先取投递时的 attempt.quality（种子名解析），
无投递记录（手工入库/扫描收编）时对文件名重跑 enrich。

写入时机：
- 库存对账关闭工单时（wanted_fulfillment 调 ``fill_snapshots``）——所有新
  入库单元统一落快照，与规则组是否开洗版无关（数据热且便宜，规则组随后
  开洗版时立即可用）；
- 存量回填 tick（``backfill_upgrade_snapshots``）——只处理"规则组已配洗版
  目标"的订阅的历史 imported 单元，分批做纯 DB 变换（probe 数据 library_file
  里都有，不重新探测文件）。

快照三态：NULL=未回填；``{}``（空对象）=已尝试构建但关键维度全部无法识别
（不参与洗版，且不会被回填任务反复重试）；非空=正常基线。

**落库一律全键**（``model_dump()`` 不带 exclude_defaults）：``{}`` 因此在结构上
专属于哨兵。曾经用 ``exclude_defaults=True`` 落库，正常快照在"全部维度取默认值"
时也会 dump 成 ``{}`` 被误当哨兵——多加一个可比维度（如 SDR 是 hdr 的默认空列表）
这个碰撞就会真实发生并静默漏洗，见 quality-upgrade.md §16.2。

**结构版本**：快照带 ``v``（``SNAPSHOT_VERSION``）。新增可比维度时 +1，
``backfill_upgrade_snapshots`` 的陈旧扫描会把存量快照逐批重算——纯 DB 变换，
不重新探测文件（§16.3）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    LibraryFile,
    RuleSet,
    Subscription,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_enrich import enrich
from movieclaw_matcher import (
    DISC_SOURCE,
    SNAPSHOT_VERSION,
    QualitySnapshot,
    RuleSetSpec,
    build_snapshot,
    resolution_rank,
    source_tier,
)
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.subscription.upgrade")

# 回填每 tick 处理的工单数：回填是低优先级的一次性补账，小批慢跑即可
_BACKFILL_BATCH = 50
_BACKFILL_TICK_SECONDS = 900

# 排期补挂的全表轮转游标（进程内状态；重启丢失只是从头再扫一轮）
_pending_arm_cursor = 0
# 陈旧快照重算的全表轮转游标（同上）。"版本落后"这个条件要跨方言写 JSON 取值
# 才能进 SQL，得不偿失；沿用本模块既有的游标轮转 + Python 侧判定的做法
_stale_snapshot_cursor = 0

# 入库验证互斥：监听导入与库扫描可能并发触发同一条目的对账，双重确认会
# 对同一批旧文件重复走回收站/删行（第二个会话删已删行会抛错）
_verify_lock = asyncio.Lock()

# 选"最优文件"用的中性偏好（内置默认分辨率序）：快照本身与规则组无关，
# 只有多版本并存时需要一个稳定的挑选顺序
_NEUTRAL_SPEC = RuleSetSpec()


def rule_set_ids_with_upgrade(rule_sets: list[RuleSet]) -> set[int]:
    """解析 spec，返回配置了洗版目标的规则组 id 集合（解析失败视为未配置）。"""
    ids: set[int] = set()
    for row in rule_sets:
        try:
            spec = RuleSetSpec.model_validate(row.spec or {})
        except ValueError:
            continue
        if spec.upgrade_source is not None and row.id is not None:
            ids.add(row.id)
    return ids


def _file_sort_key(file: LibraryFile) -> tuple[int, int, int]:
    """多版本并存时挑最优文件的排序键：分辨率位次 > 片源档 > 新入库优先。"""
    return (
        resolution_rank(file.resolution, _NEUTRAL_SPEC) or 0,
        source_tier(file.media_source, False) or 0,
        file.id or 0,
    )


def snapshot_from_file(
    file: LibraryFile, name_attrs: QualitySnapshot | None
) -> QualitySnapshot:
    """由库文件行 + 名称解析来源构造快照（§4.1 分层取值）。

    ``name_attrs`` 为空时对文件名重跑 enrich（与入库管线同一套解析器与词表），
    并用 library_file 已存的 media_source/release_group 覆盖——它们是入库时
    对**原始名称**的解析结果，比重命名后的文件名更可靠。
    probe 是否成功以"拿到过任一实测值"为据——完全失败时不冒充实测（尤其
    不能把 hdr=None 当成"测得 SDR"覆盖名称信息）。
    """
    if name_attrs is None:
        parsed = enrich(Path(file.file_path).stem)
        name_attrs = QualitySnapshot.model_validate(
            parsed.model_dump(exclude_defaults=True)
        )
        if file.media_source is not None:
            name_attrs.media_source = file.media_source
        if file.release_group is not None:
            name_attrs.release_group = file.release_group
    # 原盘（BDMV / VIDEO_TS / ISO）的片源由**结构**决定，压过名称解析：
    # attempt.quality 里的 "Blu-ray"（T4）会让一个 Remux 候选被判成升级，
    # 而 Remux 正是从这张盘剥出来的——那是降级，且默认会把原盘送进回收站
    # （issue #163）。remux 一并清零：布尔位同样来自名称，不能把 T6 拉回 T5。
    #
    # 它排在人工标注之前是有意的：原盘台账行自带非空片源，进不了标注候选池
    # （source_annotation._candidate_filter 只收未知与此前人工标注的行），
    # 整个标注体系对它不适用；能撞上这一条的只有本次修复之前留下的存量标注
    # ——而那份菜单里根本没有「原盘」这个选项，用户当时选的是次优解。
    if file.is_disc():
        name_attrs = name_attrs.model_copy(
            update={"media_source": DISC_SOURCE, "remux": False}
        )
    # 人工标注的片源是**权威值**：保护位的语义就是"自动名称解析不得覆盖"
    # （media-source-annotation.md），而 attempt.quality 恰恰是名称解析的产物。
    # 少这一条，任何一次快照重算都会把用户的标注静默抹回名称值，被解卡的
    # 「无法确认」单元又卡回去。remux 一并清零，与标注服务同一口径
    elif file.media_source_manual and file.media_source is not None:
        name_attrs = name_attrs.model_copy(
            update={"media_source": file.media_source, "remux": False}
        )
    probed = file.resolution is not None or file.bit_rate is not None
    return build_snapshot(
        name_attrs,
        probed=probed,
        probe_resolution=file.resolution,
        probe_hdr_label=file.hdr,
        probe_video_codec=file.video_codec,
        probe_bit_rate=file.bit_rate,
    )


async def fill_snapshots(
    session: AsyncSession, media_item_id: int, wanted_rows: list[WantedItem]
) -> None:
    """为一批（同条目的）工单构建并写入质量快照，只改内存行、不 commit。

    找不到在位文件或关键维度全部无法识别时写 ``{}``（已处理哨兵），
    避免回填任务对同一批行无限重试。
    """
    if not wanted_rows:
        return
    files = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.media_item_id == media_item_id,
                    LibraryFile.in_place(),
                )
            )
        )
        .scalars()
        .all()
    )
    by_unit: dict[tuple[int, int], list[LibraryFile]] = {}
    for file in files:
        by_unit.setdefault((file.season_number, file.episode_number), []).append(file)

    for wanted in wanted_rows:
        unit_files = by_unit.get((wanted.season_number, wanted.episode_number))
        if not unit_files:
            wanted.quality = {}
            continue
        best = max(unit_files, key=_file_sort_key)
        name_attrs: QualitySnapshot | None = None
        # 出处维度优先取投递时的种子名快照（attempt.quality）；快照文件与
        # 投递种子对应关系按"该单元当前 info_hash"定位——手工入库无 attempt
        if wanted.info_hash:
            attempt = (
                await session.execute(
                    select(SubscriptionDownloadAttempt).where(
                        SubscriptionDownloadAttempt.subscription_id
                        == wanted.subscription_id,
                        SubscriptionDownloadAttempt.info_hash == wanted.info_hash,
                    )
                )
            ).scalar_one_or_none()
            if attempt is not None and attempt.quality:
                name_attrs = QualitySnapshot.model_validate(attempt.quality)
        snapshot = snapshot_from_file(best, name_attrs)
        wanted.quality = snapshot.model_dump()
        wanted.updated_at = utcnow()


# ---------------------------------------------------------------------------
# 工单物化：给存量内容一个洗版基线的载体（quality-upgrade.md §13.1）
# ---------------------------------------------------------------------------


async def materialize_owned_wanted(
    session: AsyncSession, subscription: Subscription
) -> list[WantedItem]:
    """为期望集合 E 中**库里已有但没有工单**的单元补建 imported 工单行。

    订阅创建会跳过库里已有的单元（"无需重复下载"）——但洗版的全部状态
    （基线快照、证伪计数、搜索排期）都住在工单行上，存量内容没有行就
    没有洗版。物化幂等：唯一约束 ``uq_wanted_sub_season_episode`` 天然防重，
    已有行（含 in_scope=False 的历史行）一律不动。
    只改内存行 + flush、不 commit（跟随调用方事务）。返回新建的行。
    """
    from movieclaw_api.services.subscription.core import expected_units
    from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
    from movieclaw_db.repositories.media_repo import MediaItemRepository
    from movieclaw_media.models import MediaKind

    assert subscription.id is not None
    episodes = await MediaItemRepository(session).list_episodes(subscription.media_item_id)
    units = expected_units(
        MediaKind(subscription.kind),
        episodes,
        list(subscription.selected_seasons),
        subscription.follow_future,
    )
    owned = await LibraryFileRepository(session).owned_units(subscription.media_item_id)
    existing = {
        (row.season_number, row.episode_number)
        for row in (
            await session.execute(
                select(WantedItem).where(WantedItem.subscription_id == subscription.id)
            )
        ).scalars()
    }
    # imported_at 取该单元最早文件的入库时间（真实历史），查一次全条目文件
    file_added: dict[tuple[int, int], object] = {}
    for file in (
        await session.execute(
            select(LibraryFile).where(
                LibraryFile.media_item_id == subscription.media_item_id,
                LibraryFile.in_place(),
            )
        )
    ).scalars():
        key = (file.season_number, file.episode_number)
        if file.created_at is not None and (
            key not in file_added or file.created_at < file_added[key]  # type: ignore[operator]
        ):
            file_added[key] = file.created_at

    created: list[WantedItem] = []
    now = utcnow()
    for unit in units:
        key = (unit.season_number, unit.episode_number)
        if key not in owned or key in existing:
            continue
        row = WantedItem(
            subscription_id=subscription.id,
            media_item_id=subscription.media_item_id,
            season_number=unit.season_number,
            episode_number=unit.episode_number,
            status=WantedStatus.IMPORTED,
            in_scope=True,
            air_date=unit.air_date,
            imported_at=file_added.get(key, now),
        )
        session.add(row)
        created.append(row)
    if created:
        await session.flush()
        logger.info(
            "洗版物化：订阅 #%s 为 %d 个存量单元补建了工单行",
            subscription.id,
            len(created),
        )
    return created


# ---------------------------------------------------------------------------
# 洗版 attempt 的工单解析（状态机的关键差异点）
# ---------------------------------------------------------------------------


async def upgrade_attempt_wanted_rows(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
    *,
    in_scope_only: bool = True,
) -> list[WantedItem]:
    """洗版 attempt 照看的工单：按 attempt.units 定位 **imported** 行。

    缺口语义的关联（``wanted.info_hash == attempt.info_hash ∧ status 在途``）
    对洗版 attempt 恒为空——工单不重开、info_hash 直到确认前仍指向旧版本。
    死种巡检 / 试用目标判定 / 换源候选评估凡是要回答"这个 attempt 还在为谁
    工作"，洗版语义必须走本函数，否则洗版 attempt 会被当成"工单已闭合"
    错误完结（真实教训：投递后首个巡检 tick 即被打成 IMPORTED）。
    """
    units = {
        (int(u[0]), int(u[1]))
        for u in attempt.units
        if isinstance(u, list) and len(u) == 2
    }
    if not units:
        return []
    conditions = [
        WantedItem.subscription_id == attempt.subscription_id,
        WantedItem.status == WantedStatus.IMPORTED,
    ]
    if in_scope_only:
        conditions.append(WantedItem.in_scope.is_(True))  # type: ignore[attr-defined]
    rows = list((await session.execute(select(WantedItem).where(*conditions))).scalars())
    return [r for r in rows if (r.season_number, r.episode_number) in units]


# ---------------------------------------------------------------------------
# 一轮洗版（quality-upgrade.md §13.2）：手动触发的同步组合动作
# ---------------------------------------------------------------------------


async def run_upgrade_round(
    session: AsyncSession, subscription_id: int, *, rule_set_id: int | None = None
) -> dict:
    """「一轮洗版」：可选换组 → 物化 → 补快照 → 逐集体检 → 排期 → 踢搜索。

    同步执行、幂等（重复触发对已排期/在途单元无副作用）。返回体检报告
    （直接可渲染的结构，标签由后端生成）。中文错误全部走 BadRequest。
    """
    from movieclaw_api.exceptions import BadRequestException, NotFoundException
    from movieclaw_api.services.subscription.matching import (
        UPGRADE_FUSE_LIMIT,
        upgrade_ready,
    )
    from movieclaw_db.models import (
        ActivityType,
        DownloadAttemptStatus,
        SubscriptionActivity,
    )
    from movieclaw_db.repositories import SubscriptionRepository
    from movieclaw_matcher import provably_at_cutoff, quality_label, upgrade_target_label

    subscription = await session.get(Subscription, subscription_id)
    if subscription is None:
        raise NotFoundException(f"订阅不存在：#{subscription_id}")
    if subscription.status == "paused":
        raise BadRequestException("订阅已暂停，请先恢复追踪再触发洗版")

    # （可选）换组：目标组必须配置洗版目标——"选规则 + 触发"合成一步
    if rule_set_id is not None and rule_set_id != subscription.rule_set_id:
        rule_set = await session.get(RuleSet, rule_set_id)
        if rule_set is None:
            raise NotFoundException(f"规则组不存在：#{rule_set_id}")
        try:
            new_spec = RuleSetSpec.model_validate(rule_set.spec or {})
        except ValueError as exc:
            raise BadRequestException(f"规则组「{rule_set.name}」的参数无法解析：{exc}") from exc
        if new_spec.upgrade_source is None:
            raise BadRequestException(
                f"规则组「{rule_set.name}」未配置洗版目标，请先在规则组编辑器中选择「洗到哪一档」"
            )
        subscription.rule_set_id = rule_set_id
        subscription.updated_at = utcnow()

    specs = await _specs_for_subscriptions(session, {subscription_id})
    spec = specs.get(subscription_id)
    if spec is None:
        raise BadRequestException("当前规则组的参数无法解析，请在规则组页面重新保存修正")
    if spec.upgrade_source is None:
        raise BadRequestException(
            "当前规则组未配置洗版目标；在请求中带上一个已配置洗版目标的规则组，"
            "或先到「设置 → 订阅 → 规则组」配置"
        )

    # 物化存量单元 + 当场补快照（不等回填 tick——体检要看到每一集）
    await materialize_owned_wanted(session, subscription)
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == subscription_id,
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                )
            )
        ).scalars()
    )
    pending_snapshot = [
        w for w in rows if w.status == WantedStatus.IMPORTED and w.quality is None
    ]
    if pending_snapshot:
        await fill_snapshots(session, subscription.media_item_id, pending_snapshot)

    # 在途洗版单元集合（体检里如实展示"已在洗"）
    in_flight: set[tuple[int, int]] = set()
    for attempt in (
        await session.execute(
            select(SubscriptionDownloadAttempt).where(
                SubscriptionDownloadAttempt.subscription_id == subscription_id,
                SubscriptionDownloadAttempt.purpose == "upgrade",
                SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                    (
                        DownloadAttemptStatus.ACTIVE,
                        DownloadAttemptStatus.REPLACEMENT_PENDING,
                        DownloadAttemptStatus.TRIAL,
                        DownloadAttemptStatus.CLEANUP_PENDING,
                        DownloadAttemptStatus.COMPLETED,
                    )
                ),
            )
        )
    ).scalars():
        in_flight.update(
            (int(u[0]), int(u[1]))
            for u in attempt.units
            if isinstance(u, list) and len(u) == 2
        )

    # 逐集体检 + 可洗单元排期（用户显式触发 = 补旧级 priority 0，立即到期；
    # 熔断中的视为人工介入解除，与「立即搜索」同语义）
    now = utcnow()
    target_label = upgrade_target_label(spec) or ""
    units: list[dict] = []
    counts = {"upgradable": 0, "at_cutoff": 0, "in_flight": 0, "not_comparable": 0, "missing": 0}
    for wanted in sorted(rows, key=lambda w: (w.season_number, w.episode_number)):
        unit = (wanted.season_number, wanted.episode_number)
        current_label: str | None = None
        if wanted.status != WantedStatus.IMPORTED:
            state = "missing"
        elif not wanted.quality:
            state = "not_comparable"
        else:
            snapshot = QualitySnapshot.model_validate(wanted.quality)
            current_label = quality_label(snapshot, spec)
            if unit in in_flight:
                state = "in_flight"
            elif upgrade_ready(wanted, spec, now=now) or (
                wanted.upgrade_verify_failures >= UPGRADE_FUSE_LIMIT
                and _provably_below(snapshot, spec)
            ):
                if wanted.upgrade_verify_failures >= UPGRADE_FUSE_LIMIT:
                    from movieclaw_api.services.system_notice import resolve_notices

                    wanted.upgrade_verify_failures = 0
                    await resolve_notices(
                        session,
                        prefix=(
                            f"subscription.upgrade:{subscription_id}:"
                            f"{wanted.season_number}:{wanted.episode_number}"
                        ),
                    )
                wanted.priority = 0
                wanted.next_search_at = now
                wanted.updated_at = now
                state = "upgradable"
            elif provably_at_cutoff(snapshot, spec):
                state = "at_cutoff"
            else:
                # 第三态：证明不了低于目标也证明不了已达标（如分辨率位次
                # 未知、同分辨率但片源未知）——如实报"无法确认"，不冒充达标
                state = "not_comparable"
        counts[state] += 1
        units.append(
            {
                "season_number": wanted.season_number,
                "episode_number": wanted.episode_number,
                "state": state,
                "current_label": current_label,
                "target_label": target_label,
            }
        )

    summary_parts = []
    if counts["upgradable"]:
        summary_parts.append(f"{counts['upgradable']} 个单元可洗版，已排入立即搜索")
    if counts["in_flight"]:
        summary_parts.append(f"{counts['in_flight']} 个已在洗版中")
    if counts["at_cutoff"]:
        summary_parts.append(f"{counts['at_cutoff']} 个已达目标")
    if counts["not_comparable"]:
        summary_parts.append(f"{counts['not_comparable']} 个无法确认当前版本档位")
    if counts["missing"]:
        summary_parts.append(f"{counts['missing']} 个缺失将照常下载")
    summary = "；".join(summary_parts) if summary_parts else "没有可处理的单元"

    await session.commit()
    await SubscriptionRepository(session).add_activity(
        SubscriptionActivity(
            subscription_id=subscription_id,
            type=ActivityType.ADJUSTED,
            message=f"用户触发一轮洗版（目标 {target_label}）：{summary}",
            payload={
                "reason": "upgrade_run",
                "counts": counts,
                "rule_set_id": subscription.rule_set_id,
            },
        )
    )
    if counts["upgradable"] or counts["missing"]:
        from movieclaw_api.services.subscription.wanted_search import kick_search_soon

        kick_search_soon()
    return {
        "target_label": target_label,
        "rule_set_id": subscription.rule_set_id,
        "summary": summary,
        "counts": counts,
        "units": units,
    }


def _provably_below(snapshot, spec) -> bool:
    from movieclaw_matcher import provably_below_cutoff

    return provably_below_cutoff(snapshot, spec)


# ---------------------------------------------------------------------------
# 入库验证：实测说了算（quality-upgrade.md §6.3）
# ---------------------------------------------------------------------------

def _file_from_attempt(file: LibraryFile, attempt: SubscriptionDownloadAttempt) -> bool:
    """该库文件是否来自这次洗版投递。

    首选入库来源精确匹配（监听导入会带 site/torrent）；扫描收编的文件没有
    来源信息，退而按时间关联（attempt 创建之后才出现的文件）。
    """
    if file.site_id and attempt.site_id:
        return file.site_id == attempt.site_id and file.torrent_id == attempt.torrent_id
    return file.created_at is not None and attempt.created_at is not None and (
        file.created_at >= attempt.created_at
    )


async def verify_upgrades(session: AsyncSession, media_item_id: int) -> None:
    """洗版入库验证：对该条目在途洗版单元，用实测新快照裁决确认/证伪。

    在库存对账的同一钩子点运行（任何入库路径都会触发），实测说了算：
    - **确认**（新最优文件档位严格高于基线）：刷新快照与 info_hash 关联、
      旧版本文件进回收站、旧任务交给换源清理状态机（CLEANUP_PENDING，
      由 download_progress 巡检以 H&R/所有权/文件重叠证据安全清理）、
      写 UPGRADED 活动并推送；手工塞入的更优文件同样确认（无 attempt 记账）。
    - **证伪**（洗版投递的文件实测不构成升级）：新文件移入回收站、
      attempt 置 FAILED 进排除清单、熔断计数 +1，连续达阈值转入长冷却
      并出 system_notice 提示人工介入。
    """
    async with _verify_lock:
        await _verify_upgrades_locked(session, media_item_id)


async def _verify_upgrades_locked(session: AsyncSession, media_item_id: int) -> None:
    from movieclaw_api.services.subscription.matching import (
        UPGRADE_FUSE_COOLDOWN,
        UPGRADE_FUSE_LIMIT,
    )
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        SubscriptionActivity,
    )
    from movieclaw_db.models.subscription_activity import ActivityType
    from movieclaw_db.repositories import SubscriptionRepository
    from movieclaw_matcher import quality_label

    # 该条目所有"已入库且有基线"的单元
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.media_item_id == media_item_id,
                    WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    WantedItem.quality.isnot(None),  # type: ignore[union-attr]
                )
            )
        ).scalars()
    )
    rows = [w for w in rows if w.quality]  # 排除 {} 哨兵
    if not rows:
        return
    specs = await _specs_for_subscriptions(session, {w.subscription_id for w in rows})

    # 在途洗版 attempt：{(sub_id, unit) -> attempt}
    attempts = list(
        (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id.in_(  # type: ignore[union-attr]
                        {w.subscription_id for w in rows}
                    ),
                    SubscriptionDownloadAttempt.purpose == "upgrade",
                    SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                        (
                            DownloadAttemptStatus.ACTIVE,
                            DownloadAttemptStatus.REPLACEMENT_PENDING,
                            DownloadAttemptStatus.TRIAL,
                            DownloadAttemptStatus.COMPLETED,
                        )
                    ),
                )
            )
        ).scalars()
    )
    attempts_by_unit: dict[tuple[int, tuple[int, int]], list[SubscriptionDownloadAttempt]] = {}
    for attempt in attempts:
        for u in attempt.units:
            if isinstance(u, list) and len(u) == 2:
                attempts_by_unit.setdefault(
                    (attempt.subscription_id, (int(u[0]), int(u[1]))), []
                ).append(attempt)

    files = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.media_item_id == media_item_id,
                    LibraryFile.in_place(),
                )
            )
        ).scalars()
    )
    files_by_unit: dict[tuple[int, int], list[LibraryFile]] = {}
    for file in files:
        files_by_unit.setdefault((file.season_number, file.episode_number), []).append(file)

    # 已证伪 attempt 的来源集合：它们的文件（回收站失败残留 / 意外重复入库）
    # 必须隔离清理，绝不参与最优选择——否则证伪文件会借文件名解析
    # "重生"为一次手工升级
    failed_sources: set[tuple[int, str, str]] = {
        (a.subscription_id, a.site_id, a.torrent_id)
        for a in (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id.in_(  # type: ignore[union-attr]
                        {w.subscription_id for w in rows}
                    ),
                    SubscriptionDownloadAttempt.purpose == "upgrade",
                    SubscriptionDownloadAttempt.status == DownloadAttemptStatus.FAILED,
                )
            )
        ).scalars()
        if a.site_id and a.torrent_id
    }

    from movieclaw_api.services.library.recycle import recycle_file
    from movieclaw_matcher import QualitySnapshot

    item = await session.get(MediaItem, media_item_id)
    repo = SubscriptionRepository(session)

    def _recycle_trigger(w: WantedItem) -> dict:
        """回收审计快照（library-file-recycle.md §4）：存展示用快照不外键。"""
        return {
            "kind": "subscription",
            "id": w.subscription_id,
            "label": f"《{item.title}》订阅洗版" if item is not None else "订阅洗版",
        }

    for wanted in rows:
        unit = (wanted.season_number, wanted.episode_number)
        unit_files = files_by_unit.get(unit) or []
        if len(unit_files) < 2 and (wanted.subscription_id, unit) not in attempts_by_unit:
            continue  # 单版本且无在途洗版：没有可裁决的事
        quarantine = [
            f
            for f in unit_files
            if f.site_id
            and (wanted.subscription_id, f.site_id, f.torrent_id) in failed_sources
        ]
        if quarantine and len(quarantine) < len(unit_files):
            for file in quarantine:
                outcome = await recycle_file(
                    session,
                    file,
                    reason="upgrade_refuted",
                    trigger=_recycle_trigger(wanted),
                    note="洗版证伪残留隔离：来自已证伪来源的文件不参与版本裁决",
                )
                if outcome == "already_gone":
                    await session.delete(file)
            await session.commit()
            unit_files = [f for f in unit_files if f not in quarantine]
        baseline = QualitySnapshot.model_validate(wanted.quality)
        spec = specs.get(wanted.subscription_id)
        # 同一单元可能同时挂着旧洗版源（等替换）与试用源两个 attempt：优先
        # 选与单元文件来源精确对应的那个（site/torrent 匹配），避免把试用源
        # 交付的文件记到死源头上；无精确匹配时取最新创建的
        unit_attempts = attempts_by_unit.get((wanted.subscription_id, unit), [])
        attempt = None
        for att in sorted(
            unit_attempts, key=lambda a: (a.created_at or utcnow(), a.id or 0), reverse=True
        ):
            if att.site_id and any(
                f.site_id == att.site_id and f.torrent_id == att.torrent_id
                for f in files_by_unit.get(unit, [])
            ):
                attempt = att
                break
        if attempt is None and unit_attempts:
            attempt = max(
                unit_attempts, key=lambda a: (a.created_at or utcnow(), a.id or 0)
            )
        # 混合投递（缺口+洗版同一 attempt）里，缺口单元也在 attempt.units 中，
        # 但它是靠这次投递才入库的——不是洗版目标，绝不能拿它走证伪分支。
        # 判据：单元必须在 attempt 创建之前就已入库，才算该 attempt 要洗的对象
        if (
            attempt is not None
            and (
                wanted.imported_at is None
                or attempt.created_at is None
                or wanted.imported_at > attempt.created_at
            )
        ):
            attempt = None

        # 逐文件算快照，按**快照**找最优（名称来源：来自洗版投递的文件用
        # attempt.quality——文件行本身可能还没有片源信息）
        from movieclaw_matcher import candidate_ladder_rank

        # 选最优用**规则组自己的阶梯**（拿不到 spec 时回落中性序）：与下面的
        # 判定同口径。此前固定按 (分辨率, 片源) 选，多维阶梯下会出现"选中的
        # 文件不是判定认为最优的那个"，全靠 file.id 兜底才碰巧对
        rank_spec = spec or _NEUTRAL_SPEC
        best_file: LibraryFile | None = None
        best_snapshot: QualitySnapshot | None = None
        best_key: tuple[tuple[int, ...], int] = ((), -1)
        snapshots_by_file: dict[int, QualitySnapshot] = {}
        for file in unit_files:
            name_attrs = None
            if attempt is not None and attempt.quality and _file_from_attempt(file, attempt):
                name_attrs = QualitySnapshot.model_validate(attempt.quality)
            snapshot = snapshot_from_file(file, name_attrs)
            if file.id is not None:
                snapshots_by_file[file.id] = snapshot
            key = (candidate_ladder_rank(snapshot, rank_spec), file.id or 0)
            if key > best_key:
                best_file, best_snapshot, best_key = file, snapshot, key

        if best_file is None or best_snapshot is None or spec is None:
            continue

        # 验证是不设停止线的纯序比较：实测新快照严格优于基线即确认——
        # 手工塞入超过洗版目标的版本同样是合法升级（quality-upgrade.md §6.3）
        upgrade_enabled = spec.upgrade_source is not None
        if _better(best_snapshot, baseline, spec):
            if not upgrade_enabled:
                # 未开洗版：只静默把基线刷新为实测最优（保持台账真实，日后
                # 开洗版立即可用），**绝不**替换/移动用户的文件——删除性动作
                # 必须有洗版目标这个显式 opt-in
                wanted.quality = best_snapshot.model_dump()
                wanted.updated_at = utcnow()
                await session.commit()
                continue
            # ---- 确认升级 ----
            now = utcnow()
            old_hash = wanted.info_hash
            new_label = quality_label(best_snapshot, spec)
            old_label = quality_label(baseline, spec)
            wanted.quality = best_snapshot.model_dump()
            wanted.upgrade_verify_failures = 0
            wanted.updated_at = now
            # 该单元此前若因连续证伪出过熔断提示，升级成功即问题消失
            from movieclaw_api.services.system_notice import resolve_notices

            await resolve_notices(
                session,
                prefix=(
                    f"subscription.upgrade:{wanted.subscription_id}:"
                    f"{wanted.season_number}:{wanted.episode_number}"
                ),
            )
            trash_paths: list[str] = []
            if attempt is not None and _file_from_attempt(best_file, attempt):
                wanted.info_hash = attempt.info_hash
                attempt.status = DownloadAttemptStatus.IMPORTED
                attempt.cleanup_note = "洗版完成：新版本已入库"
                attempt.updated_at = now
                # 旧任务交给换源清理状态机（H&R/所有权/文件重叠证据齐备才删）。
                # 前置硬条件：旧 attempt 必须**不再服务任何其他单元**——旧
                # S01 整季包提供了 E01–E10 而本次只洗 E01 时，把整包送进清理
                # 会杀掉其余 9 集的做种（真实风险场景，绝不允许）
                still_serving = (
                    await session.execute(
                        select(WantedItem.id).where(
                            WantedItem.subscription_id == wanted.subscription_id,
                            WantedItem.info_hash == old_hash,
                            WantedItem.id != wanted.id,
                        )
                    )
                ).first()
                if old_hash and old_hash != attempt.info_hash and still_serving is None:
                    old_attempt = (
                        await session.execute(
                            select(SubscriptionDownloadAttempt).where(
                                SubscriptionDownloadAttempt.subscription_id
                                == wanted.subscription_id,
                                SubscriptionDownloadAttempt.info_hash == old_hash,
                            )
                        )
                    ).scalar_one_or_none()
                    if old_attempt is not None and old_attempt.status not in (
                        DownloadAttemptStatus.SUPERSEDED,
                        DownloadAttemptStatus.RETAINED,
                        DownloadAttemptStatus.CANCELLED,
                    ):
                        if attempt.replaces_attempt_id in (None, old_attempt.id):
                            attempt.replaces_attempt_id = old_attempt.id
                            old_attempt.status = DownloadAttemptStatus.CLEANUP_PENDING
                        else:
                            # 整季包一次替换多个来源不同的旧单集时，replaces
                            # 指针只能指向一个旧 attempt——清理巡检靠这个指针
                            # 找"新源"读取证据，指不到的旧 attempt 挂进
                            # CLEANUP_PENDING 只会永远等不到清理。其余旧任务
                            # 保守保留做种（与"证据不足不删数据"的换源铁律一致）
                            old_attempt.status = DownloadAttemptStatus.RETAINED
                            old_attempt.cleanup_note = (
                                "洗版整季替换：多个旧任务无法自动比对文件重叠，"
                                "保留做种，可在活动页手动清理"
                            )
                        old_attempt.updated_at = now
            # 旧版本文件进回收站（quality-upgrade.md §7.1 / library-file-recycle.md），
            # 一律按保留期倒计时；kept_in_place 仅剩移动失败的降级形态。
            # 「保留共存」（upgrade_keep_old，收藏家模式）：旧版本不进回收站，
            # 多版本并存——升级本身（基线/关联/活动）照常
            kept_in_place = 0
            if not spec.upgrade_keep_old:
                for file in unit_files:
                    if file.id == best_file.id:
                        continue
                    outcome = await recycle_file(
                        session,
                        file,
                        reason="upgrade_replaced",
                        trigger=_recycle_trigger(wanted),
                        note=f"洗版替换：{old_label} → {new_label}",
                    )
                    if outcome == "already_gone":
                        await session.delete(file)  # 文件早没了：行删掉与磁盘一致
                    elif outcome == "moved_to_trash":
                        trash_paths.append(file.file_path)
                    else:
                        kept_in_place += 1  # 移动失败降级：原地待回收，照常倒计时
            await session.commit()
            await repo.add_activity(
                SubscriptionActivity(
                    subscription_id=wanted.subscription_id,
                    wanted_item_id=wanted.id,
                    type=ActivityType.UPGRADED,
                    message=(
                        f"{_unit_text(wanted)}已洗版：{old_label} → {new_label}"
                        + ("，旧版本已移入回收站（保留 7 天）" if trash_paths else "")
                        + (
                            "；部分旧版本移入回收站失败，已原地转入待回收"
                            "（按保留期自动清理）——可在库详情恢复或立即清理"
                            if kept_in_place
                            else ""
                        )
                        + ("；旧版本按规则保留共存" if spec.upgrade_keep_old else "")
                    ),
                    payload={
                        "from": old_label,
                        "to": new_label,
                        "trash_paths": trash_paths,
                        "kept_in_place": kept_in_place,
                        "kept_coexisting": spec.upgrade_keep_old,
                        "units": [[wanted.season_number, wanted.episode_number]],
                    },
                )
            )
            if item is not None:
                from movieclaw_api.services.channel_push import (
                    notify_channels,
                    tmdb_push_image_url,
                )

                notify_channels(
                    f"✨ 已洗版:《{item.title}》{_unit_text(wanted)}\n{old_label} → {new_label}",
                    event="upgraded",
                    image_url=tmdb_push_image_url(item.backdrop_path, item.poster_path),
                )
            logger.info(
                "洗版完成：条目 #%s %s %s → %s",
                media_item_id,
                _unit_text(wanted),
                old_label,
                new_label,
            )
        elif attempt is not None and any(
            _file_from_attempt(f, attempt) for f in unit_files
        ):
            # ---- 洗版投递的文件已入库但不构成升级：区分"造假"与"被抢先" ----
            now = utcnow()
            trash_paths = []
            from_attempt = [f for f in unit_files if _file_from_attempt(f, attempt)]
            others = [f for f in unit_files if not _file_from_attempt(f, attempt)]
            if attempt.manual:
                # 手动选种（§13.8）：用户显式挑的文件绝不能被系统丢弃。
                # 未能证明更优 → 新旧版本共存保留，不计熔断、不进排除清单。
                # 基线刷新为实测最优——手选常常是给"无法确认"单元补一个
                # 命名可信的版本，新文件档位可知时基线随之修复
                attempt.status = DownloadAttemptStatus.RETAINED
                attempt.cleanup_note = (
                    "手动选种版本已入库：实测未能证明优于原版本，新旧版本共存"
                    "保留（未自动替换）；不需要的版本可在库详情删除"
                )
                attempt.updated_at = now
                wanted.quality = best_snapshot.model_dump()
                wanted.updated_at = now
                await session.commit()
                await repo.add_activity(
                    SubscriptionActivity(
                        subscription_id=wanted.subscription_id,
                        wanted_item_id=wanted.id,
                        type=ActivityType.UPGRADE_VERIFY_FAILED,
                        message=(
                            f"{_unit_text(wanted)}手选版本已入库，但实测未能证明更优："
                            "新旧版本共存保留，未自动替换；不需要的版本可在库详情删除"
                        ),
                        payload={
                            "reason": "manual_retained",
                            "units": [[wanted.season_number, wanted.episode_number]],
                        },
                    )
                )
                continue
            # 防御：旧版本文件必须还在位才移走证伪文件（宁可留下劣质版本，
            # 也绝不把单元清空）
            if others:
                for file in from_attempt:
                    outcome = await recycle_file(
                        session,
                        file,
                        reason="upgrade_refuted",
                        trigger=_recycle_trigger(wanted),
                        note="洗版证伪：实测档位不高于当前版本",
                    )
                    if outcome == "already_gone":
                        await session.delete(file)
                    elif outcome == "moved_to_trash":
                        trash_paths.append(file.file_path)
            # 资源是否诚实：投递文件的实测快照没有低于其声称档位 → 资源没
            # 撒谎，只是基线在下载期间被更优版本（如手工入库）抢先刷高。
            # 诚实资源不计熔断、不进排除清单、不写证伪活动——错误的惩罚会
            # 把好资源永久拉黑
            claimed = QualitySnapshot.model_validate(attempt.quality or {})
            attempt_measured = [
                snapshots_by_file[f.id] for f in from_attempt if f.id in snapshots_by_file
            ]
            honest = any(not _better(claimed, m, spec) for m in attempt_measured)
            if honest:
                attempt.status = DownloadAttemptStatus.CANCELLED
                attempt.cleanup_note = (
                    "洗版下载完成时基线已被更优版本抢先，本次结果不再需要；"
                    "保留下载器任务"
                )
                attempt.updated_at = now
                await session.commit()
                logger.info(
                    "洗版抢先收口：条目 #%s %s 的洗版结果已被更优版本取代",
                    media_item_id,
                    _unit_text(wanted),
                )
                continue
            attempt.status = DownloadAttemptStatus.FAILED
            attempt.cleanup_note = "洗版证伪：实测档位不高于当前版本，候选已排除"
            attempt.updated_at = now
            wanted.upgrade_verify_failures += 1
            fused = wanted.upgrade_verify_failures >= UPGRADE_FUSE_LIMIT
            if fused:
                wanted.next_search_at = now + UPGRADE_FUSE_COOLDOWN
            wanted.updated_at = now
            await session.commit()
            await repo.add_activity(
                SubscriptionActivity(
                    subscription_id=wanted.subscription_id,
                    wanted_item_id=wanted.id,
                    type=ActivityType.UPGRADE_VERIFY_FAILED,
                    message=(
                        f"{_unit_text(wanted)}洗版候选证伪：标称 "
                        f"{quality_label(QualitySnapshot.model_validate(attempt.quality or {}))}，"
                        f"实测为 {quality_label(best_snapshot)}，已排除该资源"
                        + (
                            f"；连续 {wanted.upgrade_verify_failures} 次证伪，"
                            "该单元洗版转入 30 天冷却"
                            if fused
                            else ""
                        )
                    ),
                    payload={
                        "site_id": attempt.site_id,
                        "torrent_id": attempt.torrent_id,
                        "claimed": attempt.quality,
                        "measured": best_snapshot.model_dump(exclude_defaults=True),
                        "verify_failures": wanted.upgrade_verify_failures,
                    },
                )
            )
            if fused:
                from movieclaw_api.services.system_notice import upsert_notice
                from movieclaw_db.models.system_notice import NoticeSeverity

                await upsert_notice(
                    session,
                    dedupe_key=(
                        f"subscription.upgrade:{wanted.subscription_id}:"
                        f"{wanted.season_number}:{wanted.episode_number}"
                    ),
                    severity=NoticeSeverity.WARNING,
                    source="subscription",
                    title="洗版连续证伪，已暂停该单元",
                    message=(
                        f"《{item.title if item else '未知条目'}》{_unit_text(wanted)}"
                        f"连续 {wanted.upgrade_verify_failures} 次抓到标称与实测不符的资源，"
                        "洗版已转入 30 天冷却。可在订阅详情检查候选质量或调整规则组。"
                    ),
                )
            logger.warning(
                "洗版证伪：条目 #%s %s 标称与实测不符（连续 %d 次）",
                media_item_id,
                _unit_text(wanted),
                wanted.upgrade_verify_failures,
            )


def _better(snapshot, baseline, spec) -> bool:
    """实测快照是否严格优于基线（不设停止线的纯序比较，供确认路径复用）。

    **必须与抓取判定走同一条阶梯**（``compare_ladder``）。这里曾经手写
    "先比分辨率、再比片源"，对 §14 加进阶梯的编码/HDR/平台三个维度视而不见：
    抓取端认定的合法升级，到验证端一律判否——文件进回收站，而"诚实资源"判定
    又不会把它拉黑，于是下一轮再抓同一个候选，下载 → 回收 → 再下载 无限循环。
    不可比（某位单侧未知）保持判否，与旧实现一致。
    """
    from movieclaw_matcher import compare_ladder, ladder_vector

    return (
        compare_ladder(ladder_vector(snapshot, spec), ladder_vector(baseline, spec)) == 1
    )


def _unit_text(wanted: WantedItem) -> str:
    if wanted.season_number == 0 and wanted.episode_number == 0:
        return "正片"
    return f"S{wanted.season_number:02d}E{wanted.episode_number:02d}"


# ---------------------------------------------------------------------------
# 洗版搜索调度（quality-upgrade.md §6.4：被动为主，主动极低频）
# ---------------------------------------------------------------------------


async def _specs_for_subscriptions(
    session: AsyncSession, subscription_ids: set[int]
) -> dict[int, RuleSetSpec | None]:
    """{subscription_id: 解析后的规则组 spec}；解析失败为 None（跳过洗版）。"""
    if not subscription_ids:
        return {}
    subs = list(
        (
            await session.execute(
                select(Subscription).where(Subscription.id.in_(subscription_ids))  # type: ignore[union-attr]
            )
        ).scalars()
    )
    rule_ids = {s.rule_set_id for s in subs}
    rules = {
        r.id: r
        for r in (
            await session.execute(select(RuleSet).where(RuleSet.id.in_(rule_ids)))  # type: ignore[union-attr]
        ).scalars()
    }
    result: dict[int, RuleSetSpec | None] = {}
    for sub in subs:
        rule = rules.get(sub.rule_set_id)
        try:
            result[sub.id] = RuleSetSpec.model_validate(rule.spec or {} if rule else {})
        except ValueError:
            result[sub.id] = None
    return result


async def upgrading_counts(
    session: AsyncSession, subscription_ids: list[int]
) -> dict[int, int]:
    """批量派生每个订阅「洗版中」的单元数（订阅列表/海报墙用）。

    与详情页的 ``upgrade_ready`` 完全同口径（可证明低于目标且未熔断），
    否则海报墙亮着青点、点进详情却说没在洗。只扫配了洗版目标的订阅的
    已入库单元，规模与洗版启用面成正比，列表接口可以承受。
    """
    from movieclaw_api.services.subscription.matching import upgrade_ready

    specs = await _specs_for_subscriptions(session, set(subscription_ids))
    enabled = {
        sid
        for sid, spec in specs.items()
        if spec is not None and spec.upgrade_source is not None
    }
    if not enabled:
        return {}
    now = utcnow()
    counts: dict[int, int] = {}
    for w in (
        await session.execute(
            select(WantedItem).where(
                WantedItem.subscription_id.in_(enabled),  # type: ignore[attr-defined]
                WantedItem.status == WantedStatus.IMPORTED,
                WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                WantedItem.quality.isnot(None),  # type: ignore[union-attr]
            )
        )
    ).scalars():
        spec = specs[w.subscription_id]
        assert spec is not None  # enabled 集合已保证
        if upgrade_ready(w, spec, now=now):
            counts[w.subscription_id] = counts.get(w.subscription_id, 0) + 1
    return counts


async def arm_upgrade_candidates(session: AsyncSession, wanted_rows: list[WantedItem]) -> int:
    """给可洗版的 imported 单元排洗版搜索（首搜在 24h 内错峰）。

    只改内存行、不 commit（跟随调用方事务）。触发点：入库对账落快照后、
    存量回填后。被动匹配不依赖排期（上下文实时读 spec），排期只服务
    主动搜索兜底。返回排期数。
    """
    import random

    from movieclaw_api.services.subscription.matching import (
        UPGRADE_FIRST_SEARCH_SPREAD_HOURS,
        UPGRADE_PRIORITY,
        upgrade_ready,
    )

    rows = [w for w in wanted_rows if w.status == WantedStatus.IMPORTED and w.in_scope]
    if not rows:
        return 0
    specs = await _specs_for_subscriptions(session, {w.subscription_id for w in rows})
    now = utcnow()
    armed = 0
    for wanted in rows:
        spec = specs.get(wanted.subscription_id)
        if spec is None or spec.upgrade_source is None:
            continue
        if not upgrade_ready(wanted, spec, now=now):
            continue
        wanted.priority = UPGRADE_PRIORITY
        wanted.next_search_at = now + timedelta(
            seconds=random.uniform(0, UPGRADE_FIRST_SEARCH_SPREAD_HOURS * 3600)
        )
        wanted.search_attempts = 0  # 调度字段进入洗版语义，退避曲线重新起步
        wanted.updated_at = now
        armed += 1
    return armed


async def reset_upgrade_search_now(session: AsyncSession, subscription_id: int) -> int:
    """「立即搜索」的洗版半边：把该订阅可洗的单元全部重置为立刻到期。

    只碰"当下确实可洗"的单元（到顶/不可比/熔断冷却中的不碰）。
    只改内存行、不 commit（跟随调用方事务）。返回重置数。
    """
    from movieclaw_api.services.subscription.matching import (
        UPGRADE_FUSE_LIMIT,
        UPGRADE_PRIORITY,
    )
    from movieclaw_matcher import provably_below_cutoff

    specs = await _specs_for_subscriptions(session, {subscription_id})
    spec = specs.get(subscription_id)
    if spec is None or spec.upgrade_source is None:
        return 0
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == subscription_id,
                    WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    WantedItem.quality.isnot(None),  # type: ignore[union-attr]
                )
            )
        ).scalars()
    )
    now = utcnow()
    reset = 0
    for wanted in rows:
        if not wanted.quality:
            continue
        if not provably_below_cutoff(QualitySnapshot.model_validate(wanted.quality), spec):
            continue
        if wanted.upgrade_verify_failures >= UPGRADE_FUSE_LIMIT:
            # 「立即搜索」正是熔断提示（system_notice）要求的人工介入：
            # 解除熔断、清零计数重新观察，并熄灭对应提示
            from movieclaw_api.services.system_notice import resolve_notices

            wanted.upgrade_verify_failures = 0
            await resolve_notices(
                session,
                prefix=(
                    f"subscription.upgrade:{subscription_id}:"
                    f"{wanted.season_number}:{wanted.episode_number}"
                ),
            )
        wanted.priority = UPGRADE_PRIORITY
        wanted.next_search_at = now
        wanted.updated_at = now
        reset += 1
    return reset


async def postpone_upgrade_wanted(
    session: AsyncSession, media_id: int, *, delay: timedelta | None, count_attempt: bool
) -> int:
    """给该条目下到期未洗成的洗版单元排下一次搜索（worker 退避记账的洗版半边）。

    自愈：单元已不可洗（到顶/规则组撤销洗版/熔断冷却未到）→ 解除排期
    （next_search_at=None），不再打扰站点。返回顺延数。
    """
    from movieclaw_api.services.subscription.matching import (
        upgrade_backoff_delay,
        upgrade_ready,
    )

    now = utcnow()
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.media_item_id == media_id,
                    WantedItem.status == WantedStatus.IMPORTED,
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    WantedItem.next_search_at.isnot(None),  # type: ignore[union-attr]
                    WantedItem.next_search_at <= now,  # type: ignore[operator]
                )
            )
        ).scalars()
    )
    if not rows:
        return 0
    specs = await _specs_for_subscriptions(session, {w.subscription_id for w in rows})
    from movieclaw_api.services.subscription.matching import UPGRADE_FUSE_LIMIT

    postponed = 0
    for wanted in rows:
        spec = specs.get(wanted.subscription_id)
        if spec is None or spec.upgrade_source is None or not upgrade_ready(
            wanted, spec, now=now
        ):
            wanted.next_search_at = None  # 自愈解除排期
            wanted.updated_at = now
            continue
        # 熔断冷却已到期的单元走到这里说明它重新参赛：计数清零重新观察——
        # 否则常规 7d 退避会被 upgrade_ready 误判成"仍在冷却"，把被动匹配
        # （洗版主通道）无限期关掉
        if wanted.upgrade_verify_failures >= UPGRADE_FUSE_LIMIT:
            wanted.upgrade_verify_failures = 0
        if count_attempt:
            wanted.next_search_at = now + upgrade_backoff_delay(wanted.search_attempts)
            wanted.search_attempts += 1
            wanted.last_search_at = now
        else:
            wanted.next_search_at = now + (delay or timedelta(minutes=15))
        wanted.updated_at = now
        postponed += 1
    await session.commit()
    return postponed


def _snapshot_version(quality: dict) -> int:
    """快照结构版本；缺键或值损坏一律按 v1（重算一次即修好，绝不让巡检抛错）。"""
    try:
        return int(quality.get("v", 1))
    except (TypeError, ValueError):
        return 1


async def _refill_stale_snapshots(session: AsyncSession, upgrade_ids: set[int]) -> int:
    """把结构版本落后的存量快照逐批重算（quality-upgrade.md §16.3）。

    新增可比维度（如 v2 的 video_codec / platforms）后，老快照在这些位上恒为
    未知：一旦用户把新维度加进洗版阶梯，比较会在该位单侧未知而截断，这些单元
    会**大面积变成不可比、洗版停摆**。重算是纯 DB 变换（probe 各列都在
    library_file 里），逐批做即可。

    ``{}`` 哨兵不在重算范围：它表示"没有在位文件或什么都识别不出"，与新增
    维度无关，重算只会白白重扫。
    """
    global _stale_snapshot_cursor
    rows = list(
        (
            await session.execute(
                select(WantedItem)
                .join(Subscription, Subscription.id == WantedItem.subscription_id)
                .where(
                    WantedItem.id > _stale_snapshot_cursor,  # type: ignore[operator]
                    WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    WantedItem.quality.isnot(None),  # type: ignore[union-attr]
                    Subscription.rule_set_id.in_(upgrade_ids),  # type: ignore[union-attr]
                )
                .order_by(WantedItem.id)  # type: ignore[arg-type]
                .limit(_BACKFILL_BATCH * 4)
            )
        ).scalars()
    )
    _stale_snapshot_cursor = (rows[-1].id or 0) if rows else 0
    stale = [
        row
        for row in rows
        if row.quality and _snapshot_version(row.quality) < SNAPSHOT_VERSION
    ]
    if not stale:
        return 0
    by_media: dict[int, list[WantedItem]] = {}
    for row in stale:
        by_media.setdefault(row.media_item_id, []).append(row)
    # 与入库验证互斥：验证确认升级时会把旧文件移进回收站（in_place 变假）。
    # 重算恰好撞进这个窗口的话，fill_snapshots 会因为"该单元没有在位文件"
    # 写下 {} 哨兵，把一条好基线换成"无法识别"——而 {} 又被排除在重算之外，
    # 单元就此永久退出洗版。回填 tick 900 秒一轮、每轮 50 行，锁竞争可忽略
    previous = {row.id: row.quality for row in stale}
    async with _verify_lock:
        for media_item_id, wanted_rows in by_media.items():
            await fill_snapshots(session, media_item_id, wanted_rows)
    # 双保险：重算**永远不把已有基线降级成哨兵**。文件此刻不在位（卸载的
    # 媒体库、正在搬运）是暂时状态，真相是"这次测不了"而不是"识别不出"，
    # 留着旧基线，下一轮轮转再试
    for row in stale:
        if not row.quality and previous.get(row.id):
            row.quality = previous[row.id]
    # 重算可能让单元在新维度上第一次变得"可证明低于目标"，当场排期而不是
    # 等排期补挂巡检轮转到它
    armed = await arm_upgrade_candidates(session, stale)
    await session.commit()
    logger.info(
        "洗版基线重算：%d 个单元的质量快照升到 v%d，%d 个进入洗版排期",
        len(stale),
        SNAPSHOT_VERSION,
        armed,
    )
    return len(stale)


@register_task(
    "backfill_upgrade_snapshots",
    title="洗版基线回填",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=_BACKFILL_TICK_SECONDS,
    description=(
        "为已配置洗版目标的规则组所引用订阅，补齐历史已入库单元的质量快照"
        "（洗版比较的基线）。纯数据库变换、分批慢跑，补完即空转。"
    ),
)
async def backfill_upgrade_snapshots() -> None:
    """存量回填 tick：每次最多处理一批 quality IS NULL 的 imported 单元。"""
    db = get_database()
    async with db.session() as session:
        rule_sets = list((await session.execute(select(RuleSet))).scalars().all())
        upgrade_ids = rule_set_ids_with_upgrade(rule_sets)
        if not upgrade_ids:
            return
        rows = list(
            (
                await session.execute(
                    select(WantedItem)
                    .join(
                        Subscription, Subscription.id == WantedItem.subscription_id
                    )
                    .where(
                        WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                        WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                        WantedItem.quality.is_(None),  # type: ignore[union-attr]
                        Subscription.rule_set_id.in_(upgrade_ids),  # type: ignore[union-attr]
                    )
                    .limit(_BACKFILL_BATCH)
                )
            )
            .scalars()
            .all()
        )
        # 注意这里**不能**在 rows 为空时早退：下面两个巡检（版本重算、排期
        # 补挂）恰恰是为"稳态"设计的——库全部回填完之后 NULL 行本就为零，
        # 早退会让它们在成熟部署上永远不执行
        if rows:
            by_media: dict[int, list[WantedItem]] = {}
            for row in rows:
                by_media.setdefault(row.media_item_id, []).append(row)
            for media_item_id, wanted_rows in by_media.items():
                await fill_snapshots(session, media_item_id, wanted_rows)
            armed = await arm_upgrade_candidates(session, rows)
            await session.commit()
            logger.info(
                "洗版基线回填：本轮补齐 %d 个单元的质量快照，%d 个进入洗版排期",
                len(rows),
                armed,
            )

        await _refill_stale_snapshots(session, upgrade_ids)

        # 已有快照但尚未排期的单元（如规则组事后才配洗版目标）：本 tick 顺带
        # 补排期。查询条件表达不了"可洗"（到顶/{} 哨兵会被 arm 判否留在
        # NULL），所以用 id 游标轮转全表——既不让同一批判否行每 tick 重扫，
        # 也不让 LIMIT 把排在后面的真正可洗单元饿死
        global _pending_arm_cursor
        pending = list(
            (
                await session.execute(
                    select(WantedItem)
                    .join(Subscription, Subscription.id == WantedItem.subscription_id)
                    .where(
                        WantedItem.id > _pending_arm_cursor,  # type: ignore[operator]
                        WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                        WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                        WantedItem.quality.isnot(None),  # type: ignore[union-attr]
                        WantedItem.next_search_at.is_(None),  # type: ignore[union-attr]
                        Subscription.rule_set_id.in_(upgrade_ids),  # type: ignore[union-attr]
                    )
                    .order_by(WantedItem.id)  # type: ignore[arg-type]
                    .limit(_BACKFILL_BATCH * 4)
                )
            ).scalars()
        )
        # 游标是进程内状态：重启丢失只意味着从头再扫一轮，无害
        _pending_arm_cursor = (pending[-1].id or 0) if pending else 0
        if pending:
            armed_late = await arm_upgrade_candidates(session, pending)
            if armed_late:
                await session.commit()
                logger.info("洗版排期补挂：%d 个已有快照的单元进入洗版排期", armed_late)

