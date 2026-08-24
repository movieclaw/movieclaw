"""自动刷分享率引擎：盯本地索引的免费种第一时间抢下做种，预算内自动汰换。

完整设计见 docs/design/site-protection-ratio-boost.md。每 tick 三步：

1. **对账**：从每台涉及的下载器 ``list_torrents`` 读一次全量（无逐任务请求），
   按 infohash 对回 ``ratio_boost_task`` 台账——累计上传量差分出上传速度 EMA，
   台账里有、下载器里没有的标记 missing（用户手动删除是明确意图，只让出
   预算，不追究不抢回）。
2. **预算收敛**：某站在池占用超过预算（用户调小了预算）时，按汰换规则删
   低效任务，直到回到预算内或没有可汰换的为止。
3. **准入**：扫描各开启刷流站点的免费新种，评分排序后在预算内提交；预算
   不够时先尝试汰换腾位，腾不出就放弃候选。

安全约束（比效率更重要，任何改动不得破坏）：

- **只碰自己的任务**：提交时发现种子已在下载器中（``already_exists``）的
  绝不入台账管理，与订阅管线 ``owned_by_movieclaw`` 同一哲学；
- **绝不制造 H&R**：站点未配置 ``hr_seed_hours``（真实考核时长未知）时，
  候选排除明确标注 H&R 的种子；配置了才敢准入，且该任务的保留期取
  max(站点保留期, 真实考核时长)——用户设的保留天数只是"标记缺失/未知"
  时的保底，解析到真实考核要求时以真实为准。保留期内的任务在任何预算
  压力下都不会被删；
- **免费窗口内下得完才抢**：免费期过后继续下载会产生真实下载量，反而
  伤害分享率，宁可放弃。
"""

from __future__ import annotations

import contextlib
import logging
import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.boost_bandwidth import concurrent_download_cap, per_task_limit
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    BoostTaskState,
    DownloaderClient,
    ManualDownloadIntent,
    RatioBoostStat,
    RatioBoostTask,
    RatioBoostTaskSample,
    SiteCredential,
    SiteTorrent,
    SubscriptionDownloadAttempt,
    utcnow,
)
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader.base import BaseDownloader
from movieclaw_downloader.factory import create_downloader
from movieclaw_downloader.models import DownloaderConfig, TorrentBrief
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.ratio_boost")

# ---------------------------------------------------------------------------
# 引擎参数（docs/design/site-protection-ratio-boost.md 第 2 节）
# ---------------------------------------------------------------------------

_TICK_SECONDS = 300
# 候选窗口：只抢发布 24 小时内的免费种（新种 peer 群最活跃，上传机会最大）
_FRESH_WINDOW = timedelta(hours=24)
# 促销观测新鲜度：免费/做种数来自索引快照，观测太旧时促销可能已结束，
# 抢一个"其实已不免费"的种子会产生真实下载量——宁可等下一轮同步再看
_VOLATILE_FRESHNESS = timedelta(hours=2)
# 止损：免费窗口已过仍未下完（剩余超过 1/10）→ 继续下就是付费下载，放弃；
# 下载 48 小时仍未完成 → 死种，放弃让出预算（二者都是未完成任务，
# 不受 72 小时保留期约束——保留期保护的是已完成的做种）
_ABANDON_REMAINING_FRACTION = 0.1
_STUCK_AFTER = timedelta(hours=48)
# 免费窗口安全垫：按预期下载速度估算下载时长，窗口不足即放弃。
# 预期速度优先用带宽哨兵包络摊出的每任务限速（见 _admit_candidates）——
# 并发上升会摊薄每个任务的速度，用固定值估算会系统性乐观、把注定赶不上
# 免费窗口的种子放进来白烧一遍带宽。链路无拥塞证据（包络为 None）时回退
# 到这个保守常量
_ASSUMED_DL_SPEED = 5 * 1024 * 1024  # 5 MiB/s
_MIN_FREE_MARGIN = timedelta(hours=2)
# 汰换的最低保留期（H&R 安全垫）默认值：每站可配（boost_hold_days），
# 默认 3 天覆盖多数站点考核时长；无 H&R 的站可调 0 = 自由汰换。
# 注意这只是"考核时长未知"时的保底：站点配置了 hr_seed_hours 且种子明确
# 标注 H&R 时，该任务的保留期取 max(保底, 真实考核时长)，真实值优先
_DEFAULT_MIN_HOLD = timedelta(days=3)
# 效率的衡量单位是「密度」= 单位存储的上传速度（rate/size，其倒数即「周转」）。
# 预算约束的是存储×时间，最大化总上传是个背包问题，按密度贪心保留/汰换即
# 近似最优——绝对速度会留错资产：200 GiB 跑 10 KiB/s（周转 240 天）远差于
# 2 GiB 跑 8 KiB/s（周转 3 天）。
#
# 这里**刻意没有绝对地板**。曾有过「周转 > 10 天才可汰换」的写法，实测证明
# 它是错的：地板换算成密度是 1.21 KiB/s per GiB，而同期门外候选的实测密度
# 是 30~210——地板定在市场出清价的 1/25 到 1/170，于是整池 68 个种全部
# "合格"、一个都换不动，池子冻死在低产出状态。绝对阈值回答的是"这块资产
# 烂不烂"，而真正的问题永远是"它比我能换进来的东西烂不烂"。判据因此改为
# 边际比较（见 pick_evictions），绝对量只保留 H&R 保留期这一条硬约束。
#
# 空闲层判据：24 小时 EMA 低于此值 = 当下零产出，换掉它不损失任何在产产能，
# 因此**不需要过换手保证金**。必须用 EMA 而不是瞬时速度——实测某时刻
# 125 个种里 121 个瞬时上行为 0，其中包括累计上传 100 GiB 的池内头号资产
#（PT 上传以天为周期突发，见 _EMA_WINDOW_SECONDS）。用瞬时判会清空全池
_IDLE_EMA_FLOOR = 1024.0  # 1 KiB/s
# 换手保证金：动一个**还在产出**的种子，候选的期望密度必须超过它实测密度
# 这么多倍。在池的产出是实测的、候选的产出是期望的，两者置信度不对称——
# 拿在产资产赌未验证的候选，赌错是双输（丢实测产出 + 白烧一遍下载带宽）。
# 免费种下载不计分享率，所以保证金不必很大；但下载会挤占上行（实测：下行
# 冲到 69 MB/s 时上传从 5.2 塌到 1.5 MiB/s），所以也不能是 1
_SWAP_MARGIN = 2.0
# 评分→密度的换算系数：候选只有无量纲评分 L/(S+1)，在池的是实测密度
#（字节/秒每字节），不标定就没法比大小。系数从池内任务自己反解，不写死——
# 不同站点的蜂群动力学、不同线路的上行能力都会改变它，与带宽哨兵的基线
# 同理，必须从本地观测里长出来。
#
# 只用「完成后 24 小时内」的样本标定：候选即将变成的就是这个阶段的样子。
# 实测该系数强烈依赖种龄（入池 0-12h 中位 0.324，36-60h 只有 0.027，差 12 倍），
# 用全池中位会把候选低估一个数量级。取中位而非均值：密度分布长尾。
_CALIBRATION_WINDOW = timedelta(hours=24)
# 样本不足时返回 None，调用方退回"只动空闲层"的保守路径。这条路径也是
# 冷启动的自举来源：空闲层汰换不需要标定，换进来的新种在 24 小时内又
# 补充了标定样本，系数因此永远不会饿死
_CALIBRATION_MIN_SAMPLES = 5
# 上传速度 EMA 的时间窗口：PT 上传以「天」为周期突发（晚高峰猛、白天静），
# 短窗口会把昨晚狂传的种子在今天下午误判成死种。α 按时距计算（1-e^(-dt/W)），
# tick 间隔漂移也不影响窗口语义
_EMA_WINDOW_SECONDS = 24 * 3600
# 汰换判定分层，均从**下载完成时刻**起算——完成前是在下载，谈不上传得慢；
# 完成后才是做种表现的考场（EMA 从入池就在积累，含下载阶段的边下边传）：
# - 零产出速汰：完成 6 小时累计上传仍不足 64 MiB → 蜂群根本不来要数据，
#   不必等公平测量窗走完，及时淘汰"下完却一直起不来"的种子；
# - 公平判定窗：完成满 12 小时（此时已观测入池至少 12 小时以上），
#   密度读数才算可信，进入常规汰换候选。
# 蜂群已死（tracker 汇报 0 下载者）不受这两个窗口约束，随时可换。
# 注意这一组是**可判定性**门槛（"我有没有资格评价它"），不是质量门槛
#（"它够不够好"）——质量一律交给边际比较，绝不设绝对线。
_ZERO_YIELD_AFTER = timedelta(hours=6)
_ZERO_YIELD_BYTES = 64 * 1024**2
_JUDGMENT_AFTER_COMPLETE = timedelta(hours=12)
# 单种体积上限 = 预算的 1/4：单种吃掉大半预算会让汰换失去弹性
_MAX_SIZE_BUDGET_FRACTION = 4
# 最低准入评分：L/(S+1) 太低 = 和一群做种者抢少量需求，注定低效。
# 首日数据：评分 < 3 的准入（入场 seeders 45~218）上传/下载比只有 0.3~0.6，
# 占着带宽和预算拉低平均；评分 50+ 的普遍 ≥ 1。宁可空转不收垃圾
_MIN_ADMIT_SCORE = 3.0
# 每站每 tick 最多提交数：对站点保持克制
_MAX_ADMIT_PER_TICK = 3
# 在下并发不设常量上限：见 boost_bandwidth.concurrent_download_cap()。
# 曾经这里写死 3，理由是首夜数据"平均在下 7.5 个时下行冲到 69 MB/s、上传
# 塌掉 3/4"。但那次事故的因是**下行总速率**，不是任务个数——3 个种各跑
# 20 MiB/s 比 10 个种各跑 1 MiB/s 伤得多，个数只是速率的劣质代理。
# 速率现在由带宽哨兵的 AIMD 包络直接管着，并发上限从包络导出（包络 /
# 每任务速度地板），链路没有拥塞证据时干脆不设限
# JIT 预测止损的证据门槛：入池不足 30 分钟不预测（速率观测还不充分，
# 可能刚排完队开始下载，凭均速误杀会永久跳过一个好种）
_PREDICT_MIN_AGE = timedelta(minutes=30)
# 提交后宽限：刚提交的任务可能还没出现在下载器列表里，不能立刻判 missing
_MISSING_GRACE = timedelta(minutes=10)
# 准入扫描没有余量门槛：曾经有过 1 GiB 的绝对门槛，它是上面那条 10 天地板
# 的派生——余量 = 剩余预算 + 可汰换容量，地板判定"没有可换的"，余量就掉到
# 门槛以下。实测卡在 0.99 GiB，差 1% 停扫。更坏的是同一判据还驱动
# wants_fast_sync，池满会让索引同步退化到慢排期：停止发现 = 输掉新鲜度
# 竞赛，而新鲜度恰恰是刷流收益的主要来源。扫描本身很便宜（一次 SQL +
# 200 行内存打分），现在永远跑，能不能接由边际比较自己回答
# 余量恢复后推同步一把的阈值：游标排期还在 15 分钟开外才值得打断
#（刚回落不久/本来就快的排期没必要动）
_NUDGE_THRESHOLD = timedelta(minutes=15)
# 小时桶统计的保留时长：展示窗口最长 7 天，30 天留足余量后清理
_STAT_RETENTION = timedelta(days=30)
# 刷流任务的下载器分类与保存子目录：与媒体下载（movieclaw）彻底隔离，
# 不落媒体库、不进监听导入规则的视野
BOOST_CATEGORY = "movieclaw-boost"
BOOST_TAG = "movieclaw-boost"
_BOOST_SUBDIR = "movieclaw-boost"
# 站点刷流暂停时的按种上传限速：本质是给用户的前台流量（看视频等）让出
# 上行，所以要压到接近于零——1 KiB/s 保住与蜂群/tracker 的连接不掉种，
# 即使上百个做种聚合起来也只有百余 KiB/s。刻意不用 qB 的"暂停任务"：
# 暂停任务会停止汇报 tracker，部分站点会把它记为停止做种（H&R 风险）
_PAUSED_UPLOAD_LIMIT = 1024  # 1 KiB/s


