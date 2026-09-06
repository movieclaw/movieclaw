"""影片分享的浏览器端到端（docs/design/media-share.md §8 验收）。

真后端（uvicorn 子进程，TMDB 指向本地假服务）+ 真前端（``pnpm dev``）+ 无头
Chromium（Playwright）+ ffmpeg 生成的 VP9/Opus 真片。两个浏览器上下文：

- **超管**：建库扫描 → 详情页 ⋯ → 分享…（3 天 + 密码）→ 对话框切到「已分享」
  → 再点一次直接看到同一链接 → 管理页「分享」标签 → 取消分享；
- **访客**（全新上下文，没有任何 Cookie）：打开链接只见密码卡片、页面上没有
  片名 → 错密码被拒 → 对密码进影片页（无侧栏、无站内入口）→ 真实播放、
  进度只记本浏览器 → 剧集分享选集播放。

安全断言穿插其中：解锁 Cookie HttpOnly 且 Path 收窄；分享凭据打不开任何既有
业务接口（401）；访客视图无落盘路径；分享条目以外一律 404；取消分享后同一取流
地址立即 404、页面变「分享不存在或已取消」；访客的播放在活动页显示为「分享访客」。

标 integration：要 ffmpeg、pnpm（apps/web 已 install）与 Playwright Chromium，
CI 不跑。本地：``pytest -m integration tests/e2e/test_media_share_browser.py``。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "apps" / "web"
ADMIN = {"username": "admin", "password": "e2e-passw0rd"}
_CHROMIUM_CANDIDATES = (os.environ.get("E2E_CHROMIUM"), "/opt/pw-browsers/chromium")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要系统 ffmpeg"),
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


def _gen_clip(dest: Path, seconds: int, *, chapters: list[tuple[int, str]] | None = None) -> None:
    """VP9 + Opus 的 MP4：Playwright 自带的 Chromium 没有 H.264，这是它能直接播的组合。

    ``chapters`` 是 (起点秒, 标题) 列表：写成 FFMETADATA 内嵌进容器，分享页要验
    「场景」横排真的渲染出来。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    extra: list[str] = []
    if chapters:
        lines = [";FFMETADATA1"]
        for index, (start, title) in enumerate(chapters):
            end = chapters[index + 1][0] if index + 1 < len(chapters) else seconds
            lines += [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start * 1000}",
                f"END={end * 1000}",
                f"title={title}",
            ]
        meta = dest.with_suffix(".ffmeta")
        meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        extra = ["-i", str(meta), "-map_metadata", "2"]
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=640x360:rate=25",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            *extra,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libvpx-vp9", "-b:v", "300k", "-deadline", "realtime", "-cpu-used", "8",
            "-g", "50", "-c:a", "libopus", "-b:a", "48k", "-strict", "-2", "-shortest",
            "-movflags", "+faststart", str(dest),
        ],
        check=True,
        timeout=300,
    )  # fmt: skip
    if chapters:
        dest.with_suffix(".ffmeta").unlink()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """拉起后端 + 前端，产出一部电影与一部两集的剧；模块结束时收掉子进程。"""
    root = tmp_path_factory.mktemp("media-share-e2e")
    movie_root = root / "media" / "movies"
    tv_root = root / "media" / "tv"
    # 电影带两个内嵌章节 + 一份写了导演 / 演员的 NFO：分享页要把「场景」横排、
    # 演职员、音轨字幕、相关链接都渲染出来（假 TMDB 没有这些，只能靠本地）
    movie_file = movie_root / "某电影 (2020)" / "某电影.2020.1080p.mp4"
    _gen_clip(movie_file, 20, chapters=[(0, "开场"), (8, "高潮")])
    movie_file.with_suffix(".nfo").write_text(
        "<movie><title>某电影</title><year>2020</year>"
        "<plot>一部用于端到端验收的假电影。</plot><genre>剧情</genre>"
        "<director>张三</director>"
        "<actor><name>李四</name><role>主角</role></actor>"
        "<actor><name>王五</name><role>配角</role></actor></movie>",
        encoding="utf-8",
    )
    season_dir = tv_root / "测试剧集 (2024)" / "Season 01"
    for ep in (1, 2):
        _gen_clip(season_dir / f"测试剧集.S01E{ep:02d}.1080p.mp4", 10)
    tmdb_log = root / "tmdb-requests.log"
    tmdb_log.touch()

    api_port, web_port = _free_port(), _free_port()
    data = root / "data"
    data.mkdir()
    env = {
        **os.environ,
        "APP_ENV": "local",
        "APP_RELOAD": "false",
        "APP_PORT": str(api_port),
        "DATABASE_URL": f"sqlite+aiosqlite:///{data / 'app.db'}",
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
            "movie_root": movie_root,
            "tv_root": tv_root,
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


def _wait_for(fn, *, timeout: float, what: str, interval: float = 1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"等待超时：{what}（最后一次观察：{last!r}）")


def _login(page, base: str) -> None:
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


def _play_current_video(page) -> None:
    """等 <video> 就绪 → 静音起播（无头浏览器没有用户手势）→ 真的在走。"""
    page.wait_for_function(
        "() => { const v = document.querySelector('video'); return v && v.readyState >= 2; }",
        timeout=30_000,
    )
    page.evaluate(
        "() => { const v = document.querySelector('video'); v.muted = true;"
        " return v.play().catch(() => null); }"
    )
    page.wait_for_function(
        "() => { const v = document.querySelector('video'); return v && v.currentTime > 2; }",
        timeout=30_000,
    )


def _no_site_entrances(page) -> None:
    """分享页是一张独立的页：没有侧栏、没有导航、没有任何站内链接。"""
    for prefix in ("/library", "/login", "/settings", "/search", "/subscriptions", "/discover"):
        assert page.locator(f'a[href^="{prefix}"]').count() == 0, f"分享页出现了站内入口 {prefix}"
    assert page.locator("aside").count() == 0
    assert page.locator("nav").count() == 0


def test_media_share_full_flow(stack) -> None:  # noqa: PLR0915
    from playwright.sync_api import expect, sync_playwright

    base = stack["base"]
    shots: Path = stack["shots"]
    shots.mkdir(exist_ok=True)
    movie_root: Path = stack["movie_root"]

    def api(ctx, method: str, path: str, **kw):
        return getattr(ctx.request, method)(f"{base}/api/v1{path}", **kw)

    def api_json(ctx, path: str) -> dict:
        resp = api(ctx, "get", path)
        assert resp.ok, f"{path}: {resp.status} {resp.text()}"
        return resp.json()["data"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **_chromium_kwargs())
        admin_ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        admin = admin_ctx.new_page()
        admin.set_default_timeout(20_000)
        admin.set_default_navigation_timeout(120_000)
        page_errors: list[str] = []
        admin.on("pageerror", lambda e: page_errors.append(str(e)))

        # ---- 超管：建号登录，建两个库并等扫描识别出真片 ----
        _login(admin, base)
        movie_lib = api(
            admin, "post", "/libraries",
            data={"name": "电影", "kind": "movie", "root_paths": [str(movie_root)]},
        ).json()["data"]["id"]
        tv_lib = api(
            admin, "post", "/libraries",
            data={"name": "剧集", "kind": "tv", "root_paths": [str(stack["tv_root"])]},
        ).json()["data"]["id"]

        def scanned(lib_id: int, title: str):
            lib = next(x for x in api_json(admin, "/libraries") if x["id"] == lib_id)
            if lib["scanning"] or not lib["last_scan"]:
                return None
            items = api_json(admin, f"/libraries/{lib_id}/items")
            rows = items if isinstance(items, list) else items.get("items", [])
            return next((r for r in rows if r.get("title") == title), None)

        movie = _wait_for(lambda: scanned(movie_lib, "某电影"), timeout=180, what="电影库扫描识别")
        show = _wait_for(lambda: scanned(tv_lib, "测试剧集"), timeout=180, what="剧集库扫描识别")
        movie_id, show_id = movie["media_item_id"], show["media_item_id"]

        # ---- 超管：详情页 ⋯ → 分享… → 3 天 + 密码 → 生成链接 ----
        admin.goto(f"{base}/library/{movie_lib}/item/{movie_id}")
        expect(admin.get_by_role("heading", name="某电影")).to_be_visible(timeout=60_000)
        admin.get_by_role("button", name="更多操作").click()
        admin.get_by_role("menuitem", name="分享…").click()
        dialog = admin.get_by_role("dialog")
        expect(dialog.get_by_text("分享《某电影》")).to_be_visible()
        dialog.get_by_role("radio", name="3 天").click()
        dialog.get_by_role("checkbox").check()
        password_box = dialog.get_by_label("访问密码")
        generated = password_box.input_value()
        assert re.fullmatch(r"[abcdefghijkmnpqrstuvwxyz23456789]{6}", generated), generated
        password_box.fill("k7pw2m")
        admin.screenshot(path=str(shots / "01-share-dialog-form.png"))
        dialog.get_by_role("button", name="生成链接").click()
        expect(dialog.get_by_text("《某电影》已分享")).to_be_visible()
        expect(dialog.get_by_text("k7pw2m", exact=True)).to_be_visible()
        expect(dialog.get_by_role("button", name="复制链接和密码")).to_be_visible()
        admin.screenshot(path=str(shots / "02-share-dialog-ready.png"))

        share = api_json(admin, f"/libraries/{movie_lib}/items/{movie_id}/share")
        assert share and share["password"] == "k7pw2m" and share["view_count"] == 0
        slug = share["slug"]
        assert len(slug) == 16
        assert share["url"] == f"/s/{slug}"  # 未配置外部访问地址：相对路径
        expect(dialog.locator(f'[title$="/s/{slug}"]')).to_be_visible()
        remaining_h = (
            time.mktime(time.strptime(share["expires_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            - time.mktime(time.gmtime())
        ) / 3600
        assert 71 < remaining_h <= 72.1, share["expires_at"]
        admin.keyboard.press("Escape")
        expect(dialog).to_have_count(0)

        # 再点一次：直接是「已分享」形态，同一条链接
        admin.get_by_role("button", name="更多操作").click()
        admin.get_by_role("menuitem", name="分享…").click()
        dialog = admin.get_by_role("dialog")
        expect(dialog.get_by_text("《某电影》已分享")).to_be_visible()
        expect(dialog.locator(f'[title$="/s/{slug}"]')).to_be_visible()
        admin.keyboard.press("Escape")

        # ---- 访客：全新上下文，没有任何 Cookie ----
        visitor_ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
        visitor = visitor_ctx.new_page()
        visitor.set_default_timeout(20_000)
        visitor.set_default_navigation_timeout(120_000)
        visitor.on("pageerror", lambda e: page_errors.append(f"visitor: {e}"))

        probe = api(visitor_ctx, "get", f"/share/{slug}")
        assert probe.status == 200
        assert probe.json()["data"] == {
            "requires_password": True,
            "unlocked": False,
            "expires_at": probe.json()["data"]["expires_at"],
            "media_item_id": None,
        }
        assert "某电影" not in probe.text(), "密码之前探针不能露片名"
        assert api(visitor_ctx, "get", f"/share/{slug}/item").status == 401
        assert api(visitor_ctx, "get", "/share/no-such-share-slug").status == 404

        visitor.goto(f"{base}/s/{slug}")
        expect(visitor.get_by_role("heading", name="需要密码")).to_be_visible()
        assert "某电影" not in visitor.content(), "密码卡片不能露片名"
        _no_site_entrances(visitor)
        visitor.screenshot(path=str(shots / "03-visitor-password-gate.png"))
        visitor.locator("input[type=password]").fill("wrong0")
        visitor.get_by_role("button", name="打开").click()
        expect(visitor.get_by_text("密码不对")).to_be_visible()
        assert "/s/" in visitor.url and "/login" not in visitor.url, "401 不能把访客弹去登录页"
        visitor.locator("input[type=password]").fill("k7pw2m")
        visitor.get_by_role("button", name="打开").click()

        # ---- 访客：影片页 ----
        expect(visitor.get_by_role("heading", name="某电影")).to_be_visible(timeout=60_000)
        expect(visitor.get_by_text("2020")).to_be_visible()
        expect(visitor.get_by_text(re.compile("失效"))).to_be_visible()
        expect(visitor.get_by_role("button", name="播放", exact=True)).to_be_visible()
        # 浏览面要完整：音轨 / 字幕两行、「场景」横排（内嵌章节）、演职员、相关链接
        expect(visitor.get_by_text("音轨", exact=True)).to_be_visible()
        expect(visitor.get_by_text("字幕", exact=True)).to_be_visible()
        expect(visitor.get_by_role("heading", name="场景")).to_be_visible()
        expect(visitor.get_by_text("2 个章节")).to_be_visible()
        expect(visitor.get_by_text("开场")).to_be_visible()
        expect(visitor.get_by_text("李四")).to_be_visible()
        expect(visitor.get_by_text("张三")).to_be_visible()
        tmdb_link = visitor.get_by_role("link", name="TMDB")
        expect(tmdb_link).to_be_visible()
        assert (tmdb_link.get_attribute("href") or "").startswith("https://www.themoviedb.org/movie/300")
        _no_site_entrances(visitor)
        visitor.screenshot(path=str(shots / "04-visitor-item-page.png"), full_page=True)

        # 解锁 Cookie：HttpOnly、Path 收窄到这一条分享的接口
        unlock_cookie = next(c for c in visitor_ctx.cookies() if c["name"] == "movieclaw_share")
        assert unlock_cookie["httpOnly"] is True
        assert unlock_cookie["path"] == f"/api/v1/share/{slug}"
        # 分享凭据打不开任何既有业务接口
        for path in (
            "/libraries",
            f"/libraries/{movie_lib}/items/{movie_id}",
            f"/playback/items/{movie_id}",
            "/playback/resume?media_item_id=1",
            "/shares",
        ):
            assert api(visitor_ctx, "get", path).status == 401, path
        # 访客视图：没有落盘路径、没有库归属；分享条目以外 404
        item_resp = api(visitor_ctx, "get", f"/share/{slug}/item")
        assert item_resp.status == 200
        item_text = item_resp.text()
        assert str(movie_root) not in item_text and "file_path" not in item_text
        assert "file_name" not in item_text and "scrape_library" not in item_text
        assert item_resp.json()["data"]["media_item_id"] == movie_id
        for path in (
            f"/share/{slug}/playback/items/{show_id}",
            f"/share/{slug}/playback/items/{show_id}/episodes?season_number=1",
            f"/share/{slug}/images/assets/{show_id}/poster.jpg",
        ):
            assert api(visitor_ctx, "get", path).status == 404, path
        # 访客试着进站内页面：登录闸门把他送去登录页，看不到任何内容
        visitor.goto(f"{base}/library")
        visitor.wait_for_url(lambda u: "/login" in u)
        visitor.goto(f"{base}/s/{slug}")
        expect(visitor.get_by_role("heading", name="某电影")).to_be_visible(timeout=60_000)

        # ---- 访客：真实播放；超管活动页看到「分享访客」 ----
        visitor.get_by_role("button", name="播放", exact=True).click()
        visitor.wait_for_url(lambda u: f"/s/{slug}/play" in u)
        _play_current_video(visitor)
        stream_url = visitor.evaluate("() => document.querySelector('video').currentSrc")
        assert "token=" in stream_url, stream_url
        visitor.screenshot(path=str(shots / "05-visitor-playing.png"))

        def visitor_on_activity():
            body = api(admin, "get", "/playback/activity").text()
            return body if "分享访客" in body else None

        _wait_for(visitor_on_activity, timeout=45, what="活动页出现分享访客", interval=2)
        # 跳到 8 秒再退出：进度只记访客自己的浏览器，服务端成员表里没有他
        visitor.evaluate("() => { document.querySelector('video').currentTime = 8; }")
        visitor.wait_for_timeout(1500)
        visitor.go_back()
        visitor.wait_for_url(lambda u: u.rstrip("/").endswith(f"/s/{slug}"))
        expect(visitor.get_by_role("button", name=re.compile("^继续观看"))).to_be_visible(
            timeout=30_000
        )
        admin_resume = api_json(
            admin, f"/playback/resume?media_item_id={movie_id}&season_number=0&episode_number=0"
        )
        assert admin_resume["position_ms"] == 0, "访客的进度不能写进超管的观看状态"
        assert "movieclaw_session" not in {c["name"] for c in visitor_ctx.cookies()}
        share = api_json(admin, f"/libraries/{movie_lib}/items/{movie_id}/share")
        assert share["view_count"] >= 1 and share["last_accessed_at"]

        # ---- 剧集分享（无密码，1 天）：整部剧、选集播放 ----
        show_share = api(
            admin, "post", f"/libraries/{tv_lib}/items/{show_id}/share",
            data={"expires_in_days": 1, "password": None},
        ).json()["data"]
        show_slug = show_share["slug"]
        assert show_share["password"] is None
        visitor.goto(f"{base}/s/{show_slug}")
        expect(visitor.get_by_role("heading", name="测试剧集")).to_be_visible(timeout=60_000)
        expect(visitor.get_by_role("heading", name="分集")).to_be_visible()
        cards = visitor.locator("[data-episode-number]")
        expect(cards).to_have_count(3)  # 元数据三集 ∪ 库里两集：第三集灰显
        expect(visitor.locator('[data-episode-number="3"]').get_by_text("缺")).to_be_visible()
        visitor.locator('[data-episode-number="2"]').click()
        expect(visitor.get_by_text(re.compile("第 1 季 第 2 集"))).to_be_visible()
        visitor.screenshot(path=str(shots / "06-visitor-show-page.png"))
        visitor.get_by_role("button", name="播放", exact=True).click()
        visitor.wait_for_url(lambda u: f"/s/{show_slug}/play/s01e02" in u)
        _play_current_video(visitor)
        visitor.go_back()
        visitor.wait_for_url(lambda u: u.rstrip("/").endswith(f"/s/{show_slug}"))

        # ---- 超管：管理页「分享」标签列出两条 → 取消电影的分享 ----
        admin.goto(f"{base}/library/manage?tab=shares")
        expect(admin.get_by_role("tab", name=re.compile("^分享"))).to_have_attribute(
            "aria-selected", "true"
        )
        expect(admin.get_by_role("link", name="某电影")).to_be_visible()
        expect(admin.get_by_role("link", name="测试剧集")).to_be_visible()
        admin.screenshot(path=str(shots / "07-manage-shares-tab.png"))
        movie_row = admin.locator("li").filter(has=admin.get_by_role("link", name="某电影"))
        movie_row.get_by_role("button", name="取消").click()
        admin.get_by_role("dialog").get_by_role("button", name="取消分享").click()
        expect(admin.get_by_role("link", name="某电影")).to_have_count(0)
        assert [s["slug"] for s in api_json(admin, "/shares")] == [show_slug]

        # ---- 取消后：同一取流地址立即 404；访客刷新看到「分享不存在或已取消」 ----
        assert api(visitor_ctx, "get", stream_url).status == 404
        assert api(visitor_ctx, "get", f"/share/{slug}/item").status == 404
        visitor.goto(f"{base}/s/{slug}")
        expect(visitor.get_by_text("分享不存在或已取消")).to_be_visible()
        visitor.screenshot(path=str(shots / "08-visitor-revoked.png"))
        # 剧集的分享不受影响
        assert api(visitor_ctx, "get", f"/share/{show_slug}/item").status == 200

        assert not page_errors, page_errors
        visitor_ctx.close()
        admin_ctx.close()
        browser.close()
