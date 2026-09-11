"""图片磁盘缓存测试：命中/回源、哈希分片、并发去重、容量清理、路由集成。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import image_proxy as image_proxy_module
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.image_cache import ImageCache, reset_image_cache
from movieclaw_api.services.image_proxy import ImageProxy
from movieclaw_api.services.image_variants import (
    ImageVariant,
    ImageVariantService,
    local_source_version,
    reset_image_variant_service,
)
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box


async def _fake_resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


def _make_cache(tmp_path: Path, handler, *, max_bytes: int = 10 * 1024 * 1024) -> ImageCache:
    proxy = ImageProxy(transport=httpx.MockTransport(handler), resolver=_fake_resolver)
    return ImageCache(tmp_path / "images", proxy, max_bytes=max_bytes)


async def test_miss_fetches_and_hit_reads_local(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=b"jpeg-bytes")

    cache = _make_cache(tmp_path, handler)
    url = "https://img.host-a.com/poster.jpg"

    first = await cache.get_or_fetch(url)
    assert first.content_type == "image/jpeg"
    assert first.path.read_bytes() == b"jpeg-bytes"
    # 哈希分片：内容文件位于哈希前两位的子目录，旁边有记录来源的元数据文件
    digest = hashlib.sha256(url.encode()).hexdigest()
    assert first.path == tmp_path / "images" / digest[:2] / digest
    meta = json.loads(first.path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["url"] == url
    assert meta["content_type"] == "image/jpeg"

    second = await cache.get_or_fetch(url)
    assert second.path == first.path
    assert calls == 1, "第二次访问应命中本地缓存，不再回源"


async def test_same_path_on_different_hosts_do_not_collide(tmp_path: Path) -> None:
    """域名参与哈希：不同图床上的同名路径必须是两个独立的缓存条目。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "image/png"}, content=request.url.host.encode()
        )

    cache = _make_cache(tmp_path, handler)
    a = await cache.get_or_fetch("https://img.host-a.com/x/poster.png")
    b = await cache.get_or_fetch("https://img.host-b.com/x/poster.png")
    assert a.path != b.path
    assert a.path.read_bytes() == b"img.host-a.com"
    assert b.path.read_bytes() == b"img.host-b.com"


async def test_concurrent_requests_fetch_once(tmp_path: Path) -> None:
    calls = 0
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await release.wait()
        return httpx.Response(200, headers={"Content-Type": "image/webp"}, content=b"webp")

    cache = _make_cache(tmp_path, handler)
    url = "https://img.host-a.com/hot.webp"
    tasks = [asyncio.create_task(cache.get_or_fetch(url)) for _ in range(5)]
    await asyncio.sleep(0.01)  # 让 5 个请求都进入等待
    release.set()
    results = await asyncio.gather(*tasks)
    assert calls == 1, "同一 URL 的并发请求应只回源一次"
    assert all(r.path == results[0].path for r in results)


async def test_purge_evicts_least_recently_used(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"x" * 100)

    # 上限很小：写入若干条后触发清理，最旧的条目被淘汰
    cache = _make_cache(tmp_path, handler, max_bytes=500)
    paths = []
    for i in range(6):
        cached = await cache.get_or_fetch(f"https://img.host-a.com/{i}.png")
        paths.append(cached.path)
        # 拉开 mtime，保证淘汰顺序稳定可断言
        os.utime(cached.path, (i, i))

    cache._purge_if_over_limit()
    survivors = [p for p in paths if p.exists()]
    assert not paths[0].exists(), "最久未访问的条目应最先被淘汰"
    assert not paths[0].with_suffix(".json").exists(), "元数据应与内容一并删除"
    total = sum(p.stat().st_size for p in survivors)
    assert total <= 500 * 0.9, "清理后总量应降到上限的 90% 以内"

    # 被淘汰的条目再次访问：重新回源，缓存自动恢复
    again = await cache.get_or_fetch("https://img.host-a.com/0.png")
    assert again.path.read_bytes() == b"x" * 100


