"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import type { Route } from "next";
import { usePathname } from "next/navigation";

import { ChevronLeftIcon, MenuIcon } from "@/components/icons";
import { SearchCommand } from "@/components/search-command";
import { useBackNavigation } from "@/lib/back-navigation";
import { isHomeRoute, usePageChrome } from "@/lib/page-chrome";
import { useTheme } from "@/lib/ui-prefs";
import { useResolvedTheme } from "@/themes/registry";
import { useIsMobile } from "@/lib/use-media-query";

/** 没有可用站内历史时的结构父级；只作兜底，不覆盖真实来路。 */
export interface PageNavFallback {
  label: string;
  href: Route;
}

/**
 * 标题淡入的滚动区间（px）：滚过 START 才开始显形，到 END 完全实体。
 * START 取得比顶栏自身高度（52px）略低一点，是为了避开「页面大标题还没滑走、
 * 吸顶小标题已经浮出来」的重影——大标题紧贴顶栏下沿的页面（如媒体库详情）
 * 正是这个位置关系；END 再往后拉一段，让淡入过程完整落在大标题走完之后。
 */
const REVEAL_START = 34;
const REVEAL_END = 82;

/**
 * 顶栏控件的统一形状：圆形深色玻璃键。返回键与页面操作（⋯）并排在同一行，
 * 必须是同一副长相，否则一边圆一边胶囊会像两套控件凑在一起。
 * 页面侧的操作按钮（见 library-detail-view 的 ⋯ 菜单）复用这个类名。
 *
 * 尺寸分两档：触屏 44px（iOS HIG 的最小可点目标，导航栏图标键的原生比例，
 * 36px 的键在触屏上会显得局促难点）；鼠标 36px（精度高，44px 反而笨重）。
 * 分档按指针能力（pointer-coarse）而不是视口宽度——iPad 竖屏 / 手机横屏
 * 都会越过 md 断点，按视口分档会让触屏设备吃到鼠标档（与播放器控件、
 * .nf-icon-btn 的同一结论，2026-09 移动端审查统一）。
 * 图标同比例缩放（约为键径的一半），改动时两档要一起看。
 */
/** 尺寸档单拎出来导出：NetflixBackButton（裸键，不吃玻璃材质）与本类共享
 *  同一份尺寸——两颗键同在 4vw 左基线上，键径不同就会有 2px 级的中心错位。 */
export const PAGE_NAV_BUTTON_SIZE_CLASS = "size-9 pointer-coarse:size-11";
export const PAGE_NAV_BUTTON_CLASS =
  // page-nav-btn：主题钩子——Netflix 主题在 globals.css 里压成实底深灰圆钮
  // 并关掉毛玻璃（玻璃圆钮是银玻璃控件语言，见 docs/design/web-themes.md §5.5）
  `page-nav-btn grid ${PAGE_NAV_BUTTON_SIZE_CLASS} shrink-0 place-items-center rounded-full border border-white/[0.09] bg-black/30 text-white/85 backdrop-blur-md transition hover:bg-black/50 hover:text-white active:scale-[0.94]`;

/**
 * 找到本组件所在的滚动容器（全站页面都是「外壳固定 + 内层 overflow-y-auto」，
 * 不是整窗滚动），找不到时退回整窗滚动，保证组件放到任何页面都能工作。
 */
function scrollParentOf(node: HTMLElement | null): HTMLElement | null {
  let el = node?.parentElement ?? null;
  while (el) {
    const overflowY = getComputedStyle(el).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return el;
    el = el.parentElement;
  }
  return null;
}

/**
 * 全站统一的子页面顶栏（Apple Music 式）：左侧常驻一颗圆形返回键，
 * 页面向下滚动时，右侧渐显一条吸顶的当前页标题。
 *
 * 为什么从「面包屑」改成这个：
 * - 子页面的层级链路已经由侧栏高亮 + 来路推导表达清楚了，顶部再排一条
 *   「A › B › C」是重复信息，且占掉整行宽度；
 * - 用户在子页面真正高频的动作只有一个——回到刚才的位置。把它做成一颗随手可点的
 *   圆键，比「点面包屑倒数第二段」的命中区更大、动线更短；
 * - 页面标题在首屏本来就以大标题呈现（Hero / 页头），滚走之后才需要一条
 *   小标题补位「我在看什么」——所以标题跟着滚动淡入，而不是一直占位。
 *
 * 回跳目标：优先消费浏览器里真实存在的同源上一页，保留用户进入前的列表、筛选
 * 与滚动上下文；分享链接或新标签直达、没有可用站内历史时，才 replace 到调用方
 * 给出的结构父级。这里的箭头因此始终是「后退」，不再与「向上一级」混用。
 *
 * 淡入实现：滚动回调只往根节点写一个 CSS 变量 --nav-reveal（0→1），
 * 蒙版与标题各自用它驱动 opacity——不触发 React 重渲染，滚动过程零抖动。
 * 注意这些属性一律不加 transition：变量驱动的属性加过渡会被 Chromium 卡住旧值。
 *
 * 位置约定：必须作为滚动容器的直接子节点（sticky 的定位参照就是它），
 * 组件自带 page-inset 横向留白（主题变量档）；页面正文同样挂 page-inset 时，
 * 调用处给蒙版补 page-inset-bleed 反向抵消即可铺满整宽——两者取自同一变量，
 * 天然成对，不存在窄屏多探出 8px 的错位。
 */
