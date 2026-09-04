"""浏览器端到端专用的后端启动器（由 test_other_library_browser.py 以子进程拉起）。

与生产入口同一 ``create_app``，只把"文件写入中"的两处静默窗口调成 0：
测试用的假视频是刚生成的，5 分钟静默会让扫描与监听导入都等不起。
"""

from __future__ import annotations

import os

import uvicorn

import movieclaw_api.services.library.ingest as ingest_mod
import movieclaw_api.services.library.scan as scan_mod

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
