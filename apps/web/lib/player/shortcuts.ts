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

/** 帧率未知时逐帧步进按这个帧率算（一帧 ≈ 41.7 毫秒）。24 是电影的下限，
 * 宁可一次走得偏少：多按一下总比跳过想看的那一帧强。 */
export const FALLBACK_FRAME_RATE = 24;

export type PlayerAction =
  | { type: "toggle-play" }
  | { type: "seek-by"; seconds: number }
  | { type: "seek-percent"; percent: number }
  | { type: "volume-by"; delta: number }
  | { type: "toggle-mute" }
  | { type: "toggle-fullscreen" }
  | { type: "toggle-subtitles" }
  /** 逐帧步进（暂停时才有意义）：direction 为 ±1 帧 */
  | { type: "step-frame"; direction: 1 | -1 };

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
  // 逐帧：, / . 是 YouTube 的取值（与 < > 同键位，不必按 Shift）
  if (key === ",") return { type: "step-frame", direction: -1 };
  if (key === ".") return { type: "step-frame", direction: 1 };
  return null;
}

/**
 * 事件目标是不是**能录入文字**的控件。contentEditable 的富文本区同样算。
 *
 * 滑块（`input[type=range]`）明确排除：播放器里唯一的滑块就是进度条，而它
 * 被点一下就拿走焦点。一律按「输入控件」放行的话，点过进度条之后空格不再
 * 播放暂停、F 不全屏、M 不静音——用户只当播放器坏了，而且找不回来（焦点
 * 一直留在那儿）。方向键更拧巴：全局的 ±5 秒被让掉，落到 range 原生的
 * ±1 秒（step），同一个键在点条前后跳的秒数都不一样。
 */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT") return (target as HTMLInputElement).type !== "range";
  return tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}
