"""应用内更新服务（docs/design/in-app-update.md M3）。

职责与流程：
- 检查：查 GitHub 最新 Release → 下载 manifest.json → 与当前版本、镜像
  runtime 版本比对，告诉用户「可更新 / 已最新 / 需升级镜像」；
- 执行：后台线程完成 下载 → sha256 逐文件校验 → 解包到 versions/<ver> →
  备份 SQLite → 原子切换 current/previous 符号链接 → 以约定码 43 触发
  前后端全量重启（entrypoint 重新解析代码来源后从新版本拉起）；
- 回退：current 与 previous 互换（可再切回来），无 previous 时删除 current
  回落镜像基线；同样以 43 重启。

安全设计：
- 更新从不覆盖镜像内文件，只在 data 卷的 updates/ 目录落盘并切换指针，
  任何一步失败都不影响正在运行的版本（详见 entrypoint 的解析与兜底）；
- sha256 校验是强制项，其信任锚点是 manifest：未配置签名公钥时 manifest
  固定直连 GitHub（只有大文件走加速镜像），恶意镜像无法自洽伪造；
  配置 UPDATE_MANIFEST_PUBKEY 后 manifest 强制验签，此时可整体走镜像；
- 解包用 tarfile 的 data 过滤器，杜绝路径穿越；
- 切换前用 sqlite3 backup API 在线备份数据库（WAL 下直接拷文件会丢
  未 checkpoint 的事务；迁移是单向的，回退跨版本时靠备份恢复数据）。

只有 Docker entrypoint 环境才支持应用内更新（据 MOVIECLAW_RUNTIME_VERSION
环境变量判定）：源码部署的用户直接 git pull 即可，无需这套机制。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import cmp_to_key
from pathlib import Path

import httpx

from movieclaw_api import __version__
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.app_update import (
    LastAbnormalExitView,
    ModelUpdateCheckView,
    PendingUpdateView,
    RollbackOptionsView,
    RollbackTargetView,
    UpdateCheckView,
    UpdateProgressView,
    UpdateStatusView,
)
from movieclaw_api.schemas.base import normalize_utc_iso, utc_isoformat
from movieclaw_api.services.app_config import FULL_RESTART_EXIT_CODE, schedule_restart
from movieclaw_api.settings.schemas import (
    get_app_update_prefs,
    get_app_update_state,
    save_app_update_prefs,
    save_app_update_state,
)
from movieclaw_db.models import utcnow
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_net import egress_transport, resolve_proxy_url
from movieclaw_scheduler import register_task

logger = logging.getLogger("movieclaw_api.app_update")

# Release 产物文件名（与 scripts/build-release-artifacts.sh 的产出一致）
_ARTIFACT_NAMES = ("app-backend.tar.gz", "app-web.tar.gz")
_MANIFEST_NAME = "manifest.json"
# 版本目录里的 Release 元信息（安装时落盘，回退选择器离线展示更新说明）
_RELEASE_INFO_NAME = "release-info.json"
# 数据库备份保留份数（SQLite 单文件，成本低，多留几份）
_BACKUP_KEEP = 5
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=15.0)


# ---------------------------------------------------------------------------
# 路径与环境
# ---------------------------------------------------------------------------


def _updates_dir() -> Path:
    return Path(get_settings().updates_dir).resolve()


def _runtime_version() -> int | None:
    """镜像的运行时版本；非 Docker entrypoint 环境（源码部署/开发）为 None。"""
    raw = os.environ.get("MOVIECLAW_RUNTIME_VERSION", "")
    return int(raw) if raw.isdigit() else None


def _sanitize(version: str) -> str:
    """与 entrypoint 的 sanitize 同款：版本号转文件名安全形式。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", version)


_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.\-]+))?(?:\+.*)?$")


def _is_newer(candidate: str, current: str) -> bool:
    """candidate 是否比 current 新（semver 语义的够用子集）。

    规则：数字主段逐段比较；带预发布段（-beta.1 等）的版本比同主段的正式版
    旧；预发布段按点分段、数字段按数值比（beta.10 > beta.9，semver 语义）。
    任何一侧无法解析时退化为「不相等即视为更新」——绝不抛异常（版本命名
    不规范不能把检查更新整个打挂）。
    """

    def parse(text: str) -> tuple[tuple[int, ...], tuple] | None:
        match = _VERSION_RE.match(text.strip())
        if match is None:
            return None
        nums = tuple(int(p) for p in match.group(1).split("."))
        pre = match.group(2)
        if not pre:
            # 正式版排在同主段的任何预发布之后：(1,) > (0, …)
            return nums, (1,)
        # 数字段标 (0, 数值)、非数字段标 (1, 字符串)：同位仅同类比较，不混型
        pre_key = tuple(
            (0, int(seg)) if seg.isdigit() else (1, seg) for seg in pre.split(".")
        )
        return nums, (0, pre_key)

    a, b = parse(candidate), parse(current)
    if a is None or b is None:
        return candidate.strip() != current.strip()
    # 主段长度对齐（1.2 与 1.2.0 等价）
    width = max(len(a[0]), len(b[0]))
    a_nums = a[0] + (0,) * (width - len(a[0]))
    b_nums = b[0] + (0,) * (width - len(b[0]))
    return (a_nums, a[1]) > (b_nums, b[1])


# ---------------------------------------------------------------------------
# 进度状态（模块级单例，后台线程写、接口读）
# ---------------------------------------------------------------------------


@dataclass
class _Progress:
    phase: str = "idle"
    detail: str = ""
    percent: float | None = None
    error: str | None = None
    target_version: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(
        self,
        phase: str,
        detail: str = "",
        *,
        percent: float | None = None,
        error: str | None = None,
        target_version: str | None = ...,  # type: ignore[assignment]
    ) -> None:
        with self.lock:
            self.phase = phase
            self.detail = detail
            self.percent = percent
            self.error = error
            if target_version is not ...:
                self.target_version = target_version

    def view(self) -> UpdateProgressView:
        with self.lock:
            return UpdateProgressView(
                phase=self.phase,
                detail=self.detail,
                percent=self.percent,
                error=self.error,
                target_version=self.target_version,
            )

    def busy(self) -> bool:
        with self.lock:
            return self.phase in ("checking", "downloading", "verifying", "applying", "restarting")


_progress = _Progress()


def get_progress() -> UpdateProgressView:
    return _progress.view()


def reset_progress_for_tests() -> None:
    """测试隔离用：把进度状态复位为 idle。"""
    _progress.set("idle", target_version=None)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def _is_marked_bad(version: str) -> bool:
    return (_updates_dir() / "state" / f"bad-{_sanitize(version)}").is_file()


# 异常退出记录的展示窗口：太久前的旧事件不再打扰用户（记录带时间戳，
# entrypoint 只在下一次异常退出时覆盖写入，不会自动清理）
_LAST_EXIT_MAX_AGE_SECONDS = 7 * 86400


def _read_last_abnormal_exit() -> LastAbnormalExitView | None:
    """读 entrypoint 落盘的异常退出记录（updates/state/last-exit.json）。

    这是尽力而为的诊断信息：文件不存在、损坏或超出展示窗口都按「无记录」
    处理，绝不影响状态接口本身。
    """
    path = _updates_dir() / "state" / "last-exit.json"
    try:
        data = json.loads(path.read_bytes())
        ts = int(data["ts"])
        if time.time() - ts > _LAST_EXIT_MAX_AGE_SECONDS:
            return None
        return LastAbnormalExitView(
            at=ts,
            reason=str(data.get("reason", "")),
            exit_code=int(data.get("exit_code", 1)),
            detail=str(data.get("detail", "")),
        )
    except Exception:
        return None


def _overlay_state(vdir: Path) -> tuple[str, bool, str]:
    """判定一个 overlay 版本目录能否被 entrypoint 采用（与其校验口径一致）。

    返回 (版本号, 是否可用, 不可用原因的中文说明)。status 的 has_previous、
    rollback 的目标校验、previous 指针的候选校验都用它——「能回退/会生效」
    的承诺必须以 entrypoint 真实会接受为准，否则用户会经历一次什么都没变的
    全量重启。
    """
    manifest_path = vdir / _MANIFEST_NAME
    if not vdir.is_dir() or not manifest_path.is_file():
        return "", False, "缺少更新清单"
    try:
        manifest = json.loads(manifest_path.read_bytes())
        version = str(manifest["version"])
        requires = int(manifest["requires_runtime"])
    except Exception:
        return "", False, "更新清单损坏"
    runtime = _runtime_version()
    if runtime is not None and requires != runtime:
        if requires > runtime:
            reason = f"需要 runtime {requires}（当前镜像为 {runtime}），需升级镜像"
        else:
            # requires < runtime 只可能是升级镜像前装的残留（安装时二者必相等，
            # 见 start_update 的前置校验），启动清场 prune_stale_overlays 会删掉它
            reason = f"为旧镜像构建（runtime {requires}，当前镜像为 {runtime}），已停用"
        return version, False, reason
    if _is_marked_bad(version):
        return version, False, "曾连续启动失败，已被自动回落保护"
    return version, True, ""


