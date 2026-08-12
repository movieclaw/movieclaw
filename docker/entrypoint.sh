#!/bin/bash
# =============================================================================
# movieclaw 容器入口：一个容器同时跑 FastAPI 后端和 Next.js 前端。
#
# 进程模型：
#   - 后端监听容器内 8000（不对外），前端监听 3000（对外唯一端口）
#   - /api/v1 请求由 Next 服务器反代到后端（构建时固化的 rewrite 规则）
#   - 后端健康后才启动前端；运行中任意进程退出、或完整健康链路持续失败时，
#     整个容器退出（交给 Docker 的 restart 策略拉起），避免出现"半死"状态
#   - 完整启动/重启期间由一个占位服务顶住 3000：页面请求返回「正在启动」并
#     自动轮询刷新，/api/* 返回 503——用户看到的是明确的启动状态而不是
#     浏览器的「无法连接」
#   - 例外一：后端以约定码 42 退出表示「设置页请求的重启」，只重启后端，
#     前端进程保持运行（窗口内 API 反代短暂 502，发起重启的页面本就在轮询
#     等待）；后端与反代链路重新验证健康后恢复看门狗，失败则升级为完整重启
#   - 例外二：后端以约定码 43 退出表示「应用内更新/回退后的全量重启」，
#     前后端一起重启，并重新解析代码来源（可能切到新的 overlay 版本）
#   - 异常退出前把原因写入 data 卷（updates/state/last-exit.json），后端下次
#     启动后读取并在 UI 外显——无人值守的自愈不能悄无声息
#
# 代码来源解析（应用内更新机制，docs/design/in-app-update.md）：
#   镜像内 /app/src、/app/web 是构建时烧入的基线，永远完整可运行；
#   /app/data/updates/versions/<ver>/ 是应用内更新下载的 overlay（data 卷上，
#   容器重建不丢）。本脚本启动时按 current → previous → 基线 的顺序解析出
#   实际启动的代码目录——更新从不覆盖镜像内文件，只改变启动指向。
#   overlay 必须通过完整性与 runtime 兼容校验（requires_runtime 与镜像的
#   /etc/movieclaw-runtime 一致）才会被采用；启动后短时间内连续失败 2 次的
#   overlay 会被标记为 bad 并自动回落，保证坏更新永远不会让容器起不来。
#
# 本脚本烧在镜像里、无法应用内更新，因此只保留最小且稳定的逻辑：
# 解析启动指向、拉起进程、处理重启约定码与失败兜底。版本相关的复杂逻辑
# （下载/校验/切换/回退）都在可更新的后端代码里（services/app_update.py）。
#
# 测试钩子：`entrypoint.sh resolve` 只打印解析结果不拉进程；路径可用
# MOVIECLAW_APP_ROOT / MOVIECLAW_DATA_DIR / MOVIECLAW_RUNTIME_FILE 覆盖，
# 供 tests/docker/ 在临时目录里验证解析矩阵。
# =============================================================================
set -euo pipefail

APP_ROOT="${MOVIECLAW_APP_ROOT:-/app}"
DATA_DIR="${MOVIECLAW_DATA_DIR:-$APP_ROOT/data}"
RUNTIME_FILE="${MOVIECLAW_RUNTIME_FILE:-/etc/movieclaw-runtime}"
UPDATES_DIR="$DATA_DIR/updates"
STATE_DIR="$UPDATES_DIR/state"
# overlay 启动失败的判定窗口与次数：启动后不足 GRACE 秒即退出算「启动失败」，
# 同一版本连续失败 MAX 次即标记 bad 并回落
STARTUP_GRACE_SECONDS="${MOVIECLAW_STARTUP_GRACE_SECONDS:-60}"
MAX_STARTUP_FAILURES=2
# 启动和运行期健康监督的边界：后端在完成 FastAPI lifespan 前不会监听 8000，
# 因此前端必须等待它真正可请求后才能启动。启动超时设得比 overlay 的短失败窗口
# 长得多，兼顾首次迁移/慢盘；超时仍未就绪则属于确定的启动失败，不能无限等待。
API_STARTUP_TIMEOUT_SECONDS="${MOVIECLAW_API_STARTUP_TIMEOUT_SECONDS:-300}"
# 设置页请求重启时应用已运行过，后端应很快恢复；单独使用较短的等待窗口，
# 失败后直接走完整启动/回退，避免前端长时间处于无后端可用的状态。
API_RESTART_TIMEOUT_SECONDS="${MOVIECLAW_API_RESTART_TIMEOUT_SECONDS:-60}"
WEB_STARTUP_TIMEOUT_SECONDS="${MOVIECLAW_WEB_STARTUP_TIMEOUT_SECONDS:-60}"
HEALTHCHECK_INTERVAL_SECONDS="${MOVIECLAW_HEALTHCHECK_INTERVAL_SECONDS:-10}"
HEALTHCHECK_FAILURE_THRESHOLD="${MOVIECLAW_HEALTHCHECK_FAILURE_THRESHOLD:-6}"
SHUTDOWN_GRACE_SECONDS="${MOVIECLAW_SHUTDOWN_GRACE_SECONDS:-20}"
API_HEALTH_URL="http://127.0.0.1:8000/api/v1/health"
WEB_HEALTH_URL="http://127.0.0.1:3000/api/v1/health"
# 看门狗以此约定码退出；主循环会把它转换成容器失败退出，不能与后端的 42/43
# 重启约定混用。
HEALTH_WATCHDOG_EXIT_CODE=75

