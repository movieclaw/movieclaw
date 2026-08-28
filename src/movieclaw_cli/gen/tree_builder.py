"""spec → click 命令树（docs/design/cli.md §3.2 参数映射规则）。

命令名完全由 operation_id 决定：`subscriptions.list` → `mclaw subscriptions list`，
`library.items.get` → `mclaw library items get`。help 来自 summary/description，
参数来自 parameters / requestBody——spec 写好，命令即成，CLI 侧零手工维护。

P1 全量映射（除 x-cli-hidden 与 x-cli-stream 外全部生成）：
- path 参数 → 位置参数；query 参数 → --标志；
- requestBody 顶层字段拍平成 --标志，嵌套对象/数组收折为 --<字段>-json，
  整体替代形态 --input <文件|->（三选一）；
- multipart 上传 → --file；FileResponse 下载 → --output-file；
- x-cli-dangerous 驱动 --yes 确认门槛；x-cli-long-task 驱动 --wait 轮询。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import click

from movieclaw_cli.core.errors import CliError, ExitCode
from movieclaw_cli.core.http import Api
from movieclaw_cli.core.output import emit

# 允许写在子命令尾部的全局标志（kubectl 惯例：`mclaw subscriptions list -o json`）。
# 根组上的同名标志依然有效，叶子上的取值优先。
_OVERRIDE_VALUE_OPTS = ("output", "server", "timeout")
_OVERRIDE_FLAG_OPTS = ("quiet", "debug", "yes")
RESERVED_PARAM_NAMES = (
    set(_OVERRIDE_VALUE_OPTS)
    | set(_OVERRIDE_FLAG_OPTS)
    | {"file", "output_file", "wait", "wait_timeout"}
)
# "input" 不在保留清单：API 字段叫 input 时（如 session.start 的用户输入），
# API 字段优先，该命令放弃 --input 整体替代形态（逐字段传参仍完整可用）

# 域分组的一行产品能力简介（click group 的 help；生成命令自身的参数与详细
# 说明来自 spec）。模型会先看 mclaw 工具目录、再用域级 --help 探索命令，
# 因此这里要用用户语义区分相邻域，不能只重复内部对象名。
DOMAIN_HELP = {
    "app": "外部访问设置、应用重启、版本升级/回退与 NER 模型更新",
    "appearance": "首页背景与图库",
    "auth": "个人信息、会话与 API 令牌",
    "channels": "微信、Telegram、Discord 消息推送与 AI 对话入口",
    "discover": "浏览 TMDB/豆瓣电影与剧集片单，并读取影视条目完整资料",
    "dl": "qBittorrent/Transmission 下载器、路径映射与种子投递",
    "extension": "浏览器插件 Cookie 同步",
    "health": "API 存活检查",
    "jobs": "后台作业查询、事件、等待、取消与重试",
    "library": "管理本地电影/剧集媒体库、库存文件、识别结果、元数据、图片与字幕；"
    "organize-files 按 scrape 配的命名模板批量整理存量文件名",
    "llm": "OpenAI、阿里云百炼及 OpenAI 兼容模型接入与验证",
    "logs": "系统日志查看与实时跟随",
    "members": "家庭成员账号、能力开关与可见范围",
    "net": "全局/指定服务代理、镜像地址与连通性测试",
    "notices": "系统待处理事项",
    "people": "本地媒体库影人档案与作品",
    "rules": "订阅资源质量与过滤规则组",
    "scrape": "刮削与整理配置：元数据语言优先级、选图口味与图片档位、"
    "目录与文件名模板、媒体目录写入开关",
    "search": "统一搜索影视条目、PT 种子和本地媒体库，并管理搜索预设与历史",
    "session": "AI 会话开始/继续、指定消息重试、SSE 跟随、完整轨迹与上下文管理",
    "site": "PT 资源站点接入、鉴权验证与缓存状态",
    "subscriptions": "持续追踪电影/剧集缺失资源并自动搜索、下载和整理入库",
    "transcode": "远程硬件转码 Worker 配置与状态",
    "ui": "Web 界面质感、布局与显示偏好",
    "watch": "下载完成目录监听、自动识别、标准命名与入库",
    "webhook": "播放、收藏等事件的 Webhook 推送与投递记录",
}

# 二级分组同样是模型的探索入口，必须说明“这里解决什么问题”。只给一级域
# 描述会让 items / identification / metadata 等孤立名词失去选择依据。
COMMAND_GROUP_HELP = {
    "library.artwork": "查看、下载和选定媒体条目的海报或背景图",
    "library.identification": "处理待识别、错识别和已忽略文件，明确指定文件所属影视条目",
    "library.items": "查看和管理已经入库的电影、剧集条目及其物理文件",
    "library.metadata": "刷新整个媒体库的 TMDB 元数据并查看或停止刷新任务",
    "library.missing": "查看磁盘上已经缺失的库存记录、重新下载或清理台账",
    "library.scan": "扫描媒体库根路径，把存量文件识别并登记到库存台账",
    "library.subtitles": "预检和生成 AI 字幕，或校准外挂字幕时间轴",
    "search.history": "列出、回放、删除或清空影视条目与 PT 种子搜索历史",
    "search.presets": "列出或更新 PT 种子搜索的分类与站点组合预设",
}

_SIMPLE_TYPES = {"string", "integer", "number", "boolean"}


def _global_override_options() -> list[click.Option]:
    return [
        click.Option(
            ["-o", "--output"],
            type=click.Choice(["table", "json", "yaml"]),
            default=None,
            help="输出格式（覆盖全局设置）",
        ),
        click.Option(["--server"], default=None, help="服务器地址（覆盖全局设置）"),
        click.Option(["--timeout"], type=float, default=None, help="请求超时秒数（覆盖全局设置）"),
        click.Option(["--quiet"], is_flag=True, default=False, help="成功时不输出数据"),
        click.Option(["--debug"], is_flag=True, default=False, help="打印调试信息到 stderr"),
        click.Option(["--yes"], is_flag=True, default=False, help="跳过确认提示"),
    ]


def merge_settings(kwargs: dict[str, Any]) -> Any:
    """从命令参数中取出全局覆盖标志，合并进当前 Settings（叶子优先）。"""
    ctx = click.get_current_context()
    settings = ctx.obj
    overrides: dict[str, Any] = {}
    for key in _OVERRIDE_VALUE_OPTS:
        value = kwargs.pop(key, None)
        if value is not None:
            overrides[key] = value
    for key in _OVERRIDE_FLAG_OPTS:
        if kwargs.pop(key, False):
            overrides[key] = True
    return replace(settings, **overrides) if overrides else settings


def _resolve_schema(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    """展开 $ref 与 anyOf（可空类型取非 null 分支）。"""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = components.get(name, {})
    if "anyOf" in schema:
        for candidate in schema["anyOf"]:
            resolved = _resolve_schema(candidate, components)
            if resolved.get("type") != "null":
                return resolved
    return schema


def _click_type(schema: dict[str, Any]) -> Any:
    if enum := schema.get("enum"):
        return click.Choice([str(v) for v in enum])
    return {
        "integer": click.INT,
        "number": click.FLOAT,
        "boolean": click.BOOL,
    }.get(schema.get("type", "string"), click.STRING)


def _body_fields(body_schema: dict[str, Any], components: dict[str, Any]) -> list[dict[str, Any]]:
    """把 requestBody 的 JSON schema 顶层拍平为字段清单。"""
    schema = _resolve_schema(body_schema, components)
    required = set(schema.get("required") or [])
    fields: list[dict[str, Any]] = []
    for name, prop in (schema.get("properties") or {}).items():
        resolved = _resolve_schema(prop, components)
        kind = "simple" if resolved.get("type") in _SIMPLE_TYPES else "json"
        fields.append(
            {
                "name": name,
                "kind": kind,
                "schema": resolved,
                "required": name in required,
                "description": prop.get("description") or resolved.get("description"),
                # 无默认值的可选简单字段置 None（不发送）；有默认值交给服务端补
            }
        )
    return fields


def iter_operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """展平 spec 中的全部操作，附带解析后的参数、body、x-cli 元数据。"""
    components = spec.get("components", {}).get("schemas", {})
    ops: list[dict[str, Any]] = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            params = []
            for param in op.get("parameters", []):
                if param.get("in") not in {"path", "query"}:
                    continue  # 会话 Cookie 等鉴权参数由 http 层注入，不进命令
                schema = _resolve_schema(param.get("schema", {}), components)
                params.append(
                    {
                        "name": param["name"],
                        "in": param["in"],
                        "required": bool(param.get("required")),
                        "schema": schema,
                        "description": param.get("description") or schema.get("description"),
                    }
                )
            content = (op.get("requestBody") or {}).get("content") or {}
            json_body = content.get("application/json", {}).get("schema")
            multipart = content.get("multipart/form-data", {}).get("schema")
            resp_200 = op.get("responses", {}).get("200", {})
            resp_content = resp_200.get("content")
            json_ref = ""
            if resp_content:
                json_ref = (
                    resp_content.get("application/json", {}).get("schema", {}).get("$ref", "")
                )
            ops.append(
                {
                    "operation_id": op.get("operationId", ""),
                    "method": method,
                    "path": path,
                    "summary": op.get("summary", ""),
                    "description": op.get("description", ""),
                    "params": params,
                    "body_fields": _body_fields(json_body, components) if json_body else [],
                    "has_json_body": json_body is not None,
                    "is_upload": multipart is not None,
                    # 200 无内容 = 文件直出（FileResponse），CLI 以 --output-file 落盘
                    "is_download": method == "get" and resp_content is None,
                    "envelope": json_ref.rsplit("/", 1)[-1].startswith("ApiResponse_"),
                    "hidden": bool(op.get("x-cli-hidden")),
                    "stream": op.get("x-cli-stream"),
                    "dangerous": op.get("x-cli-dangerous"),
                    "long_task": op.get("x-cli-long-task"),
                    "job": op.get("x-cli-job"),
                }
            )
    return ops


def is_generable(op: dict[str, Any]) -> bool:
    """P1 生成范围：除 Web 基础设施（hidden）与 SSE 流（P2 精选层接入）外全部生成。"""
    return bool(op["operation_id"]) and not op["hidden"] and not op["stream"]


def _api_path(path: str) -> str:
    """spec 里的路径带 /api/v1 前缀，http 层会再拼一次，这里剥掉。"""
    prefix = "/api/v1"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _load_input_body(value: str) -> Any:
    """--input 形态：从文件或 stdin（-）读取整体 JSON 请求体。"""
    try:
        text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(
            f"无法读取 --input 文件：{value}（{exc}）", exit_code=ExitCode.USAGE
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(
            f"--input 内容不是合法 JSON：{exc}",
            exit_code=ExitCode.USAGE,
            hint="请提供 JSON 对象；字段结构见对应 API schema",
        ) from exc


def _build_body(op: dict[str, Any], kwargs: dict[str, Any]) -> Any:
    """从命令标志组装 JSON 请求体（或采用 --input 整体替代）。"""
    # API 字段本身叫 input 时（如 session.start），该命令没有整体替代形态，
    # kwargs 里的 input 是普通字段值，不能在这里消费掉
    has_input_field = any(f["name"] == "input" for f in op["body_fields"])
    if not has_input_field and (input_value := kwargs.pop("input", None)):
        for f in op["body_fields"]:
            kwargs.pop(_dest_name(f), None)
        return _load_input_body(input_value)
    body: dict[str, Any] = {}
    missing: list[str] = []
    for f in op["body_fields"]:
        value = kwargs.pop(_dest_name(f), None)
        if value is None:
            if f["required"]:
                missing.append(_flag_name(f))
            continue
        if f["kind"] == "json":
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise CliError(
                    f"{_flag_name(f)} 不是合法 JSON：{exc}", exit_code=ExitCode.USAGE
                ) from exc
        body[f["name"]] = value
    if missing:
        raise CliError(
            f"缺少必填参数：{' '.join(missing)}",
            exit_code=ExitCode.USAGE,
            hint="逐个字段传参，或用 --input body.json 整体提供请求体（- 表示 stdin）",
        )
    return body


def _flag_name(field: dict[str, Any]) -> str:
    base = field["name"].replace("_", "-")
    return f"--{base}-json" if field["kind"] == "json" else f"--{base}"


def _dest_name(field: dict[str, Any]) -> str:
    return f"{field['name']}_json" if field["kind"] == "json" else field["name"]


def _confirm_danger(op: dict[str, Any], settings: Any, arg_line: str) -> None:
    """危险操作门槛（docs/design/cli.md §5.6）：confirm 需 --yes（TTY 可交互确认）；
    destructive 一律要求显式 --yes，并回显影响面。"""
    level = op["dangerous"]
    if not level:
        return
    if settings.yes:
        if level == "destructive":
            print(f"⚠ 破坏性操作（会删除磁盘文件）：{op['summary']}｜{arg_line}", file=sys.stderr)
        return
    if level == "confirm" and sys.stdin.isatty():
        if click.confirm(f"确认执行「{op['summary']}」？", default=False, err=True):
            return
        raise click.exceptions.Abort()
    raise CliError(
        f"该操作需要确认：{op['summary']}",
        exit_code=ExitCode.NEED_CONFIRM,
        hint="确认无误后重新执行并加 --yes"
        + ("（此操作会删除磁盘文件，请核对参数）" if level == "destructive" else ""),
    )


def wait_long_task(
    op: dict[str, Any],
    ops_by_id: dict[str, dict[str, Any]],
    api: Api,
    kwargs: dict[str, Any],
    wait_timeout: float,
) -> None:
    """长任务 --wait：轮询进度端点直到终态（docs/design/cli.md §8.3）。"""
    task = op["long_task"]
    progress_op = ops_by_id.get(task.get("progress_op", ""))
    if progress_op is None:
        return
    url = _api_path(progress_op["path"])
    for p in progress_op["params"]:
        if p["in"] == "path":
            url = url.replace("{" + p["name"] + "}", str(kwargs[p["name"]]))
    field = task.get("progress_field")
    done_field = task.get("done_field")
    started = time.monotonic()
    last_line = ""
    # 启动竞态防护：服务端是「响应先回、后台任务随后注册状态」，立刻轮询会把
    # 「还没开始」误判成「已结束」。先小睡一拍；此后若从未观测到运行态，需要
    # 连续多次读到终态才下结论（快任务确实可能在首次轮询前就跑完）。
    time.sleep(1.0)
    saw_running = False
    done_streak = 0
    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed > wait_timeout:
                progress_cmd = "mclaw " + progress_op["operation_id"].replace(".", " ")
                raise CliError(
                    f"等待超时（{wait_timeout:.0f} 秒），任务仍在后台执行",
                    exit_code=ExitCode.TASK_FAILED,
                    hint=f"稍后可用 {progress_cmd} 查看进度，或调大 --wait-timeout",
                )
            data = api.request("GET", url)
            progress = (data or {}).get(field) if field else data
            done = (progress is None) if field else not (data or {}).get(done_field)
            if done:
                if saw_running:
                    # 注意：进度接口不区分成功/失败，这里只能断言「已结束」；
                    # 若怀疑失败可查系统日志（mclaw logs tail）
                    print("任务已结束", file=sys.stderr)
                    return
                done_streak += 1
                if done_streak >= 3:
                    print("任务已结束（未观测到运行状态，可能启动后瞬间完成）", file=sys.stderr)
                    return
                time.sleep(2)
                continue
            saw_running = True
            done_streak = 0
            line = json.dumps(progress, ensure_ascii=False, default=str)
            if line != last_line:
                print(f"进行中：{line}", file=sys.stderr)
                last_line = line
            # 轮询节奏自适应：起步勤快，跑得久了放缓，别给服务器添乱
            time.sleep(2 if elapsed < 30 else 5 if elapsed < 120 else 10)
    except KeyboardInterrupt:
        print(
            "已停止等待（任务仍在后台执行，未被取消；如需取消请用对应的 stop 命令）",
            file=sys.stderr,
        )
        raise click.exceptions.Abort() from None


def _value_at_path(value: Any, path: str) -> Any:
    """按点分路径读取响应字段；不存在返回 None。"""
    for token in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(token)
    return value


def wait_persistent_job(
    api: Api,
    start_data: Any,
    metadata: dict[str, Any],
    wait_timeout: float,
) -> None:
    """等待统一 Job 终态；本地停止等待不会取消服务端任务。"""
    job_id = _value_at_path(start_data, metadata.get("id_path", "id"))
    if not isinstance(job_id, str) or not job_id:
        raise CliError(
            "服务端已接收任务，但响应中没有可追踪的任务 ID",
            exit_code=ExitCode.BUSINESS,
            hint="请用 mclaw jobs list --active-only 查找刚创建的任务",
        )
    started = time.monotonic()
    revision = 0
    last_line = ""
    terminal = {"succeeded", "failed", "cancelled", "blocked"}
    try:
        while True:
            remaining = wait_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise CliError(
                    f"等待超时（{wait_timeout:.0f} 秒），任务仍在后台执行",
                    exit_code=ExitCode.TASK_FAILED,
                    hint=f"稍后执行 mclaw jobs show {job_id}，或调大 --wait-timeout",
                )
            payload = api.request(
                "GET",
                f"/jobs/{job_id}/wait",
                params={"after_revision": revision, "wait_seconds": min(25.0, remaining)},
            )
            job = (payload or {}).get("job") or {}
            revision = max(revision, int(job.get("revision") or 0))
            status = str(job.get("status") or "")
            progress = job.get("progress") or {}
            line = str(progress.get("message") or status or "等待执行")
            percent = progress.get("percent")
            if percent is not None:
                line = f"{line}（{percent:g}%）"
            if line != last_line:
                print(f"{job_id} · {line}", file=sys.stderr)
                last_line = line
            if status not in terminal:
                continue
            if status == "succeeded":
                print(f"任务已完成：{job_id}", file=sys.stderr)
                return
            error = job.get("error") or {}
            message = error.get("message") or progress.get("message") or f"任务状态：{status}"
            hint = f"执行 mclaw jobs show {job_id} 查看详情"
            if any(action.get("type") == "handoff_agent" for action in error.get("actions") or []):
                hint += "；也可以交给 MovieClaw Agent 处理"
            raise CliError(str(message), exit_code=ExitCode.TASK_FAILED, hint=hint)
    except KeyboardInterrupt:
        print(
            f"已停止等待，任务 {job_id} 仍在后台执行；需要取消时运行 mclaw jobs cancel {job_id}",
            file=sys.stderr,
        )
        raise click.exceptions.Abort() from None


def _make_command(op: dict[str, Any], ops_by_id: dict[str, dict[str, Any]]) -> click.Command:
    path_params = [p for p in op["params"] if p["in"] == "path"]
    query_params = [p for p in op["params"] if p["in"] == "query"]
    method = op["method"].upper()

    def callback(**kwargs: Any) -> None:
        settings = merge_settings(kwargs)
        wait = kwargs.pop("wait", False)
        wait_timeout = kwargs.pop("wait_timeout", 3600.0) or 3600.0
        output_file = kwargs.pop("output_file", None)
        upload_file = kwargs.pop("file", None)

        url = _api_path(op["path"])
        arg_bits = []
        for p in path_params:
            url = url.replace("{" + p["name"] + "}", str(kwargs[p["name"]]))
            arg_bits.append(f"{p['name']}={kwargs[p['name']]}")
        _confirm_danger(op, settings, " ".join(arg_bits) or "（无参数）")

        query = {
            p["name"]: kwargs.get(p["name"])
            for p in query_params
            if kwargs.get(p["name"]) is not None
        }
        body = _build_body(op, kwargs) if op["has_json_body"] else None

        api: Api = settings.make_api()
        try:
            if op["is_download"]:
                target = Path(output_file)
                api.download(url, target, params=query or None)
                print(f"已保存：{target}", file=sys.stderr)
                return
            if op["is_upload"]:
                data = api.request(method, url, params=query or None, upload_path=Path(upload_file))
            else:
                data = api.request(method, url, params=query or None, json_body=body)
            if api.last_message and not settings.quiet:
                print(api.last_message, file=sys.stderr)
            emit(data, output=settings.output, quiet=settings.quiet)
            if op["job"] and wait:
                wait_persistent_job(api, data, op["job"], wait_timeout)
            elif op["long_task"] and wait:
                wait_long_task(op, ops_by_id, api, kwargs, wait_timeout)
        finally:
            api.close()

    cli_params: list[click.Parameter] = []
    for p in path_params:
        cli_params.append(click.Argument([p["name"]], type=_click_type(p["schema"])))
    for p in query_params:
        flag = "--" + p["name"].replace("_", "-")
        default = p["schema"].get("default")
        cli_params.append(
            click.Option(
                [flag],
                type=_click_type(p["schema"]),
                required=p["required"],
                default=default,
                show_default=default is not None,
                help=p["description"],
            )
        )
    for f in op["body_fields"]:
        schema = f["schema"]
        help_text = f["description"] or ""
        if f["required"]:
            help_text = f"[必填] {help_text}".rstrip()
        if f["kind"] == "json":
            help_text = (
                (help_text + "（JSON 字面量）").strip("（） ")
                if not help_text
                else (help_text + "（JSON 字面量）")
            )
        cli_params.append(
            click.Option(
                [_flag_name(f)],
                type=click.STRING if f["kind"] == "json" else _click_type(schema),
                # 必填校验在 _build_body 做（--input 可整体替代，不能在 click 层强制）
                required=False,
                default=None,
                help=help_text or ("JSON 字面量" if f["kind"] == "json" else None),
            )
        )
    if op["has_json_body"] and not any(f["name"] == "input" for f in op["body_fields"]):
        cli_params.append(
            click.Option(
                ["--input"],
                default=None,
                help="从文件读取整体 JSON 请求体（- 表示 stdin），替代逐字段传参",
            )
        )
    if op["is_upload"]:
        cli_params.append(
            click.Option(
                ["--file"],
                required=True,
                type=click.Path(exists=True, dir_okay=False),
                help="要上传的本地文件路径",
            )
        )
    if op["is_download"]:
        cli_params.append(
            click.Option(["--output-file"], required=True, help="下载内容保存到的本地路径")
        )
    if op["long_task"] or op["job"]:
        cli_params.append(
            click.Option(
                ["--wait/--no-wait"],
                default=not bool(op["job"]),
                show_default=True,
                help=(
                    "等待持久化任务完成；默认立即返回任务 ID"
                    if op["job"]
                    else "等待任务完成（轮询进度到终态）；--no-wait 启动后立即返回"
                ),
            )
        )
        cli_params.append(
            click.Option(
                ["--wait-timeout"],
                type=float,
                default=3600.0,
                show_default=True,
                help="--wait 的最长等待秒数，超时退出码 6（任务继续后台执行）",
            )
        )

    # API 参数与全局覆盖标志重名时 API 参数优先（守护测试保证目前无重名）
    taken = {p["name"] for p in op["params"]} | {_dest_name(f) for f in op["body_fields"]}
    cli_params.extend(o for o in _global_override_options() if o.name not in taken)

    help_text = op["summary"]
    if op["description"]:
        help_text = f"{op['summary']}\n\n{op['description']}"
    help_text += f"\n\n示例：\n\n  {_example_line(op)}"
    if op["dangerous"]:
        label = "破坏性操作（删除磁盘文件）" if op["dangerous"] == "destructive" else "需确认"
        help_text += f"\n\n⚠ {label}：非交互执行须加 --yes"
    return click.Command(
        name=op["operation_id"].rsplit(".", 1)[-1],
        callback=callback,
        params=cli_params,
        help=help_text,
        short_help=("⚠ " if op["dangerous"] else "") + op["summary"],
    )


def _example_line(op: dict[str, Any]) -> str:
    """从命令形状自动合成一条示例（spec 未提供 x-cli-examples 时的兜底）。"""
    parts = ["mclaw", *op["operation_id"].split(".")]
    parts += [f"<{p['name']}>" for p in op["params"] if p["in"] == "path"]
    parts += [
        f"--{p['name'].replace('_', '-')} {_example_value(p['name'], p['schema'])}"
        for p in op["params"]
        if p["in"] == "query" and p["required"]
    ]
    parts += [
        f"{_flag_name(f)} {_example_value(f['name'], f['schema'], json_value=f['kind'] == 'json')}"
        for f in op["body_fields"]
        if f["required"]
    ]
    if op["is_upload"]:
        parts.append("--file <本地文件>")
    if op["is_download"]:
        parts.append("--output-file <保存路径>")
    if op["dangerous"]:
        parts.append("--yes")
    return " ".join(parts)


def _example_value(name: str, schema: dict[str, Any], *, json_value: bool = False) -> str:
    """为自动示例选择可直接理解的值，避免对人和模型都无信息量的 ``<值>``。"""
    semantic = {
        "title_ref": "tmdb:movie:438631",
        "collection_ref": "tmdb:movie:popular",
        "media_type": "movie",
        "provider": "tmdb",
        "kind": "movie",
        "state": "active",
        "root_paths": "'[\"/media/movies\"]'",
        "file_ids": "'[101,102]'",
        "ordered_ids": "'[1,2]'",
    }
    if name in semantic:
        return semantic[name]
    if enum := schema.get("enum"):
        return str(enum[0])
    if json_value:
        return "'[1,2]'" if schema.get("type") == "array" else "'<JSON>'"
    if schema.get("type") == "boolean":
        return "true"
    if schema.get("type") in {"integer", "number"}:
        return "1"
    return f"<{name}>"


def build_tree(root: click.Group, spec: dict[str, Any]) -> None:
    """把生成命令挂到根命令组。已存在的同名命令（精选层）优先，不覆盖。"""
    ops = [op for op in iter_operations(spec) if is_generable(op)]
    ops_by_id = {op["operation_id"]: op for op in iter_operations(spec) if op["operation_id"]}
    for op in ops:
        segments = op["operation_id"].split(".")
        group = root
        for index, segment in enumerate(segments[:-1]):
            existing = group.commands.get(segment)
            if existing is None:
                prefix = ".".join(segments[: index + 1])
                existing = click.Group(
                    name=segment,
                    help=DOMAIN_HELP.get(segment) or COMMAND_GROUP_HELP.get(prefix),
                )
                group.add_command(existing)
            elif not isinstance(existing, click.Group):
                break  # 精选层占了同名命令，生成命令让位
            group = existing
        else:
            if segments[-1] not in group.commands:
                group.add_command(_make_command(op, ops_by_id))


def generated_command_paths(spec: dict[str, Any]) -> list[str]:
    """全部生成命令的完整命令路径（快照测试的数据源）。"""
    return sorted(
        op["operation_id"].replace(".", " ") for op in iter_operations(spec) if is_generable(op)
    )
