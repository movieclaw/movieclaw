#!/usr/bin/env python3
"""为压测数据集补齐**真实存在**的本地海报文件。

为什么需要：只灌数据库、不落图片文件，浏览器端到端测出来的是一屏 404，
图片解码与网络占用全部缺席——那不是用户真实感受到的页面。这里给每个
条目的 ``media_metadata.poster_file`` 落一个真实 JPEG（w500 尺寸、量级
与 TMDB 海报相当），让前端跑在真实的图片负载下。

实现取巧但不失真：先渲染 N 张互不相同的底图，再**硬链接**到几万个目标
路径。磁盘只占 N 张的体积，而 HTTP 层每个海报仍是各自的 URL、各自的
一次请求与一次解码——浏览器按 URL 缓存，链接复用不会作弊掉网络开销。

用法::

    python scripts/perf/seed_poster_assets.py --db data/movieclaw.db
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw

DISTINCT = 400          # 互不相同的底图张数
SIZE = (500, 750)       # TMDB w500 海报的实际尺寸


def _make_sources(pool_dir: Path) -> list[Path]:
    """渲染 DISTINCT 张底图：竖向渐变 + 噪点色块，JPEG 压出来 40~90 KB，
    与真实海报同量级（纯色图会压到几 KB，测不出解码与传输成本）。"""
    pool_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    sources: list[Path] = []
    for index in range(DISTINCT):
        path = pool_dir / f"src{index:04d}.jpg"
        if not path.exists():
            base = Image.new("RGB", SIZE)
            draw = ImageDraw.Draw(base)
            top = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
            bottom = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
            for y in range(SIZE[1]):
                ratio = y / SIZE[1]
                draw.line(
                    [(0, y), (SIZE[0], y)],
                    fill=tuple(
                        int(top[c] * (1 - ratio) + bottom[c] * ratio) for c in range(3)
                    ),
                )
            for _ in range(900):
                x, y = rng.randint(0, 499), rng.randint(0, 749)
                draw.rectangle(
                    [x, y, x + rng.randint(2, 24), y + rng.randint(2, 24)],
                    fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)),
                )
            base.save(path, "JPEG", quality=82)
        sources.append(path)
    return sources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/movieclaw.db")
    parser.add_argument("--assets", default="data/metadata/images")
    args = parser.parse_args()

    assets = Path(args.assets)
    sources = _make_sources(assets / "_perfpool")
    print(f"底图就绪：{len(sources)} 张，"
          f"平均 {sum(p.stat().st_size for p in sources) / len(sources) / 1024:.0f} KB")

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT media_item_id, poster_file FROM media_metadata "
        "WHERE poster_file IS NOT NULL"
    ).fetchall()
    linked = 0
    for item_id, rel in rows:
        target = assets / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sources[item_id % len(sources)]
        try:
            os.link(source, target)     # 硬链接：不占额外磁盘
        except OSError:
            target.write_bytes(source.read_bytes())
        linked += 1
    print(f"已为 {linked:,} 个条目落地本地海报（共 {len(rows):,} 条有资产记录）")

    # 没有本地资产的条目回落 TMDB 图床——压测环境不该真的出网，
    # 统一改成本地资产，把「图片子系统」这一变量从测量里择出去
    conn.execute(
        "UPDATE media_metadata SET poster_file = 'perf/' || media_item_id || '.jpg' "
        "WHERE poster_file IS NULL"
    )
    conn.commit()
    rest = conn.execute(
        "SELECT media_item_id, poster_file FROM media_metadata "
        "WHERE poster_file LIKE 'perf/%'"
    ).fetchall()
    for item_id, rel in rest:
        target = assets / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sources[item_id % len(sources)]
        try:
            os.link(source, target)
        except OSError:
            target.write_bytes(source.read_bytes())
    print(f"补齐回落条目 {len(rest):,} 个，全部条目现在都有本地海报")
    conn.close()


if __name__ == "__main__":
    main()
