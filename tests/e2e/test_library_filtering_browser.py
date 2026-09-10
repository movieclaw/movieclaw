"""媒体库筛选与合集的浏览器端到端。

真后端（uvicorn 子进程）+ 真前端（``pnpm dev``）+ 无头 Chromium。库存直接往
SQLite 播种：一个二十四部电影的电影库，刻意铺开每一个筛选维度都用得上的事实
（类型 / 地区 / 年代 / 评分 / 片长 / 语言 / 画质 / HDR / 失联 / 未刮削 / 观看态），
外加一个剧集库做"跨库不串台"的对照。

覆盖 docs/design/library-filtering.md 与 library-collections.md 这轮全部改动：

- 静止态密度：一行只有排序与「筛选」两个控件（铁律：静止态只留信息不留控件）；
- 一级四维下拉：计数、多选不关菜单、维内「或」维间「且」画出来；
- 三处同口径：面板上的「筛出 N 部」= 墙上的格子数 = 接口返回条数；
- 更多筛选双栏（找片 / 查库），文件级条件只认本库；
- 筛空不给空墙：放宽建议能把内容救回来；
- URL 是筛选态唯一事实源：刷新后条件还在，清空回到原墙；
- 存为合集（自动收录 / 固定这批）、合集 chip 把墙筛成它、`＝ 合集「X」` 标记
  改一条即消失、合集视图纵向网格、合集详情页规则条；
- 「我的收藏」内置合集：点一次心它就出现，空的时候不出现；
- 每一步都用 Jellyfin 协议交叉核对——网页与 Infuse 看的是同一份数据。

标 integration：要 pnpm（apps/web 已 install）与 Playwright Chromium，CI 不跑。
本地：``pytest -m integration tests/e2e/test_library_filtering_browser.py``。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
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

# —— TMDB 类型 id（语言无关，与产品里存的是同一套）——
ANIME, SCIFI, DRAMA, ACTION, DOC = 16, 878, 18, 28, 99

#: 播种谱：(标题, 类型, 地区, 上映日, 评分, 片长, 语言, 画质, HDR)
#: 刻意铺开——每个筛选维度都要有至少两个取值、且组合不平凡，否则"排除自身维度
#: 的计数"这类逻辑用一维数据是验不出来的。
CATALOG: list[tuple[str, list[int], list[str], str, float, int, str, str, str | None]] = [
    # 日本动画：三部 2010s + 一部 2000s，评分高低都有
    ("千与千寻", [ANIME, DRAMA], ["JP"], "2001-07-20", 8.5, 125, "ja", "1080p", None),
    ("你的名字", [ANIME, DRAMA], ["JP"], "2016-08-26", 8.5, 106, "ja", "2160p", "HDR10"),
    ("天气之子", [ANIME], ["JP"], "2019-07-19", 7.5, 114, "ja", "2160p", "HDR10"),
    ("辉夜姬物语", [ANIME], ["JP"], "2013-11-23", 8.0, 137, "ja", "1080p", None),
    # 韩国
    ("寄生虫", [DRAMA], ["KR"], "2019-05-30", 8.5, 132, "ko", "2160p", None),
    ("釜山行", [ACTION], ["KR"], "2016-07-20", 7.6, 118, "ko", "1080p", None),
    ("老男孩", [ACTION, DRAMA], ["KR"], "2003-11-21", 8.3, 120, "ko", "720p", None),
    # 美国科幻
    ("盗梦空间", [SCIFI, ACTION], ["US"], "2010-07-16", 8.4, 148, "en", "2160p", "HDR10"),
    ("星际穿越", [SCIFI, DRAMA], ["US"], "2014-11-07", 8.4, 169, "en", "2160p", "HDR10"),
    ("黑客帝国", [SCIFI, ACTION], ["US"], "1999-03-31", 8.2, 136, "en", "1080p", None),
    ("2001太空漫游", [SCIFI], ["US"], "1968-04-03", 8.3, 149, "en", "1080p", None),
    ("回到未来", [SCIFI], ["US"], "1985-07-03", 8.0, 116, "en", "720p", None),
    # 中国
    ("霸王别姬", [DRAMA], ["CN"], "1993-01-01", 9.2, 171, "zh", "1080p", None),
    ("流浪地球", [SCIFI, ACTION], ["CN"], "2019-02-05", 6.9, 125, "zh", "2160p", "HDR10"),
    ("让子弹飞", [ACTION, DRAMA], ["CN"], "2010-12-16", 8.8, 132, "zh", "1080p", None),
    ("我不是药神", [DRAMA], ["CN"], "2018-07-05", 8.9, 117, "zh", "1080p", None),
    # 法国
    ("天使爱美丽", [DRAMA], ["FR"], "2001-04-25", 8.3, 122, "fr", "1080p", None),
    ("这个杀手不太冷", [ACTION, DRAMA], ["FR"], "1994-09-14", 8.5, 110, "fr", "1080p", None),
    # 短片与纪录片：把片长档的两端占住
    ("短片一号", [DOC], ["US"], "2021-03-01", 6.2, 42, "en", "1080p", None),
    ("短片二号", [DOC], ["JP"], "2022-05-01", 5.9, 55, "ja", "720p", None),
    ("城市纪事", [DOC], ["CN"], "2020-09-01", 7.1, 88, "zh", "1080p", None),
    ("海洋", [DOC], ["FR"], "2009-10-01", 7.8, 104, "fr", "1080p", None),
    # 2020s 补两部，让年代档不至于只有一部
    ("沙丘", [SCIFI, DRAMA], ["US"], "2021-10-22", 7.8, 155, "en", "2160p", "HDR10"),
    ("瞬息全宇宙", [SCIFI, ACTION], ["US"], "2022-03-25", 7.8, 139, "en", "2160p", None),
]

#: 这两部走特殊库存态：一部文件失联，一部从来没刮到过档案
MISSING_TITLE = "回到未来"
UNSCRAPED_TITLE = "无名录像"


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
    root = tmp_path_factory.mktemp("filtering-e2e")
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
    # start_new_session：让每个子进程自成进程组，收尾时按**组**杀。pnpm 会再拉起
    # 一个 next dev，terminate 掉 pnpm 本身收不掉那个孙子——跑几轮就攒下一堆
    # 抢端口和 CPU 的孤儿进程，前端最后起不来（这是真踩过的坑，不是预防性代码）
    api = subprocess.Popen(  # noqa: S603
        [sys.executable, str(Path(__file__).with_name("_api_launcher.py"))],
        env=env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        cwd=str(REPO),
        start_new_session=True,
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
        start_new_session=True,
    )
    try:
        _wait_http(f"http://127.0.0.1:{api_port}/api/v1/auth/bootstrap", 90)
        _wait_http(f"http://127.0.0.1:{web_port}/login", 180)
        yield {
            "base": f"http://127.0.0.1:{web_port}",
            "metadata_dir": data / "metadata",
            "roots": roots,
            "database_url": database_url,
            "shots": root / "shots",
            "logs": (root / "api.log", root / "web.log"),
        }
    finally:
        for proc in (web, api):
            _kill_tree(proc)
        api_log.close()
        web_log.close()


def _bootstrap_and_login(page, base: str) -> None:
    """首启引导（如果还没建超管）+ 登录。两条用例都要走一遍。"""
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


def _ensure_libraries(page, stack) -> tuple[int, dict[str, int]]:
    """确保两个库与库存都在，返回 (电影库 id, 片名 → media_item_id)。

    单独跑任一条用例都得能跑通——用例之间靠"上一条建好了"隐式串起来，
    是 e2e 最常见的假绿：单跑就崩，而单跑正是排查时要做的第一件事。
    """
    base = stack["base"]
    listed = page.request.get(f"{base}/api/v1/libraries").json().get("data") or []
    by_name = {row["name"]: row["id"] for row in listed}
    if "电影库" in by_name and "剧集库" in by_name:
        # 已经播过种：把片名映射查回来
        items = page.request.get(
            f"{base}/api/v1/libraries/{by_name['电影库']}/items"
        ).json()["data"]
        ids = {row["title"]: row["media_item_id"] for row in items}
        return by_name["电影库"], ids

    library_ids = {}
    for key, name, kind in (("movies", "电影库", "movie"), ("shows", "剧集库", "tv")):
        resp = page.request.post(
            f"{base}/api/v1/libraries",
            data={"name": name, "kind": kind, "root_paths": [str(stack["roots"][key])]},
        )
        assert resp.ok, resp.text()
        library_ids[key] = resp.json()["data"]["id"]
    ids = _seed(stack["database_url"], library_ids, stack["roots"], stack["metadata_dir"])
    return library_ids["movies"], ids


def _kill_tree(proc: subprocess.Popen) -> None:
    """连同子孙一起收掉（进程组）；组没了就退回单进程。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            continue


