"use client";

/**
 * 下载目标选择弹窗（手动下载的保存位置确认）。
 *
 * 点搜索结果的「下载」先弹出本层，让用户明确文件会落到哪，而不是静默
 * 提交后再猜。候选来源分三层：
 *   1. 智能入库（种子解析出可靠身份时）：走后端与订阅同源的三级兜底
 *      （监听目录 / 库条目目录），并用 dispatch-preview 预检结论展示
 *      真实归宿与配置警示；
 *   2. 下载器已配置的目录：默认保存目录 + 各路径映射的 movieclaw 侧目录，
 *      双视角展示（movieclaw 路径 → 下载器路径），跨容器部署一眼可核对；
 *   3. 下载器默认目录兜底：不指定路径，由下载器自行决定。
 * 底部小字引导去「设置 → 下载器」配置路径映射。
 *
 * 保存位置记忆（docs/design/download-target-memory.md）：提交成功即按种子分类
 * （TorrentCategory）记住本次选择，**不需要用户勾选任何东西**。下次点该分类的
 * 「下载」先弹确认条（本文件的 DownloadTargetConfirmBar），看得见落点再确认。
 * 旧版那个「记住本次选择」复选框已删除且不应以任何形式回归——它默认不勾、
 * 又要求用户预判「以后还会不会下同类的」，是这个功能长期形同虚设的根因。
 */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { FolderIcon } from "@/components/icons";
import { CATEGORY_LABEL, type TorrentCategory } from "@/lib/categories";
import { formatRelativeTime } from "@/lib/time";
import { Modal } from "@/components/modal";
import {
  listDownloaders,
  submitTorrentDownload,
  type ConfiguredDownloader,
  type DownloadSubmitResult,
  type DownloadTargetPref,
  type ManualDownloadTarget,
  type PathMapping,
  resolveManualDownloadTarget,
} from "@/lib/api/downloaders";

/** 弹窗需要的种子身份切片（由搜索结果的 TorrentHit 提炼）。 */
export interface DownloadTargetRequest {
  site_id: string;
  download_url: string;
  /** 站点内种子 ID：随提交锚定，任务中心据此提供「打开种子页」 */
  torrent_id: string;
  /** 解析出的条目身份；三件套不全时为 null（智能入库选项不出现） */
  identity: { kind: "movie" | "tv"; title: string; year: number } | null;
  subtitle: string | null;
  /**
   * 种子分类（TorrentHit.category ?? "other"）：记忆的桶键。
   * 用站点声明的一级分类而非 enrich 推断的 media_type/content_type——推断值会
   * null、同一部剧的两条种子可能判得不一致，还会随模型版本漂移，拿它当持久化
   * 偏好的键会让用户的记忆桶在某次升级后悄悄换位置。
   */
  category: string;
}

/* —— 记忆命中后的直接提交 —— */

/**
 * 按记住的目标提交（确认条的「确认下载」），不经完整弹窗。
 *
 * smart 目标存的是**策略不是路径**，每次都要重跑 TMDB 身份确认与投递预检；
 * 未收敛或配置有警示时返回 null，调用方据此展开完整弹窗并说明原因——绝不把
 * 不确定的资源静默放进默认库。
 */
export async function submitRememberedTarget(
  request: DownloadTargetRequest,
  target: DownloadTargetPref,
): Promise<DownloadSubmitResult | null> {
  const identity = request.identity;
  let libraryPart = {};
  if (target.kind === "smart") {
    if (!identity) return null;
    const resolved = await resolveManualDownloadTarget({
      kind: identity.kind,
      title: identity.title,
      year: identity.year,
      subtitle: request.subtitle,
      downloader_id: target.downloader_id,
    }).catch(() => null);
    if (!resolved?.ok || resolved.status !== "ready" || resolved.tmdb_id == null) return null;
    libraryPart = {
      auto_route: true,
      media_kind: identity.kind,
      tmdb_id: resolved.tmdb_id,
      title: identity.title,
      year: identity.year,
      subtitle: request.subtitle,
    };
  }
  return submitTorrentDownload({
    site_id: request.site_id,
    download_url: request.download_url,
    torrent_id: request.torrent_id,
    category: request.category,
    ...libraryPart,
    ...(target.kind === "dir" ? { save_path: target.save_path } : {}),
    ...(target.downloader_id != null ? { downloader_id: target.downloader_id } : {}),
  });
}

/** 与后端 translate_save_path 同规则的前端版（仅用于展示下载器视角）。 */
function toRemoteView(path: string, mappings: PathMapping[] | null): string {
  if (!mappings) return path;
  let best: PathMapping | null = null;
  for (const m of mappings) {
    const local = m.local.replace(/\/+$/, "");
    if (
      (path === local || path.startsWith(local + "/")) &&
      (best === null || local.length > best.local.replace(/\/+$/, "").length)
    ) {
      best = m;
    }
  }
  if (!best) return path;
  const local = best.local.replace(/\/+$/, "");
  return best.remote.replace(/\/+$/, "") + path.slice(local.length);
}

/** 一个可选的保存目标。 */
interface TargetOption {
  key: string;
  kind: "smart" | "dir" | "default";
  /** 提交时的 save_path（smart/default 为 null，走各自的后端语义） */
  savePath: string | null;
  label: string;
  /** 次级说明（路径、双视角、警示等） */
  detail: string | null;
  warning: string | null;
}

export function DownloadTargetDialog({
  request,
  remembered = null,
  reason = null,
  onClose,
  onSubmitted,
  topmost = false,
}: {
  /** null = 关闭 */
  request: DownloadTargetRequest | null;
  /** 该分类已有的记忆：用于预选中对应项；null = 没有记忆，按默认规则挑 */
  remembered?: DownloadTargetPref | null;
  /** 记忆失效时的中文原因，显示在弹窗顶部；静默回落是原实现最让人困惑的地方 */
  reason?: string | null;
  onClose: () => void;
  onSubmitted: (result: DownloadSubmitResult) => void;
  /** 触发按钮长在灯箱这类高层浮层里时置位，弹窗抬到最高层（见 Modal 的层级约定） */
  topmost?: boolean;
}) {
  if (!request) return null;
  // 以 request 为 key 强制内容组件重新挂载：每次打开都从全新状态开始，
  // 避免默认选中 effect 读到上一次的旧数据抢先选中错误项。
  return (
    <DialogContent
      key={`${request.site_id}:${request.download_url}`}
      request={request}
      remembered={remembered}
      reason={reason}
      topmost={topmost}
      onClose={onClose}
      onSubmitted={onSubmitted}
    />
  );
}