PYTHON_BIN="${MOVIECLAW_PYTHON_BIN:-/venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3)"
fi

# 镜像的运行时版本（依赖集合的代号，见 docker/runtime-version）。
# 读不到时置 0：任何 overlay 都不会匹配，等价于禁用 overlay、只走基线。
if [ -r "$RUNTIME_FILE" ]; then
    RUNTIME_VERSION="$(tr -d '[:space:]' < "$RUNTIME_FILE")"
else
    RUNTIME_VERSION=0
fi
export MOVIECLAW_RUNTIME_VERSION="$RUNTIME_VERSION"

# ---------------------------------------------------------------------------
# overlay 解析
# ---------------------------------------------------------------------------

# 读 manifest.json 的一个字段；文件缺失/损坏一律输出空串（调用方按无效处理）
read_manifest_field() {
    "$PYTHON_BIN" - "$1" "$2" <<'PY' 2>/dev/null || true
import json, sys
try:
    value = json.load(open(sys.argv[1])).get(sys.argv[2], "")
    print("" if value is None else value)
except Exception:
    pass
PY
}

# 版本号转文件名安全形式（bad 标记 / 失败计数的文件名）
sanitize() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'
}

# 校验一个 overlay 目录是否可用；可用则输出其版本号，否则输出空
overlay_version_if_valid() {
    local dir="$1"
    [ -d "$dir" ] || return 0
    local manifest="$dir/manifest.json"
    # 布局完整性：后端入口、迁移配置、前端入口缺一不可
    local f
    for f in "$manifest" "$dir/backend/src/movieclaw_api/main.py" \
             "$dir/backend/alembic.ini" "$dir/web/apps/web/server.js"; do
        if [ ! -f "$f" ]; then
            echo "[entrypoint] overlay $dir 缺少 $f，忽略该版本" >&2
            return 0
        fi
    done
    local version requires
    version="$(read_manifest_field "$manifest" version)"
    requires="$(read_manifest_field "$manifest" requires_runtime)"
    if [ -z "$version" ] || [ -z "$requires" ]; then
        echo "[entrypoint] overlay $dir 的 manifest.json 不完整，忽略该版本" >&2
        return 0
    fi
    if [ "$requires" != "$RUNTIME_VERSION" ]; then
        echo "[entrypoint] overlay v$version 需要 runtime=$requires，镜像为 runtime=$RUNTIME_VERSION，忽略（需升级 Docker 镜像）" >&2
        return 0
    fi
    if [ -f "$STATE_DIR/bad-$(sanitize "$version")" ]; then
        echo "[entrypoint] overlay v$version 曾连续启动失败已标记为坏版本，忽略" >&2
        return 0
    fi
    echo "$version"
}

# 解析启动指向，结果写入全局变量并导出给子进程（后端与 Agent 都靠这些感知）：
#   ACTIVE_SOURCE=overlay|baseline  ACTIVE_VERSION（overlay 时非空）
#   BACKEND_ROOT（含 src/ alembic/ alembic.ini 的项目根） WEB_ROOT（含 apps/web/server.js）
resolve_code() {
    ACTIVE_SOURCE=baseline
    ACTIVE_VERSION=""
    BACKEND_ROOT="$APP_ROOT"
    WEB_ROOT="$APP_ROOT/web"
    local link target version
    for link in current previous; do
        target="$UPDATES_DIR/$link"
        [ -e "$target" ] || continue
        version="$(overlay_version_if_valid "$target")"
        if [ -n "$version" ]; then
            ACTIVE_SOURCE=overlay
            ACTIVE_VERSION="$version"
            BACKEND_ROOT="$(readlink -f "$target")/backend"
            WEB_ROOT="$(readlink -f "$target")/web"
            if [ "$link" = "previous" ]; then
                echo "[entrypoint] current 版本不可用，回退使用上一版本 v$version" >&2
            fi
            break
        fi
    done

    export MOVIECLAW_CODE_SOURCE="$ACTIVE_SOURCE"
    export MOVIECLAW_CODE_ROOT="$BACKEND_ROOT"
    # 更新/模型目录以 entrypoint 的 DATA_DIR 为唯一事实源导出给后端：
    # 后端配置里的同名变量默认值只是「约定一致」，显式导出彻底堵死
    # 「更新装到 A 目录、启动解析 B 目录」的 split-brain（哪怕用户只覆盖了其一）
    export MOVIECLAW_UPDATES_DIR="$UPDATES_DIR"
    export MOVIECLAW_MODELS_DIR="$DATA_DIR/models/ner"
    if [ -n "$ACTIVE_VERSION" ]; then
        export MOVIECLAW_OVERLAY_VERSION="$ACTIVE_VERSION"
    else
        unset MOVIECLAW_OVERLAY_VERSION || true
    fi

    # NER 模型指针：data 卷上有完整的模型目录则用它（应用内模型更新），
    # 否则回落镜像内置模型（Dockerfile 的 ENV MOVIECLAW_NER_DIR）
    local model_dir="$DATA_DIR/models/ner/current"
    if [ -f "$model_dir/model.int8.onnx" ] && [ -f "$model_dir/tokenizer.json" ] \
        && [ -f "$model_dir/labels.json" ]; then
        MOVIECLAW_NER_DIR="$(readlink -f "$model_dir")"
        export MOVIECLAW_NER_DIR
    fi
}

