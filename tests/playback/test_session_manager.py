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
    """把命令装配换成假 ffmpeg。``delay`` 模拟 playlist 迟迟不出现。"""

    def fake_build(plan, *, source_path, session_dir, start_ms=0, hw_backend=None, start_number=None):
        playlist = Path(session_dir) / "index.m3u8"
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


@pytest.mark.asyncio
async def test_ensure_segment_bails_out_on_stopped_session(manager, tmp_path):
    """回归：用户退出（DELETE）删掉会话目录后，还挂着的分片请求绝不能再
    拉起 ffmpeg——往已删除的目录写只会 No such file or directory 冒成 500。"""
    session = _vod_session(tmp_path / "gone", head=0, completed=set())
    session.state = "stopped"
    assert await manager.ensure_segment(session, 5) is None
    assert session.process is None  # 没有试图重启
