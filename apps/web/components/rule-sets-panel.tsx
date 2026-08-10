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
import { PLATFORM_OPTIONS } from "@/lib/platforms";

/**
 * 规则组管理（设置 → 订阅 → 规则组）：Web 端唯一的规则组配置入口。
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

/**
 * 编码按"家族"选择：词表把 x265 / H.265 / HEVC 归一成不同值，而用户想表达的
 * 是"要 H.265 这一族"——UI 选一族即把全部等价写法写进白名单，避免因发布组
 * 写法不同而漏掉资源。
 */
const CODEC_FAMILIES: { label: string; values: string[] }[] = [
  { label: "H.265", values: ["x265", "H.265", "HEVC"] },
  { label: "H.264", values: ["x264", "H.264", "AVC"] },
  { label: "AV1", values: ["AV1"] },
];

/** spec → 人话芯片列表（清单摘要 / 订阅弹窗与详情页复用；空数组=全不限）。 */
export function specSummary(spec: RuleSetSpec): string[] {
  const chips: string[] = [];
  if (spec.resolutions?.length) chips.push(spec.resolutions.join(" > "));
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
  if (spec.hdr === "require") chips.push("必须 HDR");
  if (spec.hdr === "forbid") chips.push("排除 HDR");
  if (spec.dv === "require") chips.push("必须 DV");
  if (spec.dv === "forbid") chips.push("排除 DV");
  if (spec.free_only) chips.push("仅免费");
  if (spec.min_seeders != null) chips.push(`做种 ≥ ${spec.min_seeders}`);
  if (spec.size_min_mb != null || spec.size_max_mb != null) {
    const min = spec.size_min_mb != null ? `${spec.size_min_mb}` : "";
    const max = spec.size_max_mb != null ? `${spec.size_max_mb}` : "";
    chips.push(min && max ? `单集 ${min}–${max}MB` : min ? `单集 ≥ ${min}MB` : `单集 ≤ ${max}MB`);
  }
  if (spec.exclude_hr)
    chips.push(spec.hr_unknown_policy === "strict" ? "排除 H&R（未知也排）" : "排除 H&R");
  if (spec.platforms_allow?.length)
    chips.push(`平台白名单 ${spec.platforms_allow.length} 个`);
  if (spec.platforms_block?.length)
    chips.push(`平台黑名单 ${spec.platforms_block.length} 个`);
  if (spec.release_groups_allow?.length)
    chips.push(`制作组白名单 ${spec.release_groups_allow.length} 个`);
  if (spec.release_groups_block?.length)
    chips.push(`制作组黑名单 ${spec.release_groups_block.length} 个`);
  if (spec.platforms_allow?.length && spec.release_groups_allow?.length) {
    chips.push(spec.source_match_mode === "all" ? "平台且制作组" : "平台或制作组");
  }
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
  const [hdr, setHdr] = useState<"any" | "require" | "forbid">(spec.hdr ?? "any");
  const [dv, setDv] = useState<"any" | "require" | "forbid">(spec.dv ?? "any");
  const [freeOnly, setFreeOnly] = useState(spec.free_only ?? false);
  const [excludeHr, setExcludeHr] = useState(spec.exclude_hr ?? false);
  const [hrStrict, setHrStrict] = useState(spec.hr_unknown_policy === "strict");
  const [minSeeders, setMinSeeders] = useState(spec.min_seeders?.toString() ?? "");
  const [sizeMin, setSizeMin] = useState(spec.size_min_mb?.toString() ?? "");
  const [sizeMax, setSizeMax] = useState(spec.size_max_mb?.toString() ?? "");
  const [platformsAllow, setPlatformsAllow] = useState(spec.platforms_allow ?? []);
  const [platformsBlock, setPlatformsBlock] = useState(spec.platforms_block ?? []);
  const [groupsAllow, setGroupsAllow] = useState((spec.release_groups_allow ?? []).join(", "));
  const [groupsBlock, setGroupsBlock] = useState((spec.release_groups_block ?? []).join(", "));
  const [sourceMatchMode, setSourceMatchMode] = useState<
    NonNullable<RuleSetSpec["source_match_mode"]>
  >(spec.source_match_mode ?? "any");
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

  const toggleResolution = (value: string) =>
    setResolutions((prev) =>
      prev.includes(value) ? prev.filter((r) => r !== value) : [...prev, value],
    );

  const toggleCodecFamily = (label: string) =>
    setCodecFamilies((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });

  const submit = async () => {
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
    const codecs = [
      ...CODEC_FAMILIES.filter((f) => codecFamilies.has(f.label)).flatMap((f) => f.values),
      ...codecExtras,
    ];
    if (codecs.length) next.video_codecs = codecs;
    if (hdr !== "any") next.hdr = hdr;
    if (dv !== "any") next.dv = dv;
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
      setError("体积下限不能大于上限");
      return;
    }
    const allow = parseGroups(groupsAllow);
    const block = parseGroups(groupsBlock);
    const platformOverlap = platformsAllow.filter((value) => platformsBlock.includes(value));
    if (platformOverlap.length) {
      setError("同一平台不能同时加入白名单和黑名单");
      return;
    }
    const blockedGroups = new Set(block.map((value) => value.toLocaleLowerCase()));
    if (allow.some((value) => blockedGroups.has(value.toLocaleLowerCase()))) {
      setError("同一制作组不能同时加入白名单和黑名单");
      return;
    }
    if (platformsAllow.length) next.platforms_allow = platformsAllow;
    if (platformsBlock.length) next.platforms_block = platformsBlock;
    if (allow.length) next.release_groups_allow = allow;
    if (block.length) next.release_groups_block = block;
    if (platformsAllow.length && allow.length && sourceMatchMode === "all") {
      next.source_match_mode = "all";
    }

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
          所有条件都可以留空 = 不限该维度；来源白名单可单独选择组合关系。
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

          <Field label="HDR" hint="判断整个 HDR 家族（HDR10/HLG/DV 等）；DV 可在下方单独控制">
            <div className="flex flex-wrap gap-1.5">
              {(
                [
                  ["any", "不限"],
                  ["require", "必须 HDR"],
                  ["forbid", "排除 HDR"],
                ] as const
              ).map(([value, label]) => (
                <ToggleChip
                  key={value}
                  active={hdr === value}
                  onClick={() => {
                    setHdr(value);
                    // 排除整个 HDR 家族时，「必须 DV」成为矛盾条件，自动复位
                    if (value === "forbid" && dv === "require") setDv("any");
                  }}
                >
                  {label}
                </ToggleChip>
              ))}
            </div>
          </Field>

          <Field
            label="杜比视界（DV）"
            hint="与上面的 HDR 条件叠加生效，如「必须 HDR」+「排除 DV」= 只要不带 DV 的 HDR 资源"
          >
            <div className="flex flex-wrap gap-1.5">
              {(
                [
                  ["any", "不限"],
                  ["require", "必须 DV"],
                  ["forbid", "排除 DV"],
                ] as const
              ).map(([value, label]) => (
                <ToggleChip
                  key={value}
                  active={dv === value}
                  onClick={() => {
                    setDv(value);
                    // 「必须 DV」与「排除 HDR」矛盾（DV 属于 HDR 家族），自动复位后者
                    if (value === "require" && hdr === "forbid") setHdr("any");
                  }}
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

          <Field label="平台白名单">
            <PlatformPicker
              selected={platformsAllow}
              disabledValues={platformsBlock}
              onChange={setPlatformsAllow}
            />
          </Field>
          <Field label="平台黑名单">
            <PlatformPicker
              selected={platformsBlock}
              disabledValues={platformsAllow}
              onChange={setPlatformsBlock}
            />
          </Field>

          <Field
            label="制作组白名单"
            hint="完整匹配制作组，逗号或空格分隔；Pure@HDSWEB 与 HDSWEB 是两个不同值"
          >
            <input
              type="text"
              value={groupsAllow}
              onChange={(e) => setGroupsAllow(e.target.value)}
              placeholder="如 Pure@HDSWEB, HHWEB"
              className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
            />
          </Field>
          <Field label="制作组黑名单" hint="黑名单优先，且只按制作组完整值排除">
            <input
              type="text"
              value={groupsBlock}
              onChange={(e) => setGroupsBlock(e.target.value)}
              placeholder="留空不启用"
              className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
            />
          </Field>

          {platformsAllow.length > 0 && groupsAllow.trim() && (
            <Field label="来源白名单关系">
              <div className="flex flex-wrap gap-1.5">
                <ToggleChip
                  active={sourceMatchMode === "any"}
                  onClick={() => setSourceMatchMode("any")}
                >
                  符合任一
                </ToggleChip>
                <ToggleChip
                  active={sourceMatchMode === "all"}
                  onClick={() => setSourceMatchMode("all")}
                >
                  必须同时符合
                </ToggleChip>
              </div>
            </Field>
          )}

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

function PlatformPicker({
  selected,
  disabledValues,
  onChange,
}: {
  selected: string[];
  disabledValues: string[];
  onChange: (values: string[]) => void;
}) {
  const [query, setQuery] = useState("");
  const selectedSet = new Set(selected);
  const disabledSet = new Set(disabledValues);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = PLATFORM_OPTIONS.filter((option) => {
    if (!normalizedQuery) return true;
    return `${option.label} ${option.aliases} ${option.value}`
      .toLocaleLowerCase()
      .includes(normalizedQuery);
  });

  const toggle = (value: string) => {
    onChange(
      selectedSet.has(value)
        ? selected.filter((item) => item !== value)
        : [...selected, value],
    );
  };

  return (
    <div>
      <input
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="搜索平台或发布名，如 IQ、DSNP、Viu"
        className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
      />
      <div className="scroll-thin mt-2 max-h-44 space-y-2 overflow-y-auto pr-1">
        {(["国际", "亚洲", "动漫"] as const).map((group) => {
          const options = visible.filter((option) => option.group === group);
          if (!options.length) return null;
          return (
            <div key={group} className="flex flex-wrap items-center gap-1.5">
              <span className="w-8 shrink-0 text-caption text-[var(--text-faint)]">{group}</span>
              {options.map((option) => {
                const disabled = disabledSet.has(option.value);
                return (
                  <button
                    key={option.value}
                    type="button"
                    disabled={disabled}
                    title={`${option.aliases}${disabled ? "；已在另一名单中" : ""}`}
                    onClick={() => toggle(option.value)}
                    className={`rounded-full border px-2.5 py-1 text-caption transition disabled:cursor-not-allowed disabled:opacity-30 ${
                      selectedSet.has(option.value)
                        ? "border-white/25 bg-white/[0.14] text-white"
                        : "border-white/[0.08] bg-white/[0.03] text-[var(--text-muted)] hover:bg-white/[0.07]"
                    }`}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
          );
        })}
        {visible.length === 0 && (
          <p className="py-2 text-center text-caption text-[var(--text-faint)]">没有匹配的平台</p>
        )}
      </div>
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
