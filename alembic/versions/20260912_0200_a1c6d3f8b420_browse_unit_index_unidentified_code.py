"""ix_library_file_browse_unit 末尾追加 unidentified_code

海报墙的排序键查询是单库浏览里最贵的一条 SQL::

    SELECT DISTINCT library_file.media_item_id, media_item.title
    FROM library_file JOIN media_item ON media_item.id = library_file.media_item_id
    WHERE library_file.library_id = ?
      AND library_file.media_item_id IS NOT NULL
      AND library_file.state != 'trashed'
      AND library_file.unidentified_code IS NULL

它本该走 ``ix_library_file_browse_unit`` 的覆盖扫描，却只差 ``unidentified_code``
这一列不在索引里——为了判这一个条件就得逐行回表，于是 planner 干脆改挑
``ix_library_file_library_size``，把覆盖扫描的优势整个丢掉。

把这一列追加到**索引末尾**：既有前缀一个都没动，原本靠这棵索引的查询
（Jellyfin Latest、海报墙聚合）该怎么走还怎么走；代价只是索引宽一点点。

实测（79 个库 / 3.7 万条目 / 7 万文件 / 148MB 的合成库，scripts/perf/
seed_library_dataset.py + bench_disk_io.py）：

    排序键查询   19.2ms → 7.9ms   （改走 COVERING INDEX）
    海报墙聚合   16.5ms → 13.0ms  （顺带也改走了这棵索引）
    库体积       148.2 → 148.3 MiB

向前兼容（CLAUDE.md 硬约束 3）：纯索引重建，不动任何数据。旧版本回退后
索引多一列它不认识，SQLite 照常使用（索引对查询是透明的加速结构），
downgrade 也原样建回旧定义。

Revision ID: a1c6d3f8b420
Revises: e4a7c2b9d165
Create Date: 2026-09-12 02:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a1c6d3f8b420"
down_revision: str | None = "e4a7c2b9d165"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_library_file_browse_unit"
_BASE_COLUMNS = [
    "library_id",
    "state",
    "media_item_id",
    "season_number",
    "episode_number",
    "created_at",
]


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="library_file")
    op.create_index(
        _INDEX, "library_file", [*_BASE_COLUMNS, "unidentified_code"], unique=False
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="library_file")
    op.create_index(_INDEX, "library_file", _BASE_COLUMNS, unique=False)
