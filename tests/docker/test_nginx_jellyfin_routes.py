"""nginx 前门必须覆盖 Jellyfin 兼容层的全部命名空间。

这次的教训（2026-08-25）：后端加了 /Persons 与 /Search/Hints 路由，nginx
的放行清单没跟上，请求落到 Next 返回 404 HTML——Infuse 搜索两路并发里
一路失败，整个搜索功能对用户「完全不可用」。后端路由与 nginx 模板分属
两个文件，靠人肉同步必然再漏，这里把两份清单钉在一起。
"""

from __future__ import annotations

import re
from pathlib import Path

from movieclaw_jellyfin.router import NAMESPACE_PREFIXES

_TEMPLATE = Path(__file__).resolve().parents[2] / "docker" / "nginx.conf.template"

#: 刻意只放行子路径的命名空间：与 Next 页面路由或旧接口同名，整段放行会
#: 劫持网页。它们各自有独立的 location 块，这里只要求模板里出现该前缀。
_SUBPATH_ONLY = {"search", "sessions", "library"}


def test_nginx_template_covers_all_jellyfin_namespaces() -> None:
    text = _TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"\^/\(([a-z|]+)\)\(/\|\$\)", text)
    assert match, "nginx 模板里找不到 Jellyfin 命名空间的放行正则"
    covered = set(match.group(1).split("|"))

    for prefix in sorted(NAMESPACE_PREFIXES):
        if prefix in _SUBPATH_ONLY:
            assert re.search(rf"\^/{prefix}[(/]", text), (
                f"命名空间 {prefix} 声明为子路径放行，但模板里没有对应 location"
            )
        else:
            assert prefix in covered, (
                f"后端命名空间 /{prefix} 不在 nginx 放行清单里——该命名空间的"
                "请求会落到 Next 返回 404 HTML（客户端视角就是功能坏了）"
            )


def test_nginx_template_forwards_transcode_worker_websocket_upgrade() -> None:
    """外置 Worker 的 WebSocket 握手不能被通用 API 反代降级成普通 GET。"""
    text = _TEMPLATE.read_text(encoding="utf-8")
    assert re.search(
        r"map\s+\$http_upgrade\s+\$movieclaw_connection_upgrade\s*\{"
        r".*?default\s+upgrade;.*?\"\"\s+close;.*?\}",
        text,
        flags=re.DOTALL,
    )

    location = re.search(
        r"location\s*=\s*/api/v1/transcode-worker/ws\s*\{(?P<body>.*?)\n\s*\}",
        text,
        flags=re.DOTALL,
    )
    assert location, "nginx 模板缺少外置转码 Worker 的精确 WebSocket location"
    body = location.group("body")
    for directive in (
        "proxy_http_version 1.1;",
        "proxy_set_header Upgrade $http_upgrade;",
        "proxy_set_header Connection $movieclaw_connection_upgrade;",
    ):
        assert directive in body, f"WebSocket location 缺少 {directive}"
