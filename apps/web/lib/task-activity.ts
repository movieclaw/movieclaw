"use client";

/**
 * 任务活动的**唯一计数口径**。
 *
 * 侧栏「活动」角标、活动页一级切换器上的「任务」提示、任务视角内部的状态
 * 选项卡，三处说的都得是同一件事：这个系统现在有几件任务要你看。以前它们
 * 各算各的——侧栏按"下载器任务条数 + 活跃 Job 条数"数，任务页按"业务过程"
 * 数（一个下载任务和它触发的入库 Job 是同一件事，合成一条），于是同一屏上
 * 会出现侧栏 3、进行中 2 的自相矛盾。数字对不上，提醒就失去了可信度。
 *
 * 因此把分组与归类下沉到这里：谁要计数就调 `useTaskActivity()`，谁要渲染就
 * 拿同一份分组结果去画。本模块只做归类，不碰各领域的事实源——下载状态仍来自
 * 下载器快照，Job 状态仍来自 job 表（见 docs/design/activity.md）。
 */

import { useMemo } from "react";

import type { DownloadTask } from "@/lib/api/downloaders";
import type { JobView } from "@/lib/api/jobs";
import { useDownloadTasks } from "@/lib/download-tasks";
// 单条 Job 的档位判定放在 job-attention（无 React、可单测）；本模块只做
// 跨领域的分组与计数。
import {
  ACTIVE_FEED_JOB_STATUSES,
  jobIsHistorical,
  jobNeedsAttention,
} from "@/lib/job-attention";
import { useJobs } from "@/lib/jobs";

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

export function downloadGroupNeedsAttention(
  group: DownloadTaskGroup,
  ingestJobsByHash: Map<string, JobView>,
): boolean {
  return group.tasks.some((task) => {
    const ingestJob = ingestJobsByHash.get(task.info_hash.toLowerCase());
    return (
      task.can_replace ||
      task.state === "error" ||
      task.state === "missing" ||
      contentMissingLabel(task) != null ||
      (ingestJob != null && jobNeedsAttention(ingestJob))
    );
  });
}

export interface TaskActivity {
  /** infohash（小写）→ 关联的入库 Job：下载与入库是同一件事的两段。 */
  ingestJobsByHash: Map<string, JobView>;
  attentionDownloadGroups: DownloadTaskGroup[];
  activeDownloadGroups: DownloadTaskGroup[];
  /** 刷流做种：没有入库/救援语义，既不进时间线也不参与任何计数。 */
  boostTasks: DownloadTask[];
  standaloneAttentionJobs: JobView[];
  standaloneActiveJobs: JobView[];
  standaloneHistoricalJobs: JobView[];
  /** 需要处理的条数（下载过程按作品合并后计一条）。 */
  attentionTotal: number;
  /** 进行中的条数。 */
  activeTotal: number;
  historyTotal: number;
}

/**
 * 汇总当前的任务活动。数据来自全站唯一的两个 Provider（Job / 下载快照），
 * 本 Hook 不发起任何请求，因此侧栏与活动页同时使用也不会重复读下载器。
 */
export function useTaskActivity(): TaskActivity {
  const { jobs } = useJobs();
  const { tasks: downloadTasks } = useDownloadTasks();

  const ingestJobsByHash = useMemo(() => {
    const linked = new Map<string, JobView>();
    for (const job of jobs) {
      if (job.job_type !== "library.ingest") continue;
      for (const resource of job.resources) {
        if (resource.resource_type === "download" && !linked.has(resource.resource_id)) {
          linked.set(resource.resource_id.toLowerCase(), job);
        }
      }
    }
    return linked;
  }, [jobs]);

  // 刷流（boost）种子常年大量在跑，计入提醒会让数字失去意义，先行分流
  const boostTasks = useMemo(
    () => downloadTasks.filter((task) => task.source === "boost"),
    [downloadTasks],
  );
  const mediaTasks = useMemo(
    () => downloadTasks.filter((task) => task.source !== "boost"),
    [downloadTasks],
  );
  const downloadGroups = useMemo(() => groupDownloadTasks(mediaTasks), [mediaTasks]);
  const attentionDownloadGroups = useMemo(
    () => downloadGroups.filter((group) => downloadGroupNeedsAttention(group, ingestJobsByHash)),
    [downloadGroups, ingestJobsByHash],
  );
  const activeDownloadGroups = useMemo(
    () => downloadGroups.filter((group) => !downloadGroupNeedsAttention(group, ingestJobsByHash)),
    [downloadGroups, ingestJobsByHash],
  );

  // 已经串进下载生命周期的 ingest Job 不再作为第二件事重复出现（既不重复渲染，
  // 也不重复计数——这正是侧栏数字曾经比页面大一号的原因）。
  const linkedIngestJobIds = useMemo(
    () =>
      new Set(
        downloadTasks
          .map((task) => ingestJobsByHash.get(task.info_hash.toLowerCase())?.id)
          .filter((jobId): jobId is string => jobId != null),
      ),
    [downloadTasks, ingestJobsByHash],
  );
  const standaloneAttentionJobs = useMemo(
    () => jobs.filter((job) => jobNeedsAttention(job) && !linkedIngestJobIds.has(job.id)),
    [jobs, linkedIngestJobIds],
  );
  const standaloneActiveJobs = useMemo(
    () =>
      jobs.filter(
        (job) => ACTIVE_FEED_JOB_STATUSES.has(job.status) && !linkedIngestJobIds.has(job.id),
      ),
    [jobs, linkedIngestJobIds],
  );
  const standaloneHistoricalJobs = useMemo(
    () => jobs.filter((job) => jobIsHistorical(job) && !linkedIngestJobIds.has(job.id)),
    [jobs, linkedIngestJobIds],
  );

  return {
    ingestJobsByHash,
    attentionDownloadGroups,
    activeDownloadGroups,
    boostTasks,
    standaloneAttentionJobs,
    standaloneActiveJobs,
    standaloneHistoricalJobs,
    attentionTotal: attentionDownloadGroups.length + standaloneAttentionJobs.length,
    activeTotal: activeDownloadGroups.length + standaloneActiveJobs.length,
    historyTotal: standaloneHistoricalJobs.length,
  };
}

/**
 * 提醒角标的展示模型：**只表达当前最该被看见的那件事**，并给出点进去
 * 直达那件事的落点。
 *
 * 有需要处理的任务时亮警示色、落在「需要处理」；否则退回进行中计数、落在
 * 「进行中」——角标数的是任务，落点就必须是任务视角对应的那一片，
 * 不能把人丢到「观看」再让他自己找为什么有提醒。什么都没有时才回到页面
 * 默认的「观看」。
 */
export interface TaskActivityBadge {
  /** 是否为"需要处理"的警示形态（红色）；否则是进行中（蓝色）。 */
  alert: boolean;
  /** 要展示的数字；为 0 表示不该出现角标。 */
  count: number;
  /** 点击落点（含视角查询参数）。 */
  href: string;
  /** 无障碍/悬浮提示文案。 */
  hint: string;
}

export function taskActivityBadge(activity: TaskActivity): TaskActivityBadge {
  const alert = activity.attentionTotal > 0;
  if (alert) {
    return {
      alert: true,
      count: activity.attentionTotal,
      href: "/activity?view=attention",
      hint: `${activity.attentionTotal} 项需要处理`,
    };
  }
  return {
    alert: false,
    count: activity.activeTotal,
    href: activity.activeTotal > 0 ? "/activity?view=active" : "/activity",
    hint: activity.activeTotal > 0 ? `${activity.activeTotal} 个进行中` : "暂无进行中的任务",
  };
}
