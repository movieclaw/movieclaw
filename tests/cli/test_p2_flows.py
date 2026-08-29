"""P2 精选命令的进程内流程测试。

用 httpx.MockTransport 冒充服务器（经 core.http.default_transport 注入点），
逐条验证精选命令的编排逻辑：请求顺序、退出码、stdout/stderr 分工。
真实网络与真实服务器的链路由 e2e 测试（test_e2e_login / test_p1_flows）覆盖，
这里专注「一条命令背后的多接口流程」是否正确。
"""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from movieclaw_cli.core import http as http_mod


def _envelope(data, message=None):
    return {"success": True, "code": "OK", "message": message, "data": data}


@pytest.fixture
def run_cli(tmp_path, monkeypatch, capsys):
    """进程内跑一条 mclaw 命令：返回 (退出码, stdout, stderr)。

    配置目录隔离 + 服务器地址走环境变量，transport 由各用例传入。
    """
    home = tmp_path / "cli-home"
    home.mkdir()
    monkeypatch.setenv("MOVIECLAW_CONFIG_DIR", str(home))
    monkeypatch.setenv("MOVIECLAW_SERVER", "http://mock")
    monkeypatch.delenv("MOVIECLAW_TOKEN", raising=False)

    def _run(args: list[str], transport: httpx.BaseTransport) -> tuple[int, str, str]:
        from movieclaw_cli.__main__ import main

        monkeypatch.setattr(http_mod, "default_transport", transport)
        monkeypatch.setattr(http_mod, "last_seen_spec_hash", None)
        monkeypatch.setattr(sys, "argv", ["mclaw", *args])
        code = main()
        out, err = capsys.readouterr()
        return code, out, err

    return _run


def _transport(routes: dict[tuple[str, str], httpx.Response], calls: list[dict]):
    """按 (method, path) 路由的假服务器；calls 收集请求以便断言顺序与载荷。"""

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        body = request.content.decode("utf-8") if request.content else ""
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": body,
                "params": dict(request.url.params),
            }
        )
        if key not in routes:
            return httpx.Response(
                404, json={"success": False, "code": "NOT_FOUND", "message": "接口不存在"}
            )
        return routes[key]

    return httpx.MockTransport(handler)


def test_subscriptions_create_is_one_api_call(run_cli) -> None:
    """新 CLI 与 API 同契约：title_ref 一次调用完成建档、预检与创建。"""
    calls: list[dict] = []
    transport = _transport(
        {
            ("POST", "/api/v1/subscriptions"): httpx.Response(
                200,
                json=_envelope(
                    {
                        "subscription": {"id": 7, "media": {"title": "沙丘2"}},
                        "download_routing": {"ok": True, "mode": "watch"},
                    },
                    message="已加入订阅",
                ),
            )
        },
        calls,
    )

    code, out, err = run_cli(
        ["subscriptions", "create", "--title-ref", "tmdb:movie:693134", "-o", "json"],
        transport,
    )

    assert code == 0, err
    assert json.loads(out)["subscription"]["id"] == 7
    assert [call["path"] for call in calls] == ["/api/v1/subscriptions"]
    assert json.loads(calls[0]["body"]) == {"title_ref": "tmdb:movie:693134"}


# ---------------------------------------------------------------------------
# search（SSE 聚合 + 快照）→ download（行号引用）
# ---------------------------------------------------------------------------

_SSE_BODY = (
    'event: start\ndata: {"keyword": "沙丘", "sites": [{"site_id": "a"}]}\n\n'
    "event: site_result\ndata: "
    + json.dumps(
        {
            "site_id": "a",
            "site_name": "站点A",
            "count": 2,
            "elapsed_ms": 120,
            "items": [
                {
                    "site_id": "a",
                    "site_name": "站点A",
                    "title": "Dune.2160p",
                    "seeders": 5,
                    "size": "20 GB",
                    "size_bytes": 20_000_000_000,
                    "free": True,
                    "download_url": "https://a/dl/1",
                    "attrs": {
                        "media_type": "movie",
                        "resolution": "2160p",
                        "titles_zh": ["沙丘"],
                        "year": 2021,
                    },
                },
                {
                    "site_id": "a",
                    "site_name": "站点A",
                    "title": "Dune.1080p",
                    "seeders": 9,
                    "size": "8 GB",
                    "size_bytes": 8_000_000_000,
                    "free": False,
                    "download_url": "https://a/dl/2",
                    "attrs": {
                        "media_type": "movie",
                        "resolution": "1080p",
                        "titles_zh": ["沙丘"],
                        "year": 2021,
                    },
                },
            ],
        },
        ensure_ascii=False,
    )
    + "\n\nevent: done\ndata: "
    + json.dumps({"total": 2, "elapsed_ms": 130, "sites": []}, ensure_ascii=False)
    + "\n\n"
)


