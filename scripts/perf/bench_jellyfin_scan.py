#!/usr/bin/env python3
"""Jellyfin 兼容层「播放器首屏扫描」压测脚本（mock 数据 + 前后对照）。

背景
----
Infuse 之类的播放器每次启动都会对媒体库做一次密集扫描：几十次 Latest、
几十次库分页、上百次 Shows/{id}/Seasons，全部在几十秒内打完。这个脚本
把那一次扫描原样搬进进程内，用来量化服务端改动的实际收益。

三件事一起做，缺一不可：

1. **mock 数据**：按真实部署的形状生成（10 个库 / 855 个条目 / 6837 个
   台账行 / 9353 集元数据 / 556 季 / 92 条播放状态），固定随机种子，
   任何两次生成完全一致——不然前后对照没有意义。
2. **请求脚本**：请求条数与端点配比抄自真实 Infuse 启动时的访问日志
   （Latest 50 次、/Items 79 次、Seasons 107 次但只涉及 6 部剧、
   条目详情 49 次），跑完整 HTTP 栈（TestClient → ASGI → 路由 → DB）。
3. **响应快照**：每个请求的状态码与响应体都落进 --dump 的 JSON，
   优化后用 --compare 逐字节比对。**性能优化不允许改变输入输出**，
   这个比对就是那条红线的自动化守卫。

用法::

    # 基线：跑一遍并落快照
    python scripts/perf/bench_jellyfin_scan.py --dump /tmp/jf-before.json

    # 改完代码：再跑一遍，比对响应 + 看耗时差
    python scripts/perf/bench_jellyfin_scan.py --compare /tmp/jf-before.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

# 固定种子：两次生成的库必须逐字节一致
SEED = 20260828
NOW = datetime(2026, 8, 28, 12, 0, 0)
NOW_S = NOW.isoformat(sep=" ")

ADMIN = {"username": "admin", "password": "s3cret-pass"}
AUTH_HEADER = (
    'MediaBrowser Client="Infuse", Device="Apple TV", '
    'DeviceId="bench-device-1", Version="8.2"'
)

# —— 真实部署的库形状（2026-08-28 从 NAS 生产库统计）——
# (名称, 类型, 条目数, 目标台账行数)
LIBRARY_PLAN = [
    ("大陆电影", "movie", 100, 100),
    ("大陆剧", "tv", 88, 3114),
    ("欧美电影", "movie", 331, 334),
    ("日韩电影", "movie", 89, 92),
    ("港台电影", "movie", 72, 80),
    ("日韩剧", "tv", 44, 645),
    ("港台剧", "tv", 5, 51),
    ("欧美剧", "tv", 49, 1155),
    ("综艺", "tv", 10, 494),
    ("纪录片", "tv", 38, 770),
]

# 每部剧的台账行数：生产实测是强偏态（最大 234 行，绝大多数在 10~40 行），
# 平均分配会把 Seasons/详情这类"整剧水合"接口的成本抹平，测不出真问题
TV_FILE_DIST = ([234, 176, 161, 149, 110, 96, 88, 80, 74, 70]
                + [x for x in range(40, 70, 2)] * 2
                + [x for x in range(12, 40)] * 5
                + [x for x in range(1, 12)] * 6)

# 每剧季数分布（生产实测：127 部 1 季、44 部 2 季……）
SEASON_DIST = [1] * 127 + [2] * 44 + [3] * 27 + [4] * 13 + [5] * 6 + [6] * 7 + \
    [7] * 5 + [8] * 1 + [9] * 3 + [10] * 3 + [11] * 1 + [12] * 1 + [13] * 1
# 单元多版本分布（5757 个单元 1 个文件、472 个 2 个、22 个 3 个）
VERSION_DIST = [1] * 5757 + [2] * 472 + [3] * 22

CN_HEAD = ["长安", "三体", "流浪", "无间", "琅琊", "大明", "沙丘", "白夜", "隐秘",
           "山海", "风起", "赘婿", "觉醒", "漫长", "开端", "狂飙", "繁花", "边水",
           "唐朝", "雪中", "剑来", "凡人", "斗罗", "遮天", "武动", "天官", "魔道"]
CN_TAIL = ["十二时辰", "之诗", "地球", "行者", "榜", "王朝", "救赎", "追凶",
           "的角落", "情", "陇西", "年代", "季", "的季节", "时刻", "人生",
           "小巷", "迷雾", "诡事", "神话", "仙途", "大陆", "记", "赐福"]
EN_TITLES = ["Dune", "Oppenheimer", "Interstellar", "Arrival", "Blade Runner",
             "The Expanse", "Severance", "Fallout", "Andor", "Foundation",
             "Chernobyl", "Succession", "Silo", "Shogun", "Reacher", "Bosch"]

RESOLUTIONS = ["2160p", "1080p", "1080p", "1080p", "720p"]
CODECS = ["hevc", "h264", "h264", "av1"]
HDRS = [None, None, None, "HDR10", "Dolby Vision"]
GENRES = ["剧情", "动作", "科幻", "悬疑", "犯罪", "喜剧", "爱情", "历史"]
STATUSES_TV = ["Returning Series", "Ended", "Canceled"]

_AUDIO = json.dumps([
    {"codec": "eac3", "profile": None, "channels": 6, "channel_layout": "5.1",
     "language": "zho", "title": "国语", "default": True},
    {"codec": "aac", "profile": "LC", "channels": 2, "channel_layout": "stereo",
     "language": "eng", "title": "English", "default": False},
], ensure_ascii=False)
_SUBS = json.dumps([
    {"codec": "subrip", "language": "zho", "title": "简体",
     "forced": False, "default": True},
    {"codec": "ass", "language": "zho", "title": "特效", "forced": False,
     "default": False},
], ensure_ascii=False)
_OVERVIEW = "在一个被时代洪流裹挟的年代里，一群普通人各自挣扎、彼此照见，" \
    "最终在命运的岔路口做出了自己的选择。" * 2


# ---------------------------------------------------------------------------
# mock 数据生成
# ---------------------------------------------------------------------------
def seed_dataset(db_path: str) -> dict:
    """按生产形状灌入 mock 数据，返回压测脚本需要的 id 索引。"""
    rng = random.Random(SEED)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA foreign_keys=OFF")

    items, metas, seasons, episodes, files = [], [], [], [], []
    states = []
    used: set[str] = set()
    item_id = file_id = 0
    lib_index: list[dict] = []
    tv_by_size: list[tuple[int, int]] = []   # (文件数, item_id)，取热点剧用

    def _title() -> str:
        while True:
            t = (rng.choice(CN_HEAD) + rng.choice(CN_TAIL)) if rng.random() < 0.7 \
                else f"{rng.choice(EN_TITLES)} {rng.randint(1, 99)}"
            if t not in used:
                used.add(t)
                return t

    def _dt(days_back: int = 34) -> datetime:
        # 生产库 created_at 跨度就是最近 34 天（一次性导入 + 持续入库）
        return NOW - timedelta(seconds=rng.randint(0, days_back * 86400))

    def _versions(n_versions: int, unit_created: datetime):
        """同一单元的多个版本：入库时间随版本递增。

        生产实测 495 个多版本单元里，created_at 的先后与自增 id 的先后
        100% 一致（版本是先后扫进来的）。mock 必须复现这一点，否则会造出
        真实世界不存在的组合，把"多版本输出顺序"这种隐式次序的比对搅浑。
        """
        return [unit_created + timedelta(minutes=v) for v in range(n_versions)]

    for lib_id, (name, kind, item_count, target_files) in enumerate(LIBRARY_PLAN, 1):
        lib_items: list[int] = []
        produced = 0
        # 剧集库按偏态分布分配台账行，再整体缩放到该库的目标行数
        if kind == "tv":
            quota = [rng.choice(TV_FILE_DIST) for _ in range(item_count)]
            scale = target_files / max(sum(quota), 1)
            quota = [max(1, round(q * scale)) for q in quota]
        else:
            quota = [1] * item_count
        for _idx in range(item_count):
            item_id += 1
            lib_items.append(item_id)
            title = _title()
            year = rng.randint(1998, 2026)
            created = _dt()
            created_s = created.isoformat(sep=" ")
            items.append((
                item_id, kind, 100_000 + item_id, f"tt{7_000_000 + item_id}",
                str(30_000_000 + item_id) if rng.random() < 0.6 else None,
                title, title, year, "[]",
                "Released" if kind == "movie" else rng.choice(STATUSES_TV),
                f"/p{item_id:05d}.jpg", f"/b{item_id:05d}.jpg", created_s, created_s,
            ))
            metas.append((
                item_id, _OVERVIEW, None,
                json.dumps(rng.sample(GENRES, 2), ensure_ascii=False), "[]",
                rng.randint(90, 150) if kind == "movie" else rng.randint(35, 60),
                f"{year}-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}", "zh", "[]",
                "[]", round(rng.uniform(5.5, 9.5), 1), rng.randint(50, 9000),
                "[]", "[]", f"{item_id}/poster.jpg", f"{item_id}/backdrop.jpg",
                created_s, "zh-CN", created_s, created_s,
            ))
            if kind == "movie":
                for _v, stamp in enumerate(
                    _versions(rng.choice(VERSION_DIST), created)
                ):
                    file_id += 1
                    produced += 1
                    files.append(_file_row(rng, file_id, lib_id, item_id, 0, 0,
                                           f"/media/{lib_id}/{item_id}_{_v}.mkv",
                                           stamp))
                continue
            n_seasons = rng.choice(SEASON_DIST)
            item_files = 0
            want = quota[_idx]
            for s in range(1, n_seasons + 1):
                seasons.append((item_id, s, f"第 {s} 季",
                                f"{year + s - 1}-01-01", want, None, None,
                                f"{item_id}/s{s}.jpg", created_s, created_s))
                # 生产库 media_episode(9353) 比在位台账行(6837)多约 1/3：
                # TMDB 抓到整季元数据、但只下了其中一部分集
                n_eps = max(1, round(want / n_seasons * 1.35))
                have = max(1, round(want / n_seasons))
                for e in range(1, n_eps + 1):
                    episodes.append((
                        item_id, s, e, f"第 {e} 集", _OVERVIEW[:120],
                        f"{year + s - 1}-0{rng.randint(1, 9)}-01",
                        rng.randint(35, 60), round(rng.uniform(6, 9.5), 1),
                        None, f"{item_id}/s{s}e{e}.jpg", created_s, created_s,
                    ))
                    if e > have:
                        continue  # 只有元数据、没有文件的集
                    for _v, stamp in enumerate(
                        _versions(rng.choice(VERSION_DIST), _dt())
                    ):
                        file_id += 1
                        produced += 1
                        item_files += 1
                        files.append(_file_row(
                            rng, file_id, lib_id, item_id, s, e,
                            f"/media/{lib_id}/{item_id}/S{s:02d}E{e:02d}_{_v}.mkv",
                            stamp))
            tv_by_size.append((item_files, item_id))
        lib_index.append({"id": lib_id, "name": name, "kind": kind,
                          "items": lib_items})

    # 播放状态：92 行、73 条已看（生产实测）
    flat = [(i[0],) for i in items]
    picked = rng.sample(flat, 92)
    for n, (mid,) in enumerate(picked):
        states.append((mid, 0, 0 if n % 3 else 1, 1 if n < 73 else 0,
                       rng.randint(0, 3_600_000), rng.randint(1, 5), 0, NOW_S,
                       0, NOW_S, NOW_S))

    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO library (id,name,kind,root_paths,is_default,sort_order,"
        "stats_item_count,stats_episode_count,stats_file_count,"
        "stats_total_size_bytes,stats_unidentified_count,stats_missing_count,"
        "stats_ignored_count,stats_refreshed_at,created_at,updated_at,"
        "match_rules,write_media_assets,auto_clear_missing,realtime_watch) "
        "VALUES (?,?,?,?,0,?,0,0,0,0,0,0,0,?,?,?,'[]',1,0,1)",
        [(x["id"], x["name"], x["kind"], json.dumps([f"/media/{x['id']}"]),
          x["id"], NOW_S, NOW_S, NOW_S) for x in lib_index])
    cur.executemany(
        "INSERT INTO media_item (id,kind,tmdb_id,imdb_id,douban_id,title,"
        "original_title,year,aliases,status,poster_path,backdrop_path,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", items)
    cur.executemany(
        "INSERT INTO media_metadata (media_item_id,overview,tagline,genres,"
        "genre_ids,runtime_minutes,release_date,original_language,"
        "origin_countries,studios,vote_average,vote_count,directors,\"cast\","
        "poster_file,backdrop_file,scraped_at,scrape_language,created_at,"
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", metas)
    cur.executemany(
        "INSERT INTO media_season (media_item_id,season_number,name,air_date,"
        "episode_count,overview,poster_path,poster_file,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", seasons)
    cur.executemany(
        "INSERT INTO media_episode (media_item_id,season_number,episode_number,"
        "name,overview,air_date,runtime_minutes,vote_average,still_path,"
        "still_file,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        episodes)
    cur.executemany(
        "INSERT INTO library_file (id,library_id,media_item_id,season_number,"
        "episode_number,file_path,size_bytes,container,resolution,video_codec,"
        "hdr,bit_depth,duration_seconds,bit_rate,frame_rate,color_space,"
        "media_source,release_group,source,state,audio_streams,subtitle_streams,"
        "external_subtitles,added_batch_id,identity_source,resolved_version,"
        "file_mtime_ns,media_source_manual,created_at,updated_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", files)
    cur.executemany(
        "INSERT INTO playback_state (media_item_id,season_number,episode_number,"
        "played,position_ms,play_count,is_favorite,last_played_at,member_id,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", states)
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()

    tv_by_size.sort(reverse=True)
    return {
        "libraries": lib_index,
        "counts": {"items": len(items), "files": len(files),
                   "episodes": len(episodes), "seasons": len(seasons)},
        # 扫描期被反复问 Seasons 的 6 部剧：取最大的几部（最坏情况）
        "hot_series": [mid for _, mid in tv_by_size[:6]],
        "all_items": [i[0] for i in items],
    }


def describe_dataset(db_path: str) -> dict:
    """从一个现成的库里读出压测脚本需要的 id（不写任何数据）。

    用生产库快照跑同一套脚本，是比 mock 更硬的一道验证：多版本文件、
    0 季特辑、有元数据没文件的集、跨库条目这些真实形态，mock 造不全。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    libs = []
    for lib_id, name, kind in conn.execute(
        "SELECT id,name,kind FROM library ORDER BY sort_order, id"
    ):
        libs.append({"id": lib_id, "name": name, "kind": kind, "items": []})
    hot = [
        r[0]
        for r in conn.execute(
            "SELECT lf.media_item_id FROM library_file lf "
            "JOIN media_item mi ON mi.id = lf.media_item_id "
            "WHERE lf.state='in_place' AND mi.kind='tv' "
            "GROUP BY lf.media_item_id ORDER BY COUNT(*) DESC LIMIT 6"
        )
    ]
    all_items = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT media_item_id FROM library_file "
            "WHERE state='in_place' AND media_item_id IS NOT NULL "
            "ORDER BY media_item_id"
        )
    ]
    counts = {
        "items": conn.execute("SELECT COUNT(*) FROM media_item").fetchone()[0],
        "files": conn.execute("SELECT COUNT(*) FROM library_file").fetchone()[0],
        "episodes": conn.execute("SELECT COUNT(*) FROM media_episode").fetchone()[0],
        "seasons": conn.execute("SELECT COUNT(*) FROM media_season").fetchone()[0],
    }
    conn.close()
    return {"libraries": libs, "counts": counts, "hot_series": hot,
            "all_items": all_items}