# 测试钩子：只解析并打印结果，不拉起任何进程
if [ "${1:-}" = "resolve" ]; then
    resolve_code
    echo "source=$ACTIVE_SOURCE"
    echo "version=$ACTIVE_VERSION"
    echo "backend_root=$BACKEND_ROOT"
    echo "web_root=$WEB_ROOT"
    echo "runtime=$RUNTIME_VERSION"
    echo "ner_dir=${MOVIECLAW_NER_DIR:-}"
    exit 0
fi

# ---------------------------------------------------------------------------
# 进程管理
# ---------------------------------------------------------------------------

cd "$APP_ROOT"

# 数据库迁移由后端启动时自动执行（movieclaw_db/migrations.py），无需在此处理

# 容器内后端端口显式钉死为 8000：它是 Next 反代目标（构建时固化），
# 不受部署者环境变量干扰；容器对外端口请改 compose 的 ports 映射。
API_PID=""
WEB_PID=""
WATCHDOG_PID=""
PLACEHOLDER_PID=""
SHUTTING_DOWN=0
# 启动门禁的失败信息由函数写入全局变量。Shell 函数的 return 值只表达成功/失败，
# 用变量保留子进程真实退出码，才能继续支持 42/43 与 overlay 的失败计数。
START_FAILURE_CODE=1
START_FAILURE_UPTIME=0
START_FAILURE_IS_TIMEOUT=0
WAIT_FAILURE_CODE=1
WAIT_TIMED_OUT=0

