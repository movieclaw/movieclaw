"""硬件加速自检（docs/design/web-player.md §5.2）。

自建软件里硬件加速最大的成本不是写代码，是**用户配不对**：``--device
/dev/dri`` 给没给、容器内用户在不在 ``render`` 组（GID 在群晖/unRAID/裸
Debian 上各不相同）、驱动版本够不够。这些出问题的现象**全都是「转码失败」
黑盒**——用户只看到播不了，既不知道原因也不知道该改什么。

所以这里不猜，**真跑一次一秒钟的编码**：

- 只看 ``ffmpeg -encoders`` 里有没有这个编码器是最常见的误判来源。发行版
  ffmpeg 普遍带着 ``h264_vaapi``，但容器里根本没挂 ``/dev/dri``，此时报
  「可用」会把用户直接送进必然失败的档 3。
- 只看设备节点存不存在也不够：节点在、但当前用户没有读写权限（最典型的
  群晖场景），照样编码不了。

探测结果连同**可操作的中文诊断**一起给出来，设置页直接展示。用户看到的不该是
「转码失败」，而是「检测到 Intel 核显，但 /dev/dri/renderD128 无访问权限，
请在 compose 里加 group_add」。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from movieclaw_api.services.playback.ffmpeg_args import HW_BACKENDS
from movieclaw_api.services.playback.remote_worker import remote_worker_available

logger = logging.getLogger("movieclaw_api.playback.hwprobe")

#: 单次探测上限。一秒钟的编码正常在两三秒内跑完；卡更久基本是驱动在等设备。
_PROBE_TIMEOUT = 20.0

#: 探测用的合成源：一秒、小分辨率，编码器初始化得起来就够，不追求真实负载。
_PROBE_SOURCE = "testsrc2=size=320x240:rate=25:duration=1"

#: 各后端的探测参数。与 ``ffmpeg_args.HW_BACKENDS`` 共用编码器名，避免两处漂移；
#: 但**上传路径不同**——探测是软件源要送进 GPU，播放是 GPU 解码直连编码。
_PROBE_FILTERS: dict[str, list[str]] = {
    "vaapi": ["-vaapi_device", "/dev/dri/renderD128", "-vf", "format=nv12,hwupload"],
    "qsv": ["-vf", "format=nv12,hwupload=extra_hw_frames=64"],
    "nvenc": ["-vf", "format=yuv420p"],
    "videotoolbox": ["-vf", "format=nv12"],
}

#: 后端 → 面向用户的名字。设置页里「vaapi」不如「Intel/AMD 核显」好懂。
BACKEND_LABELS: dict[str, str] = {
    "vaapi": "Intel / AMD 显卡（VAAPI）",
    "qsv": "Intel 核显（QSV）",
    "nvenc": "NVIDIA 显卡（NVENC）",
    "videotoolbox": "Apple 芯片（VideoToolbox）",
}


@dataclass(frozen=True)
class HwBackendStatus:
    """一个后端的自检结论。``detail`` 是给用户看的中文，不是给日志看的。"""

    name: str
    label: str
    available: bool
    detail: str


_cache: list[HwBackendStatus] | None = None


def probe_backends(*, force: bool = False) -> list[HwBackendStatus]:
    """（阻塞，调用方须放线程池）逐个后端真跑一秒编码。

    结果缓存：ffmpeg 构建、设备节点与驱动在进程生命周期内不会变，而决策要
    逐次播放问。``force=True`` 用于设置页的「重新检测」按钮——用户按提示挂上
    设备后要能立刻看到结果，不必重启容器。
    """
    global _cache
    if _cache is not None and not force:
        return _with_remote_backend(_cache)

    if shutil.which("ffmpeg") is None:
        _cache = [
            HwBackendStatus(
                name=name,
                label=BACKEND_LABELS.get(name, name),
                available=False,
                detail="系统中未找到 ffmpeg，无法检测硬件加速。",
            )
            for name in HW_BACKENDS
        ]
        return _with_remote_backend(_cache)

    encoders = _list_encoders()
    _cache = [_probe_one(name, encoders) for name in HW_BACKENDS]
    usable = [s.name for s in _cache if s.available]
    if usable:
        logger.info("硬件加速自检通过：%s", "、".join(usable))
    else:
        logger.info(
            "未检测到可用的硬件加速——需要转码的片子只能走软件转码"
            "（默认关闭，播放此类影片时会弹窗征求开启）。逐后端原因如下：%s",
            "；".join(f"{s.label}：{s.detail}" for s in _cache if not s.available),
        )
    return _with_remote_backend(_cache)


def available_backends() -> tuple[str, ...]:
    """可用后端名，按优先级。决策引擎的 ``PlaybackPolicy`` 输入之一。"""
    return tuple(s.name for s in probe_backends() if s.available)


def available_local_backends() -> tuple[str, ...]:
    """只返回 NAS 当前进程实际探测通过的后端。

    ``available_backends()`` 会叠加外置 Worker 的 VideoToolbox 能力，供播放
    决策判断「是否存在硬件转码能力」。但这个合并结果不能直接拿来生成 NAS
    进程的 ffmpeg 参数，否则 Worker 短暂断线的竞态会把
    ``h264_videotoolbox`` 错交给 Linux ffmpeg。执行层必须使用这份本地快照，
    远程执行则由会话路由单独选择。
    """
    if _cache is None:
        probe_backends()
    return tuple(status.name for status in (_cache or ()) if status.available)


def hardware_available() -> bool:
    return bool(available_backends())


def reset_probe_cache() -> None:
    """测试用：丢掉缓存。"""
    global _cache
    _cache = None


async def probe_backends_async(*, force: bool = False) -> list[HwBackendStatus]:
    return await asyncio.to_thread(probe_backends, force=force)


def _with_remote_backend(statuses: list[HwBackendStatus]) -> list[HwBackendStatus]:
    """把在线远程 Worker 映射为决策层可识别的 VideoToolbox 后端。

    远程 Worker 不参与 NAS 容器内的一秒探测；它在连接 hello 中已经完成了
    ffmpeg 能力探测。动态追加而不是写入本地缓存，保证 Worker 断线后下一次
    决策立即回到真实的本机结果。
    """
    if not remote_worker_available("videotoolbox"):
        return statuses
    remote_status = HwBackendStatus(
        name="videotoolbox",
        label=BACKEND_LABELS["videotoolbox"],
        available=True,
        detail="已连接远程 Worker，并通过其 VideoToolbox 能力声明。",
    )
    # Linux 探测矩阵也会包含 videotoolbox=False；在线 Worker 应覆盖这个
    # “本机没有该后端”的诊断，而不是重复返回两个同名后端。
    if any(status.name == "videotoolbox" for status in statuses):
        return [
            remote_status if status.name == "videotoolbox" else status
            for status in statuses
        ]
    return [*statuses, remote_status]


def _probe_one(name: str, encoders: frozenset[str]) -> HwBackendStatus:
    backend = HW_BACKENDS[name]
    label = BACKEND_LABELS.get(name, name)

    def status(available: bool, detail: str) -> HwBackendStatus:
        return HwBackendStatus(name=name, label=label, available=available, detail=detail)

    if backend.encoder not in encoders:
        # VideoToolbox 只存在于 macOS，Linux 容器里查不到是正常的，别让用户
        # 以为自己漏配了什么。
        hint = (
            "这是 macOS 专有的加速方式，Linux 上不存在，属正常情况。"
            if name == "videotoolbox"
            else "官方镜像已内置支持；自行编译或使用发行版 ffmpeg 时可能缺少。"
        )
        return status(False, f"当前 ffmpeg 构建不含 {backend.encoder} 编码器。{hint}")
    device_problem = _device_problem(name)
    if device_problem is not None:
        return status(False, device_problem)

    proc = _run_probe(backend.encoder, name)
    if proc is None:
        return status(False, "硬件编码探测超时——设备可能被占用或驱动无响应。")
    if proc.returncode == 0:
        return status(True, "已通过一秒钟的真实编码验证，可用于硬件转码。")
    return status(False, _explain_failure(proc.stderr.decode(errors="replace")))


def _run_probe(encoder: str, name: str) -> subprocess.CompletedProcess[bytes] | None:
    argv = [
        "ffmpeg", "-hide_banner", "-v", "error",
        "-f", "lavfi", "-i", _PROBE_SOURCE,
        *_PROBE_FILTERS.get(name, []),
        "-c:v", encoder, "-f", "null", "-",
    ]
    try:
        return subprocess.run(argv, capture_output=True, timeout=_PROBE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _device_problem(name: str) -> str | None:
    """设备节点层面的问题；没问题返回 None。

    这一层单独拿出来说，是因为它的修法与「驱动装没装」完全不同：一个是改
    compose，一个是装驱动。混成一句「硬件不可用」用户无从下手。
    """
    if name == "videotoolbox":
        return None  # 不依赖固定节点
    if name == "nvenc":
        if not Path("/dev/nvidia0").exists() and not Path("/dev/nvidiactl").exists():
            return (
                "未检测到 NVIDIA 设备节点。Docker 部署需要安装 NVIDIA Container "
                "Toolkit，并在 compose 里声明 `deploy.resources.reservations.devices` "
                "或加 `--gpus all`。"
            )
        return None
    # vaapi / qsv 都走 /dev/dri
    dri = Path("/dev/dri")
    if not dri.is_dir():
        return (
            "未检测到 /dev/dri。容器部署需要把显卡设备挂进容器——在 compose 的"
            "服务下加 `devices: [\"/dev/dri:/dev/dri\"]` 后重启。"
        )
    render_nodes = sorted(dri.glob("renderD*"))
    if not render_nodes:
        return "/dev/dri 里没有 renderD* 渲染节点，宿主机可能没有可用的显卡驱动。"
    if not any(os.access(node, os.R_OK | os.W_OK) for node in render_nodes):
        gid = _node_gid(render_nodes[0])
        hint = f"（宿主机上该节点属组 GID 是 {gid}）" if gid is not None else ""
        return (
            f"{render_nodes[0]} 存在但当前用户无读写权限{hint}。请在 compose 里加 "
            "`group_add: [\"<render 组 GID>\"]` 后重启容器——这个 GID 在群晖、"
            "unRAID 与各发行版上并不相同，需按宿主机实际值填。"
        )
    return None


def _node_gid(node: Path) -> int | None:
    try:
        return node.stat().st_gid
    except OSError:
        return None


def _explain_failure(stderr: str) -> str:
    """ffmpeg 的英文报错 → 可操作的中文。命中不了就原样带上摘要，别装懂。"""
    lowered = stderr.lower()
    if "permission denied" in lowered:
        return (
            "驱动报权限不足。请把容器用户加入宿主机的 render 组"
            "（compose 里的 `group_add`）后重启。"
        )
    if "no such file or directory" in lowered or "cannot open" in lowered:
        return "驱动打不开设备节点，请确认显卡设备已挂进容器且宿主机驱动正常。"
    if "not supported" in lowered or "unsupported" in lowered:
        return "驱动不支持这种编码，可能是显卡代次过旧或驱动版本过低。"
    if "failed to initialise" in lowered or "failed to initialize" in lowered:
        return "驱动初始化失败，通常是宿主机显卡驱动缺失或版本不匹配。"
    summary = " ".join(stderr.split())[:200]
    return f"硬件编码探测失败：{summary or '（ffmpeg 未给出原因）'}"


def _list_encoders() -> frozenset[str]:
    """ffmpeg 构建里带的编码器名集合。"""
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