def _search_transport(calls: list[dict], *, target: dict | None = None):
    if target is None:
        target = {
            "status": "ready",
            "tmdb_id": 438631,
            "candidates": [],
            "library_id": 7,
            "library_name": "电影库",
            "mode": "watch",
            "path": "/download/电影",
            "staging_path": None,
            "route_matched": False,
            "route_reason": "入库到默认库「电影库」",
            "ok": True,
            "warning": None,
        }
    return _transport(
        {
            ("GET", "/api/v1/search/torrents/stream"): httpx.Response(
                200, content=_SSE_BODY.encode(), headers={"content-type": "text/event-stream"}
            ),
            ("POST", "/api/v1/downloaders/resolve-target"): httpx.Response(
                200,
                json=_envelope(target, message="已识别资源并预演自动入库目录"),
            ),
            ("POST", "/api/v1/downloaders/submit"): httpx.Response(
                200,
                json=_envelope(
                    {
                        "info_hash": "h",
                        "name": "Dune.2160p",
                        "already_exists": False,
                        "downloader_id": 1,
                        "downloader_name": "qb",
                        "save_path": "/dl",
                    },
                    message="已提交到「qb」",
                ),
            ),
        },
        calls,
    )


def test_search_aggregates_and_snapshots_then_download_by_row(run_cli, tmp_path) -> None:
    calls: list[dict] = []
    code, out, err = run_cli(["search", "沙丘", "-o", "json"], _search_transport(calls))
    assert code == 0, err
    rows = json.loads(out)
    # 默认按 seeders 降序：1080p(9) 在前
    assert [r["row"] for r in rows] == [1, 2]
    assert rows[0]["title"] == "Dune.1080p" and rows[0]["seeders"] == 9
    assert "站点A：2 条" in err and "完成：共 2 条" in err

    calls.clear()
    code, out, err = run_cli(["download", "1", "-o", "json"], _search_transport(calls))
    assert code == 0, err
    assert [call["path"] for call in calls] == [
        "/api/v1/downloaders/resolve-target",
        "/api/v1/downloaders/submit",
    ]
    resolved = json.loads(calls[0]["body"])
    assert resolved == {
        "kind": "movie",
        "title": "沙丘",
        "year": 2021,
        "subtitle": None,
    }
    submitted = json.loads(calls[1]["body"])
    assert submitted["download_url"] == "https://a/dl/2"  # 行号 1 = 排序后的 1080p
    assert submitted["title"] == "沙丘" and submitted["year"] == 2021
    assert submitted["auto_route"] is True
    assert submitted["media_kind"] == "movie" and submitted["tmdb_id"] == 438631
    assert "智能入库" in err
    assert "已提交到「qb」" in err


def test_download_ambiguous_returns_candidates_without_submitting(run_cli) -> None:
    calls: list[dict] = []
    run_cli(["search", "沙丘", "-o", "json"], _search_transport(calls))
    calls.clear()
    target = {
        "status": "ambiguous",
        "tmdb_id": None,
        "candidates": [
            {"tmdb_id": 11, "title": "沙丘", "year": 2021, "episode_count": None},
            {"tmdb_id": 12, "title": "沙丘", "year": 1984, "episode_count": None},
        ],
        "library_id": None,
        "library_name": None,
        "mode": None,
        "path": None,
        "staging_path": None,
        "route_matched": None,
        "route_reason": None,
        "ok": False,
        "warning": None,
    }

    code, out, err = run_cli(
        ["download", "1", "-o", "json"],
        _search_transport(calls, target=target),
    )

    assert code == 7
    assert json.loads(out)["candidates"][0]["tmdb_id"] == 11
    assert [call["path"] for call in calls] == ["/api/v1/downloaders/resolve-target"]
    assert "--tmdb-id" in err and "未提交下载" in err


