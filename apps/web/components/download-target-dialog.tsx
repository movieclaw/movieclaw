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
 */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { FolderIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import {
  listDownloaders,
  submitTorrentDownload,
  type ConfiguredDownloader,
  type DownloadSubmitResult,
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
}

/* —— 「记住选择」：高频批量下载不必每次都过弹窗 —— */

/** 记住的目标按内容类型分桶：电影/剧集常进不同的库或目录，other = 身份未识别 */
type RememberKind = "movie" | "tv" | "other";

export interface RememberedTarget {
  /** smart = 自动入库（需要唯一身份）；dir = 固定目录；default = 下载器默认目录 */
  kind: "smart" | "dir" | "default";
  savePath: string | null;
  /** 指定的下载器；null = 默认下载器 */
  downloaderId: number | null;
}

const REMEMBER_KEY = "movieclaw.download-target";

function rememberKindOf(request: DownloadTargetRequest): RememberKind {
  return request.identity?.kind ?? "other";
}

export function readRememberedTarget(request: DownloadTargetRequest): RememberedTarget | null {
  try {
    const store = JSON.parse(window.localStorage.getItem(REMEMBER_KEY) ?? "{}");
    const target = store[rememberKindOf(request)] as RememberedTarget | undefined;
    if (!target || typeof target !== "object" || !target.kind) return null;
    // 记住的是"自动入库"但这条种子没解析出身份：套不上，回落弹窗
    if (target.kind === "smart" && !request.identity) return null;
    return target;
  } catch {
    return null;
  }
}

function writeRememberedTarget(kind: RememberKind, target: RememberedTarget | null): void {
  try {
    const store = JSON.parse(window.localStorage.getItem(REMEMBER_KEY) ?? "{}");
    if (target === null) delete store[kind];
    else store[kind] = target;
    window.localStorage.setItem(REMEMBER_KEY, JSON.stringify(store));
  } catch {
    // localStorage 不可用（隐私模式等）：静默降级为每次都弹窗
  }
}

/**
 * 按记住的目标直接提交（下载按钮的快速通道），不经弹窗。
 * smart 目标需要重新确认 TMDB 身份和投递预检；未收敛/配置有警示时返回 null
 * 让调用方回落弹窗，不把不确定资源静默放进默认库。
 */
export async function submitRememberedTarget(
  request: DownloadTargetRequest,
  target: RememberedTarget,
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
      downloader_id: target.downloaderId,
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
    ...libraryPart,
    ...(target.kind === "dir" ? { save_path: target.savePath } : {}),
    ...(target.downloaderId != null ? { downloader_id: target.downloaderId } : {}),
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
  onClose,
  onSubmitted,
}: {
  /** null = 关闭 */
  request: DownloadTargetRequest | null;
  onClose: () => void;
  onSubmitted: (result: DownloadSubmitResult) => void;
}) {
  if (!request) return null;
  // 以 request 为 key 强制内容组件重新挂载：每次打开都从全新状态开始，
  // 避免默认选中 effect 读到上一次的旧数据抢先选中错误项。
  return (
    <DialogContent
      key={`${request.site_id}:${request.download_url}`}
      request={request}
      onClose={onClose}
      onSubmitted={onSubmitted}
    />
  );
}

function DialogContent({
  request,
  onClose,
  onSubmitted,
}: {
  request: DownloadTargetRequest;
  onClose: () => void;
  onSubmitted: (result: DownloadSubmitResult) => void;
}) {
  const [rememberedTarget] = useState<RememberedTarget | null>(() =>
    readRememberedTarget(request),
  );
  const revealOtherInitially =
    request.identity === null ||
    (rememberedTarget !== null && rememberedTarget.kind !== "smart");
  // 可用（启用 + 验证通过）的全部下载器：≥2 台时出现下载器选择
  const [downloaders, setDownloaders] = useState<ConfiguredDownloader[]>([]);
  const [downloaderId, setDownloaderId] = useState<number | null>(
    rememberedTarget?.kind === "smart" ? rememberedTarget.downloaderId : null,
  );
  const [manualTarget, setManualTarget] = useState<ManualDownloadTarget | null>(null);
  const [selectedCandidateId, setSelectedCandidateId] = useState<number | null>(null);
  const [showOtherTargets, setShowOtherTargets] = useState(revealOtherInitially);
  const [downloadersLoaded, setDownloadersLoaded] = useState(false);
  const [loadingDownloaders, setLoadingDownloaders] = useState(false);
  const [loadingTarget, setLoadingTarget] = useState(request.identity !== null);
  const [selected, setSelected] = useState<string | null>(null);
  // 记住选择：默认勾选态跟随已有记忆（勾着提交=保存/刷新，取消勾选提交=清除）
  const [remember, setRemember] = useState<boolean>(rememberedTarget !== null);
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
      downloader_id: rememberedTarget?.kind === "smart" ? rememberedTarget.downloaderId : null,
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
          rememberedTarget?.downloaderId != null
            ? usable.find((d) => d.id === rememberedTarget.downloaderId)
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
            ? `${manualTarget.route_reason ?? ""}；投递到监听导入目录 ${manualTarget.path}，完成后整理到 ${manualTarget.staging_path}（外部流转回库根后入账）`
            : `${manualTarget.route_reason ?? ""}；投递到监听导入目录 ${manualTarget.path}，完成后自动整理入库`
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
    const remembered = readRememberedTarget(request);
    if (remembered) {
      const match = options.find((o) =>
        remembered.kind === "dir"
          ? o.kind === "dir" && o.savePath === remembered.savePath
          : o.kind === remembered.kind,
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
  }, [downloadersLoaded, loadingTarget, options, selected, request, showOtherTargets]);

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
        // 记住/清除选择（按内容类型分桶）：勾着=保存本次目标，取消勾选=清除
        writeRememberedTarget(
          rememberKindOf(request),
          remember
            ? { kind: option.kind, savePath: option.savePath, downloaderId: pickedDownloaderId }
            : null,
        );
        onSubmitted(result);
        onClose();
      })
      .catch((e) => setError(e instanceof Error ? e.message : "提交失败，请重试"))
      .finally(() => setBusy(false));
  };

  return (
    <Modal open onClose={onClose} label="选择保存位置">
      <div className="space-y-4 p-6">
          <h2 className="text-title font-bold text-white">选择保存位置</h2>

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
                  已识别资源，但当前不能自动入库：{manualTarget.warning ?? "请检查媒体库和监听导入配置。"}
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

          {/* 记住选择：批量下载的快速通道——之后点「下载」直接提交，
              toast 上有「更改」可随时回到本弹窗 */}
          <label className="flex cursor-pointer items-center gap-2 text-sub text-[var(--text-muted)]">
            <input
              type="checkbox"
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
              className="size-3.5 accent-[var(--accent-2)]"
            />
            记住本次选择，下次点「下载」直接提交（按电影/剧集分别记忆）
          </label>

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

          <div className="flex justify-end gap-3 pt-1">
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
      </div>
    </Modal>
  );
}
