"""模拟 movieclaw 的 SSE 端点，用来端到端验证搜索流、会话流与 Job 等待。

真环境跑这三条要接 PT 站点和大模型，CI 里不可能有；协议本身可以在这里
完整走一遍：分帧、断流续传、终态退出码、行号快照 → 下载。
"""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"session_flakes": 0, "job_polls": 0}


def envelope(data, message="success"):
    return json.dumps(
        {"success": True, "code": "OK", "message": message, "data": data}, ensure_ascii=False
    ).encode()


def error(code, message):
    return json.dumps(
        {"success": False, "code": code, "message": message, "details": None}, ensure_ascii=False
    ).encode()


HIT = {
    "site_id": "demo",
    "site_name": "演示站",
    "title": "沙丘2.2024.2160p",
    "subtitle": "中字",
    "size": "42 GB",
    "size_bytes": 45097156608,
    "seeders": 120,
    "free": True,
    "download_url": "https://demo.example/t/1",
    "attrs": {
        "resolution": "2160p",
        "release_group": "DEMO",
        "media_type": "movie",
        "titles_zh": ["沙丘2"],
        "titles_en": ["Dune Part Two"],
        "year": 2024,
    },
}
HIT2 = {
    **HIT,
    "title": "沙丘2.2024.1080p",
    "seeders": 9,
    "size_bytes": 12,
    "attrs": {**HIT["attrs"], "resolution": "1080p"},
    "free": False,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # 真服务端在流结束时会关连接；不关的话客户端读不到 EOF，只能等空闲超时
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def frame(self, event, data, event_id=None):
        out = f"event: {event}\n"
        if event_id is not None:
            out += f"id: {event_id}\n"
        out += f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(out.encode())
        self.wfile.flush()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/v1/health":
            return self.send_json(
                json.dumps({"status": "ok", "service": "movieclaw-stub"}).encode()
            )
        if path == "/api/v1/search/torrents/stream":
            self.start_sse()
            if "truncated" in self.path:
                # 没有 done 就闭合：CLI 必须报「结果不完整」而不是当完整结果用
                self.frame(
                    "site_result",
                    {"site_name": "演示站", "count": 1, "elapsed_ms": 5, "items": [HIT]},
                )
                return
            self.frame("start", {"sites": ["demo"]})
            self.frame(
                "site_result",
                {"site_name": "演示站", "count": 2, "elapsed_ms": 12, "items": [HIT2, HIT]},
            )
            self.frame("site_error", {"site_name": "坏站", "error": "连接超时"})
            self.frame("done", {"total": 2, "elapsed_ms": 20})
            return
        if path == "/api/v1/search/torrents/stream-truncated":
            self.start_sse()
            self.frame(
                "site_result", {"site_name": "演示站", "count": 1, "elapsed_ms": 5, "items": [HIT]}
            )
            return  # 没有 done：CLI 必须报「结果不完整」
        m = re.match(r"^/api/v1/sessions/([^/]+)/events$", path)
        if m:
            session = m.group(1)
            self.start_sse()
            resume = self.headers.get("Last-Event-ID")
            if session == "flaky" and not resume:
                # 首次连接在终态前断掉，逼 CLI 走 Last-Event-ID 续传（可重复跑）
                self.frame("agent_start", {"provider": "demo", "model": "m1"}, 1)
                self.frame("text_delta", {"delta": "前半段"}, 2)
                return
            if not resume:
                self.frame("agent_start", {"provider": "demo", "model": "m1"}, 1)
                self.frame("text_delta", {"delta": "你好"}, 2)
            self.frame(
                "tool_call", {"tool_call": {"name": "library.list", "arguments": {"a": 1}}}, 3
            )
            self.frame("tool_result", {"tool_result": {"is_error": False, "elapsed_ms": 8}}, 4)
            if session == "failing":
                self.frame("agent_error", {"error": "模型没有配置"}, 5)
            elif session == "cancelled":
                self.frame("agent_cancelled", {}, 5)
            else:
                self.frame(
                    "agent_done",
                    {
                        "result": {
                            "steps": 2,
                            "elapsed_ms": 30,
                            "usage": {"input_tokens": 10, "output_tokens": 20},
                        }
                    },
                    5,
                )
            return
        m = re.match(r"^/api/v1/jobs/([^/]+)/wait$", path)
        if m:
            STATE["job_polls"] += 1
            status = "running" if STATE["job_polls"] < 2 else "succeeded"
            return self.send_json(
                envelope(
                    {
                        "job": {
                            "id": m.group(1),
                            "revision": STATE["job_polls"],
                            "status": status,
                            "progress": {"message": "处理中", "percent": 50},
                        }
                    }
                )
            )
        m = re.match(r"^/api/v1/jobs/([^/]+)$", path)
        if m:
            return self.send_json(envelope({"id": m.group(1), "status": "succeeded"}))
        return self.send_json(error("RESOURCE_NOT_FOUND", f"没有这个端点：{path}"), 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/api/v1/downloaders/resolve-target":
            if body.get("selected_tmdb_id"):
                return self.send_json(
                    envelope(
                        {
                            "status": "ready",
                            "ok": True,
                            "tmdb_id": body["selected_tmdb_id"],
                            "library_id": 1,
                            "library_name": "电影库",
                            "route_reason": "入库到「电影库」",
                            "path": "/media/movies",
                        }
                    )
                )
            return self.send_json(
                envelope(
                    {
                        "status": "ambiguous",
                        "candidates": [
                            {"tmdb_id": 693134, "title": "沙丘2"},
                            {"tmdb_id": 438631, "title": "沙丘"},
                        ],
                    }
                )
            )
        if path == "/api/v1/downloaders/submit":
            return self.send_json(
                envelope(
                    {
                        "task_id": "t-1",
                        "auto_route": body.get("auto_route", False),
                        "tmdb_id": body.get("tmdb_id"),
                    },
                    "已提交到默认下载器",
                )
            )
        if path == "/api/v1/sessions":
            return self.send_json(
                envelope({"session_id": body.get("session_id") or "s-1", "message_id": "m-1"})
            )
        return self.send_json(error("RESOURCE_NOT_FOUND", f"没有这个端点：{path}"), 404)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
