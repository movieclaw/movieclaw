"""转码会话管理测试（docs/design/web-player.md §4）。

用**假 ffmpeg**（一段 python 脚本）代替真 ffmpeg：会话管理要验的是进程契约、
并发、配额、心跳回收，与转码内容无关；假进程让这些用例又快又确定。真 ffmpeg
的验证在 ``test_transcode_integration.py``。

重点是那五条进程契约——尤其**契约 3（杀整个进程组）**：``entrypoint.sh`` 的
trap 只 kill API 进程，不会连坐孙子进程；不做这条，每次后端重启都会留下满负荷
烧 GPU、持续写盘的孤儿 ffmpeg。这里专门起一个「会自己再 fork 一层」的假进程
来验证孙子进程真的一起死了。
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from movieclaw_api.services.playback import session as session_mod
from movieclaw_api.services.playback.ffmpeg_args import TranscodeCommand
from movieclaw_api.services.playback.session import (
    DiskQuotaError,
    SessionLimitError,
    SessionStartError,
    TranscodeSessionManager,
)
from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan
from movieclaw_playback.hls_vod import SegmentPlan


def make_plan(tier: PlaybackTier = PlaybackTier.REMUX, file_id: int = 1) -> PlaybackPlan:
    return PlaybackPlan(
        tier=tier,
        file_id=file_id,
        container="hls-fmp4",
        video=VideoPlan(action="copy", codec="h264"),
        audio=AudioPlan(action="copy", track_ref="embedded:0"),
        reason="测试",
    )


# --- 假 ffmpeg：几种行为 -------------------------------------------------

def _script(body: str) -> list[str]:
    return ["python3", "-c", body]


WRITES_PLAYLIST_THEN_SLEEPS = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
out.write_text("#EXTM3U\\n#EXT-X-VERSION:7\\n")
time.sleep(300)
"""

DIES_IMMEDIATELY = """
import sys
print("Invalid data found when processing input", file=sys.stderr)
sys.exit(1)
"""

NEVER_WRITES_PLAYLIST = """
import time
time.sleep(300)
"""

# 自己再 fork 一层：用来验证 killpg 连孙子进程一起收
SPAWNS_GRANDCHILD = """
import subprocess, sys, time, pathlib
out = pathlib.Path(sys.argv[1])
child = subprocess.Popen(["python3", "-c", "import time; time.sleep(300)"])
out.write_text("#EXTM3U\\ngrandchild=%d\\n" % child.pid)
time.sleep(300)
"""


@pytest.fixture
def manager(tmp_path) -> TranscodeSessionManager:
    return TranscodeSessionManager(root=tmp_path / "transcodes")


def install_fake(monkeypatch, body: str, *, delay: float = 0.0) -> None:
    """把命令装配换成假 ffmpeg。``delay`` 模拟 playlist 迟迟不出现。

    playlist 文件名随模式走（真装配同款）：VOD（start_number 非 None）是
    live.m3u8，会话相对模式是 index.m3u8。"""

    def fake_build(
        plan, *, source_path, session_dir, start_ms=0, hw_backend=None, start_number=None
    ):
        name = "live.m3u8" if start_number is not None else "index.m3u8"
        playlist = Path(session_dir) / name
        script = body if delay <= 0 else f"import time; time.sleep({delay})\n{body}"
        return TranscodeCommand(
            argv=[*_script(script), str(playlist)],
            playlist_path=playlist,
            init_path=Path(session_dir) / "init.mp4",
        )

    monkeypatch.setattr(session_mod, "build_hls_command", fake_build)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# 启动：playlist 先行
# ---------------------------------------------------------------------------


async def test_session_is_ready_once_playlist_appears(manager, monkeypatch):
    """playlist 出现即可用——分片按需生成，客户端边拉边转。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    try:
        assert session.state == "ready"
        assert session.playlist_path.exists()
        assert session.process is not None and session.process.returncode is None
    finally:
        await manager.shutdown()


async def test_process_dying_before_playlist_surfaces_stderr(manager, monkeypatch):
    """ffmpeg 起不来时要把 stderr 尾巴带出来，不能只说「失败了」。"""
    install_fake(monkeypatch, DIES_IMMEDIATELY)
    with pytest.raises(SessionStartError) as excinfo:
        await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    assert "Invalid data" in str(excinfo.value)
    assert manager.active() == []  # 失败的会话不能留在表里


async def test_playlist_timeout_fails_and_cleans_up(manager, monkeypatch):
    install_fake(monkeypatch, NEVER_WRITES_PLAYLIST)
    monkeypatch.setattr(session_mod, "PLAYLIST_WAIT_TIMEOUT_S", 0.4)
    with pytest.raises(SessionStartError):
        await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    assert manager.active() == []
    assert list(manager._root.iterdir()) == []  # 目录也要清掉


async def test_direct_play_tier_never_creates_a_session(manager):
    with pytest.raises(ValueError):
        await manager.start(
            make_plan(PlaybackTier.DIRECT_PLAY), source_path="/m/a.mkv", member_id=0
        )


# ---------------------------------------------------------------------------
# 契约 3：杀整个进程组，连孙子进程一起
# ---------------------------------------------------------------------------


async def test_stop_kills_the_whole_process_group(manager, monkeypatch):
    """entrypoint 的 trap 只 kill API 进程，不会连坐孙子。不做 killpg，
    每次后端重启都会留下满负荷烧 GPU、持续写盘的孤儿 ffmpeg。"""
    install_fake(monkeypatch, SPAWNS_GRANDCHILD)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    child_pid = session.process.pid
    grandchild_pid = int(session.playlist_path.read_text().split("grandchild=")[1].strip())
    assert pid_alive(child_pid) and pid_alive(grandchild_pid)

    await manager.stop(session.id)

    assert await wait_until(lambda: not pid_alive(child_pid))
    assert await wait_until(lambda: not pid_alive(grandchild_pid)), (
        "孙子进程没有被回收——killpg 没生效"
    )


async def test_session_runs_in_its_own_process_group(manager, monkeypatch):
    """契约 2：start_new_session=True，否则 killpg 会把后端自己也杀了。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    try:
        assert os.getpgid(session.process.pid) != os.getpgid(os.getpid())
        assert os.getpgid(session.process.pid) == session.process.pid
    finally:
        await manager.shutdown()


