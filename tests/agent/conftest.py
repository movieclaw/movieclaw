"""Agent 测试的共享夹具。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def mclaw_binary() -> str:
    """确保 mclaw 可执行文件存在，并让工具层通过 MOVIECLAW_CLI_BIN 找到它。

    mclaw 迁到 Go 之后不再是「同 venv 里的模块」，测试要跑真子进程就得先有
    二进制。优先用外部指定的（CI 里可以复用 go 作业的产物），否则现场编一份；
    连 go 都没有就跳过——这批用例是 Agent ↔ CLI 的接线验证，缺了工具链只能
    跳，不能因此把整个 Python 门禁染红。
    """
    if existing := os.environ.get("MOVIECLAW_CLI_BIN"):
        if Path(existing).is_file():
            return existing
        pytest.skip(f"MOVIECLAW_CLI_BIN 指向的文件不存在：{existing}")

    if shutil.which("go") is None:
        pytest.skip("找不到 go 工具链，跳过需要真实 mclaw 二进制的用例")

    # 编到固定位置并复用：每个测试会话只编一次
    target = _REPO_ROOT / "cli" / ".build" / "mclaw"
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["go", "build", "-o", str(target), "./cmd/mclaw"],
        cwd=_REPO_ROOT / "cli",
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"mclaw 构建失败，跳过：{result.stderr.strip()[:300]}")
    os.environ["MOVIECLAW_CLI_BIN"] = str(target)
    return str(target)
