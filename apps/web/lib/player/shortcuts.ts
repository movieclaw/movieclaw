/**
 * 键盘快捷键（docs/design/web-player.md §6.5）。
 *
 * **按 YouTube 惯例，不自创**：播放器快捷键是肌肉记忆，自创一套只会让人
 * 在自己的媒体库里按错键。空格/K 播放暂停、←→ 五秒、JL 十秒、F 全屏、
 * M 静音、C 字幕、↑↓ 音量、0-9 跳百分比。
 *
 * 纯映射函数，不碰 DOM——「输入框里按空格不该暂停播放」这类判断因此可以
 * 直接测。
 */

export type PlayerAction =
  | { type: "toggle-play" }
  | { type: "seek-by"; seconds: number }
  | { type: "seek-percent"; percent: number }
  | { type: "volume-by"; delta: number }
  | { type: "toggle-mute" }
  | { type: "toggle-fullscreen" }
  | { type: "toggle-subtitles" };

export interface KeyContext {
  key: string;
  /** 事件目标是不是输入控件：是就一律不接管（用户在搜字幕、填备注） */
  inEditable: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
}

/**
 * 键 → 动作；不该接管的返回 null（调用方据此决定要不要 preventDefault）。
 *
 * 带 Ctrl/Cmd/Alt 的组合一律放行：Cmd+← 是浏览器后退、Ctrl+F 是页内查找，
 * 播放器抢走这些键的代价远大于收益。
 */
export function resolveShortcut(context: KeyContext): PlayerAction | null {
  if (context.inEditable) return null;
  if (context.ctrlKey || context.metaKey || context.altKey) return null;

  const key = context.key;
  if (key === " " || key === "Spacebar" || key.toLowerCase() === "k") {
    return { type: "toggle-play" };
  }
  if (key === "ArrowLeft") return { type: "seek-by", seconds: -5 };
  if (key === "ArrowRight") return { type: "seek-by", seconds: 5 };
  if (key.toLowerCase() === "j") return { type: "seek-by", seconds: -10 };
  if (key.toLowerCase() === "l") return { type: "seek-by", seconds: 10 };
  if (key === "ArrowUp") return { type: "volume-by", delta: 0.05 };
  if (key === "ArrowDown") return { type: "volume-by", delta: -0.05 };
  if (key.toLowerCase() === "m") return { type: "toggle-mute" };
  if (key.toLowerCase() === "f") return { type: "toggle-fullscreen" };
  if (key.toLowerCase() === "c") return { type: "toggle-subtitles" };
  if (/^[0-9]$/.test(key)) return { type: "seek-percent", percent: Number(key) * 10 };
  return null;
}

/** 事件目标是不是输入控件。contentEditable 的富文本区同样算。 */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}