def _chromium_kwargs() -> dict:
    for cand in _CHROMIUM_CANDIDATES:
        if cand and Path(cand).exists():
            return {"executable_path": cand}
    return {}


def _seed(
    database_url: str,
    library_ids: dict[str, int],
    roots: dict[str, Path],
    metadata_dir: Path,
) -> dict[str, int]:
    """播种电影库（含各种库存态）与剧集库，返回片名 → media_item_id。"""
    from PIL import Image

    from movieclaw_db.engine import Database
    from movieclaw_db.models import (
        FileSource,
        FileState,
        LibraryFile,
        MediaEpisode,
        MediaItem,
        MediaMetadata,
        utcnow,
    )

    ids: dict[str, int] = {}

    #: 每部片一张不同颜色的海报：合集封面是"一叠"，几张同色图叠起来与单张
    #: 无异，肉眼与截图都分辨不出堆叠有没有生效
    _HUES = ("#4a6fa5", "#a5564a", "#4aa572", "#8a4aa5", "#a59a4a", "#4a9aa5")

    def _poster(item_id: int) -> str:
        """给条目落一张真能解码的海报资产，返回它的相对路径。

        没有海报的话，合集卡片的封面就是空的——而"封面随合集列表一起下发"
        正是这轮要验的东西之一，用没有图的库验不出来。
        """
        folder = metadata_dir / "images" / str(item_id)
        folder.mkdir(parents=True, exist_ok=True)
        color = _HUES[item_id % len(_HUES)]
        Image.new("RGB", (100, 150), color).save(folder / "poster.jpg", "JPEG")
        return f"{item_id}/poster.jpg"

    async def _run() -> None:
        db = Database(database_url)
        try:
            async with db.session() as session:
                for index, row in enumerate(CATALOG):
                    title, genres, countries, released, rating, runtime, lang, res, hdr = row
                    item = MediaItem(
                        kind="movie",
                        tmdb_id=70_000 + index,
                        title=title,
                        original_title=title,
                        year=int(released[:4]),
                        aliases=[],
                    )
                    session.add(item)
                    await session.flush()
                    assert item.id
                    ids[title] = item.id
                    session.add(
                        MediaMetadata(
                            media_item_id=item.id,
                            genre_ids=genres,
                            origin_countries=countries,
                            release_date=date.fromisoformat(released),
                            vote_average=rating,
                            runtime_minutes=runtime,
                            original_language=lang,
                            poster_file=_poster(item.id),
                            # 真实刮削过的条目一定有这个时间戳；「未刮削」那部
                            # 单独播种（见下），不能靠这里留空来假装
                            scraped_at=utcnow() - timedelta(days=index),
                        )
                    )
                    path = roots["movies"] / f"{item.id}.mkv"
                    path.write_bytes(b"FAKE-MEDIA" * 64)
                    file_row = LibraryFile(
                        library_id=library_ids["movies"],
                        media_item_id=item.id,
                        season_number=0,
                        episode_number=0,
                        file_path=str(path),
                        size_bytes=path.stat().st_size + index * 1024,
                        source=FileSource.SCANNED,
                        state=FileState.IN_PLACE,
                        duration_seconds=runtime * 60,
                        resolution=res,
                        hdr=hdr,
                    )
                    if title == MISSING_TITLE:
                        # 台账还在、盘上没了——「有文件失联」那一档
                        file_row.missing_since = utcnow()
                    session.add(file_row)

                # 从来没刮到过档案的一部：没有 MediaMetadata 行
                ghost = MediaItem(
                    kind="movie",
                    tmdb_id=None,
                    # 本地条目的锚是入库时生成的稳定随机键（见 ensure_local_item），
                    # 不像 TMDB 来源那样由 tmdb_id 推出来
                    external_id="e2e-local-no-scrape",
                    title=UNSCRAPED_TITLE,
                    original_title=UNSCRAPED_TITLE,
                    source="local",
                    aliases=[],
                )
                session.add(ghost)
                await session.flush()
                assert ghost.id
                ids[UNSCRAPED_TITLE] = ghost.id
                ghost_path = roots["movies"] / f"{ghost.id}.mkv"
                ghost_path.write_bytes(b"FAKE-MEDIA" * 16)
                session.add(
                    LibraryFile(
                        library_id=library_ids["movies"],
                        media_item_id=ghost.id,
                        season_number=0,
                        episode_number=0,
                        file_path=str(ghost_path),
                        size_bytes=ghost_path.stat().st_size,
                        source=FileSource.SCANNED,
                        state=FileState.IN_PLACE,
                        resolution="1080p",
                    )
                )

                # 剧集库：一部 2160p 的日本动画剧。它与电影库同类型同地区，
                # 用来验证"筛选不跨库串台"
                show = MediaItem(
                    kind="tv",
                    tmdb_id=79_999,
                    title="某部日本动画剧",
                    original_title="Some Anime Show",
                    year=2021,
                    aliases=[],
                )
                session.add(show)
                await session.flush()
                assert show.id
                ids["某部日本动画剧"] = show.id
                session.add(
                    MediaMetadata(
                        media_item_id=show.id,
                        genre_ids=[ANIME],
                        origin_countries=["JP"],
                        release_date=date(2021, 4, 1),
                        vote_average=8.1,
                        runtime_minutes=24,
                        original_language="ja",
                        poster_file=_poster(show.id),
                        scraped_at=utcnow(),
                    )
                )
                for episode in (1, 2, 3):
                    ep_path = roots["shows"] / f"S01E0{episode}.mkv"
                    ep_path.write_bytes(b"FAKE-MEDIA" * 32)
                    session.add(
                        LibraryFile(
                            library_id=library_ids["shows"],
                            media_item_id=show.id,
                            season_number=1,
                            episode_number=episode,
                            file_path=str(ep_path),
                            size_bytes=ep_path.stat().st_size,
                            source=FileSource.SCANNED,
                            state=FileState.IN_PLACE,
                            duration_seconds=1440,
                            resolution="2160p",
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


# —— 期望值从播种谱直接算出来，不手写常量：手写的那份迟早与谱不同步 ——
def _titles(pred) -> set[str]:
    return {row[0] for row in CATALOG if pred(row)}


ANIME_TITLES = _titles(lambda r: ANIME in r[1])
JP_TITLES = _titles(lambda r: "JP" in r[2])
SCIFI_TITLES = _titles(lambda r: SCIFI in r[1])
UHD_TITLES = _titles(lambda r: r[7] == "2160p")
#: 电影库的全部条目 = 谱里的 + 那部未刮削的
MOVIE_TOTAL = len(CATALOG) + 1

#: 「筛选」那颗键：有条件时名字会变成「筛选 3」（角标进了可及名），所以按前缀认。
#: 前缀也避开了「更多筛选」与「存为合集」——子串匹配会同时命中它们。
FILTER_BTN = re.compile("^筛选")

#: 「更多筛选」里那颗 2160p 药丸。条件行里的 ✕ 叫「取消 画质 2160p」，
#: 前缀正则把两者分开。
UHD_PILL = re.compile("^2160p")


def test_filtering_and_collections_end_to_end(stack) -> None:  # noqa: PLR0915
    from playwright.sync_api import expect, sync_playwright

    from movieclaw_jellyfin.ids import collections_view_guid, item_guid

    base = stack["base"]
    shots: Path = stack["shots"]
    shots.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **_chromium_kwargs())
        # 高一点的视口：海报墙是虚拟化的，视口外的格子不在 DOM 里，
        # 矮视口下"数格子"数到的是渲染窗口而不是筛选结果
        context = browser.new_context(viewport={"width": 1440, "height": 1400}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(20_000)
        page.set_default_navigation_timeout(120_000)
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        def api(method: str, path: str, **kwargs) -> dict:
            resp = getattr(page.request, method)(f"{base}/api/v1{path}", **kwargs)
            assert resp.ok, f"{method} {path}: {resp.status} {resp.text()}"
            return resp.json()

        def shot(name: str) -> None:
            page.screenshot(path=str(shots / f"{name}.png"), full_page=False)

        # ---- 首次引导 + 登录；两个库走接口建，库存直接播种 ----
        _bootstrap_and_login(page, base)
        movie_lib, ids = _ensure_libraries(page, stack)

        # ---- Jellyfin 客户端（Infuse）登录：与网页同一个超管 ----
        jf = page.request.post(
            f"{base}/Users/AuthenticateByName",
            data={"Username": ADMIN["username"], "Pw": ADMIN["password"]},
            headers={"Authorization": _JF_AUTH},
        )
        assert jf.ok, jf.text()
        jf_key = jf.json()["AccessToken"]

        def jf_get(path: str, **params) -> dict:
            resp = page.request.get(f"{base}{path}", params={"ApiKey": jf_key, **params})
            assert resp.ok, f"{path}: {resp.status} {resp.text()}"
            return resp.json()

        def jf_view_ids() -> set[str]:
            return {v["Id"] for v in jf_get("/UserViews")["Items"]}

        # ================= 1. 静止态：一行两个控件 =================
        page.goto(f"{base}/library/{movie_lib}")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("button", name=FILTER_BTN)).to_be_visible()
        # 静止态不摆四个写着「全部」的下拉——它们只是把"什么都没选"说了四遍
        for dim in ("类型", "年代", "地区", "观看"):
            expect(page.get_by_role("button", name=dim, exact=True)).to_have_count(0)
        # 排序控件显值（看不出来的状态，控件必须把当前值显示出来）
        expect(page.get_by_role("button", name="排序")).to_contain_text("按标题")
        wall_cells = page.locator("[data-library-item-id]")
        expect(wall_cells).to_have_count(MOVIE_TOTAL)
        shot("01-resting")

        # ================= 2. 一级四维：计数与多选 =================
        page.get_by_role("button", name=FILTER_BTN).click()
        for dim in ("类型", "年代", "地区", "观看"):
            expect(page.get_by_role("button", name=dim, exact=True)).to_be_visible()

        page.get_by_role("button", name="类型", exact=True).click()
        anime_row = page.get_by_role("menuitem").filter(has_text="动画")
        expect(anime_row).to_be_visible()
        # 计数是真的：菜单里「动画」右侧那个数就是播种谱里的动画部数
        expect(anime_row).to_contain_text(str(len(ANIME_TITLES)))
        anime_row.click()
        # 多选不关菜单——关了就得重新点开才能勾第二个
        expect(page.get_by_role("menuitem").filter(has_text="科幻")).to_be_visible()
        # 勾了动画之后，别的类型不该全变 0（facet 计数排除自身维度）
        expect(page.get_by_role("menuitem").filter(has_text="科幻")).to_contain_text(
            str(len(SCIFI_TITLES))
        )
        page.keyboard.press("Escape")

        # ================= 3. 三处同口径 =================
        expect(wall_cells).to_have_count(len(ANIME_TITLES))
        expect(page.get_by_text("筛出")).to_contain_text(str(len(ANIME_TITLES)))
        served = api("get", f"/libraries/{movie_lib}/items?g={ANIME}")["data"]
        assert {row["title"] for row in served} == ANIME_TITLES
        shot("02-genre-filtered")

        # ================= 4. 维内「或」、维间「且」 =================
        page.get_by_role("button", name="类型", exact=True).click()
        page.get_by_role("menuitem").filter(has_text="科幻").click()
        page.keyboard.press("Escape")
        expect(page.get_by_text("或").first).to_be_visible()
        expect(wall_cells).to_have_count(len(ANIME_TITLES | SCIFI_TITLES))

        page.get_by_role("button", name="地区", exact=True).click()
        page.get_by_role("menuitem").filter(has_text="日本").click()
        page.keyboard.press("Escape")
        expect(page.get_by_text("且").first).to_be_visible()
        expect(wall_cells).to_have_count(len((ANIME_TITLES | SCIFI_TITLES) & JP_TITLES))
        # 条件行里印的必须是中文名。本库的日本片里没有科幻，所以「科幻」这一档
        # 在类型 facet 里被收窄成了 0 部——它仍然要留在候选里，否则用户既取消
        # 不掉它，界面上还会冒出一个裸的 TMDB id「878」
        expect(page.get_by_text("科幻").first).to_be_visible()
        assert "878" not in page.locator("main").inner_text(), "条件行不该印裸 id"
        shot("03-or-and")

        # ================= 5. URL 是筛选态唯一事实源 =================
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(page.url).query)
        assert set(query["g"][0].split(",")) == {str(ANIME), str(SCIFI)}, page.url
        assert query["c"] == ["JP"], page.url
        page.reload()
        page.wait_for_load_state("networkidle")
        # 刷新后条件还在（分享出去的链接一进来就是筛好的）
        expect(wall_cells).to_have_count(len((ANIME_TITLES | SCIFI_TITLES) & JP_TITLES))
        expect(page.get_by_text("且").first).to_be_visible()

        # ================= 6. 清空回到原墙 =================
        page.get_by_role("button", name="清空").click()
        expect(wall_cells).to_have_count(MOVIE_TOTAL)
        assert "g=" not in page.url and "c=" not in page.url, page.url

        # ================= 7. 更多筛选：文件级条件只认本库 =================
        page.get_by_role("button", name=FILTER_BTN).click()
        page.get_by_role("button", name="更多筛选").click()
        expect(page.get_by_text("找片")).to_be_visible()
        expect(page.get_by_text("查库")).to_be_visible()
        page.get_by_role("button", name=UHD_PILL).click()
        expect(wall_cells).to_have_count(len(UHD_TITLES))
        # 二级维度也要在条件行里画出来：只画一级四维的话，用 4K 筛完再收起
        # 面板，界面上只剩一个「筛选 1」的角标，筛的是什么、怎么取消都看不见
        condition_row = page.get_by_text("画质").first
        expect(condition_row).to_be_visible()
        page.get_by_role("button", name=FILTER_BTN).click()  # 收起面板
        expect(page.get_by_role("button", name="更多筛选")).to_have_count(0)
        expect(page.get_by_text("画质").first).to_be_visible()
        page.get_by_role("button", name=FILTER_BTN).click()  # 再打开
        # 剧集库里那部 2160p 的动画剧不该混进电影库的墙
        assert "某部日本动画剧" not in page.content()
        shot("04-more-filters")

        # ================= 7b. 查库：库存状态与观看状态 =================
        # 「更多筛选」那一层刚才随面板一起收过，重新点开再换条件
        if page.get_by_role("button", name=UHD_PILL).count() == 0:
            page.get_by_role("button", name="更多筛选").click()
        page.get_by_role("button", name=UHD_PILL).click()
        # 不在这里数总数：面板展开后墙被推下去，虚拟化只渲染窗口内的格子
        # 「文件失联」与「没刮到档案」问的是库的健康，不是作品长什么样，
        # 所以它们在右栏（查库），并且各有语义色
        page.get_by_role("button", name=re.compile("^文件失联")).click()
        expect(wall_cells).to_have_count(1)
        assert MISSING_TITLE in page.content()
        page.get_by_role("button", name=re.compile("^文件失联")).click()
        page.get_by_role("button", name=re.compile("^没刮到档案")).click()
        expect(wall_cells).to_have_count(1)
        assert UNSCRAPED_TITLE in page.content()
        page.get_by_role("button", name="清空").click()

        # 观看状态是个划分：未看 / 在看 / 已看完三档相加恰好是全库
        api("post", "/playback/marks", data={"media_item_id": ids["寄生虫"], "played": True})
        facets = api("get", f"/libraries/{movie_lib}/facets")["data"]
        watch = {row["value"]: row["count"] for row in facets["watch"]}
        assert watch["played"] == 1
        assert watch["unwatched"] + watch["watching"] + watch["played"] == MOVIE_TOTAL, watch
        page.goto(f"{base}/library/{movie_lib}?w=played")
        page.wait_for_load_state("networkidle")
        expect(wall_cells).to_have_count(1)
        assert "寄生虫" in page.content()

        # 分享进来的链接可能带二级条件，而那时面板是关的：条件行仍然要印出
        # 可读的档名（"gt120" → "> 120′"），不能把裸值摆在界面上
        page.goto(f"{base}/library/{movie_lib}?rt=gt120")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_text("片长").first).to_be_visible()
        assert "gt120" not in page.locator("main").inner_text(), "条件行不该印裸档名"
        expect(page.get_by_text("> 120′").first).to_be_visible()

        # ================= 8. 筛空不给空墙，放宽建议能救回来 =================
        # 注意：**点不出**空墙——计数为 0 的选项在下拉与药丸里都是禁用的
        #（"永不空货架"的第一道闸）。空墙只会从别处来：分享出去的链接、
        # 存下来的合集、或者数据变了。所以这里直接走链接，那才是真实场景。
        page.goto(f"{base}/library/{movie_lib}?g={DOC}&c=KR")
        page.wait_for_load_state("networkidle")
        expect(wall_cells).to_have_count(0)
        expect(page.get_by_text("没有同时满足")).to_be_visible()
        suggestion = page.locator("button").filter(has_text="去掉").first
        expect(suggestion).to_be_visible()
        suggestion.click()
        # 建议只列命中数 > 0 的剔除项，所以点完一定有内容
        expect(wall_cells).not_to_have_count(0)
        shot("05-relax")

        # 只用二级维度也筛得空（4K + 评分≥9 一部都没有）。这一条压的是
        # "放宽建议要覆盖二级维度"：只看一级四维的话这里一条建议都给不出，
        # 界面却会说"去掉任意一条也救不回来"——而去掉评分明明就能救回来
        page.goto(f"{base}/library/{movie_lib}?res=2160p&rating_gte=9")
        page.wait_for_load_state("networkidle")
        expect(wall_cells).to_have_count(0)
        expect(page.get_by_text("没有同时满足这 2 个条件的作品")).to_be_visible()
        rating_relax = page.locator("button").filter(has_text="评分").first
        expect(rating_relax).to_be_visible()
        rating_relax.click()
        expect(wall_cells).to_have_count(len(UHD_TITLES))
        shot("05b-relax-secondary")

        page.get_by_role("button", name="清空").click()
        expect(wall_cells).to_have_count(MOVIE_TOTAL)

        # ================= 9. 存为合集：用后果的语言问，不写 smart/manual =====
        page.get_by_role("button", name=FILTER_BTN).click()
        page.get_by_role("button", name="类型", exact=True).click()
        page.get_by_role("menuitem").filter(has_text="动画").click()
        page.keyboard.press("Escape")
        page.get_by_role("button", name="地区", exact=True).click()
        page.get_by_role("menuitem").filter(has_text="日本").click()
        page.keyboard.press("Escape")
        expect(wall_cells).to_have_count(len(ANIME_TITLES & JP_TITLES))

        page.get_by_role("button", name="存为合集").click()
        dialog = page.get_by_role("dialog", name="存为合集")
        expect(dialog).to_be_visible()
        # 建议名把条件本身念出来，比「新建合集 3」有用
        name_input = dialog.locator("input[type=text], input:not([type])").first
        expect(name_input).to_have_value("动画 · 日本")
        hit = len(ANIME_TITLES & JP_TITLES)
        expect(dialog.get_by_text(f"当前条件命中 {hit} 部")).to_be_visible()
        expect(dialog.get_by_text("自动收录")).to_be_visible()
        expect(dialog.get_by_text("以后新入库的片")).to_be_visible()
        name_input.fill("日本动画")
        dialog.get_by_role("button", name="存为合集").click()
        expect(dialog).to_have_count(0)
        shot("06-saved-collection")

        # 合集立刻出现在 chip 行上，数量与墙一致
        collection_chip = page.locator("button").filter(has_text="日本动画").first
        expect(collection_chip).to_be_visible()
        expect(collection_chip).to_contain_text(str(len(ANIME_TITLES & JP_TITLES)))
        # 条件正好等于这个合集，所以标记直接就在（推导出来的，不是存的）
        expect(page.get_by_text("＝ 合集")).to_be_visible()

        # ================= 10. 改一条条件，合集标记自然消失 =================
        # 加一个年代（在「动画 + 日本」里它的计数不为 0，所以点得动；换成
        # 「韩国」是点不动的——那一档在当前条件下就是 0，控件本来就禁用）
        page.get_by_role("button", name="年代", exact=True).click()
        page.get_by_role("menuitem").filter(has_text="2010s").click()
        page.keyboard.press("Escape")
        expect(page.get_by_text("＝ 合集")).to_have_count(0)
        # 再点回去又相等了——标记是从条件推导出来的，不是谁记着去清的
        page.get_by_role("button", name="年代", exact=True).click()
        page.get_by_role("menuitem").filter(has_text="2010s").click()
        page.keyboard.press("Escape")
        expect(page.get_by_text("＝ 合集")).to_be_visible()

        # ================= 11. 合集 chip 是「把墙筛成它」，不是跳页 =========
        page.get_by_role("button", name="清空").click()
        expect(wall_cells).to_have_count(MOVIE_TOTAL)
        collection_chip.click()
        expect(wall_cells).to_have_count(len(ANIME_TITLES & JP_TITLES))
        assert "/library/" in page.url and "/c/" not in page.url, "点 chip 不该跳页"
        # 再点一次退回全库
        collection_chip.click()
        expect(wall_cells).to_have_count(MOVIE_TOTAL)

        # ================= 12. 合集视图：纵向网格 + 详情页 =================
        page.get_by_role("tab", name="合集").click()
        card = page.locator(f'a[href^="/library/{movie_lib}/c/"]').first
        expect(card).to_be_visible()
        expect(card).to_contain_text("日本动画")
        expect(card).to_contain_text("自动收录")
        # 封面**真的解码出来了**，不只是 <img> 在 DOM 里。第一版这里是通过的，
        # 但截图上卡片是空的——自己写的 <img loading="lazy"> 在
        # content-visibility 跳过态的子树里永远不发请求（见 poster-image.tsx）
        cover_img = card.locator("img").first
        expect(cover_img).to_be_visible()
        page.wait_for_function(
            "el => el.complete && el.naturalWidth > 0",
            arg=cover_img.element_handle(),
            timeout=10_000,
        )
        shot("07-collections-grid")
        card.click()
        page.wait_for_url(lambda u: "/c/" in u)
        page.wait_for_load_state("networkidle")
        # 规则条：把"它为什么收了这些片"直接画出来。存的是 TMDB id 与国家码，
        # 界面上要看到的是中文名，而且一刻都不该冒出裸值
        expect(page.get_by_text("自动收录").first).to_be_visible()
        expect(page.get_by_text("类型").first).to_be_visible()
        expect(page.get_by_text("动画").first).to_be_visible()
        assert "类型 16" not in page.locator("main").inner_text()
        expect(page.locator("[data-library-item-id]")).to_have_count(len(ANIME_TITLES & JP_TITLES))
        # 管理动作收在 ⋯ 里：顶栏那几个位子是 36px 圆钮，塞中文标签会挤成竖排
        page.get_by_role("button", name="更多操作").click()
        expect(page.get_by_role("menuitem", name="改名")).to_be_visible()
        expect(page.get_by_role("menuitem", name="删除合集")).to_be_visible()
        page.keyboard.press("Escape")
        shot("08-collection-detail")

        # ================= 13. 合集与海报墙返回同一批 id =================
        collections = api("get", f"/collections?library_id={movie_lib}")["data"]
        smart = next(row for row in collections if row["name"] == "日本动画")
        from_collection = {
            row["title"] for row in api("get", f"/collections/{smart['id']}/items")["data"]
        }
        from_wall = {
            row["title"]
            for row in api("get", f"/libraries/{movie_lib}/items?g={ANIME}&c=JP")["data"]
        }
        assert from_collection == from_wall == (ANIME_TITLES & JP_TITLES)
        # 卡片封面由服务端随列表给出，客户端不为每个合集再请求一次成员
        assert smart["covers"], "有成员就该有封面"

        # ================= 14. 固定这批：服务端定格，客户端不回传 id =========
        page.goto(f"{base}/library/{movie_lib}?g={SCIFI}")
        page.wait_for_load_state("networkidle")
        expect(wall_cells).to_have_count(len(SCIFI_TITLES))
        page.get_by_role("button", name="存为合集").click()
        dialog = page.get_by_role("dialog", name="存为合集")
        dialog.locator("input[type=text], input:not([type])").first.fill("就这批科幻")
        dialog.get_by_text(f"固定现在这 {len(SCIFI_TITLES)} 部").click()
        dialog.get_by_role("button", name="存为合集").click()
        expect(dialog).to_have_count(0)
        fixed = next(
            row
            for row in api("get", f"/collections?library_id={movie_lib}")["data"]
            if row["name"] == "就这批科幻"
        )
        assert fixed["rule_driven"] is False, "固定这批之后不该再自动收录"
        assert fixed["rules"] == []
        assert fixed["item_count"] == len(SCIFI_TITLES)

        # ================= 15. 「我的收藏」：空的时候不出现，点一次心就出现 ==
        before = {row["name"] for row in api("get", f"/collections?library_id={movie_lib}")["data"]}
        assert "我的收藏" not in before, "一个收藏都没有时，空的内置合集不该列出来"
        api(
            "post",
            "/playback/marks",
            data={"media_item_id": ids["千与千寻"], "favorite": True},
        )
        after = {row["name"] for row in api("get", f"/collections?library_id={movie_lib}")["data"]}
        assert "我的收藏" in after, "收藏之后内置合集就该出现——它本来就是一个合集"
        builtin = next(
            row
            for row in api("get", f"/collections?library_id={movie_lib}")["data"]
            if row["name"] == "我的收藏"
        )
        assert builtin["editable"] is False and builtin["item_count"] == 1

        # ================= 16. Jellyfin：同一份合集下发到电视端 =================
        assert collections_view_guid() in jf_view_ids(), "有可见合集就该有「合集」视图"
        boxsets = jf_get("/Items", ParentId=collections_view_guid())["Items"]
        by_name = {row["Name"]: row for row in boxsets}
        assert {"日本动画", "就这批科幻", "我的收藏"} <= set(by_name), by_name.keys()
        assert by_name["日本动画"]["Type"] == "BoxSet"
        assert by_name["日本动画"]["ChildCount"] == len(ANIME_TITLES & JP_TITLES)
        # 点进 BoxSet 拿到的是成员作品，与网页同一批
        members = jf_get("/Items", ParentId=by_name["日本动画"]["Id"])["Items"]
        assert {row["Name"] for row in members} == ANIME_TITLES & JP_TITLES
        assert {row["Type"] for row in members} == {"Movie"}
        # 内置合集在电视端同样是一条 BoxSet，成员就是刚点的那部
        fav_members = jf_get("/Items", ParentId=by_name["我的收藏"]["Id"])["Items"]
        assert [row["Name"] for row in fav_members] == ["千与千寻"]
        assert fav_members[0]["Id"] == item_guid(ids["千与千寻"])
        # 合集封面回落到首位成员的海报（这里没有海报资产，只要求不 500）
        cover = page.request.get(
            f"{base}/Items/{by_name['日本动画']['Id']}/Images/Primary", params={"ApiKey": jf_key}
        )
        assert cover.status in (200, 404), cover.status

        # ================= 17. 删合集：删的是视图，作品一部不少 =================
        api("delete", f"/collections/{fixed['id']}")
        remaining = {
            row["name"] for row in api("get", f"/collections?library_id={movie_lib}")["data"]
        }
        assert "就这批科幻" not in remaining
        assert len(api("get", f"/libraries/{movie_lib}/items")["data"]) == MOVIE_TOTAL

        # ================= 18. 四档新排序 =================
        by_rating = [
            row["title"] for row in api("get", f"/libraries/{movie_lib}/items?sort=rating")["data"]
        ]
        assert by_rating[:3] == [r[0] for r in sorted(CATALOG, key=lambda r: -r[4])[:3]]
        # 片长是**升序**：问「有没有短点的」比问「哪部最长」常见得多
        by_runtime = [
            row["title"] for row in api("get", f"/libraries/{movie_lib}/items?sort=runtime")["data"]
        ]
        assert by_runtime[:3] == [r[0] for r in sorted(CATALOG, key=lambda r: r[5])[:3]]
        # 体积降序：「谁在占盘」是这一档要回答的问题
        by_size = api("get", f"/libraries/{movie_lib}/items?sort=size")["data"]
        sizes = [row["total_size_bytes"] for row in by_size]
        assert sizes == sorted(sizes, reverse=True)
        # 最近观看：刚标已看完的那部排在最前
        by_played = api("get", f"/libraries/{movie_lib}/items?sort=last_played")["data"]
        assert by_played[0]["title"] == "寄生虫"

        assert not page_errors, f"页面报错：{page_errors[:3]}"
        context.close()
        browser.close()


