"""基线 spec 的漂移守护（docs/design/cli.md §2.1）。

同一份 spec 有两个消费方：服务端运行期读它渲染 Agent 的 mclaw 工具描述，
Go CLI 构建期把它嵌进二进制。改了路由却忘记重新导出，模型看到的服务目录
就会和 CLI 实际能跑的命令对不上——这里直接红，并给出修复命令。
"""

from __future__ import annotations

from pathlib import Path

from movieclaw_api.export_openapi import build_spec, spec_hash
from movieclaw_api.services.spec_catalog import _SPEC_PATH, load_spec

_CLI_COPY = Path(__file__).resolve().parents[2] / "cli" / "internal" / "spec" / "data" / "spec.json"

_FIX = "请重新导出：scripts/export-spec.sh"


def test_baseline_spec_matches_code() -> None:
    assert spec_hash(load_spec()) == spec_hash(build_spec()), f"基线 spec 与当前代码不一致，{_FIX}"


def test_cli_embedded_copy_matches_server_copy() -> None:
    """Go CLI 内嵌的那份与服务端读的那份必须逐字节一致。"""
    assert _CLI_COPY.is_file(), f"缺少 Go CLI 的内嵌基线（{_CLI_COPY}），{_FIX}"
    assert _CLI_COPY.read_bytes() == _SPEC_PATH.read_bytes(), (
        f"Go CLI 内嵌基线与服务端基线不一致，{_FIX}"
    )
