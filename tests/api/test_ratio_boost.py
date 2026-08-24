"""自动刷分享率引擎的决策逻辑与统计测试：候选评估、免费窗口、效率 EMA、
汰换、小时桶过程指标。

引擎的 IO 编排（下载器对账/提交）依赖真实下载器，属集成范畴；这里锁死的是
全部安全约束与决策规则——H&R 绝不碰、保留期绝不删、预算腾不出就放弃。
设计见 docs/design/site-protection-ratio-boost.md。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.services.ratio_boost import (
    _EMA_WINDOW_SECONDS,
    admission_headroom,
    apply_observation,
    assess_candidate,
    budget_evictable,
    cohort_density,
    density,
    downloader_congested,
    evictable,
    eviction_order_key,
    free_window_sufficient,
    hand_over_if_claimed,
    is_idle,
    pick_evictions,
    stop_loss_reason,
    turnover_seconds,
)
from movieclaw_db.models import BoostTaskState, RatioBoostTask, SiteTorrent, TorrentSource

_NOW = datetime(2026, 8, 17, 12, 0, 0)
_GIB = 1024**3
_BUDGET = 100 * _GIB


def _row(**kw) -> SiteTorrent:
    """一条默认「完全合格」的候选行；用例按需覆盖单个字段制造不合格。"""
    defaults = dict(
        site_id="demo",
        torrent_id="t1",
        title="Free.Torrent.2160p",
        size_bytes=10 * _GIB,
        publish_time=_NOW - timedelta(hours=1),
        seeders=3,
        leechers=30,
        download_volume_factor=0.0,
        is_free=True,
        free_deadline=None,
        hit_and_run=None,
        download_url="https://demo/download/1",
        source=TorrentSource.LIST,
        volatile_refreshed_at=_NOW - timedelta(minutes=5),
    )
    defaults.update(kw)
    return SiteTorrent(**defaults)


def _task(**kw) -> RatioBoostTask:
    defaults = dict(
        site_id="demo",
        torrent_id="t1",
        info_hash="a" * 40,
        downloader_id=1,
        size_bytes=10 * _GIB,
        state=BoostTaskState.ACTIVE,
        completed=True,
        upload_rate_ema=0.0,
        created_at=_NOW - timedelta(hours=100),  # 默认已过 72 小时保留期
        updated_at=_NOW,
    )
    defaults.update(kw)
    return RatioBoostTask(**defaults)


def _assess(
    row: SiteTorrent,
    *,
    tracked: set[str] | None = None,
    hr_hold: timedelta | None = None,
):
    return assess_candidate(
        row,
        now=_NOW,
        budget_bytes=_BUDGET,
        tracked_torrent_ids=tracked or set(),
        hr_hold=hr_hold,
    )


# ---------------------------------------------------------------------------
# 候选评估
# ---------------------------------------------------------------------------


class TestAssessCandidate:
    def test_fully_qualified(self) -> None:
        ok, score = _assess(_row())
        assert ok
        assert score > 0

    def test_rejects_tracked(self) -> None:
        """抢过的（含已汰换的）不再抢，避免反复拉扯。"""
        ok, _ = _assess(_row(), tracked={"t1"})
        assert not ok

    def test_rejects_explicit_hit_and_run(self) -> None:
        """站点考核时长未知时，明确标注 H&R 的种子绝不碰；
        hit_and_run=None（站点不提供标记）允许。"""
        assert not _assess(_row(hit_and_run=True))[0]
        assert _assess(_row(hit_and_run=False))[0]
        assert _assess(_row(hit_and_run=None))[0]

    def test_hr_allowed_when_site_hold_known(self) -> None:
        """站点配置了真实考核时长（hr_seed_hours）后敢准入明确 H&R 的种子
        ——保留期会按真实时长保底（见 evictable），做满考核再谈汰换。"""
        assert _assess(_row(hit_and_run=True), hr_hold=timedelta(hours=72))[0]

    def test_rejects_non_free(self) -> None:
        assert not _assess(_row(is_free=False))[0]
        assert not _assess(_row(is_free=None))[0]

    def test_rejects_no_leechers(self) -> None:
        """没有下载者就没有上传对象；leechers=NULL（未观测）同样不抢。"""
        assert not _assess(_row(leechers=0))[0]
        assert not _assess(_row(leechers=None))[0]

    def test_rejects_stale_publish(self) -> None:
        assert not _assess(_row(publish_time=_NOW - timedelta(hours=25)))[0]
        assert not _assess(_row(publish_time=None))[0]

    def test_rejects_stale_promo_observation(self) -> None:
        """促销观测超过 2 小时（或从未观测）不可信——促销可能已结束，
        抢一个"其实已不免费"的种子会产生真实下载量。"""
        assert not _assess(_row(volatile_refreshed_at=_NOW - timedelta(hours=3)))[0]
        assert not _assess(_row(volatile_refreshed_at=None))[0]
        assert _assess(_row(volatile_refreshed_at=_NOW - timedelta(hours=1)))[0]

    def test_rejects_oversized(self) -> None:
        """单种 > 预算 1/4 会让汰换失去弹性。"""
        assert not _assess(_row(size_bytes=_BUDGET // 4 + 1))[0]
        assert _assess(_row(size_bytes=_BUDGET // 4))[0]

    def test_rejects_missing_essentials(self) -> None:
        assert not _assess(_row(download_url=None))[0]
        assert not _assess(_row(size_bytes=None))[0]

    def test_score_prefers_demand_over_supply(self) -> None:
        """leechers/(seeders+1)：供不应求的评分更高。"""
        _, hot = _assess(_row(seeders=1, leechers=50))
        _, cold = _assess(_row(seeders=50, leechers=5))
        assert hot > cold

    def test_score_doubles_on_2x_upload(self) -> None:
        _, base = _assess(_row(upload_volume_factor=1.0))
        _, doubled = _assess(_row(upload_volume_factor=2.0))
        assert doubled == base * 2

    def test_rejects_low_score(self) -> None:
        """最低分门槛：供需比太差（评分 < 3）的候选注定与做种大军抢食，
        宁可空转不收——首日数据里这类准入的上传/下载比只有 0.3~0.6。"""
        assert not _assess(_row(seeders=30, leechers=30))[0]  # 30/31 ≈ 0.97
        assert _assess(_row(seeders=9, leechers=30))[0]  # 恰好 3.0，放行


# ---------------------------------------------------------------------------
# 免费窗口
# ---------------------------------------------------------------------------


class TestFreeWindow:
    def test_null_deadline_is_sufficient(self) -> None:
        """NULL = 无促销截止/长期免费（索引层已归一 M-Team 哨兵）。"""
        assert free_window_sufficient(10 * _GIB, None, _NOW)

    def test_small_torrent_needs_min_margin(self) -> None:
        """小种子也要 2 小时安全垫。"""
        assert free_window_sufficient(1 * _GIB, _NOW + timedelta(hours=3), _NOW)
        assert not free_window_sufficient(1 * _GIB, _NOW + timedelta(hours=1), _NOW)

    def test_big_torrent_needs_download_time(self) -> None:
        """大种子按 5 MiB/s 保守估算：200 GiB 需约 11.4 小时，3 小时窗口不够。"""
        size = 200 * _GIB
        assert not free_window_sufficient(size, _NOW + timedelta(hours=3), _NOW)
        assert free_window_sufficient(size, _NOW + timedelta(hours=12), _NOW)

    def test_estimate_follows_actual_per_task_speed(self) -> None:
        """并发摊薄后每任务只剩 1 MiB/s 时，同一个窗口就不再够用。

        用固定速度估算会系统性乐观：放进来的种子在免费窗口内下不完，
        要么被止损放弃（白烧带宽），要么下成付费流量（反而伤分享率）。
        """
        size = 20 * _GIB
        window = _NOW + timedelta(hours=2, minutes=30)
        assert free_window_sufficient(size, window, _NOW, expected_speed=5 * 1024**2)
        assert not free_window_sufficient(size, window, _NOW, expected_speed=1024**2)


# ---------------------------------------------------------------------------
# 效率追踪（EMA）
# ---------------------------------------------------------------------------


class TestApplyObservation:
    def test_first_observation_establishes_rate(self) -> None:
        """首次观测以 created_at 为基线：上传量从 0 起算，能得出真实速率。

        EMA 是时距感知的（等效 24h 窗口）：α = 1 - e^(-dt/W)。
        """
        task = _task(created_at=_NOW - timedelta(seconds=1000), uploaded_bytes=0)
        task.last_checked_at = None
        apply_observation(task, uploaded_bytes=1_000_000, completed=True, now=_NOW)
        alpha = 1 - math.exp(-1000 / _EMA_WINDOW_SECONDS)
        assert task.uploaded_bytes == 1_000_000
        assert math.isclose(task.upload_rate_ema, alpha * 1000.0)
        assert task.last_checked_at == _NOW

    def test_daily_burst_survives_quiet_hours(self) -> None:
        """24h 窗口的意义：昨晚狂传、白天安静的种子不能几小时就被判死。

        旧的 0.3/5min EMA 一小时安静就衰减到 1.4%；24h 窗口下安静 6 小时
        仍保留约 78% 的效率读数，汰换判断以「天」为尺度。
        """
        task = _task(uploaded_bytes=1000, upload_rate_ema=100_000.0)  # 高效历史
        task.last_checked_at = _NOW - timedelta(hours=6)
        apply_observation(task, uploaded_bytes=1000, completed=True, now=_NOW)  # 6h 零上传
        assert task.upload_rate_ema > 100_000.0 * 0.75

    def test_none_uploaded_keeps_ema(self) -> None:
        """旧适配器不提供上传量时绝不能当 0 差分（会误汰换）。"""
        task = _task(uploaded_bytes=500, upload_rate_ema=123.0)
        task.last_checked_at = _NOW - timedelta(seconds=300)
        apply_observation(task, uploaded_bytes=None, completed=True, now=_NOW)
        assert task.uploaded_bytes == 500
        assert task.upload_rate_ema == 123.0
        assert task.last_checked_at == _NOW  # 时钟仍推进

    def test_negative_delta_resets_baseline(self) -> None:
        """下载器重建任务（累计上传回退）时重置基线、不更新 EMA。"""
        task = _task(uploaded_bytes=1_000_000, upload_rate_ema=50.0)
        task.last_checked_at = _NOW - timedelta(seconds=300)
        apply_observation(task, uploaded_bytes=100, completed=True, now=_NOW)
        assert task.uploaded_bytes == 100
        assert task.upload_rate_ema == 50.0

    def test_completed_is_sticky(self) -> None:
        """完成位只置不清：下载器重新校验期间闪烁不应把任务打回未完成。"""
        task = _task(completed=True)
        task.last_checked_at = _NOW - timedelta(seconds=300)
        apply_observation(task, uploaded_bytes=None, completed=False, now=_NOW)
        assert task.completed is True

    def test_completion_flip_stamps_completed_at(self) -> None:
        """首次观测到完成时记下 completed_at（汰换判定窗口的起点）；
        后续观测不得覆盖它。"""
        task = _task(completed=False, completed_at=None)
        task.last_checked_at = _NOW - timedelta(seconds=300)
        apply_observation(task, uploaded_bytes=None, completed=True, now=_NOW)
        assert task.completed_at == _NOW
        later = _NOW + timedelta(seconds=300)
        apply_observation(task, uploaded_bytes=None, completed=True, now=later)
        assert task.completed_at == _NOW

    def test_freeze_ema_keeps_ledger_but_not_rate(self) -> None:
        """站点刷流暂停中（做种被人为限速）：累计量/完成位/蜂群照常记账，
        但上传速度 EMA 冻结——限速下的低速不是真实效率，混进 EMA 会在
        恢复后触发一轮误汰换。基线仍推进，恢复后差分从暂停结束处起算。"""
        task = _task(upload_rate_ema=500_000.0, uploaded_bytes=1_000_000)
        task.last_checked_at = _NOW - timedelta(seconds=300)
        apply_observation(
            task,
            uploaded_bytes=1_000_100,  # 限速下 300 秒只传出 100 字节
            completed=True,
            now=_NOW,
            swarm_leechers=7,
            freeze_ema=True,
        )
        assert task.upload_rate_ema == 500_000.0  # EMA 原封不动
        assert task.uploaded_bytes == 1_000_100  # 累计量照常入账
        assert task.swarm_leechers == 7
        assert task.last_checked_at == _NOW  # 基线推进，恢复后差分不含暂停段

    def test_downloaded_bytes_recorded_and_none_safe(self) -> None:
        """下载量入台账（带宽成本，任务删除后 qb 里就没了）；
        None（旧适配器不提供）绝不能当 0 把已有账目清掉。"""
        task = _task(downloaded_bytes=500)
        task.last_checked_at = _NOW - timedelta(seconds=300)
        apply_observation(
            task, uploaded_bytes=None, completed=True, now=_NOW, downloaded_bytes=800
        )
        assert task.downloaded_bytes == 800
        apply_observation(
            task,
            uploaded_bytes=None,
            completed=True,
            now=_NOW + timedelta(seconds=300),
            downloaded_bytes=None,
        )
        assert task.downloaded_bytes == 800


# ---------------------------------------------------------------------------
# 汰换
# ---------------------------------------------------------------------------


class TestEviction:
    def test_turnover_is_the_unit(self) -> None:
        """周转 = 传一遍自己的体积要多久；没人要（EMA=0）= 无穷大。"""
        task = _task(size_bytes=10 * _GIB, upload_rate_ema=(10 * _GIB) / 86400)
        assert math.isclose(turnover_seconds(task), 86400)
        assert math.isinf(turnover_seconds(_task(upload_rate_ema=0.0)))

    def test_hold_period_is_inviolable(self) -> None:
        """入池不满保留期（默认 3 天）的任务在任何条件下不可汰换（H&R 安全垫）。"""
        young = _task(created_at=_NOW - timedelta(hours=71), upload_rate_ema=0.0)
        assert not evictable(young, _NOW)
        old = _task(created_at=_NOW - timedelta(hours=73), upload_rate_ema=0.0)
        assert evictable(old, _NOW)

    def test_hold_is_per_site_configurable(self) -> None:
        """保留期每站可配：7 天的站 5 天龄任务仍受保护；0 天 = 不设 H&R 保护。"""
        aged_5d = _task(created_at=_NOW - timedelta(days=5), upload_rate_ema=0.0)
        assert not evictable(aged_5d, _NOW, hold=timedelta(days=7))
        assert evictable(aged_5d, _NOW, hold=timedelta(days=0))

    def test_judgment_window_counts_from_completion(self) -> None:
        """判定窗口从**完成时刻**起算：下了很久才下完的种子，完成后仍有
        完整的公平测量窗，不因入池早就被立即处决；反过来刚完成的也不可
        因"看起来慢"被误杀（蜂群还有下载者、上传已起步的情况下）。"""
        just_done = _task(
            created_at=_NOW - timedelta(days=5),
            completed_at=_NOW - timedelta(hours=2),
            uploaded_bytes=200 * 1024**2,  # 上传已起步，不触发零产出速汰
            upload_rate_ema=0.0,
            swarm_leechers=5,
        )
        assert not evictable(just_done, _NOW, hold=timedelta(0))
        judged = _task(
            created_at=_NOW - timedelta(days=5),
            completed_at=_NOW - timedelta(hours=13),
            uploaded_bytes=200 * 1024**2,
            upload_rate_ema=0.0,
            swarm_leechers=5,
        )
        assert evictable(judged, _NOW, hold=timedelta(0))

    def test_zero_yield_fast_eviction(self) -> None:
        """零产出速汰：完成 6 小时上传仍不足 64 MiB 的种子不必陪跑完整
        判定窗——"光下载、上传起不来"的及时淘汰；上传已起步的同龄种子
        则等公平判定窗走完再说。"""
        stalled = _task(
            completed_at=_NOW - timedelta(hours=7),
            uploaded_bytes=10 * 1024**2,
            upload_rate_ema=0.0,
            swarm_leechers=5,
        )
        assert evictable(stalled, _NOW, hold=timedelta(0))
        warming = _task(
            completed_at=_NOW - timedelta(hours=7),
            uploaded_bytes=200 * 1024**2,
            upload_rate_ema=0.0,
            swarm_leechers=5,
        )
        assert not evictable(warming, _NOW, hold=timedelta(0))

    def test_hr_hold_overrides_short_site_hold(self) -> None:
        """真实考核时长优先：用户把保留期设 0/3 只是未知时的保底，明确
        标注 H&R 的任务按 max(站点保留期, hr_seed_hours) 保留；非 H&R
        任务不受 hr_seed_hours 约束。"""
        hr_task = _task(
            hit_and_run=True,
            created_at=_NOW - timedelta(days=4),
            upload_rate_ema=0.0,
        )
        # 站点保底 3 天已过，但真实考核 5 天未满 → 仍受保护
        assert not evictable(hr_task, _NOW, hr_hold=timedelta(days=5))
        # 考核 5 天做满后放行
        aged = _task(
            hit_and_run=True,
            created_at=_NOW - timedelta(days=6),
            upload_rate_ema=0.0,
        )
        assert evictable(aged, _NOW, hr_hold=timedelta(days=5))
        # 非 H&R 任务只看站点保留期
        plain = _task(created_at=_NOW - timedelta(days=4), upload_rate_ema=0.0)
        assert evictable(plain, _NOW, hr_hold=timedelta(days=5))

    def test_dead_swarm_skips_maturity_wait(self) -> None:
        """蜂群已死（tracker 汇报 0 下载者）= 未来注定零产出，不必等测量成熟
        即可汰换（仍须过保留期）；蜂群未知（None）不算死，宁可多留。"""
        dead_fresh = _task(
            created_at=_NOW - timedelta(hours=2),
            upload_rate_ema=0.0,
            swarm_leechers=0,
        )
        assert evictable(dead_fresh, _NOW, hold=timedelta(0))
        unknown_fresh = _task(
            created_at=_NOW - timedelta(hours=2),
            upload_rate_ema=0.0,
            swarm_leechers=None,
        )
        assert not evictable(unknown_fresh, _NOW, hold=timedelta(0))

    def test_uncompleted_is_not_judgeable(self) -> None:
        """下载中的任务没有可信的密度读数，不进汰换候选（由止损另行处理）。"""
        assert not evictable(_task(completed=False), _NOW)

    def test_evictable_asks_judgeability_not_quality(self) -> None:
        """evictable 只回答"能不能评价它"，不回答"它够不够好"。

        高效种子同样进候选池——够不够好是相对于能换进来的东西而言的，
        由 pick_evictions 的边际比较逐个候选回答。这里绝不能有绝对地板：
        曾有过的"周转 > 10 天"地板换算成密度是 1.21 KiB/s per GiB，而同期
        门外候选实测密度 30~210，于是整池 68 个种全部"合格"、一个都换不动。
        """
        efficient = _task(upload_rate_ema=(10 * _GIB) / (5 * 86400))  # 周转 5 天
        assert evictable(efficient, _NOW)

    def test_density_is_size_relative(self) -> None:
        """效率的单位是 rate/size，不是 rate。

        15 KiB/s 对 5 GiB 是约 4 天周转的好资产；对 100 GiB 是 81 天周转的
        坏资产——绝对速度会把这两个判反。
        """
        rate = 15 * 1024.0
        small = _task(size_bytes=5 * _GIB, upload_rate_ema=rate)
        big = _task(torrent_id="big", size_bytes=100 * _GIB, upload_rate_ema=rate)
        assert density(small) > density(big)
        assert eviction_order_key(big) < eviction_order_key(small)  # 大而稀的先走

    def test_pick_lowest_density_first_within_tier(self) -> None:
        """同层内按单位存储产出排序：大而稀的先走，哪怕它的绝对速度更高。"""
        dense = _task(torrent_id="dense", size_bytes=10 * _GIB, upload_rate_ema=2048.0)
        sparse = _task(torrent_id="sparse", size_bytes=40 * _GIB, upload_rate_ema=4096.0)
        # dense 密度 1.9e-7、sparse 9.5e-8（sparse 绝对速度反而是 dense 的两倍）
        picked = pick_evictions(
            [dense, sparse], need_bytes=30 * _GIB, now=_NOW, expected_density=2.5e-7
        )
        assert picked is not None
        assert [t.torrent_id for t in picked] == ["sparse"]

    def test_dead_swarm_evicted_before_slower_alive(self) -> None:
        """死种最先走：蜂群 0 下载者的种子未来注定零产出，优先于周转更慢
        但蜂群里还有人的种子——后者可能只是暂时安静。"""
        alive_slower = _task(
            torrent_id="alive", size_bytes=10 * _GIB, upload_rate_ema=100.0, swarm_leechers=3
        )
        dead_faster = _task(
            torrent_id="dead", size_bytes=10 * _GIB, upload_rate_ema=500.0, swarm_leechers=0
        )
        picked = pick_evictions([alive_slower, dead_faster], need_bytes=10 * _GIB, now=_NOW)
        assert picked is not None
        assert [t.torrent_id for t in picked] == ["dead"]

    def test_insufficient_space_returns_none(self) -> None:
        """可汰换的加起来腾不出所需空间 → 返回 None，放弃准入而非删更多。"""
        protected_by_hold = _task(created_at=_NOW - timedelta(hours=1), size_bytes=50 * _GIB)
        small = _task(torrent_id="s", upload_rate_ema=0.0, size_bytes=5 * _GIB)
        assert pick_evictions([protected_by_hold, small], need_bytes=20 * _GIB, now=_NOW) is None


class TestIdleTier:
    """空闲层：24h EMA 低于地板 = 当下零产出，排在在产资产之前，且换掉
    它不需要过保证金。"""

    def test_idle_is_measured_on_ema_never_instantaneous(self) -> None:
        """"有没有速度"只能问 24 小时 EMA。实测某时刻全池 125 个种里 121 个
        瞬时上行为 0，其中包括累计上传 100 GiB 的池内头号资产——PT 上传
        以天为周期突发，用瞬时判会清空全池。EMA 高就不是空闲，哪怕此刻静默。
        """
        bursty = _task(upload_rate_ema=400 * 1024.0, uploaded_bytes=100 * _GIB)
        assert not is_idle(bursty)
        assert is_idle(_task(upload_rate_ema=512.0))

    def test_idle_evicted_before_producing_regardless_of_density(self) -> None:
        """空闲层优先于在产层，即使空闲的那个密度更高（小体积微speed 种）。

        这是"不能把存量有速度的淘汰掉"的直接编码：先用零成本的空间接新种。
        """
        # 空闲但密度高：0.02 GiB / 512 B/s → 密度 2.4e-8… 仍排在在产之前
        idle_small = _task(torrent_id="idle", size_bytes=_GIB // 8, upload_rate_ema=512.0)
        producing = _task(torrent_id="hot", size_bytes=_GIB // 8, upload_rate_ema=8192.0)
        assert density(idle_small) < density(producing)
        picked = pick_evictions(
            [producing, idle_small], need_bytes=_GIB // 8, now=_NOW, expected_density=1.0
        )
        assert picked is not None
        assert [t.torrent_id for t in picked] == ["idle"]

    def test_idle_needs_no_margin(self) -> None:
        """空闲层不过保证金：没有标定系数（冷启动）时照样能换，这正是
        自举路径——换进来的新种 24 小时内补充标定样本。"""
        idle = _task(torrent_id="idle", upload_rate_ema=0.0)
        picked = pick_evictions([idle], need_bytes=_GIB, now=_NOW, expected_density=None)
        assert picked is not None
        assert [t.torrent_id for t in picked] == ["idle"]


class TestSwapMargin:
    """在产资产的绝对保护：候选期望密度要显著超过实测密度才换手。"""

    def test_producing_asset_survives_a_mediocre_candidate(self) -> None:
        """候选只是略强（未过保证金）→ 不动在产资产，宁可放弃这个候选。

        在池产出是实测的、候选是期望的，置信度不对称；赌错是双输
        （丢实测产出 + 白烧一遍下载带宽）。
        """
        producing = _task(size_bytes=10 * _GIB, upload_rate_ema=8192.0)
        d = density(producing)
        assert pick_evictions(
            [producing], need_bytes=10 * _GIB, now=_NOW, expected_density=d * 1.5
        ) is None
        assert pick_evictions(
            [producing], need_bytes=10 * _GIB, now=_NOW, expected_density=d * 2.5
        ) is not None

    def test_no_calibration_never_touches_producing(self) -> None:
        """标定样本不足时绝不动在产资产——没有换算依据就没有比较的资格。"""
        producing = _task(size_bytes=10 * _GIB, upload_rate_ema=8192.0)
        assert pick_evictions(
            [producing], need_bytes=10 * _GIB, now=_NOW, expected_density=None
        ) is None

    def test_stops_at_first_survivor(self) -> None:
        """候选按密度升序，一旦有一个扛住保证金，后面只会更强 → 收手。

        腾不够就返回 None 放弃这个候选，而不是继续往上啃高产出资产。
        """
        weak = _task(torrent_id="weak", size_bytes=_GIB, upload_rate_ema=1500.0)
        strong = _task(torrent_id="strong", size_bytes=_GIB, upload_rate_ema=100 * 1024.0)
        # 期望密度只够吃掉 weak，need 需要两个才够 → 放弃
        expected = density(weak) * 2.5
        assert pick_evictions(
            [weak, strong], need_bytes=2 * _GIB, now=_NOW, expected_density=expected
        ) is None


class TestCohortDensity:
    """候选期望密度 = 新鲜队列的实测密度中位数，直接读观测，不做外推。"""

    def _sample(self, idx: int, ema: float, done_ago: timedelta) -> RatioBoostTask:
        return _task(
            torrent_id=f"c{idx}",
            info_hash=f"{idx:040d}",
            size_bytes=10 * _GIB,
            entry_score=100.0,
            upload_rate_ema=ema,
            completed_at=_NOW - done_ago,
        )

    def test_median_of_recent_completions(self) -> None:
        tasks = [
            self._sample(i, ema, timedelta(hours=2))
            for i, ema in enumerate([1024.0, 2048.0, 3072.0, 4096.0, 5120.0])
        ]
        got = cohort_density(tasks, _NOW)
        assert got is not None
        assert math.isclose(got, 3072.0 / (10 * _GIB))  # 中位样本的密度

    def test_ignores_entry_score(self) -> None:
        """评分不参与量级预测：实测评分弹性只有 0.18（t=1.49，与 0 无法
        区分），弹性=1 被强烈拒绝（t=-6.8）。按评分线性外推会把高分候选
        高估两倍以上，而高分候选正是唯一会去动在产资产的那种。"""
        low = [self._sample(i, 2048.0, timedelta(hours=2)) for i in range(5)]
        high = [self._sample(i, 2048.0, timedelta(hours=2)) for i in range(5)]
        for t in high:
            t.entry_score = 900.0
        assert cohort_density(low, _NOW) == cohort_density(high, _NOW)

    def test_insufficient_samples_returns_none(self) -> None:
        """样本不足 → None，调用方退回只动空闲层的保守路径。"""
        tasks = [self._sample(i, 2048.0, timedelta(hours=2)) for i in range(4)]
        assert cohort_density(tasks, _NOW) is None

    def test_stale_completions_excluded(self) -> None:
        """只用完成后 24 小时内的样本：密度强烈依赖种龄（实测入池 0-12h
        的中位是 36-60h 的 12 倍），拿老种标定会把候选低估一个数量级。"""
        tasks = [self._sample(i, 2048.0, timedelta(hours=30)) for i in range(6)]
        assert cohort_density(tasks, _NOW) is None


class TestBudgetConvergence:
    """预算收敛的判据（budget_evictable）：用户调小预算必须收敛到位，
    高效不是免死金牌；只有保留期是铁律。"""

    def test_immature_task_is_evictable_under_budget_pressure(self) -> None:
        """刚下完、密度还测不准的种子：日常汰换要等测量成熟，预算收敛不等——
        否则 1000G 调 100G 时一池新种占着 900G 迟迟不归还。

        这是 evictable 与 budget_evictable 仅剩的区别（两者的保留期铁律相同）。
        """
        fresh = _task(completed_at=_NOW - timedelta(hours=1), uploaded_bytes=_GIB)
        assert not evictable(fresh, _NOW)
        assert budget_evictable(fresh, _NOW)

    def test_hold_remains_inviolable(self) -> None:
        """保留期铁律在预算压力下依然成立（H&R 安全垫不因用户调预算失效）。"""
        young = _task(created_at=_NOW - timedelta(hours=10))
        assert not budget_evictable(young, _NOW)
        assert budget_evictable(young, _NOW, hold=timedelta(hours=1))

    def test_hr_hold_applies_under_budget_pressure(self) -> None:
        """明确 H&R 的任务按真实考核时长保底，预算收敛也不能提前动。"""
        hr_task = _task(hit_and_run=True, created_at=_NOW - timedelta(days=4))
        assert not budget_evictable(hr_task, _NOW, hr_hold=timedelta(days=5))
        assert budget_evictable(hr_task, _NOW, hr_hold=timedelta(days=3))

    def test_uncompleted_not_touched(self) -> None:
        """下载中的任务不参与预算收敛（由止损逻辑按免费窗口/卡死处理）。"""
        assert not budget_evictable(_task(completed=False), _NOW)

    def test_completion_windows_do_not_delay_convergence(self) -> None:
        """完成后判定窗（6h/12h）是效率判定的质量门槛，不适用于预算收敛：
        刚下完的种子只要过了保留期就可为收敛让位。"""
        just_done = _task(completed_at=_NOW - timedelta(hours=1), swarm_leechers=5)
        assert budget_evictable(just_done, _NOW)


class TestAdmissionHeadroom:
    """准入余量 = 剩余预算 + 已过保留期（可判定）任务的占用。

    它只服务索引同步的节奏，**不是准入扫描的开关**——"能换手"不等于
    "该换手"，后者由准入时的边际比较逐个候选回答。
    """

    def test_empty_pool_full_headroom(self) -> None:
        assert admission_headroom([], _BUDGET, _NOW) == _BUDGET

    def test_judgeable_capacity_counts_as_headroom(self) -> None:
        """池满但有种子已过保留期：整池有换手能力，同步继续钉最快节奏。

        在产资产也算进余量——它有没有资格被评价，和它值不值得被换掉，
        是两个问题。
        """
        hot = _task(size_bytes=60 * _GIB, upload_rate_ema=100 * 1024)
        held = _task(
            torrent_id="h", size_bytes=40 * _GIB, created_at=_NOW - timedelta(hours=10)
        )
        assert admission_headroom([hot, held], _BUDGET, _NOW) == 60 * _GIB

    def test_full_pool_within_hold_has_no_headroom(self) -> None:
        """池子满且全在 72 小时保留期内：一个都动不了，余量为 0——此时索引
        同步才回落到正常自适应（不再为发现新种白打站点）。"""
        young = _task(size_bytes=100 * _GIB, created_at=_NOW - timedelta(hours=10))
        assert admission_headroom([young], _BUDGET, _NOW) == 0

    def test_terminal_states_do_not_occupy(self) -> None:
        gone = _task(state=BoostTaskState.MISSING, size_bytes=30 * _GIB)
        assert admission_headroom([gone], _BUDGET, _NOW) == _BUDGET


# ---------------------------------------------------------------------------
# 未完成任务的止损（不受 72 小时保留期约束——保留期保护的是已完成的做种）
# ---------------------------------------------------------------------------


class TestStopLoss:
    def test_free_expired_with_much_remaining_abandons(self) -> None:
        """免费窗口已过、还剩 4 成没下：每多下一字节都是付费流量，止损。"""
        task = _task(
            completed=False,
            created_at=_NOW - timedelta(hours=5),
            free_deadline=_NOW - timedelta(hours=1),
        )
        reason = stop_loss_reason(task, progress=0.6, now=_NOW)
        assert reason is not None and "免费窗口" in reason

    def test_free_expired_but_nearly_done_finishes(self) -> None:
        """已下到 95%：删了全白费，剩余付费量很小，放行下完。"""
        task = _task(
            completed=False,
            created_at=_NOW - timedelta(hours=5),
            free_deadline=_NOW - timedelta(hours=1),
        )
        assert stop_loss_reason(task, progress=0.95, now=_NOW) is None

    def test_stuck_download_abandons(self) -> None:
        task = _task(completed=False, created_at=_NOW - timedelta(hours=49))
        reason = stop_loss_reason(task, progress=0.3, now=_NOW)
        assert reason is not None and "48 小时" in reason

    def test_healthy_incomplete_and_completed_are_kept(self) -> None:
        healthy = _task(completed=False, created_at=_NOW - timedelta(hours=2))
        assert stop_loss_reason(healthy, progress=0.3, now=_NOW) is None
        # 已完成的任务永远轮不到止损（归汰换与保留期管）
        done = _task(completed=True, free_deadline=_NOW - timedelta(hours=1))
        assert stop_loss_reason(done, progress=1.0, now=_NOW) is None

    def test_queued_48h_gets_honest_reason(self) -> None:
        """排队 48 小时同样让出预算，但原因如实说是活动位满，不误报死种。"""
        task = _task(completed=False, created_at=_NOW - timedelta(hours=49))
        reason = stop_loss_reason(task, progress=0.0, now=_NOW, queued=True)
        assert reason is not None and "排队" in reason and "死种" not in reason

    def _jit_task(self, **kw):
        """JIT 预测的默认场景：入池 2 小时、窗口还剩 3 小时、10 GiB 下了一半。"""
        defaults = dict(
            completed=False,
            size_bytes=10 * _GIB,
            created_at=_NOW - timedelta(hours=2),
            free_deadline=_NOW + timedelta(hours=3),
        )
        defaults.update(kw)
        return _task(**defaults)

    def test_predicted_miss_abandons_early(self) -> None:
        """按实测速率预测赶不上免费窗口：不等窗口过完，提前止损省带宽。
        剩 5 GiB、实测 100 KiB/s → 需 14+ 小时，可用只有 1 小时（含安全垫）。"""
        reason = stop_loss_reason(self._jit_task(), progress=0.5, now=_NOW, dl_rate=100 * 1024)
        assert reason is not None and "预计赶不上" in reason

    def test_predicted_ok_keeps_downloading(self) -> None:
        """实测 10 MiB/s → 剩 5 GiB 只要 8.5 分钟，窗口充裕，不动。"""
        rate = 10 * 1024 * 1024
        assert stop_loss_reason(self._jit_task(), progress=0.5, now=_NOW, dl_rate=rate) is None

    def test_prediction_needs_evidence_age(self) -> None:
        """入池不足 30 分钟不预测：可能刚排完队开始下，均速失真，误杀会
        永久跳过一个好种。"""
        task = self._jit_task(created_at=_NOW - timedelta(minutes=10))
        assert stop_loss_reason(task, progress=0.05, now=_NOW, dl_rate=100 * 1024) is None

    def test_prediction_skips_queued_and_unknown_rate(self) -> None:
        """排队中速率失真不预测；旧适配器不提供下载量（rate=None）也不预测。"""
        task = self._jit_task()
        assert (
            stop_loss_reason(task, progress=0.5, now=_NOW, queued=True, dl_rate=100 * 1024)
            is None
        )
        assert stop_loss_reason(task, progress=0.5, now=_NOW, dl_rate=None) is None

    def test_prediction_spares_nearly_done(self) -> None:
        """已下到 9 成以上：与过期止损同一豁免——删了全白费。"""
        assert (
            stop_loss_reason(self._jit_task(), progress=0.95, now=_NOW, dl_rate=1024) is None
        )


class TestDownloaderCongested:
    def test_any_queued_download_means_congested(self) -> None:
        """存在排队中的下载任务 = 活动位满 = 暂停准入；否则放行。

        预算越大越需要这个闸：没有它，引擎会按自己的节奏把下载器队列塞爆，
        排队任务吃掉免费窗口、48 小时后被止损删掉，全程空转。
        """
        assert downloader_congested(["downloading", "queued", "completed"])
        assert not downloader_congested(["downloading", "completed", "stalled"])
        assert not downloader_congested([])


# ---------------------------------------------------------------------------
# 与订阅/手动下载的碰撞：认领转出
# ---------------------------------------------------------------------------


class TestHandOverIfClaimed:
    def test_claimed_task_leaves_pool_without_deletion(self) -> None:
        """被订阅认领的任务转出管理：让出预算、绝不删数据，之后归订阅状态机管。"""
        task = _task()
        assert hand_over_if_claimed(task, {task.info_hash}, _NOW)
        assert task.state == BoostTaskState.MISSING
        assert task.evicted_at == _NOW
        assert "认领" in (task.evict_reason or "")
        # 转出的任务不再是汰换候选——数据安全的关键断言
        assert not evictable(task, _NOW)

    def test_unclaimed_task_stays(self) -> None:
        task = _task()
        assert not hand_over_if_claimed(task, {"f" * 40}, _NOW)
        assert task.state == BoostTaskState.ACTIVE

    def test_terminal_states_untouched(self) -> None:
        """已终态（evicted/missing）的任务不重复转出，保留原始结论。"""
        task = _task(state=BoostTaskState.EVICTED, evict_reason="原始原因")
        assert not hand_over_if_claimed(task, {task.info_hash}, _NOW)
        assert task.evict_reason == "原始原因"


# ---------------------------------------------------------------------------
# 小时桶过程指标：近 24h/7d 用 X GB 种子贡献 Y GB 上传
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from movieclaw_api.core.config import get_settings
    from movieclaw_db.engine import dispose_db, get_database, init_db
    from movieclaw_db.migrations import run_migrations

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'boost.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


class _FakeDownloader:
    """set_location 的记录桩；raises 非空时抛错模拟迁移失败。"""

    def __init__(self, raises: Exception | None = None):
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    async def set_location(self, info_hash: str, save_path: str) -> None:
        if self.raises is not None:
            raise self.raises
        self.calls.append((info_hash, save_path))


@pytest.mark.asyncio
async def test_reclaim_moves_data_and_hands_over(db) -> None:
    """「刷流先抢、订阅后到」的接管：迁目录 + 台账转出 + 标记 reclaimed。"""
    from movieclaw_api.services.torrent_submit import _reclaim_boost_task
    from movieclaw_downloader.models import SubmitResult

    # downloader_id 置空：测试库没有下载器配置行，接管逻辑也不依赖它
    task = _task(downloader_id=None)
    async with db.session() as session:
        session.add(task)
        await session.commit()

        fake = _FakeDownloader()
        result = await _reclaim_boost_task(
            session,
            fake,
            SubmitResult(info_hash=task.info_hash, already_exists=True),
            save_path="/downloads/movies/Show",
        )
        await session.refresh(task)

    assert result.reclaimed_from_boost
    assert fake.calls == [(task.info_hash, "/downloads/movies/Show")]
    assert task.state == BoostTaskState.MISSING
    assert "接管" in (task.evict_reason or "")


@pytest.mark.asyncio
async def test_reclaim_leaves_user_torrents_alone(db) -> None:
    """已存在的任务不是刷流的（用户自己加的）：不迁目录、不标 reclaimed。"""
    from movieclaw_api.services.torrent_submit import _reclaim_boost_task
    from movieclaw_downloader.models import SubmitResult

    async with db.session() as session:
        fake = _FakeDownloader()
        result = await _reclaim_boost_task(
            session,
            fake,
            SubmitResult(info_hash="f" * 40, already_exists=True),
            save_path="/downloads/movies",
        )

    assert not result.reclaimed_from_boost
    assert fake.calls == []


@pytest.mark.asyncio
async def test_reclaim_move_failure_keeps_boost_ownership(db) -> None:
    """迁移失败不接管：台账保持 active（由认领转出兜底），提交结果原样返回。"""
    from movieclaw_api.services.torrent_submit import _reclaim_boost_task
    from movieclaw_downloader.models import SubmitResult

    task = _task(downloader_id=None)
    async with db.session() as session:
        session.add(task)
        await session.commit()

        fake = _FakeDownloader(raises=RuntimeError("目标目录不可写"))
        result = await _reclaim_boost_task(
            session,
            fake,
            SubmitResult(info_hash=task.info_hash, already_exists=True),
            save_path="/nope",
        )
        await session.refresh(task)

    assert not result.reclaimed_from_boost
    assert task.state == BoostTaskState.ACTIVE


@pytest.mark.asyncio
async def test_hourly_buckets_accumulate_and_windows_aggregate(db) -> None:
    """同一小时内多个 tick 累进同一桶；窗口聚合给出上传量与平均在池体积。"""
    from movieclaw_api.services.ratio_boost import (
        _record_stats,
        _SiteTickObs,
        collect_boost_stats,
    )
    from movieclaw_db.models import AuthType, RatioBoostStat, SiteCredential, utcnow

    now = utcnow()
    async with db.session() as session:
        session.add(
            SiteCredential(site_id="demo", auth_type=AuthType.COOKIE, boost_enabled=True)
        )
        await session.commit()

        # 两个 tick：上传 3 GiB + 1 GiB，在池占用恒为 40 GiB
        for gained in (3 * _GIB, 1 * _GIB):
            await _record_stats(
                session,
                deltas={
                    "demo": _SiteTickObs(
                        uploaded_delta=gained,
                        downloaded_delta=2 * _GIB,
                        upspeed_sum=1_000_000,
                        dlspeed_sum=5_000_000,
                        downloading_count=3,
                    )
                },
                used_by_site={"demo": 40 * _GIB},
                now=now,
            )
        stats = await collect_boost_stats(session)
        bucket = (
            await session.execute(select(RatioBoostStat))
        ).scalars().one()

    view = stats["demo"]
    assert view.uploaded_bytes_24h == 4 * _GIB
    assert view.avg_used_bytes_24h == 40 * _GIB  # 平均在池 = (40+40)/2
    # 7 天窗口包含 24 小时窗口的数据
    assert view.uploaded_bytes_7d == 4 * _GIB
    assert view.avg_used_bytes_7d == 40 * _GIB
    # 新增的带宽收支与采样列同样按 tick 累进（平均值 = sum / tick_count）
    assert bucket.downloaded_bytes == 4 * _GIB
    assert bucket.upspeed_bytes_sum == 2_000_000
    assert bucket.dlspeed_bytes_sum == 10_000_000
    assert bucket.downloading_count_sum == 6
    assert bucket.tick_count == 2


@pytest.mark.asyncio
async def test_task_samples_upsert_within_hour(db) -> None:
    """每任务小时采样：同一小时后写覆盖先写（行值 = 该小时最后一次观测的
    累计量），跨小时另起新桶——相邻桶差分即得产出曲线。"""
    from movieclaw_api.services.ratio_boost import _record_task_samples
    from movieclaw_db.models import RatioBoostTaskSample, utcnow

    now = utcnow().replace(minute=10)
    # downloader_id 置空：测试库没有下载器配置行，采样逻辑也不依赖它
    task = _task(downloader_id=None)
    async with db.session() as session:
        session.add(task)
        await session.commit()

        # 每次采样后提交，模拟真实的 tick 边界（引擎在对账末尾统一 commit）
        task.uploaded_bytes = 100
        task.downloaded_bytes = 1000
        task.swarm_leechers = 50
        await _record_task_samples(session, [task], now)
        await session.commit()
        # 同一小时 20 分钟后：覆盖同一桶
        task.uploaded_bytes = 300
        await _record_task_samples(session, [task], now + timedelta(minutes=20))
        await session.commit()
        # 下一个小时：另起新桶
        task.uploaded_bytes = 700
        await _record_task_samples(session, [task], now + timedelta(hours=1))
        await session.commit()

        samples = (
            (
                await session.execute(
                    select(RatioBoostTaskSample).order_by(
                        RatioBoostTaskSample.bucket_start  # type: ignore[arg-type]
                    )
                )
            )
            .scalars()
            .all()
        )

    assert [s.uploaded_bytes for s in samples] == [300, 700]
    assert samples[0].downloaded_bytes == 1000
    assert samples[0].swarm_leechers == 50


@pytest.mark.asyncio
async def test_swarm_samples_capture_fresh_torrents(db) -> None:
    """候选控制组采样：发布 48h 内且观测到 S/L 的种子入小时桶（无论刷流
    收不收），同一小时覆盖不重复；旧种/无 S/L 观测的不采。"""
    from movieclaw_db.models import SiteTorrentSwarmSample, utcnow
    from movieclaw_db.repositories.torrent_repo import (
        TorrentObservation,
        TorrentRepository,
    )

    now = utcnow()

    def obs(torrent_id: str, *, publish_age: timedelta, seeders: int | None) -> TorrentObservation:
        return TorrentObservation(
            site_id="demo",
            torrent_id=torrent_id,
            title=f"种子{torrent_id}",
            source=TorrentSource.LIST,
            publish_time=now - publish_age,
            seeders=seeders,
            leechers=30,
            download_volume_factor=0.0,
        )

    async with db.session() as session:
        repo = TorrentRepository(session)
        await repo.bulk_upsert(
            [
                obs("fresh", publish_age=timedelta(hours=2), seeders=5),
                obs("stale", publish_age=timedelta(days=3), seeders=5),
            ]
        )
        # 同一小时内复看：seeders 演化，覆盖同一桶而非新增行
        await repo.bulk_upsert([obs("fresh", publish_age=timedelta(hours=2), seeders=9)])

        samples = (
            (await session.execute(select(SiteTorrentSwarmSample))).scalars().all()
        )

    assert len(samples) == 1
    sample = samples[0]
    assert sample.torrent_id == "fresh"
    assert sample.seeders == 9
    assert sample.leechers == 30
    assert sample.is_free is True


@pytest.mark.asyncio
async def test_stat_windows_exclude_old_buckets(db) -> None:
    """8 天前的桶不进 7 天窗口；昨天的桶进 7 天窗口但不进 24 小时窗口。"""
    from movieclaw_api.services.ratio_boost import collect_boost_stats
    from movieclaw_db.models import AuthType, RatioBoostStat, SiteCredential, utcnow

    now = utcnow()
    async with db.session() as session:
        session.add(
            SiteCredential(site_id="demo", auth_type=AuthType.COOKIE, boost_enabled=True)
        )
        for age, uploaded in ((timedelta(days=8), 7 * _GIB), (timedelta(days=2), 2 * _GIB)):
            session.add(
                RatioBoostStat(
                    site_id="demo",
                    bucket_start=(now - age).replace(minute=0, second=0, microsecond=0),
                    uploaded_bytes=uploaded,
                    used_bytes_sum=10 * _GIB,
                    tick_count=1,
                )
            )
        await session.commit()
        stats = await collect_boost_stats(session)

    view = stats["demo"]
    assert view.uploaded_bytes_24h == 0
    assert view.uploaded_bytes_7d == 2 * _GIB
