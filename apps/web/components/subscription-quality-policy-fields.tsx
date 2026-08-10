"use client";

import { specSummary } from "@/components/rule-sets-panel";
import type { MediaType } from "@/lib/media-types";
import type { RuleSet } from "@/lib/api/subscriptions";

export type QualityMode = "off" | "lock_first" | "upgrade";

export function SubscriptionQualityPolicyFields({
  kind,
  ruleSets,
  mode,
  targetRuleSetId,
  onModeChange,
  onTargetRuleSetChange,
}: {
  kind: MediaType;
  ruleSets: RuleSet[];
  mode: QualityMode;
  targetRuleSetId: number | null;
  onModeChange: (mode: QualityMode) => void;
  onTargetRuleSetChange: (id: number) => void;
}) {
  const target = ruleSets.find((rule) => rule.id === targetRuleSetId);
  const chips = target ? specSummary(target.spec) : [];

  return (
    <section>
      <h3 className="mb-2 text-ui font-semibold text-white/85">版本连续性</h3>
      <select
        value={mode}
        onChange={(event) => onModeChange(event.target.value as QualityMode)}
        className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-white/90 outline-none focus:border-white/25 [&>option]:bg-[#181c28]"
      >
        <option value="off">关闭</option>
        {kind === "tv" && <option value="lock_first">首次成功入库后固定版本</option>}
        <option value="upgrade">
          {kind === "tv" ? "洗版达标后固定后续剧集" : "自动洗版到目标版本"}
        </option>
      </select>

      {mode === "upgrade" && (
        <div className="mt-2">
          <select
            value={targetRuleSetId ?? ""}
            onChange={(event) => onTargetRuleSetChange(Number(event.target.value))}
            className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-white/90 outline-none focus:border-white/25 [&>option]:bg-[#181c28]"
          >
            <option value="" disabled>
              选择洗版目标规则组
            </option>
            {ruleSets.map((rule) => (
              <option key={rule.id} value={rule.id}>
                {rule.name}
                {rule.is_default ? "（默认）" : ""}
              </option>
            ))}
          </select>
          {target && (
            <p className="mt-1.5 flex flex-wrap gap-1.5">
              {chips.length > 0 ? (
                chips.map((chip) => (
                  <span
                    key={chip}
                    className="rounded-md bg-white/[0.07] px-1.5 py-0.5 text-caption text-white/75"
                  >
                    {chip}
                  </span>
                ))
              ) : (
                <span className="text-caption text-amber-200">
                  该规则组没有可用的画质或来源目标
                </span>
              )}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
