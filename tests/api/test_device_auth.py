"""设备授权协议的端到端测试（docs/design/device-auth.md）。

覆盖三块：
1. 全链路：发起 → 待批准 → 批准 → 兑换 → 令牌可用；
2. 五条异常路径：轮询过快、被拒绝、过期、重放兑换、来源限流；
3. 形态上限：Worker 令牌被业务接口默认拒绝（这是 Worker 权限收窄的唯一执行点，
   遍历全路由验证，新增业务路由不需要额外标注也会被挡住）。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import movieclaw_api.services.auth as auth_service
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box

_AUTH = "/api/v1/auth"
_ADMIN = {"username": "admin", "password": "s3cret-pass"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post(f"{_AUTH}/bootstrap", json=_ADMIN)
        c.post(f"{_AUTH}/login", json=_ADMIN)
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


@pytest.fixture
def lan_client(tmp_path, monkeypatch):
    """来源地址可辨的客户端。

    默认 TestClient 的 client host 是字符串 "testclient"，不是合法 IP，会被
    判成「认不出来源」——那正好覆盖容器 NAT 的场景，但按来源限流的用例需要
    一个真实地址。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'lan.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".lan_secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    with TestClient(create_app(), client=("192.168.1.42", 51000)) as c:
        c.post(f"{_AUTH}/bootstrap", json=_ADMIN)
        c.post(f"{_AUTH}/login", json=_ADMIN)
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def _authorize(client: TestClient, *, client_type: str = "cli", name: str = "claude-code@mac"):
    resp = client.post(
        f"{_AUTH}/device/authorize",
        json={"client_type": client_type, "client_name": name},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _redeem(client: TestClient, device_code: str):
    return client.post(f"{_AUTH}/device/token", json={"device_code": device_code})


# ---------------------------------------------------------------------------
# 全链路
# ---------------------------------------------------------------------------


def test_full_pairing_flow_grants_usable_token(client: TestClient) -> None:
    """发起 → 批准 → 兑换 → 令牌能调业务接口，且令牌只交付这一次。"""
    grant = _authorize(client)
    assert grant["user_code"].startswith("MCLW-")
    assert grant["verification_uri"].endswith("/settings/devices")
    assert grant["expires_in"] == auth_service.DEVICE_CODE_TTL_SECONDS

    # 配对码不是凭据：它出现在网页上，而 device_code 只有客户端有
    assert grant["device_code"] != grant["user_code"]

    pending = client.get(f"{_AUTH}/devices/requests").json()["data"]
    assert [p["user_code"] for p in pending] == [grant["user_code"]]
    assert pending[0]["client_name"] == "claude-code@mac"

    assert _redeem(client, grant["device_code"]).status_code == 202

    approve = client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve")
    assert approve.status_code == 200, approve.text

    granted = _redeem(client, grant["device_code"])
    assert granted.status_code == 200, granted.text
    token = granted.json()["data"]["token"]
    assert token.startswith("mclaw_")

    # 令牌可用：Bearer 走的是与会话 Cookie 相同的授权路径
    fresh = TestClient(client.app)
    me = fresh.get(f"{_AUTH}/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text

    # 兑换一次即作废：重放同一个 device_code 不再返回令牌
    assert _redeem(client, grant["device_code"]).status_code == 400

    # 批准后请求离开待批准列表，令牌进入设备列表
    assert client.get(f"{_AUTH}/devices/requests").json()["data"] == []
    tokens = client.get(f"{_AUTH}/tokens").json()["data"]
    assert [(t["name"], t["client_type"]) for t in tokens] == [("claude-code@mac", "cli")]


def test_revoking_token_cuts_off_the_device(client: TestClient) -> None:
    """吊销是唯一的事后止损手段，必须立即生效。"""
    grant = _authorize(client)
    client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve")
    token = _redeem(client, grant["device_code"]).json()["data"]["token"]

    token_id = client.get(f"{_AUTH}/tokens").json()["data"][0]["id"]
    assert client.delete(f"{_AUTH}/tokens/{token_id}").status_code == 200

    fresh = TestClient(client.app)
    assert fresh.get(f"{_AUTH}/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ---------------------------------------------------------------------------
# 异常路径
# ---------------------------------------------------------------------------


def test_polling_too_fast_backs_off_without_voiding_the_challenge(client: TestClient) -> None:
    """轮询过快只让客户端退避——正常用户的重试不该被当成攻击而作废挑战。"""
    grant = _authorize(client)
    assert _redeem(client, grant["device_code"]).status_code == 202
    assert _redeem(client, grant["device_code"]).status_code == 429

    # 挑战仍然活着：批准后照样能兑换
    client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve")
    assert _redeem(client, grant["device_code"]).status_code == 200


def test_denied_request_grants_nothing(client: TestClient) -> None:
    """拒绝不生成任何令牌，客户端拿到 400 后应当停止轮询。"""
    grant = _authorize(client)
    assert client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/deny").status_code == 200

    assert _redeem(client, grant["device_code"]).status_code == 400
    assert client.get(f"{_AUTH}/tokens").json()["data"] == []


def test_expired_challenge_stops_the_client(client: TestClient, monkeypatch) -> None:
    """超时未批准即作废；客户端收到 400 而不是含糊的「挑战不存在」。"""
    grant = _authorize(client)
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        auth_service.time,
        "monotonic",
        lambda: real_monotonic() + auth_service.DEVICE_CODE_TTL_SECONDS + 1,
    )
    assert _redeem(client, grant["device_code"]).status_code == 400
    assert client.get(f"{_AUTH}/devices/requests").json()["data"] == []


def test_unknown_device_code_is_indistinguishable_from_expired(client: TestClient) -> None:
    """乱猜 device_code 与「已过期」返回同一结论，不给探测者留判据。"""
    assert _redeem(client, "definitely-not-a-real-device-code").status_code == 400


def test_pending_requests_are_capped_per_source(lan_client: TestClient) -> None:
    """来源可辨时，单来源未决请求有上限，防止刷屏把审批页淹掉。"""
    for _ in range(auth_service._DEVICE_MAX_PENDING_PER_IP):
        _authorize(lan_client)
    overflow = lan_client.post(
        f"{_AUTH}/device/authorize",
        json={"client_type": "cli", "client_name": "flood"},
    )
    assert overflow.status_code == 400
    assert "同一来源" in overflow.json()["message"]


def test_records_the_real_source_when_it_identifies_a_machine(lan_client: TestClient) -> None:
    _authorize(lan_client)
    requests = lan_client.get(f"{_AUTH}/devices/requests").json()["data"]
    assert requests[0]["source_ip"] == "192.168.1.42"


def test_unidentifiable_source_is_reported_empty_not_faked(client: TestClient) -> None:
    """取不到可辨地址时如实返回空串，不编一个占位地址。

    审批卡让用户照着「来源」判断这是不是自己那台机器；桥接网络里所有设备的
    源地址都会被 NAT 成同一个网关地址，摆出来只会误导。
    """
    _authorize(client)
    requests = client.get(f"{_AUTH}/devices/requests").json()["data"]
    assert requests[0]["source_ip"] == ""


def test_unidentifiable_sources_do_not_share_one_rate_limit_bucket(client: TestClient) -> None:
    """来源认不出来时不按来源分桶，否则一台机器刷屏就锁住整个局域网。

    容器桥接网络下每台设备的源地址都是同一个网关地址。如果照旧按地址计数，
    第 6 台机器根本配不上对——而它和前 5 台毫无关系。
    """
    for _ in range(auth_service._DEVICE_MAX_PENDING_PER_IP + 1):
        _authorize(client)

    # 但总数上限仍然兜着：审批页不该被淹掉
    while True:
        resp = client.post(
            f"{_AUTH}/device/authorize",
            json={"client_type": "cli", "client_name": "flood"},
        )
        if resp.status_code != 200:
            break
    assert resp.status_code == 400
    assert "过多" in resp.json()["message"]
    pending = client.get(f"{_AUTH}/devices/requests").json()["data"]
    assert len(pending) == auth_service._DEVICE_MAX_PENDING_TOTAL


def test_authorize_rejects_unknown_client_type(client: TestClient) -> None:
    resp = client.post(
        f"{_AUTH}/device/authorize",
        json={"client_type": "toaster", "client_name": "x"},
    )
    assert resp.status_code == 400


def test_approving_twice_is_rejected(client: TestClient) -> None:
    """重复批准同一条请求不会再签发一枚令牌。"""
    grant = _authorize(client)
    assert client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve").status_code == 200
    assert client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve").status_code == 400
    assert len(client.get(f"{_AUTH}/tokens").json()["data"]) == 1


def test_device_management_requires_admin_session(client: TestClient) -> None:
    """批准是防钓鱼的唯一人工闸，必须要管理员会话——匿名一律 401。"""
    grant = _authorize(client)
    anon = TestClient(client.app)
    assert anon.get(f"{_AUTH}/devices/requests").status_code == 401
    assert anon.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve").status_code == 401
    assert anon.post(f"{_AUTH}/devices/requests/{grant['user_code']}/deny").status_code == 401


# ---------------------------------------------------------------------------
# 形态上限：Worker 令牌只能转码
# ---------------------------------------------------------------------------


def test_worker_token_is_rejected_by_business_endpoints(client: TestClient) -> None:
    """遍历全路由：Worker 令牌在 require_login 处被默认拒绝。

    这条守护的价值在于「新增业务路由不需要记得标注什么」——只要照常挂
    require_login，Worker 就自动进不来。放行 Worker 的白名单只有转码那一处。
    """
    grant = _authorize(client, client_type="worker", name="Yi的Mac-mini")
    client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve")
    token = _redeem(client, grant["device_code"]).json()["data"]["token"]

    worker = TestClient(client.app)
    headers = {"Authorization": f"Bearer {token}"}
    for path in (f"{_AUTH}/me", "/api/v1/subscriptions", "/api/v1/libraries", f"{_AUTH}/tokens"):
        resp = worker.get(path, headers=headers)
        assert resp.status_code == 403, f"{path} 竟然放行了 Worker 令牌：{resp.status_code}"


def test_cli_token_is_not_restricted_by_client_type(client: TestClient) -> None:
    """对照组：同一批准者签发的 CLI 令牌不受形态上限影响。"""
    grant = _authorize(client)
    client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve")
    token = _redeem(client, grant["device_code"]).json()["data"]["token"]

    cli = TestClient(client.app)
    resp = cli.get("/api/v1/subscriptions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text


def test_tokens_cannot_mint_or_revoke_credentials(client: TestClient) -> None:
    """凭证的签发与吊销只能由人在浏览器里完成。

    这是「吊销是唯一止损手段」这句话成立的前提：设备令牌若能签发新令牌，
    就能给自己造一枚备份，吊销原来那枚也止不住损。
    """
    grant = _authorize(client)
    client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve")
    token = _redeem(client, grant["device_code"]).json()["data"]["token"]
    token_id = client.get(f"{_AUTH}/tokens").json()["data"][0]["id"]

    holder = TestClient(client.app)
    headers = {"Authorization": f"Bearer {token}"}
    # 令牌可用（业务接口通），但碰不到凭证管理面
    assert holder.get("/api/v1/subscriptions", headers=headers).status_code == 200
    assert (
        holder.post(f"{_AUTH}/tokens", json={"name": "spare"}, headers=headers).status_code == 403
    )
    assert holder.get(f"{_AUTH}/tokens", headers=headers).status_code == 403
    assert holder.delete(f"{_AUTH}/tokens/{token_id}", headers=headers).status_code == 403

    # 也不能自我扩张：批准别的设备把更多机器拉进来
    other = _authorize(client, name="attacker-box")
    assert (
        holder.post(
            f"{_AUTH}/devices/requests/{other['user_code']}/approve", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(f"{_AUTH}/devices/requests").json()["data"][0]["client_name"] == "attacker-box"
    )


def test_concurrent_approvals_mint_at_most_one_token(client: TestClient) -> None:
    """并发批准同一条请求只签出一枚令牌。

    签发要 await（读写设置项），若状态变更放在 await 之后，两个并发请求会双双
    通过「仍待批准」的检查，多签的那枚永远没人兑换、也不会有人知道它存在。
    """
    from concurrent.futures import ThreadPoolExecutor

    grant = _authorize(client)
    url = f"{_AUTH}/devices/requests/{grant['user_code']}/approve"
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = [f.result().status_code for f in [pool.submit(client.post, url) for _ in range(4)]]

    assert codes.count(200) == 1, f"批准被重复受理：{codes}"
    assert len(client.get(f"{_AUTH}/tokens").json()["data"]) == 1


def test_worker_token_is_accepted_by_the_transcode_control_plane(client: TestClient) -> None:
    """形态上限的另一半：Worker 令牌进不了业务接口，但必须进得了转码控制面。

    只断言鉴权这一关——WebSocket 握手之后的协议交换属于转码链路自己的测试。
    """
    from movieclaw_api.api.deps import resolve_worker_principal

    grant = _authorize(client, client_type="worker", name="Yi的Mac-mini")
    client.post(f"{_AUTH}/devices/requests/{grant['user_code']}/approve")
    token = _redeem(client, grant["device_code"]).json()["data"]["token"]

    async def resolve(header: str | None):
        return await resolve_worker_principal(header)

    principal = asyncio.run(resolve(f"Bearer {token}"))
    assert principal is not None and principal.client_type == "worker"

    # CLI 令牌不能冒充 Worker 去连转码控制面
    cli_grant = _authorize(client)
    client.post(f"{_AUTH}/devices/requests/{cli_grant['user_code']}/approve")
    cli_token = _redeem(client, cli_grant["device_code"]).json()["data"]["token"]
    assert asyncio.run(resolve(f"Bearer {cli_token}")) is None
    assert asyncio.run(resolve(None)) is None
    assert asyncio.run(resolve("Bearer 乱写的")) is None


# ---------------------------------------------------------------------------
# 手工令牌：给没人能按批准的环境（docs/design/device-auth.md §6.2.1、§7）
# ---------------------------------------------------------------------------


def test_manual_token_is_usable_and_listed_alongside_paired_devices(client: TestClient) -> None:
    """手工创建的令牌与配对出来的令牌同权、同列表、同一个吊销入口。

    这是「签发的入口有两个，管理的入口只有一个」这句话的检验：网页上多开一个
    创建入口，不能在设备列表之外再长出一套平行的管理面——否则用户改密后去清点
    设备时，会漏掉整整一类凭证。
    """
    created = client.post(f"{_AUTH}/tokens", json={"name": "nas-cron"})
    assert created.status_code == 200, created.text
    payload = created.json()["data"]

    # 明文只在这一次响应里出现，且形态与配对签出来的令牌一致
    assert payload["token"].startswith("mclaw_")
    assert payload["client_type"] == "manual"
    assert payload["name"] == "nas-cron"

    # 与超管同权：业务接口直接可用，不受 client_type 上限约束
    holder = TestClient(client.app)
    headers = {"Authorization": f"Bearer {payload['token']}"}
    assert holder.get("/api/v1/subscriptions", headers=headers).status_code == 200

    # 落在同一张设备列表里，吊销后立刻失效
    listed = client.get(f"{_AUTH}/tokens").json()["data"]
    assert [t["id"] for t in listed] == [payload["id"]]
    assert client.delete(f"{_AUTH}/tokens/{payload['id']}").status_code == 200
    assert holder.get("/api/v1/subscriptions", headers=headers).status_code == 401


def test_manual_token_plaintext_is_never_readable_again(client: TestClient) -> None:
    """明文只发这一次——所以网页那张卡必须让用户当场存走。

    列表接口若哪天回读了明文，界面上「关掉就再也读不到」那句警示就变成假话，
    而它正是用户愿意当场保存的唯一理由。
    """
    plaintext = client.post(f"{_AUTH}/tokens", json={"name": "ci"}).json()["data"]["token"]

    listed = client.get(f"{_AUTH}/tokens").json()["data"]
    assert plaintext not in str(listed)
    for record in listed:
        assert "token" not in record


def test_manual_token_creation_requires_a_browser_session(client: TestClient) -> None:
    """手工入口不放宽签发面：令牌仍然造不出令牌（§4.4）。

    网页上多一个按钮，不该让一枚泄漏的令牌多一条自我复制的路。
    """
    plaintext = client.post(f"{_AUTH}/tokens", json={"name": "seed"}).json()["data"]["token"]

    holder = TestClient(client.app)
    headers = {"Authorization": f"Bearer {plaintext}"}
    assert (
        holder.post(f"{_AUTH}/tokens", json={"name": "spare"}, headers=headers).status_code == 403
    )
