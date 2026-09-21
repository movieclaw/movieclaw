"""飞书群自定义机器人 Webhook 的最小 HTTP 客户端。

与 Telegram/Discord 的 bot 不同,飞书自定义机器人是纯推送出口:用户在飞书群
「设置 → 群机器人 → 添加机器人 → 自定义机器人」里拿到一个 Webhook 地址,
对它 POST JSON 即可向群里发消息——没有 bot 身份、收不到消息,也因此绑定
不需要配对码,粘贴地址即完成(见 services/im_channel.py::bind_feishu)。

凭据存储约定:channel_account.token 列(SecretBox 加密)存 JSON
``{"webhook_url": ..., "secret": ...}``,裸 URL 亦兼容(等价于无签名)。
机器人安全设置选了「签名校验」的必须带 secret,每条消息按飞书口径现算签名:

    string_to_sign = f"{timestamp}\\n{secret}"   # 时间戳+换行+密钥整体作 HMAC 密钥
    sign = base64(hmac_sha256(key=string_to_sign, msg=b""))

出口走 egress_transport("feishu"):国内网络可直连 open.feishu.cn,默认不代理;
国际版 Lark(open.larksuite.com)用户可在「设置 → 网络」为飞书开启代理。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from movieclaw_net import egress_transport

#: 404 = 机器人已被移除(Webhook 随之失效),对应通道层的凭据失效语义
_AUTH_ERROR_STATUS = (404,)
#: 签名校验失败:secret 不对,属凭据级错误
_SIGN_MISMATCH_CODE = 19021

#: Webhook 只接受飞书/Lark 官方域(同时是 SSRF 防线:服务端只会 POST 到官方域)
_ALLOWED_HOST_SUFFIXES = ("feishu.cn", "larksuite.com")
_WEBHOOK_PATH_MARK = "/open-apis/bot/v2/hook/"


class FeishuApiError(Exception):
    """Webhook 调用失败时抛出;``auth_failed`` 标记凭据级错误。"""

    def __init__(self, message: str, *, auth_failed: bool = False) -> None:
        super().__init__(message)
        self.auth_failed = auth_failed


def normalize_webhook_url(raw: str) -> str:
    """校验并归一化用户粘贴的 Webhook 地址,不合法抛 ValueError。

    只接受 https 且主机为飞书/Lark 官方域、路径带机器人 hook 令牌的地址。
    """
    url = raw.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("Webhook 地址不合法:需要完整的 https:// 链接")
    host = parts.hostname or ""
    if not any(host == s or host.endswith("." + s) for s in _ALLOWED_HOST_SUFFIXES):
        raise ValueError("Webhook 地址不合法:主机不是飞书/Lark 官方域名")
    if _WEBHOOK_PATH_MARK not in parts.path or not parts.path.split(_WEBHOOK_PATH_MARK)[-1]:
        raise ValueError("Webhook 地址不合法:路径里找不到机器人 hook 令牌")
    return url


def feishu_account_id(webhook_url: str) -> str:
    """从 Webhook 地址提取账号 id(hook 令牌):同一机器人重复接入即覆盖,不产生重复账号。"""
    return normalize_webhook_url(webhook_url).split(_WEBHOOK_PATH_MARK)[-1].rstrip("/")


def parse_credentials(token: str) -> tuple[str, str]:
    """解出 (webhook_url, secret)。token 列存 JSON;裸 URL 亦兼容(等价无签名)。"""
    try:
        data = json.loads(token)
    except ValueError:
        return token, ""
    if isinstance(data, dict) and isinstance(data.get("webhook_url"), str):
        return data["webhook_url"], str(data.get("secret") or "")
    return token, ""


def _sign(timestamp: str, secret: str) -> str:
    # 飞书的非常规口径:string_to_sign 整体作为 HMAC 密钥,消息体为空
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuClient:
    """单个自定义机器人的 Webhook 客户端(线程不安全,通道层单任务使用)。"""

    def __init__(self, token: str) -> None:
        self._webhook_url, self._secret = parse_credentials(token)
        self._http = httpx.AsyncClient(
            transport=egress_transport("feishu"),
            timeout=httpx.Timeout(15.0, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_text(self, text: str) -> None:
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        if self._secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = _sign(timestamp, self._secret)
        try:
            resp = await self._http.post(self._webhook_url, json=payload)
        except httpx.HTTPError as exc:
            raise FeishuApiError(f"飞书机器人连接失败:{exc}") from exc
        self._parse(resp)

    def _parse(self, resp: httpx.Response) -> None:
        if resp.status_code in _AUTH_ERROR_STATUS:
            raise FeishuApiError(
                "飞书机器人不存在或已被移除,请重新创建机器人并接入", auth_failed=True
            )
        if resp.status_code != 200:
            raise FeishuApiError(f"飞书机器人接口异常(HTTP {resp.status_code})")
        try:
            data = resp.json()
        except ValueError as exc:
            raise FeishuApiError(
                f"飞书机器人接口返回异常响应(HTTP {resp.status_code},非 JSON)"
            ) from exc
        # 成功响应 {"code": 0, "msg": "success"};旧版网关用 StatusCode 同义
        code = data.get("code", data.get("StatusCode"))
        if code == 0:
            return
        msg = str(data.get("msg") or data.get("StatusMessage") or code)
        if code == _SIGN_MISMATCH_CODE:
            raise FeishuApiError(
                "签名校验失败:机器人开启了签名校验但密钥不匹配,"
                "请解绑后重新接入并核对签名密钥",
                auth_failed=True,
            )
        # 飞书报文是「Key Words Not Found」(带空格):归一后比对,避免误判成普通拒收
        if "keyword" in msg.lower().replace(" ", "").replace("-", ""):
            # 「自定义关键词」安全设置:movieclaw 的推送文案无法保证含特定词,
            # 与其让后续推送静默丢失,不如在接入时就把话说明白
            raise FeishuApiError(
                "机器人开启了「自定义关键词」安全设置,推送文案未包含该关键词。"
                "建议重新创建机器人并选择「签名校验」,再填入 Webhook 地址与签名密钥"
            )
        raise FeishuApiError(f"飞书机器人拒收消息:{msg}")
