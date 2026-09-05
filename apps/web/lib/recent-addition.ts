export interface RecentAddition {
  season_count: number;
  episode_count: number;
  season_number: number | null;
  first_episode_number: number | null;
  last_episode_number: number | null;
  complete_season: boolean;
}

export interface RecentAdditionOverlay {
  primary: string;
  secondary?: string;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * 「最近添加」条目的紧凑季集摘要。
 *
 * 能用一个连续区间说清时保留精确集号；零散或跨季时改为计数，避免 hover
 * 信息层被长范围占满。
 */
export function formatRecentAddition(addition: RecentAddition): string | null {
  if (addition.episode_count === 0 || addition.season_count === 0) return null;
  if (addition.season_count > 1) {
    return `新增${addition.season_count}季 · ${addition.episode_count}集`;
  }

  const seasonNumber = addition.season_number;
  if (seasonNumber === null) return null;
  if (addition.complete_season) {
    const seasonLabel = seasonNumber === 0 ? "特别篇" : `第${seasonNumber}季`;
    return `${seasonLabel} · 全${addition.episode_count}集`;
  }

  const start = addition.first_episode_number;
  const end = addition.last_episode_number;
  if (start === null || end === null) {
    return `S${pad(seasonNumber)} · 新增${addition.episode_count}集`;
  }

  return start === end
    ? `S${pad(seasonNumber)}E${pad(start)}`
    : `S${pad(seasonNumber)}E${pad(start)}–${pad(end)}`;
}

/**
 * 组装「最近添加」专用 hover：剧集优先说明本批季集范围，时间放在第二行；
 * 电影或没有批次信息的旧条目只显示入库时间，不制造空白或累计库存信息。
 */
export function buildRecentAdditionOverlay(
  kind: "movie" | "tv" | "video",
  addition: RecentAddition | null | undefined,
  addedTimeLabel: string | null,
): RecentAdditionOverlay | undefined {
  const episodeLabel = kind === "tv" && addition ? formatRecentAddition(addition) : null;
  if (episodeLabel) {
    return addedTimeLabel
      ? { primary: episodeLabel, secondary: addedTimeLabel }
      : { primary: episodeLabel };
  }
  return addedTimeLabel ? { primary: addedTimeLabel } : undefined;
}
