#!/usr/bin/env python3
"""把 ``bench_disk_io.py`` 的 strace 输出按阶段切开、按路径归因。

``bench_disk_io`` 在每个场景前后各 stat 一次 ``/__MARK__/BEGIN-<名>`` 与
``/__MARK__/END-<名>``（注定 ENOENT，零副作用），本脚本据此切分阶段。

每个阶段给四个数：``open`` / ``read`` / 元数据（stat、getdents 等）/ **写**。
写单列出来是因为它在 NAS 上最贵——读还能被页缓存挡下来，写迟早要落盘，
还会把休眠的硬盘唤醒。后面跟着命中最多的路径（同类文件聚成一桶）。

用法::

    python scripts/perf/analyze_strace.py /tmp/iolab/trace
    python scripts/perf/analyze_strace.py before.trace after.trace   # 并排对比
"""

from __future__ import annotations

import re
import signal
import sys
from collections import Counter, defaultdict

CALL = re.compile(r"^(?:\[pid\s+(\d+)\]\s+|(\d+)\s+)?(?:[\d.]+\s+)?([a-z_0-9]+)\((.*)$")
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
MARK = re.compile(r"/__MARK__/(BEGIN|END)-([\w.-]+)")

WRITE_CALLS = {
    "write", "pwrite64", "writev", "fsync", "fdatasync", "sync_file_range",
    "utimensat", "utimes", "rename", "renameat", "renameat2", "unlink",
    "unlinkat", "mkdir", "mkdirat", "ftruncate", "truncate",
}
READ_CALLS = {"read", "pread64", "readv", "preadv"}
META_CALLS = {
    "stat", "lstat", "newfstatat", "statx", "fstat", "access", "faccessat",
    "faccessat2", "readlink", "readlinkat", "getdents64", "getdents",
}
OPEN_CALLS = {"open", "openat", "openat2"}
TRACKED = WRITE_CALLS | READ_CALLS | META_CALLS | OPEN_CALLS | {"close"}

#: 这些调用的第一个带引号参数是**数据缓冲区**而不是路径，只能靠 fd 反查
BY_FD = READ_CALLS | {
    "write", "pwrite64", "writev", "close", "fsync", "fdatasync", "ftruncate", "fstat"
}


def _bucket(name: str) -> str:
    """把同一类文件聚成一桶，免得 Top 榜被几百个同类条目刷屏。"""
    for key in ("/metadata/images/", "/cache/images/"):
        if key in name:
            tail = name.partition(key)[2]
            return f"…{key}*.{tail.rsplit('.', 1)[-1] if '.' in tail else '无扩展名'}"
    if name.endswith(".nfo"):
        return "…/*.nfo"
    if "/media/" in name:
        suffix = "." + name.rsplit(".", 1)[-1] if "." in name.rsplit("/", 1)[-1] else "（目录）"
        return f"…/media/*{suffix}"
    for db in ("movieclaw.db-wal", "movieclaw.db-shm", "movieclaw.db"):
        if name.endswith(db):
            return db
    return name


def analyse(path: str) -> tuple[list[str], dict[str, Counter], dict[str, Counter]]:
    phase = "(启动)"
    order = [phase]
    per_phase: dict[str, Counter] = defaultdict(Counter)
    paths: dict[str, Counter] = defaultdict(Counter)
    fds: dict[tuple[str, str], str] = {}

    with open(path, errors="replace") as handle:
        for line in handle:
            matched = CALL.match(line)
            if not matched:
                continue
            pid = matched.group(1) or matched.group(2) or "0"
            call, rest = matched.group(3), matched.group(4)
            marker = MARK.search(rest)
            if marker:
                if marker.group(1) == "BEGIN":
                    phase = marker.group(2)
                    if phase not in order:
                        order.append(phase)
                else:
                    phase = "(阶段之间)"
                continue
            if call not in TRACKED:
                continue
            per_phase[phase][call] += 1
            arg = QUOTED.search(rest)
            if call in OPEN_CALLS and arg:
                returned = rest.rsplit("= ", 1)[-1].strip()
                if returned.isdigit():
                    fds[(pid, returned)] = arg.group(1)
            if call in BY_FD:
                name = fds.get((pid, rest.split(",", 1)[0].strip()))
            else:
                name = arg.group(1) if arg else None
            if name:
                paths[phase][_bucket(name)] += 1
    return order, per_phase, paths


def _totals(counter: Counter) -> tuple[int, int, int, int, int]:
    return (
        sum(counter.values()),
        sum(counter[k] for k in OPEN_CALLS),
        sum(counter[k] for k in READ_CALLS),
        sum(counter[k] for k in META_CALLS),
        sum(counter[k] for k in WRITE_CALLS),
    )


def report(path: str, top: int = 12) -> None:
    order, per_phase, paths = analyse(path)
    for phase in order + [p for p in per_phase if p not in order]:
        counter = per_phase[phase]
        if not counter:
            continue
        total, opens, reads, meta, writes = _totals(counter)
        print(f"\n=== {phase} === 合计 {total}  open={opens} read={reads} "
              f"元数据={meta} 写={writes}")
        print("   ", dict(counter.most_common(10)))
        for name, count in paths[phase].most_common(top):
            print(f"      {count:7d}  {name[:100]}")


def compare(before: str, after: str) -> None:
    _, left, _ = analyse(before)
    order, right, _ = analyse(after)
    print(f"{'场景':<16}{'改前':>9}{'改后':>9}{'变化':>8}   "
          f"{'改前写':>8}{'改后写':>8}{'写变化':>8}")
    print("-" * 76)
    for phase in order:
        if phase not in left or phase not in right:
            continue
        a_total, _, _, _, a_write = _totals(left[phase])
        b_total, _, _, _, b_write = _totals(right[phase])

        def pct(x: int, y: int) -> str:
            return "   —  " if x == 0 else f"{(y - x) / x * 100:+6.0f}%"

        print(f"{phase:<16}{a_total:>9}{b_total:>9}{pct(a_total, b_total):>8}   "
              f"{a_write:>8}{b_write:>8}{pct(a_write, b_write):>8}")


if __name__ == "__main__":
    # 报告很长，`| head` 是常态；管道提前关掉时安静退出而不是打一屏 traceback
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    if len(sys.argv) == 2:
        report(sys.argv[1])
    elif len(sys.argv) == 3:
        compare(sys.argv[1], sys.argv[2])
    else:
        print(__doc__)
        raise SystemExit(2)
