"""远程转码 Worker 的控制面与 HTTPS 数据面。"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi import Path as PathParam
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect
from starlette.websockets import WebSocketDisconnect

from movieclaw_api.api.deps import require_admin, resolve_worker_principal
from movieclaw_api.exceptions import (
    InsufficientStorageException,
    NotFoundException,
    ServiceUnavailableException,
    UnauthorizedException,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.schemas.transcode_worker import (
    RemoteTranscodeConfigPayload,
    RemoteTranscodeConfigView,
)
from movieclaw_api.services.playback import remote_config as remote_transcode_config
from movieclaw_api.services.playback.disc_source import disc_source_for_file
from movieclaw_api.services.playback.ffmpeg_args import (
    REMOTE_IO_TIMEOUT_US,
    REMOTE_RECONNECT_OPTIONS,
)
from movieclaw_api.services.playback.remote_signing import verify_remote_grant
from movieclaw_api.services.playback.remote_worker import (
    REMOTE_WORKER_PROTOCOL_VERSION,
    effective_remote_transcode_config,
    get_remote_worker_registry,
    remote_worker_enabled,
)
from movieclaw_api.services.playback.session import get_session_manager
from movieclaw_db.engine import get_session
from movieclaw_db.models import LibraryFile
from movieclaw_events import new_ulid
from movieclaw_playback.streaming import (
    DisconnectAwareFileResponse,
    container_mime_type,
    is_strm,
)

logger = logging.getLogger("movieclaw_api.playback.transcode_worker")

router = APIRouter(prefix="/transcode-worker", tags=["transcode-worker"])

_ARTIFACT_NAME = re.compile(r"^(?:init\.mp4|(?:live|index)\.m3u8|seg\d{5}\.(?:m4s|ts))$")

#: 同一来源、同一原因的告警多久最多记一条。令牌被吊销的 Worker 会按退避（最长
#: 30 秒）无限重连，版本不匹配时每个分片都会被拒——每次都记就是每天几千行重复。
_WARN_INTERVAL_S = 600.0
_warned_at: dict[tuple[str, ...], float] = {}


def _warn_throttled(key: tuple[str, ...], message: str, *args: object) -> None:
    """同一 ``key`` 在 ``_WARN_INTERVAL_S`` 内只记第一条 WARNING。"""
    now = time.monotonic()
    last = _warned_at.get(key)
    if last is not None and now - last < _WARN_INTERVAL_S:
        return
    if len(_warned_at) > 1024:
        # key 只来自已知来源（客户端地址、会话、文件），正常到不了这个量；兜底防涨
        _warned_at.clear()
    _warned_at[key] = now
    logger.warning(message, *args)


def _client_host(websocket: WebSocket) -> str:
    return websocket.client.host if websocket.client else "未知地址"


async def _reject_before_hello(websocket: WebSocket, reason: str) -> None:
    """握手阶段拒绝 Worker：先 accept 再用 1008 关闭，理由才送得到 Worker。

    在 accept 之前 close，uvicorn 按 ASGI 规范只回一个空包体的 HTTP 403，关闭理由
    整句丢掉——Worker 只能看到「There was a bad response from the server」，凭证
    失效和开关没开分不出来，下面分开写的两个理由等于白写。Starlette 的 TestClient
    不模拟这一点（照样抛带理由的 WebSocketDisconnect），测试里看不出差别。
    先 accept 再关，理由随关闭帧送到 Worker，面板上照原文显示。
    """
    await websocket.accept()
    await websocket.close(code=1008, reason=reason)


def _artifact_write_failure(
    exc: OSError,
    *,
    session_id: str,
    name: str,
    received_bytes: int,
) -> InsufficientStorageException | ServiceUnavailableException:
    """把缓存写盘错误转换成 Worker 能区分的 API 错误。

    远程 Worker 的产物上传不是普通的文件不存在查询：如果临时文件写入或
    原子替换失败，返回 404 会让 Worker 误以为会话失效，并把真正的磁盘问题
    隐藏掉。``EDQUOT``/``ENOSPC`` 使用 507，其它写盘错误使用 503，二者都
    保留安全的产物名和 errno 供诊断页定位，不泄露本地路径或令牌。
    """
    errno_code = exc.errno
    details = [
        {
            "artifact": name,
            "errno": errno_code,
            "received_bytes": received_bytes,
        }
    ]
    if errno_code in {errno.EDQUOT, errno.ENOSPC}:
        logger.error(
            "远程转码产物写入失败：缓存空间或配额不足 session=%s name=%s "
            "errno=%s received_bytes=%s",
            session_id,
            name,
            errno_code,
            received_bytes,
        )
        return InsufficientStorageException(
            "远程转码缓存空间或磁盘配额不足，请在「设置 → 更新与维护 → 缓存管理」释放空间后重试。",
            details=details,
        )
    logger.error(
        "远程转码产物写入失败：缓存目录暂时不可写 session=%s name=%s "
        "errno=%s received_bytes=%s error=%s",
        session_id,
        name,
        errno_code,
        received_bytes,
        exc,
    )
    return ServiceUnavailableException(
        "远程转码缓存目录暂时不可写，请检查磁盘空间和权限后重试。",
        details=details,
    )


@router.get(
    "/config",
    response_model=ApiResponse[RemoteTranscodeConfigView],
    summary="读取远程转码配置",
    operation_id="transcode.config.show",
    dependencies=[Depends(require_admin)],
)
async def get_transcode_worker_config() -> ApiResponse[RemoteTranscodeConfigView]:
    """读取脱敏配置，绝不返回 Worker 令牌。"""
    return ok(await remote_transcode_config.build_remote_transcode_config_view())


@router.put(
    "/config",
    response_model=ApiResponse[RemoteTranscodeConfigView],
    summary="保存远程转码配置",
    operation_id="transcode.config.set",
    dependencies=[Depends(require_admin)],
)
async def save_transcode_worker_config(
    payload: RemoteTranscodeConfigPayload,
) -> ApiResponse[RemoteTranscodeConfigView]:
    """保存配置并立即刷新运行时快照。"""
    return ok(await remote_transcode_config.save_remote_transcode_config(payload))


def _invalid_remote_grant() -> UnauthorizedException:
    return UnauthorizedException("远程转码凭据无效或已过期")


async def _verify_grant(
    token: str | None,
    *,
    session_id: str,
    kind: str,
):
    """统一校验远程数据面 token；缺少 token 也必须返回 401，便于默认拒绝守护测试。"""
    if not token:
        raise _invalid_remote_grant()
    grant = await verify_remote_grant(token, session_id=session_id, kind=kind)
    if grant is None:
        raise _invalid_remote_grant()
    return grant


def _observed_base_url(websocket: WebSocket) -> str:
    """推断 Worker 刚刚是从哪个根地址连进来的。

    远程转码要下发两个 URL 给 Worker：去哪儿读源视频、把 HLS 产物传回哪儿。
    这两个地址过去只能由管理员在网页上手填，可它其实是已知的——Worker 的
    控制连接本身就是从某个地址打过来的，那个地址**必然**是这台 Worker 够得
    着的，比任何猜测都可靠。

    取值顺序：
    * scheme 优先信 ``X-Forwarded-Proto``。TLS 在反向代理上终止时，Worker 用
      的是 wss，转到应用的却是 ws，只看 scope 会拼出一个连不上的 http 地址。
    * host 用 ``Host`` 头，也就是 Worker 拨号时写的那个主机名/端口。
    * 末尾接上 ``root_path``，兼容把应用挂在子路径下的反向代理。

    只有代理把 Host 改写成了上游地址（如 ``127.0.0.1:8000``）这种少见配置，
    推断才会失真——那正是网页上「专用地址」覆盖项存在的意义。
    """
    forwarded = (websocket.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if forwarded in {"http", "https"}:
        scheme = forwarded
    elif forwarded in {"ws", "wss"}:
        scheme = "http" if forwarded == "ws" else "https"
    else:
        scheme = "https" if websocket.url.scheme == "wss" else "http"
    host = (websocket.headers.get("host") or "").strip()
    if not host:
        return ""
    root_path = (websocket.scope.get("root_path") or "").rstrip("/")
    return f"{scheme}://{host}{root_path}"


@router.websocket("/ws")
async def transcode_worker_websocket(websocket: WebSocket) -> None:
    """接收 Worker hello，并持续处理心跳与任务状态。"""
    # 凭证走标准 Authorization: Bearer，与 CLI 同一个验签入口
    # （docs/design/device-auth.md §5.4）。放 Header 而不是查询参数，是为了
    # 不把长期令牌写进反向代理访问日志与监控 URL；数据面用的短时签名 token
    # 才走查询参数，那是另一套且随会话失效。
    # 两个拒绝理由必须分开报，且顺序不能反。
    #
    # 合成一句「远程转码未启用，或凭证无效、已被吊销」会把两件性质完全不同的
    # 事混在一起：前者是管理员一个开关没打开、两秒能修；后者是授权出了问题、
    # 要重新配对。用户刚配对成功就看到「凭证无效」，第一反应是再配一遍，
    # 而真正该做的是回网页打开开关。
    #
    # 先判凭证再判开关：没有有效令牌的人不该从错误文案里读出这台服务器的
    # 功能开关状态。
    # 每条拒绝都在 NAS 留一行（同一来源同一原因限频）：拒绝理由只随关闭帧发给
    # Worker，用户说「Worker 连不上」时，NAS 日志此前一个字都没有。
    client = _client_host(websocket)
    principal = await resolve_worker_principal(websocket.headers.get("authorization"))
    if principal is None:
        reason = "凭证无效或已被吊销，请在网页「设置 → 设备」重新配对"
        _warn_throttled(
            ("ws-auth", client), "拒绝远程转码 Worker 连接（来自 %s）：%s", client, reason
        )
        await _reject_before_hello(websocket, reason)
        return
    if not remote_worker_enabled():
        reason = "服务端尚未启用远程转码，请在网页「应用 → 远程转码」打开开关并确认地址"
        _warn_throttled(
            ("ws-disabled", client), "拒绝远程转码 Worker 连接（来自 %s）：%s", client, reason
        )
        await _reject_before_hello(websocket, reason)
        return

    await websocket.accept()
    connection = None
    registry = get_remote_worker_registry()
    try:
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        except TimeoutError:
            _warn_throttled(
                ("ws-hello", client),
                "远程转码 Worker 握手超时（来自 %s）：10 秒内没发 hello",
                client,
            )
            await websocket.close(code=1008, reason="Worker hello 超时")
            return
        reason = None
        if not isinstance(hello, dict) or hello.get("type") != "worker.hello":
            reason = "Worker hello 格式错误"
        elif hello.get("protocol_version") != REMOTE_WORKER_PROTOCOL_VERSION:
            # 版本不一致单独说清楚：笼统的「格式错误」会让人以为是 Worker 坏了
            reason = (
                f"Worker 协议版本（{hello.get('protocol_version')}）与服务端"
                f"（{REMOTE_WORKER_PROTOCOL_VERSION}）不一致，请把 Worker 与服务端更新到同一版本"
            )
        if reason is not None:
            _warn_throttled(
                ("ws-hello", client), "拒绝远程转码 Worker 连接（来自 %s）：%s", client, reason
            )
            await websocket.close(code=1008, reason=reason)
            return
        try:
            connection = await registry.register(
                websocket, hello, observed_base_url=_observed_base_url(websocket)
            )
        except ValueError as exc:
            _warn_throttled(
                ("ws-register", client), "拒绝远程转码 Worker 连接（来自 %s）：%s", client, exc
            )
            await websocket.close(code=1008, reason=str(exc))
            return
        await connection.send(
            {
                "type": "worker.accepted",
                "protocol_version": REMOTE_WORKER_PROTOCOL_VERSION,
            }
        )
        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict):
                artifact_failure = await registry.handle_message(connection, message)
                if artifact_failure is not None:
                    # Worker 放弃了某个产物的上传：转给会话层记账补片
                    get_session_manager().record_remote_artifact_failure(artifact_failure)
    except WebSocketDisconnect as exc:
        # 断开码是区分「Worker 崩了」和「用户自己退出」的唯一线索，必须打出来：
        # 1000/1001 是对端发了关闭帧的正常退出；1006 代表连关闭帧都没来得及发，
        # 几乎总意味着 Mac 上的 Worker 进程异常终止（崩溃、被杀、拔网线）。
        # 看到 1006 就该去那台 Mac 上翻 ~/Library/Logs/MovieClawTranscoder.log
        # 的 [CRASH] 面包屑，以及 ~/Library/Logs/DiagnosticReports 里的 .ips。
        logger.warning(
            "远程 Worker 控制连接断开：worker=%s code=%s%s",
            connection.worker_id if connection is not None else "未握手",
            exc.code,
            "（无关闭帧，Worker 进程多半是异常退出的）" if exc.code == 1006 else "",
        )
    except Exception:  # noqa: BLE001
        logger.exception("远程 Worker 控制连接异常")
    finally:
        if connection is not None:
            await registry.unregister(connection)


@router.get(
    "/status",
    response_model=ApiResponse[dict],
    summary="远程转码 Worker 状态",
    operation_id="transcode.status",
    dependencies=[Depends(require_admin)],
)
async def transcode_worker_status() -> ApiResponse[dict]:
    """管理员诊断接口，不返回任何控制面令牌。"""
    config = effective_remote_transcode_config()
    return ok(
        {
            "enabled": config.enabled,
            "base_url_configured": bool(config.base_url),
            "ready": config.ready,
            "workers": get_remote_worker_registry().snapshot(),
        }
    )


async def _remote_source_file(
    session_id: str, token: str | None, session: AsyncSession
) -> LibraryFile:
    """三个取源接口共用的校验：令牌有效、会话是远程会话、令牌签给的就是这个文件。"""
    grant = await _verify_grant(token, session_id=session_id, kind="source")
    playback_session = get_session_manager().get(session_id)
    if (
        playback_session is None
        or not playback_session.remote
        or playback_session.file_id != grant.file_id
    ):
        raise NotFoundException("远程转码会话不存在")
    file = await session.get(LibraryFile, grant.file_id)
    if file is None or is_strm(file.file_path):
        raise NotFoundException("远程转码源文件不存在")
    return file


def _missing_source(session_id: str, file: LibraryFile, path: Path) -> NotFoundException:
    """源不在磁盘上：Worker 那边只会看到 ffmpeg 报 404、任务失败，真正的原因在
    NAS 这一侧（文件被移走/删除，或媒体目录的挂载静默失效），必须在这里说出来。"""
    _warn_throttled(
        ("source-missing", str(file.id)),
        "远程转码源文件不在磁盘上：session=%s file_id=%s path=%s"
        "（文件已被移动或删除，或媒体目录挂载失效）",
        session_id,
        file.id,
        path,
    )
    return NotFoundException("远程转码源文件已不在磁盘上")


@router.get(
    "/sessions/{session_id}/source",
    summary="远程转码源文件",
    operation_id="transcode.source",
    openapi_extra={"x-cli-hidden": True},
)
async def transcode_source(
    session_id: Annotated[str, PathParam()],
    token: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
):
    """给 Worker 提供支持 Range 的源文件读取；不允许读取 strm 占位文件。"""
    file = await _remote_source_file(session_id, token, session)
    if file.is_disc():
        # 原盘是目录，没有单一源文件；新版 Worker 读 source.ffconcat，走不到这里
        raise NotFoundException("原盘没有单一源文件，请改读 source.ffconcat 清单")
    path = Path(file.file_path)
    if not path.is_file():
        raise _missing_source(session_id, file, path)
    return DisconnectAwareFileResponse(
        path,
        media_type=container_mime_type(file.container),
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/sessions/{session_id}/source.ffconcat",
    summary="远程转码原盘清单",
    operation_id="transcode.source.ffconcat",
    openapi_extra={"x-cli-hidden": True},
)
async def transcode_disc_source(
    session_id: Annotated[str, PathParam()],
    token: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """原盘的 ffconcat 清单（docs/design/remote-transcode.md §5.2）。

    和 NAS 本机读盘用的是同一份剪辑序列与 IN/OUT（``DiscSource.concat_list``），
    只是每段换成本接口旁边的 ``clips/{i}`` 相对地址：ffmpeg 按清单自己的地址
    解析，反向代理挂在子路径下也对得上；令牌沿用这一个（它签的是整个会话的源）。
    每段逐个带上断线续读参数——命令行上的 ``-reconnect`` 只管清单这一个输入。
    """
    file = await _remote_source_file(session_id, token, session)
    disc = disc_source_for_file(file) if file.is_disc() else None
    if disc is None:
        raise NotFoundException("这个远程转码会话的源不是可读的原盘")
    token_query = quote(token or "", safe="")
    body = disc.concat_list(
        entry=lambda index, _clip: f"clips/{index}?token={token_query}",
        options=(("rw_timeout", str(REMOTE_IO_TIMEOUT_US)), *REMOTE_RECONNECT_OPTIONS),
    )
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/sessions/{session_id}/clips/{index}",
    summary="远程转码原盘剪辑",
    operation_id="transcode.source.clip",
    openapi_extra={"x-cli-hidden": True},
)
async def transcode_disc_clip(
    session_id: Annotated[str, PathParam()],
    index: Annotated[int, PathParam(ge=0)],
    token: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
):
    """原盘清单里第 ``index`` 段剪辑（m2ts）的 Range 读取。"""
    file = await _remote_source_file(session_id, token, session)
    disc = disc_source_for_file(file) if file.is_disc() else None
    if disc is None or index >= len(disc.clips):
        raise NotFoundException("原盘剪辑不存在")
    path = disc.clips[index].path
    if not path.is_file():
        raise _missing_source(session_id, file, path)
    return DisconnectAwareFileResponse(
        path, media_type="video/MP2T", headers={"Cache-Control": "no-store"}
    )


@router.put(
    "/sessions/{session_id}/artifacts/{name}",
    summary="远程转码产物上传",
    operation_id="transcode.artifact.put",
    openapi_extra={"x-cli-hidden": True},
)
async def put_transcode_artifact(
    session_id: Annotated[str, PathParam()],
    name: Annotated[str, PathParam()],
    request: Request,
    token: Annotated[str | None, Query()] = None,
):
    """流式接收一个 HLS 产物，写临时文件后原子替换到会话目录。

    ffmpeg 的 HTTP HLS 输出可能重复上传同一个 init/segment（断线重试或 seek
    重启），所以端点必须幂等。临时文件名含随机会话 ID，避免并发重传互相覆盖。
    """
    if not _ARTIFACT_NAME.fullmatch(name):
        # 持有效凭据的 Worker 传来不认识的产物名，是两端版本不一致（issue #444
        # 的反方向）。只给有效凭据记日志：名字取自请求路径，匿名请求可以随便编
        if token and await verify_remote_grant(token, session_id=session_id, kind="artifact"):
            _warn_throttled(
                ("artifact-name", session_id),
                "远程转码产物名不在服务端白名单内，已拒收：session=%s name=%s"
                "（Worker 可能比服务端新，请把两边更新到同一版本）",
                session_id,
                name[:80],
            )
        raise NotFoundException("远程转码产物名称无效")
    grant = await _verify_grant(token, session_id=session_id, kind="artifact")
    playback_session = get_session_manager().get(session_id)
    if (
        playback_session is None
        or not playback_session.remote
        or playback_session.file_id != grant.file_id
        or grant.attempt_id != playback_session.remote_job_id
    ):
        raise NotFoundException("远程转码会话不存在")
    job_state = (
        get_remote_worker_registry().job_state(playback_session.remote_job_id or "")
        if playback_session.remote_job_id
        else None
    )
    if job_state and job_state.get("type") in {"job.failed", "job.finished"}:
        # Worker 断线/失败后，旧进程即使还持有短时 token，也不能继续覆盖 NAS
        # 缓存；seek 轮次则由 attempt_id 的相等性额外隔离。
        raise NotFoundException("远程转码任务已结束")
    directory = playback_session.directory
    if not directory.is_dir():
        _warn_throttled(
            ("artifact-dir", session_id),
            "远程转码产物无处落盘：session=%s 的缓存目录不存在（被删除，或数据目录挂载失效）",
            session_id,
        )
        raise NotFoundException("远程转码会话目录不存在")
    limit = effective_remote_transcode_config().max_artifact_bytes
    content_length = request.headers.get("content-length")
    content_length_value: int | None = None
    transfer_encoding = request.headers.get("transfer-encoding")
    if content_length:
        try:
            content_length_value = int(content_length)
            if content_length_value > limit:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": "远程转码分片超过上传大小限制",
                    },
                )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={"code": "BAD_REQUEST", "message": "Content-Length 无效"},
            ) from None

    temporary = directory / f".{name}.{new_ulid()}.upload"
    target = directory / name
    written = 0
    try:
        async with await anyio.open_file(temporary, "wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "code": "PAYLOAD_TOO_LARGE",
                            "message": "远程转码分片超过上传大小限制",
                        },
                    )
                await output.write(chunk)
            await output.flush()
        os.replace(temporary, target)
        playback_session.record_remote_upload(
            name,
            status=201,
            received_bytes=written,
            content_length=content_length_value,
            transfer_encoding=transfer_encoding,
            attempt_id=grant.attempt_id,
        )
        if name in {"init.mp4", "live.m3u8"}:
            logger.info(
                "远程转码关键产物已落盘：session=%s name=%s bytes=%s",
                session_id,
                name,
                written,
            )
    except ClientDisconnect:
        # ffmpeg 的 HTTP HLS 输出可能在请求体已经完整发出后先关闭连接，不再等待
        # NAS 的响应。若 Content-Length 与已接收字节数一致，内容已经完整且临时
        # 文件已关闭，可以安全原子替换；否则只能丢弃半个产物，不能让播放器读到
        # 不完整的 m3u8/fMP4。499 沿用 nginx 的语义，明确表示客户端中断而非服务端
        # 500；客户端已经断开时响应通常无法送达，但日志和诊断状态会保持准确。
        if content_length_value is not None and written == content_length_value:
            try:
                os.replace(temporary, target)
            except OSError as exc:
                with suppress(OSError):
                    temporary.unlink()
                playback_session.record_remote_upload(
                    name,
                    status=500,
                    received_bytes=written,
                    content_length=content_length_value,
                    transfer_encoding=transfer_encoding,
                    attempt_id=grant.attempt_id,
                )
                raise _artifact_write_failure(
                    exc,
                    session_id=session_id,
                    name=name,
                    received_bytes=written,
                ) from exc
            playback_session.record_remote_upload(
                name,
                status=201,
                received_bytes=written,
                content_length=content_length_value,
                transfer_encoding=transfer_encoding,
                attempt_id=grant.attempt_id,
            )
            logger.warning(
                "远程转码产物上传客户端提前断开，但请求体已完整接收："
                "session=%s name=%s bytes=%s",
                session_id,
                name,
                written,
            )
            return Response(status_code=201, headers={"Cache-Control": "no-store"})
        with suppress(OSError):
            temporary.unlink()
        playback_session.record_remote_upload(
            name,
            status=499,
            received_bytes=written,
            content_length=content_length_value,
            transfer_encoding=transfer_encoding,
            attempt_id=grant.attempt_id,
        )
        logger.warning(
            "远程转码产物上传中断：session=%s name=%s received_bytes=%s "
            "content_length=%s transfer_encoding=%s",
            session_id,
            name,
            written,
            content_length_value,
            request.headers.get("transfer-encoding", "-"),
        )
        return Response(status_code=499, headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        temporary.unlink(missing_ok=True)
        logger.warning(
            "远程转码产物被拒收：session=%s name=%s HTTP %s 已收 %s 字节（上限 %s 字节）",
            session_id,
            name,
            exc.status_code,
            written,
            limit,
        )
        playback_session.record_remote_upload(
            name,
            status=exc.status_code,
            received_bytes=written,
            content_length=content_length_value,
            transfer_encoding=transfer_encoding,
            attempt_id=grant.attempt_id,
        )
        raise
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink()
        playback_session.record_remote_upload(
            name,
            status=500,
            received_bytes=written,
            content_length=content_length_value,
            transfer_encoding=transfer_encoding,
            attempt_id=grant.attempt_id,
        )
        raise _artifact_write_failure(
            exc,
            session_id=session_id,
            name=name,
            received_bytes=written,
        ) from exc
    except Exception:
        with suppress(OSError):
            temporary.unlink()
        raise

    return Response(
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )
