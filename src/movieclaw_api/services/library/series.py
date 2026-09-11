"""作品系列（docs/design/library-series-collections.md）。

**系列不是新实体。** 最容易走错的一步是给「系列」单开一张表再写一套同步逻辑
把它和 `collection` 对起来——那说明抽象没立住。系列合集就是一条**规则驱动的
合集**，规则是「属于系列 X」：

    rules = [{"field": "series_key", "op": "any_of", "values": ["tmdb:1241"]}]

这一句话决定了后面全部不用写：新入库一部续集自动进合集（规则驱动的合集本来
就是查询时求值）、网页与 Jellyfin 成员一致（两边都走 `resolve_members`）、
合集页/封面/BoxSet 全部沿用（那套 UI 不区分合集从哪来）。

命名分寸：代码里一律用 ``series`` 指「作品系列」，``collection`` 只留给「合集」
那个容器——「合集」这个词在本仓库已经有四个意思了（collections.md 1.2.1）。

本模块只管三件事：把系列身份规范成唯一的 ``series_key``、按它幂等地 ensure
一个合集、以及开关打开时的一次性补齐。**成员行一行都不写**（``collection_item``
只服务用户手挑的固定名单）：一部片改了识别、``series_key`` 变了，它自动从旧
合集消失、进新合集，没有任何清理逻辑要写。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_db.models import Collection, Library, LibraryFile, MediaMetadata, utcnow

logger = logging.getLogger("movieclaw_api.library_series")

#: 系列合集的 builtin 前缀。builtin 非空 = 自动生成 = 规则不可改、删除落墓碑，
#: 与「我的收藏」共用同一套推导，不需要第二个标志位
SERIES_BUILTIN_PREFIX = "series"


def normalize_series_name(name: str) -> str:
    """系列名 → 判定用的规范形。

    只做**保守**的归一：去掉所有空白、大小写折叠。不做繁简转换、不删「系列」
    「合集」这类后缀——那些都是猜，猜错了会把两个不同的系列并成一个，
    而并错了比分开更难发现。
    """
    return re.sub(r"\s+", "", name).casefold()


#: "查过了，它不属于任何系列"。三态铁律（media_metadata 的类注释）：
#: NULL=还没查过，空串=查过没有，非空=系列键。存量回填靠这个分界知道哪些条目
#: 已经问过 TMDB——不然"没有系列"的片每轮都会被重新请求一遍，永远跑不完
SERIES_KEY_NONE = ""


def build_series_key(tmdb_id: int | None, name: str | None) -> str:
    """系列身份 → 唯一的 ``series_key``（没有系列时是空串，**不是 None**）。

    **优先级定死**：有 TMDB id 就是 ``tmdb:{id}``，否则才是 ``name:{规范化名}``。
    两者并存的写法（tmdb_id 一列、名字一列）会让同一部片落进两个几乎一样的
    合集——用户看到两个《哈利·波特》。一个规范化 key 从构造上杜绝重影。

    ``name:`` 那一支只会出现在**没有 TMDB 身份**的条目上（本地库、认不出的片），
    与 ``tmdb:`` 天然不相交。
    """
    if tmdb_id:
        return f"tmdb:{int(tmdb_id)}"
    if name and name.strip():
        return f"name:{normalize_series_name(name)}"
    return SERIES_KEY_NONE


def series_rules(key: str) -> list[dict]:
    """系列合集的规则。与用户自己存的筛选同构，所以它不是特例。"""
    return [{"field": "series_key", "op": "any_of", "values": [key]}]


def series_builtin(key: str, library_id: int) -> str:
    """系列合集的 builtin 标识。

    带上 ``library_id``：规则驱动的合集必须指定所属库，所以同一个系列同时躺在
    「电影库」和「4K 电影库」时是两行、两个 BoxSet。这是"合集挂在库下面"那条
    IA 决策的自然结果，不是 bug（跨库合集统一等 F4）。
    """
    return f"{SERIES_BUILTIN_PREFIX}:{key}:{library_id}"


def is_series_collection(collection: Collection) -> bool:
    """这是不是一个自动生成的系列合集。"""
    return bool(collection.builtin) and collection.builtin.startswith(f"{SERIES_BUILTIN_PREFIX}:")


def series_key_of(collection: Collection) -> str | None:
    """系列合集 → 它的 ``series_key``（``series:tmdb:1241:3`` → ``tmdb:1241``）。"""
    if not is_series_collection(collection):
        return None
    body = (collection.builtin or "").split(":", 1)[1]
    return body.rsplit(":", 1)[0] or None


async def ensure_series_collection(
    session: AsyncSession, library_id: int, key: str, name: str | None
) -> Collection | None:
    """为一个库补齐某个系列的合集（幂等，**不 commit**）。

    幂等靠 ``builtin`` 上的唯一约束：先查后插，一条点查走索引。一次一千部的
    扫描多一千条点查可以忽略——真嫌多再按 distinct key 批量做，但别提前优化。

    **已隐藏的墓碑不复活**：用户点过「删除」的系列，这里查得到行就直接返回，
    不会把 ``hidden`` 抹掉——否则下一次刮削就把用户的选择顶掉了，那正是
    "留行当墓碑"要解决的事。
    """
    builtin = series_builtin(key, library_id)
    row = (
        await session.execute(select(Collection).where(Collection.builtin == builtin))
    ).scalar_one_or_none()
    if row is not None:
        # 系列改名（TMDB 会改）时跟着更新展示名；用户自己改过的名字不动——
        # 但目前无从分辨"用户改的"与"TMDB 给的"，所以宁可不动，改名是用户的
        return row
    row = Collection(
        name=name or key,
        library_id=library_id,
        rules=series_rules(key),
        builtin=builtin,
        # 系列要按上映**正序**看，不是按标题、也不是墙上默认的倒序——
        # 《死亡圣器(上)》排在《混血王子》前面这种事，用户会当成 bug
        sort="release_date_asc",
        position=1,  # 内置的「我的收藏」是 -1，用户自建的是 0，系列排最后
    )
    session.add(row)
    await session.flush()
    return row


async def rename_series_collections(
    session: AsyncSession, key: str | None, previous_name: str | None, name: str | None
) -> int:
    """系列名变了（典型是换了刮削语言）时，让**用户没改过名**的系列合集跟着改名（不 commit）。

    ``ensure_series_collection`` 对已存在的行不碰名字，因为它分不清名字是 TMDB 给的
    还是用户改的。调用方手上有**改之前**的系列名，这件事就分得清了：合集名仍等于
    旧系列名（或建行时兜底用的 key），说明它还是自动起的名，跟着换；不相等就是用户
    自己起的名，一个字都不动。

    例：刮削语言是中文，但系列名当初按英文落了库——重新按中文问回来之后，
    「If You Are the One (Collection)」变成「非诚勿扰（系列）」。

    返回改了几行。``key`` 相同才有意义：key 变了是换了系列，新系列的合集由 ensure 新建。
    """
    if not key or not name or name == previous_name:
        return 0
    automatic = {key} | ({previous_name} if previous_name else set())
    rows = (
        (
            await session.execute(
                select(Collection).where(
                    # 末尾带冒号：series:tmdb:12 不能误伤 series:tmdb:123
                    Collection.builtin.startswith(  # type: ignore[union-attr]
                        f"{SERIES_BUILTIN_PREFIX}:{key}:", autoescape=True
                    ),
                    Collection.name.in_(automatic),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.name = name
        row.updated_at = utcnow()
        session.add(row)
    return len(rows)


async def ensure_series_collections_for_item(session: AsyncSession, media_item_id: int) -> int:
    """一部作品刮完/入库之后，为它所在的每个库补齐系列合集（**不 commit**）。

    只写合集行，**不写成员行**——成员是 ``series_key = X`` 求值出来的。

    受每个库自己的展示开关管（``library.auto_series_collections``）：关着的库
    照样落 ``series_key``、照样写 NFO，只是不建行。
    """
    meta = (
        await session.execute(
            select(MediaMetadata.series_key, MediaMetadata.series_name).where(
                MediaMetadata.media_item_id == media_item_id
            )
        )
    ).first()
    if meta is None or not meta[0]:
        return 0
    key, name = meta[0], meta[1]
    library_ids = [
        i
        for i in (
            await session.execute(
                select(LibraryFile.library_id)
                .where(
                    LibraryFile.media_item_id == media_item_id,
                    LibraryFile.on_shelf(),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
        if i is not None
    ]
    if not library_ids:
        return 0
    allowed = {
        row.id
        for row in (
            await session.execute(select(Library).where(Library.id.in_(library_ids)))
        ).scalars()
        if row.auto_series_collections
    }
    created = 0
    for library_id in library_ids:
        if library_id not in allowed:
            continue
        if await ensure_series_collection(session, library_id, key, name) is not None:
            created += 1
    return created


#: 系列档案快照的字段形状。存的是 TMDB 的 parts[]，只留用得上的四项——
#: 它是**外部档案的快照，不是我们的事实源**，脏了重拉即可
def _part_row(raw: dict) -> dict | None:
    tmdb_id = raw.get("id")
    title = raw.get("title") or raw.get("name")
    if not tmdb_id or not title:
        return None
    return {
        "tmdb_id": int(tmdb_id),
        "title": title,
        "release_date": raw.get("release_date") or None,
        "poster_path": raw.get("poster_path") or None,
    }


async def load_series_parts(session: AsyncSession, collection: Collection) -> list[dict]:
    """系列的全片名单（**懒加载 + 快照**，不 commit）。

    只有用户真的打开这个系列的详情页时才发一次 ``GET /collection/{id}``，之后
    读快照。初稿写的是刮削时每个系列拉一次——那是白白给扫描加负担，而用户
    从没点开的系列一个请求都不该花。

    拿到 parts 才能回答「你缺哪几部」，而这才是系列合集真正的价值：只做归类
    的话，用户装个 Emby 也有。顺带白拿系列官方海报（同一次响应里就有）。

    拉不到（没配 TMDB / 网络不通 / ``name:`` 那一支根本没有上游档案）就返回
    已有的快照或空表——缺片补齐是详情页里的一块，不该让整页打不开。
    """
    if collection.series_parts is not None:
        return list(collection.series_parts)
    key = series_key_of(collection)
    if not key or not key.startswith("tmdb:"):
        # name: 那一支没有上游档案可拉（本地库/认不出的片），只能靠库里有什么
        return []
    from movieclaw_api.services.media_discover import get_tmdb_client
    from movieclaw_api.services.scrape_config import (
        ITEM_SCOPED_OVERRIDABLE,
        effective_language,
        merge_for_library,
    )

    # 片名按刮削设置的元数据语言要：不带 language 时 TMDB 一律回英文。系列合集
    # 必定挂在某个库下（规则驱动），语言取那个库的有效设置，与刮削管线同一口径
    library = (
        await session.get(Library, collection.library_id)
        if collection.library_id is not None
        else None
    )
    language = effective_language(merge_for_library(library, fields=ITEM_SCOPED_OVERRIDABLE))
    try:
        data = await get_tmdb_client().get(
            f"collection/{key.split(':', 1)[1]}", {"language": language}
        )
    except Exception:  # noqa: BLE001 -- 缺片是锦上添花，拉不到不该让详情页打不开
        logger.warning("TMDB 系列档案读取失败，缺片补齐本次不可用", exc_info=True)
        return []
    parts = [row for row in (_part_row(raw) for raw in data.get("parts") or []) if row]
    parts.sort(key=lambda row: (row["release_date"] or "9999-12-31", row["tmdb_id"]))
    collection.series_parts = parts
    collection.series_image = data.get("poster_path") or None
    session.add(collection)
    await session.flush()
    return parts


async def series_collection_id_for(
    session: AsyncSession, library_id: int, media_item_id: int
) -> int | None:
    """这部作品在这个库里的系列合集 id；没有就 None。

    影片详情页的「所属系列」入口用它。**隐藏了的合集不给 id**——用户明确说过
    不想看见这个系列，影片页却还挂着入口，那就是同一件事说了两遍不同的话。
    """
    key = (
        await session.execute(
            select(MediaMetadata.series_key).where(MediaMetadata.media_item_id == media_item_id)
        )
    ).scalar_one_or_none()
    if not key:
        return None
    return (
        await session.execute(
            select(Collection.id).where(
                Collection.builtin == series_builtin(key, library_id),
                Collection.hidden.is_(False),  # type: ignore[union-attr]
            )
        )
    ).scalar_one_or_none()


async def ensure_series_collections_for_library(session: AsyncSession, library_id: int) -> int:
    """把一个库里已有的系列一次性补齐（**不 commit**），返回本次新建的行数。

    展示开关重新打开时走这里：一条 ``GROUP BY series_key`` 拿到全部取值，
    缺哪个建哪个。**不重新联网、不重新刮削**——数据早就在列里了。

    与 ``ensure_series_collections_for_item`` 一样受库的展示开关管：扫描收尾每轮
    都走这里，不查开关的话，关着开关的库扫一次，系列合集就全回来了。
    """
    library = await session.get(Library, library_id)
    if library is None or not library.auto_series_collections:
        return 0
    rows = (
        await session.execute(
            select(MediaMetadata.series_key, MediaMetadata.series_name)
            .join(LibraryFile, LibraryFile.media_item_id == MediaMetadata.media_item_id)
            .where(
                LibraryFile.library_id == library_id,
                LibraryFile.on_shelf(),
                MediaMetadata.series_key.is_not(None),
                MediaMetadata.series_key != SERIES_KEY_NONE,
            )
            .group_by(MediaMetadata.series_key)
        )
    ).all()
    created = 0
    for key, name in rows:
        if not key:
            continue
        before = (
            await session.execute(
                select(Collection.id).where(Collection.builtin == series_builtin(key, library_id))
            )
        ).scalar_one_or_none()
        await ensure_series_collection(session, library_id, key, name)
        if before is None:
            created += 1
    if created:
        logger.info("媒体库 %s：按作品系列新建了 %d 个合集", library_id, created)
    return created
