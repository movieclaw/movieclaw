"""应用内更新的接口模型（docs/design/in-app-update.md M3）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LastAbnormalExitView(BaseModel):
    """上一次容器异常退出的记录（entrypoint 落盘 updates/state/last-exit.json）。

    容器被 Docker 自动拉起后，用户需要知道曾发生过什么——无人值守的自愈
    不能悄无声息。正常停机与设置页/更新触发的重启不会产生该记录。
    """

    #: 异常退出的时间（Unix epoch 秒；前端按本地时区格式化展示）
    at: int
    #: 机器可读的原因分类：startup_failure / api_crash / web_crash /
    #: watchdog_unhealthy / supervisor_error
    reason: str
    #: 容器当时的退出码
    exit_code: int
    #: 给用户看的中文说明
    detail: str


class UpdateStatusView(BaseModel):
    """「设置 → 关于与更新」的状态区。"""

    #: 当前运行的应用版本（代码自带的 __version__，overlay 与基线各有各的值）
    current_version: str
    #: 代码来源：baseline（镜像基线）/ overlay（应用内更新版本）/ dev（源码部署）
    code_source: str
    #: overlay 生效时的版本号；基线/开发环境为 None
    overlay_version: str | None
    #: 镜像的运行时版本（依赖集合代号）；非 Docker 部署为 None
    runtime_version: int | None
    #: 是否支持应用内更新（只有 Docker entrypoint 环境才支持；源码部署请 git pull）
    can_update: bool
    #: 是否存在可回退的上一版本
    has_previous: bool
    #: 可回退的上一版本号（has_previous 为 True 时非空）；UI 用它向用户明示
    #: 回退会落在哪个版本上，而不是含糊的「上一版本」
    previous_version: str | None
    #: 被标记为「连续启动失败」的坏版本列表（供 UI 外显兜底事件）
    bad_versions: list[str]
    #: 当前生效的 NER 模型 Release tag（如 torrent-ner-v1）；无法识别时为 None
    model_tag: str | None
    #: 盘上 current 指向、但**并未在运行**的 overlay 版本（runtime 不匹配 /
    #: 坏版本回落 / 等待重启）；一切正常时为 None
    inactive_overlay_version: str | None
    #: 上述版本未生效的中文原因
    inactive_overlay_reason: str | None
    #: 近 7 天内最近一次容器异常退出并被自动拉起的记录；没有则为 None
    last_abnormal_exit: LastAbnormalExitView | None


class UpdateCheckView(BaseModel):
    """「检查更新」的结果。"""

    current_version: str
    latest_version: str
    #: 最新版是否比当前新
    update_available: bool
    #: 最新版的 requires_runtime 是否与本镜像匹配；False = 需升级 Docker 镜像
    compatible: bool
    #: 最新版要求的运行时版本
    requires_runtime: int
    #: Release 页的更新说明（GitHub Release body，Markdown）
    changelog: str
    #: Release 发布时间（ISO 8601）
    published_at: str
    #: 最新版曾在本机连续启动失败被回落（重新安装会清除标记再试，需用户知情）
    latest_known_bad: bool


class ModelUpdateCheckView(BaseModel):
    """「检查模型更新」的结果（NER 模型独立于代码更新，见设计文档）。"""

    #: 当前生效的模型 Release tag；无法识别（老镜像无 tag 记录）时为 None
    current_tag: str | None
    latest_tag: str
    update_available: bool
    #: 最新模型 Release 是否携带更新清单（没带则无法应用内安装，需升级镜像获取）
    installable: bool
    published_at: str


class PendingUpdateView(BaseModel):
    """待更新快照：最近一次检查（定时或手动）留下的结论，读它不触网。

    前端的更新提醒完全建立在这个视图上——侧栏常驻徽标据此点亮，「设置 →
    应用」进页即用它直接渲染出新版本卡片，用户不必再手点一次「检查更新」。
    """

    #: 可更新的应用版本号；None = 无可用更新（含从未检查过）
    app_version: str | None
    #: False = 该版本含依赖变化，需重拉 Docker 镜像，应用内更新按钮不可用
    app_compatible: bool
    #: 该版本的 Release 说明（Markdown 原文）
    app_changelog: str
    #: 该版本的发布时间（ISO 8601）
    app_published_at: str
    #: 可更新的 NER 模型 Release tag；None = 无可用更新
    model_tag: str | None
    #: 最近一次检查完成时间（ISO 8601）；None = 从未检查过
    checked_at: str | None


class RollbackTargetView(BaseModel):
    """一个可回退（或切换）的目标版本。"""

    #: baseline = Docker 镜像内置版本；version = 本地保留的历史版本目录
    kind: str
    #: 目标版本号；镜像基线未曾记录版本号时为 None（只能显示「镜像内置版本」）
    version: str | None
    #: 该版本的 Release 更新说明（安装时随版本目录落盘）；早期安装的版本可能没有
    changelog: str | None
    #: 该版本的安装时间（ISO 8601）；没有记录时为 None
    installed_at: str | None
    #: 版本目录占用的磁盘字节数（baseline 为 None——它在镜像里，不占 data 卷）
    size_bytes: int | None
    #: 数据兼容判定，决定回退时对数据库怎么处理：
    #:   switch  —— 数据结构与当前一致，直接切换代码，数据原样保留；
    #:   restore —— 当前数据结构比目标新（跨过了迁移），需恢复离开该版本时的
    #:              自动备份，此后产生的数据会丢失（UI 必须明示并让用户确认）；
    #:   unknown —— 没有可对时的备份，切换后数据行为不保证（UI 强警告）
    schema_action: str
    #: schema_action=restore 时，将被恢复的备份的备份时间（ISO 8601）
    backup_taken_at: str | None


class RollbackOptionsView(BaseModel):
    """回退选择器的数据：候选版本列表 + 保留策略现状。"""

    #: 候选目标，新版本在前；不含当前正在运行的版本
    targets: list[RollbackTargetView]
    #: versions/ 目录的总磁盘占用（字节），给保留数设置行展示
    versions_dir_bytes: int
    #: 当前的本地保留版本数设置
    keep_versions: int


class RollbackPayload(BaseModel):
    """执行回退的请求体。空 target 走旧语义（切回上一版本/基线）。"""

    #: 目标：具体版本号（如 "0.2.0"）或 "baseline"（镜像内置版本）；
    #: None = 旧版兼容语义（previous 互换，无 previous 回落基线）
    target: str | None = None
    #: 是否同时恢复离开目标版本时的自动数据库备份（跨迁移回退时必须；
    #: 数据结构未变时应传 False 以保留全部现有数据）
    restore_backup: bool = False


class UpdateRetentionPayload(BaseModel):
    """保存本地版本保留数的请求体。"""

    keep_versions: int = Field(ge=2, le=20, description="本地保留的版本目录数（含当前版本）")


class UpdateProgressView(BaseModel):
    """更新执行进度（前端轮询）。"""

    #: idle / checking / downloading / verifying / applying / restarting / failed
    phase: str
    #: 当前步骤的中文描述（直接展示给用户）
    detail: str
    #: 下载进度百分比（仅 downloading 阶段有值）
    percent: float | None
    #: 失败时的中文错误信息
    error: str | None
    #: 本次更新的目标版本
    target_version: str | None
