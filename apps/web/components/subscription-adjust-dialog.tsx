"use client";

/**
 * 调整订阅弹窗（订阅详情页「调整 ›」入口）：创建后修改季选择 / 持续追新 /
 * 入库目标库。后端 sub.update 早已支持 diff 重算工单（加季补工单、减季收
 * 未投递工单、已下载/已入库一律保留），此前前端只放开了换规则组——本弹窗
 * 补齐剩余三项，用户不必再"取消订阅重订"（那会丢活动记录）。
 *
 * 数据源与订阅弹窗同源：季结构走 sub.prepare（幂等，带播出/库存进度），
 * 库选择即时走投递预检。只提交发生变化的字段（PATCH 部分更新语义）。
 */

import { useEffect, useMemo, useState } from "react";

import { useToast } from "@/components/feedback";
import { Modal } from "@/components/modal";
import { SeasonRow } from "@/components/subscribe-dialog";
import {
  SubscriptionQualityPolicyFields,
  type QualityMode,
} from "@/components/subscription-quality-policy-fields";
import { listLibraries, type MediaLibrary } from "@/lib/api/libraries";
import {
  getDispatchPreview,
  listRuleSets,
  prepareSubscription,
  updateSubscription,
  type DispatchPreview,
  type RuleSet,
  type SeasonOverview,
  type SubscriptionDetail,
} from "@/lib/api/subscriptions";

