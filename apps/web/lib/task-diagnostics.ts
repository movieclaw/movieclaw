import type { DownloadTask } from "@/lib/api/downloaders";
import type { JobView } from "@/lib/api/jobs";

type JobDiagnosticSource = Pick<
  JobView,
  "id" | "job_type" | "status" | "root_job_id"
>;

type DownloadDiagnosticSource = Pick<
  DownloadTask,
  "id" | "info_hash" | "downloader_id" | "source" | "state" | "subscriptions"
>;

/**
 * 生成可直接交给 AI 的后台作业定位信息。除当前任务 ID 外保留根任务 ID，
 * 让重试产生新作业后仍能沿整条执行链排查，而不需要用户解释两者关系。
 */
export function backgroundJobDiagnosticText(job: JobDiagnosticSource): string {
  const lines = [
    "MovieClaw 后台任务诊断信息",
    `job_id: ${job.id}`,
    `job_type: ${job.job_type}`,
    `status: ${job.status}`,
  ];
  if (job.root_job_id && job.root_job_id !== job.id) {
    lines.push(`root_job_id: ${job.root_job_id}`);
  }
  return lines.join("\n");
}

/**
 * 下载任务来自下载器实时快照，前端 id 是组合定位键，并非后台 Job ID。
 * 因此同时带上 infohash、下载器、订阅和关联入库 Job，避免 AI 把下载阶段
 * 与后续入库阶段误认成同一种任务。
 */
export function downloadTaskDiagnosticText(
  task: DownloadDiagnosticSource,
  ingestJob: Pick<JobView, "id"> | null,
): string {
  const lines = [
    "MovieClaw 下载任务诊断信息",
    `download_task_key: ${task.id}`,
    `info_hash: ${task.info_hash}`,
    `source: ${task.source}`,
    `state: ${task.state}`,
  ];
  if (task.downloader_id != null) {
    lines.push(`downloader_id: ${task.downloader_id}`);
  }
  const subscriptionIds = [
    ...new Set(task.subscriptions.map((subscription) => subscription.id)),
  ].sort((left, right) => left - right);
  if (subscriptionIds.length > 0) {
    lines.push(`subscription_ids: ${subscriptionIds.join(",")}`);
  }
  if (ingestJob) {
    lines.push(`ingest_job_id: ${ingestJob.id}`);
  }
  return lines.join("\n");
}
