"""从后端路由与 Pydantic 模型生成 iOS 端的 Swift 数据类型与接口函数。

为什么不用 OpenAPI：当前 FastAPI 导出的 OpenAPI 里字段类型丢失（只剩 title），
没法据此生成类型；而路由对象上的 response_model / 参数注解是完整的，
所以直接在进程内读 FastAPI 路由表 + Pydantic json schema 来生成。

产物（覆盖写入，勿手改）：
  MovieClaw/Core/API/Generated/Models.swift     全部请求/响应模型（命名空间 `API`）
  MovieClaw/Core/API/Generated/Endpoints.swift  每个业务接口一个 async 函数（APIClient 扩展）

用法（在仓库根目录，用后端的虚拟环境跑）：
  .venv/bin/python apps/apple/scripts/gen_api.py

生成规则要点：
- 响应模型按「序列化」口径出 schema，且所有字段视为必有（后端 ApiResponse 不做
  exclude_none/exclude_unset，带默认值的字段也总会输出），只有可为 null 的才是可选；
- 请求体按「校验」口径：带默认值的字段为可选，传 nil 时不编码，由后端取默认值；
- 统一信封 ApiResponse[T] 在生成函数里拆掉，直接返回 T；
- 每个结构体显式写 CodingKeys（snake_case ↔ camelCase），因此 APIClient 不使用
  keyDecodingStrategy —— 那个策略会连字典的键一起改写；
- 无法静态表达的类型（多类型联合、元组、任意对象）一律落到 `API.JSONValue`。
"""

from __future__ import annotations

import keyword
import re
import sys
import types
import typing
from pathlib import Path

from fastapi import params as fastapi_params
from fastapi.routing import APIRoute
from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "apps/apple/MovieClaw/Core/API/Generated"
API_PREFIX = "/api/v1"

SWIFT_KEYWORDS = {
    "associatedtype", "class", "deinit", "enum", "extension", "fileprivate", "func", "import", "init",
    "inout", "internal", "let", "open", "operator", "private", "protocol", "public", "rethrows",
    "static", "struct", "subscript", "typealias", "var", "break", "case", "continue", "default",
    "defer", "do", "else", "fallthrough", "for", "guard", "if", "in", "repeat", "return", "switch",
    "where", "while", "as", "catch", "false", "is", "nil", "super", "self", "Self", "throw",
    "throws", "true", "try", "Type", "Any", "some", "any",
}


class ResponseSchema(GenerateJsonSchema):
    """响应口径：后端输出时带默认值的字段也总会出现，全部标记为 required。

    注意用的是「校验」模式出 schema：本项目的模型在序列化模式下类型信息会丢失
    （FastAPI 导出的 OpenAPI 同样因此缺类型），校验模式的类型是完整的。
    """

    def field_is_required(self, field, total):  # noqa: D401
        return True


# ---------------------------------------------------------------------------
# 命名
# ---------------------------------------------------------------------------


def type_name(raw: str) -> str:
    """pydantic 的 def 名（可能含 [ ] - 等）→ 合法的 Swift 类型名。"""
    name = raw.replace("-Input", "Input").replace("-Output", "Output")
    name = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_")
    parts = [p for p in name.split("_") if p]
    name = "".join(p[0].upper() + p[1:] for p in parts)
    if name[0].isdigit():
        name = "T" + name
    return name


def camel(snake: str) -> str:
    parts = [p for p in re.split(r"[_\-\s.]+", snake) if p]
    if not parts:
        return "_"
    head = parts[0]
    # 全大写缩写保持原样的首段小写化：URL → url
    head = head if not head.isupper() else head.lower()
    head = head[0].lower() + head[1:]
    name = head + "".join(p[0].upper() + p[1:] for p in parts[1:])
    if name[0].isdigit():
        name = "_" + name
    return name


def ident(name: str) -> str:
    return f"`{name}`" if name in SWIFT_KEYWORDS else name


def op_func_name(route: APIRoute) -> str:
    op_id = route.operation_id or route.unique_id
    return ident(camel(op_id.replace(".", "_")))