# ---------------------------------------------------------------------------
# 纯决策函数（无 IO，单测覆盖）
# ---------------------------------------------------------------------------


def free_window_sufficient(
    size_bytes: int,
    free_deadline: datetime | None,
    now: datetime,
    *,
    expected_speed: float = _ASSUMED_DL_SPEED,
) -> bool:
    """免费窗口是否足够把种子下完（含安全垫）。

    deadline 为 NULL 表示"无促销截止/长期免费/未知"——索引层已把 M-Team
    长期免费哨兵归一为 NULL，且候选查询以 is_free=True 为前提，此处 NULL
    按"窗口充足"处理。

    ``expected_speed`` 是这个种子**实际能分到**的下载速度：并发越高每个
    任务分得越少，拿固定值估算会系统性乐观，放进来一批注定在免费窗口内
    下不完的种子——它们要么被止损放弃（白烧带宽），要么下成付费流量
    （反而伤分享率）。调用方从带宽哨兵的包络摊算后传入。
    """
    if free_deadline is None:
        return True
    required = max(_MIN_FREE_MARGIN, timedelta(seconds=size_bytes / expected_speed))
    return free_deadline - now >= required


def assess_candidate(
    row: SiteTorrent,
    *,
    now: datetime,
    budget_bytes: int,
    tracked_torrent_ids: set[str],
    hr_hold: timedelta | None = None,
    expected_speed: float = _ASSUMED_DL_SPEED,
) -> tuple[bool, float]:
    """评估一个索引行是否值得抢，返回（是否合格, 评分）。

    评分 = leechers / (seeders + 1) × 上传系数：分数高 = 供不应求 =
    上传效率预期高；2x 上传的种子同样的流量记双倍，评分翻倍。

    评分刻意**不除以体积**：每个下载者都要下完整个体积，剩余需求字节
    ≈ leechers × size，做种者份额 ≈ L×size/(S+1)——除以占用的 size 后，
    每字节期望回报 ≈ L/(S+1)，体积恰好消掉。小体积的真正优势是敏捷性
    （下得快、免费窗口风险小、汰换颗粒度细），由准入排序的同分决胜体现
    （见 _admit_candidates），不在评分里重复计价。
    ``hr_hold`` 是站点的真实 H&R 考核时长（配置文件 hr_seed_hours）：已知
    考核要求才敢准入明确标注 H&R 的种子（其保留期按真实时长保底，见
    evictable）；None=未知，明确 H&R 的一律拒绝。
    SQL 侧已做粗筛，这里是完整判据（防御性重复 + 可单测）。
    """
    if row.torrent_id in tracked_torrent_ids:
        return False, 0.0  # 抢过的不再抢（含已汰换的：证明过效率低）
    if not row.download_url:
        return False, 0.0
    if row.size_bytes is None or row.size_bytes <= 0:
        return False, 0.0
    if row.size_bytes > budget_bytes // _MAX_SIZE_BUDGET_FRACTION:
        return False, 0.0
    if row.is_free is not True:
        return False, 0.0
    if row.hit_and_run is True and hr_hold is None:
        return False, 0.0  # 站点考核时长未知时，明确标注 H&R 的绝不碰
    if not row.leechers or row.leechers < 1:
        return False, 0.0  # 没有下载者就没有上传对象
    if row.publish_time is None or now - row.publish_time > _FRESH_WINDOW:
        return False, 0.0
    if (
        row.volatile_refreshed_at is None
        or now - row.volatile_refreshed_at > _VOLATILE_FRESHNESS
    ):
        return False, 0.0  # 促销观测太旧，等同步刷新后再评估
    if not free_window_sufficient(
        row.size_bytes, row.free_deadline, now, expected_speed=expected_speed
    ):
        return False, 0.0
    score = row.leechers / ((row.seeders or 0) + 1)
    if row.upload_volume_factor and row.upload_volume_factor > 1.0:
        score *= row.upload_volume_factor
    if score < _MIN_ADMIT_SCORE:
        return False, 0.0  # 供需比太差，注定与做种大军抢食（见常量注释）
    return True, score


def hold_for(cred: SiteCredential) -> timedelta:
    """站点的汰换保留期（H&R 安全垫）：boost_hold_days，0 = 不设保护。"""
    return timedelta(days=max(0, cred.boost_hold_days))


def hr_hold_for(site_id: str) -> timedelta | None:
    """站点的真实 H&R 考核时长（配置文件 hr_seed_hours）；未配置/站点未注册
    返回 None。这是站点级政策事实（规则页明示），与用户设的保留天数
    （boost_hold_days，未知时的保底）分属两个来源，取值时二者取大。"""
    from movieclaw_tracker.exceptions import SiteNotFoundError
    from movieclaw_tracker.registry import get_site_config

    try:
        hours = get_site_config(site_id).hr_seed_hours
    except SiteNotFoundError:
        return None
    return timedelta(hours=hours) if hours and hours > 0 else None


