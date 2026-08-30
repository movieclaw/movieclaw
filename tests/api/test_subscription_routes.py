"""语义化订阅 API 的端到端契约测试。

重点守住本次重构的三条边界：创建只消费 Discover ``title_ref``；追踪状态
使用明确枚举；管理员删除与成员退出是不同公开意图。数据库仍复用既有表。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"


def _fake_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3/movie/100":
            return httpx.Response(
                200,
                json={
                    "id": 100,
                    "title": "测试电影",
                    "original_title": "Test Movie",
                    "release_date": "2024-01-01",
                    "status": "Released",
                    "external_ids": {},
                    "alternative_titles": {"titles": []},
                    "translations": {"translations": []},
                },
            )
        if request.url.path == "/3/tv/200":
            return httpx.Response(
                200,
                json={
                    "id": 200,
                    "name": "测试剧集",
                    "original_name": "Test Show",
                    "first_air_date": "2024-01-01",
                    "status": "Returning Series",
                    "external_ids": {},
                    "alternative_titles": {"results": []},
                    "translations": {"translations": []},
                    "seasons": [{"season_number": 1}],
                },
            )
        if request.url.path == "/3/tv/200/season/1":
            return httpx.Response(
                200,
                json={
                    "name": "第 1 季",
                    "air_date": "2024-01-01",
                    "episodes": [
                        {"episode_number": 1, "name": "E1", "air_date": "2999-01-01"}
                    ],
                },
            )
        if request.url.path == "/3/tv/201":
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            return httpx.Response(
                200,
                json={
                    "id": 201,
                    "name": "今日更新剧集",
                    "original_name": "Today Show",
                    "first_air_date": today,
                    "status": "Returning Series",
                    "external_ids": {},
                    "alternative_titles": {"results": []},
                    "translations": {"translations": []},
                    "seasons": [{"season_number": 1}],
                },
            )
        if request.url.path == "/3/tv/201/season/1":
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            return httpx.Response(
                200,
                json={
                    "name": "第 1 季",
                    "air_date": today,
                    "episodes": [{"episode_number": 2, "name": "E2", "air_date": today}],
                },
            )
        return httpx.Response(404, json={})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'subscriptions.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.api.routes import subscriptions as routes
    from movieclaw_api.app import create_app

    monkeypatch.setattr(routes, "get_tmdb_client", _fake_tmdb)
    app = create_app()
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/auth/bootstrap",
            json={"username": "admin", "password": "s3cret-pass"},
        )
        assert response.status_code == 200
        yield test_client

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def test_create_from_title_ref_and_set_tracking_state(client: TestClient) -> None:
    preview = client.post(
        "/api/v1/subscriptions/title-preview",
        json={"title_ref": "tmdb:movie:100"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["media"]["tmdb_id"] == 100

    created = client.post(
        "/api/v1/subscriptions",
        json={"title_ref": "tmdb:movie:100"},
    )
    assert created.status_code == 200, created.text
    payload = created.json()["data"]
    assert payload["subscription"]["media"]["title"] == "测试电影"
    assert payload["download_routing"] is not None
    subscription_id = payload["subscription"]["id"]

    paused = client.patch(
        f"/api/v1/subscriptions/{subscription_id}/tracking-state",
        json={"state": "paused"},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["data"]["status"] == "paused"

    resumed = client.patch(
        f"/api/v1/subscriptions/{subscription_id}/tracking-state",
        json={"state": "active"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["status"] in {"active", "completed"}


def test_create_rejects_guessed_or_legacy_identity_fields(client: TestClient) -> None:
    invalid_ref = client.post(
        "/api/v1/subscriptions",
        json={"title_ref": "100"},
    )
    assert invalid_ref.status_code == 400
    assert "title_ref" in invalid_ref.json()["message"]

    legacy_shape = client.post(
        "/api/v1/subscriptions",
        json={"kind": "movie", "tmdb_id": 100},
    )
    assert legacy_shape.status_code == 422


def test_create_tv_rejects_empty_season_selection(client: TestClient) -> None:
    """剧集不给季又不开自动续订：以前静默建出一条 0 工单的「已完成」空订阅。"""
    rejected = client.post(
        "/api/v1/subscriptions",
        json={"title_ref": "tmdb:tv:200", "selected_seasons": []},
    )
    assert rejected.status_code == 400, rejected.text
    assert "季" in rejected.json()["message"]
    assert client.get("/api/v1/subscriptions?kind=tv").json()["data"] == []

    # 「只追新集」仍是合法组合：不勾季 + 自动续订
    follow_only = client.post(
        "/api/v1/subscriptions",
        json={"title_ref": "tmdb:tv:200", "selected_seasons": [], "follow_future": True},
    )
    assert follow_only.status_code == 200, follow_only.text


def test_set_follow_future_uses_dedicated_tv_endpoint(client: TestClient) -> None:
    created = client.post(
        "/api/v1/subscriptions",
        json={"title_ref": "tmdb:tv:200", "selected_seasons": [1]},
    )
    assert created.status_code == 200, created.text
    subscription_id = created.json()["data"]["subscription"]["id"]

    listed = client.get("/api/v1/subscriptions?kind=tv")
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"][0]["season_collection"] == [
        {
            "season_number": 1,
            "name": "第 1 季",
            "air_date": "2024-01-01",
            "episode_count": 1,
            "aired_count": 0,
            "owned_count": 0,
        }
    ]

    enabled = client.patch(
        f"/api/v1/subscriptions/{subscription_id}/follow-future",
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["message"] == "已开启自动续订"
    assert enabled.json()["data"]["follow_future"] is True

    disabled = client.patch(
        f"/api/v1/subscriptions/{subscription_id}/follow-future",
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["message"] == "已关闭自动续订"
    assert disabled.json()["data"]["follow_future"] is False


def test_today_arrivals_lists_today_episode_without_poster_or_progress(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/subscriptions",
        json={"title_ref": "tmdb:tv:201", "selected_seasons": [1]},
    )
    assert created.status_code == 200, created.text

    response = client.get("/api/v1/subscriptions/today-arrivals")
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert len(rows) == 1
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert rows == [
        {
            "subscription_id": created.json()["data"]["subscription"]["id"],
            "wanted_id": rows[0]["wanted_id"],
            "media_title": "今日更新剧集",
            "media_kind": "tv",
            "season_number": 1,
            "episode_number": 2,
            "status": "wanted",
            "air_date": today,
            "expected_day": today,
            "days_ahead": 0,
            "release_forecast": None,
            "next_probe_at": None,
            "info_hash": None,
            "grabbed_at": None,
            "downloaded_at": None,
            "estimated_release_to_import_minutes": 60,
            "estimated_download_to_import_minutes": 10,
        }
    ]
    assert "poster_url" not in rows[0]
    assert "progress" not in rows[0]


def test_set_follow_future_rejects_movie_subscription(client: TestClient) -> None:
    created = client.post(
        "/api/v1/subscriptions",
        json={"title_ref": "tmdb:movie:100"},
    )
    subscription_id = created.json()["data"]["subscription"]["id"]

    response = client.patch(
        f"/api/v1/subscriptions/{subscription_id}/follow-future",
        json={"enabled": True},
    )
    assert response.status_code == 400
    assert response.json()["message"] == "只有剧集订阅可以设置自动续订"


def test_admin_permanent_delete_uses_explicit_endpoint_semantics(client: TestClient) -> None:
    created = client.post(
        "/api/v1/subscriptions",
        json={"title_ref": "tmdb:movie:100"},
    )
    subscription_id = created.json()["data"]["subscription"]["id"]

    deleted = client.delete(f"/api/v1/subscriptions/{subscription_id}")
    assert deleted.status_code == 200
    assert "永久删除" in deleted.json()["message"]
    assert client.get(f"/api/v1/subscriptions/{subscription_id}").status_code == 404
