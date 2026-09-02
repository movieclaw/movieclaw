"""微信入站图片:CDN 引用解析、AES 密钥的三种编码、解密与整条链路。

对照参考实现 Tencent/openclaw-weixin 的口径:密钥编码在真实流量里存在
版本差异(hex / base64 原始 16 字节 / base64(hex 字符串)),三种都要认;
下载地址优先用服务端给的 full_url,没有才自己拼 CDN 地址。
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from movieclaw_channel.weixin.media import (
    CDN_BASE_URL,
    collect_image_refs,
    decrypt_image,
)

_KEY = bytes(range(16))


def _encrypt(plain: bytes, key: bytes = _KEY) -> bytes:
    """AES-128-ECB + PKCS7 加密(模拟 CDN 上的密文)。"""
    pad = 16 - len(plain) % 16
    padded = plain + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _image_item(**image_item) -> list[dict]:
    return [{"type": 2, "image_item": image_item}]


# ---------------------------------------------------------------------------
# 引用解析
# ---------------------------------------------------------------------------


def test_prefers_full_url_from_server() -> None:
    refs = collect_image_refs(
        _image_item(media={"full_url": "https://cdn.example/a.dat", "aes_key": ""})
    )
    assert [r.url for r in refs] == ["https://cdn.example/a.dat"]
    assert refs[0].aes_key is None


def test_falls_back_to_cdn_download_url() -> None:
    refs = collect_image_refs(_image_item(media={"encrypt_query_param": "a b&c"}))
    assert refs[0].url == f"{CDN_BASE_URL}/download?encrypted_query_param=a%20b%26c"


def test_image_without_download_ref_is_skipped() -> None:
    assert collect_image_refs(_image_item(media={})) == []


def test_non_image_items_ignored() -> None:
    items = [{"type": 1, "text_item": {"text": "你好"}}, {"type": 3, "voice_item": {}}]
    assert collect_image_refs(items) == []
    assert collect_image_refs(None) == []


def test_multiple_images_keep_order() -> None:
    items = _image_item(media={"full_url": "https://cdn/1"}) + _image_item(
        media={"full_url": "https://cdn/2"}
    )
    assert [r.url for r in collect_image_refs(items)] == ["https://cdn/1", "https://cdn/2"]


# ---------------------------------------------------------------------------
# 密钥编码(三种形态都要认)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "image_item",
    [
        # 1) image_item.aeskey:hex 字符串(优先)
        {"aeskey": _KEY.hex(), "media": {"full_url": "https://cdn/x"}},
        # 2) media.aes_key:base64(原始 16 字节)
        {"media": {"full_url": "https://cdn/x", "aes_key": base64.b64encode(_KEY).decode()}},
        # 3) media.aes_key:base64(32 位 hex 字符串)的双重编码
        {
            "media": {
                "full_url": "https://cdn/x",
                "aes_key": base64.b64encode(_KEY.hex().encode()).decode(),
            }
        },
    ],
)
def test_aes_key_encodings(image_item: dict) -> None:
    refs = collect_image_refs(_image_item(**image_item))
    assert refs[0].aes_key == _KEY


def test_broken_aes_key_degrades_to_plain() -> None:
    """密钥无法解析时按明文图处理,而不是整张图丢掉。"""
    refs = collect_image_refs(
        _image_item(aeskey="not-hex", media={"full_url": "https://cdn/x", "aes_key": "!!!"})
    )
    assert refs[0].aes_key is None


# ---------------------------------------------------------------------------
# 解密
# ---------------------------------------------------------------------------


def test_decrypt_round_trip() -> None:
    plain = b"\x89PNG\r\n\x1a\n" + b"payload" * 9
    assert decrypt_image(_encrypt(plain), _KEY) == plain


def test_decrypt_keeps_payload_when_padding_missing() -> None:
    """没有 PKCS7 填充的版本差异:宁可尾部多几字节垃圾,也不丢整张图。"""
    plain = b"\x89PNG\r\n\x1a\n" + bytes(24)  # 16 的整数倍,末字节 0 不是合法填充
    encryptor = Cipher(algorithms.AES(_KEY), modes.ECB()).encryptor()
    cipher = encryptor.update(plain) + encryptor.finalize()
    assert decrypt_image(cipher, _KEY) == plain
