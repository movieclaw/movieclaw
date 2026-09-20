# -*- coding: utf-8 -*-
"""
Netflix 主题 PWA 图标 / 启动屏生成器（成品版）。

用户选定方案：直接复用 apps/web/components/netflix/brand.tsx 的现有品牌资产——
- 主屏图标：纯黑底（#000，Netflix 主题画布色）+ M 字母标（MovieclawMark）；
- 启动屏：M 字母标 + MOVIECLAW 字标（MovieclawWordmark）居中锁定组合。

渲染管线：两份内联 SVG 用 Playwright/Chromium 按高分辨率栅格化（透明底），
PIL 合成到纯色画布。SVG 路径数据逐字取自 brand.tsx（2026-09 版），那边的
字形若再修订，这里的常量需同步。

构图常量（对齐旧版启动屏的测量值）：
- 旧版启动屏爪标可见宽 ≈ 短边 23.3%，垂直居中；
- 本版 M 宽取短边 16%（M 是实心块面，视觉重量大于镂空爪标，须收一档），
  字标宽 = 2.92 × M 宽（字标大写高 ≈ 0.32 × M 高，对齐 Netflix 官方
  「N + NETFLIX」锁定组合的比例感），组间空隙 = 0.15 × M 高，整体垂直居中。
- 图标 M 可见高 = 画布 62%（旧版爪标占 52%，M 主体更瘦，放宽到 62%）。
"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

ROOT = r"G:\Github\movieclaw"
WEB = os.path.join(ROOT, "apps", "web", "public")
OUT = os.path.join(ROOT, ".zcode", "tmp", "pwa-netflix")
REPO_SCRIPT_DIR = os.path.join(ROOT, "apps", "web", "scripts")

BLACK = (0, 0, 0)

# ———— SVG 源（逐字取自 components/netflix/brand.tsx）————

M_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 110 160" style="width:100%;height:100%;display:block">
<defs>
<linearGradient id="bar" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#87040d"/><stop offset="0.55" stop-color="#9c060f"/><stop offset="1" stop-color="#b1060f"/>
</linearGradient>
<linearGradient id="sl" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="-11.3" y2="3.9">
<stop offset="0" stop-color="#000" stop-opacity="0.35"/><stop offset="1" stop-color="#000" stop-opacity="0"/>
</linearGradient>
<linearGradient id="sr" gradientUnits="userSpaceOnUse" x1="110" y1="0" x2="121.3" y2="3.9">
<stop offset="0" stop-color="#000" stop-opacity="0.35"/><stop offset="1" stop-color="#000" stop-opacity="0"/>
</linearGradient>
</defs>
<rect width="30" height="160" fill="url(#bar)"/>
<rect x="80" width="30" height="160" fill="url(#bar)"/>
<path d="M0 0 30 86.2V122.6L0 36.5Z" fill="url(#sl)"/>
<path d="M110 0 80 86.2V122.6L110 36.5Z" fill="url(#sr)"/>
<path d="M0 0H44L55 47.8L66 0H110L55 158Z" fill="#e50914"/>
</svg>"""