async def test_stop_removes_session_directory(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    directory = session.directory
    assert directory.exists()
    await manager.stop(session.id)
    assert not directory.exists()
    assert manager.get(session.id) is None


async def test_shutdown_stops_every_session(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    sessions = [
        await manager.start(make_plan(file_id=i), source_path="/m/a.mkv", member_id=0)
        for i in range(3)
    ]
    pids = [s.process.pid for s in sessions]
    await manager.shutdown()
    for pid in pids:
        assert await wait_until(lambda p=pid: not pid_alive(p))
    assert manager.active() == []


# ---------------------------------------------------------------------------
# 契约 4：stderr 持续读取（管道写满会把 ffmpeg 卡死）
# ---------------------------------------------------------------------------


async def test_stderr_is_drained_so_a_chatty_process_never_blocks(manager, monkeypatch):
    """写满 64KB 管道的进程如果没人读 stderr 就会永久阻塞——
    表现为「转码莫名卡死」。"""
    chatty = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
for i in range(4000):
    print("frame=%d 这是一行足够长的进度输出用来把管道写满" % i, file=sys.stderr)
sys.stderr.flush()
out.write_text("#EXTM3U\\n")
time.sleep(300)
"""
    install_fake(monkeypatch, chatty)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    try:
        # playlist 是在写完 4000 行之后才落的：能 ready 就证明没被管道卡住
        assert session.state == "ready"
        assert len(session.stderr_tail) > 0
        assert len(session.stderr_tail) <= 40  # 只留尾巴，不无限增长
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# 契约 5：启动清残留
# ---------------------------------------------------------------------------


def test_cleanup_orphans_removes_leftover_directories(manager):
    """会话状态只在内存，所以目录里的任何东西都是上次退出留下的垃圾。"""
    manager._root.mkdir(parents=True)
    for name in ("stale-a", "stale-b"):
        directory = manager._root / name
        directory.mkdir()
        (directory / "seg00000.m4s").write_bytes(b"x" * 1024)
    assert manager.cleanup_orphans() == 2
    assert list(manager._root.iterdir()) == []


def test_cleanup_orphans_on_missing_root_is_a_noop(manager):
    assert manager.cleanup_orphans() == 0


# ---------------------------------------------------------------------------
# 并发：两个独立信号量
# ---------------------------------------------------------------------------


async def test_transcode_and_remux_limits_are_counted_separately(manager, monkeypatch):
    """机械盘上两路 4K remux 就能打满随机读而 CPU 是空的——
    瓶颈不在算力，合成一个上限会误判。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    try:
        for i in range(2):
            await manager.start(
                make_plan(PlaybackTier.REMUX, file_id=i),
                source_path="/m/a.mkv", member_id=0, max_remux=2, max_transcode=1,
            )
        # 直通满了
        with pytest.raises(SessionLimitError) as excinfo:
            await manager.start(
                make_plan(PlaybackTier.REMUX, file_id=99),
                source_path="/m/a.mkv", member_id=0, max_remux=2, max_transcode=1,
            )
        assert "2/2" in str(excinfo.value)
        # 但转码额度是独立的，仍然可用
        await manager.start(
            make_plan(PlaybackTier.SOFTWARE_TRANSCODE, file_id=50),
            source_path="/m/a.mkv", member_id=0, max_remux=2, max_transcode=1,
        )
    finally:
        await manager.shutdown()


async def test_transcode_limit_message_names_the_current_usage(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    try:
        await manager.start(
            make_plan(PlaybackTier.HARDWARE_TRANSCODE),
            source_path="/m/a.mkv", member_id=0, max_transcode=1,
        )
        with pytest.raises(SessionLimitError) as excinfo:
            await manager.start(
                make_plan(PlaybackTier.HARDWARE_TRANSCODE, file_id=2),
                source_path="/m/a.mkv", member_id=0, max_transcode=1,
            )
        assert "1/1" in str(excinfo.value) and "转码" in str(excinfo.value)
    finally:
        await manager.shutdown()


async def test_stopped_session_frees_its_slot(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    try:
        first = await manager.start(
            make_plan(), source_path="/m/a.mkv", member_id=0, max_remux=1
        )
        await manager.stop(first.id)
        second = await manager.start(
            make_plan(file_id=2), source_path="/m/a.mkv", member_id=0, max_remux=1
        )
        assert second.state == "ready"
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# 磁盘配额：盘满会让 SQLite 写不进去，整个应用不可用
# ---------------------------------------------------------------------------


async def test_low_free_space_refuses_new_sessions(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    monkeypatch.setattr(
        session_mod.shutil, "disk_usage",
        lambda _p: type("U", (), {"total": 0, "used": 0, "free": 100 * 1024**2})(),
    )
    with pytest.raises(DiskQuotaError) as excinfo:
        await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    assert "磁盘剩余空间不足" in str(excinfo.value)


async def test_quota_exhaustion_refuses_new_sessions(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    manager._root.mkdir(parents=True)
    (manager._root / "old").mkdir()
    (manager._root / "old" / "seg.m4s").write_bytes(b"x" * 4096)
    with pytest.raises(DiskQuotaError) as excinfo:
        await manager.start(
            make_plan(), source_path="/m/a.mkv", member_id=0, quota_bytes=1024
        )
    assert "配额" in str(excinfo.value)


async def test_usage_bytes_counts_session_output(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    try:
        (session.directory / "seg00000.m4s").write_bytes(b"x" * 2048)
        assert manager.usage_bytes() >= 2048
        assert session.size_bytes() >= 2048
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# 心跳回收：用户关页面不会发任何信号
# ---------------------------------------------------------------------------


async def test_idle_session_is_reaped(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    monkeypatch.setattr(session_mod, "SESSION_IDLE_TIMEOUT_S", 0.2)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    pid = session.process.pid
    await asyncio.sleep(0.3)
    assert await manager.reap() == 1
    assert manager.get(session.id) is None
    assert await wait_until(lambda: not pid_alive(pid))


async def test_ping_keeps_session_alive(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    monkeypatch.setattr(session_mod, "SESSION_IDLE_TIMEOUT_S", 0.3)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    try:
        for _ in range(3):
            await asyncio.sleep(0.15)
            assert manager.ping(session.id) is True
        assert await manager.reap() == 0
        assert manager.get(session.id) is not None
    finally:
        await manager.shutdown()


async def test_ping_unknown_session_returns_false(manager):
    assert manager.ping("nope") is False


# ---------------------------------------------------------------------------
# 归属隔离与 seek 复用
# ---------------------------------------------------------------------------


async def test_session_is_private_to_its_member(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=7)
    try:
        assert manager.get(session.id, member_id=7) is not None
        assert manager.get(session.id, member_id=8) is None
        assert manager.ping(session.id, member_id=8) is False
    finally:
        await manager.shutdown()


async def test_new_session_for_same_file_replaces_the_old_one(manager, monkeypatch):
    """seek 出已转区间要起新会话，旧的必须先杀——不然用户连拖五下进度条
    就有五个 ffmpeg 在跑，NAS 直接躺平。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    try:
        first = await manager.start(make_plan(file_id=42), source_path="/m/a.mkv", member_id=1)
        pid = first.process.pid
        assert await manager.stop_for_file(42, member_id=1) == 1
        assert await wait_until(lambda: not pid_alive(pid))
        assert manager.get(first.id) is None
    finally:
        await manager.shutdown()


async def test_stop_for_file_leaves_other_members_alone(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    try:
        mine = await manager.start(make_plan(file_id=42), source_path="/m/a.mkv", member_id=1)
        theirs = await manager.start(make_plan(file_id=42), source_path="/m/a.mkv", member_id=2)
        assert await manager.stop_for_file(42, member_id=1) == 1
        assert manager.get(mine.id) is None
        assert manager.get(theirs.id) is not None
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# 巡检任务
# ---------------------------------------------------------------------------


async def test_reaper_task_starts_and_stops_cleanly(manager, monkeypatch):
    monkeypatch.setattr(session_mod, "REAP_INTERVAL_S", 0.05)
    manager.start_reaper()
    assert manager._reaper is not None and not manager._reaper.done()
    await asyncio.sleep(0.15)
    await manager.shutdown()
    assert manager._reaper is None


async def test_reaper_survives_an_exception(manager, monkeypatch):
    """巡检不能因单次异常停摆。"""
    monkeypatch.setattr(session_mod, "REAP_INTERVAL_S", 0.02)
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("巡检炸了")
        return 0

    monkeypatch.setattr(manager, "reap", boom)
    manager.start_reaper()
    await asyncio.sleep(0.2)
    assert calls["n"] >= 2, "第一次异常之后巡检就没再跑"
    await manager.shutdown()


async def test_stop_unknown_session_is_a_noop(manager):
    assert await manager.stop("nope") is False


async def test_signal_handler_contract_uses_killpg(manager, monkeypatch):
    """守护测试：终止路径必须走 killpg，不能只 terminate 单个进程。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    seen: list[tuple[int, int]] = []
    real_killpg = os.killpg
    monkeypatch.setattr(
        session_mod.os, "killpg",
        lambda pgid, sig: (seen.append((pgid, sig)), real_killpg(pgid, sig))[1],
    )
    await manager.stop(session.id)
    assert seen and seen[0][1] == signal.SIGTERM


# ---------------------------------------------------------------------------
# 竞态与异常路径
# ---------------------------------------------------------------------------


async def test_concurrent_starts_respect_the_limit(manager, monkeypatch):
    """并发起会话不能突破上限——检查与占位之间如果有 await，就会漏。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    results = await asyncio.gather(
        *[
            manager.start(
                make_plan(file_id=i), source_path="/m/a.mkv", member_id=0, max_remux=2
            )
            for i in range(5)
        ],
        return_exceptions=True,
    )
    started = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, SessionLimitError)]
    try:
        assert len(started) + len(refused) == 5
        assert len(started) <= 2, f"并发突破了上限：起了 {len(started)} 个"
        assert refused, "全都起成功了，上限没生效"
    finally:
        await manager.shutdown()


async def test_concurrent_stops_are_idempotent(manager, monkeypatch):
    """同一会话被并发停止（用户点停 + 心跳超时 + 关页面）不能炸。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    pid = session.process.pid
    results = await asyncio.gather(*[manager.stop(session.id) for _ in range(4)])
    assert sum(1 for r in results if r) == 1  # 只有一个真的停掉了
    assert await wait_until(lambda: not pid_alive(pid))


async def test_session_dying_midway_is_reported_not_hidden(manager, monkeypatch):
    """ffmpeg 起来之后半路崩了——不能假装还活着让客户端一直拉空分片。"""
    dies_after_playlist = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
out.write_text("#EXTM3U\\n")
time.sleep(0.2)
print("Segmentation fault", file=sys.stderr)
sys.exit(139)
"""
    install_fake(monkeypatch, dies_after_playlist)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    try:
        assert session.state == "ready"
        assert await wait_until(lambda: session.process.returncode is not None)
        # 进程已死但 stderr 尾巴留着，供诊断面板与日志定位
        assert any("Segmentation fault" in line for line in session.stderr_tail)
    finally:
        await manager.shutdown()


async def test_one_member_can_play_two_different_files(manager, monkeypatch):
    """停旧会话只针对同一文件——同时开两部片是正常使用。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    try:
        first = await manager.start(make_plan(file_id=1), source_path="/m/a.mkv", member_id=5)
        second = await manager.start(make_plan(file_id=2), source_path="/m/b.mkv", member_id=5)
        assert await manager.stop_for_file(1, member_id=5) == 1
        assert manager.get(first.id) is None
        assert manager.get(second.id) is not None
    finally:
        await manager.shutdown()


async def test_ping_during_reap_does_not_resurrect_a_stopped_session(manager, monkeypatch):
    """回收和心跳撞车时，回收赢——会话已经杀了就不该被 ping 复活。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    monkeypatch.setattr(session_mod, "SESSION_IDLE_TIMEOUT_S", 0.1)
    session = await manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    await asyncio.sleep(0.15)
    await manager.reap()
    assert manager.ping(session.id) is False
    assert manager.get(session.id) is None


async def test_start_failure_frees_the_concurrency_slot(manager, monkeypatch):
    """启动失败必须把占用的名额还回去，否则失败几次就再也起不了会话。"""
    install_fake(monkeypatch, DIES_IMMEDIATELY)
    for _ in range(3):
        with pytest.raises(SessionStartError):
            await manager.start(
                make_plan(), source_path="/m/a.mkv", member_id=0, max_remux=1
            )
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(
        make_plan(), source_path="/m/a.mkv", member_id=0, max_remux=1
    )
    try:
        assert session.state == "ready"
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# 磁盘低水位哨兵：空间见底暂停写入、回升恢复、暂停中也能被杀掉
# ---------------------------------------------------------------------------


class _FakeDiskUsage:
    def __init__(self, free: int) -> None:
        self.free = free
        self.total = 100 * 1024**3
        self.used = self.total - free


def set_free_bytes(monkeypatch, free: int) -> None:
    monkeypatch.setattr(
        session_mod.shutil, "disk_usage", lambda _path: _FakeDiskUsage(free)
    )


def _process_stopped(pid: int) -> bool:
    """进程组长处于 SIGSTOP 挂起态（macOS/Linux 的 ps 状态首字母 T）。"""
    import subprocess

    out = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True
    )
    return out.stdout.strip().startswith("T")


@pytest.mark.asyncio
async def test_low_watermark_pauses_running_sessions(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/tmp/a.mkv", member_id=1)

    set_free_bytes(monkeypatch, session_mod.MIN_FREE_BYTES - 1)
    await manager.reap()

    assert session.disk_paused is True
    assert await wait_until(lambda: _process_stopped(session.process.pid))
    await manager.shutdown()


@pytest.mark.asyncio
async def test_recovered_disk_resumes_paused_sessions(manager, monkeypatch):
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/tmp/a.mkv", member_id=1)

    set_free_bytes(monkeypatch, session_mod.MIN_FREE_BYTES - 1)
    await manager.reap()
    assert session.disk_paused is True

    # 回升到恢复线之上才放行——迟滞回差，避免在临界值附近反复停/走
    set_free_bytes(monkeypatch, session_mod.RESUME_FREE_BYTES)
    await manager.reap()

    assert session.disk_paused is False
    assert await wait_until(lambda: not _process_stopped(session.process.pid))
    await manager.shutdown()


@pytest.mark.asyncio
async def test_between_watermarks_keeps_paused_state(manager, monkeypatch):
    """两档水位之间（回差区）不改变现状：已暂停的保持暂停。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/tmp/a.mkv", member_id=1)

    set_free_bytes(monkeypatch, session_mod.MIN_FREE_BYTES - 1)
    await manager.reap()
    set_free_bytes(monkeypatch, (session_mod.MIN_FREE_BYTES + session_mod.RESUME_FREE_BYTES) // 2)
    await manager.reap()

    assert session.disk_paused is True
    await manager.shutdown()


@pytest.mark.asyncio
async def test_low_watermark_pauses_and_resumes_remote_worker_session(manager, monkeypatch):
    """远程 ffmpeg 没有 NAS PID，也必须受到同一套磁盘水位保护。"""

    class FakeRemoteRegistry:
        def __init__(self) -> None:
            self.commands: list[tuple[str, str]] = []

        def job_state(self, job_id: str) -> dict[str, str]:
            assert job_id == "remote-job"
            return {"type": "job.accepted"}

        async def pause(self, job_id: str) -> bool:
            self.commands.append(("pause", job_id))
            return True

        async def resume(self, job_id: str) -> bool:
            self.commands.append(("resume", job_id))
            return True

    registry = FakeRemoteRegistry()
    monkeypatch.setattr(session_mod, "get_remote_worker_registry", lambda: registry)
    remote = session_mod.TranscodeSession(
        id="remote-session",
        file_id=1,
        member_id=1,
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        directory=manager.cache_root / "remote-session",
        start_ms=0,
        plan=make_plan(PlaybackTier.HARDWARE_TRANSCODE),
        state="ready",
        remote=True,
        remote_worker_id="mac-mini-a",
        remote_job_id="remote-job",
    )
    remote.directory.mkdir(parents=True)
    manager._sessions[remote.id] = remote

    set_free_bytes(monkeypatch, session_mod.MIN_FREE_BYTES - 1)
    await manager.reap()
    assert remote.disk_paused is True
    assert registry.commands == [("pause", "remote-job")]

    set_free_bytes(monkeypatch, session_mod.RESUME_FREE_BYTES)
    await manager.reap()
    assert remote.disk_paused is False
    assert registry.commands == [("pause", "remote-job"), ("resume", "remote-job")]


@pytest.mark.asyncio
async def test_remote_session_dispatches_job_without_local_process(manager, monkeypatch):
    """远程会话只登记控制面任务，NAS 进程表里不能出现本地 ffmpeg。"""

    class FakeRemoteRegistry:
        def __init__(self) -> None:
            self.dispatches: list[tuple[str, dict, str]] = []
            self.cancelled: list[str] = []
            self.removed: list[str] = []

        def create_job_waiter(self, job_id: str) -> None:
            self.waiting = job_id

        async def dispatch(self, job_id: str, payload: dict, *, backend: str) -> str:
            self.dispatches.append((job_id, payload, backend))
            return "mac-mini-a"

        async def wait_job_event(self, job_id: str) -> dict[str, str]:
            assert job_id == self.waiting
            return {"type": "job.accepted"}

        def job_state(self, job_id: str) -> dict[str, str]:
            assert job_id == self.waiting
            return {"type": "job.accepted"}

        def worker_online(self, worker_id: str | None) -> bool:
            return worker_id == "mac-mini-a"

        def remove_job_waiter(self, job_id: str) -> None:
            assert job_id == self.waiting

        async def cancel(self, job_id: str) -> None:
            self.cancelled.append(job_id)

        def remove_job(self, job_id: str) -> None:
            self.removed.append(job_id)

    async def fake_issue_remote_grant(**kwargs: object) -> str:
        return f"{kwargs['kind']}-grant"

    registry = FakeRemoteRegistry()
    monkeypatch.setattr(session_mod, "get_remote_worker_registry", lambda: registry)
    monkeypatch.setattr(session_mod, "issue_remote_grant", fake_issue_remote_grant)
    plan = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=1,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264", height=1080),
        audio=AudioPlan(action="copy", track_ref=None),
        reason="测试",
    )

    session = await manager.start(
        plan,
        source_path="/media/movie.mkv",
        member_id=1,
        hw_backend="videotoolbox",
        segment_plan=_boundaries(2),
        use_remote=True,
        remote_base_url="https://nas.example.com",
    )

    assert session.remote is True
    assert session.process is None
    assert session.remote_worker_id == "mac-mini-a"
    job_id, payload, backend = registry.dispatches[0]
    assert session.remote_job_id == job_id
    assert backend == "videotoolbox"
    assert "https://nas.example.com/api/v1/transcode-worker/sessions/" in " ".join(
        payload["ffmpeg_args"]
    )

    assert await manager.stop(session.id) is True
    assert registry.cancelled == [job_id]
    assert registry.removed == [job_id]


@pytest.mark.asyncio
async def test_remote_start_failure_does_not_fallback_to_local_software(manager, monkeypatch):
    """远程 Worker 在首片前不可用时，不能绕过软件转码同意门槛。"""

    class FakeRemoteRegistry:
        def __init__(self) -> None:
            self.cancelled: list[str] = []
            self.removed: list[str] = []

        def create_job_waiter(self, job_id: str) -> None:
            self.job_id = job_id

        async def dispatch(self, job_id: str, payload: dict, *, backend: str) -> str:
            raise session_mod.RemoteWorkerUnavailable("Worker 刚刚断线")

        def remove_job_waiter(self, job_id: str) -> None:
            pass

        async def cancel(self, job_id: str) -> None:
            self.cancelled.append(job_id)

        def remove_job(self, job_id: str) -> None:
            self.removed.append(job_id)

    local_builds: list[str | None] = []

    def fake_build(
        plan,
        *,
        source_path,
        session_dir,
        start_ms=0,
        hw_backend=None,
        start_number=None,
        output_base_url=None,
        output_url_suffix="",
    ):
        name = "live.m3u8" if start_number is not None else "index.m3u8"
        playlist = Path(session_dir) / name
        if output_base_url:
            return TranscodeCommand(
                argv=["python3", "-c", "pass"],
                playlist_path=playlist,
                init_path=Path(session_dir) / "init.mp4",
            )
        local_builds.append(hw_backend)
        return TranscodeCommand(
            argv=[*_script(WRITES_PLAYLIST_THEN_SLEEPS), str(playlist)],
            playlist_path=playlist,
            init_path=Path(session_dir) / "init.mp4",
        )

    registry = FakeRemoteRegistry()
    monkeypatch.setattr(session_mod, "get_remote_worker_registry", lambda: registry)

    async def fake_issue_remote_grant(**_: object) -> str:
        return "grant"

    monkeypatch.setattr(session_mod, "issue_remote_grant", fake_issue_remote_grant)
    monkeypatch.setattr(session_mod, "build_hls_command", fake_build)
    plan = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=1,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264", height=1080),
        audio=AudioPlan(action="copy"),
        reason="测试",
    )

    with pytest.raises(SessionStartError, match="远程转码不可用"):
        await manager.start(
            plan,
            source_path="/media/movie.mkv",
            member_id=1,
            hw_backend="videotoolbox",
            segment_plan=_boundaries(2),
            use_remote=True,
            remote_base_url="https://nas.example.com",
        )

    assert manager.active() == []
    assert local_builds == []
    assert len(registry.cancelled) == 1
    assert registry.removed == registry.cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "timeout"])
async def test_remote_restart_failure_cleans_up_new_job(manager, tmp_path, monkeypatch, failure):
    """seek 重启在新 job 接单失败或超时时，不能留下远程槽位和运行任务。"""

    class FakeRemoteRegistry:
        def __init__(self) -> None:
            self.created: str | None = None
            self.cancelled: list[tuple[str, bool]] = []
            self.removed: list[str] = []
            self.waiters_removed: list[str] = []

        def create_job_waiter(self, job_id: str) -> None:
            self.created = job_id

        async def dispatch(self, job_id: str, payload: dict, *, backend: str) -> str:
            return "mac-mini-a"

        async def wait_job_event(self, job_id: str) -> dict[str, str]:
            assert job_id == self.created
            if failure == "timeout":
                raise session_mod.RemoteWorkerUnavailable("远程 Worker 接单超时")
            return {"type": "job.failed", "error": "Worker 拒绝 seek 任务"}

        async def cancel(self, job_id: str, *, force: bool = False) -> None:
            self.cancelled.append((job_id, force))

        def remove_job(self, job_id: str) -> None:
            self.removed.append(job_id)

        def remove_job_waiter(self, job_id: str) -> None:
            self.waiters_removed.append(job_id)

    registry = FakeRemoteRegistry()
    monkeypatch.setattr(session_mod, "get_remote_worker_registry", lambda: registry)

    async def fake_issue_remote_grant(**_: object) -> str:
        return "grant"

    monkeypatch.setattr(session_mod, "issue_remote_grant", fake_issue_remote_grant)
    session = _vod_session(tmp_path / "session", head=0, completed=set())
    session.remote = True
    session.state = "ready"
    session.hw_backend = "videotoolbox"
    session.remote_source_url = "https://nas.example.com/source"
    session.remote_artifact_base_url = "https://nas.example.com/artifacts"
    session.directory.mkdir(parents=True)

    with pytest.raises(SessionStartError):
        await manager._restart_remote(session, 0)

    assert registry.created is not None
    assert registry.cancelled == [(registry.created, True)]
    assert registry.removed == [registry.created]
    assert registry.waiters_removed == [registry.created]
    assert session.remote_job_id is None
    assert session.remote_worker_id is None
    assert session.state == "failed"


@pytest.mark.asyncio
async def test_paused_session_can_still_be_stopped(manager, monkeypatch):
    """挂起的进程不响应 SIGTERM，stop 必须先 SIGCONT 再杀，否则要干等超时。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    session = await manager.start(make_plan(), source_path="/tmp/a.mkv", member_id=1)
    pid = session.process.pid

    set_free_bytes(monkeypatch, session_mod.MIN_FREE_BYTES - 1)
    await manager.reap()
    assert await wait_until(lambda: _process_stopped(pid))

    started = time.monotonic()
    assert await manager.stop(session.id) is True
    # 走的是 SIGCONT+SIGTERM 快路径，而不是 3 秒超时后的 SIGKILL
    assert time.monotonic() - started < 2.0
    assert await wait_until(lambda: not pid_alive(pid))


# --- VOD 供片判定（不起进程的纯逻辑回归） ---------------------------------


def _vod_session(tmp_path: Path, *, head: int, completed: set[int]) -> session_mod.TranscodeSession:
    plan = SegmentPlan(boundaries=tuple(float(i * 4) for i in range(800)), duration_s=3200.0)
    return session_mod.TranscodeSession(
        id="vod-test",
        file_id=1,
        member_id=0,
        tier=PlaybackTier.REMUX,
        directory=tmp_path,
        start_ms=0,
        plan=make_plan(),
        segment_plan=plan,
        head_segment=head,
        completed_segments=set(completed),
    )


def test_highest_produced_counts_contiguous_run_only(manager, tmp_path):
    """回归：续播起在 783、被杂散请求拉回第 0 段重启后，台账里上一轮的
    783/784 全都 ≥ head(0)。取 max 会把 produced 顶成 784，于是请求 785 时
    ahead 判定失灵——既不重启也永远等不来，表现为播放器一直缓冲。
    正确口径是**从 head 起连续**产出到哪。"""
    session = _vod_session(tmp_path, head=0, completed={0, 1, 783, 784})
    assert manager._highest_produced(session) == 1
    # 一个都没写完：head - 1
    session = _vod_session(tmp_path, head=10, completed={783, 784})
    assert manager._highest_produced(session) == 9
    # 与上一轮无缝衔接时旧分片照常计入——它们本来就直接可服务
    session = _vod_session(tmp_path, head=782, completed={782, 783, 784})
    assert manager._highest_produced(session) == 784


def test_sync_completed_accepts_remote_absolute_playlist_urls(manager, tmp_path):
    session = _vod_session(tmp_path, head=0, completed=set())
    session.playlist_path.write_text(
        "#EXTM3U\n"
        'https://nas.example.com/artifacts/seg00000.m4s?token=artifact\n'
        'https://nas.example.com/artifacts/seg00001.m4s?token=artifact\n',
        encoding="utf-8",
    )

    manager._sync_completed(session)

    assert session.completed_segments == {0, 1}


def test_remote_atomic_segment_is_ready_without_live_playlist(manager, tmp_path):
    """远程分片已原子落盘时，不应因 live.m3u8 上传中断而等待 30 秒。"""
    session = _vod_session(tmp_path, head=0, completed=set())
    session.remote = True
    session.remote_job_id = "remote-job"
    session.directory.mkdir(parents=True, exist_ok=True)
    (session.directory / "seg00000.m4s").touch()
    assert manager._segment_ready(session, 0) is False
    (session.directory / "seg00000.m4s").write_bytes(b"complete-segment")

    assert manager._segment_ready(session, 0) is True
    assert session.completed_segments == {0}
    assert manager._highest_produced(session) == 0


@pytest.mark.asyncio
async def test_remote_failed_segment_upload_is_retried(manager, tmp_path, monkeypatch):
    """回归：中间分片上传失败、后续分片已到位时，不能继续等缺口自然出现。"""
    session = _vod_session(tmp_path, head=0, completed={0, 1, 2, 4, 5})
    session.remote = True
    session.remote_job_id = "remote-job"
    session.pending_segments[3] = 1
    session.pending_since[3] = time.monotonic()
    session.record_remote_upload(
        "seg00003.m4s",
        status=499,
        received_bytes=123,
        content_length=456,
        transfer_encoding="chunked",
    )
    restarted: list[int] = []

    async def fake_restart(_session, index: int) -> None:
        restarted.append(index)

    monkeypatch.setattr(manager, "_restart_remote", fake_restart)

    await manager._maybe_restart_for(session, 3)

    assert restarted == [3]
    assert session.restart_generation == 1


@pytest.mark.asyncio
async def test_remote_restart_window_does_not_report_worker_offline(manager, tmp_path, monkeypatch):
    """远程 seek 切换 job 时，worker id 暂空不能把正常切换判成断线。"""
    session = _vod_session(tmp_path, head=0, completed=set())
    session.remote = True
    session.remote_restarting = True
    session.directory.mkdir(parents=True, exist_ok=True)
    session.pending_segments[0] = 1
    session.pending_since[0] = time.monotonic()

    class FakeRemoteRegistry:
        def __init__(self) -> None:
            self.worker_checks = 0

        def job_state(self, _job_id: str) -> dict[str, str] | None:
            raise AssertionError("重启窗口不应读取旧 job 状态")

        def worker_online(self, _worker_id: str | None) -> bool:
            self.worker_checks += 1
            return False

    registry = FakeRemoteRegistry()
    monkeypatch.setattr(session_mod, "get_remote_worker_registry", lambda: registry)
    monkeypatch.setattr(TranscodeSessionManager, "_SEGMENT_WAIT_S", 0.5)

    async def finish_segment() -> None:
        await asyncio.sleep(0.05)
        (session.directory / "seg00000.m4s").write_bytes(b"complete")

    target = session.directory / "seg00000.m4s"
    result, _ = await asyncio.gather(
        manager._await_segment(
            session,
            0,
            target,
            time.monotonic(),
            0,
            time.monotonic() + 0.5,
            0,
        ),
        finish_segment(),
    )

    assert result == target
    assert registry.worker_checks == 0


@pytest.mark.asyncio
async def test_remote_cached_segment_survives_worker_offline(manager, tmp_path, monkeypatch):
    """整片转完后 Mac 关机，缓存里已落盘的分片仍要能播。

    回归守卫：在线状态检查曾排在就绪检查之前且不看分片是否已落盘，导致
    远程 job 正常 finished、Worker 下线之后，每一个分片请求都被打成
    failed——已经转好的整部片子被整体作废。
    """
    session = _vod_session(tmp_path, head=0, completed=set())
    session.remote = True
    session.remote_job_id = "job-1"
    session.remote_worker_id = "worker-1"
    session.directory.mkdir(parents=True, exist_ok=True)
    session.pending_segments[0] = 1
    session.pending_since[0] = time.monotonic()

    # 分片早已完整落盘（远程上传端点是原子替换，存在即完整）
    target = session.directory / "seg00000.m4s"
    target.write_bytes(b"complete")

    class OfflineRegistry:
        def job_state(self, _job_id: str) -> dict[str, str] | None:
            return {"type": "job.finished"}

        def worker_online(self, _worker_id: str | None) -> bool:
            return False

    monkeypatch.setattr(session_mod, "get_remote_worker_registry", lambda: OfflineRegistry())

    result = await manager._await_segment(
        session, 0, target, time.monotonic(), 0, time.monotonic() + 0.5, 0
    )

    assert result == target
    assert session.state != "failed"


@pytest.mark.asyncio
async def test_remote_missing_segment_after_job_end_still_fails(manager, tmp_path, monkeypatch):
    """job 已结束而分片确实没落盘时，仍必须快速失败而不是空等到超时。"""
    session = _vod_session(tmp_path, head=0, completed=set())
    session.remote = True
    session.remote_job_id = "job-1"
    session.remote_worker_id = "worker-1"
    session.directory.mkdir(parents=True, exist_ok=True)
    session.pending_segments[0] = 1
    session.pending_since[0] = time.monotonic()

    class OfflineRegistry:
        def job_state(self, _job_id: str) -> dict[str, str] | None:
            return {"type": "job.failed", "error": "VideoToolbox 初始化失败"}

        def worker_online(self, _worker_id: str | None) -> bool:
            return False

    monkeypatch.setattr(session_mod, "get_remote_worker_registry", lambda: OfflineRegistry())

    target = session.directory / "seg00000.m4s"
    result = await manager._await_segment(
        session, 0, target, time.monotonic(), 0, time.monotonic() + 0.5, 0
    )

    assert result is None
    assert session.state == "failed"


def _boundaries(count: int) -> SegmentPlan:
    return SegmentPlan(boundaries=tuple(float(i * 4) for i in range(count)), duration_s=count * 4.0)


@pytest.mark.asyncio
async def test_vod_start_does_not_wait_for_live_playlist(manager, monkeypatch):
    """VOD 客户端列表由服务端生成，不依赖 ffmpeg 的 live.m3u8——起播响应
    不该为它白等（实测 0.3~0.8 秒）。进程活着、快速失败窗口过了就放行。"""
    install_fake(monkeypatch, NEVER_WRITES_PLAYLIST)
    started = time.monotonic()
    session = await manager.start(
        make_plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_boundaries(800)
    )
    try:
        assert session.state == "ready"
        assert not session.playlist_path.exists()  # 真的没等它出现
        assert session.process is not None and session.process.returncode is None
        assert time.monotonic() - started < 2.0  # 只守 0.3 秒窗口，不是 30 秒超时
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_vod_start_still_fails_fast_on_dead_process(manager, monkeypatch):
    """快速放行不放过「命令本身有错、进程秒退」——这类要立刻给带 stderr
    的明确报错，而不是让用户对着分片 404 循环猜。"""
    install_fake(monkeypatch, DIES_IMMEDIATELY)
    # 窗口放宽到 2 秒：CI 慢机器上 python 假进程可能起得比 0.3 秒还慢，
    # 这里要验的是「窗口内死亡必报错」，不是窗口的具体长度
    monkeypatch.setattr(session_mod, "VOD_FAST_FAIL_WINDOW_S", 2.0)
    with pytest.raises(SessionStartError) as excinfo:
        await manager.start(
            make_plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_boundaries(800)
        )
    assert "Invalid data" in str(excinfo.value)
    assert manager.active() == []


@pytest.mark.asyncio
async def test_seek_restart_kills_without_sigterm_grace(manager, monkeypatch):
    """seek 重启走 SIGKILL 快杀：SIGTERM 让 ffmpeg 收尾写完当前分片要一两秒，
    而用户正对着转圈等新位置——没写完的分片不进列表、重启后会被覆盖，
    立杀没有损失。优雅退出只属于 stop()（契约 3 的测试另有覆盖）。"""
    writes_live_then_sleeps = """
import sys, time, pathlib
pathlib.Path(sys.argv[1]).write_text("#EXTM3U\\n#EXT-X-VERSION:7\\n")
time.sleep(300)
"""
    install_fake(monkeypatch, writes_live_then_sleeps)
    monkeypatch.setattr(TranscodeSessionManager, "_SEGMENT_WAIT_S", 0.6)
    session = await manager.start(
        make_plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_boundaries(800)
    )
    seen: list[int] = []
    real_killpg = os.killpg
    monkeypatch.setattr(
        session_mod.os, "killpg",
        lambda pgid, sig: (seen.append(sig), real_killpg(pgid, sig))[1],
    )
    try:
        # 远处分片触发重启；假进程不产分片，等待窗内拿不到 → None
        assert await manager.ensure_segment(session, 50) is None
        assert session.head_segment == 50  # 重启确实发生了
        assert signal.SIGKILL in seen
        assert signal.SIGTERM not in seen
    finally:
        monkeypatch.setattr(session_mod.os, "killpg", real_killpg)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_large_seek_wins_over_stale_nearby_request(manager, monkeypatch):
    """用户跳到远处时，旧的头部探测请求不能把目标拖满 30 秒。"""
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS)
    monkeypatch.setattr(TranscodeSessionManager, "_SEGMENT_WAIT_S", 1.0)
    session = await manager.start(
        make_plan(), source_path="/m/a.mkv", member_id=0,
        segment_plan=_boundaries(800),
    )
    # 模拟日志中的状态：当前轮次已经连续产出到 12，13 是播放器仍挂着的
    # 近邻请求，366 才是随后到达的真正跳转目标。
    session.completed_segments.update(range(13))
    try:
        await asyncio.gather(
            manager.ensure_segment(session, 13),
            manager.ensure_segment(session, 366),
        )
        assert session.head_segment == 366
        assert session.restart_generation == 1
        assert session.restart_target == 366
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_parallel_prefetch_never_triggers_restart_storm(manager, monkeypatch):
    """回归（2026-08-25 iPhone 真机事故）：AVPlayer 会**并行**请求相邻分片。
    重启判定若只看单个请求，阈值 1 之下会互相杀——N+2 嫌转码器超前杀过去、
    N 发现落后又杀回来，一片都产不出直到超时。判定必须看全体等待者的最小
    分片号：并行预取时最小号紧贴转码头 → 全员等待，一次都不许杀。"""
    # 假 ffmpeg：起步 0.3 秒后每 0.25 秒产出一片（写文件 + 进列表），模拟
    # 慢速软转；三个并发请求到达时它一片都还没写完——事故的并发窗口
    slow_producer = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
d = out.parent
out.write_text("#EXTM3U\\n#EXT-X-VERSION:7\\n")
time.sleep(0.3)
for i in range(6):
    (d / ("seg%05d.m4s" % i)).write_bytes(b"x" * 64)
    with out.open("a") as f:
        f.write("#EXTINF:4.0,\\nseg%05d.m4s\\n" % i)
    time.sleep(0.25)
time.sleep(300)
"""
    install_fake(monkeypatch, slow_producer)
    session = await manager.start(
        make_plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_boundaries(800)
    )
    kills: list[int] = []
    real_killpg = os.killpg
    monkeypatch.setattr(
        session_mod.os, "killpg",
        lambda pgid, sig: (kills.append(sig), real_killpg(pgid, sig))[1],
    )
    try:
        results = await asyncio.gather(
            manager.ensure_segment(session, 0),
            manager.ensure_segment(session, 1),
            manager.ensure_segment(session, 2),
        )
        assert all(r is not None for r in results), "并行预取的分片没有全部就绪"
        assert kills == [], f"并行预取触发了重启风暴：{kills}"
        assert session.head_segment == 0  # 转码器从未被打断
        assert session.pending_segments == {}  # 挂号表清干净
    finally:
        monkeypatch.setattr(session_mod.os, "killpg", real_killpg)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_restart_never_bounces_between_waiters(manager, monkeypatch):
    """重启绝不在等待者之间往复互杀（3↔7 事故形态）。

    语义随探测宽限调整（2026-08-25）：有前方（≥ head）等待者时，落后者一律
    让路——否则 AVPlayer 的列表头探测就能劫持转码器（另一场真机事故）。
    落后者的出路：真实回拖时播放器会取消前方请求，它随即成为独行等待者，
    熬过宽限期照常重启（见 test_lone_backward_seek_still_restarts_after_grace）。
    这里守住的底线：无论并发怎么竞速，头部只会单调走到一个胜者，绝不回弹。"""
    writes_live_then_exits = """
import sys, pathlib
pathlib.Path(sys.argv[1]).write_text("#EXTM3U\\n#EXT-X-VERSION:7\\n")
"""
    install_fake(monkeypatch, writes_live_then_exits)
    monkeypatch.setattr(TranscodeSessionManager, "_SEGMENT_WAIT_S", 2.0)
    session = await manager.start(
        make_plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_boundaries(800)
    )
    heads: list[int] = []
    real_spawn = manager._spawn

    async def spy_spawn(sess, command):
        heads.append(sess.head_segment)
        return await real_spawn(sess, command)

    monkeypatch.setattr(manager, "_spawn", spy_spawn)
    try:
        await asyncio.gather(
            manager.ensure_segment(session, 7),
            manager.ensure_segment(session, 3),
        )
        assert heads, "进程已死却从未尝试重启"
        # 竞速窗口内第一记可能打到 3 或 7，但此后必须钉死在那个号上——
        # 3↔7 往复互杀（每 50ms 一次 SIGKILL + spawn）是当年的事故形态
        winner = heads[0]
        assert all(h == winner for h in heads), f"出现回弹互杀：{heads[:12]}"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_head_probe_never_hijacks_restart(manager, monkeypatch):
    """回归（2026-08-25 iPhone 真机事故，PGS 烧录切换）：原生 HLS 改 JS seek
    后，AVPlayer 在 seek 落地前会从列表头狂打 seg00000。会话起在分片 317，
    探测请求（0）与正经请求（317）并发到达——落后于头且有前方等待者的
    请求绝不能触发重启，否则转码器被劫持回第 0 段、起播点反而挨饿。"""
    # 假 ffmpeg：模拟从 317 起转的慢速软转，起步后陆续产出 317~322
    slow_producer_317 = """
import sys, time, pathlib
out = pathlib.Path(sys.argv[1])
d = out.parent
out.write_text("#EXTM3U\\n#EXT-X-VERSION:7\\n")
time.sleep(0.3)
for i in range(317, 323):
    (d / ("seg%05d.m4s" % i)).write_bytes(b"x" * 64)
    with out.open("a") as f:
        f.write("#EXTINF:4.0,\\nseg%05d.m4s\\n" % i)
    time.sleep(0.25)
time.sleep(300)
"""
    install_fake(monkeypatch, slow_producer_317)
    monkeypatch.setattr(TranscodeSessionManager, "_SEGMENT_WAIT_S", 2.0)
    session = await manager.start(
        make_plan(), source_path="/m/a.mkv", member_id=0,
        segment_plan=_boundaries(800), start_ms=317 * 4000,
    )
    assert session.head_segment == 317
    kills: list[int] = []
    real_killpg = os.killpg
    monkeypatch.setattr(
        session_mod.os, "killpg",
        lambda pgid, sig: (kills.append(sig), real_killpg(pgid, sig))[1],
    )
    try:
        probe, target = await asyncio.gather(
            manager.ensure_segment(session, 0),
            manager.ensure_segment(session, 317),
        )
        assert target is not None, "正经起播点的分片没拿到"
        assert probe is None  # 探测请求等待窗内拿不到，404 即可——AVPlayer 不在乎
        assert kills == [], f"头部探测劫持了转码器：{kills}"
        assert session.head_segment == 317  # 从未被拉回第 0 段
    finally:
        monkeypatch.setattr(session_mod.os, "killpg", real_killpg)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_lone_backward_seek_still_restarts_after_grace(manager, monkeypatch):
    """真正的用户回拖不能被探测宽限误伤：前方没有任何等待者时，落后请求
    熬过宽限期照常重启直奔。"""
    writes_live_then_sleeps = """
import sys, time, pathlib
pathlib.Path(sys.argv[1]).write_text("#EXTM3U\\n#EXT-X-VERSION:7\\n")
time.sleep(300)
"""
    install_fake(monkeypatch, writes_live_then_sleeps)
    monkeypatch.setattr(TranscodeSessionManager, "_SEGMENT_WAIT_S", 2.0)
    monkeypatch.setattr(TranscodeSessionManager, "_PROBE_GRACE_S", 0.3)
    session = await manager.start(
        make_plan(), source_path="/m/a.mkv", member_id=0,
        segment_plan=_boundaries(800), start_ms=317 * 4000,
    )
    try:
        # 假进程不产分片，拿不到是预期；要验的是宽限期后重启确实发生
        await manager.ensure_segment(session, 5)
        assert session.head_segment == 5, "宽限期后的真实回拖没有触发重启"
    finally:
        await manager.shutdown()


def test_sync_completed_skips_reparse_when_playlist_unchanged(manager, tmp_path):
    """签名门控：一次 seek 有好几个并发分片请求各自 50ms 轮询，live.m3u8
    没变就不该反复全量重解析（两小时片近两千行）。"""
    session = _vod_session(tmp_path, head=0, completed=set())
    playlist = session.playlist_path
    playlist.write_text("#EXTM3U\nseg00000.m4s\n", encoding="utf-8")

    manager._sync_completed(session)
    assert session.completed_segments == {0}

    # 文件没变：清掉台账再同步，跳过解析 → 台账保持空，证明确实没重读
    session.completed_segments.clear()
    manager._sync_completed(session)
    assert session.completed_segments == set()

    # 文件变了（追加一行，size 必变）：重新解析
    playlist.write_text("#EXTM3U\nseg00000.m4s\nseg00001.m4s\n", encoding="utf-8")
    manager._sync_completed(session)
    assert session.completed_segments == {0, 1}


@pytest.mark.asyncio
async def test_ensure_segment_bails_out_on_stopped_session(manager, tmp_path):
    """回归：用户退出（DELETE）删掉会话目录后，还挂着的分片请求绝不能再
    拉起 ffmpeg——往已删除的目录写只会 No such file or directory 冒成 500。"""
    session = _vod_session(tmp_path / "gone", head=0, completed=set())
    session.state = "stopped"
    assert await manager.ensure_segment(session, 5) is None
    assert session.process is None  # 没有试图重启


@pytest.mark.asyncio
async def test_cancelled_start_leaves_no_session_behind(manager, monkeypatch):
    """回归：客户端在起播途中断开（关页面/切集）时，uvicorn 会**取消**本次
    请求协程，start 里抛出的是 CancelledError（BaseException）。只清理
    Exception 的话，会话留在表里、ffmpeg 继续空转到超时回收——「关了播放
    ffmpeg 还在跑」的一条服务端来路。"""
    # playlist 延迟 0.5 秒出现：把 spawn 的等待窗口拉长，好让取消落在中途
    install_fake(monkeypatch, WRITES_PLAYLIST_THEN_SLEEPS, delay=0.5)
    task = asyncio.create_task(
        manager.start(make_plan(), source_path="/m/a.mkv", member_id=0)
    )
    assert await wait_until(
        lambda: bool(manager.active()) and manager.active()[0].process is not None
    )
    pid = manager.active()[0].process.pid
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.active() == []  # 会话没有留在表里
    assert await wait_until(lambda: not pid_alive(pid))  # 进程组整个被收掉


@pytest.mark.asyncio
async def test_stop_during_vod_restart_leaves_no_process(manager, monkeypatch):
    """回归：stop 与 VOD 分片重启不互斥的话，一次「杀旧进程 → 拉新进程」的
    重启可能在 stop 的 killpg 之后才把新 ffmpeg 拉起来——会话已出表，那个
    进程从此没人管。stop 必须拿重启锁：要么排在重启后面把新进程一并杀掉，
    要么先到并让排队的重启放弃；锁内重申 stopped 终态，后续等待者也不会
    再拉起下一轮。"""
    install_fake(monkeypatch, NEVER_WRITES_PLAYLIST)
    monkeypatch.setattr(TranscodeSessionManager, "_SEGMENT_WAIT_S", 2.0)
    session = await manager.start(
        make_plan(), source_path="/m/a.mkv", member_id=0, segment_plan=_boundaries(800)
    )
    pids = [session.process.pid]
    real_spawn = manager._spawn

    async def slow_spawn(sess, command):
        # 拉长重启临界区，确保 stop 在「旧进程已杀、新进程未起」的窗口到达
        await asyncio.sleep(0.3)
        await real_spawn(sess, command)
        if sess.process is not None:
            pids.append(sess.process.pid)

    monkeypatch.setattr(manager, "_spawn", slow_spawn)
    ensure = asyncio.create_task(manager.ensure_segment(session, 50))
    await asyncio.sleep(0.1)  # 让重启进入临界区
    try:
        assert await manager.stop(session.id) is True
        await ensure
        assert session.state == "stopped"  # 终态没有被重启写回 ready
        for pid in pids:
            assert await wait_until(lambda p=pid: not pid_alive(p)), f"进程 {pid} 泄漏"
        assert manager.get(session.id) is None
    finally:
        await manager.shutdown()
