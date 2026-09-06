import type { Metadata } from "next";

import { ActivityView } from "@/components/activity-view";
import {
  activityScopeFromQuery,
  taskCenterViewFromQuery,
  watchViewFromQuery,
} from "@/lib/task-center";

export const metadata: Metadata = { title: "活动" };

/** 活动（/activity）：观看（媒体库实时活动）与任务（下载/入库/后台作业）两个视角。 */
export default async function ActivityPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const query = await searchParams;
  const initialScope = activityScopeFromQuery(query.view);
  const initialView = taskCenterViewFromQuery(query.view);
  const initialWatchView = watchViewFromQuery(query.view);
  // 不给 key：切换视角会经 router.replace 改写查询串，带 key 会让整个视图
  // 重新挂载，把已经拉到的活动快照连同轮询状态一起丢掉（表现为切到「任务」
  // 后「观看」旁的实时圆点熄灭）。视角与状态由 ActivityView 自己持有，
  // 这里只提供首次挂载的初值，真实导航（前进/后退）由它内部同步。
  return (
    <ActivityView
      initialScope={initialScope}
      initialView={initialView}
      initialWatchView={initialWatchView}
    />
  );
}
