"""成员体系的端到端测试（docs/design/member-management.md P0）。

覆盖四块：
1. 成员管理 CRUD：建号/编辑开关与白名单/重置密码/启停/删除。
2. 成员登录与会话：role 视图、能力开关快照、停用即时踢下线、
   改密只踢自己（超管会话不受影响）。
3. 能力开关行为：默认成员可订阅不可搜索；管理员开启 allow_search 后放行。
4. 守护测试（越权默认拒绝兜底）：以**默认能力的成员**会话遍历 OpenAPI 里
   的每一条路由，凡不在成员白名单里的必须被拒（403 管理员专属 /
   403 能力未开 / 401 独立密钥体系）。以后新增路由忘了声明归属，这里直接红。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box

_AUTH = "/api/v1/auth"
_MEMBERS = "/api/v1/members"
_ADMIN = {"username": "admin", "password": "s3cret-pass"}
_MEMBER = {"username": "family", "password": "family-pass-1"}
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # 订阅读接口的服务组装依赖 TMDB 客户端；测试不发真实请求，占位即可
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def _login(client: TestClient, username: str, password: str) -> str:
    """登录并返回会话 Cookie 值（随后可用 _use 在多身份间切换）。"""
    client.cookies.clear()
    resp = client.post(f"{_AUTH}/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return client.cookies.get("movieclaw_session")


def _use(client: TestClient, cookie: str) -> None:
    """切换当前会话到指定 Cookie。"""
    client.cookies.clear()
    client.cookies.set("movieclaw_session", cookie)


def _setup_admin_and_member(client: TestClient) -> tuple[str, str, int]:
    """建超管 + 建默认成员，返回 (超管 Cookie, 成员 Cookie, 成员 id)。"""
    resp = client.post(f"{_AUTH}/bootstrap", json=_ADMIN)
    assert resp.status_code == 200, resp.text
    admin_cookie = client.cookies.get("movieclaw_session")

    created = client.post(_MEMBERS, json=_MEMBER)
    assert created.status_code == 200, created.text
    member_id = created.json()["data"]["id"]

    member_cookie = _login(client, _MEMBER["username"], _MEMBER["password"])
    return admin_cookie, member_cookie, member_id


# ---------------------------------------------------------------------------
# 成员管理 CRUD
# ---------------------------------------------------------------------------


def test_member_crud_flow(client: TestClient) -> None:
    """建号 → 列表 → 编辑开关与白名单 → 重置密码 → 停用/启用 → 删除。"""
    admin_cookie, _member_cookie, member_id = _setup_admin_and_member(client)
    _use(client, admin_cookie)

    # 列表包含新成员，默认开关：可订阅 / 不可搜索 / 全库全站可见
    rows = client.get(_MEMBERS).json()["data"]
    assert [r["username"] for r in rows] == ["family"]
    assert rows[0]["allow_subscribe"] is True
    assert rows[0]["allow_search"] is False
    assert rows[0]["all_libraries"] is True

    # 编辑：开启搜索、切库白名单（先建一个真实库；无效库 id 给 400 而非 500）
    lib = client.post(
        "/api/v1/libraries",
        json={"name": "电影库", "kind": "movie", "root_paths": ["/data/movies-test"]},
    )
    assert lib.status_code == 200, lib.text
    library_id = lib.json()["data"]["id"]

    invalid = client.put(
        f"{_MEMBERS}/{member_id}",
        json={"all_libraries": False, "library_ids": [library_id, 9999]},
    )
    assert invalid.status_code == 400
    # 校验失败必须整体不生效：不能留下"白名单模式已开、白名单却没存上"
    # 的半套权限（成员会瞬间全库不可见）
    after_invalid = client.get(f"{_MEMBERS}/{member_id}").json()["data"]
    assert after_invalid["all_libraries"] is True
    assert after_invalid["library_ids"] == []

    updated = client.put(
        f"{_MEMBERS}/{member_id}",
        json={
            "allow_search": True,
            "all_libraries": False,
            "library_ids": [library_id],
            "all_sites": False,
            "site_ids": ["mteam"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["allow_search"] is True
    assert updated.json()["data"]["library_ids"] == [library_id]
    assert updated.json()["data"]["site_ids"] == ["mteam"]

    # 重置密码：新明文只回显一次，可用于登录
    reset = client.post(f"{_MEMBERS}/{member_id}/reset-password")
    assert reset.status_code == 200
    new_password = reset.json()["data"]["password"]
    assert _login(client, "family", new_password)

    # 停用后登录被拒并明确提示；启用后恢复
    _use(client, admin_cookie)
    assert client.put(f"{_MEMBERS}/{member_id}/status", json={"enabled": False}).status_code == 200
    client.cookies.clear()
    denied = client.post(f"{_AUTH}/login", json={"username": "family", "password": new_password})
    assert denied.status_code == 401
    assert "停用" in denied.json()["message"]

    _use(client, admin_cookie)
    assert client.put(f"{_MEMBERS}/{member_id}/status", json={"enabled": True}).status_code == 200
    assert _login(client, "family", new_password)

    # 删除后成员消失、无法登录
    _use(client, admin_cookie)
    assert client.delete(f"{_MEMBERS}/{member_id}").status_code == 200
    assert client.get(_MEMBERS).json()["data"] == []
    client.cookies.clear()
    assert (
        client.post(f"{_AUTH}/login", json={"username": "family", "password": new_password})
    ).status_code == 401


def test_member_username_conflicts_are_rejected(client: TestClient) -> None:
    """用户名与超管互斥（大小写不敏感）、与现有成员不重复。"""
    admin_cookie, _, _ = _setup_admin_and_member(client)
    _use(client, admin_cookie)

    conflict_admin = client.post(_MEMBERS, json={"username": "ADMIN", "password": "whatever-123"})
    assert conflict_admin.status_code == 409

    conflict_member = client.post(_MEMBERS, json={"username": "family", "password": "whatever-123"})
    assert conflict_member.status_code == 409


# ---------------------------------------------------------------------------
# 成员登录与会话语义
# ---------------------------------------------------------------------------


def test_member_session_view_carries_role_and_capabilities(client: TestClient) -> None:
    _, member_cookie, _ = _setup_admin_and_member(client)
    _use(client, member_cookie)

    me = client.get(f"{_AUTH}/me").json()["data"]
    assert me["username"] == "family"
    assert me["role"] == "member"
    assert me["capabilities"] == {
        "allow_subscribe": True,
        "allow_search": False,
        "allow_direct_download": False,
    }


def test_disable_member_kicks_active_session(client: TestClient) -> None:
    """停用即时生效：已登录会话下一次请求就 401（token_version 校验）。"""
    admin_cookie, member_cookie, member_id = _setup_admin_and_member(client)

    _use(client, member_cookie)
    assert client.get(f"{_AUTH}/me").status_code == 200

    _use(client, admin_cookie)
    client.put(f"{_MEMBERS}/{member_id}/status", json={"enabled": False})

    _use(client, member_cookie)
    assert client.get(f"{_AUTH}/me").status_code == 401


def test_member_password_change_kicks_only_self(client: TestClient) -> None:
    """成员改密：本人旧会话失效、当前会话续期；超管会话不受影响。"""
    admin_cookie, member_cookie, _ = _setup_admin_and_member(client)
    other_device = _login(client, _MEMBER["username"], _MEMBER["password"])

    _use(client, member_cookie)
    resp = client.put(
        f"{_AUTH}/password",
        json={"old_password": _MEMBER["password"], "new_password": "brand-new-pass-9"},
    )
    assert resp.status_code == 200
    # 操作者本人拿到新 Cookie，不被踢出
    assert client.get(f"{_AUTH}/me").status_code == 200

    # 该成员的另一台设备被踢
    _use(client, other_device)
    assert client.get(f"{_AUTH}/me").status_code == 401

    # 超管完全不受影响（成员改密不轮换全局密钥）
    _use(client, admin_cookie)
    assert client.get(f"{_AUTH}/me").status_code == 200


def test_member_profile_updates_do_not_touch_admin(client: TestClient) -> None:
    """成员改昵称落成员表；超管的昵称与配置不受影响。"""
    admin_cookie, member_cookie, _ = _setup_admin_and_member(client)

    _use(client, member_cookie)
    resp = client.put(f"{_AUTH}/profile", json={"nickname": "小家伙"})
    assert resp.status_code == 200
    assert resp.json()["data"]["nickname"] == "小家伙"
    assert resp.json()["data"]["role"] == "member"

    _use(client, admin_cookie)
    assert client.get(f"{_AUTH}/me").json()["data"]["nickname"] == "admin"


# ---------------------------------------------------------------------------
# 能力开关行为
# ---------------------------------------------------------------------------


def test_search_capability_gate(client: TestClient) -> None:
    """默认成员搜索被 403（含明确中文提示）；管理员开启开关后放行。"""
    admin_cookie, member_cookie, member_id = _setup_admin_and_member(client)

    _use(client, member_cookie)
    denied = client.get("/api/v1/search/torrents", params={"keyword": "沙丘"})
    assert denied.status_code == 403
    assert "搜索" in denied.json()["message"]

    _use(client, admin_cookie)
    client.put(f"{_MEMBERS}/{member_id}", json={"allow_search": True})

    _use(client, member_cookie)
    assert client.get("/api/v1/search/torrents", params={"keyword": "沙丘"}).status_code == 200


def test_torrent_history_results_cannot_bypass_search_capability(client: TestClient) -> None:
    """先搜索后被收回权限时，统一历史结果端点不能成为种子结果旁路。"""
    admin_cookie, member_cookie, member_id = _setup_admin_and_member(client)

    _use(client, admin_cookie)
    client.put(f"{_MEMBERS}/{member_id}", json={"allow_search": True})

    _use(client, member_cookie)
    assert client.get("/api/v1/search/torrents", params={"keyword": "沙丘"}).status_code == 200
    history_id = client.get("/api/v1/search/history").json()["data"][0]["id"]
    assert client.get(f"/api/v1/search/history/{history_id}/results").status_code == 200

    _use(client, admin_cookie)
    client.put(f"{_MEMBERS}/{member_id}", json={"allow_search": False})

    _use(client, member_cookie)
    denied = client.get(f"/api/v1/search/history/{history_id}/results")
    assert denied.status_code == 403
    assert "搜索" in denied.json()["message"]


def test_subscribe_capability_gate(client: TestClient) -> None:
    """关闭 allow_subscribe 后，订阅写接口与影视搜索 403；读接口不受影响。"""
    admin_cookie, member_cookie, member_id = _setup_admin_and_member(client)

    _use(client, admin_cookie)
    client.put(f"{_MEMBERS}/{member_id}", json={"allow_subscribe": False})

    _use(client, member_cookie)
    assert client.get("/api/v1/subscriptions").status_code == 200
    denied = client.post("/api/v1/subscriptions/title-preview", json={})
    assert denied.status_code == 403
    media_search = client.post(
        "/api/v1/search/titles",
        json={"query": "沙丘", "provider": "douban"},
    )
    assert media_search.status_code == 403


def test_direct_download_member_restrictions(client: TestClient) -> None:
    """一键下载（P2）：默认成员 403；开了开关后手选目录/指定下载器仍被拒
    （服务端强制自动路由），管理员不受影响。"""
    admin_cookie, member_cookie, member_id = _setup_admin_and_member(client)
    payload = {"site_id": "mteam", "download_url": "https://example.com/t.torrent"}

    _use(client, member_cookie)
    denied = client.post("/api/v1/downloaders/submit", json=payload)
    assert denied.status_code == 403
    assert "一键下载" in denied.json()["message"]

    _use(client, admin_cookie)
    client.put(f"{_MEMBERS}/{member_id}", json={"allow_direct_download": True})

    _use(client, member_cookie)
    picked_path = client.post(
        "/api/v1/downloaders/submit", json={**payload, "save_path": "/data/hand-picked"}
    )
    assert picked_path.status_code == 403
    assert "保存目录" in picked_path.json()["message"]
    picked_downloader = client.post(
        "/api/v1/downloaders/submit", json={**payload, "downloader_id": 1}
    )
    assert picked_downloader.status_code == 403
    assert "下载器" in picked_downloader.json()["message"]
    # 智能入库按收藏范围路由，可能选中成员不可见的库并回显库名——同样拒绝
    auto_routed = client.post(
        "/api/v1/downloaders/submit",
        json={**payload, "auto_route": True, "media_kind": "movie", "tmdb_id": 1, "title": "片"},
    )
    assert auto_routed.status_code == 403
    assert "智能入库" in auto_routed.json()["message"]


def test_ui_preferences_isolated_per_member(client: TestClient) -> None:
    """界面偏好（P2）：成员保存进自己的列，不覆盖超管的全局配置。"""
    admin_cookie, member_cookie, _ = _setup_admin_and_member(client)

    _use(client, admin_cookie)
    admin_prefs = client.get("/api/v1/ui/preferences").json()["data"]

    # 成员改自己的偏好（整体覆盖：在超管当前值上翻一个字段）
    _use(client, member_cookie)
    member_draft = dict(admin_prefs)
    member_draft["sidebar"] = {**admin_prefs.get("sidebar", {}), "transparency": 0.33}
    saved = client.put("/api/v1/ui/preferences", json=member_draft)
    assert saved.status_code == 200
    member_prefs = client.get("/api/v1/ui/preferences").json()["data"]
    assert member_prefs["sidebar"]["transparency"] == 0.33

    # 超管的全局配置不受影响
    _use(client, admin_cookie)
    assert client.get("/api/v1/ui/preferences").json()["data"] == admin_prefs


def test_backdrop_gallery_isolated_per_member(client: TestClient) -> None:
    """背景图库与生效项按账号隔离，匿名登录页只读取管理员图库。"""
    admin_cookie, first_cookie, first_member_id = _setup_admin_and_member(client)

    _use(client, admin_cookie)
    admin_item = client.post(
        "/api/v1/appearance/backdrops",
        files={"file": ("admin.png", _PNG_1X1, "image/png")},
    ).json()["data"]["backdrops"][0]

    second_password = "second-pass-1"
    created = client.post(
        _MEMBERS,
        json={"username": "second", "password": second_password},
    )
    assert created.status_code == 200, created.text
    second_cookie = _login(client, "second", second_password)

    _use(client, first_cookie)
    assert client.get("/api/v1/appearance").json()["data"]["backdrops"] == []
    first_view = client.post(
        "/api/v1/appearance/backdrops",
        files={"file": ("first.png", _PNG_1X1, "image/png")},
    ).json()["data"]
    first_item = first_view["backdrops"][0]
    assert first_view["active_id"] == first_item["id"]
    assert client.get(f"/api/v1/appearance/backdrops/{admin_item['id']}").status_code == 404

    _use(client, second_cookie)
    assert client.get("/api/v1/appearance").json()["data"]["backdrops"] == []
    assert client.get(f"/api/v1/appearance/backdrops/{first_item['id']}").status_code == 404
    second_view = client.post(
        "/api/v1/appearance/backdrops",
        files={"file": ("second.png", _PNG_1X1, "image/png")},
    ).json()["data"]
    second_item = second_view["backdrops"][0]

    _use(client, first_cookie)
    assert client.get("/api/v1/appearance").json()["data"]["backdrops"] == [first_item]
    assert client.get(f"/api/v1/appearance/backdrops/{second_item['id']}").status_code == 404

    _use(client, admin_cookie)
    assert client.get("/api/v1/appearance").json()["data"]["backdrops"] == [admin_item]
    assert client.get(f"/api/v1/appearance/backdrops/{first_item['id']}").status_code == 404

    client.cookies.clear()
    assert client.get("/api/v1/appearance").json()["data"]["backdrops"] == [admin_item]
    assert client.get(f"/api/v1/appearance/backdrops/{admin_item['id']}").status_code == 200

    # 删除成员时一并清除其个人图库，不留下运行期文件。
    _use(client, admin_cookie)
    gallery = Path(get_settings().media_dir) / "backdrops" / "members" / str(first_member_id)
    assert gallery.is_dir()
    assert client.delete(f"{_MEMBERS}/{first_member_id}").status_code == 200
    assert not gallery.exists()


def test_library_visibility_whitelist(client: TestClient) -> None:
    """库可见性（P1）：白名单成员的列表被过滤、白名单外的库 404、
    可见库的落盘路径对成员抹除。"""
    admin_cookie, member_cookie, member_id = _setup_admin_and_member(client)
    _use(client, admin_cookie)

    lib_a = client.post(
        "/api/v1/libraries",
        json={"name": "电影库", "kind": "movie", "root_paths": ["/data/vis-movies"]},
    ).json()["data"]["id"]
    lib_b = client.post(
        "/api/v1/libraries",
        json={"name": "私藏库", "kind": "movie", "root_paths": ["/data/vis-private"]},
    ).json()["data"]["id"]

    # 默认全可见（含路径抹除语义）
    _use(client, member_cookie)
    rows = client.get("/api/v1/libraries").json()["data"]
    assert {r["id"] for r in rows} == {lib_a, lib_b}
    assert all(r["root_paths"] == [] and r["primary_root"] is None for r in rows)

    # 切白名单只留 lib_a
    _use(client, admin_cookie)
    client.put(f"{_MEMBERS}/{member_id}", json={"all_libraries": False, "library_ids": [lib_a]})

    _use(client, member_cookie)
    rows = client.get("/api/v1/libraries").json()["data"]
    assert [r["id"] for r in rows] == [lib_a]
    assert client.get(f"/api/v1/libraries/{lib_a}").status_code == 200
    # 白名单外与不存在同样 404，不泄露存在性
    assert client.get(f"/api/v1/libraries/{lib_b}").status_code == 404
    assert client.get(f"/api/v1/libraries/{lib_b}/items").status_code == 404

    # 管理员不受影响，且看得到真实路径
    _use(client, admin_cookie)
    admin_rows = client.get("/api/v1/libraries").json()["data"]
    assert {r["id"] for r in admin_rows} == {lib_a, lib_b}
    assert all(r["root_paths"] for r in admin_rows)


def test_recent_playback_is_a_member_browsing_route(client: TestClient) -> None:
    """最近观看属于成员浏览面；新账号没有记录时返回空列表而不是拒绝访问。"""
    _admin_cookie, member_cookie, _ = _setup_admin_and_member(client)
    _use(client, member_cookie)

    response = client.get("/api/v1/playback/recent")

    assert response.status_code == 200
    assert response.json()["data"] == {"items": []}


# ---------------------------------------------------------------------------
# 守护测试：成员越权默认拒绝兜底
# ---------------------------------------------------------------------------

# 默认能力成员（可订阅 / 不可搜索）可访问的路由白名单。
# 新路由要对成员开放，必须在此登记并说明理由；未登记的路由成员访问必须被拒。
_MEMBER_ALLOWLIST = {
    # 公开区（匿名可访问，成员自然可访问）
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/auth/bootstrap"),
    ("POST", "/api/v1/auth/bootstrap"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
    # 个人信息自助（按 Principal 分流到成员表）
    ("GET", "/api/v1/auth/me"),
    ("PUT", "/api/v1/auth/profile"),
    ("PUT", "/api/v1/auth/password"),
    ("POST", "/api/v1/auth/avatar"),
    ("GET", "/api/v1/auth/avatar"),
    # 外观：匿名读取管理员背景；登录成员的图库、当前背景与写操作均按账号隔离
    ("GET", "/api/v1/appearance"),
    ("POST", "/api/v1/appearance/backdrops"),
    ("PUT", "/api/v1/appearance/active"),
    ("DELETE", "/api/v1/appearance/backdrops/{backdrop_id}"),
    ("GET", "/api/v1/appearance/backdrops/{backdrop_id}"),
    # 界面偏好（同上，P2 per-member 化）
    ("GET", "/api/v1/ui/preferences"),
    ("PUT", "/api/v1/ui/preferences"),
    # 发现页 / 详情 / 演职员 / 图片：纯浏览面
    ("GET", "/api/v1/ui/discovery/{media_type}"),
    ("GET", "/api/v1/discover/collections"),
    ("GET", "/api/v1/discover/collections/{collection_ref}/titles"),
    ("GET", "/api/v1/discover/filters"),
    ("GET", "/api/v1/discover/titles"),
    ("POST", "/api/v1/search/titles"),
    ("GET", "/api/v1/discover/titles/{title_ref}"),
    ("GET", "/api/v1/discover/people/{tmdb_person_id}"),
    # 发现页页脚展示当前院线地区（只读；切换是 PUT，管理员专属）
    ("GET", "/api/v1/discover/region"),
    ("GET", "/api/v1/people/{tmdb_person_id}"),
    ("GET", "/api/v1/images/assets/{path}"),
    ("GET", "/api/v1/images/proxy"),
    # 媒体库：浏览面（管理动作全部在库路由级挂 require_admin）
    ("GET", "/api/v1/libraries"),
    ("GET", "/api/v1/search/library-items"),
    ("GET", "/api/v1/libraries/files/{file_id}/thumb"),
    # 字幕预览是媒体详情的浏览面；接口自身按文件所属库校验成员可见性，
    # 且轨引用只能命中该文件已登记的内封/外挂字幕，不能读取任意路径。
    ("GET", "/api/v1/libraries/files/{file_id}/subtitles/preview"),
    ("GET", "/api/v1/libraries/{library_id}"),
    ("GET", "/api/v1/libraries/{library_id}/cover"),
    ("GET", "/api/v1/libraries/{library_id}/item-ids"),
    ("GET", "/api/v1/libraries/{library_id}/item-index"),
    ("GET", "/api/v1/libraries/{library_id}/items"),
    ("GET", "/api/v1/libraries/{library_id}/items/{media_item_id}"),
    ("GET", "/api/v1/libraries/{library_id}/items/{media_item_id}/artwork"),
    ("GET", "/api/v1/libraries/{library_id}/items/{media_item_id}/episodes"),
    # 最近观看是按成员隔离的个人播放数据，并继续受媒体库白名单过滤。
    ("GET", "/api/v1/playback/recent"),
    # 播放页条目信息/分集清单：按可见库解析归属，不可见与不存在同样 404
    ("GET", "/api/v1/playback/items/{media_item_id}"),
    ("GET", "/api/v1/playback/items/{media_item_id}/episodes"),
    # 播放决策：成员当然要能放片。结果按请求里的可见库过滤，
    # 不可见库的文件一律 404；软件转码开关是全局设置，成员只会拿到
    # can_self_enable=false 的说明，改不了配置。
    ("POST", "/api/v1/playback/decide"),
    # 会话是私人资源：get/ping/stop 都按 member_id 校验归属，成员之间互相看不见。
    ("POST", "/api/v1/playback/sessions"),
    ("POST", "/api/v1/playback/sessions/{session_id}/ping"),
    ("DELETE", "/api/v1/playback/sessions/{session_id}"),
    # 播放诊断是成员播放器的自助排障入口；使用与取流相同的签名 token，
    # 服务端按 token 中的 member_id 校验会话归属，不暴露其他成员的状态。
    ("GET", "/api/v1/playback/sessions/{session_id}/diagnostics"),
    # 取流端点不挂登录依赖（<video src> 带不了 header），改用签名 token；
    # 无 token 或 token 不符一律 404，与「不存在」不可区分。
    ("GET", "/api/v1/playback/sessions/{session_id}/index.m3u8"),
    # master 列表与字幕媒体列表和 index.m3u8 完全同构：同样不挂登录依赖
    # （iOS 原生 HLS 的请求也带不了 header），同样用签名 token 校验归属。
    ("GET", "/api/v1/playback/sessions/{session_id}/master.m3u8"),
    ("GET", "/api/v1/playback/sessions/{session_id}/sub{index}.m3u8"),
    ("GET", "/api/v1/playback/sessions/{session_id}/{name}"),
    ("GET", "/api/v1/playback/files/{file_id}/stream"),
    ("GET", "/api/v1/playback/files/{file_id}/subtitles"),
    ("GET", "/api/v1/playback/files/{file_id}/fonts"),
    ("GET", "/api/v1/playback/files/{file_id}/fonts/{name}"),
    ("GET", "/api/v1/playback/files/{file_id}/trickplay"),
    ("GET", "/api/v1/playback/files/{file_id}/trickplay/{name}"),
    # 质量上报是成员使用面：每个人看的片各自统计，汇总只有管理员能读
    ("POST", "/api/v1/playback/metrics"),
    # 客户端日志上报同理：成员的播放器出问题也要能报现场（只记日志不落库）
    ("POST", "/api/v1/playback/client-log"),
    # 观看进度与续播点是按成员隔离的个人数据，且只认可见库里的播放单元
    # （不可见的单元一律 404，与"不存在"不可区分）。
    ("POST", "/api/v1/playback/progress"),
    ("GET", "/api/v1/playback/resume"),
    # 搜索历史：个人数据；统一结果端点再按记录类型检查对应能力。
    ("GET", "/api/v1/search/history"),
    ("GET", "/api/v1/search/history/{history_id}/results"),
    ("DELETE", "/api/v1/search/history/{history_id}"),
    ("DELETE", "/api/v1/search/history"),
    # 订阅：读 + 写（写受 allow_subscribe，默认开）；运维接口是管理员专属
    ("GET", "/api/v1/subscriptions"),
    ("GET", "/api/v1/subscriptions/today-arrivals"),
    ("POST", "/api/v1/subscriptions"),
    ("POST", "/api/v1/subscriptions/title-preview"),
    ("GET", "/api/v1/subscriptions/{subscription_id}"),
    ("PATCH", "/api/v1/subscriptions/{subscription_id}"),
    ("DELETE", "/api/v1/subscriptions/{subscription_id}/following"),
    ("GET", "/api/v1/subscriptions/{subscription_id}/activities"),
    ("PATCH", "/api/v1/subscriptions/{subscription_id}/tracking-state"),
    ("PATCH", "/api/v1/subscriptions/{subscription_id}/follow-future"),
    ("POST", "/api/v1/subscriptions/{subscription_id}/missing-resource-searches"),
    # 一轮洗版与「立即搜索」同口径：订阅能力 + 归属校验（路由内 assert_can_manage）
    ("POST", "/api/v1/subscriptions/{subscription_id}/upgrade-runs"),
}

# 路径参数哑值（与 test_auth.py 的匿名守护测试保持一致）
_PATH_DUMMIES = {
    "{site_id}": "mteam",
    "{history_id}": "1",
    "{backdrop_id}": "f" * 32,
    "{downloader_id}": "1",
    "{info_hash}": "f" * 40,
    "{kind}": "movie",
    "{media_type}": "movie",
    "{collection_ref}": "tmdb:movie:popular",
    "{title_ref}": "tmdb:movie:1",
    "{row_id}": "popular",
    "{tmdb_id}": "1",
    "{tmdb_person_id}": "1",
    "{douban_id}": "26266893",
    "{collection_id}": "movie_top250",
    "{subscription_id}": "1",
    "{rule_set_id}": "1",
    "{library_id}": "1",
    "{media_item_id}": "1",
    "{rule_id}": "1",
    "{entry_id}": "1",
    "{file_id}": "1",
    "{device_id}": "no-such-device",
    "{name}": "seg00000.m4s",
    "{token_id}": "1",
    "{notice_id}": "1",
    "{run_id}": "test-run",
    "{session_id}": "test-session",
    # 字幕媒体列表的路径参数嵌在文件名里（.../sub{index}.m3u8）
    "{index}": "0",
    "{day}": "2026-01-01",
    "{path}": "1/poster.jpg",
    "{challenge_id}": "test-challenge",
    "{account_id}": "test-bot",
    "{channel}": "weixin",
    "{endpoint_id}": "test-endpoint",
    "{member_id}": "1",
    "{job_id}": "job_test",
}


def test_every_route_denies_member_overreach(client: TestClient) -> None:
    """行为级越权拒绝：以默认能力成员的会话请求 OpenAPI 里每一条路由。

    白名单之外必须被拒——403（管理员专属 / 能力未开）或 401（插件同步令牌
    等独立密钥体系不认会话 Cookie）。白名单之内必须**不是** 401/403（业务上
    可以 404/422，那证明已通过鉴权与授权进入业务逻辑）。
    """
    _admin_cookie, member_cookie, _ = _setup_admin_and_member(client)
    openapi = client.get("/api/v1/openapi.json").json()
    _use(client, member_cookie)

    checked = 0
    for path, methods in openapi["paths"].items():
        url = path
        for placeholder, dummy in _PATH_DUMMIES.items():
            url = url.replace(placeholder, dummy)
        assert "{" not in url, f"守护测试不认识路径参数，请补充哑值：{path}"
        for method in methods:
            resp = client.request(method.upper(), url)
            if (method.upper(), path) in _MEMBER_ALLOWLIST:
                assert resp.status_code not in (401, 403), (
                    f"成员白名单路由被拒：{method.upper()} {path} → {resp.status_code}。"
                    "若该路由已改为管理员专属，请把它移出 _MEMBER_ALLOWLIST。"
                )
            else:
                assert resp.status_code in (401, 403), (
                    f"路由未挡住成员越权：{method.upper()} {path} → {resp.status_code}。"
                    "管理路由请挂 require_admin；确属成员使用面请在 "
                    "_MEMBER_ALLOWLIST 登记并说明理由。"
                )
            checked += 1

    assert checked > 0, "守护测试没有扫到任何路由，枚举逻辑可能失效"
