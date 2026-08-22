/**
 * 音轨菜单（docs/design/web-player.md §6.9）。
 *
 * 换音轨与换字幕**完全不是一回事**，这也是这个模块存在的理由：
 *
 * - 字幕是旁挂的，切换只是换一份文本，播放流不动；
 * - 音轨在开会话时就被 ffmpeg `-map` 进命令里了，**改不了**。所以换轨必须
 *   带着当前位置重开一个会话——和 seek 拖出已转区间走的是同一条路。
 *
 * 由此还有一条反直觉的后果：选了非默认音轨会把档 0（原文件直出）顶成档 1，
 * 因为直出时整份文件交给 `<video>`，浏览器只放容器里的默认轨，我们选的那条
 * 根本到不了用户耳朵里。判定在服务端（`decide.py`），这里只负责让用户看懂
 * 有哪几条轨、现在放的是哪条。
 */

import type { AudioTrack } from "@/lib/api/playback";

import { languageLabel } from "../language-labels.ts";

export interface AudioOption {
  /** 中性轨引用，同时用作 React key 与轨记忆的值 */
  ref: string;
  label: string;
  isDefault: boolean;
}

/** 声道数 → 人话。5.1 / 7.1 是通用写法，别写「6 声道」。 */
const CHANNEL_LABELS: Record<number, string> = {
  1: "单声道",
  2: "立体声",
  6: "5.1",
  8: "7.1",
};

function channelLabel(channels: number | null): string | null {
  if (!channels) return null;
  return CHANNEL_LABELS[channels] ?? `${channels} 声道`;
}

/**
 * 一条音轨的显示名：语言 · 编码 · 声道。
 *
 * 语言放最前面——多音轨片子里用户找的是「国语还是日语」，编码和声道是次要
 * 信息。没有语言标记时退回序号，不能一律叫「未知语言」：那样多条无标记的轨
 * 会长得一模一样，菜单等于没得选。
 */
export function audioTrackLabel(track: AudioTrack): string {
  const name = languageLabel(track.language) ?? refLabel(track.ref);
  const rest = [(track.codec || "").toUpperCase() || null, channelLabel(track.channels)].filter(
    Boolean,
  );
  return rest.length ? `${name} · ${rest.join(" · ")}` : name;
}

function refLabel(ref: string): string {
  if (ref.startsWith("embedded:")) return `音轨 ${ref.slice("embedded:".length)}`;
  return "未知音轨";
}

/**
 * 决策里的候选音轨 → 菜单项。
 *
 * **只有一条轨时返回空列表**：没得选的菜单是纯噪音，调用方据此把整个按钮
 * 藏起来。绝大多数片子只有一条音轨，多音轨才是需要这个入口的那少数。
 */
export function planAudioOptions(tracks: AudioTrack[] | undefined): AudioOption[] {
  if (!tracks || tracks.length < 2) return [];
  return tracks.map((track) => ({
    ref: track.ref,
    label: audioTrackLabel(track),
    isDefault: track.is_default,
  }));
}
