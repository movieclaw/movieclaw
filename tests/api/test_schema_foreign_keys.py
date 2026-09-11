"""CI 守卫：模型声明的外键必须在迁移后的真实库里同样存在。

模型（SQLModel）与迁移（alembic）是两份各自手写的真相。模型里写了
``ForeignKey(..., ondelete="SET NULL")`` 不代表数据库里真有这条约束——
``ingest_entry.media_item_id`` 就是加列迁移漏建了约束，作品删除后台账指向
被复用的 id、静默改挂到无关作品上，直到线上出事才发现。测试库若用
``create_all`` 建表会照模型把约束建出来，这类漂移永远测不到，所以这里必须
走真实迁移链再逐条比对。
"""

from __future__ import annotations

import sqlite3

from alembic import command
from sqlmodel import SQLModel

import movieclaw_db.models  # noqa: F401 -- 注册全部表到 SQLModel.metadata
from movieclaw_api.core.config import get_settings
from movieclaw_db.migrations import _build_config


def test_migrated_schema_has_every_model_foreign_key(tmp_path, monkeypatch) -> None:
    database = tmp_path / "fk-drift.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    command.upgrade(_build_config(), "head")

    drift: list[str] = []
    with sqlite3.connect(database) as connection:
        for table in SQLModel.metadata.sorted_tables:
            actual = {
                row[3]: (row[2], row[6].upper())
                for row in connection.execute(f"PRAGMA foreign_key_list('{table.name}')")
            }
            for fk in table.foreign_keys:
                expected = (fk.column.table.name, (fk.ondelete or "NO ACTION").upper())
                got = actual.get(fk.parent.name)
                if got != expected:
                    drift.append(
                        f"{table.name}.{fk.parent.name}: 模型 {expected}，迁移后 {got or '无约束'}"
                    )
    get_settings.cache_clear()
    assert drift == [], "模型与迁移的外键不一致，请补迁移：\n" + "\n".join(drift)
