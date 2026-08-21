"""刷流带宽哨兵：让刷流下载像"清道夫协议"一样只吃剩余容量。

首夜数据实证（2026-08-18 21 时）：刷流下载冲到 69 MB/s 时，上传从 5.2 MB/s
塌到 1.5——下行队列膨胀（bufferbloat）拖死了上传的 ACK/请求回程。上传是
刷流的稀缺资源，下载再快也不该以它为代价。本模块用双时间尺度闭环保护它：

- **快环（哨兵，约 20 秒）**：有刷流任务在下载时轮询下载器全局瞬时速度
  （transfer_speeds，一次轻量请求）。上行掉出健康区就立刻把刷流任务限速
  让路——不区分拥塞是刷流还是用户前台下载造成的，刷流是后台，见人就让。
  事故窗口从"塌一小时"缩到"塌半分钟"。
- **慢环（AIMD 包络）**：维护"该环境不伤上传的刷流下载总包络"。拥塞时
  乘性回退（×0.7），健康时加性恢复（+2 MiB/s），学到的值持久化到
  DownloaderClient.boost_dl_envelope_bytes，重启不清零。包络涨回下行观测
  峰值之上时自动解除限速——对称大带宽环境永远触发不了拥塞，全程零配置
  零行为变化。

环境无关是硬要求：所有阈值都是**相对量**（上行能力基线、下行峰值都是
衰减最大值，从每个用户自己的观测里长出来），没有任何写死的绝对带宽数字
（地板值除外——它保证被压制的任务仍能赶上免费窗口）。

安全约束：只对 movieclaw-boost 的任务按种限速，绝不动下载器全局限速或
用户自己的任务。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import select

from movieclaw_db.engine import get_database
from movieclaw_db.models import BoostTaskState, DownloaderClient, RatioBoostTask, utcnow
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader.base import BaseDownloader
from movieclaw_downloader.factory import create_downloader
from movieclaw_downloader.models import DownloaderConfig

logger = logging.getLogger("movieclaw_api.boost_bandwidth")

# ---------------------------------------------------------------------------
# 控制参数
# ---------------------------------------------------------------------------

# 快环节奏：有在下任务时 20 秒一采；空闲时 60 秒探一次库（一条索引查询）
_INTERVAL_ACTIVE = 20.0
_INTERVAL_IDLE = 60.0
# 上行基线/下行峰值用「衰减最大值」估计：每个样本衰减一点，半衰期约 3 天
# （20 秒采样 × 3 天 ≈ 13000 样本）——线路升降级能在几天内被重新学到
_PEAK_DECAY = 0.5 ** (1 / 13000)
# 基线成熟门槛：样本不足时绝不动手（刚启动的几分钟宁可不管，不能误伤）
_BASELINE_MIN_SAMPLES = 30
# 拥塞判据：上行 < 基线 × 0.6，且下行确有压力（> 8 MiB/s——低于此值的
# 下载不可能是拥塞主因，上传低多半是蜂群没需求，不能怪到下载头上）
_CONGESTED_UP_FRACTION = 0.6
_CONGESTION_MIN_DL = 8 * 1024 * 1024
# 健康判据（加性恢复的前提）：上行 ≥ 基线 × 0.85
_HEALTHY_UP_FRACTION = 0.85
# AIMD：拥塞乘性回退 ×0.7（持续拥塞下 2 分钟内退到地板）；健康时每分钟
# 加性恢复 2 MiB/s
_ENVELOPE_BACKOFF = 0.7
_ENVELOPE_STEP = 2 * 1024 * 1024
_GROW_MIN_INTERVAL = timedelta(seconds=60)
# 包络地板：被压到最低时刷流下载总共仍有 5 MiB/s——保证在下任务能以
# 可预期的速度赶免费窗口（赶不上的由 JIT 预测止损放弃）
_ENVELOPE_FLOOR = 5 * 1024 * 1024
# 单任务限速地板：包络摊薄到每任务不低于 1 MiB/s
_PER_TASK_FLOOR = 1024 * 1024


# ---------------------------------------------------------------------------
# 纯决策（无 IO，单测覆盖）
# ---------------------------------------------------------------------------


@dataclass
class LinkControl:
    """一台下载器所在链路的控制状态。

    baseline / dl_peak 是衰减最大值：既能快速学到能力上限（max 一步到位），
    又能在线路降级后随时间忘掉旧上限（衰减半衰期 3 天）。
    """

    baseline: float = 0.0  # 上行能力估计（字节/秒）
    dl_peak: float = 0.0  # 下行观测峰值（字节/秒）
    samples: int = 0
    envelope: float | None = None  # 安全下载包络；None=不限
    last_grow_at: datetime | None = None


def observe(control: LinkControl, up_bytes: int, dl_bytes: int, now: datetime) -> str | None:
    """喂入一次全局速度观测，更新控制状态。返回事件名（clamp/grow/unclamp）
    或 None（无动作）。

    AIMD 语义：
    - **clamp**：拥塞证据确凿（上行塌 + 下行有压力）→ 包络乘性回退。持续
      拥塞时每个样本都会再退，几个样本内到地板；
    - **grow**：上行健康 → 包络加性恢复（限频每分钟一步），拥塞若是偶发
      （邻居抢网/瞬时抖动），几十分钟后限制自动松开；
    - **unclamp**：包络涨回下行观测峰值之上 → 限速不再有约束意义，解除。
    """
    control.baseline = max(control.baseline * _PEAK_DECAY, float(up_bytes))
    control.dl_peak = max(control.dl_peak * _PEAK_DECAY, float(dl_bytes))
    control.samples += 1
    if control.samples < _BASELINE_MIN_SAMPLES or control.baseline <= 0:
        return None

    congested = (
        up_bytes < control.baseline * _CONGESTED_UP_FRACTION
        and dl_bytes > max(_CONGESTION_MIN_DL, control.envelope or 0)
    )
    if congested:
        basis = control.envelope if control.envelope is not None else float(dl_bytes)
        control.envelope = max(float(_ENVELOPE_FLOOR), basis * _ENVELOPE_BACKOFF)
        return "clamp"

    if (
        control.envelope is not None
        and up_bytes >= control.baseline * _HEALTHY_UP_FRACTION
        and (control.last_grow_at is None or now - control.last_grow_at >= _GROW_MIN_INTERVAL)
    ):
        control.envelope += _ENVELOPE_STEP
        control.last_grow_at = now
        if control.envelope >= control.dl_peak:
            control.envelope = None  # 已不构成约束，解除限速
            return "unclamp"
        return "grow"
    return None


def per_task_limit(envelope: float | None, task_count: int) -> int | None:
    """把包络摊到每个在下任务上（字节/秒）；None=不限速。"""
    if envelope is None or task_count <= 0:
        return None
    return max(_PER_TASK_FLOOR, int(envelope) // task_count)


def concurrent_download_cap(envelope: float | None) -> int | None:
    """刷流在下任务的并发上限，由包络导出；None=不设限。

    并发数本身**不该**被一个拍脑袋的常量限制。稀缺资源是上行，威胁它的是
    下行**总速率**（下行队列膨胀拖死上传的 ACK/请求回程），不是任务个数：
    3 个种各跑 20 MiB/s 远比 10 个种各跑 1 MiB/s 伤得多。总速率已经由本
    模块的 AIMD 包络管着，并发只在一个地方还需要设限——**包络的可执行性**。

    ``per_task_limit`` 把包络摊到每个任务上，且不低于每任务地板（地板保证
    被压制的任务仍能赶上免费窗口）。任务数一旦超过 ``包络 / 地板``，摊薄就
    击穿地板，聚合速率反过来超出包络，限速形同虚设。上限因此正好是这个比值，
    没有任何自由参数。

    ``envelope is None`` = 这条链路从未出现过拥塞证据（对称大带宽环境，
    下行再快也不伤上行）→ 不设并发限制。对这类用户，任何固定并发帽都是
    纯粹的损失。
    """
    if envelope is None:
        return None
    return max(1, int(envelope) // _PER_TASK_FLOOR)


# ---------------------------------------------------------------------------
# 哨兵循环
# ---------------------------------------------------------------------------


@dataclass
class _Applied:
    """上次已下发到下载器的限速状态，避免每个采样都重复调 API。"""

    limit: int | None = None
    hashes: frozenset[str] = field(default_factory=frozenset)


class _BandwidthSentinel:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._adapters: dict[int, BaseDownloader] = {}
        self._controls: dict[int, LinkControl] = {}
        self._applied: dict[int, _Applied] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="boost-bandwidth-sentinel")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for adapter in self._adapters.values():
            with contextlib.suppress(Exception):
                await adapter.close()
        self._adapters.clear()

    async def _run(self) -> None:
        while not self._stop.is_set():
            interval = _INTERVAL_IDLE
            try:
                interval = await self._tick()
            except Exception:  # noqa: BLE001 -- 哨兵绝不能因单次异常退出
                logger.warning("刷流带宽哨兵本轮采样失败，下轮重试", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    async def _adapter(self, row: DownloaderClient, repo: DownloaderRepository) -> BaseDownloader:
        assert row.id is not None
        adapter = self._adapters.get(row.id)
        if adapter is None:
            adapter = create_downloader(
                DownloaderConfig(
                    type=row.client_type.value,
                    url=row.url,
                    username=row.username,
                    password=repo.decrypted_password(row),
                )
            )
            self._adapters[row.id] = adapter
        return adapter

    async def _tick(self) -> float:
        """一次采样：读在下任务 → 读全局速度 → AIMD 决策 → 差异化下发限速。
        返回下一次采样的间隔秒数。"""
        now = utcnow()
        async with get_database().session() as session:
            rows = (
                await session.execute(
                    select(RatioBoostTask.downloader_id, RatioBoostTask.info_hash).where(
                        RatioBoostTask.state == BoostTaskState.ACTIVE,
                        RatioBoostTask.completed == False,  # noqa: E712 -- SQL 表达式
                        RatioBoostTask.downloader_id != None,  # noqa: E711
                    )
                )
            ).all()
            if not rows:
                return _INTERVAL_IDLE
            by_downloader: dict[int, set[str]] = {}
            for did, info_hash in rows:
                by_downloader.setdefault(did, set()).add(info_hash)

            repo = DownloaderRepository(session)
            for did, hashes in by_downloader.items():
                client_row = await session.get(DownloaderClient, did)
                if client_row is None or not client_row.enabled:
                    continue
                try:
                    await self._observe_and_apply(client_row, repo, hashes, now)
                except Exception:  # noqa: BLE001 -- 单台故障不拖垮循环
                    logger.warning(
                        "刷流带宽哨兵：下载器 #%d 采样/限速失败", did, exc_info=True
                    )
                    # 连接可能已坏：丢弃缓存适配器，下轮重建
                    adapter = self._adapters.pop(did, None)
                    if adapter is not None:
                        with contextlib.suppress(Exception):
                            await adapter.close()
            await session.commit()
        return _INTERVAL_ACTIVE

    async def _observe_and_apply(
        self,
        client_row: DownloaderClient,
        repo: DownloaderRepository,
        hashes: set[str],
        now: datetime,
    ) -> None:
        assert client_row.id is not None
        did = client_row.id
        adapter = await self._adapter(client_row, repo)
        up, dl = await adapter.transfer_speeds()

        control = self._controls.get(did)
        if control is None:
            # 冷启动：接续上次持久化的包络（拥塞记忆跨重启保留）
            control = LinkControl(envelope=float(client_row.boost_dl_envelope_bytes)
                                  if client_row.boost_dl_envelope_bytes else None)
            self._controls[did] = control

        event = observe(control, up, dl, now)
        if event is not None:
            persisted = int(control.envelope) if control.envelope is not None else None
            if client_row.boost_dl_envelope_bytes != persisted:
                client_row.boost_dl_envelope_bytes = persisted
                client_row.updated_at = now
            if event == "clamp":
                logger.info(
                    "刷流让路：上行 %.1f MiB/s 低于基线 %.1f 的健康区，下载包络回退至 %.1f MiB/s"
                    "（下载器「%s」，在下 %d 个任务）",
                    up / 1024**2,
                    control.baseline / 1024**2,
                    (control.envelope or 0) / 1024**2,
                    client_row.name,
                    len(hashes),
                )
            elif event == "unclamp":
                logger.info("刷流下载限速已解除（上行持续健康，下载器「%s」）", client_row.name)

        limit = per_task_limit(control.envelope, len(hashes))
        applied = self._applied.get(did)
        desired = _Applied(limit=limit, hashes=frozenset(hashes))
        if applied is None or applied.limit != desired.limit or applied.hashes != desired.hashes:
            # 任务集或目标限速有变化才下发；None（不限）也要显式下发一次，
            # 否则解除限速后新旧任务会残留旧限
            await adapter.set_download_limits(sorted(hashes), limit)
            self._applied[did] = desired


_sentinel: _BandwidthSentinel | None = None


def init_boost_bandwidth_sentinel() -> None:
    """启动刷流带宽哨兵后台循环（lifespan 启动阶段调用）。"""
    global _sentinel
    _sentinel = _BandwidthSentinel()
    _sentinel.start()
    logger.info("刷流带宽哨兵已启动（上行受压时刷流下载秒级让路）")


async def close_boost_bandwidth_sentinel() -> None:
    """停止哨兵并释放下载器连接（lifespan 关闭阶段调用）。"""
    global _sentinel
    if _sentinel is not None:
        await _sentinel.close()
        _sentinel = None
