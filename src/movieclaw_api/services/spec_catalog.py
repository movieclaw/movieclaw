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
from functools import lru_cache
from pathlib import Path
from typing import Any

from movieclaw_api.exceptions import AppException

#: spec 基线文件。构建期由 `movieclaw_api.export_openapi` 现场导出（Dockerfile
#: 与发版脚本都会做），因此运行期一定存在且与代码严格同版。
_SPEC_PATH = Path(__file__).resolve().parents[1] / "data" / "spec.json"

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


class SpecCatalogUnavailable(AppException):
    """基线 spec 读不到。

    只可能是部署产物不完整（基线文件没进镜像或安装包）。这类故障必须给出可读
    结论——裸的 "internal server error" 会让自部署用户完全无从判断该重新部署
    还是该改配置。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=500,
            code="SPEC_BASELINE_MISSING",
            message=(
                f"服务端缺少 CLI 基线 spec（{_SPEC_PATH}）：{detail}。"
                "Agent 功能不可用。这通常是镜像或安装包不完整，请更新到新版镜像后重新部署。"
            ),
        )


def load_spec() -> dict[str, Any]:
    """读取基线 spec。"""
    try:
        return json.loads(_SPEC_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SpecCatalogUnavailable("文件不存在或不可读") from exc
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
