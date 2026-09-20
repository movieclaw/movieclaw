"use client";

import type { ReactNode } from "react";

import { BellIcon, BookmarkIcon, FilmIcon, FolderIcon, TvIcon } from "@/components/icons";
import { useTheme } from "@/lib/ui-prefs";

interface ContentEmptyStateProps {
  variant: "library" | "subscription";
  title: string;
  description: ReactNode;
  action?: ReactNode;
}

/**
 * 内容型页面的空状态展台。
 *
 * 空数据不是故障，不用警告式面板；这里用与全站一致的银玻璃表面承接说明，
 * 再以纯 CSS 层次和项目现有图标分别表达「内容归档」与「持续追踪」。
 */
export function ContentEmptyState({
  variant,
  title,
  description,
  action,
}: ContentEmptyStateProps) {
  // 展台自己的左右外边距就是页面级留白（调用方渲染在滚动容器一层）：
  // Netflix 主题对齐全站 4vw 左基线，银玻璃维持 mx-6
  const insetMx = useTheme().id === "netflix" ? "mx-[4vw]" : "mx-6 max-md:mx-4";
  return (
    <section
      aria-label={title}
      className={`css-glass relative mt-7 flex min-h-[360px] overflow-hidden !rounded-3xl px-6 py-10 ${insetMx} max-md:mt-5 max-md:min-h-[340px] max-md:px-5`}
    >
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-[radial-gradient(circle_at_50%_15%,rgba(205,214,230,0.11),transparent_42%),linear-gradient(180deg,rgba(255,255,255,0.025),transparent_55%)]"
      />
      <div
        aria-hidden="true"
        className="absolute inset-x-[12%] top-0 h-px bg-gradient-to-r from-transparent via-white/25 to-transparent"
      />

      <div className="relative m-auto flex max-w-[520px] flex-col items-center text-center">
        <EmptyArtwork variant={variant} />
        <h3 className="mt-2 text-title font-semibold tracking-[-0.01em] text-[var(--text)]">
          {title}
        </h3>
        <div className="mt-2 max-w-[460px] text-ui leading-7 text-[var(--text-muted)]">
          {description}
        </div>
        {action && <div className="mt-5">{action}</div>}
      </div>
    </section>
  );
}

/** 装饰图完全不承载信息，读屏只朗读空状态标题、说明与操作。 */
function EmptyArtwork({ variant }: { variant: ContentEmptyStateProps["variant"] }) {
  return (
    <div aria-hidden="true" className="relative h-36 w-60">
      <div className="absolute left-1/2 top-1/2 size-36 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/[0.06]" />
      <div className="absolute left-1/2 top-1/2 size-24 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/[0.09] bg-white/[0.025] shadow-[0_0_55px_rgba(159,176,201,0.12)]" />

      {variant === "library" ? (
        <>
          <span className="absolute left-7 top-7 grid h-[92px] w-16 -rotate-6 place-items-center rounded-xl border border-white/[0.09] bg-white/[0.04] text-white/20 shadow-xl">
            <FilmIcon className="size-6" />
          </span>
          <span className="absolute right-7 top-7 grid h-[92px] w-16 rotate-6 place-items-center rounded-xl border border-white/[0.09] bg-white/[0.04] text-white/20 shadow-xl">
            <TvIcon className="size-6" />
          </span>
          {/* solid-card：图标座挂卡片材质钩子（Netflix 下换 #181818 并随钩子
              box-shadow:none 抹掉 inset 白高光；冷蓝灰 #202530 是硬编码，
              不动它以免银玻璃变化），圆版/方版形状保留 */}
          <span className="solid-card absolute left-1/2 top-1/2 z-10 grid size-[76px] -translate-x-1/2 -translate-y-1/2 place-items-center rounded-2xl border border-white/[0.16] bg-[#202530]/90 text-white/75 shadow-[0_18px_45px_rgba(0,0,0,0.45),inset_0_1px_0_rgba(255,255,255,0.12)] backdrop-blur-xl">
            <FolderIcon className="size-9" />
          </span>
          <span className="absolute inset-x-5 bottom-3 h-px bg-gradient-to-r from-transparent via-white/20 to-transparent" />
        </>
      ) : (
        <>
          <span className="absolute left-5 top-9 grid size-11 -rotate-6 place-items-center rounded-xl border border-white/[0.09] bg-white/[0.04] text-white/35 shadow-lg">
            <FilmIcon className="size-5" />
          </span>
          <span className="absolute right-5 top-9 grid size-11 rotate-6 place-items-center rounded-xl border border-white/[0.09] bg-white/[0.04] text-white/35 shadow-lg">
            <TvIcon className="size-5" />
          </span>
          <span className="solid-card absolute left-1/2 top-1/2 z-10 grid size-[76px] -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border border-white/[0.16] bg-[#202530]/90 text-white/75 shadow-[0_18px_45px_rgba(0,0,0,0.45),inset_0_1px_0_rgba(255,255,255,0.12)] backdrop-blur-xl">
            <BookmarkIcon className="size-8" />
          </span>
          <span className="absolute bottom-4 right-[72px] z-20 grid size-8 place-items-center rounded-full border border-white/15 bg-[#303743] text-white/70 shadow-lg">
            <BellIcon className="size-4" />
          </span>
          <span className="absolute bottom-[22px] right-[68px] z-30 size-2 animate-pulse rounded-full bg-[var(--ok)] shadow-[0_0_10px_var(--ok)] motion-reduce:animate-none" />
        </>
      )}
    </div>
  );
}
