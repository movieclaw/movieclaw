"""远程转码 Worker 配置的运行时装配。

配置来源分两层：

1. ``RemoteTranscodeSetting`` 保存网页修改后的开关、令牌、专用根地址和上传上限；
2. 远程转码专用根地址为空时，使用 ``AppServerSetting.external_url``。

播放决策和 WebSocket 鉴权都是同步函数，而配置存储是异步接口，因此这里在
应用启动/保存时维护一份进程内快照。快照更新后无需重启即可让所有消费者看到
同一份有效配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.transcode_worker import (
    RemoteTranscodeBaseUrlSource,
    RemoteTranscodeConfigPayload,
    RemoteTranscodeConfigView,
)
from movieclaw_api.settings import (
    MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES,
    AppServerSetting,
    RemoteTranscodeSetting,
)
from movieclaw_api.settings.store import get_setting_store


@dataclass(frozen=True, slots=True)
class RemoteTranscodeRuntimeConfig:
    """供同步播放/Worker 代码读取的远程转码有效配置快照。"""

    enabled: bool
    worker_token: str
    base_url: str
    base_url_source: RemoteTranscodeBaseUrlSource
    max_artifact_bytes: int

    @property
    def ready(self) -> bool:
        """开关、令牌和合法的 HTTP(S) 根地址是否全部就绪。"""
        return self.enabled and not remote_transcode_issues(self)


_current: RemoteTranscodeRuntimeConfig | None = None


def _normalize_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def remote_transcode_issues(config: RemoteTranscodeRuntimeConfig) -> list[str]:
    """返回启用远程转码所缺少的前置条件，内容可直接展示给管理员。"""
    issues: list[str] = []
    if not config.worker_token:
        issues.append("Worker 令牌未配置")
    try:
        parsed = urlsplit(config.base_url)
    except ValueError:
        parsed = None
    if parsed is None or not parsed.netloc:
        # 漏写 scheme 的地址（如 192.168.1.10:3000）会被 urlsplit 解析成空
        # netloc，跟真的没填一模一样。用户明明填了却被告知「未配置」会让人
        # 完全找不到方向，所以这里把两种情况分开说。
        if config.base_url.strip():
            issues.append(
                "远程转码外部访问地址必须以 http:// 或 https:// 开头，"
                f"当前填写的是「{config.base_url.strip()}」"
            )
        else:
            issues.append("远程转码外部访问地址未配置")
    elif parsed.scheme not in {"http", "https"}:
        issues.append("远程转码外部访问地址必须使用 HTTP 或 HTTPS")
    elif parsed.username or parsed.password:
        issues.append("远程转码外部访问地址不能包含账号或密码")
    elif parsed.query or parsed.fragment:
        issues.append("远程转码外部访问地址不能包含查询参数或片段")
    return issues


def _runtime_from_setting(
    setting: RemoteTranscodeSetting,
    *,
    external_url: str,
) -> RemoteTranscodeRuntimeConfig:
    """将数据库配置与系统外部地址合并成运行时快照。"""
    normalized_override = _normalize_base_url(setting.base_url)
    normalized_external = _normalize_base_url(external_url)
    if normalized_override:
        base_url = normalized_override
        source = "remote_transcode_setting"
    elif normalized_external:
        base_url = normalized_external
        source = "system_external_url"
    else:
        base_url = ""
        source = "unset"
    return RemoteTranscodeRuntimeConfig(
        enabled=setting.enabled,
        worker_token=setting.worker_token,
        base_url=base_url,
        base_url_source=source,
        # 旧版本允许管理员填入超过 Worker 内存代理能力的值。读取时先
        # 截到统一上限，避免升级后配置加载失败，同时保证实际接收上限
        # 不会超过 Worker 能处理的 512 MiB；新保存请求由 schema 直接拒绝超限。
        max_artifact_bytes=min(
            setting.max_artifact_bytes, MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES
        ),
    )


def _apply(
    setting: RemoteTranscodeSetting,
    *,
    external_url: str,
) -> RemoteTranscodeRuntimeConfig:
    global _current
    _current = _runtime_from_setting(
        setting,
        external_url=external_url,
    )
    return _current


async def load_remote_transcode_config() -> RemoteTranscodeRuntimeConfig:
    """启动时加载网页配置并建立进程内快照。"""
    store = get_setting_store()
    setting = await store.get(RemoteTranscodeSetting)
    app_setting = await store.get(AppServerSetting)
    return _apply(
        setting,
        external_url=app_setting.external_url,
    )


async def refresh_remote_transcode_config() -> RemoteTranscodeRuntimeConfig:
    """外部访问地址变化后重新装配快照，不改变远程转码配置本体。"""
    store = get_setting_store()
    setting = await store.get(RemoteTranscodeSetting)
    app_setting = await store.get(AppServerSetting)
    previous = _current
    runtime = _apply(
        setting,
        external_url=app_setting.external_url,
    )
    if previous is not None and previous.base_url != runtime.base_url:
        # 地址变化后，旧连接上的任务可能仍在使用旧的源/产物 URL；断开连接让
        # Worker 清理旧任务并按新地址重新握手，避免两套入口交叉写入。
        from movieclaw_api.services.playback.remote_worker import get_remote_worker_registry

        await get_remote_worker_registry().disconnect_all("远程转码地址已变更")
    return runtime


def current_remote_transcode_config() -> RemoteTranscodeRuntimeConfig | None:
    """读取已加载的配置快照；生命周期之前返回 ``None``。"""
    return _current


def effective_remote_transcode_config() -> RemoteTranscodeRuntimeConfig:
    """读取当前有效配置；生命周期之前使用默认的关闭配置。"""
    if _current is not None:
        return _current
    return _runtime_from_setting(RemoteTranscodeSetting(), external_url="")


async def build_remote_transcode_config_view() -> RemoteTranscodeConfigView:
    """构造脱敏的网页配置视图。"""
    await load_remote_transcode_config()
    setting = await get_setting_store().get(RemoteTranscodeSetting)
    runtime = effective_remote_transcode_config()
    return RemoteTranscodeConfigView(
        enabled=setting.enabled,
        worker_token_configured=bool(runtime.worker_token),
        base_url=runtime.base_url,
        base_url_override=_normalize_base_url(setting.base_url),
        base_url_source=runtime.base_url_source,
        max_artifact_bytes=runtime.max_artifact_bytes,
        ready=runtime.ready,
        issues=remote_transcode_issues(runtime),
    )


async def save_remote_transcode_config(
    payload: RemoteTranscodeConfigPayload,
) -> RemoteTranscodeConfigView:
    """保存网页配置并立即刷新运行时快照。"""
    await load_remote_transcode_config()
    store = get_setting_store()
    current = await store.get(RemoteTranscodeSetting)
    worker_token = (
        current.worker_token
        if payload.worker_token is None
        else payload.worker_token.strip()
    )
    base_url = (
        current.base_url
        if payload.base_url is None
        else _normalize_base_url(payload.base_url)
    )
    next_setting = RemoteTranscodeSetting(
        enabled=payload.enabled,
        worker_token=worker_token,
        base_url=base_url,
        max_artifact_bytes=payload.max_artifact_bytes,
    )
    app_setting = await store.get(AppServerSetting)
    runtime = _runtime_from_setting(
        next_setting,
        external_url=app_setting.external_url,
    )
    issues = remote_transcode_issues(runtime)
    if next_setting.enabled and issues:
        raise BadRequestException("无法启用远程转码：" + "；".join(issues))

    previous = effective_remote_transcode_config()
    await store.set(next_setting)
    _apply(
        next_setting,
        external_url=app_setting.external_url,
    )
    if (
        previous.enabled != runtime.enabled
        or previous.worker_token != runtime.worker_token
        or previous.base_url != runtime.base_url
    ):
        # 修改令牌、地址或关闭功能时，旧 WebSocket 不能继续持有旧配置；断开后
        # 远程 Worker 会按既有重连逻辑重新握手。局部导入避免配置服务与注册表循环依赖。
        from movieclaw_api.services.playback.remote_worker import get_remote_worker_registry

        await get_remote_worker_registry().disconnect_all("远程转码配置已变更")
    return await build_remote_transcode_config_view()


def reset_remote_transcode_config() -> None:
    """测试用：清除进程内快照。"""
    global _current
    _current = None


__all__ = [
    "RemoteTranscodeRuntimeConfig",
    "build_remote_transcode_config_view",
    "current_remote_transcode_config",
    "effective_remote_transcode_config",
    "load_remote_transcode_config",
    "refresh_remote_transcode_config",
    "remote_transcode_issues",
    "reset_remote_transcode_config",
    "save_remote_transcode_config",
]
