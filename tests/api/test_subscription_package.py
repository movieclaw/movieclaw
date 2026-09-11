"""订阅域子包的边界守护测试。

规则：包外（src/movieclaw_api 下、subscription 包之外）不允许直接 import
子包内部模块（core/matching/dispatch/wanted_search/wanted_fulfillment/health/
release_forecast/replacement/disproven/identity_audit），
只能从 ``movieclaw_api.services.subscription`` 公共接口导入。守住目录边界，
防止环形依赖以"再捅一个内部模块"的方式复发。
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "movieclaw_api"
_PACKAGE = _SRC / "services" / "subscription"
_INTERNAL = re.compile(
    r"movieclaw_api\.services\.subscription\."
    r"(core|matching|dispatch|wanted_search|wanted_fulfillment|health|release_forecast"
    r"|replacement|disproven|identity_audit)\b"
)


def test_no_external_import_of_subscription_internals() -> None:
    violations: list[str] = []
    for path in _SRC.rglob("*.py"):
        if _PACKAGE in path.parents:
            continue  # 包内互引不受限
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            # 只查 import 语句；注释/文档字符串里的路径引用不算违规
            if not (stripped.startswith("from ") or stripped.startswith("import ")):
                continue
            if _INTERNAL.search(stripped):
                violations.append(f"{path.relative_to(_SRC)}:{lineno}: {stripped}")
    assert not violations, "包外禁止直捅订阅子包内部模块，请改从公共接口导入：\n" + "\n".join(
        violations
    )
