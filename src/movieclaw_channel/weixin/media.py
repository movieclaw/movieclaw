"""微信入站图片的下载与解密(对照 Tencent/openclaw-weixin 的 CDN 实现移植)。

iLink 的图片不随消息体下发,而是给一条 CDN 引用:

- ``image_item.media.full_url``:服务端直接给出的完整下载地址(新协议);
- 没有 full_url 时回退到 ``{cdn_base}/download?encrypted_query_param=...``
  自行拼接(参考实现同款回退)。

下载到的通常是 **AES-128-ECB(PKCS7)** 密文,密钥来自消息本身:

- ``image_item.aeskey``:hex 字符串,优先用它;
- ``media.aes_key``:base64。历史上有两种编码并存(参考实现注释 "in the wild"):
  base64(16 字节原始密钥),以及 base64(32 位 hex 字符串)——两种都要认;
- 两者都没有:该图是明文直下(不解密)。

网络与 iLink 网关同口径:国内直连,不走 movieclaw_net 的境外代理。
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger("movieclaw_channel.weixin.media")

#: 默认 CDN 基址(仅在服务端没给 full_url 时用于拼接下载地址)
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

#: 消息 item 类型:2=图片(1=文本 3=语音 4=文件 5=视频,后三者本期不接)
ITEM_IMAGE = 2

@dataclass(frozen=True, slots=True)
class ImageRef:
    """一张待下载图片的引用(下载地址 + 可选解密密钥)。"""

    url: str
    #: 16 字节 AES 密钥;None 表示 CDN 上就是明文
    aes_key: bytes | None


def _parse_aes_key(image_item: dict[str, Any]) -> bytes | None:
    """解析图片的 AES 密钥,兼容 hex / base64 / base64(hex) 三种编码。"""
    hex_key = str(image_item.get("aeskey") or "")
    if hex_key:
        try:
            key = bytes.fromhex(hex_key)
        except ValueError:
            logger.warning("微信图片 aeskey 不是合法 hex,改用 media.aes_key")
        else:
            if len(key) == 16:
                return key
            logger.warning("微信图片 aeskey 长度异常(%d 字节),改用 media.aes_key", len(key))

    raw_key = str((image_item.get("media") or {}).get("aes_key") or "")
    if not raw_key:
        return None
    try:
        decoded = base64.b64decode(raw_key, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("微信图片 aes_key 不是合法 base64,按明文图处理")
        return None
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        # base64(hex 字符串) 的双重编码形态
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            pass
    logger.warning("微信图片 aes_key 长度异常(%d 字节),按明文图处理", len(decoded))
    return None


def collect_image_refs(
    item_list: list[dict[str, Any]] | None, *, cdn_base_url: str = CDN_BASE_URL
) -> list[ImageRef]:
    """从消息的 item_list 里挑出全部图片,组装下载引用(无图返回空表)。"""
    refs: list[ImageRef] = []
    for item in item_list or []:
        if item.get("type") != ITEM_IMAGE:
            continue
        image_item = item.get("image_item") or {}
        media = image_item.get("media") or {}
        url = str(media.get("full_url") or "")
        if not url:
            query = str(media.get("encrypt_query_param") or "")
            if not query:
                logger.warning("微信图片缺少下载地址(full_url/encrypt_query_param 都为空),已跳过")
                continue
            base = cdn_base_url.rstrip("/")
            url = f"{base}/download?encrypted_query_param={quote(query, safe='')}"
        refs.append(ImageRef(url=url, aes_key=_parse_aes_key(image_item)))
    return refs


def decrypt_image(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密 CDN 密文;PKCS7 去填充失败时退回不去填充的明文。

    iLink 是半公开协议,填充口径存在版本差异。去不掉填充时尾部最多多出
    15 字节垃圾——JPEG/PNG 解码器都能容忍,总比整张图丢掉强。
    """
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    if not plain:
        return plain
    pad = plain[-1]
    if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
        return plain[:-pad]
    logger.debug("微信图片 PKCS7 去填充未命中,按原样返回明文")
    return plain
