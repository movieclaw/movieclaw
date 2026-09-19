"""媒体库自定义封面接口的端到端测试（issue #427）。

覆盖：上传即生效（封面接口返回上传的图）、库视图的 custom_cover 标记、
恢复自动拼贴、删库时封面文件随库清理，以及对非图片 / 空文件 / 超大文件的拒绝。
上传目录用临时目录隔离，不污染真实的 data/uploads。
"""

from __future__ import annotations

import random
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings

_BASE = "/api/v1/libraries"


def _png(width: int, height: int) -> bytes:
    """噪声图——纯色图压出来只有几百字节，看不出压缩效果。"""
    from PIL import Image

    rng = random.Random(3)
    img = Image.new("RGB", (width, height))
    img.putdata(
        [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(width * height)
        ]
    )
    buffer = BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


@pytest.fixture
def client(tmp_path: Path, media_dir: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(media_dir))
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()

    from movieclaw_api.api.deps import require_login
    from movieclaw_api.api.routes import libraries as library_routes
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    async def skip_initial_scan(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        """封面用例不执行异步扫描。"""

    monkeypatch.setattr(library_routes, "enqueue_scan_job", skip_initial_scan)

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _upload(client: TestClient, library_id: int, data: bytes):
    return client.post(
        f"{_BASE}/{library_id}/cover",
        files={"file": ("cover.png", data, "image/png")},
    )


def _create(client: TestClient, tmp_path: Path) -> int:
    r = client.post(
        _BASE,
        json={"name": "电影库", "kind": "movie", "root_paths": [str(tmp_path / "movies")]},
    )
    assert r.status_code == 200
    return r.json()["data"]["id"]


def test_upload_cover_is_served_and_flagged(
    client: TestClient, tmp_path: Path, media_dir: Path
) -> None:
    """上传后：封面接口给出这张图、库视图标记 custom_cover、图被压成 JPEG。"""
    from PIL import Image

    library_id = _create(client, tmp_path)
    # 空库本来没有封面素材，拼贴给 404
    assert client.get(f"{_BASE}/{library_id}/cover").status_code == 404
    assert client.get(f"{_BASE}/{library_id}").json()["data"]["custom_cover"] is False

    source = _png(2400, 1000)
    r = client.post(
        f"{_BASE}/{library_id}/cover",
        files={"file": ("cover.png", source, "image/png")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["bytes"] < len(source)  # 压过了

    got = client.get(f"{_BASE}/{library_id}/cover")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/jpeg"
    img = Image.open(BytesIO(got.content))
    assert img.format == "JPEG"
    assert img.size == (1600, 667)  # 长边压到 1600，比例不变

    assert client.get(f"{_BASE}/{library_id}").json()["data"]["custom_cover"] is True
    assert (media_dir / "library-covers" / f"{library_id}.jpg").is_file()


def test_delete_cover_restores_collage(client: TestClient, tmp_path: Path) -> None:
    """删自定义封面后回落到自动拼贴（空库无素材即 404）。"""
    library_id = _create(client, tmp_path)
    _upload(client, library_id, _png(400, 200))

    assert client.delete(f"{_BASE}/{library_id}/cover").status_code == 200
    assert client.get(f"{_BASE}/{library_id}/cover").status_code == 404
    assert client.get(f"{_BASE}/{library_id}").json()["data"]["custom_cover"] is False
    # 再删一次：没有就是没有
    assert client.delete(f"{_BASE}/{library_id}/cover").status_code == 404


def test_cover_etag_changes_after_reupload(client: TestClient, tmp_path: Path) -> None:
    """换图后 ETag 必须变，否则播放器与浏览器会一直吃旧缓存。"""
    library_id = _create(client, tmp_path)
    _upload(client, library_id, _png(400, 200))
    first = client.get(f"{_BASE}/{library_id}/cover").headers["ETag"]
    # 同一 ETag 应当 304
    revalidated = client.get(f"{_BASE}/{library_id}/cover", headers={"If-None-Match": first})
    assert revalidated.status_code == 304

    _upload(client, library_id, _png(500, 300))
    assert client.get(f"{_BASE}/{library_id}/cover").headers["ETag"] != first


def test_deleting_library_removes_custom_cover(
    client: TestClient, tmp_path: Path, media_dir: Path
) -> None:
    """删库要把封面文件带走——它落在不可清理的 uploads 组，留下就是永久孤儿。"""
    library_id = _create(client, tmp_path)
    _upload(client, library_id, _png(300, 200))
    cover_file = media_dir / "library-covers" / f"{library_id}.jpg"
    assert cover_file.is_file()

    assert client.delete(f"{_BASE}/{library_id}").status_code == 200
    assert not cover_file.exists()


@pytest.mark.parametrize(
    ("payload", "hint"),
    [
        (b"", "为空"),
        (b"not an image at all", "无法识别"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "无法识别"),
    ],
)
def test_upload_rejects_bad_input(
    client: TestClient, tmp_path: Path, payload: bytes, hint: str
) -> None:
    """空文件、非图片、以及可内嵌脚本的 SVG 一律 400 + 中文提示。"""
    library_id = _create(client, tmp_path)
    r = client.post(f"{_BASE}/{library_id}/cover", files={"file": ("x", payload, "image/png")})
    assert r.status_code == 400
    assert hint in r.json()["message"]


def test_upload_rejects_oversized(client: TestClient, tmp_path: Path) -> None:
    """超过体积上限在解码前就挡下（防滥用硬闸）。"""
    from movieclaw_api.services.library.cover import MAX_UPLOAD_BYTES

    library_id = _create(client, tmp_path)
    blob = b"\xff" * (MAX_UPLOAD_BYTES + 1)
    r = client.post(f"{_BASE}/{library_id}/cover", files={"file": ("big.jpg", blob, "image/jpeg")})
    assert r.status_code == 400
    assert "过大" in r.json()["message"]


def test_upload_to_missing_library_is_404(client: TestClient) -> None:
    """库不存在时别在磁盘上留孤儿文件。"""
    r = client.post(f"{_BASE}/9999/cover", files={"file": ("c.png", _png(100, 100), "image/png")})
    assert r.status_code == 404
