#!/usr/bin/env python3
"""媒体库性能压测数据生成器（真实 schema、真实体量）。

用途
----
在**已跑过 alembic 迁移**的 SQLite 上直接批量灌入媒体库数据，用于压测
媒体库首页、单库海报墙、条目详情等读路径在真实体量下的表现。

为什么直接写 SQLite 而不走 API/扫描管线：入库管线要真实文件、要 ffprobe、
要打 TMDB，几万条根本跑不完；而压测关心的是**读路径**，只要落库的行与
真实扫描产出的行在列口径上一致（state/media_item_id/season/episode/
audio_streams 三态/added_batch_id …），读路径的成本就与真实部署一致。

生成的形态刻意贴近真实用户：
- 库数量 50~100，大部分是中小库，个别大库 5000~10000 个媒体资源；
- 剧集库按「剧 × 季 × 集」展开，一部剧几十行台账（这是台账行爆炸的真实来源）；
- 标题中英混排（拼音排序键的真实分布）；
- 一部分文件未识别（media_item_id IS NULL）、一部分 missing、一部分未探测
  （audio_streams IS NULL）——这三种状态都被读路径的聚合分支消费。

用法::

    python scripts/perf/seed_library_dataset.py --db data/movieclaw.db
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
from datetime import date, datetime, timedelta

# 固定随机种子：同一份数据集可重复生成，压测前后对比才有意义
SEED = 20260821

NOW = datetime(2026, 8, 21, 9, 0, 0)

# —— 标题素材：中英混排，覆盖拼音各档与 A-Z，贴近真实中文媒体库的分布 ——
CN_HEAD = [
    "长安", "三体", "流浪", "无间", "琅琊", "大明", "沙丘", "白夜", "隐秘",
    "山海", "风起", "赘婿", "觉醒", "漫长", "开端", "狂飙", "繁花", "边水",
    "唐朝", "雪中", "剑来", "凡人", "斗罗", "遮天", "武动", "天官", "魔道",
    "庆余", "余华", "人世", "装台", "对手", "警察", "扫黑", "深海", "满江",
    "封神", "刺杀", "独行", "热辣", "第二", "孤注", "消失", "涉过", "宇宙",
    "银河", "星际", "机动", "钢铁", "进击", "咒术", "间谍", "鬼灭", "海贼",
]
CN_TAIL = [
    "十二时辰", "之诗", "地球", "行者", "榜", "王朝", "救赎", "追凶", "的角落",
    "情", "陇西", "年代", "季", "的季节", "时刻", "人生", "小巷", "迷雾",
    "诡事", "神话", "仙途", "大陆", "记", "赐福", "祖师", "年", "文城",
    "间", "戏台", "特工", "荣耀", "风暴", "潜行", "红河", "英雄", "月亮",
    "第一部", "小说家", "月球", "滚烫", "十条", "的她", "怒海", "探索",
    "护卫队", "穿越", "战士", "侠", "巨人", "回战", "过家家", "之刃", "王",
]
EN_TITLES = [
    "Dune", "Oppenheimer", "Interstellar", "Arrival", "Blade Runner",
    "The Expanse", "Severance", "Fallout", "Andor", "Foundation",
    "Chernobyl", "Succession", "Yellowstone", "Ozark", "Mindhunter",
    "Westworld", "Silo", "Shogun", "Reacher", "Bosch", "Barry",
    "Zootopia", "Inception", "Tenet", "Gravity", "Prometheus", "Alien",
    "Kaiju", "Nope", "Sinners", "Wicked", "Furiosa", "Napoleon", "Maestro",
    "Poor Things", "Anatomy of a Fall", "Past Lives", "Aftersun", "Tar",
]

RESOLUTIONS = ["2160p", "1080p", "1080p", "1080p", "720p"]
CODECS = ["hevc", "h264", "h264", "av1"]
HDRS = [None, None, None, "HDR10", "Dolby Vision", "HLG"]
CONTAINERS = ["mkv", "mkv", "mkv", "mp4"]
SOURCES = ["WEB-DL", "Blu-ray", "Remux", "HDTV", "WEBRip"]
GROUPS = ["FRDS", "CMCT", "HDS", "NTb", "playWEB", "ADWeb", "MTeam", None]
STATUSES_TV = ["Returning Series", "Ended", "Canceled", "In Production"]
GENRE_POOL = [
    (18, "剧情"), (28, "动作"), (878, "科幻"), (16, "动画"), (35, "喜剧"),
    (53, "惊悚"), (80, "犯罪"), (10765, "Sci-Fi & Fantasy"), (99, "纪录"),
]

# —— 库规模档位 ————————————————————————————————————————————————
# (库名前缀, kind, 库数量, 每库媒体资源数区间)
# 「媒体资源数」= 台账文件行数：电影 1 部 1 行，剧集 1 集 1 行——与用户在
# 界面上看到的「文件数」口径一致。
LIBRARY_PLAN = [
    # 个别巨型库：任务要求的 5000~10000 资源
    ("电影库·主库", "movie", 1, (9500, 9500)),
    ("剧集库·主库", "tv", 1, (9200, 9200)),
    ("动漫库·主库", "tv", 1, (7600, 7600)),
    ("电影库·4K 收藏", "movie", 1, (5400, 5400)),
    ("纪录片库·主库", "movie", 1, (5100, 5100)),
    # 中型库
    ("剧集库", "tv", 8, (600, 1600)),
    ("电影库", "movie", 10, (400, 1500)),
    # 长尾小库（真实用户按题材/来源/家庭成员分的库）
    ("专题电影库", "movie", 30, (40, 400)),
    ("专题剧集库", "tv", 26, (40, 400)),
]


def _title(rng: random.Random, used: set[str]) -> str:
    for _ in range(60):
        if rng.random() < 0.22:
            base = rng.choice(EN_TITLES)
            title = base if rng.random() < 0.5 else f"{base} {rng.randint(2, 9)}"
        else:
            title = rng.choice(CN_HEAD) + rng.choice(CN_TAIL)
            if rng.random() < 0.25:
                title += f"第{rng.choice('二三四五六七八九')}季"
        if title not in used:
            used.add(title)
            return title
    # 兜底：加序号保证全局唯一
    title = f"{rng.choice(CN_HEAD)}{rng.choice(CN_TAIL)}·{len(used)}"
    used.add(title)
    return title


def _dt(rng: random.Random, days_back: int = 900) -> datetime:
    return NOW - timedelta(
        days=rng.randint(0, days_back), seconds=rng.randint(0, 86_400)
    )


def seed(db_path: str) -> None:
    rng = random.Random(SEED)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")   # 灌数据期间关同步，快一个数量级
    conn.execute("PRAGMA foreign_keys=OFF")

    started = time.perf_counter()
    used_titles: set[str] = set()
    tmdb_seq = 100_000

    libraries: list[tuple] = []
    lib_id = 0
    sort_order = 0
    for prefix, kind, count, (lo, hi) in LIBRARY_PLAN:
        for index in range(count):
            lib_id += 1
            sort_order += 1
            name = prefix if count == 1 else f"{prefix} {index + 1:02d}"
            target_files = rng.randint(lo, hi)
            libraries.append((lib_id, name, kind, sort_order, target_files))

    print(f"计划：{len(libraries)} 个媒体库，"
          f"目标台账行 {sum(l[4] for l in libraries):,}")

    item_rows: list[tuple] = []
    season_rows: list[tuple] = []
    episode_rows: list[tuple] = []
    metadata_rows: list[tuple] = []
    file_rows: list[tuple] = []
    library_rows: list[tuple] = []

    item_id = 0
    file_id = 0

    for lib_id, name, kind, sort_order, target_files in libraries:
        root = f"/media/{kind}/{lib_id:03d}"
        produced_files = 0
        item_count = 0
        episode_count = 0
        total_size = 0
        unidentified = 0
        missing = 0
        ignored = 0

        while produced_files < target_files:
            item_id += 1
            tmdb_seq += 1
            title = _title(rng, used_titles)
            year = rng.randint(1998, 2026)
            created = _dt(rng)
            poster = f"/p{item_id % 9999:04d}poster.jpg"
            item_rows.append((
                item_id, kind, tmdb_seq,
                f"tt{7_000_000 + item_id}",
                str(30_000_000 + item_id) if rng.random() < 0.6 else None,
                title, title, year, "[]",
                "Released" if kind == "movie" else rng.choice(STATUSES_TV),
                poster, f"/b{item_id % 9999:04d}back.jpg",
                created, created,
            ))
            genres = rng.sample(GENRE_POOL, k=rng.randint(1, 3))
            metadata_rows.append((
                item_id,
                "这是一段用于压测的作品简介文本，长度与真实刮削结果相当。" * 3,
                "压测宣传语",
                json.dumps([g[1] for g in genres], ensure_ascii=False),
                json.dumps([g[0] for g in genres]),
                rng.choice([90, 100, 120, 45, 24]),
                str(date(year, rng.randint(1, 12), rng.randint(1, 28))),
                "zh-CN",
                json.dumps(["CN"] if rng.random() < 0.6 else ["US"]),
                json.dumps(["压测影业"], ensure_ascii=False),
                round(rng.uniform(5.0, 9.5), 1), rng.randint(50, 20_000),
                json.dumps(["某导演"], ensure_ascii=False),
                json.dumps(
                    [{"name": f"演员{i}", "character": f"角色{i}", "order": i,
                      "profile_path": f"/a{i}.jpg"} for i in range(12)],
                    ensure_ascii=False,
                ),
                # 一半条目有本地海报资产（走 /images/assets 分支），一半回落 TMDB
                f"posters/{item_id % 500}/{item_id}.jpg" if item_id % 2 == 0 else None,
                None, created, "zh-CN", created, created,
            ))

            batch = f"batch-{lib_id}-{item_id % 40}"
            if kind == "movie":
                item_count += 1
                file_id += 1
                produced_files += 1
                size = rng.randint(2, 60) * 1024 * 1024 * 1024
                total_size += size
                file_rows.append(_file_row(
                    rng, file_id, lib_id, item_id, 0, 0,
                    f"{root}/{title} ({year})/{title}.{rng.choice(CONTAINERS)}",
                    size, created, batch,
                ))
            else:
                item_count += 1
                seasons = rng.randint(1, 3)
                for season_number in range(1, seasons + 1):
                    eps = rng.choice([12, 12, 16, 20, 24, 26, 36])
                    season_rows.append((
                        item_id, season_number, f"第 {season_number} 季",
                        str(date(min(year + season_number - 1, 2026),
                                 rng.randint(1, 12), rng.randint(1, 28))),
                        eps, "季简介", f"/s{season_number}.jpg", None,
                        created, created,
                    ))
                    for episode_number in range(1, eps + 1):
                        air = date(min(year + season_number - 1, 2026),
                                   rng.randint(1, 12), rng.randint(1, 28))
                        episode_rows.append((
                            item_id, season_number, episode_number,
                            f"第 {episode_number} 集",
                            "分集简介，压测用。" * 4, str(air),
                            45, round(rng.uniform(6, 9), 1),
                            f"/still{episode_number}.jpg", None, created, created,
                        ))
                        # 库存并非全集齐：真实库常缺几集，缺集统计的分支要被走到
                        if rng.random() < 0.12:
                            continue
                        if produced_files >= target_files:
                            continue
                        file_id += 1
                        produced_files += 1
                        episode_count += 1
                        size = rng.randint(300, 4000) * 1024 * 1024
                        total_size += size
                        path = (f"{root}/{title}/Season {season_number}/"
                                f"{title} S{season_number:02d}E{episode_number:02d}.mkv")
                        file_rows.append(_file_row(
                            rng, file_id, lib_id, item_id,
                            season_number, episode_number, path, size, created, batch,
                        ))

        # 每库掺入未识别 / 已忽略 / 缺失文件——三种状态都在读路径上有分支
        extra = max(4, produced_files // 60)
        for n in range(extra):
            file_id += 1
            kind_of_extra = rng.random()
            size = rng.randint(1, 20) * 1024 * 1024 * 1024
            path = f"{root}/_未整理/未知内容_{lib_id}_{n}.mkv"
            if kind_of_extra < 0.6:
                unidentified += 1
                file_rows.append(_unidentified_row(rng, file_id, lib_id, path, size))
            elif kind_of_extra < 0.8:
                ignored += 1
                file_rows.append(_unidentified_row(
                    rng, file_id, lib_id, path, size, ignored=True))
            else:
                missing += 1
                file_rows.append(_unidentified_row(
                    rng, file_id, lib_id, path, size, missing=True))

        library_rows.append((
            lib_id, name, kind, json.dumps([root]),
            1 if lib_id in (1, 2) else 0, sort_order,
            item_count, episode_count, produced_files + unidentified + ignored,
            total_size, unidentified, missing, ignored, NOW, NOW, NOW,
        ))
        print(f"  [{lib_id:>3}] {name:<22} {kind:<5} 条目 {item_count:>5} "
              f"文件 {produced_files:>6}")

    print(f"\n生成完毕（内存）：item={len(item_rows):,} file={len(file_rows):,} "
          f"season={len(season_rows):,} episode={len(episode_rows):,} "
          f"耗时 {time.perf_counter() - started:.1f}s\n开始写库……")

    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO library (id,name,kind,root_paths,is_default,sort_order,"
        "stats_item_count,stats_episode_count,stats_file_count,"
        "stats_total_size_bytes,stats_unidentified_count,stats_missing_count,"
        "stats_ignored_count,stats_refreshed_at,created_at,updated_at,"
        "match_rules,write_media_assets,auto_clear_missing,realtime_watch) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',1,0,1)",
        library_rows,
    )
    cur.executemany(
        "INSERT INTO media_item (id,kind,tmdb_id,imdb_id,douban_id,title,"
        "original_title,year,aliases,status,poster_path,backdrop_path,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        item_rows,
    )
    cur.executemany(
        "INSERT INTO media_metadata (media_item_id,overview,tagline,genres,"
        "genre_ids,runtime_minutes,release_date,original_language,"
        "origin_countries,studios,vote_average,vote_count,directors,\"cast\","
        "poster_file,backdrop_file,scraped_at,scrape_language,created_at,"
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        metadata_rows,
    )
    cur.executemany(
        "INSERT INTO media_season (media_item_id,season_number,name,air_date,"
        "episode_count,overview,poster_path,poster_file,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        season_rows,
    )
    cur.executemany(
        "INSERT INTO media_episode (media_item_id,season_number,episode_number,"
        "name,overview,air_date,runtime_minutes,vote_average,still_path,"
        "still_file,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        episode_rows,
    )
    cur.executemany(
        "INSERT INTO library_file (id,library_id,media_item_id,season_number,"
        "episode_number,file_path,size_bytes,container,resolution,video_codec,"
        "hdr,bit_depth,duration_seconds,bit_rate,frame_rate,color_space,"
        "media_source,release_group,source,state,missing_since,ignored_at,"
        "unidentified_reason,unidentified_code,audio_streams,subtitle_streams,"
        "external_subtitles,added_batch_id,identity_source,resolved_version,"
        "file_mtime_ns,created_at,updated_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        file_rows,
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    print(f"写库完成，总耗时 {time.perf_counter() - started:.1f}s")


_AUDIO = json.dumps([
    {"codec": "eac3", "profile": None, "channels": 6,
     "channel_layout": "5.1", "language": "zho", "title": "国语",
     "default": True},
    {"codec": "aac", "profile": "LC", "channels": 2,
     "channel_layout": "stereo", "language": "eng", "title": "English",
     "default": False},
], ensure_ascii=False)
_SUBS = json.dumps([
    {"codec": "subrip", "language": "zho", "title": "简体",
     "forced": False, "default": True},
], ensure_ascii=False)


def _file_row(rng, file_id, lib_id, item_id, season, episode, path, size,
              created, batch) -> tuple:
    """已识别的在位台账行。约 15% 未探测介质规格（audio_streams IS NULL）——
    这是海报墙「待补探」分支与 probing 排序的输入。"""
    unprobed = rng.random() < 0.15
    return (
        file_id, lib_id, item_id, season, episode, path, size,
        rng.choice(CONTAINERS),
        None if unprobed else rng.choice(RESOLUTIONS),
        None if unprobed else rng.choice(CODECS),
        None if unprobed else rng.choice(HDRS),
        None if unprobed else rng.choice([8, 10]),
        None if unprobed else rng.randint(1200, 9000),
        None if unprobed else rng.randint(2_000_000, 80_000_000),
        None if unprobed else rng.choice([23.976, 24.0, 25.0, 29.97]),
        None if unprobed else rng.choice(["BT.709", "BT.2020"]),
        rng.choice(SOURCES), rng.choice(GROUPS),
        rng.choice(["imported", "scanned"]),
        "in_place", None, None, None, None,
        None if unprobed else _AUDIO,
        None if unprobed else _SUBS,
        "[]", batch, "resolved", 3,
        int(created.timestamp() * 1_000_000_000),
        created, created,
    )


def _unidentified_row(rng, file_id, lib_id, path, size, *,
                      ignored=False, missing=False) -> tuple:
    created = _dt(rng)
    return (
        file_id, lib_id, None, 0, 0, path, size, "mkv",
        None, None, None, None, None, None, None, None,
        None, None, "scanned",
        "missing" if missing else "in_place",
        created if missing else None,
        created if ignored else None,
        None if (ignored or missing) else "文件名里解析不出可用的片名",
        None if (ignored or missing) else "unparsable",
        None, None, None, None, None, None,
        int(created.timestamp() * 1_000_000_000), created, created,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/movieclaw.db")
    seed(parser.parse_args().db)
