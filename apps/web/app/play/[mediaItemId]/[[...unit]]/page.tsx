import type { Metadata } from "next";

import { PlayerPage } from "@/components/player/player-page";
import { parseUnitSegment } from "@/lib/player/play-links";

/** 兜底标题；片名要等接口返回，播放器内不再改写 document.title（全屏时看不到）。 */
export const metadata: Metadata = { title: "播放" };

/**
 * 网页播放页（docs/design/web-player.md §6.10）。
 *
 * 地址就是分享凭证，形态按「短、稳、可读」设计：
 *
 *     电影：  /play/127
 *     剧集：  /play/127/s01e03
 *     指定位置：/play/127/s01e03?t=1520   （秒；YouTube 同款语义）
 *
 * - 只带 media_item_id：它以 (kind, tmdb_id) 为锚、幂等复用，换文件版本、
 *   重扫库都不变；库自增 id 删库重建就断，所以**不进地址**，由服务端按
 *   成员可见性解析归属。
 * - 季集用 sXXeYY 路径段而不是查询参数：媒体行业的通用写法，一眼能读。
 * - 不带标题 slug：中文进 URL 会被百分号编码，粘出来比数字更难看，且标题
 *   随刮削更新会变——同一内容两个地址比不可读更糟。
 * - `returnTo` 一类导航状态**永不进地址**：那是分享者的上下文，不是内容
 *   标识（走 sessionStorage，见 lib/player/return-path.ts）。
 */

/** 只接受单个非负整数查询参数；重复、负数和非数字一律按缺失处理。 */
function queryNumber(value: string | string[] | undefined): number | undefined {
  if (typeof value !== "string" || !/^\d+$/.test(value)) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
}

export default async function PlayPage({
  params,
  searchParams,
}: {
  params: Promise<{ mediaItemId: string; unit?: string[] }>;
  searchParams: Promise<{ t?: string | string[] }>;
}) {
  const { mediaItemId, unit } = await params;
  const query = await searchParams;
  const parsed = parseUnitSegment(unit?.[0]);
  const startSeconds = queryNumber(query.t);
  return (
    <PlayerPage
      mediaItemId={Number(mediaItemId)}
      season={parsed?.season}
      episode={parsed?.episode}
      startMsOverride={startSeconds !== undefined ? startSeconds * 1000 : undefined}
    />
  );
}
