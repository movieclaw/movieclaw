"""钉了首页的合集 → 顶层「虚拟媒体库」（docs/design/library-collections.md 4.11）。

合集在协议侧有两个身份：「合集」视图下的 BoxSet（test_boxsets.py 覆盖），
以及这里的 CollectionFolder。这组用例压的是第二个身份——它什么时候出现、
点进去拿到什么、以及那些"下发了却被客户端自己藏掉"的隐形坑。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.jellyfin.helpers import AUTH_HEADER, jf_login

from movieclaw_jellyfin.ids import (
    collection_guid,
    collection_view_guid,
    collections_view_guid,
    library_guid,
    root_guid,
)

#: 「收全部」的规则：空 values 的 any_of 不收窄，等于把整库收进来。
ALL_ITEMS = [{"field": "genres", "op": "any_of", "values": []}]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f'{AUTH_HEADER}, Token="{token}"'}


def _add_collection(client: TestClient, *, name: str, library_id: int, **kw) -> int:
    payload = {"name": name, "library_id": library_id, **kw}
    resp = client.post("/api/v1/collections", json=payload)
    assert resp.status_code == 200, resp.text
    return int(resp.json()["data"]["id"])


def _pin(client: TestClient, collection_id: int, *, hidden: bool = False) -> None:
    """把一个合集钉上媒体库首页——走真实的界面偏好接口，不直接写库。

    这样用例同时验证了"网页端那颗开关与电视端看到的库是同一份数据"。
    """
    prefs = client.get("/api/v1/ui/preferences").json()["data"]
    row = {"id": f"row:c{collection_id}", "collection_id": collection_id}
    if hidden:
        row["hidden"] = True
    prefs["home"] = {"rows": [*prefs.get("home", {}).get("rows", []), row]}
    resp = client.put("/api/v1/ui/preferences", json=prefs)
    assert resp.status_code == 200, resp.text


def _unpin(client: TestClient, collection_id: int) -> None:
    prefs = client.get("/api/v1/ui/preferences").json()["data"]
    prefs["home"] = {
        "rows": [r for r in prefs["home"]["rows"] if r.get("collection_id") != collection_id]
    }
    resp = client.put("/api/v1/ui/preferences", json=prefs)
    assert resp.status_code == 200, resp.text


def _view_ids(client: TestClient, token: str) -> set[str]:
    resp = client.get("/UserViews", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    return {v["Id"] for v in resp.json()["Items"]}


@pytest.fixture
def token(client: TestClient) -> str:
    return jf_login(client)


def test_unpinned_collection_is_not_a_library(
    client: TestClient, token: str, seeded: dict
) -> None:
    """没钉首页的合集只是 BoxSet，不该在 /UserViews 里多出一个库。"""
    cid = _add_collection(client, name="没钉", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    ids = _view_ids(client, token)
    assert collection_view_guid(cid) not in ids
    # 但它作为 BoxSet 的那个身份照常在
    assert collections_view_guid() in ids


def test_pinned_collection_becomes_a_library_view(
    client: TestClient, token: str, seeded: dict
) -> None:
    """钉上首页 → /UserViews 多一个 CollectionFolder，形态与真库同型。"""
    cid = _add_collection(client, name="诺兰", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    _pin(client, cid)

    views = client.get("/UserViews", headers=_headers(token)).json()["Items"]
    view = next((v for v in views if v["Id"] == collection_view_guid(cid)), None)
    assert view is not None, "钉了首页的合集该以媒体库的身份出现"
    assert view["Type"] == "CollectionFolder"
    assert view["IsFolder"] is True
    assert view["Name"] == "诺兰"
    assert view["ParentId"] == root_guid()
    # 挂在电影库下的合集，形态就是电影库的形态——不解析成员就能答出来
    assert view["CollectionType"] == "movies"
    assert view["ChildCount"] >= 1


def test_both_identities_coexist(client: TestClient, token: str, seeded: dict) -> None:
    """变成虚拟库之后 BoxSet 身份仍在——已配对客户端的深链与收藏不该指空。"""
    cid = _add_collection(client, name="两处都在", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    _pin(client, cid)

    assert collection_view_guid(cid) in _view_ids(client, token)
    listed = client.get(
        f"/Items?ParentId={collections_view_guid()}", headers=_headers(token)
    ).json()
    assert collection_guid(cid) in {row["Id"] for row in listed["Items"]}
    # 两个身份的 GUID 必须不同，否则客户端缓存的 Type 会打架
    assert collection_view_guid(cid) != collection_guid(cid)


def test_virtual_library_children_are_members(
    client: TestClient, token: str, seeded: dict
) -> None:
    """点进虚拟库拿到的就是合集成员——伪装的是外壳，不是另一套名单。"""
    cid = _add_collection(client, name="全部电影", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    _pin(client, cid)

    virtual = client.get(f"/Items?ParentId={collection_view_guid(cid)}", headers=_headers(token))
    boxset = client.get(f"/Items?ParentId={collection_guid(cid)}", headers=_headers(token))
    assert virtual.status_code == 200, virtual.text
    assert [row["Id"] for row in virtual.json()["Items"]] == [
        row["Id"] for row in boxset.json()["Items"]
    ]
    assert {row["Type"] for row in virtual.json()["Items"]} <= {"Movie", "Series", "Video"}


def test_virtual_library_detail(client: TestClient, token: str, seeded: dict) -> None:
    cid = _add_collection(client, name="单条", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    _pin(client, cid)
    resp = client.get(f"/Items/{collection_view_guid(cid)}", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    dto = resp.json()
    assert dto["Type"] == "CollectionFolder"
    assert dto["Name"] == "单条"
    assert dto["ParentId"] == root_guid()


def test_enabled_folders_lists_virtual_libraries(
    client: TestClient, token: str, seeded: dict
) -> None:
    """EnabledFolders 必须带上虚拟库 GUID。

    这份清单一旦非空，客户端就拿它当白名单过滤自己看到的库——虚拟库不在里面
    就会被客户端自己藏掉，而且不报任何错。这是整条链路上最难查的一个坑。
    """
    cid = _add_collection(client, name="白名单", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    _pin(client, cid)
    me = client.get("/Users/Me", headers=_headers(token))
    assert me.status_code == 200, me.text
    folders = me.json()["Policy"]["EnabledFolders"]
    assert collection_view_guid(cid) in folders
    assert library_guid(seeded["movie_lib"]) in folders


def test_empty_pinned_collection_is_not_downlinked(
    client: TestClient, token: str, seeded: dict
) -> None:
    """钉了首页但一个成员都没有的合集不下发——点进去空无一物是死路。"""
    cid = _add_collection(
        client,
        name="一个都不命中",
        library_id=seeded["movie_lib"],
        rules=[{"field": "genres", "op": "any_of", "values": [999999]}],
    )
    _pin(client, cid)
    assert collection_view_guid(cid) not in _view_ids(client, token)


def test_hidden_home_row_is_not_a_library(client: TestClient, token: str, seeded: dict) -> None:
    """首页上藏起来的那一行不算钉着——它在首页就是不显示，虚拟库同理。"""
    cid = _add_collection(client, name="藏起来的", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    _pin(client, cid, hidden=True)
    assert collection_view_guid(cid) not in _view_ids(client, token)


def test_unpinning_removes_the_view_but_not_the_link(
    client: TestClient, token: str, seeded: dict
) -> None:
    """取消钉首页只让它从 /UserViews 消失；已存下的链接照旧能打开。

    客户端会把库 GUID 存进自己的快捷入口和播放队列，让那些链接当场变成错误页
    比多留一个可达的 GUID 更糟。
    """
    cid = _add_collection(client, name="取消钉", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    _pin(client, cid)
    assert collection_view_guid(cid) in _view_ids(client, token)

    _unpin(client, cid)
    assert collection_view_guid(cid) not in _view_ids(client, token)
    detail = client.get(f"/Items/{collection_view_guid(cid)}", headers=_headers(token))
    assert detail.status_code == 200


def test_latest_is_scoped_to_members(client: TestClient, token: str, seeded: dict) -> None:
    """虚拟库的「最新」只看合集成员。

    不接这一条的话 parentId 会被当成没给，客户端在合集库里看到的是**全库**
    最新——那比空着更糟，它看起来像是对的。
    """
    cid = _add_collection(client, name="只有电影", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    _pin(client, cid)

    scoped = client.get(
        f"/Items/Latest?parentId={collection_view_guid(cid)}&limit=50", headers=_headers(token)
    )
    assert scoped.status_code == 200, scoped.text
    names = {row["Id"] for row in scoped.json()}
    assert names, "电影库里有片，这一行不该是空的"

    everything = client.get("/Items/Latest?limit=50", headers=_headers(token))
    assert {row["Id"] for row in everything.json()} > names, "不给 parentId 时才是全库最新"


def test_virtual_folders_lists_virtual_libraries(
    client: TestClient, token: str, seeded: dict
) -> None:
    """Infuse 添加媒体库时读的那张表也要有它，否则伪装只做了一半。"""
    cid = _add_collection(
        client, name="Infuse 也要", library_id=seeded["movie_lib"], rules=ALL_ITEMS
    )
    _pin(client, cid)

    resp = client.get("/Library/VirtualFolders", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    infos = {info["ItemId"]: info for info in resp.json()}
    assert collection_view_guid(cid) in infos
    info = infos[collection_view_guid(cid)]
    # 虚拟库没有磁盘路径，也没有自己的扫描任务线
    assert info["Locations"] == []
    assert info["RefreshStatus"] == "Idle"

    options = client.get("/UserViews/GroupingOptions", headers=_headers(token))
    assert options.status_code == 200
    assert collection_view_guid(cid) in {opt["Id"] for opt in options.json()}


def test_virtual_library_cover_falls_back_to_first_member(
    client: TestClient, token: str, seeded: dict
) -> None:
    """封面走合集那套「借首个成员的海报」，不为虚拟库再生成一套库封面拼贴。"""
    cid = _add_collection(client, name="封面", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    _pin(client, cid)
    views = client.get("/UserViews", headers=_headers(token)).json()["Items"]
    view = next(v for v in views if v["Id"] == collection_view_guid(cid))
    # 有成员就该有 Primary tag（tag 本身是首个成员的条目 GUID）
    assert view["ImageTags"].get("Primary")
