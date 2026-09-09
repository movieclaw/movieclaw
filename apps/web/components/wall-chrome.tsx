"use client";

import { useEffect, useRef } from "react";

import { HistoryIcon, XIcon } from "@/components/icons";

/**
 * 一面墙的三个通用部件：底部滚动加载哨兵、顶部向上补页哨兵、
 * 「回到上次浏览的位置」胶囊。
 *
 * 它们原先长在单库页里（components/library-detail-view.tsx），「全部收藏」页
 * 接同一套位置记忆时是从那个文件 import 出来用的——公共件寄居在某一个页面
 * 文件里，谁都得绕道那边。搬到这里，两页平级引用。
 *
 * 三者都只管「墙的边框」，不碰数据：分页窗口怎么取、位置记在哪，仍由各页
 * 自己的状态机决定（lib/library-wall-recall.ts 负责跨会话那一层）。
 */

/**
 * 海报墙底部的滚动加载哨兵。
 *
 * 提前 600px 触发下一页，正常滚动速度下新一批在滚到底之前就已到位，看不出
 * 分页。已加载数变化后重新观察：一页塞不满视口时哨兵仍在屏内，重新观察会
 * 立刻再触发一次，直到填满或到底——否则墙会停在半屏、再也不加载。
 * 「全部收藏」页的墙同样用它翻页。
 */
export function WallLoadMore({
  hasMore,
  loaded,
  start,
  total,
  onReach,
  rootMargin = "600px 0px",
}: {
  hasMore: boolean;
  loaded: number;
  /** 当前窗口在整份排序里的起点（跳字母后不为 0），决定进度文案的口径 */
  start: number;
  total: number;
  onReach: () => void;
  /**
   * 提前多远开始取下一页。海报墙 600px（约半屏）够用：格子小、图也小，到底
   * 那一下基本无感。图床浏览模式要给得更宽——一页只有二十几部作品但每张图
   * 都大，等滑到底再发请求，接上来的一屏全是还在下载的空瓦片。
   */
  rootMargin?: string;
}) {
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const target = sentinel.current;
    if (!target || !hasMore) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) onReach();
      },
      { rootMargin },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMore, loaded, onReach, rootMargin]);

  return (
    <div
      ref={sentinel}
      className="mt-8 flex h-10 items-center justify-center text-sub text-[var(--text-muted)]"
      aria-live="polite"
    >
      {hasMore ? (
        <>
          {/* 转圈：这一条原先只有一行字，静止不动时看着像卡住了 */}
          <span
            aria-hidden="true"
            className="mr-2 size-3.5 animate-spin rounded-full border-2 border-white/15 border-t-white/60 motion-reduce:animate-none"
          />
          {`加载中…（第 ${start + 1}–${start + loaded} 部 / 共 ${Math.max(total, start + loaded)}）`}
        </>
      ) : null}
    </div>
  );
}

/**
 * 海报墙顶部的向上加载哨兵（与底部的 WallLoadMore 对称）。
 *
 * 窗口起点不为 0 时才挂（跳字母 / 回到上次位置之后）。提前 600px 触发，正常
 * 上滑速度下上一页在滑到墙顶之前就已到位；跳转刚落地时墙顶正贴着视口顶边，
 * 它会立刻补一页，于是"跳过去马上往上滑"也是有内容的。
 *
 * 零高度、不显示加载态：它加在墙**上方**，一旦露出加载文案就要把整墙往下推，
 * 与前置加载自己的滚动补偿打架。补页要么已经提前到位，要么慢一点到——不会
 * 出现"卡住不动"的画面。
 * 「全部收藏」页的墙同样用它向上补页。
 */
export function WallLoadPrev({ start, onReach }: { start: number; onReach: () => void }) {
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const target = sentinel.current;
    if (!target || start <= 0) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) onReach();
      },
      { rootMargin: "600px 0px" },
    );
    observer.observe(target);
    return () => observer.disconnect();
    // start 每补一页就变一次：重新观察，够到墙顶就接着往上补
  }, [onReach, start]);

  return (
    // 外层零高度：哨兵在墙**上方**，占哪怕一像素都要把整面墙推下去。里面那块
    // 绝对定位、给足 1px——IntersectionObserver 对零面积目标的判定各家浏览器
    // 并不一致，有一点真实面积才稳
    <div aria-hidden="true" className="relative h-0">
      <div ref={sentinel} className="absolute inset-x-0 top-0 h-px" />
    </div>
  );
}

/**
 * 「回到上次浏览的位置」胶囊：贴在视口底部中央，与 Toast 同一套浮入语言。
 *
 * 为什么是胶囊而不是直接跳回去：跳转会换掉整个分页窗口（上方的内容要往上滑
 * 才补回来），这一步得由用户决定——有人回来就是想从头看看新入库了什么。所以只问一句，
 * 不理它往下滑一屏它就让位（见 lib/use-wall-recall.ts），× 是给想立刻清屏的人。
 *
 * z-[45]：压过墙与索引条，但低于弹层（50/60）、灯箱（70）与 Toast（95）——
 * 它只是个建议，任何真正的操作都该盖住它。
 * 「全部收藏」页弹的是同一枚胶囊。
 */
export function WallRecallPill({ onJump, onDismiss }: { onJump: () => void; onDismiss: () => void }) {
  return (
    <div
      aria-live="polite"
      className="pointer-events-none fixed inset-x-0 bottom-[calc(var(--safe-bottom)+18px)] z-[45] flex justify-center px-4"
    >
      <div className="toast-item pointer-events-auto flex items-center gap-1 rounded-full border border-white/[0.12] bg-[rgba(16,18,26,0.92)] py-1 pl-1 pr-1.5 shadow-[0_18px_50px_rgba(0,0,0,0.55)] backdrop-blur-2xl">
        <button
          type="button"
          onClick={onJump}
          className="flex items-center gap-2 rounded-full px-3 py-1.5 text-sub font-medium text-white/90 transition hover:bg-white/[0.1] active:scale-[0.98]"
        >
          <HistoryIcon className="size-4 shrink-0 text-[var(--accent)]" />
          回到上次浏览的位置
        </button>
        <button
          type="button"
          onClick={onDismiss}
          aria-label="不用了"
          className="flex size-7 shrink-0 items-center justify-center rounded-full text-white/45 transition hover:bg-white/[0.1] hover:text-white/80"
        >
          <XIcon className="size-3.5" />
        </button>
      </div>
    </div>
  );
}