async def test_hit_does_not_write_and_keeps_lru_usable(tmp_path: Path) -> None:
    """命中不写盘：海报墙滚一屏上百张图，逐张刷 mtime 就是上百次随机写，
    足以把休眠的硬盘唤醒。mtime 只服务于 LRU 排序，够新就不该动它。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"png")

    cache = _make_cache(tmp_path, handler)
    url = "https://img.host-a.com/p.png"
    cached = await cache.get_or_fetch(url)
    before = cached.path.stat().st_mtime_ns

    for _ in range(5):
        again = await cache.get_or_fetch(url)
        assert again.content_type == "image/png"
        assert again.version == cached.version
    assert cached.path.stat().st_mtime_ns == before, "刚写过的条目不该被反复刷 mtime"

    # 但足够旧的条目仍要刷新，否则 LRU 会把还在用的图当成冷数据淘汰掉
    stale = 1.0
    os.utime(cached.path, (stale, stale))
    await cache.get_or_fetch(url)
    assert cached.path.stat().st_mtime > stale, "久未刷新的条目命中时应更新 mtime"


async def test_hit_metadata_follows_content_replacement(tmp_path: Path) -> None:
    """命中路径会在进程内记住元数据，但内容被重新写过后必须立刻跟上——
    否则重新回源/换图之后，派生缩略图会一直按旧版本号命中旧图。"""
    content_type = "image/png"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": content_type}, content=b"v1")

    cache = _make_cache(tmp_path, handler)
    url = "https://img.host-a.com/p.png"
    first = await cache.get_or_fetch(url)
    await cache.get_or_fetch(url)  # 让元数据进内存

    # 模拟一次重新回源（原地覆盖内容 + 新的元数据）
    content_type = "image/webp"
    content_path, meta_path = cache._entry_paths(url)
    meta_path.unlink()
    content_path.unlink()
    second = await cache.get_or_fetch(url)
    assert second.content_type == "image/webp"
    assert second.version != first.version


async def test_variant_is_webp_cached_and_never_upscales(tmp_path: Path) -> None:
    """横卡派生按 480×270 封顶；小于目标的分集图保持原尺寸且二次命中缓存。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    cache = _make_cache(tmp_path, handler)
    service = ImageVariantService(cache)

    large = tmp_path / "large.jpg"
    Image.new("RGB", (1600, 900), "#224466").save(large, "JPEG", quality=95)
    first = await service.get_or_create(
        large,
        source_key="asset:large.jpg",
        source_version=local_source_version(large),
        variant=ImageVariant.LANDSCAPE_CARD,
    )
    with Image.open(first.path) as image:
        assert image.format == "WEBP"
        assert image.size == (480, 270)
    assert first.path.stat().st_size < large.stat().st_size

    second = await service.get_or_create(
        large,
        source_key="asset:large.jpg",
        source_version=local_source_version(large),
        variant=ImageVariant.LANDSCAPE_CARD,
    )
    assert second.path == first.path

    small = tmp_path / "small.jpg"
    Image.new("RGB", (300, 169), "#664422").save(small, "JPEG")
    derived_small = await service.get_or_create(
        small,
        source_key="asset:small.jpg",
        source_version=local_source_version(small),
        variant=ImageVariant.LANDSCAPE_CARD,
    )
    with Image.open(derived_small.path) as image:
        assert image.size == (300, 169), "小分集图不应为凑 480px 被强行放大"


