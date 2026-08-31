"""图片附件 API 端到端：上传 → 引用发消息 → 转录回放 → 下载 → retry 三态。

覆盖协议约束（docs/design/agent-image-input.md §8）：
- 对外只收 attachment_id，ContentPart 由服务端组装；
- 转录里图片是引用形态（无 base64 字节）；
- retry 不传 attachments 时沿用原图（text() 会丢图的回归防线）。
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from tests.api.test_agent import _StreamProtocol, configure_provider

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.agent_attachments import reset_agent_attachment_store
from movieclaw_api.services.agent_sessions import reset_agent_session_store
from movieclaw_llm.protocols import PROTOCOLS


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("AGENT_SESSIONS_DIR", str(tmp_path / "agent-sessions"))
    get_settings.cache_clear()
    reset_agent_session_store()
    reset_agent_attachment_store()
    monkeypatch.setitem(PROTOCOLS, "openai_chat", _StreamProtocol)

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    reset_agent_session_store()
    reset_agent_attachment_store()


def upload_png(client, name: str = "截图.png") -> str:
    r = client.post(
        "/api/v1/sessions/attachments",
        files={"file": (name, png_bytes(), "image/png")},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["width"] == 8 and data["bytes"] > 0
    return data["attachment_id"]


def send_and_finish(client, payload: dict) -> tuple[str, str]:
    started = client.post("/api/v1/sessions", json=payload)
    assert started.status_code == 202, started.text
    data = started.json()["data"]
    with client.stream("GET", f"/api/v1/sessions/{data['session_id']}/events") as r:
        r.read()
    return data["session_id"], data["message_id"]


def wait_not_running(client, session_id: str) -> dict:
    for _ in range(50):
        item = client.get(f"/api/v1/sessions/{session_id}").json()["data"]["session"]
        if not item["running"]:
            return item
        time.sleep(0.1)
    pytest.fail("会话运行状态未在期限内清空")


# ---------------------------------------------------------------------------
# 上传校验
# ---------------------------------------------------------------------------


def test_upload_rejects_disguised_file(client) -> None:
    r = client.post(
        "/api/v1/sessions/attachments",
        files={"file": ("evil.png", b"<svg onload=alert(1)/>", "image/png")},
    )
    assert r.status_code == 400
    assert "不支持的图片格式" in r.json()["message"]


def test_upload_and_reference_in_new_session(client) -> None:
    configure_provider(client)
    attachment_id = upload_png(client)
    session_id, _ = send_and_finish(
        client, {"content": "看下这张图", "attachments": [attachment_id]}
    )
    wait_not_running(client, session_id)

    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    user_content = detail["entries"][0]["message"]["content"]
    # 转录里是引用形态的内容块：text + image，image 无字节
    assert user_content[0]["type"] == "text"
    assert user_content[1]["type"] == "image"
    assert user_content[1]["attachment_id"] == attachment_id
    assert user_content[1]["name"] == "截图.png"
    assert not user_content[1].get("data")

    # 已绑定附件可下载（immutable 缓存），未绑定的会话读不到
    got = client.get(f"/api/v1/sessions/{session_id}/attachments/{attachment_id}")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert "immutable" in got.headers["cache-control"]
    other = client.get(f"/api/v1/sessions/nonexistent/attachments/{attachment_id}")
    assert other.status_code == 404


def test_image_only_message_allowed_and_previewed(client) -> None:
    configure_provider(client)
    attachment_id = upload_png(client)
    session_id, _ = send_and_finish(client, {"content": "", "attachments": [attachment_id]})
    item = wait_not_running(client, session_id)
    # 纯图消息的侧栏预览与标题用 [图片] 占位
    assert item["title"] == "[图片]"
    assert item["last_prompt"] == "[图片]"


def test_empty_content_without_attachments_rejected(client) -> None:
    r = client.post("/api/v1/sessions", json={"content": ""})
    assert r.status_code == 422


def test_transcript_has_no_base64(client, tmp_path) -> None:
    """脱水不变量的文件级检查：转录 JSONL 里 grep 不到 base64 字节。"""
    configure_provider(client)
    attachment_id = upload_png(client)
    session_id, _ = send_and_finish(client, {"content": "图", "attachments": [attachment_id]})
    wait_not_running(client, session_id)
    transcript = (tmp_path / "agent-sessions" / f"{session_id}.jsonl").read_text("utf-8")
    assert '"data"' not in transcript
    assert attachment_id in transcript


# ---------------------------------------------------------------------------
# retry 三态
# ---------------------------------------------------------------------------


def _retry(client, session_id: str, payload: dict) -> None:
    r = client.post(f"/api/v1/sessions/{session_id}/retry", json=payload)
    assert r.status_code == 202, r.text
    with client.stream("GET", f"/api/v1/sessions/{session_id}/events") as s:
        s.read()
    wait_not_running(client, session_id)


def _first_user_content(client, session_id: str):
    detail = client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    return detail["entries"][0]["message"]["content"]


def test_retry_keeps_original_images_by_default(client) -> None:
    configure_provider(client)
    attachment_id = upload_png(client)
    session_id, message_id = send_and_finish(
        client, {"content": "这是什么", "attachments": [attachment_id]}
    )
    wait_not_running(client, session_id)

    # 换问题但不传 attachments → 沿用原图（text() 丢图的回归防线）
    _retry(client, session_id, {"message_id": message_id, "content": "换个问题"})
    content = _first_user_content(client, session_id)
    assert content[0]["text"] == "换个问题"
    assert content[1]["attachment_id"] == attachment_id


def test_retry_with_empty_attachments_drops_images(client) -> None:
    configure_provider(client)
    attachment_id = upload_png(client)
    session_id, message_id = send_and_finish(
        client, {"content": "这是什么", "attachments": [attachment_id]}
    )
    wait_not_running(client, session_id)

    _retry(
        client,
        session_id,
        {"message_id": message_id, "content": "不看图了", "attachments": []},
    )
    # 显式去图后是纯字符串消息
    assert _first_user_content(client, session_id) == "不看图了"


# ---------------------------------------------------------------------------
# 会话删除联动
# ---------------------------------------------------------------------------


def test_delete_session_removes_attachments(client, tmp_path) -> None:
    configure_provider(client)
    attachment_id = upload_png(client)
    session_id, _ = send_and_finish(client, {"content": "图", "attachments": [attachment_id]})
    wait_not_running(client, session_id)
    assets = tmp_path / "agent-sessions" / f"{session_id}.assets"
    assert assets.is_dir()

    r = client.delete(f"/api/v1/sessions/{session_id}")
    assert r.status_code == 200
    assert not assets.exists()


def test_fork_copies_attachments_source_deletable(client, tmp_path) -> None:
    """fork 同 id 复制附件：源会话删除后，新会话的图仍可下载（引用未改写）。"""
    configure_provider(client)
    attachment_id = upload_png(client)
    session_id, _ = send_and_finish(client, {"content": "看图", "attachments": [attachment_id]})
    wait_not_running(client, session_id)

    forked = client.post(f"/api/v1/sessions/{session_id}/fork")
    assert forked.status_code == 201, forked.text
    new_id = forked.json()["data"]["session"]["id"]
    # 快照里的引用未改写（同 id）
    history = forked.json()["data"]["entries"][0]["replacement_history"]
    image_parts = [
        p
        for message in history
        for p in (message["content"] if isinstance(message["content"], list) else [])
        if p["type"] == "image"
    ]
    assert [p["attachment_id"] for p in image_parts] == [attachment_id]

    assert client.delete(f"/api/v1/sessions/{session_id}").status_code == 200
    got = client.get(f"/api/v1/sessions/{new_id}/attachments/{attachment_id}")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
