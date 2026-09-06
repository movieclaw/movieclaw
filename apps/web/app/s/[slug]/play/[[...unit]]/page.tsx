import type { Metadata } from "next";

import { SharePlayerPage } from "@/components/share/share-player-page";
import { parseUnitSegment } from "@/lib/player/play-links";

export const metadata: Metadata = { title: "播放" };

/** 只接受单个非负整数查询参数；重复、负数和非数字一律按缺失处理。 */
function queryNumber(value: string | string[] | undefined): number | undefined {
  if (typeof value !== "string" || !/^\d+$/.test(value)) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
}

/**
 * 分享页的播放器 `/s/{slug}/play[/sXXeYY][?t=]`：与 /play 同一套地址约定与
 * 同一个播放器，只是接口作用域换成分享通道、退出固定回 /s/{slug}。
 */
export default async function SharedPlayPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string; unit?: string[] }>;
  searchParams: Promise<{ t?: string | string[] }>;
}) {
  const { slug, unit } = await params;
  const query = await searchParams;
  const parsed = parseUnitSegment(unit?.[0]);
  const startSeconds = queryNumber(query.t);
  return (
    <SharePlayerPage
      slug={slug}
      season={parsed?.season}
      episode={parsed?.episode}
      startMsOverride={startSeconds !== undefined ? startSeconds * 1000 : undefined}
    />
  );
}