async def test_variant_keeps_source_aspect_without_cropping(tmp_path: Path) -> None:
    """其他库的横版封面（16:9）走竖海报预设时等比缩进外接框，不能裁成 2:3 竖条：
    前端卡片框按真实比例排版，服务端一裁就只剩画面正中一小块。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    service = ImageVariantService(_make_cache(tmp_path, handler))
    wide = tmp_path / "wide-poster.jpg"
    Image.new("RGB", (960, 540), "#335577").save(wide, "JPEG", quality=95)
    derived = await service.get_or_create(
        wide,
        source_key="asset:wide-poster.jpg",
        source_version=local_source_version(wide),
        variant=ImageVariant.POSTER_CARD,
    )
    with Image.open(derived.path) as image:
        width, height = image.size
        assert width == 328, "宽度贴齐外接框"
        assert abs(width / height - 16 / 9) < 0.02, f"比例应保持 16:9，实际 {width}x{height}"

    tall = tmp_path / "tall-thumb.jpg"
    Image.new("RGB", (1080, 1920), "#553377").save(tall, "JPEG", quality=95)
    derived_tall = await service.get_or_create(
        tall,
        source_key="asset:tall-thumb.jpg",
        source_version=local_source_version(tall),
        variant=ImageVariant.LANDSCAPE_CARD,
    )
    with Image.open(derived_tall.path) as image:
        width, height = image.size
        assert height == 270, "高度贴齐外接框"
        assert abs(width / height - 9 / 16) < 0.02, f"比例应保持 9:16，实际 {width}x{height}"


# ---------------------------------------------------------------------------
# 路由集成：登录 → /images/proxy → 缓存落盘 → FileResponse + 长缓存头
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("IMAGE_CACHE_DIR", str(tmp_path / "img-cache"))
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    reset_image_cache()
    reset_image_variant_service()
    valid_image = BytesIO()
    Image.new("RGB", (1600, 900), "#335577").save(valid_image, "JPEG", quality=95)

    def image_handler(request: httpx.Request) -> httpx.Response:
        content = (
            valid_image.getvalue()
            if request.url.path.endswith("/valid.jpg")
            else b"route-jpeg"
        )
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=content)

    # 把共享代理单例替换成 Mock 传输 + 静态 DNS，用例不出网
    monkeypatch.setattr(
        image_proxy_module,
        "_proxy",
        ImageProxy(
            transport=httpx.MockTransport(image_handler),
            resolver=_fake_resolver,
        ),
    )

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post("/api/v1/auth/bootstrap", json={"username": "admin", "password": "s3cret-pass"})
        yield c

    reset_image_cache()
    reset_image_variant_service()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def test_proxy_route_serves_cached_image(client: TestClient, tmp_path: Path) -> None:
    url = "https://img.host-a.com/poster.jpg"
    resp = client.get("/api/v1/images/proxy", params={"url": url})
    assert resp.status_code == 200
    assert resp.content == b"route-jpeg"
    assert resp.headers["content-type"] == "image/jpeg"
    assert "immutable" in resp.headers["cache-control"]
    # 已按 URL 哈希落盘到配置的缓存目录
    digest = hashlib.sha256(url.encode()).hexdigest()
    assert (tmp_path / "img-cache" / digest[:2] / digest).is_file()
    # 二次访问命中缓存，同样成功
    assert client.get("/api/v1/images/proxy", params={"url": url}).status_code == 200


def test_remote_and_local_routes_share_landscape_variant(
    client: TestClient, tmp_path: Path
) -> None:
    """远程原图与本地 metadata 资产都能请求同一个受控横卡预设。"""
    remote = client.get(
        "/api/v1/images/proxy",
        params={
            "url": "https://img.host-a.com/valid.jpg",
            "variant": "landscape-card",
        },
    )
    assert remote.status_code == 200
    assert remote.headers["content-type"] == "image/webp"
    assert "immutable" in remote.headers["cache-control"]
    with Image.open(BytesIO(remote.content)) as image:
        assert image.size == (480, 270)

    asset = tmp_path / "metadata" / "images" / "14" / "backdrop.jpg"
    asset.parent.mkdir(parents=True)
    Image.new("RGB", (1920, 1080), "#775533").save(asset, "JPEG", quality=95)
    version = str(int(asset.stat().st_mtime))
    local = client.get(
        "/api/v1/images/assets/14/backdrop.jpg",
        params={"v": version, "variant": "landscape-card"},
    )
    assert local.status_code == 200
    assert local.headers["content-type"] == "image/webp"
    assert "immutable" in local.headers["cache-control"]
    with Image.open(BytesIO(local.content)) as image:
        assert image.size == (480, 270)
