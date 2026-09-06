"""人物页接口：按 TMDB 影人 ID 取「我库里的这个人」。

数据全部来自本地表（person / media_item_person），不联网——与条目详情页同一条
自足原则。存量库在做过一次「刷新元数据」之前没有 person 关系数据（老的
cast JSON 里没有 TMDB person id，见迁移 c4d7a9e2b581 的说明），此时返回 404。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.api.deps import require_login
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.schemas.person import PersonCreditView, PersonView
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.access import visible_library_ids
from movieclaw_db.engine import get_session
from movieclaw_db.models import MediaMetadata
from movieclaw_db.repositories import PersonRepository
from movieclaw_media.models import MediaKind

router = APIRouter(prefix="/people", tags=["people"])


@router.get(
    "/{tmdb_person_id}",
    response_model=ApiResponse[PersonView],
    summary="人物页：库内这个影人的档案与作品",
    operation_id="people.show",
)
async def get_person(
    tmdb_person_id: int,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_login),
) -> ApiResponse[PersonView]:
    """影人档案 + 他在**当前身份可浏览的库**内参演/执导的全部条目。

    海报口径与库存海报墙一致：优先本地刮削资产（断网可用），回落 TMDB 图床。
    作品按请求主体的可浏览库集合过滤（超管、成员、CLI/Agent 令牌同一条判定，
    docs/design/library-access.md §2.5）：范围外的库不从这里漏出片名与海报。
    """
    repo = PersonRepository(session)
    person = await repo.get_by_tmdb_id(tmdb_person_id)
    if person is None or person.id is None:
        raise NotFoundException("库内没有这个影人的记录")

    credits = await repo.list_credits(
        person.id, visible_library_ids=await visible_library_ids(session, principal)
    )
    if not credits:
        # person 行只增不删（一个人参演多部，删一部不代表这个人该消失），因此
        # 会出现「有这个人、但他的片都已从库里删掉」的孤儿行。此时页面的前提
        # ——「他在我库里的作品」——不成立，给 404 让前端走空状态，
        # 而不是渲染一个「库内 0 部」的空壳页。他的片全在范围外的库里也是
        # 同一个 404：不泄露「有这个人、但你不能看」
        raise NotFoundException("库内没有这位影人的作品")

    base = get_settings().tmdb_image_base_url.rstrip("/")

    # 海报优先本地资产：一次查完再配对，不逐条目查（N+1）
    item_ids = [c.media_item.id for c in credits if c.media_item.id is not None]
    poster_assets: dict[int, str] = {}
    if item_ids:
        poster_assets = {
            item_id: poster_file
            for item_id, poster_file in (
                await session.execute(
                    select(MediaMetadata.media_item_id, MediaMetadata.poster_file).where(
                        MediaMetadata.media_item_id.in_(item_ids),  # type: ignore[attr-defined]
                        MediaMetadata.poster_file.is_not(None),  # type: ignore[union-attr]
                    )
                )
            ).all()
        }

    from movieclaw_api.services.media_scrape import asset_version

    views: list[PersonCreditView] = []
    for credit in credits:
        item = credit.media_item
        if item.id is None:
            continue
        if item.id in poster_assets:
            # ?v=<mtime>：换图会原地覆盖同一路径，不带版本前端会一直显示旧图
            rel = poster_assets[item.id]
            poster_url: str | None = f"/images/assets/{rel}?v={asset_version(rel)}"
        else:
            poster_url = f"{base}/w500{item.poster_path}" if item.poster_path else None
        views.append(
            PersonCreditView(
                media_item_id=item.id,
                kind=MediaKind(item.kind),
                tmdb_id=item.tmdb_id,
                title=item.title,
                year=item.year,
                poster_url=poster_url,
                library_id=credit.library_id,
                department=credit.department,
                character=credit.character,
            )
        )

    return ok(
        PersonView(
            tmdb_person_id=person.tmdb_person_id,
            name=person.name,
            original_name=person.original_name,
            avatar_url=f"{base}/w300{person.profile_path}" if person.profile_path else None,
            credits=views,
        )
    )