WORDMARK_LETTERS = [
    (0, "M85.4 0V92.8Q74.1 93.7 62.7 94.6V30.7Q58.2 62.8 53.6 95.4Q45.5 96.1 37.5 96.7Q32.7 65.2 27.9 33.2V97.6Q16.6 98.6 5.2 99.5V0Q22.1 0 38.9 0Q40.3 8.7 42 20.4Q43.8 32.7 45.6 44.9Q48.5 22.3 51.5 0Z"),
    (96.7, "M64.6 51.2Q64.6 64.3 63.9 69.8Q63.2 75.3 59.5 80.1Q55.7 84.8 49.4 87.7Q43 90.6 34.5 91.2Q26.5 91.8 20.1 89.9Q13.7 87.9 9.8 83.5Q5.9 78.9 5.2 73.4Q4.4 67.7 4.4 53.6V37.9Q4.4 24.2 5.2 18.4Q5.9 12.6 9.6 7.8Q13.3 3.2 19.7 0.6Q26.1 -1.9 34.5 -1.9Q42.6 -1.9 48.9 0.4Q55.3 2.8 59.2 7.4Q63.1 12 63.9 17.3Q64.6 22.7 64.6 36.2ZM38.6 22.9Q38.6 16.6 37.8 15Q37.1 13.2 34.7 13.2Q32.6 13.2 31.5 14.7Q30.5 16.1 30.5 23V64.9Q30.5 72.7 31.2 74.4Q31.9 76.2 34.5 76.1Q37.1 75.9 37.9 73.8Q38.6 71.7 38.6 63.7Z"),
    (171.8, "M67 0Q60.4 41.8 53.7 84Q34 84.8 14.3 85.9Q6.8 43.4 -0.7 0Q13 0 26.7 0Q31.4 35.1 33.5 59.3Q35.5 34.8 37.7 15.8Q38.6 7.9 39.5 0Z"),
    (244, "M31.2 0V82.5Q18.2 82.8 5.2 83.2V0Z"),
    (286.4, "M5.2 0Q26.9 0 48.5 0V16.4Q39.9 16.4 31.2 16.4V32Q39.3 32 47.4 32V47.6Q39.3 47.6 31.2 47.6V65.6Q40.8 65.6 50.3 65.6V82Q27.8 81.9 5.2 82.2Z"),
    (345, "M65.9 36.4Q52.9 36.2 39.9 36.1V21.7Q39.9 15.4 39.1 13.8Q38.2 12.2 35.4 12.2Q32.2 12.2 31.3 14.1Q30.5 16 30.5 22.3V60.6Q30.5 66.6 31.3 68.5Q32.2 70.4 35.2 70.4Q38.1 70.5 39 68.7Q39.9 66.8 39.9 60V49.7Q52.9 49.9 65.9 50.1V53.4Q65.9 66.4 63.7 71.7Q61.5 77 54 80.8Q46.5 84.7 35.5 84.4Q24.1 84.2 16.7 80.7Q9.3 77.1 6.9 71.1Q4.4 65.1 4.4 53V29Q4.4 20.1 5.2 15.7Q5.9 11.3 9.6 7.1Q13.3 3 19.8 0.7Q26.3 -1.7 34.8 -1.7Q46.3 -1.7 53.7 1.9Q61.2 5.7 63.6 11.2Q65.9 16.8 65.9 28.5Z"),
    (421, "M31.2 0V68.3Q39.1 68.6 47 69V86.2Q26.1 85 5.2 84.2V0Z"),
    (475.1, "M50.1 0Q57.5 44.9 65 91Q51.7 90 38.4 89.1Q37.8 81 37.1 73Q32.4 72.7 27.7 72.4Q26.9 80.3 26.2 88.2Q12.8 87.3 -0.7 86.6Q5.9 43.7 12.5 0ZM36.3 57.2Q34.3 42 32.4 19.9Q28.4 45.1 27.4 56.8Z"),
    (545.3, "M103 0Q97.2 50 91.5 99Q75.2 97.6 59 96.2Q54.5 73.8 51.1 45.5Q49.6 57.4 43.9 94.9Q27.8 93.6 11.6 92.3Q5.8 45.7 0 0Q12.7 0 25.3 0Q26.6 16.3 27.9 32.7Q29.2 48.5 30.6 64.4Q32.1 39.8 37.8 0Q51.4 0 64.9 0Q65.4 4.3 67.7 32Q69.1 50.4 70.5 69Q72.7 33.8 77.8 0Z"),
]

paths = "".join(f'<path d="{d}" transform="translate({x} 0)"/>' for x, d in WORDMARK_LETTERS)
WORDMARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1.5 -3 651.3 104" fill="#e50914" '
    'style="width:100%;height:100%;display:block">' + paths + "</svg>"
)


def rasterize():
    """Playwright 把两份 SVG 按高分辨率栅格化为透明底 PNG（先粗排放大再缩，LANCZOS 收边）。"""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 7000, "height": 2200})
        page.set_content(
            "<!doctype html><html><body style='margin:0;background:transparent'>"
            f"<div id='m' style='width:1100px;height:1600px'>{M_SVG}</div>"
            f"<div id='wm' style='width:6513px;height:1040px'>{WORDMARK_SVG}</div>"
            "</body></html>"
        )
        page.wait_for_timeout(200)
        m = page.locator("#m").screenshot(omit_background=True)
        wm = page.locator("#wm").screenshot(omit_background=True)
        b.close()
    open(os.path.join(OUT, "mark-m.png"), "wb").write(m)
    open(os.path.join(OUT, "wordmark.png"), "wb").write(wm)
    return Image.open(os.path.join(OUT, "mark-m.png")).convert("RGBA"), Image.open(
        os.path.join(OUT, "wordmark.png")
    ).convert("RGBA")


MARK, WORDMARK = rasterize()


def paste_scaled(canvas, im, cx, cy, target_h):
    s = target_h / im.height
    im2 = im.resize((max(1, round(im.width * s)), max(1, round(target_h))), Image.LANCZOS)
    canvas.alpha_composite(im2, (round(cx - im2.width / 2), round(cy - im2.height / 2)))


def render_icon(size):
    canvas = Image.new("RGBA", (size, size), BLACK + (255,))
    paste_scaled(canvas, MARK, size / 2, size / 2, size * 0.62)
    return canvas.convert("RGB")


