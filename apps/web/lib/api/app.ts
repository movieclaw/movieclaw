import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

/** 保存请求体（见 routes/app_config.AppConfigPayload）。 */
export interface AppConfigPayload {
  /** 网络可访问到本应用的完整地址（http/https）；空 = 未配置 */
  external_url: string;
}

/** 读取响应：当前与保存请求体同构。 */
export type AppConfigView = AppConfigPayload;

/** 读取应用设置。 */
export function getAppConfig(): Promise<AppConfigView> {
  return unwrap(request<ApiEnvelope<AppConfigView>>("/app/config"));
}

/** 保存应用设置（保存即生效）。 */
export function saveAppConfig(payload: AppConfigPayload): Promise<AppConfigView> {
  return unwrap(
    request<ApiEnvelope<AppConfigView>>("/app/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 请求重启应用：后端优雅停机后由 Docker 等进程守护拉起。 */
export function restartApp(): Promise<null> {
  return unwrap(request<ApiEnvelope<null>>("/app/restart", { method: "POST" }));
}

// ---------------------------------------------------------------------------
// 应用内更新（设置 → 应用，见 routes/app_update）
// ---------------------------------------------------------------------------

/** 上一次容器异常退出的记录（entrypoint 落盘；近 7 天内才返回）。 */
export interface LastAbnormalExitView {
  /** 异常退出时间（Unix epoch 秒，按本地时区格式化展示） */
  at: number;
  /** 原因分类：startup_failure / api_crash / web_crash / watchdog_unhealthy / supervisor_error */
  reason: string;
  /** 容器当时的退出码 */
  exit_code: number;
  /** 中文说明 */
  detail: string;
}

/** 版本与更新能力状态（GET /app/update/status）。 */
export interface UpdateStatusView {
  /** 当前运行的应用版本 */
  current_version: string;
  /** 代码来源：baseline（镜像内置）/ overlay（应用内更新版本）/ dev（源码部署） */
  code_source: "baseline" | "overlay" | "dev" | string;
  /** overlay 生效时的版本号 */
  overlay_version: string | null;
  /** 镜像运行时版本（依赖集合代号）；非 Docker 部署为 null */
  runtime_version: number | null;
  /** 是否支持应用内更新（仅 Docker 部署） */
  can_update: boolean;
  /** 是否存在可回退的上一版本 */
  has_previous: boolean;
  /** 可回退的上一版本号（has_previous 时非空）；回退 UI 用它明示落点 */
  previous_version: string | null;
  /** 曾连续启动失败被自动回落的坏版本列表 */
  bad_versions: string[];
  /** 当前生效的 NER 模型 Release tag（如 torrent-ner-v1）；无法识别时为 null */
  model_tag: string | null;
  /** 盘上已安装但未在运行的 overlay 版本（runtime 不匹配/坏版本回落/等待重启） */
  inactive_overlay_version: string | null;
  /** 上述版本未生效的中文原因 */
  inactive_overlay_reason: string | null;
  /** 近 7 天内最近一次容器异常退出并被自动拉起的记录；没有则为 null */
  last_abnormal_exit: LastAbnormalExitView | null;
}

/** 检查更新的结果（POST /app/update/check）。 */
export interface UpdateCheckView {
  current_version: string;
  latest_version: string;
  update_available: boolean;
  /** false = 新版本包含依赖变化，需升级 Docker 镜像 */
  compatible: boolean;
  requires_runtime: number;
  /** GitHub Release 的更新说明（Markdown 文本） */
  changelog: string;
  published_at: string;
  /** 最新版曾在本机连续启动失败被自动回落（重装会清除标记再试一次） */
  latest_known_bad: boolean;
}

/** 更新执行进度（GET /app/update/progress）。 */
export interface UpdateProgressView {
  phase:
    | "idle"
    | "checking"
    | "downloading"
    | "verifying"
    | "applying"
    | "restarting"
    | "failed"
    | string;
  detail: string;
  percent: number | null;
  error: string | null;
  target_version: string | null;
}

/**
 * 待更新快照（GET /app/update/pending）：最近一次检查（每小时的定时任务或用户
 * 手动点「检查更新」）留下的结论，读它不触网。
 *
 * 更新提醒的全部界面都建立在它之上：侧栏常驻的更新入口据此点亮，「设置 → 应用」
 * 进页即用它直接渲染出新版本卡片（用户不必再手点一次「检查更新」）。
 */
export interface PendingUpdateView {
  /** 可更新的应用版本号；null = 无可用更新（含从未检查过） */
  app_version: string | null;
  /** false = 该版本含依赖变化，需重拉 Docker 镜像，应用内更新不可用 */
  app_compatible: boolean;
  /** 该版本的 Release 说明（Markdown 原文） */
  app_changelog: string;
  app_published_at: string;
  /** 可更新的 NER 模型 Release tag；null = 无可用更新 */
  model_tag: string | null;
  /** 最近一次检查完成时间；null = 从未检查过 */
  checked_at: string | null;
}

export function getPendingUpdate(): Promise<PendingUpdateView> {
  return unwrap(request<ApiEnvelope<PendingUpdateView>>("/app/update/pending"));
}

export function getUpdateStatus(): Promise<UpdateStatusView> {
  return unwrap(request<ApiEnvelope<UpdateStatusView>>("/app/update/status"));
}

export function checkUpdate(): Promise<UpdateCheckView> {
  return unwrap(request<ApiEnvelope<UpdateCheckView>>("/app/update/check", { method: "POST" }));
}

export function applyUpdate(): Promise<UpdateProgressView> {
  return unwrap(request<ApiEnvelope<UpdateProgressView>>("/app/update/apply", { method: "POST" }));
}

export function getUpdateProgress(): Promise<UpdateProgressView> {
  return unwrap(request<ApiEnvelope<UpdateProgressView>>("/app/update/progress"));
}

/** 一个可回退（或切换）的目标版本（GET /app/update/rollback/options）。 */
export interface RollbackTargetView {
  /** baseline = Docker 镜像内置版本；version = 本地保留的历史版本 */
  kind: "version" | "baseline" | string;
  /** 目标版本号；镜像基线未曾记录版本号时为 null */
  version: string | null;
  /** 该版本的 Release 更新说明（Markdown）；早期安装的版本可能没有 */
  changelog: string | null;
  installed_at: string | null;
  /** 版本目录磁盘占用（字节）；baseline 为 null */
  size_bytes: number | null;
  /**
   * 数据兼容判定：
   *   switch  —— 数据结构一致，直接切换，数据全保留；
   *   restore —— 跨过了数据库升级，需恢复对应时点的自动备份（此后数据丢失）；
   *   unknown —— 无可对时的备份，切换后数据行为不保证
   */
  schema_action: "switch" | "restore" | "unknown" | string;
  /** schema_action=restore 时，将被恢复的备份的备份时间 */
  backup_taken_at: string | null;
}

export interface RollbackOptionsView {
  /** 候选目标，新版本在前；不含当前正在运行的版本 */
  targets: RollbackTargetView[];
  /** versions/ 目录总磁盘占用（字节） */
  versions_dir_bytes: number;
  /** 当前的本地保留版本数设置 */
  keep_versions: number;
}

export function getRollbackOptions(): Promise<RollbackOptionsView> {
  return unwrap(
    request<ApiEnvelope<RollbackOptionsView>>("/app/update/rollback/options"),
  );
}

/** 回退到指定版本（"baseline" = 镜像内置版本）；返回后端的中文结果说明。 */
export async function rollbackTo(target: string, restoreBackup: boolean): Promise<string> {
  const envelope = await request<ApiEnvelope<null>>("/app/update/rollback", {
    method: "POST",
    body: JSON.stringify({ target, restore_backup: restoreBackup }),
  });
  return envelope.message;
}

/** 设置本地保留的版本目录数（立即按新策略清理）。 */
export async function saveUpdateRetention(keepVersions: number): Promise<void> {
  await request<ApiEnvelope<null>>("/app/update/retention", {
    method: "PUT",
    body: JSON.stringify({ keep_versions: keepVersions }),
  });
}

/** 检查 NER 模型更新的结果（POST /app/update/model/check）。 */
export interface ModelUpdateCheckView {
  /** 当前生效的模型 Release tag；老镜像无记录时为 null（显示「无法识别」） */
  current_tag: string | null;
  latest_tag: string;
  update_available: boolean;
  /** 最新模型发布是否携带更新清单（没带则无法应用内安装） */
  installable: boolean;
  published_at: string;
}

export function checkModelUpdate(): Promise<ModelUpdateCheckView> {
  return unwrap(
    request<ApiEnvelope<ModelUpdateCheckView>>("/app/update/model/check", { method: "POST" }),
  );
}

export function applyModelUpdate(): Promise<UpdateProgressView> {
  return unwrap(
    request<ApiEnvelope<UpdateProgressView>>("/app/update/model/apply", { method: "POST" }),
  );
}
