"""「其他」媒体库的浏览器端到端（docs/design/library-other-kind.md 全链路验收）。

真后端（uvicorn 子进程）+ 真前端（``pnpm dev`` 子进程）+ ffmpeg 生成的 VP9/Opus
假视频 + 无头 Chromium（Playwright）。覆盖：首次引导登录 → 建「其他」库 →
建库即扫描 → 抓帧缩略图 → 16:9 海报墙与能力位收敛的菜单 → 详情页读 sidecar
NFO → 播放 → 服务端记进度、详情页「继续观看」→ 影视库认不出的文件以临时
身份上墙（未识别角标、可播、保留修正识别）→ 监听导入规则「落入其他库」→
往监听目录丢文件 → 原样入库 → Jellyfin 视图输出 homevideos/Video。

标 integration：要 ffmpeg、pnpm（apps/web 已 install）与 Playwright Chromium，
CI 不跑。本地：``pytest -m integration tests/e2e``。前端 dev server 用独立的
``.next-e2e`` 构建目录，Next 会往 apps/web/tsconfig.json 的 include 里补一行
（可 git checkout 还原）。
"""

from __future__ import annotations

import json
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


def _gen_clip(dest: Path, seconds: int) -> None:
    """VP9 + Opus 的 MP4：Playwright 自带的 Chromium 没有 H.264，这是它能直接播的组合。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=640x360:rate=25",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libvpx-vp9", "-b:v", "300k", "-deadline", "realtime", "-cpu-used", "8",
            "-g", "50", "-c:a", "libopus", "-b:a", "48k", "-strict", "-2", "-shortest",
            "-movflags", "+faststart", str(dest),
        ],
        check=True,
        timeout=300,
    )  # fmt: skip


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """拉起后端 + 前端，产出假媒体目录；模块结束时收掉子进程。"""
    root = tmp_path_factory.mktemp("other-library-e2e")
    home_root = root / "media" / "home"
    movie_root = root / "media" / "movies"
    watch = root / "watch"
    watch.mkdir(parents=True)
    # 续播点按 Jellyfin 阈值落库（片长 ≥5 分钟、进度 ≥5%），主片给 6 分钟
    _gen_clip(home_root / "2019" / "春节团圆饭.mp4", 360)
    (home_root / "2019" / "春节团圆饭.nfo").write_text(
        "<movie><title>春节团圆饭</title><plot>2019 年除夕全家合影与年夜饭记录。</plot>"
        "<premiered>2019-02-04</premiered><runtime>12</runtime><genre>家庭</genre></movie>",
        encoding="utf-8",
    )
    _gen_clip(home_root / "旅行 Vlog 第三期.mp4", 8)
    _gen_clip(movie_root / "zzqx" / "zzqx.mp4", 8)
    pending = root / "pending-派对全程.mp4"
    _gen_clip(pending, 8)

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
            "api": f"http://127.0.0.1:{api_port}",
            "home_root": home_root,
            "movie_root": movie_root,
            "watch": watch,
            "pending": pending,
            "shots": root / "shots",
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


def _pick_directory(page, path: str) -> None:
    """目录选择弹窗：铅笔切到手动输入 → 输入绝对路径回车 → 选择此目录。"""
    page.get_by_title("手动输入路径").click()
    box = page.get_by_placeholder("输入绝对路径后回车跳转")
    box.fill(path)
    box.press("Enter")
    page.get_by_role("button", name="选择此目录").click()


def test_other_library_full_flow(stack) -> None:  # noqa: PLR0915
    from playwright.sync_api import expect, sync_playwright

    base, api = stack["base"], stack["api"]
    shots: Path = stack["shots"]
    shots.mkdir(exist_ok=True)

    def api_get(page, path: str) -> dict:
        resp = page.request.get(f"{base}/api/v1{path}")
        assert resp.ok, f"{path}: {resp.status} {resp.text()}"
        return resp.json()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **_chromium_kwargs())
        page = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="zh-CN"
        ).new_page()
        page.set_default_timeout(20_000)
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # ---- 首次引导 + 登录（AuthField 的 label 没有 for 绑定，按输入框顺序填）----
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

        # ---- 建「其他」库：表单只剩基本信息，带缩略图/首页排除两个开关 ----
        page.goto(f"{base}/library")
        page.get_by_role("button", name=re.compile("创建第一个媒体库|添加媒体库")).first.click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="其他", exact=True).click()
        expect(dialog.get_by_role("tab", name="收藏范围")).to_have_count(0)
        expect(dialog.get_by_role("tab", name="刮削设置")).to_have_count(0)
        expect(dialog.get_by_text("为本地内容生成缩略图")).to_be_visible()
        expect(dialog.get_by_text("不在首页展示")).to_be_visible()
        dialog.locator("input[type=text]").first.fill("家庭录像")
        dialog.get_by_role("button", name="浏览服务器目录并添加").click()
        _pick_directory(page, str(stack["home_root"]))
        page.screenshot(path=str(shots / "01-create-video-library.png"))
        dialog.get_by_role("button", name="保存", exact=True).click()
        expect(dialog).to_have_count(0)
        home = next(lib for lib in api_get(page, "/libraries")["data"] if lib["name"] == "家庭录像")
        assert home["kind"] == "video" and home["source"] == "local"
        assert home["capabilities"]["scraped"] is False and home["capabilities"]["naming"] is False
        assert home["capabilities"]["jellyfin_collection"] == "homevideos"

        # ---- 建库即扫描：零识别、按 NFO/文件名命名；抓帧缩略图 16:9 ----
        def scanned():
            lib = next(x for x in api_get(page, "/libraries")["data"] if x["id"] == home["id"])
            return lib if (not lib["scanning"] and lib["stats"]["item_count"] == 2) else None

        assert (
            _wait_for(scanned, timeout=90, what="其他库扫描入账 2 条")["stats"][
                "unidentified_count"
            ]
            == 0
        )
        items = api_get(page, f"/libraries/{home['id']}/items")["data"]
        assert {i["title"] for i in items} == {"春节团圆饭", "旅行 Vlog 第三期"}
        assert all(i["source"] == "local" and i["tmdb_id"] is None for i in items)

        def thumbed():
            rows = api_get(page, f"/libraries/{home['id']}/items")["data"]
            ok = all(r["poster_url"] and "/images/assets/" in r["poster_url"] for r in rows)
            return rows if ok else None

        items = _wait_for(thumbed, timeout=90, what="两条录像都生成缩略图")
        assert all(abs(i["primary_aspect"] - 16 / 9) < 0.01 for i in items)

        # ---- 海报墙：16:9 卡片、图片真加载；菜单无「整理文件名」----
        page.goto(f"{base}/library/{home['id']}")
        expect(page.locator("[data-library-item-id]")).to_have_count(2)
        page.wait_for_function(
            "() => { const imgs = Array.from("
            "document.querySelectorAll('[data-library-item-id] img'));"
            " return imgs.length === 2 && imgs.every(i => i.complete && i.naturalWidth > 0); }",
            timeout=30_000,
        )
        ratio = page.evaluate(
            "() => getComputedStyle(document.querySelector("
            "'[data-library-item-id] [style*=aspect-ratio]')).aspectRatio"
        )
        assert ratio.strip().startswith("1.7"), ratio
        page.get_by_role("button", name="更多操作").click()
        expect(page.get_by_role("menuitem", name="整理文件名")).to_have_count(0)
        expect(page.get_by_role("menuitem", name="重新生成缩略图")).to_be_visible()
        page.keyboard.press("Escape")
        page.screenshot(path=str(shots / "02-video-wall.png"))

        # ---- 详情页：NFO 简介；菜单无识别/刮削/选图 ----
        nfo_item = next(i for i in items if i["title"] == "春节团圆饭")
        page.goto(f"{base}/library/{home['id']}/item/{nfo_item['media_item_id']}")
        expect(page.get_by_role("heading", name="春节团圆饭")).to_be_visible()
        expect(page.get_by_text("2019 年除夕全家合影")).to_be_visible()
        page.get_by_role("button", name="更多操作").click()
        for gone in ("修正识别结果…", "刷新元数据", "更换图片…"):
            expect(page.get_by_role("menuitem", name=gone)).to_have_count(0)
        expect(page.get_by_role("menuitem", name="重新生成缩略图")).to_be_visible()
        page.keyboard.press("Escape")

        # ---- 播放 → 服务端记进度 → 详情页「继续观看」----
        page.get_by_role("button", name="播放", exact=True).click()
        page.wait_for_url(lambda u: "/play/" in u)
        page.wait_for_function(
            "() => { const v = document.querySelector('video'); return v && v.readyState >= 2; }",
            timeout=30_000,
        )
        # 无头浏览器没有用户手势：静音后 play（用户本来就是点了播放键进来的）
        page.evaluate(
            "() => { const v = document.querySelector('video'); v.muted = true;"
            " return v.play().catch(() => null); }"
        )
        page.wait_for_function(
            "() => { const v = document.querySelector('video'); return v && v.currentTime > 2; }",
            timeout=30_000,
        )
        page.evaluate("() => { document.querySelector('video').currentTime = 60; }")
        unit = f"media_item_id={nfo_item['media_item_id']}&season_number=0&episode_number=0"

        def progressed():
            state = api_get(page, f"/playback/resume?{unit}")["data"]
            return state if (state.get("position_ms") or 0) > 0 else None

        state = _wait_for(progressed, timeout=45, what="服务端记录到播放进度", interval=2)
        assert state["duration_ms"] == 360_000, state
        page.screenshot(path=str(shots / "03-playing.png"))
        page.go_back()
        page.wait_for_url(lambda u: "/item/" in u)
        page.reload()
        expect(page.get_by_role("button", name=re.compile("^继续观看"))).to_be_visible(
            timeout=30_000
        )

        # ---- 影视库里认不出的文件：临时身份上墙、未识别角标、可播、保留修正识别 ----
        page.goto(f"{base}/library")
        page.get_by_role("button", name="添加媒体库").first.click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="电影", exact=True).click()
        dialog.locator("input[type=text]").first.fill("电影库")
        dialog.get_by_role("button", name="浏览服务器目录并添加").click()
        _pick_directory(page, str(stack["movie_root"]))
        dialog.get_by_role("button", name="保存", exact=True).click()
        expect(dialog).to_have_count(0)
        movie_lib = next(
            lib for lib in api_get(page, "/libraries")["data"] if lib["name"] == "电影库"
        )

        def movie_scanned():
            lib = next(x for x in api_get(page, "/libraries")["data"] if x["id"] == movie_lib["id"])
            return lib if not lib["scanning"] and lib["last_scan"] else None

        _wait_for(movie_scanned, timeout=120, what="电影库扫描完成")
        rows = api_get(page, f"/libraries/{movie_lib['id']}/items")["data"]
        assert len(rows) == 1 and rows[0]["source"] == "local" and rows[0]["tmdb_id"] is None
        pending = api_get(
            page,
            f"/libraries/identification/unidentified-files?library_id={movie_lib['id']}",
        )
        assert len(pending["data"]) >= 1
        page.goto(f"{base}/library/{movie_lib['id']}")
        expect(page.locator("[data-library-item-id]")).to_have_count(1)
        expect(page.get_by_label("未识别").first).to_be_visible()
        page.screenshot(path=str(shots / "04-movie-unidentified.png"))
        page.locator("[data-library-item-id] a").first.click()
        page.wait_for_url(lambda u: "/item/" in u)
        expect(page.get_by_role("button", name="播放", exact=True)).to_be_visible()
        page.get_by_role("button", name="更多操作").click()
        expect(page.get_by_role("menuitem", name="修正识别结果…")).to_be_visible()
        expect(page.get_by_role("menuitem", name="刷新元数据")).to_have_count(0)
        page.keyboard.press("Escape")

        # ---- 监听导入：落入其他库 → 丢文件 → 原样入库 ----
        page.goto(f"{base}/settings/import-watch")
        page.get_by_role("button", name="添加自动入库规则").first.click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="浏览服务器目录并选择…").click()
        _pick_directory(page, str(stack["watch"]))
        dialog.get_by_role("button", name="落入其他库").click()
        dialog.get_by_role("button", name="复制", exact=True).click()
        dialog.get_by_role("button", name="保存", exact=True).click()
        expect(dialog).to_have_count(0)
        rule = next(
            r
            for r in api_get(page, "/import-watch")["data"]
            if r["source_path"] == str(stack["watch"])
        )
        assert (
            rule["kind"] == "video"
            and rule["library_id"] is None
            and "其他" in rule["target_label"]
        )

        entry = stack["watch"] / "2024 生日派对"
        entry.mkdir()
        shutil.copyfile(stack["pending"], entry / "派对全程.mp4")
        (entry / "派对全程.nfo").write_text(
            "<movie><title>生日派对</title><year>2024</year></movie>",
            encoding="utf-8",
        )

        def imported():
            lib = next(x for x in api_get(page, "/libraries")["data"] if x["id"] == home["id"])
            return lib if lib["stats"]["item_count"] == 3 else None

        _wait_for(imported, timeout=120, what="监听导入把生日派对原样搬进其他库", interval=2)
        dest = stack["home_root"] / "2024 生日派对"
        assert (dest / "派对全程.mp4").is_file() and (dest / "派对全程.nfo").is_file()
        newest = api_get(page, f"/libraries/{home['id']}/items?sort=added_at")["data"][0]
        assert newest["title"] == "生日派对" and newest["source"] == "local"
        page.goto(f"{base}/library/{home['id']}")
        expect(page.locator("[data-library-item-id]")).to_have_count(3)
        page.screenshot(path=str(shots / "05-wall-after-import.png"))

        # ---- Jellyfin 视角：homevideos 视图、Video 叶子、真实比例 ----
        auth = page.request.post(
            f"{api}/Users/AuthenticateByName",
            data={"Username": ADMIN["username"], "Pw": ADMIN["password"]},
            headers={
                "Authorization": (
                    'MediaBrowser Client="e2e", Device="e2e", DeviceId="e2e-device", Version="1.0"'
                )
            },
        )
        assert auth.ok, auth.text()
        token = auth.json()["AccessToken"]
        views = page.request.get(f"{api}/UserViews", params={"ApiKey": token}).json()
        by_name = {v["Name"]: v for v in views["Items"]}
        assert by_name["家庭录像"]["CollectionType"] == "homevideos"
        jf_items = page.request.get(
            f"{api}/Items", params={"ApiKey": token, "parentId": by_name["家庭录像"]["Id"]}
        ).json()
        assert jf_items["TotalRecordCount"] == 3
        assert {i["Type"] for i in jf_items["Items"]} == {"Video"}
        assert all(abs(i["PrimaryImageAspectRatio"] - 16 / 9) < 0.01 for i in jf_items["Items"])

        assert not page_errors, page_errors
        (shots / "summary.json").write_text(
            json.dumps(
                {"home": home["capabilities"], "resume": state}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        browser.close()
