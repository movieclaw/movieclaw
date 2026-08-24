"use client";

/**
 * 条目页的音轨 / 字幕摘要区（电影与剧集共用）。
 *
 * 为什么不是一排徽章：
 * - 一个文件可能带十几条同语言字幕（内封 + 外挂 + AI 生成）。逐条铺徽章时，
 *   摘要行只是把同一个答案（「有中文字幕」）重复十遍；真正有区分度的格式、
 *   来源、文件名反而全埋在 Tooltip 里，一次只能看一条，移动端还悬不出来。
 * - 所以折叠态按**语言**去重成芯片（`简体中文 ×4`），完整轨道下沉到一个
 *   按语言分组的列表：桌面走浮层，移动走底部抽屉，同一份列表两种承载。
 * - 芯片统一中性色。codec 家族的彩色只留在展开态的格式色块上——它在那里才
 *   有区分作用，铺十几个彩点在剧照上只是噪音。
 *
 * 多文件（多版本 / 多分集）时先选物理文件再看轨道，避免不同版本的语言与格式
 * 混在一起。
 */

import { useEffect, useMemo, useRef, useState } from "react";

import {
  FloatingFocusManager,
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  shift,
  size,
  useDismiss,
  useFloating,
  useInteractions,
  useRole,
} from "@floating-ui/react";

