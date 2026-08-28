"""远程转码数据面的短时签名。

远程 Worker 需要从 NAS 读取源文件、向 NAS 写回 HLS 产物，但这两类地址都
不能复用浏览器的播放 token：Worker 是另一条信任边界，且上传权限必须严格
限定到一个会话。这里复用登录签名密钥，但使用独立 salt 和用途字段做域隔离。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeSerializer

from movieclaw_api.services.auth import get_signing_secret

_REMOTE_SALT = "movieclaw.playback.remote-worker.v1"
REMOTE_GRANT_TTL_S = 12 * 3600


@dataclass(frozen=True)
class RemoteGrant:
    """远程数据面凭据允许访问的范围。"""

    session_id: str
    file_id: int
    kind: str  # source | artifact
    expires_at: int
    attempt_id: str | None = None


async def issue_remote_grant(
    *,
    session_id: str,
    file_id: int,
    kind: str,
    attempt_id: str | None = None,
    ttl_seconds: int = REMOTE_GRANT_TTL_S,
) -> str:
    """签发一个只属于当前播放会话的源文件/产物凭据。"""
    if kind not in {"source", "artifact"}:
        raise ValueError(f"不支持的远程凭据用途：{kind}")
    serializer = URLSafeSerializer(await get_signing_secret(), salt=_REMOTE_SALT)
    return serializer.dumps(
        {
            "s": session_id,
            "f": file_id,
            "k": kind,
            "exp": int(time.time()) + ttl_seconds,
            "a": attempt_id,
        }
    )


async def verify_remote_grant(
    token: str,
    *,
    session_id: str,
    kind: str,
    file_id: int | None = None,
    attempt_id: str | None = None,
) -> RemoteGrant | None:
    """验证签名、用途、会话和文件范围；任何不符都返回 ``None``。"""
    serializer = URLSafeSerializer(await get_signing_secret(), salt=_REMOTE_SALT)
    try:
        payload = serializer.loads(token)
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        grant = RemoteGrant(
            session_id=str(payload["s"]),
            file_id=int(payload["f"]),
            kind=str(payload["k"]),
            expires_at=int(payload["exp"]),
            attempt_id=str(payload["a"]) if payload.get("a") else None,
        )
    except (KeyError, TypeError, ValueError):
        return None
    if grant.expires_at <= int(time.time()):
        return None
    if grant.session_id != session_id or grant.kind != kind:
        return None
    if file_id is not None and grant.file_id != file_id:
        return None
    if attempt_id is not None and grant.attempt_id != attempt_id:
        return None
    return grant
