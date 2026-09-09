"""Jellyfin GUID ↔ movieclaw 实体的结构化编码（设计文档 8.1）。

Jellyfin 协议里一切 ID 都是 32 位 hex GUID；movieclaw 是整型主键 + 季集数字对。
用无状态的结构化编码取代映射表：

    16 字节 = [魔数 "MC" 2B][类型 1B][保留 1B][载荷 12B]

集的 GUID 编码 (item, season, episode) 数字对而非 media_episode 行 id——
集行随元数据刷新可增删重建，数字对才是全局约定的稳定引用。

出参恒为小写（规避 Jellyfin stream 端点 Ordinal 大小写敏感匹配的坑，
设计文档 6.4）；入参宽松：剥横线/花括号、不区分大小写（对齐 Guid.Parse
接受 N/D/B 格式的行为）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

_MAGIC = b"MC"


class EntityKind(IntEnum):
    """GUID 类型字节。0x00 是用户/服务器等固定实体。"""

    FIXED = 0x00
    LIBRARY = 0x01
    ITEM = 0x02
    SEASON = 0x03
    EPISODE = 0x04
    MEDIA_SOURCE = 0x05
    PERSON = 0x06
    # 成员用户（多用户，docs/design/member-management.md §3.7）。超管保持
    # FIXED_USER 的原 GUID——已配对的客户端升级后不掉线。
    MEMBER_USER = 0x07
    # 合集（docs/design/library-collections.md 4.2）：载荷 = collection.id。
    # 自增主键不回收，GUID 天然稳定，与 library/item 同源，不需要映射表
    COLLECTION = 0x08


# 固定实体的载荷常量（FIXED 类型下细分）
FIXED_USER = 1
FIXED_ROOT = 2
#: 「合集」聚合视图（CollectionType=boxsets）。它是一个固定实体，
#: 走 FIXED 类型即可，不必为一个单例再开一个类型字节。
FIXED_COLLECTIONS = 3


@dataclass(frozen=True)
class EntityRef:
    """一个解码后的实体引用。season/episode 仅对相应类型有意义。"""

    kind: EntityKind
    entity_id: int
    season: int = 0
    episode: int = 0


def _pack(kind: EntityKind, entity_id: int, season: int = 0, episode: int = 0) -> str:
    payload = (
        entity_id.to_bytes(8, "big")
        + season.to_bytes(2, "big")
        + episode.to_bytes(2, "big")
    )
    return (_MAGIC + bytes([kind, 0]) + payload).hex()


def library_guid(library_id: int) -> str:
    return _pack(EntityKind.LIBRARY, library_id)


def item_guid(media_item_id: int) -> str:
    return _pack(EntityKind.ITEM, media_item_id)


def season_guid(media_item_id: int, season: int) -> str:
    return _pack(EntityKind.SEASON, media_item_id, season)


def episode_guid(media_item_id: int, season: int, episode: int) -> str:
    return _pack(EntityKind.EPISODE, media_item_id, season, episode)


def media_source_guid(library_file_id: int) -> str:
    return _pack(EntityKind.MEDIA_SOURCE, library_file_id)


def collection_guid(collection_id: int) -> str:
    return _pack(EntityKind.COLLECTION, collection_id)


def collections_view_guid() -> str:
    """「合集」聚合视图的 GUID（顶层 CollectionFolder，全局唯一）。"""
    return _pack(EntityKind.FIXED, FIXED_COLLECTIONS)


def person_guid(person_id: int) -> str:
    return _pack(EntityKind.PERSON, person_id)


def user_guid() -> str:
    return _pack(EntityKind.FIXED, FIXED_USER)


def member_user_guid(member_id: int) -> str:
    """成员的用户 GUID（按 member.id 无状态编码）。"""
    return _pack(EntityKind.MEMBER_USER, member_id)


def user_guid_for(member_id: int) -> str:
    """按登录身份（0=超管哨兵）取用户 GUID。"""
    return user_guid() if member_id == 0 else member_user_guid(member_id)


def root_guid() -> str:
    return _pack(EntityKind.FIXED, FIXED_ROOT)


def normalize_guid(value: str) -> str | None:
    """把任意合法 GUID 写法归一化为 32 位小写 hex；不合法返回 None。"""
    cleaned = value.strip().strip("{}()").replace("-", "").lower()
    if len(cleaned) != 32:
        return None
    try:
        bytes.fromhex(cleaned)
    except ValueError:
        return None
    return cleaned


def decode_guid(value: str) -> EntityRef | None:
    """解码一个 GUID。魔数/类型不匹配返回 None（调用方按 404 处理）。"""
    cleaned = normalize_guid(value)
    if cleaned is None:
        return None
    raw = bytes.fromhex(cleaned)
    if raw[:2] != _MAGIC:
        return None
    try:
        kind = EntityKind(raw[2])
    except ValueError:
        return None
    return EntityRef(
        kind=kind,
        entity_id=int.from_bytes(raw[4:12], "big"),
        season=int.from_bytes(raw[12:14], "big"),
        episode=int.from_bytes(raw[14:16], "big"),
    )


def is_empty_guid(value: str) -> bool:
    """是否为全零 GUID（Jellyfin 里全零 = 根/未指定）。"""
    cleaned = normalize_guid(value)
    return cleaned == "0" * 32
