#!/usr/bin/env python3
"""媒体库前端端到端体验压测（Playwright + Chromium，真实浏览器）。

模拟一个真实用户在大体量媒体库下的完整动线：

    登录 → 媒体库首页 → 滚动首页 → 进单库海报墙 → 滚动加载
         → 点 A-Z 索引跳档 → 进条目详情 → 返回

每一步都采集用户能感知到的指标：导航耗时、FCP/LCP、首屏出现内容的时刻、
XHR 请求数与总字节、长任务（阻塞主线程 >50ms）总时长。

用法::

    python scripts/perf/e2e_library_ux.py --web http://127.0.0.1:3000 \
        --user admin --password '...'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from playwright.async_api import async_playwright

def _chromium_path() -> str | None:
    """本机预装的 Chromium（会话环境已装好，不联网下载）；找不到就交给
    Playwright 自己的解析逻辑。"""
    import glob

    for pattern in (
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/opt/pw-browsers/chromium/chrome-linux/chrome",
    ):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


CHROMIUM = _chromium_path()

# 页面上「首屏内容真的出现了」的判定选择器（骨架屏不算）
LIBRARY_CARD = "[data-library-card]"


class Trace:
    """一次页面动线的网络与耗时记录。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.marks: list[tuple[str, float]] = []

    def attach(self, page) -> None:
        page.on("response", self._on_response)

    async def _on_response(self, response) -> None:
        url = response.url
        if "/api/v1/" not in url and "/images/" not in url:
            return
        try:
            body = await response.body()
            size = len(body)
        except Exception:
            size = 0
        self.requests.append({
            "url": url.split("://", 1)[-1].split("/", 1)[-1],
            "status": response.status,
            "bytes": size,
        })

    def summary(self, prefix: str = "/api/v1/") -> dict:
        rows = [r for r in self.requests if prefix.lstrip("/") in r["url"]]
        return {
            "count": len(rows),
            "bytes": sum(r["bytes"] for r in rows),
            "errors": sum(1 for r in rows if r["status"] >= 400),
        }

    def top(self, n: int = 6) -> list[dict]:
        buckets: dict[str, dict] = {}
        for row in self.requests:
            # 把 /libraries/<id>/items 这类聚成一个模式看总量
            parts = row["url"].split("?")[0].split("/")
            pattern = "/".join(
                "{id}" if p.isdigit() else p for p in parts
            )
            b = buckets.setdefault(pattern, {"pattern": pattern, "count": 0, "bytes": 0})
            b["count"] += 1
            b["bytes"] += row["bytes"]
        return sorted(buckets.values(), key=lambda b: -b["count"])[:n]


PERF_SCRIPT = """() => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const paints = {};
  for (const p of performance.getEntriesByType('paint')) paints[p.name] = p.startTime;
  const longTasks = window.__longTasks || [];
  return {
    ttfb: nav.responseStart || null,
    domContentLoaded: nav.domContentLoadedEventEnd || null,
    load: nav.loadEventEnd || null,
    fcp: paints['first-contentful-paint'] || null,
    lcp: window.__lcp || null,
    longTaskCount: longTasks.length,
    longTaskMs: longTasks.reduce((a, b) => a + b, 0),
    longTaskMax: longTasks.length ? Math.max(...longTasks) : 0,
  };
}"""

# 在文档脚本之前注入：LCP 与长任务观察器必须先于页面脚本装好
INIT_SCRIPT = """
window.__longTasks = [];
try {
  new PerformanceObserver((l) => {
    for (const e of l.getEntries()) window.__longTasks.push(e.duration);
  }).observe({ type: 'longtask', buffered: true });
} catch (e) {}
try {
  new PerformanceObserver((l) => {
    const entries = l.getEntries();
    window.__lcp = entries[entries.length - 1].startTime;
  }).observe({ type: 'largest-contentful-paint', buffered: true });
} catch (e) {}
"""


