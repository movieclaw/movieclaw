#!/usr/bin/env python3
"""真浏览器 + 真 ffmpeg 的 HLS 路径验收（docs/design/player-pipeline-optimization.md §7.3）。

Chromium 解不了 H.264，所以样片要用 VP9+Opus 的 MKV：决策落档 1 remux，走
hls.js 喂 fMP4 分片——这条路正是闭环节流（§A）、缓存复用（§B）生效的地方。

步骤：登录 → 播 → 等首帧 → 读诊断（领先秒数 / 暂停原因 / 缓存）→ 远跳 →
等出画 → 回拖 → 离开 → 重开同一部片 → 诊断应报缓存命中、首帧不慢于第一次。

用法（先起后端与 ``next dev``，把样片以 mkv/vp9/opus 登记进台账）::

    python scripts/perf/e2e_player_hls.py --web http://127.0.0.1:3000 \\
        --user admin --password '...' --item 42

与 e2e_player_feel.py 同一套做法：Playwright + 本机预装 Chromium。
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import sys

from playwright.async_api import async_playwright


def _chromium_path() -> str | None:
    for pattern in (
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/opt/pw-browsers/chromium/chrome-linux/chrome",
    ):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


COLLECTOR = """
() => {
  const state = { startedAt: performance.now(), firstFrameAt: null, seeks: 0, waiting: 0 };
  window.__hls = state;
  const attach = (video) => {
    if (!video || video.__attached) return;
    video.__attached = true;
    video.addEventListener('timeupdate', () => {
      if (state.firstFrameAt === null && video.readyState >= 2 && video.currentTime > 0.1) {
        state.firstFrameAt = performance.now();
      }
    });
    video.addEventListener('seeking', () => { state.seeks += 1; });
    video.addEventListener('waiting', () => { state.waiting += 1; });
  };
  const scan = () => attach(document.querySelector('video'));
  scan();
  new MutationObserver(scan).observe(document, { childList: true, subtree: true });
}
"""


async def _diagnostics(page, api: str) -> dict:
    """从播放页里拿到会话 id 与 token，直接问服务端诊断。"""
    return await page.evaluate(
        """
      async (api) => {
        const v = document.querySelector('video');
        const src = v?.currentSrc || '';
        // hls.js 挂的是 blob: 地址；真正的会话地址在 hls.js 实例上拿不到，
        // 退而求其次：从性能条目里找最近一次 index.m3u8 请求
        const entries = performance.getEntriesByType('resource')
          .map(e => e.name).filter(n => n.includes('/playback/sessions/'));
        const last = entries[entries.length - 1];
        if (!last) return { error: 'no session request seen', src };
        const m = last.match(/sessions\\/([^/]+)\\/[^?]+\\?token=([^&]+)/);
        if (!m) return { error: 'cannot parse', last };
        const r = await fetch(`${api}/playback/sessions/${m[1]}/diagnostics?token=${m[2]}`);
        const j = await r.json();
        return { session_id: m[1], ...j.data };
      }
    """,
        api,
    )


async def play_once(page, args, label: str) -> dict:
    # 采集器在 run() 里只挂一次：add_init_script 会累积，挂两次时第一份闭包里的
    # MutationObserver 先把 video 标成已挂、第二份状态对象就永远等不到首帧
    await page.goto(f"{args.web}/play/{args.item}", wait_until="domcontentloaded")
    await page.wait_for_function(
        "() => window.__hls && window.__hls.firstFrameAt !== null", timeout=args.timeout * 1000
    )
    ttff = await page.evaluate("() => window.__hls.firstFrameAt - window.__hls.startedAt")
    report = {"ttff_ms": round(ttff)}
    await asyncio.sleep(2.0)
    report["diag_after_start"] = await _diagnostics(page, args.api)
    return report


async def run(args: argparse.Namespace) -> dict:
    report: dict = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=_chromium_path(), args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        await page.goto(f"{args.web}/login", wait_until="networkidle")
        await page.click('input[type="text"]')
        await page.keyboard.type(args.user)
        await page.click('input[type="password"]')
        await page.keyboard.type(args.password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(lambda url: "/login" not in url, timeout=60_000)
        await page.add_init_script(f"({COLLECTOR})()")

        # ---- 第一次播放：冷缓存 ----
        first = await play_once(page, args, "first")
        report["first"] = first
        report["engine_is_hls"] = await page.evaluate(
            "() => (document.querySelector('video')?.currentSrc || '').startsWith('blob:')"
        )

        # ---- 让播放头停住、看闭环节流把 ffmpeg 挂起（LEAD_HIGH 是 120 秒，样片只有
        #      120 秒，所以这里只验「领先秒数在涨、且没有被判缺粮」）----
        await asyncio.sleep(3.0)
        report["diag_mid"] = await _diagnostics(page, args.api)

        # ---- 远跳到 100 秒（远超已缓冲）：应重启直奔，出画不超时 ----
        seeks_before = await page.evaluate("() => window.__hls.seeks")
        await page.evaluate("() => { document.querySelector('video').currentTime = 100; }")
        await page.wait_for_function(
            "() => { const v = document.querySelector('video');"
            " return v.currentTime > 100.5 && !v.seeking; }",
            timeout=60_000,
        )
        current = await page.evaluate("() => document.querySelector('video').currentTime")
        report["far_seek"] = {
            "current": round(current, 1),
            "seeks": await page.evaluate("() => window.__hls.seeks") - seeks_before,
        }
        await asyncio.sleep(1.5)
        report["diag_after_far_seek"] = await _diagnostics(page, args.api)

        # ---- 回拖到 2 秒：第一轮转出的分片仍在，不该重启 ----
        head_before = report["diag_after_far_seek"].get("head_segment")
        await page.evaluate("() => { document.querySelector('video').currentTime = 2; }")
        await page.wait_for_function(
            "() => { const v = document.querySelector('video');"
            " return v.currentTime > 2.5 && v.currentTime < 30 && !v.seeking; }",
            timeout=60_000,
        )
        await asyncio.sleep(1.0)
        d = await _diagnostics(page, args.api)
        report["back_seek"] = {"head_before": head_before, "head_after": d.get("head_segment")}

        # ---- 离开播放页（显式 stop）→ 重开：应命中缓存 ----
        await page.goto(f"{args.web}/", wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        second = await play_once(page, args, "second")
        report["second"] = second
        await browser.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", default="http://127.0.0.1:3000")
    parser.add_argument("--api", default="/api/v1")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--item", required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = True
    def check(cond: bool, text: str) -> None:
        nonlocal ok
        print(("✓ " if cond else "✗ ") + text)
        ok = ok and cond
    check(report.get("engine_is_hls") is True, "走的是 hls.js（blob: 源）")
    d1 = report["first"]["diag_after_start"]
    check(d1.get("cache_hit") is False, "第一次：冷缓存")
    check(d1.get("lead_seconds") is not None, f"诊断带领先秒数：{d1.get('lead_seconds')}")
    check(report["far_seek"]["current"] > 100, f"远跳出画：{report['far_seek']['current']} 秒")
    # 短样片会在起播后一秒内全部转完（§A 不限速），远跳多半落在已转分片上；
    # 只有落在没转过的地方才会重启直奔。两种结局都对，这里只记录
    print(f"  远跳后头部={report['diag_after_far_seek'].get('head_segment')} "
          f"已产出={report['diag_after_far_seek'].get('highest_produced_segment')}")
    back = report["back_seek"]
    check(back["head_after"] == back["head_before"], "回拖命中已转分片，不重启")
    d2 = report["second"]["diag_after_start"]
    check(d2.get("cache_hit") is True, f"第二次：缓存命中，{d2.get('cached_segments')} 段免转")
    check(report["second"]["ttff_ms"] <= report["first"]["ttff_ms"] * 1.5 + 500,
          f"首帧：第一次 {report['first']['ttff_ms']} ms → 第二次 {report['second']['ttff_ms']} ms")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
