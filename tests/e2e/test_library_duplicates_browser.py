"""媒体库管理页「重复文件」标签的浏览器端到端（docs/design/library-duplicate-files.md 验收）。

真后端 + 真前端 + 无头 Chromium，基座沿用 test_library_manage_browser 的 stack。
重复数据不走扫描（那要 ffprobe 与 TMDB），直接往后端 sqlite 写台账行，**文件真实
落盘**——清理要真的搬得动它们。覆盖：没扫过时页面上只有「开始扫描」→ 扫描作业跑完
出三档摘要 → 标签栏计数与 ?tab=duplicates 深链 → 点进一档看明细：电影块列文件行、
剧集块折成版本行 → 「留这个」确认弹窗与磁盘落位 → 「都留着」让单元消失 → 回收站按
「重复清理」筛得到 → 条目详情页的「处理重复」入口与文件区「来源」行。

标 integration：要 pnpm（apps/web 已 install）与 Playwright Chromium，CI 不跑。
本地：``pytest -m integration tests/e2e/test_library_duplicates_browser.py``。
"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
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

_SCAN_ORIGIN = {
    "kind": "scan",
    "label": "存量扫描发现（非本系统入库）",
    "detail": "手动扫描发现",
}
_SUB_ORIGIN = {
    "kind": "subscription",
    "label": "订阅《九门》自动投递",
    "detail": "HDSky · Nine.Gates.2025.2160p.WEB-DL-CHDWEB · qBittorrent · 硬链接入库",
}


async def _seed(db_path: Path, movie_lib: int, tv_lib: int, movies: Path, tv: Path) -> dict:
    """三种形态：电影两个真版本、电影一对同内容副本、一季三集各两个版本（同构）。"""
    from movieclaw_db.engine import dispose_db, get_database, init_db
    from movieclaw_db.models import FileSource, LibraryFile, MediaEpisode, MediaItem

    init_db(f"sqlite+aiosqlite:///{db_path}", echo=False)
    db = get_database()
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
            twin = MediaItem(
                kind="movie",
                tmdb_id=2,
                title="长安三万里",
                original_title="Chang An",
                year=2023,
                aliases=[],
            )
            show = MediaItem(
                kind="tv",
                tmdb_id=3,
                title="权力的游戏",
                original_title="Game of Thrones",
                year=2011,
                aliases=[],
            )
            session.add_all([movie, twin, show])
            await session.flush()
            for n in (1, 2, 3):
                session.add(
                    MediaEpisode(
                        media_item_id=show.id, season_number=1, episode_number=n, name=f"第 {n} 集"
                    )
                )

            def add(lib, item, directory, name, size, *, origin, duration, **kw):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * size)
                row = LibraryFile(
                    library_id=lib,
                    media_item_id=item,
                    file_path=str(path),
                    size_bytes=size,
                    duration_seconds=duration,
                    source=FileSource.SCANNED,
                    origin=origin,
                    **kw,
                )
                session.add(row)
                return row

            # 九门：2160p（订阅投递）与 1080p（扫描发现）→ 不同版本
            add(
                movie_lib,
                movie.id,
                movies / "九门 (2025)",
                "九门.2025.2160p.WEB-DL.mkv",
                8192,
                origin=_SUB_ORIGIN,
                duration=7200,
                resolution="2160p",
                media_source="WEB-DL",
                bit_rate=20_000_000,
            )
            add(
                movie_lib,
                movie.id,
                movies / "九门 (2025)",
                "九门 (2025) - 1080p.mkv",
                4096,
                origin=_SCAN_ORIGIN,
                duration=7200,
                resolution="1080p",
                media_source="WEB-DL",
                bit_rate=8_000_000,
            )
            # 长安三万里：同尺寸同时长 → 一模一样
            for name, origin in (
                ("长安三万里.2023.2160p.WEB-DL.mkv", _SCAN_ORIGIN),
                ("长安三万里 (2023) - 2160p.mkv", _SCAN_ORIGIN),
            ):
                add(
                    movie_lib,
                    twin.id,
                    movies / "长安三万里 (2023)",
                    name,
                    3000,
                    origin=origin,
                    duration=6000,
                    resolution="2160p",
                    media_source="WEB-DL",
                    bit_rate=15_000_000,
                )
            # 权力的游戏 S01：三集各两个版本 → 不同版本 · 同构 → 版本行
            season = tv / "权力的游戏 (2011)" / "Season 01"
            for n in (1, 2, 3):
                add(
                    tv_lib,
                    show.id,
                    season,
                    f"Game.of.Thrones.S01E0{n}.1080p.BluRay-DEMAND.mkv",
                    2048,
                    origin=_SCAN_ORIGIN,
                    duration=3000 + n,
                    season_number=1,
                    episode_number=n,
                    resolution="1080p",
                    media_source="Blu-ray",
                    bit_rate=7_000_000,
                )
                add(
                    tv_lib,
                    show.id,
                    season,
                    f"Game.of.Thrones.S01E0{n}.720p.HDTV-CTU.mkv",
                    1024,
                    origin=_SCAN_ORIGIN,
                    duration=3000 + n,
                    season_number=1,
                    episode_number=n,
                    resolution="720p",
                    media_source="HDTV",
                    bit_rate=2_500_000,
                )
            await session.commit()
            return {"movie": movie.id, "twin": twin.id, "show": show.id}
    finally:
        await dispose_db()


def test_duplicate_files_tab(stack) -> None:  # noqa: PLR0915, F811
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

        # ---- 首次引导 + 登录 ----
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

        # 还没扫过：页面上只有一件事可做——不在打开页面时偷偷算一遍（§9）
        page.goto(f"{base}/library/manage")
        tabs = page.get_by_role("tablist")
        expect(tabs.get_by_role("tab", name="重复文件", exact=True)).to_be_visible()
        tabs.get_by_role("tab", name="重复文件", exact=True).click()
        expect(page.get_by_role("heading", name="还没有扫描过重复文件")).to_be_visible()
        assert "tab=duplicates" in page.url

        with ThreadPoolExecutor(max_workers=1) as pool:
            ids = pool.submit(
                asyncio.run, _seed(db_path, movie_lib, tv_lib, roots["movies"], roots["tv"])
            ).result()

        # ---- 用户按下「开始扫描」，作业跑完出摘要 ----
        page.get_by_role("button", name="开始扫描").first.click()
        _wait_for(
            lambda: page.get_by_text(re.compile("上次扫描：")).count() > 0,
            timeout=60,
            what="重复扫描作业跑完",
            interval=0.5,
        )

        # ---- 落地是三档摘要：先做哪一档一目了然 ----
        expect(page.get_by_role("tab", name=re.compile(r"^重复文件\s*5$"))).to_be_visible()
        expect(page.get_by_role("heading", name="可以放心清理")).to_be_visible()
        expect(page.get_by_role("heading", name="建议清理")).to_be_visible()
        expect(page.get_by_role("heading", name="需要你决定")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^全部清理 · 1$"))).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^全部按建议清理 · 4$"))).to_be_visible()
        page.screenshot(path=str(shots / "duplicates-三档摘要.png"), full_page=True)

        # ---- 点进「建议清理」看明细：电影块列文件行，剧集块折成版本行 ----
        page.get_by_role("button", name="逐个看").nth(1).click()
        versions = page.get_by_role("region", name="建议清理")
        expect(versions.get_by_role("link", name="九门")).to_be_visible()
        expect(versions.get_by_role("link", name="权力的游戏")).to_be_visible()
        expect(versions.get_by_text("S01 · 3 集有重复")).to_be_visible()
        expect(page.get_by_text("清理的文件进回收站，7 天内可恢复")).to_be_visible()

        # 剧集折成两个版本行，各覆盖 3 集；建议保留 1080p
        expect(versions.get_by_text("3 集", exact=True).first).to_be_visible()
        expect(versions.get_by_role("button", name="整季留这个")).to_have_count(2)
        expect(versions.get_by_role("button", name="整季都留着")).to_be_visible()

        # 电影块列文件行，来源 label 直接可读（两个目标在这一页汇合）
        expect(versions.get_by_text("九门.2025.2160p.WEB-DL.mkv")).to_be_visible()
        expect(versions.get_by_text("订阅《九门》自动投递").first).to_be_visible()
        expect(versions.get_by_text("存量扫描发现（非本系统入库）").first).to_be_visible()
        expect(versions.get_by_text("建议保留 · 档位最高")).to_be_visible()
        page.screenshot(path=str(shots / "duplicates-建议清理明细.png"), full_page=True)

        # ---- 「留这个」：确认弹窗写明留谁清谁，确认后文件真的搬进回收站 ----
        old_movie = roots["movies"] / "九门 (2025)" / "九门 (2025) - 1080p.mkv"
        kept_movie = roots["movies"] / "九门 (2025)" / "九门.2025.2160p.WEB-DL.mkv"
        assert old_movie.exists() and kept_movie.exists()
        # 「留这个」只出现在文件行上（版本行是「整季留这个」），不同版本堆里只有九门有文件行；
        # 首行是建议保留的 2160p——点它 = 留 2160p、清 1080p
        versions.get_by_role("button", name="留这个", exact=True).first.click()
        expect(page.get_by_role("heading", name="留下这个，其余移入回收站？")).to_be_visible()
        expect(page.get_by_text("九门 (2025) - 1080p.mkv", exact=False).last).to_be_visible()
        page.get_by_role("button", name=re.compile(r"^移入回收站 · 1$")).click()
        expect(page.get_by_text(re.compile("已移入回收站 1 个文件"))).to_be_visible()

        _wait_for(lambda: not old_movie.exists(), timeout=15, what="旧版本离开原路径", interval=0.3)
        assert kept_movie.exists(), "保留的那个必须原地不动"
        trashed = list((roots["movies"] / ".movieclaw-trash").iterdir())
        assert [t.name for t in trashed] == ["九门 (2025) - 1080p.mkv"]
        expect(page.get_by_role("tab", name=re.compile(r"^重复文件\s*4$"))).to_be_visible()
        expect(versions.get_by_role("link", name="九门")).to_have_count(0)

        # ---- 「整季都留着」：单元消失，文件一个不动 ----
        versions.get_by_role("button", name="整季都留着").click()
        expect(page.get_by_text(re.compile("整季都留着"))).to_be_visible()
        _wait_for(
            lambda: page.get_by_role("link", name="权力的游戏").count() == 0,
            timeout=15,
            what="剧集块消失",
            interval=0.3,
        )
        season = roots["tv"] / "权力的游戏 (2011)" / "Season 01"
        assert len(list(season.iterdir())) == 6, "「都留着」不能动任何文件"
        expect(page.get_by_role("tab", name=re.compile(r"^重复文件\s*1$"))).to_be_visible()
        # 返回摘要：这一档做完了，剩下的活在另一档上
        page.get_by_role("button", name="‹ 返回摘要").click()
        expect(page.get_by_role("heading", name="可以放心清理")).to_be_visible()

        # ---- 回收站：按「重复清理」筛得到，原因整句可读 ----
        page.goto(f"{base}/library/manage?tab=recycle")
        expect(page.get_by_role("button", name=re.compile(r"^重复清理 1$"))).to_be_visible()
        expect(page.get_by_text(re.compile("留下「九门.2025.2160p.WEB-DL.mkv」"))).to_be_visible()

        # ---- 条目详情页：「处理重复」入口 + 文件区「来源」行 ----
        page.goto(f"{base}/library/{movie_lib}/item/{ids['twin']}")
        entry = page.get_by_role("link", name=re.compile("2 个版本 · 处理重复"))
        expect(entry).to_be_visible()
        # 文件名在多版本选择下拉里也有一份，按钮角色才唯一
        page.get_by_role("button", name="长安三万里.2023.2160p.WEB-DL.mkv").click()
        expect(page.get_by_text("来源", exact=True)).to_be_visible()
        expect(page.get_by_text("存量扫描发现（非本系统入库）").first).to_be_visible()
        expect(page.get_by_text("手动扫描发现")).to_be_visible()
        page.screenshot(path=str(shots / "duplicates-条目页来源.png"), full_page=True)

        # 入口跳回管理页并按本条目筛选
        entry.click()
        page.wait_for_url(re.compile(r"tab=duplicates"))
        assert f"item={ids['twin']}" in page.url
        expect(page.get_by_role("button", name=re.compile("只看《长安三万里》"))).to_be_visible()
        expect(page.get_by_role("link", name="长安三万里")).to_be_visible()
        expect(page.get_by_role("link", name="九门")).to_have_count(0)

        assert not page_errors, f"页面报错：{page_errors}"
        context.close()
        browser.close()
