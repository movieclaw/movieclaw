/**
 * 停顿归因：分清「解码器卡住」和「客户端追上了编码器」。
 *
 * 只看「currentTime 不动」会把这两件事混为一谈，而它们的正确处置完全相反：
 *
 * - **解码器卡住**（缓冲里有数据却放不动）：坏流的最常见表现不是报错，而是
 *   解码器悄悄停住、界面永远转圈。这种要尽快判失败走降档回路，换一档重来。
 * - **供流跟不上**（缓冲已经耗尽）：转码是边转边给的，软件转码 4K 在弱机器上
 *   编码速度低于实时播放，客户端追上编码器就会停下来等。这是**正常现象**，
 *   判成失败会触发降档——而档 4 已经是最低档，用户看到的是「所有播放方式都
 *   失败了」，真实原因其实只是服务器转得慢。
 *
 * 供流侧也不能无限等：会话半路死掉（ffmpeg 崩了）的表现同样是缓冲耗尽后
 * 一直没有新数据。所以给一个宽得多的上限，超了再报，并且把原因说对。
 */

/** 有数据却不前进，判定为解码卡死的秒数。 */
export const STALL_TIMEOUT_S = 8;
/**
 * 缓冲耗尽后仍无进展、判定为供流中断的秒数。
 *
 * 取值要压得住「软件转码起步慢」这种正常情况：转码会话刚起来时编码器要先
 * 追上首帧，客户端等十几秒是常事。
 */
export const STARVE_TIMEOUT_S = 45;
/** 小于这个秒数的前方缓冲视同没有——四舍五入的抖动不该被当成"有数据"。 */
const BUFFER_EPSILON_S = 0.5;

export type StallVerdict = "ok" | "decode-stalled" | "starved";

export interface StallInput {
  paused: boolean;
  ended: boolean;
  seeking: boolean;
  /** 距上次采样，currentTime 是否前进过 */
  advanced: boolean;
  /** currentTime 前方还有多少秒缓冲 */
  bufferedAhead: number;
  /** 已经连续停顿了多少秒 */
  stalledFor: number;
}

/**
 * 一次采样的判定。暂停、结束、拖动中一律不算停顿——用户自己按的暂停不该
 * 被当成故障。
 */
export function classifyStall(input: StallInput): StallVerdict {
  if (input.paused || input.ended || input.seeking || input.advanced) return "ok";
  if (input.bufferedAhead > BUFFER_EPSILON_S) {
    return input.stalledFor >= STALL_TIMEOUT_S ? "decode-stalled" : "ok";
  }
  return input.stalledFor >= STARVE_TIMEOUT_S ? "starved" : "ok";
}

/** 判定 → 面向用户的中文原因。用户报障时这句话就是全部线索。 */
export function stallReason(verdict: Exclude<StallVerdict, "ok">): string {
  return verdict === "decode-stalled"
    ? `播放停滞超过 ${STALL_TIMEOUT_S} 秒，这一档的码流浏览器吃不下`
    : `等待服务端供流超过 ${STARVE_TIMEOUT_S} 秒——转码速度跟不上播放，或转码已中断`;
}

/** `currentTime` 前方的连续缓冲秒数；不在任何缓冲区间内记 0。 */
export function bufferedAhead(video: {
  currentTime: number;
  buffered: { length: number; start(i: number): number; end(i: number): number };
}): number {
  for (let i = 0; i < video.buffered.length; i += 1) {
    if (video.buffered.start(i) <= video.currentTime && video.buffered.end(i) >= video.currentTime) {
      return video.buffered.end(i) - video.currentTime;
    }
  }
  return 0;
}
