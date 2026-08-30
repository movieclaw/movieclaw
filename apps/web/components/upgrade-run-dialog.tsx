"use client";

import { useEffect, useMemo, useState } from "react";

import { MediaSourceAnnotationDialog } from "@/components/media-source-annotation-dialog";
import { Modal } from "@/components/modal";
import { RuleSetEditorDialog, upgradeTargetLabel } from "@/components/rule-sets-panel";
import {
  listRuleSets,
  runSubscriptionUpgradeRound,
  setSubscriptionTrackingState,
  updateSubscription,
  type RuleSet,
  type SubscriptionDetail,
  type UpgradeRunReport,
  type UpgradeRunUnit,
} from "@/lib/api/subscriptions";
import { seasonsNeedingAnnotation } from "@/lib/media-source-annotation";
import { usePermissions } from "@/lib/permissions";

/**
 * 「洗一轮版」弹层（quality-upgrade.md §13.4/§13.5）：手动洗版的统一确认层。
 *
 * 两段式：确认段 → 报告段。确认段把既有订阅的三种状态并成一条动线：
 *   - 规则组未配洗版目标 → 必须先选一个带洗版目标的组（含快捷新建入口），
 *     「选组 + 触发」由后端 upgrade-runs 的 rule_set_id 参数合成一次调用；
 *   - 库里有文件但不在订阅范围的季 → 如实列出供勾选并入（扩大范围会连带
 *     补缺下载，是用户决策，不静默纳入）；
 *   - 订阅已暂停 → 提示并改为「恢复并触发」。
 * 报告段渲染后端体检快照（摘要句 + 按季分组的单元徽标），不落库、不轮询——
 * 后续进展看追踪明细的「洗版中」徽标与活动流水。
 */
