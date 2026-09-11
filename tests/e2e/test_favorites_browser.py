"""收藏 / 已看标记与首页「我的收藏」的浏览器端到端。

真后端（uvicorn 子进程）+ 真前端（``pnpm dev``）+ 无头 Chromium。库存直接往
SQLite 播种：二十五部电影 + 一部三集剧，各有在位文件。覆盖整条链路、且每一步都
用 Jellyfin 协议交叉核对（网页与 Infuse 点的是同一份数据）：

- 电影详情页：心 → 收藏（Jellyfin ``UserData.IsFavorite`` 同步为真）；对勾 →
  已看（``Played`` 为真、续播点清零）；再点回未看；
- 剧集详情页：心收藏整部剧（Series 的 ``IsFavorite``）；对勾标记当前选中集，
  分集卡右上角出现对勾、Jellyfin 的 ``UnplayedItemCount`` 减一；
- 首页「我的收藏」跟在「接下来继续」之下，横滚最近收藏的 20 部（26 个收藏时最早的
  被挤出）；「查看全部」进 /library/favorites，与单库页同一套海报墙、全部换行
  铺开，顶栏那颗键能切进图床浏览（跨库图廊，与海报墙同一份名单）再切回来，
  顶栏返回键回首页；
- Infuse 取消收藏电影 → 详情页的心翻回未收藏、全部收藏页里也没有了。

标 integration：要 pnpm（apps/web 已 install）与 Playwright Chromium，CI 不跑。
本地：``pytest -m integration tests/e2e/test_favorites_browser.py``。
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
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "apps" / "web"
ADMIN = {"username": "admin", "password": "e2e-passw0rd"}
_CHROMIUM_CANDIDATES = (os.environ.get("E2E_CHROMIUM"), "/opt/pw-browsers/chromium")
_JF_AUTH = 'MediaBrowser Client="Infuse", Device="Apple TV", DeviceId="e2e-atv", Version="8.2"'

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
    root = tmp_path_factory.mktemp("favorites-e2e")
    roots = {name: root / "media" / name for name in ("movies", "shows")}
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


def _eventually(read, expected, *, timeout: float = 10.0):
    """轮询到 ``read()`` 等于 ``expected``（Python 版 Playwright 没有 expect.poll）。
    网页点完心后请求在飞，Jellyfin 侧读到的必须是落库之后的值。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = read()
        if last == expected:
            return
        time.sleep(0.2)
    raise AssertionError(f"等了 {timeout} 秒仍不是 {expected!r}，最后读到 {last!r}")


#: 二十五部电影 + 一部剧 = 26 个收藏，超过首页横滚行的 20 部上限，才测得到「只放最近 20」
MOVIE_COUNT = 25
SHOW_TITLE = "追更的剧"


def _seed_library(
    database_url: str, library_ids: dict[str, int], roots: dict[str, Path]
) -> dict[str, int]:
    """播种二十五部电影 + 一部三集剧（都有在位文件），返回片名 → media_item_id。"""
    from movieclaw_db.engine import Database
    from movieclaw_db.models import (
        FileSource,
        FileState,
        LibraryFile,
        MediaEpisode,
        MediaItem,
    )

    def _file(library_id: int, item_id: int, path: Path, season: int, episode: int) -> LibraryFile:
        path.write_bytes(b"FAKE-MEDIA" * 32)
        return LibraryFile(
            library_id=library_id,
            media_item_id=item_id,
            season_number=season,
            episode_number=episode,
            file_path=str(path),
            size_bytes=path.stat().st_size,
            source=FileSource.SCANNED,
            state=FileState.IN_PLACE,
            duration_seconds=600,
        )

    ids: dict[str, int] = {}

    async def _run() -> None:
        db = Database(database_url)
        try:
            async with db.session() as session:
                for index in range(1, MOVIE_COUNT + 1):
                    title = f"电影 {index:02d}"
                    item = MediaItem(
                        kind="movie",
                        tmdb_id=90_000 + index,
                        title=title,
                        original_title=title,
                        year=2000 + index,
                        aliases=[],
                    )
                    session.add(item)
                    await session.flush()
                    assert item.id
                    ids[title] = item.id
                    session.add(
                        _file(
                            library_ids["movies"],
                            item.id,
                            roots["movies"] / f"movie-{index:02d}.mkv",
                            0,
                            0,
                        )
                    )
                show = MediaItem(
                    kind="tv",
                    tmdb_id=95_000,
                    title=SHOW_TITLE,
                    original_title="Ongoing Show",
                    year=2024,
                    aliases=[],
                )
                session.add(show)
                await session.flush()
                assert show.id
                ids[SHOW_TITLE] = show.id
                for episode in (1, 2, 3):
                    session.add(
                        _file(
                            library_ids["shows"],
                            show.id,
                            roots["shows"] / f"S01E0{episode}.mkv",
                            1,
                            episode,
                        )
                    )
                    session.add(
                        MediaEpisode(
                            media_item_id=show.id,
                            season_number=1,
                            episode_number=episode,
                            name=f"第 {episode} 集",
                        )
                    )
                await session.commit()
        finally:
            await db.dispose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _run()).result()
    return ids


