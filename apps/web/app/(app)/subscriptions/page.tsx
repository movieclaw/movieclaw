import type { Metadata } from "next";

import { SubscriptionsPage } from "@/components/subscriptions-page";

export const metadata: Metadata = { title: "我的订阅" };

/** 我的订阅（/subscriptions）：按主题分流（银玻璃海报墙 / Netflix 行式布局）。 */
export default function SubscriptionsPageRoute() {
  return (
    <div className="flex h-full flex-col">
      <SubscriptionsPage />
    </div>
  );
}