def build_status() -> UpdateStatusView:
    updates = _updates_dir()
    state_dir = updates / "state"
    bad_versions = sorted(
        p.name.removeprefix("bad-") for p in state_dir.glob("bad-*")
    ) if state_dir.is_dir() else []
    previous = updates / "previous"
    # 回退目标的具体版本号：UI 要向用户明示「回退到 v 几」而不是含糊的
    # 「上一版本」——回退会全量重启，用户必须先知道自己会落在哪个版本上
    previous_version = (
        _overlay_state(Path(previous))[0]
        if previous.is_symlink() and _overlay_state(Path(previous))[1]
        else None
    )
    has_previous = previous_version is not None

    # 盘上 current 指向的版本与实际运行版本不一致时，把「为什么没生效」外显：
    # 否则 runtime 不匹配 / bad 回落场景下，用户昨天还在的版本会凭空消失
    running_overlay = os.environ.get("MOVIECLAW_OVERLAY_VERSION") or None
    inactive_version: str | None = None
    inactive_reason: str | None = None
    current = updates / "current"
    if current.is_symlink():
        version, usable, reason = _overlay_state(Path(current))
        if version and version != running_overlay:
            inactive_version = version
            inactive_reason = reason if not usable else "尚未生效（等待应用重启）"

    return UpdateStatusView(
        current_version=__version__,
        code_source=os.environ.get("MOVIECLAW_CODE_SOURCE", "dev"),
        overlay_version=running_overlay,
        runtime_version=_runtime_version(),
        can_update=_runtime_version() is not None,
        has_previous=has_previous,
        previous_version=previous_version,
        bad_versions=list(bad_versions),
        model_tag=_current_model_tag(),
        inactive_overlay_version=inactive_version,
        inactive_overlay_reason=inactive_reason,
        last_abnormal_exit=_read_last_abnormal_exit(),
    )


# ---------------------------------------------------------------------------
# 检查更新
# ---------------------------------------------------------------------------


def _mirror_url(url: str) -> str:
    """按配置给下载地址套上加速镜像前缀（ghproxy 风格）。"""
    mirror = get_settings().update_download_mirror.strip()
    if not mirror:
        return url
    return mirror.rstrip("/") + "/" + url


_SIGNATURE_NAME = "manifest.json.sig"


