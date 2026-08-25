"""Jellyfin 兼容层 HTTP 全链路：发现身份 → 登录 → 浏览 → 播放 → 进度。

断言直接取自设计文档 v1.1 的协议形态（docs/design/jellyfin-compat.md）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jellyfin.helpers import ADMIN, AUTH_HEADER, jf_login
from movieclaw_jellyfin.ids import episode_guid, item_guid, library_guid, season_guid

TICKS_PER_MS = 10_000


# ---------------------------------------------------------------------------
# 身份与认证
# ---------------------------------------------------------------------------


def test_system_info_public_shape(client: TestClient) -> None:
    resp = client.get("/System/Info/Public")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ProductName"] == "Jellyfin Server"
    assert body["Version"].startswith("10.10")
    assert body["StartupWizardCompleted"] is True
    assert body["OperatingSystem"] == ""
    assert len(body["Id"]) == 32
    assert body["LocalAddress"].startswith("http")


def test_system_info_public_uses_forwarded_external_address(client: TestClient) -> None:
    """前端/反代入口不能把内部上游地址作为播放器后续访问地址下发。"""
    resp = client.get(
        "/System/Info/Public",
        headers={
            "Host": "127.0.0.1:8000",
            "X-Forwarded-Host": "192.168.1.50:3000, proxy.internal",
            "X-Forwarded-Proto": "https, http",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["LocalAddress"] == "https://192.168.1.50:3000"


def test_system_ping_returns_product_name_json_string(client: TestClient) -> None:
    for method in (client.get, client.post):
        resp = method("/System/Ping")
        assert resp.status_code == 200
        # 带引号的 JSON 字符串，而非裸文本；内容是产品名不是服务器名
        assert resp.json() == "Jellyfin Server"


def test_login_requires_identity_headers(client: TestClient) -> None:
    resp = client.post(
        "/Users/AuthenticateByName",
        json={"Username": ADMIN["username"], "Pw": ADMIN["password"]},
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/plain")


def test_login_wrong_password_is_401_text(client: TestClient) -> None:
    resp = client.post(
        "/Users/AuthenticateByName",
        json={"Username": ADMIN["username"], "Pw": "wrong"},
        headers={"Authorization": AUTH_HEADER},
    )
    assert resp.status_code == 401
    assert resp.text == "Error processing request."


def test_login_success_shape(client: TestClient) -> None:
    resp = client.post(
        "/Users/AuthenticateByName",
        json={"username": ADMIN["username"], "pw": ADMIN["password"]},  # 键名大小写不敏感
        headers={"Authorization": AUTH_HEADER},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"User", "SessionInfo", "AccessToken", "ServerId"}
    assert len(body["AccessToken"]) == 32
    user = body["User"]
    assert user["Name"] == ADMIN["username"]
    assert len(user["Id"]) == 32 and "-" not in user["Id"]
    policy = user["Policy"]
    assert policy["EnableMediaPlayback"] is True
    assert policy["ForceRemoteSourceTranscoding"] is False
    assert policy["IsHidden"] is False
    assert user["Configuration"]["SubtitleMode"] == "Default"
    session = body["SessionInfo"]
    assert session["PlayableMediaTypes"] == []
    assert session["UserId"] == user["Id"]


def test_relogin_same_device_revokes_old_token(client: TestClient) -> None:
    token1 = jf_login(client)
    token2 = jf_login(client)
    assert token1 != token2
    assert client.get("/Users/Me", params={"ApiKey": token1}).status_code == 401
    assert client.get("/Users/Me", params={"ApiKey": token2}).status_code == 200


def test_admin_password_change_revokes_jellyfin_token(client: TestClient) -> None:
    """超管改密必须吊销播放器里的长期 AccessToken。"""
    token = jf_login(client)
    assert client.get("/Users/Me", params={"ApiKey": token}).status_code == 200

    changed = client.put(
        "/api/v1/auth/password",
        json={"old_password": ADMIN["password"], "new_password": "admin-new-pass-456"},
    )
    assert changed.status_code == 200, changed.text
    assert client.get("/Users/Me", params={"ApiKey": token}).status_code == 401


def test_token_positions(client: TestClient) -> None:
    token = jf_login(client)
    ok_variants = [
        {"headers": {"Authorization": f'MediaBrowser Token="{token}"'}},
        {"headers": {"X-Emby-Token": token}},
        {"headers": {"X-MediaBrowser-Token": token}},
        {"params": {"ApiKey": token}},
        {"params": {"api_key": token}},
    ]
    for kwargs in ok_variants:
        assert client.get("/Users/Me", **kwargs).status_code == 200, kwargs
    # 无 token → 401 空 body（认证管道形态）
    resp = client.get("/Users/Me")
    assert resp.status_code == 401 and resp.content == b""


def test_users_public_lists_single_user(client: TestClient) -> None:
    body = client.get("/Users/Public").json()
    assert len(body) == 1 and body[0]["Name"] == ADMIN["username"]


def test_startup_helpers(client: TestClient) -> None:
    assert client.get("/Branding/Configuration").json() == {"SplashscreenEnabled": False}
    css = client.get("/Branding/Css")
    assert css.headers["content-type"].startswith("text/css")
    assert client.get("/QuickConnect/Enabled").json() is False
    token = jf_login(client)
    auth = {"ApiKey": token}
    assert client.get("/Sessions", params=auth).json() == []
    assert client.post("/Sessions/Capabilities/Full", params=auth, json={}).status_code == 204
    assert client.delete("/Videos/ActiveEncodings", params=auth).status_code == 204
    assert client.get("/System/Endpoint", params=auth).json() == {
        "IsLocal": True,
        "IsInNetwork": True,
    }


def test_infuse_library_probe_endpoints(client: TestClient, seeded: dict) -> None:
    """Infuse 添加媒体库的完整探测链路（issue #124）。

    未认证必须是 Jellyfin 形态的 401（而非前端 404 HTML），认证后逐个
    返回兼容响应——缺任何一个 Infuse 都会在"验证媒体库"一步失败。
    """
    for path in (
        "/Plugins",
        "/Packages",
        "/MediaSegments/4d430200000000000000000100000000",
        "/Library/VirtualFolders",
        "/UserViews/GroupingOptions",
        "/DisplayPreferences/usersettings",
    ):
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.content == b"", path

    token = jf_login(client)
    auth = {"ApiKey": token}

    assert client.get("/Plugins", params=auth).json() == []
    # Infuse-Library 建索引会探测插件仓库；拿到 404 HTML 会让整轮索引失败
    # （2026-08-25 抓包实证），必须是 JSON 空数组
    assert client.get("/Packages", params=auth).json() == []
    # 起播即查的媒体分段：无分段时是空 QueryResult，不能是 404
    assert client.get(
        "/MediaSegments/4d430200000000000000000100000000", params=auth
    ).json() == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}

    folders = client.get("/Library/VirtualFolders", params=auth).json()
    by_name = {f["Name"]: f for f in folders}
    assert set(by_name) == {"电影", "剧集"}
    assert by_name["电影"]["CollectionType"] == "movies"
    assert by_name["剧集"]["CollectionType"] == "tvshows"
    assert by_name["电影"]["ItemId"] == library_guid(seeded["movie_lib"])
    assert by_name["电影"]["Locations"]  # 主账号设备下发库根路径
    assert by_name["电影"]["RefreshStatus"] == "Idle"
    assert "RefreshProgress" not in by_name["电影"]  # 仅 Active 时输出
    options = by_name["电影"]["LibraryOptions"]
    assert [p["Path"] for p in options["PathInfos"]] == by_name["电影"]["Locations"]
    assert options["Enabled"] is True

    # GroupingOptions 是裸数组（不套 QueryResult），元素仅 Name+Id，按名称排序
    grouping = client.get("/UserViews/GroupingOptions", params=auth).json()
    assert isinstance(grouping, list)
    assert [g["Name"] for g in grouping] == ["剧集", "电影"]
    assert {g["Name"]: g for g in grouping}["电影"]["Id"] == library_guid(
        seeded["movie_lib"]
    )
    assert all(set(g) == {"Name", "Id"} for g in grouping)
    # legacy 路由（UserViewsController.GetGroupingOptionsLegacy）
    legacy = client.get(f"/Users/{'0' * 32}/GroupingOptions", params=auth).json()
    assert legacy == grouping
    # 大小写归一化可达（library 命名空间进归一化中间件）
    assert client.get("/library/virtualfolders", params=auth).status_code == 200

    # 大小写归一化覆盖新命名空间；PascalCase 的 Client 参数也要能取到
    prefs = client.get(
        "/displaypreferences/usersettings",
        params={"userId": "ignored", "Client": "emby", **auth},
    ).json()
    # Id 是真 Jellyfin 的派生规则金样：GetMD5('usersettings') 按 .NET Guid 字节序
    assert prefs["Id"] == "3ce5b65d-e116-d731-65d1-efc4a30ec35c"
    assert prefs["SortBy"] == "SortName"
    assert prefs["SortOrder"] == "Ascending"
    assert prefs["Client"] == "emby"
    assert prefs["CustomPrefs"]["chromecastVersion"] == "stable"
    assert prefs["CustomPrefs"]["skipForwardLength"] == "30000"
    assert prefs["ShowBackdrop"] is True
    assert isinstance(prefs["PrimaryImageHeight"], int)

    post = client.post(
        "/DisplayPreferences/usersettings", params=auth, json={"SortBy": "SortName"}
    )
    assert post.status_code == 204

    # /emby 前缀别名同样可达
    assert client.get("/emby/Plugins", params=auth).json() == []
    assert (
        client.get("/emby/DisplayPreferences/usersettings", params=auth).status_code
        == 200
    )


def test_virtual_folders_refresh_status(
    client: TestClient, seeded: dict, monkeypatch
) -> None:
    """扫描/元数据刷新任务线映射到 Active(+百分比)/Queued 三态（救回自 a50127f）。"""
    from types import SimpleNamespace

    from movieclaw_api.services import media_scrape
    from movieclaw_api.services.library import scan as scan_module

    token = jf_login(client)
    auth = {"ApiKey": token}
    movie_lib = seeded["movie_lib"]

    def by_name(body: list) -> dict:
        return {v["Name"]: v for v in body}

    # 电影库在扫描（3/10）→ Active 30.0；剧集库无任务 → Idle
    monkeypatch.setattr(
        scan_module,
        "scan_progress",
        lambda lid: SimpleNamespace(processed=3, total=10) if lid == movie_lib else None,
    )
    body = by_name(client.get("/Library/VirtualFolders", params=auth).json())
    assert body["电影"]["RefreshStatus"] == "Active"
    assert body["电影"]["RefreshProgress"] == 30.0
    assert body["剧集"]["RefreshStatus"] == "Idle"

    # 元数据刷新已启动但状态未就绪 → Queued
    monkeypatch.setattr(scan_module, "scan_progress", lambda lid: None)
    monkeypatch.setattr(
        media_scrape, "is_library_refreshing", lambda lid: lid == movie_lib
    )
    body = by_name(client.get("/Library/VirtualFolders", params=auth).json())
    assert body["电影"]["RefreshStatus"] == "Queued"
    assert "RefreshProgress" not in body["电影"]


def test_case_insensitive_paths_and_emby_alias(client: TestClient) -> None:
    assert client.get("/system/info/public").status_code == 200
    assert client.get("/SYSTEM/INFO/PUBLIC").status_code == 200
    assert client.get("/emby/System/Info/Public").status_code == 200


def test_item_detail_triggers_self_heal(client: TestClient, seeded: dict, monkeypatch) -> None:
    """条目没有刮削档案时，单条目详情后台触发自愈刮削；已刮过则不触发。"""
    import os
    import sqlite3
    import time

    from movieclaw_api.services import media_scrape

    calls: list[int] = []

    async def fake_scrape(media_item_id: int, **_kw) -> bool:
        calls.append(media_item_id)
        return True

    monkeypatch.setattr(media_scrape, "scrape_media_item", fake_scrape)
    token = jf_login(client)
    auth = {"ApiKey": token}

    # 种子数据的 metadata 行 scraped_at 为 None → 视同"从未刮过"，应触发
    assert client.get(f"/Items/{item_guid(seeded['movie'])}", params=auth).status_code == 200
    for _ in range(100):  # fire-and-forget 任务在应用事件循环上，稍候可见
        if calls:
            break
        time.sleep(0.02)
    assert calls == [seeded["movie"]]

    # 补上 scraped_at（cast 为空、也无关系行）→ 视同已刮过且无数据可补，不触发
    db_path = os.environ["DATABASE_URL"].split("///", 1)[1]
    conn = sqlite3.connect(db_path)
    conn.execute(
        "update media_metadata set scraped_at = '2026-01-01 00:00:00' where media_item_id = ?",
        (seeded["show"],),
    )
    conn.commit()
    calls.clear()
    assert client.get(f"/Items/{item_guid(seeded['show'])}", params=auth).status_code == 200
    time.sleep(0.2)
    assert calls == []

    # 已刮过但档案有 cast、关系表为空（影人功能上线前的存量形态）→ 触发
    conn.execute(
        """update media_metadata set "cast" = '[{"name": "Bryan Cranston"}]'
           where media_item_id = ?""",
        (seeded["show"],),
    )
    conn.commit()
    conn.close()
    calls.clear()
    assert client.get(f"/Items/{item_guid(seeded['show'])}", params=auth).status_code == 200
    for _ in range(100):
        if calls:
            break
        time.sleep(0.02)
    assert calls == [seeded["show"]]


# ---------------------------------------------------------------------------
# 浏览
# ---------------------------------------------------------------------------


def test_user_views_both_routes(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    auth = {"ApiKey": token}
    for path in ("/UserViews", f"/Users/{'0' * 32}/Views"):
        body = client.get(path, params=auth).json()
        assert body["TotalRecordCount"] == 2
        by_name = {v["Name"]: v for v in body["Items"]}
        assert by_name["电影"]["CollectionType"] == "movies"
        assert by_name["剧集"]["CollectionType"] == "tvshows"
        assert by_name["电影"]["Type"] == "CollectionFolder"
        assert by_name["电影"]["Id"] == library_guid(seeded["movie_lib"])


def test_items_in_movie_library(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    body = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "parentId": library_guid(seeded["movie_lib"]),
            "includeItemTypes": "Movie",
            "fields": "Overview,Genres",
            "sortBy": "SortName",
            "sortOrder": "Ascending",
        },
    ).json()
    assert body["TotalRecordCount"] == 1
    movie = body["Items"][0]
    assert movie["Type"] == "Movie"
    assert movie["Name"] == "盗梦空间"
    assert movie["ProductionYear"] == 2010
    assert movie["RunTimeTicks"] == 148 * 60 * 1000 * TICKS_PER_MS
    assert movie["Overview"].startswith("植入梦境")
    assert movie["Genres"] == ["科幻", "动作"]
    assert movie["CommunityRating"] == 8.4
    assert movie["ImageTags"].get("Primary")  # 有海报资产
    ud = movie["UserData"]
    assert ud["Played"] is False and ud["PlayCount"] == 0


def test_items_auto_recursive_in_tv_library(client: TestClient, seeded: dict) -> None:
    """库 + includeItemTypes + 未传 recursive → 自动递归（12.0 行为照抄）。"""
    token = jf_login(client)
    body = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "parentId": library_guid(seeded["tv_lib"]),
            "includeItemTypes": "Episode",
        },
    ).json()
    assert body["TotalRecordCount"] == 3
    assert all(i["Type"] == "Episode" for i in body["Items"])


def test_items_search_and_pagination(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    body = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "searchTerm": "breaking",
            "recursive": "true",
            "includeItemTypes": "Movie,Series",
        },
    ).json()
    assert body["TotalRecordCount"] == 1
    assert body["Items"][0]["Type"] == "Series"
    assert body["Items"][0]["Status"] == "Ended"

    paged = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "parentId": library_guid(seeded["tv_lib"]),
            "includeItemTypes": "Episode",
            "startIndex": "1",
            "limit": "1",
            "sortBy": "ParentIndexNumber,IndexNumber",
        },
    ).json()
    assert paged["TotalRecordCount"] == 3  # 分页前总数
    assert paged["StartIndex"] == 1
    assert len(paged["Items"]) == 1
    assert paged["Items"][0]["IndexNumber"] == 2


def test_single_item_full_fields(client: TestClient, seeded: dict) -> None:
    """单条目接口无 fields 参数、全字段语义（含 MediaSources）。"""
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    for path in (f"/Items/{guid}", f"/Users/{'0' * 32}/Items/{guid}"):
        body = client.get(path, params={"ApiKey": token}).json()
        assert body["Overview"], path
        assert body["MediaSources"], path
        assert body["ProviderIds"]["Tmdb"] == "27205"
        assert body["ProviderIds"]["Imdb"] == "tt1375666"


def test_seasons_and_episodes(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    show_guid = item_guid(seeded["show"])
    seasons = client.get(f"/Shows/{show_guid}/Seasons", params={"ApiKey": token}).json()
    assert seasons["TotalRecordCount"] == 2
    s1 = seasons["Items"][0]
    assert s1["Type"] == "Season" and s1["IndexNumber"] == 1
    assert s1["SeriesId"] == show_guid
    assert s1["UserData"]["UnplayedItemCount"] == 2

    eps = client.get(
        f"/Shows/{show_guid}/Episodes",
        params={"ApiKey": token, "seasonId": season_guid(seeded["show"], 1)},
    ).json()
    assert eps["TotalRecordCount"] == 2
    ep = eps["Items"][0]
    assert ep["Type"] == "Episode"
    assert ep["Name"] == "Pilot"
    assert ep["ParentIndexNumber"] == 1 and ep["IndexNumber"] == 1
    assert ep["SeasonId"] == season_guid(seeded["show"], 1)
    assert ep["SeriesName"] == "绝命毒师"

    missing = client.get(
        f"/Shows/{show_guid}/Episodes",
        params={"ApiKey": token, "seasonId": season_guid(seeded["show"], 9)},
    )
    assert missing.status_code == 404


def test_unknown_guid_is_404(client: TestClient) -> None:
    token = jf_login(client)
    resp = client.get(f"/Items/{'a' * 32}", params={"ApiKey": token})
    assert resp.status_code == 404


def test_image_served_and_missing(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    ok = client.get(f"/Items/{guid}/Images/Primary", params={"ApiKey": token, "tag": "abc"})
    assert ok.status_code == 200
    assert ok.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert ok.headers["ETag"] == '"abc"'
    not_modified = client.get(
        f"/Items/{guid}/Images/Primary",
        params={"ApiKey": token, "tag": "abc"},
        headers={"If-None-Match": '"abc"'},
    )
    assert not_modified.status_code == 304

    missing = client.get(f"/Items/{guid}/Images/Logo", params={"ApiKey": token})
    assert missing.status_code == 404


def test_item_image_three_layers(
    client: TestClient, seeded: dict, media_root, tmp_path, monkeypatch
) -> None:
    """条目图片三层解析：目录美术图 > 刮削资产 > TMDB 图床兜底（与 Web 同口径）。"""
    import os
    import sqlite3
    from types import SimpleNamespace

    from PIL import Image

    token = jf_login(client)
    auth = {"ApiKey": token}
    movie_guid = item_guid(seeded["movie"])

    # ① 条目目录美术图最优先：短缓存（用户可随时换图），压过刮削资产
    entry = media_root / "Inception (2010)"
    Image.new("RGB", (50, 75), "#aa3333").save(entry / "poster.jpg", "JPEG")
    resp = client.get(f"/Items/{movie_guid}/Images/Primary", params=auth)
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "private, max-age=3600"

    # ② 目录图撤掉 → 回到刮削资产层
    (entry / "poster.jpg").unlink()
    resp = client.get(f"/Items/{movie_guid}/Images/Primary", params=auth)
    assert resp.status_code == 200
    assert "private" not in resp.headers["Cache-Control"]

    # ③ TMDB 兜底：show 无资产，标上 poster_path 后 DTO 有 tag、图片经代理可出
    db_path = os.environ["DATABASE_URL"].split("///", 1)[1]
    conn = sqlite3.connect(db_path)
    conn.execute(
        "update media_item set poster_path = '/fallback.jpg' where id = ?",
        (seeded["show"],),
    )
    conn.commit()
    conn.close()

    fake_file = tmp_path / "fake-tmdb.jpg"
    Image.new("RGB", (60, 90), "#22aa66").save(fake_file, "JPEG")

    class _FakeCache:
        async def get_or_fetch(self, url: str):
            assert "/fallback.jpg" in url
            return SimpleNamespace(path=fake_file, content_type="image/jpeg")

    from movieclaw_api.services import image_cache as image_cache_module

    monkeypatch.setattr(image_cache_module, "get_image_cache", lambda: _FakeCache())

    show_guid = item_guid(seeded["show"])
    body = client.get(f"/Items/{show_guid}", params=auth).json()
    assert "Primary" in body["ImageTags"]  # 资产未落地也要有 tag，客户端才会来请求
    resp = client.get(f"/Items/{show_guid}/Images/Primary", params=auth)
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"


# ---------------------------------------------------------------------------
# 播放
# ---------------------------------------------------------------------------


def test_playback_info_direct_play_no_transcode(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    body = client.post(
        f"/Items/{guid}/PlaybackInfo",
        params={"ApiKey": token},
        json={"DeviceProfile": {"MaxStreamingBitrate": 1}},  # profile 被整体忽略
    ).json()
    assert len(body["PlaySessionId"]) == 32
    assert "ErrorCode" not in body
    sources = body["MediaSources"]
    assert len(sources) == 2  # 本地 mkv + strm 两个版本

    local = next(s for s in sources if s["Protocol"] == "File")
    assert local["SupportsDirectPlay"] is True
    assert local["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in local
    assert local["Container"] == "mkv"
    assert local["RequiresOpening"] is False
    video = next(s for s in local["MediaStreams"] if s["Type"] == "Video")
    assert video["VideoRange"] == "HDR" and video["VideoRangeType"] == "HDR10"
    assert video["DisplayTitle"] == "4K HEVC HDR"
    audio = next(s for s in local["MediaStreams"] if s["Type"] == "Audio")
    assert audio["DisplayTitle"] == "Chinese - AAC - 5.1 - Default"

    remote = next(s for s in sources if s["Protocol"] == "Http")
    assert remote["Path"] == "https://pan.example.com/inception-1080p.mp4"
    assert remote["IsRemote"] is True
    assert remote["SupportsDirectPlay"] is True
    # strm 版本从未探测：hdr 为空是「未知」不是 SDR，不能妄断
    remote_video = next(s for s in remote["MediaStreams"] if s["Type"] == "Video")
    assert remote_video["VideoRange"] == "Unknown"
    assert remote_video["VideoRangeType"] == "Unknown"
    assert remote_video["DisplayTitle"] == "Video"


def test_probed_sdr_file_stays_sdr(client: TestClient, seeded: dict) -> None:
    """探测跑过且无 HDR 的文件是真 SDR，不受 Unknown 兜底影响。"""
    token = jf_login(client)
    guid = episode_guid(seeded["show"], 1, 1)
    body = client.post(f"/Items/{guid}/PlaybackInfo", params={"ApiKey": token}).json()
    video = next(
        s for s in body["MediaSources"][0]["MediaStreams"] if s["Type"] == "Video"
    )
    assert video["VideoRange"] == "SDR" and video["VideoRangeType"] == "SDR"
    assert video["DisplayTitle"] == "1080p H264 SDR"


def test_stream_range_request(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    info = client.post(f"/Items/{guid}/PlaybackInfo", params={"ApiKey": token}).json()
    local = next(s for s in info["MediaSources"] if s["Protocol"] == "File")

    full = client.get(
        f"/Videos/{guid}/stream",
        params={"ApiKey": token, "static": "true", "mediaSourceId": local["Id"]},
    )
    assert full.status_code == 200
    assert full.headers["content-type"] == "video/x-matroska"
    assert len(full.content) == 4096

    partial = client.get(
        f"/Videos/{guid}/stream",
        params={"ApiKey": token, "static": "true", "mediaSourceId": local["Id"]},
        headers={"Range": "bytes=0-1023"},
    )
    assert partial.status_code == 206
    assert len(partial.content) == 1024
    assert partial.headers["Content-Range"] == "bytes 0-1023/4096"

    # mediaSourceId == itemId → 回落第一个版本
    fallback = client.get(
        f"/Videos/{guid}/stream",
        params={"ApiKey": token, "static": "true", "mediaSourceId": guid},
    )
    assert fallback.status_code in (200, 302)

    # 无 static → 400（我们不转码）
    no_static = client.get(f"/Videos/{guid}/stream", params={"ApiKey": token})
    assert no_static.status_code == 400


def test_stream_strm_redirects_without_proxy(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    info = client.post(f"/Items/{guid}/PlaybackInfo", params={"ApiKey": token}).json()
    remote = next(s for s in info["MediaSources"] if s["Protocol"] == "Http")
    resp = client.get(
        f"/Videos/{guid}/stream",
        params={"ApiKey": token, "static": "true", "mediaSourceId": remote["Id"]},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://pan.example.com/inception-1080p.mp4"
    assert resp.content == b""

    head = client.head(
        f"/Videos/{guid}/stream",
        params={"ApiKey": token, "static": "true", "mediaSourceId": remote["Id"]},
        follow_redirects=False,
    )
    assert head.status_code == 302


def test_download_local_file_with_attachment(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    info = client.post(f"/Items/{guid}/PlaybackInfo", params={"ApiKey": token}).json()
    local = next(s for s in info["MediaSources"] if s["Protocol"] == "File")

    resp = client.get(
        f"/Items/{guid}/Download",
        params={"ApiKey": token, "mediaSourceId": local["Id"]},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/x-matroska"
    assert "attachment" in resp.headers["content-disposition"]
    assert len(resp.content) == 4096

    # 下载器断点续传：Range 语义与取流一致
    partial = client.get(
        f"/Items/{guid}/Download",
        params={"ApiKey": token, "mediaSourceId": local["Id"]},
        headers={"Range": "bytes=0-1023"},
    )
    assert partial.status_code == 206
    assert len(partial.content) == 1024

    # /File 变体：裸文件，不带 attachment
    raw = client.get(
        f"/Items/{guid}/File",
        params={"ApiKey": token, "mediaSourceId": local["Id"]},
    )
    assert raw.status_code == 200
    assert "content-disposition" not in raw.headers

    # 无 token → 401
    anon = client.get(f"/Items/{guid}/Download")
    assert anon.status_code == 401


def test_download_strm_redirects_to_cloud(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    guid = item_guid(seeded["movie"])
    info = client.post(f"/Items/{guid}/PlaybackInfo", params={"ApiKey": token}).json()
    remote = next(s for s in info["MediaSources"] if s["Protocol"] == "Http")

    resp = client.get(
        f"/Items/{guid}/Download",
        params={"ApiKey": token, "mediaSourceId": remote["Id"]},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://pan.example.com/inception-1080p.mp4"


def test_progress_reporting_flow(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    auth = {"ApiKey": token}
    ep = episode_guid(seeded["show"], 1, 1)
    runtime_ms = 47 * 60 * 1000

    # 开始播放 → play_count+1
    assert (
        client.post("/Sessions/Playing", params=auth, json={"ItemId": ep}).status_code
        == 204
    )
    # 进度 50%（含未知字段 EventName，必须被忽略）
    assert (
        client.post(
            "/Sessions/Playing/Progress",
            params=auth,
            json={
                "ItemId": ep,
                "PositionTicks": runtime_ms // 2 * TICKS_PER_MS,
                "IsPaused": False,
                "EventName": "timeupdate",
            },
        ).status_code
        == 204
    )
    detail = client.get(f"/Items/{ep}", params=auth).json()
    ud = detail["UserData"]
    assert ud["PlayCount"] == 1
    assert ud["Played"] is False
    assert ud["PlaybackPositionTicks"] == runtime_ms // 2 * TICKS_PER_MS
    assert 49 < ud["PlayedPercentage"] < 51

    # 继续观看列表（新旧路由）
    for path in ("/UserItems/Resume", f"/Users/{'0' * 32}/Items/Resume"):
        resume = client.get(path, params=auth).json()
        assert resume["TotalRecordCount"] == 1
        assert resume["Items"][0]["Id"] == ep

    # NextUp：看了一半的那集本身出现（enableResumable 语义）
    nextup = client.get("/Shows/NextUp", params=auth).json()
    assert nextup["Items"][0]["Id"] == ep

    # 播到 95% 后仅靠 Progress（强杀 App 场景）也要标已看
    assert (
        client.post(
            "/Sessions/Playing/Progress",
            params=auth,
            json={"ItemId": ep, "PositionTicks": int(runtime_ms * 0.95) * TICKS_PER_MS},
        ).status_code
        == 204
    )
    ud = client.get(f"/Items/{ep}", params=auth).json()["UserData"]
    assert ud["Played"] is True
    assert ud["PlaybackPositionTicks"] == 0

    # NextUp 推进到下一集
    nextup = client.get("/Shows/NextUp", params=auth).json()
    assert nextup["Items"][0]["Id"] == episode_guid(seeded["show"], 1, 2)


def test_stopped_failed_skips_persistence(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    auth = {"ApiKey": token}
    movie = item_guid(seeded["movie"])
    assert (
        client.post(
            "/Sessions/Playing/Stopped",
            params=auth,
            json={"ItemId": movie, "PositionTicks": 999 * TICKS_PER_MS, "Failed": True},
        ).status_code
        == 204
    )
    ud = client.get(f"/Items/{movie}", params=auth).json()["UserData"]
    assert ud["PlaybackPositionTicks"] == 0


def test_stopped_cancels_device_streams_even_when_failed(
    client: TestClient, seeded: dict, monkeypatch
) -> None:
    """Stopped 是取流停止信号；Failed 只跳过播放状态写入，不能留下预读。"""
    from movieclaw_jellyfin.routes import playstate

    token = jf_login(client)
    calls: list[str] = []
    monkeypatch.setattr(playstate, "stop_device_streams", calls.append)

    response = client.post(
        "/Sessions/Playing/Stopped",
        params={"ApiKey": token},
        json={"ItemId": item_guid(seeded["movie"]), "Failed": True},
    )

    assert response.status_code == 204
    assert calls == ["test-device-1"]


def test_mark_played_cascade_and_unplayed(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    auth = {"ApiKey": token}
    show = item_guid(seeded["show"])

    resp = client.post(f"/UserPlayedItems/{show}", params=auth)
    assert resp.status_code == 200
    dto = resp.json()
    assert dto["Played"] is True and dto["Key"]

    # 级联到全部集
    series = client.get(f"/Items/{show}", params=auth).json()
    assert series["UserData"]["Played"] is True
    assert series["UserData"]["UnplayedItemCount"] == 0

    # DELETE 全清零（legacy 路由）
    resp = client.request(
        "DELETE", f"/Users/{'0' * 32}/PlayedItems/{show}", params=auth
    )
    assert resp.status_code == 200
    series = client.get(f"/Items/{show}", params=auth).json()
    assert series["UserData"]["Played"] is False
    assert series["UserData"]["UnplayedItemCount"] == 3


def test_favorites_roundtrip(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    auth = {"ApiKey": token}
    movie = item_guid(seeded["movie"])
    assert client.post(f"/UserFavoriteItems/{movie}", params=auth).json()["IsFavorite"] is True
    body = client.get(
        "/Items",
        params={
            "ApiKey": token,
            "recursive": "true",
            "includeItemTypes": "Movie,Series",
            "filters": "IsFavorite",
        },
    ).json()
    assert body["TotalRecordCount"] == 1
    assert (
        client.request("DELETE", f"/UserFavoriteItems/{movie}", params=auth).json()[
            "IsFavorite"
        ]
        is False
    )


def test_latest_groups_new_episodes(client: TestClient, seeded: dict) -> None:
    token = jf_login(client)
    body = client.get("/Items/Latest", params={"ApiKey": token}).json()
    assert isinstance(body, list)  # 扁平数组，非 QueryResult
    types = {i["Type"] for i in body}
    assert "Movie" in types
    series = next(i for i in body if i["Type"] == "Series")
    assert series["ChildCount"] == 3  # 同剧 3 个新单元聚合


def test_playing_ping_requires_session_id(client: TestClient) -> None:
    token = jf_login(client)
    assert (
        client.post(
            "/Sessions/Playing/Ping", params={"ApiKey": token, "playSessionId": "x"}
        ).status_code
        == 204
    )
    assert client.post("/Sessions/Playing/Ping", params={"ApiKey": token}).status_code == 400
