"""下载器路径体检的判定口径。

核心诉求：把「movieclaw 看不到下载文件」这个症状拆成互斥、可自动判定的几种
根因，每种对应不同的修复动作。尤其是 EMPTY 与 MISSING 必须分开——前者重启
容器就好，后者要改配置或挂卷。
"""

from __future__ import annotations

from movieclaw_api.services.downloader_paths import (
    PathState,
    diagnose_landing,
    probe_local_dir,
    probe_mappings,
    summarize,
)


def test_目录有内容判为可见(tmp_path) -> None:
    (tmp_path / "剧集").mkdir()
    probe = probe_local_dir(str(tmp_path))
    assert probe.state == PathState.OK
    assert probe.healthy


def test_空目录单独成一类而不是笼统的不可达(tmp_path) -> None:
    """挂载失效的典型特征：目录在、但空。文案必须给出"重启容器"这个动作。"""
    empty = tmp_path / "download"
    empty.mkdir()
    probe = probe_local_dir(str(empty))
    assert probe.state == PathState.EMPTY
    assert not probe.healthy
    assert "重启" in probe.detail


def test_目录不存在与目录为空是两种结论(tmp_path) -> None:
    missing = probe_local_dir(str(tmp_path / "根本没有这个目录"))
    empty_dir = tmp_path / "空的"
    empty_dir.mkdir()
    empty = probe_local_dir(str(empty_dir))
    assert missing.state == PathState.MISSING
    assert empty.state == PathState.EMPTY
    assert missing.detail != empty.detail


def test_路径指到文件上判为不是目录(tmp_path) -> None:
    target = tmp_path / "其实是个文件"
    target.write_text("x")
    assert probe_local_dir(str(target)).state == PathState.NOT_DIR


def test_未被映射覆盖的落点单独归因(tmp_path) -> None:
    """下载器上报的目录不在任何一条映射里——这是配置缺失，不是环境故障。"""
    mappings = [{"local": "/download", "remote": "/downloads"}]
    probe = diagnose_landing("/完全无关的位置", mappings)
    assert probe.state == PathState.UNMAPPED


def test_映射覆盖后继续体检目录本身(tmp_path) -> None:
    covered = tmp_path / "download"
    covered.mkdir()
    mappings = [{"local": str(tmp_path), "remote": "/downloads"}]
    probe = diagnose_landing(str(covered), mappings)
    # 被映射覆盖，但目录本身是空的 —— 结论应落在 EMPTY 而不是 UNMAPPED
    assert probe.state == PathState.EMPTY


def test_没有配置映射时不误报未覆盖(tmp_path) -> None:
    """两边视角一致的直装部署没有映射可言，不该因此判故障。"""
    real = tmp_path / "download"
    real.mkdir()
    (real / "文件").write_text("x")
    assert diagnose_landing(str(real), None).state == PathState.OK
    assert probe_mappings(None) == []


def test_多条映射收敛到最严重的一条(tmp_path) -> None:
    ok_dir = tmp_path / "好的"
    ok_dir.mkdir()
    (ok_dir / "内容").write_text("x")
    empty_dir = tmp_path / "空的"
    empty_dir.mkdir()
    probes = probe_mappings(
        [
            {"local": str(ok_dir), "remote": "/a"},
            {"local": str(empty_dir), "remote": "/b"},
            {"local": str(tmp_path / "不存在"), "remote": "/c"},
        ]
    )
    assert len(probes) == 3
    worst = summarize(probes)
    assert worst is not None
    assert worst[0] == PathState.MISSING


def test_全部健康时不产出结论(tmp_path) -> None:
    ok_dir = tmp_path / "好的"
    ok_dir.mkdir()
    (ok_dir / "内容").write_text("x")
    assert summarize(probe_mappings([{"local": str(ok_dir), "remote": "/a"}])) is None
