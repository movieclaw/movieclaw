"""首页「最近观看」⋯ 菜单清空观看记录的浏览器端到端（docs/design/library-access.md 2.6）。

真后端（uvicorn 子进程）+ 真前端（``pnpm dev``）+ 无头 Chromium。观看记录不经
播放器产生，直接往 SQLite 里播种 mock 数据：两个库、五条最近播放时间各异的
状态行（今天 / 三天前 / 上个月）。覆盖：

- 单库页 ⋯ 菜单不再有「清空我的观看记录」；
- 「最近观看」标题右侧的 ⋯ 菜单四条：清空今天 → 只掉今天的两张卡；清空最近
  一周 → 再掉三天前那张；清空某个媒体库（弹窗下拉选库）→ 只掉该库的；清空全部
  → 分区整段隐藏；
- 时间窗口内没有记录时的回执文案；每一步都用接口核对剩余记录。

标 integration：要 pnpm（apps/web 已 install）与 Playwright Chromium，CI 不跑。
本地：``pytest -m integration tests/e2e/test_recent_watch_clear_browser.py``。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "apps" / "web"
ADMIN = {"username": "admin", "password": "e2e-passw0rd"}
_CHROMIUM_CANDIDATES = (os.environ.get("E2E_CHROMIUM"), "/opt/pw-browsers/chromium")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("pnpm") is None, reason="需要 pnpm 启动前端 dev server"),
    pytest.mark.skipif(
        not (WEB / "node_modules").is_dir(), reason="apps/web 未安装依赖（pnpm install）"
    ),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float) -> None:
    import urllib.request

    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310
                if resp.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1)
    raise RuntimeError(f"服务未就绪：{url}（{last}）")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """拉起后端 + 前端；模块结束时收掉子进程。"""
    root = tmp_path_factory.mktemp("recent-watch-e2e")
    roots = {name: root / "media" / name for name in ("a", "b")}
    for path in roots.values():
        path.mkdir(parents=True)
    tmdb_log = root / "tmdb-requests.log"
    tmdb_log.touch()
    api_port, web_port = _free_port(), _free_port()
    data = root / "data"
    data.mkdir()
    database_url = f"sqlite+aiosqlite:///{data / 'app.db'}"
    env = {
        **os.environ,
        "APP_ENV": "local",
        "APP_RELOAD": "false",
        "APP_PORT": str(api_port),
        "DATABASE_URL": database_url,
        "METADATA_DIR": str(data / "metadata"),
        "LOG_DIR": str(data / "logs"),
        "SECRET_KEY_FILE": str(data / ".secret_key"),
        "E2E_TMDB_LOG": str(tmdb_log),
    }
    api_log = (root / "api.log").open("w")
    api = subprocess.Popen(  # noqa: S603
        [sys.executable, str(Path(__file__).with_name("_api_launcher.py"))],
        env=env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        cwd=str(REPO),
    )
    web_log = (root / "web.log").open("w")
    web = subprocess.Popen(  # noqa: S603
        ["pnpm", "dev", "-p", str(web_port)],
        env={
            **os.environ,
            "NEXT_DIST_DIR": ".next-e2e",
            "MOVIECLAW_API_PROXY_TARGET": f"http://127.0.0.1:{api_port}",
        },
        stdout=web_log,
        stderr=subprocess.STDOUT,
        cwd=str(WEB),
    )
    try:
        _wait_http(f"http://127.0.0.1:{api_port}/api/v1/auth/bootstrap", 90)
        _wait_http(f"http://127.0.0.1:{web_port}/login", 180)
        yield {
            "base": f"http://127.0.0.1:{web_port}",
            "roots": roots,
            "database_url": database_url,
            "shots": root / "shots",
            "logs": (root / "api.log", root / "web.log"),
        }
    finally:
        for proc in (web, api):
            proc.terminate()
        for proc in (web, api):
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        api_log.close()
        web_log.close()


def _chromium_kwargs() -> dict:
    for cand in _CHROMIUM_CANDIDATES:
        if cand and Path(cand).exists():
            return {"executable_path": cand}
    return {}


#: mock 观看记录：(库, 片名, 最近播放距今)。两个库各有今天与上个月的记录，
#: 库 A 再多一条三天前的——四种清法各自恰好只碰到该碰的那几条。
_SEED = (
    ("a", "今天看的 A", timedelta(hours=1)),
    ("a", "三天前看的 A", timedelta(days=3)),
    ("a", "上个月看的 A", timedelta(days=30)),
    ("b", "今天看的 B", timedelta(hours=2)),
    ("b", "上个月看的 B", timedelta(days=40)),
)


def _seed_watch_history(database_url: str, library_ids: dict[str, int]) -> None:
    """直接往后端正在用的 SQLite 里播种条目、在位台账与观看状态（超管 member_id=0）。"""
    from movieclaw_db.engine import Database
    from movieclaw_db.models import LibraryFile, MediaItem, PlaybackState
    from movieclaw_db.models.base import utcnow

    async def _run() -> None:
        db = Database(database_url)
        try:
            async with db.session() as session:
                for index, (lib, title, ago) in enumerate(_SEED):
                    item = MediaItem(
                        kind="movie",
                        tmdb_id=90_000 + index,
                        title=title,
                        original_title=title,
                        year=2020,
                        aliases=[],
                    )
                    session.add(item)
                    await session.commit()
                    session.add(
                        LibraryFile(
                            library_id=library_ids[lib],
                            media_item_id=item.id,
                            file_path=f"/media/{lib}/{index}.mkv",
                            source="scanned",
                            size_bytes=1_000,
                        )
                    )
                    session.add(
                        PlaybackState(
                            member_id=0,
                            media_item_id=item.id,
                            position_ms=600_000,
                            play_count=1,
                            last_played_at=utcnow() - ago,
                        )
                    )
                    await session.commit()
        finally:
            await db.dispose()

    # pytest-asyncio 让测试线程上挂着一个事件循环（同步测试也在其中），
    # asyncio.run 不能嵌套；播种放到独立线程里跑自己的循环
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _run()).result()


def test_recent_watch_clear_menu(stack) -> None:  # noqa: PLR0915
    from playwright.sync_api import expect, sync_playwright

    base = stack["base"]
    roots: dict[str, Path] = stack["roots"]
    shots: Path = stack["shots"]
    shots.mkdir(exist_ok=True)

    def api(page, method: str, path: str, **kwargs) -> dict:
        resp = getattr(page.request, method)(f"{base}/api/v1{path}", **kwargs)
        assert resp.ok, f"{method} {path}: {resp.status} {resp.text()}"
        return resp.json()

    def recent_titles(page) -> list[str]:
        return sorted(i["title"] for i in api(page, "get", "/playback/recent")["data"]["items"])

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

        # ---- 两个库走接口建；观看记录直接播种进数据库 ----
        library_ids = {
            key: api(
                page,
                "post",
                "/libraries",
                data={
                    "name": f"库 {key.upper()}",
                    "kind": "movie",
                    "root_paths": [str(roots[key])],
                },
            )["data"]["id"]
            for key in ("a", "b")
        }
        _seed_watch_history(stack["database_url"], library_ids)
        assert recent_titles(page) == sorted(title for _, title, _ in _SEED)

        # ---- 单库页：⋯ 菜单里没有「清空我的观看记录」了 ----
        page.goto(f"{base}/library/{library_ids['a']}")
        page.get_by_role("button", name="更多操作").click()
        menu = page.get_by_role("menu")
        expect(menu.get_by_role("menuitem", name="编辑库")).to_be_visible()
        expect(menu.get_by_role("menuitem", name=re.compile("观看记录"))).to_have_count(0)
        page.screenshot(path=str(shots / "01-library-menu-without-clear.png"))
        page.keyboard.press("Escape")

        # ---- 首页：最近观看五张卡，标题右侧有 ⋯ ----
        page.goto(f"{base}/library")
        section = page.locator("section[aria-labelledby=recent-watch-title]")
        expect(section.get_by_role("heading", name="最近观看")).to_be_visible()

        def card(title: str):
            return section.get_by_role("link", name=re.compile(f"^{re.escape(title)}，"))

        for _, title, _ in _SEED:
            expect(card(title)).to_be_visible()
        expect(page.get_by_role("link", name="管理媒体库")).to_be_visible()
        page.screenshot(path=str(shots / "02-home-recent-watch.png"), full_page=True)

        def open_menu():
            section.get_by_role("button", name="清空观看记录").click()
            return page.get_by_role("menu")

        menu = open_menu()
        for label in (
            "清空今天的观看记录…",
            "清空最近一周的观看记录…",
            "清空全部观看记录…",
            "清空某个媒体库的观看记录…",
        ):
            expect(menu.get_by_role("menuitem", name=label)).to_be_visible()
        page.screenshot(path=str(shots / "03-recent-watch-menu.png"))

        # ---- 清空今天：确认弹窗 → 只掉今天的两张卡 ----
        menu.get_by_role("menuitem", name="清空今天的观看记录…").click()
        confirm = page.get_by_role("dialog").last
        expect(confirm.get_by_text("清空今天的观看记录？")).to_be_visible()
        page.screenshot(path=str(shots / "04-confirm-today.png"))
        confirm.get_by_role("button", name="清空").click()
        expect(page.get_by_text("已清空今天的观看记录")).to_be_visible()
        expect(card("今天看的 A")).to_have_count(0)
        expect(card("今天看的 B")).to_have_count(0)
        expect(card("三天前看的 A")).to_be_visible()
        assert recent_titles(page) == ["三天前看的 A", "上个月看的 A", "上个月看的 B"]

        # 今天已经没有记录：再清一次给出「没有可清除的记录」的回执，卡片不动
        open_menu().get_by_role("menuitem", name="清空今天的观看记录…").click()
        page.get_by_role("dialog").last.get_by_role("button", name="清空").click()
        expect(page.get_by_text("今天的观看记录里没有可清除的记录")).to_be_visible()
        assert recent_titles(page) == ["三天前看的 A", "上个月看的 A", "上个月看的 B"]

        # ---- 清空最近一周：三天前那张也掉，上个月的两张留下 ----
        open_menu().get_by_role("menuitem", name="清空最近一周的观看记录…").click()
        confirm = page.get_by_role("dialog").last
        expect(confirm.get_by_text("清空最近一周的观看记录？")).to_be_visible()
        confirm.get_by_role("button", name="清空").click()
        expect(page.get_by_text("已清空最近一周的观看记录")).to_be_visible()
        expect(card("三天前看的 A")).to_have_count(0)
        expect(card("上个月看的 A")).to_be_visible()
        expect(card("上个月看的 B")).to_be_visible()
        assert recent_titles(page) == ["上个月看的 A", "上个月看的 B"]

        # ---- 清空某个媒体库：弹窗里下拉选「库 B」→ 只掉 B 的 ----
        open_menu().get_by_role("menuitem", name="清空某个媒体库的观看记录…").click()
        dialog = page.get_by_role("dialog", name="清空某个媒体库的观看记录")
        expect(dialog).to_be_visible()
        select = dialog.locator("select")
        expect(select.locator("option")).to_have_count(2)
        select.select_option(label="库 B")
        expect(dialog.get_by_role("button", name="清空「库 B」")).to_be_enabled()
        page.screenshot(path=str(shots / "05-pick-library.png"))
        dialog.get_by_role("button", name="清空「库 B」").click()
        expect(dialog).to_have_count(0)
        expect(page.get_by_text("已清除你在这个库里的观看记录")).to_be_visible()
        expect(card("上个月看的 B")).to_have_count(0)
        expect(card("上个月看的 A")).to_be_visible()
        assert recent_titles(page) == ["上个月看的 A"]

        # ---- 清空全部：分区整段隐藏（连同 ⋯ 入口） ----
        open_menu().get_by_role("menuitem", name="清空全部观看记录…").click()
        confirm = page.get_by_role("dialog").last
        expect(confirm.get_by_text("清空全部观看记录？")).to_be_visible()
        confirm.get_by_role("button", name="清空").click()
        expect(page.get_by_text("已清除你的全部观看记录")).to_be_visible()
        expect(section).to_have_count(0)
        expect(page.get_by_role("heading", name="我的媒体库")).to_be_visible()
        assert recent_titles(page) == []
        page.screenshot(path=str(shots / "06-home-after-clear-all.png"), full_page=True)

        assert not page_errors, f"页面脚本报错：{page_errors}"
        browser.close()
