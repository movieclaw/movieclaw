import type { Metadata } from "next";

import { SharePage } from "@/components/share/share-page";

/** 兜底标题；片名要等解锁并读到影片信息后由视图内的 usePageTitle 覆盖。 */
export const metadata: Metadata = { title: "分享的影片" };

/**
 * 影片分享页 `/s/{slug}`（docs/design/media-share.md §1.2）：探针 → 密码卡片 /
 * 失效提示 / 影片页，全部由客户端组件按接口结果切换。
 */
export default async function SharedItemPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <SharePage slug={slug} />;
}
