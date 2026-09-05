"""下载器路径可达性体检：把「文件在哪、movieclaw 看不看得见」做成可自动判定的结论。

## 为什么需要它

下载器的「测试连接」只验证 API 通不通。但 movieclaw 与下载器常常分处不同容器
甚至不同主机，两边看同一块盘的路径不一样（``path_mappings`` 就是为此存在的）。
**API 通、路径瞎**是完全可能的组合，而且症状极具迷惑性：下载器一切正常、种子
100% 完成，movieclaw 却报「下载完成但无法入库」——用户按提示去检查路径映射，
看到的配置又完全正确（配置确实是对的，坏的是运行时挂载），于是排查就此卡死。

真实事故：容器创建时 bind mount 解析失败，docker 兜底在 overlay 层建了个空目录，
``docker inspect`` 看配置一切正常，容器内 ``ls /download`` 却是空的。这个故障
持续 45 小时，期间系统反复重下同一批内容白烧 90GB，没有任何一条告警说出根因。

## 判定口径

体检不给"可能是 A 或 B 或 C"的三选一——那是把诊断成本转嫁给最没有能力承担的人。
每种状态对应一个确切结论和一个确切动作：

- ``UNMAPPED``：路径映射里没有覆盖这个目录，翻译无从谈起；
- ``MISSING``：目录在 movieclaw 侧根本不存在（卷没挂载，或映射的 local 侧写错）；
- ``NOT_DIR``：路径存在但不是目录（映射指到了文件上）；
- ``EMPTY``：目录存在**但是空的**——这是挂载失效的典型特征（容器启动时绑定
  没解析成功，docker 在 overlay 层建了个空壳），重启容器通常即可修复；
- ``OK``：目录可见且有内容。

``EMPTY`` 与 ``MISSING`` 必须分开：前者重启容器就好，后者要去改配置或挂卷，
处理方式完全不同，合成一句"路径不可达"等于什么都没说。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from movieclaw_api.services.torrent_submit import mapping_covers

logger = logging.getLogger("movieclaw_api.downloader_paths")


class PathState(StrEnum):
    """一条路径在 movieclaw 侧的体检结论。顺序即严重程度，越靠后越糟。"""

    OK = "ok"
    EMPTY = "empty"
    NOT_DIR = "not_dir"
    MISSING = "missing"
    UNMAPPED = "unmapped"


# 除 OK 外都算故障；供调用方一处判定，避免各处重复枚举
BROKEN_STATES = frozenset(
    {PathState.EMPTY, PathState.NOT_DIR, PathState.MISSING, PathState.UNMAPPED}
)

# 每种状态的用户可读结论 + 该做什么。文案直接进红灯与设置页，
# 因此说的是"接下来做什么"，不是"可能哪里有问题"
_DETAIL: dict[PathState, str] = {
    PathState.OK: "可见，目录有内容",
    PathState.EMPTY: (
        "目录存在但完全是空的——通常是容器启动时挂载没有生效"
        "（docker 会在这种情况下兜底建一个空目录，配置看起来完全正常）。"
        "重启 movieclaw 容器一般即可修复"
    ),
    PathState.NOT_DIR: "这个路径存在，但它是一个文件而不是目录，映射的本地侧写错了",
    PathState.MISSING: (
        "movieclaw 侧根本没有这个目录：存放下载的卷没有挂载到 movieclaw，或者路径映射的本地侧写错了"
    ),
    PathState.UNMAPPED: (
        "下载器上报的这个目录没有被任何一条路径映射覆盖，movieclaw 无法把它翻译成自己能访问的位置"
    ),
}


@dataclass(frozen=True)
class PathProbe:
    """一条路径的体检结果。``local`` 是 movieclaw 视角，``remote`` 是下载器视角。"""

    local: str
    remote: str
    state: PathState
    detail: str

    @property
    def healthy(self) -> bool:
        return self.state == PathState.OK

    def as_dict(self) -> dict[str, str]:
        """落库与接口返回用的扁平形态。"""
        return {
            "local": self.local,
            "remote": self.remote,
            "state": self.state.value,
            "detail": self.detail,
        }


def _classify(local: str) -> PathState:
    """对 movieclaw 视角的一个目录做 stat 判定。

    任何 OSError（权限不足、网络挂载超时等）都归入 MISSING——从 movieclaw 的
    角度看，读不到就是不可达，与不存在等价；具体原因写进日志供排查。
    """
    path = Path(local)
    try:
        if not path.exists():
            return PathState.MISSING
        if not path.is_dir():
            return PathState.NOT_DIR
        # 只取第一个条目就返回，不遍历整个目录——下载目录可能有上万个文件
        next(path.iterdir())
    except StopIteration:
        return PathState.EMPTY
    except OSError as exc:
        logger.warning("路径体检读取失败（按不可达处理）：%s —— %s", local, exc)
        return PathState.MISSING
    return PathState.OK


def probe_local_dir(local: str, remote: str = "") -> PathProbe:
    """体检单个 movieclaw 视角目录。"""
    state = _classify(local)
    return PathProbe(local=local, remote=remote, state=state, detail=_DETAIL[state])


def probe_mappings(mappings: list[dict[str, str]] | None) -> list[PathProbe]:
    """逐条体检下载器的路径映射。

    没有配置映射时返回空列表——两边视角一致的直装部署无路径可验，
    不该因为"没配映射"就报故障。
    """
    if not mappings:
        return []
    probes: list[PathProbe] = []
    for mapping in mappings:
        local = mapping.get("local")
        if not local:
            continue
        probes.append(probe_local_dir(local, mapping.get("remote", "")))
    return probes


def diagnose_landing(
    local_dir: str | None,
    mappings: list[dict[str, str]] | None,
) -> PathProbe:
    """落点核验专用：判定「下载器说文件在这，movieclaw 为什么看不到」。

    先确认这个目录是否被映射覆盖（``UNMAPPED`` 与目录本身的问题是两回事，
    前者是配置缺失、后者是环境故障），再对目录本身做体检。
    """
    if not local_dir:
        return PathProbe(
            local="", remote="", state=PathState.UNMAPPED, detail=_DETAIL[PathState.UNMAPPED]
        )
    if mappings and not mapping_covers(local_dir, mappings):
        return PathProbe(
            local=local_dir,
            remote="",
            state=PathState.UNMAPPED,
            detail=_DETAIL[PathState.UNMAPPED],
        )
    return probe_local_dir(local_dir)


def summarize(probes: list[PathProbe]) -> tuple[PathState, str] | None:
    """把多条体检结果收敛成"最严重的那一条"，供状态栏与红灯标题使用。

    返回 None 表示全部健康（或无可验路径）。
    """
    broken = [p for p in probes if not p.healthy]
    if not broken:
        return None
    worst = max(broken, key=lambda p: list(PathState).index(p.state))
    return worst.state, worst.detail
