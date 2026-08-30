"""远程转码 Worker 配置的运行时装配。

源视频与产物上传的根地址只有两层：

1. ``RemoteTranscodeSetting.base_url``：网页「高级」里的覆盖项，通常为空；
2. 为空时留空，由 ``session.py`` 改用**接单 Worker 自己连上来的地址**
   （``WorkerConnection.observed_base_url``）。

第 2 层是默认路径：Worker 的控制连接本来就是从某个地址打进来的，那个地址
必然是它够得着的，不需要任何人去填，所以「地址没配」不再是启用远程转码的
阻塞条件。

这里**刻意不再回退到系统外部访问地址**。那个地址是「用户从外面怎么访问这个
应用」，常常是公网域名或反向代理；拿它去下发给一台明明在同一个局域网里、
刚从内网地址连进来的 Worker，会把大量视频分片绕出去再绕回来，明显更慢。
旧版正是因为这样，才需要在网页上再填一个「专用地址」把它扳回内网——那个
输入框解决的问题，本来就是上一层自己制造的。

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
    RemoteTranscodeSetting,
)
from movieclaw_api.settings.store import get_setting_store


@dataclass(frozen=True, slots=True)
class RemoteTranscodeRuntimeConfig:
    """供同步播放/Worker 代码读取的远程转码有效配置快照。"""

    enabled: bool
    base_url: str
    base_url_source: RemoteTranscodeBaseUrlSource
    max_artifact_bytes: int

    @property
    def ready(self) -> bool:
        """开关是否打开，且填过的地址（如果填了）合法。

        地址留空即就绪：默认走 Worker 自报的连接地址，没有可填的前置条件。
        令牌也不在这里判：它是逐台设备配对签发的，「有没有 Worker 连着」是
        运行时状态而不是配置前置条件（docs/design/device-auth.md §5.4）。
        """
        return self.enabled and not remote_transcode_issues(self)


_current: RemoteTranscodeRuntimeConfig | None = None


def _normalize_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def remote_transcode_issues(config: RemoteTranscodeRuntimeConfig) -> list[str]:
    """返回启用远程转码所缺少的前置条件，内容可直接展示给管理员。

    地址**留空不是问题**：那是默认路径，运行时会用接单 Worker 自己连上来的
    地址。这里只校验「填了但填错」——填了个连不上的地址比没填更危险，因为
    它会静默覆盖掉那个一定可用的推断值。
    """
    issues: list[str] = []
    if not config.base_url.strip():
        return issues
    try:
        parsed = urlsplit(config.base_url)
    except ValueError:
        parsed = None
    if parsed is None or not parsed.netloc:
        # 漏写 scheme 的地址（如 192.168.1.10:3000）会被 urlsplit 解析成空
        # netloc；用户明明填了却被告知「未配置」会让人完全找不到方向。
        issues.append(
            "远程转码覆盖地址必须以 http:// 或 https:// 开头，"
            f"当前填写的是「{config.base_url.strip()}」"
        )
    elif parsed.scheme not in {"http", "https"}:
        issues.append("远程转码覆盖地址必须使用 HTTP 或 HTTPS")
    elif parsed.username or parsed.password:
        issues.append("远程转码覆盖地址不能包含账号或密码")
    elif parsed.query or parsed.fragment:
        issues.append("远程转码覆盖地址不能包含查询参数或片段")
    return issues


def _runtime_from_setting(
    setting: RemoteTranscodeSetting,
) -> RemoteTranscodeRuntimeConfig:
    """把数据库配置装成运行时快照。"""
    base_url = _normalize_base_url(setting.base_url)
    # 留空 = 自动：运行时用 Worker 连上来的地址，这里没有可展示的静态值。
    source = "remote_transcode_setting" if base_url else "worker_connection"
    return RemoteTranscodeRuntimeConfig(
        enabled=setting.enabled,
        base_url=base_url,
        base_url_source=source,
        # 旧版本允许管理员填入超过 Worker 内存代理能力的值。读取时先
        # 截到统一上限，避免升级后配置加载失败，同时保证实际接收上限
        # 不会超过 Worker 能处理的 512 MiB；新保存请求由 schema 直接拒绝超限。
        max_artifact_bytes=min(
            setting.max_artifact_bytes, MAX_REMOTE_TRANSCODE_ARTIFACT_BYTES
        ),
    )


def _apply(setting: RemoteTranscodeSetting) -> RemoteTranscodeRuntimeConfig:
    global _current
    _current = _runtime_from_setting(setting)
    return _current


async def load_remote_transcode_config() -> RemoteTranscodeRuntimeConfig:
    """启动时加载网页配置并建立进程内快照。"""
    setting = await get_setting_store().get(RemoteTranscodeSetting)
    return _apply(setting)


def current_remote_transcode_config() -> RemoteTranscodeRuntimeConfig | None:
    """读取已加载的配置快照；生命周期之前返回 ``None``。"""
    return _current


def effective_remote_transcode_config() -> RemoteTranscodeRuntimeConfig:
    """读取当前有效配置；生命周期之前使用默认的关闭配置。"""
    if _current is not None:
        return _current
    return _runtime_from_setting(RemoteTranscodeSetting())


async def build_remote_transcode_config_view() -> RemoteTranscodeConfigView:
    """构造脱敏的网页配置视图。"""
    await load_remote_transcode_config()
    setting = await get_setting_store().get(RemoteTranscodeSetting)
    runtime = effective_remote_transcode_config()
    return RemoteTranscodeConfigView(
        enabled=setting.enabled,
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
    base_url = (
        current.base_url
        if payload.base_url is None
        else _normalize_base_url(payload.base_url)
    )
    next_setting = RemoteTranscodeSetting(
        enabled=payload.enabled,
        base_url=base_url,
        max_artifact_bytes=payload.max_artifact_bytes,
    )
    runtime = _runtime_from_setting(next_setting)
    issues = remote_transcode_issues(runtime)
    if next_setting.enabled and issues:
        raise BadRequestException("无法启用远程转码：" + "；".join(issues))

    previous = effective_remote_transcode_config()
    await store.set(next_setting)
    _apply(next_setting)
    if (
        previous.enabled != runtime.enabled
        or previous.base_url != runtime.base_url
    ):
        # 修改地址或关闭功能时，旧 WebSocket 不能继续持有旧配置；断开后
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
    "remote_transcode_issues",
    "reset_remote_transcode_config",
    "save_remote_transcode_config",
]
