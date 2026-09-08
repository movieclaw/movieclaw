"""同名同年孪生条目的按需探测（docs/design/identity-confidence.md §9）。

有些电影就是重名。2026 年有两部《The Odyssey》——诺兰的大片和 Marcel Walz 的
小成本片，片名一样、年份也一样。发布组给种子起名时能写的只有片名和年份
（``The.Odyssey.2026.1080p...``），于是"片名匹配 + 年份校验"这两条身份守卫会
对两部片同时成立，谁也分不出来。

本模块负责回答"这部电影有没有同名同年的兄弟"，答案缓存进
``media_item.identity_twins``。有兄弟的条目，自动投递门槛升一档（裁决表见
``matching.py``）。

**按需探测，不在建档时全量探**。初稿想放在 ``ensure_media_item`` 里，被否掉：
那要给每次建档加一次 TMDB 请求、要三态字段语义、存量条目还得靠刷新任务慢慢
回填。而真正需要这个答案的时刻很稀有——「电影 + 只靠片名年份认出来 + 已经
决定要投这个候选」，绝大多数候选在规则过滤阶段就没了。所以在那一刻现探、
顺手缓存，既不需要回填脚本，也不需要区分"没探过"和"探过没有"。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_db.models import MediaItem
from movieclaw_matcher import normalize_title

logger = logging.getLogger("movieclaw_api.identity_twins")

# 一次探测最多看几个搜索结果：TMDB 按热度排序，同名同年的兄弟不会排在很后面；
# 放太大只会把无关的同名剧集/老片拖进来。
_MAX_RESULTS = 10


async def ensure_twins(session: AsyncSession, item: MediaItem) -> list[dict]:
    """返回该条目的同名同年孪生清单（缓存优先，未探过则现探）。

    探测失败（TMDB 不可达等）返回空列表且**不写缓存**——保持 NULL 的"未探测"
    语义，下次再试。绝不能让一次网络抖动把订阅永久卡成待确认。
    """
    if item.identity_twins is not None:
        return item.identity_twins
    if item.kind != "movie" or item.year is None:
        # 剧集不探：另有季集号做区分，同名同年撞车的概率远低于电影，
        # 而剧集条目多得多，探测成本不值当（§9.5）
        return []

    try:
        twins = await _probe(item)
    except Exception as exc:  # noqa: BLE001 -- 探不到就按"没探过"处理，下次再试
        logger.warning("孪生条目探测失败（《%s》）：%s；本次按无歧义处理", item.title, exc)
        return []

    item.identity_twins = twins
    session.add(item)
    await session.commit()
    if twins:
        logger.info(
            "《%s》(%s) 存在同名同年条目：%s——该条目的自动投递门槛升一档",
            item.title,
            item.year,
            "、".join(f"{t['title']}(tmdb={t['tmdb_id']})" for t in twins),
        )
    return twins


async def _probe(item: MediaItem) -> list[dict]:
    """向 TMDB 搜两轮，筛出"片名归一化后相同、年份相差 ≤1"的其它条目。

    搜两轮的原因与 ``resolve.py`` 里那条注释同源：普通搜索按热度排序，冷门的
    那一部可能掉出前几名；带 ``primary_release_year`` 再捞一轮能把它捞回来。

    只比 ``title`` / ``original_title`` 两个字段，**不拉 alternative_titles**：
    那要给每个候选各发一次请求，成本翻几倍而收益边际——真正会撞车的是发布组
    会用的那个名字，基本就是原名。

    归一化必须复用内核的 ``normalize_title``：与身份匹配同口径，否则会出现
    "探测说不歧义、匹配却撞车"的裂缝。
    """
    from movieclaw_api.services.media_discover import get_tmdb_client

    client = get_tmdb_client()
    own_names = {normalize_title(name) for name in (item.title, item.original_title) if name}
    own_names.discard("")

    seen: dict[int, dict] = {}
    for params in (
        {"query": item.original_title or item.title},
        {"query": item.original_title or item.title, "primary_release_year": item.year},
    ):
        data = await client.get("search/movie", {**params, "language": "zh-CN"})
        for raw in (data.get("results") or [])[:_MAX_RESULTS]:
            twin = _as_twin(raw, item, own_names)
            if twin is not None:
                seen.setdefault(twin["tmdb_id"], twin)

    twins = sorted(seen.values(), key=lambda t: t["tmdb_id"])
    if not twins:
        return []
    # 只给真正的孪生拉一次详情：imdb_id 与 runtime 是判别器要用的证据
    for twin in twins:
        await _load_twin_detail(client, twin)
    return twins


def _as_twin(raw: dict, item: MediaItem, own_names: set[str]) -> dict | None:
    """搜索结果 → 孪生条目；不是同名同年的其它片返回 None。"""
    tmdb_id = raw.get("id")
    if not isinstance(tmdb_id, int) or tmdb_id == item.tmdb_id:
        return None
    release = str(raw.get("release_date") or "")
    year = int(release[:4]) if release[:4].isdigit() else None
    if year is None or item.year is None or abs(year - item.year) > 1:
        return None
    names = {
        normalize_title(name)
        for name in (raw.get("title"), raw.get("original_title"))
        if isinstance(name, str) and name
    }
    if not names & own_names:
        return None
    return {
        "tmdb_id": tmdb_id,
        "title": raw.get("title") or raw.get("original_title") or "",
        "year": year,
        "imdb_id": None,
        "runtime_minutes": None,
    }


async def _load_twin_detail(client, twin: dict) -> None:
    """补齐孪生的 imdb_id 与片长；失败静默（少一条证据而已，不致命）。"""
    try:
        data = await client.get(
            f"movie/{twin['tmdb_id']}", {"append_to_response": "external_ids"}
        )
    except Exception:  # noqa: BLE001 -- 少一条证据不影响"存在孪生"这个结论
        return
    external = data.get("external_ids") or {}
    twin["imdb_id"] = data.get("imdb_id") or external.get("imdb_id") or None
    runtime = data.get("runtime")
    twin["runtime_minutes"] = runtime if isinstance(runtime, int) and runtime > 0 else None


async def ambiguous_verdict(
    session: AsyncSession,
    *,
    subscription_id: int,
    item: MediaItem,
    identity,
    candidate,
    twins: list[dict],
    may_ask: bool = True,
) -> tuple[str, str]:
    """歧义条目遇到"只靠片名年份认出来"的候选时怎么办。

    返回 ``(处置, 理由)``，处置取值：

    - ``"reject"``：有证据表明这个种子属于孪生那一部，直接否决且**不打扰用户**；
    - ``"ask"``：证据不足，停下来问用户（告警在此点亮）。

    ``may_ask=False``：本轮已经为这个订阅问过一次了，判定照做、活动照记，但
    **不再点灯**。一部热门片一批能有几十个候选，逐个点灯等于刷屏。

    走到这里的候选一定**没有外部 ID**：有的话，相等已经在信号一升格成
    ``exact_id``（不进本函数），不等已经被 §5.2 的冲突反证拦下了。所以能用的
    只剩体积与片长。
    """
    from movieclaw_matcher import better_explained_by_twin

    by_id = {t["tmdb_id"]: t for t in twins}
    runtimes = {
        t["tmdb_id"]: t["runtime_minutes"] for t in twins if t.get("runtime_minutes")
    }
    loser = better_explained_by_twin(candidate, identity.runtime_minutes, runtimes)
    if loser is not None:
        other = by_id[loser]
        return "reject", (
            f"体积与片长更像同名同年的《{other['title']}》"
            f"（{other['year']}，tmdb={loser}），不像本条目"
        )

    if may_ask:
        await _ask_user(
            session,
            subscription_id=subscription_id,
            item=item,
            candidate=candidate,
            twins=twins,
        )
    return "ask", "存在同名同年的另一部影片，且没有任何可区分的证据，已暂停并请你确认"


async def _ask_user(
    session: AsyncSession, *, subscription_id: int, item: MediaItem, candidate, twins: list[dict]
) -> None:
    """点亮一条待确认告警。

    复用告警中心而不是新建一套交互：``upsert_notice`` 自带的两条语义正好
    对上——同一 (订阅, 站点, 种子) 只存一行；用户 dismiss（"不是，别再推荐"）
    之后**永久沉默**，不会每轮匹配都来烦一次。

    两个按钮都用现成接口，前端不需要新端点：
    - 「就是这部，下载」→ 手动选种（``POST /subscriptions/{id}/grab``），它本来
      就绕过自动裁决，正是"用户显式选择高于一切"的既有通道；
    - 「不是，别再推荐」→ 告警 dismiss（``POST /system/notices/{id}/dismiss``）。
    """
    from movieclaw_api.services.system_notice import upsert_notice
    from movieclaw_db.models import NoticeSeverity

    others = "、".join(f"《{t['title']}》（{t['year']}，tmdb={t['tmdb_id']}）" for t in twins)
    await upsert_notice(
        session,
        dedupe_key=f"subscription.ambiguous:{subscription_id}:{candidate.site_id}:{candidate.torrent_id}",
        severity=NoticeSeverity.WARNING,
        source="subscription",
        title=f"《{item.title}》有一个候选无法确认是不是你要的影片",
        message=(
            f"存在同名同年的另一部影片：{others}。"
            f"来自 {candidate.site_id} 的「{candidate.title[:80]}」只标了片名和年份，"
            "站点也没有标注影片编号，系统无法区分它属于哪一部，已暂停下载。"
            "确认是你要的那部就点下载，不是的话点忽略，之后不再为这个种子打扰你。"
        ),
        payload={
            "subscription_id": subscription_id,
            "media_item_id": item.id,
            "site_id": candidate.site_id,
            "torrent_id": candidate.torrent_id,
            "torrent_title": candidate.title,
            "twins": twins,
        },
    )
