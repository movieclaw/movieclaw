"""终验发现缺陷的回归测试（对照修复清单，防止翻案倒退）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jellyfin.helpers import jf_login
from movieclaw_jellyfin.ids import episode_guid, item_guid, library_guid, season_guid

TICKS_PER_MS = 10_000


def test_pascalcase_query_params(client: TestClient, seeded: dict) -> None:
    """致命缺陷#1：PascalCase 客户端的 query 键必须被归一化。"""
    token = jf_login(client)
    body = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "ParentId": library_guid(seeded["tv_lib"]),  # 大写 P
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "SortBy": "ParentIndexNumber,IndexNumber",
            "Limit": "2",
        },
    ).json()
    assert body["TotalRecordCount"] == 3
    assert len(body["Items"]) == 2
    assert body["Items"][0]["Type"] == "Episode"

    # Static/MediaSourceId 大小写混写也必须能播
    guid = item_guid(seeded["movie"])
    info = client.post(f"/Items/{guid}/PlaybackInfo", params={"apikey": token}).json()
    local = next(s for s in info["MediaSources"] if s["Protocol"] == "File")
    resp = client.get(
        f"/Videos/{guid}/stream",
        params={"APIKEY": token, "Static": "true", "MediaSourceID": local["Id"]},
    )
    assert resp.status_code == 200


def test_stopped_without_position_marks_played(client: TestClient, seeded: dict) -> None:
    """缺陷#2：Stopped 不带 PositionTicks = 播到结尾，标已看且不清别人的进度。"""
    token = jf_login(client)
    auth = {"ApiKey": token}
    ep = episode_guid(seeded["show"], 2, 1)
    assert (
        client.post(
            "/Sessions/Playing/Stopped", params=auth, json={"ItemId": ep}
        ).status_code
        == 204
    )
    ud = client.get(f"/Items/{ep}", params=auth).json()["UserData"]
    assert ud["Played"] is True and ud["PlaybackPositionTicks"] == 0


def test_progress_zero_clears_resume_point(client: TestClient, seeded: dict) -> None:
    """拖回开头（position=0）要抹掉续播点，已看状态不变。"""
    token = jf_login(client)
    auth = {"ApiKey": token}
    ep = episode_guid(seeded["show"], 1, 1)
    runtime_ticks = 47 * 60 * 1000 * TICKS_PER_MS
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": ep, "PositionTicks": runtime_ticks // 2},
    )
    assert client.get("/UserItems/Resume", params=auth).json()["TotalRecordCount"] == 1
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": ep, "PositionTicks": 0},
    )
    assert client.get("/UserItems/Resume", params=auth).json()["TotalRecordCount"] == 0


def test_series_favorite_visible_on_folder(client: TestClient, seeded: dict) -> None:
    """缺陷#7：收藏整剧/整季要在对应条目的 UserData 回显，且不污染 S00E00。"""
    token = jf_login(client)
    auth = {"ApiKey": token}
    show = item_guid(seeded["show"])
    season1 = season_guid(seeded["show"], 1)

    resp = client.post(f"/UserFavoriteItems/{show}", params=auth).json()
    assert resp["IsFavorite"] is True
    assert "UnplayedItemCount" in resp  # 文件夹形态的聚合响应
    assert client.get(f"/Items/{show}", params=auth).json()["UserData"]["IsFavorite"] is True

    assert client.post(f"/UserFavoriteItems/{season1}", params=auth).json()["IsFavorite"] is True
    seasons = client.get(f"/Shows/{show}/Seasons", params=auth).json()["Items"]
    by_index = {s["IndexNumber"]: s for s in seasons}
    assert by_index[1]["UserData"]["IsFavorite"] is True
    assert by_index[2]["UserData"]["IsFavorite"] is False


def test_sort_date_created_without_fields(client: TestClient, seeded: dict) -> None:
    """缺陷#3：sortBy=DateCreated 不带 fields 也必须生效（排序键取自数据）。"""
    token = jf_login(client)
    body = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "parentId": library_guid(seeded["tv_lib"]),
            "includeItemTypes": "Episode",
            "sortBy": "DateCreated",
            "sortOrder": "Descending",
        },
    ).json()
    assert body["TotalRecordCount"] == 3
    # 不带 fields=DateCreated 时 DTO 里没有该字段，但顺序依然确定
    assert "DateCreated" not in body["Items"][0]