# ---------------------------------------------------------------------------
# JSON schema → Swift 类型
# ---------------------------------------------------------------------------


def ref_name(ref: str) -> str:
    return type_name(ref.split("/")[-1])


def swift_type(schema: dict, defs: dict) -> tuple[str, bool]:
    """返回 (Swift 类型, 是否可为 null)。"""
    if not schema:
        return "API.JSONValue", True
    if "$ref" in schema:
        return "API." + ref_name(schema["$ref"]), False
    for key in ("anyOf", "oneOf"):
        if key in schema:
            variants = schema[key]
            non_null = [v for v in variants if v.get("type") != "null"]
            nullable = len(non_null) != len(variants)
            if len(non_null) == 1:
                inner, inner_nullable = swift_type(non_null[0], defs)
                return inner, nullable or inner_nullable
            # 多个都是字符串字面量（Literal["a","b"]）→ String
            if all(v.get("type") == "string" for v in non_null):
                return "String", nullable
            # 整数与浮点并存 → Double
            if {v.get("type") for v in non_null} <= {"integer", "number"}:
                return "Double", nullable
            return "API.JSONValue", True
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return swift_type(schema["allOf"][0], defs)
    if "const" in schema:
        value = schema["const"]
        return {bool: "Bool", int: "Int", float: "Double"}.get(type(value), "String"), False
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        nullable = len(non_null) != len(t)
        if len(non_null) == 1:
            inner, _ = swift_type({**schema, "type": non_null[0]}, defs)
            return inner, nullable
        return "API.JSONValue", True
    if t == "string":
        return "String", False
    if t == "integer":
        return "Int", False
    if t == "number":
        return "Double", False
    if t == "boolean":
        return "Bool", False
    if t == "null":
        return "API.JSONValue", True
    if t == "array":
        if "prefixItems" in schema:
            return "[API.JSONValue]", False
        inner, inner_nullable = swift_type(schema.get("items", {}), defs)
        return f"[{inner}{'?' if inner_nullable and inner != 'API.JSONValue' else ''}]", False
    if t == "object" or "additionalProperties" in schema:
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            inner, inner_nullable = swift_type(extra, defs)
            return f"[String: {inner}{'?' if inner_nullable and inner != 'API.JSONValue' else ''}]", False
        if "properties" not in schema:
            return "[String: API.JSONValue]", False
    if "enum" in schema:
        values = schema["enum"]
        if all(isinstance(v, str) for v in values):
            return "String", False
        if all(isinstance(v, int) for v in values):
            return "Int", False
    return "API.JSONValue", True


def doc_lines(text: str | None, indent: str) -> list[str]:
    if not text:
        return []
    return [f"{indent}/// {line.strip()}" for line in text.strip().splitlines() if line.strip()]


def gen_struct(name: str, schema: dict, defs: dict) -> list[str]:
    lines: list[str] = []
    lines += doc_lines(schema.get("description"), "    ")
    if "enum" in schema:
        # 独立的枚举 schema：用可扩展的字符串包装，未来新增的枚举值也能解码
        lines.append(f"    typealias {name} = String")
        values = ", ".join(repr(v) for v in schema["enum"])
        lines.append(f"    // 取值：{values}")
        return lines
    props = schema.get("properties") or {}
    if not props:
        lines.append(f"    typealias {name} = [String: API.JSONValue]")
        return lines
    required = set(schema.get("required") or [])
    lines.append(f"    struct {name}: Codable, Hashable, Sendable {{")
    keys: list[tuple[str, str]] = []
    used: set[str] = set()
    for json_key, prop in props.items():
        swift_name = camel(json_key)
        while swift_name in used:
            swift_name += "_"
        used.add(swift_name)
        typ, nullable = swift_type(prop, defs)
        optional = nullable or json_key not in required
        lines += doc_lines(prop.get("description"), "        ")
        if prop.get("readOnly") and not optional:
            # 计算字段由服务端推导：解码时照常必有，本地构造时不必填（数组给空、其余可选）
            if typ.startswith("["):
                lines.append(f"        var {ident(swift_name)}: {typ} = []")
            else:
                lines.append(f"        var {ident(swift_name)}: {typ}?")
        else:
            lines.append(f"        var {ident(swift_name)}: {typ}{'?' if optional else ''}")
        keys.append((swift_name, json_key))
    lines.append("")
    lines.append("        enum CodingKeys: String, CodingKey {")
    for swift_name, json_key in keys:
        if swift_name == json_key:
            lines.append(f"            case {ident(swift_name)}")
        else:
            lines.append(f'            case {ident(swift_name)} = "{json_key}"')
    lines.append("        }")
    lines.append("    }")
    return lines


