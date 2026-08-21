"""取流签名 token 测试（docs/design/web-player.md §4.7）。

token 是取流端点唯一的凭据——``<video src>``、hls.js 拉分片、原生 HLS 都带
不了自定义 header。所以它的每一种「不符」都必须挡住，而且**挡法要一致**：
一律返回 None（端点按 404 应答），不给探测者区分「token 假」和「文件不存在」
的机会。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import rotate_session_secret
from movieclaw_api.services.playback.signing import (
    issue_stream_token,
    verify_stream_token,
)
from movieclaw_api.settings.store import (
    init_setting_store,
    reset_setting_store,
)
from movieclaw_db.crypto import init_secret_box, reset_secret_box
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations


@pytest_asyncio.fixture
async def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'sign.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()
    reset_secret_box()
    reset_setting_store()
    init_secret_box(None, tmp_path / ".secret_key")
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    init_setting_store(database=get_database())
    yield
    reset_setting_store()
    reset_secret_box()
    await dispose_db()
    get_settings.cache_clear()


async def test_round_trip_carries_the_grant(store):
    token = await issue_stream_token(member_id=3, file_id=42, session_id="S1")
    grant = await verify_stream_token(token, file_id=42, session_id="S1")
    assert grant is not None
    assert grant.member_id == 3 and grant.file_id == 42 and grant.session_id == "S1"


async def test_token_without_session_is_valid_for_direct_play(store):
    """档 0 没有会话，token 只绑文件。"""
    token = await issue_stream_token(member_id=0, file_id=7)
    grant = await verify_stream_token(token, file_id=7)
    assert grant is not None and grant.session_id is None


@pytest.mark.parametrize("mutate", [
    lambda t: t + "x",
    lambda t: "x" + t,
    lambda t: t[:-1],
    lambda t: t.replace(".", "_", 1),
    lambda _t: "",
    lambda _t: "not-a-token",
])
async def test_tampered_tokens_are_rejected(store, mutate):
    token = await issue_stream_token(member_id=0, file_id=1)
    assert await verify_stream_token(mutate(token)) is None


async def test_expired_token_is_rejected(store):
    token = await issue_stream_token(member_id=0, file_id=1, ttl_seconds=-1)
    assert await verify_stream_token(token) is None


async def test_token_is_scoped_to_its_file(store):
    token = await issue_stream_token(member_id=0, file_id=1)
    assert await verify_stream_token(token, file_id=1) is not None
    assert await verify_stream_token(token, file_id=2) is None


async def test_token_is_scoped_to_its_session(store):
    token = await issue_stream_token(member_id=0, file_id=1, session_id="A")
    assert await verify_stream_token(token, session_id="A") is not None
    assert await verify_stream_token(token, session_id="B") is None


async def test_direct_play_token_cannot_reach_a_session(store):
    """不带 session 的 token 拿去取分片必须失败——否则档 0 的链接能翻墙进会话。"""
    token = await issue_stream_token(member_id=0, file_id=1)
    assert await verify_stream_token(token, session_id="A") is None


async def test_rotating_the_session_secret_invalidates_stream_tokens(store):
    """复用登录密钥的附带语义：全端下线也会掐断所有在途取流链接。"""
    token = await issue_stream_token(member_id=0, file_id=1)
    assert await verify_stream_token(token) is not None
    await rotate_session_secret()
    assert await verify_stream_token(token) is None


async def test_token_does_not_leak_a_secret(store):
    """负载只放授权范围，不放任何秘密——token 本身可以出现在日志与 URL 里。"""
    token = await issue_stream_token(member_id=9, file_id=88, session_id="S")
    import base64

    payload = token.split(".")[0]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    assert b"88" in decoded  # 授权范围是明文的，这没问题
    assert b"secret" not in decoded.lower()
