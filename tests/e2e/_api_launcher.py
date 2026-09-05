"""浏览器端到端专用的后端启动器（由 test_other_library_browser.py 以子进程拉起）。

与生产入口同一 ``create_app``，差别只有三处，全部是测试环境的现实约束：

- "文件写入中"的两处静默窗口调成 0——假视频是刚生成的，5 分钟静默等不起；
- TMDB 指到本地假服务（``_fake_tmdb``），沙箱没有 Key 也没有外网；
- 去掉代理环境变量，本机回环请求不走沙箱代理。
"""

from __future__ import annotations

import os
from pathlib import Path

for _var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_var, None)

from _fake_tmdb import serve  # noqa: E402  (同目录，以脚本方式运行)

_server, _port = serve(Path(os.environ["E2E_TMDB_LOG"]))
os.environ["TMDB_API_KEY"] = "e2e-fake-key"
os.environ["TMDB_API_BASE_URL"] = f"http://127.0.0.1:{_port}/3"

import uvicorn  # noqa: E402

import movieclaw_api.services.library.ingest as ingest_mod  # noqa: E402
import movieclaw_api.services.library.scan as scan_mod  # noqa: E402

ingest_mod.QUIET_SECONDS = 0
scan_mod.NEW_FILE_QUIET_SECONDS = 0

from movieclaw_api.app import create_app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=int(os.environ["APP_PORT"]),
        log_config=None,
        access_log=False,
    )
