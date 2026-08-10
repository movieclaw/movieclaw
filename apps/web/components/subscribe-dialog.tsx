"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { CheckIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import { PosterImage } from "@/components/poster-image";
import { RuleSetEditorDialog, specSummary } from "@/components/rule-sets-panel";
import {
  SubscriptionQualityPolicyFields,
  type QualityMode,
} from "@/components/subscription-quality-policy-fields";
import { listLibraries, type MediaLibrary } from "@/lib/api/libraries";
import {
  createSubscription,
  deleteSubscription,
  getDispatchPreview,
  listRuleSets,
  prepareSubscription,
  type DispatchPreview,
  type PrepareResult,
  type ResolveCandidate,
  type RuleSet,
  type SeasonOverview,
} from "@/lib/api/subscriptions";
import { cachedImageUrl } from "@/lib/image-proxy";
import type { MediaType } from "@/lib/media-types";

/**
 * 订阅弹层的打开参数：TMDB 入口带 tmdbId；豆瓣入口带 doubanId + title(+year)，
 * 由后端收敛到 TMDB 锚（歧义时本弹层内让用户从候选中确认一次）。
 */
export interface SubscribeTarget {
  kind: MediaType;
  source: "tmdb" | "douban";
  tmdbId?: number;
  doubanId?: string;
  title: string;
  year?: number;
}

/**
 * 订阅弹层：一次点击完成订阅，复杂度沉到默认值。
 *
 * 流程（对应后端 /subscriptions/prepare 的三态）：
 *   loading → ready（渲染季选择 + 追新开关 + 规则组）
 *           → ambiguous（豆瓣收敛歧义：候选墙确认一次后重新 prepare）
 *           → not_found（TMDB 未收录，无法订阅）
 * 已订阅的条目进入管理态：展示状态并提供取消订阅。
 *
 * 默认值策略：剧集默认勾选全部已播出的正季（特别季 0 须手动勾）、
 * 在播剧默认打开「持续追新」；规则组默认选中系统默认组。
 */
