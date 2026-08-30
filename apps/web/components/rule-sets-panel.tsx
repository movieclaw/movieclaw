"use client";

import { useEffect, useMemo, useState } from "react";

import { useConfirm, useToast } from "@/components/feedback";
import { Modal } from "@/components/modal";
import {
  createRuleSet,
  deleteRuleSet,
  listRuleSets,
  setDefaultRuleSet,
  updateRuleSet,
  type RuleSet,
  type RuleSetSpec,
} from "@/lib/api/subscriptions";
import { PLATFORM_OPTIONS, platformLabel } from "@/lib/platforms";
import {
  CODEC_FAMILIES,
  DEFAULT_LADDER,
  LADDER_OPTIONS,
  MEDIA_SOURCE_OPTIONS,
  upgradeLadderPreview,
  type UpgradeLadderPreview,
} from "@/lib/upgrade-ladder";

/**
 * 规则组管理（设置 → 订阅规则 → 规则组）：Web 端唯一的规则组配置入口。
 *
 * 设计原则：
 * - **表单化，不让用户手写 JSON**——spec 的每个维度铺成对应控件；
 * - **列表顺序即偏好**：分辨率按点击顺序入列（与后端评分语义一致），
 *   选中的芯片上标序号，先选的优先；
 * - **摘要芯片**：清单里把 spec 翻译成人话（"2160p > 1080p · 仅免费"），
 *   看一眼就知道每个组在过滤什么；
 * - **复制微调**：订阅不做 per-订阅 override，"想微调就复制一个规则组"是
 *   既定产品哲学——复制按钮预填原组条件，改名保存即成新组；
 * - **删除保护前置**：默认组与被订阅引用的组直接禁用删除按钮并说明原因，
 *   不让用户点了再吃后端 409。
 *
 * specSummary 与 RuleSetEditorDialog 导出复用：订阅弹窗（选组时看清内容、
 * 快捷新建）与订阅详情页（换组）共用同一套摘要与编辑器。
 */

/** 编辑器打开参数：ruleSet 有值=编辑；null=新建，template 提供预填（复制场景）。 */
interface EditorTarget {
  ruleSet: RuleSet | null;
  template: { name: string; spec: RuleSetSpec } | null;
}

