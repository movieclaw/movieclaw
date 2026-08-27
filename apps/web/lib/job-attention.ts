/**
 * 一条 Job **此刻属于哪一档**：要你处理 / 进行中 / 已结束。
 *
 * 从 `task-activity` 里单拎出来，是因为这三个判定必须只有一份实现——侧栏角标、
 * 活动页一级切换器、任务视角内部的状态选项卡都读它，各算各的就会出现同一屏上
 * 侧栏 3、进行中 2 的自相矛盾（那正是 task-activity 存在的理由）。这里不碰
 * React、不发请求，因此可以直接单测。
 */

import type { JobStatus, JobView } from "@/lib/api/jobs";

/** 需要用户判断的 Job 状态；与任务视角「需要处理」同口径。 */
export const ATTENTION_JOB_STATUSES = new Set<JobStatus>(["blocked", "failed"]);
export const ACTIVE_FEED_JOB_STATUSES = new Set<JobStatus>([
  "queued",
  "running",
  "retry_wait",
  "cancelling",
  "waiting",
]);
export const HISTORY_JOB_STATUSES = new Set<JobStatus>(["succeeded", "cancelled"]);

/** 失败任务是否已被用户忽略：判定口径只此一处，计数与渲染都读它。 */
export function isDismissed(job: Pick<JobView, "dismissed_at">): boolean {
  return job.dismissed_at != null;
}

/**
 * 这条 Job 是不是**现在**要用户动手。
 *
 * 光看状态不够：`failed` 是终态，取消对它无效，于是它会永远赖在「需要处理」
 * 里，侧栏红角标永不熄灭——用户手上没有任何让它闭嘴的动作（issue #221）。
 * 「忽略」补上了这个出口：任务仍然是失败（日志、重试、事件时间线一概不动），
 * 但用户已经拍板不处理，就不该继续被算成待办。
 */
export function jobNeedsAttention(job: Pick<JobView, "status" | "dismissed_at">): boolean {
  return ATTENTION_JOB_STATUSES.has(job.status) && !isDismissed(job);
}

/** 已经了结的 Job：正常终态，加上被用户忽略的失败任务。 */
export function jobIsHistorical(job: Pick<JobView, "status" | "dismissed_at">): boolean {
  return HISTORY_JOB_STATUSES.has(job.status) || (job.status === "failed" && isDismissed(job));
}
