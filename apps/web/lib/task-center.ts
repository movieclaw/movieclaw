/**
 * 「活动」页（/activity）的视角模型。
 *
 * 页面分两个一级视角，它们是**不同维度**、互不从属：
 *   - 观看：媒体库的实时活动（谁在看什么、设备、观看历史），纯观察，无处置语义；
 *   - 任务：下载 / 入库 / 后台作业，按"是否需要我处理"分四个状态。
 *
 * URL 只用一个 `view` 参数承载：带合法状态值即"任务"视角，缺省或非法即"观看"。
 * 这样既不用第二个参数，也让历史深链（/tasks?view=active）迁移后原样落地。
 */

/** 任务视角下的状态切片；查询参数使用同一集合，保证深链不会漂移。 */
export const TASK_CENTER_VIEWS = [
  "all",
  "attention",
  "active",
  "history",
] as const;

export type TaskCenterViewName = (typeof TASK_CENTER_VIEWS)[number];

/**
 * 观看视角下的切片：此刻在播 / 每场一行的播放记录 / 一段时间的观看统计。
 * 三者时间语义与刷新节奏都不同，摞在一页里读不出重点，所以和任务一样分片
 * （docs/design/activity.md「观看视角的三个切片」）。值与任务切片共用同一个
 * `view` 查询参数，不能撞名（任务已占 history）。
 */
export const WATCH_VIEWS = ["playing", "plays", "stats"] as const;

export type WatchViewName = (typeof WATCH_VIEWS)[number];

export const WATCH_VIEW_LABELS: readonly { id: WatchViewName; label: string }[] = [
  { id: "playing", label: "正在播放" },
  { id: "plays", label: "最近播放" },
  { id: "stats", label: "观看统计" },
] as const;

/** 一级视角：观看（媒体库实时活动）/ 任务（按状态分组）。 */
export type ActivityScope = "media" | "tasks";

function readQuery(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/** 查询值是否指向任务视角的某个状态切片。 */
function isTaskView(candidate: string | undefined): candidate is TaskCenterViewName {
  return TASK_CENTER_VIEWS.includes(candidate as TaskCenterViewName);
}

/**
 * 从查询参数解析一级视角。带合法状态值 → 任务；其余（含缺省、非法、
 * 重复参数）→ 观看，即页面默认落点。
 */
export function activityScopeFromQuery(
  value: string | string[] | undefined,
): ActivityScope {
  return isTaskView(readQuery(value)) ? "tasks" : "media";
}

function isWatchView(candidate: string | undefined): candidate is WatchViewName {
  return WATCH_VIEWS.includes(candidate as WatchViewName);
}

/** 观看切片：缺省与非法值都落「正在播放」——它是本页的默认落点。 */
export function watchViewFromQuery(value: string | string[] | undefined): WatchViewName {
  const candidate = readQuery(value);
  return isWatchView(candidate) ? candidate : "playing";
}

/** 非法或重复查询值安全回退「全部」，避免 URL 直接控制内部状态。 */
export function taskCenterViewFromQuery(
  value: string | string[] | undefined,
): TaskCenterViewName {
  const candidate = readQuery(value);
  return isTaskView(candidate) ? candidate : "all";
}
