"""端到端用的本地假 TMDB（线程内 http.server）。

沙箱里没有 TMDB Key 也没有外网，影视库的识别链却必须真跑一遍才能验证
"已识别 + 未识别"混合展示。数据与 tests/api/test_library_scan.py 的桩同源：
一部电影《某电影》(2020, id 300) 与一部剧《测试剧集》(2024, id 200, 一季三集)。
所有请求路径追加写进 ``log_path``，测试据此断言「其他」库扫描全程零 TMDB 请求。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROUTES: dict[str, dict] = {
    "/3/movie/300": {
        "id": 300,
        "title": "某电影",
        "original_title": "Some Movie",
        "release_date": "2020-05-01",
        "runtime": 95,
        "overview": "一部用于端到端验收的假电影。",
        "status": "Released",
        "genres": [{"id": 18, "name": "剧情"}],
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
    "/3/tv/200": {
        "id": 200,
        "name": "测试剧集",
        "original_name": "Test Show",
        "first_air_date": "2024-01-01",
        "overview": "一部用于端到端验收的假剧集。",
        "status": "Returning Series",
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "seasons": [{"season_number": 1}],
    },
    "/3/tv/200/season/1": {
        "name": "第 1 季",
        "air_date": "2024-01-01",
        "episodes": [
            {"episode_number": 1, "name": "E1", "air_date": "2024-01-01"},
            {"episode_number": 2, "name": "E2", "air_date": "2024-01-08"},
            {"episode_number": 3, "name": "E3", "air_date": "2024-01-15"},
        ],
    },
}


def _search(kind: str, query: str) -> list[dict]:
    if kind == "movie" and "某电影" in query:
        return [
            {
                "id": 300,
                "title": "某电影",
                "original_title": "Some Movie",
                "release_date": "2020-05-01",
            }
        ]
    if kind == "tv" and ("测试剧集" in query or "Test Show" in query):
        return [
            {
                "id": 200,
                "name": "测试剧集",
                "original_name": "Test Show",
                "first_air_date": "2024-01-01",
            }
        ]
    return []


def serve(log_path: Path) -> tuple[ThreadingHTTPServer, int]:
    """起一个假 TMDB，返回 (server, port)。请求路径逐行追加到 log_path。"""
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # 静默默认访问日志
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            with lock, log_path.open("a", encoding="utf-8") as fh:
                fh.write(parsed.path + "\n")
            if parsed.path in ("/3/search/movie", "/3/search/tv"):
                query = parse_qs(parsed.query).get("query", [""])[0]
                payload: dict | None = {"results": _search(parsed.path.rsplit("/", 1)[1], query)}
            else:
                payload = ROUTES.get(parsed.path)
            body = json.dumps(payload or {}).encode("utf-8")
            self.send_response(200 if payload is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]
