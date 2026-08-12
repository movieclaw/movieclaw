"""容器入口的进程监督测试。

这里不依赖 Docker：临时目录中的假 API 与假 Next 进程分别模拟后端监听、
启动即退出、永久卡住和健康接口退化。测试的是 entrypoint 的真实子进程关系，
从而锁住「前端不得在后端未就绪时启动」这一部署契约。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"


def _write_executable(path: Path, source: str) -> None:
    """写入临时进程替身，并赋予可执行权限。"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


_FAKE_API = """#!{python}
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] in ("-c", "-"):
    os.execv({python!r}, [{python!r}, *sys.argv[1:]])

mode = os.environ["FAKE_API_MODE"]
count_file = os.environ.get("FAKE_API_START_COUNT")
if count_file:
    path = Path(count_file)
    count = int(path.read_text(encoding="utf-8")) if path.exists() else 0
    path.write_text(str(count + 1), encoding="utf-8")
if mode == "exit":
    raise SystemExit(int(os.environ.get("FAKE_API_EXIT_CODE", "17")))
if mode == "hang":
    while True:
        time.sleep(1)
if mode == "restart-once":
    state = Path(os.environ["FAKE_API_STATE_FILE"])
    if not state.exists():
        state.touch()
        raise SystemExit(42)
if mode == "restart-twice":
    state = Path(os.environ["FAKE_API_STATE_FILE"])
    count = int(state.read_text(encoding="utf-8")) if state.exists() else 0
    if count < 2:
        state.write_text(str(count + 1), encoding="utf-8")
        raise SystemExit(42)
if mode == "restart-after-ready-once":
    state = Path(os.environ["FAKE_API_STATE_FILE"])
    if not state.exists():
        state.touch()
        threading.Timer(
            float(os.environ.get("FAKE_API_RESTART_DELAY_SECONDS", "3")),
            lambda: os._exit(42),
        ).start()
if mode == "exit-after-ready":
    threading.Timer(
        float(os.environ.get("FAKE_API_EXIT_AFTER_READY_SECONDS", "2")),
        lambda: os._exit(int(os.environ.get("FAKE_API_EXIT_CODE", "17"))),
    ).start()

delay = float(os.environ.get("FAKE_API_DELAY_SECONDS", "0"))
if delay:
    time.sleep(delay)
failed_after = float(os.environ.get("FAKE_API_FAIL_AFTER_SECONDS", "0"))
started = time.monotonic()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        failed = failed_after and time.monotonic() - started >= failed_after
        self.send_response(500 if failed else 200)
        self.end_headers()
        self.wfile.write(b"failed" if failed else b"ok")

    def log_message(self, format, *args):
        pass

ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
"""


_FAKE_WEB = """#!{python}
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

delay = float(os.environ.get("FAKE_WEB_DELAY_SECONDS", "0"))
if delay:
    time.sleep(delay)

Path(os.environ["FAKE_WEB_MARKER"]).write_text(str(time.monotonic()), encoding="utf-8")
count_file = os.environ.get("FAKE_WEB_START_COUNT")
if count_file:
    path = Path(count_file)
    count = int(path.read_text(encoding="utf-8")) if path.exists() else 0
    path.write_text(str(count + 1), encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with urlopen("http://127.0.0.1:8000/api/v1/health", timeout=0.5) as response:
                status = response.status
        except (OSError, URLError):
            status = 502
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass

ThreadingHTTPServer(("127.0.0.1", 3000), Handler).serve_forever()
"""


