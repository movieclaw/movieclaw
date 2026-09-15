"""根路径落在什么挂载上（services/library/mounts.py）。

判定只看 /proc/mounts 的最长前缀；读不到就是 unknown，不猜。
"""

from __future__ import annotations

from types import SimpleNamespace

from movieclaw_api.services.library import mounts
from movieclaw_api.services.library.watch import watchable_roots

PROC_MOUNTS = """\
/dev/mapper/cachedev_2 /volume1 btrfs rw,relatime 0 0
192.168.1.80:/volume1/media /volume1/remote/media nfs rw,vers=3 0 0
//nas/share /mnt/smb cifs rw 0 0
rclone: /mnt/gd fuse.rclone rw 0 0
tmpfs /run tmpfs rw 0 0
/dev/sda1 /mnt/with\\040space ext4 rw 0 0
overlay / overlay rw 0 0
"""


def test_longest_prefix_wins_and_fstype_classifies():
    table = mounts.parse_mounts(PROC_MOUNTS)
    assert mounts.mount_kind_of("/volume1/remote/media/电影", table) == "network"  # nfs
    # btrfs（不是 /volume1/remote/… 那棵 nfs）
    assert mounts.mount_kind_of("/volume1/download", table) == "local"
    assert mounts.mount_kind_of("/mnt/smb/av", table) == "network"  # cifs
    assert mounts.mount_kind_of("/mnt/gd/movies", table) == "network"  # fuse.rclone
    assert mounts.mount_kind_of("/mnt/with space/x", table) == "local"  # 八进制转义的挂载点
    assert mounts.mount_kind_of("/somewhere/else", table) == "local"  # 根挂载兜底
    # /volume1/remote 本身（不是 media 子树）落在 btrfs 上
    assert mounts.mount_kind_of("/volume1/remote/av2", table) == "local"


def test_unreadable_mount_table_is_unknown_not_a_guess():
    assert mounts.mount_kind_of("/anything", []) == "unknown"


def test_watchable_roots_skips_network_roots_regardless_of_switch(monkeypatch):
    """开关说的是"我想要"，挂载说的是"做不到"：网络根即使开着实时监控也不建。"""
    table = mounts.parse_mounts(PROC_MOUNTS)
    monkeypatch.setattr(mounts, "system_mounts", lambda: table)
    libs = [
        SimpleNamespace(
            id=1, name="电影", realtime_watch=True, root_paths=["/volume1/remote/media/电影"]
        ),
        SimpleNamespace(id=2, name="本地", realtime_watch=True, root_paths=["/volume1/local"]),
        SimpleNamespace(id=3, name="关了", realtime_watch=False, root_paths=["/volume1/other"]),
    ]
    roots, skipped = watchable_roots(libs)
    assert roots == [(2, "/volume1/local")]
    assert skipped == [("电影", "/volume1/remote/media/电影")]
    assert mounts.library_on_network_mount(["/volume1/local", "/mnt/smb/x"]) is True
    assert mounts.library_on_network_mount(["/volume1/local"]) is False
