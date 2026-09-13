"""转码产物的跨会话复用：指纹与台账文件（docs/design/player-pipeline-optimization.md §B）。

转码结果是（源文件、档位、参数）的**确定性函数**，但此前每个会话都从零转：
会话结束整目录删除，续播、重看、家里两个人先后看同一部片都整部重转。本模块
把「哪些输入决定输出字节」收成一个指纹，并给会话目录配一份可跨进程读的台账，
让下一次开会话能认领同指纹的目录、已转出的分片直接读文件。

两条不变量：

1. **任何影响输出字节的输入都必须进指纹**。漏一项就是静默播错流（换了音轨
   仍命中旧缓存，用户听到的是上次选的那条）。``CACHE_VERSION`` 是命令装配
   参数的版本号——改 ffmpeg 参数时 +1，旧缓存整体作废。
2. **台账只登记「写完了」的分片**。文件存在不代表写完（ffmpeg 边写边落盘），
   台账里的编号来自会话层已确认完成的集合；认领时再与盘上实际存在的文件求交。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from movieclaw_playback.decide import PlaybackPlan
from movieclaw_playback.hls_vod import SegmentPlan

logger = logging.getLogger("movieclaw_api.playback.cache")

#: 命令装配的输出版本。ffmpeg_args 里任何会改变分片字节的改动都要 +1。
CACHE_VERSION = 1
#: 台账文件名。目录里没有它就是上次进程没走完 stop 的残留，启动时清掉。
MANIFEST_NAME = "manifest.json"
#: 台账格式版本，与 CACHE_VERSION 分开：前者管「文件内容还认不认」，
#: 后者管「台账还读不读得懂」。
MANIFEST_FORMAT = 1


def cache_components(
    plan: PlaybackPlan,
    *,
    source_path: str,
    hw_backend: str | None,
    remote: bool,
    segment_plan: SegmentPlan,
) -> dict[str, Any]:
    """指纹的明文成分。全部落进台账，命中时逐项比对，不只比哈希。"""
    try:
        stat = os.stat(source_path)
        source = {"path": source_path, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        # 源文件 stat 不到（网络挂载抖动）：仍按路径算，但记下无法校验——
        # 文件被替换而路径不变时会命中旧缓存，这是可接受的极端边角
        source = {"path": source_path, "size": None, "mtime_ns": None}
    return {
        "version": CACHE_VERSION,
        "file_id": plan.file_id,
        "source": source,
        "tier": int(plan.tier),
        "container": plan.container,
        "video": asdict(plan.video),
        "audio": asdict(plan.audio),
        # 远程 Worker 与本地同名后端的编码器实现不同（jellyfin-ffmpeg 版本、
        # 平台），产物不能互认
        "backend": f"{'remote' if remote else 'local'}:{hw_backend or 'software'}",
        # 分片边界由关键帧表与时长决定，同一个源文件恒定；仍然记进指纹，
        # 关键帧探测规则变了（SEGMENT_SECONDS、切分规则）旧缓存自然失效
        "boundaries": hashlib.sha1(
            repr((tuple(segment_plan.boundaries), segment_plan.duration_s)).encode()
        ).hexdigest(),
    }


def cache_key(components: dict[str, Any]) -> str:
    """成分 → 目录名。24 个十六进制字符，碰撞概率对一台 NAS 的片库可以忽略。"""
    payload = json.dumps(components, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:24]


@dataclass
class Manifest:
    """会话目录的台账。"""

    key: str
    components: dict[str, Any]
    boundaries: list[float]
    duration_s: float
    completed: list[int]
    created_at: float
    last_used_at: float

    @classmethod
    def load(cls, directory: Path) -> Manifest | None:
        """读台账；缺失、损坏、格式版本不认识都返回 None（视为无缓存）。"""
        path = directory / MANIFEST_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("format") != MANIFEST_FORMAT:
            return None
        try:
            return cls(
                key=str(raw["key"]),
                components=dict(raw["components"]),
                boundaries=[float(b) for b in raw["boundaries"]],
                duration_s=float(raw["duration_s"]),
                completed=sorted({int(i) for i in raw["completed"]}),
                created_at=float(raw.get("created_at", 0.0)),
                last_used_at=float(raw.get("last_used_at", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, directory: Path) -> None:
        """原子写：先写临时文件再 rename，读到半个 JSON 就当没有台账。"""
        path = directory / MANIFEST_NAME
        tmp = directory / f".{MANIFEST_NAME}.tmp"
        payload = {
            "format": MANIFEST_FORMAT,
            "key": self.key,
            "components": self.components,
            "boundaries": self.boundaries,
            "duration_s": self.duration_s,
            "completed": sorted(self.completed),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("转码缓存台账写入失败（%s）：%s", directory, exc)

    def matches(self, components: dict[str, Any]) -> bool:
        """成分逐项相等才算同一份缓存——不只比哈希，哈希碰撞与版本漂移都挡住。"""
        return self.components == components

    def usable_segments(self, directory: Path) -> set[int]:
        """台账登记且盘上真的还在的分片。"""
        present: set[int] = set()
        for index in self.completed:
            if (directory / f"seg{index:05d}.m4s").is_file():
                present.add(index)
        return present


def new_manifest(key: str, components: dict[str, Any], segment_plan: SegmentPlan) -> Manifest:
    now = time.time()
    return Manifest(
        key=key,
        components=components,
        boundaries=list(segment_plan.boundaries),
        duration_s=segment_plan.duration_s,
        completed=[],
        created_at=now,
        last_used_at=now,
    )
