"""Transmission 适配器。

基于 transmission-rpc 包（同步 requests 实现）访问 Transmission 的 RPC 接口，
所有阻塞调用通过 asyncio.to_thread 放入线程池执行。

与 qBittorrent 的能力差异：Transmission 没有"分类"概念，DownloadRequest
的 category 映射为第一个 label，tags 依次追加其后（labels 需要
Transmission 4.0+，旧版守护进程会忽略并告警）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

from transmission_rpc import Client
from transmission_rpc.error import (
    TransmissionAuthError,
    TransmissionConnectError,
    TransmissionError,
)

from movieclaw_downloader.base import BaseDownloader
from movieclaw_downloader.exceptions import (
    DownloaderAuthError,
    DownloaderConnectError,
    DownloaderDeleteError,
    DownloaderSubmitError,
)
from movieclaw_downloader.models import (
    DownloaderInfo,
    DownloaderLimits,
    DownloaderType,
    DownloadRequest,
    SubmitResult,
    TorrentBrief,
    TorrentFile,
    TorrentStatus,
)

logger = logging.getLogger("movieclaw_downloader.transmission")


def _normalize_state(torrent, completed: bool) -> str:  # noqa: ANN001 -- transmission_rpc.Torrent
    """把 Transmission 的任务状态收敛到 TorrentStatus.state 的统一词表。

    原始 status 词表（transmission-rpc）：stopped / check pending / checking /
    download pending / downloading / seed pending / seeding。
    """
    if completed:
        return "completed"
    if int(torrent.fields.get("error", 0)) != 0:
        return "error"
    status = str(torrent.fields.get("status", ""))
    # fields 里 status 是数字枚举：0=stopped 3=download pending 4=downloading
    if status == "4":
        # 有速度才算真在下，零速对齐 qBittorrent 的 stalled 语义
        return "downloading" if int(torrent.fields.get("rateDownload", 0)) > 0 else "stalled"
    if status == "3":
        return "queued"
    if status == "0":
        return "paused"
    if status in ("1", "2"):
        return "checking"
    return "unknown"


@contextmanager
def _translate_errors(url: str, *, operation: str = "submit") -> Iterator[None]:
    """把 transmission-rpc 的异常翻译成本模块的统一异常。"""
    try:
        yield
    except TransmissionAuthError as exc:
        raise DownloaderAuthError(
            "Transmission 认证失败：用户名或密码错误", details={"url": url}
        ) from exc
    except TransmissionConnectError as exc:
        raise DownloaderConnectError(
            "无法连接到 Transmission，请检查 RPC 地址和端口",
            details={"url": url, "error": str(exc)},
        ) from exc
    except TransmissionError as exc:
        if operation == "delete":
            raise DownloaderDeleteError(
                "删除 Transmission 任务失败，请检查下载器状态",
                details={"url": url, "error": str(exc)},
            ) from exc
        raise DownloaderSubmitError(
            "Transmission 拒绝了该请求（种子无效或下载器返回错误）",
            details={"url": url, "error": str(exc)},
        ) from exc


class TransmissionDownloader(BaseDownloader):
    """Transmission 下载器适配器。"""

    _tr: Client | None = None

    def _client(self) -> Client:
        """惰性创建底层客户端。

        注意 transmission_rpc.Client 构造时就会发起 RPC 请求获取会话，
        因此本方法只能在线程池内调用，不能出现在事件循环里。
        """
        if self._tr is None:
            parts = urlsplit(self.config.url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise DownloaderConnectError(
                    "Transmission 地址格式错误，应形如 http://主机:9091",
                    details={"url": self.config.url},
                )
            with _translate_errors(self.config.url):
                self._tr = Client(
                    protocol=parts.scheme,
                    host=parts.hostname,
                    port=parts.port or 9091,
                    # 未写路径时补全官方默认 RPC 路径
                    path=parts.path if parts.path not in ("", "/") else "/transmission/rpc",
                    username=self.config.username,
                    password=self.config.password,
                    timeout=self.config.timeout,
                )
        return self._tr

    async def submit(self, request: DownloadRequest) -> SubmitResult:
        return await asyncio.to_thread(self._submit_sync, request)

    def _submit_sync(self, request: DownloadRequest) -> SubmitResult:
        client = self._client()
        info_hash = self._resolve_info_hash(request)

        # category + tags 统一压平成 Transmission 的 labels
        labels = [label for label in [request.category, *request.tags] if label]

        with _translate_errors(self.config.url):
            # 提交前按 infohash 去重：已存在直接幂等返回，不重复添加
            if info_hash:
                try:
                    existing = client.get_torrent(info_hash)
                except KeyError:
                    existing = None
                if existing is not None:
                    logger.info("种子已存在于 Transmission，跳过提交: %s", info_hash)
                    return SubmitResult(
                        info_hash=info_hash,
                        name=existing.name,
                        already_exists=True,
                    )

            torrent = client.add_torrent(
                request.torrent_bytes if request.torrent_bytes is not None else request.magnet,
                download_dir=request.save_path,
                paused=request.paused,
                labels=labels or None,
            )

        # 磁力链接解析不出 hash 时，退而使用 Transmission 返回的值
        result_hash = info_hash or torrent.hash_string
        logger.info("已提交种子到 Transmission: hash=%s name=%s", result_hash, torrent.name)
        return SubmitResult(info_hash=result_hash, name=torrent.name)

    async def get_torrent(
        self, info_hash: str, *, include_files: bool = True
    ) -> TorrentStatus | None:
        return await asyncio.to_thread(self._get_torrent_sync, info_hash, include_files)

    def _get_torrent_sync(self, info_hash: str, include_files: bool = True) -> TorrentStatus | None:
        client = self._client()
        with _translate_errors(self.config.url):
            try:
                torrent = client.get_torrent(info_hash)
            except KeyError:
                return None
        completed = float(torrent.percent_done) >= 1.0
        # 直接读原始字段：transmission-rpc 的 .eta 属性在无法估算时抛异常，
        # fields 里的原始值 -1（未知）/-2（不适用）直接判掉更稳
        eta = int(torrent.fields.get("eta", -1))
        size_bytes = int(torrent.fields.get("sizeWhenDone", 0))
        have_valid = torrent.fields.get("haveValid")
        have_unchecked = torrent.fields.get("haveUnchecked")
        if have_valid is None and have_unchecked is None:
            completed_bytes = int(size_bytes * float(torrent.percent_done))
        else:
            completed_bytes = int(have_valid or 0) + int(have_unchecked or 0)
        downloaded = torrent.fields.get("downloadedEver")
        return TorrentStatus(
            info_hash=info_hash,
            name=torrent.name,
            progress=float(torrent.percent_done),
            completed_bytes=max(0, min(size_bytes, completed_bytes)) if size_bytes else None,
            downloaded_bytes=max(0, int(downloaded)) if downloaded is not None else None,
            completed=completed,
            save_path=torrent.download_dir,
            files=(
                [
                    # file.name 是种子内相对路径（含顶层目录）
                    TorrentFile(
                        path=file.name,
                        size_bytes=int(file.size),
                        completed_bytes=max(
                            0,
                            min(int(file.size), int(file.completed)),
                        ),
                        selected=bool(file.selected),
                    )
                    for file in torrent.get_files()
                ]
                # 进度快照类调用不需要文件清单，跳过逐文件的构造
                if include_files
                else []
            ),
            size_bytes=size_bytes or None,
            dlspeed_bytes=int(torrent.fields.get("rateDownload", 0)),
            eta_seconds=eta if eta > 0 else None,
            state=_normalize_state(torrent, completed=completed),
        )

    async def list_torrents(self) -> list[TorrentBrief]:
        return await asyncio.to_thread(self._list_torrents_sync)

    @staticmethod
    def _swarm_counts(torrent) -> tuple[int | None, int | None]:  # noqa: ANN001
        """从 trackerStats 取蜂群规模（全网 seeder/leecher 数，跨 tracker 取最大）。

        -1 表示该 tracker 未汇报，跳过；一个都没有 → None（未知，不可当 0）。
        """
        stats = torrent.fields.get("trackerStats") or []
        seeders = [int(s.get("seederCount", -1)) for s in stats]
        leechers = [int(s.get("leecherCount", -1)) for s in stats]
        valid_seeders = [v for v in seeders if v >= 0]
        valid_leechers = [v for v in leechers if v >= 0]
        return (
            max(valid_seeders) if valid_seeders else None,
            max(valid_leechers) if valid_leechers else None,
        )

    def _list_torrents_sync(self) -> list[TorrentBrief]:
        client = self._client()
        with _translate_errors(self.config.url):
            torrents = client.get_torrents()
        # Transmission 的任务名即落盘根目录/文件名，无独立的 content_path
        briefs: list[TorrentBrief] = []
        for torrent in torrents:
            progress = float(torrent.percent_done)
            completed = progress >= 1.0
            eta = int(torrent.fields.get("eta", -1))
            swarm_seeders, swarm_leechers = self._swarm_counts(torrent)
            briefs.append(
                TorrentBrief(
                    name=torrent.name,
                    content_name=torrent.name,
                    completed=completed,
                    info_hash=str(torrent.hash_string).lower(),
                    progress=progress,
                    completed_bytes=max(
                        0,
                        min(
                            int(torrent.fields.get("sizeWhenDone", 0)),
                            int(torrent.fields.get("haveValid", 0) or 0)
                            + int(torrent.fields.get("haveUnchecked", 0) or 0)
                            or int(int(torrent.fields.get("sizeWhenDone", 0)) * progress),
                        ),
                    ),
                    size_bytes=int(torrent.fields.get("sizeWhenDone", 0)) or None,
                    dlspeed_bytes=int(torrent.fields.get("rateDownload", 0)),
                    upspeed_bytes=int(torrent.fields.get("rateUpload", 0)),
                    # uploadedEver/downloadedEver 是本任务的累计上/下行字节；
                    # uploadRatio 的 -1（Transmission 表示"未定义"）归一为 None
                    uploaded_bytes=(
                        int(torrent.fields["uploadedEver"])
                        if torrent.fields.get("uploadedEver") is not None
                        else None
                    ),
                    downloaded_bytes=(
                        int(torrent.fields["downloadedEver"])
                        if torrent.fields.get("downloadedEver") is not None
                        else None
                    ),
                    ratio=(
                        float(torrent.fields["uploadRatio"])
                        if float(torrent.fields.get("uploadRatio", -1)) >= 0
                        else None
                    ),
                    swarm_seeders=swarm_seeders,
                    swarm_leechers=swarm_leechers,
                    eta_seconds=eta if eta > 0 else None,
                    state=_normalize_state(torrent, completed=completed),
                )
            )
        return briefs

    async def delete_torrent(self, info_hash: str, *, delete_files: bool = False) -> None:
        await asyncio.to_thread(self._delete_torrent_sync, info_hash, delete_files)

    def _delete_torrent_sync(self, info_hash: str, delete_files: bool) -> None:
        """按用户选择删除任务或连同数据文件。"""
        client = self._client()
        with _translate_errors(self.config.url, operation="delete"):
            client.remove_torrent(info_hash.lower(), delete_data=delete_files)
        logger.info(
            "已从 Transmission 删除任务%s: hash=%s",
            "并删除数据文件" if delete_files else "并保留数据文件",
            info_hash,
        )

    async def transfer_speeds(self) -> tuple[int, int]:
        return await asyncio.to_thread(self._transfer_speeds_sync)

    def _transfer_speeds_sync(self) -> tuple[int, int]:
        """全局瞬时速度来自 session-stats（uploadSpeed/downloadSpeed，字节/秒）。"""
        client = self._client()
        with _translate_errors(self.config.url):
            stats = client.session_stats()
        f = stats.fields
        return int(f.get("uploadSpeed", 0) or 0), int(f.get("downloadSpeed", 0) or 0)

    async def set_download_limits(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        await asyncio.to_thread(self._set_download_limits_sync, info_hashes, limit_bytes)

    def _set_download_limits_sync(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        """Transmission 的按种限速：downloadLimit 是 kB/s + 独立开关，
        None=关开关（取消限速）。不存在的 hash 由守护进程静默忽略。"""
        if not info_hashes:
            return
        ids = [h.lower() for h in info_hashes]
        with _translate_errors(self.config.url):
            if limit_bytes is None or limit_bytes <= 0:
                self._client().change_torrent(ids, download_limited=False)
            else:
                self._client().change_torrent(
                    ids,
                    download_limited=True,
                    download_limit=max(1, round(limit_bytes / 1000)),
                )

    async def set_upload_limits(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        await asyncio.to_thread(self._set_upload_limits_sync, info_hashes, limit_bytes)

    def _set_upload_limits_sync(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        """Transmission 的按种上传限速：uploadLimit 是 kB/s + 独立开关，
        None=关开关（取消限速）。不存在的 hash 由守护进程静默忽略。"""
        if not info_hashes:
            return
        ids = [h.lower() for h in info_hashes]
        with _translate_errors(self.config.url):
            if limit_bytes is None or limit_bytes <= 0:
                self._client().change_torrent(ids, upload_limited=False)
            else:
                self._client().change_torrent(
                    ids,
                    upload_limited=True,
                    upload_limit=max(1, round(limit_bytes / 1000)),
                )

    async def get_limits(self) -> DownloaderLimits:
        return await asyncio.to_thread(self._get_limits_sync)

    def _get_limits_sync(self) -> DownloaderLimits:
        """Transmission 限速原生是 kB/s（1 kB=1000 字节）+ 独立开关：开关关 =
        不限速（归一为 None）。队列读下载队列开关为准；无「活动总数」概念。"""
        client = self._client()
        with _translate_errors(self.config.url):
            session = client.get_session()
        f = session.fields
        return DownloaderLimits(
            download_limit_bytes=(
                int(f.get("speed-limit-down", 0)) * 1000
                if f.get("speed-limit-down-enabled")
                else None
            ),
            upload_limit_bytes=(
                int(f.get("speed-limit-up", 0)) * 1000
                if f.get("speed-limit-up-enabled")
                else None
            ),
            alt_speed_enabled=bool(f.get("alt-speed-enabled")),
            queue_enabled=bool(f.get("download-queue-enabled")),
            max_active_downloads=f.get("download-queue-size"),
            max_active_uploads=f.get("seed-queue-size"),
            max_active_torrents=None,  # Transmission 没有下载+做种总数上限
        )

    async def set_limits(self, limits: DownloaderLimits) -> None:
        await asyncio.to_thread(self._set_limits_sync, limits)

    def _set_limits_sync(self, limits: DownloaderLimits) -> None:
        client = self._client()
        kwargs: dict = {}
        # 限速：None=取消（关开关），有值=开开关并换算成 kB/s（最小 1）
        if limits.download_limit_bytes is None:
            kwargs["speed_limit_down_enabled"] = False
        else:
            kwargs["speed_limit_down_enabled"] = True
            kwargs["speed_limit_down"] = max(1, round(limits.download_limit_bytes / 1000))
        if limits.upload_limit_bytes is None:
            kwargs["speed_limit_up_enabled"] = False
        else:
            kwargs["speed_limit_up_enabled"] = True
            kwargs["speed_limit_up"] = max(1, round(limits.upload_limit_bytes / 1000))
        if limits.alt_speed_enabled is not None:
            kwargs["alt_speed_enabled"] = limits.alt_speed_enabled
        # 队列：总开关同时作用于下载与做种两个队列（统一语义）
        if limits.queue_enabled is not None:
            kwargs["download_queue_enabled"] = limits.queue_enabled
            kwargs["seed_queue_enabled"] = limits.queue_enabled
        if limits.max_active_downloads is not None:
            kwargs["download_queue_size"] = limits.max_active_downloads
        if limits.max_active_uploads is not None:
            kwargs["seed_queue_size"] = limits.max_active_uploads
        # max_active_torrents：Transmission 不支持，静默忽略
        with _translate_errors(self.config.url):
            client.set_session(**kwargs)
        logger.info("已更新 Transmission 全局限制: %s", limits.model_dump(exclude_none=True))

    async def set_location(self, info_hash: str, save_path: str) -> None:
        await asyncio.to_thread(self._set_location_sync, info_hash, save_path)

    def _set_location_sync(self, info_hash: str, save_path: str) -> None:
        """改保存目录并由 Transmission 自行搬移数据（move_torrent_data）。"""
        client = self._client()
        with _translate_errors(self.config.url, operation="submit"):
            client.move_torrent_data(info_hash.lower(), location=save_path)
        logger.info("已移动 Transmission 任务目录: hash=%s -> %s", info_hash, save_path)

    async def test_connection(self) -> DownloaderInfo:
        return await asyncio.to_thread(self._test_connection_sync)

    def _test_connection_sync(self) -> DownloaderInfo:
        client = self._client()
        with _translate_errors(self.config.url):
            session = client.get_session()
        return DownloaderInfo(type=DownloaderType.TRANSMISSION, version=session.version)

    async def close(self) -> None:
        # transmission-rpc 无显式登出/断开接口，丢弃引用即可
        self._tr = None
