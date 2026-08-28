"""外置 Worker 控制面、签名范围与远程 HLS 命令测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from movieclaw_api.services.playback import remote_signing
from movieclaw_api.services.playback import remote_worker as remote_worker_module
from movieclaw_api.services.playback.ffmpeg_args import build_hls_command
from movieclaw_api.services.playback.remote_config import RemoteTranscodeRuntimeConfig
from movieclaw_api.services.playback.remote_worker import (
    RemoteWorkerRegistry,
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
        worker_id = await registry.dispatch(
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
    await registry.dispatch("job-a", {}, backend="videotoolbox")
    await registry.unregister(connection)
    event = await registry.wait_job_event("job-a", timeout=0.1)
    assert event["type"] == "job.failed"
    assert "断开" in event["error"]


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
    await registry.dispatch("job-a", {}, backend="videotoolbox")

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
    await registry.dispatch("job-a", {}, backend="videotoolbox")

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
    await registry.dispatch(
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
        worker_token="secret",
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
