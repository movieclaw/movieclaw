"""连接级 PRAGMA 的守护（movieclaw_db.engine._configure_sqlite_pragmas）。

这几项每条都在为一个具体问题服务，改动前先看清代价：

- ``journal_mode=WAL`` / ``busy_timeout`` / ``foreign_keys`` / ``synchronous``
  是既有约定，见函数注释；
- ``cache_size`` 是「空间换 IO」：SQLite 自带 2MB 默认值在大库上会让海报墙、
  筛选栏把同一批页反复读回来（实测 158MB 的库，筛选栏一次请求 122443 次
  ``pread``）。这里钉住「默认给到 32MB」与「设 0 能退回 SQLite 默认」两件事，
  免得哪天被顺手删掉。
"""

from __future__ import annotations

from pathlib import Path

from movieclaw_db.engine import DEFAULT_CACHE_MB, Database


async def _pragmas(db: Database) -> dict[str, int]:
    async with db.engine.begin() as conn:
        out = {}
        for name in ("cache_size", "journal_mode", "busy_timeout", "foreign_keys",
                     "synchronous"):
            value = (await conn.exec_driver_sql(f"PRAGMA {name}")).scalar_one()
            out[name] = value
        return out


async def test_default_pragmas(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    try:
        pragmas = await _pragmas(db)
    finally:
        await db.dispose()
    # 负数 = 按 KiB 计（正数才是页数）；32MB 的表达就是 -32768
    assert pragmas["cache_size"] == -DEFAULT_CACHE_MB * 1024
    assert str(pragmas["journal_mode"]).lower() == "wal"
    assert pragmas["busy_timeout"] == 5000
    assert pragmas["foreign_keys"] == 1
    assert pragmas["synchronous"] == 1  # NORMAL


async def test_cache_mb_is_configurable(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}", cache_mb=8)
    try:
        assert (await _pragmas(db))["cache_size"] == -8 * 1024
    finally:
        await db.dispose()


async def test_zero_falls_back_to_sqlite_default(tmp_path: Path) -> None:
    """内存吃紧的设备设 0：完全不下这条 PRAGMA，保持 SQLite 自己的默认值。"""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}", cache_mb=0)
    try:
        assert (await _pragmas(db))["cache_size"] == -2000
    finally:
        await db.dispose()
