"""``Authorization: MediaBrowser ...`` 头解析（设计文档 4.1）。

按 Jellyfin 客户端实际发送的头格式解析，而非简单 split：
- 头值无空格 → 整头作废；scheme 取第一个空格前部分，MediaBrowser/Emby
  大小写不敏感；
- 引号内的逗号不是分隔符（``x="123,123"`` → 值 ``123,123``）；
- 键 Trim 去空白且**大小写敏感**（精确 Client/Device/DeviceId/Version/Token）；
- 值先去首尾引号再 URL 解码；空值片段丢弃；值不要求带引号。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote_plus

_SCHEMES = ("mediabrowser", "emby")


@dataclass(frozen=True)
class AuthorizationInfo:
    """从头里解析出的客户端标识。缺失的键为空串。"""

    client: str = ""
    device: str = ""
    device_id: str = ""
    version: str = ""
    token: str = ""

    @property
    def has_identity(self) -> bool:
        """登录接口要求四键齐全（缺任一 → 400，SessionManager.cs:1643-1646）。"""
        return bool(self.client and self.device and self.device_id and self.version)


def parse_authorization_header(header: str | None) -> AuthorizationInfo | None:
    """解析 Authorization / X-Emby-Authorization 头；scheme 不认识返回 None。"""
    if not header:
        return None
    space = header.find(" ")
    if space < 0:
        return None
    if header[:space].lower() not in _SCHEMES:
        return None

    parts = _split_parts(header[space + 1 :])
    return AuthorizationInfo(
        client=parts.get("Client", ""),
        device=parts.get("Device", ""),
        device_id=parts.get("DeviceId", ""),
        version=parts.get("Version", ""),
        token=parts.get("Token", ""),
    )


def _split_parts(value: str) -> dict[str, str]:
    """把 ``k1="v1", k2=v2`` 切成字典。

    先按引号外的逗号切段，再在每段第一个 ``=`` 处分出键和值：键去空白、
    区分大小写；值去空白和首尾引号后做 URL 解码；键或值为空的段丢弃。
    """
    segments: list[str] = []
    current: list[str] = []
    quoted = False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
    segments.append("".join(current))

    result: dict[str, str] = {}
    for segment in segments:
        key, sep, raw = segment.partition("=")
        key = key.strip()
        raw = raw.strip().strip('"')
        if sep and key and raw:
            result[key] = unquote_plus(raw)
    return result
