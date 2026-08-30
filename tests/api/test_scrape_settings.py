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


def test_naming_templates_save_and_validate(client: TestClient) -> None:
    """命名模板：合法模板落库并立即生效，非法模板整体拒绝。"""
    base = client.get("/api/v1/scrape/config").json()["data"]["setting"]
    assert base["naming_entry_dir"] == ""  # 空 = 用内置默认模板

    payload = {
        **base,
        "naming_entry_dir": "{title} ({year}) [tmdbid-{tmdb_id}]",
        "naming_episode_file": "{title}.S{season:02d}E{episode:02d}",
    }
    resp = client.put("/api/v1/scrape/config", json=payload)
    assert resp.status_code == 200
    assert (
        resp.json()["data"]["setting"]["naming_entry_dir"] == "{title} ({year}) [tmdbid-{tmdb_id}]"
    )

    # 保存即生效：渲染器读的是同一份快照
    from movieclaw_api.services.library.naming import effective_templates

    templates = effective_templates()
    assert templates.entry_dir == "{title} ({year}) [tmdbid-{tmdb_id}]"
    assert templates.episode_file == "{title}.S{season:02d}E{episode:02d}"
    # 没配的字段继续用内置默认
    assert templates.season_dir == "Season {season:02d}"

    for patch in (
        {"naming_entry_dir": "{title}/{year}"},  # 路径分隔符
        {"naming_entry_dir": "{year}"},  # 缺片名占位符
        {"naming_episode_file": "{title} E{episode:02d}"},  # 缺季号
        {"naming_season_dir": "Season"},  # 缺季号
        {"naming_entry_dir": "{title} {episode}"},  # 该模板不可用的占位符
    ):
        bad = client.put("/api/v1/scrape/config", json={**base, **patch})
        assert bad.status_code == 422, patch


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


# ---------------------------------------------------------------------------
# 库级覆盖（docs/design/scrape-customization.md §1 / P3）
# ---------------------------------------------------------------------------