def apply_observation(
    task: RatioBoostTask,
    *,
    uploaded_bytes: int | None,
    completed: bool,
    now: datetime,
    swarm_seeders: int | None = None,
    swarm_leechers: int | None = None,
    downloaded_bytes: int | None = None,
    freeze_ema: bool = False,
) -> None:
    """把下载器的一次观测写回台账：完成位 + 上传量差分 → 上传速度 EMA。

    uploaded_bytes / downloaded_bytes 为 None（旧适配器不提供）时只更新
    完成位，绝不能当 0 参与差分——会把 EMA 错误打到 0 触发误汰换。
    差分为负说明下载器重建过任务（重新校验/换实例），重置基线不更新 EMA。

    ``freeze_ema=True``（站点刷流暂停中）时照常记账（完成位/累计量/蜂群），
    但**不更新上传速度 EMA**：暂停期间做种被人为限速，观测到的低速不是
    真实效率，混进 EMA 会在恢复后触发一轮误汰换。基线（last_checked_at/
    uploaded_bytes）仍然推进，恢复后差分从暂停结束处重新起算。
    """
    if completed and not task.completed:
        # 首次观测到完成：汰换判定窗口（零产出速汰/公平判定）从此刻起算
        task.completed_at = now
    task.completed = task.completed or completed
    if uploaded_bytes is not None:
        baseline_at = task.last_checked_at or task.created_at
        dt = (now - baseline_at).total_seconds()
        delta = uploaded_bytes - task.uploaded_bytes
        if not freeze_ema and dt > 0 and delta >= 0:
            rate = delta / dt
            # 时距感知 EMA：等效 24 小时观测窗口（见 _EMA_WINDOW_SECONDS）
            alpha = 1 - math.exp(-dt / _EMA_WINDOW_SECONDS)
            task.upload_rate_ema = alpha * rate + (1 - alpha) * task.upload_rate_ema
        task.uploaded_bytes = max(uploaded_bytes, 0)
    if downloaded_bytes is not None:
        # 下载量只记账不参与 EMA：它是带宽成本台账（任务删除后 qb 里就没了）
        task.downloaded_bytes = max(downloaded_bytes, 0)
    # 蜂群快照只在下载器有汇报时覆盖（None 保留上次已知值，不可当 0）
    if swarm_seeders is not None:
        task.swarm_seeders = swarm_seeders
    if swarm_leechers is not None:
        task.swarm_leechers = swarm_leechers
    task.last_checked_at = now
    task.updated_at = now


def hand_over_if_claimed(
    task: RatioBoostTask, claimed_hashes: set[str], now: datetime
) -> bool:
    """种子被订阅投递/手动下载认领时，把刷流任务转出管理。返回是否发生转出。

    与订阅的碰撞是双向的，这里处理「刷流先抢、订阅后到」的方向：订阅投递
    命中同一 infohash 时下载器幂等返回 already_exists，工单照常记账——此后
    这份数据是订阅的依赖，刷流**绝不能再把它连数据汰换掉**。转出 = 置
    MISSING 让出预算、任务与数据原样保留，之后归订阅的所有权/H&R 状态机
    管辖（反方向「订阅先抢、刷流后到」由准入排除 + already_exists 双重拦截）。
    """
    if task.state != BoostTaskState.ACTIVE or task.info_hash not in claimed_hashes:
        return False
    task.state = BoostTaskState.MISSING
    task.evicted_at = now
    task.evict_reason = "已被订阅/手动下载认领，移出刷流管理（预算让出，任务与数据保留）"
    task.updated_at = now
    return True


def downloader_congested(states: Iterable[str]) -> bool:
    """下载器是否拥堵：存在排队中（queued）的下载任务即视为拥堵。

    队列上限（最大活动种子数等）决定同时活动的任务数——已经有任务在排队
    说明活动位满了，此时继续准入只是把新种压进队尾：排队吃掉免费窗口、
    排到 48 小时被止损删掉，全是空转。拥堵时刷流暂停投放，把活动位让给
    已有任务（含用户自己的下载），队列消化后自动恢复——预算越大越需要
    这个闸，否则引擎会一口气把队列塞爆。
    """
    return any(state == "queued" for state in states)


def stop_loss_reason(
    task: RatioBoostTask,
    progress: float,
    now: datetime,
    *,
    queued: bool = False,
    dl_rate: float | None = None,
) -> str | None:
    """未完成任务的止损判定：该放弃则返回中文原因，否则 None。

    与汰换（针对已完成的做种）不同，止损针对**下载中**的任务，不受 72 小时
    保留期约束——多数站点的 H&R 考核针对已完成下载，未完成任务放弃的风险
    远小于"免费窗口过后继续付费下载"的确定伤害：

    - 免费窗口已过、剩余还超过 1/10：每多下一字节都是付费流量，删；
      已下到 9 成以上则放行下完（删了全白费，剩余付费量很小）；
    - **JIT 预测止损**：窗口还没过，但按实测下载速率（``dl_rate``，
      字节/秒）预测赶不上（含安全垫）——早停一小时就省一小时的白下带宽，
      比等窗口过了才认账强。证据门槛：入池满 30 分钟、未在排队（排队时
      速率失真）、速率确为观测值（None=旧适配器不提供，跳过）；
    - 48 小时仍未完成：死种/无源，或一直在队列里排不上（拥堵感知准入
      挡住了新增，但存量排队任务长期抢不到活动位时也该让出预算）——
      两种情况动作相同，原因如实区分。
    """
    if task.completed:
        return None
    if (
        task.free_deadline is not None
        and now > task.free_deadline
        and (1.0 - progress) > _ABANDON_REMAINING_FRACTION
    ):
        return f"免费窗口已过仍未下完（进度 {progress:.0%}），止损放弃避免付费下载"
    if (
        task.free_deadline is not None
        and now <= task.free_deadline
        and not queued
        and dl_rate is not None
        and dl_rate > 0
        and (1.0 - progress) > _ABANDON_REMAINING_FRACTION
        and now - task.created_at >= _PREDICT_MIN_AGE
    ):
        remaining_seconds = (1.0 - progress) * task.size_bytes / dl_rate
        available = (task.free_deadline - now - _MIN_FREE_MARGIN).total_seconds()
        if remaining_seconds > available:
            return (
                f"免费窗口预计赶不上（进度 {progress:.0%}，"
                f"实测速度约 {dl_rate / 1024**2:.1f} MiB/s），提前止损避免付费下载"
            )
    if now - task.created_at >= _STUCK_AFTER:
        if queued:
            return "排队 48 小时仍未获得下载机会（下载器活动任务已满），放弃让出预算"
        return "下载 48 小时仍未完成（死种或无可用资源），放弃让出预算"
    return None


def _observed_dl_rate(
    task: RatioBoostTask, downloaded_before: int, baseline_at: datetime, now: datetime
) -> float | None:
    """任务的实测下载速率（字节/秒），供 JIT 预测止损。

    取两个口径的较大者，对任务**从宽**（预测是用来放弃的，宁可少杀）：
    - 入池均速：累计下载量 / 入池时长——平滑但会被前期排队拉低；
    - 本 tick 瞬时速：下载量差分 / tick 间隔——捕捉"刚开始动起来"。
    旧适配器不提供下载量（downloaded_bytes 恒 0 且无差分）时返回 None，
    调用方跳过预测。
    """
    if task.downloaded_bytes <= 0:
        return None
    age = (now - task.created_at).total_seconds()
    avg = task.downloaded_bytes / age if age > 0 else 0.0
    dt = (now - baseline_at).total_seconds()
    instant = (task.downloaded_bytes - downloaded_before) / dt if dt > 0 else 0.0
    return max(avg, instant)


def turnover_seconds(task: RatioBoostTask) -> float:
    """周转周期：按当前上传 EMA，把自己的体积再上传一遍需要多少秒。

    这是刷流效率的统一度量——「这块存储隔多久为分享率贡献一次自己的大小」。
    EMA 为 0（完全没人要）时返回无穷大。
    """
    if task.upload_rate_ema <= 0:
        return math.inf
    return task.size_bytes / task.upload_rate_ema


def density(task: RatioBoostTask) -> float:
    """产出密度 = 单位存储的上传速度（字节/秒每字节），周转的倒数。

    这是预算这个背包问题里的"单价"：预算约束的是存储，密度就是每字节存储
    换来的上传速度。汰换排序、边际比较都用它，绝不用绝对速度——15 KiB/s
    对 5 GiB 是好资产，对 100 GiB 是坏资产。
    """
    if task.size_bytes <= 0:
        return 0.0
    return task.upload_rate_ema / task.size_bytes


def is_idle(task: RatioBoostTask) -> bool:
    """当下是否零产出：24 小时 EMA 低于空闲地板。

    "有没有速度"必须问 EMA，不能问瞬时——PT 上传以天为周期突发，实测某
    时刻全池 121/125 个种瞬时上行为 0，其中包括累计上传 100 GiB 的头号
    资产。空闲层的种子换掉不损失在产产能，因此汰换时排在有产出的前面，
    且不需要过换手保证金。
    """
    return task.upload_rate_ema < _IDLE_EMA_FLOOR


def swarm_dead(task: RatioBoostTask) -> bool:
    """蜂群是否已死：tracker 明确汇报 0 个下载者。

    这是「未来还有没有人要」的直接证据——EMA 只能说明过去。None（下载器
    未提供蜂群数据）不算死，宁可多留不误杀。
    """
    return task.swarm_leechers == 0


