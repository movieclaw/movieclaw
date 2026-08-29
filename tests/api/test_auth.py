"""登录鉴权体系的端到端测试。

覆盖三块：
1. 一次性初始化锁（本体系最核心的安全保证）：重复初始化被 409 拒绝、
   并发建号只有一个成功、初始化状态查询正确。
2. 登录会话全流程：登录/登出/查询会话/改密码强制全端下线/登录限速。
3. 守护测试（默认拒绝兜底）：匿名遍历 OpenAPI 里的**每一条**路由，
   凡不在公开白名单里的必须 401。以后新增路由忘挂鉴权，这里直接红。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import movieclaw_api.services.auth as auth_service
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box

_AUTH = "/api/v1/auth"
_ADMIN = {"username": "admin", "password": "s3cret-pass"}


@pytest.mark.real_password_hash
def test_production_password_hash_roundtrip() -> None:
    """生产推荐参数至少保留一条真实 hash/verify 烟测，防止测试快路径遮住接线错误。"""
    encoded = auth_service.hash_password(_ADMIN["password"])

    assert encoded.startswith("$argon2id$")
    assert auth_service.verify_password(_ADMIN["password"], encoded) is True


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 每个用例独立临时库 / 密钥文件，彻底隔离；登录限速计数也要清零
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
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


def _bootstrap(client: TestClient, **overrides):
    return client.post(f"{_AUTH}/bootstrap", json={**_ADMIN, **overrides})


# ---------------------------------------------------------------------------
# 一次性初始化锁
# ---------------------------------------------------------------------------


def test_bootstrap_status_flips_after_init(client: TestClient) -> None:
    """空库为未初始化；建号后状态翻转，且不可逆。"""
    assert client.get(f"{_AUTH}/bootstrap").json()["data"]["initialized"] is False

    resp = _bootstrap(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "admin"

    assert client.get(f"{_AUTH}/bootstrap").json()["data"]["initialized"] is True


def test_bootstrap_rejects_second_attempt(client: TestClient) -> None:
    """核心安全保证：管理员已存在时，任何再次建号请求一律 409。"""
    assert _bootstrap(client).status_code == 200

    # 换个用户名/密码也不行——锁依据是"管理员是否存在"，与请求内容无关
    second = _bootstrap(client, username="hacker", password="evil-pass-123")
    assert second.status_code == 409

    # 原管理员不受影响，仍可正常登录
    login = client.post(f"{_AUTH}/login", json=_ADMIN)
    assert login.status_code == 200


def test_bootstrap_concurrent_requests_only_one_wins(client: TestClient) -> None:
    """并发建号：同时打进来的请求最多只有一个成功（asyncio 锁串行化）。"""
    payloads = [{"username": f"user{i}", "password": f"password-{i:02d}"} for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda p: client.post(f"{_AUTH}/bootstrap", json=p), payloads))

    codes = sorted(r.status_code for r in responses)
    assert codes.count(200) == 1, f"并发建号应恰好一个成功，实际：{codes}"
    assert all(c in (200, 409) for c in codes)


def test_bootstrap_auto_login(client: TestClient) -> None:
    """建号成功即自动登录（返回体带会话 Cookie，随后可直接访问受保护接口）。"""
    resp = _bootstrap(client)
    assert resp.status_code == 200
    assert "movieclaw_session" in resp.cookies

    me = client.get(f"{_AUTH}/me")
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "admin"


def test_bootstrap_validates_input(client: TestClient) -> None:
    """用户名过短 / 密码过短直接 422，不会写库。"""
    assert _bootstrap(client, username="ab").status_code == 422
    assert _bootstrap(client, password="short").status_code == 422
    # 校验失败不占用一次性锁
    assert client.get(f"{_AUTH}/bootstrap").json()["data"]["initialized"] is False


# ---------------------------------------------------------------------------
# 登录会话全流程
# ---------------------------------------------------------------------------


def test_login_logout_flow(client: TestClient) -> None:
    _bootstrap(client)
    client.cookies.clear()  # 丢弃建号自动登录的会话，从头走登录

    # 未登录访问受保护接口 → 401
    assert client.get("/api/v1/sites/catalog").status_code == 401

    # 登录后可访问
    login = client.post(f"{_AUTH}/login", json=_ADMIN)
    assert login.status_code == 200
    assert client.get("/api/v1/sites/catalog").status_code == 200

    # 登出后再次 401
    client.post(f"{_AUTH}/logout")
    assert client.get("/api/v1/sites/catalog").status_code == 401


def test_login_rejects_wrong_credentials(client: TestClient) -> None:
    _bootstrap(client)
    client.cookies.clear()

    wrong_pass = client.post(f"{_AUTH}/login", json={**_ADMIN, "password": "wrong-pass"})
    assert wrong_pass.status_code == 401
    wrong_user = client.post(f"{_AUTH}/login", json={**_ADMIN, "username": "nobody"})
    assert wrong_user.status_code == 401


def test_login_before_bootstrap_hints_setup(client: TestClient) -> None:
    """未初始化就登录：明确提示先完成引导，而不是含糊的凭据错误。"""
    resp = client.post(f"{_AUTH}/login", json=_ADMIN)
    assert resp.status_code == 400
    assert "初始化" in resp.json()["message"]


def test_login_throttled_after_repeated_failures(client: TestClient) -> None:
    """连续失败达到阈值后 429 限速，正确密码也要等窗口过后。"""
    _bootstrap(client)
    client.cookies.clear()

    for _ in range(5):
        client.post(f"{_AUTH}/login", json={**_ADMIN, "password": "wrong-pass"})

    locked = client.post(f"{_AUTH}/login", json=_ADMIN)  # 正确密码也被限速拦下
    assert locked.status_code == 429


# 超管会话视图的固定部分：role 恒为 admin，能力开关恒全开
_ADMIN_VIEW_EXTRAS = {
    "role": "admin",
    "capabilities": {
        "allow_subscribe": True,
        "allow_search": True,
        "allow_direct_download": True,
    },
}


def test_nickname_defaults_to_username_and_editable(client: TestClient) -> None:
    """建号时昵称默认取用户名；可在个人信息里修改，用户名保持不变。"""
    created = _bootstrap(client).json()["data"]
    assert created == {
        "username": "admin",
        "nickname": "admin",
        "avatar_url": None,
        **_ADMIN_VIEW_EXTRAS,
    }

    resp = client.put(f"{_AUTH}/profile", json={"nickname": "呀哈喽"})
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "username": "admin",
        "nickname": "呀哈喽",
        "avatar_url": None,
        **_ADMIN_VIEW_EXTRAS,
    }

    # /auth/me 与后续登录都返回新昵称；登录仍用用户名
    assert client.get(f"{_AUTH}/me").json()["data"]["nickname"] == "呀哈喽"
    client.cookies.clear()
    login = client.post(f"{_AUTH}/login", json=_ADMIN)
    assert login.json()["data"] == {
        "username": "admin",
        "nickname": "呀哈喽",
        "avatar_url": None,
        **_ADMIN_VIEW_EXTRAS,
    }


def test_change_password_kicks_other_sessions(client: TestClient) -> None:
    """改密码轮换签名密钥：旧会话全部失效，本会话续期，新密码可登录。"""
    _bootstrap(client)
    old_session = client.cookies.get("movieclaw_session")

    resp = client.put(
        f"{_AUTH}/password",
        json={"old_password": _ADMIN["password"], "new_password": "new-pass-456"},
    )
    assert resp.status_code == 200

    # 操作者本人拿到新 Cookie，不被踢出
    assert client.get(f"{_AUTH}/me").status_code == 200

    # 旧会话令牌已失效（模拟另一台设备）
    client.cookies.set("movieclaw_session", old_session)
    assert client.get(f"{_AUTH}/me").status_code == 401

    # 旧密码不可登录，新密码可以
    client.cookies.clear()
    assert client.post(f"{_AUTH}/login", json=_ADMIN).status_code == 401
    new_login = client.post(
        f"{_AUTH}/login", json={"username": "admin", "password": "new-pass-456"}
    )
    assert new_login.status_code == 200


def test_throttle_lock_survives_bucket_flood() -> None:
    """限速桶淘汰不能变成解锁通道：海量随机用户名注水后，
    被锁定账号的桶必须原地存活（无差别 LRU 会被 128 个新桶挤掉）。"""
    from movieclaw_api.exceptions import AppException
    from movieclaw_api.services import auth as auth_service

    auth_service.reset_auth_state()
    try:
        target = auth_service._throttle_for("admin")
        for _ in range(auth_service.LoginThrottle.THRESHOLD):
            target.record_failure()
        with pytest.raises(AppException):
            target.ensure_allowed()  # 已锁定

        for i in range(auth_service._MAX_THROTTLE_BUCKETS * 2):
            auth_service._throttle_for(f"flood-{i}")

        # 注水后同名桶还是那一个，锁定仍然生效
        assert auth_service._throttle_for("admin") is target
        with pytest.raises(AppException):
            auth_service._throttle_for("admin").ensure_allowed()
    finally:
        auth_service.reset_auth_state()


# ---------------------------------------------------------------------------
# 守护测试：默认拒绝兜底
# ---------------------------------------------------------------------------

# 公开白名单：新增公开接口必须在此登记并说明理由，否则守护测试失败。
_PUBLIC_ALLOWLIST = {
    # 网页播放器取流：<video src>、hls.js 拉分片、原生 HLS 都带不了自定义
    # header（整条取流链路在浏览器内部，JS 插不进手），所以不挂登录依赖，
    # 改用查询参数里的短时效签名 token。无 token / token 不符一律 404——
    # 与「资源不存在」不可区分，不给探测者留判据。
    ("GET", "/api/v1/playback/sessions/{session_id}/index.m3u8"),
    ("GET", "/api/v1/playback/sessions/{session_id}/{name}"),
    ("GET", "/api/v1/playback/files/{file_id}/stream"),
    ("GET", "/api/v1/playback/files/{file_id}/subtitles"),
    ("GET", "/api/v1/playback/files/{file_id}/fonts"),
    ("GET", "/api/v1/playback/files/{file_id}/fonts/{name}"),
    ("GET", "/api/v1/playback/files/{file_id}/trickplay"),
    ("GET", "/api/v1/playback/files/{file_id}/trickplay/{name}"),
    ("GET", "/api/v1/health"),  # 存活探针
    ("GET", "/api/v1/auth/bootstrap"),  # 前端判断进引导页还是登录页
    ("POST", "/api/v1/auth/bootstrap"),  # 首次建号（服务端一次性锁自我封闭）
    ("POST", "/api/v1/auth/login"),  # 登录本身
    ("POST", "/api/v1/auth/logout"),  # 仅清 Cookie，无信息暴露
    # 设备授权的两个协议端点（docs/design/device-auth.md §2.1）：设备在拿到
    # 令牌之前无凭可用，必须匿名。防滥用不靠鉴权，靠服务层三道约束——单 IP
    # 未决请求上限、轮询退避、挑战全程不落库；且批准这一步要管理员会话。
    ("POST", "/api/v1/auth/device/authorize"),
    ("POST", "/api/v1/auth/device/token"),
    ("GET", "/api/v1/appearance"),  # 登录页需要背景图地址
    ("GET", "/api/v1/appearance/backdrops/{backdrop_id}"),  # 登录页背景图文件
}


def test_every_route_denies_anonymous_access(client: TestClient) -> None:
    """行为级默认拒绝：匿名请求 OpenAPI 里每一条路由，白名单之外必须 401。

    不做依赖内省而直接发请求，是为了连"鉴权依赖挂了但被绕过"的实现错误
    也能兜住。路径参数用哑值填充——鉴权在路由解析后、业务逻辑前执行，
    未登录时必须 401 而非 404/422。
    """
    openapi = client.get("/api/v1/openapi.json").json()

    checked = 0
    for path, methods in openapi["paths"].items():
        url = (
            path.replace("{site_id}", "mteam")
            .replace("{history_id}", "1")
            .replace("{backdrop_id}", "f" * 32)
            .replace("{downloader_id}", "1")
            .replace("{info_hash}", "f" * 40)
            .replace("{kind}", "movie")
            .replace("{media_type}", "movie")
            .replace("{collection_ref}", "tmdb:movie:popular")
            .replace("{title_ref}", "tmdb:movie:1")
            .replace("{row_id}", "popular")
            .replace("{tmdb_id}", "1")
            .replace("{tmdb_person_id}", "1")
            .replace("{douban_id}", "26266893")
            .replace("{collection_id}", "movie_top250")
            .replace("{subscription_id}", "1")
            .replace("{rule_set_id}", "1")
            .replace("{library_id}", "1")
            .replace("{media_item_id}", "1")
            .replace("{rule_id}", "1")
            .replace("{entry_id}", "1")
            .replace("{file_id}", "1")
            .replace("{token_id}", "1")
            .replace("{notice_id}", "1")
            .replace("{run_id}", "test-run")
            .replace("{session_id}", "test-session")
            .replace("{day}", "2026-01-01")
            .replace("{path}", "1/poster.jpg")
            .replace("{challenge_id}", "test-challenge")
            .replace("{account_id}", "test-bot")
            .replace("{channel}", "weixin")
            .replace("{endpoint_id}", "test-endpoint")
            .replace("{member_id}", "1")
            .replace("{job_id}", "job_test")
            .replace("{device_id}", "test-device")
            .replace("{user_code}", "MCLW-TEST")
            .replace("{name}", "seg00000.m4s")
            .replace("{index}", "0")
        )
        assert "{" not in url, f"守护测试不认识路径参数，请补充哑值：{path}"
        for method in methods:
            if (method.upper(), path) in _PUBLIC_ALLOWLIST:
                continue
            resp = client.request(method.upper(), url)
            assert resp.status_code == 401, (
                f"路由未受登录/令牌保护：{method.upper()} {path} → {resp.status_code}。"
                "若确须公开，请在 _PUBLIC_ALLOWLIST 登记并说明理由。"
            )
            checked += 1

    assert checked > 0, "守护测试没有扫到任何受保护路由，枚举逻辑可能失效"
