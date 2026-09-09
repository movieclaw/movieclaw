#!/usr/bin/env python3
"""「全部收藏」页位置记忆的端到端验收（Playwright + Chromium，真实浏览器）。

验的是两层位置记忆在真实浏览器里到底成不成立（设计见 apps/web 里
lib/use-scroll-restoration.ts 与 lib/library-wall-recall.ts）：

    A. 会话内：滚到第 N 屏 → 进作品详情 → 返回 → 位置与窗口条数都在
    B. 数据变了也在：在详情页取消收藏 → 返回 → 那一部消失、位置仍在
    C. 跨会话：写一条旧记录 → 重新打开这一页 → 底部弹胶囊 → 点它跳过去
    D. 跳过去还能往上滑：墙顶哨兵把上文补回来，窗口起点回到 0
    E. 图床浏览模式同样保持（锚点换成瓦片，窗口同样整份接回来）

为什么必须用真实浏览器：这几件事全部发生在「Next 卸载组件 → 新组件挂载 →
虚拟化窗口重新量测 → 图片撑开高度」这条真实时间线上，jsdom 与单测都复现不出来。

数据自带（--seed）：直接往已跑过 alembic 迁移的 SQLite 里灌一个库 + 若干部
作品 + 对应的收藏行。与 scripts/perf/seed_library_dataset.py 同一条思路——
读路径只关心落库的行，不必走真实扫描管线。

用法::

    # 1. 建库并灌数据（一次就够）
    .venv/bin/alembic upgrade head
    python scripts/perf/e2e_favorites_recall.py --seed --db data/movieclaw.db

    # 2. 起后端与前端（另开两个终端）
    .venv/bin/uvicorn movieclaw_api.main:app --port 8000
    pnpm --filter web build && pnpm --filter web exec next start -p 3000

    # 3. 首次需要一个管理员账号（已有就跳过）
    curl -X POST localhost:8000/api/v1/auth/bootstrap -H 'Content-Type: application/json' \
        -d '{"username":"admin","password":"movieclaw-e2e"}'

    # 4. 跑验收（每次跑之前重新灌一遍数据，断言才有确定的起点）
    python scripts/perf/e2e_favorites_recall.py \
        --web http://127.0.0.1:3000 --user admin --password 'movieclaw-e2e'

改动前后都要跑一遍：把 favorites-view.tsx 换回修复前的版本重新构建，
A / C / E 必须**失败**（位置回到墙首、墙缩回一页、胶囊不出现）——测不出差别的
验收等于没验收。
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import sqlite3
import sys
from datetime import datetime, timedelta

# —— 数据集形状 ——
# 收藏数刻意跨过好几页：前端一页 60 格（PAGE_SIZE），要让「返回时只补第一页
# 就装不下离开时的位置」这个 bug 真的能复现，收藏必须远多于一页。
FAVORITE_COUNT = 320
LIBRARY_ID = 1
NOW = datetime(2026, 9, 1, 12, 0, 0)


def _write_assets(assets_root, item_id: int) -> tuple[str, str]:
    """给一部作品画一张海报和一张剧照（纯色 JPEG，几百字节）。

    为什么非要有真图：图廊模式的一组就是一部作品的图，没有图就没有瓦片，
    那一半功能根本测不到；海报墙这边也一样——真实的「图片解码撑开高度」正是
    滚动恢复要熬过去的那段时间窗，用占位方块测等于把最难的部分绕开了。
    """
    from PIL import Image

    folder = assets_root / str(item_id)
    folder.mkdir(parents=True, exist_ok=True)
    tone = (item_id * 37 % 200 + 40, item_id * 53 % 200 + 40, item_id * 71 % 200 + 40)
    Image.new("RGB", (200, 300), tone).save(folder / "poster.jpg", quality=60)
    Image.new("RGB", (320, 180), tone).save(folder / "backdrop.jpg", quality=60)
    return f"{item_id}/poster.jpg", f"{item_id}/backdrop.jpg"


def seed(db_path: str, assets_dir: str) -> None:
    """灌一个库 + FAVORITE_COUNT 部电影 + 每部一行收藏 + 每部两张图。

    收藏顺序由 playback_state.updated_at 倒序决定（服务端就是这么排的），
    这里让第 i 部的 updated_at 依次递减，于是墙上的顺序恒等于「测试收藏 001、
    002、003……」——断言里可以直接按片名认位置。
    """
    from pathlib import Path

    assets_root = Path(assets_dir)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    cur = conn.cursor()
    for table in ("playback_state", "library_file", "media_metadata", "media_item", "library"):
        cur.execute(f"DELETE FROM {table}")

    ts = NOW.isoformat(sep=" ")
    cur.execute(
        "INSERT INTO library (id,name,kind,root_paths,is_default,sort_order,"
        "stats_item_count,stats_episode_count,stats_file_count,stats_total_size_bytes,"
        "stats_unidentified_count,stats_missing_count,stats_ignored_count,"
        "stats_refreshed_at,created_at,updated_at,match_rules,write_media_assets,"
        "auto_clear_missing,realtime_watch) "
        f"VALUES ({LIBRARY_ID},'端到端电影库','movie','[\"/media/e2e\"]',1,1,"
        f"{FAVORITE_COUNT},0,{FAVORITE_COUNT},0,0,0,0,?,?,?,'[]',1,0,0)",
        (ts, ts, ts),
    )

    items, files, marks, metas = [], [], [], []
    for i in range(1, FAVORITE_COUNT + 1):
        title = f"测试收藏 {i:03d}"
        poster_file, backdrop_file = _write_assets(assets_root, i)
        metas.append((i, poster_file, 200, 300, backdrop_file,
                      "[]", "[]", "[]", "[]", "[]", "zh-CN", ts, ts))
        tmdb_id = 900_000 + i
        # external_id 是身份锚的第三个分量（source, kind, external_id）；
        # 走 SQL 直插时列默认回调不生效，这里显式补上 tmdb_id 的字符串形式
        # metadata_refreshed_at / next_refresh_at 一起给足：进详情页会触发自动补刮削，
        # 不压住的话种子条目会被真实 TMDB 档案改写（片名、海报都换掉），
        # 断言就跟着飘。测试要的是稳定的一份数据，不是联网结果
        items.append((i, "movie", tmdb_id, str(tmdb_id), "tmdb", title, title,
                      2000 + i % 25, "[]", "released", None, None,
                      ts, "2099-01-01 00:00:00", ts, ts))
        files.append((
            i, LIBRARY_ID, i, 0, 0, f"/media/e2e/{title}.mkv", 1_000_000, "mkv",
            "1080p", "h264", None, 8, 5400, 4_000_000, 23.976, "bt709", "web-dl", None,
            "scan", "in_place", None, None, None, None, "[]", "[]", "[]", None, "manual",
            None, 0, ts, ts,
        ))
        # 第 1 部最新收藏，第 320 部最早——墙上就是 001 在最前
        marked = (NOW - timedelta(minutes=i)).isoformat(sep=" ")
        marks.append((i, 0, 0, 0, 0, 0, 1, None, 0, marked, marked))

    cur.executemany(
        "INSERT INTO media_item (id,kind,tmdb_id,external_id,source,title,original_title,"
        "year,aliases,status,poster_path,backdrop_path,metadata_refreshed_at,next_refresh_at,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        items,
    )
    cur.executemany(
        "INSERT INTO library_file (id,library_id,media_item_id,season_number,episode_number,"
        "file_path,size_bytes,container,resolution,video_codec,hdr,bit_depth,duration_seconds,"
        "bit_rate,frame_rate,color_space,media_source,release_group,source,state,missing_since,"
        "ignored_at,unidentified_reason,unidentified_code,audio_streams,subtitle_streams,"
        "external_subtitles,added_batch_id,identity_source,resolved_version,file_mtime_ns,"
        "created_at,updated_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        files,
    )
    cur.executemany(
        "INSERT INTO media_metadata (media_item_id,poster_file,poster_width,poster_height,"
        "backdrop_file,genres,origin_countries,studios,directors,\"cast\",scrape_language,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        metas,
    )
    cur.executemany(
        "INSERT INTO playback_state (media_item_id,season_number,episode_number,position_ms,"
        "played,play_count,is_favorite,last_played_at,member_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        marks,
    )
    conn.commit()
    conn.close()
    print(f"已灌入：1 个媒体库 / {FAVORITE_COUNT} 部作品 / {FAVORITE_COUNT} 条收藏 / "
          f"{FAVORITE_COUNT * 2} 张图片资产")


def _chromium_path() -> str | None:
    """本机预装的 Chromium（会话环境已装好，不联网下载）。"""
    for pattern in (
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/opt/pw-browsers/chromium/chrome-linux/chrome",
    ):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


# 墙上每一格挂的位置锚点（apps/web/components/poster-wall.tsx）
CELL = "[data-library-item-id]"
# 图廊里一部作品一个组（apps/web/components/video-gallery.tsx）
GALLERY_ITEM = "[data-gallery-item-id]"

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"  {'✅' if ok else '❌'} {name}{('  —— ' + detail) if detail else ''}")


# 真正的滚动容器：墙挂在页面内部那个 overflow-y-auto 的 div 上，不是 window。
# 从任一格反查（closest）而不是按 class 猜——侧栏里也有 overflow-y-auto。
_SCROLLER = """(() => {
  const cell = document.querySelector('[data-library-item-id], [data-gallery-tile-id]');
  return (cell && cell.closest('.overflow-y-auto'))
    || [...document.querySelectorAll('.overflow-y-auto')]
         .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
})"""


def js(source: str) -> str:
    """把片段里的 SCROLLER 换成上面那段查找逻辑（JS 里满是花括号，
    用占位替换而不是 format/%）。"""
    return source.replace("SCROLLER", _SCROLLER)


async def wall_state(page) -> dict:
    """这一屏的四件事：滚动位置、墙高、挂着的格数、首个可见格的片名。

    首个可见格与 lib/library-wall-recall.ts 的 firstVisibleAnchorId 同一判据
    （下边缘越过容器可视区顶边），断言里就能按「那一部」而不是像素说话。
    """
    return await page.evaluate(
        js("""() => {
          const box = SCROLLER();
          const cells = [...document.querySelectorAll('[data-library-item-id]')];
          const top = box.getBoundingClientRect().top;
          const first = cells.find((c) => c.getBoundingClientRect().bottom > top);
          // 认条目 id 而不是片名：id 是墙自己的位置锚点（data-library-item-id），
          // 片名会被后台刮削改写，拿它当断言基准是在测网络
          const id = (el) => el ? Number(el.getAttribute('data-library-item-id')) : null;
          return {
            scrollTop: Math.round(box.scrollTop),
            scrollHeight: box.scrollHeight,
            mounted: cells.length,
            firstVisible: id(first),
            top: id(cells[0]),
          };
        }"""),
    )


async def settled_wall_state(page, tries: int = 12) -> dict:
    """等这一屏真的稳下来再量。

    墙是虚拟化的：回位之后要等一次重新量测才会把这一屏的格子挂上来，中间有
    几帧 DOM 里一个格子都没有。不等就量，量到的是那个空档而不是结果。
    """
    state = await wall_state(page)
    for _ in range(tries):
        if state["mounted"] > 0 and state["firstVisible"]:
            return state
        await page.wait_for_timeout(400)
        state = await wall_state(page)
    return state


async def gallery_state(page) -> dict:
    """图廊这一屏：首个可见瓦片是哪一张、它离可视区顶边多远。

    这里**不能**按像素断言。图廊是瀑布流，返回时图片按各自的时序解码、上方
    每一列的高度都会重排，同一张瓦片对应的 scrollTop 本来就不是同一个数。
    lib/use-scroll-restoration.ts 对这种墙给的承诺也正是「同一张瓦片停在同一
    处」（anchorAttribute: data-gallery-tile-id），断言就照这条来。
    """
    return await page.evaluate(
        js("""() => {
          const box = SCROLLER();
          const top = box.getBoundingClientRect().top;
          const tile = [...document.querySelectorAll('[data-gallery-tile-id]')]
            .find((t) => t.getBoundingClientRect().bottom > top);
          return {
            scrollTop: Math.round(box.scrollTop),
            tile: tile ? tile.getAttribute('data-gallery-tile-id') : null,
            offset: tile ? Math.round(tile.getBoundingClientRect().top - top) : null,
          };
        }"""),
    )


async def scroll_to(page, top: int) -> None:
    await page.evaluate(
        js("(top) => SCROLLER().scrollTo({ top, behavior: 'instant' })"), top
    )
    await page.wait_for_timeout(500)


async def scroll_by_screens(page, screens: int) -> None:
    """一屏一屏往下滑，等分页哨兵把下一页接上来（模拟真人滑动）。"""
    for _ in range(screens):
        await page.evaluate(
            js("() => { const b = SCROLLER();"
               " b.scrollBy({ top: b.clientHeight * 0.9, behavior: 'instant' }); }"),
        )
        await page.wait_for_timeout(600)


async def wait_until_quiet(page, quiet_ms: int = 1500, limit_ms: int = 12_000) -> None:
    """等墙不再长高再走。

    滚动加载的下一页可能还在路上：这时候离开，快照记下的是一个「半页」的窗口，
    返回时整窗对账拿回来的页数与它对不上，墙高一变，按像素回位就会落到别处
    （图廊尤其明显，一部作品十几张图，一页的高度差着好几屏）。真人滑到一处
    总会停一下，这里把那一下补上。
    """
    height = -1
    stable = 0
    waited = 0
    while waited < limit_ms:
        now = await page.evaluate(js("() => SCROLLER().scrollHeight"))
        stable = stable + 500 if now == height else 0
        height = now
        if stable >= quiet_ms:
            return
        await page.wait_for_timeout(500)
        waited += 500


async def open_first_visible(page) -> None:
    """点开当前屏幕上第一个可见的那一格（用户从这一屏进详情就是这么点的）。"""
    await page.evaluate(
        js("""() => {
          const box = SCROLLER();
          const top = box.getBoundingClientRect().top;
          const cell = [...document.querySelectorAll('[data-library-item-id]')]
            .find((c) => c.getBoundingClientRect().bottom > top);
          cell.querySelector('a').click();
        }"""),
    )
    await page.wait_for_url("**/item/**", timeout=30_000)
    await page.wait_for_timeout(1000)


async def favorite_id_at(page, offset: int) -> int | None:
    """服务端名单里第 offset 部是哪个条目（期望值从同一份数据来，不写死——
    测试中途取消过收藏，名单会整体前移）。"""
    return await page.evaluate(
        """async (offset) => {
          const r = await fetch(`/api/v1/playback/favorites?limit=1&offset=${offset}`);
          const body = await r.json();
          return body.data.items[0]?.media_item_id ?? null;
        }""",
        offset,
    )


async def login(page, web: str, user: str, password: str) -> None:
    await page.goto(f"{web}/login", wait_until="domcontentloaded")
    await page.fill("input[type='text']", user)
    await page.fill("input[type='password']", password)
    await page.click("button[type='submit']")
    await page.wait_for_url(lambda url: "/login" not in url, timeout=30_000)


async def run(web: str, user: str, password: str, headed: bool, shot_path: str) -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headed, executable_path=_chromium_path()
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        page.on("pageerror", lambda e: print(f"  ⚠ 页面异常：{e}"))
        await login(page, web, user, password)

        # —— A. 会话内：进详情再返回，位置与窗口都在 ——
        print("\nA. 会话内返回（收藏墙 → 作品详情 → 返回）")
        await page.goto(f"{web}/library/favorites", wait_until="domcontentloaded")
        await page.wait_for_selector(CELL, timeout=30_000)
        await scroll_by_screens(page, 6)
        await wait_until_quiet(page)
        before = await settled_wall_state(page)
        print(f"     离开前：scrollTop={before['scrollTop']} 墙高={before['scrollHeight']} "
              f"首个可见={before['firstVisible']}")
        check(before["scrollTop"] > 2000, "先滑到足够深（越过第一页 60 格）",
              f"scrollTop={before['scrollTop']}")

        await open_first_visible(page)
        await page.go_back()
        await page.wait_for_selector(CELL, timeout=30_000)
        await page.wait_for_timeout(2000)
        after = await settled_wall_state(page)
        print(f"     返回后：scrollTop={after['scrollTop']} 墙高={after['scrollHeight']} "
              f"首个可见={after['firstVisible']}")
        check(abs(after["scrollTop"] - before["scrollTop"]) <= 8, "滚动位置回到原处",
              f"{before['scrollTop']} → {after['scrollTop']}")
        check(after["firstVisible"] == before["firstVisible"], "首个可见的还是同一部",
              f"{before['firstVisible']} → {after['firstVisible']}")
        check(abs(after["scrollHeight"] - before["scrollHeight"]) <= 4,
              "已加载的窗口整份接回来（墙没有缩回一页）",
              f"墙高 {before['scrollHeight']} → {after['scrollHeight']}")

        # —— B. 在详情页取消收藏，返回后那一部消失、位置仍在 ——
        print("\nB. 在详情页取消收藏后返回")
        target = after["firstVisible"]
        await open_first_visible(page)
        heart = page.locator("button[aria-label*='收藏']").first
        await heart.wait_for(timeout=15_000)
        await heart.click()
        await page.wait_for_timeout(1500)
        await page.go_back()
        await page.wait_for_selector(CELL, timeout=30_000)
        await page.wait_for_timeout(2500)
        purged = await settled_wall_state(page)
        on_wall = await page.evaluate(
            "() => [...document.querySelectorAll('[data-library-item-id]')]"
            ".map((c) => Number(c.getAttribute('data-library-item-id')))",
        )
        print(f"     返回后：scrollTop={purged['scrollTop']} 首个可见={purged['firstVisible']}")
        check(target is not None and target not in on_wall,
              "取消收藏的那一部已从墙上消失（返回时整窗对过账）", f"条目 #{target}")
        check(abs(purged["scrollTop"] - after["scrollTop"]) <= 8, "位置没有因为对账而跳走",
              f"{after['scrollTop']} → {purged['scrollTop']}")

        # —— C. 跨会话：胶囊 —— #
        print("\nC. 跨会话「回到上次浏览的位置」胶囊")
        expected = await favorite_id_at(page, 200)
        await page.evaluate(
            """() => localStorage.setItem('movieclaw.wall-recall', JSON.stringify({
                 'library:favorites': {
                   offset: 200, view: 'favorites', updatedAt: Date.now() - 3600e3,
                 },
               }))""",
        )
        # 换一页再回来 = 组件重新挂载、没有会话快照，等同于「第二天再进来」
        await page.goto(f"{web}/library", wait_until="domcontentloaded")
        await page.goto(f"{web}/library/favorites", wait_until="domcontentloaded")
        await page.wait_for_selector(CELL, timeout=30_000)
        await page.wait_for_timeout(1200)
        pill = page.locator("button:has-text('回到上次浏览的位置')")
        appeared = await pill.count() > 0
        check(appeared, "重新进入时弹出胶囊（会话内返回时不弹，见 A）")
        jumped = None
        if appeared:
            await pill.click()
            await page.wait_for_timeout(3000)
            jumped = await settled_wall_state(page)
            print(f"     跳转后：首个可见=条目 #{jumped['firstVisible']}（期望 #{expected}）")
            check(jumped["firstVisible"] == expected,
                  "跳到了记录里的那一部（服务端名单第 201 部）",
                  f"{jumped['firstVisible']} vs {expected}")

            # —— D. 跳过去之后往上滑，上文由墙顶哨兵补回来 ——
            print("\nD. 跳转之后往上滑，墙顶把上文补回来")
            head_id = jumped["top"]
            for _ in range(8):
                await scroll_to(page, 0)
                await page.wait_for_timeout(900)
                state = await wall_state(page)
                if state["top"] == head_id:
                    break
                head_id = state["top"]
            first_ever = await favorite_id_at(page, 0)
            print(f"     一路上滑到墙顶：条目 #{head_id}（名单第一部是 #{first_ever}）")
            check(head_id == first_ever,
                  "一路上滑能回到墙首（跳转位置之上不是一堵墙）",
                  f"墙顶=#{head_id} 期望=#{first_ever}")

        # —— E. 图床浏览模式 ——
        print("\nE. 图床浏览模式的返回")
        await page.evaluate("() => localStorage.removeItem('movieclaw.wall-recall')")
        await page.goto(f"{web}/library/favorites", wait_until="domcontentloaded")
        await page.wait_for_selector(CELL, timeout=30_000)
        await page.click("button[aria-label='图床浏览']")
        await page.wait_for_selector("[data-gallery-tile-id]", timeout=30_000)
        # 图廊要滑得比海报墙更深：一页只有二十几部作品但每部十几张图，滑浅了
        # 「返回时只补第一页」也能勉强凑够高度，测不出差别
        await scroll_by_screens(page, 12)
        await wait_until_quiet(page)
        gal_before = await gallery_state(page)
        tiles_before = await page.evaluate(
            "() => document.querySelectorAll('[data-gallery-item-id]').length")
        print(f"     离开前：scrollTop={gal_before['scrollTop']} 首个可见瓦片={gal_before['tile']}")
        check(gal_before["scrollTop"] > 1500, "图廊也先滑到足够深",
              f"scrollTop={gal_before['scrollTop']}")
        # 必须走站内跳转再返回：会话快照与滚动位置都只活在当前这次页面加载里，
        # page.goto 是整页重载、等同于刷新——那本来就该从墙首开始，拿它测「返回」
        # 会测出一个假失败。图廊里的站内落点是每组标题那条通往作品详情的链接
        await page.evaluate(
            js("""() => {
              const box = SCROLLER();
              const top = box.getBoundingClientRect().top;
              const link = [...document.querySelectorAll('a[href*="/item/"]')]
                .find((a) => a.getBoundingClientRect().bottom > top);
              link.click();
            }"""),
        )
        await page.wait_for_url("**/item/**", timeout=30_000)
        await page.wait_for_timeout(1000)
        await page.go_back()
        await page.wait_for_selector("[data-gallery-tile-id]", timeout=30_000)
        await page.wait_for_timeout(2500)
        gal_after = await gallery_state(page)
        print(f"     返回后：scrollTop={gal_after['scrollTop']} 首个可见瓦片={gal_after['tile']}")
        check(gal_after["tile"] is not None and gal_after["tile"] == gal_before["tile"],
              "图廊返回后还停在同一张瓦片上",
              f"{gal_before['tile']} → {gal_after['tile']}")
        drift = (abs(gal_after["offset"] - gal_before["offset"])
                 if gal_after["offset"] is not None else None)
        check(drift is not None and drift <= 40,
              "那张瓦片还停在屏幕上的同一高度",
              f"离顶边 {gal_before['offset']}px → {gal_after['offset']}px")
        check(tiles_before > 0, "图廊窗口非空（每部作品的图都在）", f"{tiles_before} 组")

        await page.screenshot(path=shot_path)
        await browser.close()
    return report()


def report() -> int:
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print(f"\n{'=' * 62}\n通过 {passed}/{len(RESULTS)}")
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  失败：{name} {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true", help="只灌数据，不跑浏览器")
    parser.add_argument("--db", default="data/movieclaw.db")
    parser.add_argument("--assets", default="data/metadata/images",
                        help="图片资产根目录（后端 METADATA_DIR 下的 images/）")
    parser.add_argument("--web", default="http://127.0.0.1:3000")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="movieclaw-e2e")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--shot", default="e2e-favorites-final.png",
                        help="最后一屏的截图落点")
    args = parser.parse_args()
    if args.seed:
        seed(args.db, args.assets)
        sys.exit(0)
    sys.exit(asyncio.run(run(args.web, args.user, args.password, args.headed, args.shot)))
