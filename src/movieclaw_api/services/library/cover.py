"""库封面拼贴——服务端渲染的「氛围光货架」（双端共用）。

复刻控制台 LibraryCover 组件（apps/web/components/library-view.tsx）的构图，
让 Jellyfin 兼容层（播放器库卡片）与控制台媒体库页显示**同一张**真实图片：

- 画布 21:10；氛围光 = 首张海报放大重模糊提饱和铺满，再压暗保证前景对比；
- 至多 4 张海报（各占画布宽 22.5%，2:3 竖版，圆角 + 白描边 + 落影）立排；
- 每张海报下方带向下渐隐的倒影；底部一枚中性地面光斑像射灯打在舞台上。

素材选择：库内**最近入库**且有本地海报资产的 4 部作品（与控制台货架同一
口径）。产物落 data/metadata/library-covers/{库id}-{key}.jpg，key 由海报
路径+mtime 派生——库内容变化自动重渲，旧文件顺手清理。渲染是 CPU 活，
统一走 asyncio.to_thread，不堵事件循环。

用户上传的**自定义封面**优先于拼贴（issue #427）：本模块是三端封面的唯一
入口（控制台卡片、管理页缩略图、Jellyfin 库 Primary 图），自定义封面在
``ensure_library_cover`` 的最前面短路，下游一处都不用改。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile, MediaMetadata

logger = logging.getLogger("movieclaw_api.library_cover")

# 画布：控制台卡片是 21/10 比例；1260x600 对 2x 屏的卡片宽度绰绰有余
CANVAS_W, CANVAS_H = 1260, 600
POSTER_W_RATIO = 0.225  # 单张海报占画布宽
POSTER_GAP_RATIO = 0.02
POSTER_TOP_RATIO = 0.045
MAX_POSTERS = 4

# 同一库的封面选择、素材指纹计算和 Pillow 渲染必须作为一个整体去重。根级
# Items 与库图片接口会并发调用本服务；仅锁渲染仍会让每个请求重复扫描候选素材。
_cover_tasks: dict[int, asyncio.Task[tuple[Path, str] | None]] = {}


# ---------------------------------------------------------------------------
# 自定义封面（用户上传）
#
# 存 **uploads** 而不是 metadata：metadata/library-covers 在存储登记里是可清理
# 的缓存（拼贴随时能重渲），用户自己做的图清掉就没了。uploads 是不可清理的
# 用户数据组，语义才对（services/storage/registry.py 的 uploads 条目已覆盖此
# 子目录，登记项不得嵌套，故不新增条目）。
#
# 一库一槽位：data/uploads/library-covers/{库id}.jpg。"有没有自定义封面" =
# 文件在不在，不加数据库列、不做迁移（与首页背景图同一套思路）。
# ---------------------------------------------------------------------------

# 收前硬闸：与首页背景图一致。前端会先压一道，这里是防滥用的下限
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# 解压炸弹防线：10MB 的 PNG 能解出几个 GB 的位图。先看 size 再解码，
# Image.open 只读文件头、不触碰像素，这一步零成本
MAX_UPLOAD_PIXELS = 50_000_000
# 长边上限：卡片在 2 倍屏上也就 800 逻辑像素宽，拼贴本体才 1260 宽，
# 1600 已经绰绰有余；再大只是白占磁盘和带宽
CUSTOM_MAX_EDGE = 1600
# JPEG 而非 WebP：Jellyfin 图片路由本就按 image/jpeg 输出，第三方播放器
# （VidHub / Infuse / Emby 系）对 WebP 支持参差，省下的两成体积换不来
# "封面不显示"这类难排查的报障
CUSTOM_JPEG_QUALITY = 85
# 透明图压到 JPEG 必须先填底；用货架背景同色，观感连续
_FLATTEN_BG = (8, 10, 16)


def custom_covers_dir() -> Path:
    return Path(get_settings().media_dir) / "library-covers"


def custom_cover_path(library_id: int) -> Path:
    return custom_covers_dir() / f"{library_id}.jpg"


def has_custom_cover(library_id: int) -> bool:
    """该库是否设了自定义封面（库视图据此决定按钮形态与空库要不要出图）。"""
    return custom_cover_path(library_id).is_file()


def _custom_cover_entry(library_id: int) -> tuple[Path, str] | None:
    """自定义封面的 (路径, 版本 key)；没有则 None。

    key 与拼贴同形（32 位 hex）——Jellyfin 客户端拿它当 ImageTags.Primary，
    换个形状不值得赌各家实现的宽容度。内容变了 key 就变，缓存自动失效。
    """
    path = custom_cover_path(library_id)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    return path, hashlib.md5(f"custom:{path}:{mtime}".encode()).hexdigest()


def normalize_cover_image(data: bytes) -> bytes:
    """把用户上传的任意图片归一化成一张「小而够用」的 JPEG 封面。

    纯 CPU 同步函数，调用方负责丢 asyncio.to_thread。手机原图/4K 截图
    通常 5~10MB，过一遍这里落到 100~300KB 量级（实测降 97% 以上）。

    做了这些事（顺序有讲究）：
    1. **真解码**，不信客户端报的 Content-Type——MIME 是上传方说了算的，
       顺带把可内嵌脚本的 SVG 挡在门外（Pillow 根本不认它）；
    2. 像素数超限直接拒，防解压炸弹；
    3. 按 EXIF 摆正，否则手机横拍的封面是躺着的；
    4. 长边缩到 1600（只缩不放，LANCZOS）；
    5. 透明通道合到深色底上（JPEG 没有 alpha）；
    6. 重编码为渐进式 JPEG——顺带把 EXIF（含 GPS）一起丢掉。

    **不裁剪**：各消费方本来就 object-cover 自己裁，用户精心做的图不该被
    我们先切一刀。

    校验失败抛 ``ValueError``，消息是给非开发者看的中文，路由层直接转 400。
    """
    from PIL import Image, ImageOps

    try:
        img = Image.open(BytesIO(data))
    except Exception as exc:  # Pillow 对坏文件抛的异常类型不止一种
        raise ValueError("无法识别这个文件，请上传 JPG / PNG / WebP 等常见格式的图片") from exc

    width, height = img.size
    if width * height > MAX_UPLOAD_PIXELS:
        # 上限跟着常量走，别把数字写死在文案里——改了常量提示就撒谎了
        raise ValueError(
            f"图片尺寸过大（{width}×{height}），"
            f"请先缩小到 {MAX_UPLOAD_PIXELS // 10_000} 万像素以内再上传"
        )

    try:
        img = ImageOps.exif_transpose(img) or img
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            base = Image.new("RGBA", rgba.size, (*_FLATTEN_BG, 255))
            img = Image.alpha_composite(base, rgba).convert("RGB")
        else:
            img = img.convert("RGB")
        img.thumbnail((CUSTOM_MAX_EDGE, CUSTOM_MAX_EDGE), Image.LANCZOS)
        buffer = BytesIO()
        img.save(
            buffer,
            "JPEG",
            quality=CUSTOM_JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
    except Exception as exc:
        raise ValueError("这张图片处理失败，可能已损坏或格式不受支持，请换一张试试") from exc

    out = buffer.getvalue()
    logger.info(
        "自定义封面已压缩：%d×%d %d 字节 → %d×%d %d 字节",
        width,
        height,
        len(data),
        img.width,
        img.height,
        len(out),
    )
    return out


def _drop_collage(library_id: int) -> None:
    """清掉该库的拼贴产物——设了自定义封面就再没人读它了，留着白占磁盘。"""
    out_dir = covers_dir()
    if not out_dir.is_dir():
        return
    for stale in out_dir.glob(f"{library_id}-*.jpg"):
        stale.unlink(missing_ok=True)


def save_custom_cover(library_id: int, image: bytes) -> str:
    """把**已归一化**的封面落盘并返回新的版本 key（调用方拿去打缓存）。

    先写临时文件再 ``os.replace`` 原子换上：换图的同时可能正有请求在读这张图，
    直接覆写会露出截断的半张；中途崩溃也会留下一张永久损坏的封面。
    """
    target = custom_cover_path(library_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(f".{uuid4().hex}.tmp")
    try:
        staging.write_bytes(image)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    _drop_collage(library_id)
    entry = _custom_cover_entry(library_id)
    logger.info("媒体库 #%d 已设置自定义封面：%s（%d 字节）", library_id, target, len(image))
    # 刚写完必然存在；真被并发删了就退回一个稳定占位，调用方只拿它打缓存
    return entry[1] if entry else ""


def remove_custom_cover(library_id: int) -> bool:
    """删除自定义封面，回落到自动拼贴；返回是否确有文件被删。"""
    target = custom_cover_path(library_id)
    if not target.is_file():
        return False
    target.unlink(missing_ok=True)
    logger.info("媒体库 #%d 的自定义封面已删除，封面回落到自动拼贴", library_id)
    return True


def covers_dir() -> Path:
    return Path(get_settings().metadata_dir) / "library-covers"


def _assets_root() -> Path:
    from movieclaw_api.services.media_scrape import assets_root

    return Path(assets_root())


async def select_cover_posters(library_id: int) -> list[Path]:
    """选出该库最近入库、有本地海报资产的至多 4 部作品的海报绝对路径。"""
    root = _assets_root()
    async with get_database().session() as session:
        # 只需要海报路径与入库时间：整行读取会反序列化每部作品的简介、演员等
        # 大字段；VidHub 的根级 Items 每次启动都会走这里，大库上代价不可接受。
        rows = (
            await session.execute(
                select(
                    MediaMetadata.poster_file,
                    func.max(LibraryFile.created_at).label("latest_created_at"),
                )
                .join(
                    LibraryFile,
                    LibraryFile.media_item_id == MediaMetadata.media_item_id,
                )
                .where(
                    LibraryFile.library_id == library_id,
                    LibraryFile.in_place(),
                    MediaMetadata.poster_file.is_not(None),
                )
                .group_by(MediaMetadata.media_item_id, MediaMetadata.poster_file)
                .order_by(func.max(LibraryFile.created_at).desc())
            )
        ).all()
    result: list[Path] = []
    for rel, _created_at in rows:
        path = root / rel
        if path.is_file():
            result.append(path)
        if len(result) >= MAX_POSTERS:
            break
    return result


def _cover_key(paths: list[Path]) -> str:
    hasher = hashlib.md5()
    for p in paths:
        try:
            hasher.update(f"{p}:{p.stat().st_mtime_ns};".encode())
        except OSError:
            hasher.update(f"{p}:gone;".encode())
    return hasher.hexdigest()


def _clear_cover_task(
    library_id: int, task: asyncio.Task[tuple[Path, str] | None]
) -> None:
    """仅移除当前任务，避免完成回调误删后续同库任务。"""
    if _cover_tasks.get(library_id) is task:
        _cover_tasks.pop(library_id, None)


async def ensure_library_cover(library_id: int) -> tuple[Path, str] | None:
    """返回该库封面的 (文件路径, 版本 key)；没有可用封面返回 None。

    **自定义封面优先**：用户上传过就直接给他的图，一次 stat 的成本，连拼贴
    的候选素材都不用扫（issue #427）。

    没有自定义封面才走拼贴，幂等：key 命中直接返回；素材变化重渲并清理该库
    旧产物。同一库的并发调用复用同一任务，避免重复扫描海报或重复执行 Pillow
    渲染。
    """
    custom = _custom_cover_entry(library_id)
    if custom is not None:
        return custom
    task = _cover_tasks.get(library_id)
    if task is None:
        task = asyncio.create_task(_ensure_library_cover_once(library_id))
        _cover_tasks[library_id] = task
        task.add_done_callback(lambda done: _clear_cover_task(library_id, done))
    # 单个 HTTP 请求断开时，不应取消其他请求正在等待的共享封面生成。
    return await asyncio.shield(task)


async def _ensure_library_cover_once(library_id: int) -> tuple[Path, str] | None:
    """执行一次完整的封面选择、缓存检查和渲染流程。"""
    posters = await select_cover_posters(library_id)
    if not posters:
        return None
    key = _cover_key(posters)
    out_dir = covers_dir()
    target = out_dir / f"{library_id}-{key}.jpg"
    if target.is_file():
        return target, key
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(render_shelf_collage, posters, target)
    except Exception:
        logger.exception("库封面拼贴渲染失败（library_id=%d），本次退化为无封面", library_id)
        return None
    for stale in out_dir.glob(f"{library_id}-*.jpg"):
        if stale != target:
            stale.unlink(missing_ok=True)
    return target, key


def render_shelf_collage(poster_paths: list[Path], out: Path) -> None:
    """Pillow 渲染「氛围光货架」。纯同步，调用方负责丢线程池。"""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    def load_poster(path: Path, width: int) -> Image.Image:
        img = Image.open(path).convert("RGB")
        height = round(width * 3 / 2)
        # cover 语义裁剪到 2:3
        scale = max(width / img.width, height / img.height)
        img = img.resize((round(img.width * scale), round(img.height * scale)))
        left = (img.width - width) // 2
        top = (img.height - height) // 2
        return img.crop((left, top, left + width, top + height))

    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), "#080a10")

    # ---- 氛围光底：首图 cover 铺满 → 放大 1.5x → 重模糊 → 提饱和 → 压暗 ----
    first = Image.open(poster_paths[0]).convert("RGB")
    scale = max(CANVAS_W / first.width, CANVAS_H / first.height) * 1.5
    ambient = first.resize((round(first.width * scale), round(first.height * scale)))
    left = (ambient.width - CANVAS_W) // 2
    top = (ambient.height - CANVAS_H) // 2
    ambient = ambient.crop((left, top, left + CANVAS_W, top + CANVAS_H))
    ambient = ambient.filter(ImageFilter.GaussianBlur(56))
    ambient = ImageEnhance.Color(ambient).enhance(1.5)
    canvas = Image.blend(canvas, ambient, 0.7)  # 首图 opacity 0.7 落在深底上
    dark = Image.new("RGB", canvas.size, "#080a10")
    canvas = Image.blend(canvas, dark, 0.5)  # bg-[#080a10]/50 压暗

    # ---- 灯箱底光：首图模糊自底向上 screen 发光（颜色天然取自海报主色）----
    glow_h = CANVAS_H // 2
    glow = ambient.resize((CANVAS_W, glow_h)).filter(ImageFilter.GaussianBlur(40))
    glow = ImageEnhance.Color(glow).enhance(1.4)
    from PIL import ImageChops

    region = canvas.crop((0, CANVAS_H - glow_h, CANVAS_W, CANVAS_H))
    screened = ImageChops.screen(region, glow)
    # 自底向上的线性渐隐（底边最亮 0.55 → 顶部 0）
    mask = Image.new("L", (CANVAS_W, glow_h))
    mask_draw = ImageDraw.Draw(mask)
    for y in range(glow_h):
        mask_draw.line(
            [(0, y), (CANVAS_W, y)], fill=round(255 * 0.55 * (y / glow_h))
        )
    region.paste(screened, (0, 0), mask)
    canvas.paste(region, (0, CANVAS_H - glow_h))

    # ---- 中性地面光斑：射灯打在舞台地面上 ----
    spot = Image.new("L", (CANVAS_W, CANVAS_H), 0)
    spot_draw = ImageDraw.Draw(spot)
    spot_draw.ellipse(
        (
            round(CANVAS_W * 0.2),
            round(CANVAS_H * 0.78),
            round(CANVAS_W * 0.8),
            round(CANVAS_H * 1.3),
        ),
        fill=round(255 * 0.09),
    )
    spot = spot.filter(ImageFilter.GaussianBlur(50))
    white = Image.new("RGB", canvas.size, "white")
    canvas.paste(white, (0, 0), spot)

    # ---- 海报排：圆角 + 白描边 + 落影 + 倒影 ----
    count = min(len(poster_paths), MAX_POSTERS)
    poster_w = round(CANVAS_W * POSTER_W_RATIO)
    poster_h = round(poster_w * 3 / 2)
    gap = round(CANVAS_W * POSTER_GAP_RATIO)
    row_w = count * poster_w + (count - 1) * gap
    x = (CANVAS_W - row_w) // 2
    y = round(CANVAS_H * POSTER_TOP_RATIO)
    radius = 6

    rounded_mask = Image.new("L", (poster_w, poster_h), 0)
    ImageDraw.Draw(rounded_mask).rounded_rectangle(
        (0, 0, poster_w - 1, poster_h - 1), radius=radius, fill=255
    )

    canvas_rgba = canvas.convert("RGBA")
    for i in range(count):
        poster = load_poster(poster_paths[i], poster_w)
        px = x + i * (poster_w + gap)

        # 落影：黑色圆角矩形下移 6px、重模糊
        shadow = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
        shadow_tile = Image.new("RGBA", (poster_w, poster_h), (0, 0, 0, 128))
        shadow.paste(shadow_tile, (px, y + 8), rounded_mask)
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        canvas_rgba = Image.alpha_composite(canvas_rgba, shadow)

        # 海报本体（圆角）+ 1px 白描边
        tile = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
        tile.paste(poster, (px, y), rounded_mask)
        ImageDraw.Draw(tile).rounded_rectangle(
            (px, y, px + poster_w - 1, y + poster_h - 1),
            radius=radius,
            outline=(255, 255, 255, 51),
            width=1,
        )
        canvas_rgba = Image.alpha_composite(canvas_rgba, tile)

        # 倒影：翻转副本贴底边，轻模糊，向下快速渐隐（近处最实 0.4 → 26% 处消失）
        flipped = poster.transpose(Image.FLIP_TOP_BOTTOM).filter(
            ImageFilter.GaussianBlur(1)
        )
        refl_h = poster_h
        fade = Image.new("L", (poster_w, refl_h), 0)
        fade_draw = ImageDraw.Draw(fade)
        fade_span = round(refl_h * 0.26)
        for ry in range(fade_span):
            alpha = round(255 * 0.4 * (1 - ry / fade_span))
            fade_draw.line([(0, ry), (poster_w, ry)], fill=alpha)
        refl_mask = Image.new("L", (poster_w, refl_h), 0)
        refl_mask.paste(fade, (0, 0), rounded_mask)
        refl = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
        refl.paste(flipped, (px, y + poster_h + 2), refl_mask)
        canvas_rgba = Image.alpha_composite(canvas_rgba, refl)

    canvas_rgba.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
