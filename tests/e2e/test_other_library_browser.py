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
    # 电影库：一部假 TMDB 认得出的正片 + 一个认不出的文件（混合展示）
    _gen_clip(movie_root / "某电影 (2020)" / "某电影.2020.1080p.mp4", 8)
    _gen_clip(movie_root / "zzqx" / "zzqx.mp4", 8)
    # 剧集库：规范目录两集 + 一个认不出的文件
    tv_root = root / "media" / "tv"
    for ep in (1, 2):
        _gen_clip(tv_root / "测试剧集 (2024)" / "Season 01" / f"测试剧集.S01E{ep:02d}.1080p.mp4", 8)
    _gen_clip(tv_root / "未知内容目录" / "zzqx.mp4", 8)
    pending = root / "pending-派对全程.mp4"
    _gen_clip(pending, 8)
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
            "api": f"http://127.0.0.1:{api_port}",
            "home_root": home_root,
            "movie_root": movie_root,
            "tv_root": tv_root,
            "tmdb_log": tmdb_log,
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


def _create_library(page, expect, kind_label: str, name: str, root: Path) -> None:
    page.get_by_role("button", name=re.compile("创建第一个媒体库|添加媒体库")).first.click()
    dialog = page.get_by_role("dialog")
    # 第 1 步：类型卡片（可访问名 = 标题 + 一句话说明，按开头匹配）
    dialog.get_by_role("button", name=re.compile(f"^{kind_label}")).click()
    # 第 2 步：名称按类型预填，改成指定名；添加根目录
    dialog.locator("input[type=text]").first.fill(name)
    dialog.get_by_role("button", name="浏览服务器目录并添加").click()
    _pick_directory(page, str(root))
    if kind_label == "其他":
        dialog.get_by_role("button", name="创建并开始扫描").click()
    else:
        # 影视库多一步收藏范围：留空跳过（=默认库）
        dialog.get_by_role("button", name="下一步：收藏范围").click()
        expect(dialog.get_by_text("当前：未声明")).to_be_visible()
        dialog.get_by_role("button", name="跳过，创建并开始扫描").click()
    expect(dialog).to_have_count(0)


