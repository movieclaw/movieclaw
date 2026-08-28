"""媒体库路由：库自述收藏范围的求值与选库（docs/design/library-routing.md）。

每个库可声明"本库收什么"（``library.match_rules``：条件列表，条件间 AND、
条件内 any_of 交集即满足）；路由 = 在同 kind 的库里找命中的那个，多库命中
取特异性（条件条数）最高、打平取创建更早的；都不命中落该 kind 默认库——
**路由永不失败**，任何异常（元数据不可得、无命中、未知字段）都有兜底与
可读的中文理由。

三条产品铁律的落点：
- 规则只决定默认值：消费方（订阅弹窗预检/创建定格）永远允许用户显式改库；
- 路由永不失败：``route`` 的三个出口都带 reason，绝不抛异常阻断投递/入库；
- 全程可解释：``RouteDecision.reason`` 是直接可展示的整句中文，预检、订阅
  时间线、入库结论三处共用，不各写各的文案。

作品事实（RoutingFacts）按优先级取：刮削档案（media_metadata.genre_ids，
断网可用）→ TMDB 轻量详情（一次 GET，genre ID 原生返回）→ 都不可得
返回 None（route 落默认库并写明理由）。genres 匹配用 TMDB genre **ID**
而非名字——genre 名随 ``tmdb_language`` 变化，存名字会静默失效。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models import MediaItem, MediaMetadata
from movieclaw_db.models.library import Library
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.genres import country_label, genre_label
from movieclaw_media.library import extract_genre_ids, extract_origin_countries

logger = logging.getLogger("movieclaw_api.library_routing")

# 收藏范围条件支持的字段（v1：区域 + 类型）。加维度（导演/公司/系列）时
# 扩展这里与 evaluate 的取值分支即可——存储结构与算法不变
RULE_FIELDS = ("genres", "origin_countries")
_FIELD_LABELS = {"genres": "类型", "origin_countries": "区域"}


@dataclass(frozen=True)
class RoutingFacts:
    """路由用的作品事实（语言无关：genre 用 TMDB ID，区域用国家码）。"""

    genre_ids: tuple[int, ...] = field(default_factory=tuple)
    origin_countries: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RouteDecision:
    """一次路由的结论。library 为 None 仅当该类型一个库都没有。"""

    library: Library | None
    matched: bool  # True=命中某库的收藏范围；False=默认库兜底/无库
    reason: str  # 可直接展示的中文理由
    item: MediaItem | None = None
    """本次路由所依据的条目（走 route_for_item/route_for_tmdb 才有）。
    预检展示条目目录时要用它渲染命名模板里的 {original_title}/{tmdb_id}
    ——预览与真实落点必须出自同一套模板（命名同源，见 library/naming.py）。
    未建档的临时条目（id 为 None）标题是占位符，展示前需自行判别。"""


def evaluate(rules: list, facts: RoutingFacts | None) -> bool:
    """收藏范围声明是否命中作品：条件间 AND，条件内 any_of（交集即满足）。

    保守语义：事实缺失（facts=None）、空声明、未知字段/算子（新版本写的
    规则被旧代码读到）一律不命中——宁可落默认库，不误路由。
    """
    if facts is None or not rules:
        return False
    for rule in rules:
        if not isinstance(rule, dict):
            return False
        fld = rule.get("field")
        op = rule.get("op", "any_of")
        values = rule.get("values") or []
        if op != "any_of":
            logger.warning("收藏范围条件包含未知算子 %r，保守判为不命中", op)
            return False
        if fld == "genres":
            have: set = set(facts.genre_ids)
        elif fld == "origin_countries":
            have = set(facts.origin_countries)
        else:
            logger.warning("收藏范围条件包含未知字段 %r，保守判为不命中", fld)
            return False
        if not have & set(values):
            return False
    return True


def _hit_reason(kind: str, library: Library, facts: RoutingFacts) -> str:
    """命中理由：逐条件展示**实际命中**的值（作品事实 ∩ 条件值）。"""
    parts: list[str] = []
    for rule in library.match_rules:
        fld = rule.get("field")
        values = set(rule.get("values") or [])
        if fld == "genres":
            hit = [genre_label(kind, g) for g in facts.genre_ids if g in values]
        elif fld == "origin_countries":
            hit = [country_label(c) for c in facts.origin_countries if c in values]
        else:  # evaluate 已挡掉未知字段，这里只是防御
            continue
        if hit:
            parts.append(f"{_FIELD_LABELS[fld]}={'/'.join(hit)}")
    detail = "、".join(parts) if parts else "收藏范围命中"
    return f"命中「{library.name}」：{detail}"


async def route(session: AsyncSession, kind: str, facts: RoutingFacts | None) -> RouteDecision:
    """按收藏范围选库；不命中落该 kind 默认库。永不抛出。"""
    repo = LibraryRepository(session)
    declared = [lib for lib in await repo.list_all(kind=kind) if lib.match_rules]
    hits = [lib for lib in declared if evaluate(lib.match_rules, facts)]
    if hits:
        # 特异性（条件条数）优先，打平取 id 小者（创建早优先）
        best = sorted(hits, key=lambda lib: (-len(lib.match_rules), lib.id))[0]
        assert facts is not None  # 命中即有事实
        return RouteDecision(library=best, matched=True, reason=_hit_reason(kind, best, facts))

    default = await repo.get_default(kind)
    if default is None:
        return RouteDecision(library=None, matched=False, reason="该类型没有可用的媒体库")
    if facts is None and declared:
        reason = f"作品元数据暂不可得，入库到默认库「{default.name}」"
    elif declared:
        reason = f"未命中任何收藏范围，入库到默认库「{default.name}」"
    else:
        reason = f"入库到默认库「{default.name}」"
    return RouteDecision(library=default, matched=False, reason=reason)


async def gather_facts(session: AsyncSession, item: MediaItem) -> RoutingFacts | None:
    """装配作品事实：刮削档案 → TMDB 轻量详情 → None（兜底默认库）。

    第二级几乎不会失败：能走到路由说明识别/建档刚与 TMDB 打过交道；
    单次请求失败的瞬间窗口落默认库 + 写明理由已是正确行为，不做重试队列。
    """
    if item.id is not None:
        meta = (
            await session.execute(
                select(MediaMetadata).where(MediaMetadata.media_item_id == item.id)
            )
        ).scalar_one_or_none()
        if meta is not None and meta.genre_ids:
            return RoutingFacts(
                genre_ids=tuple(meta.genre_ids),
                origin_countries=tuple(meta.origin_countries or []),
            )

    from movieclaw_api.services.media_discover import get_tmdb_client
    from movieclaw_api.services.scrape_config import effective_language

    try:
        data = await get_tmdb_client().get(
            f"{item.kind}/{item.tmdb_id}", {"language": effective_language()}
        )
        return RoutingFacts(
            genre_ids=tuple(extract_genre_ids(data)),
            origin_countries=tuple(extract_origin_countries(data)),
        )
    except Exception as exc:  # noqa: BLE001 -- 路由永不失败：事实拿不到就兜底默认库
        logger.warning("拉取《%s》的路由元数据失败（%s），将落默认库", item.title, exc)
        return None


async def route_for_item(session: AsyncSession, kind: str, item: MediaItem) -> RouteDecision:
    """便捷入口：装配事实 + 选库（订阅创建定格、监听导入 auto 模式共用）。"""
    decision = await route(session, kind, await gather_facts(session, item))
    return replace(decision, item=item)


async def route_for_tmdb(session: AsyncSession, kind: str, tmdb_id: int) -> RouteDecision:
    """按 TMDB 锚路由（投递预检用：弹窗打开时条目可能尚未建档）。

    条目已在库时走常规三级来源；未建档时用**临时条目**（不落库）直接走
    TMDB 轻量详情——预检是只读操作，不该有建档副作用。
    """
    existing = (
        await session.execute(
            select(MediaItem).where(MediaItem.kind == kind, MediaItem.tmdb_id == tmdb_id)
        )
    ).scalar_one_or_none()
    item = existing or MediaItem(
        kind=kind, tmdb_id=tmdb_id, title=f"tmdb#{tmdb_id}", original_title="", aliases=[]
    )
    return await route_for_item(session, kind, item)


@dataclass(frozen=True)
class SavePathDecision:
    """「投递目录三级兜底」的统一结论（全仓唯一实现，四个消费方共用）。

    真实投递（download_dispatch）、订阅弹窗预检（preview_dispatch_route）、
    链路体检（subscription_health）、手动下载（routes/downloaders）对
    mode/path 的推导必须走 ``resolve_save_path``，不允许各自再算一遍——
    否则迟早出现"体检说好、投递却挂"的口径漂移。
    """

    mode: str
    """watch=投监听导入目录；inplace=直接下载进库；downloader_default=下载器默认目录。"""

    path: str | None
    """投递基底目录（movieclaw 视角）；None 表示落下载器默认目录。"""

    entry_level: bool
    """path 是否为库内**条目级**目录——只有条目级目录才允许锚定下载线索
    （锚到监听目录/库主根会波及目录下所有无关内容）。"""

    entry_dir: str | None
    """库推导的条目目录 ``主根/标题 (年份)``（给了 title 才有），供展示
    "完成后将整理入库到 …"的文案；watch 模式下与 path 不同。"""

    rule: object | None
    """命中的监听导入规则（ImportWatch | None），体检的转移段检查需要它。"""


async def resolve_save_path(
    session: AsyncSession,
    library: Library | None,
    *,
    kind: str,
    title: str | None = None,
    year: int | None = None,
    item: object | None = None,
) -> SavePathDecision:
    """投递目录三级兜底的唯一入口。

    ① 库有监听导入规则（库为 None 时找该 kind 的 auto 规则）→ 规则源目录
       （分离布局：下载区继续做种，完成后监听导入按 info_hash 认领硬链/复制进库）；
    ② 无规则且给了 title → 库推导的条目目录（原地入库：直接下载进库根，
       库扫描实时入账）；未给 title（预检/体检场景）→ 库主根；
    ③ 没有可用库/库无根路径 → None（下载器默认目录，不会自动入库）。
    """
    from movieclaw_api.services.import_watch_config import resolve_dispatch_rule
    from movieclaw_api.services.library.config import derive_save_path

    rule = await resolve_dispatch_rule(session, library.id if library else None, kind=kind)
    # 带上条目：命名模板里的 {original_title}/{tmdb_id} 只有拿到条目才渲染得出，
    # 投递侧与整理侧因此算出同一个目录名（命名同源，见 library/naming.py）
    entry_dir = (
        derive_save_path(library, title=title, year=year, item=item) if library and title else None
    )
    if rule is not None:
        return SavePathDecision(
            mode="watch", path=rule.source_path, entry_level=False, entry_dir=entry_dir, rule=rule
        )
    if library is not None and title:
        # 给了 title 就只认条目目录：库没根路径时不回落主根（主根同样是 None）
        return SavePathDecision(
            mode="inplace" if entry_dir else "downloader_default",
            path=entry_dir,
            entry_level=entry_dir is not None,
            entry_dir=entry_dir,
            rule=None,
        )
    root = library.primary_root if library else None
    return SavePathDecision(
        mode="inplace" if root else "downloader_default",
        path=root,
        entry_level=False,
        entry_dir=entry_dir,
        rule=None,
    )


def validate_match_rules(raw: list | None) -> list[dict]:
    """收藏范围条件的写入侧校验（library_config 保存时调用）。

    返回清洗后的条件列表；结构非法抛 BadRequestException（中文报错）。
    值类型按字段收紧：genres 必须是 int（TMDB genre ID），
    origin_countries 必须是非空字符串（国家码，统一大写）。
    """
    from movieclaw_api.exceptions import BadRequestException

    if not raw:
        return []
    cleaned: list[dict] = []
    for rule in raw:
        if not isinstance(rule, dict):
            raise BadRequestException("收藏范围条件必须是对象列表")
        fld = rule.get("field")
        if fld not in RULE_FIELDS:
            raise BadRequestException(f"收藏范围条件包含不支持的字段：{fld}")
        if rule.get("op", "any_of") != "any_of":
            raise BadRequestException("收藏范围条件目前只支持 any_of（任一匹配）")
        values = rule.get("values")
        if not isinstance(values, list) or not values:
            raise BadRequestException(f"收藏范围条件「{_FIELD_LABELS[fld]}」的取值不能为空")
        if fld == "genres":
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
                raise BadRequestException("类型条件的取值必须是 TMDB 类型 ID（整数）")
            deduped: list = sorted(set(values))
        else:
            if not all(isinstance(v, str) and v.strip() for v in values):
                raise BadRequestException("区域条件的取值必须是国家/地区码")
            deduped = sorted({v.strip().upper() for v in values})
        cleaned.append({"field": fld, "op": "any_of", "values": deduped})
    if len({r["field"] for r in cleaned}) != len(cleaned):
        raise BadRequestException("收藏范围里同一字段只能出现一次（取值本身就是多选）")
    return cleaned
