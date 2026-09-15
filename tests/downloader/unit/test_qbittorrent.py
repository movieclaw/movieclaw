"""QBittorrentDownloader 适配器单元测试。

用假客户端替换 qbittorrent-api 的 Client，不发送真实 HTTP 请求。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import qbittorrentapi

from movieclaw_downloader.clients.qbittorrent import QBittorrentDownloader
from movieclaw_downloader.exceptions import (
    DownloaderAuthError,
    DownloaderConnectError,
    DownloaderDeleteError,
    DownloaderSubmitError,
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
    type=DownloaderType.QBITTORRENT,
    url="http://localhost:8080",
    username="admin",
    password="pass",
)


class FakeQbtClient:
    """模拟 qbittorrent-api Client：内部维护一个按 hash 索引的种子表。"""

    def __init__(self, add_response: str = "Ok."):
        self.store: dict[str, SimpleNamespace] = {}
        self.files: dict[str, list[SimpleNamespace]] = {}
        self.add_response = add_response
        self.add_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.location_calls: list[dict] = []
        self.autotmm_calls: list[dict] = []
        self.speed_mode_calls: list[bool] = []
        self.priority_calls: list[dict] = []
        self.resume_calls: list[str] = []
        # 添加成功后自动登记到 store 的 (hash, name)，模拟下载器注册行为
        self.register_on_add: tuple[str, str] | None = (TORRENT_HASH, "test.mkv")

    def torrents_info(self, torrent_hashes=None):
        if torrent_hashes is None:
            return list(self.store.values())
        found = self.store.get(torrent_hashes)
        return [found] if found else []

    def torrents_add(self, **kwargs):
        self.add_calls.append(kwargs)
        if self.add_response == "Ok." and self.register_on_add:
            info_hash, name = self.register_on_add
            self.store[info_hash] = SimpleNamespace(hash=info_hash, name=name)
        return self.add_response

    def torrents_delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        self.store.pop(kwargs["torrent_hashes"], None)

    def torrents_set_auto_management(self, **kwargs):
        self.autotmm_calls.append(kwargs)

    def torrents_set_location(self, **kwargs):
        self.location_calls.append(kwargs)

    # -- 全局限制（get_limits / set_limits） --
    def transfer_download_limit(self):
        return getattr(self, "_dl_limit", 0)

    def transfer_upload_limit(self):
        return getattr(self, "_up_limit", 0)

    def transfer_speed_limits_mode(self):
        return getattr(self, "_alt_mode", "0")

    def transfer_set_download_limit(self, limit):
        self._dl_limit = limit

    def transfer_set_upload_limit(self, limit):
        self._up_limit = limit

    def transfer_set_speed_limits_mode(self, *, intended_state):
        self.speed_mode_calls.append(intended_state)
        self._alt_mode = "1" if intended_state else "0"

    def app_preferences(self):
        return getattr(
            self,
            "_prefs",
            {
                "queueing_enabled": True,
                "max_active_downloads": 3,
                "max_active_uploads": 20,
                "max_active_torrents": 50,
            },
        )

    def app_set_preferences(self, *, prefs):
        merged = dict(self.app_preferences())
        merged.update(prefs)
        self._prefs = merged

    def torrents_files(self, *, torrent_hash):
        return self.files.get(torrent_hash, [])

    def torrents_file_priority(self, **kwargs):
        self.priority_calls.append(kwargs)

    def torrents_resume(self, **kwargs):
        self.resume_calls.append(kwargs["torrent_hashes"])

    def auth_log_in(self):
        pass

    def app_version(self):
        return "v5.0.2"


def make_downloader(fake: FakeQbtClient) -> QBittorrentDownloader:
    downloader = QBittorrentDownloader(CONFIG)
    downloader._qbt = fake
    return downloader


class TestSubmit:
    async def test_submit_torrent_bytes(self):
        fake = FakeQbtClient()
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

        call = fake.add_calls[0]
        assert call["torrent_files"] == TORRENT_BYTES
        assert call["urls"] is None
        assert call["save_path"] == "/downloads/movies"
        assert call["category"] == "movies"
        assert call["tags"] == ["pt", "auto"]
        assert call["is_paused"] is True
        assert call["use_auto_torrent_management"] is False

    async def test_submit_magnet(self):
        fake = FakeQbtClient()
        magnet = f"magnet:?xt=urn:btih:{TORRENT_HASH}"
        downloader = make_downloader(fake)

        result = await downloader.submit(DownloadRequest(magnet=magnet))

        assert result.info_hash == TORRENT_HASH
        assert fake.add_calls[0]["urls"] == magnet
        assert fake.add_calls[0]["torrent_files"] is None

    async def test_already_exists_is_idempotent(self):
        fake = FakeQbtClient()
        fake.store[TORRENT_HASH] = SimpleNamespace(hash=TORRENT_HASH, name="existing.mkv")
        downloader = make_downloader(fake)

        result = await downloader.submit(DownloadRequest(torrent_bytes=TORRENT_BYTES))

        assert result.already_exists is True
        assert result.name == "existing.mkv"
        assert fake.add_calls == []  # 未重复提交

    async def test_add_fails_raises(self):
        fake = FakeQbtClient(add_response="Fails.")
        downloader = make_downloader(fake)

        with pytest.raises(DownloaderSubmitError):
            await downloader.submit(DownloadRequest(torrent_bytes=TORRENT_BYTES))

    async def test_connection_error_translated(self):
        fake = FakeQbtClient()

        def raise_conn(**kwargs):
            raise qbittorrentapi.APIConnectionError("boom")

        fake.torrents_add = raise_conn
        downloader = make_downloader(fake)

        with pytest.raises(DownloaderConnectError):
            await downloader.submit(DownloadRequest(torrent_bytes=TORRENT_BYTES))


class TestConnection:
    async def test_test_connection(self):
        downloader = make_downloader(FakeQbtClient())
        info = await downloader.test_connection()
        assert info.type == DownloaderType.QBITTORRENT
        assert info.version == "v5.0.2"

    async def test_login_failed_translated(self):
        fake = FakeQbtClient()

        def raise_login():
            raise qbittorrentapi.LoginFailed("bad credentials")

        fake.auth_log_in = raise_login
        downloader = make_downloader(fake)

        with pytest.raises(DownloaderAuthError):
            await downloader.test_connection()


class TestSetLocation:
    async def test_set_location_disables_autotmm_and_moves(self):
        """迁移目录前必须关 auto-TMM（与 submit 同理，分类规则会抢走目录控制权）。"""
        fake = FakeQbtClient()

        await make_downloader(fake).set_location(TORRENT_HASH, "/downloads/movies")

        assert fake.autotmm_calls == [{"torrent_hashes": TORRENT_HASH, "enable": False}]
        assert fake.location_calls == [
            {"torrent_hashes": TORRENT_HASH, "location": "/downloads/movies"}
        ]

    async def test_set_location_error_translated(self):
        fake = FakeQbtClient()

        def raise_api_error(**kwargs):
            raise qbittorrentapi.APIError("permission denied")

        fake.torrents_set_location = raise_api_error

        with pytest.raises(DownloaderSubmitError):
            await make_downloader(fake).set_location(TORRENT_HASH, "/nope")


class TestLimits:
    async def test_get_limits_normalizes_unlimited_to_none(self):
        """qB 的 0=不限速归一为 None；队列上限从 app 偏好读取。"""
        fake = FakeQbtClient()
        fake._dl_limit = 0
        fake._up_limit = 2 * 1024 * 1024

        limits = await make_downloader(fake).get_limits()

        assert limits.download_limit_bytes is None
        assert limits.upload_limit_bytes == 2 * 1024 * 1024
        assert limits.queue_enabled is True
        assert limits.max_active_torrents == 50

    async def test_set_limits_roundtrip(self):
        """写入后读回：None 限速落成 0（不限），队列上限进 app 偏好。"""
        from movieclaw_downloader.models import DownloaderLimits

        fake = FakeQbtClient()
        downloader = make_downloader(fake)

        await downloader.set_limits(
            DownloaderLimits(
                download_limit_bytes=None,
                upload_limit_bytes=5 * 1024 * 1024,
                alt_speed_enabled=True,
                queue_enabled=True,
                max_active_torrents=200,
            )
        )
        limits = await downloader.get_limits()

        assert limits.download_limit_bytes is None
        assert limits.upload_limit_bytes == 5 * 1024 * 1024
        assert limits.alt_speed_enabled is True
        assert limits.max_active_torrents == 200
        # 未提供的队列字段保持原值不被覆盖
        assert limits.max_active_downloads == 3

    async def test_set_limits_skips_alt_speed_when_unchanged(self):
        """备用限速状态未变时不得调用 set_speed_limits_mode：
        qbittorrent-api 在 qB < 4.4.0 上此场景会误调不存在的端点返回 404。"""
        from movieclaw_downloader.models import DownloaderLimits

        fake = FakeQbtClient()
        fake._alt_mode = "0"

        await make_downloader(fake).set_limits(
            DownloaderLimits(alt_speed_enabled=False, max_active_downloads=5)
        )

        assert fake.speed_mode_calls == []
        assert fake.app_preferences()["max_active_downloads"] == 5


class TestDeleteTorrent:
    async def test_delete_keeps_downloaded_files_by_default(self):
        fake = FakeQbtClient()

        await make_downloader(fake).delete_torrent(TORRENT_HASH)

        assert fake.delete_calls == [{"torrent_hashes": TORRENT_HASH, "delete_files": False}]

    async def test_delete_can_remove_downloaded_files(self):
        fake = FakeQbtClient()

        await make_downloader(fake).delete_torrent(TORRENT_HASH, delete_files=True)

        assert fake.delete_calls == [{"torrent_hashes": TORRENT_HASH, "delete_files": True}]

    async def test_delete_error_translated(self):
        fake = FakeQbtClient()

        def raise_api(**kwargs):
            raise qbittorrentapi.APIError("boom")

        fake.torrents_delete = raise_api

        with pytest.raises(DownloaderDeleteError, match="删除 qBittorrent 任务失败"):
            await make_downloader(fake).delete_torrent(TORRENT_HASH)


class TestListTorrents:
    async def test_list_includes_task_center_progress_snapshot(self):
        fake = FakeQbtClient()
        fake.store[TORRENT_HASH] = SimpleNamespace(
            hash=TORRENT_HASH,
            name="Task.Center.Show",
            content_path="/downloads/Task.Center.Show",
            progress=0.42,
            size=4096,
            dlspeed=1024,
            eta=120,
            state="downloading",
        )

        rows = await make_downloader(fake).list_torrents()

        assert rows[0].content_name == "Task.Center.Show"
        assert rows[0].progress == 0.42
        assert rows[0].dlspeed_bytes == 1024
        assert rows[0].eta_seconds == 120
        assert rows[0].state == "downloading"


class TestGetTorrent:
    async def test_file_snapshot_keeps_completed_bytes_and_selection(self):
        fake = FakeQbtClient()
        fake.store[TORRENT_HASH] = SimpleNamespace(
            hash=TORRENT_HASH,
            name="Partial.Show",
            progress=0.5,
            completed=150,
            downloaded=80,
            save_path="/downloads/tv",
            size=300,
            dlspeed=100,
            eta=60,
            state="downloading",
        )
        fake.files[TORRENT_HASH] = [
            SimpleNamespace(name="Partial.Show/ep1.mkv", size=100, progress=1.0, priority=1),
            SimpleNamespace(name="Partial.Show/ep2.mkv", size=200, progress=0.25, priority=1),
            SimpleNamespace(name="Partial.Show/sample.mkv", size=20, progress=0.0, priority=0),
        ]

        status = await make_downloader(fake).get_torrent(TORRENT_HASH)

        assert status is not None
        assert status.completed_bytes == 150
        assert status.downloaded_bytes == 80
        assert [file.completed_bytes for file in status.files] == [100, 50, 0]
        assert [file.selected for file in status.files] == [True, True, False]


class TestSharedClient:
    """同一连接参数的适配器实例共享底层客户端：长连接与登录态跨实例复用。"""

    @pytest.fixture(autouse=True)
    def _isolate_registry(self, monkeypatch):
        from movieclaw_downloader.clients import qbittorrent as module

        monkeypatch.setattr(module, "_shared_clients", {})
        created: list[dict] = []

        def fake_client(**kwargs):
            created.append(kwargs)
            return SimpleNamespace(kwargs=kwargs)

        monkeypatch.setattr(module.qbittorrentapi, "Client", fake_client)
        self.created = created

    async def test_same_config_shares_one_client(self):
        first = QBittorrentDownloader(CONFIG)
        second = QBittorrentDownloader(CONFIG)
        assert first._client() is second._client()
        assert len(self.created) == 1

    async def test_close_keeps_shared_client_alive(self):
        first = QBittorrentDownloader(CONFIG)
        client = first._client()
        await first.close()
        assert first._qbt is None
        # 另一个实例 close 之后再来取，拿到的还是同一个已登录客户端，不会重建
        assert QBittorrentDownloader(CONFIG)._client() is client
        assert len(self.created) == 1

    async def test_changed_credentials_replace_stale_client(self):
        from movieclaw_downloader.clients import qbittorrent as module

        QBittorrentDownloader(CONFIG)._client()
        changed = DownloaderConfig(
            type=DownloaderType.QBITTORRENT,
            url=CONFIG.url,
            username="admin",
            password="new-pass",
        )
        fresh = QBittorrentDownloader(changed)._client()
        assert fresh.kwargs["password"] == "new-pass"
        assert len(self.created) == 2
        # 同一 URL 只保留最新凭据的那一个条目
        assert len(module._shared_clients) == 1


class TestFileSelection:
    """选择性下载原语：set_file_selection 只写"取消选中"的一侧，resume 恢复。"""

    @staticmethod
    def _downloader_with_files(count: int = 4):
        fake = FakeQbtClient()
        fake.files[TORRENT_HASH] = [
            SimpleNamespace(index=i, name=f"Pack/S01E{i + 1:02d}.mkv", priority=1)
            for i in range(count)
        ]
        return fake, make_downloader(fake)

    async def test_unwanted_files_get_priority_zero(self):
        fake, downloader = self._downloader_with_files()

        await downloader.set_file_selection(TORRENT_HASH, [1])

        # 只写取消选中的一侧，选中的文件不产生任何调用
        assert fake.priority_calls == [
            {"torrent_hash": TORRENT_HASH, "file_ids": [0, 2, 3], "priority": 0}
        ]

    async def test_explicit_index_field_wins_over_position(self):
        # qB 的 index 字段与列表位置理论上恒等，显式读字段做双保险
        fake = FakeQbtClient()
        fake.files[TORRENT_HASH] = [
            SimpleNamespace(index=5, priority=1),
            SimpleNamespace(index=9, priority=1),
        ]
        downloader = make_downloader(fake)

        await downloader.set_file_selection(TORRENT_HASH, [9])

        assert fake.priority_calls == [
            {"torrent_hash": TORRENT_HASH, "file_ids": [5], "priority": 0}
        ]

    async def test_all_selected_sends_no_call(self):
        fake, downloader = self._downloader_with_files()

        await downloader.set_file_selection(TORRENT_HASH, [0, 1, 2, 3])

        assert fake.priority_calls == []

    async def test_resume(self):
        fake = FakeQbtClient()
        downloader = make_downloader(fake)

        await downloader.resume(TORRENT_HASH)

        assert fake.resume_calls == [TORRENT_HASH]
