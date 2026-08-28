"""「站点取种 → 提交下载器」的公共编排，手动下载与订阅自动投递共用。

流程固定四步：
1. 选定**默认且可用**的下载器（is_default + enabled + 连接测试通过）；
2. 确定保存目录并守门。目录三级取值：调用方给的 ``save_path``（媒体库推导的
   入库路径）> 下载器配置的默认目录 > 下载器自身默认目录。界面上配置的路径
   一律是 **movieclaw 视角**；下载器与 movieclaw 不在同一容器/主机时两边看到
   的路径不同，提交前按下载器配置的路径映射（``path_mappings``，最长前缀优先）
   翻译成下载器视角。**守门**：配了映射但目录不被任何映射覆盖 → 拒绝提交
   （投出去会落进下载器容器内的"黑洞"路径，movieclaw 永远看不到完成的文件）；
3. 通过站点访问管理器拿到已认证的站点客户端，用 download_url 取回 .torrent 字节
   （PT 站点的种子必须带登录态才能下载，不能把 URL 直接丢给下载器）；
4. 提交。幂等：种子已存在时不报错，结果里以 already_exists 标记。

错误统一抛 AppException 子类，消息为可读中文——API 路由直接透传给前端展示，
订阅投递路径捕获后进活动台账，两边都不需要再翻译。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import BadRequestException, UpstreamServiceException
from movieclaw_api.services.site_access import SiteUnavailableError, get_site_access
from movieclaw_db.models import DownloaderClient, DownloadHint, ManualDownloadIntent, utcnow
from movieclaw_db.models.manual_download_intent import MANUAL_DOWNLOAD_INTENT_TTL
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader.factory import create_downloader
from movieclaw_downloader.models import DownloaderConfig, DownloadRequest, SubmitResult
from movieclaw_enrich import enrich
from movieclaw_media.models import MediaKind

if TYPE_CHECKING:
    from movieclaw_api.services.library.resolve import ResolveCandidate

logger = logging.getLogger("movieclaw_api.torrent_submit")

# 身份锚的兜底存活窗口：正常下载远短于此；超窗仍未被入库消费的锚基本是
# 任务已从下载器删除的孤儿行，锚定新下载时顺带清理，避免无限累积
_INTENT_STALE_AFTER = MANUAL_DOWNLOAD_INTENT_TTL


@dataclass(frozen=True)
class ManualTargetResolution:
    """手动种子识别的只读结论。

    预检只负责把搜索结果的标题收敛为 TMDB 身份，不创建媒体条目也不提前
    选择目录。真正提交时会按该 TMDB 锚再次路由并建档，避免前端预检结果
    过期或被篡改后错误投递。
    """

    tmdb_id: int | None
    candidates: list[ResolveCandidate]


async def resolve_manual_target(
    *, kind: str, title: str, year: int, subtitle: str | None = None
) -> ManualTargetResolution:
    """收敛手动搜索结果的身份，副标题中的中文名仅作失败后的备选查询词。"""
    from movieclaw_api.services.library.resolve import LocalEvidence, resolve_with_candidates
    from movieclaw_api.services.media_discover import get_tmdb_client

    subtitle_attrs = enrich(subtitle or "") if subtitle else None
    alt_title = subtitle_attrs.titles_zh[0] if subtitle_attrs and subtitle_attrs.titles_zh else None
    outcome = await resolve_with_candidates(
        get_tmdb_client(),
        MediaKind(kind),
        LocalEvidence(title=title, year=year, alt_title=alt_title),
    )
    return ManualTargetResolution(tmdb_id=outcome.tmdb_id, candidates=outcome.candidates)


def _best_match(
    path: str, mappings: list[dict[str, str]], *, source_key: str, target_key: str
) -> tuple[str, str] | None:
    """在映射表里找 path 的最长前缀命中，返回（命中前缀, 对端前缀）。

    前缀必须落在路径分隔符边界上（``/data/downloads`` 不会误配
    ``/data/downloads2``）；无命中返回 None。source_key/target_key
    决定翻译方向（local→remote 或 remote→local）。
    """
    best: tuple[str, str] | None = None
    for mapping in mappings:
        source = (mapping.get(source_key) or "").rstrip("/")
        target = (mapping.get(target_key) or "").rstrip("/")
        if not source or not target:
            continue
        if (path == source or path.startswith(source + "/")) and (
            best is None or len(source) > len(best[0])
        ):
            best = (source, target)
    return best


def translate_save_path(
    path: str | None, mappings: list[dict[str, str]] | None
) -> str | None:
    """把 movieclaw 视角的保存目录翻译成下载器视角（最长前缀匹配）。

    映射形如 ``[{"local": "/data/downloads", "remote": "/downloads"}]``。
    未命中任何映射时原样返回——视角一致的部署（映射为空）零影响；
    配了映射却未覆盖的路径由 ``mapping_covers`` 在提交前拦截。
    """
    if not path or not mappings:
        return path
    best = _best_match(path, mappings, source_key="local", target_key="remote")
    if best is None:
        return path
    local, remote = best
    return remote + path[len(local) :]


def translate_to_local(
    path: str | None, mappings: list[dict[str, str]] | None
) -> str | None:
    """反向翻译：把下载器上报的路径翻译回 movieclaw 视角（最长前缀匹配）。

    救援巡检核验落点用。未命中原样返回（视角一致部署）。
    """
    if not path or not mappings:
        return path
    best = _best_match(path, mappings, source_key="remote", target_key="local")
    if best is None:
        return path
    remote, local = best
    return local + path[len(remote) :]


def mapping_covers(path: str, mappings: list[dict[str, str]]) -> bool:
    """movieclaw 视角的路径是否被某条映射的 local 前缀覆盖。"""
    return _best_match(path, mappings, source_key="local", target_key="remote") is not None


async def submit_torrent(
    session: AsyncSession,
    *,
    site_id: str,
    download_url: str | None,
    tags: list[str],
    save_path: str | None = None,
    subtitle: str | None = None,
    downloader_id: int | None = None,
    category: str = "movieclaw",
) -> tuple[SubmitResult, DownloaderClient]:
    """从站点取回种子并提交到下载器，返回（提交结果, 所用下载器记录）。

    tags 用于区分来源（如手动 movieclaw-manual / 订阅 movieclaw-sub），
    方便用户在下载器里筛选。save_path 由调用方按媒体库推导（缺省回落
    下载器配置的默认目录）。subtitle 是种子副标题：与库推导的 save_path
    同时在场时落一条 download_hint，供扫描器识别时取用（副标题里的中文
    片名是拼音命名种子唯一可用的查询词）——调用方只在 save_path 为
    **条目级**目录时传入，锚到库主根会波及根下所有文件。
    downloader_id 指定投递目标（手动下载配了多台按需分流用）；缺省仍走
    默认下载器——订阅投递路径不传该参数，行为不变。
    category 是下载器分类：媒体下载固定 movieclaw；刷流传 movieclaw-boost
    与媒体任务隔离（不进监听导入的视野）。
    """
    if not download_url:
        raise BadRequestException("该种子没有可用的下载入口（download_url 缺失）")

    # 1. 选下载器（先于取种：注定投不出去时不白打站点请求）。
    # 指定 id 时要求该台"启用 + 验证通过"，否则给指向明确的中文错误
    if downloader_id is not None:
        row = await session.get(DownloaderClient, downloader_id)
        if row is None:
            raise BadRequestException(f"下载器不存在：#{downloader_id}")
        if not row.enabled or row.status != ConfigStatus.ACTIVE:
            raise BadRequestException(
                f"下载器「{row.name}」当前不可用（已停用或连接验证未通过），"
                "请在「设置 → 下载器」里检查后重试，或改用其他下载器"
            )
    else:
        result = await session.execute(
            select(DownloaderClient).where(
                DownloaderClient.is_default.is_(True),  # type: ignore[attr-defined]
                DownloaderClient.enabled.is_(True),  # type: ignore[attr-defined]
                DownloaderClient.status == ConfigStatus.ACTIVE,
            )
        )
        row = result.scalars().first()
    if row is None:
        raise BadRequestException("没有可用的默认下载器（请在「设置 → 下载器」里添加并设为默认）")

    # 2. 保存目录（库推导路径 > 下载器配置默认目录，均为 movieclaw 视角）+
    # 守门：下载器配了路径映射（跨容器部署的声明），保存目录却不在任何映射
    # 覆盖范围内 → 下载器大概率无法访问，投出去会落进容器黑洞（下载器在
    # 自己文件系统里凭空创建该路径），movieclaw 永远看不到完成的文件。
    # 拒绝并给出可操作的中文指引，比静默挂起好得多
    effective_save_path = save_path or row.save_path
    if (
        effective_save_path
        and row.path_mappings
        and not mapping_covers(effective_save_path, row.path_mappings)
    ):
        raise BadRequestException(
            f"保存目录 {effective_save_path} 不在下载器「{row.name}」的路径映射覆盖范围内，"
            "下载器可能无法访问该目录——请在「设置 → 下载器」为它补一条路径映射"
            "（下载器实际可直达同名路径时，添加一条两边相同的映射即可），"
            "或改用监听导入规则"
        )
    submit_save_path = translate_save_path(effective_save_path, row.path_mappings)

    # 3. 站点取种：站点不可用（未配置/停用/未验证）是配置问题，站点请求失败是上游问题
    try:
        site = await get_site_access().get(site_id)
    except SiteUnavailableError as exc:
        raise BadRequestException(str(exc)) from exc
    try:
        torrent_bytes = await site.download_torrent(download_url)
    except Exception as exc:
        raise UpstreamServiceException(f"从站点 {site_id} 取回种子失败：{exc}") from exc

    # 4. 提交（幂等，重复种子不报错）
    repo = DownloaderRepository(session)
    config = DownloaderConfig(
        type=row.client_type.value,
        url=row.url,
        username=row.username,
        password=repo.decrypted_password(row),
    )
    downloader = create_downloader(config)
    try:
        submit_result = await downloader.submit(
            DownloadRequest(
                torrent_bytes=torrent_bytes,
                save_path=submit_save_path,
                category=category,
                tags=tags,
            )
        )
        # 「已存在」且是刷流引擎自己抢下的种子 → 接管：把数据迁到本次请求
        # 的目标目录（否则文件留在刷流目录，入库监听永远看不见），台账转出。
        # 迁移失败不连累提交——留给刷流的认领转出兜底，订阅按"非自有任务"
        # 的既有路径处理
        if submit_result.already_exists and category != "movieclaw-boost":
            submit_result = await _reclaim_boost_task(
                session, downloader, submit_result, save_path=submit_save_path
            )
    except Exception as exc:
        raise UpstreamServiceException(f"提交到下载器「{row.name}」失败：{exc}") from exc
    finally:
        await downloader.close()

    # 目录日志：有映射翻译时同时打两个视角，方便核对跨容器部署是否配对
    if submit_save_path != effective_save_path:
        dir_text = f"{effective_save_path} →（映射）{submit_save_path}"
    else:
        dir_text = effective_save_path or "（下载器默认）"
    logger.info(
        "种子已提交到下载器「%s」：site=%s name=%s hash=%s 已存在=%s 目录=%s",
        row.name,
        site_id,
        submit_result.name,
        submit_result.info_hash,
        submit_result.already_exists,
        dir_text,
    )

    # 5. 落下载线索：只锚调用方给的库推导目录（下载器默认目录不在库根、
    # 扫描器看不见）。提交已成功，线索写失败只损失识别信号，不能连累提交。
    if save_path and subtitle and subtitle.strip():
        try:
            await _upsert_hint(
                session, save_path=save_path, subtitle=subtitle.strip(), site_id=site_id
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "下载线索写入失败（目录 %s），副标题识别信号将缺失", save_path, exc_info=True
            )
    return submit_result, row


async def _reclaim_boost_task(
    session: AsyncSession,
    downloader,  # noqa: ANN001 -- BaseDownloader，避免顶层引入适配器依赖
    submit_result: SubmitResult,
    *,
    save_path: str | None,
) -> SubmitResult:
    """已存在的任务若是刷流引擎抢下的，就地接管：迁目录 + 台账转出。

    这是「刷流先抢、订阅/手动后到」碰撞的正确收尾（docs/design/
    site-protection-ratio-boost.md 2.5）：数据迁到媒体的目标目录后，入库
    监听就能看见它——完整的种子往往已经下载完成，相当于零流量秒到。
    迁移成功返回带 ``reclaimed_from_boost=True`` 的新结果；不满足条件或
    迁移失败原样返回（刷流侧的认领转出仍会兜底所有权，不留双头管理）。
    """
    from movieclaw_db.models import BoostTaskState, RatioBoostTask

    if not submit_result.info_hash:
        return submit_result
    task = (
        await session.execute(
            select(RatioBoostTask).where(
                RatioBoostTask.info_hash == submit_result.info_hash.lower(),
                RatioBoostTask.state == BoostTaskState.ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        return submit_result  # 已存在的任务不是刷流的（用户自己加的），不碰
    if save_path:
        try:
            await downloader.set_location(submit_result.info_hash, save_path)
        except Exception:  # noqa: BLE001 -- 迁移失败不连累提交，走既有兜底路径
            logger.warning(
                "刷流任务接管时迁移目录失败（hash=%s → %s），保留原目录",
                submit_result.info_hash,
                save_path,
                exc_info=True,
            )
            return submit_result
    task.state = BoostTaskState.MISSING
    task.evicted_at = utcnow()
    task.evict_reason = "已被订阅/手动下载接管，数据已迁至媒体目标目录"
    task.updated_at = utcnow()
    await session.commit()
    logger.info(
        "刷流任务已被接管：%s（hash=%s，目录迁至 %s）",
        task.title,
        submit_result.info_hash,
        save_path or "（下载器默认）",
    )
    return submit_result.model_copy(update={"reclaimed_from_boost": True})


async def anchor_manual_download(
    session: AsyncSession,
    *,
    info_hash: str | None,
    media_item_id: int,
    library_id: int,
    site_id: str,
    torrent_id: str | None = None,
    downloader_id: int | None = None,
    download_name: str | None = None,
    save_path: str | None = None,
) -> None:
    """按 infohash 保存手动下载的已确认身份，供监听导入完成后直接认领。

    下载器已存在同一任务时会返回同一个 hash。此时保留首次提交时确认的
    身份/库，不能让一次后来的重复点击覆盖原任务的入库归属。
    """
    if not info_hash:
        logger.warning("手动下载未返回 infohash，监听导入将无法复用已确认身份")
        return
    # 顺带清理超窗孤儿锚（任务下载中途被从下载器删除时，没有入库时刻来
    # 消费它）。跟随本次锚定同一事务提交，失败回滚也只是留到下次再清。
    await session.execute(
        delete(ManualDownloadIntent)
        .where(ManualDownloadIntent.created_at < utcnow() - _INTENT_STALE_AFTER)  # type: ignore[arg-type]
        .execution_options(synchronize_session=False)
    )
    normalized = info_hash.lower()
    existing = (
        await session.execute(
            select(ManualDownloadIntent).where(ManualDownloadIntent.info_hash == normalized)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            ManualDownloadIntent(
                info_hash=normalized,
                media_item_id=media_item_id,
                library_id=library_id,
                downloader_id=downloader_id,
                download_name=download_name,
                save_path=save_path,
                site_id=site_id,
                torrent_id=torrent_id,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            # 下载器的重复提交已是幂等的；两次请求恰好同时经过上面的查询时，
            # 第二个写入也必须保持同样语义，不能把「任务已成功加入」报成 500。
            await session.rollback()
            existing = (
                await session.execute(
                    select(ManualDownloadIntent).where(ManualDownloadIntent.info_hash == normalized)
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
        if existing is None:
            logger.info(
                "已锚定手动下载身份：hash=%s media_item=%s library=%s",
                normalized,
                media_item_id,
                library_id,
            )
            return
    # 同一 hash 即同一份内容：只补旧版本没有的物理任务锚与站点种子 ID，
    # 绝不改写首次确认的媒体/库归属。路径只有目标身份也一致时才可信——同一
    # hash 被后来点到另一部片时，不能把旧任务静默指向新目录。
    changed = False
    same_target = existing.media_item_id == media_item_id and existing.library_id == library_id
    if same_target:
        if existing.downloader_id is None and downloader_id is not None:
            existing.downloader_id = downloader_id
            changed = True
        if not existing.download_name and download_name:
            existing.download_name = download_name
            changed = True
        if not existing.save_path and save_path:
            existing.save_path = save_path
            changed = True
    if (
        torrent_id
        and not existing.torrent_id
        and (existing.site_id is None or existing.site_id == site_id)
    ):
        existing.site_id = site_id
        existing.torrent_id = torrent_id
        changed = True
    if changed:
        existing.updated_at = utcnow()
        await session.commit()
    if not same_target:
        logger.warning(
            "手动下载 hash=%s 已锚定到 media_item=%s/library=%s；"
            "忽略本次不同目标 media_item=%s/library=%s",
            normalized,
            existing.media_item_id,
            existing.library_id,
            media_item_id,
            library_id,
        )


async def _upsert_hint(
    session: AsyncSession, *, save_path: str, subtitle: str, site_id: str
) -> None:
    """按目录幂等落线索：同一条目重复提交（换版本重下）覆盖为最新副标题。"""
    result = await session.execute(select(DownloadHint).where(DownloadHint.save_path == save_path))
    existing = result.scalars().first()
    if existing is None:
        session.add(DownloadHint(save_path=save_path, subtitle=subtitle, site_id=site_id))
    else:
        existing.subtitle = subtitle
        existing.site_id = site_id
        existing.updated_at = utcnow()
    await session.commit()
