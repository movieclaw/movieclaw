"""硬件加速可用性判定（docs/design/web-player.md §5.2 第一版）。

自建软件里硬件加速最大的成本不是写代码，是**用户配不对**：``--device
/dev/dri`` 给没给、容器内用户在不在 ``render`` 组（GID 在群晖/unRAID/裸
Debian 上各不相同）、驱动版本够不够。这些出问题的现象全都是「转码失败」黑盒。

本版做两件事，**两个条件都满足才算可用**：
1. ffmpeg 构建里确实带了对应的硬件编码器（``ffmpeg -encoders``）；
2. 对应的设备节点存在且当前进程可读写。

只满足 (1) 是最常见的误判来源——发行版 ffmpeg 普遍带着 vaapi 编码器，但
容器里根本没挂 ``/dev/dri``，此时报「可用」会把用户直接送进必然失败的档 3。

⚠️ 尚未实现的部分：§5.2 要求的**真实 1 秒转码探测**（跑一次真的编码，
把失败原因翻成中文写进设置页）随执行器一起做。在那之前本模块只给
「大概率可用」的结论，不给诊断文案。
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("movieclaw_api.playback.hwprobe")

_PROBE_TIMEOUT = 15.0

# 编码器名 → 必须存在的设备节点（None = 该后端不依赖固定节点）。
# 顺序即优先级：先专用（QSV/NVENC）后通用（VAAPI），最后是 macOS 本机开发用的
# VideoToolbox——容器里不会命中它，但原生跑后端时能真正跑通硬件路径。
_BACKENDS: tuple[tuple[str, str, str | None], ...] = (
    ("qsv", "h264_qsv", "/dev/dri"),
    ("nvenc", "h264_nvenc", "/dev/nvidia0"),
    ("vaapi", "h264_vaapi", "/dev/dri"),
    ("videotoolbox", "h264_videotoolbox", None),
)


@functools.cache
def available_backends() -> tuple[str, ...]:
    """当前环境可用的硬件编码后端名（按优先级），空元组=只能软件转码。

    缓存：ffmpeg 构建与设备节点在进程生命周期内不会变，而决策要逐次播放问。
    """
    encoders = _list_encoders()
    if not encoders:
        return ()
    found = tuple(
        name
        for name, encoder, device in _BACKENDS
        if encoder in encoders and _device_usable(device)
    )
    if not found:
        logger.info(
            "未检测到可用的硬件加速编码器——需要转码的片子只能走软件转码"
            "（默认关闭，可在「设置 → 播放」开启）。容器部署请确认已挂载 "
            "/dev/dri 或显卡设备，并把容器用户加入 render 组。"
        )
    return found


def hardware_available() -> bool:
    """有没有任何可用的硬件加速后端。决策引擎的 ``PlaybackPolicy`` 输入之一。"""
    return bool(available_backends())


@functools.cache
def _list_encoders() -> frozenset[str]:
    """ffmpeg 构建里带的编码器名集合；ffmpeg 缺失返回空集。"""
    if shutil.which("ffmpeg") is None:
        return frozenset()
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-encoders"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    # 每行形如 " V....D h264_vaapi   H.264/AVC (VAAPI)"，取第二列
    names = set()
    for line in proc.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6:
            names.add(parts[1])
    return frozenset(names)


def _device_usable(device: str | None) -> bool:
    """设备节点存在且可读写。None = 该后端不依赖固定节点（如 VideoToolbox）。"""
    if device is None:
        return True
    path = Path(device)
    if not path.exists():
        return False
    if path.is_dir():
        # /dev/dri 目录：要有至少一个 renderD* 且可读写（权限不足是最常见的坑）
        return any(os.access(child, os.R_OK | os.W_OK) for child in path.glob("renderD*"))
    return os.access(path, os.R_OK | os.W_OK)
