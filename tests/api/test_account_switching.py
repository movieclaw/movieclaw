"""多账号切换（docs/design/account-switching.md）的端到端测试。

覆盖：登录并入账号袋、列表顺序、切换、退出当前 / 退出全部、移除、既有失效
语义（停用 / 改密）对袋子自动生效、上限淘汰、头像按持有者隔离、篡改的袋子
被整袋丢弃。TestClient 自带 Cookie 罐，模拟的就是同一个浏览器。
"""

from __future__ import annotations

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
_SESSION = "movieclaw_session"
_ACCOUNTS = "movieclaw_accounts"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
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


def _login(client: TestClient, username: str, password: str) -> None:
    """在**同一个浏览器**里再登录一个账号（不清 Cookie 罐，这正是"添加账号"）。"""
    resp = client.post(f"{_AUTH}/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text


def _create_member(client: TestClient, username: str, password: str) -> int:
    resp = client.post(_MEMBERS, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


def _setup(client: TestClient) -> int:
    """建超管（自动登录）→ 建成员 → 同一浏览器再登录成员。返回成员 id。"""
    assert client.post(f"{_AUTH}/bootstrap", json=_ADMIN).status_code == 200
    member_id = _create_member(client, _MEMBER["username"], _MEMBER["password"])
    _login(client, _MEMBER["username"], _MEMBER["password"])
    return member_id


def _accounts(client: TestClient) -> list[dict]:
    resp = client.get(f"{_AUTH}/accounts")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _me(client: TestClient) -> str:
    resp = client.get(f"{_AUTH}/me")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["username"]


# ---------------------------------------------------------------------------
# 登录并入袋子、列表、切换
# ---------------------------------------------------------------------------


def test_login_adds_account_and_keeps_previous(client: TestClient) -> None:
    """再登录一个账号不会顶掉前一个；新登录的成为激活账号并排在第一。"""
    _setup(client)

    assert _me(client) == "family"
    accounts = _accounts(client)
    assert [(a["username"], a["active"]) for a in accounts] == [
        ("family", True),
        ("admin", False),
    ]
    assert accounts[0]["role"] == "member"
    assert accounts[1]["role"] == "admin"
    assert _ACCOUNTS in client.cookies


def test_switch_account_without_password(client: TestClient) -> None:
    """切换只认袋子里的令牌，不要密码；切到超管也一样（明确的产品决策）。"""
    _setup(client)

    resp = client.post(f"{_AUTH}/accounts/switch", json={"username": "admin"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["username"] == "admin"
    assert resp.json()["data"]["role"] == "admin"

    assert _me(client) == "admin"
    # 切换后能访问管理员专属接口，证明激活 Cookie 确实换成了超管令牌
    assert client.get(_MEMBERS).status_code == 200
    assert [(a["username"], a["active"]) for a in _accounts(client)] == [
        ("admin", True),
        ("family", False),
    ]

    # 用户名大小写不敏感
    assert client.post(f"{_AUTH}/accounts/switch", json={"username": "FAMILY"}).status_code == 200
    assert _me(client) == "family"


def test_switch_to_unknown_account_is_404(client: TestClient) -> None:
    _setup(client)
    resp = client.post(f"{_AUTH}/accounts/switch", json={"username": "nobody"})
    assert resp.status_code == 404
    assert "重新登录" in resp.json()["message"]
    # 失败不影响当前会话
    assert _me(client) == "family"


def test_relogin_same_account_replaces_instead_of_duplicating(client: TestClient) -> None:
    """同一账号重复登录（会话过期后重新登录的典型场景）不会在袋子里出现两次。"""
    _setup(client)
    _login(client, _MEMBER["username"], _MEMBER["password"])
    assert [a["username"] for a in _accounts(client)] == ["family", "admin"]


# ---------------------------------------------------------------------------
# 退出与移除
# ---------------------------------------------------------------------------


def test_logout_current_switches_to_next(client: TestClient) -> None:
    """退出当前账号自动切到袋子里的下一个；袋子空了才真正变成未登录。"""
    _setup(client)

    resp = client.post(f"{_AUTH}/logout")
    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "admin"
    assert _me(client) == "admin"
    assert [a["username"] for a in _accounts(client)] == ["admin"]

    resp = client.post(f"{_AUTH}/logout")
    assert resp.status_code == 200
    assert resp.json()["data"] is None
    assert client.get(f"{_AUTH}/me").status_code == 401
    assert _SESSION not in client.cookies
    assert _ACCOUNTS not in client.cookies


def test_logout_all_clears_everything(client: TestClient) -> None:
    _setup(client)

    resp = client.post(f"{_AUTH}/logout", json={"all": True})
    assert resp.status_code == 200
    assert resp.json()["data"] is None
    assert client.get(f"{_AUTH}/me").status_code == 401
    assert _SESSION not in client.cookies
    assert _ACCOUNTS not in client.cookies


def test_logout_without_any_session_still_succeeds(client: TestClient) -> None:
    """公开接口：会话早已过期 / 从未登录时照样能顺利"登出"。"""
    assert client.post(f"{_AUTH}/bootstrap", json=_ADMIN).status_code == 200
    client.cookies.clear()
    resp = client.post(f"{_AUTH}/logout")
    assert resp.status_code == 200
    assert resp.json()["data"] is None


def test_remove_inactive_account_keeps_current(client: TestClient) -> None:
    _setup(client)

    resp = client.delete(f"{_AUTH}/accounts/admin")
    assert resp.status_code == 200
    # 移除的不是当前账号：返回体仍是当前账号，会话不变
    assert resp.json()["data"]["username"] == "family"
    assert _me(client) == "family"
    assert [a["username"] for a in _accounts(client)] == ["family"]

    assert client.delete(f"{_AUTH}/accounts/admin").status_code == 404


def test_remove_active_account_switches_to_next(client: TestClient) -> None:
    _setup(client)

    resp = client.delete(f"{_AUTH}/accounts/family")
    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "admin"
    assert _me(client) == "admin"


# ---------------------------------------------------------------------------
# 既有失效语义对袋子自动生效
# ---------------------------------------------------------------------------


def test_disabled_member_disappears_from_bag(client: TestClient) -> None:
    """成员被停用：袋子里它的令牌验签失败，列表里消失，切过去 404。"""
    member_id = _setup(client)
    client.post(f"{_AUTH}/accounts/switch", json={"username": "admin"})
    assert client.put(f"{_MEMBERS}/{member_id}/status", json={"enabled": False}).status_code == 200

    assert [a["username"] for a in _accounts(client)] == ["admin"]
    resp = client.post(f"{_AUTH}/accounts/switch", json={"username": "family"})
    assert resp.status_code == 404


def test_admin_password_change_empties_bag_except_self(client: TestClient) -> None:
    """超管改密轮换全局密钥：袋子里其余账号一并失效，只剩超管自己（新令牌）。"""
    _setup(client)
    client.post(f"{_AUTH}/accounts/switch", json={"username": "admin"})

    resp = client.put(
        f"{_AUTH}/password",
        json={"old_password": _ADMIN["password"], "new_password": "brand-new-pass-9"},
    )
    assert resp.status_code == 200
    assert _me(client) == "admin"
    assert [a["username"] for a in _accounts(client)] == ["admin"]


def test_member_password_change_keeps_other_accounts(client: TestClient) -> None:
    """成员改密只踢自己的其他设备：袋子里的超管原样保留，本人令牌换成新版本。"""
    _setup(client)

    resp = client.put(
        f"{_AUTH}/password",
        json={"old_password": _MEMBER["password"], "new_password": "brand-new-pass-9"},
    )
    assert resp.status_code == 200
    assert [(a["username"], a["active"]) for a in _accounts(client)] == [
        ("family", True),
        ("admin", False),
    ]
    # 改密后仍能来回切换（袋子里的成员令牌是重签的新版本）
    assert client.post(f"{_AUTH}/accounts/switch", json={"username": "admin"}).status_code == 200
    assert client.post(f"{_AUTH}/accounts/switch", json={"username": "family"}).status_code == 200
    assert _me(client) == "family"


# ---------------------------------------------------------------------------
# 上限、头像、篡改
# ---------------------------------------------------------------------------


def test_bag_evicts_oldest_beyond_limit(client: TestClient) -> None:
    """第 6 个账号登录时淘汰最久未用的那个，登录本身永远成功。"""
    assert client.post(f"{_AUTH}/bootstrap", json=_ADMIN).status_code == 200
    names = [f"user{i}" for i in range(5)]
    for name in names:
        _create_member(client, name, f"password-{name}")
    for name in names:
        _login(client, name, f"password-{name}")

    # 登录顺序：admin, user0..user4 → 超管是最久未用的，被淘汰
    assert [a["username"] for a in _accounts(client)] == list(reversed(names))
    assert client.post(f"{_AUTH}/accounts/switch", json={"username": "admin"}).status_code == 404


def test_avatar_of_saved_account_is_readable_only_from_its_bag(client: TestClient) -> None:
    """列表里的头像地址带 account 参数；只有袋子里有该账号的浏览器才能读到。"""
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d"
        "4944415478da63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
    )
    _setup(client)
    # 当前是成员，先给成员传头像
    resp = client.post(f"{_AUTH}/avatar", files={"file": ("avatar.png", png, "image/png")})
    assert resp.status_code == 200, resp.text

    client.post(f"{_AUTH}/accounts/switch", json={"username": "admin"})
    accounts = {a["username"]: a for a in _accounts(client)}
    assert accounts["admin"]["avatar_url"] is None
    member_avatar = accounts["family"]["avatar_url"]
    assert member_avatar and "account=family" in member_avatar
    assert client.get(member_avatar).status_code == 200

    # 另一个"浏览器"只登录了超管：袋子里没有该成员，读不到它的头像
    client.cookies.clear()
    _login(client, _ADMIN["username"], _ADMIN["password"])
    assert client.get(member_avatar).status_code == 404


def test_tampered_bag_is_discarded_but_session_survives(client: TestClient) -> None:
    """账号袋签名被改：整袋当空，当前激活会话不受影响（它仍并进列表）。"""
    _setup(client)
    client.cookies.set(_ACCOUNTS, "garbage.not-a-signature")

    assert _me(client) == "family"
    assert [(a["username"], a["active"]) for a in _accounts(client)] == [("family", True)]
