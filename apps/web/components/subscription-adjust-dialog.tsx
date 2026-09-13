"use client";

/**
 * 调整订阅弹窗（订阅详情页「调整 ›」入口）：创建后修改季选择 / 入库目标库。
 * 后端 subscriptions.update 支持 diff 重算工单（加季补工单、减季收
 * 未投递工单、已下载/已入库一律保留），此前前端只放开了换规则组——本弹窗
 * 补齐剩余两项，用户不必再"取消订阅重订"（那会丢活动记录）。自动续订是
 * 详情页「更多」里的独立动作，不与批量调整混在一起。
 *
 * 数据源与订阅弹窗同源：季结构走内部 title-preview（幂等，带播出/库存进度），
 * 库选择即时走投递预检。只提交发生变化的字段（PATCH 部分更新语义）。
 *
 * 减季的清理收尾（season-cleanup-dialog.tsx）刻意做成保存**之后**的第二步：
 * ① 保存按钮不该变成破坏性操作——同一个弹窗还能改入库库，调库的人不该撞见
 * 一排删除勾选；② 清理数量必须是实测的，想在保存前给出数量就得把后端的出域
 * 判定复制一份到前端，那是第二份真相源。先保存，再拿真实的 reconcile 结果追问。
 */

import { useEffect, useMemo, useState } from "react";

