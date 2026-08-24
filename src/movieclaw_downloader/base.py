from __future__ import annotations

import abc

from movieclaw_downloader.models import (
    DownloaderConfig,
    DownloaderInfo,
    DownloaderLimits,
    DownloadRequest,
    SubmitResult,
    TorrentBrief,
    TorrentStatus,
)
from movieclaw_downloader.torrent import compute_info_hash, parse_magnet_info_hash


class BaseDownloader(abc.ABC):
    """所有下载器适配器的操作契约。

    设计约定：
    - 核心能力是 submit()：把种子提交给真正的下载软件，本服务自身不下载。
    - 全部方法是 async。qbittorrent-api / transmission-rpc 都是同步实现
      （requests），适配器内部用 asyncio.to_thread 包装，避免阻塞事件循环。
    - 失败通过 DownloaderConnectError / DownloaderAuthError /
      DownloaderSubmitError / DownloaderDeleteError 抛出；正常返回即代表操作成功。
    - 重复提交是幂等的：种子已存在时返回 already_exists=True，不报错。
    """

    def __init__(self, config: DownloaderConfig) -> None:
        self.config = config

    @abc.abstractmethod
    async def submit(self, request: DownloadRequest) -> SubmitResult:
        """提交一个下载任务。"""

    @abc.abstractmethod
    async def get_torrent(
        self, info_hash: str, *, include_files: bool = True
    ) -> TorrentStatus | None:
        """按 infohash 查询下载任务的进度与文件清单；不存在返回 None。

        入库管线据此判断"下载器确认完成"（completed）并拿到文件的落盘位置。

        ``include_files=False`` 时跳过文件清单的获取（``files`` 返回空列表）：
        进度快照类的高频轮询只需要 info 字段，省掉一次逐文件查询的开销。
        """

    @abc.abstractmethod
    async def list_torrents(self) -> list[TorrentBrief]:
        """列出全部下载任务的轻量概览（名称/落盘根名/是否完成）。

        下载监听导入据此按**名称**判定条目是否下载完成——名称匹配免疫
        容器路径映射，比 save_path 比对可靠（见 TorrentBrief 注释）。
        """

    @abc.abstractmethod
    async def delete_torrent(self, info_hash: str, *, delete_files: bool = False) -> None:
        """按 infohash 删除下载任务。

        ``delete_files=False`` 时只从下载器移除任务；显式传 True 时同时让
        下载器删除任务数据。删除是幂等操作，任务已经不存在时也视为成功。
        """

    @abc.abstractmethod
    async def set_location(self, info_hash: str, save_path: str) -> None:
        """把下载任务的保存目录改为 ``save_path``（下载器视角），并移动已有数据。

        用途：刷流抢下的种子被订阅/手动下载认领时，把数据从刷流目录迁到
        媒体的目标目录，让入库监听能看见它（docs/design/
        site-protection-ratio-boost.md 2.5）。两端实现都由下载器自行搬移
        文件（qB set_location / Transmission move_torrent_data），跨盘时
        是真实拷贝，调用方不应假设瞬间完成。
        """

    @abc.abstractmethod
    async def get_limits(self) -> DownloaderLimits:
        """读取下载器的全局限制：限速、备用限速档与任务队列上限。

        归一口径见 DownloaderLimits 注释。队列上限尤其值得暴露：刷流大量
        做种时任务会撞上「最大活动种子数」进 queued 排队，用户需要能看到
        并调整这个值。
        """

    @abc.abstractmethod
    async def set_limits(self, limits: DownloaderLimits) -> None:
        """写入下载器的全局限制（读-改-写语义：调用方传完整期望状态）。

        限速 ``None`` = 取消限速；下载器不支持的字段（如 Transmission 的
        max_active_torrents）静默忽略。
        """

    @abc.abstractmethod
    async def transfer_speeds(self) -> tuple[int, int]:
        """读取下载器全局的瞬时速度：(上传字节/秒, 下载字节/秒)。

        刷流带宽哨兵的快环信号源：上行受压时刷流下载秒级让路。含下载器里
        **全部**任务的流量（用户自己的下载也在内）——哨兵保护的是整条链路
        的上行，不区分流量归属。
        """

    @abc.abstractmethod
    async def set_download_limits(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        """批量设置指定任务的下载限速（字节/秒），``None`` = 取消限速。

        刷流带宽哨兵只对 movieclaw-boost 分类的任务限速让路，绝不触碰
        下载器全局限速或用户自己的任务。不存在的 hash 静默忽略（幂等）。
        """

    @abc.abstractmethod
    async def set_upload_limits(self, info_hashes: list[str], limit_bytes: int | None) -> None:
        """批量设置指定任务的上传限速（字节/秒），``None`` = 取消限速。

        站点刷流「暂停」用它把在池做种压到极低速，为用户的前台流量
        （看视频等）让出上行；恢复时传 None 解除。只作用于传入的
        movieclaw-boost 任务，绝不触碰下载器全局限速或用户自己的任务。
        不存在的 hash 静默忽略（幂等）。
        """

    @abc.abstractmethod
    async def test_connection(self) -> DownloaderInfo:
        """验证连通性与凭证，返回下载器版本信息。"""

    @abc.abstractmethod
    async def close(self) -> None:
        """释放连接资源（登出、丢弃底层客户端等）。"""

    # -- 工具方法 ----------------------------------------------------------

    @staticmethod
    def _resolve_info_hash(request: DownloadRequest) -> str | None:
        """从请求中解析 v1 infohash，作为去重键和统一返回标识。"""
        if request.torrent_bytes is not None:
            return compute_info_hash(request.torrent_bytes)
        assert request.magnet is not None  # 模型校验保证二选一
        return parse_magnet_info_hash(request.magnet)
