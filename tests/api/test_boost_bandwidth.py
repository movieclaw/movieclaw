"""刷流带宽哨兵的 AIMD 决策测试：基线学习、拥塞回退、健康恢复、限速解除。

哨兵循环本体（下载器轮询/限速下发）依赖真实下载器，属集成范畴；这里锁死
纯决策函数 observe / per_task_limit 的控制语义——环境无关（阈值全是相对量）、
误伤保护（基线未成熟不动手、无下载压力不怪罪下载）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from movieclaw_api.services.boost_bandwidth import (
    _BASELINE_MIN_SAMPLES,
    _ENVELOPE_FLOOR,
    _PER_TASK_FLOOR,
    LinkControl,
    concurrent_download_cap,
    observe,
    per_task_limit,
)

_NOW = datetime(2026, 8, 19, 12, 0, 0)
_MIB = 1024**2


def _warmed(up: int = 6 * _MIB, dl: int = 2 * _MIB) -> LinkControl:
    """喂满成熟门槛的样本，得到基线 ≈ up 的控制状态。"""
    control = LinkControl()
    for _ in range(_BASELINE_MIN_SAMPLES):
        observe(control, up, dl, _NOW)
    return control


class TestObserve:
    def test_immature_baseline_never_acts(self) -> None:
        """样本不足时绝不动手：刚启动的几分钟宁可不管，不能误伤。"""
        control = LinkControl()
        for _ in range(_BASELINE_MIN_SAMPLES - 2):
            # 上行归零 + 下行狂飙的"拥塞"景象，也不许动
            assert observe(control, 0, 100 * _MIB, _NOW) is None
        assert control.envelope is None

    def test_congestion_clamps_multiplicatively(self) -> None:
        """上行塌 + 下行有压力 → 包络乘性回退；持续拥塞逐样本再退，
        直到地板。"""
        control = _warmed()
        assert observe(control, 1 * _MIB, 60 * _MIB, _NOW) == "clamp"
        first = control.envelope
        assert first is not None and first < 60 * _MIB
        # 持续拥塞：反复回退，最终停在地板不再往下
        for _ in range(20):
            observe(control, 1 * _MIB, 60 * _MIB, _NOW)
        assert control.envelope == float(_ENVELOPE_FLOOR)

    def test_upload_drop_without_download_pressure_is_not_congestion(self) -> None:
        """夜里没人要数据、上传自然归零，但下行也很低——不能怪到下载头上。"""
        control = _warmed()
        assert observe(control, 0, 1 * _MIB, _NOW) is None
        assert control.envelope is None

    def test_healthy_grows_additively_and_unclamps(self) -> None:
        """健康时加性恢复（限频每分钟一步）；涨回下行峰值之上自动解除。"""
        control = _warmed(dl=12 * _MIB)
        observe(control, 1 * _MIB, 60 * _MIB, _NOW)  # 拥塞入场
        assert control.envelope is not None
        events = set()
        now = _NOW
        for _ in range(200):
            now += timedelta(seconds=61)  # 满足恢复限频
            event = observe(control, 6 * _MIB, 1 * _MIB, now)
            if event:
                events.add(event)
            if control.envelope is None:
                break
        assert "grow" in events and "unclamp" in events
        assert control.envelope is None  # 恢复到不限

    def test_grow_is_rate_limited(self) -> None:
        """恢复限频：同一分钟内的连续健康样本只涨一步。"""
        control = _warmed()
        observe(control, 1 * _MIB, 60 * _MIB, _NOW)
        after_clamp = control.envelope
        observe(control, 6 * _MIB, 1 * _MIB, _NOW + timedelta(seconds=61))
        grown = control.envelope
        assert grown is not None and after_clamp is not None and grown > after_clamp
        # 一秒后再来一个健康样本：未到限频间隔，不再涨
        observe(control, 6 * _MIB, 1 * _MIB, _NOW + timedelta(seconds=62))
        assert control.envelope == grown


class TestPerTaskLimit:
    def test_splits_envelope_with_floor(self) -> None:
        assert per_task_limit(None, 3) is None  # 不限
        assert per_task_limit(30 * _MIB, 3) == 10 * _MIB
        # 摊薄到地板以下时按地板：限速不能把任务饿死
        assert per_task_limit(2 * _MIB, 8) == _PER_TASK_FLOOR
        assert per_task_limit(30 * _MIB, 0) is None


class TestConcurrentDownloadCap:
    """并发上限由包络导出，没有自由参数——个数是速率的劣质代理，
    真正该被限的是下行总速率。"""

    def test_derived_from_envelope_over_floor(self) -> None:
        """上限 = 包络 / 每任务地板：正好是"再多一个就击穿地板"的那个点。

        超过它，per_task_limit 会退到地板，聚合速率反过来超出包络，
        限速形同虚设。
        """
        assert concurrent_download_cap(30 * _MIB) == 30 * _MIB // _PER_TASK_FLOOR
        assert concurrent_download_cap(5 * _MIB) == 5 * _MIB // _PER_TASK_FLOOR
        # 与 per_task_limit 自洽：正好在上限内时摊出的限速不低于地板
        cap = concurrent_download_cap(30 * _MIB)
        assert per_task_limit(30 * _MIB, cap) >= _PER_TASK_FLOOR

    def test_unclamped_link_has_no_cap(self) -> None:
        """链路从未出现拥塞证据（对称大带宽）→ 不设并发限制。

        对这类用户，任何固定并发帽都是纯粹的损失——下行再快也不伤上行。
        """
        assert concurrent_download_cap(None) is None

    def test_never_zero(self) -> None:
        """包络被压到极低时仍允许 1 个在下：否则刷流彻底停摆，
        而单个任务本来就受 per_task_limit 的地板保护。"""
        assert concurrent_download_cap(1) == 1
        assert concurrent_download_cap(_PER_TASK_FLOOR // 2) == 1
