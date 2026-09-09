"""entrypoint 的 overlay 解析矩阵（docs/design/in-app-update.md M2）。

entrypoint.sh 提供 `resolve` 测试钩子：只解析启动指向并打印 key=value，
不拉起任何进程；APP_ROOT / DATA_DIR / RUNTIME_FILE 均可用环境变量覆盖，
因此可以在临时目录里完整验证解析逻辑——这是应用内更新的安全地基，
坏 overlay 在任何情况下都必须回落到可运行的版本。
"""

from __future__ import annotations

import json
import platform
import socket
import subprocess
import sys
from pathlib import Path

import pytest

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"

# 镜像的 runtime 版本，测试里统一用 1
_RUNTIME = "1"


@pytest.fixture()
def env_root(tmp_path: Path) -> Path:
    """搭一个最小的容器文件布局：app 根 + data 卷 + runtime 版本文件。"""
    (tmp_path / "app").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "runtime").write_text(_RUNTIME + "\n", encoding="utf-8")
    return tmp_path


def _make_overlay(
    root: Path,
    version: str,
    *,
    requires_runtime: int = 1,
    complete: bool = True,
    manifest_text: str | None = None,
) -> Path:
    """在 data 卷上摆一个 overlay 版本目录（布局与真实产物解包后一致）。"""
    vdir = root / "data" / "updates" / "versions" / f"v{version}"
    (vdir / "backend" / "src" / "movieclaw_api").mkdir(parents=True)
    (vdir / "backend" / "src" / "movieclaw_api" / "main.py").touch()
    (vdir / "backend" / "alembic.ini").touch()
    if complete:
        (vdir / "web" / "apps" / "web").mkdir(parents=True)
        (vdir / "web" / "apps" / "web" / "server.js").touch()
    if manifest_text is None:
        manifest_text = json.dumps(
            {"schema": 1, "version": version, "requires_runtime": requires_runtime}
        )
    (vdir / "manifest.json").write_text(manifest_text, encoding="utf-8")
    return vdir


def _link(root: Path, name: str, target: Path) -> None:
    link = root / "data" / "updates" / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def _resolve(root: Path, **extra_env: str) -> dict[str, str]:
    result = subprocess.run(
        ["bash", str(_ENTRYPOINT), "resolve"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "MOVIECLAW_APP_ROOT": str(root / "app"),
            "MOVIECLAW_DATA_DIR": str(root / "data"),
            "MOVIECLAW_RUNTIME_FILE": str(root / "runtime"),
            "MOVIECLAW_PYTHON_BIN": sys.executable,
            **extra_env,
        },
        check=True,
    )
    return dict(
        line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line
    )


def test_no_overlay_uses_baseline(env_root: Path) -> None:
    out = _resolve(env_root)
    assert out["source"] == "baseline"
    assert out["backend_root"] == str(env_root / "app")
    assert out["web_root"] == str(env_root / "app" / "web")


def test_valid_overlay_wins(env_root: Path) -> None:
    vdir = _make_overlay(env_root, "0.2.0")
    _link(env_root, "current", vdir)
    out = _resolve(env_root)
    assert out["source"] == "overlay"
    assert out["version"] == "0.2.0"
    assert out["backend_root"] == str(vdir / "backend")
    assert out["web_root"] == str(vdir / "web")


def test_runtime_mismatch_falls_back_to_baseline(env_root: Path) -> None:
    vdir = _make_overlay(env_root, "0.2.0", requires_runtime=2)
    _link(env_root, "current", vdir)
    assert _resolve(env_root)["source"] == "baseline"


def test_corrupt_manifest_falls_back_to_baseline(env_root: Path) -> None:
    vdir = _make_overlay(env_root, "0.2.0", manifest_text="{oops")
    _link(env_root, "current", vdir)
    assert _resolve(env_root)["source"] == "baseline"


def test_incomplete_layout_falls_back_to_baseline(env_root: Path) -> None:
    vdir = _make_overlay(env_root, "0.2.0", complete=False)
    _link(env_root, "current", vdir)
    assert _resolve(env_root)["source"] == "baseline"


def test_dangling_current_falls_back_to_baseline(env_root: Path) -> None:
    _link(env_root, "current", env_root / "data" / "updates" / "versions" / "v9.9.9")
    assert _resolve(env_root)["source"] == "baseline"