# ---------------------------------------------------------------------------
# 路由收集
# ---------------------------------------------------------------------------


class RouteInfo:
    """FastAPI 0.14x 起 include_router 变成惰性挂载：完整路径、响应模型等在「生效上下文」上，
    依赖树与请求体仍在原始 APIRoute 上。这里把两者合成一个视图。"""

    def __init__(self, original: APIRoute, ctx, path: str):
        self.original = original
        self.path = path
        self.methods = original.methods
        self.response_model = getattr(ctx, "response_model", None) if ctx is not None else original.response_model
        if self.response_model is None:
            self.response_model = original.response_model
        self.summary = (getattr(ctx, "summary", None) if ctx is not None else None) or original.summary
        self.name = original.name
        self.operation_id = (getattr(ctx, "operation_id", None) if ctx is not None else None) or original.operation_id
        self.unique_id = f"{sorted(original.methods)[0]}_{path}"
        self.dependant = original.dependant
        self.body_field = original.body_field


def iter_routes(routes):
    from fastapi.routing import _iter_routes_with_context

    yield from _iter_routes_with_context(routes)


def unwrap_envelope(model):
    """ApiResponse[T] → (T, True)；其余原样返回 (model, False)。"""
    from movieclaw_api.schemas.response import ApiResponse

    if isinstance(model, type) and issubclass(model, BaseModel):
        meta = getattr(model, "__pydantic_generic_metadata__", None) or {}
        if meta.get("origin") is ApiResponse and meta.get("args"):
            return meta["args"][0], True
    return model, False


def collect(dependant, attr: str) -> list:
    """递归收集依赖树里的 path/query 参数（按别名去重）。"""
    out, seen = [], set()

    def walk(dep):
        for f in getattr(dep, attr):
            if f.alias not in seen:
                seen.add(f.alias)
                out.append(f)
        for sub in dep.dependencies:
            walk(sub)

    walk(dependant)
    return out


def is_none_type(tp) -> bool:
    return tp is None or tp is type(None)