def test_download_tmdb_id_is_sent_to_preflight_before_submit(run_cli) -> None:
    calls: list[dict] = []
    run_cli(["search", "沙丘", "-o", "json"], _search_transport(calls))
    calls.clear()

    code, _out, err = run_cli(
        ["download", "1", "--tmdb-id", "12", "-o", "json"],
        _search_transport(calls),
    )

    assert code == 0, err
    assert json.loads(calls[0]["body"])["selected_tmdb_id"] == 12
    assert json.loads(calls[1]["body"])["tmdb_id"] == 438631


def test_download_unroutable_target_fails_closed(run_cli) -> None:
    calls: list[dict] = []
    run_cli(["search", "沙丘", "-o", "json"], _search_transport(calls))
    calls.clear()
    target = {
        "status": "ready",
        "tmdb_id": 438631,
        "candidates": [],
        "library_id": 7,
        "library_name": "电影库",
        "mode": "watch",
        "path": "/download/电影",
        "staging_path": None,
        "route_matched": False,
        "route_reason": "入库到默认库「电影库」",
        "ok": False,
        "warning": "目录不在下载器的路径映射覆盖范围内",
    }

    code, out, err = run_cli(
        ["download", "1", "-o", "json"],
        _search_transport(calls, target=target),
    )

    assert code == 1 and out == ""
    assert [call["path"] for call in calls] == ["/api/v1/downloaders/resolve-target"]
    assert "路径映射" in err and "--downloader-default" in err


def test_download_without_complete_identity_fails_before_preflight(run_cli) -> None:
    from movieclaw_cli.overlay.search_cmds import load_snapshot, save_snapshot

    calls: list[dict] = []
    run_cli(["search", "沙丘", "-o", "json"], _search_transport(calls))
    snapshot = load_snapshot()
    assert snapshot is not None
    snapshot["items"][0]["attrs"].pop("media_type")
    save_snapshot(snapshot["server"], snapshot["keyword"], snapshot["items"])
    calls.clear()

    code, out, err = run_cli(
        ["download", "1", "-o", "json"],
        _search_transport(calls),
    )

    assert code == 1 and out == ""
    assert calls == []
    assert "缺少可靠的媒体类型" in err and "--downloader-default" in err


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--library", "7"], {"library_id": 7}),
        (["--save-path", "/download/临时"], {"save_path": "/download/临时"}),
        (["--downloader-default"], {}),
    ],
)
def test_download_explicit_target_skips_smart_preflight(run_cli, args, expected) -> None:
    calls: list[dict] = []
    run_cli(["search", "沙丘", "-o", "json"], _search_transport(calls))
    calls.clear()

    code, _out, err = run_cli(
        ["download", "1", *args, "-o", "json"],
        _search_transport(calls),
    )

    assert code == 0, err
    assert [call["path"] for call in calls] == ["/api/v1/downloaders/submit"]
    submitted = json.loads(calls[0]["body"])
    assert "auto_route" not in submitted
    for key, value in expected.items():
        assert submitted[key] == value


@pytest.mark.parametrize(
    "args",
    [
        ["1", "--library", "7", "--save-path", "/download/临时"],
        ["1", "--library", "7", "--downloader-default"],
        ["1", "--tmdb-id", "12", "--downloader-default"],
        ["1", "--tmdb-id", "12", "--library", "7"],
    ],
)
def test_download_rejects_conflicting_route_options(run_cli, args) -> None:
    calls: list[dict] = []
    code, out, err = run_cli(["download", *args], _search_transport(calls))
    assert code == 2 and out == "" and calls == []
    assert "不能" in err or "只能" in err


def test_search_resolution_filter(run_cli) -> None:
    code, out, _err = run_cli(
        ["search", "沙丘", "--resolution", "2160p", "-o", "json"], _search_transport([])
    )
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1 and rows[0]["resolution"] == "2160p"


