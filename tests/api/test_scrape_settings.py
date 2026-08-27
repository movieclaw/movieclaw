"""刮削与整理设置接口测试：读写配置、校验、env 回落与发现页地区联动。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.media_discover import reset_media_service
from movieclaw_api.services.scrape_config import reset_scrape_config
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    reset_media_service()
    reset_scrape_config()

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post(
            "/api/v1/auth/bootstrap",
            json={"username": "admin", "password": "s3cret-pass"},
        )
        yield c

    reset_media_service()
    reset_scrape_config()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def test_scrape_config_defaults_follow_env(client: TestClient) -> None:
    """未保存过配置时：setting 为默认（跟随语义），effective 展示 env 回落值
    （TMDB_LANGUAGE 默认 zh-CN → [zh-CN, en-US]，档位跟随三个 env 默认）。"""
    resp = client.get("/api/v1/scrape/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["setting"]["language_priority"] == []
    assert data["setting"]["poster_mode"] == "default"
    assert data["effective"]["language_priority"] == ["zh-CN", "en-US"]
    assert data["effective"]["poster_size"] == "w780"
    assert data["effective"]["backdrop_size"] == "original"
    assert data["effective"]["still_size"] == "w300"


def test_scrape_config_save_and_effective(client: TestClient) -> None:
    """保存后 effective 立即反映新值；快照热更新（effective_* 同步生效）。"""
    payload = {
        "language_priority": ["ja-JP", "en-US"],
        "cert_country_priority": ["JP", "US"],
        "poster_mode": "language",
        "poster_language_priority": ["orig", "meta", "null"],
        "backdrop_language_priority": ["null", "en"],
        "poster_min_width": 0,
        "backdrop_min_width": 1280,
        "poster_size": "w500",
        "backdrop_size": "w1280",
        "still_size": "",
    }
    resp = client.put("/api/v1/scrape/config", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["setting"]["language_priority"] == ["ja-JP", "en-US"]
    assert data["effective"]["language_priority"] == ["ja-JP", "en-US"]
    assert data["effective"]["poster_size"] == "w500"
    assert data["effective"]["still_size"] == "w300"  # 空 = 继续跟随 env

    from movieclaw_api.services.scrape_config import (
        effective_image_prefs,
        effective_languages,
    )

    assert effective_languages() == ["ja-JP", "en-US"]
    prefs = effective_image_prefs()
    assert prefs.poster_mode == "language"
    assert prefs.poster_langs == ("orig", "meta", "null")
    assert prefs.backdrop_min_width == 1280

    # 重读也拿到已保存值（落库而非仅内存）
    resp = client.get("/api/v1/scrape/config")
    assert resp.json()["data"]["setting"]["poster_mode"] == "language"


def test_scrape_config_validation_rejected(client: TestClient) -> None:
    """非法值整体拒绝：坏语言标签 / 未知图片档位 / 未知 poster_mode。"""
    base = client.get("/api/v1/scrape/config").json()["data"]["setting"]
    for patch in (
        {"language_priority": ["chinese"]},
        {"poster_size": "w9999"},
        {"poster_mode": "fancy"},
        {"poster_language_priority": []},
        {"cert_country_priority": ["cn!"]},
    ):
        resp = client.put("/api/v1/scrape/config", json={**base, **patch})
        assert resp.status_code == 422, patch


def test_discover_region_defaults_and_save(client: TestClient) -> None:
    """地区默认跟随 TMDB_REGION（CN）；保存即生效并落库。"""
    resp = client.get("/api/v1/discover/region")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["region"] == "CN"
    assert data["can_edit"] is True

    resp = client.put("/api/v1/discover/region", json={"region": "US"})
    assert resp.status_code == 200
    assert resp.json()["data"]["region"] == "US"

    from movieclaw_api.services.scrape_config import effective_region

    assert effective_region() == "US"
    assert client.get("/api/v1/discover/region").json()["data"]["region"] == "US"


def test_discover_region_rejects_bad_code(client: TestClient) -> None:
    resp = client.put("/api/v1/discover/region", json={"region": "china"})
    assert resp.status_code == 422
