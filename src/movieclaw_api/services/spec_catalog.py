"""OpenAPI spec 的目录视图：哪些操作会成为 CLI 命令、覆盖哪些域。

**为什么单独成文件**：服务端渲染 Agent 的 mclaw 工具描述时需要知道「CLI 有哪些
域」，此前是直接 `import movieclaw_cli` 拿的。那是一处方向错误的依赖——被 import
的三个函数做的事就是**读 spec.json**，与「命令行客户端」没有任何关系，服务端却
因此在运行期依赖了一个客户端包。CLI 换语言时这处依赖会立刻变成硬阻塞。

这里只做服务端真正需要的那点事：展平 operation、判定是否进命令树、给出域集合。
参数解析、schema 展开、命令树构建那些是 CLI 自己的事，不在这里。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from movieclaw_api.exceptions import AppException

logger = logging.getLogger(__name__)

#: spec 基线文件。它是构建产物，不入 git：镜像与发版脚本构建期由
#: `movieclaw_api.export_openapi` 现场导出，因此部署环境里一定存在且与代码
#: 严格同版。本地从源码直接起服务时通常没有这个文件，load_spec 会从代码现算。
_SPEC_PATH = Path(__file__).resolve().parents[1] / "data" / "spec.json"

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


class SpecCatalogUnavailable(AppException):
    """基线 spec 文件存在但内容坏了。

    只可能是部署产物损坏（基线文件被截断或改坏）。这类故障必须给出可读
    结论——裸的 "internal server error" 会让自部署用户完全无从判断该重新部署
    还是该改配置。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=500,
            code="SPEC_BASELINE_CORRUPT",
            message=(
                f"服务端的 CLI 基线 spec 损坏（{_SPEC_PATH}）：{detail}。"
                "Agent 功能不可用。这通常是镜像或安装包不完整，请更新到新版镜像后重新部署。"
            ),
        )


def load_spec() -> dict[str, Any]:
    """读取基线 spec；文件不存在时从当前代码现算。

    两条路径得到的内容完全一致（文件本来就是同一份代码导出的）。文件缺失只会
    发生在本地从源码起服务或跑测试时，部署产物里构建期一定写好了；现算要拉起
    整个 FastAPI app，几百毫秒，且结果由 command_domains 缓存，只算一次。
    """
    try:
        text = _SPEC_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.info("基线 spec 文件不存在（%s），改为从当前代码现算", _SPEC_PATH)
        from movieclaw_api.export_openapi import build_spec

        return build_spec()
    except OSError as exc:
        raise SpecCatalogUnavailable(f"文件不可读（{exc}）") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecCatalogUnavailable(f"内容不是合法 JSON（{exc}）") from exc


def iter_command_operations(spec: dict[str, Any]) -> list[dict[str, str]]:
    """展平出**会成为 CLI 命令**的操作。

    判定与 CLI 生成器同口径（docs/design/cli.md §3）：有 operation_id、
    未标 ``x-cli-hidden``（纯 Web 基础设施）、未标 ``x-cli-stream``
    （SSE 流由精选层手写接入）。两边一致性由守护测试保证。
    """
    ops: list[dict[str, str]] = []
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            operation_id = op.get("operationId") or ""
            if not operation_id or op.get("x-cli-hidden") or op.get("x-cli-stream"):
                continue
            ops.append({"operation_id": operation_id, "method": method, "path": path})
    return ops


@lru_cache(maxsize=1)
def command_domains() -> frozenset[str]:
    """全部会生成命令的域（operation_id 的第一段）。

    进程内缓存：spec 是构建期产物，运行期不会变。
    """
    return frozenset(
        op["operation_id"].split(".")[0] for op in iter_command_operations(load_spec())
    )
