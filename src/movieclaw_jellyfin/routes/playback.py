"""播放链路（设计文档 §6）：PlaybackInfo、取流、整文件下载与外挂字幕。

- PlaybackInfo：不解析 DeviceProfile，恒返回未经设备适配的 MediaSources
  （等价于"无转码权限的 Jellyfin"，协议合法）；外挂字幕流带
  DeliveryMethod/DeliveryUrl（无条件输出，jellyfin-subtitle.md §4.3），
  DefaultAudio/SubtitleStreamIndex 记忆优先（§4.3/S3）；
- /Videos/{id}/stream：本地文件走 FileResponse（原生 Range/206/HEAD）；
  strm 条目读内容后 302 到云端直链，不代理（零网盘流量）。
  鉴权：真 Jellyfin 此接口匿名，我们要求 token（偏离③，公网暴露考量）；
- /Videos/{id}/{msId}/Subtitles/{idx}[/{ticks}]/Stream.{fmt}：外挂字幕
  输出（§4.4）——内容与格式转换来自 movieclaw_playback.subtitles，
  本层只做 GUID/编号反解与 HTTP 形态。
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from movieclaw_api.services.library.access import member_visible_ids
from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile
from movieclaw_jellyfin.catalog import (
    index_for_subtitle_track,
    media_source_dto,
    subtitle_track_for_index,
)
from movieclaw_jellyfin.errors import bad_request_text, not_found
from movieclaw_jellyfin.ids import (
    EntityKind,
    EntityRef,
    decode_guid,
    episode_guid,
    item_guid,
    media_source_guid,
)
from movieclaw_jellyfin.security import RequestIdentity, require_device
from movieclaw_playback import activity
from movieclaw_playback import state as playback_state
from movieclaw_playback.events import ClientInfo
from movieclaw_playback.streaming import (
    DisconnectAwareFileResponse,
    container_mime_type,
    is_strm,
    register_device_stream,
    resolve_strm_url,
    unregister_device_stream,
)
from movieclaw_playback.subtitles import (
    SUBTITLE_OFF,
    SubtitleServeError,
    resolve_default_audio,
    resolve_default_subtitle,
    resolve_external_subtitle,
    serve_subtitle_async,
)

logger = logging.getLogger("movieclaw_jellyfin.playback")

router = APIRouter(dependencies=[Depends(require_device)])


async def _files_for_ref(ref, member_id: int = 0) -> list[LibraryFile]:
    """按条目/单元 GUID 取在位文件行（多版本多行，稳定排序）。

    成员的库可见性在这里强制（三个播放处理器共用本装载点）：白名单外
    库里的文件直接不出现，条目因此对该成员表现为 404——GUID 可枚举，
    不能只在浏览路径挡、放播放路径直进（member-management.md §3.6）。
    """
    async with get_database().session() as session:
        visible = await member_visible_ids(session, member_id)
        q = select(LibraryFile).where(
            LibraryFile.media_item_id == ref.entity_id,
            LibraryFile.in_place(),
        )
        if visible is not None:
            q = q.where(LibraryFile.library_id.in_(visible))
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
async def playback_info(
    request: Request,
    item_id: str,
    identity: RequestIdentity = Depends(require_device),
) -> JSONResponse:
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

    files = await _files_for_ref(ref, identity.device.member_id)
    selected = _select_source(files, media_source_id, item_id)
    if not selected:
        return JSONResponse({"MediaSources": [], "ErrorCode": "NoCompatibleStream"})
    # 播放协商是唯一现读 strm 的场景：直链多带时效签名，须现读现用；
    # 解析失败的版本剔除，全部失败按"无可播源"应答
    pairs = [(f, s) for f in selected if (s := media_source_dto(f, resolve_strm=True))]
    if not pairs:
        return JSONResponse({"MediaSources": [], "ErrorCode": "NoCompatibleStream"})

    # 轨记忆按 (成员, 单元) 一次读出，各版本共用（jellyfin-subtitle.md §6.3）
    unit = (
        (ref.entity_id, ref.season, ref.episode)
        if ref.kind == EntityKind.EPISODE
        else (ref.entity_id, 0, 0)
    )
    async with get_database().session() as session:
        audio_mem, subtitle_mem = await playback_state.get_remembered_tracks(
            session, unit, member_id=identity.device.member_id
        )
    for f, source in pairs:
        _apply_subtitle_delivery(source, f, ref, identity.device.token)
        _apply_default_tracks(source, f, audio_mem, subtitle_mem)
    return JSONResponse(
        {
            "MediaSources": [s for _, s in pairs],
            "PlaySessionId": secrets.token_hex(16),
        }
    )


def _range_start(request: Request) -> int:
    """请求 Range 的起始字节；无 Range 或形态不认识按 0（从头开始）。

    只取起点：整文件下载据此还原真实下载位置（断点续传从中间开始，
    只看本次已传字节会把进度算回 0）。后缀式 ``bytes=-N`` 表达"最后 N 字节"，
    起点依赖文件长度，这里不做推断，按 0 处理（进度退化为保守读数）。
    """
    raw = (request.headers.get("Range") or "").strip().lower()
    if not raw.startswith("bytes="):
        return 0
    first = raw[len("bytes=") :].split(",")[0].strip()
    start = first.split("-")[0].strip()
    if not start.isdigit():
        return 0
    return int(start)


def _stream_unit(ref: EntityRef) -> playback_state.Unit:
    """取流目标的播放单元（与 playstate._leaf_unit 同口径：电影 (0,0) 哨兵）。"""
    if ref.kind == EntityKind.EPISODE:
        return (ref.entity_id, ref.season, ref.episode)
    return (ref.entity_id, 0, 0)


def _identity_client(identity: RequestIdentity) -> ClientInfo:
    """设备凭据 → 协议无关的客户端信息（活动注册表的设备展示字段）。"""
    device = identity.device
    return ClientInfo(
        name=device.client or "",
        device_name=device.device_name or "",
        device_id=device.device_id or "",
        version=device.version or "",
    )


def _unit_item_guid(ref: EntityRef) -> str:
    """播放单元的规范条目 GUID（DeliveryUrl 用，恒小写无横线）。"""
    if ref.kind == EntityKind.EPISODE:
        return episode_guid(ref.entity_id, ref.season, ref.episode)
    return item_guid(ref.entity_id)


def _apply_subtitle_delivery(source: dict, f: LibraryFile, ref: EntityRef, token: str) -> None:
    """给外挂字幕流补投递字段（仅 PlaybackInfo 场景，jellyfin-subtitle.md §4.3）。

    无条件输出是既定偏离：真 Jellyfin 无 DeviceProfile 时不输出，我们不
    解析 profile，无条件输出是外挂字幕可用的必要超集。fmt 恒为源格式
    （Infuse/VidHub 对 srt/ass 全支持，无需预转换）；ApiKey 直接拼在 URL
    上——播放器媒体内核拉字幕不带自定义认证头。
    """
    item_g = _unit_item_guid(ref)
    ms_g = media_source_guid(f.id)
    for stream in source.get("MediaStreams", []):
        if stream.get("Type") != "Subtitle" or not stream.get("IsExternal"):
            continue
        fmt = str(stream.get("Codec") or "srt")
        # Codec 是 Jellyfin 惯用名（subrip/webvtt），URL 后缀要用文件格式
        fmt = {"subrip": "srt", "webvtt": "vtt"}.get(fmt, fmt)
        stream["DeliveryMethod"] = "External"
        stream["IsExternalUrl"] = False
        stream["DeliveryUrl"] = (
            f"/Videos/{item_g}/{ms_g}/Subtitles/{stream['Index']}/0/Stream.{fmt}?ApiKey={token}"
        )


def _apply_default_tracks(
    source: dict, f: LibraryFile, audio_mem: str | None, subtitle_mem: str | None
) -> None:
    """默认轨输出：记忆优先、失效回落选择策略（jellyfin-subtitle.md §4.3/S3）。

    - 字幕：记忆 "off" → -1（协议里"-1=用户明确不要字幕"是有效值）；
      记忆有效 → 其 Index；无记忆/失效 → Default 模式策略；策略也无 →
      不输出字段（客户端默认不开字幕）；
    - 音轨：记忆有效则覆盖 media_source_dto 按 default 旗标算出的值。
    """
    audio_index = resolve_default_audio(f, audio_mem)
    if audio_index is not None:
        # Jellyfin 把外挂流置前，video/audio 的协议编号随外挂数量整体后移。
        source["DefaultAudioStreamIndex"] = len(f.external_subtitles or []) + 1 + audio_index

    track = resolve_default_subtitle(f, subtitle_mem)
    if track == SUBTITLE_OFF:
        source["DefaultSubtitleStreamIndex"] = -1
    elif track is not None:
        index = index_for_subtitle_track(f, track)
        if index is not None:
            source["DefaultSubtitleStreamIndex"] = index


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

    files = await _files_for_ref(ref, identity.device.member_id)
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
    # MIME 优先级对齐 StreamingHelpers：URL 后缀 → ?container= query → 真实容器
    media_type = container_mime_type(
        container or request.query_params.get("container") or f.container or path.suffix
    )
    # 停止播放并不保证客户端立刻关闭 Range 连接。按已认证设备登记这条流，
    # 让 /Sessions/Playing/Stopped 能主动停止读盘；TCP 断连仍是第二道兜底。
    device_id = identity.device.device_id
    session_stopped = register_device_stream(device_id)
    # 顺带登记到播放活动注册表：活动页「观看」视角据此展示实时传输速率
    meter = activity.register_stream(
        device_id=device_id,
        kind=activity.STREAM_KIND_PLAY,
        member_id=identity.device.member_id,
        unit=_stream_unit(ref),
        file_id=f.id,
        file_name=path.name,
        size_bytes=f.size_bytes,
        client=_identity_client(identity),
    )

    def _close() -> None:
        unregister_device_stream(device_id, session_stopped)
        activity.unregister_stream(meter)

    return DisconnectAwareFileResponse(
        path,
        media_type=media_type,
        session_stopped=session_stopped,
        byte_sink=meter.add,
        on_close=_close,
    )


@router.get("/Items/{item_id}/Download")
@router.head("/Items/{item_id}/Download")
@router.get("/Items/{item_id}/File")
@router.head("/Items/{item_id}/File")
async def download_item(
    request: Request,
    item_id: str,
    identity: RequestIdentity = Depends(require_device),
) -> Response:
    """整文件下载（Jellyfin LibraryController 的 Download/File 两条路由）。

    我们在 UserDto.Policy 里宣告了 ``EnableContentDownloading: true``，客户端
    （VidHub 等）据此显示下载按钮，点击后打的就是 /Items/{id}/Download——
    不实现它下载会直接 404 失败。语义对齐播放取流：

    - 本地文件回 FileResponse（原生 Range/206，下载器可断点续传）；
      Download 按真 Jellyfin 带 attachment 文件名，File 不带；
    - strm 条目与取流同策略（偏离，真 Jellyfin 会回 .strm 文本本身）：
      302 到云端直链，客户端下载到的是真实媒体文件，服务器零流量；
    - ``mediaSourceId`` 为超集扩展：真 Jellyfin 此接口只认条目主文件，
      我们允许客户端指定下载某个版本，缺省取第一个。
    """
    ref = decode_guid(item_id)
    if ref is None or ref.kind not in (EntityKind.ITEM, EntityKind.EPISODE):
        raise not_found()

    files = await _files_for_ref(ref, identity.device.member_id)
    selected = _select_source(files, request.query_params.get("mediaSourceId"), item_id)
    if not selected:
        raise not_found()
    f = selected[0]

    if is_strm(f.file_path):
        url = resolve_strm_url(f.file_path)
        if url is None:
            raise not_found()
        return RedirectResponse(url, status_code=302)

    path = Path(f.file_path)
    if not path.is_file():
        raise not_found()
    # 下载不是播放会话：不登记设备流，避免用户边下边看时点"停止播放"误杀
    # 下载读盘；但下载器取消/断网后必须停止读盘（裸 FileResponse 会把几十 GB
    # 读到底）。Content-Length 由响应类统一保留，下载器据此显示进度与续传
    is_download = request.url.path.lower().endswith("/download")
    # 独立登记为下载活动：活动页「观看」视角展示"谁在下哪个文件、多快"
    meter = activity.register_stream(
        device_id=identity.device.device_id,
        kind=activity.STREAM_KIND_DOWNLOAD,
        member_id=identity.device.member_id,
        unit=_stream_unit(ref),
        file_id=f.id,
        file_name=path.name,
        size_bytes=f.size_bytes,
        client=_identity_client(identity),
        start_offset=_range_start(request),
    )
    return DisconnectAwareFileResponse(
        path,
        media_type=container_mime_type(f.container or path.suffix),
        filename=path.name if is_download else None,
        byte_sink=meter.add,
        on_close=lambda: activity.unregister_stream(meter),
    )


# 路由模板用小写 stream.{fmt}：大小写归一化中间件的 "stream." 前缀规范
# 形态已被取流路由（/Videos/{id}/stream.{container}）注册为小写，任何
# 大小写的来路（含 DeliveryUrl 的协议形态 Stream.srt）都会被归一到小写
_SUBTITLE_FORMAT_ALIASES = {
    "subrip": "srt",
    "webvtt": "vtt",
}


def _normalize_subtitle_format(value: str) -> str:
    """Jellyfin/FFmpeg codec 名 → 字幕服务使用的文件格式名。

    MediaStream.Codec 按 Jellyfin 惯例输出 subrip/webvtt，VidHub 会用它
    自行构造 Stream.{format}，而不是照抄 DeliveryUrl 的 srt/vtt 后缀。
    这里仅归一协议方言；是否需要实际文本转换仍由 B 层比较源/目标格式。
    """
    normalized = value.strip().lower()
    return _SUBTITLE_FORMAT_ALIASES.get(normalized, normalized)


@router.get("/Videos/{item_id}/{media_source_id}/Subtitles/{stream_index}/stream.{fmt}")
@router.get(
    "/Videos/{item_id}/{media_source_id}/Subtitles/{stream_index}/{start_ticks}/stream.{fmt}"
)
async def subtitle_stream(
    request: Request,
    item_id: str,
    media_source_id: str,
    stream_index: int,
    fmt: str,
    start_ticks: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> Response:
    """外挂字幕输出（jellyfin-subtitle.md §4.4，对齐真 Jellyfin 两条路由）。

    - 带 ticks 的是 DeliveryUrl 引用的形态；ticks 接受并忽略——不转码
      就没有 seek 时间轴平移，DeliveryUrl 恒填 0；
    - 每个 route 段有同名 query 可覆盖（对齐 ParameterObsolete 兼容行为）；
      ``?format=`` 显式空串 → 按源格式原样输出（仍经编码归一）；
    - 鉴权 require_device（偏离③：真 Jellyfin 此接口匿名，我们要 token，
      DeliveryUrl 自带 ?ApiKey=）；库可见性随 _files_for_ref 强制——
      GUID 可枚举，白名单外成员在字幕路径同样 404；
    - 内容与格式转换全部来自 movieclaw_playback.subtitles（B 层），
      本层只做 GUID/编号反解与 HTTP 形态；错误一律 404 空 body +
      中文日志（文件不在/格式不支持/解析失败，部署者要能看懂）。
    """
    del start_ticks  # 接受并忽略（见 docstring）
    qp = request.query_params
    item_id = qp.get("itemId") or item_id
    media_source_id = qp.get("mediaSourceId") or media_source_id
    raw_index = qp.get("index")
    if raw_index:
        try:
            stream_index = int(raw_index)
        except ValueError:
            raise not_found() from None
    # "format" 键存在但为空串 = 显式要求源格式原样输出（传 None 给 B 层）
    out_format: str | None = fmt
    if "format" in qp:
        out_format = qp.get("format") or None
    if out_format is not None:
        out_format = _normalize_subtitle_format(out_format)

    ref = decode_guid(item_id)
    if ref is None or ref.kind not in (EntityKind.ITEM, EntityKind.EPISODE):
        raise not_found()
    files = await _files_for_ref(ref, identity.device.member_id)
    selected = _select_source(files, media_source_id, item_id)
    if not selected:
        raise not_found()
    f = selected[0]

    track = subtitle_track_for_index(f, stream_index)
    if track is None:
        logger.warning(
            "字幕请求的流序号不存在（可能条目已重扫、编号已变化）：%s #%d",
            f.file_path,
            stream_index,
        )
        raise not_found()
    sub_ref = resolve_external_subtitle(f, track)
    if sub_ref is None:
        # 指到内封轨：v1 不做服务端抽取（DirectPlay 播放器自行解封装）
        logger.warning("字幕请求指向内封轨或台账已失效，无法输出：%s（%s）", f.file_path, track)
        raise not_found()
    try:
        content, mime = await serve_subtitle_async(sub_ref, out_format)
    except SubtitleServeError as exc:
        logger.warning("外挂字幕输出失败：%s", exc)
        raise not_found() from None
    return Response(content=content, media_type=mime)