import { useToast } from "@/components/feedback";
import { Modal } from "@/components/modal";
import { SeasonCleanupDialog } from "@/components/season-cleanup-dialog";
import { SeasonRow } from "@/components/subscribe-dialog";
import { listLibraries, type MediaLibrary } from "@/lib/api/libraries";
import {
  cleanupSubscriptionSeasons,
  getSubscriptionRemovalPreview,
  previewSubscriptionDownloadRouting,
  previewSubscriptionTitle,
  updateSubscription,
  type DispatchPreview,
  type SeasonOverview,
  type SubscriptionDetail,
  type SubscriptionRemovalOptions,
  type SubscriptionRemovalPreview,
} from "@/lib/api/subscriptions";
import { usePermissions } from "@/lib/permissions";

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
  const { canManageSubscriptions } = usePermissions();
  const toast = useToast();
  const isMovie = detail.media.kind === "movie";
  // null = 加载中；[] 也是有效结果（电影没有季）
  const [seasons, setSeasons] = useState<SeasonOverview[] | null>(isMovie ? [] : null);
  const [libraries, setLibraries] = useState<MediaLibrary[]>([]);
  const [selectedSeasons, setSelectedSeasons] = useState<Set<number>>(
    () => new Set(detail.selected_seasons),
  );
  const [libraryId, setLibraryId] = useState<number | null>(detail.library_id);
  const [preview, setPreview] = useState<DispatchPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 保存成功后的清理追问；null = 不追问（没减季 / 无可清理内容 / 预览失败）
  const [cleanup, setCleanup] = useState<{
    seasons: number[];
    preview: SubscriptionRemovalPreview;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      isMovie
        ? Promise.resolve(null)
        : previewSubscriptionTitle({
            title_ref: `tmdb:${detail.media.kind}:${detail.media.tmdb_id}`,
          }),
      canManageSubscriptions
        ? listLibraries(detail.media.kind)
        : Promise.resolve([]),
    ])
      .then(([prepared, libs]) => {
        if (cancelled) return;
        if (prepared) setSeasons(prepared.status === "ready" ? prepared.seasons : []);
        setLibraries(libs);
      })
      .catch(() => {
        if (!cancelled) setError("加载季集与媒体库信息失败，请稍后重试");
      });
    return () => {
      cancelled = true;
    };
  }, [canManageSubscriptions, detail.media.kind, detail.media.tmdb_id, isMovie]);

  // 选库即预演投递落点（与订阅弹窗同一套提示；null=该类型默认库也预演）
  useEffect(() => {
    if (!canManageSubscriptions) {
      setPreview(null);
      return;
    }
    let cancelled = false;
    setPreview(null);
    previewSubscriptionDownloadRouting(detail.media.kind, libraryId, detail.media.tmdb_id)
      .then((p) => {
        if (!cancelled) setPreview(p);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [canManageSubscriptions, detail.media.kind, detail.media.tmdb_id, libraryId]);

  const toggleSeason = (n: number) =>
    setSelectedSeasons((prev) => {
      const next = new Set(prev);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });

  // 被取消勾选、且已有进度（有工单走到 grabbed 之后）的季：减季不动下载器
  // 任务，但会停止业务关联与换源——把后果讲在保存之前。
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
      library: canManageSubscriptions && libraryId !== detail.library_id,
    };
  }, [canManageSubscriptions, detail, isMovie, libraryId, selectedSeasons]);
  const dirty = changed.seasons || changed.library;

  const save = async () => {
    setBusy(true);
    setError(null);
    // 本次被移出范围的季——清理追问的范围。保存前算好：保存后季状态已经变了
    const dropped = changed.seasons
      ? detail.selected_seasons.filter((s) => !selectedSeasons.has(s))
      : [];
    try {
      await updateSubscription(detail.id, {
        ...(changed.seasons
          ? { selected_seasons: [...selectedSeasons].sort((a, b) => a - b) }
          : {}),
        // 显式带上 null 即「清除指定库、改回默认库路由」（后端区分未传与 null）
        ...(canManageSubscriptions && changed.library ? { library_id: libraryId } : {}),
      });
      toast.success("订阅已调整");
      if (dropped.length > 0 && canManageSubscriptions) {
        // 数量取自后端保存后的真实出域结果，不在前端推算
        const plan = await getSubscriptionRemovalPreview(detail.id, dropped).catch(() => null);
        if (plan === null) {
          // 预览拿不到就不追问——没数字的勾选框等于让用户盲签。但要明确说出
          // "没做清理"，用户才知道要不要自己去处理
          toast.info("订阅已调整；这一季的内容未作清理");
        } else if (plan.torrent_count > 0 || plan.library_file_count > 0) {
          setCleanup({ seasons: dropped, preview: plan });
          setBusy(false);
          return; // 等用户在追问弹窗里决定；onSaved() 推迟到那一步
        }
      }
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "调整失败，请稍后重试");
      setBusy(false);
    }
  };

  /** 追问弹窗点了「清理」：订阅已经调整完，这里只处理内容清理的成败。 */
  const confirmCleanup = async (options: SubscriptionRemovalOptions) => {
    if (cleanup === null) return;
    try {
      await cleanupSubscriptionSeasons(detail.id, cleanup.seasons, options);
      toast.success("正在后台清理，可在「任务中心」查看进度");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "清理失败，请稍后重试");
    }
    setCleanup(null);
    onSaved();
  };

  // 减季保存成功且有内容可清理：由追问弹窗接手（不叠在本弹窗之上——季已经改完，
  // 让两个弹窗同时在场只会让人以为还能回去改）
  if (cleanup !== null) {
    return (
      <SeasonCleanupDialog
        open
        title={detail.media.title}
        seasons={cleanup.seasons}
        preview={cleanup.preview}
        onClose={() => {
          setCleanup(null);
          onSaved();
        }}
        onConfirm={confirmCleanup}
      />
    );
  }

  return (
    <Modal open onClose={onClose} label="调整订阅" width="lg" panelClassName="max-h-[76dvh]">
      {/* 头部常驻 */}
      <div className="border-b border-white/[0.07] px-6 pb-4 pt-6 max-md:px-5">
        <h2 className="text-title font-bold text-white">调整订阅</h2>
        <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
          《{detail.media.title}》——加季会恢复或补建追踪；减季会让整季退出追踪范围，
          但不会删除下载器任务、已下载文件或入库内容。
        </p>
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-6 max-md:p-5">
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
                  第 {droppedWithProgress.join("、")} 季已有下载进度：保存只让它退出追踪
                  （停止进度关联、缺失搜索与自动换源），不会动任何文件
                  {canManageSubscriptions && "；保存后会问你要不要顺手清理这一季的内容"}
                </p>
              )}
            </section>
          )}

          {canManageSubscriptions && libraries.length > 0 && (
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
                      ? `将投递到自动入库的监听目录 ${preview.path ?? ""}，下载完成后自动整理入库`
                      : // 条目目录由后端按命名模板渲染，前端不自己拼名字
                        `将直接下载到库内目录 ${preview.entry_dir ?? preview.path ?? ""}，完成后自动入账`}
                  </p>
                ) : (
                  <p className="mt-1.5 rounded-lg border border-amber-400/25 bg-amber-500/10 px-3 py-2 text-caption leading-relaxed text-amber-200">
                    {preview.warning}
                  </p>
                ))}
            </section>
          )}
        </div>
      </div>

      {/* 底栏常驻：季数一多就要滚，保存按钮不能被滚出屏幕 */}
      <div className="flex justify-end gap-3 border-t border-white/[0.07] px-6 py-4 max-md:px-5">
        <button type="button" onClick={onClose} className="btn-glass h-9 px-4 text-ui font-medium">
          取消
        </button>
        <button
          type="button"
          disabled={busy || !dirty || (!isMovie && selectedSeasons.size === 0)}
          onClick={() => void save()}
          className="btn-accent h-9 rounded-full px-5 text-ui font-semibold disabled:opacity-40"
        >
          {busy ? "保存中…" : "保存调整"}
        </button>
      </div>
    </Modal>
  );
}
