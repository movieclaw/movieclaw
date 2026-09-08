#!/usr/bin/env python3
"""播放器「手感」的端到端验收（docs/design/player-feel.md §0）。

在真浏览器里把一部真片播起来，逐条验那五个可观察的标准，而不是靠感觉：

1. **进度条匀速**：连续 1.2 秒逐帧采样已播段宽度，跟着 `timeupdate` 走
   （约 4Hz）时不同取值只有个位数，每帧自绘则接近采样数本身。
2. **双击左右跳转**：右三分之一连点两下 = 前进十秒。
3. **长按倍速**：按住 0.7 秒 → 2×，松手还原，且保音高。
4. **拖动跟手**：跳转不要钱时（档 0 直出 / 落点已缓冲）拖动中画面跟随。
5. **章节刻度**：轨道上有刻度，气泡里有章节名。
6. **iOS 伪横屏下坐标不串轴**：容器整体转 90° 之后，进度条与双击分区仍按
   用户眼里的左右走（2026-09-08 实测这里错过一次，见 player-feel.md §10）。
7. **触屏拖动时控制条不淡出**：拖着不动超过自动隐藏的倒计时，进度条不能在
   手指底下消失（同上，只在触屏踩得到，见 §12）。

**形态矩阵**：同一组交互要在三种形态下各跑一遍——桌面鼠标、触屏竖屏、触屏
伪横屏。**桌面会遮住触屏的 bug**（鼠标的 pointermove 顺带重排了控制条的自动
隐藏倒计时，触屏没有这条），所以触屏那两档必须用 CDP 派**真触摸事件**，
`page.mouse` 验不出来。

外加逐帧步进（暂停时 `.` 走一帧）。用法::

    python scripts/perf/e2e_player_feel.py --web http://127.0.0.1:3000 \\
        --user admin --password '...' --item 42

与 e2e_player_qoe.py 同一套做法：Playwright + 本机预装 Chromium，不联网
下载浏览器。**注意**：Playwright 的 Chromium 不含 H.264/AAC 专有解码器，
需要转码/HLS 的路径在它上面播不出画面——那条路的验收走
`tests/api/test_playback_e2e.py`（真 ffmpeg 拼流 + ffprobe）与真机。
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

#: 采集器。挂在 window 上，播完再读回来。
#:
#: **MutationObserver 必须挂在 `document` 而不是 `documentElement`**：初始化
#: 脚本跑在 documentElement 存在之前，`observe(null)` 会抛异常，把整段采集器
#: 从那一行起全部掐掉——表现是页面明明在播、采集器却一个读数都没有。
COLLECTOR = """
() => {
  const state = { startedAt: performance.now(), firstFrameAt: null, method: null, seeks: 0 };
  window.__feel = state;
  const attach = (video) => {
    if (!video || video.__feelAttached) return;
    video.__feelAttached = true;
    state.video = video;
    video.requestVideoFrameCallback?.(() => {
      if (state.firstFrameAt === null) {
        state.firstFrameAt = performance.now();
        state.method = 'rVFC';
      }
    });
    // headless 下 requestVideoFrameCallback 不一定发；「有画面数据且时间在走」
    // 是等价的兜底信号
    video.addEventListener('timeupdate', () => {
      if (state.firstFrameAt === null && video.readyState >= 2 && video.currentTime > 0.1) {
        state.firstFrameAt = performance.now();
        state.method = 'timeupdate';
      }
    });
    video.addEventListener('seeking', () => { state.seeks += 1; });
  };
  const scan = () => attach(document.querySelector('video'));
  scan();
  new MutationObserver(scan).observe(document, { childList: true, subtree: true });
}
"""


async def run(args: argparse.Namespace) -> dict:
    report: dict = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=CHROMIUM, args=["--autoplay-policy=no-user-gesture-required"]
        )
        # has_touch：双击跳转与长按倍速只认触摸指针（桌面双击是全屏）
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800}, has_touch=True
        )
        page = await context.new_page()

        # 受控表单只认真实键入：fill() 设的值不会让提交键解禁
        await page.goto(f"{args.web}/login", wait_until="networkidle")
        await page.click('input[type="text"]')
        await page.keyboard.type(args.user)
        await page.click('input[type="password"]')
        await page.keyboard.type(args.password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(lambda url: "/login" not in url, timeout=60_000)

        # 采集器要在播放页脚本之前挂上，否则会漏掉首帧
        await page.add_init_script(f"({COLLECTOR})()")
        await page.goto(f"{args.web}/play/{args.item}", wait_until="domcontentloaded")
        try:
            await page.wait_for_function(
                "() => window.__feel && window.__feel.firstFrameAt !== null",
                timeout=args.timeout * 1000,
            )
        except Exception:
            return {
                "error": "超时未出画",
                "video": await page.evaluate(
                    "() => { const v = document.querySelector('video');"
                    " return v ? { src: v.currentSrc, readyState: v.readyState,"
                    " error: v.error?.message, currentTime: v.currentTime } : null; }"
                ),
            }

        report["ttff_ms"] = round(
            await page.evaluate("() => window.__feel.firstFrameAt - window.__feel.startedAt")
        )
        report["ttff_method"] = await page.evaluate("() => window.__feel.method")
        # 回到片头附近：后面每一项都要前后留出余量
        await page.evaluate("() => { document.querySelector('video').currentTime = 10; }")
        await asyncio.sleep(1.5)

        # ---- 1. 进度条是不是每帧都在动 ----
        report["progress_paint"] = await page.evaluate(
            """
          async () => {
            const el = document.querySelector('[data-player-played]');
            const thumb = document.querySelector('[data-player-thumb]');
            if (!el) return null;
            const seen = [];
            let thumbInSync = true;
            const t0 = performance.now();
            while (performance.now() - t0 < 1200) {
              seen.push(el.style.width);
              // 已播段与圆点由同一次 paint 写，任何一帧对不上都是接线漏了一处
              if (thumb && thumb.style.left !== el.style.width) thumbInSync = false;
              await new Promise(r => requestAnimationFrame(r));
            }
            return { samples: seen.length, distinct: new Set(seen).size, thumbInSync };
          }
        """
        )

        # ---- 5. 章节刻度 + 气泡里的章节名 ----
        report["chapter_marks"] = await page.evaluate(
            "() => Array.from(document.querySelectorAll('.player-scrub-track > span'))"
            ".map(s => s.style.left)"
        )
        shade = await page.query_selector(".player-scrub-shade")
        if shade:
            box = await shade.bounding_box()
            await page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2)
            await asyncio.sleep(0.3)
            report["bubble"] = await page.evaluate(
                "() => Array.from(document.querySelectorAll('.player-scrub-shade p'))"
                ".map(p => p.textContent)"
            )

        # ---- 2. 双击右三分之一 = 前进十秒 ----
        before = await page.evaluate("() => document.querySelector('video').currentTime")
        vbox = await (await page.query_selector("video")).bounding_box()
        x = vbox["x"] + vbox["width"] * 0.8
        y = vbox["y"] + vbox["height"] * 0.45
        await page.touchscreen.tap(x, y)
        await asyncio.sleep(0.12)
        await page.touchscreen.tap(x, y)
        await asyncio.sleep(1.2)
        after = await page.evaluate("() => document.querySelector('video').currentTime")
        report["double_tap_forward_s"] = round(after - before, 2)

        # ---- 4. 拖动中画面跟随 ----
        scrub = await page.query_selector(".player-scrub")
        box = await scrub.bounding_box()
        y = box["y"] + box["height"] / 2
        await page.mouse.move(box["x"] + box["width"] * 0.25, y)
        await page.mouse.down()
        follow = []
        for ratio in (0.30, 0.35, 0.40):
            await page.mouse.move(box["x"] + box["width"] * ratio, y)
            await asyncio.sleep(0.15)
            follow.append(
                round(await page.evaluate("() => document.querySelector('video').currentTime"), 1)
            )
        await page.mouse.up()
        await asyncio.sleep(0.4)
        report["scrub_follow"] = follow

        # ---- 连按快进：跳转便宜时（档 0 直出）应当次次立刻生效 ----
        seeks_before = await page.evaluate("() => window.__feel.seeks")
        t_before = await page.evaluate("() => document.querySelector('video').currentTime")
        forward = await page.query_selector('button[aria-label="前进 10 秒"]')
        for _ in range(3):
            await forward.click()
            await asyncio.sleep(0.08)
        await asyncio.sleep(1.0)
        report["repeat_skip"] = {
            "delta_s": round(
                await page.evaluate("() => document.querySelector('video').currentTime") - t_before,
                1,
            ),
            "seeks": await page.evaluate("() => window.__feel.seeks") - seeks_before,
        }

        # ---- 3. 长按 = 2 倍速，松手还原 ----
        report["hold_speed"] = await page.evaluate(
            """
          async () => {
            const v = document.querySelector('video');
            const r = v.getBoundingClientRect();
            const fire = (type, x, y) => {
              const t = new Touch({ identifier: 1, target: v, clientX: x, clientY: y });
              v.dispatchEvent(new TouchEvent(type, { touches: type === 'touchend' ? [] : [t],
                changedTouches: [t], bubbles: true, cancelable: true }));
            };
            const x = r.left + r.width / 2, y = r.top + r.height / 2;
            fire('touchstart', x, y);
            await new Promise(res => setTimeout(res, 700));
            const during = v.playbackRate;
            fire('touchend', x, y);
            await new Promise(res => setTimeout(res, 100));
            return { during, after: v.playbackRate, preservesPitch: v.preservesPitch };
          }
        """
        )

        # ---- 逐帧步进（暂停时）----
        report["frame_step"] = await page.evaluate(
            """
          async () => {
            const v = document.querySelector('video');
            window.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
            await new Promise(r => setTimeout(r, 400));
            const settled = v.currentTime;
            await new Promise(r => setTimeout(r, 300));
            const drift = +(v.currentTime - settled).toFixed(3);
            const before = v.currentTime;
            window.dispatchEvent(new KeyboardEvent('keydown', { key: '.', bubbles: true }));
            await new Promise(r => setTimeout(r, 300));
            return { paused: v.paused, drift, step: +(v.currentTime - before).toFixed(3) };
          }
        """
        )
        report["fake_landscape"] = await run_fake_landscape(browser, args)
        report["touch_portrait"] = await run_touch_portrait(browser, args)
        await browser.close()
    return report


#: 模拟 iPhone Safari：它没有元素级全屏，播放器因此走 CSS 伪横屏
#: （整个容器 rotate(90deg)）。桌面 Chromium 有全屏，必须把它按掉才走得到那条路。
NO_ELEMENT_FULLSCREEN = """
() => {
  const reject = function () { return Promise.reject(new Error('no element fullscreen')); };
  try {
    const def = (name, value) =>
      Object.defineProperty(Element.prototype, name, { value, configurable: true });
    def('requestFullscreen', reject);
    def('webkitRequestFullscreen', undefined);
  } catch { /* 属性动不了就算了，下面会因为进不了伪横屏而跳过这一段 */ }
}
"""


async def run_fake_landscape(browser, args: argparse.Namespace) -> dict:
    """伪横屏下的坐标换算（手机尺寸视口 + 触屏）。

    单独开一个上下文：这条路只在**手机竖屏拿着、播放器自己转 90°** 时出现，
    与上面那套桌面尺寸的检查不是一回事。
    """
    context = await browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    # 必须是 IIFE：只传一个函数字面量的话它永远不会被调用（这里踩过）
    await context.add_init_script(f"({NO_ELEMENT_FULLSCREEN})()")
    page = await context.new_page()
    try:
        await page.goto(f"{args.web}/login", wait_until="networkidle")
        await page.click('input[type="text"]')
        await page.keyboard.type(args.user)
        await page.click('input[type="password"]')
        await page.keyboard.type(args.password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(lambda url: "/login" not in url, timeout=60_000)
        await page.goto(f"{args.web}/play/{args.item}", wait_until="domcontentloaded")
        await page.wait_for_function(
            "() => { const v = document.querySelector('video');"
            " return v && v.readyState >= 2 && v.currentTime > 0.5; }",
            timeout=args.timeout * 1000,
        )
        # 轻点唤出控制层 → 点横屏键（走的正是 iPhone 那条伪横屏）
        box = await page.evaluate(
            "() => { const b = document.querySelector('video').getBoundingClientRect();"
            " return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; }"
        )
        await page.touchscreen.tap(box["x"], box["y"])
        await asyncio.sleep(0.8)
        button = await page.query_selector('button[aria-label="横屏"]')
        if button:
            await button.click(timeout=5000)
        await asyncio.sleep(1.5)
        rotated = await page.evaluate(
            "() => document.querySelector('.player-root')"
            ".classList.contains('player-fake-landscape')"
        )
        if not rotated:
            return {"fake_landscape": False}

        await page.evaluate("() => { document.querySelector('video').currentTime = 10; }")
        await asyncio.sleep(1.2)
        bar = await page.evaluate(
            "() => { const b = document.querySelector('.player-scrub').getBoundingClientRect();"
            " return { left: b.left, top: b.top, w: b.width, h: b.height }; }"
        )
        # 用户眼里「从左往右拖」= 物理竖直方向：进度条转过来之后外接矩形是
        # 「厚 × 长」，拿 clientX/rect.width 算等于把整部片压进 44 个像素
        x = bar["left"] + bar["w"] / 2
        await page.mouse.move(x, bar["top"] + bar["h"] * 0.2)
        await page.mouse.down()
        for ratio in (0.4, 0.7):
            await page.mouse.move(x, bar["top"] + bar["h"] * ratio)
            await asyncio.sleep(0.25)
        await page.mouse.up()
        await asyncio.sleep(1.2)
        result = {
            "fake_landscape": True,
            "bar_aabb": {k: round(v) for k, v in bar.items()},
            "drag_to_70pct_s": round(
                await page.evaluate("() => document.querySelector('video').currentTime"), 1
            ),
        }
        video = await page.evaluate(
            "() => { const b = document.querySelector('video').getBoundingClientRect();"
            " return { left: b.left, top: b.top, w: b.width, h: b.height }; }"
        )
        tap_x = video["left"] + video["w"] / 2
        for key, fraction in (("double_tap_right_s", 0.85), ("double_tap_left_s", 0.15)):
            before = await page.evaluate("() => document.querySelector('video').currentTime")
            tap_y = video["top"] + video["h"] * fraction
            await page.touchscreen.tap(tap_x, tap_y)
            await asyncio.sleep(0.12)
            await page.touchscreen.tap(tap_x, tap_y)
            await asyncio.sleep(1.3)
            after = await page.evaluate("() => document.querySelector('video').currentTime")
            result[key] = round(after - before, 1)
        return result
    finally:
        await context.close()


async def run_touch_portrait(browser, args: argparse.Namespace) -> dict:
    """触屏竖屏：**只有真触摸事件才踩得到**的那几条。

    `page.mouse` 在这里没有意义——它发出的是鼠标指针事件，而播放器对鼠标和
    手指的处理本来就分岔（鼠标 move 会唤出控制层、顺带重排自动隐藏倒计时，
    手指不会）。所以这一档全程走 CDP 的 `Input.dispatchTouchEvent`。
    """
    context = await browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    page = await context.new_page()
    try:
        await page.goto(f"{args.web}/login", wait_until="networkidle")
        await page.click('input[type="text"]')
        await page.keyboard.type(args.user)
        await page.click('input[type="password"]')
        await page.keyboard.type(args.password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(lambda url: "/login" not in url, timeout=60_000)
        await page.goto(f"{args.web}/play/{args.item}", wait_until="domcontentloaded")
        await page.wait_for_function(
            "() => { const v = document.querySelector('video'); return v && v.readyState >= 2; }",
            timeout=args.timeout * 1000,
        )
        # 手机上下文里自动播放被浏览器拦下：按一下播放键。暂停时控制层本来就
        # 该常驻，那样验不出自动隐藏
        video_box = await page.evaluate(
            "() => { const b = document.querySelector('video').getBoundingClientRect();"
            " return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; }"
        )
        await page.touchscreen.tap(video_box["x"], video_box["y"])
        await asyncio.sleep(0.8)
        play = await page.query_selector('button[aria-label="播放"]')
        if play:
            await play.click()
        await asyncio.sleep(1.5)
        if await page.evaluate("() => document.querySelector('video').paused"):
            return {"playing": False}

        bar = await page.evaluate(
            "() => { const b = document.querySelector('.player-scrub').getBoundingClientRect();"
            " return { left: b.left, top: b.top, w: b.width, h: b.height }; }"
        )
        cdp = await context.new_cdp_session(page)
        y = bar["top"] + bar["h"] / 2
        x0 = bar["left"] + bar["w"] * 0.3
        touch_at = lambda kind, x: cdp.send(  # noqa: E731 - 三行样板不值得起个名字
            "Input.dispatchTouchEvent",
            {"type": kind, "touchPoints": [] if kind == "touchEnd" else [{"x": x, "y": y}]},
        )
        await touch_at("touchStart", x0)
        chrome_states = []
        for step in range(12):  # 6 秒 > 控制条 4 秒的自动隐藏倒计时
            await touch_at("touchMove", x0 + step * 1.5)
            await asyncio.sleep(0.5)
            chrome_states.append(
                await page.evaluate("() => document.querySelector('.player-root').dataset.chrome")
            )
        await touch_at("touchEnd", x0)
        return {"playing": True, "chrome_while_dragging": sorted(set(chrome_states))}
    finally:
        await context.close()


def verdicts(r: dict) -> list[tuple[bool, str]]:
    """把读数翻译成 §0 那几条验收标准的通过与否。"""
    paint = r.get("progress_paint") or {}
    distinct = paint.get("distinct", 0)
    samples = paint.get("samples", 1)
    follow = r.get("scrub_follow") or []
    hold = r.get("hold_speed") or {}
    step = r.get("frame_step") or {}
    rotated = r.get("fake_landscape") or {}
    touch = r.get("touch_portrait") or {}
    return [
        # 跟 timeupdate 走时 1.2 秒内只有 4~6 个不同取值；每帧自绘接近采样数
        (
            distinct > samples * 0.5 and paint.get("thumbInSync") is not False,
            f"进度条匀速：1.2 秒内 {distinct}/{samples} 帧取值不同"
            f"，圆点与已播段同步 {paint.get('thumbInSync')}",
        ),
        (
            9 <= r.get("double_tap_forward_s", 0) <= 13,
            f"双击前进十秒：+{r.get('double_tap_forward_s')} 秒",
        ),
        (
            hold.get("during") == 2 and hold.get("after") == 1 and hold.get("preservesPitch"),
            f"长按倍速：按住 {hold.get('during')}× → 松手 {hold.get('after')}×，"
            f"保音高 {hold.get('preservesPitch')}",
        ),
        (
            len(follow) == 3 and follow[0] < follow[1] < follow[2],
            f"拖动跟手：拖动中画面走到 {follow}",
        ),
        (
            len(r.get("chapter_marks") or []) > 0,
            f"章节刻度：{r.get('chapter_marks')}，气泡 {r.get('bubble')}",
        ),
        (
            # 转过来之后进度条的外接矩形是「厚 × 长」，拿 clientX 算会把整部片
            # 压进 44 个物理像素；双击分区同理会被转到竖直方向上去
            bool(rotated.get("fake_landscape"))
            and 80 <= rotated.get("drag_to_70pct_s", 0) <= 88
            and rotated.get("double_tap_right_s") == 10
            and rotated.get("double_tap_left_s") == -10,
            f"伪横屏坐标：拖到七成 = {rotated.get('drag_to_70pct_s')} 秒，"
            f"双击右/左 = {rotated.get('double_tap_right_s')}/{rotated.get('double_tap_left_s')} 秒"
            + ("" if rotated.get("fake_landscape") else "（没能进入伪横屏，本条无效）"),
        ),
        (
            step.get("paused") and step.get("drift") == 0 and 0 < step.get("step", 0) < 0.5,
            f"逐帧步进：暂停后走了 {step.get('step')} 秒",
        ),
        (
            # 拖动不重排倒计时的话，进度条会在手指底下消失、拖动却还在继续
            bool(touch.get("playing")) and touch.get("chrome_while_dragging") == ["visible"],
            f"触屏拖动 6 秒控制条不淡出：{touch.get('chrome_while_dragging')}"
            + ("" if touch.get("playing") else "（没能起播，本条无效）"),
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", default="http://127.0.0.1:3000")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--item", required=True, help="media_item_id（电影）")
    parser.add_argument("--timeout", type=float, default=90.0, help="等首帧的上限（秒）")
    parser.add_argument("--json", action="store_true", help="只输出原始读数")
    args = parser.parse_args()

    report = asyncio.run(run(args))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if "error" in report else 0
    if "error" in report:
        print(f"✗ {report['error']}：{report.get('video')}")
        return 1

    print(f"首帧 {report['ttff_ms']} ms（{report['ttff_method']}）")
    failed = 0
    for ok, line in verdicts(report):
        print(f"{'✓' if ok else '✗'} {line}")
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
