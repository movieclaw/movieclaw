/**
 * 起播意图与自动播放兜底（docs/design/web-player.md §6.5）。
 *
 * **为什么这件事不能只写一行 `video.play()`**：用户在条目页点「播放」是一次
 * 真实手势，但从那一刻到真正能 play，中间隔着「问续播点 → 决策 → 开转码
 * 会话 → 动态 import hls.js → 挂流」四段异步，短则几百毫秒，长则几秒。
 * 手势早就过期了，于是 `play()` 会被浏览器的自动播放策略按两种方式拒绝：
 *
 * - `NotAllowedError`：策略拦截。**静音的视频永远允许自动播放**，所以兜底
 *   是「先静音放起来，再给一个显眼的恢复声音入口」——用户看到画面已经在动，
 *   点一下恢复声音；而不是对着一张静止的封面图不知道该点哪里。
 * - `AbortError`：`load()` / 换源打断了上一次 play。这是**时序问题不是策略
 *   问题**，重试就好，绝不能因此静音——静音一次就得让用户白点一下。
 *
 * 归约本身是纯的，所以「什么情况该静音、什么情况该重试」能被单测穷举；组件
 * 那边只剩「什么时候调它」。
 */

/** 自动播放的结果。UI 据此决定是否显示恢复声音 / 手动播放的入口。 */
export type AutoplayOutcome =
  /** 正常出声播放 */
  | "playing"
  /** 被策略拦下，已降级为静音播放——必须给用户一个恢复声音的入口 */
  | "muted"
  /** 时序性打断（换源 / load 抢占），下一个可播时机再试一次即可 */
  | "interrupted"
  /** 静音也放不起来：只能请用户自己点 */
  | "blocked";

/** `attemptAutoplay` 只需要 `<video>` 的这几个能力，测试因此不必造 DOM。 */
export interface AutoplayTarget {
  muted: boolean;
  play(): Promise<void>;
}

/** 拿到错误的 `name`。跨 realm 的 DOMException 用 instanceof 判不准，只看名字。 */
function errorName(error: unknown): string {
  if (typeof error === "object" && error !== null && "name" in error) {
    return String((error as { name: unknown }).name);
  }
  return "";
}

/**
 * 把「被策略拦下」与「被时序打断」分开，只对前者做静音降级。
 *
 * 判定放在这里而不是 catch 里散着写：这两类错误的正确处置完全相反，混在
 * 一起的表现是「偶尔莫名其妙就没声音了」，而且极难复现。
 */
export function classifyPlayFailure(error: unknown): Exclude<AutoplayOutcome, "playing"> {
  switch (errorName(error)) {
    case "NotAllowedError":
      return "muted"; // 可以靠静音救回来
    case "AbortError":
      return "interrupted"; // 重试即可
    default:
      return "blocked";
  }
}

/**
 * 尝试自动播放：先出声试，被策略拦下就静音再试一次。
 *
 * 静音重试失败时把 `muted` 还原——留着一个静音又没播的播放器，用户点了播放
 * 会得到一段没有声音的片子，然后以为是文件没音轨。
 */
export async function attemptAutoplay(video: AutoplayTarget): Promise<AutoplayOutcome> {
  try {
    await video.play();
    return "playing";
  } catch (error) {
    const verdict = classifyPlayFailure(error);
    if (verdict !== "muted") return verdict;

    const wasMuted = video.muted;
    video.muted = true;
    try {
      await video.play();
      return "muted";
    } catch (retryError) {
      video.muted = wasMuted;
      return classifyPlayFailure(retryError) === "interrupted" ? "interrupted" : "blocked";
    }
  }
}

/** 已经放了多少次自动播放。超过这个次数就不再自动重试，改为请用户点。 */
export const MAX_AUTOPLAY_ATTEMPTS = 4;

export interface AutoplayGateInput {
  /** 用户还想不想自动播放：他自己按过暂停之后就是 false */
  wanted: boolean;
  /** 视频当前是不是暂停的 */
  paused: boolean;
  /** 已经试过几次 */
  attempts: number;
  /** 上一次的结果 */
  last: AutoplayOutcome | null;
}

/**
 * 该不该（再）试一次自动播放。
 *
 * 重试是必须的：`play()` 在 hls.js 刚 attach、MediaSource 还没 open 时可能
 * 直接被打断，只试一次的结果就是「转圈转到底、用户手动点一下才动」——也就是
 * 这次要修的现象。但重试也必须有闸：
 *
 * - 用户自己按了暂停 → 立刻停手，否则播放器会和用户抢遥控器；
 * - 已经在放了 → 没什么可试的（静音降级后的恢复声音是另一条路）；
 * - 已经判定 `blocked` → 再试多少次结果一样，只会拖慢界面响应；
 * - `muted` 是**终态**：已经放起来了，剩下的是等用户点「开启声音」。
 */
export function shouldAttemptAutoplay(input: AutoplayGateInput): boolean {
  if (!input.wanted) return false;
  if (!input.paused) return false;
  if (input.last === "blocked" || input.last === "muted") return false;
  return input.attempts < MAX_AUTOPLAY_ATTEMPTS;
}
