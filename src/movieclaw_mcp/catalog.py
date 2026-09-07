"""spec → MCP 工具目录的索引层（docs/design/mcp-server.md §4.1 / §4.2）。

这一层只做一件事：把 OpenAPI spec 里「会成为 CLI 命令的操作」整理成 MCP 需要的
形态——工具名、参数分箱（path / query / body）、注解依据、以及调用时怎么拼请求。
工具渲染（tools.py）与调用调度（dispatch.py）都从这里取元数据，**判定口径只有
这一份**，不会出现「列出来的工具」和「能调的工具」不一致。

与 CLI 的关系：入选口径与 ``services.spec_catalog.iter_command_operations`` 完全
一致（有 operationId、非 x-cli-hidden、非 x-cli-stream），因此「CLI 有的命令」
与「MCP 有的工具」天然同集合，不需要各自维护清单。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from movieclaw_api.services.spec_catalog import load_spec

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})

#: 参数落点：路径段 / 查询串 / 请求体。dispatch 按它把模型给的扁平参数拼回请求。
ArgLocation = Literal["path", "query", "body"]

#: 不进工具面的域：
#: - mcp 自身——端点不该能增删 MCP 端点（与「签发凭证必须人在浏览器里」同义）。
_EXCLUDED_DOMAINS = frozenset({"mcp"})

#: 不进工具面的单个操作：会话的发起与重跑。理由与 mclaw 工具里的那条硬闸相同——
#: 防止接进来的 Agent 递归拉起产品内的 Agent。其余 session.* 是只读/管理动作，保留。
_EXCLUDED_OPERATIONS = frozenset({"session.start", "session.retry"})

#: 工具名的合法形态。部分客户端对函数名有 ``^[a-zA-Z0-9_-]{1,64}$`` 之类的限制，
#: 我们取更严的一档（只允许下划线），避免出现「某个客户端悄悄丢掉一个工具」。
TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{1,64}$")


def tool_name_of(operation_id: str) -> str:
    """``operation_id`` → MCP 工具名：点号与连字符统一成下划线。

    不加 ``mclaw_`` 前缀——客户端本来就按服务器名分组展示与调用，再加一层前缀
    只是让每个工具白占一截 token（docs/design/mcp-server.md §4.1）。
    """
    return operation_id.replace(".", "_").replace("-", "_")


@dataclass(frozen=True)
class Operation:
    """一个可被 MCP 调用的操作。"""

    operation_id: str
    tool_name: str
    domain: str
    #: 去掉域之后的命令名（折叠模式下 command 枚举用的就是它），如 ``list-active-downloads``
    command: str
    method: str
    path: str
    summary: str
    description: str
    #: 参数名 → 落点。顺序即 schema 里的属性顺序（path → query → body）。
    arg_locations: dict[str, ArgLocation]
    #: 参数名 → JSON Schema（已解析 $ref，引用统一改写到 ``$defs``）
    arg_schemas: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    #: inputSchema 需要携带的 ``$defs``（请求体里嵌套模型的传递闭包）
    defs: dict[str, Any] = field(default_factory=dict)
    #: ``x-cli-dangerous``：confirm | destructive | None
    dangerous: str | None = None
    #: 是否是「提交后台任务」的操作（返回 job_id，模型应改调 jobs_wait 跟进）
    is_job: bool = False

    @property
    def read_only(self) -> bool:
        return self.method == "get"


def _resolve_refs(schema: Any, spec: dict[str, Any], defs: dict[str, Any]) -> Any:
    """把 ``#/components/schemas/X`` 改写成 ``#/$defs/X``，并收集传递闭包。

    刻意**不做内联展开**：请求体里有自引用模型（如树形节点），内联会无限膨胀；
    ``$defs`` + ``$ref`` 是 JSON Schema 2020-12 的标准形态，MCP 也明确允许。
    """
    if isinstance(schema, list):
        return [_resolve_refs(item, spec, defs) for item in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        if name not in defs:
            defs[name] = {}  # 先占位，阻断自引用导致的无限递归
            target = (spec.get("components", {}).get("schemas", {}) or {}).get(name, {})
            defs[name] = _resolve_refs(target, spec, defs)
        return {**{k: v for k, v in schema.items() if k != "$ref"}, "$ref": f"#/$defs/{name}"}
    return {key: _resolve_refs(value, spec, defs) for key, value in schema.items()}


def _describe(op: dict[str, Any]) -> tuple[str, str]:
    """取操作的一行摘要与补充说明。

    补充说明来自路由 docstring（FastAPI 会写进 description），里面往往有「什么时候
    该用它、有什么后果」这类模型真正需要的信息，截断后带上；正文太长的只留开头。
    """
    summary = (op.get("summary") or "").strip()
    detail = (op.get("description") or "").strip()
    if detail:
        detail = " ".join(detail.split())
        if len(detail) > 360:
            detail = detail[:360] + "…"
    return summary, detail


def _build_operation(
    spec: dict[str, Any], path: str, method: str, op: dict[str, Any]
) -> Operation | None:
    operation_id: str = op.get("operationId") or ""
    domain = operation_id.split(".")[0]
    if domain in _EXCLUDED_DOMAINS or operation_id in _EXCLUDED_OPERATIONS:
        return None

    body_content = (op.get("requestBody") or {}).get("content") or {}
    # 上传类接口不进工具面：MCP 客户端没有「服务端文件系统」的概念，给出去只会
    # 让模型反复失败（docs/design/mcp-server.md §4.2）。
    if any(media.startswith("multipart/") for media in body_content):
        return None

    locations: dict[str, ArgLocation] = {}
    schemas: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    defs: dict[str, Any] = {}

    for param in op.get("parameters") or []:
        where = param.get("in")
        # header / cookie 参数是鉴权管线的事（Authorization、会话 Cookie），
        # 由调度层自己注入，绝不能出现在给模型看的参数面里。
        if where not in ("path", "query"):
            continue
        name = param["name"]
        schema = _resolve_refs(dict(param.get("schema") or {}), spec, defs)
        if param.get("description"):
            schema.setdefault("description", param["description"])
        locations[name] = "path" if where == "path" else "query"
        schemas[name] = schema
        if param.get("required"):
            required.append(name)

    json_schema = (body_content.get("application/json") or {}).get("schema")
    if json_schema:
        resolved = _resolve_refs(dict(json_schema), spec, defs)
        # 请求体是个具名模型时，把它的属性摊平成工具参数——模型面对
        # ``{"follow_future": false}`` 比面对 ``{"payload": {...}}`` 稳得多。
        target = defs.get(resolved["$ref"].rsplit("/", 1)[-1]) if "$ref" in resolved else resolved
        properties = (target or {}).get("properties") or {}
        body_required = set((target or {}).get("required") or [])
        for name, prop in properties.items():
            if name in locations:  # 与 path/query 撞名：外层优先，body 侧让位
                continue
            locations[name] = "body"
            schemas[name] = prop
            if name in body_required:
                required.append(name)
        if not properties:
            # 非对象请求体（数组等）：整体作为一个 body 参数交出去
            locations["body"] = "body"
            schemas["body"] = resolved
            required.append("body")

    summary, detail = _describe(op)
    return Operation(
        operation_id=operation_id,
        tool_name=tool_name_of(operation_id),
        domain=domain,
        command=operation_id.split(".", 1)[1] if "." in operation_id else operation_id,
        method=method,
        path=path,
        summary=summary,
        description=detail,
        arg_locations=locations,
        arg_schemas=schemas,
        required=tuple(required),
        defs=defs,
        dangerous=op.get("x-cli-dangerous"),
        is_job=bool(op.get("x-cli-job")),
    )


@lru_cache(maxsize=1)
def operation_index() -> dict[str, Operation]:
    """``operation_id`` → 操作元数据。进程内缓存：spec 是构建期产物，运行期不变。"""
    spec = load_spec()
    index: dict[str, Operation] = {}
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            operation_id = op.get("operationId") or ""
            if not operation_id or op.get("x-cli-hidden") or op.get("x-cli-stream"):
                continue
            built = _build_operation(spec, path, method, op)
            if built is not None:
                index[operation_id] = built
    return index


@lru_cache(maxsize=1)
def tools_by_name() -> dict[str, Operation]:
    """工具名 → 操作。展开模式下 ``tools/call`` 用它一步反查。"""
    return {op.tool_name: op for op in operation_index().values()}


@lru_cache(maxsize=1)
def operations_by_domain() -> dict[str, tuple[Operation, ...]]:
    """服务域 → 该域的操作（按 operation_id 字典序，输出顺序确定）。"""
    grouped: dict[str, list[Operation]] = {}
    for op in operation_index().values():
        grouped.setdefault(op.domain, []).append(op)
    return {
        domain: tuple(sorted(ops, key=lambda o: o.operation_id))
        for domain, ops in sorted(grouped.items())
    }


def available_services() -> tuple[str, ...]:
    """可供端点勾选的服务域（字典序）。"""
    return tuple(operations_by_domain())
