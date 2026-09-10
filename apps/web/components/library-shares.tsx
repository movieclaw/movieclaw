"use client";

import { useCallback, useEffect, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import { ContentEmptyState } from "@/components/content-empty-state";
import { copyText } from "@/components/copy-button";
import { useConfirm, useToast } from "@/components/feedback";
import { CopyIcon, LockIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import { type ShareView, listShares, revokeShare } from "@/lib/api/shares";
import { imageUrl } from "@/lib/image-proxy";
import { LIBRARY_KIND_LABELS } from "@/lib/media-types";
import { absoluteShareUrl, expiryHint, shareCopyText } from "@/lib/share";
import { formatDateTime, formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/**
 * 媒体库管理页的「分享」标签（docs/design/media-share.md §5.4）：全部有效分享
 * 一行一条——海报、片名、有效期、是否有密码、打开次数、最近打开，「复制」
 * 「取消」。分享多了只有这里能一眼管住；过期 / 已取消的不列。
 */
export function LibraryShares({ onCountChange }: { onCountChange?: (total: number) => void }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [shares, setShares] = useState<ShareView[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);

  const reload = useCallback(() => {
    listShares()
      .then((rows) => {
        setFailed(false);
        setShares(rows);
        onCountChange?.(rows.length);
      })
      .catch(() => setFailed(true));
  }, [onCountChange]);

  useEffect(() => {
    reload();
  }, [reload]);
  useVisiblePolling(reload, 30_000);

  const copy = (share: ShareView) => {
    const link = absoluteShareUrl(share.url, window.location.origin);
    void copyText(shareCopyText(share.title, link, share.password))
      .then(() => toast.success(share.password ? "已复制链接和密码" : "已复制链接"))
      .catch(() => toast.error("浏览器拒绝访问剪贴板，请手动复制"));
  };

  const revoke = async (share: ShareView) => {
    const ok = await confirm({
      title: `取消《${share.title}》的分享？`,
      description: "链接立即失效，正在播放的访客会在一分钟内中断。",
      confirmLabel: "取消分享",
      cancelLabel: "先不",
      tone: "danger",
    });
    if (!ok) return;
    setBusyId(share.id);
    try {
      await revokeShare(share.id);
      toast.success("分享已取消");
      reload();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "取消分享失败，请稍后重试");
    } finally {
      setBusyId(null);
    }
  };

  if (shares === null) {
    return (
      <div className="flex items-center gap-2.5 px-6 py-10 text-ui text-[var(--text-muted)] max-md:px-4">
        <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
        正在读取分享…
      </div>
    );
  }

  if (shares.length === 0) {
    return (
      <ContentEmptyState
        variant="library"
        title="还没有分享任何影片"
        description="在影片详情页的 ⋯ 菜单里可以创建分享：拿到链接的人不用登录就能看这一部影片。"
      />
    );
  }

  return (
    <div className="px-6 pb-10 pt-4 max-md:px-4">
      {failed && (
        <div className="mb-3 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200">
          与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
        </div>
      )}
      <ul className="space-y-2">
        {shares.map((share) => (
          <li
            key={share.id}
            className="glass-row flex items-center gap-4 rounded-2xl px-4 py-3 max-md:flex-wrap"
          >
            <Link
              href={`/library/${share.library_id}/item/${share.media_item_id}` as Route}
              className="relative h-[66px] w-11 shrink-0 overflow-hidden rounded-lg bg-white/[0.06]"
            >
              <PosterImage src={imageUrl(share.poster_url)} alt="" className="size-full object-cover" />
            </Link>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <Link
                  href={`/library/${share.library_id}/item/${share.media_item_id}` as Route}
                  className="truncate text-ui font-semibold text-[var(--text)] hover:underline"
                >
                  {share.title}
                </Link>
                {share.password && (
                  <span
                    className="inline-flex items-center gap-1 text-caption text-[var(--text-faint)]"
                    title={`密码 ${share.password}`}
                  >
                    <LockIcon className="size-3.5" />
                    密码
                  </span>
                )}
              </div>
              <p className="mt-0.5 truncate text-caption text-[var(--text-muted)]">
                {[
                  // 合集分享没有"形态"，写它此刻有几部——那才是这条链接的内容
                  share.collection_id !== null
                    ? `合集 · ${share.item_count ?? 0} 部`
                    : share.kind
                      ? LIBRARY_KIND_LABELS[share.kind]
                      : null,
                  share.year ? String(share.year) : null,
                  `${expiryHint(share.expires_at)}（${formatDateTime(share.expires_at)}）`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              <p className="tnum mt-0.5 truncate text-caption text-[var(--text-faint)]">
                {share.view_count > 0 ? `已打开 ${share.view_count} 次` : "还没有人打开"}
                {share.last_accessed_at ? ` · 最近 ${formatRelativeTime(share.last_accessed_at)}` : ""}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-1.5 max-md:w-full max-md:justify-end">
              <button
                type="button"
                onClick={() => copy(share)}
                className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-sub text-[var(--text-muted)] transition-colors hover:bg-white/[0.07] hover:text-[var(--text)]"
              >
                <CopyIcon className="size-3.5" />
                复制
              </button>
              <button
                type="button"
                disabled={busyId === share.id}
                onClick={() => revoke(share)}
                className="rounded-lg px-2.5 py-1.5 text-sub text-[#ff9f9f] transition-colors hover:bg-[rgba(255,90,90,0.16)] disabled:opacity-50"
              >
                取消
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