async def _wait_settled(page, idle_ms: int = 1200, timeout_ms: int = 60_000) -> float:
    """等到 API 请求安静下来（连续 idle_ms 没有新的 /api/v1 响应）。

    networkidle 在有轮询的页面上永远等不到；这里只看业务接口。
    """
    started = time.perf_counter()
    last = {"t": time.monotonic()}

    def bump(response):
        if "/api/v1/" in response.url:
            last["t"] = time.monotonic()

    page.on("response", bump)
    try:
        while (time.perf_counter() - started) * 1000 < timeout_ms:
            if (time.monotonic() - last["t"]) * 1000 > idle_ms:
                break
            await asyncio.sleep(0.05)
    finally:
        page.remove_listener("response", bump)
    return (time.perf_counter() - started) * 1000 - idle_ms


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", default="http://127.0.0.1:3000")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--library", type=int, default=1, help="单库页压测用的库 id")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    report: dict = {"steps": []}

    def step(name: str, **fields) -> None:
        report["steps"].append({"step": name, **fields})
        detail = "  ".join(
            f"{k}={v if not isinstance(v, float) else round(v, 1)}"
            for k, v in fields.items()
        )
        print(f"  {name:<28} {detail}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=CHROMIUM,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        # 桌面 1440x900、无节流：先量「最好情况」，再单独跑一轮 4× CPU 节流
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        await context.add_init_script(INIT_SCRIPT)
        page = await context.new_page()
        trace = Trace()
        trace.attach(page)

        # —— 登录 ————————————————————————————————————
        print("\n【步骤 1】登录")
        t = time.perf_counter()
        await page.goto(f"{args.web}/login", wait_until="domcontentloaded")
        await page.fill('input[name="username"], input[type="text"]', args.user)
        await page.fill('input[type="password"]', args.password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(lambda u: "/login" not in u, timeout=60_000)
        step("登录并跳转", 耗时ms=(time.perf_counter() - t) * 1000)

        # —— 媒体库首页 ————————————————————————————
        print("\n【步骤 2】媒体库首页（冷加载）")
        trace.requests.clear()
        t = time.perf_counter()
        await page.goto(f"{args.web}/library", wait_until="domcontentloaded")
        nav_ms = (time.perf_counter() - t) * 1000
        # 首屏「库卡片真的画出来了」的时刻
        t_card = time.perf_counter()
        await page.wait_for_selector(LIBRARY_CARD, timeout=60_000)
        card_ms = (time.perf_counter() - t_card) * 1000
        settle_ms = await _wait_settled(page)
        perf = await page.evaluate(PERF_SCRIPT)
        api = trace.summary()
        step("导航(DOMContentLoaded)", 耗时ms=nav_ms)
        step("首张库卡片可见", 再等ms=card_ms, 累计ms=nav_ms + card_ms)
        step("整页数据加载完成", 再等ms=settle_ms,
             累计ms=nav_ms + card_ms + settle_ms)
        step("Web Vitals", FCP=perf["fcp"], LCP=perf["lcp"],
             长任务数=perf["longTaskCount"], 长任务总ms=perf["longTaskMs"],
             最长任务ms=perf["longTaskMax"])
        step("接口请求", 请求数=api["count"], 总字节KB=round(api["bytes"] / 1024, 1),
             失败数=api["errors"])
        print("  首页接口分布：")
        for row in trace.top():
            print(f"    {row['pattern']:<46} × {row['count']:<4} "
                  f"{row['bytes'] / 1024:>8.1f} KB")
        report["home_patterns"] = trace.top(10)

        # —— 首页停留 30s：观察轮询开销 ————————————————
        print("\n【步骤 3】首页停留 35 秒（观察轮询开销）")
        trace.requests.clear()
        await asyncio.sleep(35)
        api = trace.summary()
        step("35 秒轮询", 请求数=api["count"],
             总字节KB=round(api["bytes"] / 1024, 1),
             每分钟请求数=round(api["count"] * 60 / 35, 1))

        # —— 单库海报墙 ————————————————————————————
        print(f"\n【步骤 4】进入单库海报墙（library {args.library}）")
        trace.requests.clear()
        t = time.perf_counter()
        await page.goto(f"{args.web}/library/{args.library}",
                        wait_until="domcontentloaded")
        nav_ms = (time.perf_counter() - t) * 1000
        t_grid = time.perf_counter()
        await page.wait_for_selector("a[href*='/item/']", timeout=90_000)
        grid_ms = (time.perf_counter() - t_grid) * 1000
        settle_ms = await _wait_settled(page)
        perf = await page.evaluate(PERF_SCRIPT)
        api = trace.summary()
        step("导航(DOMContentLoaded)", 耗时ms=nav_ms)
        step("首格海报可见", 再等ms=grid_ms, 累计ms=nav_ms + grid_ms)
        step("整页数据加载完成", 再等ms=settle_ms)
        step("Web Vitals", FCP=perf["fcp"], LCP=perf["lcp"],
             长任务数=perf["longTaskCount"], 长任务总ms=perf["longTaskMs"])
        step("接口请求", 请求数=api["count"],
             总字节KB=round(api["bytes"] / 1024, 1))

        # —— 滚动加载 ————————————————————————————
        print("\n【步骤 5】海报墙连续滚动 8 屏（滚动加载）")
        trace.requests.clear()
        scroll_lat = []
        for i in range(8):
            t = time.perf_counter()
            await page.mouse.wheel(0, 2400)
            await page.wait_for_timeout(700)
            scroll_lat.append((time.perf_counter() - t) * 1000)
        settle_ms = await _wait_settled(page, idle_ms=1500)
        perf = await page.evaluate(PERF_SCRIPT)
        api = trace.summary()
        step("滚动 8 屏", 触发请求数=api["count"],
             拉取KB=round(api["bytes"] / 1024, 1), 之后等待ms=settle_ms)
        step("滚动期间主线程", 长任务数=perf["longTaskCount"],
             长任务总ms=perf["longTaskMs"], 最长任务ms=perf["longTaskMax"])
        node_count = await page.evaluate("() => document.querySelectorAll('*').length")
        step("DOM 节点数", 节点=node_count)

        # —— A-Z 索引跳档 ————————————————————————
        print("\n【步骤 6】点 A-Z 索引跳档")
        jump = await page.query_selector_all("[data-wall-index] button, [data-index-letter]")
        if jump:
            trace.requests.clear()
            t = time.perf_counter()
            await jump[min(len(jump) - 1, 12)].click()
            await _wait_settled(page, idle_ms=900)
            step("跳档到位", 耗时ms=(time.perf_counter() - t) * 1000,
                 请求数=trace.summary()["count"])
        else:
            step("跳档到位", 说明="未找到索引条元素，跳过")

        # —— 条目详情 ————————————————————————————
        print("\n【步骤 7】打开条目详情")
        trace.requests.clear()
        link = await page.query_selector("a[href*='/item/']")
        t = time.perf_counter()
        await link.click()
        await page.wait_for_url("**/item/**", timeout=60_000)
        await _wait_settled(page, idle_ms=900)
        step("详情页可交互", 耗时ms=(time.perf_counter() - t) * 1000,
             请求数=trace.summary()["count"],
             KB=round(trace.summary()["bytes"] / 1024, 1))

        # —— 返回海报墙（用户最频繁的往返）——————————
        print("\n【步骤 8】返回海报墙（浏览器后退）")
        trace.requests.clear()
        t = time.perf_counter()
        await page.go_back()
        await page.wait_for_selector("a[href*='/item/']", timeout=90_000)
        await _wait_settled(page, idle_ms=900)
        step("后退回到海报墙", 耗时ms=(time.perf_counter() - t) * 1000,
             请求数=trace.summary()["count"])

        # —— 4× CPU 节流下重跑首页（模拟 NAS / 老设备）————
        print("\n【步骤 9】4× CPU 节流下重跑媒体库首页（模拟低端设备）")
        client = await page.context.new_cdp_session(page)
        await client.send("Emulation.setCPUThrottlingRate", {"rate": 4})
        trace.requests.clear()
        t = time.perf_counter()
        await page.goto(f"{args.web}/library", wait_until="domcontentloaded")
        await page.wait_for_selector(LIBRARY_CARD, timeout=120_000)
        card_ms = (time.perf_counter() - t) * 1000
        settle_ms = await _wait_settled(page)
        perf = await page.evaluate(PERF_SCRIPT)
        step("首张库卡片可见(4×节流)", 累计ms=card_ms)
        step("整页数据加载完成(4×节流)", 再等ms=settle_ms, 总计ms=card_ms + settle_ms)
        step("Web Vitals(4×节流)", FCP=perf["fcp"], LCP=perf["lcp"],
             长任务总ms=perf["longTaskMs"])
        await client.send("Emulation.setCPUThrottlingRate", {"rate": 1})

        await browser.close()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"\n结果已写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