def _wait_scan_done(page, api_get, library_id: int, *, timeout: float = 120):
    def done():
        lib = next(x for x in api_get(page, "/libraries")["data"] if x["id"] == library_id)
        return lib if not lib["scanning"] and lib["last_scan"] else None

    return _wait_for(done, timeout=timeout, what=f"库 #{library_id} 扫描完成")


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
        # dev server 首次访问要现编译页面，冷启动可达一分钟；只放宽导航超时
        page.set_default_navigation_timeout(120_000)
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
        # 三张类型卡片，「其他」的文案不替用户定义内容、不提数据源
        expect(dialog.get_by_role("button", name=re.compile("^电影 自动识别影片"))).to_be_visible()
        expect(dialog.get_by_text("放什么由你定")).to_be_visible()
        expect(dialog.get_by_text("TMDB")).to_have_count(0)
        dialog.get_by_role("button", name=re.compile("^其他")).click()
        # 其他库只有两步：没有收藏范围；开关不出现在新建里，只提示已按推荐值设好
        expect(dialog.get_by_text("3 · 收藏范围")).to_have_count(0)
        expect(dialog.get_by_text("第 2 步，共 2 步")).to_be_visible()
        expect(dialog.get_by_text("已按推荐值设好")).to_be_visible()
        expect(dialog.get_by_role("switch")).to_have_count(0)
        assert dialog.locator("input[type=text]").first.input_value() == "其他"  # 按类型预填
        dialog.locator("input[type=text]").first.fill("家庭录像")
        create = dialog.get_by_role("button", name="创建并开始扫描")
        expect(create).to_be_disabled()  # 没有根目录不能建
        dialog.get_by_role("button", name="浏览服务器目录并添加").click()
        _pick_directory(page, str(stack["home_root"]))
        page.screenshot(path=str(shots / "01-create-video-library.png"))
        create.click()
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
        # 其他库从建库到入账全程没有碰过 TMDB（假 TMDB 的请求日志为空）
        assert stack["tmdb_log"].read_text(encoding="utf-8") == ""

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

        # 编辑面板：分区折叠 + 摘要行；其他库没有收藏范围/刮削设置两区；开关在分区里
        page.get_by_role("button", name="更多操作").click()
        page.get_by_role("menuitem", name="编辑库").click()
        edit = page.get_by_role("dialog")
        expect(edit.get_by_role("button", name=re.compile("^扫描与监控"))).to_be_visible()
        expect(edit.get_by_text("抓帧缩略图")).to_be_visible()
        expect(edit.get_by_role("button", name=re.compile("^收藏范围"))).to_have_count(0)
        expect(edit.get_by_role("button", name=re.compile("^刮削设置"))).to_have_count(0)
        edit.get_by_role("button", name=re.compile("^扫描与监控")).click()
        expect(edit.get_by_role("switch", name="在首页展示")).to_have_attribute(
            "aria-checked", "true"
        )
        edit.get_by_role("switch", name="在首页展示").click()
        edit.get_by_role("button", name="保存", exact=True).click()
        expect(edit).to_have_count(0)
        home = next(lib for lib in api_get(page, "/libraries")["data"] if lib["id"] == home["id"])
        assert home["exclude_from_home"] is True
        page.screenshot(path=str(shots / "02b-edit-video-library.png"))

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

        # ---- 电影库（混合）：认得出的正片 2:3 海报 + 认不出的文件 16:9 临时条目同墙 ----
        page.goto(f"{base}/library")
        _create_library(page, expect, "电影", "电影库", stack["movie_root"])
        movie_lib = next(
            lib for lib in api_get(page, "/libraries")["data"] if lib["name"] == "电影库"
        )
        assert movie_lib["capabilities"]["scraped"] is True
        movie_lib = _wait_scan_done(page, api_get, movie_lib["id"])
        assert movie_lib["stats"]["item_count"] == 1, movie_lib["stats"]
        assert movie_lib["stats"]["unidentified_count"] == 1, movie_lib["stats"]
        # 默认口径只有正式条目；认不出的文件在独立的 provisional 口径里
        confirmed = api_get(page, f"/libraries/{movie_lib['id']}/items")["data"]
        assert [r["title"] for r in confirmed] == ["某电影"], confirmed
        identified = confirmed[0]
        assert identified["source"] == "tmdb" and identified["tmdb_id"] == 300
        assert abs(identified["primary_aspect"] - 2 / 3) < 0.01
        provisional_rows = api_get(
            page, f"/libraries/{movie_lib['id']}/items?identity=provisional"
        )["data"]
        assert len(provisional_rows) == 1, provisional_rows
        provisional = provisional_rows[0]
        assert provisional["source"] == "local" and provisional["tmdb_id"] is None
        assert abs(provisional["primary_aspect"] - 16 / 9) < 0.01
        pending = api_get(
            page,
            f"/libraries/identification/unidentified-files?library_id={movie_lib['id']}",
        )
        assert len(pending["data"]) == 1
        # 假 TMDB 确实被识别链请求过（对照其他库的零请求）
        assert "/3/search/movie" in stack["tmdb_log"].read_text(encoding="utf-8")

        assert (
            provisional["title"] == "zzqx" and provisional["year"] is None
        )  # 目录名，不写推断年份
        page.goto(f"{base}/library/{movie_lib['id']}")
        # 正式条目在主墙（2:3 海报、拼音序），认不出的文件在下方独立的「未识别」分区
        # （16:9 抓帧、按入账时间），两者不混排
        main_cards = page.locator("[data-library-item-id]:not([data-wall=provisional] *)")
        expect(main_cards).to_have_count(1)
        expect(main_cards.first).to_have_attribute(
            "data-library-item-id", str(identified["media_item_id"])
        )
        shelf = page.locator("[data-wall=provisional]")
        expect(shelf.get_by_role("heading", name="未识别 1")).to_be_visible()
        shelf_cards = shelf.locator("[data-library-item-id]")
        expect(shelf_cards).to_have_count(1)
        expect(shelf_cards.first).to_have_attribute(
            "data-library-item-id", str(provisional["media_item_id"])
        )
        expect(shelf.get_by_text("zzqx")).to_be_visible()
        ratios = page.evaluate(
            "() => Array.from(document.querySelectorAll('[data-library-item-id]')).map(c => ({"
            "id: c.dataset.libraryItemId,"
            " ratio: getComputedStyle(c.querySelector('[style*=aspect-ratio]')).aspectRatio}))"
        )
        by_id = {int(r["id"]): r for r in ratios}
        assert by_id[identified["media_item_id"]]["ratio"].startswith("0.66")
        assert by_id[provisional["media_item_id"]]["ratio"].startswith("1.7")
        expect(page.get_by_text("1 个文件待识别")).to_be_visible()
        # 分区里的「去待处理认领」打开待处理抽屉的待识别页
        shelf.get_by_role("button", name="去待处理认领").click()
        drawer = page.get_by_role("dialog", name="待处理")
        expect(drawer.get_by_role("button", name="待识别 1")).to_be_visible()
        expect(drawer.get_by_text(re.compile("^zzqx"))).to_be_visible()
        page.keyboard.press("Escape")
        # 影视库的 ⋯ 菜单保留整理与刷新元数据
        page.get_by_role("button", name="更多操作").click()
        expect(page.get_by_role("menuitem", name="整理文件名")).to_be_visible()
        expect(page.get_by_role("menuitem", name="刷新元数据")).to_be_visible()
        page.keyboard.press("Escape")
        page.screenshot(path=str(shots / "04-movie-mixed-wall.png"))

        # 影视库的编辑面板多两区：收藏范围（未声明）与刮削设置（跟随全局）
        page.get_by_role("button", name="更多操作").click()
        page.get_by_role("menuitem", name="编辑库").click()
        edit = page.get_by_role("dialog")
        expect(edit.get_by_text("未声明（承接该类型未命中的作品）")).to_be_visible()
        expect(edit.get_by_text("跟随全局设置")).to_be_visible()
        expect(edit.get_by_text("未识别文件缩略图")).to_be_visible()
        page.keyboard.press("Escape")
        expect(edit).to_have_count(0)

        # 已识别条目详情：TMDB 外链 + 完整的识别/刮削/选图菜单
        page.goto(f"{base}/library/{movie_lib['id']}/item/{identified['media_item_id']}")
        expect(page.get_by_role("heading", name="某电影")).to_be_visible()
        expect(page.get_by_role("link", name=re.compile("TMDB"))).to_have_attribute(
            "href", re.compile(r"themoviedb\.org/movie/300")
        )
        page.get_by_role("button", name="更多操作").click()
        for present in ("修正识别结果…", "刷新元数据", "更换图片…", "转移到其他库…"):
            expect(page.get_by_role("menuitem", name=present)).to_be_visible()
        page.keyboard.press("Escape")
        page.screenshot(path=str(shots / "05-movie-identified-detail.png"))

        # 未识别条目详情：可播放，保留「修正识别结果」这条转正通道，无刷新元数据/选图
        page.goto(f"{base}/library/{movie_lib['id']}/item/{provisional['media_item_id']}")
        expect(page.get_by_role("button", name="播放", exact=True)).to_be_visible()
        page.get_by_role("button", name="更多操作").click()
        expect(page.get_by_role("menuitem", name="修正识别结果…")).to_be_visible()
        expect(page.get_by_role("menuitem", name="刷新元数据")).to_have_count(0)
        expect(page.get_by_role("menuitem", name="更换图片…")).to_have_count(0)
        page.keyboard.press("Escape")

        # ---- 剧集库（混合）：两集识别成一部剧 + 认不出的文件 ----
        page.goto(f"{base}/library")
        _create_library(page, expect, "剧集", "剧集库", stack["tv_root"])
        tv_lib = next(lib for lib in api_get(page, "/libraries")["data"] if lib["name"] == "剧集库")
        tv_lib = _wait_scan_done(page, api_get, tv_lib["id"])
        assert tv_lib["stats"]["item_count"] == 1 and tv_lib["stats"]["unidentified_count"] == 1
        tv_rows = api_get(page, f"/libraries/{tv_lib['id']}/items")["data"]
        assert [r["source"] for r in tv_rows] == ["tmdb"], tv_rows
        show = tv_rows[0]
        assert show["title"] == "测试剧集" and show["tmdb_id"] == 200
        assert show["episode_count"] == 2 and show["seasons"] == [1]
        tv_provisional = api_get(page, f"/libraries/{tv_lib['id']}/items?identity=provisional")
        assert [r["title"] for r in tv_provisional["data"]] == ["未知内容目录"]
        page.goto(f"{base}/library/{tv_lib['id']}")
        expect(page.locator("[data-library-item-id]:not([data-wall=provisional] *)")).to_have_count(
            1
        )
        expect(page.get_by_text("测试剧集").first).to_be_visible()
        tv_shelf = page.locator("[data-wall=provisional]")
        expect(tv_shelf.get_by_role("heading", name="未识别 1")).to_be_visible()
        expect(tv_shelf.get_by_text("未知内容目录")).to_be_visible()  # 目录名而非解析残片
        page.screenshot(path=str(shots / "06-tv-mixed-wall.png"))
        page.goto(f"{base}/library/{tv_lib['id']}/item/{show['media_item_id']}")
        expect(page.get_by_role("heading", name="测试剧集")).to_be_visible()
        # 分集区拿到了假 TMDB 的第 1 季两集
        expect(page.get_by_text(re.compile(r"1\. E1")).first).to_be_visible(timeout=30_000)
        page.screenshot(path=str(shots / "07-tv-detail.png"))

        # 首页：库卡片都在；家庭录像勾了「不在首页展示」后没有它的「最近添加」行，
        # 影视库的临时条目也不上首页
        page.goto(f"{base}/library")
        for name in ("家庭录像", "电影库", "剧集库"):
            expect(page.get_by_role("heading", name=name).first).to_be_visible()
        expect(page.get_by_text("某电影").first).to_be_visible()
        expect(page.get_by_text("zzqx")).to_have_count(0)
        expect(page.get_by_text("未知内容目录")).to_have_count(0)
        recent_movie = api_get(page, f"/libraries/{movie_lib['id']}/items?sort=added_at&limit=20")
        assert [r["title"] for r in recent_movie["data"]] == ["某电影"]

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
        page.screenshot(path=str(shots / "08-wall-after-import.png"))

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
        # 影视库视图不受影响：电影库 CollectionType=movies，正片是带 Tmdb 的 Movie，
        # 临时条目同样以 Movie 叶子出现但没有外部 id
        assert by_name["电影库"]["CollectionType"] == "movies"
        assert by_name["剧集库"]["CollectionType"] == "tvshows"
        movies = page.request.get(
            f"{api}/Items",
            params={"ApiKey": token, "parentId": by_name["电影库"]["Id"], "fields": "ProviderIds"},
        ).json()
        assert movies["TotalRecordCount"] == 2
        providers = {m["Name"]: m.get("ProviderIds", {}) for m in movies["Items"]}
        assert providers["某电影"].get("Tmdb") == "300"
        assert all(m["Type"] == "Movie" for m in movies["Items"])
        assert sum(1 for p in providers.values() if "Tmdb" not in p) == 1

        assert not page_errors, page_errors
        (shots / "summary.json").write_text(
            json.dumps(
                {"home": home["capabilities"], "resume": state}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        browser.close()
