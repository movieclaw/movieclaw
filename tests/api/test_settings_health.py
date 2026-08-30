"""设置健康聚合接口（GET /settings/health）的口径测试。

守护两件事：
1. 零状态：全新实例四个计数都是 0（侧栏不该有任何角标）；
2. 计数口径与各分区页面同源：failed 站点/下载器、stale 通道账号、
   待批准设备请求各计入对应字段，非异常状态（active/pending）不误报。

鉴权（匿名 401 / 成员 403）由全路由守护测试兜底
（tests/api/test_auth.py 与 tests/api/test_member_auth.py），这里不重复。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

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


def _health(client: TestClient) -> dict:
    resp = client.get("/api/v1/settings/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True and body["code"] == "OK"
    return body["data"]


def test_fresh_instance_reports_all_zero(client: TestClient) -> None:
    assert _health(client) == {
        "sites_failed": 0,
        "downloaders_failed": 0,
        "im_push_need_rebind": 0,
        "device_requests_pending": 0,
    }


def test_counts_follow_section_level_judgements(client: TestClient) -> None:
    """failed/stale/待批准计入对应字段；active、pending 等状态不误报。"""
    from movieclaw_db.engine import get_database
    from movieclaw_db.models.channel_account import ChannelAccount, ChannelAccountStatus
    from movieclaw_db.models.downloader_client import ClientType, DownloaderClient
    from movieclaw_db.models.site_credential import AuthType, ConfigStatus, SiteCredential

    async def seed() -> None:
        db = get_database()
        async with db.session() as session:
            # 站点：1 个验证失败 + 1 个正常（后者不计入）
            session.add(
                SiteCredential(
                    site_id="broken",
                    auth_type=AuthType.COOKIE,
                    status=ConfigStatus.FAILED,
                    last_error="Cookie 已过期",
                )
            )
            session.add(
                SiteCredential(
                    site_id="fine", auth_type=AuthType.COOKIE, status=ConfigStatus.ACTIVE
                )
            )
            # 下载器：1 个连接失败 + 1 个待验证（pending 不算异常）
            session.add(
                DownloaderClient(
                    name="坏了的 qB",
                    client_type=ClientType.QBITTORRENT,
                    url="http://127.0.0.1:1",
                    status=ConfigStatus.FAILED,
                )
            )
            session.add(
                DownloaderClient(
                    name="待验证的 TR",
                    client_type=ClientType.TRANSMISSION,
                    url="http://127.0.0.1:2",
                    status=ConfigStatus.PENDING,
                )
            )
            # 推送通道：微信 stale + Telegram active（stale 才需要重新绑定）
            session.add(
                ChannelAccount(
                    channel_id="weixin",
                    account_id="bot-stale",
                    token="enc::x",
                    base_url="https://gw.example",
                    status=ChannelAccountStatus.STALE,
                )
            )
            session.add(
                ChannelAccount(
                    channel_id="telegram",
                    account_id="bot-ok",
                    token="enc::y",
                    base_url="https://api.telegram.org",
                    status=ChannelAccountStatus.ACTIVE,
                )
            )
            await session.commit()

    asyncio.run(seed())

    # 设备：走真实配对协议发起一条待批准请求（与审批卡同一数据源）
    resp = client.post(
        f"{_AUTH}/device/authorize", json={"client_type": "cli", "client_name": "test@ci"}
    )
    assert resp.status_code == 200, resp.text

    assert _health(client) == {
        "sites_failed": 1,
        "downloaders_failed": 1,
        "im_push_need_rebind": 1,
        "device_requests_pending": 1,
    }