export function RuleSetsPanel() {
  const confirm = useConfirm();
  const toast = useToast();
  const [ruleSets, setRuleSets] = useState<RuleSet[] | null>(null);
  const [editing, setEditing] = useState<EditorTarget | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    void listRuleSets()
      .then((rows) => {
        setRuleSets(rows);
        setError(null);
      })
      .catch(() => setRuleSets([]));
  };

  useEffect(reload, []);

  const makeDefault = async (rs: RuleSet) => {
    try {
      await setDefaultRuleSet(rs.id);
      toast.success(`「${rs.name}」已设为默认规则组`);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "设置失败，请稍后重试");
    }
  };

  const remove = async (rs: RuleSet) => {
    if (
      !(await confirm({
        title: `删除规则组「${rs.name}」？`,
        confirmLabel: "删除",
        tone: "danger",
      }))
    ) {
      return;
    }
    try {
      await deleteRuleSet(rs.id);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "删除失败，请稍后重试");
    }
  };

  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-body font-semibold text-white/90">规则组</h3>
        <button
          type="button"
          onClick={() => setEditing({ ruleSet: null, template: null })}
          className="btn-glass px-3 py-1.5 text-sub font-medium"
        >
          + 新建规则组
        </button>
      </div>
      <p className="mb-4 text-sub leading-6 text-[var(--text-muted)]">
        规则组定义「什么样的资源可接受」——硬性条件（分辨率、编码、体积、免费等）
        与偏好顺序，在订阅弹窗中按订阅选用；标「默认」的组是新订阅的初始选择。
        修改只影响之后的资源评估，已下载的内容不受影响。
      </p>

      {error && (
        <p className="mb-3 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-sub leading-6 text-red-200">
          {error}
        </p>
      )}

      {ruleSets === null ? (
        <p className="rounded-xl bg-white/[0.03] px-4 py-4 text-ui text-[var(--text-muted)]">
          正在加载…
        </p>
      ) : (
        <div className="space-y-1.5">
          {ruleSets.map((rs) => {
            const chips = specSummary(rs.spec);
            const deleteBlock = rs.is_default
              ? "默认规则组不可删除"
              : rs.reference_count > 0
                ? `正被 ${rs.reference_count} 个订阅使用，先把它们改到其他规则组`
                : null;
            return (
              <div
                key={rs.id}
                className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-2.5"
              >
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-2">
                    <span className="truncate text-ui font-medium text-white/90">{rs.name}</span>
                    {rs.is_default && (
                      <span className="shrink-0 rounded-full border border-white/[0.14] bg-white/[0.1] px-2 py-0.5 text-micro font-semibold text-white/80">
                        默认
                      </span>
                    )}
                    {rs.reference_count > 0 && (
                      <span className="tnum shrink-0 text-caption text-[var(--text-faint)]">
                        {rs.reference_count} 个订阅使用中
                      </span>
                    )}
                  </p>
                  <p className="mt-1 flex flex-wrap gap-1.5">
                    {chips.length === 0 ? (
                      <span className="text-caption text-[var(--text-faint)]">
                        全不限：任何识别为本条目的资源都可接受
                      </span>
                    ) : (
                      chips.map((chip) => (
                        <span
                          key={chip}
                          className="rounded-md bg-white/[0.07] px-1.5 py-0.5 text-caption text-white/75"
                        >
                          {chip}
                        </span>
                      ))
                    )}
                  </p>
                </div>
                {!rs.is_default && (
                  <button
                    type="button"
                    title="新订阅未指定规则组时使用本组（不改已有订阅）"
                    onClick={() => void makeDefault(rs)}
                    className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium"
                  >
                    设为默认
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setEditing({ ruleSet: rs, template: null })}
                  className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium"
                >
                  编辑
                </button>
                <button
                  type="button"
                  title="以本组条件为底新建一个规则组"
                  onClick={() =>
                    setEditing({
                      ruleSet: null,
                      template: { name: `${rs.name} 副本`, spec: rs.spec },
                    })
                  }
                  className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium"
                >
                  复制
                </button>
                <button
                  type="button"
                  disabled={deleteBlock !== null}
                  title={deleteBlock ?? undefined}
                  onClick={() => void remove(rs)}
                  className="shrink-0 rounded-full border border-red-400/25 bg-red-500/[0.08] px-3 py-1.5 text-sub font-medium text-red-200/90 transition hover:bg-red-500/20 disabled:cursor-not-allowed disabled:opacity-35"
                >
                  删除
                </button>
              </div>
            );
          })}
        </div>
      )}

      {editing !== null && (
        <RuleSetEditorDialog
          ruleSet={editing.ruleSet}
          template={editing.template ?? undefined}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// spec → 摘要芯片
// ---------------------------------------------------------------------------

/** 分辨率可选项（词表归一值里的常用档；其余档位罕见，需要时再扩）。 */
const RESOLUTION_OPTIONS = ["2160p", "1080p", "720p"];

// 语言值与 movieclaw_enrich.lang_decl 的 BCP 47 输出对齐；任一命中即过。
// "zh" 前缀匹配简/繁/未标简繁的一切中文声明——日常"要中文字幕"选它就够
const SUB_LANG_OPTIONS: [string, string][] = [
  ["zh", "中文（不限简繁）"],
  ["zh-Hans", "简体中文"],
  ["zh-Hant", "繁体中文"],
  ["en", "英文"],
];
const AUDIO_LANG_OPTIONS: [string, string][] = [
  ["cmn", "国语"],
  ["yue", "粤语"],
  ["ja", "日语"],
  ["en", "英语"],
];

/**
 * 洗版目标片源档的三个选项就是全部预设（docs/design/quality-upgrade.md §8.2）
 * ——没有需要照抄的社区配置。目标分辨率缺省取分辨率偏好的第一位。
 */
const UPGRADE_OPTIONS: [
  NonNullable<RuleSetSpec["upgrade_source"]> & string,
  string,
][] = [
  ["web-dl", "洗到 WEB-DL"],
  ["blu-ray", "洗到蓝光"],
  ["remux", "洗到 Remux"],
];
const UPGRADE_SOURCE_LABEL: Record<string, string> = {
  "web-dl": "WEB-DL",
  "blu-ray": "蓝光",
  remux: "Remux",
};

/** 片源档规范值 → 展示名；未知值原样返回（与 platformLabel 同一取向）。 */
const mediaSourceLabel = (value: string): string =>
  MEDIA_SOURCE_OPTIONS.find((option) => option.value === value)?.label ?? value;

/**
 * HDR 值域（顺序即编辑器里的芯片顺序）。"SDR" 是哨兵值 = 资源未标注任何
 * HDR 格式——命名惯例里 HDR 属"缺席即否定"的强标记。
 */
const HDR_OPTIONS = ["DV", "HDR10+", "HDR10", "HLG", "SDR"];

/**
 * 旧三态字段 → (白名单, 黑名单)。与后端 models._hdr_from_legacy 同一张表，
 * 属兼容层：存量规则组保存过一次后就带上新键了，旧键退休时这段一起删。
 */
function hdrFromLegacy(spec: RuleSetSpec): { levels: string[]; block: string[] } {
  if (spec.hdr_levels?.length || spec.hdr_block?.length) {
    return { levels: spec.hdr_levels ?? [], block: spec.hdr_block ?? [] };
  }
  const family = HDR_OPTIONS.filter((v) => v !== "SDR");
  if (spec.hdr === "forbid") return { levels: ["SDR"], block: [] };
  if (spec.dv === "require") return { levels: ["DV"], block: [] };
  if (spec.hdr === "require") {
    return spec.dv === "forbid"
      ? { levels: family.filter((v) => v !== "DV"), block: ["DV"] }
      : { levels: family, block: [] };
  }
  if (spec.dv === "forbid") return { levels: [], block: ["DV"] };
  return { levels: [], block: [] };
}

/**
 * HDR 白名单 → 摘要文案。整族与纯 SDR 这两种最常见的配置（也是旧 hdr 三态
 * 换算出来的形态）说人话，避免清单里出现 "HDR DV/HDR10+/HDR10/HLG" 这种
 * 又长又没信息量的芯片。
 */
function hdrChipText(levels: string[]): string {
  const family = HDR_OPTIONS.filter((v) => v !== "SDR");
  if (levels.length === family.length && family.every((v) => levels.includes(v))) {
    return "必须 HDR";
  }
  if (levels.length === 1 && levels[0] === "SDR") return "排除 HDR";
  return `HDR ${levels.join("/")}`;
}

const sameLadder = (a: string[], b: string[]) =>
  a.length === b.length && a.every((v, i) => v === b[i]);

/** 洗版目标的人话标签（摘要芯片 / 订阅详情页规则组 fact 复用）。 */
export function upgradeTargetLabel(spec: RuleSetSpec): string | null {
  if (!spec.upgrade_source) return null;
  const resolution = spec.cutoff_resolution || spec.resolutions?.[0] || "1080p";
  const parts = [
    `${resolution} ${UPGRADE_SOURCE_LABEL[spec.upgrade_source] ?? spec.upgrade_source}`,
  ];
  // 与后端 decision.upgrade_target_label 同一口径：只渲染"进了阶梯且配了偏好"
  // 的维度（偏好为空的位后端会自动跳过）
  const ladder = spec.upgrade_ladder ?? DEFAULT_LADDER;
  if (ladder.includes("hdr") && spec.hdr_levels?.length) {
    parts.push(spec.hdr_levels[0]);
  }
  if (ladder.includes("video_codec") && spec.video_codecs?.length) {
    parts.push(spec.video_codecs[0]);
  }
  if (ladder.includes("platform") && spec.platforms?.length) {
    parts.push(platformLabel(spec.platforms[0]));
  }
  return parts.join(" · ");
}

/** spec → 人话芯片列表（清单摘要 / 订阅弹窗与详情页复用；空数组=全不限）。
 *  `withoutUpgrade`：编辑器底部把洗版单独渲染成阶梯，那里不再要这枚芯片。 */
export function specSummary(
  spec: RuleSetSpec,
  { withoutUpgrade = false }: { withoutUpgrade?: boolean } = {},
): string[] {
  const chips: string[] = [];
  if (spec.resolutions?.length) chips.push(spec.resolutions.join(" > "));
  if (spec.media_sources?.length) {
    chips.push(spec.media_sources.map(mediaSourceLabel).join(" > "));
  }
  const upgradeTarget = withoutUpgrade ? null : upgradeTargetLabel(spec);
  if (upgradeTarget) {
    chips.push(`洗到 ${upgradeTarget}${spec.upgrade_keep_old ? "（保留旧版）" : ""}`);
  }
  if (spec.video_codecs?.length) {
    const rest = new Set(spec.video_codecs);
    const labels: string[] = [];
    for (const family of CODEC_FAMILIES) {
      if (family.values.some((v) => rest.has(v))) {
        labels.push(family.label);
        family.values.forEach((v) => rest.delete(v));
      }
    }
    labels.push(...rest);
    chips.push(labels.join("/"));
  }
  if (spec.platforms?.length) {
    chips.push(`平台: ${spec.platforms.map(platformLabel).join("/")}`);
  }
  if (spec.platforms_block?.length) {
    chips.push(`排除平台: ${spec.platforms_block.map(platformLabel).join("/")}`);
  }
  const { levels: hdrLevels, block: hdrBlock } = hdrFromLegacy(spec);
  if (hdrLevels.length) chips.push(hdrChipText(hdrLevels));
  if (hdrBlock.length) chips.push(`排除 ${hdrBlock.join("/")}`);
  if (spec.subtitle_languages_require?.length)
    chips.push(
      `字幕: ${spec.subtitle_languages_require.map((v) => SUB_LANG_OPTIONS.find(([code]) => code === v)?.[1] ?? v).join("/")}`,
    );
  if (spec.audio_languages_require?.length)
    chips.push(
      `音轨: ${spec.audio_languages_require.map((v) => AUDIO_LANG_OPTIONS.find(([code]) => code === v)?.[1] ?? v).join("/")}`,
    );
  if (spec.free_only) chips.push("仅免费");
  if (spec.min_seeders != null) chips.push(`做种 ≥ ${spec.min_seeders}`);
  if (spec.size_min_mb != null || spec.size_max_mb != null) {
    const min = spec.size_min_mb != null ? `${spec.size_min_mb}` : "";
    const max = spec.size_max_mb != null ? `${spec.size_max_mb}` : "";
    chips.push(min && max ? `单集 ${min}–${max}MB` : min ? `单集 ≥ ${min}MB` : `单集 ≤ ${max}MB`);
  }
  if (spec.exclude_hr)
    chips.push(spec.hr_unknown_policy === "strict" ? "排除 H&R（未知也排）" : "排除 H&R");
  if (spec.release_groups_allow?.length)
    chips.push(`制作组白名单 ${spec.release_groups_allow.length} 个`);
  if (spec.release_groups_block?.length)
    chips.push(`制作组黑名单 ${spec.release_groups_block.length} 个`);
  return chips;
}

// ---------------------------------------------------------------------------
// 编辑器弹窗
// ---------------------------------------------------------------------------

export function RuleSetEditorDialog({
  ruleSet,
  template,
  raised = false,
  onClose,
  onSaved,
}: {
  /** null = 新建 */
  ruleSet: RuleSet | null;
  /** 新建时的预填内容（复制场景）；编辑时忽略 */
  template?: { name: string; spec: RuleSetSpec };
  /** 叠在其他弹窗之上时置 true（如订阅弹窗里快捷新建） */
  raised?: boolean;
  onClose: () => void;
  /** 保存成功回调，参数是后端返回的最新规则组（快捷新建场景要选中它） */
  onSaved: (saved: RuleSet) => void;
}) {
  const spec = useMemo<RuleSetSpec>(
    () => ruleSet?.spec ?? template?.spec ?? {},
    [ruleSet, template],
  );

  const [name, setName] = useState(ruleSet?.name ?? template?.name ?? "");
  const [resolutions, setResolutions] = useState<string[]>(spec.resolutions ?? []);
  const [codecFamilies, setCodecFamilies] = useState<Set<string>>(
    () =>
      new Set(
        CODEC_FAMILIES.filter((f) =>
          f.values.some((v) => (spec.video_codecs ?? []).includes(v)),
        ).map((f) => f.label),
      ),
  );
  // 平台白/黑名单：界面只维护"常驻选项"部分，其余值走 platformExtras 透传
  const [platformsAllow, setPlatformsAllow] = useState<string[]>(() =>
    (spec.platforms ?? []).filter((v) => PLATFORM_OPTIONS.includes(v)),
  );
  const [platformsBlock, setPlatformsBlock] = useState<string[]>(() =>
    (spec.platforms_block ?? []).filter((v) => PLATFORM_OPTIONS.includes(v)),
  );
  const [mediaSources, setMediaSources] = useState<string[]>(spec.media_sources ?? []);
  const [upgradeLadder, setUpgradeLadder] = useState<string[]>(
    () => spec.upgrade_ladder ?? [...DEFAULT_LADDER],
  );
  const [hdrLevels, setHdrLevels] = useState<string[]>(() => hdrFromLegacy(spec).levels);
  const [hdrBlock, setHdrBlock] = useState<string[]>(() => hdrFromLegacy(spec).block);
  const [subLangs, setSubLangs] = useState<string[]>(
    spec.subtitle_languages_require ?? [],
  );
  const [audioLangs, setAudioLangs] = useState<string[]>(
    spec.audio_languages_require ?? [],
  );
  const [freeOnly, setFreeOnly] = useState(spec.free_only ?? false);
  const [excludeHr, setExcludeHr] = useState(spec.exclude_hr ?? false);
  const [hrStrict, setHrStrict] = useState(spec.hr_unknown_policy === "strict");
  const [minSeeders, setMinSeeders] = useState(spec.min_seeders?.toString() ?? "");
  const [sizeMin, setSizeMin] = useState(spec.size_min_mb?.toString() ?? "");
  const [sizeMax, setSizeMax] = useState(spec.size_max_mb?.toString() ?? "");
  const [groupsAllow, setGroupsAllow] = useState((spec.release_groups_allow ?? []).join(", "));
  const [groupsBlock, setGroupsBlock] = useState((spec.release_groups_block ?? []).join(", "));
  // 洗版目标：""=不洗版（唯一的洗版配置点，docs/design/quality-upgrade.md §8.2）
  const [upgradeSource, setUpgradeSource] = useState<string>(spec.upgrade_source ?? "");
  const [upgradeKeepOld, setUpgradeKeepOld] = useState<boolean>(spec.upgrade_keep_old ?? false);
  const [cutoffResolution, setCutoffResolution] = useState<string>(
    spec.cutoff_resolution ?? "",
  );
  // 洗版优先级默认收起：只想"洗到 Remux"的人不该被维度排序打断，
  // 但也不能藏起来——折叠头直接显示当前顺序，需要时一点就开
  const [ladderOpen, setLadderOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 编码族之外的原有值（手工经 API 写入的自定义编码）：编辑时不丢
  const codecExtras = useMemo(
    () =>
      (spec.video_codecs ?? []).filter(
        (v) => !CODEC_FAMILIES.some((f) => f.values.includes(v)),
      ),
    [spec],
  );

  // 常驻选项之外的既有平台值（经 API 配置的小众平台）：编辑时不丢
  const platformExtras = useMemo(
    () => ({
      allow: (spec.platforms ?? []).filter((v) => !PLATFORM_OPTIONS.includes(v)),
      block: (spec.platforms_block ?? []).filter((v) => !PLATFORM_OPTIONS.includes(v)),
    }),
    [spec],
  );

  const platformState = (value: string): "off" | "include" | "exclude" =>
    platformsAllow.includes(value)
      ? "include"
      : platformsBlock.includes(value)
        ? "exclude"
        : "off";

  /** 三态循环：不限 → 只要 → 排除 → 不限。同一平台因此不可能同时进白/黑名单
   *  （后端读取路径有意保持宽容、不对矛盾配置报错，矛盾在这里从源头消除）。 */
  const cyclePlatform = (value: string) => {
    const state = platformState(value);
    if (state === "off") {
      setPlatformsAllow((prev) => [...prev, value]);
    } else if (state === "include") {
      setPlatformsAllow((prev) => prev.filter((v) => v !== value));
      setPlatformsBlock((prev) => [...prev, value]);
    } else {
      setPlatformsBlock((prev) => prev.filter((v) => v !== value));
    }
  };

  const hdrState = (value: string): "off" | "include" | "exclude" =>
    hdrLevels.includes(value) ? "include" : hdrBlock.includes(value) ? "exclude" : "off";

  const cycleHdr = (value: string) => {
    const state = hdrState(value);
    if (state === "off") {
      setHdrLevels((prev) => [...prev, value]);
    } else if (state === "include") {
      setHdrLevels((prev) => prev.filter((v) => v !== value));
      setHdrBlock((prev) => [...prev, value]);
    } else {
      setHdrBlock((prev) => prev.filter((v) => v !== value));
    }
  };

  /** 阶梯维度：交互与分辨率偏好完全一致——点击依次入列，序号即位次。
   *  最后一维不允许移除：空阶梯没有任何一位可比，后端会回落到缺省二元组，
   *  界面上却显示"一个都没选"——不让用户进入这种口径不一致的状态。 */
  const toggleLadderDim = (value: string) =>
    setUpgradeLadder((prev) => {
      if (!prev.includes(value)) return [...prev, value];
      return prev.length > 1 ? prev.filter((v) => v !== value) : prev;
    });

  const labelOf = (options: readonly (readonly [string, string])[], values: string[]) =>
    values.map((v) => options.find(([code]) => code === v)?.[1] ?? v).join("/");

  // 折叠段的实时摘要：不展开也知道里面配了什么，这是长表单能收起来的前提
  const qualitySummary =
    [
      codecFamilies.size ? [...codecFamilies].join("/") : "",
      platformsAllow.length ? platformsAllow.map(platformLabel).join("/") : "",
      platformsBlock.length ? `排除 ${platformsBlock.map(platformLabel).join("/")}` : "",
      hdrLevels.length ? hdrChipText(hdrLevels) : "",
      hdrBlock.length ? `排除 ${hdrBlock.join("/")}` : "",
      groupsAllow.trim() ? `组 ${groupsAllow.trim()}` : "",
      groupsBlock.trim() ? `排除组 ${groupsBlock.trim()}` : "",
    ]
      .filter(Boolean)
      .join(" · ") || "未设置";

  const limitSummary =
    [
      subLangs.length ? `字幕 ${labelOf(SUB_LANG_OPTIONS, subLangs)}` : "",
      audioLangs.length ? `音轨 ${labelOf(AUDIO_LANG_OPTIONS, audioLangs)}` : "",
      freeOnly ? "仅免费" : "",
      excludeHr ? "排除 H&R" : "",
      minSeeders.trim() ? `做种 ≥ ${minSeeders.trim()}` : "",
      sizeMin.trim() || sizeMax.trim()
        ? `体积 ${sizeMin.trim() || "0"}–${sizeMax.trim() || "∞"}MB`
        : "",
    ]
      .filter(Boolean)
      .join(" · ") || "未设置";

  /** 某维度是否还没配偏好——后端会把这种位直接跳过。 */
  const ladderUnconfigured = (dim: string) =>
    (dim === "hdr" && !hdrLevels.length) ||
    (dim === "video_codec" && !codecFamilies.size && !codecExtras.length) ||
    (dim === "platform" && !platformsAllow.length && !platformExtras.allow.length);

  // 名称维度上浮的告警条件（§14.4）：平台识别率最低，排在前面会截断整条比较。
  // 按**生效**维度判断——被跳过的维度不占位，否则"平台后面跟着一个未配置的
  // 编码位"会误报
  const effectiveLadder = upgradeLadder.filter((dim) => !ladderUnconfigured(dim));
  const ladderText = effectiveLadder
    .map((dim) => LADDER_OPTIONS.find((o) => o.value === dim)?.label ?? dim)
    .join(" › ");
  const platformIndex = effectiveLadder.indexOf("platform");
  const platformNotLast =
    platformIndex >= 0 && platformIndex < effectiveLadder.length - 1;

  const toggleResolution = (value: string) =>
    setResolutions((prev) =>
      prev.includes(value) ? prev.filter((r) => r !== value) : [...prev, value],
    );

  /** 片源档：交互与分辨率偏好完全一致（点击依次入列，序号即位次）。
   *  收窄白名单时若洗版终点被排除在外，直接把终点清掉——不让用户进入
   *  "保存时才报错"的状态（与平台三态那处"矛盾在源头消除"同一取向）。 */
  const toggleMediaSource = (value: string) =>
    setMediaSources((prev) => {
      const next = prev.includes(value)
        ? prev.filter((v) => v !== value)
        : [...prev, value];
      if (upgradeSource && next.length && !next.includes(upgradeSource)) {
        setUpgradeSource("");
      }
      return next;
    });

  const toggleCodecFamily = (label: string) =>
    setCodecFamilies((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });

  /**
   * 表单状态 → spec 的**唯一**构造点：保存与底部"这条规则会这样筛选"共用它，
   * 保证用户看到的摘要就是将要存下去的东西。校验错误随返回值给出——memo 里
   * 不能 setState，也正好把校验变成纯函数。
   */
  const draft = useMemo<{ spec: RuleSetSpec; error: string | null }>(() => {
    const parseInt_ = (text: string): number | undefined => {
      const n = Number.parseInt(text.trim(), 10);
      return Number.isFinite(n) && n >= 0 ? n : undefined;
    };
    const parseGroups = (text: string): string[] =>
      text
        .split(/[,，、\s]+/)
        .map((g) => g.trim())
        .filter(Boolean);

    const next: RuleSetSpec = {};
    if (resolutions.length) next.resolutions = resolutions;
    if (mediaSources.length) next.media_sources = mediaSources;
    const codecs = [
      ...CODEC_FAMILIES.filter((f) => codecFamilies.has(f.label)).flatMap((f) => f.values),
      ...codecExtras,
    ];
    if (codecs.length) next.video_codecs = codecs;
    // 与缺省一致就不写进 spec——spec 只记录用户真的改过的东西
    if (upgradeSource !== "" && !sameLadder(upgradeLadder, DEFAULT_LADDER)) {
      next.upgrade_ladder = upgradeLadder;
    }
    const allowPlatforms = [...platformsAllow, ...platformExtras.allow];
    if (allowPlatforms.length) next.platforms = allowPlatforms;
    const blockPlatforms = [...platformsBlock, ...platformExtras.block];
    if (blockPlatforms.length) next.platforms_block = blockPlatforms;
    // 只写新键：旧的 hdr/dv 由后端在校验层反向回填（回退兼容），前端不掺和
    if (hdrLevels.length) next.hdr_levels = hdrLevels;
    if (hdrBlock.length) next.hdr_block = hdrBlock;
    if (subLangs.length) next.subtitle_languages_require = subLangs;
    if (audioLangs.length) next.audio_languages_require = audioLangs;
    if (freeOnly) next.free_only = true;
    if (excludeHr) {
      next.exclude_hr = true;
      if (hrStrict) next.hr_unknown_policy = "strict";
    }
    const seeders = parseInt_(minSeeders);
    if (seeders !== undefined && seeders > 0) next.min_seeders = seeders;
    const min = parseInt_(sizeMin);
    const max = parseInt_(sizeMax);
    if (min !== undefined && min > 0) next.size_min_mb = min;
    if (max !== undefined && max > 0) next.size_max_mb = max;
    if (min !== undefined && max !== undefined && min > 0 && max > 0 && min > max) {
      return { spec: next, error: "体积下限不能大于上限" };
    }
    const allow = parseGroups(groupsAllow);
    const block = parseGroups(groupsBlock);
    if (allow.length) next.release_groups_allow = allow;
    if (block.length) next.release_groups_block = block;
    if (upgradeSource) {
      if (mediaSources.length && !mediaSources.includes(upgradeSource)) {
        return { spec: next, error: "洗版目标片源必须在允许的片源范围内" };
      }
      next.upgrade_source = upgradeSource as RuleSetSpec["upgrade_source"];
      // 目标分辨率仅在偏离缺省（分辨率偏好首选）时写入；限定了分辨率时
      // 目标必须落在允许范围内（后端同样校验）
      if (cutoffResolution && cutoffResolution !== (resolutions[0] ?? "")) {
        if (resolutions.length && !resolutions.includes(cutoffResolution)) {
          return { spec: next, error: "洗版目标分辨率必须在允许的分辨率范围内" };
        }
        next.cutoff_resolution = cutoffResolution;
      }
      if (upgradeKeepOld) next.upgrade_keep_old = true;
    }
    // API 写入的预留字段（站点白名单）编辑时原样保留，不因 UI 保存而丢失
    if (spec.sites?.length) next.sites = spec.sites;
    return { spec: next, error: null };
  }, [
    resolutions,
    mediaSources,
    codecFamilies,
    codecExtras,
    upgradeSource,
    upgradeLadder,
    platformsAllow,
    platformsBlock,
    platformExtras,
    hdrLevels,
    hdrBlock,
    subLangs,
    audioLangs,
    freeOnly,
    excludeHr,
    hrStrict,
    minSeeders,
    sizeMin,
    sizeMax,
    groupsAllow,
    groupsBlock,
    cutoffResolution,
    upgradeKeepOld,
    spec.sites,
  ]);

  // 底部回执分两段：硬性筛选走芯片，洗版走阶梯（洗版开着时芯片里不再重复目标）
  const ladderPreview = useMemo(
    () => upgradeLadderPreview(draft.spec, platformLabel),
    [draft.spec],
  );
  const draftChips = useMemo(
    () => specSummary(draft.spec, { withoutUpgrade: ladderPreview !== null }),
    [draft.spec, ladderPreview],
  );

  const submit = async () => {
    if (draft.error) {
      setError(draft.error);
      return;
    }
    const next = draft.spec;
    setBusy(true);
    setError(null);
    try {
      const saved =
        ruleSet === null
          ? await createRuleSet(name.trim(), next)
          : await updateRuleSet(ruleSet.id, name.trim(), next);
      onSaved(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      label={ruleSet ? `编辑规则组「${ruleSet.name}」` : "新建规则组"}
      width="lg"
      raised={raised}
    >
      <div className="scroll-thin max-h-[76dvh] overflow-y-auto p-6 max-md:p-5">
        <h2 className="text-title font-bold text-white">
          {ruleSet ? "编辑规则组" : "新建规则组"}
        </h2>
        <p className="mt-1 text-sub text-[var(--text-muted)]">
          所有条件都可以留空 = 不限该维度；条件之间是「且」的关系。
        </p>
        {ruleSet !== null && ruleSet.reference_count > 0 && (
          <p className="mt-2 rounded-lg border border-amber-400/25 bg-amber-500/10 px-3.5 py-2.5 text-sub leading-6 text-amber-200">
            此组正被 {ruleSet.reference_count} 个订阅使用，保存后对它们之后的资源评估立即生效
            （已下载的内容不受影响）。
          </p>
        )}

        <div className="mt-4 space-y-5">
          <Field label="名称">
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="如：4K 免费、追剧省流"
              className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
            />
          </Field>

          <Field
            label="分辨率"
            hint="点击依次选择，先选的优先（选中顺序 = 下载偏好）；不选 = 不限。限定分辨率后，无法从种子名识别出分辨率的资源也会被排除，可用「手动选种」兜底"
          >
            <div className="flex flex-wrap gap-1.5">
              {RESOLUTION_OPTIONS.map((option) => {
                const index = resolutions.indexOf(option);
                return (
                  <ToggleChip
                    key={option}
                    active={index >= 0}
                    onClick={() => toggleResolution(option)}
                  >
                    {index >= 0 && resolutions.length > 1 && (
                      <span className="mr-1.5 inline-flex size-4 items-center justify-center rounded-full bg-white/20 text-micro font-semibold">
                        {index + 1}
                      </span>
                    )}
                    {option}
                  </ToggleChip>
                );
              })}
            </div>
          </Field>

          {/* 片源与分辨率是用户谈论"版本好坏"时实际用的两个主轴（§2.2），也是
              仅有的两个偏好序参与候选选优的维度，因此并排放在最外层；它还必须
              排在洗版之前——洗版终点档只能从这个白名单里挑 */}
          <Field
            label="片源"
            hint="点击依次选择，先选的优先（选中顺序 = 下载偏好）；不选 = 不限。Rip 类 = WEBRip/BDRip，电视录制类 = HDTV/DVD。限定片源后，无法从种子名识别出片源的资源也会被排除，可用「手动选种」兜底"
          >
            <div className="flex flex-wrap gap-1.5">
              {MEDIA_SOURCE_OPTIONS.map((option) => {
                const index = mediaSources.indexOf(option.value);
                return (
                  <ToggleChip
                    key={option.value}
                    active={index >= 0}
                    onClick={() => toggleMediaSource(option.value)}
                  >
                    {index >= 0 && mediaSources.length > 1 && (
                      <span className="mr-1.5 inline-flex size-4 items-center justify-center rounded-full bg-white/20 text-micro font-semibold">
                        {index + 1}
                      </span>
                    )}
                    {option.label}
                  </ToggleChip>
                );
              })}
            </div>
          </Field>

          <Section title="画质与来源" summary={qualitySummary}>
          <Field label="视频编码" hint="按家族选择，等价写法（如 x265 / HEVC）一并计入；不选 = 不限">
            <div className="flex flex-wrap gap-1.5">
              {CODEC_FAMILIES.map((family) => (
                <ToggleChip
                  key={family.label}
                  active={codecFamilies.has(family.label)}
                  onClick={() => toggleCodecFamily(family.label)}
                >
                  {family.label}
                </ToggleChip>
              ))}
              {codecExtras.length > 0 && (
                <span className="self-center text-caption text-[var(--text-faint)]">
                  另有自定义值：{codecExtras.join("、")}（保留）
                </span>
              )}
            </div>
          </Field>

          <Field
            label="流媒体平台"
            hint="点一下 = 只要，再点 = 排除，第三下取消。设了「只要」之后，识别不出平台的资源会被排除（与分辨率/编码口径一致）"
          >
            <div className="flex flex-wrap gap-1.5">
              {PLATFORM_OPTIONS.map((id) => (
                <TriChip
                  key={id}
                  state={platformState(id)}
                  onClick={() => cyclePlatform(id)}
                >
                  {platformLabel(id)}
                </TriChip>
              ))}
            </div>
            {(platformExtras.allow.length > 0 || platformExtras.block.length > 0) && (
              <p className="mt-1.5 text-caption text-[var(--text-faint)]">
                另有自定义值：
                {[...platformExtras.allow, ...platformExtras.block].map(platformLabel).join("、")}
                （保留）
              </p>
            )}
          </Field>

          <Field
            label="HDR"
            hint='点一下 = 只要，再点 = 排除，第三下取消。"SDR" 指资源没标注任何 HDR 格式；都不选 = 不限'
          >
            <div className="flex flex-wrap gap-1.5">
              {HDR_OPTIONS.map((value) => (
                <TriChip key={value} state={hdrState(value)} onClick={() => cycleHdr(value)}>
                  {value}
                </TriChip>
              ))}
            </div>
          </Field>

          <Field
            label="制作组白名单"
            hint="只接受这些制作组的资源，逗号或空格分隔（如 FRDS, WiKi）；留空 = 不限"
          >
            <input
              type="text"
              value={groupsAllow}
              onChange={(e) => setGroupsAllow(e.target.value)}
              placeholder="留空不限"
              className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
            />
          </Field>
          <Field label="制作组黑名单" hint="这些制作组的资源一律不要">
            <input
              type="text"
              value={groupsBlock}
              onChange={(e) => setGroupsBlock(e.target.value)}
              placeholder="留空不启用"
              className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
            />
          </Field>
          </Section>

          <Section title="下载与限制" summary={limitSummary}>
          <Field
            label="字幕语言"
            hint="任一命中即通过；按种子标题声明判断——未声明字幕的资源会被排除，不选 = 不限"
          >
            <div className="flex flex-wrap gap-1.5">
              {SUB_LANG_OPTIONS.map(([value, label]) => (
                <ToggleChip
                  key={value}
                  active={subLangs.includes(value)}
                  onClick={() =>
                    setSubLangs((prev) =>
                      prev.includes(value)
                        ? prev.filter((v) => v !== value)
                        : [...prev, value],
                    )
                  }
                >
                  {label}
                </ToggleChip>
              ))}
            </div>
          </Field>

          <Field
            label="音轨语言"
            hint="任一命中即通过；按种子标题声明判断——未声明音轨的资源会被排除，不选 = 不限"
          >
            <div className="flex flex-wrap gap-1.5">
              {AUDIO_LANG_OPTIONS.map(([value, label]) => (
                <ToggleChip
                  key={value}
                  active={audioLangs.includes(value)}
                  onClick={() =>
                    setAudioLangs((prev) =>
                      prev.includes(value)
                        ? prev.filter((v) => v !== value)
                        : [...prev, value],
                    )
                  }
                >
                  {label}
                </ToggleChip>
              ))}
            </div>
          </Field>

          <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
            <CheckRow
              label="只要免费资源"
              hint="促销状态未知的按非免费处理"
              checked={freeOnly}
              onChange={setFreeOnly}
            />
            <CheckRow
              label="排除 H&R 考核种子"
              hint="有做种考核要求的资源不下载"
              checked={excludeHr}
              onChange={setExcludeHr}
            />
          </div>
          {excludeHr && (
            <div className="-mt-2 rounded-xl bg-white/[0.03] px-4 py-2.5">
              <label className="flex cursor-pointer items-center justify-between">
                <span className="text-sub text-[var(--text-muted)]">
                  站点未提供 H&R 信息时，保守视作有考核而排除
                </span>
                <input
                  type="checkbox"
                  checked={hrStrict}
                  onChange={(e) => setHrStrict(e.target.checked)}
                  className="size-4 accent-[var(--accent-2)]"
                />
              </label>
            </div>
          )}

          <div className="grid grid-cols-3 gap-3 max-md:grid-cols-1">
            <Field label="做种数下限">
              <NumberInput value={minSeeders} onChange={setMinSeeders} placeholder="不限" />
            </Field>
            <Field label="单集体积下限 (MB)">
              <NumberInput value={sizeMin} onChange={setSizeMin} placeholder="不限" />
            </Field>
            <Field label="单集体积上限 (MB)">
              <NumberInput value={sizeMax} onChange={setSizeMax} placeholder="不限" />
            </Field>
          </div>
          <p className="-mt-3 text-caption leading-relaxed text-[var(--text-faint)]">
            体积按「每集均摊」评估：整季包用总体积 ÷ 集数比较，整季合集不会被单集上限误杀。
          </p>
          </Section>

          <Field
            label="洗版"
            hint="收齐后继续追更高版本，直到达到目标档位为止。新版本入库后旧版本进回收站保留 7 天，做种中的任务不受影响；不开启 = 下到即止"
          >
            <div className="flex flex-wrap gap-1.5">
              <ToggleChip active={upgradeSource === ""} onClick={() => setUpgradeSource("")}>
                不洗版
              </ToggleChip>
              {UPGRADE_OPTIONS.filter(
                ([value]) => !mediaSources.length || mediaSources.includes(value),
              ).map(([value, label]) => (
                <ToggleChip
                  key={value}
                  active={upgradeSource === value}
                  onClick={() => setUpgradeSource(value)}
                >
                  {label}
                </ToggleChip>
              ))}
            </div>
            {upgradeSource !== "" && (
              <div className="mt-2 rounded-xl bg-white/[0.03] px-4 py-2.5">
                <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
                  <span className="text-sub text-[var(--text-muted)]">目标分辨率</span>
                  <div className="flex flex-wrap gap-1.5">
                    {(resolutions.length ? resolutions : RESOLUTION_OPTIONS).map((option) => {
                      const effective = cutoffResolution || resolutions[0] || "1080p";
                      return (
                        <ToggleChip
                          key={option}
                          active={effective === option}
                          onClick={() =>
                            setCutoffResolution(option === (resolutions[0] ?? "") ? "" : option)
                          }
                        >
                          {option}
                        </ToggleChip>
                      );
                    })}
                  </div>
                </div>
                <p className="mt-1.5 text-caption text-[var(--text-faint)]">
                  {resolutions.length > 0
                    ? "缺省跟随上方分辨率偏好的第一位"
                    : "未限定分辨率时缺省洗到 1080p（避免意外进入 4K 的磁盘占用）"}
                </p>
                {/* 旧版本处置（library-file-recycle.md §9）：配置洗版的那一刻
                    就是询问旧版去留的合适时机；保留共存服务收藏家场景 */}
                <label className="mt-2.5 flex cursor-pointer items-center justify-between gap-3 border-t border-white/[0.06] pt-2.5">
                  <span>
                    <span className="block text-sub text-[var(--text-muted)]">
                      洗到新版本后保留旧版本
                    </span>
                    <span className="mt-0.5 block text-caption text-[var(--text-faint)]">
                      多版本共存（收藏家模式）；关闭 = 旧版本进回收站保留 7 天
                    </span>
                  </span>
                  <input
                    type="checkbox"
                    checked={upgradeKeepOld}
                    onChange={(e) => setUpgradeKeepOld(e.target.checked)}
                    className="size-4 accent-[var(--accent-2)]"
                  />
                </label>

                {/* 洗版优先级：交互与上方分辨率偏好一致——点击依次入列、序号即
                    位次。多一位就是多一轮潜在的重复下载，所以缺省只有前两位 */}
                <div className="mt-2.5 border-t border-white/[0.06] pt-2.5">
                  <button
                    type="button"
                    onClick={() => setLadderOpen((v) => !v)}
                    className="flex w-full items-center gap-3 text-left"
                  >
                    <span className="shrink-0 text-sub text-[var(--text-muted)]">
                      洗版优先级
                    </span>
                    <span className="min-w-0 flex-1 truncate text-right text-caption text-[var(--text-faint)]">
                      {ladderText}
                    </span>
                    <span
                      className={`shrink-0 text-[var(--text-faint)] transition-transform ${
                        ladderOpen ? "rotate-90" : ""
                      }`}
                    >
                      ›
                    </span>
                  </button>
                  {ladderOpen && (
                    <>
                  <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
                    <span className="text-caption text-[var(--text-faint)]">点击依次选择</span>
                    <div className="flex flex-wrap gap-1.5">
                      {LADDER_OPTIONS.map((dim) => {
                        const index = upgradeLadder.indexOf(dim.value);
                        const unconfigured = ladderUnconfigured(dim.value);
                        return (
                          <ToggleChip
                            key={dim.value}
                            active={index >= 0}
                            onClick={() => toggleLadderDim(dim.value)}
                          >
                            {index >= 0 && upgradeLadder.length > 1 && (
                              <span className="mr-1.5 inline-flex size-4 items-center justify-center rounded-full bg-white/20 text-micro font-semibold">
                                {index + 1}
                              </span>
                            )}
                            {dim.label}
                            {index >= 0 && unconfigured && (
                              <span className="ml-1 text-[var(--text-faint)]">· 未配置</span>
                            )}
                          </ToggleChip>
                        );
                      })}
                    </div>
                  </div>
                    <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
                      按顺序逐维度比较，先分出高低的那一维说了算。每多一维，就多一轮
                      潜在的重复下载——缺省只比分辨率与片源。标「未配置」的维度会被
                      自动跳过；全被跳过时按缺省的「分辨率 › 片源」比。
                    </p>
                    </>
                  )}
                  {platformNotLast && (
                    <p className="mt-1.5 rounded-lg border border-amber-400/25 bg-amber-500/10 px-3 py-2 text-caption leading-relaxed text-amber-200">
                      很多资源的标题里根本没写平台。把平台排在前面，等于要求「先比平台
                      再比别的」——没写平台的资源就全都分不出高低，洗版会大面积停住。
                      建议把平台放到最后一位。
                    </p>
                  )}
                </div>
              </div>
            )}
          </Field>

          {/* 配完给一句人话回执：新用户最缺的就是"我配的到底是什么"的确认。
              内容来自与保存同一个 draft.spec，不会出现"看到的和存下去的不一样" */}
          <div className="rounded-xl bg-white/[0.03] px-4 py-3">
            <p className="text-caption text-[var(--text-faint)]">
              {ladderPreview ? "先这样筛掉不要的" : "这条规则会这样筛选"}
            </p>
            <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
              {draftChips.length
                ? draftChips.join(" · ")
                : "不限任何条件——身份对得上的资源都接受"}
            </p>
            {ladderPreview && (
              <UpgradeLadderPreviewView preview={ladderPreview} keepOld={upgradeKeepOld} />
            )}
          </div>

          {error && (
            <p className="rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-sub leading-6 text-red-200">
              {error}
            </p>
          )}

          <div className="flex justify-end gap-3 pt-1">
            <button type="button" onClick={onClose} className="btn-glass h-9 px-4 text-ui font-medium">
              取消
            </button>
            <button
              type="button"
              disabled={busy || !name.trim()}
              onClick={() => void submit()}
              className="btn-accent h-9 rounded-full px-5 text-ui font-semibold disabled:opacity-50"
            >
              {busy ? "正在保存…" : "保存"}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

/**
 * 洗版阶梯预览：把"洗到哪一档"从一句话摊成一条**看得见的路**。
 *
 * 一句"洗到 1080p 蓝光 · DV"读不出洗版真正的两个后果，而它们恰恰是用户
 * 最该在按下保存之前知道的（docs/design/quality-upgrade.md §2.1 / §14.6）：
 *
 * - **终点之下还有多少档**：每上一档就是一次重复下载，多加一维阶梯的代价
 *   在这里才具体；
 * - **哪些档已经算达标**：分辨率排在第一位时，任意 2160p 资源（哪怕 WEBRip）
 *   都优于 1080p 蓝光，洗版会直接停在那儿——所以"更高档同样算达标"必须写在
 *   展开后的终点卡上，而不是留给用户自己推。
 *
 * **默认折叠**：完整阶梯要占十来行，常驻会把配置本身挤出视野；收起态只留
 * 一行终点（这段里唯一值得常驻的信息），其余进按钮后面。用点击而不是 hover
 * ——这个弹窗在手机上同样要用，触屏没有 hover；且这里是要读几行的解释，
 * 浮层里读长内容不如就地展开。折叠的交互形态与同一弹窗里的「洗版优先级」
 * 「画质与来源」保持一致，不新增第三种。
 *
 * 展开后版式即语义：终点在最上方并高亮（"最好品质"是这条路的目的地），下方
 * 按档位降序排开、每行一个 ↑ 指回终点——从下往上读就是资源逐级被替换的过程。
 */
function UpgradeLadderPreviewView({
  preview,
  keepOld,
}: {
  preview: UpgradeLadderPreview;
  keepOld: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-2.5 border-t border-white/[0.06] pt-2.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 text-left"
      >
        <span className="min-w-0 flex-1 text-sub text-[var(--text-muted)]">
          再一级级洗到{" "}
          <span className="font-semibold text-white">{preview.target}</span>
        </span>
        <span className="shrink-0 text-caption text-[var(--text-faint)]">
          {open ? "收起" : "看会经过哪些档"}
        </span>
        <span
          className={`shrink-0 text-[var(--text-faint)] transition-transform ${
            open ? "rotate-90" : ""
          }`}
        >
          ›
        </span>
      </button>

      {open && (
        <div className="mt-2.5">
          <p className="text-caption leading-relaxed text-[var(--text-faint)]">
            {`按「${preview.dimensions.join(" › ")}」逐维比较，先分出高低的那一维说了算。`}
          </p>

          <div className="mt-2 rounded-lg border border-[var(--accent-2)]/45 bg-[var(--accent-2)]/[0.14] px-3 py-2">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="rounded-full bg-[var(--accent-2)]/25 px-2 py-0.5 text-micro font-semibold text-white">
                终点
              </span>
              <span className="text-sub font-semibold text-white">{preview.target}</span>
            </div>
            <p className="mt-1 text-caption leading-relaxed text-[var(--text-faint)]">
              到手即停，不再洗版
              {preview.ceiling
                ? `；比它更高的档（最高 ${preview.ceiling}）同样算达标，也会停`
                : ""}
              。{keepOld ? "旧版本保留共存。" : "旧版本进回收站保留 7 天。"}
            </p>
          </div>

          {preview.below.length > 0 ? (
            <>
              <p className="mt-2.5 text-caption leading-relaxed text-[var(--text-faint)]">
                {"到终点之前，先到手的可能是下面任意一档；之后每遇到更高的一档，就再下一次、换掉旧的："}
              </p>
              <ol className="mt-1.5 space-y-1">
                {preview.below.map((label) => (
                  <li
                    key={label}
                    className="flex items-center gap-2 text-sub text-[var(--text-muted)]"
                  >
                    <span className="text-[var(--text-faint)]">↑</span>
                    <span>{label}</span>
                  </li>
                ))}
                {preview.moreBelow > 0 && (
                  <li className="flex items-center gap-2 text-caption text-[var(--text-faint)]">
                    <span>↑</span>
                    <span>更低的 {preview.moreBelow} 档同理</span>
                  </li>
                )}
              </ol>
            </>
          ) : (
            <p className="mt-2 text-caption text-[var(--text-faint)]">
              终点已是最低档，不会有过渡版本。
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 表单积木
// ---------------------------------------------------------------------------

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <p className="mb-1.5 text-ui font-semibold text-white/85">{label}</p>
      {children}
      {hint && <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">{hint}</p>}
    </div>
  );
}

function ToggleChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full border px-3 py-1.5 text-sub font-medium transition ${
        active
          ? "border-white/25 bg-white/[0.14] text-white"
          : "border-white/[0.08] bg-white/[0.03] text-[var(--text-muted)] hover:bg-white/[0.07]"
      }`}
    >
      {children}
    </button>
  );
}

/**
 * 可折叠分段：折叠态直接显示该段的**当前摘要**——不展开也知道里面配了什么，
 * 这是这个长表单能收起来的前提（渐进披露，quality-upgrade.md §14.9）。
 */
function Section({
  title,
  summary,
  children,
}: {
  title: string;
  summary: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl border border-white/[0.07] bg-white/[0.02]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
      >
        <span className="shrink-0 text-ui font-semibold text-white/85">{title}</span>
        <span className="min-w-0 flex-1 truncate text-right text-caption text-[var(--text-faint)]">
          {summary}
        </span>
        <span
          className={`shrink-0 text-[var(--text-faint)] transition-transform ${
            open ? "rotate-90" : ""
          }`}
        >
          ›
        </span>
      </button>
      {open && (
        <div className="space-y-5 border-t border-white/[0.06] px-4 pb-4 pt-4">{children}</div>
      )}
    </div>
  );
}

/**
 * 三态芯片：不限 → 只要 → 排除 → 不限。
 *
 * 平台有十几个选项，白/黑名单铺成两组芯片就是三十多个点击目标；三态把它压回
 * 一组，且每个芯片自解释（选中=只要，红色带 ✕=排除）。
 */
function TriChip({
  state,
  onClick,
  children,
}: {
  state: "off" | "include" | "exclude";
  onClick: () => void;
  children: React.ReactNode;
}) {
  const tone =
    state === "include"
      ? "border-white/25 bg-white/[0.14] text-white"
      : state === "exclude"
        ? "border-red-400/35 bg-red-500/12 text-red-200"
        : "border-white/[0.08] bg-white/[0.03] text-[var(--text-muted)] hover:bg-white/[0.07]";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full border px-3 py-1.5 text-sub font-medium transition ${tone}`}
    >
      {state === "exclude" && <span className="mr-1">✕</span>}
      {children}
    </button>
  );
}

function CheckRow({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3">
      <span>
        <span className="block text-ui font-medium text-white/90">{label}</span>
        <span className="mt-0.5 block text-caption text-[var(--text-faint)]">{hint}</span>
      </span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="size-4 accent-[var(--accent-2)]"
      />
    </label>
  );
}

function NumberInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  return (
    <input
      type="number"
      min={0}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
    />
  );
}