def render_splash(w, h):
    canvas = Image.new("RGBA", (w, h), BLACK + (255,))
    shorter = min(w, h)
    m_h = shorter * 0.16 * (160 / 110)
    wm_h = m_h * 0.32
    gap = m_h * 0.15
    total = m_h + gap + wm_h
    top = h * 0.5 - total / 2
    paste_scaled(canvas, MARK, w / 2, top + m_h / 2, m_h)
    paste_scaled(canvas, WORDMARK, w / 2, top + m_h + gap + wm_h / 2, wm_h)
    return canvas.convert("RGB")


# —— 设备清单（与 lib/apple-splash.ts 两张表保持一致）——
IPHONES = [(375, 667, 2), (414, 896, 2), (375, 812, 3), (414, 896, 3), (390, 844, 3),
           (393, 852, 3), (402, 874, 3), (420, 912, 3), (428, 926, 3), (430, 932, 3), (440, 956, 3)]
IPADS = [(744, 1133, 2), (768, 1024, 2), (810, 1080, 2), (820, 1180, 2),
         (834, 1194, 2), (834, 1210, 2), (1024, 1366, 2), (1032, 1376, 2)]

os.makedirs(os.path.join(WEB, "splash"), exist_ok=True)
os.makedirs(os.path.join(WEB, "icons"), exist_ok=True)

for size, name in [(512, "icon-512.png"), (192, "icon-192.png"), (180, "../apple-touch-icon.png")]:
    render_icon(size).save(os.path.join(WEB, "icons", name), optimize=True)

for w, h, r in IPHONES:
    render_splash(w * r, h * r).save(os.path.join(WEB, "splash", f"splash-{w}x{h}@{r}.png"), optimize=True)
for w, h, r in IPADS:
    render_splash(w * r, h * r).save(os.path.join(WEB, "splash", f"splash-{w}x{h}@{r}.png"), optimize=True)
    render_splash(h * r, w * r).save(os.path.join(WEB, "splash", f"splash-{w}x{h}@{r}-land.png"), optimize=True)

# —— 预览拼图 ——
FONT = "C:\\Windows\\Fonts\\msyh.ttc"
FONT_BD = "C:\\Windows\\Fonts\\msyhbd.ttc"


def rounded(im, radius):
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius, fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


# 图标预览：大图 + 实际主屏尺寸示意
prev = Image.new("RGB", (1180, 560), (24, 24, 27))
d = ImageDraw.Draw(prev)
d.text((48, 26), "最终成品：黑底 M 字母标（Netflix 主题同款）", font=ImageFont.truetype(FONT_BD, 34), fill=(240, 240, 240))
big = rounded(render_icon(448), 100)
prev.paste(big, (64, 92), big)
d.text((64 + 224, 92 + 448 + 20), "512", font=ImageFont.truetype(FONT, 26), fill=(200, 200, 200), anchor="ma")
for i, s in enumerate([120, 90, 60]):
    ic = rounded(render_icon(512).resize((s, s), Image.LANCZOS), round(s * 0.224))
    x = 620 + (0 if i == 0 else (i - 1) * 200 + 110 - s // 1)
    prev.paste(ic, (620 + i * 190, 160), ic)
    d.text((620 + i * 190 + s // 2, 160 + s + 16), f"~{s}px 档", font=ImageFont.truetype(FONT, 24), fill=(200, 200, 200), anchor="ma")
prev.save(os.path.join(OUT, "preview-final-icons.png"))

# 启动屏预览：竖屏 + 横屏
pw, ph = 330, 715
sp = render_splash(1179, 2556).resize((pw - 8, round((pw - 8) * 2556 / 1179)), Image.LANCZOS)
land = render_splash(2732, 2048).resize((620, round(620 * 2048 / 2732)), Image.LANCZOS)
prev2 = Image.new("RGB", (pw + 620 + 200, ph + 160), (24, 24, 27))
d2 = ImageDraw.Draw(prev2)
d2.text((48, 26), "最终成品启动屏（M + MOVIECLAW 锁定组合）", font=ImageFont.truetype(FONT_BD, 34), fill=(240, 240, 240))
prev2.paste(rounded(sp, 36), (48, 92))
d2.rectangle([46, 90, 48 + sp.width + 1, 92 + sp.height + 1], outline=(70, 70, 74), width=2)
lx = 48 + pw + 56
prev2.paste(rounded(land, 24), (lx, 92 + (sp.height - land.height) // 2))
d2.rectangle([lx - 2, 90 + (sp.height - land.height) // 2, lx + land.width + 1, 92 + (sp.height - land.height) // 2 + land.height + 1], outline=(70, 70, 74), width=2)
prev2.save(os.path.join(OUT, "preview-final-splash.png"))

print("done")
