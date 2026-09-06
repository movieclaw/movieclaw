/**
 * 分享访客的观看进度（docs/design/media-share.md §1.2）：不落成员表，只记在
 * 访客自己的浏览器里。键含分享 slug 与播放单元，换浏览器就从头看——没有
 * 「已看」标记、没有播放次数，只有「上次看到哪」。
 *
 * 纯逻辑模块：存储可注入，`node --test` 直接跑。
 */

export interface LocalProgressUnit {
  media_item_id: number;
  season_number?: number;
  episode_number?: number;
}

export interface LocalProgressRecord {
  position_ms: number;
  audio_track: string | null;
  subtitle_track: string | null;
  updated_at: number;
}

export interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const PREFIX = "movieclaw.share.progress";

export function localProgressKey(scopeKey: string, unit: LocalProgressUnit): string {
  return `${PREFIX}.${scopeKey}.${unit.media_item_id}.${unit.season_number ?? 0}x${
    unit.episode_number ?? 0
  }`;
}

function storageOrNull(storage?: KeyValueStorage): KeyValueStorage | null {
  if (storage) return storage;
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null; // 隐私模式 / 禁用站点数据：当作没有记录
  }
}

export function readLocalProgress(
  scopeKey: string,
  unit: LocalProgressUnit,
  storage?: KeyValueStorage,
): LocalProgressRecord | null {
  const store = storageOrNull(storage);
  if (!store) return null;
  try {
    const raw = store.getItem(localProgressKey(scopeKey, unit));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<LocalProgressRecord>;
    if (typeof parsed.position_ms !== "number") return null;
    return {
      position_ms: Math.max(0, parsed.position_ms),
      audio_track: typeof parsed.audio_track === "string" ? parsed.audio_track : null,
      subtitle_track: typeof parsed.subtitle_track === "string" ? parsed.subtitle_track : null,
      updated_at: typeof parsed.updated_at === "number" ? parsed.updated_at : 0,
    };
  } catch {
    return null;
  }
}

/**
 * 写一次进度。`position_ms` 缺省（播放器「停止」上报不带位置 = 播到结尾）
 * 时记 0：下次从头看，与服务端「看完的从头播」语义一致。轨记忆没给就沿用
 * 上次的值。
 */
export function writeLocalProgress(
  scopeKey: string,
  unit: LocalProgressUnit,
  patch: { position_ms?: number; audio_track?: string; subtitle_track?: string },
  storage?: KeyValueStorage,
  now: number = Date.now(),
): LocalProgressRecord | null {
  const store = storageOrNull(storage);
  if (!store) return null;
  const previous = readLocalProgress(scopeKey, unit, store);
  const record: LocalProgressRecord = {
    position_ms: patch.position_ms ?? 0,
    audio_track: patch.audio_track ?? previous?.audio_track ?? null,
    subtitle_track: patch.subtitle_track ?? previous?.subtitle_track ?? null,
    updated_at: now,
  };
  try {
    store.setItem(localProgressKey(scopeKey, unit), JSON.stringify(record));
  } catch {
    return null;
  }
  return record;
}
