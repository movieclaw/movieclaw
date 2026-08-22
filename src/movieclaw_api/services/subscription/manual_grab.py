"""人工选择种子下载：把搜索结果里的一条种子直接投给指定订阅。

自动管线（被动匹配/主动搜索）之外的第三条投递路径——用户在搜索页看到了
一个合适的种子，不必再等规则组恰好放行：**手动选种即用户对规则的显式覆盖，
跳过规则过滤直接投递**。但身份匹配不跳过：种子必须能被确认为本条目、且
覆盖至少一个当前缺口或可洗版的已入库单元（规则组配了洗版目标时），否则给
可读中文错误——手动通道解决"规则太严"，不解决"下错片"。

洗版承接（quality-upgrade.md §13.8）：无缺口时手选即"人工换版本"——投递
带 ``manual`` 标记走 purpose="upgrade" 通道，入库后实测验证裁决：证明更优
则正常替换旧版，证明不了则新旧版本**共存保留**（用户显式挑的文件绝不被
系统丢弃，也不计熔断/排除清单）。

复用点（与自动管线同一套原语，不存在第二种口径）：
- attrs 优先用前端回传的搜索结果解析——它本就是搜索链路里服务端 enrich
  的现算产物，用户按它筛选后才选中这条；缺失时服务端重新推导兜底
  （title 本身就来自调用方，重算并无额外信任增益，回传反而保证"所见即所投"）；
- 身份匹配 match_identity + 覆盖展开 covered_units（发布时间上限同样生效）；
- 投递走 dispatch()（三级兜底、失败回滚、GRABBED 活动、救援巡检全部继承，
  source="手动选种" 让时间线可区分人与机器的动作）。
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
)
from movieclaw_db.models import (
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionStatus,
    WantedItem,
    WantedStatus,
)
from movieclaw_enrich import enrich
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import (
    MediaIdentity,
    QualitySnapshot,
    RuleSetSpec,
    RuleVerdict,
    TorrentCandidate,
    match_identity,
    quality_label,
)

logger = logging.getLogger("movieclaw_api.subscription_manual_grab")


async def grab_manual(
    session: AsyncSession,
    subscription_id: int,
    *,
    site_id: str,
    torrent_id: str,
    title: str,
    subtitle: str = "",
    category: str | None = None,
    attrs: dict | None = None,
    download_url: str | None = None,
    size_bytes: int | None = None,
    seeders: int | None = None,
    is_free: bool | None = None,
    hit_and_run: bool | None = None,
    imdb_id: str | None = None,
    douban_id: str | None = None,
    publish_time: datetime | None = None,
) -> list[WantedItem]:
    """把一条种子手动投给订阅，返回它满足的工单列表。

    交互式搜索的结果现算现返、不落种子索引，因此种子字段由前端原样回传
    （与 dl.submit 拿 download_url 的信任模型一致——调用方本就是管理员）。
    """
    from movieclaw_api.services.subscription.dispatch import dispatch
    from movieclaw_api.services.subscription.matching import (
        covered_units,
        publish_calendar_date,
    )

    subscription = await session.get(Subscription, subscription_id)
    if subscription is None:
        raise NotFoundException(f"订阅不存在：#{subscription_id}")
    if subscription.status == SubscriptionStatus.PAUSED:
        raise BadRequestException("订阅已暂停，请先恢复追踪再手动选种")
    item = await session.get(MediaItem, subscription.media_item_id)
    assert item is not None  # 外键保证

    open_wanted = {
        (w.season_number, w.episode_number): w
        for w in (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == subscription_id,
                    WantedItem.status == WantedStatus.WANTED,
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    }

    # 洗版承接（quality-upgrade.md §13.8）：规则组配了洗版目标时，已入库
    # 单元也是手选的合法目标——「无法确认」的单元靠这条路人工替换。投递带
    # manual 标记，入库后由实测验证裁决：证明更优则替换旧版，否则共存保留
    spec: RuleSetSpec | None = None
    rule_set = await session.get(RuleSet, subscription.rule_set_id)
    if rule_set is not None:
        try:
            spec = RuleSetSpec.model_validate(rule_set.spec or {})
        except ValueError:
            spec = None
    upgrade_pool: dict[tuple[int, int], WantedItem] = {}
    if spec is not None and spec.upgrade_source is not None:
        upgrade_pool = {
            (w.season_number, w.episode_number): w
            for w in (
                await session.execute(
                    select(WantedItem).where(
                        WantedItem.subscription_id == subscription_id,
                        WantedItem.status == WantedStatus.IMPORTED,
                        WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    )
                )
            )
            .scalars()
            .all()
        }
    if not open_wanted and not upgrade_pool:
        if spec is None or spec.upgrade_source is None:
            raise BadRequestException(
                f"《{item.title}》当前没有缺口——所有追踪项都已投递或入库。"
                "若想手选替换已入库的版本，请先给订阅换用配置了洗版目标的规则组"
            )
        raise BadRequestException(
            f"《{item.title}》当前没有缺口，也没有已入库的追踪项可供替换"
        )

    # attrs 是前端原样回传的解析结果：结构不合法（陈旧客户端/字段漂移）不算
    # 致命错误，退回服务端 enrich 重新推导兜底，别把 500 甩给用户
    parsed_attrs: TorrentAttrs | None = None
    if attrs:
        try:
            parsed_attrs = TorrentAttrs.model_validate(attrs)
        except ValidationError:
            logger.warning(
                "手动选种回传的种子属性无法解析，改用服务端重新推导：site=%s torrent=%s",
                site_id,
                torrent_id,
            )
    if parsed_attrs is None:
        parsed_attrs = enrich(title, subtitle, category)

    candidate = TorrentCandidate(
        site_id=site_id,
        torrent_id=torrent_id,
        title=title,
        subtitle=subtitle,
        attrs=parsed_attrs,
        imdb_id=imdb_id,
        douban_id=douban_id,
        size_bytes=size_bytes,
        seeders=seeders,
        is_free=is_free,
        hit_and_run=hit_and_run,
        download_url=download_url,
        publish_time=publish_time,
    )
    identity = MediaIdentity(
        kind=item.kind,
        year=item.year,
        aliases=tuple(item.aliases),
        imdb_id=item.imdb_id,
        douban_id=item.douban_id,
        season_numbers=tuple(sorted({s for s, _ in open_wanted} | {s for s, _ in upgrade_pool})),
    )
    match = match_identity(candidate, identity)
    if match is None:
        raise BadRequestException(
            f"无法确认「{title[:60]}」就是《{item.title}》"
            f"{f'（{item.year}）' if item.year else ''}——种子名里识别不出对应的"
            "片名/年份/季集信息。请核对种子标题，若确属本条目但命名过于特殊，"
            "可改用「一键下载」手动选择保存目录"
        )

    published = publish_calendar_date(publish_time)
    covered = covered_units(match, open_wanted, published=published)
    upgrade_covered = covered_units(match, upgrade_pool, published=published)
    if not covered and not upgrade_covered:
        targets = sorted(open_wanted) + sorted(upgrade_pool)
        target_text = "、".join(
            "正片" if s == 0 and e == 0 else f"S{s:02d}E{e:02d}" for s, e in targets[:8]
        )
        raise BadRequestException(
            f"该种子识别为{_match_text(match)}，但可投递的追踪项是 {target_text}"
            f"{'…' if len(targets) > 8 else ''}——没有可满足的追踪项"
        )
    upgrade_labels: tuple[str, str] | None = None
    if upgrade_covered:
        upgrade_labels = (
            quality_label(
                QualitySnapshot.model_validate(upgrade_covered[0].quality or {}), spec
            ),
            quality_label(parsed_attrs, spec),
        )

    # 手动选种 = 用户显式覆盖规则组：verdict 直接放行（score=0 只进活动 payload）
    done = await dispatch(
        session,
        subscription=subscription,
        item=item,
        wanted_rows=covered,
        candidate=candidate,
        verdict=RuleVerdict(accepted=True, score=0),
        source="手动选种",
        upgrade_rows=upgrade_covered,
        upgrade_labels=upgrade_labels,
        manual=True,
    )
    if not done:
        raise ConflictException(
            "这些追踪项刚被自动管线抢先投递或已在洗版中，无需重复操作——详见活动记录"
        )
    return covered + upgrade_covered


def _match_text(match) -> str:  # noqa: ANN001 -- IdentityMatch
    """身份匹配结果 → 中文描述（错误提示用）。"""
    if match.is_complete_series:
        return "全集包"
    parts = [f"S{s:02d}E{e:02d}" for s, e in sorted(match.episodes)]
    parts.extend(f"第 {s} 季整季包" for s in sorted(match.pack_seasons))
    return "、".join(parts) if parts else "未知单元"
