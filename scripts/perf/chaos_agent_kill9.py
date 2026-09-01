"""真机混沌演练：工具执行中 kill -9 API 进程 → 重启 → 验证自愈与续聊。

对应 docs/design/agent-runtime-resilience.md 验收标准 1（kill -9 恢复）。
不进 CI，本机手动跑：`.venv/bin/python scripts/perf/chaos_agent_kill9.py`。
运行数据落在系统临时目录，用真实 uvicorn 进程 + 假 OpenAI 兼容端点
（chaos_agent_mock_llm.py），不需要真实模型 Key。
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
CHAOS = Path(tempfile.mkdtemp(prefix="movieclaw-chaos-"))
MOCK_LLM = Path(__file__).with_name("chaos_agent_mock_llm.py")
PY = sys.executable
API = "http://127.0.0.1:18801/api/v1"
MARKER = "movieclaw-chaos-marker"

ENV = {
    **os.environ,
    "DATABASE_URL": f"sqlite+aiosqlite:///{CHAOS}/data/chaos.db",
    "SECRET_KEY_FILE": str(CHAOS / "data" / ".secret_key"),
    "AGENT_SESSIONS_DIR": str(CHAOS / "data" / "agent-sessions"),
    "SCHEDULER_ENABLED": "0",
}


def start_api(log_name: str) -> subprocess.Popen:
    # 句柄随子进程存续，交给进程退出时回收，不用 with
    log = open(CHAOS / log_name, "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            PY, "-m", "uvicorn", "movieclaw_api.main:app", "--factory",
            "--host", "127.0.0.1", "--port", "18801",
        ],
        cwd=CHAOS,
        env=ENV,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{API}/health", timeout=2)
            if r.status_code == 200:
                return proc
        except Exception:
            pass
        if proc.poll() is not None:
            sys.exit(f"API 进程提前退出，见 {log_name}")
        time.sleep(0.3)
    sys.exit("API 未在期限内就绪")


def wait_marker(present: bool, timeout: float = 20) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(["pgrep", "-f", MARKER], capture_output=True, text=True).stdout
        pids = out.split()
        if bool(pids) == present:
            return pids
        time.sleep(0.2)
    sys.exit(f"标记进程未在期限内{'出现' if present else '消失'}")


def wait_settled(client: httpx.Client, sid: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = client.get(f"{API}/sessions/{sid}").json()["data"]["session"]
        if not item["running"]:
            return
        time.sleep(0.3)
    sys.exit("会话运行状态未清空")


def main() -> None:
    (CHAOS / "data").mkdir(parents=True, exist_ok=True)
    print(f"运行目录：{CHAOS}")
    mock = subprocess.Popen([PY, str(MOCK_LLM)])
    api = start_api("api-round1.log")
    try:
        with httpx.Client(timeout=15) as c:
            # 初始化管理员并登录（cookie 会话）
            r = c.post(
                f"{API}/auth/bootstrap",
                json={"username": "admin", "password": "chaos-test-1"},
            )
            assert r.status_code == 200, r.text
            r = c.put(
                f"{API}/llm/provider",
                json={
                    "provider_type": "openai_compat",
                    "base_url": "http://127.0.0.1:18802/v1",
                    "api_key": "sk-chaos",
                    "default_model": "mock-model",
                    "extra_models": [
                        {"id": "mock-model", "context_window": 100000, "max_output_tokens": 8000}
                    ],
                },
            )
            assert r.status_code == 200, r.text
            r = c.post(f"{API}/sessions", json={"content": "执行一个长任务"})
            assert r.status_code == 202, r.text
            sid = r.json()["data"]["session_id"]
            print(f"[1] 会话已启动 session={sid}")
            wait_marker(present=True)
            print("[2] bash 子进程已在执行（sleep 挂起中），现在 kill -9 API 进程")

            os.kill(api.pid, signal.SIGKILL)
            api.wait()
            # 硬崩没有收尾机会：子进程按文档边界残留（README 已注明），手工清场
            leftover = wait_marker(present=True, timeout=5)
            print(f"[3] 确认硬崩后子进程残留（文档声明的裸机边界）：pids={leftover}")
            for pid in leftover:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid), signal.SIGKILL)

            transcript = (CHAOS / "data" / "agent-sessions" / f"{sid}.jsonl").read_text()
            assert '"tool_calls"' in transcript and "call_chaos" in transcript
            assert "服务重启或异常" not in transcript, "硬崩时不应有收尾写入"
            print("[4] 硬崩现场确认：转录中 tool_call 无回执（孤儿）")

        api = start_api("api-round2.log")
        log2 = (CHAOS / "api-round2.log").read_text()
        assert "工具调用在执行中被中断，已补写回执" in log2, "启动自愈日志缺失"
        print("[5] 重启完成，启动自愈日志已出现")

        with httpx.Client(timeout=15) as c:
            r = c.post(f"{API}/auth/login", json={"username": "admin", "password": "chaos-test-1"})
            assert r.status_code == 200, r.text
            entries = c.get(f"{API}/sessions/{sid}").json()["data"]["entries"]
            roles = [e["message"]["role"] for e in entries]
            assert roles == ["user", "assistant", "tool"], roles
            receipt = entries[-1]["message"]
            assert receipt["tool_call_id"] == "call_chaos"
            assert "服务重启或异常" in receipt["content"]
            assert "结果未知" in receipt["content"]
            print("[6] 转录已补配对：service_interrupted 回执就位")

            # 30 秒心跳窗自愈之前，running 可能仍为 True；等它清空后直接续聊
            wait_settled(c, sid, timeout=45)
            print("[7] 运行状态已自愈为已结束")
            r = c.post(f"{API}/sessions", json={"content": "继续任务", "session_id": sid})
            assert r.status_code == 202, r.text
            wait_settled(c, sid)
            entries = c.get(f"{API}/sessions/{sid}").json()["data"]["entries"]
            last = entries[-1]["message"]
            assert last["role"] == "assistant", entries
            assert "已确认状态" in (last["content"] or ""), last
            print("[8] 续聊成功：模型基于中断上下文给出终答 →", last["content"])
        print("CHAOS-OK")
    finally:
        for proc in (api, mock):
            if proc.poll() is None:
                proc.kill()
        subprocess.run(["pkill", "-9", "-f", MARKER], capture_output=True)


if __name__ == "__main__":
    main()
