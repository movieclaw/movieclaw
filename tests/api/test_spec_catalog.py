"""spec 目录视图与「服务端不依赖 CLI」的守护（docs/design/cli-go-migration.md Stage 0）。

服务端曾经直接 `import movieclaw_cli` 来拿域清单。那是方向错误的依赖：被 import
的函数做的是读 spec.json，与命令行客户端无关，却让服务端在运行期依赖了一个客户端
包——CLI 换语言时立刻变成硬阻塞（后来确实换了，见 cli-go-migration.md）。
这里把「不许再依赖回去」钉死：CLI 现在是 Go 二进制，服务端只能以子进程调用它。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from movieclaw_api.services import spec_catalog
from movieclaw_api.services.mclaw_tool import render_service_map, spec_domains

_SRC = Path(__file__).resolve().parents[2] / "src"


def _top_level_imports(package: str) -> set[str]:
    modules: set[str] = set()
    for path in (_SRC / package).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return modules


def test_no_python_cli_package_remains() -> None:
    """Python CLI 已退役。留着一份会分叉：改了这边、Go 那边不动，命令面就对不上。"""
    assert not (_SRC / "movieclaw_cli").exists(), (
        "src/movieclaw_cli 又出现了。CLI 现在是 cli/ 下的 Go 二进制，"
        "需要 spec 信息请走 services.spec_catalog；需要执行命令请起子进程。"
    )


def test_server_packages_do_not_import_any_cli_package() -> None:
    """服务端与 Agent 都不 import CLI。方向反了就没法独立换实现。"""
    for package in ("movieclaw_api", "movieclaw_agent"):
        assert "movieclaw_cli" not in _top_level_imports(package), (
            f"{package} 又 import 了 movieclaw_cli"
        )


# ---------------------------------------------------------------------------
# 目录视图本身
# ---------------------------------------------------------------------------


def test_command_operations_exclude_hidden_and_stream() -> None:
    """判定口径与 CLI 生成器一致：hidden 是 Web 基础设施，stream 由精选层手写。"""
    spec = {
        "paths": {
            "/a": {"get": {"operationId": "domain.a"}},
            "/b": {"get": {"operationId": "domain.b", "x-cli-hidden": True}},
            "/c": {"get": {"operationId": "domain.c", "x-cli-stream": "done"}},
            "/d": {"get": {"summary": "没有 operationId"}},
            # 非 HTTP 方法的键（parameters 等）不能被当成操作
            "/e": {"parameters": [], "post": {"operationId": "other.e"}},
        }
    }
    ids = {op["operation_id"] for op in spec_catalog.iter_command_operations(spec)}
    assert ids == {"domain.a", "other.e"}


def test_real_spec_yields_a_sane_catalog() -> None:
    """对真实基线 spec 跑一遍，避免判定逻辑写对了但读不到文件。"""
    ops = spec_catalog.iter_command_operations(spec_catalog.load_spec())
    assert len(ops) > 100, "基线 spec 里的命令操作数量异常，检查导出是否完整"
    domains = spec_catalog.command_domains()
    assert {"subscriptions", "library", "search", "auth"} <= domains


def test_missing_spec_gives_an_actionable_error(monkeypatch, tmp_path) -> None:
    """产物不完整时必须给可读结论，不能变成一句裸的 internal server error。"""
    monkeypatch.setattr(spec_catalog, "_SPEC_PATH", tmp_path / "nope.json")
    with pytest.raises(spec_catalog.SpecCatalogUnavailable) as excinfo:
        spec_catalog.load_spec()
    assert excinfo.value.code == "SPEC_BASELINE_MISSING"
    assert "镜像或安装包不完整" in excinfo.value.message


def test_service_map_covers_every_open_domain() -> None:
    """渲染出的目录与 spec 的开放域集合严格同步（新增域忘了润色会被这里拦下）。"""
    rendered = render_service_map()
    for domain in spec_domains():
        assert f"- {domain}" in rendered, f"域 {domain} 没有出现在服务目录里"
