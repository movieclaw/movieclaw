"""TransmissionDownloader 适配器单元测试。

用假客户端替换 transmission-rpc 的 Client，不发送真实 HTTP 请求。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from transmission_rpc.error import TransmissionAuthError, TransmissionConnectError

from movieclaw_downloader.clients.transmission import TransmissionDownloader
from movieclaw_downloader.exceptions import (
    DownloaderAuthError,
    DownloaderConnectError,
    DownloaderDeleteError,
)
from movieclaw_downloader.models import DownloaderConfig, DownloaderType, DownloadRequest
from movieclaw_downloader.torrent import compute_info_hash

TORRENT_BYTES = (
    b"d4:infod6:lengthi1024e4:name8:test.mkv12:piece lengthi16384e6:pieces20:"
    + b"\x01" * 20
    + b"ee"
)
TORRENT_HASH = compute_info_hash(TORRENT_BYTES)

CONFIG = DownloaderConfig(
    type=DownloaderType.TRANSMISSION,
    url="http://localhost:9091",
    username="admin",
    password="pass",
)


class FakeTrClient:
    """模拟 transmission-rpc Client：按 hash 索引的种子表。"""

    def __init__(self):
        self.store: dict[str, SimpleNamespace] = {}
        self.add_calls: list[tuple] = []
        self.remove_calls: list[tuple] = []
        self.move_calls: list[tuple] = []
        self.change_calls: list[tuple] = []
        self.start_calls: list[str] = []

    def get_torrent(self, torrent_id):
        if torrent_id not in self.store:
            raise KeyError("Torrent not found in result")
        return self.store[torrent_id]

    def add_torrent(self, torrent, **kwargs):
        self.add_calls.append((torrent, kwargs))
        return SimpleNamespace(name="test.mkv", hash_string=TORRENT_HASH)

    def get_torrents(self):
        return list(self.store.values())

    def remove_torrent(self, torrent_id, *, delete_data=False):
        self.remove_calls.append((torrent_id, delete_data))
        self.store.pop(torrent_id, None)

    def move_torrent_data(self, torrent_id, *, location):
        self.move_calls.append((torrent_id, location))

    def change_torrent(self, torrent_id, **kwargs):
        self.change_calls.append((torrent_id, kwargs))

    def start_torrent(self, torrent_id):
        self.start_calls.append(torrent_id)

    # -- 全局限制（get_limits / set_limits） --
    def get_session(self):
        return SimpleNamespace(
            version="4.0.5",
            fields=getattr(
                self,
                "_session_fields",
                {
                    "speed-limit-down": 8000,
                    "speed-limit-down-enabled": True,
                    "speed-limit-up": 1000,
                    "speed-limit-up-enabled": False,
                    "alt-speed-enabled": False,
                    "download-queue-enabled": True,
                    "download-queue-size": 5,
                    "seed-queue-size": 30,
                },
            ),
        )

    def set_session(self, **kwargs):
        self.session_calls = getattr(self, "session_calls", [])
        self.session_calls.append(kwargs)


def make_downloader(fake: FakeTrClient) -> TransmissionDownloader:
    downloader = TransmissionDownloader(CONFIG)
    downloader._tr = fake
    return downloader


class TestSubmit:
    async def test_submit_torrent_bytes(self):
        fake = FakeTrClient()
        downloader = make_downloader(fake)

        result = await downloader.submit(
            DownloadRequest(
                torrent_bytes=TORRENT_BYTES,
                save_path="/downloads/movies",
                category="movies",
                tags=["pt", "auto"],
                paused=True,
            )
        )

        assert result.info_hash == TORRENT_HASH
        assert result.name == "test.mkv"
        assert result.already_exists is False

        torrent, kwargs = fake.add_calls[0]
        assert torrent == TORRENT_BYTES
        assert kwargs["download_dir"] == "/downloads/movies"
        # category 映射为第一个 label，tags 追加其后
        assert kwargs["labels"] == ["movies", "pt", "auto"]
        assert kwargs["paused"] is True

    async def test_submit_magnet_without_labels(self):
        fake = FakeTrClient()
        magnet = f"magnet:?xt=urn:btih:{TORRENT_HASH}"
        downloader = make_downloader(fake)

        result = await downloader.submit(DownloadRequest(magnet=magnet))

        assert result.info_hash == TORRENT_HASH
        torrent, kwargs = fake.add_calls[0]
        assert torrent == magnet
        assert kwargs["labels"] is None  # 无 category/tags 时不触发 labels 的版本要求

    async def test_v2_magnet_falls_back_to_client_hash(self):
        """本地解析不出 hash 的磁力链接，退用 Transmission 返回的 hash。"""
        fake = FakeTrClient()
        downloader = make_downloader(fake)

        result = await downloader.submit(
            DownloadRequest(magnet="magnet:?xt=urn:btmh:1220" + "a" * 64)
        )

        assert result.info_hash == TORRENT_HASH  # 来自 add_torrent 的返回值

    async def test_already_exists_is_idempotent(self):
        fake = FakeTrClient()
        fake.store[TORRENT_HASH] = SimpleNamespace(name="existing.mkv", hash_string=TORRENT_HASH)
        downloader = make_downloader(fake)

        result = await downloader.submit(DownloadRequest(torrent_bytes=TORRENT_BYTES))

        assert result.already_exists is True
        assert result.name == "existing.mkv"
        assert fake.add_calls == []  # 未重复提交

    async def test_auth_error_translated(self):
        fake = FakeTrClient()

        def raise_auth(torrent, **kwargs):
            raise TransmissionAuthError("401")

        fake.add_torrent = raise_auth
        downloader = make_downloader(fake)

        with pytest.raises(DownloaderAuthError):
            await downloader.submit(DownloadRequest(torrent_bytes=TORRENT_BYTES))

    async def test_connect_error_translated(self):
        fake = FakeTrClient()

        def raise_conn(torrent, **kwargs):
            raise TransmissionConnectError("refused")

        fake.add_torrent = raise_conn
        downloader = make_downloader(fake)

        with pytest.raises(DownloaderConnectError):
            await downloader.submit(DownloadRequest(torrent_bytes=TORRENT_BYTES))


class TestConnection:
    async def test_test_connection(self):
        downloader = make_downloader(FakeTrClient())
        info = await downloader.test_connection()
        assert info.type == DownloaderType.TRANSMISSION
        assert info.version == "4.0.5"

    def test_invalid_url_rejected(self):
        downloader = TransmissionDownloader(
            DownloaderConfig(type=DownloaderType.TRANSMISSION, url="not-a-url")
        )
        with pytest.raises(DownloaderConnectError):
            downloader._client()


class TestLimits:
    async def test_get_limits_converts_kbps_and_flags(self):
        """kB/s（1000 进制）换算为字节；开关关 = 不限速（None）；
        Transmission 无「最大活动种子数」概念，恒为 None。"""
        fake = FakeTrClient()

        limits = await make_downloader(fake).get_limits()

        assert limits.download_limit_bytes == 8000 * 1000
        assert limits.upload_limit_bytes is None  # 开关关着，数值再大也是不限
        assert limits.queue_enabled is True
        assert limits.max_active_downloads == 5
        assert limits.max_active_uploads == 30
        assert limits.max_active_torrents is None

    async def test_set_limits_writes_flags_and_kbps(self):
        """写入：None=关开关；有值=开开关并换算 kB/s；队列总开关同时作用于
        下载与做种队列；max_active_torrents 被静默忽略。"""
        from movieclaw_downloader.models import DownloaderLimits

        fake = FakeTrClient()

        await make_downloader(fake).set_limits(
            DownloaderLimits(
                download_limit_bytes=2_000_000,
                upload_limit_bytes=None,
                queue_enabled=False,
                max_active_uploads=99,
                max_active_torrents=123,  # Tr 不支持，应被忽略
            )
        )

        kwargs = fake.session_calls[0]
        assert kwargs["speed_limit_down_enabled"] is True
        assert kwargs["speed_limit_down"] == 2000
        assert kwargs["speed_limit_up_enabled"] is False
        assert kwargs["download_queue_enabled"] is False
        assert kwargs["seed_queue_enabled"] is False
        assert kwargs["seed_queue_size"] == 99
        assert "max_active_torrents" not in kwargs


class TestSetLocation:
    async def test_set_location_moves_data(self):
        fake = FakeTrClient()

        await make_downloader(fake).set_location(TORRENT_HASH, "/downloads/movies")

        assert fake.move_calls == [(TORRENT_HASH, "/downloads/movies")]


class TestDeleteTorrent:
    async def test_delete_keeps_downloaded_files_by_default(self):
        fake = FakeTrClient()

        await make_downloader(fake).delete_torrent(TORRENT_HASH)

        assert fake.remove_calls == [(TORRENT_HASH, False)]

    async def test_delete_can_remove_downloaded_files(self):
        fake = FakeTrClient()

        await make_downloader(fake).delete_torrent(TORRENT_HASH, delete_files=True)

        assert fake.remove_calls == [(TORRENT_HASH, True)]

    async def test_delete_error_translated(self):
        fake = FakeTrClient()

        def raise_error(torrent_id, *, delete_data=False):
            from transmission_rpc.error import TransmissionError

            raise TransmissionError("boom")

        fake.remove_torrent = raise_error

        with pytest.raises(DownloaderDeleteError, match="删除 Transmission 任务失败"):
            await make_downloader(fake).delete_torrent(TORRENT_HASH)


class TestListTorrents:
    async def test_list_includes_task_center_progress_snapshot(self):
        fake = FakeTrClient()
        fake.store[TORRENT_HASH] = SimpleNamespace(
            hash_string=TORRENT_HASH,
            name="Task.Center.Movie",
            percent_done=0.5,
            fields={"eta": 60, "sizeWhenDone": 8192, "rateDownload": 2048, "status": 4},
        )

        rows = await make_downloader(fake).list_torrents()

        assert rows[0].progress == 0.5
        assert rows[0].size_bytes == 8192
        assert rows[0].dlspeed_bytes == 2048
        assert rows[0].eta_seconds == 60
        assert rows[0].state == "downloading"


class TestGetTorrent:
    async def test_file_snapshot_keeps_completed_bytes_and_selection(self):
        fake = FakeTrClient()
        fake.store[TORRENT_HASH] = SimpleNamespace(
            hash_string=TORRENT_HASH,
            name="Partial.Show",
            percent_done=0.5,
            download_dir="/downloads/tv",
            fields={
                "eta": 60,
                "sizeWhenDone": 300,
                "rateDownload": 100,
                "status": 4,
                "haveValid": 0,
                "haveUnchecked": 150,
                "downloadedEver": 80,
            },
            get_files=lambda: [
                SimpleNamespace(
                    name="Partial.Show/ep1.mkv",
                    size=100,
                    completed=100,
                    selected=True,
                ),
                SimpleNamespace(
                    name="Partial.Show/ep2.mkv",
                    size=200,
                    completed=50,
                    selected=True,
                ),
                SimpleNamespace(
                    name="Partial.Show/sample.mkv",
                    size=20,
                    completed=0,
                    selected=False,
                ),
            ],
        )

        status = await make_downloader(fake).get_torrent(TORRENT_HASH)

        assert status is not None
        assert status.completed_bytes == 150
        assert status.downloaded_bytes == 80
        assert [file.completed_bytes for file in status.files] == [100, 50, 0]
        assert [file.selected for file in status.files] == [True, True, False]


class TestFileSelection:
    """选择性下载原语：set_file_selection 走 files-unwanted，resume 走 start。"""

    async def test_unwanted_files_marked(self):
        fake = FakeTrClient()
        fake.store[TORRENT_HASH] = SimpleNamespace(files=lambda: [None] * 4)
        downloader = make_downloader(fake)

        await downloader.set_file_selection(TORRENT_HASH, [1])

        assert fake.change_calls == [(TORRENT_HASH, {"files_unwanted": [0, 2, 3]})]

    async def test_all_selected_sends_no_call(self):
        fake = FakeTrClient()
        fake.store[TORRENT_HASH] = SimpleNamespace(files=lambda: [None] * 2)
        downloader = make_downloader(fake)

        await downloader.set_file_selection(TORRENT_HASH, [0, 1])

        assert fake.change_calls == []

    async def test_resume_starts_torrent(self):
        fake = FakeTrClient()
        downloader = make_downloader(fake)

        await downloader.resume(TORRENT_HASH)

        assert fake.start_calls == [TORRENT_HASH]
