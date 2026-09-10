"""影片分享（docs/design/media-share.md）：管理侧接口与访客侧接口。

两个路由器：

- ``admin_router``（管理区，挂 ``require_admin``）：条目的分享创建 / 查询 /
  取消，以及全部有效分享的列表（媒体库管理页「分享」标签）；
- ``public_router``（公开区，``/share/{slug}/…``）：访客通道。每个端点都挂
  ``require_share_access``——它是**唯一**产出分享主体的地方，分享凭据到不了
  任何既有业务接口（``require_login`` 一行不动）。

访客端点刻意「转调既有路由函数」而不是重写：换成分享主体、断言条目、改写
图片地址，三件事之外全部照旧——起播决策、字幕策略、章节图这些后续演进，
分享页自动跟上，不会出现两套代码各改各的。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Path, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.api.deps import require_admin
from movieclaw_api.api.routes import images as images_routes
from movieclaw_api.api.routes import libraries as libraries_routes
from movieclaw_api.api.routes import playback as playback_routes
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import AppException, NotFoundException
from movieclaw_api.schemas.library import (
    LibraryItemDetailView,
    LocalMetaView,
    SeasonEpisodesView,
)
from movieclaw_api.schemas.playback import (
    PlaybackDecideRequest,
    PlaybackDecisionView,
    PlaybackDiagnosticsView,
    PlaybackItemView,
    PlaybackProgressRequest,
    PlaybackSessionRequest,
    PlaybackSessionView,
    PlaybackStateView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.schemas.share import (
    ShareCreateRequest,
    SharedCollectionItemView,
    SharedCollectionView,
    SharedFileView,
    SharedItemView,
    SharePublicView,
    ShareUnlockRequest,
    ShareView,
)
from movieclaw_api.services import media_scrape
from movieclaw_api.services import share as share_service
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.image_variants import ImageVariant
from movieclaw_api.services.library.access import (
    assert_item_visible,
    assert_library_visible,
)
from movieclaw_api.services.library.collections import count_members, resolve_members
from movieclaw_api.services.playback import watch as playback_watch
from movieclaw_db.engine import get_session
from movieclaw_db.models import Collection, LibraryFile, MediaItem
from movieclaw_db.models.media_share import MediaShare
from movieclaw_db.repositories.media_repo import MediaItemRepository
from movieclaw_media.models import MediaKind
from movieclaw_playback import activity

logger = logging.getLogger(__name__)

admin_router = APIRouter(tags=["shares"])
public_router = APIRouter(prefix="/share", tags=["shares"])


# ---------------------------------------------------------------------------
# 管理侧
# ---------------------------------------------------------------------------


async def _poster_url(session: AsyncSession, item: MediaItem) -> str | None:
    """列表用的海报：本地刮削资产 > TMDB 图床（与播放页条目信息同两层）。"""
    assert item.id is not None
    meta_row = await MediaItemRepository(session).get_metadata(item.id)
    if meta_row is not None and meta_row.poster_file:
        version = media_scrape.asset_version(meta_row.poster_file)
        return f"/images/assets/{meta_row.poster_file}?v={version}"
    if item.poster_path:
        base = get_settings().tmdb_image_base_url.rstrip("/")
        return f"{base}/w500{item.poster_path}"
    return None


async def _share_view(session: AsyncSession, row: MediaShare) -> ShareView:
    assert row.id is not None
    common = {
        "id": row.id,
        "slug": row.slug,
        "url": await share_service.share_url(row.slug),
        "library_id": row.library_id,
        "password": share_service.password_of(row),
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "view_count": row.view_count,
        "last_accessed_at": row.last_accessed_at,
    }
    if row.collection_id is not None:
        collection = await session.get(Collection, row.collection_id)
        if collection is None:
            raise NotFoundException("合集不存在（可能已被删除）")
        return ShareView(
            collection_id=row.collection_id,
            title=collection.name,
            # 数量是现算的：规则驱动的合集会自己长，管理列表上写死一个数字
            # 只会与访客看到的对不上
            item_count=await count_members(session, collection),
            **common,  # type: ignore[arg-type]
        )
    item = await session.get(MediaItem, row.media_item_id)
    if item is None:
        raise NotFoundException("媒体条目不存在（可能已被删除）")
    return ShareView(
        media_item_id=row.media_item_id,
        title=item.title,
        kind=MediaKind(item.kind),
        year=item.year,
        poster_url=await _poster_url(session, item),
        **common,  # type: ignore[arg-type]
    )


async def _assert_item_in_library(
    session: AsyncSession, principal: Principal, library_id: int, media_item_id: int
) -> None:
    """条目必须在这个库里有台账行——分享出去的是「这个库里的这部片」。"""
    await assert_library_visible(session, principal, library_id)
    await libraries_routes._item_rows(session, library_id, media_item_id)


@admin_router.get(
    "/libraries/{library_id}/items/{media_item_id}/share",
    response_model=ApiResponse[ShareView | None],
    summary="看这部影片当前的分享链接、密码和有效期",
    operation_id="library.items.share.get",
)
async def get_item_share(
    library_id: int,
    media_item_id: int,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ShareView | None]:
    """这部影片有没有正在生效的分享，有的话链接是什么。

    返回链接地址、访问密码（原文）、到期时间、被打开过多少次、最后一次是
    什么时候。没有有效分享时返回 null。
    """
    await _assert_item_in_library(session, principal, library_id, media_item_id)
    row = await share_service.get_active_for_item(session, media_item_id)
    return ok(await _share_view(session, row) if row else None)


@admin_router.post(
    "/libraries/{library_id}/items/{media_item_id}/share",
    response_model=ApiResponse[ShareView],
    summary="生成一条观看链接，发给没有账号的人也能看",
    operation_id="library.items.share.create",
)
async def create_item_share(
    library_id: int,
    media_item_id: int,
    payload: ShareCreateRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ShareView]:
    """为这部影片生成一个公开链接，拿到链接的人不用注册、不用登录就能看。

    ``expires_in_days`` 是有效期，只有 1 / 3 / 7 / 30 四档（默认 7），没有
    「永久」——分享是临时授权，过期自动失效；``password`` 给链接再加一道密码，
    不填就是任何人拿到链接都能看。

    这部片已经有一条有效分享时原样返回它，不会重复生成（响应 code 为
    ``SHARE_EXISTS``）。想换一条新链接，先 ``library.items.share.revoke``。
    """
    await _assert_item_in_library(session, principal, library_id, media_item_id)
    row, created = await share_service.create_share(
        session,
        media_item_id=media_item_id,
        library_id=library_id,
        expires_in_days=payload.expires_in_days,
        password=payload.password,
        created_by_member_id=principal.member_id if principal.member_id is not None else 0,
    )
    view = await _share_view(session, row)
    if not created:
        return ok(view, code="SHARE_EXISTS", message="这部影片已有一条有效分享")
    logger.info(
        "创建影片分享：%s（条目 %d，%d 天）", row.slug, media_item_id, payload.expires_in_days
    )
    return ok(view, message="分享链接已生成")


@admin_router.delete(
    "/libraries/{library_id}/items/{media_item_id}/share",
    response_model=ApiResponse[dict],
    summary="取消这部影片的分享，链接立刻失效",
    operation_id="library.items.share.revoke",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def revoke_item_share(
    library_id: int,
    media_item_id: int,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """撤掉这部影片的分享：已经发出去的链接立刻打不开，正在看的人也会中断。

    影片本身与观看记录都不受影响，需要时可以再生成一条新的。重复取消不报错。
    """
    await _assert_item_in_library(session, principal, library_id, media_item_id)
    row = await share_service.get_active_for_item(session, media_item_id)
    if row is not None:
        await share_service.revoke(session, row)
        logger.info("取消影片分享：%s（条目 %d）", row.slug, media_item_id)
    return ok({"revoked": row is not None}, message="分享已取消")


async def _collection_for_share(
    session: AsyncSession, principal: Principal, collection_id: int
) -> Collection:
    """分享一个合集的前置：合集要存在、对操作者可见。

    **不要求它是手动合集**：规则驱动的合集分享出去之后会自己长，那正是这条
    链接比"一串条目"有意思的地方（新入库的片自动出现在朋友那边）。
    """
    row = await session.get(Collection, collection_id)
    if row is None:
        raise NotFoundException("合集不存在（可能已被删除）")
    if row.library_id is not None:
        await assert_library_visible(session, principal, row.library_id)
    return row


@admin_router.get(
    "/collections/{collection_id}/share",
    response_model=ApiResponse[ShareView | None],
    summary="看这个合集当前的分享链接、密码和有效期",
    operation_id="collection.share.get",
)
async def get_collection_share(
    collection_id: int,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ShareView | None]:
    await _collection_for_share(session, principal, collection_id)
    row = await share_service.get_active_for_collection(session, collection_id)
    return ok(await _share_view(session, row) if row else None)


@admin_router.post(
    "/collections/{collection_id}/share",
    response_model=ApiResponse[ShareView],
    summary="把整个合集分享出去，拿到链接的人不用登录就能看",
    operation_id="collection.share.create",
)
async def create_collection_share(
    collection_id: int,
    payload: ShareCreateRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ShareView]:
    """分享的是**这个合集此刻的成员**，而且会跟着合集一起变。

    规则驱动的合集分享出去之后新入库的片自动出现在对方那边；从合集里移出去
    的片立刻打不开，不需要再来取消一次。有效期与密码两项与影片分享同一套。
    """
    collection = await _collection_for_share(session, principal, collection_id)
    row, created = await share_service.create_share(
        session,
        collection_id=collection_id,
        library_id=collection.library_id,
        expires_in_days=payload.expires_in_days,
        password=payload.password,
        created_by_member_id=principal.member_id if principal.member_id is not None else 0,
    )
    view = await _share_view(session, row)
    if not created:
        return ok(view, code="SHARE_EXISTS", message="这个合集已有一条有效分享")
    logger.info(
        "创建合集分享：%s（合集 %d，%d 天）", row.slug, collection_id, payload.expires_in_days
    )
    return ok(view, message="分享链接已生成")


@admin_router.delete(
    "/collections/{collection_id}/share",
    response_model=ApiResponse[dict],
    summary="取消这个合集的分享，链接立刻失效",
    operation_id="collection.share.revoke",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def revoke_collection_share(
    collection_id: int,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    await _collection_for_share(session, principal, collection_id)
    row = await share_service.get_active_for_collection(session, collection_id)
    if row is not None:
        await share_service.revoke(session, row)
        logger.info("取消合集分享：%s（合集 %d）", row.slug, collection_id)
    return ok({"revoked": row is not None}, message="分享已取消")


@admin_router.get(
    "/shares",
    response_model=ApiResponse[list[ShareView]],
    summary="列出当前所有还有效的分享链接",
    operation_id="shares.list",
)
async def list_shares(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ShareView]]:
    """一次看清「我到底把哪些片分享出去了」。

    每条给出片名、链接、有没有密码、什么时候到期、被打开过多少次。已过期的
    分享自动失效并从这里消失，不需要手动清理。
    """
    views: list[ShareView] = []
    for row in await share_service.list_active(session):
        try:
            views.append(await _share_view(session, row))
        except NotFoundException:
            continue  # 条目刚被删、级联还没落地的瞬时态
    return ok(views)


@admin_router.delete(
    "/shares/{share_id}",
    response_model=ApiResponse[dict],
    summary="按 id 取消一条分享，链接立刻失效",
    operation_id="shares.revoke",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def revoke_share(
    share_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    """按分享 id 撤掉一条分享，链接立刻失效。

    与 ``library.items.share.revoke`` 等价，用在从 ``shares.list`` 里挑一条
    直接撤掉的场景。
    """
    row = await session.get(MediaShare, share_id)
    if row is None:
        raise NotFoundException("分享不存在")
    await share_service.revoke(session, row)
    logger.info("取消影片分享：%s（条目 %d）", row.slug, row.media_item_id)
    return ok({"revoked": True}, message="分享已取消")


# ---------------------------------------------------------------------------
# 访客侧
# ---------------------------------------------------------------------------


async def _load_share(session: AsyncSession, slug: str) -> MediaShare:
    """按 slug 取分享；不存在 / 已取消 → SHARE_NOT_FOUND，已过期 → SHARE_EXPIRED。

    两个 code 都是 404：过期与不存在对探测者一视同仁，只是给真正拿到链接的
    人一句更准确的话。
    """
    row = await share_service.get_by_slug(session, slug)
    if row is None or row.revoked_at is not None:
        raise AppException(status_code=404, code="SHARE_NOT_FOUND", message="分享不存在或已取消")
    if not share_service.is_active(row):
        raise AppException(status_code=404, code="SHARE_EXPIRED", message="分享已过期")
    return row


async def _unlocked(row: MediaShare, cookie: str | None) -> bool:
    if not row.password_encrypted:
        return True
    return bool(cookie) and await share_service.verify_unlock_token(cookie or "", row)


async def require_share_access(
    slug: Annotated[str, Path(max_length=32)],
    unlock_cookie: str | None = Cookie(default=None, alias=share_service.SHARE_COOKIE_NAME),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """访客端点的鉴权依赖：分享有效且（有密码时）本浏览器已解锁，产出分享主体。"""
    row = await _load_share(session, slug)
    if not await _unlocked(row, unlock_cookie):
        raise AppException(status_code=401, code="SHARE_LOCKED", message="请先输入分享密码")
    return share_service.share_principal(row)


def _grant(principal: Principal):
    assert principal.share is not None
    return principal.share


@public_router.get(
    "/{slug}",
    response_model=ApiResponse[SharePublicView],
    summary="分享探针：要不要密码、本浏览器是否已解锁",
    operation_id="share.probe",
    openapi_extra={"x-cli-hidden": True},
)
async def probe_share(
    slug: Annotated[str, Path(max_length=32)],
    unlock_cookie: str | None = Cookie(default=None, alias=share_service.SHARE_COOKIE_NAME),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SharePublicView]:
    """唯一不要求解锁的访客端点。密码之前不露片名海报（设计文档 §1.2）。"""
    row = await _load_share(session, slug)
    unlocked = await _unlocked(row, unlock_cookie)
    return ok(
        SharePublicView(
            requires_password=bool(row.password_encrypted),
            unlocked=unlocked,
            expires_at=row.expires_at,
            media_item_id=row.media_item_id if unlocked else None,
            collection_id=row.collection_id if unlocked else None,
        )
    )


@public_router.post(
    "/{slug}/unlock",
    response_model=ApiResponse[SharePublicView],
    summary="输入分享密码，解锁本浏览器",
    operation_id="share.unlock",
    openapi_extra={"x-cli-hidden": True},
)
async def unlock_share(
    slug: Annotated[str, Path(max_length=32)],
    payload: ShareUnlockRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SharePublicView]:
    row = await _load_share(session, slug)
    await share_service.check_password(row, payload.password)
    if row.password_encrypted:
        token, max_age = await share_service.issue_unlock_token(row)
        response.set_cookie(
            key=share_service.SHARE_COOKIE_NAME,
            value=token,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            secure=get_settings().session_cookie_secure,
            # 只发给这一条分享的接口：多条分享互不干扰，也不随任何其他请求外泄
            path=share_service.cookie_path(slug),
        )
    return ok(
        SharePublicView(
            requires_password=bool(row.password_encrypted),
            unlocked=True,
            expires_at=row.expires_at,
            media_item_id=row.media_item_id,
            collection_id=row.collection_id,
        ),
        message="已解锁",
    )


# -- 影片信息 -----------------------------------------------------------------


def _rewrite_url(url: str | None, slug: str, library_id: int, media_item_id: int) -> str | None:
    """详情视图里的图片地址 → 分享通道地址。

    站内相对地址按前缀改写；TMDB 图床的绝对地址改成分享通道的图片代理——
    前端所有远程图片都经站内代理（缓存 + 国内可达），访客没有账号进不了
    成员区的 /images/proxy，所以分享通道再挂一份同样的代理。
    """
    if not url:
        return url
    if url.startswith("http"):
        return f"/share/{slug}/images/proxy?url={quote(url, safe='')}"
    if url.startswith("/images/assets/"):
        return f"/share/{slug}/images/assets/{url[len('/images/assets/') :]}"
    art_prefix = f"/libraries/{library_id}/items/{media_item_id}/artwork"
    if url.startswith(art_prefix):
        return f"/share/{slug}/artwork{url[len(art_prefix) :]}"
    thumb_prefix = "/libraries/files/"
    if url.startswith(thumb_prefix) and url.endswith("/thumb"):
        return f"/share/{slug}/files/{url[len(thumb_prefix) :]}"
    # 认不出来的站内地址不给访客（访客也拿不到），宁缺毋漏
    return None


def project_item(detail: LibraryItemDetailView, principal: Principal) -> SharedItemView:
    """详情视图 → 访客视图：去掉路径 / 库归属 / 管理字段，图片地址改走分享通道。"""
    grant = _grant(principal)

    def rw(url: str | None) -> str | None:
        # 条目 id 取**这一次在看的**那部，而不是 grant 上那个：合集分享的
        # grant 根本没有条目 id
        return _rewrite_url(url, grant.slug, grant.library_id or 0, detail.media_item_id)

    local_meta: LocalMetaView | None = None
    if detail.local_meta is not None:
        local_meta = detail.local_meta.model_copy(
            update={
                "director_credits": [
                    d.model_copy(update={"thumb_url": rw(d.thumb_url)})
                    for d in detail.local_meta.director_credits
                ],
                "actors": [
                    a.model_copy(update={"thumb_url": rw(a.thumb_url)})
                    for a in detail.local_meta.actors
                ],
                "nfo_name": "",
            }
        )
    files = [
        SharedFileView(
            id=f.id,
            size_bytes=f.size_bytes,
            container=f.container,
            resolution=f.resolution,
            video_codec=f.video_codec,
            hdr=f.hdr,
            bit_depth=f.bit_depth,
            duration_seconds=f.duration_seconds,
            media_source=f.media_source,
            season_number=f.season_number,
            episode_number=f.episode_number,
            missing=f.missing,
            state=f.state,
            audio_streams=f.audio_streams,
            subtitle_streams=[s.model_copy(update={"file_name": None}) for s in f.subtitle_streams],
            chapters=(
                [c.model_copy(update={"image_url": rw(c.image_url)}) for c in f.chapters]
                if f.chapters is not None
                else None
            ),
        )
        for f in detail.files
    ]
    return SharedItemView(
        media_item_id=detail.media_item_id,
        kind=detail.kind,
        tmdb_id=detail.tmdb_id,
        imdb_id=detail.imdb_id,
        douban_id=detail.douban_id,
        title=detail.title,
        original_title=detail.original_title,
        year=detail.year,
        poster_url=rw(detail.poster_url),
        backdrop_url=rw(detail.backdrop_url),
        primary_aspect=detail.primary_aspect,
        local_meta=local_meta,
        files=files,
        seasons=detail.seasons,
        expires_at=grant.expires_at,
    )


def _rewrite_episodes(
    view: SeasonEpisodesView, principal: Principal, media_item_id: int
) -> SeasonEpisodesView:
    grant = _grant(principal)
    for ep in view.episodes:
        ep.still_url = _rewrite_url(
            ep.still_url, grant.slug, grant.library_id or 0, media_item_id
        )
    return view


async def _shared_item(session: AsyncSession, principal: Principal, item: int | None) -> int:
    """这次要看合集里的哪一部。

    条目分享忽略 ``item``（范围就那一个）；合集分享必须给，而且**必须是此刻的
    成员**——判定走 access 的收口（``assert_item_visible``），不在这里另写一套。
    """
    grant = _grant(principal)
    if grant.collection_id is None:
        return grant.media_item_id or 0
    if item is None:
        raise NotFoundException("请指定要看合集里的哪一部")
    await assert_item_visible(session, principal, item)
    return item


async def _shared_library(session: AsyncSession, principal: Principal, media_item_id: int) -> int:
    """这一部片走哪个库的详情。

    条目分享就是分享出去的那个库。合集分享（尤其跨库的）按条目**自己的**台账
    行取——同一部片散在两个库时随便挑一个都能放，取第一个即可。
    """
    grant = _grant(principal)
    if grant.library_id is not None:
        return grant.library_id
    library_id = (
        await session.execute(
            select(LibraryFile.library_id)
            .where(LibraryFile.media_item_id == media_item_id, LibraryFile.on_shelf())
            .limit(1)
        )
    ).scalar_one_or_none()
    if library_id is None:
        raise NotFoundException("媒体条目不存在")
    return int(library_id)


@public_router.get(
    "/{slug}/collection",
    response_model=ApiResponse[SharedCollectionView],
    summary="分享页的合集信息（名字 + 此刻的成员）",
    operation_id="share.collection",
    openapi_extra={"x-cli-hidden": True},
)
async def get_shared_collection(
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SharedCollectionView]:
    """成员**每次访问现算**：规则驱动的合集会自己长，分享出去之后新入库的片
    也会出现在对方那边。"""
    grant = _grant(principal)
    if grant.collection_id is None:
        raise NotFoundException("这条分享不是一个合集")
    collection = await session.get(Collection, grant.collection_id)
    if collection is None:
        raise NotFoundException("合集不存在（可能已被删除）")
    ids = await resolve_members(session, collection)
    rows = (
        (await session.execute(select(MediaItem).where(MediaItem.id.in_(ids)))).scalars().all()
        if ids
        else []
    )
    by_id = {row.id: row for row in rows}
    items = []
    for item_id in ids:
        item = by_id.get(item_id)
        if item is None:
            continue
        poster = await _poster_url(session, item)
        items.append(
            SharedCollectionItemView(
                media_item_id=item_id,
                title=item.title,
                year=item.year,
                kind=MediaKind(item.kind),
                # 访客拿不到 /images/... 那条内部路径，统一改写成分享域下的地址
                poster_url=_rewrite_url(poster, grant.slug, 0, item_id),
            )
        )
    row = await session.get(MediaShare, grant.share_id)
    if row is not None:
        await share_service.touch_view(session, row)
    return ok(SharedCollectionView(name=collection.name, item_count=len(items), items=items))


@public_router.get(
    "/{slug}/item",
    response_model=ApiResponse[SharedItemView],
    summary="分享页的影片信息",
    operation_id="share.item",
    openapi_extra={"x-cli-hidden": True},
)
async def get_shared_item(
    item: Annotated[int | None, Query(description="合集分享时指定看哪一部")] = None,
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SharedItemView]:
    grant = _grant(principal)
    media_item_id = await _shared_item(session, principal, item)
    detail = await libraries_routes.get_library_item(
        await _shared_library(session, principal, media_item_id),
        media_item_id,
        principal=principal,
        session=session,
    )
    row = await session.get(MediaShare, grant.share_id)
    if row is not None:
        await share_service.touch_view(session, row)
    return ok(project_item(detail.data, principal))


@public_router.get(
    "/{slug}/episodes",
    response_model=ApiResponse[SeasonEpisodesView],
    summary="分享页一季的分集清单",
    operation_id="share.episodes",
    openapi_extra={"x-cli-hidden": True},
)
async def get_shared_episodes(
    season_number: Annotated[int, Query(ge=0)],
    item: Annotated[int | None, Query(description="合集分享时指定看哪一部")] = None,
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SeasonEpisodesView]:
    media_item_id = await _shared_item(session, principal, item)
    resp = await libraries_routes.list_item_episodes(
        await _shared_library(session, principal, media_item_id),
        media_item_id,
        season_number=season_number,
        principal=principal,
        session=session,
    )
    return ok(_rewrite_episodes(resp.data, principal, media_item_id))


# -- 图片 ---------------------------------------------------------------------


@public_router.get(
    "/{slug}/artwork",
    response_class=FileResponse,
    summary="分享条目的本地美术图",
    operation_id="share.artwork",
    openapi_extra={"x-cli-hidden": True},
)
async def get_shared_artwork(
    kind: Literal["poster", "fanart"] = Query(default="poster"),
    item: Annotated[int | None, Query(description="合集分享时指定看哪一部")] = None,
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    media_item_id = await _shared_item(session, principal, item)
    return await libraries_routes.get_item_artwork(
        await _shared_library(session, principal, media_item_id),
        media_item_id,
        kind=kind,
        session=session,
    )


@public_router.get(
    "/{slug}/images/assets/{path:path}",
    response_class=FileResponse,
    summary="分享条目的刮削图片资产（海报 / 剧照 / 章节图 / 头像）",
    operation_id="share.asset",
    openapi_extra={"x-cli-hidden": True},
)
async def get_shared_asset(
    path: str,
    variant: ImageVariant | None = Query(default=None),
    v: str | None = Query(default=None, max_length=32),
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """只放行首段 = 分享条目 id 的路径；人物头像等不按条目分目录的资产不给
    （详情投影里认不出的地址已经置空，访客不会请求到这里）。"""
    head = path.split("/", 1)[0]
    if not head.isdigit():
        raise NotFoundException("图片资产不存在")
    # 合集分享盖得住多个条目，所以判定不能是"等于那一个"，而是"在范围里"——
    # 走 access 的收口，不在这里另写一套
    await assert_item_visible(session, principal, int(head))
    return await images_routes.get_metadata_asset(
        path, variant=variant, v=v, principal=principal, session=session
    )


@public_router.get(
    "/{slug}/images/proxy",
    response_class=FileResponse,
    summary="分享页的远程图片代理（TMDB 图床）",
    operation_id="share.image-proxy",
    openapi_extra={"x-cli-hidden": True},
)
async def get_shared_proxy_image(
    url: str = Query(min_length=1, max_length=2048),
    variant: ImageVariant | None = Query(default=None),
    _principal: Principal = Depends(require_share_access),
) -> FileResponse:
    """与成员区 /images/proxy 同一个实现：域名白名单（SSRF 防护）在服务层。"""
    return await images_routes.proxy_image(url=url, variant=variant)


@public_router.get(
    "/{slug}/files/{file_id}/thumb",
    response_class=FileResponse,
    summary="分享条目的分集缩略图",
    operation_id="share.thumb",
    openapi_extra={"x-cli-hidden": True},
)
async def get_shared_thumb(
    file_id: int,
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    await _assert_file_in_share(session, principal, file_id)
    return await libraries_routes.get_file_thumb(file_id, principal=principal, session=session)


# -- 播放 ---------------------------------------------------------------------


async def _assert_file_in_share(session: AsyncSession, principal: Principal, file_id: int) -> None:
    """这个文件属不属于这条分享盖得住的条目。

    条目分享还要求文件就在分享出去的那个库里；合集分享（尤其跨库的）没有
    "那一个库"，条目在范围里就够——范围本身已经是这条链接的闸门。
    """
    grant = _grant(principal)
    row = await session.get(LibraryFile, file_id)
    if row is None or row.media_item_id is None:
        raise NotFoundException("文件不存在")
    if grant.library_id is not None and row.library_id != grant.library_id:
        raise NotFoundException("文件不存在")
    await assert_item_visible(session, principal, row.media_item_id)


async def _assert_decide_payload(
    session: AsyncSession, principal: Principal, payload: PlaybackDecideRequest
) -> None:
    """决策 / 起播请求只能指向分享的那个条目（按文件 id 或按单元二选一）。"""
    if payload.file_id is not None:
        await _assert_file_in_share(session, principal, payload.file_id)
    elif payload.media_item_id is None:
        raise NotFoundException("没有找到可播放的文件")
    else:
        await assert_item_visible(session, principal, payload.media_item_id)


@public_router.post(
    "/{slug}/playback/decide",
    response_model=ApiResponse[PlaybackDecisionView],
    summary="分享页播放决策",
    operation_id="share.playback.decide",
    openapi_extra={"x-cli-hidden": True},
)
async def shared_decide(
    payload: PlaybackDecideRequest,
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackDecisionView]:
    await _assert_decide_payload(session, principal, payload)
    return await playback_routes.decide_playback_route(
        payload, principal=principal, session=session
    )


@public_router.post(
    "/{slug}/playback/sessions",
    response_model=ApiResponse[PlaybackSessionView],
    summary="分享页开始播放",
    operation_id="share.playback.session.start",
    openapi_extra={"x-cli-hidden": True},
)
async def shared_start_session(
    payload: PlaybackSessionRequest,
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackSessionView]:
    await _assert_decide_payload(session, principal, payload)
    return await playback_routes.start_playback_session(
        payload, principal=principal, session=session
    )


@public_router.post(
    "/{slug}/playback/sessions/{session_id}/ping",
    response_model=ApiResponse[dict],
    summary="分享页播放心跳",
    operation_id="share.playback.session.ping",
    openapi_extra={"x-cli-hidden": True},
)
async def shared_ping_session(
    session_id: Annotated[str, Path()],
    principal: Principal = Depends(require_share_access),
) -> ApiResponse[dict]:
    return await playback_routes.ping_playback_session(session_id, principal=principal)


@public_router.delete(
    "/{slug}/playback/sessions/{session_id}",
    response_model=ApiResponse[dict],
    summary="分享页结束播放",
    operation_id="share.playback.session.stop",
    openapi_extra={"x-cli-hidden": True, "x-cli-dangerous": "confirm"},
)
async def shared_stop_session(
    session_id: Annotated[str, Path()],
    principal: Principal = Depends(require_share_access),
) -> ApiResponse[dict]:
    return await playback_routes.stop_playback_session(session_id, principal=principal)


@public_router.get(
    "/{slug}/playback/sessions/{session_id}/diagnostics",
    response_model=ApiResponse[PlaybackDiagnosticsView],
    summary="分享页播放会话诊断",
    operation_id="share.playback.session.diagnostics",
    openapi_extra={"x-cli-hidden": True},
)
async def shared_session_diagnostics(
    session_id: Annotated[str, Path()],
    token: Annotated[str, Query()],
    _principal: Principal = Depends(require_share_access),
) -> ApiResponse[PlaybackDiagnosticsView]:
    return await playback_routes.get_session_diagnostics(session_id, token=token)


@public_router.post(
    "/{slug}/playback/progress",
    response_model=ApiResponse[PlaybackStateView],
    summary="分享页播放心跳（只刷新活动页的实时会话，不落任何观看状态）",
    operation_id="share.playback.progress",
    openapi_extra={"x-cli-hidden": True},
)
async def shared_progress(
    payload: PlaybackProgressRequest,
    request: Request,
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackStateView]:
    """访客不是成员：进度只记在访客自己的浏览器里（前端 localStorage），这里
    **不写** playback_state / playback_log。只维护活动页的实时会话——超管才能
    看到「分享访客正在播放」并结束它；结束后的拒绝窗口靠响应里的
    ``ended_by_admin`` 让播放器退出。"""
    # 只认这条分享盖得住的条目（合集分享盖得住多个，所以判定不是"等于那一个"）
    await assert_item_visible(session, principal, payload.media_item_id)
    unit = (payload.media_item_id, payload.season_number, payload.episode_number)
    member_id = share_service.SHARE_VISITOR_MEMBER_ID
    client = playback_watch.web_client_info(
        device_id=playback_watch.web_device_id(payload.device_id, member_id=member_id),
        user_agent=request.headers.get("user-agent"),
    )
    if payload.event == "start":
        activity.report_start(client.device_id, member_id=member_id, client=client, unit=unit)
    elif payload.event == "stop":
        playback_watch.end_session(client.device_id)
    else:
        playback_watch.report_heartbeat(
            unit,
            member_id=member_id,
            client=client,
            position_ms=payload.position_ms,
            paused=payload.paused,
        )
    ended_by_admin = payload.event != "stop" and activity.device_ended(client.device_id)
    return ok(
        PlaybackStateView(
            position_ms=payload.position_ms or 0,
            played=False,
            play_count=0,
            duration_ms=None,
            audio_track=payload.audio_track,
            subtitle_track=payload.subtitle_track,
            ended_by_admin=ended_by_admin,
        )
    )


@public_router.get(
    "/{slug}/playback/items/{media_item_id}",
    response_model=ApiResponse[PlaybackItemView],
    summary="分享页播放器的条目信息",
    operation_id="share.playback.item",
    openapi_extra={"x-cli-hidden": True},
)
async def shared_playback_item(
    media_item_id: Annotated[int, Path()],
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PlaybackItemView]:
    grant = _grant(principal)
    await assert_item_visible(session, principal, media_item_id)
    resp = await playback_routes.get_playback_item(
        media_item_id, principal=principal, session=session
    )
    resp.data.poster_url = _rewrite_url(
        resp.data.poster_url, grant.slug, grant.library_id or 0, media_item_id
    )
    return resp


@public_router.get(
    "/{slug}/playback/items/{media_item_id}/episodes",
    response_model=ApiResponse[SeasonEpisodesView],
    summary="分享页播放器的分集清单",
    operation_id="share.playback.item.episodes",
    openapi_extra={"x-cli-hidden": True},
)
async def shared_playback_episodes(
    media_item_id: Annotated[int, Path()],
    season_number: Annotated[int, Query(ge=0)],
    principal: Principal = Depends(require_share_access),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SeasonEpisodesView]:
    await assert_item_visible(session, principal, media_item_id)
    resp = await playback_routes.get_playback_item_episodes(
        media_item_id, season_number=season_number, principal=principal, session=session
    )
    return ok(_rewrite_episodes(resp.data, principal, media_item_id))
