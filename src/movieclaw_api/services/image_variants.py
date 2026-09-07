"""图片派生缓存：把原图按受控预设压成可复用的小图。

原图仍是事实源（远程图由 ``ImageCache`` 缓存，本地刮削图在 metadata 目录）；
本模块只生成可随时删除重建的 WebP 派生物。派生结果继续写进同一个图片缓存，
因此与原图共用 singleflight、LRU 容量上限和 ``data/`` 持久化约定。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from movieclaw_api.exceptions import UpstreamServiceException
from movieclaw_api.services.image_cache import CachedImage, ImageCache, get_image_cache

logger = logging.getLogger("movieclaw_api.image_variants")

# 改编码参数时 bump：旧派生图留给 LRU 淘汰，新请求自动生成新版本。
_ENCODER_VERSION = "v2"


class ImageVariant(StrEnum):
    """允许从 HTTP 暴露的固定预设；拒绝任意宽高，避免制造无限缓存键。"""

    LANDSCAPE_CARD = "landscape-card"
    POSTER_CARD = "poster-card"
    # 图片库（docs/design/library-photo-kind.md 3.4）：相册墙的瓦片与灯箱的
    # 屏幕适配图。两者都是「装进盒子、不裁切」——照片的比例本身就是内容
    PHOTO_TILE = "photo-tile"
    PHOTO_SCREEN = "photo-screen"
    # 影视库 / 其他库的图床浏览模式（瀑布流墙）在宽松密度下的瓦片。相册墙同一档
    # 直接用原图是因为图片库的墙图本来就是 720 缩略图；图廊的源却是 TMDB w1280
    # 剧照与本地刮削原件，一张几百 KB 到几 MB，滑过去就是一片黑等着下载
    GALLERY_TILE = "gallery-tile"


@dataclass(frozen=True)
class VariantPreset:
    width: int
    height: int
    quality: int


_PRESETS = {
    # 预设的宽高是**外接框**而不是输出尺寸：派生图按原图比例等比缩到框内，
    # 绝不裁切。卡片框的比例由后端按真实尺寸给出（primary_aspect），派生图若
    # 在服务端先裁成固定比例，其他库里 16:9 的横版封面会被裁成 2:3 竖条、再被
    # 前端 object-cover 二次裁切，用户看到的只剩画面正中一小块。
    # 最近观看/分集横卡最大 240 CSS px，480px 足够覆盖常见 2x 屏。
    ImageVariant.LANDSCAPE_CARD: VariantPreset(width=480, height=270, quality=78),
    # 竖海报最大 164 CSS px，328px 覆盖 2x 屏；也供横卡缺背景时的海报兜底复用。
    ImageVariant.POSTER_CARD: VariantPreset(width=328, height=492, quality=80),
    # 相册墙瓦片：紧凑/标准密度列宽 ≤230 CSS px，480px 覆盖 2x 屏；宽松密度用 720 的原缩略图
    ImageVariant.PHOTO_TILE: VariantPreset(width=480, height=480, quality=78),
    # 灯箱屏幕适配图：长边 2048 覆盖 4K 以下全屏，几百 KB 而不是原图的几 MB；放大才拉原图
    ImageVariant.PHOTO_SCREEN: VariantPreset(width=2048, height=2048, quality=82),
    # 图廊宽松密度：列宽 340 CSS px，720px 覆盖 2x 屏
    ImageVariant.GALLERY_TILE: VariantPreset(width=720, height=720, quality=78),
}


def local_source_version(path: Path) -> str:
    """本地事实源的轻量版本指纹；不读整文件即可让原地换图自动失效。"""
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


class ImageVariantService:
    """按固定预设惰性生成 WebP；同一原图/版本/预设只编码一次。"""

    def __init__(self, cache: ImageCache, *, max_parallel: int = 2) -> None:
        self._cache = cache
        # 首页首次出现多张原图时限制 Pillow 并发，避免 NAS 瞬间吃满 CPU。
        self._slots = asyncio.Semaphore(max_parallel)

    async def get_or_create(
        self,
        source_path: Path,
        *,
        source_key: str,
        source_version: str,
        variant: ImageVariant,
    ) -> CachedImage:
        preset = _PRESETS[variant]
        cache_key = (
            f"image-variant:{_ENCODER_VERSION}:{variant.value}:"
            f"{source_key}:{source_version}"
        )

        async def produce() -> tuple[bytes, str]:
            async with self._slots:
                try:
                    data = await asyncio.to_thread(_render_webp, source_path, preset)
                except (OSError, ValueError, UnidentifiedImageError) as exc:
                    logger.warning("图片派生失败：%s（%s）", source_path, exc)
                    raise UpstreamServiceException("图片缩略图生成失败") from exc
                return data, "image/webp"

        return await self._cache.get_or_create(
            cache_key,
            produce,
            metadata={
                "source_key": source_key,
                "source_version": source_version,
                "variant": variant.value,
            },
        )


def _render_webp(source_path: Path, preset: VariantPreset) -> bytes:
    """同步解码、等比缩放到预设外接框内并编码；不裁切，小于目标的原图绝不放大。"""
    with Image.open(source_path) as opened:
        opened.seek(0)  # 动图只取首帧；卡片缩略图不承诺播放动画。
        image = ImageOps.exif_transpose(opened)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")

        # 所有预设同一口径：等比装进外接框、不裁切、不放大。照片与卡片曾各走一条
        # 分支（卡片按框比例裁切），卡片改为不裁切后两条分支已无差别，合成一条
        scale = min(
            1.0,
            preset.width / image.width,
            preset.height / image.height,
        )
        output_size = (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        )
        rendered = (
            image
            if output_size == image.size
            else image.resize(output_size, Image.Resampling.LANCZOS)
        )
        output = BytesIO()
        rendered.save(output, "WEBP", quality=preset.quality, method=4)
        return output.getvalue()


_service: ImageVariantService | None = None


def get_image_variant_service() -> ImageVariantService:
    global _service
    if _service is None:
        _service = ImageVariantService(get_image_cache())
    return _service


def reset_image_variant_service() -> None:
    """仅供测试：图片缓存实例重建后同步丢弃持有旧缓存的服务单例。"""
    global _service
    _service = None
