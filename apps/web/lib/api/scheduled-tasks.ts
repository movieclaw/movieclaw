import { request } from "@/lib/http";
import type { ScheduleShape, TriggerType } from "@/lib/scheduled-tasks";

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

/** 一个注册的定时任务：定义（代码里）+ 周期（库里可改）+ 运行台账 */
export interface ScheduledTask extends ScheduleShape {
  key: string;
  title: string;
  description: string;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface ScheduledTaskUpdate {
  enabled: boolean;
  trigger_type: TriggerType;
  interval_seconds?: number | null;
  cron_expr?: string | null;
}

export function listScheduledTasks(): Promise<ScheduledTask[]> {
  return unwrap(request<ApiEnvelope<ScheduledTask[]>>("/scheduled-tasks"));
}

export function updateScheduledTask(key: string, body: ScheduledTaskUpdate): Promise<ScheduledTask> {
  return unwrap(
    request<ApiEnvelope<ScheduledTask>>(`/scheduled-tasks/${encodeURIComponent(key)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  );
}
