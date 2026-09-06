"""媒体库管理页「回收站」标签的浏览器端到端（docs/design/library-recycle-bin.md 验收）。

真后端 + 真前端 + 无头 Chromium，基座沿用 test_library_manage_browser 的 stack。
待回收数据不走洗版流程（那要真种子与下载器），直接往后端的 sqlite 里写台账行：
一部电影的旧版本 + 一部剧的三集（其中一集是「原地待回收」形态）。覆盖：标签栏
计数与 ?tab=recycle 深链 → 摘要行 / 库与原因胶囊 → 一条目一行、展开每集（集号 集名 ·
品质）→ 整组勾选与底部批量条 → 批量恢复（文件回到原路径）→ 「立即清理全部」确认弹窗
→ 空状态 → 手机端卡片。

标 integration：要 pnpm（apps/web 已 install）与 Playwright Chromium，CI 不跑。
本地：``pytest -m integration tests/e2e/test_library_recycle_bin_browser.py``。
"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from .test_library_manage_browser import (  # noqa: F401  (stack 是 fixture，按名注入)
    ADMIN,
    _chromium_kwargs,
    _wait_for,
    stack,
)

playwright = pytest.importorskip("playwright.sync_api")

pytestmark = [pytest.mark.integration]

_TRIGGER = {"kind": "subscription", "id": 1, "label": "《测试》订阅洗版"}


async def _seed(db_path: Path, movie_lib: int, tv_lib: int, movies: Path, tv: Path) -> dict:
    """直接写后端的 sqlite：条目、集名、四条待回收台账行（文件真实放在回收站目录里）。"""
    from movieclaw_db.engine import dispose_db, get_database, init_db
    from movieclaw_db.models import (
        FileSource,
        FileState,
        LibraryFile,
        MediaEpisode,
        MediaItem,
        utcnow,
    )

    init_db(f"sqlite+aiosqlite:///{db_path}", echo=False)
    db = get_database()
    now = utcnow()
    try:
        async with db.session() as session:
            movie = MediaItem(
                kind="movie",
                tmdb_id=1,
                title="九门",
                original_title="Nine Gates",
                year=2025,
                aliases=[],
            )
            show = MediaItem(
                kind="tv",
                tmdb_id=2,
                title="权力的游戏",
                original_title="Game of Thrones",
                year=2011,
                aliases=[],
            )
            session.add_all([movie, show])
            await session.flush()
            session.add(
                MediaEpisode(
                    media_item_id=show.id, season_number=1, episode_number=3, name="雪诺大人"
                )
            )

            def trashed(
                lib_id,
                item_id,
                root: Path,
                name: str,
                size: int,
                *,
                hours: float,
                reason: str,
                note: str,
                kept: bool = False,
                **kw,
            ):
                trash_dir = root / ".movieclaw-trash"
                trash_dir.mkdir(exist_ok=True)
                path = trash_dir / name
                path.write_bytes(b"x" * size)
                return LibraryFile(
                    library_id=lib_id,
                    media_item_id=item_id,
                    file_path=str(path),
                    size_bytes=size,
                    source=FileSource.IMPORTED,
                    state=FileState.TRASHED,
                    trashed_at=now - timedelta(days=1),
                    trash_original_path=None if kept else str(root / name),
                    purge_after=now + timedelta(hours=hours),
                    trash_context={"reason": reason, "trigger": _TRIGGER, "note": note},
                    **kw,
                )

            rows = [
                trashed(
                    movie_lib,
                    movie.id,
                    movies,
                    "九门.2025.1080p.WEB-DL.H264.AAC-XXX.mkv",
                    4096,
                    hours=6.5,  # 取整后显示「6 小时后」
                    reason="upgrade_replaced",
                    note="洗版替换：1080p WEB-DL → 2160p Remux",
                    resolution="1080p",
                    media_source="WEB-DL",
                    video_codec="h264",
                    release_group="XXX",
                    audio_streams=[{"codec": "aac", "channels": 2}],
                ),
            ]
            for n, (hours, reason, note, kept) in enumerate(
                [
                    (9.5, "upgrade_replaced", "洗版替换：720p HDTV → 1080p BluRay", False),
                    (9.5, "upgrade_replaced", "洗版替换：720p HDTV → 1080p BluRay", False),
                    (50, "upgrade_refuted", "洗版证伪：实测档位不高于当前版本", True),
                ],
                start=3,
            ):
                rows.append(
                    trashed(
                        tv_lib,
                        show.id,
                        tv,
                        f"Game.of.Thrones.S01E0{n}.720p.HDTV.x264-CTU.mkv",
                        1024,
                        hours=hours,
                        reason=reason,
                        note=note,
                        kept=kept,
                        season_number=1,
                        episode_number=n,
                        resolution="720p",
                        media_source="HDTV",
                        video_codec="h264",
                        release_group="CTU",
                        audio_streams=[{"codec": "ac3", "channel_layout": "5.1(side)"}],
                    )
                )
            session.add_all(rows)
            await session.commit()
            return {
                "movie": movie.id,
                "show": show.id,
                "episode_paths": [r.trash_original_path for r in rows[1:]],
            }
    finally:
        await dispose_db()


def test_recycle_bin_tab(stack) -> None:  # noqa: PLR0915, F811
    from playwright.sync_api import expect, sync_playwright

    base = stack["base"]
    roots: dict[str, Path] = stack["roots"]
    shots: Path = stack["shots"]
    shots.mkdir(exist_ok=True)
    db_path = roots["movies"].parent.parent / "data" / "app.db"

    def api(page, method: str, path: str, **kw) -> dict:
        resp = page.request.fetch(f"{base}/api/v1{path}", method=method, **kw)
        assert resp.ok, f"{method} {path}: {resp.status} {resp.text()}"
        return resp.json()["data"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **_chromium_kwargs())
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(20_000)
        page.set_default_navigation_timeout(120_000)
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # ---- 首次引导 + 登录（与管理页 e2e 同一段） ----
        page.goto(f"{base}/")
        page.wait_for_url(lambda u: "/setup" in u or "/login" in u)
        if "/setup" in page.url:
            page.locator("input[type=text]").fill(ADMIN["username"])
            page.locator("input[type=password]").nth(0).fill(ADMIN["password"])
            page.locator("input[type=password]").nth(1).fill(ADMIN["password"])
            page.locator("button[type=submit]").click()
            page.wait_for_url(lambda u: "/setup" not in u)
        page.goto(f"{base}/login")
        page.wait_for_load_state("networkidle")
        if "/login" in page.url:
            page.locator("input[type=text]").fill(ADMIN["username"])
            page.locator("input[type=password]").first.fill(ADMIN["password"])
            page.locator("button[type=submit]").click()
            page.wait_for_url(lambda u: "/login" not in u)

        # ---- 两个库：走 API 建，关掉实时监控，等首次扫描结束再灌数据（避免扫描/监听抢收编） ----
        movie_lib = api(
            page,
            "POST",
            "/libraries",
            data={
                "name": "电影",
                "kind": "movie",
                "root_paths": [str(roots["movies"])],
                "realtime_watch": False,
            },
        )["id"]
        tv_lib = api(
            page,
            "POST",
            "/libraries",
            data={
                "name": "剧集",
                "kind": "tv",
                "root_paths": [str(roots["tv"])],
                "realtime_watch": False,
            },
        )["id"]

        def all_idle():
            rows = api(page, "GET", "/libraries")
            return rows if all(not r["scanning"] and r["last_scan"] for r in rows) else None

        _wait_for(all_idle, timeout=120, what="两个库首次扫描完成")

        # 空回收站：标签仍渲染、不带数字，切过去是空状态
        page.goto(f"{base}/library/manage")
        tabs = page.get_by_role("tablist")
        expect(tabs.get_by_role("tab", name="回收站", exact=True)).to_be_visible()
        tabs.get_by_role("tab", name="回收站", exact=True).click()
        expect(page.get_by_role("heading", name="回收站是空的")).to_be_visible()
        assert "tab=recycle" in page.url

        # Playwright 同步 API 自己跑着一个事件循环，asyncio.run 只能放到另一个线程里
        with ThreadPoolExecutor(max_workers=1) as pool:
            seeded = pool.submit(
                asyncio.run, _seed(db_path, movie_lib, tv_lib, roots["movies"], roots["tv"])
            ).result()

        # ---- 深链直达回收站：标签计数、摘要行、胶囊 ----
        page.goto(f"{base}/library/manage?tab=recycle")
        expect(page.get_by_role("tab", name=re.compile(r"^回收站\s*4$"))).to_be_visible()
        expect(page.get_by_text("4 个文件", exact=True)).to_be_visible()
        expect(page.get_by_text("2 个条目", exact=True)).to_be_visible()
        expect(page.get_by_text(re.compile("3 个将在 24 小时内自动清理"))).to_be_visible()
        expect(page.get_by_text("1 个仍在原位", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^全部库 4$"))).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^电影 1$"))).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^剧集 3$"))).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^洗版替换 3$"))).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^洗版证伪 1$"))).to_be_visible()

        # ---- 一条目一行：电影行直接是文件名 + 品质；剧集行「3 集 · 1 季」，展开到每集 ----
        table = page.get_by_role("table", name="待回收文件")
        for col in ("条目", "文件 · 品质", "原因", "自动清理 · 操作"):
            expect(table.get_by_role("columnheader", name=col, exact=True)).to_be_visible()
        expect(table.get_by_text("九门.2025.1080p.WEB-DL.H264.AAC-XXX.mkv")).to_be_visible()
        expect(table.get_by_text("1080p WEB-DL", exact=True).first).to_be_visible()
        expect(table.get_by_text("洗版替换：1080p WEB-DL → 2160p Remux")).to_be_visible()
        expect(table.get_by_text("6 小时后", exact=True)).to_be_visible()
        expect(table.get_by_text("3 集", exact=True)).to_be_visible()
        expect(table.get_by_text("洗版替换 2 · 洗版证伪 1")).to_be_visible()
        expect(table.get_by_text("最早 9 小时后", exact=True)).to_be_visible()
        expect(table.get_by_role("button", name="恢复 3")).to_be_visible()
        expect(table.get_by_role("button", name="清理 3")).to_be_visible()
        table.get_by_role("button", name="展开 3 个文件").click()
        expect(table.get_by_text("S01E03", exact=True)).to_be_visible()
        expect(table.get_by_text("雪诺大人", exact=True)).to_be_visible()
        expect(table.get_by_text("Game.of.Thrones.S01E05.720p.HDTV.x264-CTU.mkv")).to_be_visible()
        expect(table.get_by_text("原地", exact=True)).to_be_visible()
        expect(table.get_by_text(re.compile("AC3 5.1")).first).to_be_visible()
        page.screenshot(path=str(shots / "10-recycle-desktop.png"), full_page=True)

        # ---- 点文件名看完整存放路径：原路径（恢复回去的位置）+ 现在的位置（回收站内） ----
        page.get_by_role(
            "button", name="查看「九门.2025.1080p.WEB-DL.H264.AAC-XXX.mkv」的存放路径"
        ).click()
        tip = page.get_by_role("tooltip")
        expect(tip).to_contain_text("原路径")
        expect(tip).to_contain_text(
            str(roots["movies"] / "九门.2025.1080p.WEB-DL.H264.AAC-XXX.mkv")
        )
        expect(tip).to_contain_text(str(roots["movies"] / ".movieclaw-trash"))
        page.wait_for_timeout(400)  # 等淡入结束，截图才不是半透明
        page.screenshot(path=str(shots / "15-recycle-path.png"))
        page.keyboard.press("Escape")
        expect(tip).to_have_count(0)

        # ---- 手机端：一条目一卡，「3 集 · 1 季」那行就是展开开关 ----
        mobile = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN")
        mobile.add_cookies(context.cookies())
        mpage = mobile.new_page()
        mpage.set_default_timeout(20_000)
        mpage.goto(f"{base}/library/manage?tab=recycle")
        expect(mpage.get_by_text("4 个文件", exact=True)).to_be_visible()
        mpage.get_by_role("button", name=re.compile(r"^3 集 · 1 季")).click()
        expect(mpage.get_by_text("雪诺大人", exact=True)).to_be_visible()
        mpage.screenshot(path=str(shots / "11-recycle-mobile.png"), full_page=True)
        mobile.close()

        # ---- 整组勾选 → 底部批量条按文件计数 → 恢复所选：三集回到原路径 ----
        page.get_by_role("checkbox", name="选择「权力的游戏」").check()
        expect(page.get_by_text(re.compile(r"已选\s*3\s*个文件"))).to_be_visible()
        page.screenshot(path=str(shots / "12-recycle-selected.png"), full_page=True)
        page.get_by_role("button", name="恢复所选").click()
        expect(page.get_by_text("已恢复 3 个文件")).to_be_visible()
        for path in seeded["episode_paths"]:
            if path:
                assert Path(path).exists(), f"恢复后文件应回到原路径：{path}"
        expect(page.get_by_text("1 个文件", exact=True)).to_be_visible()
        assert api(page, "GET", "/libraries/trashed-files?limit=1")["total_files"] == 1
        tv_stats = next(r for r in api(page, "GET", "/libraries") if r["id"] == tv_lib)["stats"]
        assert tv_stats["file_count"] == 3, "恢复后库统计应重算"

        # ---- 立即清理全部：确认弹窗列事实，按钮带文件数；清完落到空状态 ----
        page.get_by_role("button", name=re.compile(r"^立即清理全部 · 1$")).click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_text("清理全部待回收文件？")).to_be_visible()
        expect(dialog.get_by_text(re.compile("释放空间"))).to_be_visible()
        page.screenshot(path=str(shots / "13-recycle-confirm.png"))
        dialog.get_by_role("button", name="清理 1 个文件").click()
        expect(page.get_by_text("已清理 1 个文件")).to_be_visible()
        expect(page.get_by_role("heading", name="回收站是空的")).to_be_visible()
        page.screenshot(path=str(shots / "14-recycle-empty.png"), full_page=True)
        assert not (
            roots["movies"] / ".movieclaw-trash" / "九门.2025.1080p.WEB-DL.H264.AAC-XXX.mkv"
        ).exists()
        expect(page.get_by_role("tab", name=re.compile(r"^回收站\s*\d+$"))).to_have_count(0)

        assert not page_errors, page_errors
        context.close()
        browser.close()
