"""iLink Bot 网关的 HTTP 客户端(JSON over HTTPS)。

协议要点(参考 Tencent/openclaw-weixin):
- 所有接口 POST/GET JSON,bytes 字段(游标、密钥)在 JSON 里是 base64 字符串;
- 鉴权:``Authorization: Bearer <bot_token>`` + ``AuthorizationType: ilink_bot_token``;
- 身份头:``iLink-App-Id`` / ``iLink-App-ClientVersion``(版本编码为 0x00MMNNPP);
- ``getupdates`` 是长轮询:服务端 hold 住请求直到有新消息或超时,客户端超时
  属正常控制流(返回空结果重试),不算错误;
- 每个请求体附带 ``base_info``(channel_version + bot_agent,仅观测用)。

网络说明:微信网关是国内直连地址,刻意**不走** movieclaw_net 的代理路由
(代理通常面向境外站点,套上反而容易断)。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import secrets
from typing import Any

import httpx

logger = logging.getLogger("movieclaw_channel.weixin.client")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

#: 服务端返回该错误码表示 bot_token 已失效,须重新扫码
STALE_TOKEN_ERRCODE = -14

#: iLink 身份头(与 openclaw-weixin 同款声明)
_APP_ID = "bot"
_CHANNEL_VERSION = "0.1.0"
_BOT_AGENT = f"MovieClaw/{_CHANNEL_VERSION}"

#: 常规接口超时(秒);长轮询单独指定
_API_TIMEOUT_S = 15.0


class WeixinApiError(Exception):
    """iLink 接口返回业务错误(ret/errcode 非 0)或 HTTP 错误。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _encode_client_version(version: str) -> int:
    """版本号编码为 uint32:major<<16 | minor<<8 | patch(高 8 位固定 0)。"""
    parts = version.split(".")

    def _num(i: int) -> int:
        try:
            return int(parts[i]) & 0xFF
        except (IndexError, ValueError):
            return 0

    return (_num(0) << 16) | (_num(1) << 8) | _num(2)


def _base_headers() -> dict[str, str]:
    # X-WECHAT-UIN:随机 uint32 → 十进制字符串 → base64(每请求随机)
    uin = base64.b64encode(str(secrets.randbits(32)).encode()).decode()
    return {
        "Content-Type": "application/json",
        "iLink-App-Id": _APP_ID,
        "iLink-App-ClientVersion": str(_encode_client_version(_CHANNEL_VERSION)),
        "X-WECHAT-UIN": uin,
    }


def _base_info() -> dict[str, str]:
    return {"channel_version": _CHANNEL_VERSION, "bot_agent": _BOT_AGENT}


def _raise_for_business_error(payload: dict[str, Any], label: str) -> None:
    """检查响应体的 ret/errcode,非 0 则抛业务错误(游标类接口自行处理,不走这里)。"""
    ret = payload.get("ret")
    if ret not in (None, 0):
        raise WeixinApiError(
            f"{label} 失败:ret={ret} errmsg={payload.get('errmsg') or '(无)'}", code=int(ret)
        )