function DialogContent({
  request,
  remembered,
  reason,
  onClose,
  onSubmitted,
  topmost,
}: {
  request: DownloadTargetRequest;
  remembered: DownloadTargetPref | null;
  reason: string | null;
  onClose: () => void;
  onSubmitted: (result: DownloadSubmitResult) => void;
  topmost: boolean;
}) {
  // 记忆是「智能入库」时不必展开目录列表——预选的就是它
  const rememberedTarget = remembered;
  const revealOtherInitially =
    request.identity === null ||
    (rememberedTarget !== null && rememberedTarget.kind !== "smart");
  // 可用（启用 + 验证通过）的全部下载器：≥2 台时出现下载器选择
  const [downloaders, setDownloaders] = useState<ConfiguredDownloader[]>([]);
  const [downloaderId, setDownloaderId] = useState<number | null>(
    rememberedTarget?.kind === "smart" ? rememberedTarget.downloader_id : null,
  );
  const [manualTarget, setManualTarget] = useState<ManualDownloadTarget | null>(null);
  const [selectedCandidateId, setSelectedCandidateId] = useState<number | null>(null);
  const [showOtherTargets, setShowOtherTargets] = useState(revealOtherInitially);
  const [downloadersLoaded, setDownloadersLoaded] = useState(false);
  const [loadingDownloaders, setLoadingDownloaders] = useState(false);
  const [loadingTarget, setLoadingTarget] = useState(request.identity !== null);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 下载器切换/候选确认都可能重发预检，只允许最后一次请求更新界面。
  const targetRequestId = useRef(0);

  // 默认路径只做智能预检，不拉下载器配置；只有用户展开“其他保存位置”或
  // 智能识别失败时才加载下载器与路径映射，减少每次点下载的固定请求成本。
  useEffect(() => {
    let cancelled = false;
    const requestId = ++targetRequestId.current;
    const identity = request.identity;
    setSelectedCandidateId(null);
    if (!identity) {
      setLoadingTarget(false);
      setShowOtherTargets(true);
      return () => {
        cancelled = true;
        targetRequestId.current += 1;
      };
    }

    setLoadingTarget(true);
    void resolveManualDownloadTarget({
      kind: identity.kind,
      title: identity.title,
      year: identity.year,
      subtitle: request.subtitle,
      downloader_id: rememberedTarget?.kind === "smart" ? rememberedTarget.downloader_id : null,
    })
      .then((target) => {
        if (cancelled || requestId !== targetRequestId.current) return;
        setManualTarget(target);
        if (target.status !== "ready" || !target.ok) setShowOtherTargets(true);
      })
      .catch(() => {
        if (cancelled || requestId !== targetRequestId.current) return;
        setManualTarget(null);
        setShowOtherTargets(true);
      })
      .finally(() => {
        if (!cancelled && requestId === targetRequestId.current) setLoadingTarget(false);
      });
    return () => {
      cancelled = true;
      targetRequestId.current += 1;
    };
  }, [rememberedTarget, request]);

  /** 用当前下载器和（如有）用户确认的歧义候选重跑预检。 */
  const reloadManualTarget = useCallback(
    (nextDownloaderId: number | null, tmdbId: number | null) => {
      const identity = request.identity;
      if (!identity) return;
      const requestId = ++targetRequestId.current;
      setLoadingTarget(true);
      setManualTarget(null);
      void resolveManualDownloadTarget({
        kind: identity.kind,
        title: identity.title,
        year: identity.year,
        subtitle: request.subtitle,
        downloader_id: nextDownloaderId,
        selected_tmdb_id: tmdbId,
      })
        .then((target) => {
          if (requestId === targetRequestId.current) setManualTarget(target);
        })
        .catch(() => {
          if (requestId === targetRequestId.current) setManualTarget(null);
        })
        .finally(() => {
          if (requestId === targetRequestId.current) setLoadingTarget(false);
        });
    },
    [request],
  );

  // “其他保存位置”展开后才读取下载器配置；加载完成后若最终选中的不是
  // 初始预检使用的下载器，再补一次预检以保持路径映射口径一致。
  useEffect(() => {
    if (!showOtherTargets || downloadersLoaded) return;
    let cancelled = false;
    setLoadingDownloaders(true);
    void listDownloaders()
      .then((rows) => rows.filter((d) => d.usable))
      .catch(() => [] as ConfiguredDownloader[])
      .then((usable) => {
        if (cancelled) return;
        const current = usable.find((d) => d.id === downloaderId);
        const remembered =
          rememberedTarget?.downloader_id != null
            ? usable.find((d) => d.id === rememberedTarget.downloader_id)
            : undefined;
        const selectedDownloader =
          current ?? remembered ?? usable.find((d) => d.is_default) ?? usable[0] ?? null;
        const selectedDownloaderId = selectedDownloader?.id ?? null;
        const preflightDownloaderId =
          selectedDownloader && !selectedDownloader.is_default ? selectedDownloader.id : null;
        setDownloaders(usable);
        setDownloaderId(selectedDownloaderId);
        setDownloadersLoaded(true);
        setLoadingDownloaders(false);
        if (request.identity && preflightDownloaderId !== downloaderId) {
          reloadManualTarget(preflightDownloaderId, selectedCandidateId);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    downloaderId,
    downloadersLoaded,
    reloadManualTarget,
    rememberedTarget,
    request.identity,
    selectedCandidateId,
    showOtherTargets,
  ]);

  const downloader = useMemo(
    () => downloaders.find((d) => d.id === downloaderId) ?? null,
    [downloaders, downloaderId],
  );

  const options = useMemo<TargetOption[]>(() => {
    const result: TargetOption[] = [];
    if (
      request.identity &&
      manualTarget?.status === "ready" &&
      manualTarget.tmdb_id != null &&
      manualTarget.library_id != null &&
      manualTarget.ok
    ) {
      // 条目目录由后端按命名模板渲染（entry_dir），前端不再自己拼名字——
      // 模板可全局/按库自定义，自己拼出来的预览会与真实落点不符
      const entryDir = manualTarget.entry_dir ?? manualTarget.path;
      const detail =
        manualTarget.mode === "watch"
          ? manualTarget.staging_path
            ? `${manualTarget.route_reason ?? ""}；投递到自动入库的监听目录 ${manualTarget.path}，完成后整理到 ${manualTarget.staging_path}（外部流转回库根后入账）`
            : `${manualTarget.route_reason ?? ""}；投递到自动入库的监听目录 ${manualTarget.path}，完成后自动整理入库`
          : manualTarget.mode === "inplace"
            ? `${manualTarget.route_reason ?? ""}；直接下载到 ${entryDir?.replace(/\/+$/, "")}，完成后自动入账`
            : null;
      result.push({
        key: "smart",
        kind: "smart",
        savePath: null,
        label: `自动入库到「${manualTarget.library_name}」`,
        detail,
        warning: null,
      });
    }
    if (showOtherTargets) {
      const seen = new Set<string>();
      const dirs: { path: string; source: string }[] = [];
      if (downloader?.save_path) {
        dirs.push({ path: downloader.save_path, source: "默认保存目录" });
      }
      for (const m of downloader?.path_mappings ?? []) {
        if (!seen.has(m.local) && m.local !== downloader?.save_path) {
          dirs.push({ path: m.local, source: "路径映射" });
        }
        seen.add(m.local);
      }
      for (const dir of dirs) {
        const remote = toRemoteView(dir.path, downloader?.path_mappings ?? null);
        result.push({
          key: `dir:${dir.path}`,
          kind: "dir",
          savePath: dir.path,
          label: dir.path,
          detail: remote !== dir.path ? `下载器视角：${remote}（${dir.source}）` : dir.source,
          warning: null,
        });
      }
      result.push({
        key: "default",
        kind: "default",
        savePath: null,
        label: "下载器默认目录",
        detail: "不指定路径，由下载器按自身设置决定；movieclaw 不会自动整理入库",
        warning: null,
      });
    }
    return result;
  }, [request, manualTarget, downloader, showOtherTargets]);

  // 换下载器后目录候选整组换血：选中的目录项若已不存在，退回未选中让下方
  // 默认选中逻辑重新挑一个
  useEffect(() => {
    setSelected((prev) => (prev && options.some((o) => o.key === prev) ? prev : null));
  }, [options]);

  // 默认选中：已记住的目标 > 智能入库可用且预检通过 > 第一个目录 > 下载器默认
  useEffect(() => {
    if (options.length === 0 || selected !== null) return;
    if (rememberedTarget) {
      const match = options.find((o) =>
        rememberedTarget.kind === "dir"
          ? o.kind === "dir" && o.savePath === rememberedTarget.save_path
          : o.kind === rememberedTarget.kind,
      );
      if (match) {
        setSelected(match.key);
        return;
      }
    }
    // 没有记忆时等两边都返回再自动选择，避免目录先到就抢选、随后智能结果
    // 出现却无法成为默认；等待期间用户仍可手动点已显示的目录立即提交。
    if (loadingTarget || (showOtherTargets && !downloadersLoaded)) return;
    const smart = options.find((o) => o.kind === "smart");
    if (smart && !smart.warning) setSelected(smart.key);
    // 智能入库不可用或有警示时，回落到第一个非智能项（目录 > 下载器默认）
    else setSelected((options.find((o) => o.kind !== "smart") ?? options[0]).key);
  }, [downloadersLoaded, loadingTarget, options, selected, rememberedTarget, showOtherTargets]);

  const submit = () => {
    const option = options.find((o) => o.key === selected);
    if (!option || busy) return;
    setBusy(true);
    setError(null);
    const identity = request.identity;
    // 只在非默认下载器时显式带 downloader_id：默认台走后端原有语义
    const pickedDownloaderId = downloader
      ? downloader.is_default
        ? null
        : downloader.id
      : downloaderId;
    void submitTorrentDownload({
      site_id: request.site_id,
      download_url: request.download_url,
      torrent_id: request.torrent_id,
      // 带上分类 = 提交成功后由后端记住本次选择（不需要用户勾选任何东西）
      category: request.category,
      ...(option.kind === "smart" && identity && manualTarget?.tmdb_id != null
        ? {
            auto_route: true,
            media_kind: identity.kind,
            tmdb_id: manualTarget.tmdb_id,
            title: identity.title,
            year: identity.year,
            subtitle: request.subtitle,
          }
        : {}),
      ...(option.kind === "dir" ? { save_path: option.savePath } : {}),
      ...(pickedDownloaderId != null ? { downloader_id: pickedDownloaderId } : {}),
    })
      .then((result) => {
        onSubmitted(result);
        onClose();
      })
      .catch((e) => setError(e instanceof Error ? e.message : "提交失败，请重试"))
      .finally(() => setBusy(false));
  };

  return (
    <Modal open topmost={topmost} onClose={onClose} label="选择保存位置">
      {/* 头部常驻：目录一多就得滚，标题不跟着滚走才知道自己在选什么 */}
      <div className="border-b border-white/[0.07] px-6 pb-4 pt-6">
        <h2 className="text-title font-bold text-white">选择保存位置</h2>
      </div>

      <div className="scroll-thin min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-4">
        {reason && (
          <p className="rounded-xl border border-[var(--warn-line,rgba(245,196,81,.28))] bg-[rgba(245,196,81,.09)] px-3 py-2 text-sub leading-relaxed text-[#f6dfab]">
            {reason}
          </p>
        )}
          {error && (
            <p className="rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2.5 text-ui leading-6 text-red-200">
              {error}
            </p>
          )}

          <div className="space-y-2">
              {loadingTarget && request.identity && (
                <div className="flex h-[52px] items-center rounded-xl border border-white/[0.06] bg-white/[0.03] px-3.5 text-caption text-[var(--text-faint)]">
                  正在识别影视条目并预演智能入库…
                </div>
              )}
              {!loadingTarget && request.identity && manualTarget && manualTarget.status !== "ready" && (
                <p className="rounded-lg border border-amber-400/20 bg-amber-500/10 px-3.5 py-2.5 text-caption leading-relaxed text-amber-100">
                  {manualTarget.status === "ambiguous"
                    ? "识别到多个可能条目。请确认正确条目后，系统会继续预演自动入库目录。"
                    : "未自动入库：无法可靠识别该资源；为避免投错库请手选保存目录。"}
                </p>
              )}
              {request.identity && manualTarget?.status === "ambiguous" && (
                <div className="flex flex-wrap gap-2">
                  {manualTarget.candidates.map((candidate) => (
                    <button
                      key={candidate.tmdb_id}
                      type="button"
                      onClick={() => {
                        setSelectedCandidateId(candidate.tmdb_id);
                        reloadManualTarget(downloaderId, candidate.tmdb_id);
                      }}
                      className="rounded-full border border-amber-400/35 px-3 py-1 text-caption text-amber-100 transition-colors hover:bg-amber-500/15"
                    >
                      {candidate.title}
                      {candidate.year ? ` (${candidate.year})` : ""}
                      {candidate.episode_count ? ` · ${candidate.episode_count} 集` : ""}
                    </button>
                  ))}
                </div>
              )}
              {!loadingTarget && request.identity && manualTarget === null && (
                <p className="rounded-lg border border-amber-400/20 bg-amber-500/10 px-3.5 py-2.5 text-caption leading-relaxed text-amber-100">
                  自动识别暂不可用；为避免投错库请手选保存目录后再下载。
                </p>
              )}
              {request.identity && manualTarget?.status === "ready" && !manualTarget.ok && (
                <p className="rounded-lg border border-amber-400/20 bg-amber-500/10 px-3.5 py-2.5 text-caption leading-relaxed text-amber-100">
                  已识别资源，但当前不能自动入库：{manualTarget.warning ?? "请检查媒体库和自动入库配置。"}
                </p>
              )}
              {!showOtherTargets && (
                <button
                  type="button"
                  onClick={() => setShowOtherTargets(true)}
                  className="w-full rounded-xl border border-dashed border-white/[0.1] px-3.5 py-2.5 text-left text-ui text-[var(--text-muted)] transition-colors hover:border-white/20 hover:bg-white/[0.03] hover:text-white/90"
                >
                  其他保存位置
                </button>
              )}
              {showOtherTargets && loadingDownloaders && (
                <div className="h-[52px] animate-pulse rounded-xl bg-white/[0.04]" />
              )}
              {options.map((option) => (
                <button
                  key={option.key}
                  type="button"
                  onClick={() => setSelected(option.key)}
                  data-active={selected === option.key}
                  className="flex w-full items-start gap-2.5 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-left transition-colors hover:border-[var(--accent)]/50 data-[active=true]:border-[var(--accent)]/70 data-[active=true]:bg-[var(--accent-soft)]"
                >
                  <FolderIcon className="mt-0.5 size-4 shrink-0 text-[var(--accent)]/80" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-mono text-ui font-medium text-[var(--text)]">
                      {option.label}
                    </span>
                    {option.detail && (
                      <span className="mt-0.5 block text-caption leading-relaxed text-[var(--text-faint)]">
                        {option.detail}
                      </span>
                    )}
                    {option.warning && (
                      <span className="mt-1 block rounded-md bg-amber-500/10 px-2 py-1 text-caption leading-relaxed text-amber-200">
                        {option.warning}
                      </span>
                    )}
                  </span>
                </button>
              ))}
            </div>

          {/* 下载器分流：≥2 台可用才出现（单台用户界面零变化），默认预选默认台 */}
          {showOtherTargets && downloaders.length >= 2 && (
            <div className="flex items-center gap-2.5">
              <span className="shrink-0 text-sub text-[var(--text-muted)]">下载器</span>
              <select
                value={downloaderId ?? undefined}
                onChange={(e) => {
                  const nextDownloaderId = Number(e.target.value);
                  setDownloaderId(nextDownloaderId);
                  reloadManualTarget(nextDownloaderId, selectedCandidateId);
                }}
                className="min-w-0 flex-1 rounded-lg border border-white/[0.08] bg-white/[0.04] px-3 py-1.5 text-sub text-white/90 outline-none focus:border-white/25 [&>option]:bg-[#181c28]"
              >
                {downloaders.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                    {d.is_default ? "（默认）" : ""}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* 记忆是自动的，这里只陈述事实、不要用户做决定——曾经的复选框
              默认不勾又要人预判「以后还会不会下同类的」，等于永远不生效 */}
          <p className="text-sub leading-relaxed text-[var(--text-muted)]">
            这次的选择会记为「{CATEGORY_LABEL[request.category as TorrentCategory] ??
              request.category}」的默认位置，之后点「下载」先给你确认一次——
            随时可以改，或在确认条上「不再记住」。
          </p>

          {showOtherTargets && (
            <p className="text-caption leading-relaxed text-[var(--text-faint)]">
              movieclaw 与下载器不在同一容器/主机、看到的路径不同？到
              <Link
                href="/settings/downloaders"
                className="mx-0.5 text-[var(--accent)] hover:underline"
              >
                设置 → 下载器
              </Link>
              配置路径映射，提交时会自动翻译成下载器视角。
            </p>
          )}
      </div>

      {/* 底栏常驻：确认按钮永远在屏幕上，不必先把长列表滚到底才找得到 */}
      <div className="flex justify-end gap-3 border-t border-white/[0.07] px-6 py-4">
        <button type="button" onClick={onClose} className="btn-glass h-9 px-4 text-ui font-medium">
          取消
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={busy || selected === null}
          className="btn-accent h-9 rounded-full px-5 text-ui font-semibold disabled:opacity-40"
        >
          {busy ? "提交中…" : "确认下载"}
        </button>
      </div>
    </Modal>
  );
}

/* —— 确认条：命中记忆时代替静默提交 —— */

/** 记忆里存的目标翻译成人话（确认条主行）。 */
function targetHeadline(target: DownloadTargetPref, resolvedPath: string | null): string | null {
  if (target.kind === "dir") return target.save_path;
  if (target.kind === "default") return "下载器的默认目录";
  return resolvedPath; // smart：要等预检算出来
}

/**
 * 保存位置确认条。命中记忆时点「下载」弹它，而不是直接提交。
 *
 * 为什么不沿用原来的静默快速通道：那样点下去种子就已经进了下载器，记忆不对
 * 只能事后补救。这里多一次点击，换来的是**提交前就看得见落点**——批量下载的
 * 代价从「1 次点击 + 不知道去哪」变成「2 次点击 + 全程可见」。
 *
 * 也不做 split button（结果行内主按钮 + 下拉箭头）：移动端那一行已经很挤。
 * 确认条是横向浮层，窄屏纵向堆成三行，不占结果行的横向空间。
 */
export function DownloadTargetConfirmBar({
  request,
  target,
  onConfirm,
  onChange,
  onForget,
  onClose,
}: {
  request: DownloadTargetRequest;
  target: DownloadTargetPref;
  onConfirm: () => void;
  onChange: () => void;
  /** 「不再记住」：清除该分类记忆后展开完整弹窗重选 */
  onForget: () => void;
  onClose: () => void;
}) {
  // smart 目标存的是策略不是路径，得重跑预检才知道这次落到哪
  const [resolvedPath, setResolvedPath] = useState<string | null>(null);
  const [preflighting, setPreflighting] = useState(target.kind === "smart");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const identity = request.identity;
    if (target.kind !== "smart" || !identity) return;
    let cancelled = false;
    setPreflighting(true);
    void resolveManualDownloadTarget({
      kind: identity.kind,
      title: identity.title,
      year: identity.year,
      subtitle: request.subtitle,
      downloader_id: target.downloader_id,
    })
      .then((t) => {
        if (!cancelled) setResolvedPath(t.entry_dir ?? t.path ?? null);
      })
      .catch(() => {
        if (!cancelled) setResolvedPath(null);
      })
      .finally(() => {
        if (!cancelled) setPreflighting(false);
      });
    return () => {
      cancelled = true;
    };
  }, [request, target]);

  const label = CATEGORY_LABEL[request.category as TorrentCategory] ?? request.category;
  const headline = targetHeadline(target, resolvedPath);

  return (
    <Modal open onClose={onClose} label="确认保存位置">
      <div className="flex items-start gap-3 px-5 pb-3 pt-5">
        <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-[9px] bg-[var(--accent-soft)]">
          <FolderIcon className="size-4 text-[var(--accent)]" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-caption uppercase tracking-[0.09em] text-[var(--text-faint)]">
            保存到
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-2">
            <b className="text-body font-semibold text-white">{label}</b>
            <span className="size-[3px] rounded-full bg-[var(--text-faint)]" />
            <span className="text-sub text-[var(--text-muted)]">
              {target.kind === "smart"
                ? "智能入库"
                : (target.downloader_name ?? "默认下载器")}
            </span>
          </div>
          {/* 预检未回来时占位骨架：先让用户看到「在确认什么」，比整条延迟出现
              少一次视觉跳变 */}
          {preflighting ? (
            <div className="mt-1.5 space-y-1.5" aria-label="正在确认归宿">
              <div className="h-2.5 w-3/4 animate-pulse rounded bg-white/10" />
              <div className="h-2.5 w-2/5 animate-pulse rounded bg-white/10" />
            </div>
          ) : (
            <p className="mt-1 break-all font-mono text-caption leading-relaxed text-white">
              {headline ?? "由下载器决定"}
            </p>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-2.5 border-t border-white/[0.07] px-5 py-3 sm:flex-row sm:items-center sm:justify-between">
        <span className="text-center text-caption text-[var(--text-faint)] sm:text-left">
          上次用过 · {formatRelativeTime(target.updated_at)} ·{" "}
          <button
            type="button"
            onClick={onForget}
            className="text-[var(--accent-2)] underline underline-offset-2 hover:text-[var(--accent)]"
          >
            不再记住
          </button>
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onChange}
            className="btn-glass h-9 flex-1 px-4 text-ui font-medium sm:flex-none"
          >
            更改
          </button>
          <button
            type="button"
            disabled={busy || preflighting}
            onClick={() => {
              setBusy(true);
              onConfirm();
            }}
            className="btn-accent h-9 flex-1 rounded-full px-5 text-ui font-semibold disabled:opacity-40 sm:flex-none"
          >
            {busy ? "提交中…" : "确认下载"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