import { useConfirm, useToast } from "@/components/feedback";
import { TrashIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import { SubtitleGenPanel } from "@/components/subtitle-gen-panel";
import { SubtitlePreviewDialog } from "@/components/subtitle-preview-dialog";
import {
  type AudioStream,
  type LibraryItemFile,
  type SubtitleStream,
  deleteExternalSubtitle,
} from "@/lib/api/libraries";
import { formatVideoResolution } from "@/lib/format";
import { usePermissions } from "@/lib/permissions";
import { useIsMobile } from "@/lib/use-media-query";

/* ------------------------------------------------------------------------ */
/* 展示格式化：ffprobe 原始值 → 用户认知的规格语言                              */
/* ------------------------------------------------------------------------ */

/** 音轨编码的完整读法，用在列表行的主文案上。 */
const AUDIO_CODEC_LABELS: Record<string, string> = {
  aac: "AAC",
  ac3: "Dolby Digital",
  eac3: "Dolby Digital+",
  truehd: "Dolby TrueHD",
  dts: "DTS",
  flac: "FLAC",
  opus: "Opus",
  mp3: "MP3",
  vorbis: "Vorbis",
};

/** 同一批编码的紧凑写法，只用在列表行首那枚格式色块上（放不下完整读法）。 */
const AUDIO_FORMAT_TOKENS: Record<string, string> = {
  aac: "AAC",
  ac3: "AC3",
  eac3: "EAC3",
  truehd: "TrueHD",
  dts: "DTS",
  flac: "FLAC",
  opus: "Opus",
  mp3: "MP3",
  vorbis: "Vorbis",
};

const SUBTITLE_CODEC_LABELS: Record<string, string> = {
  subrip: "SRT",
  srt: "SRT",
  ass: "ASS",
  ssa: "SSA",
  hdmv_pgs_subtitle: "PGS",
  dvd_subtitle: "VobSub",
  mov_text: "Text",
  webvtt: "VTT",
  vtt: "VTT",
  sub: "VobSub",
  sup: "PGS",
};

const LANGUAGE_LABELS: Record<string, string> = {
  chs: "简体中文",
  cht: "繁体中文",
  chi: "中文",
  zho: "中文",
  cmn: "中文",
  yue: "粤语",
  eng: "英语",
  jpn: "日语",
  kor: "韩语",
  fre: "法语",
  fra: "法语",
  ger: "德语",
  deu: "德语",
  spa: "西班牙语",
  rus: "俄语",
  ita: "意大利语",
  por: "葡萄牙语",
  tha: "泰语",
  hin: "印地语",
};

function languageLabel(code: string | null): string | null {
  if (!code || code === "und") return null;
  return LANGUAGE_LABELS[code.toLowerCase()] ?? code;
}

/**
 * 语言组的排序权重，越小越靠前。
 *
 * 这是一款中文软件，中文优先于其他语言；简繁刻意不合并——它们正是用户
 * 真正要区分的东西。未在表内的语言统一落到 OTHER，双语与未标语言垫底。
 */
const LANGUAGE_RANK: Record<string, number> = {
  chs: 1,
  cht: 2,
  chi: 3,
  zho: 3,
  cmn: 3,
  yue: 4,
  eng: 5,
  jpn: 6,
  kor: 7,
};
const OTHER_LANGUAGE_RANK = 40;
const BILINGUAL_RANK = 80;
const UNKNOWN_LANGUAGE_RANK = 90;
const UNKNOWN_LANGUAGE = "未标语言";

function languageRank(code: string | null): number {
  if (!code || code === "und") return UNKNOWN_LANGUAGE_RANK;
  return LANGUAGE_RANK[code.toLowerCase()] ?? OTHER_LANGUAGE_RANK;
}

/** 声道数 → 惯用布局标签（channel_layout 可用时优先，去掉 (side) 等后缀）。 */
function channelsLabel(stream: AudioStream): string | null {
  const layout = stream.channel_layout?.split("(")[0]?.trim();
  if (layout && /^\d/.test(layout)) return layout;
  const map: Record<number, string> = { 1: "单声道", 2: "2.0", 6: "5.1", 7: "6.1", 8: "7.1" };
  if (stream.channels == null) return null;
  return map[stream.channels] ?? `${stream.channels} 声道`;
}

// 这些 profile 值只是编码内部档次（如 AAC 的 LC、HEVC 的 Main 10），单独展示
// 反而让人困惑——只有 DTS-HD MA 这类"比 codec 更有信息量"的 profile 才顶替 codec
const GENERIC_PROFILES = new Set(["lc", "main", "high", "baseline", "main 10"]);

function informativeProfile(stream: AudioStream): string | null {
  if (!stream.profile) return null;
  return GENERIC_PROFILES.has(stream.profile.toLowerCase()) ? null : stream.profile;
}

/** 音轨 → 列表行主文案：格式（有信息量的 profile 优先）· 声道。 */
function audioSpecLabel(stream: AudioStream): string {
  const codec =
    informativeProfile(stream) ||
    (stream.codec ? (AUDIO_CODEC_LABELS[stream.codec.toLowerCase()] ?? stream.codec.toUpperCase()) : "未知格式");
  return [codec, channelsLabel(stream)].filter(Boolean).join(" · ");
}

/**
 * 音轨行右端常显的最高规格。
 *
 * 音轨没有可点动作，列表纯只读，摘要行必须自己回答"这片音质到什么档次"——
 * 这比"有几种语言"更接近用户真正的决策依据。评分先看编码档次再看声道数。
 */
const AUDIO_CODEC_TIER: Record<string, number> = {
  truehd: 5,
  dts: 4,
  eac3: 3,
  flac: 3,
  ac3: 2,
  opus: 1,
  aac: 1,
  vorbis: 0,
  mp3: 0,
};

function topAudioSpec(streams: AudioStream[]): string | null {
  // 只有一条音轨时芯片本身已经写了格式，再挂一枚徽章纯属重复
  if (streams.length < 2) return null;
  let best: AudioStream | null = null;
  let bestScore = -1;
  for (const stream of streams) {
    const tier = AUDIO_CODEC_TIER[stream.codec?.toLowerCase() ?? ""] ?? 0;
    const score = tier * 100 + (stream.channels ?? 0);
    if (score > bestScore) {
      bestScore = score;
      best = stream;
    }
  }
  if (!best) return null;
  const codec =
    informativeProfile(best) ||
    (best.codec ? (AUDIO_CODEC_LABELS[best.codec.toLowerCase()] ?? best.codec.toUpperCase()) : null);
  return [codec, channelsLabel(best)].filter(Boolean).join(" ") || null;
}

/** AI 生成字幕的标题里编了目标语言，翻译回中文读法（双语保留两个语言）。 */
function generatedSubtitleLabel(stream: SubtitleStream): string | null {
  const title = stream.title?.toLowerCase();
  if (!title) return null;
  if (title.startsWith("ai-bilingual-")) {
    const languages = title.slice("ai-bilingual-".length).split("-");
    if (languages.length === 2) {
      return `${languageLabel(languages[0]) ?? languages[0]} + ${languageLabel(languages[1]) ?? languages[1]}`;
    }
  }
  if (title === "ai-chs") return "简体中文";
  if (title === "ai-cht") return "繁体中文";
  return null;
}

/** 字幕 → 分组用的语言名（AI 产物的语言写在标题里，优先认它）。 */
function subtitleLanguageName(stream: SubtitleStream): string {
  return generatedSubtitleLabel(stream) ?? languageLabel(stream.language) ?? stream.title?.trim() ?? UNKNOWN_LANGUAGE;
}

function subtitleFormatToken(stream: SubtitleStream): string {
  if (!stream.codec) return "字幕";
  return SUBTITLE_CODEC_LABELS[stream.codec.toLowerCase()] ?? stream.codec.toUpperCase();
}

/** 字幕色块只承担格式识别，颜色刻意保持低饱和，避免重新变成徽章墙。 */
enum TrackTone {
  Srt = "srt",
  Ass = "ass",
  Pgs = "pgs",
  VobSub = "vobsub",
  Text = "text",
  Ai = "ai",
  Other = "other",
}

const TRACK_TONE_CLASS: Record<TrackTone, string> = {
  [TrackTone.Srt]: "bg-sky-300/[0.1] text-sky-100/90",
  [TrackTone.Ass]: "bg-violet-300/[0.1] text-violet-100/90",
  [TrackTone.Pgs]: "bg-amber-300/[0.1] text-amber-100/90",
  [TrackTone.VobSub]: "bg-orange-300/[0.1] text-orange-100/90",
  [TrackTone.Text]: "bg-cyan-300/[0.1] text-cyan-100/90",
  // AI 产物沿用主按钮的冷银色，与其他格式色块保持同一层级
  [TrackTone.Ai]: "bg-[#cdd6e6]/[0.12] text-[#eef2f8]/90",
  [TrackTone.Other]: "bg-white/[0.065] text-white/75",
};

function isAiSubtitle(stream: SubtitleStream): boolean {
  const title = stream.title?.toLowerCase() ?? "";
  const fileTokens = stream.file_name?.toLowerCase().split(".") ?? [];
  return (
    title.startsWith("ai-") || fileTokens.some((token) => token === "ai" || token.startsWith("ai-"))
  );
}

function subtitleTone(stream: SubtitleStream): TrackTone {
  if (isAiSubtitle(stream)) return TrackTone.Ai;
  switch (stream.codec?.toLowerCase()) {
    case "subrip":
    case "srt":
      return TrackTone.Srt;
    case "ass":
    case "ssa":
      return TrackTone.Ass;
    case "hdmv_pgs_subtitle":
    case "sup":
      return TrackTone.Pgs;
    case "dvd_subtitle":
    case "sub":
      return TrackTone.VobSub;
    case "mov_text":
    case "webvtt":
    case "vtt":
      return TrackTone.Text;
    default:
      return TrackTone.Other;
  }
}

/** 音轨与字幕共用低饱和色系，颜色只帮助快速区分常见编码族。 */
function audioTone(stream: AudioStream): TrackTone {
  switch (stream.codec?.toLowerCase()) {
    case "aac":
    case "mp3":
      return TrackTone.Srt;
    case "truehd":
      return TrackTone.Ass;
    case "ac3":
    case "eac3":
      return TrackTone.Pgs;
    case "dts":
      return TrackTone.VobSub;
    case "flac":
    case "opus":
    case "vorbis":
      return TrackTone.Text;
    default:
      return TrackTone.Other;
  }
}

function externalSubtitleSuffix(stream: SubtitleStream): string {
  const tokens = stream.file_name?.toLowerCase().split(".") ?? [];
  if (tokens.some((token) => token === "ai" || token.startsWith("ai-"))) return "AI 外挂";
  if (tokens.includes("pgs-ocr")) return "PGS 转换";
  return "外挂";
}

/**
 * 外挂字幕的行内标签：去掉与视频同名的那段前缀，只留区分用的 token 段。
 *
 * 同一部影片的外挂字幕**全都**以视频文件名开头（发现规则就是这么定的），
 * 完整文件名放进列表行里，一列截断下来几条长得一模一样——真正的区分信息
 * （chs / ai-chs / 简体.default）全在被截掉的尾部。手机抽屉里尤其致命：
 * 删除要的就是"我删的是哪一条"。完整路径仍会在删除确认框里给全。
 */
function externalSubtitleLabel(fileName: string | null, videoStem: string): string {
  if (!fileName) return "外挂字幕";
  if (videoStem && fileName.toLowerCase().startsWith(videoStem.toLowerCase())) {
    const rest = fileName.slice(videoStem.length).replace(/^\./, "");
    if (rest) return rest;
  }
  return fileName;
}

/** 外挂字幕与视频同目录，据此还原它的完整路径（删除确认框要摆给用户看）。 */
function siblingPath(videoPath: string, filename: string): string {
  const cut = Math.max(videoPath.lastIndexOf("/"), videoPath.lastIndexOf("\\"));
  return cut < 0 ? filename : `${videoPath.slice(0, cut + 1)}${filename}`;
}

/** 字幕预览接口使用的中性轨引用：内封按序号，外挂按文件名。 */
function subtitleTrackRef(stream: SubtitleStream, index: number): string | null {
  if (!stream.external) return `embedded:${index}`;
  return stream.file_name ? `external:${stream.file_name}` : null;
}

/* ------------------------------------------------------------------------ */
/* 中性轨道模型：音轨与字幕拍平成同一种结构，分组/排序/渲染只写一份             */
/* ------------------------------------------------------------------------ */

interface TrackEntry {
  key: string;
  /** 分组键：面向用户的语言名（chi/zho/cmn 会并成同一个「中文」） */
  language: string;
  /** 语言排序权重，越小越靠前 */
  rank: number;
  /** 行首色块文案：SRT / ASS / PGS / TrueHD… */
  format: string;
  tone: TrackTone;
  /** 主文案（会截断的那一列）：音轨给规格，字幕给文件名或内封轨号 */
  primary: string;
  /** 次文案：音轨给轨号，字幕给来源（内封 / 外挂 / AI 外挂） */
  secondary: string;
  /** 属性标记：默认 / 强制 */
  flags: string[];
  /** 是否外挂文件；音轨没有内封/外挂之分，恒为 null */
  external: boolean | null;
  /** 组内排序：默认 → 内封 → 外挂 → AI 生成 */
  order: number;
  /** 可删除的外挂字幕文件名；内封轨与音轨为 null（长在容器里，删不掉） */
  deletable: string | null;
  /** 仅字幕有：点击后打开预览弹窗所需的一切 */
  preview: { stream: SubtitleStream; track: string; label: string } | null;
}

function audioEntries(streams: AudioStream[]): TrackEntry[] {
  return streams.map((stream, index) => {
    const spec = audioSpecLabel(stream);
    const title = stream.title?.trim();
    return {
      key: `audio:${index}`,
      language: languageLabel(stream.language) ?? UNKNOWN_LANGUAGE,
      rank: languageRank(stream.language),
      format: stream.codec
        ? (AUDIO_FORMAT_TOKENS[stream.codec.toLowerCase()] ?? stream.codec.toUpperCase())
        : "音轨",
      tone: audioTone(stream),
      primary: title ? `${spec} · ${title}` : spec,
      secondary: `轨 ${index + 1}`,
      flags: stream.default ? ["默认"] : [],
      external: null,
      order: stream.default ? 0 : 1,
      deletable: null,
      preview: null,
    };
  });
}

function subtitleEntries(streams: SubtitleStream[], videoStem: string): TrackEntry[] {
  // 内封轨在界面上按"内封里的第几条"编号，而不是在混合数组里的下标——
  // subtitle_streams 里内封与外挂是混在一起的，直接用下标会给出错误的轨号
  let embeddedOrdinal = 0;
  return streams.map((stream, index) => {
    const language = subtitleLanguageName(stream);
    const format = subtitleFormatToken(stream);
    const external = stream.external;
    if (!external) embeddedOrdinal += 1;
    const track = subtitleTrackRef(stream, index);
    const label = [language, format].filter(Boolean).join(" · ");
    return {
      key: `subtitle:${track ?? `unknown:${index}`}`,
      language,
      rank: subtitleRank(stream, language),
      format,
      tone: subtitleTone(stream),
      primary: external
        ? externalSubtitleLabel(stream.file_name, videoStem)
        : `内封轨 ${embeddedOrdinal}`,
      secondary: external ? externalSubtitleSuffix(stream) : "内封",
      flags: [stream.default ? "默认" : null, stream.forced ? "强制" : null].filter(
        (flag): flag is string => flag !== null,
      ),
      external,
      order: stream.default ? 0 : isAiSubtitle(stream) ? 3 : external ? 2 : 1,
      // 外挂（含 AI 生成）是磁盘上的独立文件，可以单独删；内封轨要删得
      // 重封装视频本体，不给入口
      deletable: external ? (stream.file_name ?? null) : null,
      preview: track ? { stream, track, label } : null,
    };
  });
}

function subtitleRank(stream: SubtitleStream, language: string): number {
  if (language.includes(" + ")) return BILINGUAL_RANK;
  if (language === UNKNOWN_LANGUAGE) return UNKNOWN_LANGUAGE_RANK;
  const byCode = languageRank(stream.language);
  if (byCode !== UNKNOWN_LANGUAGE_RANK) return byCode;
  // AI 产物的 language 字段常常是空的，语言只写在标题里，按中文名反查权重
  const code = Object.keys(LANGUAGE_LABELS).find((key) => LANGUAGE_LABELS[key] === language);
  return code ? languageRank(code) : OTHER_LANGUAGE_RANK;
}

interface LanguageGroup {
  language: string;
  entries: TrackEntry[];
  rank: number;
  hasDefault: boolean;
}

/**
 * 按语言分组并排序。
 *
 * 语言组：默认轨所在语言 → 简中 → 繁中 → 中文/粤语 → 其余按条数降序 →
 * 双语与未标语言垫底。组内：默认 → 内封 → 外挂 → AI 生成。
 * ffprobe 的原始顺序本身就是"看起来乱"的一部分，这里统一重排。
 */
function groupByLanguage(entries: TrackEntry[]): LanguageGroup[] {
  const buckets = new Map<string, TrackEntry[]>();
  for (const entry of entries) {
    const bucket = buckets.get(entry.language);
    if (bucket) bucket.push(entry);
    else buckets.set(entry.language, [entry]);
  }
  const groups = [...buckets].map(([language, list]) => ({
    language,
    entries: [...list].sort((a, b) => a.order - b.order),
    rank: Math.min(...list.map((entry) => entry.rank)),
    hasDefault: list.some((entry) => entry.order === 0),
  }));
  groups.sort(
    (a, b) =>
      Number(b.hasDefault) - Number(a.hasDefault) ||
      a.rank - b.rank ||
      b.entries.length - a.entries.length ||
      a.language.localeCompare(b.language, "zh-Hans-CN"),
  );
  return groups;
}

/* ------------------------------------------------------------------------ */
/* 组件                                                                       */
/* ------------------------------------------------------------------------ */

export function MediaTrackRows({
  files,
  selectedFileId,
  onSelectedFileIdChange,
  onChanged,
}: {
  files: LibraryItemFile[];
  selectedFileId: number | null;
  onSelectedFileIdChange: (fileId: number) => void;
  onChanged?: () => void;
}) {
  // 只认在位文件：缺失与待回收的版本都不该出现在音轨/字幕选择器里
  const availableFiles = files.filter((file) => file.state === "in_place");
  const [previewTarget, setPreviewTarget] = useState<{
    file: LibraryItemFile;
    stream: SubtitleStream;
    track: string;
    label: string;
  } | null>(null);
  const confirm = useConfirm();
  const toast = useToast();
  // 删磁盘文件是管理动作：成员看不到入口（后端同样只对管理员开放）
  const { canManageLibraries } = usePermissions();

  // 文件刷新或删除后，若原选择已不存在，直接派生回第一项，无需额外 effect。
  const selectedFile =
    availableFiles.find((file) => file.id === selectedFileId) ?? availableFiles[0];

  const audioGroups = useMemo(
    () => groupByLanguage(audioEntries(selectedFile?.audio_streams ?? [])),
    [selectedFile?.audio_streams],
  );
  // 外挂字幕的行内标签要减掉这段前缀（见 externalSubtitleLabel）
  const videoStem = (selectedFile?.file_name ?? "").replace(/\.[^.]+$/, "");
  const subtitleGroups = useMemo(
    () => groupByLanguage(subtitleEntries(selectedFile?.subtitle_streams ?? [], videoStem)),
    [selectedFile?.subtitle_streams, videoStem],
  );

  /**
   * 删除一条外挂字幕：先把**完整路径**摆给用户确认，同意后直接删磁盘文件。
   *
   * 删除不进回收站也没有撤销，所以确认框列的是真实路径而不是"这条字幕"——
   * 同一部影片常有一堆同语言字幕，只有路径能让用户确认删的是哪一个。
   */
  const requestDelete = async (entry: TrackEntry) => {
    const filename = entry.deletable;
    if (!filename || !selectedFile) return;
    const agreed = await confirm({
      title: "删除这个字幕文件？",
      description: "将从磁盘直接删除下面的文件，删除后无法恢复：",
      bullets: [siblingPath(selectedFile.file_path, filename)],
      confirmLabel: "删除字幕",
      tone: "danger",
    });
    if (!agreed) return;
    try {
      await deleteExternalSubtitle(selectedFile.id, filename);
      toast.success(`已删除字幕：${filename}`);
      onChanged?.();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "字幕删除失败，请稍后重试");
    }
  };

  if (availableFiles.length === 0 || !selectedFile) return null;
  const multipleVersions = availableFiles.length > 1;
  const fileOptionLabel = (file: LibraryItemFile) => {
    const resolution = file.resolution ? formatVideoResolution(file.resolution) : null;
    return resolution ? `${resolution} — ${file.file_name}` : file.file_name;
  };
  const audioSpec = topAudioSpec(selectedFile.audio_streams ?? []);

  return (
    <>
      <div className="mt-4 max-w-4xl space-y-2.5">
        {multipleVersions && (
          <select
            aria-label="选择视频文件"
            value={selectedFile.id}
            onChange={(event) => onSelectedFileIdChange(Number(event.target.value))}
            className="w-full rounded-xl border border-white/[0.08] bg-white/[0.055] px-3.5 py-2 text-sub text-white/90 outline-none transition focus:border-white/25 focus:bg-white/[0.08] [&>option]:bg-[#181c28]"
          >
            {availableFiles.map((file) => (
              <option key={file.id} value={file.id}>
                {fileOptionLabel(file)}
              </option>
            ))}
          </select>
        )}

        <TrackRow
          key={`${selectedFile.id}:audio`}
          label="音轨"
          kind="音轨"
          groups={audioGroups}
          trailing={
            audioSpec ? (
              <span className="tnum shrink-0 rounded-[7px] md:ml-auto border border-white/[0.14] bg-white/[0.04] px-2.5 py-1 text-caption font-semibold text-white/80">
                {audioSpec}
              </span>
            ) : null
          }
          empty={
            selectedFile.audio_streams === null ? "尚未探测" : "文件内没有音轨"
          }
        />

        <TrackRow
          key={`${selectedFile.id}:subtitle`}
          label="字幕"
          kind="字幕"
          groups={subtitleGroups}
          footer={
            canManageLibraries
              ? "点任意一条打开字幕预览；外挂字幕可就地删除"
              : "点任意一条打开字幕预览"
          }
          onSelect={(entry) => {
            if (!entry.preview) return;
            setPreviewTarget({ file: selectedFile, ...entry.preview });
          }}
          onDelete={canManageLibraries ? (entry) => void requestDelete(entry) : undefined}
          trailing={
            <div className="flex shrink-0 items-center md:ml-auto">
              <SubtitleGenPanel key={selectedFile.id} file={selectedFile} onChanged={onChanged} />
            </div>
          }
          empty="无内封或外挂字幕"
        />
      </div>

      {previewTarget && (
        <SubtitlePreviewDialog
          open
          file={previewTarget.file}
          stream={previewTarget.stream}
          track={previewTarget.track}
          label={previewTarget.label}
          onClose={() => setPreviewTarget(null)}
          onChanged={onChanged}
        />
      )}
    </>
  );
}

