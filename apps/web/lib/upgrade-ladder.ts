import type { RuleSetSpec } from "@/lib/api/subscriptions";

/**
 * 洗版阶梯预览（docs/design/quality-upgrade.md §2.1 / §14.3）：把规则组的洗版
 * 配置翻译成一条**从终点往下数**的档位清单。
 *
 * 为什么需要它：开了洗版之后，规则组就不再是一个"筛子"而是一条**有序阶梯**
 * ——后端按维度字典序逐位比较，先分出高低的那一维说了算。用一行"洗到 1080p
 * 蓝光"概括，用户看不出两件最要命的事：
 *
 * - **终点之下还有多少档**：每上一档就是一次重复下载，这是"多加一维阶梯"的
 *   真实代价（§14.6），必须在配置现场就看得见；
 * - **哪些档其实已经算达标**：分辨率排在第一位时，任意 2160p 资源（哪怕是
 *   WEBRip）都优于 1080p Remux，洗版会直接停在那里。这个后果从"洗到 1080p
 *   蓝光"这句话里完全读不出来。
 *
 * 实现上就是把生效维度的偏好序当成一个**混合进制数**：每一维一位，位值 0 =
 * 该维最优。全序序号越小档位越高，于是"终点的下一档"就是"终点序号 + 1"——
 * 与后端的字典序比较天然同构，不需要再实现一遍比较逻辑。
 *
 * 口径与 `movieclaw_matcher.decision` 保持一致（跳过未配偏好的维度、目标取
 * cutoff/首项、片源档序）；这里只做展示，绝不参与任何决策。
 */

/** 参与洗版比较的维度选项（顺序即编辑器里的芯片顺序），值域与后端一致。 */
export const LADDER_OPTIONS: { value: string; label: string }[] = [
  { value: "resolution", label: "分辨率" },
  { value: "source", label: "片源" },
  { value: "hdr", label: "HDR" },
  { value: "video_codec", label: "编码" },
  { value: "platform", label: "平台" },
];

/** 与后端 decision._DEFAULT_LADDER 同值：不配 upgrade_ladder 时的二元组。 */
export const DEFAULT_LADDER = ["resolution", "source"];

/**
 * 编码按"家族"选择：词表把 x265 / H.265 / HEVC 归一成不同值，而用户想表达的
 * 是"要 H.265 这一族"——UI 选一族即把全部等价写法写进白名单，避免因发布组
 * 写法不同而漏掉资源；阶梯上它们也必须塌缩成同一位次（后端 _codec_ladder）。
 */
export const CODEC_FAMILIES: { label: string; values: string[] }[] = [
  { label: "H.265", values: ["x265", "H.265", "HEVC"] },
  { label: "H.264", values: ["x264", "H.264", "AVC"] },
  { label: "AV1", values: ["AV1"] },
];

/**
 * 片源档选项（值域与后端 MediaSourceTier 一致，顺序即内置档序 T5…T1）。
 * 同档的多种写法（WEBRip/BDRip 都是 T2）合成一项，用户不需要认识全部词；
 * T0（人工标注"按最低档"）不是一类可下载的资源，不进选项。
 */
export const MEDIA_SOURCE_OPTIONS: { value: string; label: string; hint: string }[] = [
  { value: "remux", label: "Remux", hint: "原盘直封" },
  { value: "blu-ray", label: "蓝光", hint: "Blu-ray / UHD Blu-ray" },
  { value: "web-dl", label: "WEB-DL", hint: "流媒体原流" },
  { value: "rip", label: "Rip 类", hint: "WEBRip / BDRip" },
  { value: "tv", label: "电视录制类", hint: "HDTV / DVD" },
];

/** 洗版目标可选的三档——没人会把终点设成"洗到 Rip"。 */
export const UPGRADE_SOURCE_VALUES = ["web-dl", "blu-ray", "remux"];

const SOURCE_LABEL: Record<string, string> = Object.fromEntries(
  MEDIA_SOURCE_OPTIONS.map((option) => [option.value, option.label]),
);

/** 未配置 resolutions 时的内置偏好序，与后端 _DEFAULT_RESOLUTION_LADDER 同值。 */
const DEFAULT_RESOLUTION_LADDER = [
  "4320p",
  "2160p",
  "1440p",
  "1080p",
  "720p",
  "576p",
  "480p",
];

/** 目标分辨率的兜底缺省（后端 _FALLBACK_CUTOFF_RESOLUTION）。 */
const FALLBACK_CUTOFF_RESOLUTION = "1080p";

/** 终点之下最多展示几档——再多就成了背不下来的表格。 */
const MAX_RUNGS_BELOW = 5;

export interface UpgradeLadderPreview {
  /** 生效的比较维度（中文名，顺序即位次） */
  dimensions: string[];
  /** 终点档标签，如 "1080p · 蓝光 · DV" */
  target: string;
  /** 阶梯上比终点更高的最高档；终点已是最高档时为 null */
  ceiling: string | null;
  /** 终点之下的过渡档（降序，最多 MAX_RUNGS_BELOW 条） */
  below: string[];
  /** 过渡档里没展示出来的剩余档数 */
  moreBelow: number;
}

