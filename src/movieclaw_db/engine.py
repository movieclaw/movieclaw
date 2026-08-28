from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger("movieclaw_db.engine")


# ---------------------------------------------------------------------------
# SQLite 连接级 PRAGMA 设置
# ---------------------------------------------------------------------------
def _configure_sqlite_pragmas(dbapi_conn, _connection_record) -> None:
    """每次建立新连接时执行的 PRAGMA 设置。

    这些设置是 SQLite 在"单体应用 + 一定并发"场景下稳定运行的关键：

    - ``journal_mode=WAL``：写前日志模式。让"多个读"与"单个写"可以并发，
      读操作不再被写操作阻塞，显著改善 FastAPI async 下的并发表现。
    - ``busy_timeout=5000``：遇到数据库锁时最多等待 5 秒再报错，
      避免瞬时写冲突直接抛出 "database is locked"。
    - ``foreign_keys=ON``：SQLite 默认不强制外键约束，显式打开以保证数据完整性。
    - ``synchronous=NORMAL``：配合 WAL 使用的推荐值，在安全与性能间取得平衡。
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()


def _ensure_sqlite_dir(database_url: str) -> None:
    """确保 SQLite 数据库文件所在目录存在。

    形如 ``sqlite+aiosqlite:///./data/movieclaw.db`` 的 URL，若 ``data/`` 目录
    不存在，SQLite 打开时会直接失败。这里提前创建，让非开发者部署时开箱即用。
    """
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    raw_path = database_url[len(prefix) :]
    # 内存数据库（:memory: 或空路径）无需建目录
    if not raw_path or raw_path.startswith(":memory:"):
        return
    db_path = Path(raw_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Database：封装 engine 与 session 工厂的持有者
# ---------------------------------------------------------------------------
class Database:
    """数据库连接的统一入口。

    持有一个 async engine 和对应的 session 工厂，贯穿应用整个生命周期。
    典型用法是在应用启动时创建单例（见 ``init_db``），关闭时调用 ``dispose``。
    """

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        _ensure_sqlite_dir(database_url)
        self.is_sqlite = database_url.startswith("sqlite")

        self._engine: AsyncEngine = create_async_engine(
            database_url,
            echo=echo,
            # 允许连接在不同线程间复用（aiosqlite 在线程池中执行，需关闭该检查）
            connect_args={"check_same_thread": False},
        )

        # 为底层同步引擎注册 connect 事件，写入 PRAGMA
        # （async 引擎通过 sync_engine 暴露事件挂载点）
        if database_url.startswith("sqlite"):
            event.listen(self._engine.sync_engine, "connect", _configure_sqlite_pragmas)

        # expire_on_commit=False：提交后对象仍可访问属性，避免 async 上下文中
        # 触发意外的隐式惰性加载（在异步环境里惰性加载会报错）
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.info("数据库引擎已初始化：%s", database_url)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session(self) -> AsyncSession:
        """新建一个会话。调用方负责关闭（推荐用 ``async with``）。"""
        return self._session_factory()

    async def dispose(self) -> None:
        """释放连接池。应用关闭时调用。"""
        await self._engine.dispose()
        logger.info("数据库引擎已释放")


# ---------------------------------------------------------------------------
# 查询计划统计
# ---------------------------------------------------------------------------
async def refresh_query_statistics(db: Database) -> None:
    """刷新 SQLite 的索引选择性统计（``sqlite_stat1``）。

    没有这张表时 SQLite 只能靠内置猜测挑索引，而它的猜测在本库上是错的：
    ``library_file.state`` 全表只有 ``in_place`` 一个取值，索引
    ``ix_library_file_state`` 命中全部行，planner 却把它当成高选择性条件优先
    选中——于是"取某个条目的文件行"这种最高频的查询变成整表 6800 行的索引
    扫描再回过头逐行过滤。生产库实测：跑一次统计后同一条查询 1.66ms → 0.32ms。
    影响面是全站的，媒体库网页、Jellyfin 兼容层、播放链路都走同一批查询。

    用 ``PRAGMA optimize`` 而不是裸 ``ANALYZE``：它只对"统计缺失或已经明显
    过时"的表动手，日常启动几乎零成本；``analysis_limit`` 给单表采样设上限，
    库再大也不会把启动卡住（生产库整轮 12ms）。

    启动与关闭各跑一次：启动那次修正上一轮运行期间的增量（扫描入库会让行数
    翻倍），关闭那次把本轮的变化落下去，下次启动即是新的。失败只记日志——
    统计是纯优化，缺了只是慢，不该拦住应用起停。
    """
    if not db.is_sqlite:
        return
    try:
        async with db.engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA analysis_limit=400")
            await conn.exec_driver_sql("PRAGMA optimize")
    except Exception:
        logger.warning("刷新数据库查询统计失败（不影响功能，只可能让查询变慢）",
                       exc_info=True)


# ---------------------------------------------------------------------------
# 模块级单例 + FastAPI 依赖
# ---------------------------------------------------------------------------
_db: Database | None = None


def init_db(database_url: str, *, echo: bool = False) -> Database:
    """初始化全局数据库单例。应在应用启动（lifespan）时调用一次。"""
    global _db
    if _db is not None:
        logger.warning("数据库已初始化，重复调用 init_db 被忽略")
        return _db
    _db = Database(database_url, echo=echo)
    return _db


def get_database() -> Database:
    """获取全局数据库单例。未初始化时抛错，提示调用方检查启动流程。"""
    if _db is None:
        raise RuntimeError("数据库尚未初始化，请确认应用启动时已调用 init_db()")
    return _db


async def dispose_db() -> None:
    """释放并清空全局数据库单例。应用关闭时调用。"""
    global _db
    if _db is not None:
        await _db.dispose()
        _db = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：为每个请求提供一个会话，请求结束自动关闭。

    用法::

        @router.get("/x")
        async def handler(session: AsyncSession = Depends(get_session)):
            ...

    事务边界交由 Repository / Service 层用 ``async with session.begin()`` 或
    显式 ``commit()`` 控制，这里只负责会话的创建与释放。
    """
    db = get_database()
    async with db.session() as session:
        yield session