def _verify_manifest_signature(raw: bytes, sig_text: bytes) -> None:
    """用配置的 Ed25519 公钥校验更新清单签名（base64 签名，签的是清单原始字节）。

    仅在 UPDATE_MANIFEST_PUBKEY 配置时被调用；校验失败抛 BadRequest（中文直达用户）。
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        pubkey = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(get_settings().update_manifest_pubkey.strip())
        )
    except (binascii.Error, ValueError) as exc:
        raise BadRequestException(
            "UPDATE_MANIFEST_PUBKEY 配置无效（应为 base64 的 32 字节 Ed25519 公钥），"
            "签名校验无法进行"
        ) from exc
    try:
        pubkey.verify(base64.b64decode(sig_text.strip()), raw)
    except (InvalidSignature, binascii.Error, ValueError) as exc:
        raise BadRequestException(
            "更新清单的签名校验失败，发布内容可能被篡改，已放弃本次更新。"
            "请改为直连 GitHub 重试，并向项目反馈"
        ) from exc


async def _download_manifest_bytes(client: httpx.AsyncClient, release: dict) -> bytes:
    """下载 Release 的更新清单；配置了签名公钥时强制验签后才返回。

    信任模型：清单是产物 sha256 的信任锚点，若也走第三方加速镜像下载，
    恶意镜像可同时伪造清单与产物、sha256 自洽通过。因此**未配置签名公钥时
    清单固定直连 GitHub**（体积小，直连通常可行），只有大文件走镜像；
    配置了公钥后签名接管防篡改，清单也允许走镜像加速。
    """
    signed = bool(get_settings().update_manifest_pubkey.strip())
    manifest_asset = next(
        (a for a in release.get("assets", []) if a.get("name") == _MANIFEST_NAME), None
    )
    if manifest_asset is None:
        raise BadRequestException(
            f"发布（{release.get('tag_name', '?')}）没有携带更新清单，"
            "可能是旧版发布或发布流程未完成，暂无法应用内更新"
        )

    async def fetch(url: str) -> bytes:
        resp = await client.get(_mirror_url(url) if signed else url)
        resp.raise_for_status()
        return resp.content

    try:
        raw = await fetch(manifest_asset["browser_download_url"])
    except httpx.HTTPError as exc:
        raise BadRequestException(
            "下载更新清单失败。为防篡改，未配置签名公钥时清单固定直连 GitHub 获取——"
            "直连不可达时可配置代理，或配置 UPDATE_MANIFEST_PUBKEY 签名校验后走加速镜像"
        ) from exc
    if signed:
        sig_asset = next(
            (a for a in release.get("assets", []) if a.get("name") == _SIGNATURE_NAME), None
        )
        if sig_asset is None:
            raise BadRequestException(
                f"本实例要求更新清单携带签名，但发布（{release.get('tag_name', '?')}）"
                f"缺少 {_SIGNATURE_NAME}，已放弃本次更新"
            )
        try:
            sig_raw = await fetch(sig_asset["browser_download_url"])
        except httpx.HTTPError as exc:
            raise BadRequestException("下载更新清单签名失败，请稍后重试") from exc
        _verify_manifest_signature(raw, sig_raw)
    return raw


def _parse_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw)
        version = str(manifest["version"])
        requires = int(manifest["requires_runtime"])
        files = manifest["files"]
        for name in _ARTIFACT_NAMES:
            if not files[name]["sha256"]:
                raise KeyError(name)
    except Exception as exc:
        raise BadRequestException(
            "更新清单（manifest.json）格式异常，可能是发布产物不完整，请稍后重试或反馈问题"
        ) from exc
    return {"version": version, "requires_runtime": requires, "files": files, "raw": raw}


# GitHub 条件请求缓存（ETag → 上次的 Release 列表）：命中 304 时 GitHub
# **不计入**未认证配额（60 次/小时/IP），这是每小时轮询不挤兑同出口 IP
# 用户配额的关键——绝大多数轮询都是 304，只有真发了新版才付一次完整请求。
_releases_etag: str | None = None
_releases_cached: list[dict] | None = None


def reset_release_cache_for_tests() -> None:
    global _releases_etag, _releases_cached
    _releases_etag = None
    _releases_cached = None


async def _fetch_releases(what: str) -> list[dict]:
    """拉全部 Release 列表（应用与模型的 Release 混在同一个仓库里，
    必须列表过滤，绝不能用 /releases/latest——模型发布会把 latest 顶掉）。"""
    global _releases_etag, _releases_cached
    settings = get_settings()
    api_url = f"{settings.update_api_base_url}/repos/{settings.update_repo}/releases?per_page=100"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "movieclaw-updater"}
    if _releases_etag and _releases_cached is not None:
        headers["If-None-Match"] = _releases_etag
    async with httpx.AsyncClient(
        # 走统一网络出口（服务标签 github）：设置页可为更新流量单独开关代理
        transport=egress_transport("github"),
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
        headers=headers,
    ) as client:
        try:
            resp = await client.get(api_url)
            if resp.status_code == 304 and _releases_cached is not None:
                return _releases_cached
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BadRequestException(
                f"无法连接 GitHub 检查{what}，"
                "请确认网络可达（可配置代理或 UPDATE_API_BASE_URL 反代）"
            ) from exc
        releases = resp.json()
        etag = resp.headers.get("etag")
        if etag:
            _releases_etag = etag
            _releases_cached = releases
        return releases


_APP_TAG_RE = re.compile(r"^v\d+(?:\.\d+)*(?:[-+].*)?$")


def _latest_app_release(releases: list[dict]) -> dict | None:
    """从 Release 列表挑出版本号最大的应用发布（tag 形如 vX.Y.Z；
    跳过草稿/预发布，也天然跳过模型发布的 torrent-ner-vN）。"""
    latest: dict | None = None
    for release in releases:
        tag = release.get("tag_name") or ""
        if release.get("draft") or release.get("prerelease"):
            continue
        if not _APP_TAG_RE.match(tag):
            continue
        if latest is None or _is_newer(tag[1:], (latest.get("tag_name") or "v")[1:]):
            latest = release
    return latest


async def _fetch_latest_release() -> tuple[dict, dict]:
    """取最新应用 Release 与其 manifest。返回 (release_json, manifest_dict)。"""
    releases = await _fetch_releases("更新")
    release = _latest_app_release(releases)
    if release is None:
        raise BadRequestException("未找到任何应用发布（vX.Y.Z 的 Release），暂无可用更新")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "movieclaw-updater"}
    async with httpx.AsyncClient(
        # 走统一网络出口（服务标签 github）：设置页可为更新流量单独开关代理
        transport=egress_transport("github"),
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
        headers=headers,
    ) as client:
        raw = await _download_manifest_bytes(client, release)
    manifest = _parse_manifest(raw)
    # 清单与 Release tag 强一致（与模型侧对称）：防「旧签名清单挂到新 tag」
    # 的重放，也防手工发布时张冠李戴——下载 URL 是按清单 version 拼的
    tag = release.get("tag_name") or ""
    if tag != f"v{manifest['version']}":
        raise BadRequestException(
            f"发布（{tag}）的更新清单声明的是 v{manifest['version']}，内容不匹配，已放弃"
        )
    return release, manifest


async def check_update() -> UpdateCheckView:
    release, manifest = await _fetch_latest_release()
    latest = manifest["version"]
    runtime = _runtime_version()
    return UpdateCheckView(
        current_version=__version__,
        latest_version=latest,
        update_available=_is_newer(latest, __version__),
        compatible=runtime is not None and manifest["requires_runtime"] == runtime,
        requires_runtime=manifest["requires_runtime"],
        changelog=release.get("body") or "",
        published_at=release.get("published_at") or "",
        latest_known_bad=_is_marked_bad(latest),
    )


# ---------------------------------------------------------------------------
# 执行更新（后台线程）
# ---------------------------------------------------------------------------


def _atomic_symlink(target: Path, link: Path) -> None:
    """原子地把 link 指向 target：先建临时链接再 rename 替换。"""
    tmp = link.with_name(link.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, link)


def _verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise RuntimeError(
            f"{path.name} 的校验和不匹配（下载可能被篡改或不完整）。"
            "若使用了加速镜像，请更换镜像或改为直连后重试"
        )


def _extract(tar_path: Path, dest: Path) -> None:
    """解包 tar.gz；data 过滤器拒绝路径穿越/设备文件等危险成员。"""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(dest, filter="data")


def _validate_layout(version_dir: Path) -> None:
    """与 entrypoint 的完整性校验同款：关键入口文件缺一不可。"""
    for rel in (
        "backend/src/movieclaw_api/main.py",
        "backend/alembic.ini",
        "backend/src/movieclaw_api/data/spec.json",
        "web/apps/web/server.js",
    ):
        if not (version_dir / rel).is_file():
            raise RuntimeError(f"更新产物解包后布局异常（缺少 {rel}），已放弃本次更新")


def _db_path() -> Path | None:
    """SQLite 数据库文件路径；非 SQLite 部署返回 None。"""
    url = get_settings().database_url
    if "sqlite" not in url or ":///" not in url:
        return None
    return Path(url.split(":///", 1)[1])


def _read_alembic_rev(db_path: Path) -> str | None:
    """读数据库当前的 alembic 迁移位置（alembic_version 表）；读不到返回 None。

    它是回退数据兼容判定的锚点：备份时记下当时的迁移位置，回退时与现库比对
    ——相同说明期间没跑过迁移，代码可以直接切；不同说明数据结构已前进，
    旧代码不认识新结构，必须恢复备份。
    """
    import sqlite3

    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _backup_database(updates: Path) -> None:
    """切换版本前备份 SQLite（非 SQLite 部署跳过）。迁移单向，备份是回退跨
    版本时恢复数据的唯一通道。

    必须用 sqlite3 的 backup API 而不是拷文件：数据库开着 WAL，已提交但
    未 checkpoint 的事务都在 -wal 文件里，直接拷主文件必然丢这部分数据，
    撞上 checkpoint 还可能拷出损坏文件。备份失败视为致命，中止本次更新。

    每份备份带一个同名 .json 边车，记录「离开哪个版本 + 当时的迁移位置」：
    更新 vX → vY 前备的这份库，就是 vX 能完美运行的数据快照——回退到 vX 时
    据此判定能否直接切换（迁移位置没变）或需要恢复这份备份（跨了迁移）。
    """
    import sqlite3

    db_path = _db_path()
    if db_path is None:
        logger.info("当前数据库不是 SQLite，跳过更新前自动备份")
        return
    if not db_path.is_file():
        return
    backup_dir = updates / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"movieclaw-v{_sanitize(__version__)}-{stamp}.db"
    try:
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"更新前的数据库自动备份失败（{exc}），为保护数据已中止本次更新"
        ) from exc
    sidecar = {
        "from_version": __version__,
        "alembic_rev": _read_alembic_rev(db_path),
        "taken_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    target.with_name(target.name + ".json").write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("已在更新前备份数据库：%s", target)


def _backup_from_version(backup: Path) -> str | None:
    """从备份读它离开的版本号：优先边车，缺边车时按文件名解析（老备份）。"""
    sidecar = backup.with_name(backup.name + ".json")
    if sidecar.is_file():
        try:
            version = json.loads(sidecar.read_text(encoding="utf-8")).get("from_version")
            if version:
                return str(version)
        except (OSError, json.JSONDecodeError):
            pass
    # 文件名形如 movieclaw-v{version}-{YYYYmmdd}-{HHMMSS}.db，掐头去尾取中段
    name = backup.name
    if not (name.startswith("movieclaw-v") and name.endswith(".db")):
        return None
    core = name[len("movieclaw-v") : -len(".db")]
    parts = core.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else None


def _latest_backup_for(updates: Path, version: str) -> Path | None:
    """找「离开某版本时」最近的一份备份（回退到该版本时的数据恢复源）。"""
    backup_dir = updates / "backup"
    if not backup_dir.is_dir():
        return None
    candidates = [
        p for p in backup_dir.glob("movieclaw-*.db") if _backup_from_version(p) == version
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def _prune_backups(updates: Path, retained_versions: set[str]) -> None:
    """清理备份：仍保留在盘上的版本各留最近一份「离开时」的备份（它们是
    对应版本的回退恢复源，版本目录还在就不能删），其余按时间留最近
    ``_BACKUP_KEEP`` 份。边车与备份文件同生共死。"""
    backup_dir = updates / "backup"
    if not backup_dir.is_dir():
        return
    backups = sorted(
        backup_dir.glob("movieclaw-*.db"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    keep: set[Path] = set(backups[:_BACKUP_KEEP])
    matched: set[str] = set()
    for backup in backups:  # 新的在前，每个版本命中的第一份就是最近一份
        version = _backup_from_version(backup)
        if version and version in retained_versions and version not in matched:
            keep.add(backup)
            matched.add(version)
    for backup in backups:
        if backup not in keep:
            backup.unlink(missing_ok=True)
            backup.with_name(backup.name + ".json").unlink(missing_ok=True)


def _apply_downloaded(
    manifest: dict,
    download_dir: Path,
    *,
    keep_versions: int = 5,
    release_info: dict | None = None,
) -> None:
    """校验、解包并切换到已下载的版本（下载完成后的纯本地流程，可独立测试）。

    download_dir 内应有 manifest["files"] 声明的全部产物文件。
    ``keep_versions``：本地保留的版本目录数（含新装的这个），来自用户偏好。
    ``release_info``：该版本的 Release 元信息（changelog/published_at），随
    版本目录落盘——回退选择器要能离线展示每个历史版本的更新说明。
    """
    version = manifest["version"]
    updates = _updates_dir()
    versions_dir = updates / "versions"
    final_dir = versions_dir / f"v{_sanitize(version)}"
    partial_dir = versions_dir / f"v{_sanitize(version)}.partial"

    _progress.set("verifying", "正在校验下载文件的完整性……", target_version=version)
    for name in _ARTIFACT_NAMES:
        _verify_sha256(download_dir / name, manifest["files"][name]["sha256"])

    _progress.set("applying", "正在解包并安装新版本……", target_version=version)
    shutil.rmtree(partial_dir, ignore_errors=True)
    _extract(download_dir / "app-backend.tar.gz", partial_dir / "backend")
    _extract(download_dir / "app-web.tar.gz", partial_dir / "web")
    (partial_dir / _MANIFEST_NAME).write_bytes(manifest["raw"])
    _validate_layout(partial_dir)
    (partial_dir / _RELEASE_INFO_NAME).write_text(
        json.dumps(
            {
                "version": version,
                "changelog": (release_info or {}).get("changelog") or "",
                "published_at": (release_info or {}).get("published_at") or "",
                "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 重新安装同一版本时清掉历史的坏标记/失败计数（用户显式重试即既往不咎）
    marker = _sanitize(version)
    (updates / "state" / f"bad-{marker}").unlink(missing_ok=True)
    (updates / "state" / f"failures-{marker}").unlink(missing_ok=True)

    _backup_database(updates)

    # 落定版本目录后原子切换指针：previous ← **实际运行中的版本**，current ← 新版本。
    # 不能简单取 readlink(current)：current 可能指向一个已被标 bad、实际并未在
    # 运行的版本（entrypoint 回落了 previous），把它记成 previous 会让「回退」
    # 名存实亡，还会把真正在运行的目录当垃圾清掉。
    shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(partial_dir, final_dir)
    current = updates / "current"
    prev_target: Path | None = None
    running = os.environ.get("MOVIECLAW_OVERLAY_VERSION")
    if running:
        running_dir = versions_dir / f"v{_sanitize(running)}"
        if running_dir.is_dir() and running_dir.name != final_dir.name:
            prev_target = running_dir
    if prev_target is None and current.is_symlink():
        cur_target = Path(os.readlink(current))
        # 旧 current 只有在 entrypoint 真会采用（runtime 匹配且未标坏）时才配
        # 当 previous——把不可用的旧版本记成「可回退目标」只会制造无效回退
        if cur_target.name != final_dir.name and _overlay_state(cur_target)[1]:
            prev_target = cur_target
    if prev_target is not None:
        _atomic_symlink(prev_target, updates / "previous")
    _atomic_symlink(final_dir, current)

    _prune_versions(updates, keep_versions=keep_versions, running=running)
    logger.info("应用内更新已就绪：v%s，即将全量重启生效", version)


def _prune_versions(updates: Path, *, keep_versions: int, running: str | None = None) -> None:
    """清理版本目录：保留最近 ``keep_versions`` 个（按版本号新旧排序），
    current/previous 指向的与正在运行的无条件保留（磁盘不无限增长，但
    绝不能把正在使用的目录当垃圾清掉）。备份跟着版本走：目录还保留的
    版本各留一份「离开时」的备份作为回退恢复源。"""
    versions_dir = updates / "versions"
    if not versions_dir.is_dir():
        return
    referenced = set()
    for link in (updates / "current", updates / "previous"):
        if link.is_symlink():
            referenced.add(Path(os.readlink(link)).name)
    if running is None:
        running = os.environ.get("MOVIECLAW_OVERLAY_VERSION")
    if running:
        referenced.add(f"v{_sanitize(running)}")
    dirs = [d for d in versions_dir.iterdir() if d.is_dir()]
    # 版本号新的在前（_is_newer 的 semver 子集语义；解析失败的排最后）
    dirs.sort(
        key=cmp_to_key(
            lambda a, b: -1
            if _is_newer(a.name.removeprefix("v"), b.name.removeprefix("v"))
            else (1 if _is_newer(b.name.removeprefix("v"), a.name.removeprefix("v")) else 0)
        )
    )
    # 标坏的版本不占保留名额也不保留（它已不是可用的回退目标，是纯磁盘死重）；
    # 除非它正被 current/previous 引用或正在运行——那是别处的问题，这里不动
    healthy = [d for d in dirs if not _is_marked_bad(d.name.removeprefix("v"))]
    keep_names = {d.name for d in healthy[: max(keep_versions, 1)]} | referenced
    for vdir in dirs:
        if vdir.name not in keep_names:
            shutil.rmtree(vdir, ignore_errors=True)
    # 状态标记跟着版本目录走：目录已清理的版本，其 bad/failures 标记一并清掉
    state_dir = updates / "state"
    remaining = {v.name for v in versions_dir.iterdir() if v.is_dir()}
    if state_dir.is_dir():
        for marker in list(state_dir.glob("bad-*")) + list(state_dir.glob("failures-*")):
            marked_version = marker.name.split("-", 1)[1]
            if f"v{marked_version}" not in remaining:
                marker.unlink(missing_ok=True)
    _prune_backups(updates, {name.removeprefix("v") for name in remaining})


def _download_and_apply(
    manifest: dict, *, keep_versions: int = 5, release_info: dict | None = None
) -> None:
    """后台线程主体：下载产物 → _apply_downloaded → 触发全量重启。"""
    version = manifest["version"]
    settings = get_settings()
    updates = _updates_dir()
    tmp_dir = updates / "tmp"
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        base = (
            f"https://github.com/{settings.update_repo}/releases/download/v{version}"
        )
        total = sum(int(manifest["files"][n].get("size") or 0) for n in _ARTIFACT_NAMES)
        done = 0
        with httpx.Client(
            # 出口层的 transport 只有 async 版，同步下载客户端按同一服务
            # 标签解析代理地址；trust_env=False 与出口层语义对齐——是否
            # 走代理只由「设置 → 网络与代理」决定，不再隐式吃环境变量
            proxy=resolve_proxy_url("github"),
            trust_env=False,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "movieclaw-updater"},
        ) as client:
            for name in _ARTIFACT_NAMES:
                _progress.set(
                    "downloading",
                    f"正在下载 {name}……",
                    percent=(done / total * 100) if total else None,
                    target_version=version,
                )
                url = _mirror_url(f"{base}/{name}")
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with (tmp_dir / name).open("wb") as fh:
                        for chunk in resp.iter_bytes(1024 * 256):
                            fh.write(chunk)
                            done += len(chunk)
                            if total:
                                _progress.set(
                                    "downloading",
                                    f"正在下载 {name}……",
                                    percent=min(done / total * 100, 100.0),
                                    target_version=version,
                                )
        _apply_downloaded(
            manifest, tmp_dir, keep_versions=keep_versions, release_info=release_info
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _progress.set(
            "restarting",
            f"新版本 v{version} 安装完成，正在重启应用……",
            target_version=version,
        )
        schedule_restart(FULL_RESTART_EXIT_CODE)
    except httpx.HTTPError as exc:
        logger.warning("应用内更新下载失败：%s", exc)
        _progress.set(
            "failed",
            "",
            error="下载更新产物失败，请检查网络（可配置加速镜像 UPDATE_DOWNLOAD_MIRROR）后重试",
            target_version=version,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as exc:
        logger.exception("应用内更新失败")
        _progress.set("failed", "", error=str(exc), target_version=version)
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def start_update() -> UpdateProgressView:
    """发起一次更新：前置校验后交给后台线程执行，立即返回当前进度。"""
    if _runtime_version() is None:
        raise BadRequestException(
            "当前部署形态不支持应用内更新（仅 Docker 镜像部署支持；源码部署请用 git pull 更新）"
        )
    if _progress.busy():
        raise BadRequestException("已有更新正在进行中，请等待其完成")
    _progress.set("checking", "正在获取最新版本信息……", target_version=None)
    try:
        release, manifest = await _fetch_latest_release()
        if not _is_newer(manifest["version"], __version__):
            raise BadRequestException(f"当前已是最新版本（v{__version__}），无需更新")
        if manifest["requires_runtime"] != _runtime_version():
            raise BadRequestException(
                f"新版本 v{manifest['version']} 包含运行时依赖变化"
                f"（需要 runtime {manifest['requires_runtime']}，"
                f"当前镜像为 {_runtime_version()}），请升级 Docker 镜像完成本次更新"
            )
        keep_versions = (await get_app_update_prefs()).keep_versions
    except BaseException:
        _progress.set("idle", target_version=None)
        raise
    release_info = {
        "changelog": release.get("body") or "",
        "published_at": release.get("published_at") or "",
    }
    thread = threading.Thread(
        target=_download_and_apply,
        args=(manifest,),
        kwargs={"keep_versions": keep_versions, "release_info": release_info},
        name="app-update",
        daemon=True,
    )
    thread.start()
    return _progress.view()


# ---------------------------------------------------------------------------
# NER 模型更新（独立于代码更新：模型 Release 单独发布，tag 形如 torrent-ner-vN；
# 更新只切 data 卷上的 current 指针，之后走 43 全量重启让 entrypoint
# 重新解析模型指针——42 不重解析 MOVIECLAW_NER_DIR，新模型不会生效）
# ---------------------------------------------------------------------------

_MODEL_TAG_RE = re.compile(r"^torrent-ner-v(\d+)$")
_MODEL_FILES = ("model.int8.onnx", "tokenizer.json", "labels.json")


def _models_dir() -> Path:
    return Path(get_settings().models_dir).resolve()


def _current_model_tag() -> str | None:
    """当前生效模型的 Release tag：读模型目录里的 .release-tag 记录。

    镜像构建与应用内安装都会写这个文件；老镜像没有该记录时返回 None
    （UI 显示「无法识别」，检查更新按「可更新」处理）。
    """
    ner_dir = os.environ.get("MOVIECLAW_NER_DIR", "")
    tag_file = Path(ner_dir) / ".release-tag" if ner_dir else None
    if tag_file and tag_file.is_file():
        tag = tag_file.read_text(encoding="utf-8").strip()
        return tag or None
    return None


def _model_tag_num(tag: str | None) -> int:
    match = _MODEL_TAG_RE.match(tag or "")
    return int(match.group(1)) if match else 0


def _latest_model_release(releases: list[dict]) -> dict | None:
    """从 Release 列表挑出 tag 版本号最大的模型发布。"""
    candidates = [r for r in releases if _MODEL_TAG_RE.match(r.get("tag_name") or "")]
    if not candidates:
        return None
    return max(candidates, key=lambda r: _model_tag_num(r["tag_name"]))


def _parse_model_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw)
        files = manifest["files"]
        for name in _MODEL_FILES:
            if not files[name]["sha256"]:
                raise KeyError(name)
    except Exception as exc:
        raise BadRequestException(
            "模型更新清单（manifest.json）格式异常，请稍后重试或反馈问题"
        ) from exc
    return {"files": files, "raw": raw, "tag": manifest.get("tag") or ""}


async def _fetch_latest_model_release() -> dict:
    release = _latest_model_release(await _fetch_releases("模型更新"))
    if release is None:
        raise BadRequestException("未找到任何模型发布（torrent-ner-v* 的 Release）")
    return release


def _model_manifest_asset(release: dict) -> dict | None:
    return next(
        (a for a in release.get("assets", []) if a.get("name") == _MANIFEST_NAME), None
    )


async def check_model_update() -> ModelUpdateCheckView:
    release = await _fetch_latest_model_release()
    latest_tag = release["tag_name"]
    current = _current_model_tag()
    return ModelUpdateCheckView(
        current_tag=current,
        latest_tag=latest_tag,
        update_available=_model_tag_num(latest_tag) > _model_tag_num(current),
        installable=_model_manifest_asset(release) is not None,
        published_at=release.get("published_at") or "",
    )


def _install_model_files(tag: str, manifest: dict, download_dir: Path) -> None:
    """校验并安装已下载的模型文件（纯本地流程，可独立测试）：
    落盘到 models_dir/<tag>/ → 写 .release-tag → 原子切 current 指针 → 清旧目录。"""
    for name in _MODEL_FILES:
        _verify_sha256(download_dir / name, manifest["files"][name]["sha256"])
    models = _models_dir()
    final_dir = models / _sanitize(tag)
    partial_dir = models / f"{_sanitize(tag)}.partial"
    shutil.rmtree(partial_dir, ignore_errors=True)
    partial_dir.mkdir(parents=True)
    for name in _MODEL_FILES:
        shutil.move(str(download_dir / name), partial_dir / name)
    (partial_dir / ".release-tag").write_text(tag + "\n", encoding="utf-8")
    shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(partial_dir, final_dir)
    _atomic_symlink(final_dir, models / "current")
    # 清掉旧模型目录，但保护**当前进程正在用**的那个（MOVIECLAW_NER_DIR 指向
    # 的目录）：距离重启生效还有约 11 秒窗口，删了它会让窗口内的 NER 推理
    # 因文件缺失而失败；留下的这一个目录会在下次模型更新时被清理
    active = os.environ.get("MOVIECLAW_NER_DIR", "")
    active_dir = Path(active).resolve() if active else None
    for entry in models.iterdir():
        if entry.is_dir() and not entry.is_symlink() and entry.name != final_dir.name:
            if active_dir is not None and entry.resolve() == active_dir:
                continue
            shutil.rmtree(entry, ignore_errors=True)
    logger.info("NER 模型已安装：%s，即将重启后端生效", tag)


def _download_and_apply_model(release: dict, manifest: dict) -> None:
    """后台线程主体：下载模型文件 → 安装 → 全量重启生效（43，见下方注释）。"""
    tag = release["tag_name"]
    assets = {a.get("name"): a for a in release.get("assets", [])}
    tmp_dir = _models_dir() / "tmp"
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        total = sum(int(manifest["files"][n].get("size") or 0) for n in _MODEL_FILES)
        done = 0
        with httpx.Client(
            # 出口层的 transport 只有 async 版，同步下载客户端按同一服务
            # 标签解析代理地址；trust_env=False 与出口层语义对齐——是否
            # 走代理只由「设置 → 网络与代理」决定，不再隐式吃环境变量
            proxy=resolve_proxy_url("github"),
            trust_env=False,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "movieclaw-updater"},
        ) as client:
            for name in _MODEL_FILES:
                asset = assets.get(name)
                if asset is None:
                    raise RuntimeError(f"模型发布 {tag} 缺少文件 {name}，无法安装")
                _progress.set(
                    "downloading",
                    f"正在下载模型文件 {name}……",
                    percent=(done / total * 100) if total else None,
                    target_version=tag,
                )
                with client.stream("GET", _mirror_url(asset["browser_download_url"])) as resp:
                    resp.raise_for_status()
                    with (tmp_dir / name).open("wb") as fh:
                        for chunk in resp.iter_bytes(1024 * 256):
                            fh.write(chunk)
                            done += len(chunk)
                            if total:
                                _progress.set(
                                    "downloading",
                                    f"正在下载模型文件 {name}……",
                                    percent=min(done / total * 100, 100.0),
                                    target_version=tag,
                                )
        _progress.set("verifying", "正在校验模型文件的完整性……", target_version=tag)
        _install_model_files(tag, manifest, tmp_dir)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _progress.set(
            "restarting",
            f"模型 {tag} 安装完成，正在重启应用生效……",
            target_version=tag,
        )
        # 必须走 43 全量重启：MOVIECLAW_NER_DIR 是 entrypoint 解析模型指针后
        # 导出的**具体版本目录**路径，只有重新走一遍 resolve 才会指向新模型；
        # 42 只拉起后端、不重解析，新模型不会生效（旧目录还已被清理）。
        schedule_restart(FULL_RESTART_EXIT_CODE)
    except httpx.HTTPError as exc:
        logger.warning("模型更新下载失败：%s", exc)
        _progress.set(
            "failed",
            "",
            error="下载模型文件失败，请检查网络（可配置加速镜像 UPDATE_DOWNLOAD_MIRROR）后重试",
            target_version=tag,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as exc:
        logger.exception("模型更新失败")
        _progress.set("failed", "", error=str(exc), target_version=tag)
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def start_model_update() -> UpdateProgressView:
    """发起一次模型更新：前置校验后交给后台线程执行，立即返回当前进度。"""
    if _runtime_version() is None:
        raise BadRequestException(
            "当前部署形态不支持应用内更新模型（仅 Docker 镜像部署支持）"
        )
    if _progress.busy():
        raise BadRequestException("已有更新正在进行中，请等待其完成")
    _progress.set("checking", "正在获取最新模型信息……", target_version=None)
    try:
        release = await _fetch_latest_model_release()
        tag = release["tag_name"]
        if _model_tag_num(tag) <= _model_tag_num(_current_model_tag()):
            raise BadRequestException(f"当前模型已是最新（{_current_model_tag()}），无需更新")
        if _model_manifest_asset(release) is None:
            raise BadRequestException(
                f"模型发布 {tag} 未携带更新清单（manifest.json），暂无法应用内安装"
            )
        headers = {"User-Agent": "movieclaw-updater"}
        async with httpx.AsyncClient(
            transport=egress_transport("github"),
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        ) as client:
            raw = await _download_manifest_bytes(client, release)
        manifest = _parse_model_manifest(raw)
        # 清单声明了 tag 时必须与 Release tag 一致：开启验签后这一致性把
        # 「用旧签名清单重放成新版本」的路径也堵死（清单未声明 tag 时跳过，
        # 兼容早期模型发布）
        if manifest.get("tag") and manifest["tag"] != tag:
            raise BadRequestException(
                f"模型发布 {tag} 的更新清单声明的是 {manifest['tag']}，内容不匹配，已放弃安装"
            )
    except BaseException:
        _progress.set("idle", target_version=None)
        raise
    thread = threading.Thread(
        target=_download_and_apply_model,
        args=(release, manifest),
        name="model-update",
        daemon=True,
    )
    thread.start()
    return _progress.view()


# ---------------------------------------------------------------------------
# 更新检查结果快照（前端更新提醒的唯一数据源）
# ---------------------------------------------------------------------------
# 提醒形态的产品决策：更新**不进「待处理事项」**。待处理事项的语义是「运行时
# 故障，修好才会消失」，而「有新版可用」既不是故障、也不该被用户"处理掉"——
# 混在一起会让红色告警入口天天亮着，真故障反而被淹没（告警疲劳）。
#
# 改用主流桌面产品的做法（Chrome 菜单圆点 / VS Code 齿轮角标 / macOS 系统设置
# 徽标）：一个**常驻、不可忽略、不打断**的环境徽标，它只在有新版时存在，装了
# 新版就自然消失，不需要用户做任何"清理"动作。徽标要能随时点亮，就不能每次
# 都去打 GitHub，故这里把检查结论落成快照，前端只读快照（见 settings.schemas
# 的 AppUpdateStateSetting 模块注释）。


async def _record_app_check(view: UpdateCheckView) -> None:
    """把一次应用更新检查的结论写进快照。

    曾在本机连续启动失败被自动回落的版本按「无可用更新」记录：提醒用户去装
    一个刚坑过自己的版本是误导，等修复版本发布后自然恢复提醒。
    """
    state = await get_app_update_state()
    available = view.update_available and not view.latest_known_bad
    state.checked_at = utc_isoformat(utcnow())
    state.app_latest_version = view.latest_version if available else ""
    state.app_compatible = view.compatible if available else False
    state.app_changelog = view.changelog if available else ""
    state.app_published_at = view.published_at if available else ""
    await save_app_update_state(state)


async def _record_model_check(view: ModelUpdateCheckView) -> None:
    """把一次模型更新检查的结论写进快照。

    不携带更新清单的模型发布记为「无可用更新」：它装不进来，提醒了也没有
    对应的操作，只会变成用户消不掉的噪声。
    """
    state = await get_app_update_state()
    state.checked_at = utc_isoformat(utcnow())
    state.model_latest_tag = (
        view.latest_tag if view.update_available and view.installable else ""
    )
    await save_app_update_state(state)


async def check_update_now() -> UpdateCheckView:
    """检查应用更新并记录结论（「检查更新」按钮与定时任务共用的入口）。

    与裸的 ``check_update()`` 的区别只在于落快照：手动检查完，侧栏徽标要跟着
    同步点亮/熄灭，不能出现「页面上说有新版、侧栏没反应」的割裂。
    """
    view = await check_update()
    await _record_app_check(view)
    return view


async def check_model_update_now() -> ModelUpdateCheckView:
    """检查 NER 模型更新并记录结论（同 ``check_update_now`` 的理由）。"""
    view = await check_model_update()
    await _record_model_check(view)
    return view


async def clear_legacy_update_notices() -> None:
    """清掉旧版写进「待处理事项」的更新提醒（一次性，幂等，启动时调用）。

    v0.2.x 之前更新提醒是 dedupe_key 以 ``app_update:`` 开头的告警行。改成侧栏
    徽标后，再没有任何代码路径会去 resolve 它们——存量部署升上来会看到一条
    永远消不掉的「发现新版本」告警。这里按前缀消退一次；表里没有这类行时
    resolve_notices 直接短路返回 0，对新装用户零开销。
    """
    from movieclaw_api.services.system_notice import resolve_notices
    from movieclaw_db.engine import get_database

    try:
        async with get_database().session() as session:
            await resolve_notices(session, prefix="app_update:")
    except Exception:
        # 清场失败只是留下一条陈旧告警，绝不该拖垮启动
        logger.warning("清理旧版更新提醒失败（不影响启动）", exc_info=True)


async def read_pending() -> PendingUpdateView:
    """读取待更新快照（纯读库，不触网）。

    读取时按**当前运行版本**兜底过滤：装完新版重启后，快照里还留着
    "发现新版本 vX"（要等重启后 3 分钟的启动首查才会清掉，离线则更久），
    但此刻运行的就是 vX——不过滤的话徽标在更新完成后还会亮着几分钟，
    用户刚完成的动作看起来"没生效"。比较当前版本是纯本地判断，零成本。
    """
    state = await get_app_update_state()
    app_version = state.app_latest_version or None
    if app_version is not None and not _is_newer(app_version, __version__):
        app_version = None
    model_tag = state.model_latest_tag or None
    if model_tag is not None and _model_tag_num(model_tag) <= _model_tag_num(
        _current_model_tag()
    ):
        model_tag = None
    return PendingUpdateView(
        app_version=app_version,
        app_compatible=state.app_compatible if app_version else False,
        app_changelog=state.app_changelog if app_version else "",
        app_published_at=state.app_published_at if app_version else "",
        model_tag=model_tag,
        # 旧版本已经持久化的裸 ISO 值也在读取边界补成 UTC，无需数据迁移。
        checked_at=normalize_utc_iso(state.checked_at),
    )


# ---------------------------------------------------------------------------
# 定时自动检查（只提醒、绝不自动安装：更新会重启服务，须由用户主动触发）
# ---------------------------------------------------------------------------


@register_task(
    "check_app_update",
    title="检查应用更新",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=3600,
    description=(
        "每小时检查 GitHub 是否发布了新版本与新的 NER 模型；发现后在侧栏亮出更新"
        "入口（不会自动安装，更新在「设置 → 应用」里一键完成）。检查用 ETag 条件"
        "请求，无新版时不消耗 GitHub API 配额。仅 Docker 部署生效。"
    ),
)
async def check_app_update_task() -> None:
    if _runtime_version() is None:
        return  # 源码部署/开发环境不适用应用内更新
    # 应用与模型两条检查相互独立：一条网络失败不拖累另一条；
    # 网络不可达按「下轮再试」处理，保持上一次快照不动（离线时徽标不该乱跳）
    try:
        await check_update_now()
    except Exception as exc:
        # 兜住一切异常（含 GitHub 返回异常结构导致的解析错误）：
        # 定时检查失败只记日志、下轮再试，绝不让任务本身报故障打扰用户
        logger.info("定时检查应用更新未完成：%s", exc)
    try:
        await check_model_update_now()
    except Exception as exc:
        logger.info("定时检查模型更新未完成：%s", exc)


# ---------------------------------------------------------------------------
# 启动首查（lifespan 拉起）：NAS 用户重启容器/设备很常见，启动后几分钟就
# 检查一次，不用等下一个整点周期才第一次感知到新版
# ---------------------------------------------------------------------------

_STARTUP_CHECK_DELAY_SECONDS = 180
_startup_check_task: asyncio.Task | None = None


def start_startup_check() -> None:
    """排定启动后的更新首查（由 lifespan 在调度器启动后调用）。

    延迟几分钟再查：把启动窗口让给迁移、扫库等更要紧的初始化，也避免
    容器被 restart 策略反复拉起时高频打 GitHub。非 Docker 部署不排定。
    """
    global _startup_check_task
    if _runtime_version() is None:
        return
    _startup_check_task = asyncio.get_running_loop().create_task(_startup_check())


async def _startup_check() -> None:
    try:
        await asyncio.sleep(_STARTUP_CHECK_DELAY_SECONDS)
        await check_app_update_task()
    except asyncio.CancelledError:
        raise
    except Exception:
        # 首查失败无所谓：每小时的定时任务会继续兜着
        logger.info("启动后的更新首查未完成（定时任务会继续检查）", exc_info=True)


async def close_startup_check() -> None:
    """停机时取消未执行完的首查任务（lifespan 关闭阶段调用）。"""
    global _startup_check_task
    if _startup_check_task is not None:
        _startup_check_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _startup_check_task
        _startup_check_task = None


# ---------------------------------------------------------------------------
# 回退
# ---------------------------------------------------------------------------


def rollback() -> str:
    """回退到上一版本（current/previous 互换，可再切回）；无上一版本时
    删除 current 回落镜像基线。返回给用户的中文说明。"""
    if _runtime_version() is None:
        raise BadRequestException("当前部署形态不支持应用内回退（仅 Docker 镜像部署支持）")
    if _progress.busy():
        raise BadRequestException("已有更新正在进行中，无法回退")
    updates = _updates_dir()
    current = updates / "current"
    previous = updates / "previous"
    if current.exists() and not current.is_symlink():
        raise BadRequestException(
            "updates/current 不是符号链接（可能被手工改动过），"
            "请删除 data/updates/current 后重启容器回到镜像内置版本"
        )
    # 回退目标必须是 entrypoint 真实会采用的版本（runtime 匹配且未标坏）：
    # 否则用户会经历一次什么都没变的全量重启。不可用的 previous 视同不存在。
    if previous.is_symlink() and _overlay_state(Path(previous))[1]:
        prev_target = Path(os.readlink(previous))
        if current.is_symlink():
            cur_target = Path(os.readlink(current))
            _atomic_symlink(prev_target, current)
            _atomic_symlink(cur_target, previous)
        else:
            _atomic_symlink(prev_target, current)
            previous.unlink(missing_ok=True)
        message = "已切换到上一版本，应用正在重启……如需撤销可再次回退切回"
    elif current.is_symlink() and os.environ.get("MOVIECLAW_OVERLAY_VERSION"):
        current.unlink()
        message = "没有可用的更早版本，已回落到镜像内置版本，应用正在重启……"
    else:
        raise BadRequestException("当前运行的就是镜像内置版本，没有可回退的目标")
    logger.info("应用回退：%s", message)
    # 占住 busy 态：重启前的窗口（响应缓冲 + 优雅停机最长约 11 秒）内
    # 拒绝并发的更新/再回退请求，与更新线程置 restarting 的做法对称
    _progress.set("restarting", "正在回退并重启应用……", target_version=None)
    schedule_restart(FULL_RESTART_EXIT_CODE)
    return message


# ---------------------------------------------------------------------------
# 多版本回退选择器（数据兼容机制见 docs/design/in-app-update.md）
#
# 数据兼容的判定锚点：每次切换版本前的自动备份都带边车，记录「离开的版本 +
# 当时的 alembic 迁移位置」。回退到 vX 时把现库迁移位置与「离开 vX 时」的
# 记录比对——相同说明期间没跑过迁移，代码直接切、数据全保留（switch）；
# 不同说明数据结构已前进、旧代码不认识，需恢复那份备份并明示数据损失
# （restore）；没有可对时的备份则只能强警告（unknown）。
#
# 恢复的执行时机：回退后跑的是旧版本代码，不能指望它认识本机制——文件替换
# 必须由**当前进程**在优雅停机之后、退出重启之前完成（此时 DB 引擎已关闭，
# 纯文件操作），入口在 main.run 的停机尾声调 execute_pending_restore()。
# ---------------------------------------------------------------------------

# 待执行恢复的暂存文件（updates/state/ 下）；带时间戳，防陈旧暂存误触发
_RESTORE_PENDING_NAME = "restore-pending.json"
# 暂存的有效窗口：正常流程从暂存到停机只有几秒，超过说明中途被打断过
# （如恰好 docker stop），陈旧暂存绝不能在未来的某次重启里突然生效
_RESTORE_PENDING_TTL_SECONDS = 600


def _release_info_of(vdir: Path) -> dict:
    """读版本目录里的 Release 元信息；缺失/损坏返回空 dict（老版本目录没有）。"""
    path = vdir / _RELEASE_INFO_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _backup_sidecar(backup: Path) -> dict:
    """读备份边车；缺失/损坏返回空 dict（本机制之前的老备份没有边车）。"""
    sidecar = backup.with_name(backup.name + ".json")
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / name).stat().st_size
    return total


def _classify_target(
    updates: Path, version: str | None, live_rev: str | None
) -> tuple[str, str | None]:
    """判定回退到某版本的数据处理方式，返回 (schema_action, backup_taken_at)。"""
    if version is None:
        return "unknown", None  # 目标版本号都不知道（未记录的基线），无从对时
    backup = _latest_backup_for(updates, version)
    if backup is None:
        return "unknown", None
    sidecar = _backup_sidecar(backup)
    taken_at = sidecar.get("taken_at") or datetime.fromtimestamp(
        backup.stat().st_mtime
    ).astimezone().isoformat(timespec="seconds")
    rev = sidecar.get("alembic_rev")
    if rev and live_rev and rev == live_rev:
        # 离开该版本后没跑过任何迁移：直接切换代码即可，数据全保留
        return "switch", taken_at
    return "restore", taken_at


def _running_version_dir(updates: Path) -> Path | None:
    """当前运行版本的目录（跑基线时为 None）。"""
    running = os.environ.get("MOVIECLAW_OVERLAY_VERSION")
    if not running:
        return None
    vdir = updates / "versions" / f"v{_sanitize(running)}"
    return vdir if vdir.is_dir() else None


def _build_rollback_options(baseline_version: str, keep_versions: int) -> RollbackOptionsView:
    """装配回退选择器数据（同步、纯本地，在线程里跑——含目录体积统计）。"""
    updates = _updates_dir()
    versions_dir = updates / "versions"
    db_path = _db_path()
    live_rev = _read_alembic_rev(db_path) if db_path and db_path.is_file() else None
    running = os.environ.get("MOVIECLAW_OVERLAY_VERSION")
    targets: list[RollbackTargetView] = []
    total_bytes = 0
    dirs = sorted(
        (d for d in versions_dir.iterdir() if d.is_dir()) if versions_dir.is_dir() else [],
        key=cmp_to_key(
            lambda a, b: -1
            if _is_newer(a.name.removeprefix("v"), b.name.removeprefix("v"))
            else (1 if _is_newer(b.name.removeprefix("v"), a.name.removeprefix("v")) else 0)
        ),
    )
    for vdir in dirs:
        size = _dir_size(vdir)
        total_bytes += size
        version, usable, _reason = _overlay_state(vdir)
        # 不可用目录（清单损坏/runtime 不匹配/标坏）不进候选：列出一个
        # entrypoint 注定拒绝的目标只会制造无效的全量重启
        if not usable or not version or version == running:
            continue
        info = _release_info_of(vdir)
        schema_action, backup_taken_at = _classify_target(updates, version, live_rev)
        targets.append(
            RollbackTargetView(
                kind="version",
                version=version,
                changelog=info.get("changelog") or None,
                installed_at=info.get("installed_at") or None,
                size_bytes=size,
                schema_action=schema_action,
                backup_taken_at=backup_taken_at,
            )
        )
    # 镜像基线：只有 overlay 在运行时才是一个「回退」目标（跑基线时它就是现状）
    if running:
        schema_action, backup_taken_at = _classify_target(
            updates, baseline_version or None, live_rev
        )
        targets.append(
            RollbackTargetView(
                kind="baseline",
                version=baseline_version or None,
                changelog=None,
                installed_at=None,
                size_bytes=None,
                schema_action=schema_action,
                backup_taken_at=backup_taken_at,
            )
        )
    return RollbackOptionsView(
        targets=targets, versions_dir_bytes=total_bytes, keep_versions=keep_versions
    )


async def rollback_options() -> RollbackOptionsView:
    """回退选择器数据（候选版本 + 保留策略现状）。"""
    if _runtime_version() is None:
        raise BadRequestException("当前部署形态不支持应用内回退（仅 Docker 镜像部署支持）")
    state = await get_app_update_state()
    prefs = await get_app_update_prefs()
    return await asyncio.to_thread(
        _build_rollback_options, state.baseline_version, prefs.keep_versions
    )


def _stage_restore(updates: Path, backup: Path, target_label: str) -> None:
    """暂存一次数据库恢复，由 main.run 在停机尾声执行（见模块段注释）。"""
    state_dir = updates / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / _RESTORE_PENDING_NAME).write_text(
        json.dumps(
            {"backup": str(backup), "target": target_label, "requested_at": time.time()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def rollback_to(target: str, restore_backup: bool) -> str:
    """回退（切换）到指定版本或镜像基线，可选恢复对应时点的数据库备份。

    与旧版 ``rollback()`` 的 previous 互换不同，这里显式指定目标；previous
    指针指向当前运行版本——回退后还能一步切回来（配合回退前的保险备份，
    连数据都能一并撤销）。
    """
    if _runtime_version() is None:
        raise BadRequestException("当前部署形态不支持应用内回退（仅 Docker 镜像部署支持）")
    if _progress.busy():
        raise BadRequestException("已有更新正在进行中，无法回退")
    updates = _updates_dir()
    current = updates / "current"
    if current.exists() and not current.is_symlink():
        raise BadRequestException(
            "updates/current 不是符号链接（可能被手工改动过），"
            "请删除 data/updates/current 后重启容器回到镜像内置版本"
        )
    running = os.environ.get("MOVIECLAW_OVERLAY_VERSION")

    if target == "baseline":
        if not running:
            raise BadRequestException("当前运行的就是镜像内置版本，无需回退")
        target_dir: Path | None = None
        target_label = "镜像内置版本"
        # 恢复源按记录过的基线版本号对时；没记录过就找不到（选项接口已把
        # 这种情况标为 unknown，正常流程不会带着 restore_backup 走到这里）
        restore_version = None
    else:
        if target == running:
            raise BadRequestException(f"v{target} 正在运行，无需回退")
        target_dir = updates / "versions" / f"v{_sanitize(target)}"
        version, usable, reason = _overlay_state(target_dir)
        if not usable:
            raise BadRequestException(f"版本 v{target} 不可用：{reason}")
        if version != target:
            raise BadRequestException(f"版本目录 v{target} 的清单声明的是 v{version}，内容不匹配")
        target_label = f"v{target}"
        restore_version = target

    backup: Path | None = None
    if restore_backup:
        if target == "baseline":
            raise BadRequestException(
                "镜像内置版本没有可对时的自动备份，无法随回退恢复数据；"
                "如需恢复请在回退后从 data/updates/backup 手动恢复"
            )
        assert restore_version is not None
        backup = _latest_backup_for(updates, restore_version)
        if backup is None:
            raise BadRequestException(
                f"没有找到离开 v{restore_version} 时的自动备份，无法随回退恢复数据"
            )

    # 保险备份：记录当前版本此刻的数据（含边车）。它同时是将来「切回来」时
    # 的数据恢复源——让这次回退本身也可完整撤销
    _backup_database(updates)

    # 切换指针：previous ← 当前运行版本（回退的撤销路径），current ← 目标
    running_dir = _running_version_dir(updates)
    if target_dir is not None:
        _atomic_symlink(target_dir, current)
    else:
        current.unlink(missing_ok=True)
    if running_dir is not None:
        _atomic_symlink(running_dir, updates / "previous")

    if backup is not None:
        _stage_restore(updates, backup, target_label)
        message = f"正在回退到{target_label}并恢复对应时点的数据备份，应用即将重启……"
    else:
        message = f"正在回退到{target_label}，应用即将重启……"
    logger.info("应用回退：%s", message)
    _progress.set("restarting", message, target_version=None)
    schedule_restart(FULL_RESTART_EXIT_CODE)
    return message


def execute_pending_restore() -> None:
    """执行暂存的数据库恢复（main.run 在优雅停机后、进程退出前调用）。

    此时 uvicorn 已停、lifespan 已收尾、DB 引擎已关闭——纯文件操作是安全的。
    任何异常只记日志不阻断退出：恢复失败的后果是旧版本对着新结构的库启动
    （可能再次失败触发 entrypoint 的坏版本回落），数据本身始终无损。
    """
    updates = _updates_dir()
    pending = updates / "state" / _RESTORE_PENDING_NAME
    if not pending.is_file():
        return
    try:
        data = json.loads(pending.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    finally:
        pending.unlink(missing_ok=True)  # 先消费后执行：中途崩溃也不会反复触发
    requested_at = float(data.get("requested_at") or 0)
    if time.time() - requested_at > _RESTORE_PENDING_TTL_SECONDS:
        logger.warning("忽略陈旧的数据库恢复暂存（超过 %d 秒）", _RESTORE_PENDING_TTL_SECONDS)
        return
    backup = Path(str(data.get("backup") or ""))
    db_path = _db_path()
    if db_path is None or not backup.is_file() or not db_path.is_file():
        logger.warning("数据库恢复暂存无法执行（备份或数据库文件缺失），已跳过")
        return
    try:
        # 换库前把现库再归档一份（sqlite backup API，连未 checkpoint 的 WAL
        # 一起收干净）——恢复操作自身也绝不能是数据的单向门
        _backup_database(updates)
        tmp = db_path.with_name(db_path.name + ".restore-tmp")
        shutil.copyfile(backup, tmp)
        os.replace(tmp, db_path)
        # 旧的 WAL/SHM 属于被换掉的库，留着会被 SQLite 误认为配套文件
        db_path.with_name(db_path.name + "-wal").unlink(missing_ok=True)
        db_path.with_name(db_path.name + "-shm").unlink(missing_ok=True)
        logger.info("已恢复数据库备份（%s → 回退到%s）", backup.name, data.get("target"))
    except Exception:
        logger.exception("数据库恢复失败（数据未受损，现库保持原样）")


async def save_update_retention(keep_versions: int) -> int:
    """保存本地版本保留数并立即按新策略清理，返回生效值。"""
    prefs = await get_app_update_prefs()
    prefs.keep_versions = keep_versions
    await save_app_update_prefs(prefs)
    # 立即生效：调小保留数时马上释放磁盘，而不是等下一次更新才清
    await asyncio.to_thread(_prune_versions, _updates_dir(), keep_versions=keep_versions)
    return keep_versions


async def record_baseline_version() -> None:
    """跑镜像基线时记下版本号（lifespan 启动时调用，仅变化时写库）。

    overlay 运行期读不到基线代码的版本号，回退列表靠这份记录向用户明示
    「回落镜像内置版本 = 回到 v 几」。
    """
    if os.environ.get("MOVIECLAW_CODE_SOURCE") != "baseline":
        return
    state = await get_app_update_state()
    if state.baseline_version == __version__:
        return
    state.baseline_version = __version__
    await save_app_update_state(state)


# ---------------------------------------------------------------------------
# 启动清场：镜像升级后残留的陈旧 overlay
# ---------------------------------------------------------------------------


def _prune_stale_overlays(updates: Path, runtime: int) -> list[str]:
    """删除 requires_runtime 低于 ``runtime`` 的 overlay，返回清掉的版本号。

    安装时 requires_runtime 必等于当时镜像的 runtime（见 start_update 的
    前置校验），所以「低于」只可能是升级镜像前装的残留：entrypoint 已回落
    基线且永远不会再采用它们，留着只会让状态页长期显示「已安装但未在运行」。
    指针、版本目录、bad/failures 标记一并清理；数据库备份**不动**——它们
    仍是跨镜像升级的最后恢复点，交由保留数清理按既有策略收敛。requires
    高于 runtime 的版本保留：那是镜像被降级的场景，升回新镜像即可生效，
    状态页的「需升级镜像」提示对它是准确的。
    """
    versions_dir = updates / "versions"
    stale: dict[str, str] = {}  # 目录名 → 清单里的版本号
    if versions_dir.is_dir():
        for vdir in sorted(versions_dir.iterdir()):
            manifest_path = vdir / _MANIFEST_NAME
            if not vdir.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_bytes())
                version = str(manifest["version"])
                requires = int(manifest["requires_runtime"])
            except Exception:
                continue  # 清单损坏是另一类问题（保留数清理会收敛），不在此误删
            if requires < runtime:
                stale[vdir.name] = version
    if not stale:
        return []
    # 先摘指针再删目录：中途失败也不会留下指向半删除目录的链接
    for link in (updates / "current", updates / "previous"):
        if link.is_symlink() and Path(os.readlink(link)).name in stale:
            link.unlink(missing_ok=True)
    for name in stale:
        shutil.rmtree(versions_dir / name, ignore_errors=True)
    # bad/failures 标记跟着目录走（与 _prune_versions 同一口径）
    state_dir = updates / "state"
    if state_dir.is_dir():
        for marker in list(state_dir.glob("bad-*")) + list(state_dir.glob("failures-*")):
            if f"v{marker.name.split('-', 1)[1]}" in stale:
                marker.unlink(missing_ok=True)
    return sorted(stale.values())


async def prune_stale_overlays() -> None:
    """清理镜像升级后已无法生效的残留 overlay（lifespan 启动时调用，幂等）。

    非 Docker 部署没有 overlay 机制，直接跳过；清理失败只记日志，绝不阻断启动。
    """
    runtime = _runtime_version()
    if runtime is None:
        return
    try:
        removed = await asyncio.to_thread(_prune_stale_overlays, _updates_dir(), runtime)
    except Exception:
        logger.warning("清理陈旧更新版本失败（不影响启动）", exc_info=True)
        return
    if removed:
        logger.info(
            "已清理镜像升级前残留的更新版本 %s（当前镜像 runtime=%d，它们已无法生效）",
            "、".join(f"v{v}" for v in removed),
            runtime,
        )
