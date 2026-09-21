"use client";

import { ChevronLeftIcon } from "@/components/icons";
import { PAGE_NAV_BUTTON_SIZE_CLASS } from "@/components/page-nav";

/**
 * Netflix 桌面详情页的返回键：裸的白色 chevron，fixed 悬浮在顶栏下方左上角。
 * 这就是 Netflix 自己的返回语言——不加底、不加描边，hover 才浮一层浅白；
 * 与银玻璃的圆角玻璃键（PageNav）刻意不同貌。飘在亮图上时靠图标投影保底，
 * 横向对齐 4vw 栅格（与发现页悬浮工具栏、内容行同一条左基线）。
 *
 * Netflix 桌面返回语言一套：全出血(isHome)详情页用 NetflixBackButton；带
 * PageNav 工具条的页面用 PageNav 内返回键；两者同图标 / 同尺寸档 / 同 4vw
 * 基线 / 同 useBackNavigation 行为。isHome 全出血页（外壳不加 nf-nav-offset
 * 让位）不能渲染 PageNav——sticky z-30 会被 fixed z-40 的顶栏整个盖住，
 * 返回键看得见点不到；这是防遮挡硬约束（page-nav.tsx 渲染入口也按同源判定
 * 短路，见 lib/page-chrome.tsx 的 isHomeRoute），银玻璃与移动端仍用 PageNav。
 */
export function NetflixBackButton({
  onBack,
  onPhoto = false,
}: {
  onBack: () => void;
  /** 落在亮色头像卡等浅色画面上时传 true：挂 page-nav-btn 主题钩子获得既定的
   *  实底深灰圆底（globals.css netflix 块：rgb(0 0 0/0.45) + --line 描边 +
   *  关 blur），裸白 chevron 压浅色图对比度不够。本组件仅 Netflix 桌面渲染
   *  （isNfDesktop 分支），钩子又无银玻璃基样式，银玻璃不受影响。 */
  onPhoto?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onBack}
      aria-label="返回上一页"
      title="返回上一页"
      // calc 任意值的 +/- 两侧必须空白（下划线转义），无空格是无效 CSS。
      // 尺寸档与 PageNav 返回键同源（PAGE_NAV_BUTTON_SIZE_CLASS：size-9 /
      // pointer-coarse:size-11）：两颗键同在 4vw 基线上，键径不同就有 2px 级
      // 的图标中心错位（此前裸键 40px vs 工具条键 36px 差 2px，本档修复）。
      // 图标同取键径一半的 18px，与 PageNav 同一比例
      className={`fixed left-[4vw] top-[calc(var(--nf-nav-h)_+_12px)] z-30 flex ${PAGE_NAV_BUTTON_SIZE_CLASS} items-center justify-center rounded-full border text-white/85 transition hover:text-white ${
        onPhoto
          ? // 亮底场景：page-nav-btn 钩子（netflix 下 bg rgb(0 0 0/0.45)，
            // border-color --line，关 blur）；字面量 bg 作无钩子主题的兜底
            "page-nav-btn border-[var(--line)] bg-black/45 hover:bg-black/60"
          : "hover:bg-white/10"
      }`}
    >
      <ChevronLeftIcon className="size-[18px] drop-shadow-[0_1px_3px_rgba(0,0,0,0.8)]" />
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