def test_mobile_layout_end_to_end(stack) -> None:  # noqa: PLR0915
    """窄屏（390px）上把同一条路再走一遍，并逐屏留证。

    移动端不是"桌面端缩小"：一行放不下四个下拉、弹窗要贴着拇指、墙是虚拟化的。
    这条用例覆盖静止栏、一级四维横滚、条件行、底部抽屉、合集 chip、合集网格与
    详情页、存为合集弹窗——每一步都截图，光靠断言看不出"画出来是歪的"。
    """
    from playwright.sync_api import expect, sync_playwright

    base = stack["base"]
    shots: Path = stack["shots"]
    shots.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **_chromium_kwargs())
        # iPhone 一档的视口（< 768px 触发移动端版式）
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            locale="zh-CN",
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()
        page.set_default_timeout(20_000)
        page.set_default_navigation_timeout(120_000)
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        def shot(name: str) -> None:
            page.screenshot(path=str(shots / f"m{name}.png"))

        _bootstrap_and_login(page, base)
        movie_lib, _ids = _ensure_libraries(page, stack)
        # 单独跑这条用例时上一条的合集不在，自己补一个——用例不该依赖执行顺序
        existing = page.request.get(
            f"{base}/api/v1/collections?library_id={movie_lib}"
        ).json()["data"]
        if not any(row["name"] == "日本动画" for row in existing):
            page.request.post(
                f"{base}/api/v1/collections",
                data={
                    "name": "日本动画",
                    "library_id": movie_lib,
                    "rules": [
                        {"field": "genres", "op": "any_of", "values": [ANIME]},
                        {"field": "origin_countries", "op": "any_of", "values": ["JP"]},
                    ],
                },
            )

        # ================= 1. 静止态：窄屏一行也只有两个控件 =================
        page.goto(f"{base}/library/{movie_lib}")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("button", name=FILTER_BTN)).to_be_visible()
        expect(page.get_by_role("button", name="排序")).to_be_visible()
        shot("01-resting")
        # 横向不出滚动条：任何一处溢出都会让整页能左右拖，观感立刻塌
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1"
        ), "页面出现了横向溢出"

        # ================= 2. 一级四维横滚，不换行 =================
        page.get_by_role("button", name=FILTER_BTN).click()
        dims = page.get_by_role("button", name="类型", exact=True).locator("xpath=..")
        assert "overflow-x-auto" in (dims.get_attribute("class") or "")
        # 真的在横滚（换行的话 scrollWidth 不会超出 clientWidth）
        assert dims.evaluate("el => el.scrollWidth > el.clientWidth"), "四维没有横滚，说明换行了"
        # 「更多筛选」不跟着滚出屏幕：它是通往二级的门，滚没了就等于不存在
        more_btn = page.get_by_role("button", name="更多筛选")
        expect(more_btn).to_be_in_viewport()
        assert more_btn.evaluate(
            "el => !el.closest('[class*=overflow-x-auto]')"
        ), "「更多筛选」还在横滚区里，会跟着滚走"
        shot("02-dims-scroller")

        # ================= 3. 下拉菜单在窄屏不出界 =================
        page.get_by_role("button", name="类型", exact=True).click()
        menu = page.get_by_role("menu")
        expect(menu).to_be_visible()
        menu_box = menu.bounding_box()
        assert menu_box and menu_box["x"] >= 0 and menu_box["x"] + menu_box["width"] <= 390, (
            f"下拉菜单超出屏幕：{menu_box}"
        )
        shot("03-menu")
        page.get_by_role("menuitem").filter(has_text="动画").click()
        page.keyboard.press("Escape")

        # ================= 4. 条件行在窄屏 =================
        expect(page.get_by_text("筛出")).to_contain_text(str(len(ANIME_TITLES)))
        shot("04-condition-row")
        # 改完条件之后，条件行必须还在视口里、且没钻到浮在顶部的导航键底下——
        # 那排键是无背景的浮层，控件滚到它下面就成了一团糊字
        # 整条筛选条（合集 chip、四维、条件行）都要在导航键下沿之外
        for label in ("日本动画", "类型", "清空"):
            target = page.get_by_text(label).first
            expect(target).to_be_in_viewport()
            assert target.bounding_box()["y"] > 52, f"「{label}」钻到顶部导航键底下了"
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1"
        ), "有条件之后出现了横向溢出"

        # ================= 5. 底部抽屉：留得住墙、勾选立即生效 =================
        page.get_by_role("button", name="清空").click()
        page.get_by_role("button", name="更多筛选").click()
        sheet = page.get_by_role("button", name="收起更多筛选")
        expect(sheet).to_be_visible()
        shot("05-sheet")
        # 打开那一刻不能是「找片 / 查库」两个孤零零的空标题：档位还没数完就先
        # 说一句，否则内容随后弹进来会把抽屉在拇指底下撑高一截
        opened = page.locator("body").inner_text()
        assert ("正在数各档位" in opened) or ("≥ 8" in opened), opened[-300:]
        # 抽屉不占满屏：上方那截墙还看得见（幕只压了很淡的一层）
        box = sheet.bounding_box()
        assert box and box["height"] > 100, f"上方留白太少，抽屉几乎全屏了：{box}"
        expect(page.locator("[data-library-item-id]").first).to_be_in_viewport()

        # 每次勾选立即生效，主按钮上的数字实时跳——不做「确定」式提交
        view_all = page.locator("button").filter(has_text="查看")
        expect(view_all).to_contain_text(str(MOVIE_TOTAL))
        page.get_by_role("button", name=UHD_PILL).click()
        expect(view_all).to_contain_text(str(len(UHD_TITLES)))
        expect(page.locator("[data-library-item-id]")).to_have_count(len(UHD_TITLES))
        shot("06-sheet-applied")

        # 那颗键不是「提交」，只是把抽屉收起来
        view_all.click()
        expect(sheet).to_have_count(0)
        expect(page.locator("[data-library-item-id]")).to_have_count(len(UHD_TITLES))

        page.get_by_role("button", name="清空").click()
        # 窄屏的墙是虚拟化的，视口外的格子不在 DOM 里——所以这里不数总数
        #（数到的是渲染窗口），只确认它比刚才那 8 部多，且条件真的清掉了
        expect(page.locator("[data-library-item-id]").nth(len(UHD_TITLES))).to_be_attached()
        assert "res=" not in page.url, page.url
        expect(page.get_by_role("button", name=FILTER_BTN)).not_to_contain_text("1")

        # ================= 6. 合集 chip 行与「存为合集」弹窗 =================
        chip = page.locator("button").filter(has_text="日本动画").first
        expect(chip).to_be_visible()
        shot("07-collection-chip")
        chip.click()
        expect(page.locator("[data-library-item-id]")).to_have_count(len(ANIME_TITLES & JP_TITLES))
        expect(page.get_by_text("＝ 合集")).to_be_visible()
        chip.click()

        page.goto(f"{base}/library/{movie_lib}?g={SCIFI}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="存为合集").click()
        dialog = page.get_by_role("dialog", name="存为合集")
        expect(dialog).to_be_visible()
        shot("08-save-dialog")
        # 建议名绝不能是裸的 TMDB id：手一快就存下一个叫「878」的合集
        name_input = dialog.locator("input[type=text], input:not([type])").first
        assert name_input.input_value() != str(SCIFI), "建议名把裸 id 填进了输入框"
        expect(name_input).to_have_value("科幻")
        # 移动端弹窗贴住屏幕下沿（Modal 的 bottom sheet 形态），按钮在拇指够得着的地方
        dialog_box = dialog.bounding_box()
        assert dialog_box and dialog_box["y"] + dialog_box["height"] >= 800, (
            f"弹窗没有贴住屏幕下沿：{dialog_box}"
        )
        page.keyboard.press("Escape")

        # ================= 7. 合集网格与详情页 =================
        # 窄屏的视图切换挪到了库名那一行的右端：正文里不再单占一行，也没有去
        # 挤顶栏——顶栏那一行在 390px 上只剩 90px 给吸顶标题，塞个文字切换就是
        # 负数。这里两头都验：切换在库名同一行，且吸顶标题没被挤没
        page.goto(f"{base}/library/{movie_lib}")
        page.wait_for_load_state("networkidle")
        tabs = page.get_by_role("tab", name="合集")
        expect(tabs).to_be_visible()
        title_row = page.get_by_role("heading", name="电影库").bounding_box()
        tabs_box = tabs.bounding_box()
        assert abs(tabs_box["y"] - title_row["y"]) < 24, "视图切换没和库名同一行"
        assert tabs_box["x"] + tabs_box["width"] <= 390, "切换超出屏幕右边"
        # 滚的是内层容器（全站是「外壳固定 + 内层 overflow-y-auto」），不是窗口：
        # 对着窗口滚，吸顶标题的 --nav-reveal 一直是 0，截图上永远看不到标题，
        # 很容易被误读成"标题被挤没了"
        page.evaluate(
            "() => { const el = document.querySelector('[data-scroll-root]')"
            " ?? [...document.querySelectorAll('div')].find("
            "   d => d.scrollHeight > d.clientHeight + 200"
            "     && getComputedStyle(d).overflowY === 'auto');"
            "  if (el) el.scrollTop = 600; }"
        )
        page.wait_for_timeout(400)
        shot("12-view-switch-on-title-row")
        # 顶栏那一行必须给吸顶标题留出地方。实测：现在标题有 50.5px（放得下
        # 「电影库」），而往操作区再塞一个「作品|合集」那么宽的控件，标题会被
        # 挤成 0——这正是视图切换没有挂进顶栏、而是挂在库名那一行右端的原因。
        # 这条断言守的是"以后别再往这一行加东西"
        sticky = page.locator('[class*="sticky"] span[aria-hidden="true"]').first
        assert sticky.evaluate("el => el.getBoundingClientRect().width") > 40, (
            "顶栏塞太满，吸顶标题没地方了"
        )

        # 合集视图里不该有图床键：那面墙根本不在，点了什么也不会发生
        page.goto(f"{base}/library/{movie_lib}?view=collections")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("button", name="图床浏览")).to_have_count(0)

        page.goto(f"{base}/library/{movie_lib}?view=collections")
        page.wait_for_load_state("networkidle")
        # 明确点「日本动画」那张，不要用 .first：内置的「我的收藏」position 是 -1，
        # 排在用户合集前面，跑在桌面用例之后时 .first 会是它——用例之间靠顺序
        # 隐式串起来，正是 e2e 最常见的假绿
        card = page.locator(f'a[href^="/library/{movie_lib}/c/"]').filter(has_text="日本动画")
        expect(card).to_be_visible()
        cover = card.locator("img").first
        page.wait_for_function(
            "el => el.complete && el.naturalWidth > 0",
            arg=cover.element_handle(),
            timeout=10_000,
        )
        shot("09-collections-grid")
        card.click()
        page.wait_for_url(lambda u: "/c/" in u)
        page.wait_for_load_state("networkidle")
        expect(page.get_by_text("自动收录").first).to_be_visible()
        shot("10-collection-detail")
        # 规则条里存的是 TMDB id 与国家码，界面上一刻都不该冒出「16」「JP」——
        # 展示名还在路上时给省略号占位，到了再补
        detail_text = page.locator("main").inner_text()
        assert "类型 16" not in detail_text and "地区 JP" not in detail_text, detail_text[:200]
        # 而且省略号只是过渡态：展示名到了要真的补上，否则"不印裸值"退化成
        # "永远印省略号"，这条断言照样是绿的
        expect(page.get_by_text("动画").first).to_be_visible()
        expect(page.get_by_text("日本").first).to_be_visible()
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1"
        ), "合集详情页出现了横向溢出"
        # 管理动作在窄屏同样收在 ⋯ 里
        page.get_by_role("button", name="更多操作").click()
        expect(page.get_by_role("menuitem", name="改名")).to_be_visible()
        shot("11-detail-menu")

        assert not page_errors, f"页面报错：{page_errors[:3]}"
        context.close()
        browser.close()
