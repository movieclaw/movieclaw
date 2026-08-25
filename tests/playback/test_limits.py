"""资源上限自动推导的表驱动测试（services/playback/limits.py）。

这些值原来是「设置 → 播放」的配置项，2026-08-25 撤下后由机器规格推导——
推导公式就是新的「配置」，所以边界要钉死：算错一档就是把机器拖死或把
用户挡在门外。
"""

from __future__ import annotations

import pytest

from movieclaw_api.services.playback.limits import (
    _QUOTA_FALLBACK_BYTES,
    auto_quota_bytes,
    auto_transcode_concurrency,
)


def test_hardware_transcode_is_fixed_at_three() -> None:
    """有硬件加速时编码在 GPU 上，与核数无关——消费级显卡 3 路是安全值。"""
    assert auto_transcode_concurrency(hardware=True, cores=2) == 3
    assert auto_transcode_concurrency(hardware=True, cores=32) == 3


@pytest.mark.parametrize(
    ("cores", "expected"),
    [
        (2, 1),  # 双核也至少给 1 路——0 路等于播放功能不存在
        (4, 1),
        (8, 2),
        (16, 4),
        (64, 4),  # 封顶 4：软转再多路会把订阅、扫描等后台任务挤死
    ],
)
def test_software_transcode_scales_with_cores(cores: int, expected: int) -> None:
    """纯软转每路约吃 4 核，按核数折算。"""
    assert auto_transcode_concurrency(hardware=False, cores=cores) == expected


def test_quota_is_a_quarter_of_free_space_clamped(tmp_path, monkeypatch) -> None:
    """配额 = 剩余空间的四分之一，钳在 2–100 GB——盘越紧张配额越收缩。"""
    from movieclaw_api.services.playback import limits

    def fake_usage(free: int):
        return lambda _p: type("U", (), {"total": 0, "used": 0, "free": free})()

    monkeypatch.setattr(limits.shutil, "disk_usage", fake_usage(40 * 1024**3))
    assert auto_quota_bytes(tmp_path) == 10 * 1024**3

    monkeypatch.setattr(limits.shutil, "disk_usage", fake_usage(1 * 1024**3))
    assert auto_quota_bytes(tmp_path) == 2 * 1024**3  # 下限兜底

    monkeypatch.setattr(limits.shutil, "disk_usage", fake_usage(4000 * 1024**3))
    assert auto_quota_bytes(tmp_path) == 100 * 1024**3  # 上限封顶


def test_quota_falls_back_when_disk_unreadable(tmp_path, monkeypatch) -> None:
    """statfs 失败（目录被卸载等）回退保守默认值，而不是放开不限。"""
    from movieclaw_api.services.playback import limits

    def boom(_p):
        raise OSError("statfs 失败")

    monkeypatch.setattr(limits.shutil, "disk_usage", boom)
    assert auto_quota_bytes(tmp_path) == _QUOTA_FALLBACK_BYTES
