"""合集接口的 HTTP 形态（docs/design/library-collections.md 第 3 节）。

领域层的规则求值在 test_collections.py 里压；这里只看接口这一面：写进去的
合集读得回来（事务边界）、空合集默认不列、卡片封面由服务端一并给出。
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    Library,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    utcnow,
)

#: 「收全部」：空 values 的 any_of 不收窄，等于把整库收进来。
ALL_ITEMS = [{"field": "genres", "op": "any_of", "values": []}]


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'col.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()

    assets = tmp_path / "metadata" / "images" / "1"
    assets.mkdir(parents=True)
    (assets / "poster.jpg").write_bytes(b"\xff\xd8\xff\xdb" + b"0" * 64)

    async def _seed() -> None:
        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        async with get_database().session() as session:
            library = Library(name="电影", kind="movie", root_paths=[str(tmp_path / "media")])
            session.add(library)
            await session.flush()
            item = MediaItem(
                kind="movie",
                tmdb_id=27205,
                title="盗梦空间",
                original_title="Inception",
                year=2010,
            )
            session.add(item)
            await session.flush()
            session.add_all(
                [
                    MediaMetadata(
                        media_item_id=item.id,
                        genre_ids=[878],
                        release_date=date(2010, 7, 15),
                        poster_file="1/poster.jpg",
                        scraped_at=utcnow(),
                    ),
                    LibraryFile(
                        library_id=library.id,
                        media_item_id=item.id,
                        season_number=0,
                        episode_number=0,
                        file_path=str(tmp_path / "media" / "a.mkv"),
                        size_bytes=4096,
                        source=FileSource.SCANNED,
                        state=FileState.IN_PLACE,
                    ),
                ]
            )
            await session.commit()
        await dispose_db()

    asyncio.run(_seed())

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.api.routes import libraries as library_routes
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    async def skip_initial_scan(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        """本文件只测合集，建库顺带的扫描跳过（扫描另有专项测试）。"""

    monkeypatch.setattr(library_routes, "enqueue_scan_job", skip_initial_scan)

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _create(client: TestClient, **payload) -> dict:
    resp = client.post("/api/v1/collections", json={"library_id": 1, **payload})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_created_collection_is_readable_afterwards(client: TestClient) -> None:
    """写完能读回来——事务边界在路由，漏 commit 时这条会红。

    get_session 只负责给会话、不负责提交（那是调用方的事）。漏了 commit 的
    表现很迷惑：创建返回 200 且带着 id，下一次 GET 却一条都没有。
    """
    created = _create(client, name="科幻", rules=ALL_ITEMS)
    listed = client.get("/api/v1/collections?library_id=1").json()["data"]
    assert [row["id"] for row in listed] == [created["id"]]
    assert client.get(f"/api/v1/collections/{created['id']}").json()["data"]["name"] == "科幻"


def test_shape_is_reported_not_guessed(client: TestClient) -> None:
    """形态由服务端推导后下发，客户端不必按 rules 自己再猜一遍。"""
    smart = _create(client, name="会自己长", rules=ALL_ITEMS)
    assert smart["rule_driven"] is True and smart["editable"] is True
    fixed = _create(client, name="固定名单", item_ids=[1])
    assert fixed["rule_driven"] is False


def test_covers_come_from_the_wall_aggregate(client: TestClient) -> None:
    """卡片封面由服务端一并给出，且与海报墙上的是同一张。

    不给的话，客户端要为每个合集再请求一次成员才画得出卡片——一屏合集
    就是一屏请求。
    """
    created = _create(client, name="有封面", rules=ALL_ITEMS)
    assert created["item_count"] == 1
    assert created["covers"], "成员有海报时合集卡片就该有封面"
    wall = client.get("/api/v1/libraries/1/items").json()["data"]
    assert created["covers"][0]["url"] == wall[0]["poster_url"]


def test_empty_collection_is_hidden_unless_asked_for(client: TestClient) -> None:
    """成员为 0 的合集默认不列：点进去空无一物的合集是纯粹的死路。"""
    _create(client, name="一个都不命中", rules=[{"field": "genres", "op": "any_of", "values": [9]}])
    # 浏览口径一条都不给（内置的「我的收藏」此刻也是空的，同样不列）
    assert client.get("/api/v1/collections").json()["data"] == []
    managed = client.get("/api/v1/collections?include_empty=true").json()["data"]
    assert "一个都不命中" in {row["name"] for row in managed}


def test_delete_removes_the_view_not_the_items(client: TestClient) -> None:
    """删的是那层视图，作品一部都不会少——合集从来不拥有作品。"""
    created = _create(client, name="待删", rules=ALL_ITEMS)
    assert client.delete(f"/api/v1/collections/{created['id']}").status_code == 200
    assert client.get(f"/api/v1/collections/{created['id']}").status_code == 404
    assert len(client.get("/api/v1/libraries/1/items").json()["data"]) == 1


def test_unknown_collection_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/collections/9999").status_code == 404


def test_snapshot_fixes_the_current_hits(client: TestClient) -> None:
    """「固定这 N 部」在服务端定格：客户端只表达意图，不回传 id。

    传 rules + snapshot 建出来的是名单驱动的合集——规则被消化掉了，此后
    新入库的片不再自动进来。
    """
    created = _create(client, name="就这一批", rules=ALL_ITEMS, snapshot=True)
    assert created["rule_driven"] is False
    assert created["rules"] == []
    assert created["item_count"] == 1


def test_new_library_gets_the_builtin_favorites_collection(client: TestClient) -> None:
    """「我的收藏」是内置合集，跟着库一起建——这个抽象要吃掉既有特例。

    它成员为 0 时同样不列（默认不列空合集），所以这里问的是管理口径。
    """
    resp = client.post(
        "/api/v1/libraries",
        json={"name": "新库", "kind": "movie", "root_paths": ["/new"]},
    )
    assert resp.status_code == 200, resp.text
    library_id = resp.json()["data"]["id"]
    rows = client.get(
        f"/api/v1/collections?library_id={library_id}&include_empty=true"
    ).json()["data"]
    fav = next(row for row in rows if row["builtin"] == f"favorites:{library_id}")
    assert fav["name"] == "我的收藏"
    # 内置合集不可改规则——改了它就不是那个合集了
    assert fav["editable"] is False
    assert fav["rule_driven"] is True
    assert fav["kind"] == "builtin"
    assert client.put(f"/api/v1/collections/{fav['id']}", json={"rules": []}).status_code == 400


def test_deleting_an_automatic_collection_leaves_a_tombstone(client: TestClient) -> None:
    """自动生成的合集「删除」= 隐藏，而且必须能放回来。

    真删了下次 ensure 又会长回来，用户会觉得"删不掉"；藏了却找不回来，
    那颗按钮就是单向黑洞。两头都得堵上（设计文档 4.6.4）。
    """
    library_id = client.post(
        "/api/v1/libraries",
        json={"name": "墓碑库", "kind": "movie", "root_paths": ["/tomb"]},
    ).json()["data"]["id"]

    def listed(**params: object) -> list[dict]:
        query = "&".join(f"{k}={str(v).lower()}" for k, v in params.items())
        url = f"/api/v1/collections?library_id={library_id}&include_empty=true&{query}"
        return client.get(url).json()["data"]

    fav = next(row for row in listed() if row["builtin"] == f"favorites:{library_id}")
    assert fav["hidden"] is False

    # 删 → 200（不是 400），行还在，只是不列
    assert client.delete(f"/api/v1/collections/{fav['id']}").status_code == 200
    assert [row["id"] for row in listed()] == []
    tomb = next(row for row in listed(include_hidden=True) if row["id"] == fav["id"])
    assert tomb["hidden"] is True
    assert tomb["name"] == "我的收藏"  # 名字/封面/顺序都留着

    # 回头路：取消隐藏
    assert (
        client.put(f"/api/v1/collections/{fav['id']}", json={"hidden": False}).status_code == 200
    )
    assert [row["id"] for row in listed()] == [fav["id"]]


def test_libraries_created_outside_the_service_are_healed_at_startup(
    client: TestClient,
) -> None:
    """绕过建库接口写进来的库，启动时补齐内置合集。

    这个夹具里的库就是直接插进去的——正是"迁移只补得到迁移那一刻已有的库"
    补不到的那一类。
    """
    rows = client.get("/api/v1/collections?library_id=1&include_empty=true").json()["data"]
    assert [row["builtin"] for row in rows] == ["favorites:1"]


# ---------------------------------------------------------------------------
# 手动合集：加入 / 移出 / 排序（F4）
# ---------------------------------------------------------------------------


def test_manual_membership_is_idempotent(client: TestClient) -> None:
    """加两次不重复、不报错。

    这个动作会从海报悬浮、详情页、批量选择多处发起，用户还可能连点两下——
    把"已经加过了"做成错误，只会逼每个调用方先查一遍。
    """
    row = _create(client, name="周末陪娃看", item_ids=[])
    add = client.post(f"/api/v1/collections/{row['id']}/items", json={"media_item_ids": [1]})
    assert add.status_code == 200, add.text
    assert add.json()["data"]["item_count"] == 1
    again = client.post(f"/api/v1/collections/{row['id']}/items", json={"media_item_ids": [1]})
    assert again.status_code == 200
    assert again.json()["data"]["item_count"] == 1


def test_manual_membership_refuses_rule_driven(client: TestClient) -> None:
    """规则驱动的合集拒绝手工增删，并把出路说清楚。

    往里塞会**静默消失**（resolve_members 对它压根不看 collection_item），
    那比报错糟得多。
    """
    smart = _create(client, name="会自己长", rules=ALL_ITEMS)
    resp = client.post(f"/api/v1/collections/{smart['id']}/items", json={"media_item_ids": [1]})
    assert resp.status_code == 400
    assert "新建" in resp.json()["message"]


def test_reorder_keeps_untouched_members_at_the_tail(client: TestClient) -> None:
    """只传了一部分时，没传的按原序接在后面——不能当成"要删"。

    前端可能只把当前这一页传上来，把没传的删掉会在分页的合集里吃掉成员。
    """
    row = _create(client, name="片单", item_ids=[1])
    resp = client.put(f"/api/v1/collections/{row['id']}/order", json={"media_item_ids": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["item_count"] == 1


def test_removing_a_member_does_not_touch_the_work(client: TestClient) -> None:
    row = _create(client, name="待删", item_ids=[1])
    resp = client.delete(f"/api/v1/collections/{row['id']}/items/1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["item_count"] == 0
    # 作品还在库里
    assert len(client.get("/api/v1/libraries/1/items").json()["data"]) == 1
    # 再移一次也不报错（与加入同一条幂等口径）
    assert client.delete(f"/api/v1/collections/{row['id']}/items/1").status_code == 200


def test_cross_library_collection_has_no_owner(client: TestClient) -> None:
    """跨库合集：library_id 为 null，只能是名单驱动的。

    模型第一天就留了这个口子（``library_id`` 可空），F4 才把入口打开。
    规则驱动的仍然必须指定库——跨库的规则求值排在更后面。
    """
    created = client.post(
        "/api/v1/collections", json={"name": "跨库片单", "item_ids": [1]}
    )
    assert created.status_code == 200, created.text
    row = created.json()["data"]
    assert row["library_id"] is None
    assert row["rule_driven"] is False
    assert row["item_count"] == 1

    # 成员照常取得到，而且每一格带着自己那个库——跨库墙上要落回各自的库
    items = client.get(f"/api/v1/collections/{row['id']}/items").json()["data"]
    assert [i["media_item_id"] for i in items] == [1]
    assert items[0]["library_id"] == 1

    # 规则驱动的跨库合集仍然不给建
    refused = client.post("/api/v1/collections", json={"name": "跨库规则", "rules": ALL_ITEMS})
    assert refused.status_code == 400


# ---------------------------------------------------------------------------
# 作品详情页那一行「合集」（反查）
# ---------------------------------------------------------------------------


def _detail(client: TestClient, item_id: int = 1) -> dict:
    resp = client.get(f"/api/v1/libraries/1/items/{item_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_item_detail_lists_the_collections_it_belongs_to(client: TestClient) -> None:
    """规则驱动与名单驱动都要认出来——两种合集在这一行上没有区别。"""
    assert _detail(client)["collections"] == []

    smart = _create(client, name="科幻", rules=[{"field": "genres", "values": [878]}])
    manual = _create(client, name="周末清单", item_ids=[1])
    # 收不到这部片的合集不该出现
    _create(client, name="动画", rules=[{"field": "genres", "values": [16]}])

    names = {c["name"] for c in _detail(client)["collections"]}
    assert names == {"科幻", "周末清单"}
    ids = {c["id"] for c in _detail(client)["collections"]}
    assert ids == {smart["id"], manual["id"]}


def test_the_row_never_repeats_what_is_already_on_screen(client: TestClient) -> None:
    """「我的收藏」不出现：那颗心就在几十像素之外，同一件事说两遍是噪音。"""
    resp = client.post("/api/v1/playback/marks", json={"media_item_id": 1, "favorite": True})
    assert resp.status_code == 200, resp.text
    listed = {row["name"] for row in client.get("/api/v1/collections?library_id=1").json()["data"]}
    assert "我的收藏" in listed, "内置收藏合集本身仍然存在"
    assert _detail(client)["collections"] == [], "但它不该再在详情页上说一遍"


def test_hidden_collections_drop_out_of_the_row(client: TestClient) -> None:
    """隐藏是给自动合集的回头路，藏起来的就不该继续在详情页上露脸。"""
    created = _create(client, name="科幻", rules=[{"field": "genres", "values": [878]}])
    assert [c["name"] for c in _detail(client)["collections"]] == ["科幻"]

    resp = client.put(f"/api/v1/collections/{created['id']}", json={"hidden": True})
    assert resp.status_code == 200, resp.text
    assert _detail(client)["collections"] == []


def test_the_row_and_the_collection_page_never_disagree(client: TestClient) -> None:
    """反查与正查必须是同一个答案。

    这一条是整块的意义所在：详情页说"它在「科幻」里"、点进去却找不到它，
    是最难查的一类不一致。反查走的是 resolve_members 同一条路径，候选集
    收窄成一个条目而已，所以这条保证是结构性的。
    """
    created = _create(client, name="科幻", rules=[{"field": "genres", "values": [878]}])
    said_it_belongs = {c["id"] for c in _detail(client)["collections"]}
    members = client.get(f"/api/v1/collections/{created['id']}/items").json()["data"]
    listed_there = {row["media_item_id"] for row in members}

    assert created["id"] in said_it_belongs
    assert 1 in listed_there

    # 把它移出规则的射程：两处必须同时变
    resp = client.put(
        f"/api/v1/collections/{created['id']}",
        json={"rules": [{"field": "genres", "values": [16]}]},
    )
    assert resp.status_code == 200, resp.text
    assert _detail(client)["collections"] == []
    assert client.get(f"/api/v1/collections/{created['id']}/items").json()["data"] == []