def test_favorites_and_played_end_to_end(stack) -> None:  # noqa: PLR0915
    from playwright.sync_api import expect, sync_playwright

    from movieclaw_jellyfin.ids import item_guid

    base = stack["base"]
    roots: dict[str, Path] = stack["roots"]
    shots: Path = stack["shots"]
    shots.mkdir(exist_ok=True)

    def api(page, method: str, path: str, **kwargs) -> dict:
        resp = getattr(page.request, method)(f"{base}/api/v1{path}", **kwargs)
        assert resp.ok, f"{method} {path}: {resp.status} {resp.text()}"
        return resp.json()

    def favorite_titles(page) -> list[str]:
        return [
            i["title"] for i in api(page, "get", "/playback/favorites?limit=200")["data"]["items"]
        ]

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

        # ---- 两个库走接口建；库存直接播种 ----
        library_ids = {
            key: api(
                page,
                "post",
                "/libraries",
                data={"name": name, "kind": kind, "root_paths": [str(roots[key])]},
            )["data"]["id"]
            for key, name, kind in (("movies", "电影库", "movie"), ("shows", "剧集库", "tv"))
        }
        ids = _seed_library(stack["database_url"], library_ids, roots)

        # ---- Jellyfin 客户端（Infuse）登录：与网页同一个超管，member_id=0 ----
        jf = page.request.post(
            f"{base}/Users/AuthenticateByName",
            data={"Username": ADMIN["username"], "Pw": ADMIN["password"]},
            headers={"Authorization": _JF_AUTH},
        )
        assert jf.ok, jf.text()
        jf_key = jf.json()["AccessToken"]

        def jf_user_data(item_id: int) -> dict:
            resp = page.request.get(f"{base}/Items/{item_guid(item_id)}", params={"ApiKey": jf_key})
            assert resp.ok, resp.text()
            return resp.json()["UserData"]

        def jf_favorite(item_id: int, favorite: bool) -> None:
            method = page.request.post if favorite else page.request.delete
            resp = method(
                f"{base}/UserFavoriteItems/{item_guid(item_id)}", params={"ApiKey": jf_key}
            )
            assert resp.ok, resp.text()

        movie_1 = ids["电影 01"]
        show = ids[SHOW_TITLE]

        # ---- 电影详情页：心 → 收藏；对勾 → 已看；再点回未看 ----
        page.goto(f"{base}/library/{library_ids['movies']}/item/{movie_1}")
        heart = page.get_by_role("button", name="收藏", exact=True)
        expect(heart).to_be_visible()
        expect(heart).to_have_attribute("aria-pressed", "false")
        check = page.get_by_role("button", name="标记为已看")
        expect(check).to_be_visible()
        page.screenshot(path=str(shots / "01-movie-detail-before.png"), full_page=True)

        heart.click()
        expect(page.get_by_role("button", name="取消收藏")).to_have_attribute(
            "aria-pressed", "true"
        )
        _eventually(lambda: jf_user_data(movie_1)["IsFavorite"], True)
        assert api(page, "get", f"/playback/marks?media_item_id={movie_1}")["data"] == {
            "played": False,
            "is_favorite": True,
            "unplayed_count": None,
        }

        check.click()
        expect(page.get_by_role("button", name="标记为未看")).to_have_attribute(
            "aria-pressed", "true"
        )
        # 已看态由对勾变绿表达（不再另写「已看完」）；播放键改为重播
        expect(page.get_by_role("button", name=re.compile("^重新播放"))).to_be_visible()
        _eventually(lambda: jf_user_data(movie_1)["Played"], True)
        resume = api(page, "get", f"/playback/resume?media_item_id={movie_1}")["data"]
        assert resume["played"] is True and resume["position_ms"] == 0
        page.screenshot(path=str(shots / "02-movie-detail-favorited-played.png"), full_page=True)

        # 窄屏：两枚键变成「图标 + 文字」的胶囊、文字随状态变（触屏没有悬停提示）
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("button", name="取消收藏")).to_have_text("已收藏")
        expect(page.get_by_role("button", name="标记为未看")).to_have_text("已看完")
        page.screenshot(path=str(shots / "02b-movie-detail-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 900})
        # 桌面端只留图标，文字藏起来（说明走悬停提示）
        expect(page.get_by_role("button", name="取消收藏").get_by_text("已收藏")).to_be_hidden()

        page.get_by_role("button", name="标记为未看").click()
        expect(page.get_by_role("button", name="标记为已看")).to_have_attribute(
            "aria-pressed", "false"
        )
        expect(page.get_by_role("button", name=re.compile("^播放"))).to_be_visible()
        _eventually(lambda: jf_user_data(movie_1)["Played"], False)
        assert (
            api(page, "get", f"/playback/resume?media_item_id={movie_1}")["data"]["play_count"] == 0
        )

        # ---- 剧集详情页：心收藏整部剧；对勾标记当前选中的第一集 ----
        page.goto(f"{base}/library/{library_ids['shows']}/item/{show}")
        episodes = page.locator("section", has=page.get_by_role("heading", name="分集"))
        expect(episodes.locator("[data-episode-number]")).to_have_count(3)
        page.get_by_role("button", name="收藏", exact=True).click()
        expect(page.get_by_role("button", name="取消收藏")).to_have_attribute(
            "aria-pressed", "true"
        )
        _eventually(lambda: jf_user_data(show)["IsFavorite"], True)
        # 整剧收藏落在整剧目标上，单集没有被顺手收藏
        assert (
            api(
                page,
                "get",
                f"/playback/marks?media_item_id={show}&season_number=1&episode_number=1",
            )["data"]["is_favorite"]
            is False
        )

        page.get_by_role("button", name="标记为已看").click()
        expect(page.get_by_role("button", name="标记为未看")).to_have_attribute(
            "aria-pressed", "true"
        )
        expect(episodes.locator('[data-episode-number="1"]').get_by_label("已看完")).to_be_visible()
        expect(episodes.locator('[data-episode-number="2"]').get_by_label("已看完")).to_have_count(
            0
        )
        _eventually(lambda: jf_user_data(show)["UnplayedItemCount"], 2)
        page.screenshot(path=str(shots / "03-show-detail-episode-played.png"), full_page=True)

        # ---- Infuse 里再收藏一部电影；首页「我的收藏」横滚三部，跟在「接下来继续」之下 ----
        jf_favorite(ids["电影 02"], True)
        page.goto(f"{base}/library")
        section = page.get_by_test_id("favorites-row")
        expect(section.get_by_role("heading", name="我的收藏")).to_be_visible()
        cards = section.get_by_role("link", name=re.compile("^查看《"))
        expect(cards).to_have_count(3)
        # 最近收藏的在前：Infuse 收藏的电影 02 → 整部剧 → 电影 01
        assert favorite_titles(page) == ["电影 02", SHOW_TITLE, "电影 01"]
        expect(section.get_by_role("link", name="查看全部 3 部")).to_be_visible()
        # 首页顺序：接下来继续 → 我的收藏 → 我的媒体库
        # （先接着看正在看的，再挑想看的，最后才是管理入口）
        heading_tops = [
            page.get_by_role("heading", name=name).bounding_box()["y"]
            for name in ("接下来继续", "我的收藏", "我的媒体库")
        ]
        assert heading_tops == sorted(heading_tops)
        page.screenshot(path=str(shots / "04-home-favorites-row.png"), full_page=True)

        # ---- Infuse 再收藏其余电影（共 MOVIE_COUNT+1 部）：首页只横滚最近 20 部 ----
        for index in range(3, MOVIE_COUNT + 1):
            jf_favorite(ids[f"电影 {index:02d}"], True)
        total = MOVIE_COUNT + 1
        page.goto(f"{base}/library")
        expect(cards).to_have_count(20)
        first_top = cards.first.bounding_box()["y"]
        assert cards.last.bounding_box()["y"] == first_top  # 横滚：全部同一行
        # 首页那 20 张是最近收藏的：最后收藏的电影排最前，最早收藏的电影 01 已挤出
        expect(cards.first).to_have_attribute(
            "aria-label", re.compile(f"《电影 {MOVIE_COUNT:02d}》")
        )
        expect(section.get_by_role("link", name=re.compile("《电影 01》"))).to_have_count(0)
        page.screenshot(path=str(shots / "05-home-favorites-20.png"), full_page=True)

        # ---- 查看全部：/library/favorites 是与单库页同一套海报墙，全部铺开换行 ----
        section.get_by_role("link", name=f"查看全部 {total} 部").click()
        page.wait_for_url(re.compile(r"/library/favorites$"))
        expect(page.get_by_role("heading", name="我的收藏")).to_be_visible()
        expect(page.get_by_text(f"{total} 部作品")).to_be_visible()
        wall = page.locator("[data-library-item-id]")
        expect(wall).to_have_count(total)
        assert wall.last.bounding_box()["y"] > wall.first.bounding_box()["y"]  # 网格换行
        expect(wall.last.get_by_role("link", name=re.compile("《电影 01》"))).to_be_visible()
        page.screenshot(path=str(shots / "06-favorites-page.png"), full_page=True)

        # ---- 图床浏览：与单库页同一颗切换键，数据是跨库的收藏图廊 ----
        # 名单与顺序跟海报墙同一份，每组带自己的详情落点库（收藏跨库）
        groups = api(page, "get", "/playback/favorites/gallery?limit=100&offset=0")["data"]
        assert [g["title"] for g in groups] == favorite_titles(page)
        assert {g["library_id"] for g in groups} <= set(library_ids.values())
        page.get_by_role("button", name="图床浏览").click()
        expect(page.get_by_role("button", name="回到海报墙")).to_be_visible()
        expect(page.locator("[data-library-item-id]")).to_have_count(0)
        # 看图的两个偏好收在这一页自己的 ⋯ 里（海报墙上没有可调的，键也不出现）
        page.get_by_role("button", name="浏览设置").click()
        expect(page.get_by_role("menuitemcheckbox", name="按作品分组")).to_be_visible()
        page.keyboard.press("Escape")
        page.screenshot(path=str(shots / "06b-favorites-gallery.png"), full_page=True)
        page.get_by_role("button", name="回到海报墙").click()
        expect(page.locator("[data-library-item-id]")).to_have_count(total)

        # 顶栏返回键回到媒体库首页
        page.get_by_role("button", name=re.compile("^返回上一页")).click()
        page.wait_for_url(re.compile(r"/library$"))

        # ---- Infuse 取消收藏电影 01：详情页的心翻回未收藏、全部收藏页里也没有了 ----
        jf_favorite(movie_1, False)
        page.goto(f"{base}/library/{library_ids['movies']}/item/{movie_1}")
        expect(page.get_by_role("button", name="收藏", exact=True)).to_have_attribute(
            "aria-pressed", "false"
        )
        page.goto(f"{base}/library/favorites")
        expect(page.locator("[data-library-item-id]")).to_have_count(total - 1)
        expect(page.get_by_role("link", name=re.compile("《电影 01》"))).to_have_count(0)
        assert "电影 01" not in favorite_titles(page)
        page.screenshot(
            path=str(shots / "07-favorites-after-jellyfin-unfavorite.png"), full_page=True
        )

        assert not page_errors, f"页面脚本报错：{page_errors}"
        browser.close()
