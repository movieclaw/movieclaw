import type { Metadata } from "next";

import { LibraryCollectionDetailView } from "@/components/library-collection-detail-view";

/** 兜底标题；合集名要等接口返回，就绪后由视图内的 usePageTitle 覆盖。 */
export const metadata: Metadata = { title: "合集" };

/** 合集详情页（/library/[id]/c/[cid]）：规则条 + 成员海报墙。 */
export default async function LibraryCollectionPage({
  params,
}: {
  params: Promise<{ id: string; cid: string }>;
}) {
  const { id, cid } = await params;
  return (
    <div className="flex h-full flex-col">
      <LibraryCollectionDetailView libraryId={Number(id)} collectionId={Number(cid)} />
    </div>
  );
}