export function UpgradeRunDialog({
  detail,
  onClose,
  onFinished,
}: {
  detail: SubscriptionDetail;
  onClose: () => void;
  /** 一轮洗版已触发成功（报告已看完关闭弹层）后回调，父组件刷新详情 */
  onFinished: () => void;
}) {
  const { canManageSubscriptions } = usePermissions();
  const isMovie = detail.media.kind === "movie";
  const [ruleSets, setRuleSets] = useState<RuleSet[] | null>(null);
  const [ruleSetId, setRuleSetId] = useState<number | null>(null);
  const [creatingRuleSet, setCreatingRuleSet] = useState(false);
  // 范围外但库里有文件的季：默认不勾（并入会连带补缺下载）
  const [optedSeasons, setOptedSeasons] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<UpgradeRunReport | null>(null);

  useEffect(() => {
    listRuleSets()
      .then((rules) => {
        setRuleSets(rules);
        // 现用组已配洗版目标 → 预选它（主场景零选择）；否则若只有一个可选组也预选
        const current = rules.find((r) => r.id === detail.rule_set_id);
        if (current && upgradeTargetLabel(current.spec)) {
          setRuleSetId(current.id);
        } else {
          const candidates = rules.filter((r) => upgradeTargetLabel(r.spec));
          setRuleSetId(candidates.length === 1 ? candidates[0].id : null);
        }
      })
      .catch(() => setError("未能加载规则组列表，请稍后重试"));
  }, [detail.rule_set_id]);

  const upgradeRules = useMemo(
    () => (ruleSets ?? []).filter((r) => upgradeTargetLabel(r.spec)),
    [ruleSets],
  );
  const currentHasTarget = useMemo(() => {
    const current = (ruleSets ?? []).find((r) => r.id === detail.rule_set_id);
    return !!current && !!upgradeTargetLabel(current.spec);
  }, [ruleSets, detail.rule_set_id]);

  // 库里有文件但不在订阅范围的季（如只订了 S2、库里还有 S1）
  const outOfScopeOwned = useMemo(
    () =>
      isMovie
        ? []
        : detail.season_collection.filter(
            (s) => s.owned_count > 0 && !detail.selected_seasons.includes(s.season_number),
          ),
    [isMovie, detail.season_collection, detail.selected_seasons],
  );

  const paused = detail.status === "paused";
  const selectedRule = upgradeRules.find((r) => r.id === ruleSetId) ?? null;

  const run = async () => {
    if (!selectedRule) return;
    setBusy(true);
    setError(null);
    try {
      // 并入勾选的范围外季（既有 PATCH，后端 diff 生成补缺工单）
      if (optedSeasons.size > 0) {
        await updateSubscription(detail.id, {
          selected_seasons: [...detail.selected_seasons, ...optedSeasons].sort(
            (a, b) => a - b,
          ),
        });
      }
      if (paused) await setSubscriptionTrackingState(detail.id, "active");
      setReport(
        await runSubscriptionUpgradeRound(
          detail.id,
          selectedRule.id !== detail.rule_set_id ? selectedRule.id : undefined,
        ),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "触发洗版失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={report ? onFinished : onClose} label="洗一轮版" width="lg">
      <div className="scroll-thin max-h-[80dvh] overflow-y-auto p-6 max-md:p-5">
        {report ? (
          <UpgradeRunReportView
            title={detail.media.title}
            isMovie={isMovie}
            report={report}
            mediaItemId={detail.media.media_item_id}
            onAnnotated={async () => {
              // 标注已刷新快照：重跑一轮体检，报告当场翻新（排期一并完成）
              setReport(await runSubscriptionUpgradeRound(detail.id));
            }}
            onClose={onFinished}
          />
        ) : (
          <>
            <h2 className="text-title font-bold text-white">洗一轮版</h2>
            <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
              逐集检查《{detail.media.title}
              》库里已有的版本，低于洗版目标的立即排入搜索；洗到新版本入库并验证通过后，旧文件自动替换。
            </p>

            {error && (
              <p className="mt-3 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-sub leading-6 text-red-200">
                {error}
              </p>
            )}

            {paused && (
              <p className="mt-3 rounded-lg border border-amber-300/25 bg-amber-400/10 px-3.5 py-2.5 text-sub leading-6 text-amber-100/90">
                该订阅已暂停。触发洗版会先恢复追踪，随后开始搜索。
              </p>
            )}

            {/* —— 规则组（洗版目标住在规则组上；只列带洗版目标的组）—— */}
            <div className="mt-4">
              <p className="text-sub font-semibold text-white/85">洗版目标</p>
              {ruleSets === null ? (
                <p className="mt-2 text-sub text-[var(--text-muted)]">正在加载规则组…</p>
              ) : upgradeRules.length === 0 ? (
                <div className="mt-2 rounded-xl border border-white/[0.08] bg-white/[0.03] px-4 py-3 text-sub leading-6 text-[var(--text-muted)]">
                  还没有配置洗版目标的规则组。
                  {canManageSubscriptions ? (
                    <button
                      type="button"
                      onClick={() => setCreatingRuleSet(true)}
                      className="ml-1 font-medium text-[var(--accent-2)] hover:underline"
                    >
                      + 新建规则组
                    </button>
                  ) : (
                    " 请联系管理员在「设置 → 订阅规则 → 规则组」中配置「洗到哪一档」。"
                  )}
                </div>
              ) : (
                <div className="mt-2 space-y-1.5">
                  {!currentHasTarget && (
                    <p className="mb-2 text-sub leading-6 text-[var(--text-muted)]">
                      当前规则组未配置洗版目标，选一个带洗版目标的组，确认后一并换用。
                    </p>
                  )}
                  {upgradeRules.map((rs) => {
                    const picked = rs.id === ruleSetId;
                    return (
                      <button
                        key={rs.id}
                        type="button"
                        disabled={busy}
                        onClick={() => setRuleSetId(rs.id)}
                        className={`flex w-full items-center gap-2.5 rounded-xl border px-4 py-2.5 text-left transition disabled:opacity-50 ${
                          picked
                            ? "border-[#2dd4bf]/40 bg-[#2dd4bf]/[0.08]"
                            : "border-white/[0.08] bg-white/[0.03] hover:bg-white/[0.07]"
                        }`}
                      >
                        <span className="min-w-0 flex-1 truncate text-ui font-medium text-white/90">
                          {rs.name}
                          {rs.id === detail.rule_set_id && (
                            <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
                              当前使用
                            </span>
                          )}
                        </span>
                        <span className="shrink-0 rounded-md bg-[#2dd4bf]/15 px-1.5 py-0.5 text-caption font-medium text-[#2dd4bf]">
                          洗到 {upgradeTargetLabel(rs.spec)}
                        </span>
                      </button>
                    );
                  })}
                  {canManageSubscriptions && (
                    <button
                      type="button"
                      onClick={() => setCreatingRuleSet(true)}
                      className="w-full rounded-xl border border-dashed border-white/[0.14] px-4 py-2 text-left text-sub text-[var(--text-muted)] transition hover:border-white/25 hover:text-white/80"
                    >
                      + 新建规则组
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* —— 范围外的库存季：如实展示，勾选才并入（会连带补缺）—— */}
            {outOfScopeOwned.length > 0 && (
              <div className="mt-4">
                <p className="text-sub font-semibold text-white/85">范围外的库存季</p>
                <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
                  这些季库里有文件但不在订阅范围内，勾选后并入订阅一起洗版；季内缺集会一并搜索补齐。
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {outOfScopeOwned.map((s) => {
                    const picked = optedSeasons.has(s.season_number);
                    return (
                      <button
                        key={s.season_number}
                        type="button"
                        disabled={busy}
                        aria-pressed={picked}
                        onClick={() =>
                          setOptedSeasons((prev) => {
                            const next = new Set(prev);
                            if (next.has(s.season_number)) next.delete(s.season_number);
                            else next.add(s.season_number);
                            return next;
                          })
                        }
                        className={`rounded-full border px-3 py-1.5 text-sub transition disabled:opacity-50 ${
                          picked
                            ? "border-[#2dd4bf]/40 bg-[#2dd4bf]/[0.12] text-white"
                            : "border-white/[0.1] bg-white/[0.03] text-white/70 hover:bg-white/[0.07]"
                        }`}
                      >
                        {s.season_number === 0 ? "特别篇" : `第 ${s.season_number} 季`}
                        <span className="tnum ml-1.5 text-caption text-[var(--text-faint)]">
                          库存 {s.owned_count}
                          {s.episode_count != null && s.owned_count < s.episode_count
                            ? ` / ${s.episode_count}`
                            : ""}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                className="btn-glass h-10 px-4 text-ui font-medium"
              >
                返回
              </button>
              <button
                type="button"
                disabled={busy || !selectedRule}
                onClick={() => void run()}
                className="btn-accent inline-flex h-10 items-center gap-2 rounded-full px-5 text-ui font-semibold disabled:opacity-40"
              >
                {busy && (
                  <span className="size-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white/90" />
                )}
                {paused ? "恢复并触发洗版" : "开始体检并洗版"}
              </button>
            </div>
          </>
        )}
      </div>

      {creatingRuleSet && (
        <RuleSetEditorDialog
          ruleSet={null}
          raised
          onClose={() => setCreatingRuleSet(false)}
          onSaved={(saved) => {
            setCreatingRuleSet(false);
            setRuleSets((prev) => [...(prev ?? []), saved]);
            // 新建组带洗版目标时自动选中它（快捷新建的动机就是没得选）
            if (upgradeTargetLabel(saved.spec)) setRuleSetId(saved.id);
          }}
        />
      )}
    </Modal>
  );
}

/** 单元状态 → 报告徽标（与追踪明细的徽标语言同源）。 */
const UNIT_STATE_META: Record<
  UpgradeRunUnit["state"],
  { label: string; color: string }
> = {
  upgradable: { label: "已排洗版", color: "#2dd4bf" },
  in_flight: { label: "洗版中", color: "#34d399" },
  at_cutoff: { label: "已达目标", color: "#4ade80" },
  not_comparable: { label: "无法确认", color: "#9ca3af" },
  missing: { label: "缺失", color: "#f5c451" },
};

/** 单元状态 → 说明行（标签由后端生成，前端只做排版）。 */
function unitNote(u: UpgradeRunUnit): string {
  switch (u.state) {
    case "upgradable":
      return `当前 ${u.current_label ?? "未知"} → 目标 ${u.target_label}，已排入立即搜索`;
    case "in_flight":
      return "已投递新版本，等待入库验证";
    case "at_cutoff":
      return `当前 ${u.current_label ?? "未知"}，已达洗版目标`;
    case "not_comparable":
      return "无法确认当前版本是否低于目标，不自动洗；可「标注片源」告知系统，或用「手动选种」直接替换";
    case "missing":
      return "库里没有该单元，将照常搜索下载补齐";
  }
}

/**
 * 体检报告段：摘要句 + 按季分组的单元列表。
 * 报告是一次性快照——关闭后看追踪明细的「洗版中」徽标与活动流水跟进。
 * 订阅弹层的洗版变体（场景 B：建订阅后自动接一轮洗版）复用本段展示报告。
 */
export function UpgradeRunReportView({
  title,
  isMovie,
  report,
  mediaItemId,
  onAnnotated,
  onClose,
}: {
  title: string;
  isMovie: boolean;
  report: UpgradeRunReport;
  /** 有值且调用方提供 onAnnotated 时，「无法确认」的季显示「标注片源」入口 */
  mediaItemId?: number;
  /** 片源标注成功后回调（通常重跑一轮体检刷新报告） */
  onAnnotated?: (message: string) => void | Promise<void>;
  onClose: () => void;
}) {
  const { isAdmin } = usePermissions();
  const [annotateSeason, setAnnotateSeason] = useState<number | null>(null);
  const seasons = new Map<number, UpgradeRunUnit[]>();
  for (const u of report.units) {
    const list = seasons.get(u.season_number) ?? [];
    list.push(u);
    seasons.set(u.season_number, list);
  }
  // 标注是库数据纠错（管理员动作）：入口条件与后端 require_admin 对齐
  const annotatable =
    mediaItemId != null && onAnnotated && isAdmin
      ? seasonsNeedingAnnotation(report.units)
      : new Set<number>();
  return (
    <>
      <h2 className="text-title font-bold text-white">洗版体检报告</h2>
      <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
        《{title}》· 目标 {report.target_label}
      </p>
      <p className="mt-3 rounded-lg border border-[#2dd4bf]/25 bg-[#2dd4bf]/[0.08] px-3.5 py-2.5 text-sub leading-6 text-white/90">
        {report.summary}
      </p>

      <div className="mt-4 space-y-3">
        {[...seasons.entries()]
          .sort(([a], [b]) => a - b)
          .map(([season, units]) => (
            <div
              key={season}
              className="overflow-hidden rounded-xl border border-white/[0.07] bg-white/[0.02]"
            >
              {(!isMovie || annotatable.has(season)) && (
                <p className="flex items-center border-b border-white/[0.06] px-4 py-2 text-sub font-semibold text-white/80">
                  <span className="min-w-0 flex-1">
                    {isMovie ? "正片" : season === 0 ? "特别篇" : `第 ${season} 季`}
                  </span>
                  {annotatable.has(season) && (
                    <button
                      type="button"
                      onClick={() => setAnnotateSeason(season)}
                      className="shrink-0 rounded-md border border-white/[0.12] px-2 py-0.5 text-caption font-medium text-white/75 transition hover:bg-white/[0.07] hover:text-white"
                    >
                      标注片源
                    </button>
                  )}
                </p>
              )}
              <ul className="divide-y divide-white/[0.05]">
                {units.map((u) => {
                  const meta = UNIT_STATE_META[u.state];
                  return (
                    <li
                      key={`${u.season_number}-${u.episode_number}`}
                      className="flex items-center gap-3 px-4 py-2 max-md:items-start"
                    >
                      <span className="tnum w-12 shrink-0 text-sub font-medium text-white/90">
                        {isMovie ? "正片" : `E${String(u.episode_number).padStart(2, "0")}`}
                      </span>
                      <span
                        className="shrink-0 rounded-full px-2 py-0.5 text-micro font-semibold"
                        style={{ backgroundColor: `${meta.color}22`, color: meta.color }}
                      >
                        {meta.label}
                      </span>
                      <span className="tnum min-w-0 flex-1 text-sub leading-5 text-[var(--text-muted)] md:truncate">
                        {unitNote(u)}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
      </div>

      <p className="mt-3 text-caption leading-5 text-[var(--text-faint)]">
        后续进展在订阅详情的「追踪明细」里跟进：洗版中的单元带青色徽标，换版成功会记入活动记录。
      </p>
      <div className="mt-4 flex justify-end">
        <button
          type="button"
          onClick={onClose}
          className="btn-accent h-10 rounded-full px-5 text-ui font-semibold"
        >
          完成
        </button>
      </div>

      {annotateSeason !== null && mediaItemId != null && (
        <MediaSourceAnnotationDialog
          mediaItemId={mediaItemId}
          seasonNumber={annotateSeason}
          isMovie={isMovie}
          raised
          onClose={() => setAnnotateSeason(null)}
          onApplied={async (message) => {
            setAnnotateSeason(null);
            await onAnnotated?.(message);
          }}
        />
      )}
    </>
  );
}