/** 折叠态芯片：统一中性色，颜色留给展开态的格式色块。 */
const CHIP_CLASS =
  "tnum inline-flex h-7 items-center gap-1.5 rounded-[7px] bg-white/[0.075] px-2.5 " +
  "text-caption font-medium leading-none text-white/85 backdrop-blur-md " +
  "shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] " +
  "transition-[transform,background-color] hover:-translate-y-px hover:bg-white/[0.13] " +
  "aria-expanded:bg-white/[0.16] max-md:h-8 " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-2)]";

/**
 * 一行轨道摘要：左侧行名，中间语言芯片，右侧挂行级尾件（最高规格 / AI 生成）。
 *
 * 展开态在桌面是锚在整行芯片下方的浮层，在移动端是底部抽屉（复用 Modal 的
 * max-md 抽屉形态）。两种承载共用同一份 TrackList，只有外壳不同。
 */
function TrackRow({
  label,
  kind,
  groups,
  empty,
  footer,
  trailing,
  onSelect,
  onDelete,
}: {
  label: string;
  kind: string;
  groups: LanguageGroup[];
  empty: string;
  footer?: string;
  trailing?: React.ReactNode;
  onSelect?: (entry: TrackEntry) => void;
  onDelete?: (entry: TrackEntry) => void;
}) {
  const isMobile = useIsMobile();
  const [open, setOpen] = useState(false);
  // 打开时高亮被点的那个语言组，让"点了哪个芯片"和"在看哪一组"对得上
  const [focusLanguage, setFocusLanguage] = useState<string | null>(null);
  // 浮层的初始焦点：直接给被点的那个语言组的组头。
  // 这一处踩过两次坑——默认焦点落在浮层外壳上、或落在滚动容器自己身上，两种
  // 情况下浏览器都会把内部滚动位置复位到顶部，正好抵消掉"滚到被点的语言组"；
  // 而完全禁用初始焦点（initialFocus={-1}）又会让键盘 Tab 直接跳过整个面板。
  // 把焦点放进滚动内容里，浏览器自己就会把它滚进视野，两件事不再互相打架。
  const scrollRef = useRef<HTMLDivElement>(null);
  const focusRef = useRef<HTMLDivElement>(null);

  // floating-ui 的 returnFocus 认的是 reference 元素——这里是整行芯片的容器，
  // 它本身不可聚焦，关闭后焦点会落到行内第一枚芯片而不是刚才按的那枚，
  // 所以自己记住触发者并在关闭时收回焦点。
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const changeOpen = (next: boolean) => {
    setOpen(next);
    if (!next) triggerRef.current?.focus();
  };

  const { refs, floatingStyles, context } = useFloating({
    open: open && !isMobile,
    onOpenChange: changeOpen,
    placement: "bottom-start",
    middleware: [
      offset(8),
      flip({ padding: 12 }),
      shift({ padding: 12 }),
      // 高度跟着视口可用空间走：条目页的轨道行常常已经靠近视口下沿，不限高
      // 面板会被截断；同时给一个 21rem 的设计上限，别让长列表铺满整屏。
      // 直接写 style 而不是塞进 state——autoUpdate 每帧都会调用 apply。
      size({
        padding: 12,
        apply({ availableHeight, elements }) {
          elements.floating.style.maxHeight = `${Math.min(336, Math.max(200, availableHeight))}px`;
        },
      }),
    ],
    whileElementsMounted: autoUpdate,
  });
  const { getFloatingProps } = useInteractions([
    useDismiss(context, {
      // 删除确认框是 body 级 portal，对浮层来说算"外部点击"——不特判的话
      // 用户一点确认，整个字幕面板就被收起，接着清理下一条还得重新展开
      outsidePress: (event) =>
        !(event.target as Element | null)?.closest?.('[role="dialog"][aria-modal="true"]'),
    }),
    useRole(context, { role: "dialog" }),
  ]);

  // 桌面 3 枚、移动 2 枚，其余折成「+N 种」——移动端一行放不下三枚还带尾件
  const maxChips = isMobile ? 2 : 3;
  const shown = groups.slice(0, maxChips);
  const hidden = groups.slice(maxChips);
  const total = groups.reduce((sum, group) => sum + group.entries.length, 0);

  // 点已经展开的那枚芯片收起面板；点另一枚则原地换到对应语言组
  const toggleAt = (language: string, trigger: HTMLButtonElement) => {
    if (open && focusLanguage === language) {
      changeOpen(false);
      return;
    }
    triggerRef.current = trigger;
    setFocusLanguage(language);
    setOpen(true);
  };

  const list = (
    <TrackList
      scrollRef={scrollRef}
      focusRef={focusRef}
      kind={kind}
      groups={groups}
      total={total}
      focusLanguage={focusLanguage}
      footer={footer}
      onSelect={
        onSelect
          ? (entry) => {
              changeOpen(false);
              onSelect(entry);
            }
          : undefined
      }
      // 删除不收起面板：确认框叠在上层，删完还能接着清理同一组里的其他字幕
      onDelete={onDelete}
    />
  );

  return (
    <div className="flex items-baseline gap-4">
      <span className="w-10 shrink-0 text-sub text-[var(--text-faint)]">{label}</span>
      <div ref={refs.setReference} className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
        {groups.length === 0 ? (
          <span className="text-sub text-[var(--text-muted)]">{empty}</span>
        ) : (
          <>
            {shown.map((group) => (
              <button
                key={group.language}
                type="button"
                className={CHIP_CLASS}
                aria-haspopup="dialog"
                aria-expanded={open && focusLanguage === group.language}
                onClick={(event) => toggleAt(group.language, event.currentTarget)}
              >
                <span>{group.language}</span>
                {group.entries.length === 1 ? (
                  <span className="text-micro font-semibold text-white/50">
                    {group.entries[0]!.format}
                  </span>
                ) : (
                  <span className="font-semibold text-white/50">×{group.entries.length}</span>
                )}
              </button>
            ))}
            {hidden.length > 0 && (
              <button
                type="button"
                className={`${CHIP_CLASS} !bg-white/[0.045] text-[var(--text-muted)] hover:!bg-white/[0.1]`}
                aria-haspopup="dialog"
                aria-expanded={open && focusLanguage === hidden[0]!.language}
                onClick={(event) => toggleAt(hidden[0]!.language, event.currentTarget)}
              >
                +{hidden.length} 种
              </button>
            )}
          </>
        )}
        {trailing}
      </div>

      {open && !isMobile && (
        <FloatingPortal>
          <FloatingFocusManager context={context} modal={false} initialFocus={focusRef} returnFocus={false}>
            <div
              ref={refs.setFloating}
              style={floatingStyles}
              aria-label={`${kind}列表`}
              className="menu-surface z-50 flex w-[27rem] max-w-[calc(100vw-1.5rem)] flex-col overflow-hidden"
              {...getFloatingProps()}
            >
              {list}
            </div>
          </FloatingFocusManager>
        </FloatingPortal>
      )}

      {isMobile && (
        <Modal
          open={open}
          onClose={() => changeOpen(false)}
          label={`${kind}列表`}
          width="lg"
          panelClassName="flex max-h-[78dvh] flex-col"
        >
          {list}
        </Modal>
      )}
    </div>
  );
}

