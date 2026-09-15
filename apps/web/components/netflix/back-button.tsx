"use client";

import { ChevronLeftIcon } from "@/components/icons";

/**
 * Netflix 桌面详情页的返回键：裸的白色 chevron，fixed 悬浮在顶栏下方左上角。
 * 这就是 Netflix 自己的返回语言——不加底、不加描边，hover 才浮一层浅白；
 * 与银玻璃的圆角玻璃键（PageNav）刻意不同貌。飘在亮图上时靠图标投影保底，
 * 横向对齐 4vw 栅格（与发现页悬浮工具栏、内容行同一条左基线）。
 *
 * 存在的理由：Netflix 桌面主题下详情页是 isHome 全出血页（外壳不加
 * nf-nav-offset 让位），PageNav（sticky z-30）会被 fixed z-40 的顶栏整个
 * 盖住——发现详情页（media-detail-view）与媒体库条目详情页
 * （library-item-detail-view）在 Netflix 桌面都改用这颗键（见两处的
 * hidePageNav 分支），银玻璃与移动端仍用 PageNav。
 */
export function NetflixBackButton({ onBack }: { onBack: () => void }) {
  return (
    <button
      type="button"
      onClick={onBack}
      aria-label="返回上一页"
      title="返回上一页"
      // calc 任意值的 +/- 两侧必须空白（下划线转义），无空格是无效 CSS
      className="fixed left-[4vw] top-[calc(var(--nf-nav-h)_+_12px)] z-30 flex size-10 items-center justify-center rounded-full text-white/85 transition hover:bg-white/10 hover:text-white"
    >
      <ChevronLeftIcon className="size-6 drop-shadow-[0_1px_3px_rgba(0,0,0,0.8)]" />
    </button>
  );
}

/**
 * 与 NetflixBackButton 成对的页面操作簇（如条目详情的 ⋯ 菜单）：同一条
 * 4vw 基线浮在顶栏下方右上角。PageNav 被顶栏盖住时，页面级操作没有别处
 * 安身——回到这颗浮键上，操作入口的位置与返回键对称。
 */
export function NetflixPageActions({ children }: { children: React.ReactNode }) {
  return (
    <div className="fixed right-[4vw] top-[calc(var(--nf-nav-h)_+_12px)] z-30 flex items-center gap-2">
      {children}
    </div>
  );
}
