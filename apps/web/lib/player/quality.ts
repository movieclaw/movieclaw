/**
 * 画质选择（docs/design/web-player.md §10 预案的「手动选清晰度」）。
 *
 * 语义是**上限而不是目标**：源分辨率不超所选档就照常直通——直通是无损的，
 * 比转出来的同分辨率画质好、还零转码开销；超了才转码降到该档。所以选
 * 「1080p」看一部 1080p H.264 片依然是原画直通，不会画蛇添足地重编码。
 *
 * 「自动」= 不设上限，交给服务端决策引擎（直通优先），也是默认值。
 * 弱网场景用户手选低档，换来的是码率阶梯里对应的低带宽（720p→3M、480p→1.5M）。
 */

export interface QualityOption {
  /** 传给服务端的 max_height；null = 自动（不限制） */
  maxHeight: number | null;
  label: string;
  /** 菜单里的补充说明（带宽预期） */
  hint: string | null;
}

export const QUALITY_OPTIONS: readonly QualityOption[] = [
  { maxHeight: null, label: "自动", hint: "原画质优先，能直通不转码" },
  { maxHeight: 1080, label: "1080p", hint: "约 6 Mbps" },
  { maxHeight: 720, label: "720p", hint: "约 3 Mbps，网络一般时选它" },
  { maxHeight: 480, label: "480p", hint: "约 1.5 Mbps，弱网救急" },
] as const;

const STORAGE_KEY = "movieclaw.player.quality";

/** 读持久化的画质选择。没存过、存的值非法、或没有 localStorage 都回自动。 */
export function loadQualityPreference(): number | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return null;
    const value = Number(raw);
    return QUALITY_OPTIONS.some((o) => o.maxHeight === value) ? value : null;
  } catch {
    return null;
  }
}

export function saveQualityPreference(maxHeight: number | null): void {
  try {
    if (maxHeight === null) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, String(maxHeight));
  } catch {
    // 隐私模式下写不进：本次会话内仍生效，只是不记住
  }
}

export function qualityLabel(maxHeight: number | null): string {
  return QUALITY_OPTIONS.find((o) => o.maxHeight === maxHeight)?.label ?? "自动";
}