export function SubscribeDialog({
  target,
  onClose,
  onChanged,
}: {
  target: SubscribeTarget | null;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const [prepared, setPrepared] = useState<PrepareResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ruleSets, setRuleSets] = useState<RuleSet[]>([]);
  const [libraries, setLibraries] = useState<MediaLibrary[]>([]);
  const [selectedSeasons, setSelectedSeasons] = useState<Set<number>>(new Set());
  const [followFuture, setFollowFuture] = useState(false);
  const [ruleSetId, setRuleSetId] = useState<number | null>(null);
  const [qualityMode, setQualityMode] = useState<QualityMode>("off");
  const [targetRuleSetId, setTargetRuleSetId] = useState<number | null>(null);
  // 快捷新建规则组（编辑器叠在本弹窗之上，保存后自动选中新组）
  const [creatingRuleSet, setCreatingRuleSet] = useState(false);
  const [libraryId, setLibraryId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  // 投递路由预检：选库即预演"下载会落到哪、能否自动入库"，配置问题当场亮出
  const [dispatchPreview, setDispatchPreview] = useState<DispatchPreview | null>(null);
  // 收藏范围路由的预选结论：打开弹窗时按作品特征算出的默认库 + 中文理由。
  // 规则只决定默认值——用户改选其它库即显式指定，徽标随之消失
  const [routed, setRouted] = useState<{ libraryId: number; reason: string | null } | null>(null);

  useEffect(() => {
    if (!target || libraryId === null) {
      setDispatchPreview(null);
      return;
    }
    let cancelled = false;
    setDispatchPreview(null);
    getDispatchPreview(target.kind, libraryId)
      .then((p) => {
        if (!cancelled) setDispatchPreview(p);
      })
      .catch(() => {
        /* 预检失败静默：只是提示层，不影响订阅主流程 */
      });
    return () => {
      cancelled = true;
    };
  }, [target, libraryId]);

  /** 预检并按结果初始化表单默认值（候选确认后会带着 tmdbId 再次进入）。 */
  const runPrepare = useCallback(
    async (t: SubscribeTarget) => {
      setPrepared(null);
      setError(null);
      try {
        const [result, rules, libs] = await Promise.all([
          prepareSubscription(
            t.source === "douban" && !t.tmdbId
              ? {
                  source: "douban",
                  kind: t.kind,
                  title: t.title,
                  year: t.year,
                  douban_id: t.doubanId,
                }
              : { source: "tmdb", kind: t.kind, tmdb_id: t.tmdbId, douban_id: t.doubanId },
          ),
          listRuleSets(),
          listLibraries(t.kind),
        ]);
        setRuleSets(rules);
        const defaultRuleId = rules.find((r) => r.is_default)?.id ?? rules[0]?.id ?? null;
        setRuleSetId(defaultRuleId);
        setTargetRuleSetId(defaultRuleId);
        setQualityMode("off");
        setLibraries(libs);
        // 默认库 = 收藏范围路由的结论（按作品的类型/区域自动选库，带中文理由）；
        // 预检失败或没有路由结论时回落该类型默认库
        const fallbackId = libs.find((l) => l.is_default)?.id ?? libs[0]?.id ?? null;
        setRouted(null);
        let pickedId = fallbackId;
        if (result.status === "ready" && result.media) {
          const p = await getDispatchPreview(t.kind, null, result.media.tmdb_id).catch(
            () => null,
          );
          if (p?.library_id != null && libs.some((l) => l.id === p.library_id)) {
            pickedId = p.library_id;
            setRouted({ libraryId: p.library_id, reason: p.route_reason });
          }
        }
        setLibraryId(pickedId);
        setPrepared(result);
        // 默认勾选全部已播出的正季；在播剧默认追新
        const airedSeasons = result.seasons
          .filter((s) => s.season_number > 0 && s.aired_count > 0)
          .map((s) => s.season_number);
        setSelectedSeasons(new Set(airedSeasons));
        setFollowFuture(
          t.kind === "tv" && result.media?.status === "Returning Series",
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : "预检失败，请稍后重试");
      }
    },
    [],
  );

  useEffect(() => {
    if (target) void runPrepare(target);
  }, [target, runPrepare]);

  const toggleSeason = (n: number) =>
    setSelectedSeasons((prev) => {
      const next = new Set(prev);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });

  const pickCandidate = (candidate: ResolveCandidate) => {
    if (!target) return;
    void runPrepare({ ...target, tmdbId: candidate.tmdb_id });
  };

  const submit = async () => {
    if (!target || !prepared?.media) return;
    setBusy(true);
    setError(null);
    try {
      await createSubscription({
        kind: prepared.media.kind,
        tmdb_id: prepared.media.tmdb_id,
        selected_seasons: [...selectedSeasons].sort((a, b) => a - b),
        follow_future: followFuture,
        rule_set_id: ruleSetId,
        library_id: libraryId,
        douban_id: target.doubanId ?? null,
        quality_policy:
          qualityMode === "off"
            ? null
            : {
                mode: qualityMode,
                target_rule_set_id: qualityMode === "upgrade" ? targetRuleSetId : null,
              },
      });
      onChanged?.();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "订阅失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const unsubscribe = async () => {
    if (!prepared?.existing_subscription_id) return;
    setBusy(true);
    try {
      await deleteSubscription(prepared.existing_subscription_id);
      onChanged?.();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "取消订阅失败");
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = useMemo(() => {
    if (!prepared?.media || busy) return false;
    if (qualityMode === "upgrade" && targetRuleSetId === null) return false;
    if (prepared.media.kind === "movie") return true;
    return selectedSeasons.size > 0 || followFuture;
  }, [prepared, busy, selectedSeasons, followFuture, qualityMode, targetRuleSetId]);

  if (!target) return null;

  return (
    <Modal open onClose={onClose} label={`订阅《${target.title}》`} width="lg">
      <div className="scroll-thin max-h-[76dvh] overflow-y-auto p-6 max-md:p-5">
          <h2 className="text-title font-bold text-white">
            订阅追踪
            <span className="ml-2 text-ui font-normal text-[var(--text-muted)]">
              {target.title}
              {target.year ? ` (${target.year})` : ""}
            </span>
          </h2>

          {/* —— 加载 / 错误 —— */}
          {!prepared && !error && (
            <div className="mt-8 flex items-center justify-center gap-2.5 pb-4 text-ui text-[var(--text-muted)]">
              <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
              正在获取条目信息…
            </div>
          )}
          {error && (
            <p className="mt-4 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-ui leading-6 text-red-200">
              {error}
            </p>
          )}

          {/* —— 豆瓣收敛：未收录 —— */}
          {prepared?.status === "not_found" && (
            <p className="mt-4 text-ui leading-6 text-[var(--text-muted)]">
              TMDB 未收录该条目，暂时无法订阅。订阅依赖 TMDB
              的别名与季集数据来匹配站点资源，可尝试在 TMDB 搜索入口确认条目后再订阅。
            </p>
          )}

          {/* —— 豆瓣收敛：多候选确认 —— */}
          {prepared?.status === "ambiguous" && (
            <div className="mt-4">
              <p className="text-ui text-[var(--text-muted)]">
                找到多个可能的条目，请确认你订阅的是哪一部：
              </p>
              <div className="mt-3 grid grid-cols-4 gap-3 max-md:grid-cols-3 max-md:gap-2">
                {prepared.candidates.map((c) => (
                  <button
                    key={c.tmdb_id}
                    type="button"
                    onClick={() => pickCandidate(c)}
                    className="group text-left"
                  >
                    <div className="aspect-[2/3] overflow-hidden rounded-lg bg-[#141824] ring-1 ring-white/10 transition group-hover:ring-white/40">
                      <PosterImage
                        src={c.poster_url ? cachedImageUrl(c.poster_url) : undefined}
                        alt={c.title}
                        className="size-full"
                      />
                    </div>
                    <p className="mt-1.5 truncate text-sub text-white/90">{c.title}</p>
                    <p className="truncate text-caption text-[var(--text-faint)]">
                      {c.year ?? "年份未知"}
                    </p>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* —— 已订阅：管理态 —— */}
          {prepared?.status === "ready" && prepared.existing_subscription_id && (
            <div className="mt-4">
              <p className="flex items-center gap-2 text-ui text-white/85">
                <CheckIcon className="size-4 text-[#4ade80]" />
                该{target.kind === "movie" ? "电影" : "剧集"}已在订阅中，movieclaw
                正在持续追踪资源。
              </p>
              <div className="mt-5 flex justify-end gap-3">
                <button
                  type="button"
                  onClick={onClose}
                  className="btn-glass h-9 px-4 text-ui font-medium"
                >
                  好的
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={unsubscribe}
                  className="h-9 rounded-full border border-red-400/30 bg-red-500/10 px-4 text-ui font-medium text-red-200 transition hover:bg-red-500/20 disabled:opacity-50"
                >
                  取消订阅
                </button>
              </div>
            </div>
          )}

          {/* —— 订阅表单 —— */}
          {prepared?.status === "ready" && !prepared.existing_subscription_id && (
            <div className="mt-4 space-y-5">
              {prepared.movie_owned && (
                <p className="flex items-center gap-2 rounded-xl border border-[#4ade80]/25 bg-[#4ade80]/10 px-3.5 py-2.5 text-sub text-[#4ade80]">
                  <CheckIcon className="size-4 shrink-0" />
                  媒体库里已有这部电影，订阅后不会重复下载
                </p>
              )}
              {prepared.media?.kind === "tv" && (
                <section>
                  <h3 className="mb-2 text-ui font-semibold text-white/85">
                    选择要收录的季
                    <span className="ml-2 font-normal text-[var(--text-faint)]">
                      勾选即要整季（含未播集）
                    </span>
                  </h3>
                  <div className="space-y-1.5">
                    {prepared.seasons.map((s) => (
                      <SeasonRow
                        key={s.season_number}
                        season={s}
                        checked={selectedSeasons.has(s.season_number)}
                        onToggle={() => toggleSeason(s.season_number)}
                      />
                    ))}
                  </div>

                  <label className="mt-4 flex cursor-pointer items-center justify-between rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3">
                    <span>
                      <span className="block text-ui font-medium text-white/90">
                        持续追新
                      </span>
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

              {ruleSets.length > 0 && (
                <section>
                  <div className="mb-2 flex items-center justify-between">
                    <h3 className="text-ui font-semibold text-white/85">资源规则</h3>
                    <button
                      type="button"
                      onClick={() => setCreatingRuleSet(true)}
                      className="text-sub font-medium text-[var(--accent)] hover:underline"
                    >
                      + 新建规则组
                    </button>
                  </div>
                  <select
                    value={ruleSetId ?? undefined}
                    onChange={(e) => setRuleSetId(Number(e.target.value))}
                    className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-white/90 outline-none focus:border-white/25 [&>option]:bg-[#181c28]"
                  >
                    {ruleSets.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.name}
                        {r.is_default ? "（默认）" : ""}
                      </option>
                    ))}
                  </select>
                  {/* 所选组的条件摘要：选规则不再是「盲选」 */}
                  {(() => {
                    const picked = ruleSets.find((r) => r.id === ruleSetId);
                    if (!picked) return null;
                    const chips = specSummary(picked.spec);
                    return (
                      <p className="mt-1.5 flex flex-wrap gap-1.5">
                        {chips.length === 0 ? (
                          // 全不限是个危险默认：第一批抓到的可能是低清枪版或
                          // 零做种死种，用琥珀色把风险讲在订阅之前
                          <span className="text-caption text-[#f5c451]/90">
                            该规则组不限任何条件——可能抓到低画质或无人做种的资源，
                            建议在「设置 → 订阅 → 规则组」里加上分辨率与做种数限制
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
                    );
                  })()}
                </section>
              )}

              {ruleSets.length > 0 && prepared.media && (
                <SubscriptionQualityPolicyFields
                  kind={prepared.media.kind}
                  ruleSets={ruleSets}
                  mode={qualityMode}
                  targetRuleSetId={targetRuleSetId}
                  onModeChange={setQualityMode}
                  onTargetRuleSetChange={setTargetRuleSetId}
                />
              )}

              {libraries.length > 0 && (
                <section>
                  <h3 className="mb-2 text-ui font-semibold text-white/85">入库到</h3>
                  <select
                    value={libraryId ?? undefined}
                    onChange={(e) => setLibraryId(Number(e.target.value))}
                    className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-white/90 outline-none focus:border-white/25 [&>option]:bg-[#181c28]"
                  >
                    {libraries.map((l) => (
                      <option key={l.id} value={l.id}>
                        {l.name}
                        {l.is_default ? "（默认）" : ""}
                      </option>
                    ))}
                  </select>
                  {/* 收藏范围路由徽标：说明"为什么默认选了这个库"；用户改库即消失 */}
                  {routed && routed.reason && libraryId === routed.libraryId && (
                    <p className="mt-1.5 text-caption leading-relaxed text-[var(--accent)]/90">
                      自动选库：{routed.reason}
                    </p>
                  )}
                  {/* 投递路由预检：与后端真实投递同源判定，配置问题当场亮出 */}
                  {dispatchPreview &&
                    (dispatchPreview.ok ? (
                      <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
                        {dispatchPreview.mode === "watch" ? (
                          dispatchPreview.staging_path ? (
                            <>
                              将投递到监听导入目录{" "}
                              <span className="font-mono">{dispatchPreview.path}</span>
                              ，下载完成后整理到{" "}
                              <span className="font-mono">{dispatchPreview.staging_path}</span>
                              ，文件进入媒体库根目录后自动入账
                            </>
                          ) : (
                            <>
                              将投递到监听导入目录{" "}
                              <span className="font-mono">{dispatchPreview.path}</span>
                              ，下载完成后自动整理入库
                            </>
                          )
                        ) : (
                          (() => {
                            const folder = prepared.media
                              ? `/${prepared.media.title}${
                                  prepared.media.year ? ` (${prepared.media.year})` : ""
                                }`
                              : "";
                            return (
                              <>
                                将直接下载到库内目录{" "}
                                <span className="font-mono">
                                  {dispatchPreview.path?.replace(/\/+$/, "")}
                                  {folder}
                                </span>
                                ，完成后自动入账
                              </>
                            );
                          })()
                        )}
                      </p>
                    ) : (
                      <p className="mt-1.5 rounded-lg border border-amber-400/25 bg-amber-500/10 px-3 py-2 text-caption leading-relaxed text-amber-200">
                        {dispatchPreview.warning}
                      </p>
                    ))}
                </section>
              )}

              <div className="flex justify-end gap-3 pt-1">
                <button
                  type="button"
                  onClick={onClose}
                  className="btn-glass h-9 px-4 text-ui font-medium"
                >
                  取消
                </button>
                <button
                  type="button"
                  disabled={!canSubmit}
                  onClick={submit}
                  className="btn-accent h-9 rounded-full px-5 text-ui font-semibold disabled:opacity-50"
                >
                  {busy ? "正在订阅…" : "确认订阅"}
                </button>
              </div>
            </div>
          )}
      </div>
      {creatingRuleSet && (
        <RuleSetEditorDialog
          ruleSet={null}
          raised
          onClose={() => setCreatingRuleSet(false)}
          onSaved={(saved) => {
            setCreatingRuleSet(false);
            setRuleSets((prev) => [...prev, saved]);
            setRuleSetId(saved.id);
          }}
        />
      )}
    </Modal>
  );
}

/** 季选择行：季名 + 播出进度；未播季弱化显示但可勾（勾了=要整季）。
 *  订阅弹窗与调整订阅弹窗（subscription-adjust-dialog）共用。 */
export function SeasonRow({
  season,
  checked,
  onToggle,
}: {
  season: SeasonOverview;
  checked: boolean;
  onToggle: () => void;
}) {
  const total = season.episode_count ?? 0;
  const progress =
    season.aired_count >= total && total > 0
      ? `全 ${total} 集已播完`
      : total > 0
        ? `已播 ${season.aired_count}/${total} 集`
        : season.aired_count > 0
          ? `已播 ${season.aired_count} 集`
          : "未播出";
  // 库存提示（媒体库联通）：已有的集不会重复下载
  const owned =
    season.owned_count > 0
      ? season.owned_count >= total && total > 0
        ? "整季已在库"
        : `库里已有 ${season.owned_count} 集`
      : null;
  return (
    <label
      className={`flex cursor-pointer items-center justify-between rounded-xl border px-4 py-2.5 transition ${
        checked
          ? "border-white/20 bg-white/[0.08]"
          : "border-white/[0.06] bg-white/[0.02] hover:bg-white/[0.05]"
      }`}
    >
      <span className="flex items-baseline gap-2.5">
        <span className="text-ui font-medium text-white/90">
          {season.season_number === 0 ? "特别篇" : `第 ${season.season_number} 季`}
        </span>
        <span className="tnum text-caption text-[var(--text-faint)]">{progress}</span>
        {owned && (
          <span className="tnum text-caption font-medium text-[#4ade80]/90">{owned}</span>
        )}
      </span>
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        className="size-4 accent-[var(--accent-2)]"
      />
    </label>
  );
}
