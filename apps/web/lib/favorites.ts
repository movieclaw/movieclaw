import type { LibraryKind } from "@/lib/media-types";

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * 「我的收藏」卡片 hover 层的层级说明。
 *
 * Jellyfin 客户端可以分别收藏整剧、某一季、某一集，首页按作品去重后要说清
 * 「收藏的是哪一层」：单集写成播放器常见的 S01E02，整季写「第 N 季」（0 为
 * 特别篇）。整剧与电影就是收藏了作品本身，不必再解释，返回 null 不占 hover。
 */
export function favoriteLevelLabel(
  kind: LibraryKind,
  seasonNumber: number | null,
  episodeNumber: number | null,
): string | null {
  if (kind !== "tv" || seasonNumber === null) return null;
  if (episodeNumber !== null) return `收藏了 S${pad(seasonNumber)}E${pad(episodeNumber)}`;
  return seasonNumber === 0 ? "收藏了特别篇" : `收藏了第 ${seasonNumber} 季`;
}