export function PageNav({
  title,
  fallback,
  actions,
  toolbar,
  className = "",
}: {
  title: string;
  fallback: PageNavFallback;
  /** 页面级操作（如 ⋯ 菜单）：排在同一行的最右端，随顶栏一起吸顶常驻 */
  actions?: React.ReactNode;
  /**
   * 页面级视角切换（如库页的「作品 / 合集」）：排在搜索键左侧，与发现页把
   * TMDB / 豆瓣 切换挂进全局顶栏同一个位置。给了它就不再渲染吸顶标题——
   * 390px 宽的一行放不下 ☰ + 返回 + 切换 + 搜索 + ⋯ 之外再加一个标题，
   * 硬塞只会把整行挤出屏幕；页面正文里本来就有同名大标题。
   */
  toolbar?: React.ReactNode;
  className?: string;
}) {
  const back = useBackNavigation(fallback.href);
  const rootRef = useRef<HTMLDivElement>(null);
  const chrome = usePageChrome();
  // ☰ 键只在银玻璃渲染（开抽屉）；Netflix 的导航在底部页签，详见下方控件组注释
  const isNetflix = useTheme().structural;
  const isMobile = useIsMobile();
  const pathname = usePathname();
  const { slots } = useResolvedTheme();

  // 向外壳登记「本页自带顶栏」：移动端据此撤掉全局顶栏，两条顶栏不再摞在一起
  // （见 lib/page-chrome.tsx）。PageNav 只在子页面渲染，挂载即认领顶栏。
  // 必须用 useLayoutEffect：登记要赶在浏览器绘制之前生效，用 useEffect（绘制后）
  // 会让外壳的全局顶栏（☰ + 字标）先画出一帧再被撤掉——进入详情页时顶部 logo
  // 会肉眼可见地闪一下。本组件只在 AuthGate 之后的纯客户端子树渲染，无 SSR 警告问题。
  const registerPageNav = chrome?.registerPageNav;
  useLayoutEffect(() => {
    if (!registerPageNav) return;
    return registerPageNav();
  }, [registerPageNav]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const scroller = scrollParentOf(root);
    const target: HTMLElement | Window = scroller ?? window;
    const sync = () => {
      const top = scroller ? scroller.scrollTop : window.scrollY;
      const reveal = (top - REVEAL_START) / (REVEAL_END - REVEAL_START);
      root.style.setProperty("--nav-reveal", Math.min(1, Math.max(0, reveal)).toFixed(3));
    };
    sync();
    target.addEventListener("scroll", sync, { passive: true });
    return () => target.removeEventListener("scroll", sync);
  }, []);

  // —— 防遮挡硬约束（渲染入口短路）——
  // Netflix 桌面主题下，全出血（isHome）路由的主区不为顶栏让位，本组件
  // （sticky z-30）会被 fixed z-40 的 Netflix 顶栏整个盖住——返回键看得见
  // 却永远点不到。这些页面一律由页面自身渲染 NetflixBackButton（同 4vw 基线、
  // 同尺寸档，见 netflix/back-button.tsx），本组件直接不渲染，把「isHome 页
  // 不得渲染 PageNav」从各页的人工约定变成代码保证；判定与外壳 isHome 同源
  // （lib/page-chrome.tsx 的 isHomeRoute），新增全出血路由自动继承。
  // 只限桌面：Netflix 移动端没有 fixed 顶栏（全局顶栏已由本组件登记撤掉），
  // 详情页的 PageNav 照常保留；银玻璃两端的顶栏都不是 fixed，同样不受限。
  // 现状零行为变化：isHome 页面在 Netflix 桌面本就各自分支改用了
  // NetflixBackButton，没有任何页面在会触发本短路的状态下向本组件要过渲染。
  if (isNetflix && !isMobile && isHomeRoute(pathname, slots.libraryHero != null)) {
    return null;
  }

  const backClass = PAGE_NAV_BUTTON_CLASS;
  const backLabel = `返回上一页；无历史时返回${fallback.label}`;

  return (
    /* pt-[var(--safe-top)]：窄屏上主区不再为安全区让位（见 globals.css 的
       .app-shell[data-topbar="false"] > main），改由顶栏自己吃掉这一段——
       雾层因此从屏幕物理顶边起铺、从状态栏底下穿过，而图标那一行仍落在
       安全区以下。这正是原生 App 顶栏的层次，也与全局顶栏 .mobile-topbar 一致。 */
    <div
      ref={rootRef}
      className={`sticky top-0 z-30 ${
        // 左右留白走主题变量档（--page-inset：银玻璃 px-6、Netflix 4vw）。
        // 调用方要吸顶蒙版铺满整宽时，用 page-inset-bleed 反向抵消，天然成对
        "page-inset"
      } max-md:pt-[var(--safe-top)] ${className}`}
    >
      {/* 吸顶蒙版：不是一条「header 色块」，而是一层向下渐隐的雾——顶边最浓、
          到底部完全化开，没有分隔线，因此看不出边界，只感觉标题那一块变干净了。
          模糊同样用 mask 做渐隐（只模糊上半段），否则会在雾的下沿出现一道
          清晰度断层，反而比画一条边框还明显。蒙版向下多探 20px 留出化开的余量。
          页面顶部（reveal=0）时整层透明——首屏只剩一颗浮在画面上的返回键。 */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 -bottom-5 top-0 backdrop-blur-md"
        style={{
          opacity: "var(--nav-reveal, 0)",
          /* 雾层色相走 --page-fog（:root 银玻璃 / netflix 覆盖组纯黑）：
             内联 style 无法被 CSS 选择器压过，主题换肤必须经变量 */
          background: "var(--page-fog)",
          maskImage: "linear-gradient(180deg, #000 0%, #000 38%, transparent 92%)",
          WebkitMaskImage: "linear-gradient(180deg, #000 0%, #000 38%, transparent 92%)",
        }}
      />
      <div className="relative flex h-[52px] items-center gap-3">
        {/* 左侧控件组：银玻璃移动端补一颗 ☰ 排在返回键左边（本页顶栏顶掉了
            外壳那条全局顶栏，抽屉入口不在这儿补回来，详情页就只能先返回才能
            换区）。Netflix 主题不放 ☰：导航全在底部页签，「我的」是 /my 路由，
            这颗键只会在 390px 宽的一行里白占一格。组内 gap-2 与右侧控件组
            一致，组与标题之间才是外层的 gap-3。 */}
        <div className="flex shrink-0 items-center gap-2">
          {isMobile && chrome && !isNetflix && (
            <button
              type="button"
              onClick={chrome.openDrawer}
              aria-label="打开侧边栏"
              className={backClass}
            >
              <MenuIcon className="size-[18px] max-md:size-[22px]" />
            </button>
          )}
          <button
            type="button"
            onClick={back}
            aria-label={backLabel}
            title={backLabel}
            className={backClass}
          >
            <ChevronLeftIcon className="size-[18px] max-md:size-[22px]" />
          </button>
        </div>
        {/* 吸顶标题：内容区下方本来就有同名大标题，这里只是滚动后的补位视觉，
            对读屏隐藏，避免同一个标题被念两遍。 */}
        {!toolbar && (
          <span
            aria-hidden="true"
            className="min-w-0 truncate text-body-lg font-semibold tracking-[-0.01em] text-white/90"
            style={{
              opacity: "var(--nav-reveal, 0)",
              transform: "translateY(calc((1 - var(--nav-reveal, 0)) * 5px))",
            }}
          >
            {title}
          </span>
        )}
        {/* 页面操作靠右：与返回键同一行，页面首屏不再单独占一条工具栏，
            滚动后又随顶栏留在原地——操作入口的位置从头到尾不动。
            移动端还要在这里补一颗搜索——本页顶栏顶掉了外壳那条全局顶栏，
            搜索是其中唯一无处安放的入口（导航在抽屉里、字标只是回首页），
            排在页面操作左侧。必须条件渲染而不是 CSS 隐藏：SearchCommand 自带
            全局 ⌘K 监听，桌面上再挂一份会让一次快捷键把面板开了又关。 */}
        {(toolbar || actions || (isMobile && chrome)) && (
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {toolbar}
            {isMobile && chrome && (
              <SearchCommand onSearch={chrome.onSearch} triggerClassName={PAGE_NAV_BUTTON_CLASS} />
            )}
            {actions}
          </div>
        )}
      </div>
    </div>
  );
}