@pytest.fixture()
def entrypoint_env(tmp_path: Path) -> dict[str, str]:
    """准备最小镜像布局与可控的 API/前端进程替身。"""
    app_root = tmp_path / "app"
    (app_root / "web" / "apps" / "web").mkdir(parents=True)
    (app_root / "web" / "apps" / "web" / "server.js").touch()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    runtime_file = tmp_path / "runtime"
    runtime_file.write_text("6\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "fake-api",
        _FAKE_API.format(python=sys.executable),
    )
    _write_executable(
        bin_dir / "node",
        _FAKE_WEB.format(python=sys.executable),
    )

    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "MOVIECLAW_APP_ROOT": str(app_root),
        "MOVIECLAW_DATA_DIR": str(data_dir),
        "MOVIECLAW_RUNTIME_FILE": str(runtime_file),
        "MOVIECLAW_PYTHON_BIN": str(bin_dir / "fake-api"),
        # 模拟进程需经历 shell 调度和一次真实 HTTP 请求；留出余量避免把
        # 「验证启动顺序」的测试写成三秒竞速。
        "MOVIECLAW_API_STARTUP_TIMEOUT_SECONDS": "6",
        "MOVIECLAW_WEB_STARTUP_TIMEOUT_SECONDS": "6",
        "MOVIECLAW_API_RESTART_TIMEOUT_SECONDS": "3",
        "MOVIECLAW_HEALTHCHECK_INTERVAL_SECONDS": "1",
        "MOVIECLAW_HEALTHCHECK_FAILURE_THRESHOLD": "2",
        "MOVIECLAW_SHUTDOWN_GRACE_SECONDS": "1",
        "FAKE_WEB_MARKER": str(tmp_path / "web-started"),
        "FAKE_WEB_START_COUNT": str(tmp_path / "web-start-count"),
        "FAKE_API_START_COUNT": str(tmp_path / "api-start-count"),
    }


def _start_entrypoint(env: dict[str, str]) -> subprocess.Popen[str]:
    """启动入口脚本，合并输出便于失败断言与清理。"""
    return subprocess.Popen(
        ["bash", str(_ENTRYPOINT)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _wait_for_file(path: Path, timeout: float = 8) -> None:
    """等待前端启动标记，超时时给出清晰的测试失败。"""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"等待启动标记超时：{path}")
        time.sleep(0.05)


def _wait_for_value(path: Path, value: int, timeout: float = 8) -> None:
    """等待进程计数到达期望值，确认重启真的发生而不是仅写入状态文件。"""
    deadline = time.monotonic() + timeout
    while not path.exists() or int(path.read_text(encoding="utf-8")) < value:
        if time.monotonic() >= deadline:
            raise AssertionError(f"等待计数 {value} 超时：{path}")
        time.sleep(0.05)


def _stop(process: subprocess.Popen[str]) -> str:
    """无论断言是否失败都回收入口脚本及其子进程。"""
    if process.poll() is None:
        process.terminate()
    try:
        output, _ = process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=5)
    return output


def _http_get(url: str, timeout: float = 1.0) -> tuple[int, str]:
    """GET 一次并返回 (状态码, 响应体)；非 2xx 也正常返回而不是抛异常。"""
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _http_get_retry(url: str, timeout: float = 5) -> tuple[int, str]:
    """轮询直到端口有人应答（无论状态码），用于等待占位页或前端开始监听。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _http_get(url)
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise AssertionError(f"等待 {url} 有响应超时") from exc
            time.sleep(0.05)


def _wait_for_http_ok(url: str, timeout: float = 8) -> None:
    """轮询直到健康接口返回 2xx，确认完整链路已恢复。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            status, _ = _http_get(url)
            if 200 <= status < 300:
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise AssertionError(f"等待 {url} 恢复健康超时")
        time.sleep(0.1)


def _read_last_exit(env: dict[str, str]) -> dict:
    """读 entrypoint 落盘的异常退出记录（自愈事件外显的数据源）。"""
    path = Path(env["MOVIECLAW_DATA_DIR"]) / "updates" / "state" / "last-exit.json"
    assert path.is_file(), "异常退出后应写入 last-exit.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_web_starts_only_after_api_is_ready(entrypoint_env: dict[str, str]) -> None:
    """后端在 lifespan 中延迟时，前端不得提前监听或开始反代。"""
    entrypoint_env["FAKE_API_MODE"] = "serve"
    entrypoint_env["FAKE_API_DELAY_SECONDS"] = "2"
    marker = Path(entrypoint_env["FAKE_WEB_MARKER"])
    started = time.monotonic()
    process = _start_entrypoint(entrypoint_env)
    try:
        try:
            _wait_for_file(marker)
        except AssertionError:
            pytest.fail(_stop(process))
        assert float(marker.read_text(encoding="utf-8")) - started >= 1.7
        assert process.poll() is None
    finally:
        _stop(process)


