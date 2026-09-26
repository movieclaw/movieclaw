"""外置 Worker 控制面、签名范围与远程 HLS 命令测试。"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from movieclaw_api.api.routes.transcode_worker import _ARTIFACT_NAME
from movieclaw_api.services.playback import remote_signing
from movieclaw_api.services.playback import remote_worker as remote_worker_module
from movieclaw_api.services.playback.ffmpeg_args import build_hls_command
from movieclaw_api.services.playback.remote_config import RemoteTranscodeRuntimeConfig
from movieclaw_api.services.playback.remote_worker import (
    RemoteWorkerRegistry,
    RemoteWorkerUnavailable,
)
from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.closed = False

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)

    async def close(self, **_: object) -> None:
        self.closed = True


def _transcode_plan() -> PlaybackPlan:
    return PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=7,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264", height=1080),
        audio=AudioPlan(action="copy", track_ref=None),
        reason="测试",
    )


@pytest.mark.asyncio
async def test_remote_grant_is_scoped_by_kind_session_and_attempt(monkeypatch):
    monkeypatch.setattr(remote_signing, "get_signing_secret", lambda: _secret())

    token = await remote_signing.issue_remote_grant(
        session_id="session-a",
        file_id=7,
        kind="artifact",
        attempt_id="attempt-a",
        ttl_seconds=60,
    )

    assert await remote_signing.verify_remote_grant(
        token,
        session_id="session-a",
        kind="artifact",
        file_id=7,
        attempt_id="attempt-a",
    )
    assert (
        await remote_signing.verify_remote_grant(
            token,
            session_id="session-a",
            kind="source",
        )
        is None
    )
    assert (
        await remote_signing.verify_remote_grant(
            token,
            session_id="session-a",
            kind="artifact",
            attempt_id="attempt-b",
        )
        is None
    )


async def _secret() -> str:
    return "test-signing-secret"


@pytest.mark.asyncio
async def test_registry_rejects_capability_without_matching_encoder():
    capabilities = RemoteWorkerRegistry._parse_capabilities(
        {"backends": ["videotoolbox"], "encoders": ["libx264"]}
    )
    assert capabilities.backends == ()


async def _dispatch(registry, job_id, payload, *, backend):
    """测试辅助：占位 + 下发，等价于拆分前的一步式 dispatch。"""
    connection = registry.reserve(
        job_id, backend=backend, attempt_id=payload.get("attempt_id")
    )
    return await registry.start_job(connection, job_id, payload)


@pytest.mark.asyncio
async def test_registry_routes_job_and_ignores_other_worker_status():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(remote_worker_module, "remote_worker_enabled", lambda: True)
    try:
        registry = RemoteWorkerRegistry()
        first_socket = FakeWebSocket()
        second_socket = FakeWebSocket()
        first = await registry.register(
            first_socket,
            {
                "worker_id": "mac-mini-a",
                "capabilities": {
                    "backends": ["videotoolbox"],
                    "encoders": ["h264_videotoolbox"],
                },
            },
        )
        second = await registry.register(
            second_socket,
            {
                "worker_id": "mac-mini-b",
                "capabilities": {
                    "backends": ["videotoolbox"],
                    "encoders": ["h264_videotoolbox"],
                },
            },
        )
        registry.create_job_waiter("job-a")
        worker_id = await _dispatch(registry, 
            "job-a", {"ffmpeg_args": ["-version"]}, backend="videotoolbox"
        )
        assert worker_id == first.worker_id
        assert first_socket.messages[-1]["type"] == "job.start"

        await registry.handle_message(
            second,
            {"type": "job.finished", "job_id": "job-a"},
        )
        assert registry.job_state("job-a")["type"] == "job.pending"

        await registry.handle_message(
            first,
            {"type": "job.accepted", "job_id": "job-a"},
        )
        event = await registry.wait_job_event("job-a", timeout=0.1)
        assert event["type"] == "job.accepted"
        await registry.handle_message(
            first,
            {"type": "job.finished", "job_id": "job-a"},
        )
        assert registry.job_state("job-a")["type"] == "job.finished"
        assert first.jobs == set()
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_registry_marks_jobs_failed_when_worker_disconnects():
    registry = RemoteWorkerRegistry()
    websocket = FakeWebSocket()
    connection = await registry.register(
        websocket,
        {
            "worker_id": "mac-mini-a",
            "capabilities": {
                "backends": ["videotoolbox"],
                "encoders": ["h264_videotoolbox"],
            },
        },
    )
    registry.create_job_waiter("job-a")
    await _dispatch(registry, "job-a", {}, backend="videotoolbox")
    await registry.unregister(connection)
    event = await registry.wait_job_event("job-a", timeout=0.1)
    assert event["type"] == "job.failed"
    assert "断开" in event["error"]


@pytest.mark.asyncio
async def test_registry_sends_ts_jobs_only_to_workers_declaring_mpegts():
    """issue #444：没声明分片类型的旧版 Worker 只当它会 fMP4——它的上传代理会把 TS
    分片悄悄拒收。TS 任务只派给声明了 mpegts 的 Worker，fMP4 任务照旧谁都能接。"""
    registry = RemoteWorkerRegistry()
    videotoolbox = {"backends": ["videotoolbox"], "encoders": ["h264_videotoolbox"]}
    old = await registry.register(
        FakeWebSocket(), {"worker_id": "mac-old", "capabilities": videotoolbox}
    )
    assert old.capabilities.segment_types == ("fmp4",)

    with pytest.raises(RemoteWorkerUnavailable, match="版本过旧"):
        registry.reserve("job-ts", backend="videotoolbox", segment_type="mpegts")
    assert registry.reserve("job-mp4", backend="videotoolbox").worker_id == "mac-old"

    new = await registry.register(
        FakeWebSocket(),
        {
            "worker_id": "mac-new",
            "capabilities": {**videotoolbox, "segment_types": ["fmp4", "mpegts", "bogus"]},
        },
    )
    assert new.capabilities.segment_types == ("fmp4", "mpegts")
    reserved = registry.reserve("job-ts", backend="videotoolbox", segment_type="mpegts")
    assert reserved.worker_id == "mac-new"


@pytest.mark.asyncio
async def test_registry_hands_back_artifact_failures_only_from_the_job_owner(caplog):
    """Worker 报「某产物重试用尽没传上去」：校验归属与轮次后交回调用方转给会话层，
    不写进任务状态表（否则会盖掉 accepted/progress）。别的 Worker 冒名的、旧轮次的
    一律忽略。任务失败要在 NAS 日志里留下原因、退出码与 stderr 末尾。"""
    registry = RemoteWorkerRegistry()
    videotoolbox = {"backends": ["videotoolbox"], "encoders": ["h264_videotoolbox"]}
    owner = await registry.register(
        FakeWebSocket(), {"worker_id": "mac-a", "capabilities": videotoolbox}
    )
    other = await registry.register(
        FakeWebSocket(), {"worker_id": "mac-b", "capabilities": videotoolbox}
    )
    registry.create_job_waiter("job-1")
    await _dispatch(registry, "job-1", {"attempt_id": "job-1"}, backend="videotoolbox")
    await registry.handle_message(owner, {"type": "job.accepted", "job_id": "job-1"})
    failure = {
        "type": "job.artifact_failed",
        "job_id": "job-1",
        "attempt_id": "job-1",
        "name": "seg00007.ts",
        "status": 502,
        "error": "The network connection was lost.",
    }

    assert await registry.handle_message(other, failure) is None
    assert await registry.handle_message(owner, {**failure, "attempt_id": "old"}) is None
    assert await registry.handle_message(owner, failure) == failure
    assert registry.job_state("job-1")["type"] == "job.accepted"

    with caplog.at_level("WARNING"):
        await registry.handle_message(
            owner,
            {
                "type": "job.failed",
                "job_id": "job-1",
                "attempt_id": "job-1",
                "exit_code": 1,
                "error": "ffmpeg 退出码：1",
                "stderr_tail": "line 1\nline 2\n[h264_videotoolbox] Error: cannot create session\n",
            },
        )
    logged = [r.getMessage() for r in caplog.records if "远程转码任务失败" in r.getMessage()]
    assert logged and "cannot create session" in logged[0] and "mac-a" in logged[0]


@pytest.mark.asyncio
async def test_registry_pause_and_resume_keep_job_claimed(monkeypatch):
    monkeypatch.setattr(remote_worker_module, "remote_worker_enabled", lambda: True)
    registry = RemoteWorkerRegistry()
    websocket = FakeWebSocket()
    connection = await registry.register(
        websocket,
        {
            "worker_id": "mac-mini-a",
            "capabilities": {
                "backends": ["videotoolbox"],
                "encoders": ["h264_videotoolbox"],
            },
        },
    )
    registry.create_job_waiter("job-a")
    await _dispatch(registry, "job-a", {}, backend="videotoolbox")

    assert await registry.pause("job-a") is True
    assert websocket.messages[-1] == {"type": "job.pause", "job_id": "job-a"}
    assert connection.jobs == {"job-a"}
    assert await registry.resume("job-a") is True
    assert websocket.messages[-1] == {"type": "job.resume", "job_id": "job-a"}
    assert connection.jobs == {"job-a"}

    await registry.cancel("job-a")


@pytest.mark.asyncio
async def test_registry_force_cancel_marks_seek_stop_message(monkeypatch):
    monkeypatch.setattr(remote_worker_module, "remote_worker_enabled", lambda: True)
    registry = RemoteWorkerRegistry()
    websocket = FakeWebSocket()
    await registry.register(
        websocket,
        {
            "worker_id": "mac-mini-a",
            "capabilities": {
                "backends": ["videotoolbox"],
                "encoders": ["h264_videotoolbox"],
            },
        },
    )
    registry.create_job_waiter("job-a")
    await _dispatch(registry, "job-a", {}, backend="videotoolbox")

    await registry.cancel("job-a", force=True)

    assert websocket.messages[-1] == {
        "type": "job.stop",
        "job_id": "job-a",
        "force": True,
    }


@pytest.mark.asyncio
async def test_registry_accepts_current_attempt_progress_and_ignores_stale_attempt(monkeypatch):
    monkeypatch.setattr(remote_worker_module, "remote_worker_enabled", lambda: True)
    registry = RemoteWorkerRegistry()
    websocket = FakeWebSocket()
    connection = await registry.register(
        websocket,
        {
            "worker_id": "mac-mini-a",
            "worker_version": "0.1.0",
            "capabilities": {
                "arch": "arm64",
                "backends": ["videotoolbox"],
                "encoders": ["h264_videotoolbox"],
            },
        },
    )
    registry.create_job_waiter("job-a")
    await _dispatch(registry, 
        "job-a",
        {"attempt_id": "attempt-a", "ffmpeg_args": ["-version"]},
        backend="videotoolbox",
    )

    await registry.handle_message(
        connection,
        {
            "type": "job.progress",
            "job_id": "job-a",
            "attempt_id": "stale-attempt",
            "out_time_ms": 1000,
        },
    )
    assert registry.job_state("job-a")["type"] == "job.pending"

    await registry.handle_message(
        connection,
        {
            "type": "job.progress",
            "job_id": "job-a",
            "attempt_id": "attempt-a",
            "out_time_ms": 2000,
            "speed": "1.5x",
        },
    )
    assert registry.job_state("job-a") == {
        "type": "job.progress",
        "job_id": "job-a",
        "attempt_id": "attempt-a",
        "out_time_ms": 2000,
        "speed": "1.5x",
    }
    assert registry.snapshot()[0]["jobs"] == [
        {
            "job_id": "job-a",
            "type": "job.progress",
            "out_time_ms": 2000,
            "speed": "1.5x",
            "phase": None,
        }
    ]

    await registry.handle_message(connection, {"type": "worker.draining"})
    assert registry.snapshot()[0]["draining"] is True
    await registry.handle_message(connection, {"type": "worker.ready"})
    assert registry.snapshot()[0]["draining"] is False
    await registry.cancel("job-a")


@pytest.mark.asyncio
async def test_registry_rejects_non_string_worker_id():
    registry = RemoteWorkerRegistry()
    with pytest.raises(ValueError, match="必须是字符串"):
        await registry.register(FakeWebSocket(), {"worker_id": 123})


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://nas.example.com", True),
        ("https://nas.example.com/movieclaw", True),
        ("ftp://nas.example.com", False),
        ("https://user:pass@nas.example.com", False),
        ("https://nas.example.com?debug=true", False),
    ],
)
def test_remote_worker_requires_safe_http_base_url(base_url, expected, monkeypatch):
    config = RemoteTranscodeRuntimeConfig(
        enabled=True,
        base_url=base_url,
        base_url_source="remote_transcode_setting",
        max_artifact_bytes=512 * 1024 * 1024,
    )
    monkeypatch.setattr(
        remote_worker_module,
        "effective_remote_transcode_config",
        lambda: config,
    )
    assert remote_worker_module.remote_worker_enabled() is expected


def test_remote_hls_command_uses_http_artifact_urls():
    command = build_hls_command(
        _transcode_plan(),
        source_path="http://10.1.1.5:3000/api/source?token=source",
        session_dir=Path("/data/transcodes/session-a"),
        start_number=3,
        hw_backend="videotoolbox",
        output_base_url="http://10.1.1.5:3000/api/artifacts",
        output_url_suffix="?token=artifact",
    )

    assert command.argv[-1] == "http://10.1.1.5:3000/api/artifacts/live.m3u8?token=artifact"
    assert "-method" in command.argv
    assert command.argv[command.argv.index("-method") + 1] == "PUT"
    assert "-chunked_post" not in command.argv
    assert command.argv.count("-rw_timeout") == 2
    assert (
        command.argv[command.argv.index("-hls_fmp4_init_filename") + 1]
        == "init.mp4?token=artifact"
    )
    assert (
        command.argv[command.argv.index("-hls_segment_filename") + 1]
        == "http://10.1.1.5:3000/api/artifacts/seg%05d.m4s?token=artifact"
    )


def test_remote_source_resumes_after_connection_drop():
    """ffmpeg 的 HTTP 输入默认不重连：取源连接中途断开（领先量节流把 job 挂起超过
    nginx 的 600 秒 send_timeout、Wi-Fi 抖动）时它当成读到了片尾，退出码 0 收工，
    后面的分片永远不来。远程命令必须带上按断点续读的输入选项（放在 -i 之前才
    作用于输入）；本地读文件的命令用不着。"""
    remote = build_hls_command(
        _transcode_plan(),
        source_path="http://10.1.1.5:3000/api/source?token=source",
        session_dir=Path("/data/transcodes/session-a"),
        start_number=0,
        hw_backend="videotoolbox",
        output_base_url="http://10.1.1.5:3000/api/artifacts",
        output_url_suffix="?token=artifact",
    ).argv
    input_at = remote.index("-i")
    for flag in ("-reconnect", "-reconnect_on_network_error", "-reconnect_delay_max"):
        assert flag in remote[:input_at], flag
    assert remote[remote.index("-reconnect") + 1] == "1"

    local = build_hls_command(
        _transcode_plan(),
        source_path="/media/movie.mkv",
        session_dir=Path("/data/transcodes/session-a"),
        start_number=0,
    ).argv
    assert "-reconnect" not in local


def test_remote_hls_command_reports_progress_on_stdout():
    command = build_hls_command(
        _transcode_plan(),
        source_path="https://nas.example/api/source?token=source",
        session_dir=Path("/data/transcodes/session-a"),
        output_base_url="https://nas.example/api/artifacts",
        output_url_suffix="?token=artifact",
    )

    assert command.argv[-1].endswith("index.m3u8?token=artifact")
    assert command.argv[command.argv.index("-progress") + 1] == "pipe:1"


_WORKER_PROXY_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "macos/MovieClawTranscoder/Sources/MovieClawTranscoder/ArtifactUploadProxy.swift"
)


def _worker_artifact_pattern() -> re.Pattern[str]:
    """从 Mac Worker 源码里读出它上传代理的产物文件名白名单。"""
    source = _WORKER_PROXY_SOURCE.read_text(encoding="utf-8")
    match = re.search(
        r'artifactNamePattern\s*=\s*try!\s*NSRegularExpression\(\s*pattern:\s*#"(.+?)"#', source
    )
    assert match, f"{_WORKER_PROXY_SOURCE.name} 里找不到 artifactNamePattern，本守卫要跟着改"
    return re.compile(match.group(1))


@pytest.mark.parametrize("container", ["hls-fmp4", "hls-ts"])
@pytest.mark.parametrize("start_number", [None, 0])
def test_worker_upload_whitelist_accepts_every_artifact_the_nas_asks_for(
    container: str, start_number: int | None
):
    """issue #444 的守卫：NAS 让远程 ffmpeg 上传的每一种产物，Mac Worker 的上传代理和
    NAS 的产物端点都必须放行。

    当初 NAS 为 Infuse 加了 TS 分片，只改了自己这一侧的白名单，Worker 侧仍只认
    ``.m4s``——TS 分片全在 Worker 本机被 404 拒收，而 ffmpeg 不看上传响应码，退出码 0、
    stderr 为空，两边日志都看不出原因。产物名取自真实装配出的命令，以后新增产物
    类型也逃不过这条检查。
    """
    command = build_hls_command(
        replace(_transcode_plan(), container=container),
        source_path="http://10.1.1.5:3000/api/source?token=source",
        session_dir=Path("/data/transcodes/session-a"),
        start_number=start_number,
        hw_backend="videotoolbox",
        output_base_url="http://10.1.1.5:3000/api/artifacts",
        output_url_suffix="?token=artifact",
    )
    paths = [urlsplit(arg).path for arg in command.argv if "/artifacts/" in arg]
    if "-hls_fmp4_init_filename" in command.argv:
        paths.append(urlsplit(command.argv[command.argv.index("-hls_fmp4_init_filename") + 1]).path)
    templates = {path.rsplit("/", 1)[-1] for path in paths}
    names = {template % 12 if "%" in template else template for template in templates}
    assert any(name.startswith("seg") for name in names), names

    worker = _worker_artifact_pattern()
    for name in sorted(names):
        assert _ARTIFACT_NAME.fullmatch(name), f"NAS 产物端点拒收 {name}"
        assert worker.fullmatch(name), f"Mac Worker 上传代理拒收 {name}，要同步改 Swift 白名单"


def _ws_scope(
    *,
    scheme: str = "ws",
    host: str = "192.168.1.10:8000",
    root_path: str = "",
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
):
    """构造一个够 ``_observed_base_url`` 读的最小 WebSocket 作用域。"""
    from starlette.websockets import WebSocket

    headers: list[tuple[bytes, bytes]] = list(extra_headers)
    if host:
        headers.append((b"host", host.encode()))
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": scheme,
        "path": "/api/v1/transcode-worker/ws",
        "raw_path": b"/api/v1/transcode-worker/ws",
        "query_string": b"",
        "root_path": root_path,
        "headers": headers,
        "server": ("10.0.0.2", 8000),
        "client": ("10.0.0.9", 51234),
    }

    async def _noop(*_args, **_kwargs):  # pragma: no cover - 不会被调用
        raise AssertionError("测试不应触发 ASGI 收发")

    return WebSocket(scope, receive=_noop, send=_noop)


def test_observed_base_url_uses_the_address_the_worker_dialed():
    """Worker 从哪个地址连进来，取源/回传就用哪个地址——不需要任何人去填。"""
    from movieclaw_api.api.routes.transcode_worker import _observed_base_url

    assert _observed_base_url(_ws_scope()) == "http://192.168.1.10:8000"


def test_observed_base_url_trusts_forwarded_proto_over_scope_scheme():
    """反向代理终止 TLS 时，到应用的是 ws，但 Worker 够得着的只有 https。"""
    from movieclaw_api.api.routes.transcode_worker import _observed_base_url

    websocket = _ws_scope(
        host="nas.example.com",
        extra_headers=((b"x-forwarded-proto", b"https, http"),),
    )
    assert _observed_base_url(websocket) == "https://nas.example.com"


def test_observed_base_url_keeps_reverse_proxy_subpath():
    """应用挂在子路径下时，少了 root_path 拼出来的 URL 会 404。"""
    from movieclaw_api.api.routes.transcode_worker import _observed_base_url

    websocket = _ws_scope(host="nas.example.com", root_path="/movieclaw/")
    assert _observed_base_url(websocket) == "http://nas.example.com/movieclaw"


def test_observed_base_url_is_empty_without_host_header():
    """推断不出来时返回空串，交给网页上的覆盖项兜底，绝不瞎拼一个上游地址。"""
    from movieclaw_api.api.routes.transcode_worker import _observed_base_url

    assert _observed_base_url(_ws_scope(host="")) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal", "enabled", "hint"),
    [(None, True, "重新配对"), (object(), False, "打开开关")],
    ids=["凭证无效", "开关没开"],
)
async def test_handshake_rejection_reason_reaches_the_worker(monkeypatch, principal, enabled, hint):
    """拒绝必须先 accept 再 1008 关闭。accept 之前 close，uvicorn 回的是空包体
    HTTP 403，理由到不了 Worker；TestClient 不模拟这一点，只能直接看 ASGI 消息。"""
    from starlette.websockets import WebSocket

    from movieclaw_api.api.routes import transcode_worker as route

    async def _principal(_authorization):
        return principal

    monkeypatch.setattr(route, "resolve_worker_principal", _principal)
    monkeypatch.setattr(route, "remote_worker_enabled", lambda: enabled)
    sent: list[dict] = []

    async def _receive():
        return {"type": "websocket.connect"}

    async def _send(message):
        sent.append(message)

    await route.transcode_worker_websocket(
        WebSocket(_ws_scope().scope, receive=_receive, send=_send)
    )
    assert [message["type"] for message in sent] == ["websocket.accept", "websocket.close"]
    assert sent[1]["code"] == 1008
    assert hint in sent[1]["reason"]


@pytest.mark.asyncio
async def test_registry_keeps_each_workers_own_connect_address():
    """两台 Worker 从不同入口连进来时，各自的取源地址不能被对方覆盖。"""
    registry = RemoteWorkerRegistry()
    lan = await registry.register(
        FakeWebSocket(),
        {"worker_id": "mac-lan", "capabilities": {"backends": ["videotoolbox"]}},
        observed_base_url="http://192.168.1.10:8000",
    )
    wan = await registry.register(
        FakeWebSocket(),
        {"worker_id": "mac-wan", "capabilities": {"backends": ["videotoolbox"]}},
        observed_base_url="https://nas.example.com",
    )

    assert lan.observed_base_url == "http://192.168.1.10:8000"
    assert wan.observed_base_url == "https://nas.example.com"


@pytest.mark.asyncio
async def test_playback_position_is_pushed_only_to_workers_declaring_it():
    """``job.playback`` 只发给在 hello 里声明了 playback_progress 的 Worker：旧版不认识
    这条消息，3 秒一条会把它的日志刷满「忽略未知控制消息」。"""
    videotoolbox = {"backends": ["videotoolbox"], "encoders": ["h264_videotoolbox"]}
    snapshot = {"position_ms": 1_510_000, "viewer_paused": False, "duration_ms": 6_730_000}

    old_registry, old_socket = RemoteWorkerRegistry(), FakeWebSocket()
    old = await old_registry.register(
        old_socket, {"worker_id": "mac-old", "capabilities": videotoolbox}
    )
    assert old.capabilities.playback_progress is False
    old_registry.reserve("job-old", backend="videotoolbox")
    await old_registry.report_playback("job-old", snapshot)
    assert old_socket.messages == []

    new_registry, new_socket = RemoteWorkerRegistry(), FakeWebSocket()
    new = await new_registry.register(
        new_socket,
        {"worker_id": "mac-new", "capabilities": {**videotoolbox, "playback_progress": True}},
    )
    assert new.capabilities.playback_progress is True
    new_registry.reserve("job-new", backend="videotoolbox")
    await new_registry.report_playback("job-new", snapshot)
    assert new_socket.messages[-1] == {"type": "job.playback", "job_id": "job-new", **snapshot}
    # 不属于任何 Worker 的任务：安静地什么都不发
    await new_registry.report_playback("job-unknown", snapshot)
    assert len(new_socket.messages) == 1


@pytest.mark.asyncio
async def test_disc_jobs_only_go_to_workers_that_can_read_discs(monkeypatch):
    """原盘的源是 ffconcat 清单（各段带 option 续读参数），旧版 Worker 的 ffmpeg 未必认。"""
    monkeypatch.setattr(remote_worker_module, "remote_worker_enabled", lambda: True)
    registry = RemoteWorkerRegistry()
    base_caps = {"backends": ["videotoolbox"], "encoders": ["h264_videotoolbox"]}
    await registry.register(FakeWebSocket(), {"worker_id": "old-mac", "capabilities": base_caps})
    assert registry.has_capable_worker("videotoolbox") is True
    assert registry.has_capable_worker("videotoolbox", disc=True) is False
    with pytest.raises(RemoteWorkerUnavailable, match="不支持原盘"):
        registry.reserve("disc-job", backend="videotoolbox", disc=True)

    await registry.register(
        FakeWebSocket(),
        {"worker_id": "new-mac", "capabilities": {**base_caps, "disc_sources": True}},
    )
    connection = registry.reserve("disc-job", backend="videotoolbox", disc=True)
    assert connection.worker_id == "new-mac"


def test_undeclared_video_caps_fall_back_to_h264_hevc_without_metal_filters():
    """旧版 Worker 没申报：只按 H.264 / HEVC 能硬解算，其余编码走软解；不用 Metal 滤镜。"""
    caps = RemoteWorkerRegistry._parse_capabilities(
        {"backends": ["videotoolbox"], "encoders": ["h264_videotoolbox"]}
    )
    assert caps.disc_sources is False
    assert caps.video_caps.hw_decoders == frozenset({"h264", "hevc"})
    assert caps.video_caps.filters == frozenset()

    declared = RemoteWorkerRegistry._parse_capabilities(
        {
            "backends": ["videotoolbox"],
            "encoders": ["h264_videotoolbox"],
            "disc_sources": True,
            "hw_decoders": ["h264", "hevc", "mpeg2video", 3],
            "filters": ["scale_vt", "tonemap_videotoolbox"],
        }
    )
    assert declared.disc_sources is True
    assert declared.video_caps.hw_decoders == frozenset({"h264", "hevc", "mpeg2video"})
    assert declared.video_caps.filters == frozenset({"scale_vt", "tonemap_videotoolbox"})
