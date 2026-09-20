"use client";

import { createContext, useContext, type ReactNode } from "react";

import type { SearchSubmitOptions } from "@/components/search-command";
import type { SearchScope } from "@/lib/categories";

/** 提交搜索的回调，签名与 AppShell 的 handleSearch 一致。 */
export type PageSearchHandler = (
  keyword: string,
  scope: SearchScope,
  options?: SearchSubmitOptions,
) => void;

/**
 * 页面顶栏的归属协商 —— 解决移动端「两条顶栏叠在一起」的问题。
 *
 * 移动端外壳自带一条全局顶栏（汉堡 + 字标 + 搜索），而详情类页面又各自带一条
 * PageNav（返回 + 标题 + 页面操作）。两者都吸在顶部，窄屏上就摞成了两层，
 * 白白吃掉两倍高度，右上角还并排出现两颗互不相干的图标。
 *
 * 收口办法：让页面顶栏「认领」这一行——PageNav 挂载即向外壳登记，外壳在移动端
 * 据此撤掉自己那条，并把全局顶栏上那两个无处安放的入口（抽屉、搜索）交给
 * PageNav 一并呈现：☰ 与返回并排在左，搜索与页面操作并排在右。于是无论哪种
 * 页面，窄屏上永远只有一条顶栏，而导航能力一个不少。
 *
 * 为什么用「组件自登记」而不是在外壳里列一张路由表：全站的移动端让位逻辑
 * （见 globals.css 的 .app-shell > main）一贯是「新增路由自动继承、无需登记」，
 * 路由表会随着页面增加而漏。谁渲染了 PageNav，谁就自动接管顶栏。
 *
 * 桌面端不受影响：外壳的全局顶栏本来就只在 < 768px 渲染，PageNav 那颗搜索键
 * 也只在移动端渲染（SearchCommand 自带全局 ⌘K 监听，多挂一份会让一次快捷键
 * 把面板开了又关，因此必须条件渲染而不是 CSS 隐藏）。
 */
export interface PageChromeValue {
  /**
   * 登记「本页自带顶栏」。在 effect 里调用，返回注销函数。
   * 用计数而非布尔：路由切换时新旧页面会短暂共存，计数才不会被先卸载的那个清零。
   */
  registerPageNav: () => () => void;
  /** 全局搜索入口：移动端由 PageNav 代为呈现（外壳那条已经撤掉） */
  onSearch: PageSearchHandler;
  /** 唤起移动端抽屉侧栏：同样由 PageNav 代为呈现，详情页因此不必先返回才能换区 */
  openDrawer: () => void;
  /**
   * 收起移动端抽屉侧栏。抽屉里的动作若不切路由（如「切换账号」弹窗），抽屉不会
   * 自动收起，而抽屉层级（z-60）压在普通弹窗（z-50）之上，弹窗会被整个盖住，
   * 这类动作要先主动收抽屉。桌面版式下调用无副作用。
   */
  closeDrawer: () => void;
  /**
   * 把页面级控件挂进移动端全局顶栏那一行，返回撤销函数。
   *
   * 给的是**没有 PageNav 的顶层页面**（发现页那种侧栏一级入口）：它们不该有返回键，
   * 却又有一两个页面级控件（如发现页的 TMDB / 豆瓣 数据源切换）。若让这些控件
   * 自己吸一条顶栏，窄屏上同样会出现两排 header——而全局顶栏「字标与搜索之间」
   * 本来就空着一大段，正好安置。
   *
   * 在 effect 里调用并返回它的清理函数；节点要用稳定依赖构造，别在渲染期直接调。
   */
  setTopBarActions: (node: ReactNode) => () => void;
  /**
   * 把页面标题挂进移动端全局顶栏、顶替品牌字标的位置，返回撤销函数。
   *
   * 给沉浸类顶层页面（如 Agent 会话）：窄屏上字标传达不了任何新信息（用户
   * 就在应用里），这一格让给「我在看哪个会话」远比品牌曝光有用。字标只在
   * 没有页面认领标题时兜底显示。同样在 effect 里调用。
   */
  setTopBarTitle: (title: string) => () => void;
}

const PageChromeContext = createContext<PageChromeValue | null>(null);

export const PageChromeProvider = PageChromeContext.Provider;

/**
 * 「全出血（isHome / 氛围页）路由」判定：这些路由的主区不为顶栏让位
 * （外壳按它决定加不加 .nf-nav-offset、渲染不渲染全局蒙版），大图从顶栏
 * 底下穿过。抽出为纯函数供外壳与 page-nav 共用，两处判定必须同源。
 *
 * 为什么 PageNav 也要吃这份判定（防遮挡硬约束）：Netflix 桌面的全出血页若
 * 渲染 PageNav（sticky z-30），会被 fixed z-40 的 Netflix 顶栏整个盖住——
 * 返回键看得见却永远点不到。因此 PageNav 在渲染入口按本判定短路，改为由
 * 页面自身的 NetflixBackButton 承担返回（netflix/back-button.tsx）。
 * 「新增路由自动继承、无需登记」的全站让位原则由此获得代码保证，不再只靠
 * 各页人工规避。
 */
export function isHomeRoute(pathname: string, isNetflix: boolean): boolean {
  return (
    pathname === "/" ||
    // Netflix 主题的 /library 顶部是全出血 Billboard（netflix/library-hero.tsx）
    (isNetflix && pathname === "/library") ||
    /^\/library\/\d+\/item\/\d+/.test(pathname) ||
    pathname.startsWith("/media/")
  );
}

/** 读取顶栏协商上下文；不在 AppShell 内（如独立的 /login、/setup）时返回 null。 */
export function usePageChrome(): PageChromeValue | null {
  return useContext(PageChromeContext);
}