def main() -> int:
    from movieclaw_api.app import create_app

    app = create_app()
    routes = []
    for original, ctx in iter_routes(app.routes):
        if not isinstance(original, APIRoute):
            continue
        path = ctx.path if ctx is not None else original.path
        include = ctx.include_in_schema if ctx is not None else original.include_in_schema
        if not include or not path.startswith(API_PREFIX):
            continue
        routes.append(RouteInfo(original, ctx, path))
    routes.sort(key=lambda r: (r.path, sorted(r.methods)))

    inputs = []  # (key, mode, adapter)
    plans = []
    skipped = []
    for route in routes:
        method = sorted(m for m in route.methods if m != "HEAD")[0]
        rm = route.response_model
        if rm is None:
            skipped.append(f"{method} {route.path}（无响应模型：文件流/SSE 等，需手写）")
            continue
        inner, enveloped = unwrap_envelope(rm)
        resp_key = None
        if not is_none_type(inner):
            resp_key = ("resp", route.unique_id)
            inputs.append((resp_key, "serialization", TypeAdapter(inner)))

        body = None
        if route.body_field is not None:
            info = route.body_field.field_info
            if isinstance(info, (fastapi_params.Form, fastapi_params.File)):
                skipped.append(f"{method} {route.path}（multipart 表单上传，需手写）")
                continue
            body = ("body", route.unique_id)
            inputs.append((body, "validation", TypeAdapter(info.annotation)))

        params = []
        for kind, fields in (("path", collect(route.dependant, "path_params")), ("query", collect(route.dependant, "query_params"))):
            for f in fields:
                key = ("param", route.unique_id, kind, f.alias)
                inputs.append((key, "validation", TypeAdapter(f.field_info.annotation)))
                params.append((kind, f, key))
        plans.append((route, method, enveloped, resp_key, body, params))

    resp_inputs = [(k, "validation", a) for k, _, a in inputs if k[0] == "resp"]
    req_inputs = [(k, "validation", a) for k, _, a in inputs if k[0] != "resp"]
    resp_map, resp_top = TypeAdapter.json_schemas(
        resp_inputs, ref_template="#/$defs/{model}", schema_generator=ResponseSchema
    )
    req_map, req_top = TypeAdapter.json_schemas(req_inputs, ref_template="#/$defs/{model}")
    defs = dict(resp_top.get("$defs", {}))
    # @computed_field 只出现在序列化口径里，而部分父模型自定义了序列化器、序列化口径下整棵 schema
    # 没有类型，按 $defs 合并会漏掉。这里直接遍历所有 Pydantic 模型类，把计算字段补进同名定义（视为必有）。
    def all_models(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from all_models(sub)

    for cls in set(all_models(BaseModel)):
        computed = getattr(cls, "model_computed_fields", None) or {}
        target = defs.get(cls.__name__)
        if not computed or not target or "properties" not in target:
            continue
        ser_props = cls.model_json_schema(mode="serialization").get("properties", {})
        for prop in computed:
            if prop in ser_props and prop not in target["properties"]:
                target["properties"][prop] = ser_props[prop]
                target.setdefault("required", []).append(prop)
    req_defs = req_top.get("$defs", {})
    # 同名模型两种口径不一致（请求体里带默认值的字段是可选的）→ 请求侧改名 XxxInput
    renames = {
        name: name + "Input"
        for name, schema in req_defs.items()
        if name in defs and defs[name] != schema
    }

    def rewrite(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "$ref" and isinstance(v, str):
                    target = v.split("/")[-1]
                    out[k] = "#/$defs/" + renames.get(target, target)
                else:
                    out[k] = rewrite(v)
            return out
        if isinstance(node, list):
            return [rewrite(x) for x in node]
        return node

    for name, schema in req_defs.items():
        defs.setdefault(renames.get(name, name), rewrite(schema))
    # json_schemas 返回的键是 (我们的 key, mode)，这里摊平成只按 key 查
    key_map = {k[0]: v for k, v in resp_map.items()}
    key_map.update({k[0]: rewrite(v) for k, v in req_map.items()})

    # ---- Models.swift ----
    model_lines = [
        "// 由 apps/apple/scripts/gen_api.py 生成，勿手改。重新生成见脚本头部说明。",
        "import Foundation",
        "",
        "nonisolated extension API {",
    ]
    for raw_name in sorted(defs):
        model_lines += gen_struct(type_name(raw_name), defs[raw_name], defs)
        model_lines.append("")
    model_lines.append("}")

    # ---- Endpoints.swift ----
    ep = [
        "// 由 apps/apple/scripts/gen_api.py 生成，勿手改。重新生成见脚本头部说明。",
        "import Foundation",
        "",
        "nonisolated extension APIClient {",
    ]
    seen_names: set[str] = set()
    for route, method, enveloped, resp_key, body, params in plans:
        name = op_func_name(route)
        while name in seen_names:
            name += "_"
        seen_names.add(name)
        sig = []
        path_expr = route.path[len(API_PREFIX):]
        query_lines = []
        for kind, field, key in params:
            typ, nullable = swift_type(key_map[key], defs)
            pname = ident(camel(field.alias))
            if kind == "path":
                sig.append(f"{pname}: {typ}")
                path_expr = path_expr.replace("{" + field.name + "}", f"\\({pname})").replace(
                    "{" + field.alias + "}", f"\\({pname})"
                )
            else:
                optional = nullable or not field.field_info.is_required()
                sig.append(f"{pname}: {typ}{'? = nil' if optional else ''}")
                query_lines.append((field.alias, pname, typ, optional))
        if body is not None:
            btyp, bnull = swift_type(key_map[body], defs)
            optional_body = bnull or not route.body_field.field_info.is_required()
            sig.append(f"body: {btyp}{'? = nil' if optional_body else ''}")
        if resp_key is None:
            ret = "Void"
        else:
            rtyp, rnull = swift_type(key_map[resp_key], defs)
            ret = rtyp + ("?" if rnull and rtyp != "API.JSONValue" else "")
        doc = route.summary or route.name
        ep += doc_lines(doc, "    ")
        ep.append(f"    /// `{method} {route.path[len(API_PREFIX):]}`")
        ep.append(f"    func {name}({', '.join(sig)}) async throws -> {ret} {{")
        if query_lines:
            ep.append("        var query: [URLQueryItem] = []")
            for alias, pname, typ, optional in query_lines:
                if typ.startswith("["):
                    src = f"{pname} ?? []" if optional else pname
                    ep.append(f'        for value in {src} {{ query.append(URLQueryItem(name: "{alias}", value: "\\(value)")) }}')
                elif optional:
                    ep.append(f'        if let {pname} {{ query.append(URLQueryItem(name: "{alias}", value: "\\({pname})")) }}')
                else:
                    ep.append(f'        query.append(URLQueryItem(name: "{alias}", value: "\\({pname})"))')
        q = ", query: query" if query_lines else ""
        b = ", body: body" if body is not None else ""
        if ret == "Void":
            fn = "send" if enveloped else "raw"
            ep.append(f'        let _: API.JSONValue? = try await {fn}("{method}", "{path_expr}"{q}{b})')
        else:
            fn = "send" if enveloped else "raw"
            ep.append(f'        return try await {fn}("{method}", "{path_expr}"{q}{b})')
        ep.append("    }")
        ep.append("")
    ep.append("}")
    ep.append("")
    ep.append("// 未生成的接口（需在对应模块手写）：")
    ep += [f"// - {s}" for s in skipped]

    # ---- LiveDecodeTests.swift：对真实服务器逐个调用无必填参数的 GET，验证模型能解码 ----
    tests = [
        "// 由 apps/apple/scripts/gen_api.py 生成，勿手改。",
        "// 需要一台运行中的 MovieClaw；设置环境变量 MC_LIVE=1 才会执行",
        "// （xcodebuild 传 TEST_RUNNER_MC_LIVE=1），地址/账号见 LiveServer。",
        "import Testing",
        "@testable import MovieClaw",
        "",
        '@Suite("生成模型对真实服务器解码", .enabled(if: LiveServer.enabled), .serialized)',
        "struct LiveDecodeTests {",
    ]
    for route, method, enveloped, resp_key, body, params in plans:
        if method != "GET" or resp_key is None or body is not None:
            continue
        if any(kind == "path" for kind, _, _ in params):
            continue
        if any(field.field_info.is_required() for kind, field, _ in params):
            continue
        name = op_func_name(route).strip("`")
        tests.append(f"    @Test func {name}() async throws {{")
        tests.append(f"        try await LiveServer.check {{ try await $0.{op_func_name(route)}() }}")
        tests.append("    }")
    tests.append("}")
    test_dir = ROOT / "apps/apple/MovieClawTests/Generated"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "LiveDecodeTests.swift").write_text("\n".join(tests) + "\n", encoding="utf-8")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "Models.swift").write_text("\n".join(model_lines) + "\n", encoding="utf-8")
    (OUT_DIR / "Endpoints.swift").write_text("\n".join(ep) + "\n", encoding="utf-8")
    print(f"模型 {len(defs)} 个，接口 {len(plans)} 个，跳过 {len(skipped)} 个 → {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