def test_placeholder_page_serves_during_startup(entrypoint_env: dict[str, str]) -> None:
    """后端就绪前 3000 由占位页顶住：页面 200、/api 503，就绪后让位给真前端。"""
    entrypoint_env["FAKE_API_MODE"] = "serve"
    entrypoint_env["FAKE_API_DELAY_SECONDS"] = "3"
    marker = Path(entrypoint_env["FAKE_WEB_MARKER"])
    process = _start_entrypoint(entrypoint_env)
    try:
        status, body = _http_get_retry("http://127.0.0.1:3000/", timeout=2.5)
        assert status == 200
        assert "正在启动" in body
        api_status, _ = _http_get("http://127.0.0.1:3000/api/v1/health")
        assert api_status == 503
        _wait_for_file(marker)
        # 占位页已让位：健康接口最终穿透真前端反代到后端返回 2xx
        _wait_for_http_ok("http://127.0.0.1:3000/api/v1/health")
        assert process.poll() is None
    finally:
        _stop(process)


def test_api_exit_before_ready_never_starts_web(entrypoint_env: dict[str, str]) -> None:
    """后端启动即失败时，容器返回原退出码，前端完全不会启动。"""
    entrypoint_env["FAKE_API_MODE"] = "exit"
    entrypoint_env["FAKE_API_EXIT_CODE"] = "17"
    marker = Path(entrypoint_env["FAKE_WEB_MARKER"])
    process = _start_entrypoint(entrypoint_env)
    output, _ = process.communicate(timeout=8)

    assert process.returncode == 17
    assert not marker.exists()
    assert "后端 在就绪前退出（exit=17）" in output
    record = _read_last_exit(entrypoint_env)
    assert record["reason"] == "startup_failure"
    assert record["exit_code"] == 17


def test_api_readiness_timeout_never_starts_web(entrypoint_env: dict[str, str]) -> None:
    """后端进程仍活着却不监听时，入口超时退出而非遗留半死容器。"""
    entrypoint_env["FAKE_API_MODE"] = "hang"
    entrypoint_env["MOVIECLAW_API_STARTUP_TIMEOUT_SECONDS"] = "2"
    marker = Path(entrypoint_env["FAKE_WEB_MARKER"])
    process = _start_entrypoint(entrypoint_env)
    output, _ = process.communicate(timeout=10)

    assert process.returncode == 124
    assert not marker.exists()
    assert "等待 后端 就绪超过 2s" in output


def test_api_exit_during_web_readiness_stops_web_immediately(
    entrypoint_env: dict[str, str],
) -> None:
    """后端在前端监听前退出时，不能留前端独自等待反代超时。"""
    entrypoint_env["FAKE_API_MODE"] = "exit-after-ready"
    entrypoint_env["FAKE_API_EXIT_AFTER_READY_SECONDS"] = "2"
    # 将前端监听延迟到后端退出之后：旧实现会继续等满前端启动超时并写下标记；
    # 新实现应立即终止它，因此外部端口不会进入无后端可用的监听状态。
    entrypoint_env["FAKE_WEB_DELAY_SECONDS"] = "5"
    marker = Path(entrypoint_env["FAKE_WEB_MARKER"])
    process = _start_entrypoint(entrypoint_env)
    output, _ = process.communicate(timeout=8)

    assert process.returncode == 17
    assert not marker.exists()
    assert "后端 在前端反代就绪前退出（exit=17）" in output


