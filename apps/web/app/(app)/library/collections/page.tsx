import type { Metadata } from "next";

import { AllCollectionsView } from "@/components/all-collections-view";

export const metadata: Metadata = { title: "全部合集" };

/** 跨库合集总览（/library/collections）：按库分组，跨库的单独一组。 */
export default function AllCollectionsPage() {
  return (
    <div className="flex h-full flex-col">
      <AllCollectionsView />
    </div>
  );
}