/** 展开态列表本体：桌面浮层与移动抽屉共用，两边的差异只在外壳。 */
function TrackList({
  scrollRef,
  focusRef,
  kind,
  groups,
  total,
  focusLanguage,
  footer,
  onSelect,
  onDelete,
}: {
  scrollRef: React.RefObject<HTMLDivElement | null>;
  focusRef: React.RefObject<HTMLDivElement | null>;
  kind: string;
  groups: LanguageGroup[];
  total: number;
  focusLanguage: string | null;
  footer?: string;
  onSelect?: (entry: TrackEntry) => void;
  onDelete?: (entry: TrackEntry) => void;
}) {
  const entries = groups.flatMap((group) => group.entries);
  // 音轨的 external 恒为 null：它没有内封/外挂之分，表头就不占这一格
  const hasSource = entries.some((entry) => entry.external !== null);
  const external = entries.filter((entry) => entry.external).length;

  // 被点的语言组滚进视野。
  // - 放在 effect 里而不是内联 ref 回调上：内联回调每次渲染都是新函数，会在每
  //   次重渲染时重复滚动。
  // - 再推迟一帧：FloatingFocusManager 在本轮 effect 之后才聚焦浮层容器，那次
  //   聚焦会把滚动位置拉回顶部，先滚就白滚了。
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const scroller = scrollRef.current;
      const group = focusRef.current;
      if (!scroller || !group) return;
      scroller.scrollTop +=
        group.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 6;
    });
    return () => cancelAnimationFrame(frame);
  }, [focusLanguage, focusRef, scrollRef]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-baseline justify-between gap-4 border-b border-white/[0.08] px-4 py-2.5">
        <strong className="text-ui font-semibold text-[var(--text)]">
          {kind} · 共 {total} 条
        </strong>
        {hasSource && (
          <span className="tnum text-caption text-[var(--text-faint)]">
            内封 {total - external} · 外挂 {external}
          </span>
        )}
      </div>

      <div
        ref={scrollRef}
        tabIndex={-1}
        className="scroll-thin min-h-0 flex-1 overflow-y-auto p-1.5 outline-none"
      >
        {groups.map((group) => (
          <div
            key={group.language}
            // 被点的那一组常亮一层淡底：不用定时闪烁，滚过去之后依然认得出
            className={`rounded-[10px] p-0.5 ${
              group.language === focusLanguage ? "bg-[#cdd6e6]/[0.07]" : ""
            }`}
          >
            <div
              ref={group.language === focusLanguage ? focusRef : undefined}
              tabIndex={group.language === focusLanguage ? -1 : undefined}
              className="flex items-baseline gap-2 px-2.5 pb-1 pt-2 text-micro font-semibold uppercase tracking-[0.1em] text-[var(--text-faint)] outline-none">
              <span>{group.language}</span>
              <span className="tnum tracking-normal">· {group.entries.length}</span>
            </div>
            {group.entries.map((entry) => (
              <TrackLine
                key={entry.key}
                entry={entry}
                onSelect={entry.preview && onSelect ? () => onSelect(entry) : undefined}
                onDelete={entry.deletable && onDelete ? () => onDelete(entry) : undefined}
              />
            ))}
          </div>
        ))}
      </div>

      {footer && (
        <div className="border-t border-white/[0.08] px-4 py-2.5 text-caption text-[var(--text-faint)]">
          {footer}
        </div>
      )}
    </div>
  );
}