def test_unhealthy_full_proxy_chain_exits_container(entrypoint_env: dict[str, str]) -> None:
    """运行后 API 仍存活但健康接口失败时，看门狗必须让容器失败退出。"""
    entrypoint_env["FAKE_API_MODE"] = "serve"
    # 先给 API 与前端代理充分的启动余量，确保失败发生在运行期而非启动门禁内。
    entrypoint_env["FAKE_API_FAIL_AFTER_SECONDS"] = "5"
    process = _start_entrypoint(entrypoint_env)
    output, _ = process.communicate(timeout=12)

    assert process.returncode == 1, output
    assert "完整健康链路连续失败" in output
    # 失败日志必须带可读的原因与分段归因：假 API 返回 500，直连同样失败，
    # 应归因到后端而不是前端反代。
    assert "原因：" in output
    assert "后端直连亦失败" in output
    record = _read_last_exit(entrypoint_env)
    assert record["reason"] == "watchdog_unhealthy"


def test_restart_code_before_ready_is_startup_failure(entrypoint_env: dict[str, str]) -> None:
    """42 在首次健康前没有合法来源，必须作为启动失败外显。"""
    entrypoint_env["FAKE_API_MODE"] = "restart-once"
    entrypoint_env["FAKE_API_STATE_FILE"] = str(
        Path(entrypoint_env["FAKE_WEB_MARKER"]).with_name("api-restarted")
    )
    process = _start_entrypoint(entrypoint_env)
    output, _ = process.communicate(timeout=8)

    assert process.returncode == 42
    assert not Path(entrypoint_env["FAKE_WEB_MARKER"]).exists()
    assert "启动阶段失败（exit=42）" in output


def test_startup_restart_codes_mark_bad_overlay_and_fall_back(
    entrypoint_env: dict[str, str],
) -> None:
    """坏 overlay 连续以 42 退出时，必须被标坏并回落到镜像基线。"""
    data_dir = Path(entrypoint_env["MOVIECLAW_DATA_DIR"])
    overlay = data_dir / "updates" / "versions" / "v1.0.0"
    (overlay / "backend" / "src" / "movieclaw_api").mkdir(parents=True)
    (overlay / "backend" / "src" / "movieclaw_api" / "main.py").touch()
    (overlay / "backend" / "alembic.ini").touch()
    (overlay / "web" / "apps" / "web").mkdir(parents=True)
    (overlay / "web" / "apps" / "web" / "server.js").touch()
    (overlay / "manifest.json").write_text(
        json.dumps({"schema": 1, "version": "1.0.0", "requires_runtime": 6}),
        encoding="utf-8",
    )
    current = data_dir / "updates" / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(overlay)

    entrypoint_env["FAKE_API_MODE"] = "restart-twice"
    entrypoint_env["FAKE_API_STATE_FILE"] = str(data_dir / "restart-count")
    marker = Path(entrypoint_env["FAKE_WEB_MARKER"])
    process = _start_entrypoint(entrypoint_env)
    try:
        _wait_for_file(marker)
        assert process.poll() is None
    finally:
        output = _stop(process)

    assert (data_dir / "updates" / "state" / "bad-1.0.0").is_file()
    assert "标记为坏版本并回落" in output


def test_runtime_restart_keeps_web_alive(entrypoint_env: dict[str, str]) -> None:
    """运行中的 42 只重启后端：前端进程保持不动，链路恢复后继续对外服务。"""
    entrypoint_env["FAKE_API_MODE"] = "restart-after-ready-once"
    entrypoint_env["FAKE_API_STATE_FILE"] = str(
        Path(entrypoint_env["FAKE_WEB_MARKER"]).with_name("api-runtime-restarted")
    )
    marker = Path(entrypoint_env["FAKE_WEB_MARKER"])
    api_count = Path(entrypoint_env["FAKE_API_START_COUNT"])
    web_count = Path(entrypoint_env["FAKE_WEB_START_COUNT"])
    process = _start_entrypoint(entrypoint_env)
    try:
        _wait_for_file(marker)
        _wait_for_value(api_count, 2)
        # 新后端就绪、完整链路恢复后，前端必须还是最初那一个进程
        _wait_for_http_ok("http://127.0.0.1:3000/api/v1/health")
        assert int(web_count.read_text(encoding="utf-8")) == 1
        assert process.poll() is None
    finally:
        output = _stop(process)

    assert "保持前端运行" in output
