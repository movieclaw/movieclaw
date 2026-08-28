"""硬件加速自检测试（docs/design/web-player.md §5.2）。

自建软件里硬件加速最大的成本不是写代码，是**用户配不对**。所以这里测的重点
不是「探测准不准」（那要真显卡），而是**每一种失败都给出了可操作的中文修法**：
说清是设备没挂、还是权限不够、还是驱动不行——三者的改法完全不同，混成一句
「硬件不可用」用户无从下手。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from movieclaw_api.services.playback import hwprobe


@pytest.fixture(autouse=True)
def clean_cache():
    hwprobe.reset_probe_cache()
    yield
    hwprobe.reset_probe_cache()


def status_of(statuses, name):
    return next(s for s in statuses if s.name == name)


def test_missing_ffmpeg_reports_every_backend_with_a_reason(monkeypatch):
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: None)
    statuses = hwprobe.probe_backends()
    assert statuses and all(not s.available for s in statuses)
    assert all("未找到 ffmpeg" in s.detail for s in statuses)
    assert hwprobe.hardware_available() is False


def test_encoder_missing_from_build_says_so(monkeypatch):
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", lambda: frozenset())
    statuses = hwprobe.probe_backends()
    assert "不含 h264_vaapi 编码器" in status_of(statuses, "vaapi").detail


def test_videotoolbox_absence_on_linux_is_explained_as_normal(monkeypatch):
    """别让 Linux 用户以为自己漏配了什么——VideoToolbox 是 macOS 专有的。"""
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", lambda: frozenset())
    detail = status_of(hwprobe.probe_backends(), "videotoolbox").detail
    assert "macOS" in detail and "正常" in detail


def test_missing_dri_tells_the_user_to_mount_the_device(monkeypatch, tmp_path):
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", lambda: frozenset({"h264_vaapi"}))
    monkeypatch.setattr(hwprobe, "Path", lambda p: tmp_path / "nope")
    detail = status_of(hwprobe.probe_backends(), "vaapi").detail
    assert "/dev/dri" in detail and "devices:" in detail


def test_render_node_without_permission_names_the_group_fix(monkeypatch, tmp_path):
    """群晖上最典型的一种：节点在、但容器用户不在 render 组。"""
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "renderD128").write_bytes(b"")
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", lambda: frozenset({"h264_vaapi"}))
    monkeypatch.setattr(hwprobe, "Path", lambda _p: dri)
    monkeypatch.setattr(hwprobe.os, "access", lambda *_a: False)
    detail = status_of(hwprobe.probe_backends(), "vaapi").detail
    assert "无读写权限" in detail
    assert "group_add" in detail
    assert "GID" in detail  # 要报出宿主机上的实际 GID，用户才知道填什么


def test_dri_without_render_nodes_points_at_the_host(monkeypatch, tmp_path):
    dri = tmp_path / "dri"
    dri.mkdir()
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", lambda: frozenset({"h264_vaapi"}))
    monkeypatch.setattr(hwprobe, "Path", lambda _p: dri)
    assert "renderD" in status_of(hwprobe.probe_backends(), "vaapi").detail


def test_nvidia_without_device_nodes_names_the_toolkit(monkeypatch, tmp_path):
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", lambda: frozenset({"h264_nvenc"}))
    monkeypatch.setattr(hwprobe, "Path", lambda _p: tmp_path / "nope")
    detail = status_of(hwprobe.probe_backends(), "nvenc").detail
    assert "NVIDIA Container Toolkit" in detail


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("Permission denied", "render 组"),
        ("Cannot open display", "设备节点"),
        ("Function not supported", "不支持"),
        ("Failed to initialise VAAPI connection", "驱动初始化失败"),
    ],
)
def test_driver_errors_are_translated_to_actionable_chinese(stderr, expected):
    assert expected in hwprobe._explain_failure(stderr)


def test_unrecognised_error_carries_the_original_summary():
    """命中不了就原样带上摘要，别装懂——用户报障时这句话就是全部线索。"""
    detail = hwprobe._explain_failure("Some brand new driver bug 0xdeadbeef")
    assert "0xdeadbeef" in detail


def test_successful_probe_marks_the_backend_available(monkeypatch, tmp_path):
    """真跑通一秒编码才算可用——只看编码器在不在是最常见的误判来源。"""
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "renderD128").write_bytes(b"")
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", lambda: frozenset({"h264_vaapi"}))
    monkeypatch.setattr(hwprobe, "Path", lambda _p: dri)
    monkeypatch.setattr(hwprobe.os, "access", lambda *_a: True)
    monkeypatch.setattr(
        hwprobe, "_run_probe",
        lambda *_a: subprocess.CompletedProcess([], 0, b"", b""),
    )
    statuses = hwprobe.probe_backends()
    assert status_of(statuses, "vaapi").available is True
    assert "真实编码验证" in status_of(statuses, "vaapi").detail
    assert "vaapi" in hwprobe.available_backends()


def test_probe_timeout_is_reported_not_swallowed(monkeypatch, tmp_path):
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "renderD128").write_bytes(b"")
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", lambda: frozenset({"h264_vaapi"}))
    monkeypatch.setattr(hwprobe, "Path", lambda _p: dri)
    monkeypatch.setattr(hwprobe.os, "access", lambda *_a: True)
    monkeypatch.setattr(hwprobe, "_run_probe", lambda *_a: None)
    assert "超时" in status_of(hwprobe.probe_backends(), "vaapi").detail


def test_result_is_cached_until_forced(monkeypatch):
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return frozenset()

    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", counting)
    hwprobe.probe_backends()
    hwprobe.probe_backends()
    assert calls["n"] == 1, "缓存没生效——决策要逐次播放问，每次都跑一秒编码不可接受"
    hwprobe.probe_backends(force=True)
    assert calls["n"] == 2, "设置页的「重新检测」必须真的重测"


def test_local_available_backends_excludes_remote_overlay(monkeypatch):
    """远程 Worker 的 VideoToolbox 能力不能变成本地 ffmpeg 参数。"""
    monkeypatch.setattr(hwprobe.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(hwprobe, "_list_encoders", lambda: frozenset())
    monkeypatch.setattr(hwprobe, "remote_worker_available", lambda _backend: True)

    assert hwprobe.available_backends() == ("videotoolbox",)
    assert hwprobe.available_local_backends() == ()


def test_every_backend_has_a_human_label():
    """设置页里「vaapi」不如「Intel / AMD 显卡」好懂。"""
    from movieclaw_api.services.playback.ffmpeg_args import HW_BACKENDS

    for name in HW_BACKENDS:
        assert hwprobe.BACKEND_LABELS.get(name), f"{name} 缺少中文名"


@pytest.mark.integration
@pytest.mark.skipif(not Path("/usr/bin/ffmpeg").exists(), reason="需要系统 ffmpeg")
def test_real_probe_runs_and_explains_itself():
    """在没有显卡的机器上跑：每一条都必须给出非空的中文说明，不能只是 False。"""
    statuses = hwprobe.probe_backends(force=True)
    assert len(statuses) >= 4
    for status in statuses:
        assert status.detail, f"{status.name} 没有给出原因"
        assert status.label
