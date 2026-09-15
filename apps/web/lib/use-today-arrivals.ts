"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { DownloadTask } from "@/lib/api/downloaders";
import {
  listTodaySubscriptionArrivals,
  type Subscription,
  type TodaySubscriptionArrival,
} from "@/lib/api/subscriptions";
import {
  groupTodayArrivals,
  todayArrivalPresentation,
  type TodayArrivalGroup,
} from "@/lib/subscription-ui";
import type { SubscriptionFilter } from "@/lib/subscription-overview";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/** 停留期间静默轮询的间隔（毫秒）。 */
const POLL_MS = 10_000;

/**
 * 今日 / 近期入库预告的取数与轮询（订阅页两种主题布局共用）。
 *
 * - 进入本页或订阅清单变化时立即读取，停留期间每 10 秒静默同步；
 *   后台标签页暂停，恢复可见时补一次。
 * - request-id 守卫淘汰过期请求的迟到响应；已有快照遇到瞬时请求失败
 *   继续保留，避免时间轨道闪成错误态或重新出现加载文案。
 */
export function useTodaySubscriptionArrivals(subscriptions: Subscription[] | null): {
  arrivals: TodaySubscriptionArrival[] | null;
  failed: boolean;
} {
  // 只要有订阅就取预告：电影在下载/整理阶段同样进这块，切到电影分区也要有内容。
  const enabled = subscriptions !== null && subscriptions.length > 0;
  const [arrivals, setArrivals] = useState<TodaySubscriptionArrival[] | null>(null);
  const [failed, setFailed] = useState(false);
  const requestRef = useRef(0);
  const hasSnapshotRef = useRef(false);

  const refresh = useCallback(() => {
    if (!enabled) return;
    const requestId = ++requestRef.current;
    void listTodaySubscriptionArrivals()
      .then((rows) => {
        if (requestId !== requestRef.current) return;
        hasSnapshotRef.current = true;
        setArrivals(rows);
        setFailed(false);
      })
      .catch(() => {
        if (requestId === requestRef.current && !hasSnapshotRef.current) {
          setArrivals([]);
          setFailed(true);
        }
      });
  }, [enabled]);

  useEffect(() => {
    if (!enabled) {
      requestRef.current += 1;
      hasSnapshotRef.current = false;
      setArrivals(null);
      setFailed(false);
      return;
    }
    setFailed(false);
    refresh();
  }, [enabled, refresh, subscriptions]);

  useVisiblePolling(refresh, enabled ? POLL_MS : null);

  return { arrivals, failed };
}

/**
 * 把入库预告按订阅聚合成展示行（订阅页两种主题布局共用）。
 *
 * - 预告跟随当前分区（全部 / 剧集 / 电影），避免与正文讲不同的事；
 * - 「下载中」的预计时间用下载器实时 ETA 与任务快照按 infohash 就地修正；
 * - 后端按媒体类型各自给了焦点日，「全部」分区可能同时拿到两类的不同
 *   日子，这里再收敛一次到最近的那一天，保证预告始终只讲一件事；
 * - 分钟级时钟驱动「xx:xx」类相对文案滚动刷新，不为此重拉接口。
 */
export function useTodayArrivalGroups(
  arrivals: TodaySubscriptionArrival[] | null,
  mediaKind: SubscriptionFilter,
  tasks: DownloadTask[],
): TodayArrivalGroup[] {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  return useMemo(() => {
    const taskByHash = new Map(
      tasks.map((task) => [task.info_hash.toLowerCase(), task]),
    );
    const presented = (arrivals ?? [])
      .filter((arrival) => mediaKind === "all" || arrival.media_kind === mediaKind)
      .map((arrival) => {
        const task = arrival.info_hash
          ? taskByHash.get(arrival.info_hash.toLowerCase())
          : undefined;
        return {
          arrival,
          presentation: todayArrivalPresentation(arrival, task, now),
        };
      });
    const groups = groupTodayArrivals(presented).toSorted((left, right) => {
      const leftTime = left.presentation.estimatedAt ?? Number.MAX_SAFE_INTEGER;
      const rightTime = right.presentation.estimatedAt ?? Number.MAX_SAFE_INTEGER;
      if (leftTime !== rightTime) return leftTime - rightTime;
      return left.firstWantedId - right.firstWantedId;
    });
    const nearest = Math.min(...groups.map((group) => group.daysAhead));
    return groups.filter((group) => group.daysAhead === nearest);
  }, [arrivals, mediaKind, now, tasks]);
}
