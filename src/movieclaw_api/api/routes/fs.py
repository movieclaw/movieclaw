"""服务器文件系统浏览接口（Web 端目录选择器的数据源）。

「添加/编辑媒体库」不再让用户手打绝对路径，而是像 Jellyfin 一样弹出
目录浏览器逐级点选。本模块只做一件事：列出服务器上某个目录的子目录
——只读、仅目录、不返回文件，隐藏目录（点开头）不展示。

安全边界：挂在受保护区（须登录）。面向的是部署者本人，浏览服务器
目录本就是配置库路径的必要能力（与 Jellyfin/Radarr 的目录选择器一致），
因此不做路径白名单；但接口只读，绝不暴露文件内容。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Query

from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.fs import FsBrowseView, FsEntry
from movieclaw_api.schemas.response import ApiResponse, ok

router = APIRouter(prefix="/fs", tags=["fs"])


@router.get(
    "/browse",
    response_model=ApiResponse[FsBrowseView],
    summary="列出服务器上某个目录下有哪些子目录",
    operation_id="fs.browse",
)
def browse_directory(
    path: str | None = Query(default=None, description="要浏览的绝对路径，缺省为根目录 /"),
) -> ApiResponse[FsBrowseView]:
    """建库或修改根路径前，用它确认路径在服务器上到底长什么样。

    Docker 部署时尤其必要：你在宿主机 ``ls`` 看到的 ``/mnt/media``，容器里
    可能是 ``/data/media``，填错了扫描一个文件也找不到。

    只读、只列子目录：不返回文件，不显示点开头的隐藏目录，不改动任何东西。
    """
    # 磁盘 IO 是阻塞调用，路由声明为同步函数让 FastAPI 丢进线程池执行
    target = Path(path or "/").expanduser()
    if not target.is_absolute():
        raise BadRequestException(f"请输入绝对路径（以 / 开头）：{target}")
    # POSIX 的 normpath 会保留开头的双斜杠（"//a" 有特殊语义），这里统一归到单斜杠
    normalized = os.path.normpath(str(target))
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    target = Path(normalized)
    if not target.exists():
        raise BadRequestException(f"目录不存在：{target}")
    if not target.is_dir():
        raise BadRequestException(f"不是目录：{target}")

    entries: list[FsEntry] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=True):
                        entries.append(FsEntry(name=entry.name, path=str(target / entry.name)))
                except OSError:
                    # 个别条目 stat 失败（断链、无权限）直接跳过，不影响整页
                    continue
    except PermissionError as exc:
        raise BadRequestException(f"没有权限读取该目录：{target}") from exc

    entries.sort(key=lambda e: e.name.casefold())
    parent = None if target == target.parent else str(target.parent)
    return ok(FsBrowseView(path=str(target), parent=parent, entries=entries))
