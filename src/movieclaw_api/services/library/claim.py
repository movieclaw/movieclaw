"""待识别文件的人工认领与身份复核拍板（routes/libraries 的实现层）。

这是识别链「机器认不准 → 人来拍板」的收口：
- ``claim_files``：把一个或一组待识别文件挂到指定 TMDB 条目（单个认领与
  整组认领共用同一段编排，行为差异只在季集号来源）；
- ``resolve_review``：识别器升级后的复核分歧拍板（采纳建议 / 维持现状），
  两种拍板都把身份来源转为 manual——用户看过并决定过的身份，后续识别器
  升级不再提建议。

两条路径都在身份变更后做库存对账（close_fulfilled_wanted）：新条目的
单元"在库"成立，关闭对应的订阅工单。资产补齐（ensure_assets）是后台
任务，由路由层用 BackgroundTasks 调度——本模块只返回需要补的条目 id。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.services import media_discover
from movieclaw_api.services.library.config import LibraryConfigService
from movieclaw_api.services.library.layout import entry_dirs
from movieclaw_api.services.library.nfo import read_entry_identity, rewrite_identity_nfo
from movieclaw_api.services.library.scan import RESOLVER_VERSION, entry_nfo_candidates
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import close_fulfilled_wanted
from movieclaw_db.models import Library, LibraryFile, MediaItem, utcnow
from movieclaw_db.models.library_file import IdentitySource
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind

logger = logging.getLogger("movieclaw_api.library_claim")


async def claim_files(
    session: AsyncSession,
    file_ids: list[int],
    *,
    tmdb_id: int,
    target_kind: MediaKind | None = None,
    explicit_unit: tuple[int | None, int | None] | None = None,
) -> tuple[MediaItem, int, set[int]]:
    """把一组文件认领到指定 TMDB 条目，返回 ``(条目, 认领数, 被腾空的旧条目 id)``。

    认领不只服务"从未识别的文件"——条目详情页的「修正识别结果」把**已识别
    但认错了**的文件改挂过来走的也是这里：两者要落的东西完全相同（人工
    身份 + 纠正矛盾的 NFO + 库存对账），区别只在文件原来有没有锚。改挂会
    让旧条目可能一个文件都不剩，第三个返回值就是给调用方做孤儿清理的。

    季集号来源（单个与整组的唯一差异）：
    - ``explicit_unit`` 给定（单个认领）：用调用方指定的季集号；电影不允许
      带季集号（400）。
    - 缺省（整组认领）：每个文件沿用扫描时从文件名解析出的季集号——这正是
      "一次纠正全生效"能成立的前提（片名认不出不代表季集号也认不出）；
      电影统一用 (0,0) 哨兵。

    约束：一次只能认领同一个媒体库内的文件（409 之外的语义用 400 表达）。
    """
    rows = [row for fid in file_ids if (row := await session.get(LibraryFile, fid))]
    if not rows:
        # 单个认领报出具体 id（便于排障），整组认领用集合措辞
        if len(file_ids) == 1:
            raise NotFoundException(f"台账记录不存在：id={file_ids[0]}")
        raise NotFoundException("这些台账记录都不存在（可能已被认领或忽略）")
    library_ids = {row.library_id for row in rows}
    if len(library_ids) > 1:
        raise BadRequestException("一次只能认领同一个媒体库内的文件")
    library = await LibraryConfigService(session).get(rows[0].library_id)
    kind = MediaKind(library.kind)
    if target_kind is not None and target_kind is not kind:
        raise BadRequestException(
            f"影视条目类型 {target_kind.value} 与媒体库类型 {kind.value} 不一致"
        )
    if explicit_unit is not None and kind is MediaKind.MOVIE and any(explicit_unit):
        raise BadRequestException("电影文件不需要季集号")

    # 经模块属性取 TMDB 客户端（而非 from-import 绑定名），保证测试可打桩
    tmdb = media_discover.get_tmdb_client()
    # 认领的文件就在这个库里，归属库当场定下（设计文档 §14）
    item = await MediaLibraryService(session, tmdb).ensure_media_item(
        kind, tmdb_id, library_id=library.id
    )
    assert item.id is not None
    repo = LibraryFileRepository(session)
    movie = kind is MediaKind.MOVIE
    # 改挂前挂着的其它条目：认领完可能一个文件都不剩，交调用方做孤儿清理
    displaced = {
        row.media_item_id
        for row in rows
        if row.media_item_id is not None and row.media_item_id != item.id
    }
    for row in rows:
        assert row.id is not None
        if explicit_unit is not None:
            season_number, episode_number = explicit_unit
        else:
            season_number = 0 if movie else row.season_number
            episode_number = 0 if movie else row.episode_number
        await repo.claim_identity(
            row.id,
            media_item_id=item.id,
            season_number=season_number,
            episode_number=episode_number,
            resolved_version=RESOLVER_VERSION,
        )
    # 盘上纠错：把与认领结论矛盾的条目 NFO 原地改写。不做这一步，下次
    # 全量重扫会把毒 NFO 的错误身份原样读回（NFO 优先的识别链是自我固化
    # 的），Emby/Jellyfin 也会继续按错误 id 入档——用户的拍板必须落到盘上
    await asyncio.to_thread(_correct_conflicting_nfos, rows, kind, library, item)
    # 库存对账：认领让单元"在库"成立，关闭对应的订阅工单
    await close_fulfilled_wanted(session, item.id)
    await LibraryRepository(session).refresh_stats(library_ids)
    return item, len(rows), displaced


def _correct_conflicting_nfos(
    rows: list[LibraryFile], kind: MediaKind, library: Library, item: MediaItem
) -> None:
    """把认领文件辖域内声明了**其他 tmdbid** 的条目 NFO 改写为认领身份。

    辖域 = 文件自身的同名 NFO + 条目目录（原盘即目录本身）内的条目 NFO；
    再往上的祖先目录 NFO 覆盖着多个条目，即便矛盾也只告警不动手（一份
    出现在分组目录里的 movie.nfo 本身就是异常，改写成单一条目只会更错）。
    类型对不上的 NFO（电影库里的 tvshow.nfo）同样只告警——那是"放错库"
    问题，不是认领能裁决的。同步函数（调用方放线程池）。
    """
    roots = [Path(p) for p in library.root_paths]
    seen: set[Path] = set()
    for row in rows:
        file = Path(row.file_path)
        root = next((r for r in roots if r in file.parents), file.parent)
        is_disc = row.container in ("bluray", "dvd")
        entry = next(iter(entry_dirs(root, file)), None) or (file if is_disc else file.parent)
        for nfo_path in entry_nfo_candidates(kind, root, file, is_disc=is_disc):
            if nfo_path in seen or not nfo_path.is_file():
                continue
            seen.add(nfo_path)
            identity = read_entry_identity(nfo_path)
            if identity is None or identity.tmdb_id in (None, item.tmdb_id):
                continue
            in_scope = nfo_path.parent == entry or (not is_disc and nfo_path.parent == file.parent)
            if identity.kind is not kind or not in_scope:
                logger.warning(
                    "认领纠错跳过 %s：它声明的是 %s tmdbid=%s，超出本次认领的裁决范围",
                    nfo_path,
                    identity.kind.value,
                    identity.tmdb_id,
                )
                continue
            rewrite_identity_nfo(nfo_path, item)


async def resolve_review(
    session: AsyncSession, file_ids: list[int], *, accept: bool
) -> tuple[int, str | None, set[int]]:
    """对复核清单里的文件拍板，返回 ``(处理数, 采纳条目标题, 被腾空的旧条目 id)``。

    - ``accept=True``：改挂到建议条目；
    - ``accept=False``：维持现有身份。
    两种拍板都把身份来源转为 manual 并清掉建议（不再提醒）。
    改挂后对每个新条目做库存对账，关闭订阅工单。
    """
    rows = [row for fid in file_ids if (row := await session.get(LibraryFile, fid))]
    pending = [row for row in rows if row.review_suggestion]
    if not pending:
        raise NotFoundException("这些文件没有待拍板的复核建议（可能已被处理）")
    accepted_items: set[int] = set()
    displaced: set[int] = set()
    title: str | None = None
    for row in pending:
        suggestion = row.review_suggestion or {}
        if accept and suggestion.get("media_item_id"):
            if row.media_item_id is not None and row.media_item_id != suggestion["media_item_id"]:
                displaced.add(row.media_item_id)
            row.media_item_id = suggestion["media_item_id"]
            title = suggestion.get("title") or title
            accepted_items.add(suggestion["media_item_id"])
        row.identity_source = IdentitySource.MANUAL
        row.resolved_version = RESOLVER_VERSION
        row.review_suggestion = None
        row.updated_at = utcnow()
    await session.commit()
    # 库存对账：改挂让新条目的单元"在库"成立，关闭对应的订阅工单
    for item_id in accepted_items:
        await close_fulfilled_wanted(session, item_id)
    await LibraryRepository(session).refresh_stats({row.library_id for row in pending})
    return len(pending), title, displaced
