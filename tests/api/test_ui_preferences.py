"""界面偏好接口的端到端测试。

覆盖：默认值、保存持久化、未知字段前向兼容（忽略不报错）。
鉴权由 test_auth 的守护测试统一覆盖（/ui 挂在受保护区）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    # 配置存储/加密器是模块级单例，用例间必须手动重置，避免缓存串库
    reset_setting_store()
    reset_secret_box()

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c

    reset_setting_store()
    reset_secret_box()
    get_settings.cache_clear()


# 各页面的默认样式（与 UiPreferencesSetting 各分组默认值一致）
DEFAULT_PREFS = {
    "sidebar": {"transparency": 0.49, "brightness": -0.36, "depth": 28.0},
    "scrim": {"blur": 13.0, "dark": 0.69},
    # 空顺序 = 侧栏主导航用内置默认排布
    "nav": {"order": []},
    # 空清单 = 媒体库首页用出厂布局
    "home": {"rows": []},
}


def test_default_ui_preferences(client: TestClient) -> None:
    resp = client.get("/api/v1/ui/preferences")
    assert resp.status_code == 200
    assert resp.json()["data"] == DEFAULT_PREFS


def test_unknown_fields_ignored_for_forward_compat(client: TestClient) -> None:
    """旧版前端多传/新版前端少传字段都不报错：多的忽略、少的补默认。

    ``search`` 分组是历史遗留（图览模式已改为跟随自定义分类的 poster_mode），
    旧前端传来时按未知字段忽略即可。
    """
    resp = client.put(
        "/api/v1/ui/preferences",
        json={
            "sidebar": {"transparency": 0.6, "brightness": 0.2, "future_knob": 1},
            "search": {"poster_mode": True},
            "future_page": {},
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["sidebar"]["transparency"] == 0.6
    assert "search" not in data
    assert "future_page" not in data

    # 少传：空对象 → 全部回默认
    resp = client.put("/api/v1/ui/preferences", json={})
    assert resp.status_code == 200
    assert resp.json()["data"] == DEFAULT_PREFS


def test_save_sidebar_prefs_persists(client: TestClient) -> None:
    saved = {"transparency": 0.6, "brightness": 0.2, "depth": 60.0}
    resp = client.put("/api/v1/ui/preferences", json={"sidebar": saved})
    assert resp.status_code == 200
    assert resp.json()["data"]["sidebar"] == saved

    # 再次 GET：真正落库
    data = client.get("/api/v1/ui/preferences").json()["data"]
    assert data["sidebar"] == saved


def test_sidebar_prefs_out_of_range_rejected(client: TestClient) -> None:
    """透明度超出 0~1、明暗超出 -1~1、厚度超出 10~90 时整体拒绝（422），不产生半写入。"""
    for payload in (
        {"sidebar": {"transparency": 1.5, "brightness": 0.0}},
        {"sidebar": {"transparency": 0.0, "brightness": -2}},
        {"sidebar": {"depth": 5}},
        {"sidebar": {"depth": 120}},
    ):
        resp = client.put("/api/v1/ui/preferences", json=payload)
        assert resp.status_code == 422

    # 越界请求未污染存储，仍是默认值
    assert client.get("/api/v1/ui/preferences").json()["data"] == DEFAULT_PREFS


def test_save_scrim_prefs_persists(client: TestClient) -> None:
    saved = {"blur": 22.0, "dark": 0.8}
    resp = client.put("/api/v1/ui/preferences", json={"scrim": saved})
    assert resp.status_code == 200
    assert resp.json()["data"]["scrim"] == saved

    # 再次 GET：真正落库
    assert client.get("/api/v1/ui/preferences").json()["data"]["scrim"] == saved


def test_scrim_prefs_out_of_range_rejected(client: TestClient) -> None:
    """模糊度超出 0~40、暗度超出 0~1 时整体拒绝（422），不产生半写入。"""
    for payload in (
        {"scrim": {"blur": -1}},
        {"scrim": {"blur": 50}},
        {"scrim": {"dark": -0.1}},
        {"scrim": {"dark": 1.5}},
    ):
        resp = client.put("/api/v1/ui/preferences", json=payload)
        assert resp.status_code == 422

    assert client.get("/api/v1/ui/preferences").json()["data"] == DEFAULT_PREFS


def test_save_nav_order_persists(client: TestClient) -> None:
    """侧栏主导航的个人排序整体覆盖保存；顺序原样保留（后端不做业务校验）。"""
    saved = ["subscriptions", "library", "new", "explore-movies", "explore-tv"]
    resp = client.put("/api/v1/ui/preferences", json={"nav": {"order": saved}})
    assert resp.status_code == 200
    assert resp.json()["data"]["nav"]["order"] == saved

    # 再次 GET：真正落库
    assert client.get("/api/v1/ui/preferences").json()["data"]["nav"]["order"] == saved


def test_nav_order_accepts_unknown_ids(client: TestClient) -> None:
    """顺序里出现界面上已不存在的 id 不报错：读取端负责忽略，避免升级后写不进设置。"""
    resp = client.put(
        "/api/v1/ui/preferences", json={"nav": {"order": ["library", "removed-in-v2"]}}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["nav"]["order"] == ["library", "removed-in-v2"]


def test_nav_order_too_long_rejected(client: TestClient) -> None:
    """超过 32 项整体拒绝（422）：防脏数据无限增长，正常主导航只有个位数项。"""
    resp = client.put(
        "/api/v1/ui/preferences", json={"nav": {"order": [f"item-{i}" for i in range(33)]}}
    )
    assert resp.status_code == 422
    assert client.get("/api/v1/ui/preferences").json()["data"] == DEFAULT_PREFS


# ---------------------------------------------------------------------------
# 媒体库首页的行清单（docs/design/library-home-perspective.md）
# ---------------------------------------------------------------------------


def test_home_rows_default_is_empty(client: TestClient) -> None:
    """空清单 = 出厂布局，与 nav.order 同一约定。"""
    assert client.get("/api/v1/ui/preferences").json()["data"]["home"] == {"rows": []}


def test_home_rows_persist_with_optional_fields_left_null(client: TestClient) -> None:
    rows = [
        {"id": "up-next"},
        {"id": "favorites", "sort": "unwatched_first"},
        {"id": "libraries", "hidden": True},
        {"id": "lib:1", "sort": "release_date", "name": ""},
        {
            "id": "row:8f2c",
            "library_id": 3,
            "sort": "rating",
            "unwatched": True,
            "name": "评分最高的动漫",
        },
        {"id": "row:a91e", "collection_id": 7, "sort": "release_date_asc"},
    ]
    resp = client.put("/api/v1/ui/preferences", json={"home": {"rows": rows}})
    assert resp.status_code == 200
    saved = client.get("/api/v1/ui/preferences").json()["data"]["home"]["rows"]
    assert [r["id"] for r in saved] == [r["id"] for r in rows]
    assert saved[4]["name"] == "评分最高的动漫" and saved[4]["unwatched"] is True
    # 没传的字段是 null，不会被补成假默认（空即默认由前端解释）
    assert saved[0]["sort"] is None and saved[0]["hidden"] is None


@pytest.mark.parametrize(
    "row",
    [
        {"id": "weird"},  # 认不出的 id 形状
        {"id": "row:x"},  # 自加行没来源
        {"id": "row:x", "library_id": 1, "collection_id": 2},  # 两个来源
        {"id": "lib:1", "library_id": 1},  # 默认库行不能带来源
        {"id": "up-next", "sort": "rating"},  # 接下来继续没有排序
        {"id": "favorites", "sort": "random"},  # 收藏行不支持随机
        {"id": "lib:1", "sort": "size"},  # 首页行不开放体积档
        {"id": "row:x", "library_id": 1, "name": "x" * 41},  # 名字过长
    ],
)
def test_home_rows_bad_shape_rejected(client: TestClient, row: dict) -> None:
    resp = client.put("/api/v1/ui/preferences", json={"home": {"rows": [row]}})
    assert resp.status_code == 422
    assert client.get("/api/v1/ui/preferences").json()["data"]["home"] == {"rows": []}


def test_home_rows_duplicate_ids_and_overflow_rejected(client: TestClient) -> None:
    dup = [{"id": "lib:1"}, {"id": "lib:1"}]
    assert client.put("/api/v1/ui/preferences", json={"home": {"rows": dup}}).status_code == 422
    many = [{"id": f"row:{i}", "library_id": 1} for i in range(129)]
    assert client.put("/api/v1/ui/preferences", json={"home": {"rows": many}}).status_code == 422


def test_home_rows_unknown_targets_accepted(client: TestClient) -> None:
    """指向已删库 / 不可见合集的行照存：读取端（前端合并）负责忽略，与 nav 同款。"""
    rows = [{"id": "lib:999"}, {"id": "row:gone", "collection_id": 12345}]
    resp = client.put("/api/v1/ui/preferences", json={"home": {"rows": rows}})
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()["data"]["home"]["rows"]] == ["lib:999", "row:gone"]
