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

/**
 * 一个登记目录的占用（见 services/storage/registry.py 的登记表）。
 * 前端不写死任何目录：分组、能否清理、清理后果全部由后端登记表给出，
 * 业务新增一种缓存只需在后端登记，这一页自动多一行。
 */
export interface DirUsage {
  key: string;
  title: string;
  /** 一句话用途，行内展示 */
  summary: string;
  /** 完整说明与清理后果，悬停与确认时展示 */
  description: string;
  path: string;
  /** cache = 派生物（可清理）；data = 用户数据/系统状态（只展示） */
  group: "cache" | "data";
  /** 重建代价：expensive 的目录清空前要更重的警示 */
  rebuild_cost: "cheap" | "expensive" | "none";
  clearable: boolean;
  orphan_aware: boolean;
  exists: boolean;
  bytes: number;
  entries: number;
}

export interface StorageUsage {
  data_root: string;
  disk_total: number;
  disk_used: number;
  disk_free: number;
  /** 可回收的缓存合计 */
  cache_bytes: number;
  /** 不可回收的数据合计 */
  data_bytes: number;
  dirs: DirUsage[];
  /** data/ 根下没有任何登记覆盖的条目——出现即是代码漏登记，需反馈 */
  unregistered: { path: string; bytes: number }[];
  /** 统计时刻（Unix 秒） */
  computed_at: number;
}

/**
 * 面板读到的状态。统计一次要遍历整个 data/（大库几十秒），所以接口从不阻塞：
 * 永远立刻给上一次的结果与它的统计时刻，是否正在重算由 computing 表示。
 */
export interface StorageState {
  /** 上一次统计的结果；后端进程内还没统计过时为 null */
  usage: StorageUsage | null;
  /** 后台是否正在统计：前端据此显示「统计中」并轮询，新数据到了再替换旧数据 */
  computing: boolean;
  /** 上一次统计失败的原因；失败时旧结果仍然可用 */
  error: string | null;
}

export type CleanMode = "all" | "orphans";

export interface CleanResult {
  key: string;
  mode: CleanMode;
  removed: number;
  skipped_busy: number;
  freed_bytes: number;
}

/**
 * 读取占用状态（立刻返回，不等统计）。refresh=true 只是让后端在后台重新统计，
 * 之后按 computing 轮询这个接口，等新快照落地。
 */
export function getStorageState(refresh = false): Promise<StorageState> {
  return unwrap(
    request<ApiEnvelope<StorageState>>(
      `/app/storage${refresh ? "?refresh=1" : ""}`,
    ),
  );
}

/** 清理某个目录：all 全部清空，orphans 只删媒体库里已不存在的条目。 */
export function cleanStorage(
  key: string,
  mode: CleanMode,
): Promise<CleanResult> {
  return unwrap(
    request<ApiEnvelope<CleanResult>>(
      `/app/storage/${encodeURIComponent(key)}/clean`,
      {
        method: "POST",
        body: JSON.stringify({ mode }),
      },
    ),
  );
}
