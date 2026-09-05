"""Jellyfin 兼容层的库可见范围（docs/design/library-access.md 2.5）：

超管设备也按可浏览集投影——超管把自己从某个库的范围里摘掉后，电视端的
视图枚举、Policy.EnabledFolders、条目直达、最新媒体一并不含它；勾回即恢复。
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_jellyfin.ids import item_guid, library_guid

from .helpers import jf_login


def _set_admin_visible(client: TestClient, library_id: int, visible: bool) -> None:
    """直接改库表的可见范围标记：种子里两个库共用一个根目录，走更新接口会被
    「根路径重叠」校验拦下，而这里要验的只是投影，不是配置校验。"""
    database_url = get_settings().database_url
    prefix = "sqlite+aiosqlite:///"
    assert database_url.startswith(prefix)
    with sqlite3.connect(database_url.removeprefix(prefix)) as connection:
        connection.execute(
            "UPDATE library SET access_mode = 'selected', admin_visible = ? WHERE id = ?",
            (1 if visible else 0, library_id),
        )
        connection.commit()


def test_admin_device_follows_admin_visible(client: TestClient, seeded: dict) -> None:
    _set_admin_visible(client, seeded["movie_lib"], False)
    token = jf_login(client)
    headers = {"X-Emby-Token": token}
    movie_guid = item_guid(seeded["movie"])

    me = client.get("/Users/Me", headers=headers).json()
    assert me["Policy"]["IsAdministrator"] is True
    assert me["Policy"]["EnableAllFolders"] is False
    assert me["Policy"]["EnabledFolders"] == [library_guid(seeded["tv_lib"])]

    views = client.get("/UserViews", headers=headers).json()
    assert [v["Id"] for v in views["Items"]] == [library_guid(seeded["tv_lib"])]
    assert client.get(f"/Items/{movie_guid}", headers=headers).status_code == 404
    latest = client.get(f"/Users/{me['Id']}/Items/Latest", headers=headers).json()
    assert movie_guid not in {i["Id"] for i in latest}

    # 勾回自己：电影库回到视图里，条目直达恢复
    _set_admin_visible(client, seeded["movie_lib"], True)
    views = client.get("/UserViews", headers=headers).json()
    assert library_guid(seeded["movie_lib"]) in {v["Id"] for v in views["Items"]}
    assert client.get(f"/Items/{movie_guid}", headers=headers).status_code == 200