class WeixinClient:
    """绑定单个 bot 账号的 iLink CGI 客户端。"""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(_API_TIMEOUT_S))

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = _base_headers()
        headers["AuthorizationType"] = "ilink_bot_token"
        headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _post(
        self, endpoint: str, body: dict[str, Any], *, timeout_s: float = _API_TIMEOUT_S
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        resp = await self._client.post(
            url,
            json={**body, "base_info": _base_info()},
            headers=self._headers(),
            timeout=httpx.Timeout(timeout_s),
        )
        if resp.status_code != 200:
            raise WeixinApiError(f"{endpoint} HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # ------------------------------------------------------------------
    # 收消息:getupdates 长轮询
    # ------------------------------------------------------------------
    async def get_updates(
        self,
        cursor: str,
        *,
        stop: asyncio.Event | None = None,
        timeout_s: float = 35.0,
    ) -> dict[str, Any]:
        """长轮询拉取新消息。

        - 客户端超时(服务端 hold 满没消息)→ 返回空结果,调用方直接重试;
        - ``stop`` 置位 → 立刻掐断在飞请求,返回空结果(收消息循环随即退出);
        - 业务错误码(含 -14)不在这里抛,原样返回给 adapter 分类处理。
        """
        body = {"get_updates_buf": cursor or ""}
        request = asyncio.ensure_future(
            self._post("ilink/bot/getupdates", body, timeout_s=timeout_s + 5)
        )
        empty = {"ret": 0, "msgs": [], "get_updates_buf": ""}

        if stop is not None:
            stopper = asyncio.ensure_future(stop.wait())
            done, _ = await asyncio.wait({request, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if request not in done:
                request.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await request
                return empty
            stopper.cancel()

        try:
            return await request
        except httpx.TimeoutException:
            logger.debug("getupdates 客户端超时(长轮询正常路径),继续下一轮")
            return empty

    # ------------------------------------------------------------------
    # 发消息
    # ------------------------------------------------------------------
    async def send_text(self, to_user_id: str, text: str, context_token: str | None) -> None:
        """发送一条文本消息(context_token 必须原样回带,服务端据此定位会话)。"""
        msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"movieclaw-{secrets.token_hex(8)}",
            "message_type": 2,  # BOT
            "message_state": 2,  # FINISH
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        else:
            # 唯一会走到这里的场景:绑定后从没在微信里跟 bot 说过话,还没有
            # 任何一条入站消息下发过会话令牌。给出可操作的引导而不是干报错。
            logger.warning(
                "微信发送缺少会话令牌(context_token),消息可能无法送达 to=%s;"
                "请先在微信里给 bot 发一条消息,之后的主动推送会自动复用该会话",
                to_user_id,
            )
        payload = await self._post("ilink/bot/sendmessage", {"msg": msg})
        _raise_for_business_error(payload, "sendmessage")

    # ------------------------------------------------------------------
    # CDN 下载(入站图片)
    # ------------------------------------------------------------------
    async def download_media(self, url: str, *, max_bytes: int, timeout_s: float = 30.0) -> bytes:
        """从微信 CDN 下载一份媒体字节(可能是密文,解密由 media 模块负责)。

        CDN 与 iLink 网关同口径国内直连,复用同一连接池;超过 max_bytes 直接
        中断,不把一条超大消息的字节全读进内存。
        """
        # CDN 地址本身带鉴权参数,不需要(也不接受)iLink 的身份头
        async with self._client.stream("GET", url, timeout=httpx.Timeout(timeout_s)) as resp:
            if resp.status_code != 200:
                raise WeixinApiError(f"CDN 下载失败 HTTP {resp.status_code}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise WeixinApiError(f"媒体文件超过 {max_bytes // (1024 * 1024)}MB 上限")
                chunks.append(chunk)
        return b"".join(chunks)

    # ------------------------------------------------------------------
    # 「正在输入」状态(getconfig 换 typing_ticket → sendtyping)
    # ------------------------------------------------------------------
    async def get_config(self, ilink_user_id: str, context_token: str | None) -> str:
        """拉取该用户的 bot 配置,返回 typing_ticket(空串 = 服务端未发放)。"""
        payload = await self._post(
            "ilink/bot/getconfig",
            {"ilink_user_id": ilink_user_id, "context_token": context_token},
            timeout_s=10,
        )
        _raise_for_business_error(payload, "getconfig")
        return str(payload.get("typing_ticket") or "")

    async def send_typing(self, ilink_user_id: str, typing_ticket: str, *, typing: bool) -> None:
        """发送/取消「正在输入」指示(status: 1=输入中 2=取消)。"""
        await self._post(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": 1 if typing else 2,
            },
            timeout_s=10,
        )

    # ------------------------------------------------------------------
    # 生命周期通知(失败只告警,不阻断启停)
    # ------------------------------------------------------------------
    async def notify_start(self) -> None:
        try:
            await self._post("ilink/bot/msg/notifystart", {}, timeout_s=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notifystart 失败(忽略):%s", exc)

    async def notify_stop(self) -> None:
        try:
            await self._post("ilink/bot/msg/notifystop", {}, timeout_s=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notifystop 失败(忽略):%s", exc)


# ---------------------------------------------------------------------------
# 绑定用的无凭据接口(拿二维码 / 轮询扫码状态)
# 绑定是低频操作,用短生命周期客户端逐次请求,不维护连接池。
# ---------------------------------------------------------------------------

_QR_BOT_TYPE = "3"
_QR_POLL_TIMEOUT_S = 35.0


async def fetch_qrcode(base_url: str, local_token_list: list[str]) -> dict[str, Any]:
    """获取登录二维码。返回 {qrcode, qrcode_img_content(二维码链接)}。

    local_token_list:本机已绑定账号的 token(最多 10 个),服务端用它判断
    「该 bot 是否已绑定过本实例」(binded_redirect)。
    """
    url = f"{base_url.rstrip('/')}/ilink/bot/get_bot_qrcode?bot_type={_QR_BOT_TYPE}"
    # 与 openclaw 一致:即使无 token,POST 也要带 AuthorizationType 声明
    headers = {**_base_headers(), "AuthorizationType": "ilink_bot_token"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(_API_TIMEOUT_S)) as client:
        resp = await client.post(
            url,
            json={"local_token_list": local_token_list[:10], "base_info": _base_info()},
            headers=headers,
        )
        if resp.status_code != 200:
            raise WeixinApiError(f"获取二维码失败 HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


async def poll_qr_status(
    base_url: str, qrcode: str, verify_code: str | None = None
) -> dict[str, Any]:
    """长轮询扫码状态。网络错误/网关超时按「继续等待」处理,返回 {status: wait}。

    返回字段(confirmed 时):bot_token / ilink_bot_id / ilink_user_id / baseurl。
    """
    params: dict[str, str] = {"qrcode": qrcode}
    if verify_code:
        params["verify_code"] = verify_code
    url = f"{base_url.rstrip('/')}/ilink/bot/get_qrcode_status"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_QR_POLL_TIMEOUT_S + 5)) as client:
            resp = await client.get(url, params=params, headers=_base_headers())
            if resp.status_code != 200:
                logger.warning("扫码状态查询 HTTP %s,按等待处理", resp.status_code)
                return {"status": "wait"}
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("扫码状态查询网络错误,按等待处理:%s", exc)
        return {"status": "wait"}