/** 生效维度：偏好列表为空的位自动跳过（后端 effective_ladder 同口径）。 */
function effectiveDimensions(spec: RuleSetSpec): string[] {
  const dims = (spec.upgrade_ladder ?? DEFAULT_LADDER).filter(
    (dim) =>
      LADDER_OPTIONS.some((option) => option.value === dim) &&
      !(dim === "hdr" && !spec.hdr_levels?.length) &&
      !(dim === "video_codec" && !spec.video_codecs?.length) &&
      !(dim === "platform" && !spec.platforms?.length),
  );
  return dims.length ? dims : DEFAULT_LADDER;
}

/** 编码白名单 → 按族去重的偏好序（等价写法塌缩成一位）。 */
function codecAxis(codecs: string[]): string[] {
  const axis: string[] = [];
  for (const value of codecs) {
    const family =
      CODEC_FAMILIES.find((f) =>
        f.values.some((v) => v.toLowerCase() === value.toLowerCase()),
      )?.label ?? value;
    if (!axis.includes(family)) axis.push(family);
  }
  return axis;
}

/** 某一维的档位偏好序（高 → 低）。 */
function axisOf(
  dim: string,
  spec: RuleSetSpec,
  platformLabel: (id: string) => string,
): string[] {
  if (dim === "resolution") {
    return spec.resolutions?.length ? spec.resolutions : DEFAULT_RESOLUTION_LADDER;
  }
  if (dim === "source") {
    // 配了片源白名单就按用户序（后端 media_source_rank 同口径），否则内置档序
    const chosen = spec.media_sources?.length ? spec.media_sources : null;
    return (chosen ?? MEDIA_SOURCE_OPTIONS.map((o) => o.value)).map(
      (value) => SOURCE_LABEL[value] ?? value,
    );
  }
  if (dim === "hdr") return spec.hdr_levels ?? [];
  if (dim === "video_codec") return codecAxis(spec.video_codecs ?? []);
  return (spec.platforms ?? []).map(platformLabel);
}

/** 终点在某一维上的取值：分辨率取 cutoff、片源取洗版目标，其余维取偏好首项。 */
function targetIndexOf(dim: string, axis: string[], spec: RuleSetSpec): number {
  if (dim === "resolution") {
    const resolution =
      spec.cutoff_resolution || spec.resolutions?.[0] || FALLBACK_CUTOFF_RESOLUTION;
    return axis.indexOf(resolution);
  }
  if (dim === "source") {
    return axis.indexOf(SOURCE_LABEL[spec.upgrade_source ?? ""] ?? "");
  }
  // 只有分辨率与片源会成数量级地改变磁盘占用，才需要"接受但不主动洗"的
  // 区分；其余维度的终点就是偏好首项（后端 target_vector 同口径）
  return 0;
}

/**
 * spec → 洗版阶梯预览；未开洗版（或配置自相矛盾，如目标分辨率不在偏好序里）
 * 返回 null——这两种情况下没有"终点"可言，界面退回纯筛选摘要。
 *
 * `platformLabel` 由调用方注入：本模块要保持零运行时依赖（`node --test`
 * 直接跑 .ts，路径别名在那里解析不了）。
 */
export function upgradeLadderPreview(
  spec: RuleSetSpec,
  platformLabel: (id: string) => string = (id) => id,
): UpgradeLadderPreview | null {
  if (!spec.upgrade_source) return null;

  const dimensions = effectiveDimensions(spec);
  const axes = dimensions.map((dim) => axisOf(dim, spec, platformLabel));
  const digits = dimensions.map((dim, i) => targetIndexOf(dim, axes[i], spec));
  // 任一维取不到终点值 = 配置矛盾（校验层已拦，这里只做防御性静默）
  if (digits.some((index) => index < 0) || axes.some((axis) => !axis.length)) return null;

  const label = (combo: number[]) => combo.map((v, i) => axes[i][v]).join(" · ");
  // 混合进制：低位维度先降档，降到底再退高位一档——正是字典序的降序枚举
  const total = axes.reduce((acc, axis) => acc * axis.length, 1);
  const ordinalOfTarget = digits.reduce(
    (acc, digit, i) => acc * axes[i].length + digit,
    0,
  );
  const decode = (ordinal: number): number[] => {
    const combo = axes.map(() => 0);
    let rest = ordinal;
    for (let i = axes.length - 1; i >= 0; i -= 1) {
      combo[i] = rest % axes[i].length;
      rest = Math.floor(rest / axes[i].length);
    }
    return combo;
  };

  const belowCount = total - 1 - ordinalOfTarget;
  const shown = Math.min(belowCount, MAX_RUNGS_BELOW);
  const below: string[] = [];
  for (let step = 1; step <= shown; step += 1) below.push(label(decode(ordinalOfTarget + step)));

  return {
    dimensions: dimensions.map(
      (dim) => LADDER_OPTIONS.find((option) => option.value === dim)?.label ?? dim,
    ),
    target: label(digits),
    ceiling: ordinalOfTarget > 0 ? label(axes.map(() => 0)) : null,
    below,
    moreBelow: belowCount - shown,
  };
}
