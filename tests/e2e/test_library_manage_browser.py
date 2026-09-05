"""媒体库「预览与管理分离」的浏览器端到端（docs/design/library-manage.md 验收）。

真后端（uvicorn 子进程，TMDB 指向本地假服务）+ 真前端（``pnpm dev``）+ 无头
Chromium（Playwright）。覆盖：首次引导登录 → 首页空状态落到管理页并自动弹建库
→ 建三个库（电影 / 剧集 / 第二个电影库）→ 管理页一库一行、状态列、类型筛选与
搜索 → ··· 菜单：设为默认库 / 从首页排除 / 编辑库 / 待处理（跳单库页自动开抽屉）
→ 键盘与拖拽调整顺序（API 顺序随之变）→ 删除库 → 首页只剩浏览入口（无卡片菜单、
有「管理媒体库」）→ 手机端管理页「调整顺序」弹窗。

标 integration：要 pnpm（apps/web 已 install）与 Playwright Chromium，CI 不跑。
本地：``pytest -m integration tests/e2e/test_library_manage_browser.py``。
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
    """拉起后端 + 前端，产出三个空库目录；模块结束时收掉子进程。"""
    root = tmp_path_factory.mktemp("library-manage-e2e")
    roots = {name: root / "media" / name for name in ("movies", "tv", "hk-movies", "tv-2")}
    for path in roots.values():
        path.mkdir(parents=True)
    # 一个认不出身份的文件：让「电影」库扫完带上「1 个待识别」（状态列黄点）
    (roots["movies"] / "zzqx" / "zzqx.mp4").parent.mkdir()
    (roots["movies"] / "zzqx" / "zzqx.mp4").write_bytes(b"\0" * 4096)
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
            "roots": roots,
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


def _pick_directory(page, path: str) -> None:
    """目录选择弹窗：铅笔切到手动输入 → 输入绝对路径回车 → 选择此目录。"""
    page.get_by_title("手动输入路径").click()
    box = page.get_by_placeholder("输入绝对路径后回车跳转")
    box.fill(path)
    box.press("Enter")
    page.get_by_role("button", name="选择此目录").click()


def _fill_create_dialog(page, expect, kind_label: str, name: str, root: Path) -> None:
    """建库向导已打开：选类型 → 名称 + 根目录 → （影视库）跳过收藏范围 → 创建。"""
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name=re.compile(f"^{kind_label}")).click()
    dialog.locator("input[type=text]").first.fill(name)
    dialog.get_by_role("button", name="浏览服务器目录并添加").click()
    _pick_directory(page, str(root))
    if kind_label == "其他":
        dialog.get_by_role("button", name="创建并开始扫描").click()
    else:
        dialog.get_by_role("button", name="下一步：收藏范围").click()
        dialog.get_by_role("button", name="跳过，创建并开始扫描").click()
    expect(dialog).to_have_count(0)


def _row(page, name: str):
    """管理页里名为 name 的那一行（行内库名链接的最近的行容器）。"""
    return page.locator("[data-library-row]").filter(
        has=page.get_by_role("link", name=name, exact=True)
    )


def _open_menu(page, name: str):
    page.get_by_role("button", name=f"「{name}」的操作").click()
    return page.get_by_role("menu")


def test_library_manage_full_flow(stack) -> None:  # noqa: PLR0915
    from playwright.sync_api import expect, sync_playwright

    base = stack["base"]
    roots: dict[str, Path] = stack["roots"]
    shots: Path = stack["shots"]
    shots.mkdir(exist_ok=True)

    def api_get(page, path: str) -> dict:
        resp = page.request.get(f"{base}/api/v1{path}")
        assert resp.ok, f"{path}: {resp.status} {resp.text()}"
        return resp.json()

    def libs(page) -> list[dict]:
        return api_get(page, "/libraries")["data"]

    def lib_by_name(page, name: str) -> dict:
        return next(x for x in libs(page) if x["name"] == name)

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

        # ---- 首页空状态：「创建第一个媒体库」是链接，落到管理页并自动弹出建库向导 ----
        page.goto(f"{base}/library")
        page.get_by_role("link", name="创建第一个媒体库").click()
        page.wait_for_url(re.compile(r"/library/manage"))
        expect(page.get_by_role("dialog")).to_be_visible()
        assert "create=1" not in page.url, "进页读完 ?create=1 应立刻抹掉，刷新不再弹"
        _fill_create_dialog(page, expect, "电影", "电影", roots["movies"])
        movie = lib_by_name(page, "电影")
        assert movie["is_default"] is True

        # 新行在管理页出现；建库即扫描，状态列先是扫描相关文案，扫完落到待识别（1 个认不出的文件）
        expect(_row(page, "电影")).to_be_visible()
        expect(page.get_by_role("heading", name="媒体库管理")).to_be_visible()

        def movie_scanned():
            lib = lib_by_name(page, "电影")
            return lib if (not lib["scanning"] and lib["last_scan"]) else None

        movie = _wait_for(movie_scanned, timeout=120, what="电影库首次扫描完成")
        assert movie["stats"]["unidentified_count"] == 1
        expect(_row(page, "电影").get_by_text("1 个待识别")).to_be_visible()

        # ---- 再建两个库：管理页头部的「添加媒体库」按钮 ----
        page.get_by_role("button", name="添加媒体库").click()
        _fill_create_dialog(page, expect, "剧集", "剧集", roots["tv"])
        page.get_by_role("button", name="添加媒体库").click()
        _fill_create_dialog(page, expect, "电影", "港片", roots["hk-movies"])
        hk = lib_by_name(page, "港片")
        assert hk["is_default"] is False, "同类型第二个库不自动成为默认"
        expect(page.locator("[data-library-row]")).to_have_count(3)
        # 表头列名齐全，行内只有一个操作按钮（按钮统一收进菜单）
        table = page.get_by_role("table", name="媒体库列表")
        for col in ("库", "根目录", "库存", "状态", "可见范围", "操作"):
            expect(table.get_by_role("columnheader", name=col, exact=True)).to_be_visible()
        expect(table.get_by_role("row")).to_have_count(4)  # 表头 + 3 行
        expect(_row(page, "港片").get_by_role("button")).to_have_count(2)  # 拖拽柄 + ···

        def all_idle():
            rows = libs(page)
            return rows if all(not r["scanning"] and r["last_scan"] for r in rows) else None

        _wait_for(all_idle, timeout=120, what="三个库都扫完")
        # 界面按 3 秒轮询跟上：三行都离开扫描态后再继续（否则菜单里是「停止扫描」）
        expect(_row(page, "剧集").get_by_text("空闲")).to_be_visible()
        expect(_row(page, "港片").get_by_text("空闲")).to_be_visible()
        expect(_row(page, "电影").get_by_text("1 个待识别")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("在跑任务"))).to_have_count(0)
        expect(_row(page, "剧集").get_by_text(re.compile("最近扫描 .*实时监控开"))).to_be_visible()
        page.screenshot(path=str(shots / "01-manage-desktop.png"), full_page=True)

        # ---- 类型筛选与搜索都在客户端：行数随之变；筛选中拖拽柄隐藏 ----
        page.get_by_role("button", name=re.compile(r"^剧集 1$")).click()
        expect(page.locator("[data-library-row]")).to_have_count(1)
        expect(_row(page, "剧集")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("拖动调整"))).to_have_count(0)
        expect(page.get_by_text("清除筛选后可拖拽排序")).to_be_visible()
        page.get_by_role("button", name=re.compile(r"^剧集 1$")).click()  # 再点一次取消
        expect(page.locator("[data-library-row]")).to_have_count(3)
        search = page.get_by_role("searchbox", name="搜索媒体库")
        search.fill("hk-movies")  # 按根目录搜
        expect(page.locator("[data-library-row]")).to_have_count(1)
        expect(_row(page, "港片")).to_be_visible()
        search.fill("不存在的库")
        expect(page.get_by_text("没有符合条件的媒体库")).to_be_visible()
        page.get_by_role("button", name="清除筛选").click()
        expect(page.locator("[data-library-row]")).to_have_count(3)

        # ---- ··· 菜单：设为默认库 → 「默认」标从「电影」挪到「港片」 ----
        menu = _open_menu(page, "电影")
        expect(menu.get_by_role("menuitem", name="已是默认库")).to_be_disabled()
        page.keyboard.press("Escape")
        menu = _open_menu(page, "港片")
        page.screenshot(path=str(shots / "02-row-menu.png"))
        menu.get_by_role("menuitem", name="设为默认库").click()
        expect(page.get_by_text("已将「港片」设为默认库")).to_be_visible()  # toast 回执
        expect(_row(page, "港片").get_by_text("默认", exact=True)).to_be_visible()
        expect(_row(page, "电影").get_by_text("默认", exact=True)).to_have_count(0)
        assert lib_by_name(page, "港片")["is_default"] is True

        # ---- 从首页排除 / 在首页展示：只改这一个字段 ----
        _open_menu(page, "剧集").get_by_role("menuitem", name="从首页排除").click()
        expect(_row(page, "剧集").get_by_text(re.compile("从首页排除"))).to_be_visible()
        tv = lib_by_name(page, "剧集")
        assert tv["exclude_from_home"] is True and tv["root_paths"] == [str(roots["tv"])]
        _open_menu(page, "剧集").get_by_role("menuitem", name="在首页展示").click()
        expect(_row(page, "剧集").get_by_text(re.compile("在首页展示"))).to_be_visible()

        # ---- 编辑库：弹窗标题带库名；改名后行内更新 ----
        _open_menu(page, "剧集").get_by_role("menuitem", name="编辑库").click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_role("heading", name="编辑「剧集」")).to_be_visible()
        # 编辑按用途分区折叠：先展开「基本信息」才有名称输入框
        dialog.get_by_role("button", name=re.compile("^基本信息")).click()
        dialog.locator("input[type=text]").first.fill("美剧")
        dialog.get_by_role("button", name="保存").click()
        expect(dialog).to_have_count(0)
        expect(_row(page, "美剧")).to_be_visible()

        # ---- 键盘换位：Alt+↓ 把「电影」挪到第二位，API 顺序随之变 ----
        assert [x["name"] for x in libs(page)] == ["电影", "美剧", "港片"]
        _row(page, "电影").get_by_role("button", name=re.compile("拖动调整")).focus()
        page.keyboard.press("Alt+ArrowDown")
        _wait_for(
            lambda: [x["name"] for x in libs(page)] == ["美剧", "电影", "港片"] or None,
            timeout=10,
            what="键盘换位提交到后端",
        )
        expect(page.locator("[data-library-row]").nth(0)).to_contain_text("美剧")

        # ---- 拖拽换位：把「港片」拖到「美剧」的位置（第一位） ----
        grip = _row(page, "港片").get_by_role("button", name=re.compile("拖动调整"))
        # 落点按指针在目标行的上下半判定：放到「美剧」上沿 = 排在它之前
        grip.drag_to(_row(page, "美剧"), target_position={"x": 40, "y": 4})
        _wait_for(
            lambda: [x["name"] for x in libs(page)] == ["港片", "美剧", "电影"] or None,
            timeout=10,
            what="拖拽换位提交到后端",
        )
        expect(page.locator("[data-library-row]").nth(0)).to_contain_text("港片")
        page.screenshot(path=str(shots / "03-after-reorder.png"), full_page=True)

        # ---- 待处理：跳单库页并自动打开抽屉，地址里的 ?pending=1 读完即抹掉 ----
        _open_menu(page, "电影").get_by_role("menuitem", name=re.compile(r"^待处理 · 1 个文件$")).click()
        page.wait_for_url(re.compile(rf"/library/{lib_by_name(page, '电影')['id']}"))
        drawer = page.get_by_label("待处理", exact=True)
        expect(drawer).to_be_visible()
        expect(drawer.get_by_text("zzqx")).to_be_visible()
        assert "pending=1" not in page.url
        page.screenshot(path=str(shots / "04-pending-drawer.png"))

        # ---- 首页：卡片只做入口——没有卡片菜单，有「管理媒体库」链接，卡片可点进库 ----
        page.goto(f"{base}/library")
        expect(page.get_by_role("heading", name="我的媒体库")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("^管理「"))).to_have_count(0)
        expect(page.get_by_role("link", name="管理媒体库")).to_be_visible()
        expect(page.get_by_role("link", name="打开「港片」")).to_be_visible()
        expect(page.get_by_text("个待识别")).to_have_count(0)  # 管理信息不上首页
        page.screenshot(path=str(shots / "05-home.png"), full_page=True)
        page.get_by_role("link", name="管理媒体库").click()
        page.wait_for_url(re.compile(r"/library/manage$"))

        # ---- 删除库：确认后行消失，API 里也没了 ----
        _open_menu(page, "美剧").get_by_role("menuitem", name=re.compile("^删除库")).click()
        confirm = page.get_by_role("dialog").last
        expect(confirm.get_by_text("删除媒体库「美剧」？")).to_be_visible()
        confirm.get_by_role("button", name="删除库").click()
        expect(_row(page, "美剧")).to_have_count(0)
        assert {x["name"] for x in libs(page)} == {"电影", "港片"}

        # ---- 手机端：一库一卡；菜单里「调整顺序」→ 弹窗上下移 → 保存顺序 ----
        mobile = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            locale="zh-CN",
            storage_state=context.storage_state(),
        ).new_page()
        mobile.set_default_timeout(20_000)
        mobile.set_default_navigation_timeout(120_000)
        mobile.goto(f"{base}/library/manage")
        expect(mobile.locator("[data-library-row]")).to_have_count(2)
        expect(mobile.get_by_role("button", name=re.compile("拖动调整"))).to_have_count(0)
        expect(mobile.locator("[data-library-row]").first).to_contain_text("全员")  # 可见范围并进卡片
        mobile.get_by_role("button", name="「港片」的操作").click()
        mobile.get_by_role("menuitem", name="调整顺序").click()
        order = mobile.get_by_role("dialog")
        expect(order.get_by_role("heading", name="调整顺序")).to_be_visible()
        save = order.get_by_role("button", name="保存顺序")
        expect(save).to_be_disabled()  # 没动过不能保存
        order.get_by_role("button", name="「港片」下移").click()
        mobile.screenshot(path=str(shots / "06-mobile-reorder.png"))
        save.click()
        expect(order).to_have_count(0)
        _wait_for(
            lambda: [x["name"] for x in libs(mobile)] == ["电影", "港片"] or None,
            timeout=10,
            what="手机端顺序提交到后端",
        )
        mobile.screenshot(path=str(shots / "07-mobile-manage.png"), full_page=True)

        assert not page_errors, f"页面脚本报错：{page_errors}"
        browser.close()
