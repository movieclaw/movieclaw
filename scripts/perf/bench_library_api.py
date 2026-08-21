#!/usr/bin/env python3
"""媒体库读路径的接口压测脚本（串行采样 P50/P95，另附并发场景）。

只测**读**：媒体库首页、单库海报墙分页、A-Z 索引、条目详情、搜索、
待识别清单——这些是用户每天都要走的路径，也是体量上来后最先劣化的路径。

用法::

    python scripts/perf/bench_library_api.py \
        --base http://127.0.0.1:8000/api/v1 \
        --user admin --password '...' --repeat 12
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx


async def _sample(client: httpx.AsyncClient, method: str, url: str,
                  repeat: int) -> dict:
    """串行打 repeat 次，返回耗时分布与响应体大小。"""
    # 预热一次：首次请求要建连接、编译 SQL、填 lru_cache，不计入分布
    warm = await client.request(method, url)
    times: list[float] = []
    size = len(warm.content)
    for _ in range(repeat):
        started = time.perf_counter()
        response = await client.request(method, url)
        times.append((time.perf_counter() - started) * 1000)
        size = len(response.content)
    times.sort()
    return {
        "url": url,
        "status": warm.status_code,
        "bytes": size,
        "min": round(times[0], 1),
        "p50": round(statistics.median(times), 1),
        "p95": round(times[min(len(times) - 1, int(len(times) * 0.95))], 1),
        "max": round(times[-1], 1),
    }


async def _burst(client: httpx.AsyncClient, urls: list[str]) -> dict:
    """并发场景：一次性打出一批请求，测「整屏加载完」的墙钟时间。

    媒体库首页正是这个形态——1 个库列表 + N 个逐库条目请求同时飞出去。
    """
    started = time.perf_counter()
    responses = await asyncio.gather(*(client.get(u) for u in urls))
    elapsed = (time.perf_counter() - started) * 1000
    return {
        "count": len(urls),
        "wall_ms": round(elapsed, 1),
        "bytes": sum(len(r.content) for r in responses),
        "worst_status": max(r.status_code for r in responses),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--repeat", type=int, default=12)
    parser.add_argument("--out", default=None, help="结果写入 JSON 文件")
    args = parser.parse_args()

    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    async with httpx.AsyncClient(base_url=args.base, timeout=120.0,
                                 limits=limits) as client:
        login = await client.post(
            "/auth/login",
            json={"username": args.user, "password": args.password},
        )
        login.raise_for_status()

        libs = (await client.get("/libraries")).json()["data"]
        by_files = sorted(libs, key=lambda l: l["stats"]["file_count"], reverse=True)
        big_movie = next(l for l in by_files if l["kind"] == "movie")
        big_tv = next(l for l in by_files if l["kind"] == "tv")
        small = by_files[-1]

        # 大库里挑一个真实条目 id，用于详情页采样
        wall = (await client.get(
            f"/libraries/{big_tv['id']}/items",
            params={"sort": "title", "limit": 1},
        )).json()["data"]
        detail_item = wall[0]["media_item_id"] if wall else None

        cases: list[tuple[str, str, str]] = [
            ("媒体库首页·库列表", "GET", "/libraries"),
            ("媒体库首页·最近观看", "GET", "/playback/recent?limit=20"),
            ("单库海报墙·首屏(按标题,60)", "GET",
             f"/libraries/{big_movie['id']}/items?sort=title&limit=60&offset=0"),
            ("单库海报墙·翻到中段(按标题,60)", "GET",
             f"/libraries/{big_movie['id']}/items?sort=title&limit=60&offset=4800"),
            ("单库海报墙·翻到尾部(按标题,60)", "GET",
             f"/libraries/{big_movie['id']}/items?sort=title&limit=60&offset=9000"),
            ("单库海报墙·最近添加(60)", "GET",
             f"/libraries/{big_movie['id']}/items?sort=added_at&limit=60&offset=0"),
            ("单库海报墙·补探优先(60)", "GET",
             f"/libraries/{big_movie['id']}/items?sort=probing&limit=60&offset=0"),
            ("剧集大库海报墙·首屏(按标题,60)", "GET",
             f"/libraries/{big_tv['id']}/items?sort=title&limit=60&offset=0"),
            ("剧集大库海报墙·最近添加(60)", "GET",
             f"/libraries/{big_tv['id']}/items?sort=added_at&limit=60&offset=0"),
            ("小库海报墙·首屏(按标题,60)", "GET",
             f"/libraries/{small['id']}/items?sort=title&limit=60&offset=0"),
            ("A-Z 索引条(电影大库)", "GET",
             f"/libraries/{big_movie['id']}/item-index"),
            ("A-Z 索引条(剧集大库)", "GET",
             f"/libraries/{big_tv['id']}/item-index"),
            ("已入库 id 集合(电影大库)", "GET",
             f"/libraries/{big_movie['id']}/item-ids"),
            ("媒体库搜索(关键词)", "GET", "/search/library-items?keyword=长安"),
            ("待识别清单(全部库)", "GET",
             "/libraries/identification/unidentified-files"),
            ("已忽略清单(全部库)", "GET",
             "/libraries/identification/ignored-files"),
            ("身份复核清单", "GET", "/libraries/identification/review-cases"),
        ]
        if detail_item is not None:
            cases.append((
                "条目详情(剧集大库)", "GET",
                f"/libraries/{big_tv['id']}/items/{detail_item}",
            ))

        print(f"{'场景':<34}{'P50(ms)':>10}{'P95(ms)':>10}"
              f"{'Max(ms)':>10}{'响应(KB)':>12}")
        print("-" * 78)
        results = []
        for label, method, url in cases:
            try:
                row = await _sample(client, method, url, args.repeat)
            except Exception as exc:  # 某些环境下部分接口不可用，不中断整轮
                print(f"{label:<34}  跳过：{exc}")
                continue
            row["label"] = label
            results.append(row)
            print(f"{label:<34}{row['p50']:>10}{row['p95']:>10}"
                  f"{row['max']:>10}{row['bytes'] / 1024:>12.1f}")

        # —— 首页的真实形态：1 + N 个请求同时飞出 ——
        print("\n【媒体库首页并发形态】1 次库列表 + 每库一次「最近添加 20」")
        urls = ["/libraries"] + [
            f"/libraries/{l['id']}/items?sort=added_at&limit=20" for l in libs
        ]
        burst = await _burst(client, urls)
        print(f"  请求数 {burst['count']}，全部完成墙钟 {burst['wall_ms']} ms，"
              f"合计 {burst['bytes'] / 1024:.0f} KB")
        # 浏览器 HTTP/1.1 对单域名并发上限约 6，这里模拟真实浏览器的排队
        print("\n【模拟浏览器 6 并发上限】同一批请求，信号量限 6")
        gate = asyncio.Semaphore(6)

        async def _gated(url: str):
            async with gate:
                return await client.get(url)

        started = time.perf_counter()
        await asyncio.gather(*(_gated(u) for u in urls))
        print(f"  全部完成墙钟 {(time.perf_counter() - started) * 1000:.0f} ms")

        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump({"cases": results, "burst": burst}, fh,
                          ensure_ascii=False, indent=2)
            print(f"\n结果已写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
