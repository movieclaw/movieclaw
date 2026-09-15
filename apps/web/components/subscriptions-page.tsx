"use client";

import { NetflixSubscriptionsPage } from "@/components/netflix/subscriptions-page";
import { SubscriptionsView } from "@/components/subscriptions-view";
import { useTheme } from "@/lib/ui-prefs";

/**
 * 订阅页入口：按主题分流。
 *
 * - 银玻璃：原海报墙布局（subscriptions-view.tsx），信息架构不变。
 * - Netflix：结构级处理（netflix/subscriptions-page.tsx，web-themes.md
 *   §5.7 的 2026-09-15 修订）——预告行 + 状态分区海报行，与发现 / 媒体库
 *   页同一行栅格与控件安放方式。
 *
 * 两套布局各自带数据层（同源接口 + 共享轮询 hook），切主题即整体切换。
 */
export function SubscriptionsPage() {
  const isNetflix = useTheme().id === "netflix";
  return isNetflix ? <NetflixSubscriptionsPage /> : <SubscriptionsView />;
}
