"""movieclaw-cli 独立发行包的契约守护（docs/design/device-auth.md §6.4）。

拆包的全部价值在于「装 CLI 不用拖服务端那几十个重依赖」。这个价值很容易被
悄悄破坏——CLI 里随手 import 一个 pandas，或者从 movieclaw_api 借一个常量，
构建出来的包就装不起来了，而且要等用户装的时候才发现。

这里把两条不变量钉死：
1. CLI 只 import 标准库 + 独立包声明的那几个第三方依赖；
2. CLI 不 import 任何服务端模块。
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_ROOT = _REPO_ROOT / "src" / "movieclaw_cli"
_DIST_PYPROJECT = _REPO_ROOT / "packaging" / "cli" / "pyproject.toml"


#: 发行包名与 import 名不一致的少数情况。显式列出比自动推断可靠——
#: 自动推断要么装包，要么维护一份更大的猜测表。
_IMPORT_ALIASES = {"pyyaml": {"yaml"}}


def _declared_dependencies() -> set[str]:
    """独立包声明的第三方依赖，换算成它们提供的 import 名。"""
    spec = tomllib.loads(_DIST_PYPROJECT.read_text(encoding="utf-8"))
    names: set[str] = set()
    for raw in spec["project"]["dependencies"]:
        name = raw.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip().lower()
        names |= _IMPORT_ALIASES.get(name, {name.replace("-", "_")})
    return names


def _imported_top_level_modules() -> set[str]:
    """CLI 源码里所有顶层 import 的模块名。"""
    modules: set[str] = set()
    for path in _CLI_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return modules


def test_cli_only_imports_stdlib_and_declared_dependencies() -> None:
    allowed = (
        _declared_dependencies() | set(sys.stdlib_module_names) | {"movieclaw_cli"} | {"__future__"}
    )
    unexpected = {m for m in _imported_top_level_modules() if m.lower() not in allowed}
    assert not unexpected, (
        f"CLI 引入了独立发行包没有声明的依赖：{sorted(unexpected)}。"
        f"要么在 {_DIST_PYPROJECT.relative_to(_REPO_ROOT)} 里声明，"
        "要么不要在 CLI 里用——拆包的意义就是装它不用拖服务端的依赖。"
    )


def test_cli_does_not_import_server_packages() -> None:
    """CLI 是远程薄客户端：业务逻辑全在服务端，一行服务端代码都不该借。"""
    server_packages = {
        name.name
        for name in (_REPO_ROOT / "src").iterdir()
        if name.is_dir() and name.name.startswith("movieclaw_") and name.name != "movieclaw_cli"
    }
    borrowed = _imported_top_level_modules() & server_packages
    assert not borrowed, (
        f"CLI import 了服务端模块：{sorted(borrowed)}。"
        "这会让独立发行包装不起来（那些模块不在包里，依赖也没声明）。"
    )


def test_dist_package_version_tracks_the_app() -> None:
    """版本号从 movieclaw_api.__version__ 静态读取，不新增第四个手工同步点。

    发版规范只认三处一致（pyproject / __init__ / tag，见 CLAUDE.md）；独立包
    如果硬编码版本，就会变成第四处，迟早漂移。
    """
    spec = tomllib.loads(_DIST_PYPROJECT.read_text(encoding="utf-8"))
    assert "version" not in spec["project"], "独立包不应硬编码 version"
    assert spec["project"]["dynamic"] == ["version"]
    assert spec["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "movieclaw_api.__version__"}


def test_dist_package_ships_the_baseline_spec() -> None:
    """内置基线 spec 必须随包分发，否则断网时连 --help 都没有命令树。"""
    spec = tomllib.loads(_DIST_PYPROJECT.read_text(encoding="utf-8"))
    package_data = spec["tool"]["setuptools"]["package-data"]
    assert "data/spec.json" in package_data["movieclaw_cli"]
    assert (_CLI_ROOT / "data" / "spec.json").is_file()