def test_bad_marker_skips_current_and_uses_previous(env_root: Path) -> None:
    bad = _make_overlay(env_root, "0.3.0")
    prev = _make_overlay(env_root, "0.2.0")
    _link(env_root, "current", bad)
    _link(env_root, "previous", prev)
    state = env_root / "data" / "updates" / "state"
    state.mkdir(parents=True)
    (state / "bad-0.3.0").touch()
    out = _resolve(env_root)
    assert out["source"] == "overlay"
    assert out["version"] == "0.2.0"
    assert out["backend_root"] == str(prev / "backend")


def test_bad_current_and_bad_previous_fall_back_to_baseline(env_root: Path) -> None:
    cur = _make_overlay(env_root, "0.3.0")
    prev = _make_overlay(env_root, "0.2.0")
    _link(env_root, "current", cur)
    _link(env_root, "previous", prev)
    state = env_root / "data" / "updates" / "state"
    state.mkdir(parents=True)
    (state / "bad-0.3.0").touch()
    (state / "bad-0.2.0").touch()
    assert _resolve(env_root)["source"] == "baseline"


def test_ner_model_pointer(env_root: Path) -> None:
    model = env_root / "data" / "models" / "ner" / "current"
    model.mkdir(parents=True)
    # 不完整的模型目录不生效
    (model / "model.int8.onnx").touch()
    assert _resolve(env_root)["ner_dir"] == ""
    # 三件套齐全才切换
    (model / "tokenizer.json").touch()
    (model / "labels.json").touch()
    assert _resolve(env_root)["ner_dir"] == str(model)


# ---------------------------------------------------------------------------
# 对外端口解析（docker/resolve-web-port.sh + entrypoint 的绑定预检）
# ---------------------------------------------------------------------------


def _write_port_file(root: Path, content: str) -> Path:
    path = root / "data" / "config" / "web-port"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_web_port_defaults_to_3000(env_root: Path) -> None:
    out = _resolve(env_root)
    assert (out["web_port"], out["web_port_source"]) == ("3000", "default")


def test_web_port_from_env(env_root: Path) -> None:
    out = _resolve(env_root, MOVIECLAW_WEB_PORT="8096")
    assert (out["web_port"], out["web_port_source"]) == ("8096", "env")


def test_web_port_file_beats_env(env_root: Path) -> None:
    _write_port_file(env_root, "9123\n")
    out = _resolve(env_root, MOVIECLAW_WEB_PORT="8096")
    assert (out["web_port"], out["web_port_source"]) == ("9123", "setting")


@pytest.mark.parametrize("bad", ["", "abc", "0", "65536", "99999999999"])
def test_invalid_port_file_falls_back(env_root: Path, bad: str) -> None:
    """端口配错是用户手滑，不该让整个容器起不来——忽略并降级即可。"""
    _write_port_file(env_root, bad)
    out = _resolve(env_root, MOVIECLAW_WEB_PORT="8096")
    assert (out["web_port"], out["web_port_source"]) == ("8096", "env")


def test_unbindable_setting_port_is_discarded(env_root: Path) -> None:
    """应用内设置的端口绑不上就废弃回落：用户改端口用的正是这个 Web 界面，
    起不来就再也进不去了。废弃时留下 .rejected 供设置页外显。"""
    with socket.socket() as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        path = _write_port_file(env_root, f"{port}\n")
        out = _resolve(env_root, MOVIECLAW_WEB_PORT="8096")

    assert (out["web_port"], out["web_port_source"]) == ("8096", "env")
    assert not path.exists()
    assert path.with_name("web-port.rejected").read_text().strip() == str(port)


def test_unbindable_env_port_is_kept(env_root: Path) -> None:
    """环境变量是部署者在应用外显式设的：不存在「被自己关在门外」的路径，
    悄悄改掉反而会与他配的 ports 映射对不上，故保留并让启动如实失败。"""
    with socket.socket() as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        out = _resolve(env_root, MOVIECLAW_WEB_PORT=str(port))

    assert (out["web_port"], out["web_port_source"]) == (str(port), "env")


