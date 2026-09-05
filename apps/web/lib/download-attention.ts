/**
 * 一条下载任务**此刻是否要用户处理**，以及按作品合并的分组规则。
 *
 * 从 `task-activity` 里单拎出来，理由与 `job-attention` 相同：侧栏角标、活动页
 * 一级切换器、任务卡片上的"需要处理："前缀读的必须是同一份判定，各算各的就会
 * 出现角标数与页面对不上的自相矛盾。这里不碰 React、不发请求，可以直接单测。
 */

import type { DownloadTask } from "@/lib/api/downloaders";
import type { JobView } from "@/lib/api/jobs";
// 值导入必须带 .ts 后缀的相对路径：本模块要进 node --test（见 tsconfig 注释）
import { jobNeedsAttention } from "./job-attention.ts";

export interface DownloadTaskGroup {
  key: string;
  mediaItemId: number | null;
  title: string;
  kind: string | null;
  posterUrl: string | null;
  tasks: DownloadTask[];
}

export function groupDownloadTasks(tasks: DownloadTask[]): DownloadTaskGroup[] {
  const groups = new Map<string, DownloadTaskGroup>();
  for (const task of tasks) {
    // 只用数据库里的媒体条目主键合并。同名但未识别的资源必须各自保留，
    // 不能因为标题解析相似就把两个版本或两部同名作品错误折叠。
    const key = task.media_item_id != null ? `media:${task.media_item_id}` : `task:${task.id}`;
    const existing = groups.get(key);
    if (existing) {
      existing.tasks.push(task);
      if (!existing.posterUrl && task.poster_url) existing.posterUrl = task.poster_url;
      continue;
    }
    groups.set(key, {
      key,
      mediaItemId: task.media_item_id,
      title: task.media_title || task.name || task.info_hash,
      kind: task.media_kind,
      posterUrl: task.poster_url,
      tasks: [task],
    });
  }
  return [...groups.values()];
}

/**
 * 内容核验证明种子里没有的集（声明的覆盖范围与实际文件不符）。
 *
 * 这些集已经退回重新寻找资源，等这个种子永远等不到——不能再按"其余等待下载
 * 完成"讲，否则会出现「下载完成 7.59 GB」和「等待下载完成」同框的自相矛盾。
 */
export function contentMissingLabel(task: DownloadTask): string | null {
  const units = (task.subscriptions[0]?.units ?? []).filter((unit) => unit.content_missing);
  if (units.length === 0) return null;
  const first = units[0];
  const label = `S${String(first.season_number).padStart(2, "0")}E${String(
    first.episode_number,
  ).padStart(2, "0")}`;
  return units.length === 1 ? label : `${label} 等 ${units.length} 集`;
}

/**
 * 这条下载任务是不是**现在**要用户动手。
 *
 * 外部任务（不是 MovieClaw 投递、也没有手动认领身份的种子）不参与：它们没有
 * 工单可救援、没有入库可推进，MovieClaw 对它们能做的只有"看一眼"。下载器里
 * 积压的几十上百个陈年错误种子若全算成待办，会把真正需要处理的订阅任务淹没，
 * 用户点开只看到"请检查下载器"，而下载器本身是正常的（真实教训）。它们仍按
 * 真实状态显示在「进行中」里，只是不再报警、不再计数。
 */
export function downloadTaskNeedsAttention(
  task: DownloadTask,
  ingestJob: JobView | null | undefined,
): boolean {
  if (task.source === "external") return false;
  return (
    task.can_replace ||
    task.state === "error" ||
    task.state === "missing" ||
    // 下载完成但 movieclaw 看不到文件：侧栏红灯亮着，卡片不能还写"等待入库"
    task.landing_error != null ||
    contentMissingLabel(task) != null ||
    (ingestJob != null && jobNeedsAttention(ingestJob))
  );
}

export function downloadGroupNeedsAttention(
  group: DownloadTaskGroup,
  ingestJobsByHash: Map<string, JobView>,
): boolean {
  return group.tasks.some((task) =>
    downloadTaskNeedsAttention(task, ingestJobsByHash.get(task.info_hash.toLowerCase())),
  );
}
