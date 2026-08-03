"""命令树快照守护（docs/design/cli.md §3.2「没有静默的第三条路」）。

三条保证：
1. 生成命令集合与快照文件一致——路由增删改必然反映为快照 diff，
   评审时一目了然；新端点没被生成器认出来也会在这里现形。
2. 不生成命令的端点必须且只能是已知豁免（x-cli-hidden 的 Web 基础设施
   与 x-cli-stream 的 SSE 端点）——未知形态强制显式处理。
3. API 参数名不与 CLI 全局标志/内置标志重名，避免命令失去这些标志。
"""

from __future__ import annotations

from pathlib import Path

from movieclaw_cli.gen.spec_loader import load_baseline
from movieclaw_cli.gen.tree_builder import (
    RESERVED_PARAM_NAMES,
    generated_command_paths,
    is_generable,
    iter_operations,
)

_SNAPSHOT = Path(__file__).parent / "command_tree_snapshot.txt"

# 唯二允许不生成命令的类别：x-cli-hidden（Web 基础设施 / 由精选命令承担语义
# 的端点）、x-cli-stream（SSE 流，P2 精选层接入）
KNOWN_NON_GENERATED = {
    "images.asset",
    "images.proxy",
    "system.spec",
    "auth.login",  # 精选命令 mclaw login 负责（要持久化本地凭证）
    "auth.logout",  # 精选命令 mclaw logout 负责
    "search.stream",
    "agent.runs.stream",
    "fs.browse",  # 仅 Web 端目录选择器用；CLI/Agent 有 bash 等通用工具，不再暴露
    "libraries.cover",  # 库封面拼贴图（二进制响应，Web/Jellyfin 双端消费），CLI 无用途
}


def test_command_tree_matches_snapshot() -> None:
    expected = _SNAPSHOT.read_text(encoding="utf-8").splitlines()
    actual = generated_command_paths(load_baseline())
    assert actual == expected, (
        "生成命令树与快照不一致。确认属预期变更后更新快照：\n"
        '  .venv/bin/python -c "from movieclaw_cli.gen.spec_loader import load_baseline; '
        "from movieclaw_cli.gen.tree_builder import generated_command_paths; "
        "open('tests/cli/command_tree_snapshot.txt','w').write("
        "'\\n'.join(generated_command_paths(load_baseline()))+'\\n')\""
    )


def test_api_params_do_not_shadow_cli_flags() -> None:
    clashes = sorted(
        f"{op['operation_id']}: {p['name']}"
        for op in iter_operations(load_baseline())
        for p in op["params"]
        if p["name"] in RESERVED_PARAM_NAMES
    )
    body_clashes = sorted(
        f"{op['operation_id']}: body.{f['name']}"
        for op in iter_operations(load_baseline())
        for f in op["body_fields"]
        if f["name"] in RESERVED_PARAM_NAMES
    )
    assert not clashes + body_clashes, (
        f"以下端点参数与 CLI 内置标志重名，请在路由侧改名：{clashes + body_clashes}"
    )


def test_non_generated_endpoints_are_all_known() -> None:
    non_generated = {
        op["operation_id"] for op in iter_operations(load_baseline()) if not is_generable(op)
    }
    assert non_generated == KNOWN_NON_GENERATED, (
        f"不生成命令的端点集合发生变化：多出 {non_generated - KNOWN_NON_GENERATED}，"
        f"少了 {KNOWN_NON_GENERATED - non_generated}。新端点要么进生成范围，"
        "要么标注 x-cli-hidden/x-cli-stream 并在此登记"
    )


def test_dangerous_and_long_task_flow_into_commands() -> None:
    """x-cli 标注确实驱动了命令行为：危险端点带 ⚠ 提示，长任务端点带 --wait。"""
    ops = {op["operation_id"]: op for op in iter_operations(load_baseline())}
    assert ops["lib.items.delete"]["dangerous"] == "destructive"
    assert ops["sub.delete"]["dangerous"] == "confirm"
    assert ops["lib.scan.start"]["long_task"]["progress_op"] == "lib.show"
    assert ops["lib.refresh.start"]["long_task"]["done_field"] == "refreshing"
