"""显示偏好接口（DisplayPreferencesController.cs）。Infuse 握手在 GroupingOptions 之后必调。

真 Jellyfin 按 (user, item, client) 三元组持久化每个视图的排序/滚动方向等
展示偏好。我们不做偏好持久化——GET 恒返回出厂默认（字段与默认值照抄
DisplayPreferences 实体构造器与 DisplayPreferencesDto 构造器），POST 收下
即丢、返回 204。效果等价于"用户从未改过任何显示偏好"的原版服务器；
播放器本地自有偏好缓存，足以支撑正常使用。
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from movieclaw_jellyfin.security import require_device

router = APIRouter(dependencies=[Depends(require_device)])


def _preferences_guid(display_preferences_id: str) -> str:
    """复刻 Guid.TryParse 失败则 GetMD5 的 Id 规则（DisplayPreferencesController.cs:58-61）。

    C# 的 new Guid(byte[]) 对前三组做小端重排，Python 里等价于 bytes_le；
    输出用 Guid 默认 "D" 格式（带连字符）——注意与条目 Id 的 "N" 格式不同，
    这里照抄真实现的 ToString()。
    """
    try:
        return str(uuid.UUID(display_preferences_id))
    except ValueError:
        digest = hashlib.md5(display_preferences_id.encode("utf-8")).digest()  # noqa: S324
        return str(uuid.UUID(bytes_le=digest))


@router.get("/DisplayPreferences/{display_preferences_id}")
async def get_display_preferences(
    display_preferences_id: str, request: Request, client: str = ""
) -> JSONResponse:
    return JSONResponse(
        {
            "Id": _preferences_guid(display_preferences_id),
            "SortBy": "SortName",
            "RememberIndexing": False,
            "PrimaryImageHeight": 250,
            "PrimaryImageWidth": 250,
            "CustomPrefs": {
                "chromecastVersion": "stable",
                "skipForwardLength": "30000",
                "skipBackLength": "10000",
                "enableNextVideoInfoOverlay": "False",
                # 真实现里这两项是 string?，null 值在字典里保留输出
                "tvhome": None,
                "dashboardTheme": None,
            },
            "ScrollDirection": "Horizontal",
            "ShowBackdrop": True,
            "RememberSorting": False,
            "SortOrder": "Ascending",
            "ShowSidebar": False,
            "Client": client or "emby",
        }
    )


@router.post("/DisplayPreferences/{display_preferences_id}")
async def update_display_preferences(display_preferences_id: str, request: Request) -> Response:
    # 不持久化：读端恒为出厂默认，写端只应答 204 让客户端流程走通
    return Response(status_code=204)