def test_search_titles_uses_unified_route(run_cli) -> None:
    """CLI 的影视搜索命名、路径与公开 API 保持一致。"""
    calls: list[dict] = []
    transport = _transport(
        {
            ("POST", "/api/v1/search/titles"): httpx.Response(
                200,
                json=_envelope(
                    {
                        "query": "沙丘",
                        "titles": [{"title_ref": "tmdb:movie:438631", "title": "沙丘"}],
                        "providers": [
                            {
                                "provider": "tmdb",
                                "success": True,
                                "result_count": 1,
                                "message": None,
                            }
                        ],
                        "history_id": 7,
                    }
                ),
            )
        },
        calls,
    )

    code, out, err = run_cli(
        ["search", "titles", "沙丘", "--provider", "tmdb", "-o", "json"],
        transport,
    )

    assert code == 0, err
    assert json.loads(out) == [{"title_ref": "tmdb:movie:438631", "title": "沙丘"}]
    assert json.loads(calls[0]["body"]) == {
        "query": "沙丘",
        "provider": "tmdb",
        "save_history": True,
    }


def test_search_library_items_uses_unified_route(run_cli) -> None:
    """本地库存搜索也位于同一个 search 命令域。"""
    calls: list[dict] = []
    transport = _transport(
        {
            ("GET", "/api/v1/search/library-items"): httpx.Response(
                200,
                json=_envelope([{"library_id": 1, "library_name": "电影库", "items": []}]),
            )
        },
        calls,
    )

    code, out, err = run_cli(
        ["search", "library-items", "沙丘", "-o", "json"],
        transport,
    )

    assert code == 0, err
    assert json.loads(out)[0]["library_name"] == "电影库"
    assert calls[0]["params"] == {"keyword": "沙丘"}


def test_download_row_out_of_range_exits_2(run_cli) -> None:
    calls: list[dict] = []
    run_cli(["search", "沙丘", "-o", "json"], _search_transport(calls))
    code, _out, err = run_cli(["download", "99"], _search_transport(calls))
    assert code == 2
    assert "行号超出范围" in err


def test_download_without_snapshot_hints_search_first(run_cli) -> None:
    code, _out, err = run_cli(["download", "1"], _transport({}, []))
    assert code == 2
    assert "mclaw search" in err


# ---------------------------------------------------------------------------
# library organize-files：预览 → 确认 → 执行 → 等待
# ---------------------------------------------------------------------------


def _organize_transport(calls: list[dict]):
    return _transport(
        {
            ("POST", "/api/v1/libraries/1/file-organization-preview"): httpx.Response(
                200,
                json=_envelope(
                    {
                        "total": 10,
                        "already_ok": 7,
                        "renames": [{"from": "a.mkv", "to": "b.mkv"}] * 2,
                        "skips": [{"file": "c.mkv", "reason": "认不出"}],
                    }
                ),
            ),
            ("POST", "/api/v1/libraries/1/file-organizations"): httpx.Response(
                200, json=_envelope({"started": True, "message": "已开始"}, message="整理已开始")
            ),
            ("GET", "/api/v1/libraries/1"): httpx.Response(
                200, json=_envelope({"organize_progress": None})
            ),
        },
        calls,
    )


def test_organize_dry_run_only_previews(run_cli) -> None:
    calls: list[dict] = []
    code, out, err = run_cli(
        ["library", "organize-files", "1", "--dry-run", "-o", "json"],
        _organize_transport(calls),
    )
    assert code == 0, err
    assert json.loads(out)["total"] == 10
    assert [c["path"] for c in calls] == ["/api/v1/libraries/1/file-organization-preview"]
    assert "改名 2 项" in err


def test_organize_without_yes_exits_5(run_cli) -> None:
    calls: list[dict] = []
    code, _out, err = run_cli(["library", "organize-files", "1"], _organize_transport(calls))
    assert code == 5
    assert "--yes" in err
    assert [c["path"] for c in calls] == ["/api/v1/libraries/1/file-organization-preview"]


def test_organize_with_yes_executes_and_waits(run_cli, monkeypatch) -> None:
    # 本用例验证轮询次数与终态判断，不验证真实时间流逝；跳过生产退避可省 5 秒。
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    calls: list[dict] = []
    code, _out, err = run_cli(
        ["library", "organize-files", "1", "--yes"], _organize_transport(calls)
    )
    assert code == 0, err
    paths = [c["path"] for c in calls]
    assert paths[:2] == [
        "/api/v1/libraries/1/file-organization-preview",
        "/api/v1/libraries/1/file-organizations",
    ]
    assert "/api/v1/libraries/1" in paths[2:]  # --wait 轮询到 organize_progress 为 null
    assert "任务已结束" in err


