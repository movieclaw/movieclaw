import type { Metadata } from "next";

import { PlayerPage } from "@/components/player/player-page";

/** 兜底标题；片名要等接口返回，播放器内不再改写 document.title（全屏时看不到）。 */
export const metadata: Metadata = { title: "播放" };

/** 只接受单个非负整数查询参数；重复、负数和非数字一律按缺失处理。 */
function queryNumber(value: string | string[] | undefined): number | undefined {
  if (typeof value !== "string" || !/^\d+$/.test(value)) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
}

/**
 * 网页播放页（/play/[libraryId]/[mediaItemId]?season=&episode=）。
 *
 * 电影不带 season/episode，剧集两者都要有——播放单元的三元组约定见
 * docs/design/web-player.md §3.2。
 */
export default async function PlayPage({
  params,
  searchParams,
}: {
  params: Promise<{ libraryId: string; mediaItemId: string }>;
  searchParams: Promise<{
    season?: string | string[];
    episode?: string | string[];
    returnTo?: string | string[];
  }>;
}) {
  const { libraryId, mediaItemId } = await params;
  const query = await searchParams;
  return (
    <PlayerPage
      libraryId={Number(libraryId)}
      mediaItemId={Number(mediaItemId)}
      season={queryNumber(query.season)}
      episode={queryNumber(query.episode)}
      returnTo={typeof query.returnTo === "string" ? query.returnTo : undefined}
    />
  );
}