def _file_row(rng, file_id, lib_id, item_id, season, episode, path, created):
    return (
        file_id, lib_id, item_id, season, episode, path,
        rng.randint(800_000_000, 40_000_000_000),
        "mkv", rng.choice(RESOLUTIONS), rng.choice(CODECS), rng.choice(HDRS),
        rng.choice([8, 10]), rng.randint(1400, 9000),
        rng.randint(2_000_000, 40_000_000), 23.976, "bt709",
        rng.choice(["WEB-DL", "BluRay", "Remux"]), "GROUP", "scanned",
        "in_place", _AUDIO, _SUBS, "[]", f"batch-{lib_id}", "ner", 1,
        1_700_000_000_000_000_000, 0, created.isoformat(sep=" "),
        created.isoformat(sep=" "),
    )


# ---------------------------------------------------------------------------
# 请求脚本：条数与配比抄自真实 Infuse 启动扫描
# ---------------------------------------------------------------------------
LIST_FIELDS = ("Overview,Genres,MediaSources,MediaStreams,Path,DateCreated,"
               "ParentId,ProviderIds,ChildCount,RecursiveItemCount,OriginalTitle")


def build_script(ids: dict, guid_of) -> list[tuple[str, str]]:
    """返回 [(label, url)]。label 用于按端点归并统计。"""
    reqs: list[tuple[str, str]] = []
    libs = ids["libraries"]
    lib_guids = [guid_of("library", x["id"]) for x in libs]

    reqs.append(("UserViews", "/UserViews"))
    # Latest：50 次（每库 ~4 次 + 全局若干），legacy 与新路由各半
    for round_i in range(5):
        for g in lib_guids:
            path = "/Users/{uid}/Items/Latest" if round_i % 2 else "/Items/Latest"
            reqs.append(("Items/Latest",
                         f"{path}?parentId={g}&limit=20&groupItems=true"
                         f"&enableUserData=true&fields={LIST_FIELDS}"))
    reqs.append(("Shows/NextUp", "/Shows/NextUp?limit=20&fields=" + LIST_FIELDS))
    for _ in range(3):
        reqs.append(("Resume",
                     "/UserItems/Resume?limit=12&mediaTypes=Video"
                     f"&fields={LIST_FIELDS}"))

    # /Items 库分页：79 次
    n = 0
    while n < 79:
        for x, g in zip(libs, lib_guids, strict=True):
            for start in (0, 100):
                if n >= 79:
                    break
                types = "Movie" if x["kind"] == "movie" else "Series"
                reqs.append(("Items",
                             f"/Items?parentId={g}&includeItemTypes={types}"
                             f"&startIndex={start}&limit=100&sortBy=SortName"
                             f"&sortOrder=Ascending&fields={LIST_FIELDS}"))
                n += 1

    # Shows/{id}/Seasons：107 次，只涉及 6 部剧（生产实测的重复度）
    hot = ids["hot_series"]
    weights = [44, 33, 13, 6, 6, 5]
    for mid, w in zip(hot, weights, strict=False):
        for _ in range(w):
            reqs.append(("Shows/Seasons",
                         f"/Shows/{guid_of('item', mid)}/Seasons"
                         "?enableUserData=true&enableImages=true"))
    # 剧集下钻的几种形态：整剧 / 按季 / 分页；以及剧→季、季→集两级 parentId
    top = guid_of("item", hot[0])
    reqs.append(("Shows/Episodes", f"/Shows/{top}/Episodes?fields={LIST_FIELDS}"))
    reqs.append(("Shows/Episodes",
                 f"/Shows/{top}/Episodes?limit=50&startIndex=0&fields={LIST_FIELDS}"))
    for series in hot[:3]:
        g = guid_of("item", series)
        reqs.append(("Items(剧→季)",
                     f"/Items?parentId={g}&fields={LIST_FIELDS}"))
        for season in (1, 2):
            sg = guid_of("season", series, season)
            reqs.append(("Shows/Episodes(按季)",
                         f"/Shows/{g}/Episodes?seasonId={sg}&fields={LIST_FIELDS}"))
            reqs.append(("Items(季→集)",
                         f"/Items?parentId={sg}&fields={LIST_FIELDS}"))
    # 排序/搜索口径矩阵：DateCreated 与 Runtime 必须回落到整行装载，
    # searchTerm + Episode 同理——这几条专门守着两段式装载的判定条件
    for x, g in list(zip(libs, lib_guids, strict=True))[:4]:
        for sort in ("DateCreated", "Runtime", "PremiereDate", "CommunityRating"):
            reqs.append((f"Items(sortBy={sort})",
                         f"/Items?parentId={g}&startIndex=0&limit=40"
                         f"&sortBy={sort}&sortOrder=Descending&fields={LIST_FIELDS}"))
        if x["kind"] == "tv":
            reqs.append(("Items(递归 Episode)",
                         f"/Items?parentId={g}&includeItemTypes=Episode"
                         f"&recursive=true&startIndex=0&limit=40&sortBy=SortName"
                         f"&fields={LIST_FIELDS}"))
            reqs.append(("Items(搜索)",
                         f"/Items?parentId={g}&includeItemTypes=Episode,Series"
                         f"&recursive=true&searchTerm=a&fields={LIST_FIELDS}"))
        reqs.append(("Items(筛选)",
                     f"/Items?parentId={g}&startIndex=0&limit=40&isPlayed=false"
                     f"&fields={LIST_FIELDS}"))
    reqs.append(("Items(ids=)",
                 "/Items?ids=" + ",".join(guid_of("item", i)
                                          for i in ids["all_items"][:12])
                 + f"&fields={LIST_FIELDS}"))

    # 条目详情：49 次（全字段分支）
    detail = ids["hot_series"] + ids["all_items"][:43]
    for mid in detail[:49]:
        reqs.append(("Items/{id}", f"/Items/{guid_of('item', mid)}"))
    return reqs


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=3, help="脚本重复轮数，取中位数")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="并发客户端数。播放器扫描是并发打进来的，串行跑量不出"
                         "排队与写锁争用——要复现生产上那条「越打越慢」的曲线就调大它")
    ap.add_argument("--dump", help="把响应快照写到该文件")
    ap.add_argument("--compare", help="与该快照逐字节比对（红线：不允许有差异）")
    ap.add_argument("--keep-db", help="把生成的 mock 库留在该路径，便于单独 profile")
    ap.add_argument("--db", help="不生成 mock，改用这个现成的库（如生产库快照）"
                                 "；会拷贝一份到临时目录，原文件只读不改")
    ap.add_argument("--profile", metavar="LABEL", nargs="?", const="",
                    help="对（可选：指定端点的）请求做 cProfile，打印热点函数")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="jf-bench-"))
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp / 'jf.db'}"
    os.environ["SECRET_KEY_FILE"] = str(tmp / ".secret_key")
    os.environ["SCHEDULER_ENABLED"] = "false"
    os.environ["METADATA_DIR"] = str(tmp / "metadata")
    os.environ["LOG_DIR"] = str(tmp / "logs")
    os.environ["APP_ACCESS_LOG_ENABLED"] = "false"
    os.environ["APP_LOG_LEVEL"] = "WARNING"
    os.environ.setdefault("TMDB_API_KEY", "bench")

    from movieclaw_api.core.config import get_settings
    from movieclaw_db.engine import dispose_db, init_db
    from movieclaw_db.migrations import run_migrations
    get_settings.cache_clear()

    async def _migrate() -> None:
        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        await dispose_db()

    t0 = time.perf_counter()
    if args.db:
        # 真实库模式：拷一份出来跑（原文件一个字节都不动），密码重置成已知值
        import shutil
        shutil.copy(args.db, tmp / "jf.db")
        # 清掉全部应用配置行：其中的敏感字段是用生产主密钥加密的，本机没有
        # 那把钥匙（也不该有）。配置内核对缺失记录返回默认值（"空库也能启动"
        # 是架构红线），所以清空之后照常起，媒体库数据一行不动——压测要的
        # 就是媒体数据的真实形态。
        wipe = sqlite3.connect(str(tmp / "jf.db"))
        wipe.execute("DELETE FROM app_setting")
        wipe.execute("DELETE FROM site_credential")
        wipe.execute("DELETE FROM site_cookie")
        wipe.execute("DELETE FROM llm_provider")
        wipe.execute("DELETE FROM downloader_client")
        wipe.execute("DELETE FROM jellyfin_device")
        wipe.execute("DELETE FROM channel_account")
        wipe.execute("DELETE FROM agent_session")
        wipe.commit()
        wipe.close()
        asyncio.run(_migrate())
        ids = describe_dataset(str(tmp / "jf.db"))
        print(f"真实库就绪（{time.perf_counter() - t0:.1f}s，来自 {args.db}）："
              f"{len(ids['libraries'])} 个库 / {ids['counts']['items']} 条目 / "
              f"{ids['counts']['files']} 台账行 / {ids['counts']['episodes']} 集 / "
              f"{ids['counts']['seasons']} 季")
    else:
        asyncio.run(_migrate())
        ids = seed_dataset(str(tmp / "jf.db"))
        print(f"mock 数据就绪（{time.perf_counter() - t0:.1f}s）："
              f"{len(ids['libraries'])} 个库 / {ids['counts']['items']} 条目 / "
              f"{ids['counts']['files']} 台账行 / {ids['counts']['episodes']} 集 / "
              f"{ids['counts']['seasons']} 季")
    if args.keep_db:
        import shutil
        shutil.copy(tmp / "jf.db", args.keep_db)
        print(f"mock 库已复制到 {args.keep_db}")

    from fastapi.testclient import TestClient

    from movieclaw_api.app import create_app
    from movieclaw_jellyfin.ids import item_guid, library_guid, season_guid

    def guid_of(kind: str, entity_id: int, season: int = 0) -> str:
        if kind == "library":
            return library_guid(entity_id)
        if kind == "season":
            return season_guid(entity_id, season)
        return item_guid(entity_id)

    app = create_app()
    with TestClient(app) as client:
        r = client.post("/api/v1/auth/bootstrap", json=ADMIN)
        assert r.status_code == 200, r.text
        username = ADMIN["username"]
        r = client.post("/Users/AuthenticateByName",
                        json={"Username": username, "Pw": ADMIN["password"]},
                        headers={"Authorization": AUTH_HEADER})
        assert r.status_code == 200, r.text
        token = r.json()["AccessToken"]
        uid = r.json()["User"]["Id"]
        # ServerId 是每个实例首启随机生成的，会让逐字节比对全线报假阳性；
        # 落快照前统一替换成占位符
        server_id = client.get("/System/Info/Public").json()["Id"]
        headers = {"Authorization": f'{AUTH_HEADER}, Token="{token}"'}

        script = [(lbl, url.replace("{uid}", uid)) for lbl, url in
                  build_script(ids, guid_of)]
        print(f"请求脚本：{len(script)} 个请求")

        # 预热一轮（填 lru_cache、编译 SQL），不计入统计
        for _, url in script:
            client.get(url, headers=headers)

        if args.profile is not None:
            import cProfile
            import io as _io
            import pstats
            subset = [(label, u) for label, u in script
                      if not args.profile or label == args.profile]
            print(f"cProfile：{len(subset)} 个请求"
                  f"（{args.profile or '全部端点'}）")
            pr = cProfile.Profile()
            pr.enable()
            for _, url in subset:
                client.get(url, headers=headers)
            pr.disable()
            buf = _io.StringIO()
            st = pstats.Stats(pr, stream=buf).sort_stats("tottime")
            st.print_stats(18)
            st.sort_stats("cumulative").print_stats("movieclaw", 22)
            print(buf.getvalue())
            return 0

        rounds: list[dict] = []
        snapshot: dict[str, list] = {}
        pool = (ThreadPoolExecutor(max_workers=args.concurrency)
                if args.concurrency > 1 else None)
        for r_i in range(args.repeat):
            per_label: dict[str, list[float]] = defaultdict(list)

            def one(job):
                i, label, url = job
                s = time.perf_counter()
                resp = client.get(url, headers=headers)
                return i, label, (time.perf_counter() - s) * 1000, resp

            jobs = [(i, lbl, url) for i, (lbl, url) in enumerate(script)]
            wall0 = time.perf_counter()
            results = (list(pool.map(one, jobs)) if pool
                       else [one(j) for j in jobs])
            wall = (time.perf_counter() - wall0) * 1000
            for i, label, ms, resp in results:
                per_label[label].append(ms)
                if r_i == 0:
                    snapshot[f"{i:04d} {script[i][1]}"] = [
                        resp.status_code,
                        resp.text.replace(server_id, "<SERVER_ID>"),
                    ]
            rounds.append({"wall": wall, "per_label": dict(per_label)})
        if pool is not None:
            pool.shutdown()

    # —— 汇总 ——
    walls = [x["wall"] for x in rounds]
    best = min(range(len(rounds)), key=lambda i: walls[i])
    per = rounds[best]["per_label"]
    print()
    print(f"{'端点':<22}{'次数':>6}{'累计 ms':>11}{'均值 ms':>10}{'P95 ms':>9}")
    print("-" * 58)
    total = 0.0
    for label in sorted(per, key=lambda k: -sum(per[k])):
        v = sorted(per[label])
        s = sum(v)
        total += s
        p95 = v[min(len(v) - 1, int(len(v) * 0.95))]
        print(f"{label:<22}{len(v):>6}{s:>11.1f}{s / len(v):>10.1f}{p95:>9.1f}")
    print("-" * 58)
    print(f"{'合计':<22}{sum(len(v) for v in per.values()):>6}{total:>11.1f}")
    print(f"\n墙钟：{[round(w) for w in walls]} ms  → 最快一轮 {min(walls):.0f} ms"
          f"（中位 {statistics.median(walls):.0f} ms）")

    if args.dump:
        Path(args.dump).write_text(json.dumps(snapshot, ensure_ascii=False),
                                   encoding="utf-8")
        print(f"响应快照已写入 {args.dump}（{len(snapshot)} 条）")

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        diffs = []
        for k in sorted(set(base) | set(snapshot)):
            a, b = base.get(k), snapshot.get(k)
            if a != b:
                diffs.append(k)
        if diffs:
            print(f"\n❌ 响应不一致：{len(diffs)}/{len(snapshot)} 条")
            for k in diffs[:5]:
                a, b = base.get(k), snapshot.get(k)
                print(f"  - {k}")
                if a and b and a[0] != b[0]:
                    print(f"    状态码 {a[0]} → {b[0]}")
                elif a and b:
                    for pos in range(min(len(a[1]), len(b[1]))):
                        if a[1][pos] != b[1][pos]:
                            lo = max(0, pos - 60)
                            print(f"    第 {pos} 字符起："
                                  f"\n      基线 …{a[1][lo:pos + 80]}…"
                                  f"\n      现在 …{b[1][lo:pos + 80]}…")
                            break
                    else:
                        print(f"    长度 {len(a[1])} → {len(b[1])}")
            return 1
        print(f"\n✅ 响应与基线逐字节一致（{len(snapshot)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
