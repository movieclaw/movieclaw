"""下载器状态归一化（TorrentStatus.state 统一词表）的单元测试。"""

from __future__ import annotations

from movieclaw_downloader.clients.qbittorrent import _error_message as qb_error
from movieclaw_downloader.clients.qbittorrent import _normalize_state as qb_state
from movieclaw_downloader.clients.transmission import _error_message as tr_error
from movieclaw_downloader.clients.transmission import _normalize_state as tr_state


class _FakeTrTorrent:
    def __init__(self, fields: dict) -> None:
        self.fields = fields


def test_qbittorrent_state_words() -> None:
    assert qb_state("downloading", completed=False) == "downloading"
    assert qb_state("forcedDL", completed=False) == "downloading"
    assert qb_state("stalledDL", completed=False) == "stalled"
    assert qb_state("queuedDL", completed=False) == "queued"
    assert qb_state("pausedDL", completed=False) == "paused"
    assert qb_state("error", completed=False) == "error"
    assert qb_state("missingFiles", completed=False) == "error"
    # 完成态压过一切原始状态（做种/暂停做种都算 completed）
    assert qb_state("uploading", completed=True) == "completed"
    assert qb_state("什么都不是", completed=False) == "unknown"


def test_transmission_state_words() -> None:
    assert tr_state(_FakeTrTorrent({"status": 4, "rateDownload": 1024}), completed=False) == (
        "downloading"
    )
    # 下载态但零速：对齐 qBittorrent 的 stalled 语义
    assert tr_state(_FakeTrTorrent({"status": 4, "rateDownload": 0}), completed=False) == "stalled"
    assert tr_state(_FakeTrTorrent({"status": 3}), completed=False) == "queued"
    assert tr_state(_FakeTrTorrent({"status": 0}), completed=False) == "paused"
    # 本地错误（磁盘/权限/文件缺失）优先于状态枚举：下载器这条任务本身坏了
    assert tr_state(_FakeTrTorrent({"status": 4, "error": 3}), completed=False) == "error"
    # tracker 警告/错误不是本地故障：种子从站点撤下就是没有做种的死种，
    # 必须落入 stalled 交给换源机制，而不是被当成下载器故障冻结救援
    tracker_warning = _FakeTrTorrent({"status": 4, "error": 1, "rateDownload": 0})
    tracker_error = _FakeTrTorrent({"status": 4, "error": 2, "rateDownload": 0})
    assert tr_state(tracker_warning, completed=False) == "stalled"
    assert tr_state(tracker_error, completed=False) == "stalled"
    assert tr_state(_FakeTrTorrent({"status": 6}), completed=True) == "completed"
    assert tr_state(_FakeTrTorrent({"status": 6}), completed=False) == "unknown"


def test_qbittorrent_error_reason_distinguishes_missing_files() -> None:
    # 文件缺失是整批任务同时报错的常见原因，必须和泛化的 error 区分开，
    # 用户才知道该去检查存储而不是下载器连接
    assert "找不到已下载的文件" in (qb_error("missingFiles") or "")
    assert "查看具体原因" in (qb_error("error") or "")
    assert qb_error("downloading") is None
    assert qb_error("stalledDL") is None


def test_transmission_error_reason_passes_through_error_string() -> None:
    torrent = _FakeTrTorrent({"status": 4, "error": 3, "errorString": "No data found!"})
    assert tr_error(torrent, completed=False) == "下载器报告：No data found!"
    # 没有错误文本时也要给出可行动的兜底说明
    assert "查看具体原因" in (tr_error(_FakeTrTorrent({"error": 3}), completed=False) or "")
    assert tr_error(_FakeTrTorrent({"status": 4, "error": 0}), completed=False) is None
    # tracker 错误不属于 error 态，也就没有"下载器故障"原因可说
    unregistered = _FakeTrTorrent({"error": 2, "errorString": "Unregistered"})
    assert tr_error(unregistered, completed=False) is None
    # 已完成的任务即使带错误标记也不算下载异常
    assert tr_error(_FakeTrTorrent({"error": 3, "errorString": "x"}), completed=True) is None