def _make_library(client: TestClient, name: str, root: str, **extra) -> dict:
    resp = client.post(
        "/api/v1/libraries",
        json={"name": name, "kind": "tv", "root_paths": [root], **extra},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["data"]


def test_library_scrape_overrides_roundtrip(client: TestClient, tmp_path) -> None:
    """库级覆盖：保存 → 读回 → 合并生效（命名模板与镜像细项）。"""
    root = str(tmp_path / "anime")
    library = _make_library(
        client,
        "动漫库",
        root,
        scrape_overrides={
            "naming_entry_dir": "{original_title} ({year})",
            "mirror_episode_thumbs": False,
        },
    )
    assert library["scrape_overrides"]["naming_entry_dir"] == "{original_title} ({year})"

    # 合并读取：该库用覆盖值，其余字段继续跟全局
    from movieclaw_api.services.scrape_config import (
        effective_mirror_flags,
        effective_naming_templates,
    )
    from movieclaw_db.models import Library

    row = Library(
        name="动漫库",
        kind="tv",
        root_paths=[root],
        scrape_overrides=library["scrape_overrides"],
    )
    assert effective_naming_templates(row).entry_dir == "{original_title} ({year})"
    # 没覆盖的模板字段回落内置默认
    assert effective_naming_templates(row).season_dir == "Season {season:02d}"
    assert effective_mirror_flags(row) == (True, True, False)
    # 无覆盖的库全跟全局
    assert effective_naming_templates(None).entry_dir == "{title} ({year})"


def test_library_overrides_reject_unknown_and_invalid(client: TestClient, tmp_path) -> None:
    """不属于刮削设置的字段、以及非法模板，保存时整体拒绝（中文 400）。"""
    root = str(tmp_path / "lib2")
    # 选图**可以**按库覆盖（P4：按条目的刮削归属库生效，设计文档 §14）
    resp = client.post(
        "/api/v1/libraries",
        json={
            "name": "选图库",
            "kind": "tv",
            "root_paths": [root],
            "scrape_overrides": {"poster_mode": "language"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["scrape_overrides"] == {"poster_mode": "language"}

    # 压根不是刮削设置里的字段则拒绝
    resp = client.post(
        "/api/v1/libraries",
        json={
            "name": "野字段库",
            "kind": "tv",
            "root_paths": [str(tmp_path / "lib3")],
            "scrape_overrides": {"not_a_setting": 1},
        },
    )
    assert resp.status_code == 400
    assert "不支持按库覆盖" in resp.json()["message"]

    # 非法命名模板同样拒绝
    resp = client.post(
        "/api/v1/libraries",
        json={
            "name": "坏模板库",
            "kind": "tv",
            "root_paths": [root],
            "scrape_overrides": {"naming_episode_file": "{title}"},
        },
    )
    assert resp.status_code == 400
    assert "{season}" in resp.json()["message"]


def test_library_overrides_can_be_cleared(client: TestClient, tmp_path) -> None:
    """空对象 = 显式清空覆盖，回到全跟全局；不传 = 不改动。"""
    root = str(tmp_path / "lib3")
    library = _make_library(
        client, "可清空库", root, scrape_overrides={"naming_season_dir": "S{season:02d}"}
    )
    library_id = library["id"]

    # 不传该字段：保持原覆盖
    resp = client.put(
        f"/api/v1/libraries/{library_id}",
        json={"name": "可清空库", "kind": "tv", "root_paths": [root]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["scrape_overrides"]["naming_season_dir"] == "S{season:02d}"

    # 传空对象：清空
    resp = client.put(
        f"/api/v1/libraries/{library_id}",
        json={"name": "可清空库", "kind": "tv", "root_paths": [root], "scrape_overrides": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["scrape_overrides"] == {}


# ---------------------------------------------------------------------------
# 局部更新：CLI / Agent 天然是"只改一项"的用法
# ---------------------------------------------------------------------------


def test_partial_update_keeps_untouched_fields(client: TestClient) -> None:
    """只提交一个字段时，其余字段必须原样保留。

    这是 CLI 与 Agent 的主用法（``mclaw scrape set --poster-mode language``）。
    若按整体替换处理，一条命令就会把用户配好的语言优先级和命名模板悄悄抹掉
    ——接口还回 200，用户下次整理才发现文件名全变了。
    """
    resp = client.put(
        "/api/v1/scrape/config",
        json={
            "language_priority": ["ja-JP", "zh-CN"],
            "naming_entry_dir": "{title} ({year}) [tmdbid-{tmdb_id}]",
            "mirror_episode_thumbs": False,
        },
    )
    assert resp.status_code == 200

    resp = client.put("/api/v1/scrape/config", json={"poster_mode": "language"})
    assert resp.status_code == 200
    setting = resp.json()["data"]["setting"]
    assert setting["poster_mode"] == "language"
    assert setting["language_priority"] == ["ja-JP", "zh-CN"]
    assert setting["naming_entry_dir"] == "{title} ({year}) [tmdbid-{tmdb_id}]"
    assert setting["mirror_episode_thumbs"] is False


def test_explicit_empty_value_restores_default(client: TestClient) -> None:
    """显式传空值 = 恢复默认（这是"没传"与"传了空"的唯一区别）。"""
    client.put(
        "/api/v1/scrape/config",
        json={"naming_entry_dir": "{title}.{year}", "language_priority": ["ja-JP"]},
    )
    resp = client.put("/api/v1/scrape/config", json={"naming_entry_dir": ""})
    assert resp.status_code == 200
    setting = resp.json()["data"]["setting"]
    assert setting["naming_entry_dir"] == ""  # 已恢复默认（空 = 跟随内置模板）
    assert setting["language_priority"] == ["ja-JP"]  # 没提到的照旧


def test_partial_update_still_validates(client: TestClient) -> None:
    """局部更新不等于放松校验：非法值照样打回，且不污染已有配置。"""
    client.put("/api/v1/scrape/config", json={"naming_entry_dir": "{title}.{year}"})
    resp = client.put("/api/v1/scrape/config", json={"poster_mode": "随便填"})
    assert resp.status_code == 422
    # 报错必须说人话：CLI 与 Agent 只能靠这句话自我纠正
    assert "default" in resp.json()["details"][0]["message"]
    assert client.get("/api/v1/scrape/config").json()["data"]["setting"][
        "naming_entry_dir"
    ] == "{title}.{year}"