def test_users_bad_guid_is_400_not_401(client: TestClient) -> None:
    """终验1#1：非法 userId 是 400，绝不能 401（会触发客户端登录循环）。"""
    token = jf_login(client)
    resp = client.get("/Users/not-a-guid", params={"ApiKey": token})
    assert resp.status_code == 400
    resp = client.get(f"/Users/{'0' * 32}", params={"ApiKey": token})
    assert resp.status_code == 404


def test_emby_prefix_case_insensitive(client: TestClient) -> None:
    """终验1#3：/Emby/... 也要归一化命中。"""
    assert client.get("/Emby/System/Info/Public").status_code == 200


def test_library_view_parent_id_navigable(client: TestClient, seeded: dict) -> None:
    """终验#13：库视图 ParentId 指向根，且拿它回打 /Items 能得到视图列表。"""
    token = jf_login(client)
    views = client.get("/UserViews", params={"ApiKey": token}).json()["Items"]
    parent = views[0]["ParentId"]
    body = client.get("/Items", params={"ApiKey": token, "parentId": parent}).json()
    assert body["TotalRecordCount"] == 2  # 根 → 视图列表


def test_episode_parent_id_gated_by_fields(client: TestClient, seeded: dict) -> None:
    """ParentId 受 fields 门控：不传不出现，传了指向季。"""
    token = jf_login(client)
    ep = episode_guid(seeded["show"], 1, 1)
    plain = client.get(
        "/Items",
        params={"ApiKey": token, "ids": ep},
    ).json()["Items"][0]
    assert "ParentId" not in plain
    with_field = client.get(
        "/Items",
        params={"ApiKey": token, "ids": ep, "fields": "ParentId"},
    ).json()["Items"][0]
    assert with_field["ParentId"] == season_guid(seeded["show"], 1)


def test_nextup_anchor_is_latest_activity(client: TestClient, seeded: dict) -> None:
    """缺陷#5：NextUp 锚定最近活动，而非很久前弃坑的半集。"""
    token = jf_login(client)
    auth = {"ApiKey": token}
    e11 = episode_guid(seeded["show"], 1, 1)
    e12 = episode_guid(seeded["show"], 1, 2)
    runtime_ticks = 47 * 60 * 1000 * TICKS_PER_MS
    # 先在 E01 留半截进度（弃坑），再看完 E02
    client.post(
        "/Sessions/Playing/Progress",
        params=auth,
        json={"ItemId": e11, "PositionTicks": runtime_ticks // 2},
    )
    client.post(f"/UserPlayedItems/{e12}", params=auth)
    nextup = client.get("/Shows/NextUp", params=auth).json()
    # 最近活动是"看完 E02" → NextUp 是 S02E01，不是弃坑的 E01
    assert nextup["Items"][0]["Id"] == episode_guid(seeded["show"], 2, 1)


def test_bad_pagination_params_do_not_500(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    resp = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "parentId": library_guid(seeded["movie_lib"]),
            "startIndex": "abc",
            "limit": "xyz",
        },
    )
    assert resp.status_code == 200


