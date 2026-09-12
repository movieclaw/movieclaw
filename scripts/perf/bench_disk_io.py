#!/usr/bin/env python3
"""磁盘 IO 实验台：量「家用 NAS 的日常操作各自摸多少次盘」。

为什么要它
----------
NAS 上的机械盘最怕**随机小 IO**：读一次 NFO、列一次目录、刷一次 mtime，
单看都不值一提，但媒体库的浏览链路会把它们乘上条目数、文件数、图片数。
这类放大在接口耗时上常常看不出来（本机页缓存全接住了），必须直接数系统
调用才现形——一部 30 集的剧，详情页曾经打出 1300 次系统调用。

怎么用
------
三步，全程不碰真实 ``data/``（一切落在 ``--lab`` 指定的目录里）::

    # 1. 造一棵真文件树
    python scripts/perf/seed_media_tree.py /tmp/iolab/media

    # 2. 建库 + 扫描，产出可反复复用的 data/ 快照（只需跑一次）
    LAB_DIR=/tmp/iolab python scripts/perf/bench_disk_io.py setup

    # 3. 跑只读场景（可反复跑，对比优化前后）
    LAB_DIR=/tmp/iolab python scripts/perf/bench_disk_io.py bench

想看**系统调用**而不只是耗时，把第 3 步套进 strace，再用
``analyze_strace.py`` 按阶段归因::

    strace -f -qq -s 200 -o /tmp/iolab/trace \
      -e trace=openat,close,read,pread64,write,pwrite64,fsync,fdatasync,\
newfstatat,statx,fstat,getdents64,utimensat,rename,unlink,mkdir,ftruncate \
      python scripts/perf/bench_disk_io.py bench
    python scripts/perf/analyze_strace.py /tmp/iolab/trace

设计要点
--------
- **进程内直连 ASGI**（httpx ASGITransport）：没有网络栈噪声，strace 里剩下的
  全是真的磁盘动作；
- **阶段打点**：每个场景前后 stat 一个不存在的 ``/__MARK__/<阶段>``，strace
  里一眼可见，归因脚本据此切分；
- **假 TMDB 跑在本机**：沙箱/CI 没有 Key 也没有外网，但识别链必须真跑一遍，
  库存数据才真实。它同时兼作图床，海报下载链路也照常跑通。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import threading
import time
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed_media_tree as seed  # noqa: E402

LAB = Path(os.environ.get("LAB_DIR", "/tmp/iolab"))
DATA = LAB / "data"
MEDIA = LAB / "media"
ADMIN = {"username": "admin", "password": "lab-passw0rd"}
ROUNDS = int(os.environ.get("LAB_ROUNDS", "3"))


# ---------------------------------------------------------------------------
# 阶段打点
# ---------------------------------------------------------------------------
def mark(name: str) -> None:
    """在 strace 里留一条可检索的分隔线（一次注定 ENOENT 的 stat）。"""
    with contextlib.suppress(OSError):
        os.stat(f"/__MARK__/{name}")


# ---------------------------------------------------------------------------
# 假 TMDB：按 id 规则现编条目，覆盖 seed_media_tree 产出的全部标题
# ---------------------------------------------------------------------------
_PNG = seed.png(300, 450, 7)


def _fake_movie(tmdb_id: int) -> dict:
    index = tmdb_id - seed.MOVIE_TMDB_BASE
    title, year = seed.movie_title(index), seed.movie_year(index)
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": f"{title} EN",
        "release_date": f"{year}-05-01",
        "runtime": 90 + index % 60,
        "overview": f"{title} 的 TMDB 简介。",
        "status": "Released",
        "vote_average": 6.0 + (index % 40) / 10,
        "poster_path": f"/p{tmdb_id}.jpg",
        "backdrop_path": f"/b{tmdb_id}.jpg",
        "genres": [{"id": 18, "name": "剧情"}, {"id": 9648, "name": "悬疑"}],
        "external_ids": {"imdb_id": f"tt{tmdb_id}"},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
        "credits": {"cast": [], "crew": []},
    }


def _fake_tv(tmdb_id: int) -> dict:
    index = tmdb_id - seed.SHOW_TMDB_BASE
    title, year = seed.show_title(index), seed.show_year(index)
    return {
        "id": tmdb_id,
        "name": title,
        "original_name": f"{title} EN",
        "first_air_date": f"{year}-01-01",
        "overview": f"{title} 的 TMDB 简介。",
        "status": "Returning Series",
        "vote_average": 7.0,
        "poster_path": f"/p{tmdb_id}.jpg",
        "backdrop_path": f"/b{tmdb_id}.jpg",
        "genres": [{"id": 18, "name": "剧情"}],
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "credits": {"cast": [], "crew": []},
        "seasons": [
            {"season_number": n, "episode_count": seed.EPISODES}
            for n in range(1, seed.SEASONS + 1)
        ],
    }


def _fake_season(tmdb_id: int, number: int) -> dict:
    year = seed.show_year(tmdb_id - seed.SHOW_TMDB_BASE)
    return {
        "name": f"第 {number} 季",
        "air_date": f"{year}-01-01",
        "season_number": number,
        "episodes": [
            {
                "episode_number": ep,
                "season_number": number,
                "name": f"第{ep}集",
                "overview": f"S{number:02d}E{ep:02d} 简介",
                "air_date": f"{year}-0{number}-{ep:02d}",
                "still_path": f"/s{tmdb_id}-{number}-{ep}.jpg",
                "runtime": 45,
            }
            for ep in range(1, seed.EPISODES + 1)
        ],
    }


def _search(kind: str, query: str) -> list[dict]:
    if kind == "movie":
        for i in range(seed.MOVIE_COUNT):
            if seed.movie_title(i) == query:
                return [_fake_movie(seed.MOVIE_TMDB_BASE + i)]
    else:
        for i in range(seed.SHOW_COUNT):
            if seed.show_title(i) == query:
                return [_fake_tv(seed.SHOW_TMDB_BASE + i)]
    return []


class _TmdbHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静音
        pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/img/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)
            return
        body = self._route(parsed.path, parse_qs(parsed.query))
        if body is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"status_message":"not found"}')
            return
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _route(self, path: str, query: dict) -> dict | None:
        parts = [p for p in path.split("/") if p]
        if parts[:1] != ["3"]:
            return None
        parts = parts[1:]
        if parts[:1] == ["search"] and len(parts) == 2:
            return {"results": _search(parts[1], (query.get("query") or [""])[0])}
        if parts[:1] == ["movie"] and len(parts) == 2 and parts[1].isdigit():
            return _fake_movie(int(parts[1]))
        if parts[:1] == ["tv"] and len(parts) == 2 and parts[1].isdigit():
            return _fake_tv(int(parts[1]))
        if parts[:1] == ["tv"] and len(parts) == 4 and parts[2] == "season":
            return _fake_season(int(parts[1]), int(parts[3]))
        return {"results": []}


def _serve_fake_tmdb() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TmdbHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


# ---------------------------------------------------------------------------
# 环境装配
# ---------------------------------------------------------------------------
def prepare_env() -> None:
    """把所有 data/ 下的目录改指到实验台，并把 TMDB 指向本机假服务。"""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(var, None)
    port = _serve_fake_tmdb()
    os.environ["TMDB_API_KEY"] = "lab-key"
    os.environ["TMDB_API_BASE_URL"] = f"http://127.0.0.1:{port}/3"
    os.environ["TMDB_IMAGE_BASE_URL"] = f"http://127.0.0.1:{port}/img/"
    os.environ.setdefault("APP_ENV", "local")
    os.environ.setdefault("APP_RELOAD", "false")
    os.environ.setdefault("APP_LOG_LEVEL", "WARNING")
    os.environ.setdefault("APP_ACCESS_LOG_ENABLED", "false")
    for key, value in {
        "MOVIECLAW_DATA_DIR": DATA,
        "DATABASE_URL": f"sqlite+aiosqlite:///{DATA / 'movieclaw.db'}",
        "METADATA_DIR": DATA / "metadata",
        "LOG_DIR": DATA / "logs",
        "SECRET_KEY_FILE": DATA / ".secret_key",
        "MEDIA_DIR": DATA / "uploads",
        "IMAGE_CACHE_DIR": DATA / "cache/images",
        "MOVIECLAW_TRICKPLAY_CACHE_DIR": DATA / "cache/playback-trickplay",
        "MOVIECLAW_PLAYBACK_SUBS_CACHE_DIR": DATA / "cache/playback-subs",
        "MOVIECLAW_SUBTITLE_GEN_CACHE_DIR": DATA / "cache/subtitle_gen",
        "MOVIECLAW_UPDATES_DIR": DATA / "updates",
        "MOVIECLAW_TRANSCODE_DIR": DATA / "transcodes",
        "SITE_CONFIGS_DIR": DATA / "site-configs",
        "AGENT_WORKSPACE_DIR": DATA / "agent-workspace",
        "AGENT_SESSIONS_DIR": DATA / "agent-sessions",
        "AGENT_SKILLS_DIR": DATA / "agent-skills",
        "MOVIECLAW_WEB_PORT_FILE": DATA / "config/web-port",
    }.items():
        os.environ[key] = str(value)

    # 假图床跑在 127.0.0.1 的随机端口上，生产的 SSRF 校验会拦掉；实验台放行本机
    from urllib.parse import urlsplit

    from movieclaw_api.services import image_proxy

    async def _allow_local(self, url: str) -> str:  # noqa: ANN001
        return (urlsplit(url).hostname or "").lower()

    image_proxy.ImageProxy._validated_host = _allow_local


class Client:
    """进程内 ASGI 客户端；``api`` 前缀省得每次写。"""

    def __init__(self, app) -> None:  # noqa: ANN001
        import httpx

        self.raw = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://lab", timeout=180
        )

    async def get(self, path: str, **kw):  # noqa: ANN201
        return await self.raw.get(f"/api/v1{path}", **kw)

    async def post(self, path: str, **kw):  # noqa: ANN201
        return await self.raw.post(f"/api/v1{path}", **kw)


async def boot() -> tuple[object, AsyncExitStack, Client]:
    from movieclaw_api.app import create_app

    app = create_app()
    stack = AsyncExitStack()
    await stack.enter_async_context(app.router.lifespan_context(app))
    return app, stack, Client(app)


def _db_counts() -> dict[str, int]:
    import sqlite3

    path = DATA / "movieclaw.db"
    if not path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out: dict[str, int] = {}
        for table in ("media_item", "library_file", "media_episode"):
            with contextlib.suppress(sqlite3.Error):
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# setup：建库 + 扫描
# ---------------------------------------------------------------------------
async def setup() -> None:
    import movieclaw_api.services.library.ingest as ingest_mod
    import movieclaw_api.services.library.scan as scan_mod

    # 假视频是刚生成的，"疑似写入中"的静默窗口等不起
    ingest_mod.QUIET_SECONDS = 0
    scan_mod.NEW_FILE_QUIET_SECONDS = 0

    _app, stack, cli = await boot()
    resp = await cli.post("/auth/bootstrap", json=ADMIN)
    assert resp.status_code == 200, resp.text
    for name, kind, root in (("电影", "movie", MEDIA / "movies"), ("剧集", "tv", MEDIA / "tv")):
        resp = await cli.post(
            "/libraries",
            json={
                "name": name,
                "kind": kind,
                "root_paths": [str(root)],
                "generate_thumbnails": False,
                "extract_chapter_images": False,
                "realtime_watch": False,
            },
        )
        assert resp.status_code == 200, resp.text

    # 扫描是后台 Job：等到「都不在扫 + 台账连续几轮不再变化」
    deadline, quiet, last = time.time() + 7200, 0, None
    while time.time() < deadline and quiet < 4:
        await asyncio.sleep(5)
        libs = (await cli.get("/libraries")).json()["data"]
        counts = _db_counts()
        idle = all(not x["scanning"] and x["last_scan"] for x in libs)
        quiet = quiet + 1 if (idle and counts == last) else 0
        last = counts
        print(f"  进度 {counts} 扫描中={[x['scanning'] for x in libs]}")
    print("扫描完成：", json.dumps(_db_counts(), ensure_ascii=False))
    await cli.raw.aclose()
    await stack.aclose()


# ---------------------------------------------------------------------------
# bench：日常高频场景
# ---------------------------------------------------------------------------
class Phases:
    def __init__(self) -> None:
        self.rows: list[tuple[str, float]] = []

    async def run(self, name: str, coro_fn) -> None:  # noqa: ANN001
        mark(f"BEGIN-{name}")
        started = time.perf_counter()
        await coro_fn()
        elapsed = time.perf_counter() - started
        mark(f"END-{name}")
        self.rows.append((name, elapsed))

    def report(self) -> None:
        print("\n=== 场景耗时 ===")
        for name, elapsed in self.rows:
            print(f"{name:16s} {elapsed * 1000:9.1f} ms")


async def bench() -> None:  # noqa: PLR0915 - 场景清单本就是一长串
    _app, stack, cli = await boot()
    resp = await cli.post("/auth/login", json=ADMIN)
    assert resp.status_code == 200, resp.text

    libs = (await cli.get("/libraries")).json()["data"]
    movie_lib = next(x for x in libs if x["kind"] == "movie")["id"]
    tv_lib = next(x for x in libs if x["kind"] == "tv")["id"]
    items = (await cli.get(f"/libraries/{movie_lib}/items", params={"limit": 60})).json()["data"]
    tv_items = (await cli.get(f"/libraries/{tv_lib}/items", params={"limit": 20})).json()["data"]
    poster_urls = [
        f"/api/v1{it['poster_url']}&variant=poster-card"
        for it in items[:60]
        if it.get("poster_url")
    ]

    async def home():
        for _ in range(ROUNDS):
            await asyncio.gather(
                cli.get("/libraries"),
                cli.get("/playback/up-next"),
                cli.get("/playback/resume"),
                cli.get("/playback/activity"),
                cli.get("/playback/favorites"),
            )

    async def poster_wall():
        """打开一个媒体库：海报墙首屏 + 翻一页 + 筛选栏 + A-Z 索引。"""
        for _ in range(ROUNDS):
            await asyncio.gather(
                cli.get(f"/libraries/{movie_lib}/items", params={"limit": 60, "offset": 0}),
                cli.get(f"/libraries/{movie_lib}/facets"),
                cli.get(f"/libraries/{movie_lib}/item-index"),
            )
            await cli.get(f"/libraries/{movie_lib}/items", params={"limit": 60, "offset": 60})

    async def detail():
        for _ in range(ROUNDS):
            for it in items[:20]:
                await cli.get(f"/libraries/{movie_lib}/items/{it['media_item_id']}")

    async def tv_detail():
        for _ in range(ROUNDS):
            for it in tv_items[:8]:
                item_id = it["media_item_id"]
                await cli.get(f"/libraries/{tv_lib}/items/{item_id}")
                await cli.get(
                    f"/libraries/{tv_lib}/items/{item_id}/episodes",
                    params={"season_number": 1},
                )

    async def item_artwork():
        """详情页的条目目录美术图：每一次都要翻媒体盘。"""
        for _ in range(ROUNDS):
            for it in items[:20]:
                for kind in ("poster", "fanart"):
                    await cli.get(
                        f"/libraries/{movie_lib}/items/{it['media_item_id']}/artwork",
                        params={"kind": kind},
                    )

    async def images_cold():
        """首次访问派生图：Pillow 编码 + 落盘。"""
        for url in poster_urls:
            resp = await cli.raw.get(url)
            assert resp.status_code == 200, (url, resp.status_code)

    async def images_warm():
        """回访：派生图已在缓存里——家用场景里绝对的高频路径。"""
        for _ in range(ROUNDS):
            await asyncio.gather(*(cli.raw.get(url) for url in poster_urls))

    async def jellyfin():
        """电视端打开一个库：拿一页条目 + 每格海报（带缩放参数，真机就这么发）。"""
        auth = ('MediaBrowser Client="Infuse", Device="Apple TV", '
                'DeviceId="lab-device", Version="8.2"')
        resp = await cli.raw.post(
            "/Users/AuthenticateByName",
            json={"Username": ADMIN["username"], "Pw": ADMIN["password"]},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200, resp.text
        headers = {"Authorization": f'{auth}, Token="{resp.json()["AccessToken"]}"'}
        user_id = resp.json()["User"]["Id"]
        views = (await cli.raw.get(f"/Users/{user_id}/Views", headers=headers)).json()["Items"]
        view_id = next(v["Id"] for v in views)
        params = {"ParentId": view_id, "Limit": 60, "Recursive": "false"}
        jf_items = (
            await cli.raw.get(f"/Users/{user_id}/Items", params=params, headers=headers)
        ).json()["Items"]
        for _ in range(ROUNDS):
            await cli.raw.get(f"/Users/{user_id}/Items", params=params, headers=headers)
            await asyncio.gather(
                *(
                    cli.raw.get(
                        f"/Items/{it['Id']}/Images/Primary",
                        params={"fillWidth": 400, "quality": 90},
                        headers=headers,
                    )
                    for it in jf_items[:40]
                )
            )

    async def rescan():
        """用户点一次「扫描」：全树重走，但一行台账都不该变。"""
        for lib in (movie_lib, tv_lib):
            resp = await cli.post(f"/libraries/{lib}/scan")
            assert resp.status_code in (200, 202), resp.text
        while True:
            await asyncio.sleep(2)
            if not any(x["scanning"] for x in (await cli.get("/libraries")).json()["data"]):
                return

    async def reconcile():
        """6 小时一轮的定期对账（不做历史规格补探，因此不 stat 视频本体）。"""
        from movieclaw_api.services.library.scan import reconcile_libraries

        await reconcile_libraries()

    async def idle():
        """完全没人访问的一段时间：这段时间里的每一次写都意味着硬盘无法休眠。"""
        await asyncio.sleep(float(os.environ["LAB_IDLE"]))

    phases = Phases()
    await phases.run("home", home)
    await phases.run("poster-wall", poster_wall)
    await phases.run("detail", detail)
    await phases.run("tv-detail", tv_detail)
    await phases.run("item-artwork", item_artwork)
    await phases.run("images-cold", images_cold)
    await phases.run("images-warm", images_warm)
    await phases.run("jellyfin", jellyfin)
    if os.environ.get("LAB_SCAN") == "1":
        await phases.run("rescan", rescan)
        await phases.run("reconcile", reconcile)
    if os.environ.get("LAB_IDLE"):
        await phases.run("idle", idle)
    phases.report()

    await cli.raw.aclose()
    await stack.aclose()


def main() -> None:
    prepare_env()
    command = sys.argv[1] if len(sys.argv) > 1 else "bench"
    if command == "setup":
        if DATA.exists():
            shutil.rmtree(DATA)
        DATA.mkdir(parents=True)
        asyncio.run(setup())
    elif command == "bench":
        asyncio.run(bench())
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