# ---------------------------------------------------------------------------
# library reconcile-paths：预览 → 确认 → 创建修复扫描作业
# ---------------------------------------------------------------------------


def _path_reconcile_transport(calls: list[dict]):
    return _transport(
        {
            ("POST", "/api/v1/libraries/1/path-reconciliation-preview"): httpx.Response(
                200,
                json=_envelope(
                    {
                        "library_id": 1,
                        "old_root": "/strm/movies",
                        "new_root": "/media/movies",
                        "same_path_candidates": 4,
                        "safe_merges": 3,
                        "marked_missing": 1,
                        "conflicts": ["/strm/movies/conflict.mkv ↔ /media/movies/conflict.mkv"],
                        "unconfirmed": [],
                        "old_rows_to_delete_from_ledger": 3,
                        "disk_files_to_delete": 0,
                    }
                ),
            ),
            ("POST", "/api/v1/libraries/1/path-reconciliations"): httpx.Response(
                202,
                json=_envelope(
                    {"started": True, "message": "已开始", "job_id": "repair-1", "created": True},
                    message="已开始修复",
                ),
            ),
        },
        calls,
    )


def test_path_reconcile_dry_run_only_previews(run_cli) -> None:
    calls: list[dict] = []
    code, out, err = run_cli(
        [
            "library",
            "reconcile-paths",
            "1",
            "--old-root",
            "/strm/movies",
            "--new-root",
            "/media/movies",
            "--dry-run",
            "-o",
            "json",
        ],
        _path_reconcile_transport(calls),
    )
    assert code == 0, err
    assert json.loads(out)["safe_merges"] == 3
    assert [call["path"] for call in calls] == ["/api/v1/libraries/1/path-reconciliation-preview"]
    assert "磁盘删除 0" in err


def test_path_reconcile_requires_yes_before_start(run_cli) -> None:
    calls: list[dict] = []
    code, _out, err = run_cli(
        [
            "library",
            "reconcile-paths",
            "1",
            "--old-root",
            "/strm/movies",
            "--new-root",
            "/media/movies",
        ],
        _path_reconcile_transport(calls),
    )
    assert code == 5
    assert "--yes" in err
    assert [call["path"] for call in calls] == ["/api/v1/libraries/1/path-reconciliation-preview"]


def test_path_reconcile_with_yes_starts_job(run_cli) -> None:
    calls: list[dict] = []
    code, out, err = run_cli(
        [
            "library",
            "reconcile-paths",
            "1",
            "--old-root",
            "/strm/movies",
            "--new-root",
            "/media/movies",
            "--yes",
            "-o",
            "json",
        ],
        _path_reconcile_transport(calls),
    )
    assert code == 0, err
    assert json.loads(out)["job_id"] == "repair-1"
    assert [call["path"] for call in calls] == [
        "/api/v1/libraries/1/path-reconciliation-preview",
        "/api/v1/libraries/1/path-reconciliations",
    ]
    assert "已开始修复" in err


# ---------------------------------------------------------------------------
# session start：创建会话 → SSE 渲染 → 终态定退出码
# ---------------------------------------------------------------------------


def _session_transport(calls: list[dict], sse_body: str):
    return _transport(
        {
            ("POST", "/api/v1/sessions"): httpx.Response(
                202, json=_envelope({"session_id": "s1", "message_id": "m1"})
            ),
            ("GET", "/api/v1/sessions/s1/events"): httpx.Response(
                200, content=sse_body.encode(), headers={"content-type": "text/event-stream"}
            ),
        },
        calls,
    )


