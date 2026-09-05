export interface LibraryInventorySummary {
  season_count: number;
  episode_count: number;
  season_number: number | null;
  total_episode_count: number | null;
  all_seasons_owned: boolean;
  all_episodes_owned: boolean;
}

export type LibraryInventoryAction = "follow" | "backfill" | "none";

/**
 * 单库海报操作与 hover 的“全季 / 全集”判断必须同源：当前已知季集只要有
 * 一项未齐就提示补齐；两项都齐则邀请开启自动续订，等待未来出现的新季。
 * 是否已经订阅属于全局用户状态，由海报组件在渲染操作前另行拦截。
 */
export function libraryInventoryAction(
  kind: "movie" | "tv" | "video",
  summary: LibraryInventorySummary | null,
): LibraryInventoryAction {
  if (kind !== "tv" || summary === null) return "none";
  return summary.all_seasons_owned && summary.all_episodes_owned ? "follow" : "backfill";
}

/**
 * 单库海报 hover 的季集完整度文案。
 *
 * 季度与集数分别判断“全”：多季缺任一已知正季就写“共”，当前展示季缺任一
 * 官方集就不写“全”。单季缺集时保留分母，帮助用户直接判断还差多少；多季
 * 缺集不展开长分母，只保留实际在位集数。
 */
export function formatLibraryInventorySummary(summary: LibraryInventorySummary): string {
  const episodeLabel = summary.all_episodes_owned
    ? `全 ${summary.episode_count} 集`
    : summary.season_number !== null && summary.total_episode_count !== null
      ? `${summary.episode_count}/${summary.total_episode_count} 集`
      : `${summary.episode_count} 集`;

  if (summary.season_number !== null) {
    const seasonLabel =
      summary.season_number === 0 ? "特别篇" : `第 ${summary.season_number} 季`;
    return `${seasonLabel} · ${episodeLabel}`;
  }

  const seasonLabel = summary.all_seasons_owned
    ? `全 ${summary.season_count} 季`
    : `共 ${summary.season_count} 季`;
  return `${seasonLabel} · ${episodeLabel}`;
}