export function SubscriptionAdjustDialog({
  detail,
  onClose,
  onSaved,
}: {
  detail: SubscriptionDetail;
  onClose: () => void;
  /** 保存成功后由调用方刷新详情 */
  onSaved: () => void;
}) {
  const toast = useToast();
  const isMovie = detail.media.kind === "movie";
  // null = 加载中；[] 也是有效结果（电影没有季）
  const [seasons, setSeasons] = useState<SeasonOverview[] | null>(isMovie ? [] : null);
  const [libraries, setLibraries] = useState<MediaLibrary[]>([]);
  const [ruleSets, setRuleSets] = useState<RuleSet[]>([]);
  const [selectedSeasons, setSelectedSeasons] = useState<Set<number>>(
    () => new Set(detail.selected_seasons),
  );
  const [followFuture, setFollowFuture] = useState(detail.follow_future);
  const [libraryId, setLibraryId] = useState<number | null>(detail.library_id);
  const [qualityMode, setQualityMode] = useState<QualityMode>(
    detail.quality_policy?.mode ?? "off",
  );
  const [targetRuleSetId, setTargetRuleSetId] = useState<number | null>(
    detail.quality_policy?.target_rule_set_id ?? null,
  );
  const [preview, setPreview] = useState<DispatchPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      isMovie
        ? Promise.resolve(null)
        : prepareSubscription({
            source: "tmdb",
            kind: detail.media.kind,
            tmdb_id: detail.media.tmdb_id,
          }),
      listLibraries(detail.media.kind),
      listRuleSets(),
    ])
      .then(([prepared, libs, rules]) => {
        if (cancelled) return;
        if (prepared) setSeasons(prepared.status === "ready" ? prepared.seasons : []);
        setLibraries(libs);
        const savedTargetId = detail.quality_policy?.target_rule_set_id;
        const savedTarget = detail.quality_policy?.target;
        setRuleSets(
          savedTargetId != null &&
            savedTarget &&
            !rules.some((rule) => rule.id === savedTargetId)
            ? [
                ...rules,
                {
                  id: savedTargetId,
                  name: detail.quality_policy?.target_rule_name ?? "已保存的洗版目标",
                  is_default: false,
                  spec: savedTarget,
                  reference_count: 0,
                },
              ]
            : rules,
        );
        setTargetRuleSetId(
          (current) => current ?? rules.find((rule) => rule.is_default)?.id ?? rules[0]?.id ?? null,
        );
      })
      .catch(() => {
        if (!cancelled) setError("加载季集与媒体库信息失败，请稍后重试");
      });
    return () => {
      cancelled = true;
    };
  }, [
    detail.media.kind,
    detail.media.tmdb_id,
    detail.quality_policy?.target,
    detail.quality_policy?.target_rule_name,
    detail.quality_policy?.target_rule_set_id,
    isMovie,
  ]);

  // 选库即预演投递落点（与订阅弹窗同一套提示；null=该类型默认库也预演）
  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    getDispatchPreview(detail.media.kind, libraryId, detail.media.tmdb_id)
      .then((p) => {
        if (!cancelled) setPreview(p);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [detail.media.kind, detail.media.tmdb_id, libraryId]);

  const toggleSeason = (n: number) =>
    setSelectedSeasons((prev) => {
      const next = new Set(prev);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });

  // 被取消勾选、且已有进度（有工单走到 grabbed 之后）的季：减季不动已下内容，
  // 但未投递的缺口会被收掉——把后果讲在保存之前
  const droppedWithProgress = useMemo(() => {
    if (isMovie) return [];
    const kept = selectedSeasons;
    const progressed = new Set(
      detail.wanted.filter((w) => w.status !== "wanted").map((w) => w.season_number),
    );
    return detail.selected_seasons.filter((s) => !kept.has(s) && progressed.has(s));
  }, [detail, isMovie, selectedSeasons]);

  const changed = useMemo(() => {
    const seasonsChanged =
      !isMovie &&
      JSON.stringify([...selectedSeasons].sort((a, b) => a - b)) !==
        JSON.stringify([...detail.selected_seasons].sort((a, b) => a - b));
    return {
      seasons: seasonsChanged,
      follow: !isMovie && followFuture !== detail.follow_future,
      library: libraryId !== detail.library_id,
      quality:
        qualityMode !== (detail.quality_policy?.mode ?? "off") ||
        (qualityMode === "upgrade" &&
          targetRuleSetId !== (detail.quality_policy?.target_rule_set_id ?? null)),
    };
  }, [detail, followFuture, isMovie, libraryId, qualityMode, selectedSeasons, targetRuleSetId]);
  const dirty = changed.seasons || changed.follow || changed.library || changed.quality;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await updateSubscription(detail.id, {
        ...(changed.seasons
          ? { selected_seasons: [...selectedSeasons].sort((a, b) => a - b) }
          : {}),
        ...(changed.follow ? { follow_future: followFuture } : {}),
        // 显式带上 null 即「清除指定库、改回默认库路由」（后端区分未传与 null）
        ...(changed.library ? { library_id: libraryId } : {}),
        ...(changed.quality
          ? {
              quality_policy:
                qualityMode === "off"
                  ? null
                  : {
                      mode: qualityMode,
                      target_rule_set_id:
                        qualityMode === "upgrade" ? targetRuleSetId : null,
                    },
            }
          : {}),
      });
      toast.success("订阅已调整");
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "调整失败，请稍后重试");
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={onClose} label="调整订阅" width="lg">
      <div className="scroll-thin max-h-[76dvh] overflow-y-auto p-6 max-md:p-5">
        <h2 className="text-title font-bold text-white">调整订阅</h2>
        <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
          《{detail.media.title}》——加季会补建缺口工单；减季只收掉还没投递的，
          已下载/已入库的内容不受影响。
        </p>

        {error && (
          <p className="mt-3 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-sub leading-6 text-red-200">
            {error}
          </p>
        )}

        <div className="mt-4 space-y-5">
          {!isMovie && (
            <section>
              <h3 className="mb-2 text-ui font-semibold text-white/85">
                选择要收录的季
                <span className="ml-2 font-normal text-[var(--text-faint)]">
                  勾选即要整季（含未播集）
                </span>
              </h3>
              {seasons === null ? (
                <p className="rounded-xl bg-white/[0.03] px-4 py-3 text-sub text-[var(--text-muted)]">
                  正在加载季集信息…
                </p>
              ) : (
                <div className="space-y-1.5">
                  {seasons.map((s) => (
                    <SeasonRow
                      key={s.season_number}
                      season={s}
                      checked={selectedSeasons.has(s.season_number)}
                      onToggle={() => toggleSeason(s.season_number)}
                    />
                  ))}
                </div>
              )}
              {droppedWithProgress.length > 0 && (
                <p className="mt-2 rounded-lg border border-amber-400/25 bg-amber-500/10 px-3 py-2 text-caption leading-relaxed text-amber-200">
                  第 {droppedWithProgress.join("、")} 季已有下载进度：取消勾选不会删除
                  已下载内容，但该季还没抓到的部分将停止寻找资源
                </p>
              )}

              <label className="mt-4 flex cursor-pointer items-center justify-between rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3">
                <span>
                  <span className="block text-ui font-medium text-white/90">持续追新</span>
                  <span className="mt-0.5 block text-caption text-[var(--text-faint)]">
                    之后播出的新集、新一季自动加入追踪
                  </span>
                </span>
                <input
                  type="checkbox"
                  checked={followFuture}
                  onChange={(e) => setFollowFuture(e.target.checked)}
                  className="size-4 accent-[var(--accent-2)]"
                />
              </label>
            </section>
          )}

          {libraries.length > 0 && (
            <section>
              <h3 className="mb-2 text-ui font-semibold text-white/85">入库到</h3>
              {/* 旧订阅 library_id 可能为 null（按默认库路由）：给它一个显式占位项，
                  否则浏览器会视觉上选中第一个库、而状态仍是 null（所见非所存） */}
              <select
                value={libraryId === null ? "" : String(libraryId)}
                onChange={(e) =>
                  setLibraryId(e.target.value === "" ? null : Number(e.target.value))
                }
                className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-white/90 outline-none focus:border-white/25 [&>option]:bg-[#181c28]"
              >
                <option value="">（按默认库路由）</option>
                {libraries.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                    {l.is_default ? "（默认）" : ""}
                  </option>
                ))}
              </select>
              {preview &&
                (preview.ok ? (
                  <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
                    {preview.mode === "watch"
                      ? `将投递到监听导入目录 ${preview.path ?? ""}，下载完成后自动整理入库`
                      : `将直接下载到库内目录 ${preview.path ?? ""}，完成后自动入账`}
                  </p>
                ) : (
                  <p className="mt-1.5 rounded-lg border border-amber-400/25 bg-amber-500/10 px-3 py-2 text-caption leading-relaxed text-amber-200">
                    {preview.warning}
                  </p>
                ))}
            </section>
          )}

          {ruleSets.length > 0 && (
            <SubscriptionQualityPolicyFields
              kind={detail.media.kind}
              ruleSets={ruleSets}
              mode={qualityMode}
              targetRuleSetId={targetRuleSetId}
              onModeChange={setQualityMode}
              onTargetRuleSetChange={setTargetRuleSetId}
            />
          )}
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <button type="button" onClick={onClose} className="btn-glass h-9 px-4 text-ui font-medium">
            取消
          </button>
          <button
            type="button"
            disabled={
              busy ||
              !dirty ||
              (!isMovie && selectedSeasons.size === 0) ||
              (qualityMode === "upgrade" && targetRuleSetId === null)
            }
            onClick={() => void save()}
            className="btn-accent h-9 rounded-full px-5 text-ui font-semibold disabled:opacity-40"
          >
            {busy ? "保存中…" : "保存调整"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
