"""播放链路（设计文档 §6）：PlaybackInfo、取流与整文件下载。

- PlaybackInfo：不解析 DeviceProfile，恒返回未经设备适配的 MediaSources
  （等价于"无转码权限的 Jellyfin"，协议合法）；
- /Videos/{id}/stream：本地文件走 FileResponse（原生 Range/206/HEAD）；
  strm 条目读内容后 302 到云端直链，不代理（零网盘流量）。
  鉴权：真 Jellyfin 此接口匿名，我们要求 token（偏离③，公网暴露考量）。
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile
from movieclaw_jellyfin.catalog import media_source_dto
from movieclaw_jellyfin.errors import bad_request_text, not_found
from movieclaw_jellyfin.ids import EntityKind, decode_guid, media_source_guid
from movieclaw_jellyfin.security import RequestIdentity, require_device
from movieclaw_playback.streaming import (
    DisconnectAwareFileResponse,
    container_mime_type,
    is_strm,
    register_device_stream,
    resolve_strm_url,
    unregister_device_stream,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_device)])


async def _files_for_ref(ref) -> list[LibraryFile]:
    """按条目/单元 GUID 取在位文件行（多版本多行，稳定排序）。"""
    async with get_database().session() as session:
        q = select(LibraryFile).where(
            LibraryFile.media_item_id == ref.entity_id,
            LibraryFile.missing_since.is_(None),
        )
        if ref.kind == EntityKind.EPISODE:
            q = q.where(
                LibraryFile.season_number == ref.season,
                LibraryFile.episode_number == ref.episode,
            )
        elif ref.kind == EntityKind.ITEM:
            q = q.where(LibraryFile.season_number == 0, LibraryFile.episode_number == 0)
        rows = list((await session.execute(q)).scalars())
    rows.sort(key=lambda f: f.id)
    return rows


def _select_source(
    files: list[LibraryFile], media_source_id: str | None, item_guid_raw: str
) -> list[LibraryFile]:
    """mediaSourceId 筛选：缺省全部；等于 itemId 时回落第一个（设计文档 6.2）。"""
    if not media_source_id:
        return files
    normalized = (media_source_id or "").lower().replace("-", "")
    for f in files:
        if media_source_guid(f.id) == normalized:
            return [f]
    item_norm = item_guid_raw.lower().replace("-", "")
    if normalized == item_norm and files:
        return [files[0]]
    return []


@router.get("/Items/{item_id}/PlaybackInfo")
@router.post("/Items/{item_id}/PlaybackInfo")
async def playback_info(request: Request, item_id: str) -> JSONResponse:
    ref = decode_guid(item_id)
    if ref is None or ref.kind not in (EntityKind.ITEM, EntityKind.EPISODE):
        raise not_found()

    # query 优先于 body；DeviceProfile 与 LiveStreamId 一律忽略（后者会短路
    # 源解析，绝不能当 mediaSourceId 用）
    media_source_id = request.query_params.get("mediaSourceId")
    if media_source_id is None and request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            lowered = {str(k).lower(): v for k, v in body.items()}
            raw = lowered.get("mediasourceid")
            media_source_id = str(raw) if raw else None

    files = await _files_for_ref(ref)
    selected = _select_source(files, media_source_id, item_id)
    if not selected:
        return JSONResponse(
            {"MediaSources": [], "ErrorCode": "NoCompatibleStream"}
        )
    # 播放协商是唯一现读 strm 的场景：直链多带时效签名，须现读现用；
    # 解析失败的版本剔除，全部失败按"无可播源"应答
    sources = [s for f in selected if (s := media_source_dto(f, resolve_strm=True))]
    if not sources:
        return JSONResponse({"MediaSources": [], "ErrorCode": "NoCompatibleStream"})
    return JSONResponse(
        {
            "MediaSources": sources,
            "PlaySessionId": secrets.token_hex(16),
        }
    )


@router.get("/Videos/{item_id}/stream")
@router.head("/Videos/{item_id}/stream")
@router.get("/Videos/{item_id}/stream.{container}")
@router.head("/Videos/{item_id}/stream.{container}")
async def video_stream(
    request: Request,
    item_id: str,
    container: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> Response:
    ref = decode_guid(item_id)
    if ref is None or ref.kind not in (EntityKind.ITEM, EntityKind.EPISODE):
        raise not_found()
    static = (request.query_params.get("static") or "").lower() == "true"
    if not static:
        # 无 static=true 本应转码；我们不转码（偏离⑨）
        raise bad_request_text()

    files = await _files_for_ref(ref)
    selected = _select_source(files, request.query_params.get("mediaSourceId"), item_id)
    if not selected:
        raise not_found()
    f = selected[0]

    if is_strm(f.file_path):
        url = resolve_strm_url(f.file_path)
        if url is None:
            raise not_found()
        # 302 直链：HEAD 同样 302、不塞 body；重定向目标自己支持 Range
        return RedirectResponse(url, status_code=302)

    path = Path(f.file_path)
    if not path.is_file():
        raise not_found()
    media_type = container_mime_type(container or f.container or path.suffix)
    # 停止播放并不保证客户端立刻关闭 Range 连接。按已认证设备登记这条流，
    # 让 /Sessions/Playing/Stopped 能主动停止读盘；TCP 断连仍是第二道兜底。
    device_id = identity.device.device_id
    session_stopped = register_device_stream(device_id)
    return DisconnectAwareFileResponse(
        path,
        media_type=media_type,
        is_disconnected=request.is_disconnected,
        session_stopped=session_stopped,
        on_close=lambda: unregister_device_stream(device_id, session_stopped),
    )


@router.get("/Items/{item_id}/Download")
@router.head("/Items/{item_id}/Download")
@router.get("/Items/{item_id}/File")
@router.head("/Items/{item_id}/File")
async def download_item(request: Request, item_id: str) -> Response:
    """整文件下载（Jellyfin LibraryController 的 Download/File 两条路由）。

    我们在 UserDto.Policy 里宣告了 ``EnableContentDownloading: true``，客户端
    （VidHub 等）据此显示下载按钮，点击后打的就是 /Items/{id}/Download——
    不实现它下载会直接 404 失败。语义对齐播放取流：

    - 本地文件回 FileResponse（原生 Range/206，下载器可断点续传）；
      Download 按真 Jellyfin 带 attachment 文件名，File 不带；
    - strm 条目与取流同策略（偏离，真 Jellyfin 会回 .strm 文本本身）：
      302 到云端直链，客户端下载到的是真实媒体文件，服务器零流量；
    - ``mediaSourceId`` 为超集扩展：真 Jellyfin 此接口只认条目主文件，
      我们允许客户端指定下载某个版本，缺省优先本地文件版本。
    """
    ref = decode_guid(item_id)
    if ref is None or ref.kind not in (EntityKind.ITEM, EntityKind.EPISODE):
        raise not_found()

    files = await _files_for_ref(ref)
    media_source_id = request.query_params.get("mediaSourceId")
    selected = _select_source(files, media_source_id, item_id)
    if not selected:
        logger.warning(
            "下载请求未匹配到文件版本：item=%s mediaSourceId=%s（该条目共 %d 个在位文件）",
            item_id, media_source_id, len(files),
        )
        raise not_found()
    # 缺省优先本地文件版本：strm 版本 302 后能否下载取决于云端直链对下载器
    # 是否宽容（签名/UA 校验），本地文件由我们自己响应、行为确定
    local_first = sorted(selected, key=lambda x: is_strm(x.file_path))
    f = local_first[0]

    if is_strm(f.file_path):
        url = resolve_strm_url(f.file_path)
        if url is None:
            logger.warning("下载失败：strm 直链解析失败，file=%s", f.file_path)
            raise not_found()
        logger.info("下载重定向到云端直链：item=%s file=%s", item_id, f.file_path)
        return RedirectResponse(url, status_code=302)

    path = Path(f.file_path)
    if not path.is_file():
        logger.warning(
            "下载失败：本地文件不存在或容器内不可见（检查 Docker 挂载路径）：%s", path
        )
        raise not_found()
    logger.info(
        "开始下载：item=%s file=%s size=%s", item_id, path.name, f.size_bytes
    )
    # 下载不是播放会话：用普通 FileResponse，不登记设备流，避免用户边下边看
    # 时点"停止播放"误杀下载读盘（TCP 断连兜底对下载器依然生效）
    is_download = request.url.path.lower().endswith("/download")
    return FileResponse(
        path,
        media_type=container_mime_type(f.container or path.suffix),
        filename=path.name if is_download else None,
    )
