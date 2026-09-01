"""通道层共用的入站媒体下载(微信 / Telegram / Discord 三家同一口径)。

三个平台拿图的前半段各不相同——微信是 CDN 密文 + AES 密钥,Telegram 要先
getFile 换下载路径,Discord 直接给带签名的 CDN 链接——但**后半段完全一样**:
按上限流式拉字节,失败只丢这一张图。这里收敛的就是后半段,以及「一张图最大
多少」这个必须三家一致的口径(再往后是 API 层统一的压缩与入库)。
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("movieclaw_channel.media")

#: 单张入站图片的下载上限。这是防「一条消息拖垮收消息循环」的粗闸,不是
#: 入库限额——API 层还会把图压到 5MB 以内(MAX_IMAGE_BYTES)才落盘。
#: 取 20MB 是因为 Telegram 的 getFile 明确只支持下载 20MB 以内的文件,
#: 三家统一到这个最紧的口径,行为可预期。
MAX_INBOUND_IMAGE_BYTES = 20 * 1024 * 1024


class MediaDownloadError(Exception):
    """入站媒体下载失败(HTTP 错误或超出体积上限)。"""


async def download_capped(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = MAX_INBOUND_IMAGE_BYTES,
    timeout_s: float = 30.0,
    label: str = "媒体",
) -> bytes:
    """流式下载一份媒体字节,超过 max_bytes 立即中断。

    流式而不是 ``resp.content``:后者会把整个响应读进内存后才让我们判断大小,
    遇到超大文件时上限就形同虚设。
    """
    async with client.stream("GET", url, timeout=httpx.Timeout(timeout_s)) as resp:
        if resp.status_code != 200:
            raise MediaDownloadError(f"{label}下载失败 HTTP {resp.status_code}")
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise MediaDownloadError(f"{label}超过 {max_bytes // (1024 * 1024)}MB 上限")
            chunks.append(chunk)
    return b"".join(chunks)
