"""投递前的外部 ID 复核（docs/design/identity-confidence.md §7）。

站点的种子详情页几乎都标了 IMDb 链接，解析器（``nexusphp.py`` 的
``get_torrent_detail`` + ``selectors.py`` 里那条通用的
``a[href*='imdb.com']``）早就写好了——但在本次改动之前，**API 层从未调用过它
一次**，于是 ``site_torrent.imdb_id`` 几乎恒为 NULL，只有 M-Team 因为列表 API
自带该字段才有值。也就是说：站点早就明明白白告诉了我们"这是哪一部电影"，
我们从来没去读。

本模块补上这一次读取，并把结果回填进种子索引——回填是关键，被动匹配下次
遇到同一行就直接走"外部 ID 精确相等"，不必再拉一次详情页。

三条克制：

1. **只在投递前拉，不在匹配时拉**。投递是稀有事件（绝大多数候选在规则过滤
   阶段就被拒了），而且下一步本来就要向同一个站点发请求取 .torrent 文件，
   多这一次的边际成本接近零。
2. **只对电影**。同名同年撞车是电影独有的问题（剧集另有季集号做区分），
   而请求成本恰恰集中在剧集——追新一集一次投递，一季就是几十次多余请求。
   "对 PT 站克制"是本项目的铁律，不能为一个剧集侧几乎用不上的收益去换。
3. **拿不到就放行**。站点抖动、超时、解析失败一律当作"没有这条证据"，绝不
   让一次网络故障卡死投递。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models import SiteTorrent
from movieclaw_matcher import MediaIdentity, TorrentCandidate

logger = logging.getLogger("movieclaw_api.identity_recheck")


def needs_external_id_recheck(candidate: TorrentCandidate, media: MediaIdentity) -> bool:
    """这个候选值不值得为它拉一次详情页。

    条件全部满足才拉：条目自己有外部 ID（否则拿回来也没得比）、候选还没有
    （有的话内核的信号一/冲突反证已经用过了）、且是电影（见模块头第 2 条）。
    """
    if media.kind != "movie":
        return False
    if not (media.imdb_id or media.douban_id):
        return False
    return not (candidate.imdb_id or candidate.douban_id)


async def fetch_external_ids(
    session: AsyncSession, candidate: TorrentCandidate
) -> TorrentCandidate:
    """拉一次种子详情页，把外部 ID 补进候选并回填种子索引。

    拿不到任何 ID（站点没标 / 请求失败 / 解析失败）时**原样返回入参对象**——
    调用方可以用 ``is`` 判断这次复核有没有拿到新证据。
    """
    from dataclasses import replace

    row = (
        await session.execute(
            select(SiteTorrent).where(
                SiteTorrent.site_id == candidate.site_id,
                SiteTorrent.torrent_id == candidate.torrent_id,
            )
        )
    ).scalar_one_or_none()
    # detail_url 优先；缺失时退回种子 ID（M-Team 的详情接口直接吃 ID）
    target = (row.detail_url if row is not None else None) or candidate.torrent_id

    try:
        from movieclaw_api.services.site_access import get_site_access

        site = await get_site_access().get(candidate.site_id)  # 已认证共享实例，勿 close
        detail = await site.get_torrent_detail(target)
    except Exception as exc:  # noqa: BLE001 -- 拿不到证据就放行，绝不卡死投递
        logger.warning(
            "投递前复核未能取到 %s/%s 的详情页（%s），本次跳过外部 ID 校验",
            candidate.site_id,
            candidate.torrent_id,
            exc,
        )
        return candidate

    if not detail.imdb_id and not detail.douban_id:
        return candidate

    # 回填种子索引：这条证据是公共资产，被动匹配下次直接走信号一，全局受益
    if row is not None:
        if detail.imdb_id:
            row.imdb_id = detail.imdb_id
        if detail.douban_id:
            row.douban_id = detail.douban_id
        session.add(row)
        await session.commit()

    logger.info(
        "投递前复核取回外部 ID：%s/%s → imdb=%s douban=%s",
        candidate.site_id,
        candidate.torrent_id,
        detail.imdb_id,
        detail.douban_id,
    )
    return replace(
        candidate,
        imdb_id=detail.imdb_id or candidate.imdb_id,
        douban_id=detail.douban_id or candidate.douban_id,
    )
