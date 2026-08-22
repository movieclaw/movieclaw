#!/usr/bin/env python3
"""网页播放器的真实浏览器质量压测（docs/design/web-player.md §9.5-D）。

在真浏览器里把一部片播起来，采集 CTA-2066 口径的 QoE 读数：

    登录 → 打开播放页 → 等首帧 → 播一段 → 拖两次进度条 → 汇总

采集的每一项都在页面里用**播放器自己的口径**算，而不是从外面猜：

- **首帧只能用 ``requestVideoFrameCallback``**。``canplay`` / ``playing``
  全都早于真实出画（有时早几百毫秒），用它们量会系统性偏乐观。
- **卡顿要排除 seek 引起的 ``waiting``**，否则拖一下进度条就被记一次。

用法::

    python scripts/perf/e2e_player_qoe.py --web http://127.0.0.1:3000 \\
        --user admin --password '...' --library 1 --item 42

与 ``e2e_library_ux.py`` 同一套做法：Playwright + 本机预装 Chromium，
不联网下载浏览器。**注意**：Chromium 在 Linux 上没有 HEVC 硬解，HEVC 直通
路径只能在 macOS 的 Safari 上人验（见 SKILL.md 第五节）。
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


CHROMIUM = _chromium_path()

#: 注入页面的采集器。挂在 window 上，播完再读回来。
#: 逻辑与 apps/web/lib/player/qoe.ts 同源——那边有单测，这里只是复述口径，
#: 两边如果漂了，以 qoe.ts 为准。
COLLECTOR = """
() => {
  const state = {
    startedAt: performance.now(),
    firstFrameAt: null,
    waitingSince: null,
    inSeek: false,
    rebufferMs: 0,
    rebufferCount: 0,
    seekCount: 0,
  };
  window.__qoe = state;
  const attach = (video) => {
    if (!video || video.__qoeAttached) return;
    video.__qoeAttached = true;
    state.video = video;
    video.requestVideoFrameCallback?.(() => {
      if (state.firstFrameAt === null) state.firstFrameAt = performance.now();
    });
    video.addEventListener('seeking', () => {
      state.inSeek = true;
      state.seekCount += 1;
      state.waitingSince = null;
    });
    video.addEventListener('waiting', () => {
      if (state.inSeek || state.waitingSince !== null) return;
      state.waitingSince = performance.now();
    });
    video.addEventListener('playing', () => {
      if (state.waitingSince !== null) {
        state.rebufferMs += performance.now() - state.waitingSince;
        state.rebufferCount += 1;
        state.waitingSince = null;
      }
      state.inSeek = false;
    });
  };
  const existing = document.querySelector('video');
  if (existing) attach(existing);
  new MutationObserver(() => attach(document.querySelector('video'))).observe(
    document.documentElement, { childList: true, subtree: true },
  );
}
"""

READ_BACK = """
() => {
  const s = window.__qoe || {};
  const v = s.video;
  const q = v?.getVideoPlaybackQuality?.();
  return {
    ttff_ms: s.firstFrameAt === null ? null : Math.round(s.firstFrameAt - s.startedAt),
    rebuffer_ms: Math.round(s.rebufferMs || 0),
    rebuffer_count: s.rebufferCount || 0,
    seek_count: s.seekCount || 0,
    dropped_frames: q?.droppedVideoFrames ?? null,
    total_frames: q?.totalVideoFrames ?? null,
    current_time: v?.currentTime ?? null,
    ready_state: v?.readyState ?? null,
  };
}
"""


async def run(args: argparse.Namespace) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=CHROMIUM, args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        await page.goto(f"{args.web}/login", wait_until="domcontentloaded")
        await page.fill('input[name="username"]', args.user)
        await page.fill('input[type="password"]', args.password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(lambda url: "/login" not in url, timeout=30_000)

        # 采集器要在播放页脚本之前挂上，否则会漏掉首帧
        await page.add_init_script(f"({COLLECTOR})()")
        await page.goto(
            f"{args.web}/play/{args.library}/{args.item}", wait_until="domcontentloaded"
        )

        try:
            await page.wait_for_function(
                "() => window.__qoe && window.__qoe.firstFrameAt !== null",
                timeout=args.timeout * 1000,
            )
        except Exception:
            return {"error": "超时未出画", **(await page.evaluate(READ_BACK))}

        await asyncio.sleep(args.play_seconds)
        # 拖两次进度条：一次往前、一次往回，验 seek 不被算成卡顿
        for ratio in (0.4, 0.15):
            await page.evaluate(
                "(r) => { const v = document.querySelector('video');"
                " if (v && isFinite(v.duration)) v.currentTime = v.duration * r; }",
                ratio,
            )
            await asyncio.sleep(3)

        result = await page.evaluate(READ_BACK)
        await browser.close()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", default="http://127.0.0.1:3000")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--library", required=True, help="媒体库 id")
    parser.add_argument("--item", required=True, help="媒体条目 id")
    parser.add_argument("--play-seconds", type=float, default=20.0)
    parser.add_argument("--timeout", type=float, default=60.0, help="等首帧的上限（秒）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    result = asyncio.run(run(args))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if "error" not in result else 1

    if "error" in result:
        print(f"✗ {result['error']}（readyState={result.get('ready_state')}）")
        return 1
    dropped = result["dropped_frames"]
    total = result["total_frames"]
    ratio = f"{dropped / total * 100:.2f}%" if dropped is not None and total else "—"
    print(f"首帧        {result['ttff_ms']} ms")
    print(f"卡顿        {result['rebuffer_ms']} ms / {result['rebuffer_count']} 次")
    print(f"拖动        {result['seek_count']} 次")
    print(f"掉帧        {dropped}/{total}（{ratio}）")
    print(f"播放位置    {result['current_time']:.1f}s" if result["current_time"] else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