/**
 * 列表里的一条轨。
 *
 * 音轨没有可点动作，渲染成 div 而不是禁用按钮——禁用态对读屏是"这里本来
 * 有个操作但你用不了"，而它本来就只是一条信息。
 *
 * 删除键只挂在外挂字幕（含 AI 生成）上：内封轨要删得重封装视频本体，与
 * "删一个 sidecar 文件"完全不是一回事，索性不给入口。它是独立按钮而不是
 * 整行的第二种点击语义——预览与删除挨在一起，误触的代价不对称。
 */
function TrackLine({
  entry,
  onSelect,
  onDelete,
}: {
  entry: TrackEntry;
  onSelect?: () => void;
  onDelete?: () => void;
}) {
  const content = (
    <>
      <span
        className={`inline-flex h-5 items-center justify-center rounded-[5px] px-1.5 text-micro font-bold leading-none ${TRACK_TONE_CLASS[entry.tone]}`}
      >
        {entry.format}
      </span>
      <span className="truncate text-sub text-white/88">{entry.primary}</span>
      <span className="flex items-center gap-1.5">
        <span className="text-caption text-[var(--text-faint)]">{entry.secondary}</span>
        {entry.flags.map((flag) => (
          <span
            key={flag}
            className="rounded-[5px] bg-white/[0.07] px-1.5 py-0.5 text-micro font-semibold text-[var(--text-muted)]"
          >
            {flag}
          </span>
        ))}
        {onSelect && (
          // 行尾「动作格」：可删的行由垃圾桶占这一格（浮在其上，见下方），
          // 不可删的行放可点箭头。两者尺寸相同，所以行与行的右缘天然对齐，
          // 既不用为删除键额外留一条空沟，也不会让有删除键的行被挤窄。
          // 触屏没有 hover，箭头在移动端常显；桌面只在悬停/聚焦时出现。
          <span
            aria-hidden="true"
            className="flex size-6 shrink-0 items-center justify-center text-sub text-[var(--accent-2)] max-md:size-7"
          >
            {!onDelete && (
              <span className="opacity-0 transition-opacity group-hover/line:opacity-100 group-focus-visible/line:opacity-100 max-md:opacity-100">
                ›
              </span>
            )}
          </span>
        )}
      </span>
    </>
  );

  const className =
    "grid min-w-0 flex-1 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2.5 " +
    "rounded-[9px] px-2.5 py-1.5 text-left max-md:py-2.5";

  // 两层 group：外层 row 管垃圾桶的显隐（悬停行内任意处都算），内层 line
  // 仍挂在可点区域上——箭头的"悬停/键盘聚焦才出现"沿用原来的判定。
  // 行高亮也放在外层，垃圾桶才落在高亮块内、看着属于这一行。
  return (
    <div className="group/row relative flex items-center rounded-[9px] transition-colors hover:bg-white/[0.075] has-[:focus-visible]:bg-white/[0.075]">
      {onSelect ? (
        <button
          type="button"
          onClick={onSelect}
          aria-label={`预览字幕：${entry.language} · ${entry.format} · ${entry.primary}`}
          className={`group/line ${className} focus-visible:outline-none`}
        >
          {content}
        </button>
      ) : (
        <div className={`group/line ${className}`}>{content}</div>
      )}
      {onDelete && (
        // 浮在行尾动作格之上，不占流式空间（!absolute 是必需的：.touch-target
        // 在移动端会把元素设成 relative 以锚定撑命中区的伪元素，会顶掉普通 absolute）——占位由动作格本身承担，行与行
        // 因此始终对齐（沿用侧栏会话行 ⋯ 的做法）。桌面悬停/聚焦才显形（十几条
        // 字幕各挂一个垃圾桶只是噪音）；触屏没有 hover，.touch-reveal 让它常显，
        // .touch-target 再把命中区悄悄撑到 44×44，外观不变。
        <button
          type="button"
          onClick={onDelete}
          title="删除这个字幕文件"
          aria-label={`删除字幕文件：${entry.primary}`}
          className="touch-reveal touch-target !absolute right-2.5 top-1/2 flex size-6 -translate-y-1/2 items-center justify-center rounded-[7px] text-white/35 opacity-0 transition hover:bg-[var(--danger)]/15 hover:text-[#ff9f9f] focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--danger)]/50 group-hover/row:opacity-100 max-md:size-7"
        >
          <TrashIcon className="size-3.5 max-md:size-4" />
        </button>
      )}
    </div>
  );
}
