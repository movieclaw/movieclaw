"""远程转码 Worker 的控制面与 HTTPS 数据面。"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Annotated

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

_ARTIFACT_NAME = re.compile(r"^(?:init\.mp4|(?:live|index)\.m3u8|seg\d{5}\.m4s)$")


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
            "远程转码缓存空间或磁盘配额不足，请清理 data/cache/playback 后重试。",
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
    principal = await resolve_worker_principal(websocket.headers.get("authorization"))
    if principal is None:
        await websocket.close(
            code=1008,
            reason="凭证无效或已被吊销，请在网页「设置 → 设备」重新配对",
        )
        return
    if not remote_worker_enabled():
        await websocket.close(
            code=1008,
            reason="服务端尚未启用远程转码，请在网页「应用 → 远程转码」打开开关并确认地址",
        )
        return

    await websocket.accept()
    connection = None
    registry = get_remote_worker_registry()
    try:
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        except TimeoutError:
            await websocket.close(code=1008, reason="Worker hello 超时")
            return
        if (
            not isinstance(hello, dict)
            or hello.get("type") != "worker.hello"
            or hello.get("protocol_version") != REMOTE_WORKER_PROTOCOL_VERSION
        ):
            await websocket.close(code=1008, reason="Worker hello 格式错误")
            return
        try:
            connection = await registry.register(
                websocket, hello, observed_base_url=_observed_base_url(websocket)
            )
        except ValueError as exc:
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
                await registry.handle_message(connection, message)
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
    path = Path(file.file_path)
    if not path.is_file():
        raise NotFoundException("远程转码源文件已不在磁盘上")
    return DisconnectAwareFileResponse(
        path,
        media_type=container_mime_type(file.container),
        headers={"Cache-Control": "no-store"},
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
        playback_session.record_remote_upload(
            name,
            status=exc.status_code,
            received_bytes=written,
            content_length=content_length_value,
            transfer_encoding=transfer_encoding,
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
