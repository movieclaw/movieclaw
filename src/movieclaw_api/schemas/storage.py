"""缓存管理接口的响应模型（services/storage/service.py 的 dataclass 一一对应）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DirUsageView(BaseModel):
    key: str = Field(description="登记目录的稳定标识")
    title: str
    summary: str = Field(description="一句话用途（行内展示）")
    description: str = Field(description="完整说明与清理后果（悬停/确认时展示）")
    path: str = Field(description="运行期实际路径")
    group: Literal["cache", "data"] = Field(description="cache=可清理派生物，data=只展示")
    rebuild_cost: Literal["cheap", "expensive", "none"]
    clearable: bool = Field(description="是否允许「全部清空」")
    orphan_aware: bool = Field(description="是否提供「清理孤儿条目」")
    exists: bool
    bytes: int
    entries: int = Field(description="直接子项数量")


class UnregisteredEntryView(BaseModel):
    path: str
    bytes: int


class StorageUsageView(BaseModel):
    data_root: str
    disk_total: int
    disk_used: int
    disk_free: int
    cache_bytes: int
    data_bytes: int
    dirs: list[DirUsageView]
    unregistered: list[UnregisteredEntryView]
    computed_at: int = Field(description="统计时刻（Unix 秒）")


class StorageStateView(BaseModel):
    """面板读取占用时拿到的状态：统计很慢，所以接口给的是「上次结果 + 是否在算」。"""

    usage: StorageUsageView | None = Field(description="上一次统计的结果；进程内还没统计过时为空")
    computing: bool = Field(description="后台是否正在统计，前端据此显示进行中并轮询")
    error: str | None = Field(description="上一次统计失败的原因，旧结果仍然可用")


class CleanPayload(BaseModel):
    mode: Literal["all", "orphans"] = Field(description="all=全部清空，orphans=只删孤儿条目")


class CleanResultView(BaseModel):
    key: str
    mode: str
    removed: int
    skipped_busy: int = Field(description="因正在使用而跳过的条目数")
    freed_bytes: int
