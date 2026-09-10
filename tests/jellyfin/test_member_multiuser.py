"""Jellyfin 兼容层多用户行为（docs/design/member-management.md §3.7）。

覆盖三块：
1. 登录页与身份投影：/Users/Public 多头像、成员 Policy 非管理员 +
   EnabledFolders 按可见库下发；
2. 库可见性服务端强制：白名单外的库对成员在**枚举与直达两条路径**上
   都不可及——UserViews 过滤、条目详情/播放协商/取流/下载一律 404
   （GUID 是可枚举的结构化编码，只挡浏览不挡直达等于没挡）；
3. 观看状态按登录身份隔离：成员标记已看不影响超管的 UserData。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_jellyfin.ids import episode_guid, item_guid, library_guid

from .helpers import ADMIN, jf_login

MEMBER = {"username": "family", "password": "family-pass-1"}
MEMBER_AUTH_HEADER = (
    'MediaBrowser Client="Infuse", Device="Kid TV", '
    'DeviceId="member-device-1", Version="8.2"'
)


def _create_member(client: TestClient, *, tv_lib_id: int) -> int:
    """经 Web API 建成员并把可见库限定为剧集库（client 已带超管 Cookie）。"""
    created = client.post("/api/v1/members", json=MEMBER)
    assert created.status_code == 200, created.text
    member_id = created.json()["data"]["id"]
    updated = client.put(
        f"/api/v1/members/{member_id}",
        json={"all_libraries": False, "library_ids": [tv_lib_id]},
    )
    assert updated.status_code == 200, updated.text
    return member_id


def _member_login(client: TestClient) -> str:
    resp = client.post(
        "/Users/AuthenticateByName",
        json={"Username": MEMBER["username"], "Pw": MEMBER["password"]},
        headers={"Authorization": MEMBER_AUTH_HEADER},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["AccessToken"]


def _duplicate_episode_into_member_library(
    client: TestClient, seeded: dict, media_root: Path
) -> tuple[int, Path]:
    """制造现场同款数据：同一集同时存在于隐藏库和成员可见库。"""
    member_root = media_root.parent / "member-visible"
    member_root.mkdir()
    created = client.post(
        "/api/v1/libraries",
        json={"name": "成员剧集", "kind": "tv", "root_paths": [str(member_root)]},
    )
    assert created.status_code == 200, created.text
    library_id = created.json()["data"]["id"]

    target = member_root / "S01E01.mkv"
    target.write_bytes(b"M" * 2048)
    database_url = get_settings().database_url
    prefix = "sqlite+aiosqlite:///"
    assert database_url.startswith(prefix)
    with sqlite3.connect(database_url.removeprefix(prefix)) as connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute(
            """
            SELECT * FROM library_file
            WHERE library_id = ? AND media_item_id = ?
              AND season_number = 1 AND episode_number = 1
            """,
            (seeded["tv_lib"], seeded["show"]),
        ).fetchone()
        assert source is not None
        row = dict(source)
        row.pop("id")
        row["library_id"] = library_id
        row["file_path"] = str(target)
        columns = list(row)
        connection.execute(
            f"INSERT INTO library_file ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [row[column] for column in columns],
        )
        connection.commit()
    return library_id, target


def test_member_identity_projection(client: TestClient, seeded: dict) -> None:
    """多头像登录页；成员 Policy 投影非管理员 + EnabledFolders。"""
    _create_member(client, tv_lib_id=seeded["tv_lib"])

    public = client.get("/Users/Public").json()
    assert [u["Name"] for u in public] == [ADMIN["username"], MEMBER["username"]]
    assert public[0]["Policy"]["IsAdministrator"] is True
    assert public[1]["Policy"]["IsAdministrator"] is False
    assert public[1]["Policy"]["EnableAllFolders"] is False
    assert public[1]["Policy"]["EnabledFolders"] == [library_guid(seeded["tv_lib"])]

    # 登录响应与 /Users/Me 的身份一致，且不是超管的固定 GUID
    token = _member_login(client)
    me = client.get("/Users/Me", headers={"X-Emby-Token": token}).json()
    assert me["Name"] == MEMBER["username"]
    assert me["Id"] == public[1]["Id"]
    assert me["Id"] != public[0]["Id"]


def test_member_password_reset_revokes_jellyfin_token(
    client: TestClient, seeded: dict
) -> None:
    """管理员重置成员密码后，播放器保存的旧 AccessToken 必须失效。"""
    member_id = _create_member(client, tv_lib_id=seeded["tv_lib"])
    token = _member_login(client)
    assert client.get("/Users/Me", headers={"X-Emby-Token": token}).status_code == 200

    reset = client.post(f"/api/v1/members/{member_id}/reset-password")
    assert reset.status_code == 200, reset.text
    assert client.get("/Users/Me", headers={"X-Emby-Token": token}).status_code == 401


def test_member_password_change_revokes_jellyfin_token(
    client: TestClient, seeded: dict
) -> None:
    """成员自己改密码后，播放器保存的旧 AccessToken 也必须失效。"""
    _create_member(client, tv_lib_id=seeded["tv_lib"])
    token = _member_login(client)
    assert client.get("/Users/Me", headers={"X-Emby-Token": token}).status_code == 200

    login = client.post("/api/v1/auth/login", json=MEMBER)
    assert login.status_code == 200, login.text
    changed = client.put(
        "/api/v1/auth/password",
        json={"old_password": MEMBER["password"], "new_password": "family-new-pass-9"},
    )
    assert changed.status_code == 200, changed.text
    assert client.get("/Users/Me", headers={"X-Emby-Token": token}).status_code == 401


def test_member_library_visibility_enforced_on_all_paths(
    client: TestClient, seeded: dict, media_root: Path
) -> None:
    """白名单外的电影库：枚举不出现，直达（详情/协商/取流/下载）全 404。"""
    member_library_id, member_file = _duplicate_episode_into_member_library(
        client, seeded, media_root
    )
    _create_member(client, tv_lib_id=member_library_id)
    token = _member_login(client)
    headers = {"X-Emby-Token": token}
    movie_guid = item_guid(seeded["movie"])

    views = client.get("/UserViews", headers=headers).json()
    assert [v["Name"] for v in views["Items"]] == ["成员剧集"]

    assert client.get(f"/Items/{movie_guid}", headers=headers).status_code == 404
    info = client.get(f"/Items/{movie_guid}/PlaybackInfo", headers=headers).json()
    assert info["MediaSources"] == []
    stream = client.get(
        f"/Videos/{movie_guid}/stream", params={"static": "true"}, headers=headers
    )
    assert stream.status_code == 404
    assert (
        client.get(f"/Items/{movie_guid}/Download", headers=headers).status_code == 404
    )

    # 可见库内的剧集一切正常（对照组，防止把门修成了全关）
    show_guid = item_guid(seeded["show"])
    assert client.get(f"/Items/{show_guid}", headers=headers).status_code == 200
    episode = episode_guid(seeded["show"], 1, 1)
    detail = client.get(f"/Items/{episode}", headers=headers).json()
    assert [source["Path"] for source in detail["MediaSources"]] == [str(member_file)]
    playback = client.post(f"/Items/{episode}/PlaybackInfo", headers=headers).json()
    assert [source["Id"] for source in playback["MediaSources"]] == [
        detail["MediaSources"][0]["Id"]
    ]
    stream = client.get(
        f"/Videos/{episode}/stream.mp4",
        params={
            "static": "true",
            "mediaSourceId": detail["MediaSources"][0]["Id"],
        },
        headers=headers,
    )
    assert stream.status_code == 200
    assert stream.content == b"M" * 2048

    admin_token = jf_login(client)
    assert (
        client.get(f"/Items/{movie_guid}", headers={"X-Emby-Token": admin_token}).status_code
        == 200
    )


def test_member_virtual_folders_scoped_and_paths_hidden(
    client: TestClient, seeded: dict
) -> None:
    """成员设备的 VirtualFolders/GroupingOptions 只见白名单库，且不下发路径。"""
    _create_member(client, tv_lib_id=seeded["tv_lib"])
    member_token = _member_login(client)
    headers = {"X-Emby-Token": member_token}

    folders = client.get("/Library/VirtualFolders", headers=headers).json()
    assert [f["Name"] for f in folders] == ["剧集"]
    assert folders[0]["Locations"] == []

    grouping = client.get("/UserViews/GroupingOptions", headers=headers).json()
    assert [g["Name"] for g in grouping] == ["剧集"]

    admin_folders = client.get(
        "/Library/VirtualFolders", headers={"X-Emby-Token": jf_login(client)}
    ).json()
    assert {f["Name"] for f in admin_folders} == {"电影", "剧集"}
    assert all(f["Locations"] for f in admin_folders)


def test_member_playstate_isolated_from_admin(client: TestClient, seeded: dict) -> None:
    """成员标记已看只写自己的行：超管同一集的 UserData 不受影响。"""
    _create_member(client, tv_lib_id=seeded["tv_lib"])
    member_token = _member_login(client)
    admin_token = jf_login(client)
    ep = episode_guid(seeded["show"], 1, 1)

    marked = client.post(
        f"/UserPlayedItems/{ep}", headers={"X-Emby-Token": member_token}
    )
    assert marked.status_code == 200
    assert marked.json()["Played"] is True

    admin_item = client.get(f"/Items/{ep}", headers={"X-Emby-Token": admin_token}).json()
    assert admin_item["UserData"]["Played"] is False
    member_item = client.get(f"/Items/{ep}", headers={"X-Emby-Token": member_token}).json()
    assert member_item["UserData"]["Played"] is True


# ---------------------------------------------------------------------------
# 内容分级约束（儿童档案，docs/design/library-filtering.md F5）
# ---------------------------------------------------------------------------


def _member_headers(token: str) -> dict[str, str]:
    return {"Authorization": f'{MEMBER_AUTH_HEADER}, Token="{token}"'}


def test_content_limit_reaches_the_tv(client: TestClient, seeded: dict) -> None:
    """电视端是儿童档案最要紧的一面——孩子真正会去用的就是它。

    列表、单条直达、最近添加三条路径各验一次：GUID 是可枚举的结构化编码，
    只挡枚举不挡直达等于没挡（与库可见性那一组同一条道理）。
    """
    created = client.post("/api/v1/members", json={"username": "kid", "password": "kid-pass-11"})
    assert created.status_code == 200, created.text
    member_id = created.json()["data"]["id"]

    movie_id = seeded["movie"]
    # 给那部电影一个成人分级
    with sqlite3.connect(get_settings().database_url.split("///")[-1]) as db:
        db.execute(
            "UPDATE media_metadata SET content_rating='R' WHERE media_item_id=?", (movie_id,)
        )
        db.commit()

    resp = client.post(
        "/Users/AuthenticateByName",
        json={"Username": "kid", "Pw": "kid-pass-11"},
        headers={"Authorization": MEMBER_AUTH_HEADER},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["AccessToken"]

    listed = client.get(
        f"/Items?ParentId={library_guid(seeded['movie_lib'])}&Recursive=true",
        headers=_member_headers(token),
    ).json()["Items"]
    assert item_guid(movie_id) in {row["Id"] for row in listed}, "还没设上限时看得到"

    assert (
        client.put(
            f"/api/v1/members/{member_id}",
            json={"content_age_limit": 13, "allow_unrated": True},
        ).status_code
        == 200
    )

    listed = client.get(
        f"/Items?ParentId={library_guid(seeded['movie_lib'])}&Recursive=true",
        headers=_member_headers(token),
    ).json()["Items"]
    assert item_guid(movie_id) not in {row["Id"] for row in listed}, "列表里不该还有它"

    # 直达同样 404：GUID 猜得出来，只挡列表等于没挡
    detail = client.get(f"/Items/{item_guid(movie_id)}", headers=_member_headers(token))
    assert detail.status_code == 404, detail.text

    latest = client.get(
        f"/Users/{member_id}/Items/Latest?Limit=50", headers=_member_headers(token)
    )
    assert latest.status_code == 200, latest.text
    assert item_guid(movie_id) not in {row["Id"] for row in latest.json()}
