"""库封面拼贴的并发去重回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from movieclaw_api.services.library import cover


async def test_concurrent_cover_requests_share_selection_and_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一库的并发请求只能执行一次素材选择和拼贴渲染。"""
    selected = 0
    rendered = 0
    selecting = asyncio.Event()
    release = asyncio.Event()
    poster = tmp_path / "poster.jpg"
    poster.write_bytes(b"poster")

    async def select_once(_library_id: int) -> list[Path]:
        nonlocal selected
        selected += 1
        selecting.set()
        await release.wait()
        return [poster]

    def render_once(_posters: list[Path], output: Path) -> None:
        nonlocal rendered
        rendered += 1
        output.write_bytes(b"cover")

    monkeypatch.setattr(cover, "select_cover_posters", select_once)
    monkeypatch.setattr(cover, "_cover_key", lambda _paths: "cover-key")
    monkeypatch.setattr(cover, "covers_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(cover, "render_shelf_collage", render_once)

    tasks = [asyncio.create_task(cover.ensure_library_cover(42)) for _ in range(5)]
    await asyncio.wait_for(selecting.wait(), timeout=1)
    assert selected == 1
    release.set()

    results = await asyncio.gather(*tasks)
    assert results == [(tmp_path / "covers" / "42-cover-key.jpg", "cover-key")] * 5
    assert rendered == 1
    await asyncio.sleep(0)
    assert 42 not in cover._cover_tasks


async def test_no_poster_result_does_not_leave_cover_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无可用海报时任务会清理，之后的调用能够重新选择素材。"""
    selected = 0

    async def no_posters(_library_id: int) -> list[Path]:
        nonlocal selected
        selected += 1
        return []

    monkeypatch.setattr(cover, "select_cover_posters", no_posters)

    assert await cover.ensure_library_cover(43) is None
    await asyncio.sleep(0)
    assert 43 not in cover._cover_tasks
    assert await cover.ensure_library_cover(43) is None
    assert selected == 2


async def test_selection_error_does_not_leave_cover_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """选择素材异常仍会回收任务，避免该库后续请求永久复用失败任务。"""
    calls = 0

    async def fail_selection(_library_id: int) -> list[Path]:
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(cover, "select_cover_posters", fail_selection)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await cover.ensure_library_cover(44)
    await asyncio.sleep(0)
    assert 44 not in cover._cover_tasks

    with pytest.raises(RuntimeError, match="database unavailable"):
        await cover.ensure_library_cover(44)
    assert calls == 2


# ---------------------------------------------------------------------------
# 自定义封面（issue #427）：上传图归一化、优先级与回落
# ---------------------------------------------------------------------------


def _png(width: int, height: int, *, alpha: bool = False) -> bytes:
    """造一张噪声图——纯色图压出来只有几百字节，压不出体积差异。"""
    import random
    from io import BytesIO

    from PIL import Image

    rng = random.Random(7)
    mode = "RGBA" if alpha else "RGB"
    img = Image.new(mode, (width, height))
    img.putdata(
        [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            + ((rng.randrange(256),) if alpha else ())
            for _ in range(width * height)
        ]
    )
    buffer = BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


def test_normalize_shrinks_long_edge_and_outputs_jpeg() -> None:
    """超大图被压到长边 1600、输出 JPEG，且体积显著变小。"""
    from io import BytesIO

    from PIL import Image

    source = _png(2400, 1200)
    out = cover.normalize_cover_image(source)

    img = Image.open(BytesIO(out))
    assert img.format == "JPEG"
    assert max(img.size) == cover.CUSTOM_MAX_EDGE
    # 不裁剪：比例原样保留（2:1）
    assert img.size == (1600, 800)
    assert len(out) < len(source)


def test_normalize_keeps_small_image_unscaled() -> None:
    """小图只重编码不放大——放大只会糊，且白占体积。"""
    from io import BytesIO

    from PIL import Image

    out = cover.normalize_cover_image(_png(640, 360))
    assert Image.open(BytesIO(out)).size == (640, 360)


def test_normalize_flattens_alpha() -> None:
    """带透明通道的 PNG 能落成 JPEG（JPEG 没有 alpha，必须先填底）。"""
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(cover.normalize_cover_image(_png(320, 200, alpha=True))))
    assert img.format == "JPEG"
    assert img.mode == "RGB"


def test_normalize_rejects_non_image() -> None:
    """不是图片的文件给出中文提示，而不是抛 500。"""
    with pytest.raises(ValueError, match="无法识别"):
        cover.normalize_cover_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>")


def test_normalize_rejects_pixel_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    """像素数超限在解码前就被拒，不给解压炸弹撑爆内存的机会。"""
    monkeypatch.setattr(cover, "MAX_UPLOAD_PIXELS", 1000)
    with pytest.raises(ValueError, match="尺寸过大"):
        cover.normalize_cover_image(_png(200, 200))


async def test_custom_cover_takes_priority_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有自定义封面时不碰拼贴；删掉后自动回落到拼贴。"""
    selected = 0

    async def select_once(_library_id: int) -> list[Path]:
        nonlocal selected
        selected += 1
        poster = tmp_path / "poster.jpg"
        poster.write_bytes(b"poster")
        return [poster]

    monkeypatch.setattr(cover, "select_cover_posters", select_once)
    monkeypatch.setattr(cover, "_cover_key", lambda _paths: "collage-key")
    monkeypatch.setattr(cover, "covers_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(cover, "render_shelf_collage", lambda _p, out: out.write_bytes(b"c"))
    monkeypatch.setattr(cover, "custom_covers_dir", lambda: tmp_path / "custom")

    version = cover.save_custom_cover(51, cover.normalize_cover_image(_png(300, 200)))
    assert cover.has_custom_cover(51)

    path, key = await cover.ensure_library_cover(51)  # type: ignore[misc]
    assert path == cover.custom_cover_path(51)
    assert key == version
    assert selected == 0  # 连候选素材都没扫

    assert cover.remove_custom_cover(51) is True
    assert cover.remove_custom_cover(51) is False
    result = await cover.ensure_library_cover(51)
    assert result is not None and result[1] == "collage-key"
    assert selected == 1


def test_save_custom_cover_drops_stale_collage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设了自定义封面后，该库的拼贴产物再没人读，顺手清掉。"""
    collages = tmp_path / "covers"
    collages.mkdir()
    (collages / "52-old.jpg").write_bytes(b"stale")
    (collages / "53-other.jpg").write_bytes(b"keep")
    monkeypatch.setattr(cover, "covers_dir", lambda: collages)
    monkeypatch.setattr(cover, "custom_covers_dir", lambda: tmp_path / "custom")

    cover.save_custom_cover(52, cover.normalize_cover_image(_png(120, 80)))
    assert not (collages / "52-old.jpg").exists()
    assert (collages / "53-other.jpg").exists()  # 别的库的不许碰
