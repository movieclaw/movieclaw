"""影片分享（docs/design/media-share.md）：创建 / 查询 / 取消、密码校验、解锁 Cookie。

设计要点：

- **一部影片一条有效分享**：创建前先查有效行，有就原样返回（前端据此直接
  展示已有链接），没有才建新行。过期 / 取消的旧行保留，slug 不复用；
- **访客不是成员**：分享主体的 ``member_id`` 是哨兵 ``-1``——它没有成员行、
  没有观看状态、不会进最近观看；活动页把它显示为「分享访客」；
- **解锁态是签名 Cookie**，不落库：负载只有 slug 与密码版本号，取消或改密码
  让版本号失效即可让所有已解锁的浏览器重新要密码。Cookie 的 Path 收窄到
  这一条分享的接口前缀，浏览器不会把它发给任何其他请求；
- **密码可回显**：用 Fernet 可逆加密而不是哈希，创建者再次打开对话框能看到
  原文。它是一个几位的访问码，不是账号凭据，这个取舍在设计文档 §2.3 拍板；
- **猜密码限流**复用登录的 ``LoginThrottle``，桶键 ``share:<slug>``，参数与
  登录一致（5 次后 30 s 起翻倍、封顶 5 分钟）。
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import AppException, BadRequestException
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.auth import Principal, ShareGrant
from movieclaw_api.settings.app_server import AppServerSetting
from movieclaw_api.settings.store import get_setting_store
from movieclaw_db.crypto import get_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models.base import utcnow
from movieclaw_db.models.media_share import MediaShare

#: 访客哨兵成员 id：无成员行、无观看状态；活动页按它显示「分享访客」。
SHARE_VISITOR_MEMBER_ID = -1
#: 解锁 Cookie 名。Path 按分享收窄，所以多条分享各有一枚同名 Cookie 互不干扰。
SHARE_COOKIE_NAME = "movieclaw_share"
#: 允许的有效期档位（天）。没有「永久」（设计文档 §2.2）。
SHARE_EXPIRY_DAYS = (1, 3, 7, 30)
#: 密码长度边界。
SHARE_PASSWORD_MIN = 4
SHARE_PASSWORD_MAX = 32
#: 解锁 Cookie 最长有效期，实际取它与分享到期剩余时间的较小值。
_UNLOCK_COOKIE_TTL_S = 7 * 24 * 3600
#: itsdangerous 签名域隔离：与会话 Cookie、取流 token 互不可伪造。
_SHARE_SALT = "movieclaw.share.v1"
#: 链接路径前缀（前端裸路由 ``/s/[slug]``）。
SHARE_PATH_PREFIX = "/s/"


def new_slug() -> str:
    """96 位随机、URL 安全、16 字符。"""
    return secrets.token_urlsafe(12)


def is_active(row: MediaShare, now: datetime | None = None) -> bool:
    """有效 = 未取消且未到期。"""
    return row.revoked_at is None and row.expires_at > (now or utcnow())


def grant_of(row: MediaShare) -> ShareGrant:
    assert row.id is not None
    return ShareGrant(
        share_id=row.id,
        slug=row.slug,
        media_item_id=row.media_item_id,
        library_id=row.library_id,
        expires_at=row.expires_at,
        collection_id=row.collection_id,
    )


def share_principal(row: MediaShare) -> Principal:
    """分享主体：不是管理员、没有成员行，可见面由 ``Principal.share`` 收窄
    （services/library/access.py）。"""
    return Principal(
        kind="share",
        name=f"share:{row.slug}",
        member_id=SHARE_VISITOR_MEMBER_ID,
        is_admin=False,
        share=grant_of(row),
    )


def password_of(row: MediaShare) -> str | None:
    """解出密码原文；无密码返回 None。"""
    if not row.password_encrypted:
        return None
    return get_secret_box().decrypt(row.password_encrypted)


def _normalize_password(password: str | None) -> str | None:
    if password is None:
        return None
    cleaned = password.strip()
    if not cleaned:
        return None
    if not SHARE_PASSWORD_MIN <= len(cleaned) <= SHARE_PASSWORD_MAX:
        raise BadRequestException(
            f"分享密码长度须在 {SHARE_PASSWORD_MIN}–{SHARE_PASSWORD_MAX} 位之间"
        )
    return cleaned


# ---------------------------------------------------------------------------
# 创建 / 查询 / 取消
# ---------------------------------------------------------------------------


async def get_active_for_item(session: AsyncSession, media_item_id: int) -> MediaShare | None:
    """条目当前的有效分享（至多一条）。"""
    rows = (
        (
            await session.execute(
                select(MediaShare)
                .where(MediaShare.media_item_id == media_item_id, MediaShare.revoked_at.is_(None))  # type: ignore[union-attr]
                .order_by(MediaShare.id.desc())  # type: ignore[union-attr]
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for row in rows:
        if is_active(row, now):
            return row
    return None


async def get_by_slug(session: AsyncSession, slug: str) -> MediaShare | None:
    return (
        await session.execute(select(MediaShare).where(MediaShare.slug == slug))
    ).scalar_one_or_none()


async def list_active(session: AsyncSession) -> list[MediaShare]:
    """全部有效分享，最新的在前（媒体库管理页「分享」标签）。"""
    rows = (
        (
            await session.execute(
                select(MediaShare)
                .where(MediaShare.revoked_at.is_(None), MediaShare.expires_at > utcnow())  # type: ignore[union-attr]
                .order_by(MediaShare.id.desc())  # type: ignore[union-attr]
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_active_for_collection(
    session: AsyncSession, collection_id: int
) -> MediaShare | None:
    """合集当前的有效分享（至多一条，与条目那一支同一条规矩）。"""
    rows = (
        (
            await session.execute(
                select(MediaShare)
                .where(MediaShare.collection_id == collection_id, MediaShare.revoked_at.is_(None))  # type: ignore[union-attr]
                .order_by(MediaShare.id.desc())  # type: ignore[union-attr]
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for row in rows:
        if is_active(row, now):
            return row
    return None


async def create_share(
    session: AsyncSession,
    *,
    media_item_id: int | None = None,
    collection_id: int | None = None,
    library_id: int | None,
    expires_in_days: int,
    password: str | None,
    created_by_member_id: int = 0,
) -> tuple[MediaShare, bool]:
    """创建分享；同一目标已有有效分享时原样返回它。返回 (行, 是否新建)。

    ``media_item_id`` 与 ``collection_id`` 恰好给一个——范围二选一是这条
    链接的定义，两个都给或都不给都没有意义，与其在下游各处防御，不如在
    入口断言。
    """
    if (media_item_id is None) == (collection_id is None):
        raise BadRequestException("分享的范围只能是一个条目或一个合集")
    if expires_in_days not in SHARE_EXPIRY_DAYS:
        raise BadRequestException(
            "有效期只能是 " + " / ".join(f"{d} 天" for d in SHARE_EXPIRY_DAYS)
        )
    cleaned = _normalize_password(password)
    existing = (
        await get_active_for_item(session, media_item_id)
        if media_item_id is not None
        else await get_active_for_collection(session, collection_id or 0)
    )
    if existing is not None:
        return existing, False
    row = MediaShare(
        slug=new_slug(),
        media_item_id=media_item_id,
        collection_id=collection_id,
        library_id=library_id,
        created_by_member_id=created_by_member_id,
        password_encrypted=get_secret_box().encrypt(cleaned) if cleaned else None,
        expires_at=utcnow() + timedelta(days=expires_in_days),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, True


async def revoke(session: AsyncSession, row: MediaShare) -> None:
    """取消分享：链接立即失效；版本号递增让已解锁的浏览器也一并失效。幂等。"""
    if row.revoked_at is not None:
        return
    row.revoked_at = utcnow()
    row.password_version += 1
    row.updated_at = utcnow()
    session.add(row)
    await session.commit()


async def touch_view(session: AsyncSession, row: MediaShare) -> None:
    """影片页成功打开一次：计数 +1、记最近打开时间。"""
    row.view_count += 1
    row.last_accessed_at = utcnow()
    session.add(row)
    await session.commit()


async def share_is_live(share_id: int) -> bool:
    """取流字节面用：分享是否仍有效。独立开一个会话——调用方（取流签名验证）
    没有请求级会话可用。"""
    async with get_database().session() as session:
        row = await session.get(MediaShare, share_id)
        return row is not None and is_active(row)


# ---------------------------------------------------------------------------
# 链接
# ---------------------------------------------------------------------------


async def share_url(slug: str) -> str:
    """分享链接：配置了外部访问地址就给绝对地址，否则给相对路径由前端补全。"""
    setting = await get_setting_store().get(AppServerSetting)
    base = setting.external_url.strip().rstrip("/")
    return f"{base}{SHARE_PATH_PREFIX}{slug}"


def cookie_path(slug: str) -> str:
    """解锁 Cookie 的 Path：只发给这一条分享的接口。"""
    return f"{get_settings().api_v1_prefix}/share/{slug}"


# ---------------------------------------------------------------------------
# 密码与解锁 Cookie
# ---------------------------------------------------------------------------


def _throttle(slug: str) -> auth_service.LoginThrottle:
    return auth_service._throttle_for(f"share:{slug}")


async def check_password(row: MediaShare, password: str) -> None:
    """校验访问密码；错误抛 401，连续失败触发限流 429。"""
    throttle = _throttle(row.slug)
    try:
        throttle.ensure_allowed()
    except AppException as exc:
        # 换成访客看得懂的说法；状态码与 code 沿用登录限流
        raise AppException(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message.replace("登录失败", "密码错误"),
        ) from None
    expected = password_of(row)
    if expected is None:
        return
    if not secrets.compare_digest(expected.encode(), password.strip().encode()):
        throttle.record_failure()
        raise AppException(
            status_code=401, code="SHARE_PASSWORD_INVALID", message="密码不对，请重新输入"
        )
    throttle.reset()


async def issue_unlock_token(row: MediaShare) -> tuple[str, int]:
    """签发解锁 Cookie 值；返回 (值, max_age 秒)。"""
    remaining = int((row.expires_at - utcnow()).total_seconds())
    max_age = max(1, min(_UNLOCK_COOKIE_TTL_S, remaining))
    serializer = URLSafeSerializer(await auth_service.get_signing_secret(), salt=_SHARE_SALT)
    token = serializer.dumps(
        {"s": row.slug, "pv": row.password_version, "exp": int(time.time()) + max_age}
    )
    return token, max_age


async def verify_unlock_token(token: str, row: MediaShare) -> bool:
    """验签并核对 slug 与密码版本；任何不符都视为未解锁。"""
    serializer = URLSafeSerializer(await auth_service.get_signing_secret(), salt=_SHARE_SALT)
    try:
        payload = serializer.loads(token)
    except BadSignature:
        return False
    if not isinstance(payload, dict):
        return False
    try:
        return (
            payload["s"] == row.slug
            and int(payload["pv"]) == row.password_version
            and int(payload["exp"]) > int(time.time())
        )
    except (KeyError, TypeError, ValueError):
        return False
