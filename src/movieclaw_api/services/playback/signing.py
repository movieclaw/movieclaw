"""取流签名 URL（docs/design/web-player.md §4.7）。

**为什么必须是查询参数而不是 header**：``<video src>``、hls.js 拉分片、
``<track>`` 拉字幕都带不了自定义 header（Safari 的原生 HLS 尤其——整条
取流链路都在浏览器内部，JS 插不进手）。所以取流 URL 只能自带凭据。

复用登录会话的签名密钥 + **不同 salt** 做域隔离：这是本仓库既有的做法
（``auth.py`` 的 ``_SESSION_SALT``）。附带的好处是轮换会话密钥（全端下线）
会一并作废所有在途的取流 token，语义正确。

token 负载只放**授权范围**，不放任何秘密：成员 id、文件 id、可选的会话 id、
过期时间。拿到 token 也只能取这一个文件/会话的流。另带一个可选的浏览器设备
标识——它不是授权范围，只用来把取流字节记到活动页上对应会话的名下。

影片分享访客（docs/design/media-share.md §4.3）的 token 再多带分享 id：验签
之后回查一次分享行是否仍有效，「取消分享」对直连档也在下一个 Range 请求就
生效，不必等 token 自然到期。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeSerializer

from movieclaw_api.services.auth import get_signing_secret

_STREAM_SALT = "movieclaw.playback.stream.v1"

#: 取流 token 的有效期。比会话空闲回收（三分钟）长得多——一部三小时的电影
#: 从头播到尾中间不会重新签发，但也不能长到「链接泄漏出去还能用一年」。
STREAM_TOKEN_TTL_S = 12 * 3600


@dataclass(frozen=True)
class StreamGrant:
    """一张取流凭据允许做什么。"""

    member_id: int
    file_id: int
    session_id: str | None
    expires_at: int
    #: 播放这条流的浏览器设备标识（活动页实时会话的锚点）；旧 token 没有
    device_id: str | None = None
    #: 影片分享访客签出的 token 带分享 id；成员 token 为 None
    share_id: int | None = None


async def issue_stream_token(
    *,
    member_id: int,
    file_id: int,
    session_id: str | None = None,
    device_id: str | None = None,
    ttl_seconds: int = STREAM_TOKEN_TTL_S,
    share_id: int | None = None,
) -> str:
    serializer = URLSafeSerializer(await get_signing_secret(), salt=_STREAM_SALT)
    payload = {
        "m": member_id,
        "f": file_id,
        "s": session_id,
        "exp": int(time.time()) + ttl_seconds,
    }
    if device_id:
        payload["d"] = device_id
    if share_id is not None:
        payload["sh"] = share_id
    return serializer.dumps(payload)


async def verify_stream_token(
    token: str,
    *,
    file_id: int | None = None,
    session_id: str | None = None,
) -> StreamGrant | None:
    """验签并校验授权范围；任何一项不符都返回 None（调用方按 404 应答）。

    过期与范围不符都返回 None 而不是抛异常：取流端点对任何失败都应该长得
    一样，不给探测者区分「token 假」和「文件不存在」的机会。
    """
    serializer = URLSafeSerializer(await get_signing_secret(), salt=_STREAM_SALT)
    try:
        payload = serializer.loads(token)
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        device_id = payload.get("d")
        share_id = payload.get("sh")
        grant = StreamGrant(
            member_id=int(payload["m"]),
            file_id=int(payload["f"]),
            session_id=payload.get("s"),
            expires_at=int(payload["exp"]),
            device_id=str(device_id) if device_id else None,
            share_id=int(share_id) if share_id is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        return None
    if grant.expires_at <= int(time.time()):
        return None
    if file_id is not None and grant.file_id != file_id:
        return None
    if session_id is not None and grant.session_id != session_id:
        return None
    if grant.share_id is not None:
        # 分享被取消 / 到期后，已签出的 token 立即作废（主键一查，与取流本身
        # 要查的文件行同量级）
        from movieclaw_api.services.share import share_is_live

        if not await share_is_live(grant.share_id):
            return None
    return grant
