"""合集领域层（docs/design/library-collections.md 第 2 节）。

**贯穿全文的约束：合集根本没有自己的查询。**

``resolve_members()`` 是 ``items._wall_page_ids()`` 的一层薄适配——把 ``rules``
翻成 ``LibraryFilter``，连同 ``library_id`` / ``sort`` / 分页一起传进去。海报墙
翻页跑的是它，合集列表跑的也是它，Jellyfin 兼容层要成员时跑的还是它。

初稿把"规则求值只能有一个实现"当纪律写（"两边都要记得调同一个函数"）。纪律
是会松的。做成薄适配之后，协议层想自己写一条查询，得先把 ``_wall_page_ids``
抄一遍——那种代码在 review 里是藏不住的。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.library.access import ContentLimit
from movieclaw_api.services.library.items import (
    LibraryFilter,
    WallSort,
    _narrow,
    _wall_count,
    _wall_page_ids,
)
from movieclaw_db.models import Collection, CollectionItem, LibraryFile

#: 内置合集标识：「我的收藏」。产品里它本来就是一个合集，登记之后自动出现在
#: 合集列表与 Jellyfin BoxSet 里——这个抽象要吃掉既有特例，而不是摆在它旁边。
#: （`/library/favorites` 那个页面本期不动，见设计文档 1.2 的分寸。）
BUILTIN_FAVORITES = "favorites"

#: 内置合集的规则：与用户创建的合集共用同一套结构，只是不可编辑。
_BUILTIN_RULES: dict[str, list[dict]] = {
    BUILTIN_FAVORITES: [{"field": "watch", "op": "any_of", "values": ["favorite"]}],
}


def rules_to_filter(rules: list) -> LibraryFilter:
    """``[{field, op, values}]`` → 筛选层的值对象。

    合集规则与筛选条件是同一套结构（library-routing.md 1.1 定的那套），所以这里
    只是一次形状转换，没有第二套语义。未知字段**保守忽略**并不报错：新版本写的
    规则被旧代码读到时，宁可少收窄也不能误收窄（与路由的降级安全同源）。
    """
    genres: list[int] = []
    countries: list[str] = []
    decades: list[str] = []
    watch = None
    ratings: list[float] = []
    runtimes: list[str] = []
    languages: list[str] = []
    resolutions: list[str] = []
    stock: list[str] = []
    series_keys: list[str] = []
    hdr = None
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        field = rule.get("field")
        values = rule.get("values") or []
        if field == "genres":
            genres.extend(int(v) for v in values if str(v).lstrip("-").isdigit())
        elif field == "origin_countries" or field == "countries":
            countries.extend(str(v).upper() for v in values)
        elif field == "decades":
            decades.extend(str(v) for v in values)
        elif field == "watch":
            watch = str(values[0]) if values else None
        elif field == "rating_gte":
            ratings.extend(float(v) for v in values)
        elif field == "runtimes":
            runtimes.extend(str(v) for v in values)
        elif field == "languages":
            languages.extend(str(v).lower() for v in values)
        elif field == "resolutions":
            resolutions.extend(str(v) for v in values)
        elif field == "hdr":
            hdr = bool(values[0]) if values else None
        elif field == "stock":
            stock.extend(str(v) for v in values)
        elif field == "series_key":
            # 系列合集的规则就是这一条。字段名用单数 series_key（与列同名），
            # 与"值是个列表"并不矛盾：维内 OR 是全局约定
            series_keys.extend(str(v) for v in values)
    return LibraryFilter(
        genres=tuple(genres),
        countries=tuple(countries),
        decades=tuple(decades),
        watch=watch,  # type: ignore[arg-type]
        rating_gte=max(ratings) if ratings else None,
        runtimes=tuple(runtimes),
        languages=tuple(languages),
        resolutions=tuple(resolutions),
        hdr=hdr,
        stock=tuple(stock),
        series_keys=tuple(series_keys),
    )


def effective_rules(collection: Collection) -> list:
    """合集的实际规则。

    **行上存了规则就用它**——系列合集正是这么建的（``series_key = X`` 那一条），
    它是自动生成的（``builtin`` 非空、不可改），但规则是活的、存在行里。
    只有规则**长在代码里**的那种内置合集（「我的收藏」的"看的人收藏了它"）
    才去规则表里查：那种条件没法写成 ``[{field, op, values}]``。

    初版这里是先看 ``builtin`` 再查表，于是任何 builtin 合集都会被当成
    "规则在表里"，系列合集的规则被整条丢掉、成员恒为空。
    """
    if collection.rules:
        return list(collection.rules)
    if collection.builtin:
        # builtin 存的是「类型:库id」（favorites:12），规则表按类型查
        return _BUILTIN_RULES.get(builtin_key(collection) or "", [])
    return []


def is_rule_driven(collection: Collection) -> bool:
    """规则驱动（会自己长）还是名单驱动（固定）。形态是推导的，不是存的。"""
    return bool(effective_rules(collection))


async def resolve_members(
    session: AsyncSession,
    collection: Collection,
    *,
    member_id: int | None = None,
    visible_library_ids: set[int] | None = None,
    content_limit: ContentLimit | None = None,
    sort: WallSort | None = None,
    limit: int | None = None,
    offset: int = 0,
    only_item_id: int | None = None,
) -> list[int]:
    """合集成员的 ``media_item_id``，按 sort 排好。

    ``only_item_id`` 把候选集收窄成一个条目，用来反查"这部片属于哪些合集"：
    返回 ``[id]`` 就是在、``[]`` 就是不在。**刻意不另写一套判定**——详情页
    说它在、点进合集却找不到它，是最难查的一类不一致，而这个产品的合集
    从来没有自己的查询（第 0 节）。

    规则驱动 → ``rules_to_filter()`` 后原样交给 ``_wall_page_ids()``；
    名单驱动 → ``collection_item`` 按 position，再过一遍可见性与在位文件。

    ``visible_library_ids`` 是成员可见库的收口点（None=不受限）。跨库合集
    （``library_id IS NULL``）目前只能是名单驱动——规则驱动要指定库，
    跨库的规则求值排在 F4。
    """
    if is_rule_driven(collection) and collection.library_id is not None:
        if visible_library_ids is not None and collection.library_id not in visible_library_ids:
            return []
        return await _wall_page_ids(
            session,
            collection.library_id,
            sort or collection.sort or "title",  # type: ignore[arg-type]
            limit,
            offset,
            "confirmed",
            rules_to_filter(effective_rules(collection)),
            member_id,
            content_limit,
            only_item_id,
        )

    # 名单驱动：position 序就是用户拖出来的顺序，不给 sort 时原样保留。
    # 反查时只取那一行——打 (collection_id, media_item_id)，不拉整份名单
    member_rows = select(CollectionItem.media_item_id).where(
        CollectionItem.collection_id == collection.id
    )
    if only_item_id is not None:
        member_rows = member_rows.where(CollectionItem.media_item_id == only_item_id)
    rows = (
        (await session.execute(member_rows.order_by(CollectionItem.position, CollectionItem.id)))
        .scalars()
        .all()
    )
    ids = [i for i in rows if i is not None]
    if not ids:
        return []
    # 只留在**可见库**里还有在位文件的成员：条目被删/文件搬走之后，
    # 名单里的那一行还在，但它已经不该出现在墙上了
    where = [
        LibraryFile.media_item_id.in_(ids),  # type: ignore[attr-defined]
        LibraryFile.media_item_id.is_not(None),  # type: ignore[union-attr]
        LibraryFile.on_shelf(),
        # 名单驱动的合集同样受观看者的分级约束：手工挑进去的片不因为"是手挑的"
        # 就绕过儿童档案——那正是最需要挡住的一种（"我给自己存的片单"）
        *_narrow(None, member_id, content_limit=content_limit),
    ]
    if collection.library_id is not None:
        where.append(LibraryFile.library_id == collection.library_id)
    if visible_library_ids is not None:
        where.append(LibraryFile.library_id.in_(visible_library_ids))  # type: ignore[attr-defined]
    alive = set(
        (await session.execute(select(LibraryFile.media_item_id).where(*where).distinct()))
        .scalars()
        .all()
    )
    ordered = [i for i in ids if i in alive]
    return ordered[offset : offset + limit] if limit is not None else ordered[offset:]


async def count_members(
    session: AsyncSession,
    collection: Collection,
    *,
    member_id: int | None = None,
    visible_library_ids: set[int] | None = None,
    content_limit: ContentLimit | None = None,
) -> int:
    """成员数——规则驱动的合集走一条 ``COUNT(DISTINCT)``，**不把 id 取出来**。

    数量与顺序无关，所以数数不必经过排序那一段。这条分寸在合集只有一两个的
    时候无所谓，自动生成几十个系列合集之后就是那道悬崖：按标题排序的合集每
    数一次就要把全库标题取出来在 Python 里做一次拼音排序（``_titles_sorted``），
    几十个合集叠起来是肉眼可见的卡（设计文档 5.5）。

    收窄条件仍然只有一处（``_wall_count`` 与 ``_wall_page_ids`` 共用 ``_narrow``），
    换的只是投影。口径一致由回归用例压着：``count_members == len(resolve_members)``。

    **没有缓存，也不该有**：像「我的收藏」这类跟着看的人变的合集，用户点一下心
    成员数就变，而点心不会碰任何库级的时间戳——跟着库统计失效的缓存会一直脏到
    下次扫描（设计文档 2.4）。内容定义型的合集（系列/类型/年代）另当别论，
    但那要连着 ``refresh_stats`` 一起做，不在这一期（5.5.2）。
    """
    if is_rule_driven(collection) and collection.library_id is not None:
        if visible_library_ids is not None and collection.library_id not in visible_library_ids:
            return 0
        return await _wall_count(
            session,
            collection.library_id,
            "confirmed",
            rules_to_filter(effective_rules(collection)),
            member_id,
            content_limit,
        )
    # 名单驱动：成员本来就要逐个过一遍存活判定，没有更便宜的问法
    ids = await resolve_members(
        session,
        collection,
        member_id=member_id,
        visible_library_ids=visible_library_ids,
        content_limit=content_limit,
    )
    return len(ids)


async def has_any_member(
    session: AsyncSession,
    collection: Collection,
    *,
    member_id: int | None = None,
    visible_library_ids: set[int] | None = None,
    content_limit: ContentLimit | None = None,
) -> bool:
    """这个合集对该成员**至少有一个**成员吗（LIMIT 1，不数总数）。

    给「要不要下发合集入口」这类判断用。数总数要把整批解析出来，而这里只关心
    有没有——把「我的收藏」这类内置合集登记进来之后，每个库都常驻一行空合集，
    只判元数据存在会让电视端天天挂着一个点进去空无一物的视图。
    """
    return bool(
        await resolve_members(
            session,
            collection,
            member_id=member_id,
            visible_library_ids=visible_library_ids,
            content_limit=content_limit,
            limit=1,
        )
    )


async def visible_collections(
    session: AsyncSession,
    *,
    library_id: int | None = None,
    member_id: int | None = None,
    visible_library_ids: set[int] | None = None,
    include_hidden: bool = False,
) -> list[Collection]:
    """按元数据可见的合集（**不解析成员**——那是一条便宜的 SQL）。

    可见性第一层：private 且不是本人的不下发；所属库对该成员不可见的不下发；
    落了墓碑（``hidden``）的不下发。第二层（成员条目是否可见）在
    ``resolve_members`` 里；第三层（过滤后为空是否还下发）由调用方按场景决定
    ——列合集时该丢掉空的，判断"有没有合集"时不必为此解析每一个。

    ``include_hidden`` 是隐藏的**回头路**：界面上的"显示已隐藏的合集"用它把
    墓碑翻出来，用户才能取消隐藏。不给回头路的隐藏是单向黑洞。
    """
    query = select(Collection).order_by(Collection.position, Collection.id)
    if library_id is not None:
        query = query.where(Collection.library_id == library_id)
    if not include_hidden:
        query = query.where(Collection.hidden.is_(False))  # type: ignore[union-attr]
    rows = list((await session.execute(query)).scalars().all())
    out: list[Collection] = []
    for row in rows:
        if row.visibility == "private" and row.member_id != member_id:
            continue
        if (
            visible_library_ids is not None
            and row.library_id is not None
            and row.library_id not in visible_library_ids
        ):
            continue
        out.append(row)
    return out


async def ensure_builtin_collections(session: AsyncSession, library_id: int) -> None:
    """为一个库补齐内置合集（幂等）。

    现在只有「我的收藏」。放在库维度而不是全局，是因为合集挂在库下面
    （设计文档 4.3 的 IA 决策），跨库的那一份等 F4 开跨库入口时再说。
    """
    builtin = f"{BUILTIN_FAVORITES}:{library_id}"
    exists = (
        await session.execute(select(Collection.id).where(Collection.builtin == builtin))
    ).scalar_one_or_none()
    if exists is not None:
        return
    session.add(
        Collection(
            name="我的收藏",
            library_id=library_id,
            rules=[],
            builtin=builtin,
            sort="added_at",
            position=-1,  # 内置的排在用户合集前面
        )
    )
    await session.flush()


def builtin_key(collection: Collection) -> str | None:
    """内置标识里的类型部分（``favorites:12`` → ``favorites``）。"""
    if not collection.builtin:
        return None
    return collection.builtin.split(":", 1)[0]


async def collections_containing(
    session: AsyncSession,
    media_item_id: int,
    *,
    library_id: int,
    member_id: int | None = None,
    visible_library_ids: set[int] | None = None,
    content_limit: ContentLimit | None = None,
) -> list[Collection]:
    """这部片属于哪些合集——作品详情页那一行「合集」的数据源。

    成员是**算出来的**，没有反查表，所以这里逐个合集问一遍"它在不在里面"。
    看着像 N+1，实际不是：每一问都把候选集收窄成这一个条目
    （``resolve_members(only_item_id=...)``），规则驱动的那几个各是一条打得到
    索引的小查询，名单驱动的只取 ``(collection_id, media_item_id)`` 那一行。

    N 也比看上去小——**系列合集整类跳过**。它们是自动生成的，一个三百部的库
    可能有四十个；而这部片属于哪个系列，``media_metadata.series_key`` 已经
    直接给出答案（详情页把它单独摆一行，那是关于作品的事实，不是用户的归类）。
    真正要逐个问的只有用户自己存的那几个，个人库里是个位数。

    **「我的收藏」也不出现**：详情页上那颗心就在几十像素之外，同一件事说两遍
    是噪音。隐藏的合集、别人的私有合集同样不出现（``visible_collections``
    已经挡掉）。
    """
    from movieclaw_api.services.library.series import is_series_collection

    rows = await visible_collections(
        session,
        library_id=library_id,
        member_id=member_id,
        visible_library_ids=visible_library_ids,
    )
    found: list[Collection] = []
    for row in rows:
        if is_series_collection(row) or builtin_key(row) == BUILTIN_FAVORITES:
            continue
        hit = await resolve_members(
            session,
            row,
            member_id=member_id,
            visible_library_ids=visible_library_ids,
            content_limit=content_limit,
            limit=1,
            only_item_id=media_item_id,
        )
        if hit:
            found.append(row)
    return found