def test_session_start_renders_and_exits_0(run_cli) -> None:
    sse = (
        'id: 1\nevent: agent_start\ndata: {"provider": "deepseek", "model": "v3"}\n\n'
        'id: 2\nevent: text_delta\ndata: {"delta": "你好"}\n\n'
        "id: 3\nevent: tool_call\ndata: "
        + json.dumps(
            {"tool_call": {"name": "bash", "arguments": {"command": "ls"}}}, ensure_ascii=False
        )
        + "\n\n"
        'id: 4\nevent: tool_result\ndata: {"tool_result": {"is_error": false, "elapsed_ms": 5}}\n\n'
        "id: 5\nevent: agent_done\ndata: "
        + json.dumps(
            {
                "result": {
                    "steps": 2,
                    "elapsed_ms": 100,
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
            }
        )
        + "\n\n"
    )
    code, out, err = run_cli(["session", "start", "整理一下"], _session_transport([], sse))
    assert code == 0, err
    assert "你好" in out
    assert "→ 工具 bash" in err and "[完成] 2 步" in err


def test_session_start_error_exits_1(run_cli) -> None:
    sse = 'id: 1\nevent: agent_error\ndata: {"error": "模型连接失败"}\n\n'
    code, _out, err = run_cli(["session", "start", "任务"], _session_transport([], sse))
    assert code == 1
    assert "模型连接失败" in err


def test_session_start_detach_returns_ids(run_cli) -> None:
    calls: list[dict] = []
    code, out, _err = run_cli(
        ["session", "start", "任务", "--detach", "-o", "json"],
        _session_transport(calls, ""),
    )
    assert code == 0
    assert json.loads(out) == {"session_id": "s1", "message_id": "m1"}
    assert [c["path"] for c in calls] == ["/api/v1/sessions"]  # 未订阅事件流


def test_session_start_with_id_continues_existing_session(run_cli) -> None:
    calls: list[dict] = []
    transport = _transport(
        {
            ("POST", "/api/v1/sessions"): httpx.Response(
                202, json=_envelope({"session_id": "s1", "message_id": "m2"})
            )
        },
        calls,
    )
    code, out, err = run_cli(
        [
            "session",
            "start",
            "继续",
            "--session-id",
            "s1",
            "--detach",
            "-o",
            "json",
        ],
        transport,
    )
    assert code == 0, err
    assert json.loads(out) == {"session_id": "s1", "message_id": "m2"}
    assert [call["path"] for call in calls] == ["/api/v1/sessions"]
    assert json.loads(calls[0]["body"]) == {
        "content": "继续",
        "session_id": "s1",
    }


def test_session_retry_replaces_and_resubmits_in_one_command(run_cli) -> None:
    calls: list[dict] = []
    transport = _transport(
        {
            ("POST", "/api/v1/sessions/s1/retry"): httpx.Response(
                202, json=_envelope({"session_id": "s1", "message_id": "m3"})
            )
        },
        calls,
    )
    code, out, err = run_cli(
        [
            "session",
            "retry",
            "s1",
            "--message-id",
            "m2",
            "--prompt",
            "改写后的问题",
            "--detach",
            "-o",
            "json",
        ],
        transport,
    )
    assert code == 0, err
    assert json.loads(out) == {"session_id": "s1", "message_id": "m3"}
    assert [call["path"] for call in calls] == ["/api/v1/sessions/s1/retry"]
    assert json.loads(calls[0]["body"]) == {
        "message_id": "m2",
        "content": "改写后的问题",
    }


def test_session_retry_help_explains_history_replacement_without_confirmation(run_cli) -> None:
    code, out, err = run_cli(["session", "retry", "--help"], _transport({}, []))
    assert code == 0, err
    normalized = " ".join(out.split())
    assert "现有目标消息及其后的轨迹会被永久删除" in normalized
    assert "不传 --prompt 时按原文重试" in normalized
    assert "新的 message_id" in normalized
    assert "--yes" not in out and "⚠" not in out


def test_legacy_agent_continue_and_send_message_commands_are_removed(run_cli) -> None:
    transport = _transport({}, [])
    agent_code, _out, agent_err = run_cli(["agent", "--help"], transport)
    continue_code, _out, continue_err = run_cli(
        ["session", "continue", "s1", "旧命令"], transport
    )
    send_code, _out, send_err = run_cli(
        ["session", "send-message", "s1", "旧命令"], transport
    )
    rewind_code, _out, rewind_err = run_cli(
        ["session", "rewind", "s1", "--before-message", "m1"], transport
    )
    assert agent_code == 2 and "No such command 'agent'" in agent_err
    assert continue_code == 2 and "No such command 'continue'" in continue_err
    assert send_code == 2 and "No such command 'send-message'" in send_err
    assert rewind_code == 2 and "No such command 'rewind'" in rewind_err


# ---------------------------------------------------------------------------
# logs tail
# ---------------------------------------------------------------------------


def test_logs_tail_prints_latest_day(run_cli) -> None:
    transport = _transport(
        {
            ("GET", "/api/v1/system/logs"): httpx.Response(
                200, json=_envelope({"days": [{"day": "2026-07-30", "size_bytes": 1}]})
            ),
            ("GET", "/api/v1/system/logs/2026-07-30"): httpx.Response(
                200,
                json=_envelope(
                    {
                        "day": "2026-07-30",
                        "lines": ["行1", "行2"],
                        "total_lines": 2,
                        "truncated": False,
                        "size_bytes": 1,
                    }
                ),
            ),
        },
        [],
    )
    code, out, _err = run_cli(["logs", "tail", "--lines", "5"], transport)
    assert code == 0
    assert out.splitlines() == ["行1", "行2"]


# ---------------------------------------------------------------------------
# 审查修复的回归用例
# ---------------------------------------------------------------------------


def test_search_premature_close_exits_4_without_snapshot(run_cli, tmp_path) -> None:
    """流在 done 前闭合：部分结果绝不能当完整结果输出/落快照。"""
    partial = _SSE_BODY.split("event: done")[0]  # 去掉 done 帧
    calls: list[dict] = []
    transport = _transport(
        {
            ("GET", "/api/v1/search/torrents/stream"): httpx.Response(
                200, content=partial.encode(), headers={"content-type": "text/event-stream"}
            )
        },
        calls,
    )
    code, out, err = run_cli(["search", "沙丘", "-o", "json"], transport)
    assert code == 4
    assert "结果不完整" in err
    assert out.strip() == ""  # 不输出部分结果


def test_download_rejects_snapshot_from_other_server(run_cli, monkeypatch) -> None:
    calls: list[dict] = []
    run_cli(["search", "沙丘", "-o", "json"], _search_transport(calls))
    # 换服务器后用旧快照的行号下载：必须拒绝
    monkeypatch.setenv("MOVIECLAW_SERVER", "http://another")
    code, _out, err = run_cli(["download", "1"], _search_transport(calls))
    assert code == 2
    assert "另一台服务器" in err


def test_login_denied_exits_3(run_cli, monkeypatch) -> None:
    """配对被拒绝是终态：立刻以认证失败退出，不继续轮询。

    密码登录已废弃（docs/design/device-auth.md §6.1），CLI 唯一的授权入口
    是设备配对；这里用 mock transport 直接给出「已拒绝」的终态响应。
    """
    # login 要求 TTY（配对必须有人批准，非 TTY 直接以用法错误退出），
    # pytest 捕获下的 stdin 不是 TTY，这里伪装成 TTY 以走到真正要测的分支。
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    transport = _transport(
        {
            ("GET", "/api/v1/health"): httpx.Response(
                200, json=_envelope({"status": "ok", "version": "0.18.0"})
            ),
            ("POST", "/api/v1/auth/device/authorize"): httpx.Response(
                200,
                json=_envelope(
                    {
                        "user_code": "MCLW-TEST",
                        "device_code": "dev-code",
                        "verification_uri": "http://server/settings/devices",
                        "interval": 1,
                        "expires_in": 60,
                    }
                ),
            ),
            ("POST", "/api/v1/auth/device/token"): httpx.Response(
                400,
                json={
                    "success": False,
                    "code": "BAD_REQUEST",
                    "message": "接入请求已被拒绝，请重新发起配对",
                },
            ),
        },
        [],
    )
    code, _out, err = run_cli(["login", "--server", "http://server"], transport)
    assert code == 3
    assert "拒绝" in err


def test_session_stream_reconnects_after_interruption(run_cli, monkeypatch) -> None:
    """事件流中断（含服务重启的连接拒绝形态）应重连续传，而不是立刻放弃。"""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(
                202, json=_envelope({"session_id": "s1", "message_id": "m1"})
            )
        attempts.append(1)
        if len(attempts) == 1:
            # 第一次连接：发一半就断（无终态）
            return httpx.Response(
                200,
                content=b'id: 1\nevent: text_delta\ndata: {"delta": "part"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        assert request.headers.get("Last-Event-ID") == "1"  # 续传游标
        return httpx.Response(
            200,
            content=b'id: 2\nevent: agent_done\ndata: {"result": {"steps": 1}}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    code, out, err = run_cli(["session", "start", "任务"], httpx.MockTransport(handler))
    assert code == 0, err
    assert len(attempts) == 2
    assert "part" in out