def test_default_audio_stream_prefers_default_flag(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    info = client.post(f"/Items/{guid}/PlaybackInfo", params={"ApiKey": token}).json()
    local = next(s for s in info["MediaSources"] if s["Protocol"] == "File")
    default_audio = next(
        s for s in local["MediaStreams"] if s["Type"] == "Audio" and s["IsDefault"]
    )
    assert local["DefaultAudioStreamIndex"] == default_audio["Index"]


def test_library_views_have_counts_and_cover(client: TestClient, seeded: dict) -> None:
    """VidHub 实测缺陷：库卡片要有条目计数与封面（ChildCount + ImageTags.Primary）。"""
    token = jf_login(client)
    views = client.get("/UserViews", params={"ApiKey": token}).json()["Items"]
    by_name = {v["Name"]: v for v in views}

    movie_lib = by_name["电影"]
    assert movie_lib["ChildCount"] == 1  # 一部电影
    # 电影库封面 = 最新入库且有海报的条目（盗梦空间）的海报
    assert movie_lib["ImageTags"].get("Primary")

    tv_lib = by_name["剧集"]
    assert tv_lib["ChildCount"] == 1  # 一部剧
    assert tv_lib["RecursiveItemCount"] == 3  # 三集

    # 封面图能真实拉取（图片接口对 LIBRARY GUID 解析同一封面）
    resp = client.get(
        f"/Items/{movie_lib['Id']}/Images/Primary",
        params={"ApiKey": token, "tag": movie_lib["ImageTags"]["Primary"]},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_items_counts_endpoint(client: TestClient, seeded: dict) -> None:
    """VidHub 实测缺陷：服务器卡片统计来自 GET /Items/Counts。"""
    token = jf_login(client)
    body = client.get("/Items/Counts", params={"ApiKey": token}).json()
    assert body["MovieCount"] == 1
    assert body["SeriesCount"] == 1
    assert body["EpisodeCount"] == 3
    # 12 个计数键全部出现（非可空 int 恒输出）
    for key in (
        "ArtistCount", "ProgramCount", "TrailerCount", "SongCount", "AlbumCount",
        "MusicVideoCount", "BoxSetCount", "BookCount", "ItemCount",
    ):
        assert key in body


def test_jellyfin_counts_read_persisted_library_snapshot(
    client: TestClient, seeded: dict, tmp_path
) -> None:
    """Jellyfin 库卡片与全服统计共用预计算快照。"""
    import sqlite3

    with sqlite3.connect(tmp_path / "jf.db") as connection:
        connection.execute(
            """
            UPDATE library
            SET stats_item_count = 7, stats_episode_count = 0
            WHERE id = ?
            """,
            (seeded["movie_lib"],),
        )
        connection.execute(
            """
            UPDATE library
            SET stats_item_count = 4, stats_episode_count = 22
            WHERE id = ?
            """,
            (seeded["tv_lib"],),
        )

    token = jf_login(client)
    views = client.get("/UserViews", params={"ApiKey": token}).json()["Items"]
    by_name = {view["Name"]: view for view in views}
    assert by_name["电影"]["ChildCount"] == 7
    assert by_name["剧集"]["ChildCount"] == 4
    assert by_name["剧集"]["RecursiveItemCount"] == 22

    counts = client.get("/Items/Counts", params={"ApiKey": token}).json()
    assert counts["MovieCount"] == 7
    assert counts["SeriesCount"] == 4
    assert counts["EpisodeCount"] == 22
    assert counts["ItemCount"] == 33


def test_jellyfin_counts_dedupe_same_series_across_tv_libraries(
    client: TestClient, seeded: dict, tmp_path
) -> None:
    """同一剧跨库时不能盲加每库快照；要保持 Jellyfin 去重口径。"""
    import sqlite3

    with sqlite3.connect(tmp_path / "jf.db") as connection:
        connection.execute(
            """
            INSERT INTO library (
                created_at, updated_at, name, kind, root_paths, is_default,
                stats_item_count, stats_episode_count, stats_file_count,
                stats_total_size_bytes
            ) VALUES (?, ?, ?, 'tv', '[]', 0, 1, 1, 1, 1024)
            """,
            ("2026-08-15", "2026-08-15", "剧集备份库"),
        )
        extra_library_id = connection.execute(
            "SELECT id FROM library WHERE name = '剧集备份库'"
        ).fetchone()[0]
        # 新库收藏的仍是已有剧集的 S01E01；每库快照都是 1，
        # 但全服 SeriesCount 和 EpisodeCount 都不应增加。
        connection.execute(
            """
            INSERT INTO library_file (
                created_at, updated_at, library_id, media_item_id,
                season_number, episode_number, file_path, size_bytes, source
            ) VALUES (?, ?, ?, ?, 1, 1, '/backup/S01E01.mkv', 1024, 'scanned')
            """,
            ("2026-08-15", "2026-08-15", extra_library_id, seeded["show"]),
        )

    token = jf_login(client)
    counts = client.get("/Items/Counts", params={"ApiKey": token}).json()
    assert counts["MovieCount"] == 1
    assert counts["SeriesCount"] == 1
    assert counts["EpisodeCount"] == 3


def test_library_cover_is_server_rendered_collage(client: TestClient, seeded: dict) -> None:
    """库封面 = 服务端渲染的氛围光货架拼贴，Jellyfin 与控制台双端同一张图。"""
    token = jf_login(client)
    views = client.get("/UserViews", params={"ApiKey": token}).json()["Items"]
    movie_lib = next(v for v in views if v["Name"] == "电影")
    tag = movie_lib["ImageTags"]["Primary"]
    assert len(tag) == 32  # 素材指纹

    # Jellyfin 侧：拼贴图可拉取，21:10 画布
    resp = client.get(f"/Items/{movie_lib['Id']}/Images/Primary", params={"ApiKey": token})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(resp.content))
    assert img.size == (1260, 600)

    # 控制台侧：同一张图（session cookie 认证），ETag 304 协商
    console = client.get(f"/api/v1/libraries/{seeded['movie_lib']}/cover")
    assert console.status_code == 200
    assert console.content == resp.content
    revisit = client.get(
        f"/api/v1/libraries/{seeded['movie_lib']}/cover",
        headers={"If-None-Match": console.headers["ETag"]},
    )
    assert revisit.status_code == 304

    # 无海报素材的库：无 Primary tag、封面 404（前端回退 CSS 货架）
    tv_lib = next(v for v in views if v["Name"] == "剧集")
    assert "Primary" not in tv_lib["ImageTags"]


def test_image_scaling_params(client: TestClient, seeded: dict) -> None:
    """maxWidth 等缩放参数生效（fit-within 只缩不放，缓存复用）。"""
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    resp = client.get(
        f"/Items/{guid}/Images/Primary", params={"ApiKey": token, "maxWidth": "50"}
    )
    assert resp.status_code == 200
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(resp.content))
    assert img.width == 50  # 原图 100x150 → 缩到宽 50
    # 不放大：请求超过原尺寸时原样
    big = client.get(
        f"/Items/{guid}/Images/Primary", params={"ApiKey": token, "maxWidth": "4000"}
    )
    assert Image.open(BytesIO(big.content)).width == 100