# ---------------------------------------------------------------------------
# mclaw CLI 的指向解析（docs/design/in-app-update.md「CLI 也走 overlay」）
#
# CLI 与前后端一起随 overlay 发布，但它多一层现实约束：二进制可能根本执行不了
# （data 卷挂 noexec）、老版本 overlay 里压根没有它、软链可能改不动。任何一条
# 踩空都必须安静地退回镜像基线——mclaw 不可以因为一次更新而消失。
# ---------------------------------------------------------------------------

_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[
    platform.machine()
]


def _fake_cli(path: Path, *, executable: bool = True) -> Path:
    """摆一个假的 mclaw。

    刻意**只认 `--help`**，其余参数一律非零退出：entrypoint 的探测参数必须与
    真二进制支持的一致。上一版探测写的是 `--version`（mclaw 根本没有这个
    标志），宽松的假二进制照样通过，直到拿真二进制跑才发现「永远回落基线」。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '#!/bin/sh\n[ "$1" = "--help" ] || exit 64\nexit 0\n', encoding="utf-8"
    )
    path.chmod(0o755 if executable else 0o644)
    return path


def _cli_env(root: Path, *, link: Path | None = None) -> dict[str, str]:
    baseline = _fake_cli(root / "app" / "baseline-bin" / "mclaw")
    return {
        "MOVIECLAW_CLI_BASELINE_BIN": str(baseline),
        "MOVIECLAW_CLI_LINK": str(link if link is not None else root / "bin" / "mclaw"),
    }


def test_cli_falls_back_to_baseline_without_overlay(env_root: Path) -> None:
    env = _cli_env(env_root)
    out = _resolve(env_root, **env)
    assert out["cli_bin"] == env["MOVIECLAW_CLI_BASELINE_BIN"]


def test_overlay_cli_wins_and_link_follows(env_root: Path) -> None:
    """overlay 带了能跑的同版二进制：用它，并把对外软链一起改指过去。"""
    vdir = _make_overlay(env_root, "0.2.0")
    overlay_bin = _fake_cli(vdir / "backend" / "bin" / f"mclaw-linux-{_ARCH}")
    _link(env_root, "current", vdir)
    (env_root / "bin").mkdir()
    env = _cli_env(env_root)
    out = _resolve(env_root, **env)
    assert out["cli_bin"] == str(overlay_bin)
    assert Path(env["MOVIECLAW_CLI_LINK"]).resolve() == overlay_bin.resolve()


def test_overlay_without_cli_keeps_baseline(env_root: Path) -> None:
    """本次改动之前发布的 overlay 没有 bin/：不能因此让 mclaw 消失，
    也不能把这个 overlay 整体判为无效（前后端仍应从它启动）。"""
    vdir = _make_overlay(env_root, "0.2.0")
    _link(env_root, "current", vdir)
    env = _cli_env(env_root)
    out = _resolve(env_root, **env)
    assert out["source"] == "overlay"
    assert out["cli_bin"] == env["MOVIECLAW_CLI_BASELINE_BIN"]


def test_unexecutable_overlay_cli_falls_back(env_root: Path) -> None:
    """data 卷挂了 noexec 这类情况：文件在、位不对，跑不起来就退回基线。"""
    vdir = _make_overlay(env_root, "0.2.0")
    _fake_cli(vdir / "backend" / "bin" / f"mclaw-linux-{_ARCH}", executable=False)
    _link(env_root, "current", vdir)
    env = _cli_env(env_root)
    out = _resolve(env_root, **env)
    assert out["source"] == "overlay"
    assert out["cli_bin"] == env["MOVIECLAW_CLI_BASELINE_BIN"]


def test_unwritable_link_does_not_break_resolution(env_root: Path) -> None:
    """软链改不动（只读 rootfs）不影响解析：Agent 走 MOVIECLAW_CLI_BIN，
    照样用得上新二进制，只有 docker exec 里的 mclaw 停在基线。"""
    vdir = _make_overlay(env_root, "0.2.0")
    overlay_bin = _fake_cli(vdir / "backend" / "bin" / f"mclaw-linux-{_ARCH}")
    _link(env_root, "current", vdir)
    missing_dir = env_root / "nonexistent" / "bin" / "mclaw"
    out = _resolve(env_root, **_cli_env(env_root, link=missing_dir))
    assert out["cli_bin"] == str(overlay_bin)
    assert not missing_dir.exists()
