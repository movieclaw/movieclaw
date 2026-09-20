# -*- coding: utf-8 -*-
"""
Netflix 主题 PWA 图标 / 启动屏候选渲染器（临时脚本，选定后转正入库）。

思路：
- 爪标源文件 apps/web/public/movieclaw-logo-mark-curved.png（578px 透明底银色）；
- 按「亮度 → 颜色」分段插值把银色面重着色为品牌红 ramp / 炭黑 ramp，
  保留原稿的多面体明暗结构；
- 字标从 movieclaw-logo-v2.png 的文字区（x>=560）裁出，青色像素按
  「青度」掩膜平滑混向 Netflix 红，白色保持不变；
- 图标：纯色底 + 爪标可见宽度占画布 52%（与现版一致）；
- 启动屏：纯色底 + 爪标可见宽度占画布宽 23.3%、垂直居中（与现版一致）。
"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = r"G:\Github\movieclaw"
WEB = os.path.join(ROOT, "apps", "web", "public")
OUT = os.path.join(ROOT, ".zcode", "tmp", "pwa-netflix")

# —— Netflix 主题色板（globals.css html[data-theme="netflix"]）——
BLACK = (0, 0, 0)
SURFACE = (20, 20, 20)        # #141414
NETFLIX_RED = (229, 9, 20)    # #e50914
RED_DEEP = (178, 7, 16)       # #b20710

MARK = Image.open(os.path.join(WEB, "movieclaw-logo-mark-curved.png")).convert("RGBA")


def recolor_ramp(img, stops):
    """按像素亮度在 stops（(luma, (r,g,b)) 列表）间线性插值重着色，保留 alpha。"""
    arr = np.array(img).astype(np.float32)
    luma = arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114
    xs = [s[0] for s in stops]
    ch = [np.interp(luma, xs, [s[1][i] for s in stops]) for i in range(3)]
    out = np.stack(ch + [arr[..., 3]], axis=-1).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


# 红 ramp：暗面压到品牌暗红，最亮面给一点浅红高光，多面体结构才读得出来
RED_STOPS = [(0, (104, 3, 9)), (110, RED_DEEP), (190, NETFLIX_RED), (255, (255, 96, 102))]
# 炭黑 ramp：用于红底黑爪，暗面近黑、亮面深灰，红底上显出轮廓即可
DARK_STOPS = [(0, (14, 14, 14)), (255, (74, 74, 74))]

RED_MARK = recolor_ramp(MARK, RED_STOPS)
DARK_MARK = recolor_ramp(MARK, DARK_STOPS)
SILVER_MARK = MARK.copy()


def opaque_bbox(im):
    a = np.array(im)[:, :, 3]
    ys, xs = np.where(a > 10)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def paste_centered(canvas, mark, cx, cy, visible_w):
    """把 mark 缩放到「不透明区域宽度 = visible_w」后，以不透明区域中心对准 (cx, cy) 粘贴。"""
    x0, y0, x1, y1 = opaque_bbox(mark)
    s = visible_w / (x1 - x0)
    m2 = mark.resize((max(1, round(mark.width * s)), max(1, round(mark.height * s))), Image.LANCZOS)
    cx_in = (x0 + x1) / 2 * s
    cy_in = (y0 + y1) / 2 * s
    canvas.alpha_composite(m2, (round(cx - cx_in), round(cy - cy_in)))
    return canvas


def render_icon(mark, bg, size=512, ratio=0.52):
    canvas = Image.new("RGBA", (size, size), bg + (255,))
    paste_centered(canvas, mark, size / 2, size / 2, size * ratio)
    return canvas.convert("RGB")


def load_wordmark():
    """从 logo-v2 裁文字区，青色→Netflix 红，返回 RGBA。"""
    v2 = Image.open(os.path.join(WEB, "movieclaw-logo-v2.png")).convert("RGBA")
    text = v2.crop((560, 0, v2.width, v2.height))
    arr = np.array(text).astype(np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    m = np.clip(((g - r) + (b - r)) / 2 / 140.0, 0, 1)  # 青度掩膜
    rr = r * (1 - m) + NETFLIX_RED[0] * m
    gg = g * (1 - m) + NETFLIX_RED[1] * m
    bb = b * (1 - m) + NETFLIX_RED[2] * m
    out = np.stack([rr, gg, bb, arr[..., 3]], axis=-1).astype(np.uint8)
    wm = Image.fromarray(out, "RGBA")
    x0, y0, x1, y1 = opaque_bbox(wm)
    return wm.crop((x0, y0, x1, y1))


WORDMARK = load_wordmark()


def render_splash(mark, bg, w, h, with_wordmark=False):
    canvas = Image.new("RGBA", (w, h), bg + (255,))
    x0, y0, x1, y1 = opaque_bbox(mark)
    if with_wordmark:
        # 锁定版式：爪标中心略上移，字标宽度 = 爪标可见宽的 2.1 倍，居中在其下方
        mark_w = w * 0.20
        mark_cy = h * 0.452
        paste_centered(canvas, mark, w / 2, mark_cy, mark_w)
        wm_w = mark_w * 2.1
        s = wm_w / WORDMARK.width
        wm = WORDMARK.resize((round(WORDMARK.width * s), round(WORDMARK.height * s)), Image.LANCZOS)
        bottom = mark_cy + (y1 - y0) * s / 2
        canvas.alpha_composite(wm, (round(w / 2 - wm.width / 2), round(bottom + h * 0.035)))
    else:
        paste_centered(canvas, mark, w / 2, h * 0.4996, w * 0.2333)
    return canvas.convert("RGB")


# —— 候选定义 ——
ICONS = [
    ("A", "黑底红爪", RED_MARK, BLACK),
    ("B", "红底黑爪", DARK_MARK, NETFLIX_RED),
    ("C", "炭底红爪", RED_MARK, SURFACE),
    ("D", "红底银爪", SILVER_MARK, NETFLIX_RED),
]

SPLASHES = [
    ("S1", "纯黑红爪", RED_MARK, BLACK, False),
    ("S2", "炭面红爪", RED_MARK, SURFACE, False),
    ("S3", "纯黑红爪+字标", RED_MARK, BLACK, True),
    ("S4", "红底银爪", SILVER_MARK, NETFLIX_RED, False),
]

os.makedirs(OUT, exist_ok=True)
for key, name, mark, bg in ICONS:
    render_icon(mark, bg).save(os.path.join(OUT, f"icon-{key}.png"))

W, H = 1179, 2556  # 393x852@3 预览尺寸
for key, name, mark, bg, wm in SPLASHES:
    render_splash(mark, bg, W, H, wm).save(os.path.join(OUT, f"splash-{key}.png"))

# —— 预览拼图 ——
FONT = "C:\\Windows\\Fonts\\msyh.ttc"
FONT_BD = "C:\\Windows\\Fonts\\msyhbd.ttc"


def rounded(im, radius):
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius, fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


# 图标预览：模拟 iOS 主屏（深灰墙纸 + 圆角图标 + 标签）
cell, isize, pad = 360, 224, 48
pw = cell * len(ICONS) + pad * 2
ph = isize + 220
prev = Image.new("RGB", (pw, ph), (24, 24, 27))
draw = ImageDraw.Draw(prev)
draw.text((pad, 26), "主屏图标候选（iOS 圆角示意）", font=ImageFont.truetype(FONT_BD, 34), fill=(240, 240, 240))
for i, (key, name, mark, bg) in enumerate(ICONS):
    icon = rounded(render_icon(mark, bg, isize), round(isize * 0.224))
    x = pad + i * cell + (cell - isize) // 2
    y = 92
    prev.paste(icon, (x, y), icon)
    draw.text((pad + i * cell + cell // 2, y + isize + 26), f"{key} {name}",
              font=ImageFont.truetype(FONT, 30), fill=(210, 210, 210), anchor="ma")
prev.save(os.path.join(OUT, "preview-icons.png"))

# 启动屏预览：等比缩到手机框
ph_w, ph_h = 330, 715
gap, pad2 = 56, 48
pw2 = pad2 * 2 + (ph_w + gap) * len(SPLASHES) - gap
ph2 = ph_h + 190
prev2 = Image.new("RGB", (pw2, ph2), (24, 24, 27))
draw2 = ImageDraw.Draw(prev2)
draw2.text((pad2, 26), "启动屏候选（393×852@3 等比预览）", font=ImageFont.truetype(FONT_BD, 34), fill=(240, 240, 240))
for i, (key, name, mark, bg, wm) in enumerate(SPLASHES):
    sp = render_splash(mark, bg, W, H, wm).resize((ph_w - 8, round((ph_w - 8) * H / W)), Image.LANCZOS)
    x = pad2 + i * (ph_w + gap) + 4
    y = 92
    prev2.paste(rounded(sp, 36), (x, y))
    draw2.rectangle([x - 2, y - 2, x + sp.width + 1, y + sp.height + 1], outline=(70, 70, 74), width=2)
    draw2.text((x + sp.width // 2, y + sp.height + 24), f"{key} {name}",
               font=ImageFont.truetype(FONT, 28), fill=(210, 210, 210), anchor="ma")
prev2.save(os.path.join(OUT, "preview-splash.png"))

print("done ->", OUT)