def test_people_in_item_detail(client: TestClient, seeded: dict) -> None:
    """VidHub 实测缺陷：演职员必须随详情输出（People 字段 + 头像 tag）。"""
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    people = client.get(f"/Items/{guid}", params={"ApiKey": token}).json()["People"]
    assert [p["Name"] for p in people] == [
        "莱昂纳多·迪卡普里奥",
        "艾伦·佩吉",
        "克里斯托弗·诺兰",
    ]  # 演员按主次序在前，导演在后
    leo = people[0]
    assert leo["Type"] == "Actor" and leo["Role"] == "Cobb"
    assert len(leo["PrimaryImageTag"]) == 32
    assert "PrimaryImageTag" not in people[1]  # 无头像的不给 tag
    assert people[2]["Type"] == "Director" and "Role" not in people[2]

    # 列表接口：不传 fields=People 不输出；传了才有（fields 门控）
    plain = client.get("/Items", params={"ApiKey": token, "ids": guid}).json()["Items"][0]
    assert "People" not in plain
    gated = client.get(
        "/Items", params={"ApiKey": token, "ids": guid, "fields": "People"}
    ).json()["Items"][0]
    assert len(gated["People"]) == 3


def test_list_queries_skip_unused_json_columns(client: TestClient, seeded: dict) -> None:
    """大库列表不应读取 DTO 用不到的演员和媒体流 JSON。"""
    from sqlalchemy import event

    from movieclaw_db.engine import get_database

    token = jf_login(client)
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement.lower())

    engine = get_database().engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        plain = client.get(
            "/Items",
            params={
                "ApiKey": token,
                "parentId": library_guid(seeded["movie_lib"]),
                "includeItemTypes": "Movie",
            },
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert plain.status_code == 200
    sql = "\n".join(statements)
    assert "media_metadata.cast" not in sql
    assert "media_metadata.directors" not in sql
    assert "library_file.audio_streams" not in sql
    assert "library_file.subtitle_streams" not in sql

    statements.clear()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        sources = client.get(
            "/Items",
            params={
                "ApiKey": token,
                "parentId": library_guid(seeded["movie_lib"]),
                "includeItemTypes": "Movie",
                "fields": "MediaSources",
            },
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert sources.status_code == 200
    assert len(sources.json()["Items"][0]["MediaSources"]) == 2
    sql = "\n".join(statements)
    assert "library_file.audio_streams" in sql
    assert "library_file.subtitle_streams" in sql

    statements.clear()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        views = client.get("/UserViews", params={"ApiKey": token})
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert views.status_code == 200
    sql = "\n".join(statements)
    assert "media_metadata.poster_file" in sql
    assert "media_metadata.cast" not in sql
    assert "media_metadata.overview" not in sql


def test_large_library_list_skips_large_json_columns(
    client: TestClient, seeded: dict, monkeypatch
) -> None:
    """1,200 部电影携带大 JSON 时，首页仍走最小列集并正确分页。"""
    import json
    import os
    import sqlite3

    from sqlalchemy import event

    from movieclaw_db.engine import get_database

    db_path = os.environ["DATABASE_URL"].split("///", 1)[1]
    timestamp = "2026-08-06 00:00:00.000000"
    large_json = json.dumps(["x" * 2_048])
    item_ids = range(10_000, 11_200)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO media_item
                (id, kind, tmdb_id, source, external_id, title, original_title, aliases,
                 created_at, updated_at)
            VALUES (?, 'movie', ?, 'tmdb', ?, ?, ?, '[]', ?, ?)
            """,
            [
                (
                    item_id,
                    item_id,
                    str(item_id),
                    f"Large library item {item_id}",
                    f"Item {item_id}",
                    timestamp,
                    timestamp,
                )
                for item_id in item_ids
            ],
        )
        conn.executemany(
            """
            INSERT INTO media_metadata
                (media_item_id, genres, genre_ids, origin_countries, studios, directors, cast,
                 poster_locked, backdrop_locked, scrape_language, created_at, updated_at)
            VALUES (?, '[]', '[]', ?, ?, ?, ?, 0, 0, '', ?, ?)
            """,
            [
                (item_id, large_json, large_json, large_json, large_json, timestamp, timestamp)
                for item_id in item_ids
            ],
        )
        conn.executemany(
            """
            INSERT INTO library_file
                (library_id, media_item_id, season_number, episode_number, file_path, size_bytes,
                 audio_streams, subtitle_streams, source, created_at, updated_at)
            VALUES (?, ?, 0, 0, ?, 0, ?, ?, 'scanned', ?, ?)
            """,
            [
                (
                    seeded["movie_lib"],
                    item_id,
                    f"/large-media/item-{item_id}.mkv",
                    large_json,
                    large_json,
                    timestamp,
                    timestamp,
                )
                for item_id in item_ids
            ],
        )

    token = jf_login(client)
    statements: list[str] = []
    loaded_bundle_sizes: list[int] = []

    from movieclaw_jellyfin.routes import library as library_routes

    original_load_bundles = library_routes.load_bundles

    async def track_load_bundles(session, item_ids, **kwargs):
        loaded_bundle_sizes.append(len(item_ids))
        return await original_load_bundles(session, item_ids, **kwargs)

    monkeypatch.setattr(library_routes, "load_bundles", track_load_bundles)

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement.lower())

    engine = get_database().engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get(
            "/Items",
            params={
                "ApiKey": token,
                "parentId": library_guid(seeded["movie_lib"]),
                "includeItemTypes": "Movie",
                "limit": "20",
            },
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert response.status_code == 200
    body = response.json()
    assert body["TotalRecordCount"] == 1_201
    assert len(body["Items"]) == 20
    # 默认电影库 Items 的 count/page 已在 SQL 完成，不能回退成 1,201 个 bundle。
    assert loaded_bundle_sizes == [20]
    sql = "\n".join(statements)
    assert "media_metadata.cast" not in sql
    assert "media_metadata.directors" not in sql
    assert "media_metadata.origin_countries" not in sql
    assert "library_file.audio_streams" not in sql
    assert "library_file.subtitle_streams" not in sql

    statements.clear()
    loaded_bundle_sizes.clear()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        latest = client.get(
            "/Items/Latest",
            params={
                "ApiKey": token,
                "parentId": library_guid(seeded["movie_lib"]),
                "limit": "20",
            },
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert latest.status_code == 200
    assert len(latest.json()) == 20
    # Latest 先扫描轻量最新单元，再只装载最终的 20 个条目。
    assert loaded_bundle_sizes == [20]
    sql = "\n".join(statements)
    assert "media_metadata.cast" not in sql
    assert "media_metadata.directors" not in sql
    assert "media_metadata.origin_countries" not in sql
    assert "library_file.audio_streams" not in sql
    assert "library_file.subtitle_streams" not in sql


def test_browse_index_covers_latest_and_movie_library_page(
    client: TestClient, seeded: dict
) -> None:
    """首页 SQL 只能扫描浏览索引，不能回退读取含流信息 JSON 的宽表。"""
    import os
    import sqlite3

    db_path = os.environ["DATABASE_URL"].split("///", 1)[1]
    latest_plan_sql = """
        EXPLAIN QUERY PLAN
        SELECT
            library_file.media_item_id,
            library_file.season_number,
            library_file.episode_number,
            media_item.kind,
            max(library_file.created_at)
        FROM library_file
        JOIN media_item ON media_item.id = library_file.media_item_id
        WHERE library_file.media_item_id IS NOT NULL
          AND library_file.state = 'in_place'
          AND library_file.library_id = ?
        GROUP BY
            library_file.media_item_id,
            library_file.season_number,
            library_file.episode_number,
            media_item.kind
        ORDER BY
            max(library_file.created_at) DESC,
            min(library_file.id) ASC,
            library_file.media_item_id ASC,
            library_file.season_number ASC,
            library_file.episode_number ASC
    """
    movie_page_plan_sql = """
        EXPLAIN QUERY PLAN
        SELECT count(*)
        FROM media_item
        WHERE media_item.kind = 'movie'
          AND EXISTS (
              SELECT library_file.id
              FROM library_file
              WHERE library_file.library_id = ?
                AND library_file.media_item_id = media_item.id
                AND library_file.season_number = 0
                AND library_file.episode_number = 0
                AND library_file.state = 'in_place'
          )
    """
    with sqlite3.connect(db_path) as conn:
        latest_plan = "\n".join(
            row[3] for row in conn.execute(latest_plan_sql, (seeded["movie_lib"],))
        )
        movie_page_plan = "\n".join(
            row[3] for row in conn.execute(movie_page_plan_sql, (seeded["movie_lib"],))
        )

    assert "USING COVERING INDEX ix_library_file_browse_unit" in latest_plan
    assert "USING COVERING INDEX ix_library_file_browse_unit" in movie_page_plan


def test_home_playback_queries_only_load_active_series(
    client: TestClient, seeded: dict, monkeypatch
) -> None:
    """Resume/NextUp 不能为无播放活动的整库条目加载 bundle。"""
    from movieclaw_jellyfin.routes import library as library_routes

    token = jf_login(client)
    auth = {"ApiKey": token}
    episode = episode_guid(seeded["show"], 1, 1)
    runtime_ticks = 47 * 60 * 1000 * TICKS_PER_MS
    assert (
        client.post(
            "/Sessions/Playing/Progress",
            params=auth,
            json={"ItemId": episode, "PositionTicks": runtime_ticks // 2},
        ).status_code
        == 204
    )

    calls: list[list[int]] = []
    original_load_bundles = library_routes.load_bundles

    async def track_load_bundles(session, item_ids, **kwargs):
        calls.append(list(item_ids))
        return await original_load_bundles(session, item_ids, **kwargs)

    monkeypatch.setattr(library_routes, "load_bundles", track_load_bundles)

    resume = client.get("/UserItems/Resume", params=auth).json()
    assert resume["TotalRecordCount"] == 1
    assert resume["Items"][0]["Id"] == episode
    assert calls == [[seeded["show"]]]

    calls.clear()
    next_up = client.get("/Shows/NextUp", params=auth).json()
    assert next_up["TotalRecordCount"] == 1
    assert next_up["Items"][0]["Id"] == episode
    assert calls == [[seeded["show"]]]


def test_person_item_and_filter(client: TestClient, seeded: dict) -> None:
    """点开演员：人物条目可取，personIds 反查参演作品。"""
    from movieclaw_jellyfin.ids import person_guid

    token = jf_login(client)
    pguid = person_guid(seeded["dicaprio"])
    person = client.get(f"/Items/{pguid}", params={"ApiKey": token}).json()
    assert person["Type"] == "Person"
    assert person["Name"] == "莱昂纳多·迪卡普里奥"
    assert person["OriginalTitle"] == "Leonardo DiCaprio"

    body = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "personIds": pguid,
            "recursive": "true",
            "includeItemTypes": "Movie,Series",
        },
    ).json()
    assert body["TotalRecordCount"] == 1
    assert body["Items"][0]["Id"] == item_guid(seeded["movie"])

    # 头像：测试环境图床离线 → 404 优雅降级（生产为图片代理缓存直出）
    avatar = client.get(f"/Items/{pguid}/Images/Primary", params={"ApiKey": token})
    assert avatar.status_code == 404


def test_browse_never_touches_media_files(client: TestClient, seeded: dict) -> None:
    """issue #88 回归：浏览类请求（列表/详情）不碰媒体文件本体。

    把磁盘上的媒体文件全部删掉后再浏览——若代码仍在请求期 stat 文件或
    现读 strm，这里的形态就会变（ETag 现身/消失、strm 源被剔除）。契约：

    - strm 版本**不现读**：Path 保持 .strm 占位路径、Protocol=Http、
      IsRemote=true，真实直链等 PlaybackInfo 播放协商时现读；
    - 本地版本的 ETag 只来自台账 ``file_mtime_ns``（未回填时省略）。
    """
    import os
    import sqlite3
    from pathlib import Path

    token = jf_login(client)
    guid = item_guid(seeded["movie"])

    db_path = os.environ["DATABASE_URL"].split("///", 1)[1]
    with sqlite3.connect(db_path) as conn:
        paths = [r[0] for r in conn.execute("SELECT file_path FROM library_file")]
    for p in paths:
        Path(p).unlink()

    # 全字段单条目：MediaSources 两个版本俱在，strm 未被解析/剔除
    body = client.get(f"/Items/{guid}", params={"ApiKey": token}).json()
    sources = body["MediaSources"]
    assert len(sources) == 2
    remote = next(s for s in sources if s["Protocol"] == "Http")
    assert remote["Path"].endswith(".strm")  # 占位路径，未现读云端直链
    assert remote["IsRemote"] is True
    local = next(s for s in sources if s["Protocol"] == "File")
    assert "ETag" not in local  # mtime 未回填 → 省略，而不是现场 stat

    # 列表带 fields=MediaSources 同样不碰文件
    listing = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "parentId": library_guid(seeded["movie_lib"]),
            "recursive": "true",
            "includeItemTypes": "Movie",
            "fields": "MediaSources",
        },
    ).json()
    assert len(listing["Items"][0]["MediaSources"]) == 2


def test_media_source_etag_comes_from_ledger(client: TestClient, seeded: dict) -> None:
    """ETag 由台账 file_mtime_ns 派生（扫描时落库），请求期零文件系统调用。"""
    import hashlib
    import os
    import sqlite3

    token = jf_login(client)
    guid = item_guid(seeded["movie"])

    db_path = os.environ["DATABASE_URL"].split("///", 1)[1]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE library_file SET file_mtime_ns = 1723800000123456789 "
            "WHERE file_path LIKE '%.mkv'"
        )
        conn.commit()

    body = client.get(f"/Items/{guid}", params={"ApiKey": token}).json()
    local = next(s for s in body["MediaSources"] if s["Protocol"] == "File")
    assert local["ETag"] == hashlib.md5(b"1723800000123456789").hexdigest()


# ---------------------------------------------------------------------------
# 下载语义审计（对照 DtoService/BaseItem/Video 的 CanDownload 规则）
# ---------------------------------------------------------------------------


def test_can_download_leaf_true_folder_false(client: TestClient, seeded: dict) -> None:
    """CanDownload：Movie/Episode 为 true，Series/Season 恒 false。

    缺失该字段时 VidHub 等客户端退回 Policy.EnableContentDownloading 全局
    放行，在剧集层级也显示下载按钮，打到 /Videos/{seriesGuid}/stream 得到
    404 空 body 存成 0 字节"成品"（三体下载翻车的根因）。
    """
    token = jf_login(client)
    auth = {"ApiKey": token, "fields": "CanDownload,CanDelete"}

    movie = client.get(f"/Items/{item_guid(seeded['movie'])}", params=auth).json()
    assert movie["CanDownload"] is True and movie["CanDelete"] is False

    show = client.get(f"/Items/{item_guid(seeded['show'])}", params=auth).json()
    assert show["CanDownload"] is False

    season = client.get(f"/Items/{season_guid(seeded['show'], 1)}", params=auth).json()
    assert season["CanDownload"] is False

    ep = client.get(f"/Items/{episode_guid(seeded['show'], 1, 1)}", params=auth).json()
    assert ep["CanDownload"] is True


def test_leaf_constant_fields_present(client: TestClient, seeded: dict) -> None:
    """真 Jellyfin 恒输出的字段：LocationType/VideoType/顶层 Container；
    Season 的 ChildCount 不受 fields 门控（DtoService 短路分支）。"""
    token = jf_login(client)
    auth = {"ApiKey": token}

    movie = client.get(f"/Items/{item_guid(seeded['movie'])}", params=auth).json()
    assert movie["LocationType"] == "FileSystem"
    assert movie["VideoType"] == "VideoFile"
    assert movie["Container"] == "mkv"

    season = client.get(f"/Items/{season_guid(seeded['show'], 1)}", params=auth).json()
    assert season["ChildCount"] == 2  # S01 两集，无需 fields 请求

    # 列表路径（最小列集）也必须能输出这些恒有字段，不得触发惰性加载
    listing = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "parentId": library_guid(seeded["movie_lib"]),
            "recursive": "true",
            "includeItemTypes": "Movie",
        },
    ).json()
    assert listing["Items"][0]["Container"] == "mkv"


def test_width_height_ishd_gated(client: TestClient, seeded: dict) -> None:
    """Width/Height/IsHD 按 fields 门控输出，值从标称分辨率派生。"""
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    body = client.get(
        f"/Items/{guid}", params={"ApiKey": token, "fields": "Width,Height,IsHD"}
    ).json()
    assert (body["Width"], body["Height"]) == (3840, 2160)
    assert body["IsHD"] is True


def test_namespace_unknown_route_is_bare_404(client: TestClient) -> None:
    """命名空间内未实现的路径不得漏出业务 JSON 信封（RESOURCE_NOT_FOUND）。"""
    token = jf_login(client)
    resp = client.get("/Videos/whatever/master.m3u8", params={"ApiKey": token})
    assert resp.status_code == 404
    assert resp.content == b""
    resp = client.post("/LiveStreams/Open", params={"ApiKey": token})
    assert resp.status_code in (404, 405)
    assert resp.content == b""


def test_mark_played_survives_missing_files(client: TestClient, seeded: dict) -> None:
    """文件全部丢失的剧仍可手动标记已看（真 Jellyfin 语义：条目在即 200）。"""
    import os
    import sqlite3

    token = jf_login(client)
    db_path = os.environ["DATABASE_URL"].split("///", 1)[1]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE library_file SET missing_since = '2026-08-07 00:00:00', state = 'missing' "
            "WHERE media_item_id = ?",
            (seeded["show"],),
        )
        conn.commit()

    resp = client.post(
        f"/Users/{'0' * 32}/PlayedItems/{item_guid(seeded['show'])}",
        params={"ApiKey": token},
    )
    assert resp.status_code == 200
    assert resp.json()["Played"] is True


def test_stopped_failed_string_true_skips_persistence(
    client: TestClient, seeded: dict
) -> None:
    """Failed 为字符串 "true" 的 Stopped 同样不落库（不得记成正常观看）。"""
    token = jf_login(client)
    ep = episode_guid(seeded["show"], 2, 1)
    resp = client.post(
        "/Sessions/Playing/Stopped",
        params={"ApiKey": token},
        json={"ItemId": ep, "PositionTicks": 66 * 600_000_000, "Failed": "true"},
    )
    assert resp.status_code == 204
    ud = client.get(f"/Items/{ep}", params={"ApiKey": token}).json()["UserData"]
    assert ud["PlaybackPositionTicks"] == 0 and ud["Played"] is False