process_is_running() {
    local pid="${1:-}"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# HTTP 成功才是"就绪"，不能只探测 TCP 端口：FastAPI 的 lifespan 未完成时后端
# 尚不可接请求；前端则必须验证 rewrite 已能穿透到后端。
probe_health() {
    "$PYTHON_BIN" -c '
import sys
from urllib.request import urlopen

try:
    with urlopen(sys.argv[1], timeout=2) as response:
        raise SystemExit(0 if 200 <= response.status < 300 else 1)
except Exception:
    raise SystemExit(1)
' "$1" >/dev/null 2>&1
}

# 占位页（纯体验优化，尽力而为）：完整启动/重启期间 3000 端口本来无人监听，
# 用户浏览器只会看到「无法连接」——对非开发者这与「装坏了」无法区分，而首次
# 启动的数据迁移可能要几分钟。占位服务先顶住 3000：页面请求返回「正在启动」
# 并自动轮询健康接口，就绪后自动进入应用；/api/* 一律 503，Docker healthcheck
# 与设置页的重启轮询都按未就绪处理，不会被占位页误判成健康。
# 它起不来（如端口未释放）只会在容器日志留一条报错，绝不阻塞真正的启动流程。
start_placeholder() {
    "$PYTHON_BIN" -c '
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>movieclaw 正在启动</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #0b0f17; color: #e2e8f0;
         font-family: system-ui, sans-serif; }
  .card { text-align: center; padding: 2rem; max-width: 32rem; }
  .spin { margin: 0 auto 1.25rem; width: 2rem; height: 2rem; border-radius: 50%;
          border: 3px solid rgba(226,232,240,.25); border-top-color: #e2e8f0;
          animation: r 1s linear infinite; }
  @keyframes r { to { transform: rotate(360deg); } }
  p { margin: .4rem 0; }
  .sub { color: #94a3b8; font-size: .875rem; }
</style>
</head>
<body>
<div class="card">
  <div class="spin"></div>
  <p>movieclaw 正在启动，请稍候……</p>
  <p class="sub">首次启动或更新后可能需要执行数据迁移，通常几秒到几分钟。就绪后本页会自动进入应用。</p>
</div>
<script>
setInterval(function () {
  fetch("/api/v1/health").then(function (r) {
    if (r.ok) { location.reload(); }
  }).catch(function () {});
}, 2000);
</script>
</body>
</html>
""".encode("utf-8")

API_BODY = json.dumps(
    {"success": False, "code": "APP_STARTING", "message": "应用正在启动，请稍候", "details": None},
    ensure_ascii=False,
).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def _respond(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._respond(503, "application/json; charset=utf-8", API_BODY)
        else:
            self._respond(200, "text/html; charset=utf-8", PAGE)

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 3000), Handler).serve_forever()
' &
    PLACEHOLDER_PID=$!
}

stop_placeholder() {
    if process_is_running "$PLACEHOLDER_PID"; then
        kill "$PLACEHOLDER_PID" 2>/dev/null || true
    fi
    if [ -n "$PLACEHOLDER_PID" ]; then
        wait "$PLACEHOLDER_PID" 2>/dev/null || true
    fi
    PLACEHOLDER_PID=""
}

# 异常退出前把原因落盘（$STATE_DIR/last-exit.json）。容器随后会被 Docker 拉起，
# 后端启动后读取该文件，在「设置 → 关于与更新」向用户外显「上次发生过什么」——
# 无人值守的自愈不能悄无声息。正常停机与成功的 42/43 重启不写。
record_failure_exit() {
    local reason="$1" exit_code="$2" detail="$3"
    mkdir -p "$STATE_DIR" 2>/dev/null || return 0
    printf '{"ts": %s, "reason": "%s", "exit_code": %s, "detail": "%s"}\n' \
        "$(date +%s)" "$reason" "$exit_code" "$detail" \
        > "$STATE_DIR/last-exit.json" 2>/dev/null || true
}

# 与 probe_health 同一探测，但输出失败的具体原因（连接拒绝/超时/HTTP 状态码）。
# 这是给非开发者看的诊断日志：只说「失败」没法判断该查哪一层。
describe_health_failure() {
    "$PYTHON_BIN" -c '
import sys
from urllib.request import urlopen

try:
    with urlopen(sys.argv[1], timeout=2) as response:
        if 200 <= response.status < 300:
            print("已恢复正常")
        else:
            print(f"HTTP {response.status}")
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}"[:200])
' "$1" 2>/dev/null || echo "健康探测进程自身异常"
}

# 在等待健康接口时同步观察进程是否提前退出，避免后端 import/迁移报错后还白等
# 到超时。WAIT_FAILURE_CODE 保存真实退出码；正常退出却未就绪也按失败码 1 处理。
wait_for_health() {
    local name="$1"
    local pid="$2"
    local url="$3"
    local timeout="$4"
    # 前端反代就绪依赖后端持续存活。后端自身就绪时无需传入依赖进程；前端
    # 就绪时传入 API_PID，避免 API 恰在两阶段之间退出后，前端继续监听并对
    # 空的 8000 反代到超时。
    local dependency_name="${5:-}"
    local dependency_pid="${6:-}"
    local started now exit_code
    started="$(date +%s)"
    WAIT_FAILURE_CODE=1
    WAIT_TIMED_OUT=0

    while true; do
        if [ -n "$dependency_pid" ] && ! process_is_running "$dependency_pid"; then
            if wait "$dependency_pid" 2>/dev/null; then
                exit_code=0
            else
                exit_code=$?
            fi
            WAIT_FAILURE_CODE="$exit_code"
            if [ "$WAIT_FAILURE_CODE" -eq 0 ]; then
                WAIT_FAILURE_CODE=1
            fi
            echo "[entrypoint] $dependency_name 在${name}就绪前退出（exit=${exit_code}）。" >&2
            return 1
        fi
        if ! process_is_running "$pid"; then
            if wait "$pid" 2>/dev/null; then
                exit_code=0
            else
                exit_code=$?
            fi
            WAIT_FAILURE_CODE="$exit_code"
            if [ "$WAIT_FAILURE_CODE" -eq 0 ]; then
                WAIT_FAILURE_CODE=1
            fi
            echo "[entrypoint] $name 在就绪前退出（exit=${exit_code}）。" >&2
            return 1
        fi
        if probe_health "$url"; then
            echo "[entrypoint] $name 已就绪：$url"
            return 0
        fi
        now="$(date +%s)"
        if [ "$(( now - started ))" -ge "$timeout" ]; then
            WAIT_FAILURE_CODE=124
            WAIT_TIMED_OUT=1
            echo "[entrypoint] 等待 $name 就绪超过 ${timeout}s（${url}），停止本次启动。" >&2
            return 1
        fi
        sleep 1
    done
}

start_api() {
    echo "[entrypoint] 启动后端 (FastAPI, 127.0.0.1:8000)……来源：$ACTIVE_SOURCE${ACTIVE_VERSION:+ v$ACTIVE_VERSION}"
    API_START_TS="$(date +%s)"
    PYTHONPATH="$BACKEND_ROOT/src" APP_PORT=8000 "$PYTHON_BIN" -m movieclaw_api.main &
    API_PID=$!
}

start_web() {
    echo "[entrypoint] 启动前端 (Next.js, 0.0.0.0:3000)……来源：$ACTIVE_SOURCE${ACTIVE_VERSION:+ v$ACTIVE_VERSION}"
    WEB_START_TS="$(date +%s)"
    PORT=3000 HOSTNAME=0.0.0.0 node "$WEB_ROOT/apps/web/server.js" &
    WEB_PID=$!
}

# 运行期看门狗验证完整的「Next → rewrite → FastAPI」链路。Docker HEALTHCHECK
# 只能标记 unhealthy，restart policy 不会因该状态自动重启；由入口脚本主动失败
# 退出，Docker 才能可靠地拉起一个全新的进程组。
start_health_watchdog() {
    (
        local failures=0 reason segment
        while true; do
            sleep "$HEALTHCHECK_INTERVAL_SECONDS"
            if probe_health "$WEB_HEALTH_URL"; then
                failures=0
                continue
            fi
            failures=$(( failures + 1 ))
            # 失败时补一次后端直连探测做分段归因：完整链路断了，先分清是
            # 后端故障还是前端/反代故障，用户贴日志时就能直接定位到出问题的层。
            reason="$(describe_health_failure "$WEB_HEALTH_URL")"
            if probe_health "$API_HEALTH_URL"; then
                segment="后端直连正常，疑似前端或反代故障"
            else
                segment="后端直连亦失败，疑似后端故障"
            fi
            echo "[entrypoint] 完整健康链路检查失败（$failures/${HEALTHCHECK_FAILURE_THRESHOLD}）：$WEB_HEALTH_URL（原因：$reason；$segment）" >&2
            if [ "$failures" -ge "$HEALTHCHECK_FAILURE_THRESHOLD" ]; then
                echo "[entrypoint] 完整健康链路连续失败，停止容器以触发 Docker 重启。" >&2
                exit "$HEALTH_WATCHDOG_EXIT_CODE"
            fi
        done
    ) &
    WATCHDOG_PID=$!
}

stop_health_watchdog() {
    if process_is_running "$WATCHDOG_PID"; then
        kill "$WATCHDOG_PID" 2>/dev/null || true
    fi
    if [ -n "$WATCHDOG_PID" ]; then
        wait "$WATCHDOG_PID" 2>/dev/null || true
    fi
    WATCHDOG_PID=""
}

# 先让服务自行收尾，超出宽限期才强杀。入口脚本不能无限 wait：进程卡死时，
# 无限等待会再次制造"容器仍在、服务已失效"的半死状态。
terminate_managed_processes() {
    local deadline now pid
    stop_health_watchdog
    stop_placeholder
    for pid in "$API_PID" "$WEB_PID"; do
        if process_is_running "$pid"; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    deadline=$(( $(date +%s) + SHUTDOWN_GRACE_SECONDS ))
    while process_is_running "$API_PID" || process_is_running "$WEB_PID"; do
        now="$(date +%s)"
        if [ "$now" -ge "$deadline" ]; then
            break
        fi
        sleep 1
    done
    for pid in "$API_PID" "$WEB_PID"; do
        if process_is_running "$pid"; then
            echo "[entrypoint] 进程 $pid 未在 ${SHUTDOWN_GRACE_SECONDS}s 内退出，强制终止。" >&2
            kill -KILL "$pid" 2>/dev/null || true
        fi
        if [ -n "$pid" ]; then
            wait "$pid" 2>/dev/null || true
        fi
    done
    API_PID=""
    WEB_PID=""
}

# 按顺序启动后端、直接健康检查、前端、完整链路健康检查。参数只在运行期 42
# 重启时使用较短的后端等待窗口；代码来源仍保持当前已解析版本，不会误切 overlay。
# 任一阶段失败时，前端都不会在后端不可用的状态下对外提供反代。
start_current_code() {
    local api_timeout="${1:-$API_STARTUP_TIMEOUT_SECONDS}"
    start_placeholder
    start_api
    if ! wait_for_health "后端" "$API_PID" "$API_HEALTH_URL" "$api_timeout"; then
        START_FAILURE_CODE="$WAIT_FAILURE_CODE"
        START_FAILURE_UPTIME=$(( $(date +%s) - API_START_TS ))
        START_FAILURE_IS_TIMEOUT="$WAIT_TIMED_OUT"
        return 1
    fi
    # 释放 3000 给真正的前端；占位页里的轮询脚本会在前端就绪后自动进入应用
    stop_placeholder
    start_web
    if ! wait_for_health "前端反代" "$WEB_PID" "$WEB_HEALTH_URL" "$WEB_STARTUP_TIMEOUT_SECONDS" \
        "后端" "$API_PID"; then
        START_FAILURE_CODE="$WAIT_FAILURE_CODE"
        START_FAILURE_UPTIME=$(( $(date +%s) - WEB_START_TS ))
        START_FAILURE_IS_TIMEOUT="$WAIT_TIMED_OUT"
        return 1
    fi
    start_health_watchdog
    return 0
}

start_all() {
    resolve_code
    start_current_code "$API_STARTUP_TIMEOUT_SECONDS"
}

# 设置页重启（42）的快速路径：只重启后端，前端进程与用户的页面会话保持不动。
# 后端不可用的窗口内 API 反代会短暂 502，但发起重启的页面本就处于轮询等待态；
# 相比连前端一起冷重启，窗口更短、3000 端口也不会整体消失。看门狗必须先停——
# 否则窗口内预期的探测失败会被计成故障、把容器杀掉。代码来源保持当前已解析
# 版本，不会误切 overlay。
restart_api_keeping_web() {
    stop_health_watchdog
    start_api
    if ! wait_for_health "后端" "$API_PID" "$API_HEALTH_URL" "$API_RESTART_TIMEOUT_SECONDS"; then
        START_FAILURE_CODE="$WAIT_FAILURE_CODE"
        START_FAILURE_UPTIME=$(( $(date +%s) - API_START_TS ))
        START_FAILURE_IS_TIMEOUT="$WAIT_TIMED_OUT"
        return 1
    fi
    # 前端一直在运行，但完整反代链路必须重新验证（前端可能恰在窗口内退出）
    if ! wait_for_health "前端反代" "$WEB_PID" "$WEB_HEALTH_URL" "$WEB_STARTUP_TIMEOUT_SECONDS" \
        "后端" "$API_PID"; then
        START_FAILURE_CODE="$WAIT_FAILURE_CODE"
        START_FAILURE_UPTIME=$(( $(date +%s) - WEB_START_TS ))
        START_FAILURE_IS_TIMEOUT="$WAIT_TIMED_OUT"
        return 1
    fi
    start_health_watchdog
    return 0
}

# overlay 启动失败兜底：短时间内退出或就绪超时计一次失败，连续 MAX 次标记 bad。
# 返回 0 表示「已处理，调用方应重启全部进程再试」；返回 1 表示按真故障处理。
# 只对 overlay 生效——基线是镜像烧入的，起不来属于环境问题，必须外显。
handle_startup_failure() {
    local uptime="$1"
    local forced_timeout="${2:-0}"
    if [ "$ACTIVE_SOURCE" != "overlay" ]; then
        return 1
    fi
    local marker
    marker="$(sanitize "$ACTIVE_VERSION")"
    if [ "$uptime" -ge "$STARTUP_GRACE_SECONDS" ] && [ "$forced_timeout" -ne 1 ]; then
        # 稳定运行过一段时间后才挂：不是坏更新，清掉启动失败计数，按真故障处理
        rm -f "$STATE_DIR/failures-$marker"
        return 1
    fi
    mkdir -p "$STATE_DIR"
    local fail_file="$STATE_DIR/failures-$marker"
    # 只统计时间窗内的失败（1 小时）：不洁关机（断电/OOM/docker kill）没有
    # wake 事件来清零计数，陈旧记录若被原样累计，相隔数周的两次孤立故障
    # 会把好版本误标成坏版本——按时间戳过滤让「连续」语义不依赖清零时机
    local now cutoff
    now="$(date +%s)"
    cutoff=$(( now - 3600 ))
    if [ -f "$fail_file" ]; then
        awk -v cutoff="$cutoff" '$1 >= cutoff' "$fail_file" > "$fail_file.tmp" \
            && mv "$fail_file.tmp" "$fail_file"
    fi
    echo "$now" >> "$fail_file"
    local count
    count="$(wc -l < "$fail_file")"
    if [ "$count" -ge "$MAX_STARTUP_FAILURES" ]; then
        touch "$STATE_DIR/bad-$marker"
        echo "[entrypoint] overlay v$ACTIVE_VERSION 启动后 ${uptime}s 内退出，已连续失败 $count 次：标记为坏版本并回落。" >&2
    else
        echo "[entrypoint] overlay v$ACTIVE_VERSION 启动后 ${uptime}s 内退出（第 $count 次），重试……" >&2
    fi
    return 0
}

# 收到停止信号时把所有子进程（前后端、看门狗、占位页）都带走，确保容器干净退出。
# SHUTTING_DOWN 标志让主循环把「停机导致的进程退出」与故障区分开——
# 否则 overlay 启动后 60 秒内 docker stop 会被误计为一次「启动失败」，
# 连续两次正常停容器就可能把好版本错标成坏版本。
# trap 必须先于 start_all 安装：启动窗口内到达的 TERM 不能被 PID 1 默认忽略。
shutdown() {
    SHUTTING_DOWN=1
    kill "${API_PID:-}" "${WEB_PID:-}" "${WATCHDOG_PID:-}" "${PLACEHOLDER_PID:-}" 2>/dev/null || true
}
trap shutdown TERM INT

# 统一处理首次启动、全量重启与 overlay 回退时的就绪失败。42/43 只有在服务
# 已就绪后才是合法重启请求；启动阶段出现它们说明该版本无法完成启动，必须和
# 其他失败一样计入 overlay 回退，不能无限重试。就绪超时同样强制计为启动失败。
boot_until_running() {
    while true; do
        if start_all; then
            return 0
        fi
        if [ "$SHUTTING_DOWN" -eq 1 ]; then
            return 1
        fi
        echo "[entrypoint] 启动阶段失败（exit=${START_FAILURE_CODE}），正在收尾。" >&2
        terminate_managed_processes
        if handle_startup_failure "$START_FAILURE_UPTIME" "$START_FAILURE_IS_TIMEOUT"; then
            continue
        fi
        return 1
    done
}

# 启动失败计数的衰减：进程稳定运行超过宽限期后清零该版本的计数。
# 否则计数在 data 卷上跨周跨月累计，两次相隔很久的孤立故障会把好版本
# 误标成坏版本——设计语义是「连续」失败，不是「累计」。
clear_failures_if_seasoned() {
    if [ "$ACTIVE_SOURCE" = "overlay" ] && [ -n "$ACTIVE_VERSION" ] \
        && [ "$(( NOW - API_START_TS ))" -ge "$STARTUP_GRACE_SECONDS" ] \
        && [ "$(( NOW - WEB_START_TS ))" -ge "$STARTUP_GRACE_SECONDS" ]; then
        rm -f "$STATE_DIR/failures-$(sanitize "$ACTIVE_VERSION")"
    fi
}

# 主循环：处理重启约定码（42 后端 / 43 全量）、overlay 启动失败兜底、
# 进程退出和健康看门狗故障。所有真故障最终让容器退出，交给 Docker restart
# 策略恢复；入口脚本不在容器内无限重试基线版本。
# EXIT_CODE 预置为 143（SIGTERM 的约定码）：TERM 落在首次 wait 之前时主循环
# 会在顶部直接 break，此时若变量未定义，set -u 会让脚本在结尾崩掉、跳过收尾
identify_exited_process() {
    EXITED_PID=""
    if ! process_is_running "$API_PID"; then
        EXITED_PID="$API_PID"
    elif ! process_is_running "$WEB_PID"; then
        EXITED_PID="$WEB_PID"
    elif ! process_is_running "$WATCHDOG_PID"; then
        EXITED_PID="$WATCHDOG_PID"
    fi
    [ -n "$EXITED_PID" ]
}

collect_exit_code() {
    local pid="$1"
    if wait "$pid" 2>/dev/null; then
        EXIT_CODE=0
    else
        EXIT_CODE=$?
    fi
}

# 镜像内的 Bash 5.2 用 wait -n -p 同时取得退出码与准确 PID，避免两个子进程
# 近同时退出时把错误退出码套到 API 的 42/43 语义上。开发机的旧 Bash 则以
# 一秒轮询降级；它只用于测试环境，不影响发布镜像的精确归属。
wait_for_managed_process_exit() {
    if identify_exited_process; then
        collect_exit_code "$EXITED_PID"
        return
    fi
    if [ "${BASH_VERSINFO[0]}" -gt 5 ] \
        || { [ "${BASH_VERSINFO[0]}" -eq 5 ] && [ "${BASH_VERSINFO[1]}" -ge 1 ]; }; then
        EXITED_PID=""
        wait -n -p EXITED_PID "$API_PID" "$WEB_PID" "$WATCHDOG_PID" && EXIT_CODE=0 || EXIT_CODE=$?
        # 被信号打断时 -p 可能未写入 PID；交给后续逻辑按停机处理，绝不猜测。
        return
    fi
    if [ "${BASH_VERSINFO[0]}" -gt 4 ] \
        || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 3 ]; }; then
        wait -n "$API_PID" "$WEB_PID" "$WATCHDOG_PID" && EXIT_CODE=0 || EXIT_CODE=$?
        identify_exited_process || true
        return
    fi
    while process_is_running "$API_PID" && process_is_running "$WEB_PID" \
        && process_is_running "$WATCHDOG_PID"; do
        if [ "$SHUTTING_DOWN" -eq 1 ]; then
            EXITED_PID=""
            return
        fi
        sleep 1
    done
    if identify_exited_process; then
        collect_exit_code "$EXITED_PID"
    fi
}

EXIT_CODE=143
if ! boot_until_running; then
    if [ "$SHUTTING_DOWN" -eq 1 ]; then
        EXIT_CODE=143
    else
        record_failure_exit startup_failure "$START_FAILURE_CODE" "启动阶段失败，应用未能完成就绪"
        EXIT_CODE="$START_FAILURE_CODE"
    fi
fi
while true; do
    if [ "$SHUTTING_DOWN" -eq 1 ] || [ -z "$API_PID" ]; then
        break
    fi
    wait_for_managed_process_exit
    NOW="$(date +%s)"
    clear_failures_if_seasoned
    if [ "$SHUTTING_DOWN" -eq 1 ]; then
        break # 停机信号已到：无论进程处于什么状态都直接走停机流程
    fi
    if [ -z "$EXITED_PID" ] && kill -0 "$API_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null; then
        break # 两个进程都活着 = wait 被停止信号打断（docker stop）：走停机流程
    fi
    if [ "$EXITED_PID" = "$API_PID" ]; then
        # 后端退出：先看重启约定码
        if [ "$EXIT_CODE" -eq 42 ]; then
            echo "[entrypoint] 后端请求重启（exit=42），保持前端运行，重启后端进程……"
            if restart_api_keeping_web; then
                continue
            fi
            echo "[entrypoint] 后端重启后未能就绪，改为完整重启。" >&2
            terminate_managed_processes
            if boot_until_running; then
                continue
            fi
            record_failure_exit startup_failure "$START_FAILURE_CODE" "设置页重启后应用未能重新就绪"
            EXIT_CODE="$START_FAILURE_CODE"
            break
        fi
        if [ "$EXIT_CODE" -eq 43 ]; then
            echo "[entrypoint] 应用请求全量重启（exit=43），重新解析代码来源并重启前后端……"
            terminate_managed_processes
            if boot_until_running; then
                continue
            fi
            record_failure_exit startup_failure "$START_FAILURE_CODE" "更新或回退后的全量重启未能就绪"
            EXIT_CODE="$START_FAILURE_CODE"
            break
        fi
        if handle_startup_failure "$(( NOW - API_START_TS ))"; then
            terminate_managed_processes
            if boot_until_running; then
                continue
            fi
            record_failure_exit startup_failure "$START_FAILURE_CODE" "启动阶段失败，应用未能完成就绪"
            EXIT_CODE="$START_FAILURE_CODE"
            break
        fi
        record_failure_exit api_crash "$EXIT_CODE" "后端进程异常退出"
        break # 后端真故障：结束容器
    fi
    if [ "$EXITED_PID" = "$WEB_PID" ]; then
        if handle_startup_failure "$(( NOW - WEB_START_TS ))"; then
            terminate_managed_processes
            if boot_until_running; then
                continue
            fi
            record_failure_exit startup_failure "$START_FAILURE_CODE" "启动阶段失败，应用未能完成就绪"
            EXIT_CODE="$START_FAILURE_CODE"
            break
        fi
        record_failure_exit web_crash "$EXIT_CODE" "前端进程异常退出"
        break # 前端真故障：结束容器
    fi
    if [ "$EXITED_PID" = "$WATCHDOG_PID" ]; then
        if [ "$EXIT_CODE" -eq "$HEALTH_WATCHDOG_EXIT_CODE" ]; then
            echo "[entrypoint] 完整健康链路持续失败，停止容器以触发 Docker 重启。" >&2
            EXIT_CODE=1
            record_failure_exit watchdog_unhealthy "$EXIT_CODE" "完整健康链路连续失败，容器主动退出等待自动拉起"
        else
            echo "[entrypoint] 健康看门狗意外退出（exit=${EXIT_CODE}），停止容器。" >&2
            if [ "$EXIT_CODE" -eq 0 ]; then
                EXIT_CODE=1
            fi
            record_failure_exit supervisor_error "$EXIT_CODE" "健康看门狗意外退出"
        fi
        break
    fi
    # wait -n 被信号打断且没有退出 PID；走统一停机收尾。
    if [ "$SHUTTING_DOWN" -eq 1 ]; then
        break
    fi
    echo "[entrypoint] 无法确定退出的子进程，停止容器。" >&2
    EXIT_CODE=1
    record_failure_exit supervisor_error "$EXIT_CODE" "无法确定退出的子进程"
    break
done
echo "[entrypoint] 有进程退出（exit=${EXIT_CODE}），停止容器……"
terminate_managed_processes
exit "$EXIT_CODE"