def _effective_hold(
    task: RatioBoostTask, hold: timedelta, hr_hold: timedelta | None
) -> timedelta:
    """任务的实际保留期：明确标注 H&R 的任务按真实考核时长保底（取大）。"""
    if task.hit_and_run and hr_hold is not None:
        return max(hold, hr_hold)
    return hold


def evictable(
    task: RatioBoostTask,
    now: datetime,
    *,
    hold: timedelta = _DEFAULT_MIN_HOLD,
    hr_hold: timedelta | None = None,
) -> bool:
    """任务的密度读数是否**可判定**：下载完成 + 过了保留期 + 完成后观测已成熟。

    注意这个函数回答的是"我有没有资格评价它"，**不是"它够不够好"**。够不够
    好一律由边际比较回答（见 ``pick_evictions``）：够不够好是相对于能换进来
    的东西而言的，任何绝对阈值都会在池子整体变好或变差时判错。曾经这里有一
    条"周转 > 10 天"的绝对地板，实测把整池冻死（见 _IDLE_EMA_FLOOR 上方注释）。

    保留期（在此期间任何预算压力都不删）取两个来源之大者：

    - ``hold``：用户配置的站点保留天数（boost_hold_days）——H&R 标记缺失/
      考核时长未知时的保底，0 = 不设保护；
    - ``hr_hold``：站点真实 H&R 考核时长（hr_seed_hours），只约束准入时
      明确标注 H&R 的任务——解析到真实要求时以真实为准，哪怕用户把保底
      调成了 0。

    这是全局唯一的绝对硬约束，理由是它保护的不是效率而是**账号安全**。

    过了保留期后，判定窗口从**完成时刻**起算（完成前是在下载，密度读数
    没有意义），按证据强度分层，越确凿越早放行：

    - **蜂群已死**（tracker 汇报 0 下载者）——未来注定零产出，无需测量；
    - **零产出速汰**（完成 6 小时累计上传仍不足 64 MiB）——下完却根本
      起不来的种子不必陪跑公平测量窗；
    - **公平判定窗**（完成满 12 小时）——EMA 已覆盖足够长的观测（含下载
      阶段），此时的密度可以拿去和候选比较。
    """
    if not (
        task.state == BoostTaskState.ACTIVE
        and task.completed
        and now - task.created_at >= _effective_hold(task, hold, hr_hold)
    ):
        return False
    if swarm_dead(task):
        return True
    # 历史数据（迁移前的已完成任务）无 completed_at，回退用入池时间——
    # 只会把窗口算得更长（更保守），不会误杀
    since_complete = now - (task.completed_at or task.created_at)
    if since_complete >= _ZERO_YIELD_AFTER and task.uploaded_bytes < _ZERO_YIELD_BYTES:
        return True
    return since_complete >= _JUDGMENT_AFTER_COMPLETE


def budget_evictable(
    task: RatioBoostTask,
    now: datetime,
    *,
    hold: timedelta = _DEFAULT_MIN_HOLD,
    hr_hold: timedelta | None = None,
) -> bool:
    """预算收敛路径的可汰换判据：用户显式调小预算是明确指令，必须让占用
    收敛到新预算——因此只保留两条铁律（下载已完成 + 已过保留期），
    **不设** ``evictable`` 的周转地板与完成后判定窗。

    区别的道理：日常"为新种腾位"的汰换是引擎自己的效率权衡，删掉还在
    产出的高效种子换新种是真亏，所以有 10 天周转门槛；而预算收敛是用户
    要磁盘，高效不是免死金牌——但汰换顺序（eviction_order_key）保证
    死种和低效的先走，高效的排最后、只在必要时牺牲。
    """
    return (
        task.state == BoostTaskState.ACTIVE
        and task.completed
        and now - task.created_at >= _effective_hold(task, hold, hr_hold)
    )


def admission_headroom(
    tasks: list[RatioBoostTask],
    budget_bytes: int,
    now: datetime,
    *,
    hold: timedelta = _DEFAULT_MIN_HOLD,
    hr_hold: timedelta | None = None,
) -> int:
    """当前的准入余量 = 剩余预算 + 已过保留期（可判定）任务的占用。

    这是"刷流有没有换手能力"的粗判据，只服务索引同步的节奏（wants_fast_sync）：
    余量为 0 意味着整池都还在 H&R 保留期内，一个都动不了，此时高频刷索引
    纯属浪费站点请求。

    注意它**不再是准入扫描的开关**：可换手不等于该换手，"该不该换"由准入
    时的边际比较逐个候选回答（见 pick_evictions）。把这两件事混成一个绝对
    门槛，正是此前池子冻死的原因。
    """
    used = sum(t.size_bytes for t in tasks if t.state == BoostTaskState.ACTIVE)
    reclaimable = sum(
        t.size_bytes for t in tasks if evictable(t, now, hold=hold, hr_hold=hr_hold)
    )
    return budget_bytes - used + reclaimable


def eviction_order_key(task: RatioBoostTask) -> tuple[int, float]:
    """汰换顺序：三层，层内按密度从低到高（周转从慢到快）。

    - **0 蜂群已死**（tracker 汇报 0 下载者）：未来注定零产出，最先走。
      这是双信源策略的核心收益——同样"周转 30 天"的两个种子，蜂群 0
      下载者的那个没有翻身可能，还有下载者的那个可能只是暂时安静。
    - **1 空闲**（24h EMA 低于地板）：当下零产出，换掉不损失在产产能。
    - **2 在产**：还在出货的资产，永远排最后，且要过换手保证金才动。

    分层负责"谁先走"（相对保护），保证金负责"值不值得动"（绝对保护），
    两者合起来才是完整的策略——只有分层的话，池子全在产时照样会被一个
    平庸候选啃掉底部。
    """
    if swarm_dead(task):
        tier = 0
    elif is_idle(task):
        tier = 1
    else:
        tier = 2
    return (tier, -turnover_seconds(task))


def cohort_density(tasks: list[RatioBoostTask], now: datetime) -> float | None:
    """新鲜队列的实测密度中位数——"一个刚上桌的种子通常能跑多少"。

    这是候选的期望密度：候选即将变成的就是这批种子现在的样子。窗口取
    "完成后 24 小时内"，因为密度强烈依赖种龄（实测入池 0-12h 的中位是
    36-60h 的 12 倍），拿全池中位会把候选低估一个数量级、永远换不动在产资产。

    **刻意不按候选评分做线性外推。** 直觉上"评分翻倍则期望产出翻倍"，
    但实测站不住：以本地 103 个已完成任务回归 log(单位存储单位时间上传)
    ~ log(评分) + log(体积) + log(做种时长)，评分弹性只有 0.18（标准误
    0.12，t=1.49，与 0 无法区分），而弹性 =1 被强烈拒绝（t=-6.8）。按线性
    外推会把高分候选的期望高估两倍以上——偏偏高分候选正是唯一会去动在产
    资产的那种，高估直接变成误杀。

    评分仍然决定候选之间的**排序**（弱预测子做排序仍然有用），只是不再
    用来预测**量级**。量级只信这一个从本地观测直接读出来的数。

    样本不足返回 None：调用方退回"只动空闲层"的保守路径，那条路径不需要
    标定，且换进来的新种 24 小时内又会补充样本——系数不会饿死。
    """
    values = [
        density(t)
        for t in tasks
        if t.upload_rate_ema > 0
        and t.completed_at is not None
        and now - t.completed_at <= _CALIBRATION_WINDOW
    ]
    if len(values) < _CALIBRATION_MIN_SAMPLES:
        return None
    return statistics.median(values)


def pick_evictions(
    tasks: list[RatioBoostTask],
    need_bytes: int,
    now: datetime,
    *,
    hold: timedelta = _DEFAULT_MIN_HOLD,
    hr_hold: timedelta | None = None,
    expected_density: float | None = None,
    margin: float = _SWAP_MARGIN,
) -> list[RatioBoostTask] | None:
    """为一个候选腾出 need_bytes：沿汰换顺序往下走，踩到第一个打得过候选的
    在产任务就收手。

    ``expected_density`` 是候选的期望密度（由 ``score_density_ratio`` 换算）。
    在产任务只有在 ``密度 × margin < 期望密度`` 时才让位——在池的产出是实测
    的、候选的是期望的，置信度不对称，拿在产资产赌未验证的候选赌错是双输。
    死种与空闲层密度≈0，任何候选都赢，不必过保证金。

    ``None`` = 标定样本不足（冷启动），此时**只动死种与空闲层**：宁可少换
    也不在没有换算依据时误伤在产资产。

    因为候选按密度升序排列，一旦有一个扛住了保证金，后面的只会更强——直接
    break 而不是 continue。腾不够返回 None，调用方放弃准入，
    **绝不提前动保留期内的任务**。
    """
    candidates = sorted(
        (t for t in tasks if evictable(t, now, hold=hold, hr_hold=hr_hold)),
        key=eviction_order_key,
    )
    picked: list[RatioBoostTask] = []
    freed = 0
    for task in candidates:
        if freed >= need_bytes:
            break
        if not swarm_dead(task) and not is_idle(task):
            # 在产资产：没有换算依据就不碰；有依据也要候选明显更划算才动
            if expected_density is None:
                break
            if density(task) * margin >= expected_density:
                break
        picked.append(task)
        freed += task.size_bytes
    return picked if freed >= need_bytes else None


