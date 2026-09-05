import type { Metadata } from "next";

import { LibraryManageView } from "@/components/library-manage-view";

export const metadata: Metadata = { title: "媒体库管理" };

/** 媒体库管理（/library/manage）：一库一行的管理列表；首页只做浏览入口。 */
export default function LibraryManagePage() {
  return (
    <div className="flex h-full flex-col">
      <LibraryManageView />
    </div>
  );
}
