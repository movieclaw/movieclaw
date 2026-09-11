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
#: 系列合集的播种谱：两部同系列的片（成员 < 2 不下发，所以必须是两部）。
#: 键是片名，值是 (series_key, series_name)
SERIES_KEY = "tmdb:9999"
SERIES_NAME = "新海诚系列"
SERIES = {
    "你的名字": (SERIES_KEY, SERIES_NAME),
    "天气之子": (SERIES_KEY, SERIES_NAME),
}
#: 系列里库存没有的那一部：详情页要把它压暗画进墙里，悬停给订阅入口
MISSING_PART_TITLE = "铃芽之旅"

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
    from movieclaw_db.repositories.library_repo import LibraryRepository

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
                            # 作品系列：只给谱里点了名的那几部挂 key，其余写空串
                            # （"查过了，没有系列"，与 NULL 的"还没查过"分开）
                            series_key=SERIES.get(title, ("", None))[0],
                            series_name=SERIES.get(title, ("", None))[1],
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
                # 刷一次库存快照：stats 是写路径维护的预计算值（扫描/入库时刷新），
                # 列表页与库头部直接读它。直接往表里写行不会碰它，不刷的话
                # /library 上会写着「0 部电影」——那是夹具的账，不是产品的
                await LibraryRepository(session).refresh_stats(list(library_ids.values()))
                await session.commit()

                # 系列合集：真实链路里这一步发生在扫描收尾，夹具直接播种台账
                # 所以要自己调一次。之后合集页上就该多出一个「系列」分组
                from movieclaw_api.services.library.series import (
                    ensure_series_collections_for_library,
                    series_builtin,
                )

                await ensure_series_collections_for_library(session, library_ids["movies"])
                await session.commit()
                # 缺片补齐的上游档案（真实链路是打开详情页时懒加载 TMDB）：
                # e2e 不联网，直接把快照写进去，验的是"有了 parts 之后界面怎么显示"
                from sqlmodel import select as _select

                from movieclaw_db.models import Collection

                row = (
                    await session.execute(
                        _select(Collection).where(
                            Collection.builtin == series_builtin(SERIES_KEY, library_ids["movies"])
                        )
                    )
                ).scalar_one()
                # 两部库里有（tmdb_id 必须与播种的对得上，否则"已有"认不出来），
                # 第三部库里没有——那正是要压暗画进墙里、给订阅入口的那一格
                tmdb_of = {row[0]: 70_000 + i for i, row in enumerate(CATALOG)}
                row.series_parts = [
                    {
                        "tmdb_id": tmdb_of["你的名字"],
                        "title": "你的名字",
                        "release_date": "2016-08-26",
                        "poster_path": None,
                    },
                    {
                        "tmdb_id": tmdb_of["天气之子"],
                        "title": "天气之子",
                        "release_date": "2019-07-19",
                        "poster_path": None,
                    },
                    {
                        "tmdb_id": 9_999_101,
                        "title": MISSING_PART_TITLE,
                        "release_date": "2022-11-11",
                        "poster_path": None,
                    },
                ]
                session.add(row)
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

    from movieclaw_jellyfin.ids import collections_view_guid, item_guid, library_guid

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

        # ================= 19. 图床入口收进 ⋯：进得去也回得来 =================
        page.goto(f"{base}/library/{movie_lib}")
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("button", name="图床浏览").count() == 0, "图床键还在顶栏"
        page.get_by_role("button", name="更多操作").click()
        page.get_by_role("menuitem", name="图床浏览").click()
        # 进了图床模式：菜单项翻成「回到海报墙」，点它能回去
        page.get_by_role("button", name="更多操作").click()
        back_item = page.get_by_role("menuitem", name="回到海报墙")
        expect(back_item).to_be_visible()
        back_item.click()
        expect(page.locator("[data-library-item-id]").first).to_be_visible()

        # ================= 20. 系列合集：分组、缺片、隐藏与回头路 ===========
        page.goto(f"{base}/library/{movie_lib}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("tab", name="合集").click()
        # 分组：用户自己存的在前，自动生成的系列在后。一个 300 部的库可能有
        # 40+ 个系列，平铺的话用户存的那几个就没了
        expect(page.get_by_text("我的合集", exact=True)).to_be_visible()
        expect(page.get_by_text("系列", exact=True).first).to_be_visible()
        series_card = page.locator(f'a[href^="/library/{movie_lib}/c/"]').filter(
            has_text=SERIES_NAME
        )
        expect(series_card).to_have_count(1)
        # 卡片副行写「系列」，**不写「缺 1 部」**——一屏几十个红角标是压迫感
        expect(series_card).to_contain_text("· 系列")
        assert "缺" not in series_card.inner_text(), "缺片信息不该上卡片"
        # 等封面真的解码出来再截图：截一张还没加载完的图，等于给自己看假证据
        series_cover = series_card.locator("img").first
        page.wait_for_function(
            "el => el.complete && el.naturalWidth > 0",
            arg=series_cover.element_handle(),
            timeout=10_000,
        )
        shot("20-series-group")

        series_id = next(
            row["id"]
            for row in api("get", f"/collections?library_id={movie_lib}")["data"]
            if row["kind"] == "series"
        )
        series_card.click()
        page.wait_for_url(lambda u: "/c/" in u)
        page.wait_for_load_state("networkidle")
        # 规则条：系列的规则是 series_key，翻不成"类型/年代"那套话，
        # 硬套会显示"收录本库全部作品"——一句彻头彻尾的假话
        expect(page.get_by_text("作品系列 ·").first).to_be_visible()
        # 缺片补齐：这才是系列合集真正的价值（只归类的话装个 Emby 也有）。
        # 缺的那部不另起一块，按上映顺序画在墙上：海报压暗、副行写「未入库」，
        # 悬停给订阅入口（走全站那一份订阅弹窗，不是就地一键订上）
        expect(page.get_by_text("已有 2 / 共 3 部")).to_be_visible()
        missing_cell = page.get_by_test_id("series-missing-part").filter(
            has_text=MISSING_PART_TITLE
        )
        expect(missing_cell).to_have_count(1)
        expect(missing_cell).to_contain_text("未入库")
        # 《铃芽之旅》(2022) 最晚上映，排在墙的最后一格
        expect(page.get_by_test_id("series-wall").locator(":scope > *").last).to_contain_text(
            MISSING_PART_TITLE
        )
        missing_cell.hover()
        expect(page.get_by_role("button", name=f"订阅影片《{MISSING_PART_TITLE}》")).to_be_visible()
        # 系列按**上映正序**排：先看《你的名字》(2016) 再看《天气之子》(2019)。
        # 墙上默认的 release_date 是倒序（新的在前，浏览的语义），系列不能跟着
        # ——规则条上写着"按上映顺序排列"，那句话必须是真的
        series_titles = [
            row["title"]
            for row in api("get", f"/collections/{series_id}/items")["data"]
        ]
        assert series_titles == ["你的名字", "天气之子"], series_titles
        shot("21-series-missing-parts")

        # 隐藏：自动生成的合集删不掉（下次扫描又长回来），那颗按钮落成墓碑
        page.get_by_role("button", name="更多操作").click()
        expect(page.get_by_role("menuitem", name="删除合集")).to_have_count(0)
        page.get_by_role("menuitem", name="隐藏这个合集").click()
        page.get_by_role("button", name="隐藏").click()
        page.wait_for_url(lambda u: "/c/" not in u)
        page.wait_for_load_state("networkidle")
        page.get_by_role("tab", name="合集").click()
        expect(
            page.locator(f'a[href^="/library/{movie_lib}/c/"]').filter(has_text=SERIES_NAME)
        ).to_have_count(0)

        # 回头路：藏得回来才叫隐藏，藏不回来那是删除
        page.get_by_role("button", name="更多操作").click()
        page.get_by_role("menuitem", name="显示已隐藏的合集").click()
        hidden_card = page.locator(f'a[href^="/library/{movie_lib}/c/"]').filter(
            has_text=SERIES_NAME
        )
        expect(hidden_card).to_have_count(1)
        expect(hidden_card).to_contain_text("已隐藏")
        shot("22-hidden-collection-found")
        hidden_card.click()
        page.wait_for_url(lambda u: "/c/" in u)
        page.get_by_role("button", name="更多操作").click()
        page.get_by_role("menuitem", name="恢复显示").click()
        expect(page.get_by_text("已隐藏")).to_have_count(0)

        # ================= 21. 系列在电视端也是一条 BoxSet =================
        tv_boxsets = {
            row["Name"]: row
            for row in jf_get("/Items", ParentId=collections_view_guid())["Items"]
        }
        assert SERIES_NAME in tv_boxsets, tv_boxsets.keys()
        assert tv_boxsets[SERIES_NAME]["ChildCount"] == 2
        # ParentId 指向某个库时只回这个库的合集（此前会把别的库的一起返回）
        movie_only = jf_get(
            "/Items", ParentId=library_guid(movie_lib), IncludeItemTypes="BoxSet"
        )["Items"]
        assert SERIES_NAME in {row["Name"] for row in movie_only}

        # ================= 22. 影片页：系列与合集分两行，都点得进去 =========
        page.goto(f"{base}/library/{movie_lib}/item/{ids['你的名字']}")
        page.wait_for_load_state("networkidle")
        # 两行各带标签词：不加的话「新海诚系列」和「日本动画」长得一模一样，
        # 而它们一个是作品的事实、一个是用户的归类
        expect(page.get_by_text("系列", exact=True)).to_be_visible()
        expect(page.get_by_text("合集", exact=True)).to_be_visible()
        # 合集那一行认出了这部片所属的自建合集，且**不重复**系列
        collection_link = page.get_by_role("link", name="日本动画")
        expect(collection_link).to_be_visible()
        shot("28-item-series-and-collections")
        collection_link.click()
        page.wait_for_url(lambda u: "/c/" in u)
        expect(page.locator("[data-library-item-id]")).to_have_count(
            len(ANIME_TITLES & JP_TITLES)
        )

        page.goto(f"{base}/library/{movie_lib}/item/{ids['你的名字']}")
        page.wait_for_load_state("networkidle")
        series_link = page.get_by_role("link", name=SERIES_NAME)
        expect(series_link).to_be_visible()
        series_link.click()
        page.wait_for_url(lambda u: "/c/" in u)
        expect(page.get_by_text("作品系列 ·").first).to_be_visible()
        shot("23-item-to-series")

        # ================= 23. 加入合集：从影片页把一部片塞进手动合集 =======
        # 手工增删只对**名单驱动**的合集开放，所以先建一个（第 14 步那个
        # 「就这批科幻」已经在第 17 步删掉了）
        before = ["盗梦空间", "星际穿越", "黑客帝国"]
        fixed_id = api(
            "post",
            "/collections",
            data={
                "name": "周末清单",
                "library_id": movie_lib,
                "item_ids": [ids[t] for t in before],
            },
        )["data"]["id"]

        page.goto(f"{base}/library/{movie_lib}/item/{ids['霸王别姬']}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="更多操作").click()
        page.get_by_role("menuitem", name="加入合集…").click()
        add_dialog = page.locator("div.menu-surface:not([role=menu])").filter(has_text="加入合集")
        expect(add_dialog).to_be_visible()
        # 自动收录的合集不在可选项里：往规则驱动的合集手工塞片，下次求值就没了
        # ——那是一种"改了、看着生效了、过一会儿又变回去"的失败
        assert SERIES_NAME not in add_dialog.inner_text(), "自动收录的合集不该出现在这里"
        assert "我的收藏" not in add_dialog.inner_text(), "内置合集也不能手工改"
        shot("24-add-to-collection")
        add_dialog.get_by_role("button").filter(has_text="周末清单").click()
        expect(add_dialog).to_have_count(0)

        after_add = [row["title"] for row in api("get", f"/collections/{fixed_id}/items")["data"]]
        assert after_add == [*before, "霸王别姬"], f"新加的排在末尾：{after_add}"

        # ================= 24. 整理顺序：改完之后三处一致 =================
        page.goto(f"{base}/library/{movie_lib}/c/{fixed_id}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="更多操作").click()
        page.get_by_role("menuitem", name="整理顺序…").click()
        # 刚收起的 Radix 菜单仍留在 DOM 里、也带 .menu-surface，得把它排掉
        panel = page.locator("div.menu-surface:not([role=menu])").filter(has_text="整理顺序")
        expect(panel).to_be_visible()
        rows_in_panel = panel.locator(".glass-row")
        expect(rows_in_panel).to_have_count(len(after_add))

        # 把刚加的那部顶到最前。用上下键而不是模拟拖拽：两种操作走的是同一份
        # 状态与同一条保存路径，而拖拽在无头浏览器里验的多半是 dnd 事件本身
        moving = rows_in_panel.filter(has_text="霸王别姬")
        for _ in range(len(after_add) - 1):
            moving.locator('button[aria-label="上移"]').click()
        # 顺手移出一部，验「移出的只是名单里的一行，作品一部不少」
        dropped = after_add[-2]
        panel.locator(f'button[aria-label="移出 {dropped}"]').click()
        expect(rows_in_panel).to_have_count(len(after_add) - 1)
        shot("25-collection-order")
        panel.get_by_role("button", name="保存顺序").click()
        expect(panel).to_have_count(0)

        expected = ["霸王别姬", *[t for t in before if t != dropped]]

        # (a) 站内
        web_order = [row["title"] for row in api("get", f"/collections/{fixed_id}/items")["data"]]
        assert web_order == expected, f"站内顺序：{web_order}"
        wall_titles = {
            row["title"] for row in api("get", f"/libraries/{movie_lib}/items?limit=200")["data"]
        }
        assert dropped in wall_titles, "移出合集不该动到作品本身"

        # (b) 电视端：BoxSet 的孩子
        fixed_boxset = next(
            row
            for row in jf_get("/Items", ParentId=collections_view_guid())["Items"]
            if row["Name"] == "周末清单"
        )
        tv_order = [row["Name"] for row in jf_get("/Items", ParentId=fixed_boxset["Id"])["Items"]]
        assert tv_order == expected, f"电视端顺序：{tv_order}"

        # (c) 分享页
        page.get_by_role("button", name="更多操作").click()
        page.get_by_role("menuitem", name="分享…").click()
        expect(page.get_by_text("已有一条有效分享")).to_have_count(0)
        page.get_by_role("button", name="生成链接").click()
        # 「生成链接」成功后原地切到「已分享」形态，不关窗不二跳
        expect(page.get_by_role("button", name=re.compile("复制链接"))).to_be_visible()
        page.keyboard.press("Escape")
        slug = api("get", f"/collections/{fixed_id}/share")["data"]["slug"]
        shared = api("get", f"/share/{slug}/collection")["data"]
        assert [row["title"] for row in shared["items"]] == expected, shared["items"]

        # 访客真打得开：分享页是没有登录态的另一套渲染，接口通不等于页面通
        guest = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
        guest_page = guest.new_page()
        guest_page.set_default_timeout(20_000)
        guest_page.goto(f"{base}/s/{slug}")
        guest_page.wait_for_load_state("networkidle")
        expect(guest_page.get_by_role("heading", name="周末清单")).to_be_visible()
        expect(guest_page.get_by_text(f"{len(expected)} 部")).to_be_visible()
        guest_page.screenshot(path=str(shots / "26-shared-collection.png"))
        guest.close()

        # ================= 25. 跨库合集：只在「全部合集」里露出 =============
        # 跨库合集只能是固定名单——规则求值目前需要单一 library_id，这条约束
        # 也正是 /library/favorites 没能并进合集详情页的原因（F4.5）
        rejected = page.request.post(
            f"{base}/api/v1/collections",
            data={"name": "跨库规则", "rules": [{"field": "rating_gte", "values": ["8.5"]}]},
        )
        assert rejected.status == 400, "跨库 + 规则驱动应当被拒，而不是建出一个永远空的合集"

        cross = api(
            "post",
            "/collections",
            data={"name": "两个库都挑几部", "item_ids": [ids["霸王别姬"], ids["寄生虫"]]},
        )["data"]
        assert cross["library_id"] is None and cross["item_count"] == 2

        page.goto(f"{base}/library/{movie_lib}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("tab", name="合集").click()
        # 跨库合集不进单库页：点进去会看到本库没有的片，那比"找不到入口"更难解释
        assert "两个库都挑几部" not in page.locator("body").inner_text()

        page.goto(f"{base}/library/collections")
        page.wait_for_load_state("networkidle")
        # 顶栏标题是滚动才浮出来的（PageNav 的 --nav-reveal），静止态量不到；
        # 这一页的身份由「跨库」这一组来认——它本来就是这页存在的理由
        expect(page.get_by_role("heading", name="跨库")).to_be_visible()
        expect(page.get_by_text("不属于任何一个库的手动名单")).to_be_visible()
        cross_card = page.locator(f'a[href="/library/c/{cross["id"]}"]')
        expect(cross_card).to_have_count(1)
        shot("27-all-collections")
        cross_card.click()
        page.wait_for_url(lambda u: f"/library/c/{cross['id']}" in u)
        page.wait_for_load_state("networkidle")
        # 两部片来自两个库，同一面墙上都在——这正是跨库合集存在的理由
        expect(page.locator("[data-library-item-id]")).to_have_count(2)

        # 收尾：把这三段建的合集删掉。stack 是 module 级的，移动端那个用例接着
        # 用同一份库存，chip 行上多出来的合集会改变它量的那些位置
        api("delete", f"/collections/{fixed_id}")
        api("delete", f"/collections/{cross['id']}")


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

        # ================= 2. 点「筛选」直接拉起底部抽屉，所有维度平铺 =================
        # 窄屏不在页面里展开一排下拉：横滚会在滑动中误触弹出，两列铺开又占掉半屏、
        # 还得再点一层才看得到取值。抽屉里每个维度直接平铺成胶囊，点一下就是一个取值
        page.get_by_role("button", name=FILTER_BTN).click()
        sheet = page.get_by_role("button", name="收起筛选")
        expect(sheet).to_be_visible()
        # 一二级在同一个抽屉里：没有「更多筛选」这扇门
        expect(page.get_by_role("button", name="更多筛选")).to_have_count(0)
        anime_pill = page.get_by_role("button", name=re.compile(r"^动画\s*\d+$"))
        expect(anime_pill).to_be_visible()
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1"
        ), "打开筛选抽屉后出现了横向溢出"
        shot("02-filter-sheet")

        # ================= 3. 点胶囊立即生效，「查看 N 部」只是收起 =================
        view_all = page.locator("button").filter(has_text="查看")
        anime_pill.click()
        expect(view_all).to_contain_text(str(len(ANIME_TITLES)))
        # 点胶囊不弹任何菜单——「菜单里再弹菜单」正是要去掉的那一层
        expect(page.get_by_role("menu")).to_have_count(0)
        shot("03-sheet-primary")
        view_all.click()
        expect(sheet).to_have_count(0)

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

        # ================= 5. 底部抽屉：留得住墙、二级维度同样立即生效 =================
        page.get_by_role("button", name="清空").click()
        page.get_by_role("button", name=FILTER_BTN).click()
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
        # 窄屏的视图切换挂进 PageNav 顶栏、搜索键左侧——与发现页把 TMDB / 豆瓣
        # 切换挂进全局顶栏同一个位置。代价是吸顶标题让位：390px 放不下 ☰ + 返回
        # + 切换 + 搜索 + ⋯ 之外再加一个标题。这里验：切换与 ⋯ 同一行、排在它
        # 左边，整行没有挤出屏幕，正文里也没有第二份切换
        page.goto(f"{base}/library/{movie_lib}")
        page.wait_for_load_state("networkidle")
        tabs = page.get_by_role("tablist", name="库内视图")
        expect(tabs).to_have_count(1)
        expect(tabs).to_be_visible()
        tabs_box = tabs.bounding_box()
        more_box = page.get_by_role("button", name="更多操作").bounding_box()
        assert (
            abs((tabs_box["y"] + tabs_box["height"] / 2) - (more_box["y"] + more_box["height"] / 2))
            < 12
        ), "视图切换没挂进顶栏那一行"
        assert tabs_box["x"] + tabs_box["width"] <= more_box["x"], "视图切换没排在 ⋯ 左边"
        assert more_box["x"] + more_box["width"] <= 390, "顶栏塞太满，⋯ 被挤出屏幕右边"
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
        shot("12-view-switch-in-top-bar")
        # 切换随顶栏吸顶：正文滚走了它还在原地，与发现页的数据源切换一致
        assert abs(tabs.bounding_box()["y"] - tabs_box["y"]) < 4, "视图切换跟着正文滚走了"

        # 右上角只留搜索与 ⋯ 两颗：图床入口收进了菜单
        assert page.get_by_role("button", name="图床浏览").count() == 0, "图床键还在顶栏"
        page.get_by_role("button", name="更多操作").click()
        expect(page.get_by_role("menuitem", name="图床浏览")).to_be_visible()
        page.keyboard.press("Escape")

        # 合集视图里连菜单项都不该有：那面墙根本不在，点了什么也不会发生
        page.goto(f"{base}/library/{movie_lib}?view=collections")
        page.wait_for_load_state("networkidle")
        if page.get_by_role("button", name="更多操作").count() > 0:
            page.get_by_role("button", name="更多操作").click()
            expect(page.get_by_role("menuitem", name="图床浏览")).to_have_count(0)
            page.keyboard.press("Escape")

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
        # 页面要自己出滚动容器（外壳的 main 不滚）：少了这一层，海报墙超出一屏
        # 就滑不动。PageNav 是那个容器的直接子节点，顺着它找父节点最准
        assert page.evaluate(
            "() => { const nav = document.querySelector('main [class*=\"sticky\"]');"
            "  return !!nav && getComputedStyle(nav.parentElement).overflowY === 'auto'; }"
        ), "合集详情页没有自己的滚动容器，内容超出一屏会滑不动"
        # 管理动作在窄屏同样收在 ⋯ 里
        page.get_by_role("button", name="更多操作").click()
        expect(page.get_by_role("menuitem", name="改名")).to_be_visible()
        shot("11-detail-menu")

        assert not page_errors, f"页面报错：{page_errors[:3]}"
        context.close()
        browser.close()

