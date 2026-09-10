"""合集 → Jellyfin BoxSet（docs/design/library-collections.md 第 4 节）。

走 4.10 那张客户端核对清单：UserViews 里有没有「合集」视图、打开它拿到什么、
点进一个 BoxSet 拿到什么、单条 BoxSet 长什么样，以及不可见的合集是不是真的
一条都不下发。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.jellyfin.helpers import AUTH_HEADER, jf_login

from movieclaw_jellyfin.ids import collection_guid, collections_view_guid, library_guid


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f'{AUTH_HEADER}, Token="{token}"'}


def _add_collection(client: TestClient, *, name: str, library_id: int, **kw) -> int:
    """通过真实接口建合集。

    刻意不直接写库：这样这组用例同时验证了"两个面看的是同一份数据"——
    网页端建的合集，电视端立刻就该看得见。
    """
    payload = {"name": name, "library_id": library_id, **kw}
    resp = client.post("/api/v1/collections", json=payload)
    assert resp.status_code == 200, resp.text
    return int(resp.json()["data"]["id"])


#: 「收全部」的规则：空 values 的 any_of 不收窄，等于把整库收进来。
#: 用它是为了让用例不依赖播种数据的具体类型 id。
ALL_ITEMS = [{"field": "genres", "op": "any_of", "values": []}]


@pytest.fixture
def token(client: TestClient) -> str:
    return jf_login(client)


def test_collections_view_absent_without_collections(client: TestClient, token: str) -> None:
    """一个合集都没有时不下发「合集」视图——空视图在电视端是纯粹的死路。"""
    resp = client.get("/UserViews", headers=_headers(token))
    assert resp.status_code == 200
    assert collections_view_guid() not in {v["Id"] for v in resp.json()["Items"]}


def test_collections_view_appears_and_lists_boxsets(
    client: TestClient, token: str, seeded: dict
) -> None:
    """有可见合集时多出一个 CollectionType=boxsets 的视图，点开是 BoxSet 列表。"""
    cid = _add_collection(client, name="诺兰", library_id=seeded["movie_lib"], rules=ALL_ITEMS)

    views = client.get("/UserViews", headers=_headers(token)).json()["Items"]
    view = next((v for v in views if v["Id"] == collections_view_guid()), None)
    assert view is not None, "有合集就该有「合集」视图"
    assert view["CollectionType"] == "boxsets"
    assert view["Type"] == "CollectionFolder"

    listed = client.get(
        f"/Items?ParentId={collections_view_guid()}", headers=_headers(token)
    ).json()
    boxsets = {row["Id"]: row for row in listed["Items"]}
    assert collection_guid(cid) in boxsets
    row = boxsets[collection_guid(cid)]
    assert row["Type"] == "BoxSet"
    assert row["IsFolder"] is True
    # ChildCount 只在「把合集列出来」的请求里给
    assert row["ChildCount"] >= 1
    # CollectionType 是 CollectionFolder 的字段，BoxSet 不该有
    assert "CollectionType" not in row


def test_boxset_children_are_items(client: TestClient, token: str, seeded: dict) -> None:
    """点进 BoxSet 拿到的是成员条目——协议语义里 BoxSet 的子级只能是作品。"""
    cid = _add_collection(client, name="全部电影", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    resp = client.get(f"/Items?ParentId={collection_guid(cid)}", headers=_headers(token))
    assert resp.status_code == 200
    types = {row["Type"] for row in resp.json()["Items"]}
    assert types and types <= {"Movie", "Series", "Video"}


def test_single_boxset_detail(client: TestClient, token: str, seeded: dict) -> None:
    cid = _add_collection(client, name="单条", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    resp = client.get(f"/Items/{collection_guid(cid)}", headers=_headers(token))
    assert resp.status_code == 200
    dto = resp.json()
    assert dto["Type"] == "BoxSet" and dto["Name"] == "单条"
    assert dto["ParentId"] == collections_view_guid()


def test_include_item_types_boxset_at_root(client: TestClient, token: str, seeded: dict) -> None:
    """根级 IncludeItemTypes=BoxSet——不少客户端的首页是这么拼的。"""
    _add_collection(client, name="根级问法", library_id=seeded["movie_lib"], rules=ALL_ITEMS)
    resp = client.get("/Items?IncludeItemTypes=BoxSet&Recursive=true", headers=_headers(token))
    assert resp.status_code == 200
    assert {row["Type"] for row in resp.json()["Items"]} == {"BoxSet"}


def test_empty_collection_is_not_listed(client: TestClient, token: str, seeded: dict) -> None:
    """成员为空的合集不列：点进去空无一物是纯粹的死路。"""
    _add_collection(
        client,
        name="一个都不命中",
        library_id=seeded["movie_lib"],
        rules=[{"field": "genres", "op": "any_of", "values": [999999]}],
    )
    listed = client.get(
        f"/Items?ParentId={collections_view_guid()}", headers=_headers(token)
    ).json()
    assert "一个都不命中" not in {row["Name"] for row in listed["Items"]}


def test_private_collection_reaches_its_owner(
    client: TestClient, token: str, seeded: dict
) -> None:
    """私有合集对**本人**照常下发——私有路径端到端是通的。

    「对别人不可见」这一半在 tests/api/test_collections.py 的领域层用例里覆盖：
    这里只有一个登录身份（超管），表达不出"另一个成员"，硬凑一个只会让这条
    用例名不副实。
    """
    cid = _add_collection(
        client,
        name="只有我",
        library_id=seeded["movie_lib"],
        rules=ALL_ITEMS,
        visibility="private",
    )
    detail = client.get(f"/Items/{collection_guid(cid)}", headers=_headers(token))
    assert detail.status_code == 200
    assert detail.json()["Name"] == "只有我"

    listed = client.get(
        f"/Items?ParentId={collections_view_guid()}", headers=_headers(token)
    ).json()
    assert "只有我" in {row["Name"] for row in listed["Items"]}


def test_unknown_collection_guid_is_404(client: TestClient, token: str) -> None:
    """不存在的合集 GUID 按 404——GUID 可枚举，不能回一个空对象确认它存在。"""
    resp = client.get(f"/Items/{collection_guid(999999)}", headers=_headers(token))
    assert resp.status_code == 404


def test_display_collections_view_is_declared(client: TestClient, token: str) -> None:
    """UserConfiguration 如实声明支持合集视图——有些客户端只在它为真时才渲染入口。"""
    me = client.get("/Users/Me", headers=_headers(token))
    assert me.status_code == 200
    assert me.json()["Configuration"]["DisplayCollectionsView"] is True


def test_builtin_favorites_reaches_the_tv(client: TestClient, token: str, seeded: dict) -> None:
    """「我的收藏」是内置合集，所以它自己就会以 BoxSet 出现——不必写特例。

    这条同时压住"空的内置合集不该把视图撑出来"：收藏之前一个 BoxSet 都没有，
    「合集」视图也就不下发。
    """
    assert collections_view_guid() not in {
        v["Id"] for v in client.get("/UserViews", headers=_headers(token)).json()["Items"]
    }

    resp = client.post(
        "/api/v1/playback/marks",
        json={"media_item_id": seeded["movie"], "favorite": True},
    )
    assert resp.status_code == 200, resp.text

    listed = client.get(
        f"/Items?ParentId={collections_view_guid()}", headers=_headers(token)
    ).json()
    assert "我的收藏" in {row["Name"] for row in listed["Items"]}


def test_boxsets_under_a_library_stay_in_that_library(
    client: TestClient, token: str, seeded: dict
) -> None:
    """``?ParentId=<电影库>&IncludeItemTypes=BoxSet`` 只回这个库的合集。

    以前 ``IncludeItemTypes=BoxSet`` 那条短路完全不看 ParentId，于是在电影库里
    问会把剧集库的合集一起返回。合集只有一两个时看不出来，自动生成系列合集
    之后就是明显的串库。
    """
    movie_cid = _add_collection(
        client, name="电影侧", library_id=seeded["movie_lib"], rules=ALL_ITEMS
    )
    tv_cid = _add_collection(client, name="剧集侧", library_id=seeded["tv_lib"], rules=ALL_ITEMS)

    resp = client.get(
        f"/Items?ParentId={library_guid(seeded['movie_lib'])}&IncludeItemTypes=BoxSet",
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    ids = {row["Id"] for row in resp.json()["Items"]}
    assert collection_guid(movie_cid) in ids
    assert collection_guid(tv_cid) not in ids

    # 合集视图本身（不带 ParentId 的库）照旧两个都给
    everything = client.get(
        f"/Items?ParentId={collections_view_guid()}", headers=_headers(token)
    ).json()["Items"]
    assert {collection_guid(movie_cid), collection_guid(tv_cid)} <= {r["Id"] for r in everything}


def test_boxset_list_honours_sort_by(client: TestClient, token: str, seeded: dict) -> None:
    """客户端给的 ``SortBy`` 对合集列表要真的生效。

    这条路径以前绕过排序直接按 position 下发，一两个合集时没人察觉，
    几十个时用户会觉得"排序坏了"。
    """
    lib = seeded["movie_lib"]
    _add_collection(client, name="乙", library_id=lib, rules=ALL_ITEMS)
    _add_collection(client, name="甲", library_id=lib, rules=ALL_ITEMS)

    def names(query: str) -> list[str]:
        resp = client.get(
            f"/Items?ParentId={collections_view_guid()}&{query}", headers=_headers(token)
        )
        assert resp.status_code == 200, resp.text
        return [row["Name"] for row in resp.json()["Items"]]

    ascending = names("SortBy=SortName&SortOrder=Ascending")
    assert ascending == sorted(ascending, key=str.lower)
    assert names("SortBy=SortName&SortOrder=Descending") == list(reversed(ascending))


def test_paging_reports_the_true_total(client: TestClient, token: str, seeded: dict) -> None:
    """空合集在分页**之前**滤掉，``TotalRecordCount`` 才是对的。"""
    lib = seeded["movie_lib"]
    for name in ("一", "二", "三"):
        _add_collection(client, name=name, library_id=lib, rules=ALL_ITEMS)
    # 一个永远命中不了的条件：它不该占掉 total 的一格
    _add_collection(
        client,
        name="空的",
        library_id=lib,
        rules=[{"field": "genres", "op": "any_of", "values": [-1]}],
    )
    resp = client.get(
        f"/Items?ParentId={collections_view_guid()}&Limit=2", headers=_headers(token)
    )
    body = resp.json()
    assert len(body["Items"]) == 2
    assert body["TotalRecordCount"] == 3
