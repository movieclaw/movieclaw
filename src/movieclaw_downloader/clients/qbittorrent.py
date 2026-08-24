"""qBittorrent 适配器。

基于官方推荐的 qbittorrent-api 包（同步 requests 实现）访问 WebUI API，
所有阻塞调用通过 asyncio.to_thread 放入线程池执行。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import qbittorrentapi

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

logger = logging.getLogger("movieclaw_downloader.qbittorrent")

# 按连接参数共享的底层客户端注册表。
#
# 上层（任务中心轮询、刷流引擎、进度巡检、投递……）习惯"每次操作 create_downloader
# → 用完 close"，如果每个适配器实例都各自新建 qbittorrentapi.Client，就意味着每次操作
# 都要重新做 DNS 解析、TCP/TLS 握手和 auth/login。任务中心每 5 秒轮询一次，NAS 现场
# 实测 DNS 偶发丢包时一次解析要等 5 秒，接口耗时直接从 0.4s 变成 5.4s/10.4s。
#
# 这里把 Client（内部是带 keep-alive 连接池和已登录 SID 的 requests.Session）按
# (url, 用户名, 密码, 超时) 复用：同一台下载器的所有调用共用一条长连接，DNS 与登录只
# 在首次或连接失效重建时发生。qbittorrentapi 自带 403 自动重登与连接错误重试，旧连接
# 被服务端/反代掐断时会透明重建，上层无感。
#
# 线程安全：适配器的阻塞调用都在 asyncio.to_thread 线程池里跑，requests.Session 的
# 连接池本身支持多线程并发；qbittorrentapi 的登录状态标记在并发下最坏只是多登一次。
_shared_clients: dict[tuple[str, str | None, str | None, float | None], qbittorrentapi.Client] = {}
_shared_clients_lock = threading.Lock()


def _shared_client(
    url: str, username: str | None, password: str | None, timeout: float | None
) -> qbittorrentapi.Client:
    """取（或创建）对应连接参数的共享客户端。

    同一 URL 换了凭据或超时（用户在设置页改了配置）时，旧条目一并丢弃：既避免注册表
    随改密无限增长，也避免旧凭据的客户端继续被人拿到。
    """
    key = (url, username, password, timeout)
    with _shared_clients_lock:
        client = _shared_clients.get(key)
        if client is None:
            for stale in [k for k in _shared_clients if k[0] == url]:
                _shared_clients.pop(stale, None)
            client = qbittorrentapi.Client(
                host=url,
                username=username,
                password=password,
                REQUESTS_ARGS={"timeout": timeout},
            )
            _shared_clients[key] = client
    return client


# 提交后回查任务名称的重试节奏：qBittorrent 注册种子是异步的，
# add 返回成功到 torrents_info 可查之间有极短的窗口期。
_LOOKUP_ATTEMPTS = 5
_LOOKUP_INTERVAL = 0.3


def _normalize_state(state: str, *, completed: bool) -> str:
    """把 qBittorrent 的任务状态收敛到 TorrentStatus.state 的统一词表。"""
    if completed:
        return "completed"
    if state in ("downloading", "forcedDL", "metaDL", "allocating"):
        return "downloading"
    if state == "stalledDL":
        return "stalled"
    if state == "queuedDL":
        return "queued"
    if state == "checkingDL":
        return "checking"
    if state in ("pausedDL", "stoppedDL"):
        return "paused"
    if state in ("error", "missingFiles"):
        return "error"
    return "unknown"


@contextmanager
def _translate_errors(url: str) -> Iterator[None]:
    """把 qbittorrent-api 的异常翻译成本模块的统一异常。"""
    try:
        yield
    except qbittorrentapi.LoginFailed as exc:
        raise DownloaderAuthError(
            "qBittorrent 登录失败：用户名或密码错误", details={"url": url}
        ) from exc
    except (qbittorrentapi.Unauthorized401Error, qbittorrentapi.Forbidden403Error) as exc:
        raise DownloaderAuthError(
            "qBittorrent 拒绝访问：请检查凭证，多次失败可能触发了 IP 封禁",
            details={"url": url},
        ) from exc
    except qbittorrentapi.UnsupportedMediaType415Error as exc:
        raise DownloaderSubmitError(
            "qBittorrent 无法识别提交的种子文件（文件损坏或不是 .torrent 格式）",
            details={"url": url},
        ) from exc
    except qbittorrentapi.APIConnectionError as exc:
        raise DownloaderConnectError(
            "无法连接到 qBittorrent，请检查 WebUI 地址和端口",
            details={"url": url, "error": str(exc)},
        ) from exc


class QBittorrentDownloader(BaseDownloader):
    """qBittorrent 下载器适配器。"""

    _qbt: qbittorrentapi.Client | None = None

    def _client(self) -> qbittorrentapi.Client:
        """惰性取得底层客户端：同一下载器的所有适配器实例共享一条已登录长连接。

        构造无 IO；登录由库在首次请求时自动完成。测试可直接注入 ``_qbt`` 假客户端。
        """
        if self._qbt is None:
            self._qbt = _shared_client(
                self.config.url,
                self.config.username,
                self.config.password,
                self.config.timeout,
            )
        return self._qbt

    async def submit(self, request: DownloadRequest) -> SubmitResult:
        return await asyncio.to_thread(self._submit_sync, request)

    def _submit_sync(self, request: DownloadRequest) -> SubmitResult:
        client = self._client()
        info_hash = self._resolve_info_hash(request)

        with _translate_errors(self.config.url):
            # 提交前按 infohash 去重：已存在直接幂等返回，不重复添加
            if info_hash:
                existing = client.torrents_info(torrent_hashes=info_hash)
                if existing:
                    logger.info("种子已存在于 qBittorrent，跳过提交: %s", info_hash)
                    return SubmitResult(
                        info_hash=info_hash,
                        name=existing[0].name,
                        already_exists=True,
                    )

            result = client.torrents_add(
                torrent_files=request.torrent_bytes,
                urls=request.magnet,
                save_path=request.save_path,
                category=request.category,
                tags=request.tags or None,
                is_paused=request.paused,
                # 显式关闭自动管理：保证 save_path 生效，不被分类目录规则覆盖
                use_auto_torrent_management=False,
            )
            # 旧版 API 返回 "Ok."/"Fails." 字符串；v2.14+ 返回 metadata 字典
            if isinstance(result, str) and not result.startswith("Ok"):
                raise DownloaderSubmitError(
                    "qBittorrent 拒绝了该种子（文件无效或触发下载器自身的限制）",
                    details={"url": self.config.url, "response": result},
                )

            name = self._lookup_name(client, info_hash)

        logger.info("已提交种子到 qBittorrent: hash=%s name=%s", info_hash, name)
        return SubmitResult(info_hash=info_hash, name=name)

    @staticmethod
    def _lookup_name(client: qbittorrentapi.Client, info_hash: str | None) -> str:
        """提交后回查任务名称；短暂等待注册完成，查不到不视为失败。"""
        if not info_hash:
            return ""
        for attempt in range(_LOOKUP_ATTEMPTS):
            infos = client.torrents_info(torrent_hashes=info_hash)
            if infos:
                return infos[0].name
            if attempt < _LOOKUP_ATTEMPTS - 1:
                time.sleep(_LOOKUP_INTERVAL)
        logger.warning("提交成功但暂未在 qBittorrent 中查到种子: %s", info_hash)
        return ""

    async def get_torrent(
        self, info_hash: str, *, include_files: bool = True
    ) -> TorrentStatus | None:
        return await asyncio.to_thread(self._get_torrent_sync, info_hash, include_files)

    def _get_torrent_sync(self, info_hash: str, include_files: bool = True) -> TorrentStatus | None:
        client = self._client()
        with _translate_errors(self.config.url):
            infos = client.torrents_info(torrent_hashes=info_hash)
            if not infos:
                return None
            torrent = infos[0]
            # 文件清单是独立的 API 请求：进度快照类调用不需要它，省一次往返
            files = client.torrents_files(torrent_hash=info_hash) if include_files else []
        completed = float(torrent.progress) >= 1.0
        # qBittorrent 用 8640000（100 天）表示"无法估算"
        eta = int(getattr(torrent, "eta", 0) or 0)
        size_bytes = int(getattr(torrent, "size", 0) or 0)
        completed_bytes = int(
            getattr(torrent, "completed", 0) or int(size_bytes * float(torrent.progress))
        )
        downloaded = getattr(torrent, "downloaded", None)
        return TorrentStatus(
            info_hash=info_hash,
            name=torrent.name,
            progress=float(torrent.progress),
            completed_bytes=max(0, completed_bytes),
            downloaded_bytes=max(0, int(downloaded)) if downloaded is not None else None,
            # progress==1 即全部数据落盘（此后进入做种/完成态）
            completed=completed,
            save_path=torrent.save_path,
            files=[
                # f.name 是种子内相对路径（含子目录）
                TorrentFile(
                    path=f.name,
                    size_bytes=int(f.size),
                    completed_bytes=max(
                        0,
                        min(int(f.size), int(int(f.size) * float(f.progress))),
                    ),
                    # qB priority=0 表示“不下载”；其余值只区分优先级。
                    selected=int(getattr(f, "priority", 1)) > 0,
                )
                for f in files
            ],
            size_bytes=size_bytes or None,
            dlspeed_bytes=int(getattr(torrent, "dlspeed", 0) or 0),
            eta_seconds=eta if 0 < eta < 8640000 else None,
            state=_normalize_state(str(getattr(torrent, "state", "")), completed=completed),
        )

    async def list_torrents(self) -> list[TorrentBrief]:
        return await asyncio.to_thread(self._list_torrents_sync)

    def _list_torrents_sync(self) -> list[TorrentBrief]:
        client = self._client()
        with _translate_errors(self.config.url):
            infos = client.torrents_info()
        briefs = []
        for torrent in infos:
            # content_path 是下载器视角的落盘根路径，末段即磁盘上的真实
            # 目录/文件名（种子在客户端里被改名后 name 会失真，末段不会）
            content = str(getattr(torrent, "content_path", "") or "").rstrip("/\\")
            content_name = content.replace("\\", "/").rsplit("/", 1)[-1] if content else ""
            progress = float(torrent.progress)
            completed = progress >= 1.0
            # qBittorrent 用 8640000（100 天）表示“无法估算”。
            eta = int(getattr(torrent, "eta", 0) or 0)
            briefs.append(
                TorrentBrief(
                    name=torrent.name,
                    content_name=content_name or torrent.name,
                    completed=completed,
                    info_hash=str(torrent.hash).lower(),
                    progress=progress,
                    completed_bytes=max(
                        0,
                        int(
                            getattr(torrent, "completed", 0)
                            or int(int(getattr(torrent, "size", 0) or 0) * progress)
                        ),
                    ),
                    size_bytes=int(getattr(torrent, "size", 0) or 0) or None,
                    dlspeed_bytes=int(getattr(torrent, "dlspeed", 0) or 0),
                    upspeed_bytes=int(getattr(torrent, "upspeed", 0) or 0),
                    # uploaded/downloaded 是本任务的累计上/下行字节；
                    # 缺失（极老版本）保持 None
                    uploaded_bytes=(
                        int(torrent.uploaded)
                        if getattr(torrent, "uploaded", None) is not None
                        else None
                    ),
                    downloaded_bytes=(
                        int(torrent.downloaded)
                        if getattr(torrent, "downloaded", None) is not None
                        else None
                    ),
                    ratio=(
                        float(torrent.ratio)
                        if getattr(torrent, "ratio", None) is not None
                        else None
                    ),
                    # num_complete/num_incomplete 是 tracker 汇报的全网蜂群规模
                    swarm_seeders=(
                        int(torrent.num_complete)
                        if getattr(torrent, "num_complete", None) is not None
                        else None
                    ),
                    swarm_leechers=(
                        int(torrent.num_incomplete)
                        if getattr(torrent, "num_incomplete", None) is not None
                        else None
                    ),
                    eta_seconds=eta if 0 < eta < 8640000 else None,
                    state=_normalize_state(str(getattr(torrent, "state", "")), completed=completed),
                )
            )
        return briefs

    async def delete_torrent(self, info_hash: str, *, delete_files: bool = False) -> None:
        await asyncio.to_thread(self._delete_torrent_sync, info_hash, delete_files)

    def _delete_torrent_sync(self, info_hash: str, delete_files: bool) -> None:
        """按用户选择删除任务或连同数据；不存在的 hash 原生保持幂等。"""
        client = self._client()
        try:
            # 认证/连接错误沿用公共翻译；删除接口自身的 API 错误在外层给出
            # 精确语义，不能误报为“提交种子失败”。
            with _translate_errors(self.config.url):
                client.torrents_delete(
                    torrent_hashes=info_hash.lower(),
                    delete_files=delete_files,
                )
        except qbittorrentapi.APIError as exc:
            raise DownloaderDeleteError(
                "删除 qBittorrent 任务失败，请检查下载器状态",
                details={"url": self.config.url, "error": str(exc)},
            ) from exc
        logger.info(
            "已从 qBittorrent 删除任务%s: hash=%s",
            "并删除数据文件" if delete_files else "并保留数据文件",
            info_hash,
        )

    async def transfer_speeds(self) -> tuple[int, int]:
        return await asyncio.to_thread(self._transfer_speeds_sync)

    def _transfer_speeds_sync(self) -> tuple[int, int]:
        """全局瞬时速度来自 transfer/info（一次轻量请求，哨兵秒级轮询可承受）。"""
        client = self._client()
        with _translate_errors(self.config.url):
            info = client.transfer_info()
        return (
            int(info.get("up_info_speed", 0) or 0),
            int(info.get("dl_info_speed", 0) or 0),
        )

    async def set_download_limits(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        await asyncio.to_thread(self._set_download_limits_sync, info_hashes, limit_bytes)

    def _set_download_limits_sync(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        """qB 的按种限速：-1 = 取消限速（与 torrents/info 的 dl_limit=-1 一致）。"""
        if not info_hashes:
            return
        client = self._client()
        with _translate_errors(self.config.url):
            client.torrents_set_download_limit(
                limit=limit_bytes if limit_bytes and limit_bytes > 0 else -1,
                torrent_hashes=[h.lower() for h in info_hashes],
            )

    async def set_upload_limits(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        await asyncio.to_thread(self._set_upload_limits_sync, info_hashes, limit_bytes)

    def _set_upload_limits_sync(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        """qB 的按种上传限速：-1 = 取消限速（与下载限速同一语义）。"""
        if not info_hashes:
            return
        client = self._client()
        with _translate_errors(self.config.url):
            client.torrents_set_upload_limit(
                limit=limit_bytes if limit_bytes and limit_bytes > 0 else -1,
                torrent_hashes=[h.lower() for h in info_hashes],
            )

    async def get_limits(self) -> DownloaderLimits:
        return await asyncio.to_thread(self._get_limits_sync)

    def _get_limits_sync(self) -> DownloaderLimits:
        """限速走 transfer 专用端点（字节/秒，0=不限，语义明确无歧义）；
        队列上限走 app 偏好（queueing_enabled 及三个 max_active_*）。"""
        client = self._client()
        with _translate_errors(self.config.url):
            dl = int(client.transfer_download_limit() or 0)
            up = int(client.transfer_upload_limit() or 0)
            alt = str(client.transfer_speed_limits_mode()) == "1"
            prefs = client.app_preferences()
        return DownloaderLimits(
            download_limit_bytes=dl or None,
            upload_limit_bytes=up or None,
            alt_speed_enabled=alt,
            queue_enabled=bool(prefs.get("queueing_enabled")),
            max_active_downloads=prefs.get("max_active_downloads"),
            max_active_uploads=prefs.get("max_active_uploads"),
            max_active_torrents=prefs.get("max_active_torrents"),
        )

    async def set_limits(self, limits: DownloaderLimits) -> None:
        await asyncio.to_thread(self._set_limits_sync, limits)

    def _set_limits_sync(self, limits: DownloaderLimits) -> None:
        client = self._client()
        with _translate_errors(self.config.url):
            client.transfer_set_download_limit(limits.download_limit_bytes or 0)
            client.transfer_set_upload_limit(limits.upload_limit_bytes or 0)
            if limits.alt_speed_enabled is not None:
                # 仅在状态确需变更时才调用：qbittorrent-api 在 Web API < 2.8.14
                # （qB < 4.4.0）且目标状态与当前一致时，会误调旧版不存在的
                # setSpeedLimitsMode 端点返回 404；状态一致时本就无需任何调用
                current_alt = str(client.transfer_speed_limits_mode()) == "1"
                if current_alt != limits.alt_speed_enabled:
                    client.transfer_set_speed_limits_mode(intended_state=limits.alt_speed_enabled)
            prefs: dict = {}
            if limits.queue_enabled is not None:
                prefs["queueing_enabled"] = limits.queue_enabled
            if limits.max_active_downloads is not None:
                prefs["max_active_downloads"] = limits.max_active_downloads
            if limits.max_active_uploads is not None:
                prefs["max_active_uploads"] = limits.max_active_uploads
            if limits.max_active_torrents is not None:
                prefs["max_active_torrents"] = limits.max_active_torrents
            if prefs:
                client.app_set_preferences(prefs=prefs)
        logger.info("已更新 qBittorrent 全局限制: %s", limits.model_dump(exclude_none=True))

    async def set_location(self, info_hash: str, save_path: str) -> None:
        await asyncio.to_thread(self._set_location_sync, info_hash, save_path)

    def _set_location_sync(self, info_hash: str, save_path: str) -> None:
        """改保存目录并由 qBittorrent 自行搬移数据（关 auto-TMM 后 set_location）。"""
        client = self._client()
        try:
            with _translate_errors(self.config.url):
                # 显式关 auto-TMM，与 submit 同理：开启时分类规则会抢走目录控制权
                client.torrents_set_auto_management(torrent_hashes=info_hash.lower(), enable=False)
                client.torrents_set_location(torrent_hashes=info_hash.lower(), location=save_path)
        except qbittorrentapi.APIError as exc:
            raise DownloaderSubmitError(
                "移动 qBittorrent 任务目录失败，请检查目标目录是否可写",
                details={"url": self.config.url, "save_path": save_path, "error": str(exc)},
            ) from exc
        logger.info("已移动 qBittorrent 任务目录: hash=%s -> %s", info_hash, save_path)

    async def test_connection(self) -> DownloaderInfo:
        return await asyncio.to_thread(self._test_connection_sync)

    def _test_connection_sync(self) -> DownloaderInfo:
        client = self._client()
        with _translate_errors(self.config.url):
            client.auth_log_in()
            version = client.app_version()
        return DownloaderInfo(type=DownloaderType.QBITTORRENT, version=version)

    async def close(self) -> None:
        """释放本实例对共享客户端的引用。

        刻意不调用 auth_log_out：客户端在同一下载器的所有适配器实例间共享，登出会把
        大家正在用的 SID 作废，下一次调用又得重新登录，复用连接的意义就没了。
        长连接本身交给 requests 连接池和进程生命周期管理。
        """
        self._qbt = None