# ---------------------------------------------------------------------------
# 引擎主体
# ---------------------------------------------------------------------------


class _DownloaderPool:
    """tick 内的下载器连接池：每台只建一次连接、读一次全量列表，tick 末统一关闭。

    某台不可达时记为 None——该台上的任务本 tick 跳过对账，也不参与汰换
    （删不掉的任务不能被"计划汰换"，否则预算账会虚假平衡）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._adapters: dict[int, BaseDownloader | None] = {}
        self._briefs: dict[int, dict[str, TorrentBrief] | None] = {}

    async def adapter(self, downloader_id: int) -> BaseDownloader | None:
        if downloader_id in self._adapters:
            return self._adapters[downloader_id]
        row = await self._session.get(DownloaderClient, downloader_id)
        adapter: BaseDownloader | None = None
        if row is not None:
            try:
                repo = DownloaderRepository(self._session)
                adapter = create_downloader(
                    DownloaderConfig(
                        type=row.client_type.value,
                        url=row.url,
                        username=row.username,
                        password=repo.decrypted_password(row),
                    )
                )
            except Exception:  # noqa: BLE001 -- 单台下载器故障不拖垮整个 tick
                logger.warning("刷流：连接下载器 #%d 失败", downloader_id, exc_info=True)
                adapter = None
        self._adapters[downloader_id] = adapter
        return adapter

    async def briefs(self, downloader_id: int) -> dict[str, TorrentBrief] | None:
        """该下载器的 infohash → TorrentBrief 映射；不可达返回 None。"""
        if downloader_id in self._briefs:
            return self._briefs[downloader_id]
        adapter = await self.adapter(downloader_id)
        mapping: dict[str, TorrentBrief] | None = None
        if adapter is not None:
            try:
                mapping = {b.info_hash: b for b in await adapter.list_torrents()}
            except Exception as exc:  # noqa: BLE001
                logger.warning("刷流：读取下载器 #%d 任务列表失败：%s", downloader_id, exc)
        self._briefs[downloader_id] = mapping
        return mapping

    async def close(self) -> None:
        for adapter in self._adapters.values():
            if adapter is not None:
                with contextlib.suppress(Exception):
                    await adapter.close()


async def _claimed_hashes(session: AsyncSession) -> set[str]:
    """被订阅投递或手动下载认领的全部 infohash（统一小写）。"""
    attempt_rows = (
        (await session.execute(select(SubscriptionDownloadAttempt.info_hash))).scalars().all()
    )
    intent_rows = (await session.execute(select(ManualDownloadIntent.info_hash))).scalars().all()
    return {h.lower() for h in [*attempt_rows, *intent_rows] if h}


@dataclass
class _SiteTickObs:
    """一 tick 内某站点刷流任务的聚合观测，喂给小时桶统计。

    增量是差分口径（与效率 EMA 同源）；速度与在下任务数是本 tick 的采样值，
    小时桶里累加后除以 tick_count 即时间平均。
    """

    uploaded_delta: int = 0
    downloaded_delta: int = 0
    upspeed_sum: int = 0
    dlspeed_sum: int = 0
    downloading_count: int = 0


async def _refresh_tasks(
    session: AsyncSession,
    pool: _DownloaderPool,
    tasks: list[RatioBoostTask],
    now: datetime,
    paused_sites: set[str] | None = None,
) -> dict[str, _SiteTickObs]:
    """第一步对账：把下载器观测写回台账，找不到的标记 missing。
    返回本 tick 各站点的聚合观测（site_id → _SiteTickObs），供小时桶统计累加。

    对账前先做认领转出（见 hand_over_if_claimed）：被订阅/手动下载认领的
    任务立即脱离刷流管理，从根上杜绝后续任何汰换触碰它的数据。

    ``paused_sites`` 里的站点正处于刷流暂停：任务照常记账但冻结上传 EMA
    （限速下的低速不是真实效率，见 apply_observation），止损判定照常——
    止损防的是免费窗口过期后的付费下载，暂停期间同样成立。
    """
    paused_sites = paused_sites or set()
    deltas: dict[str, _SiteTickObs] = {}
    observed: list[RatioBoostTask] = []
    claimed = await _claimed_hashes(session)
    for task in tasks:
        if hand_over_if_claimed(task, claimed, now):
            logger.info("刷流任务已被认领，转出管理：%s（%s）", task.title, task.site_id)
            continue
        if task.downloader_id is None:
            # 下载器配置已被删除（外键 SET NULL）：任务无从管理，让出预算
            task.state = BoostTaskState.MISSING
            task.evicted_at = now
            task.evict_reason = "下载器配置已删除，任务脱离管理"
            continue
        mapping = await pool.briefs(task.downloader_id)
        if mapping is None:
            continue  # 下载器不可达：本 tick 跳过，不能误判 missing
        brief = mapping.get(task.info_hash)
        if brief is None:
            if now - task.created_at < _MISSING_GRACE:
                continue  # 刚提交的任务可能还没出现在列表里
            task.state = BoostTaskState.MISSING
            task.evicted_at = now
            task.evict_reason = "任务已从下载器中消失（通常是用户手动删除），让出刷流预算"
            logger.info("刷流任务失踪，让出预算：%s（%s）", task.title, task.site_id)
            continue
        uploaded_before = task.uploaded_bytes
        downloaded_before = task.downloaded_bytes
        baseline_at = task.last_checked_at or task.created_at
        apply_observation(
            task,
            uploaded_bytes=brief.uploaded_bytes,
            completed=brief.completed,
            now=now,
            swarm_seeders=brief.swarm_seeders,
            swarm_leechers=brief.swarm_leechers,
            downloaded_bytes=brief.downloaded_bytes,
            freeze_ema=task.site_id in paused_sites,
        )
        observed.append(task)
        # 增量按站点归集（下载器重建导致的负差分已在 apply_observation 归零基线）
        obs = deltas.setdefault(task.site_id, _SiteTickObs())
        obs.uploaded_delta += max(0, task.uploaded_bytes - uploaded_before)
        obs.downloaded_delta += max(0, task.downloaded_bytes - downloaded_before)
        obs.upspeed_sum += brief.upspeed_bytes or 0
        obs.dlspeed_sum += brief.dlspeed_bytes or 0
        if not task.completed:
            obs.downloading_count += 1
        # 未完成任务的止损：免费窗口过期 / 预测赶不上 / 长期卡死或排队
        # → 连数据删除，让出预算。预测用的实测速率取「入池均速」与「本 tick
        # 瞬时速」的较大者：任务可能先排队后开下，均速会低估当前能力
        reason = stop_loss_reason(
            task,
            brief.progress or 0.0,
            now,
            queued=brief.state == "queued",
            dl_rate=_observed_dl_rate(task, downloaded_before, baseline_at, now),
        )
        if reason is not None:
            await _evict(pool, task, now, reason=reason)
    await _record_task_samples(session, observed, now)
    await session.commit()
    return deltas


async def _record_task_samples(
    session: AsyncSession, tasks: list[RatioBoostTask], now: datetime
) -> None:
    """把本 tick 有观测的任务累计值落进小时采样表（同一小时后写覆盖先写）。

    行值始终是该小时最后一次观测的累计量，相邻桶差分即得小时产出曲线——
    台账只有"最新累计值"，回答不了衰减形状/汰换时点这类曲线问题。
    只采有真实观测的任务（下载器不可达/失踪的不采，避免把旧值当新样本）。
    """
    if not tasks:
        return
    bucket = now.replace(minute=0, second=0, microsecond=0)
    task_ids = [t.id for t in tasks if t.id is not None]
    rows = (
        await session.execute(
            select(RatioBoostTaskSample).where(
                RatioBoostTaskSample.bucket_start == bucket,
                RatioBoostTaskSample.task_id.in_(task_ids),  # type: ignore[attr-defined]
            )
        )
    ).scalars()
    samples = {s.task_id: s for s in rows}
    for task in tasks:
        if task.id is None:
            continue
        sample = samples.get(task.id)
        if sample is None:
            sample = RatioBoostTaskSample(task_id=task.id, bucket_start=bucket)
            session.add(sample)
            samples[task.id] = sample
        sample.uploaded_bytes = task.uploaded_bytes
        sample.downloaded_bytes = task.downloaded_bytes
        sample.swarm_seeders = task.swarm_seeders
        sample.swarm_leechers = task.swarm_leechers
        sample.updated_at = now


async def _evict(pool: _DownloaderPool, task: RatioBoostTask, now: datetime, reason: str) -> bool:
    """执行一次汰换：从下载器连数据一起删，台账置 EVICTED。失败返回 False。

    下载器缺失/不可达时返回 False——删不掉的任务不能记成已汰换，否则预算账
    会虚假平衡（对账步骤会另行处理 downloader_id 为 None 的脱管任务）。
    """
    if task.downloader_id is None:
        return False
    adapter = await pool.adapter(task.downloader_id)
    if adapter is None:
        return False
    try:
        await adapter.delete_torrent(task.info_hash, delete_files=True)
    except Exception as exc:  # noqa: BLE001 -- 删除失败不拖垮 tick，下轮再试
        logger.warning("刷流汰换删除失败（%s）：%s", task.title, exc)
        return False
    task.state = BoostTaskState.EVICTED
    task.evicted_at = now
    task.evict_reason = reason
    task.updated_at = now
    turnover = turnover_seconds(task)
    logger.info(
        "已汰换刷流任务：%s（%s，占用 %.1f GiB，上传 EMA %.1f KiB/s，周转约 %s）",
        task.title,
        task.site_id,
        task.size_bytes / 1024**3,
        task.upload_rate_ema / 1024,
        "∞" if math.isinf(turnover) else f"{turnover / 86400:.1f} 天",
    )
    return True


async def _apply_upload_limits(
    pool: _DownloaderPool, tasks: list[RatioBoostTask], limit_bytes: int | None
) -> list[int]:
    """把上传限速批量下发到这批任务（按下载器分组，每台一次请求）。

    ``limit_bytes=None`` = 解除限速。返回下发失败（不可达等）的下载器 id
    列表——暂停路径靠引擎每 tick 重试自愈可忽略，恢复路径必须让用户知道
    哪台没解除成功。
    """
    by_downloader: dict[int, list[str]] = {}
    for task in tasks:
        if task.state == BoostTaskState.ACTIVE and task.downloader_id is not None:
            by_downloader.setdefault(task.downloader_id, []).append(task.info_hash)
    failed: list[int] = []
    for did, hashes in by_downloader.items():
        adapter = await pool.adapter(did)
        if adapter is None:
            failed.append(did)
            continue
        try:
            await adapter.set_upload_limits(sorted(hashes), limit_bytes)
        except Exception as exc:  # noqa: BLE001 -- 单台失败不拖垮其余下载器
            logger.warning("刷流暂停/恢复：下载器 #%d 下发上传限速失败：%s", did, exc)
            failed.append(did)
    return failed


async def apply_boost_pause(session: AsyncSession, site_id: str, paused: bool) -> list[int]:
    """把某站刷流的暂停/恢复立即落到下载器：在池任务批量设置/解除上传限速。

    供站点配置服务在切换 boost_paused 时调用，让暂停"秒级生效"而不是等
    下一个 tick。返回失败的下载器 id 列表（语义见 _apply_upload_limits）。
    """
    tasks = (
        (
            await session.execute(
                select(RatioBoostTask).where(
                    RatioBoostTask.site_id == site_id,
                    RatioBoostTask.state == BoostTaskState.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    pool = _DownloaderPool(session)
    try:
        return await _apply_upload_limits(
            pool, list(tasks), _PAUSED_UPLOAD_LIMIT if paused else None
        )
    finally:
        await pool.close()


async def _default_downloader(session: AsyncSession) -> DownloaderClient | None:
    """取默认且可用的下载器（与 torrent_submit 同判据），供准入提交。"""
    result = await session.execute(
        select(DownloaderClient).where(
            DownloaderClient.is_default.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.enabled.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.status == ConfigStatus.ACTIVE,
        )
    )
    return result.scalars().first()


def _boost_save_path(downloader: DownloaderClient) -> str | None:
    """刷流保存目录：下载器默认目录下的专属子目录；未配置默认目录则交给
    下载器自身默认（此时无法建子目录，靠分类区分）。"""
    if not downloader.save_path:
        return None
    return downloader.save_path.rstrip("/") + "/" + _BOOST_SUBDIR


async def _record_stats(
    session: AsyncSession,
    *,
    deltas: dict[str, _SiteTickObs],
    used_by_site: dict[str, int],
    now: datetime,
) -> None:
    """把本 tick 的观测累进小时桶：上/下行增量、速度与在下任务数采样、
    在池占用采样。

    覆盖「有在池任务 ∪ 有观测增量」的站点——占用采样必须每 tick 都记
    （平均在池体积 = 采样和 / 采样数），不能只在有上传时记，否则安静时段
    会把平均值虚高。顺手清理超过保留期的旧桶（含每任务小时采样表）。
    """
    sites = set(used_by_site) | set(deltas)
    if not sites:
        return
    bucket = now.replace(minute=0, second=0, microsecond=0)
    empty = _SiteTickObs()
    for site_id in sites:
        row = (
            await session.execute(
                select(RatioBoostStat).where(
                    RatioBoostStat.site_id == site_id,
                    RatioBoostStat.bucket_start == bucket,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = RatioBoostStat(site_id=site_id, bucket_start=bucket)
            session.add(row)
        obs = deltas.get(site_id, empty)
        row.uploaded_bytes += obs.uploaded_delta
        row.downloaded_bytes += obs.downloaded_delta
        row.upspeed_bytes_sum += obs.upspeed_sum
        row.dlspeed_bytes_sum += obs.dlspeed_sum
        row.downloading_count_sum += obs.downloading_count
        row.used_bytes_sum += used_by_site.get(site_id, 0)
        row.tick_count += 1
        row.updated_at = now
    await session.execute(
        delete(RatioBoostStat).where(
            RatioBoostStat.bucket_start < now - _STAT_RETENTION  # type: ignore[arg-type]
        )
    )
    await session.execute(
        delete(RatioBoostTaskSample).where(
            RatioBoostTaskSample.bucket_start < now - _STAT_RETENTION  # type: ignore[arg-type]
        )
    )
    await session.commit()


async def _nudge_slow_cursor(session: AsyncSession, site_id: str, now: datetime) -> None:
    """把还在慢排期的同步游标标记到期，让新种发现速度立刻回升。

    整池都在保留期内时同步会回落到自适应节奏（可能已放疏到小时级），
    保留期一过就该重新钉住——这个信号只有刷流引擎自己知道，不推这一把
    就要等旧排期走完。排期在 15 分钟内的不动：马上就要同步了，没必要打断。

    准入每 tick 都会调它：新鲜度是刷流收益的主要来源，候选池断供比多打
    几次站点贵得多。
    """
    from movieclaw_db.repositories.torrent_repo import TorrentRepository

    repo = TorrentRepository(session)
    cursor = await repo.get_cursor(site_id)
    if cursor is not None and cursor.next_sync_at is not None and (
        cursor.next_sync_at - now > _NUDGE_THRESHOLD
    ):
        await repo.expire_cursor(site_id)
        logger.info("刷流：站点 %s 的索引同步已提前（保持新种发现速度）", site_id)


async def _admit_candidates(
    session: AsyncSession,
    pool: _DownloaderPool,
    cred: SiteCredential,
    site_tasks: list[RatioBoostTask],
    now: datetime,
) -> None:
    """第三步准入：扫描该站免费新种，评分排序后在预算内提交。"""
    from movieclaw_api.services.torrent_submit import submit_torrent

    downloader = await _default_downloader(session)
    if downloader is None:
        logger.debug("刷流：没有可用的默认下载器，站点 %s 本轮不准入", cred.site_id)
        return
    # 拥堵感知：目标下载器已有任务在排队（活动位满）时暂停准入——继续投放
    # 只会把新种压进队尾空转（排队吃免费窗口、48h 被止损删）。队列消化后
    # 自动恢复。列表来自本 tick 已缓存的全量读取，零额外请求
    assert downloader.id is not None
    briefs = await pool.briefs(downloader.id)
    if briefs is None:
        return  # 下载器不可达，本轮不准入（提交也注定失败）
    if downloader_congested(brief.state for brief in briefs.values()):
        logger.debug(
            "刷流：下载器「%s」存在排队任务（活动位已满），站点 %s 本轮暂停准入",
            downloader.name,
            cred.site_id,
        )
        return
    # 在下并发上限：由带宽哨兵学到的下载包络导出（包络 / 每任务速度地板），
    # 不是常量。包络为 None = 这条链路从未出现拥塞证据，下行再快也不伤上行，
    # 不设限。跨站点按下载器计——包络约束的是链路，不是站点
    envelope = (
        float(downloader.boost_dl_envelope_bytes)
        if downloader.boost_dl_envelope_bytes
        else None
    )
    downloading_count = (
        await session.execute(
            select(func.count()).select_from(RatioBoostTask).where(
                RatioBoostTask.state == BoostTaskState.ACTIVE,
                RatioBoostTask.completed == False,  # noqa: E712 -- SQL 表达式
                RatioBoostTask.downloader_id == downloader.id,
            )
        )
    ).scalar_one()
    cap = concurrent_download_cap(envelope)
    if cap is not None and downloading_count >= cap:
        logger.debug(
            "刷流：下载器「%s」已有 %d 个任务在下（包络 %.1f MiB/s 导出的上限 %d），"
            "站点 %s 本轮暂停准入",
            downloader.name,
            downloading_count,
            (envelope or 0) / 1024**2,
            cap,
            cred.site_id,
        )
        return
    download_slots = _MAX_ADMIT_PER_TICK if cap is None else cap - downloading_count
    # 这个种子实际能分到的下载速度：并发越高摊得越薄。免费窗口够不够下完
    # 要按它估，用固定值会系统性乐观地放进注定赶不上窗口的种子
    expected_speed = float(
        per_task_limit(envelope, downloading_count + 1) or _ASSUMED_DL_SPEED
    )

    site_id = cred.site_id
    budget = cred.boost_budget_bytes
    used = sum(t.size_bytes for t in site_tasks if t.state == BoostTaskState.ACTIVE)

    # 没有余量门槛：扫描永远跑（一次 SQL + 内存打分，很便宜），能不能接由
    # 每个候选自己的边际比较回答。池满不再是停扫的理由——停扫会连带让索引
    # 同步退化到慢排期，而新鲜度是刷流收益的主要来源，输不起
    hold = hold_for(cred)
    hr_hold = hr_hold_for(site_id)
    await _nudge_slow_cursor(session, site_id, now)
    # 候选的期望密度：本站新鲜队列的实测密度中位数（不按评分外推，理由见
    # cohort_density）。None=样本不足，本轮只动空闲层
    expected_density = cohort_density(site_tasks, now)

    # 抢过的不再抢（任何状态都算：missing/evicted 是明确结论，不能反复拉扯）
    tracked = set(
        (
            await session.execute(
                select(RatioBoostTask.torrent_id).where(RatioBoostTask.site_id == site_id)
            )
        )
        .scalars()
        .all()
    )
    # 订阅投递 / 手动下载已认领的同站种子也不抢：那是媒体下载的领地，刷流
    # 抢过去只会制造 already_exists 空转（「订阅先抢、刷流后到」方向的拦截）
    for model in (SubscriptionDownloadAttempt, ManualDownloadIntent):
        tracked.update(
            (
                await session.execute(
                    select(model.torrent_id).where(
                        model.site_id == site_id,
                        model.torrent_id != None,  # noqa: E711 -- SQL 表达式需用 !=
                    )
                )
            )
            .scalars()
            .all()
        )

    # SQL 粗筛（免费 + 新发布 + 有下载者），完整判据在 assess_candidate。
    # 明确 H&R 的种子只在站点考核时长已知（hr_seed_hours 已配）时才进入候选
    conditions = [
        SiteTorrent.site_id == site_id,
        SiteTorrent.is_free == True,  # noqa: E712 -- SQL 表达式需用 ==
        SiteTorrent.publish_time != None,  # noqa: E711
        SiteTorrent.publish_time >= now - _FRESH_WINDOW,  # type: ignore[operator]
        SiteTorrent.leechers != None,  # noqa: E711
        SiteTorrent.leechers >= 1,  # type: ignore[operator]
        SiteTorrent.size_bytes != None,  # noqa: E711
    ]
    if hr_hold is None:
        conditions.append(
            or_(
                SiteTorrent.hit_and_run == None,  # noqa: E711
                SiteTorrent.hit_and_run == False,  # noqa: E712
            )
        )
    rows = (
        (
            await session.execute(
                select(SiteTorrent)
                .where(*conditions)
                .order_by(SiteTorrent.publish_time.desc())  # type: ignore[union-attr]
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    scored: list[tuple[float, SiteTorrent]] = []
    for row in rows:
        ok, score = assess_candidate(
            row,
            now=now,
            budget_bytes=budget,
            tracked_torrent_ids=tracked,
            hr_hold=hr_hold,
            expected_speed=expected_speed,
        )
        if ok:
            scored.append((score, row))
    # 评分降序；同分小体积优先——每字节期望回报与体积近似无关（见
    # assess_candidate），小的下得快、免费窗口风险小、汰换颗粒度细
    scored.sort(key=lambda pair: (-pair[0], pair[1].size_bytes or 0))

    admitted = 0
    admit_cap = min(_MAX_ADMIT_PER_TICK, download_slots)
    save_path = _boost_save_path(downloader)
    for score, row in scored:
        if admitted >= admit_cap:
            break
        size = row.size_bytes or 0
        space = budget - used
        if size > space:
            # 预算不够：按汰换顺序腾位——死种、空闲层先走，在产资产只有在
            # 这个候选的期望密度显著超过它时才让位。腾不出就看下一个候选
            #（更小或更强的），不强行删
            plan = pick_evictions(
                site_tasks,
                size - space,
                now,
                hold=hold,
                hr_hold=hr_hold,
                expected_density=expected_density,
            )
            if plan is None:
                continue
            for victim in plan:
                if await _evict(
                    pool,
                    victim,
                    now,
                    reason=(
                        "蜂群已死，让位给更高效的新免费种"
                        if swarm_dead(victim)
                        else "24 小时无上行产出，让位给更高效的新免费种"
                        if is_idle(victim)
                        else "新免费种的期望产出显著更高，换手（按单位存储产出比较）"
                    ),
                ):
                    used -= victim.size_bytes
            if size > budget - used:
                continue  # 部分删除失败，位置仍不够
        try:
            result, _ = await submit_torrent(
                session,
                site_id=site_id,
                download_url=row.download_url,
                tags=[BOOST_TAG],
                save_path=save_path,
                downloader_id=downloader.id,
                category=BOOST_CATEGORY,
            )
        except Exception as exc:  # noqa: BLE001
            # 站点/下载器/配置任一环节失败：本站本轮停止准入，下轮重试。
            # 失败原因已是可读中文（AppException 约定），直接进日志。
            logger.warning("刷流准入提交失败（站点 %s）：%s", site_id, exc)
            break
        if not result.info_hash:
            continue  # 极罕见：无法解析 infohash 的种子无法追踪，放弃
        if result.already_exists:
            # 所有权铁律：用户自己已在下的种子绝不纳入管理（永不自动删除）。
            # 台账记一条 missing 终态防止每 tick 重复抢，不占预算不计统计。
            session.add(
                RatioBoostTask(
                    site_id=site_id,
                    torrent_id=row.torrent_id,
                    info_hash=result.info_hash.lower(),
                    downloader_id=downloader.id,
                    title=row.title,
                    size_bytes=size,
                    state=BoostTaskState.MISSING,
                    evicted_at=now,
                    evict_reason="提交时任务已在下载器中（非刷流所有，不纳入管理）",
                )
            )
            await session.commit()
            continue
        task = RatioBoostTask(
            site_id=site_id,
            torrent_id=row.torrent_id,
            info_hash=result.info_hash.lower(),
            downloader_id=downloader.id,
            title=row.title,
            size_bytes=size,
            free_deadline=row.free_deadline,
            # 明确标注 H&R 的任务保留期按真实考核时长保底（见 evictable）
            hit_and_run=row.hit_and_run is True,
            # 入场快照：准入瞬间的决策依据，此后永不覆盖——事后回归
            # "什么入场特征预测高产出"、度量种龄（created_at - 发布时间）
            entry_seeders=row.seeders,
            entry_leechers=row.leechers,
            entry_score=score,
            torrent_published_at=row.publish_time,
        )
        session.add(task)
        await session.commit()
        site_tasks.append(task)
        used += size
        admitted += 1
        logger.info(
            "刷流已抢下免费种：%s（%s，%.1f GiB，评分 %.2f，蜂群 %s/%s，已用 %.1f/%.1f GiB）",
            row.title,
            site_id,
            size / 1024**3,
            score,
            row.seeders if row.seeders is not None else "?",
            row.leechers if row.leechers is not None else "?",
            used / 1024**3,
            budget / 1024**3,
        )
    await session.commit()


@register_task(
    "ratio_boost",
    title="自动刷分享率",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=_TICK_SECONDS,
    description=(
        "盯各开启刷流站点的免费新种，第一时间抢下做种提高分享率；"
        "在站点各自的存储预算内自动汰换上传效率低的任务。"
    ),
)
async def run_ratio_boost() -> None:
    """tick 任务体：对账 → 预算收敛 → 准入。没有刷流站点且无在池任务时零成本返回。"""
    now = utcnow()
    async with get_database().session() as session:
        boost_creds = (
            (
                await session.execute(
                    select(SiteCredential).where(
                        SiteCredential.enabled == True,  # noqa: E712
                        SiteCredential.status == ConfigStatus.ACTIVE,
                        SiteCredential.boost_enabled == True,  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )
        active_tasks = (
            (
                await session.execute(
                    select(RatioBoostTask).where(RatioBoostTask.state == BoostTaskState.ACTIVE)
                )
            )
            .scalars()
            .all()
        )
        if not boost_creds and not active_tasks:
            return

        # 暂停中的站点（独立查询而非从 boost_creds 过滤：站点被停用/验证失败
        # 后不在 boost_creds 里，但暂停期间其任务的 EMA 仍须冻结）
        paused_sites = set(
            (
                await session.execute(
                    select(SiteCredential.site_id).where(
                        SiteCredential.boost_paused == True  # noqa: E712 -- SQL 表达式
                    )
                )
            )
            .scalars()
            .all()
        )

        pool = _DownloaderPool(session)
        try:
            # ① 对账：即使站点已关掉刷流，在池任务仍继续追踪（它们还在做种）
            deltas = await _refresh_tasks(
                session, pool, list(active_tasks), now, paused_sites=paused_sites
            )
            # 小时桶统计：上传增量 + 在池占用采样（转出/汰换后的任务不计占用）
            used_by_site: dict[str, int] = {}
            for t in active_tasks:
                if t.state == BoostTaskState.ACTIVE:
                    used_by_site[t.site_id] = used_by_site.get(t.site_id, 0) + t.size_bytes
            await _record_stats(session, deltas=deltas, used_by_site=used_by_site, now=now)

            for cred in boost_creds:
                site_tasks = [
                    t
                    for t in active_tasks
                    if t.site_id == cred.site_id and t.state == BoostTaskState.ACTIVE
                ]
                if cred.boost_paused:
                    # 暂停中：不汰换、不收敛预算、不拉新种，只把上传限速
                    # 重新下发一遍（覆盖暂停时下发失败/下载器换实例等情况，
                    # 幂等且每台下载器只有一次轻量请求）
                    await _apply_upload_limits(pool, site_tasks, _PAUSED_UPLOAD_LIMIT)
                    continue
                # ② 预算收敛：用户调小预算是明确指令，必须收敛到位——判据用
                # budget_evictable（不设完成后判定窗，高效不是免死金牌），顺序仍是
                # 死种优先、再按周转从慢到快（高效的排最后、只在必要时牺牲）；
                # 保留期内的任务不动，等到期后在后续 tick 里继续收敛
                used = sum(t.size_bytes for t in site_tasks)
                if used > cred.boost_budget_bytes:
                    site_hr_hold = hr_hold_for(cred.site_id)
                    for victim in sorted(
                        (
                            t
                            for t in site_tasks
                            if budget_evictable(
                                t, now, hold=hold_for(cred), hr_hold=site_hr_hold
                            )
                        ),
                        key=eviction_order_key,
                    ):
                        if used <= cred.boost_budget_bytes:
                            break
                        if await _evict(
                            pool, victim, now, reason="预算调小后收敛（按上传效率从低到高让出空间）"
                        ):
                            used -= victim.size_bytes
                    await session.commit()
                # ③ 准入
                await _admit_candidates(session, pool, cred, site_tasks, now)
        finally:
            await pool.close()


async def wants_fast_sync(session: AsyncSession, cred: SiteCredential) -> bool:
    """站点索引同步是否应钉在最快节奏（供 torrent_sync 每轮同步后询问）。

    动态判定，而非"开了刷流就一直快"：只有当整池**一个种都动不了**——即
    全部还在 H&R 保留期内——发现新种才真的没有意义，此时回 False 让同步
    回到冷热自适应/指数退避；任何一个种过了保留期，池子就有换手能力，
    钉回最快节奏。

    门槛是 "> 0" 而不是某个绝对余量：绝对门槛会在池满时停掉发现，而池满
    恰恰是最需要盯紧新种的时候——能不能接由准入的边际比较回答，同步端
    只负责别让候选池断供。
    """
    if not cred.boost_enabled or cred.boost_paused:
        # 暂停期间不拉新种，索引同步没必要钉在最快档
        return False
    tasks = (
        (
            await session.execute(
                select(RatioBoostTask).where(
                    RatioBoostTask.site_id == cred.site_id,
                    RatioBoostTask.state == BoostTaskState.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    return (
        admission_headroom(
            list(tasks),
            cred.boost_budget_bytes,
            utcnow(),
            hold=hold_for(cred),
            hr_hold=hr_hold_for(cred.site_id),
        )
        > 0
    )


async def release_site_tasks(session: AsyncSession, site_id: str) -> int:
    """站点配置被删除时，把该站在池的刷流任务全部转出管理，返回转出数。

    预算的主体（站点）已不存在，但任务与数据保留继续做种——删数据太激进
    （用户可能有意保种），转出后由用户在下载器里按 movieclaw-boost 分类
    自行处理。供 SiteConfigService.delete 调用，与凭据删除同一事务节奏。
    """
    tasks = (
        (
            await session.execute(
                select(RatioBoostTask).where(
                    RatioBoostTask.site_id == site_id,
                    RatioBoostTask.state == BoostTaskState.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for task in tasks:
        task.state = BoostTaskState.MISSING
        task.evicted_at = now
        task.evict_reason = "站点配置已删除，转出刷流管理（任务与数据保留做种）"
        task.updated_at = now
    if tasks:
        await session.commit()
        logger.info("站点 %s 已删除，%d 个刷流任务转出管理（保留做种）", site_id, len(tasks))
    return len(tasks)


async def collect_boost_stats(session: AsyncSession) -> dict:
    """按 site_id 聚合刷流统计，供 GET /sites/boost-stats 展示。

    覆盖两类站点的并集：台账里出现过的 ∪ 当前开着刷流的（后者可能还没有
    任何任务，也要让前端拿到预算数字画进度条）。

    近期窗口指标来自小时桶（ratio_boost_stat）：近 24 小时 / 近 7 天的
    上传量与**平均在池体积**（采样和/采样数），支撑"用 X GB 种子贡献了
    Y GB 上传"的过程展示。
    """
    from movieclaw_api.schemas.site import SiteBoostStatsView

    now = utcnow()

    async def window(since: datetime) -> dict[str, tuple[int, int]]:
        """窗口聚合：site_id → (上传量, 平均在池体积)。"""
        rows = (
            await session.execute(
                select(
                    RatioBoostStat.site_id,
                    func.sum(RatioBoostStat.uploaded_bytes),
                    func.sum(RatioBoostStat.used_bytes_sum),
                    func.sum(RatioBoostStat.tick_count),
                )
                .where(RatioBoostStat.bucket_start >= since)  # type: ignore[arg-type]
                .group_by(RatioBoostStat.site_id)
            )
        ).all()
        return {
            site_id: (int(up or 0), int((used_sum or 0) / ticks) if ticks else 0)
            for site_id, up, used_sum, ticks in rows
        }

    day_window = await window(now - timedelta(hours=24))
    week_window = await window(now - timedelta(days=7))

    rows = (
        await session.execute(
            select(
                RatioBoostTask.site_id,
                func.sum(
                    case((RatioBoostTask.state == BoostTaskState.ACTIVE, 1), else_=0)
                ).label("active_count"),
                func.sum(
                    case(
                        (RatioBoostTask.state == BoostTaskState.ACTIVE, RatioBoostTask.size_bytes),
                        else_=0,
                    )
                ).label("used_bytes"),
                func.sum(RatioBoostTask.uploaded_bytes).label("uploaded_total"),
                func.sum(
                    case((RatioBoostTask.state == BoostTaskState.EVICTED, 1), else_=0)
                ).label("evicted_count"),
            ).group_by(RatioBoostTask.site_id)
        )
    ).all()
    budgets = {
        c.site_id: c
        for c in (await session.execute(select(SiteCredential))).scalars().all()
    }
    ledger = {
        site_id: (
            int(active_count or 0),
            int(used_bytes or 0),
            int(uploaded_total or 0),
            int(evicted_count or 0),
        )
        for site_id, active_count, used_bytes, uploaded_total, evicted_count in rows
    }
    # 三源并集：台账里出现过的 ∪ 近期窗口有数据的 ∪ 当前开着刷流的——
    # 任一来源的站点都要出现在结果里（如任务全部转出但窗口内仍有上传贡献）
    site_ids = (
        set(ledger)
        | set(week_window)
        | {sid for sid, c in budgets.items() if c.boost_enabled}
    )
    stats: dict[str, SiteBoostStatsView] = {}
    for site_id in site_ids:
        cred = budgets.get(site_id)
        active_count, used_bytes, uploaded_total, evicted_count = ledger.get(
            site_id, (0, 0, 0, 0)
        )
        day = day_window.get(site_id, (0, 0))
        week = week_window.get(site_id, (0, 0))
        stats[site_id] = SiteBoostStatsView(
            active_count=active_count,
            used_bytes=used_bytes,
            budget_bytes=cred.boost_budget_bytes if cred else 0,
            uploaded_bytes_total=uploaded_total,
            evicted_count=evicted_count,
            uploaded_bytes_24h=day[0],
            avg_used_bytes_24h=day[1],
            uploaded_bytes_7d=week[0],
            avg_used_bytes_7d=week[1],
        )
    return stats
