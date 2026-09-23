"use client";

import Link from "next/link";
import type { Route } from "next";
import { usePathname } from "next/navigation";
import { useEffect, useState, type CSSProperties } from "react";

import { BookmarkIcon, CompassIcon, LibraryIcon, MoreIcon } from "@/components/icons";
import { SearchCommand } from "@/components/search-command";
import { usePageChrome } from "@/lib/page-chrome";
import { usePermissions } from "@/lib/permissions";

/**
 * 移动端底部标签栏的基础实现：iOS 26 液态玻璃悬浮胶囊
 * （docs/design/web-themes-mobile/04-iOS-液态玻璃底栏.md，<768px）。
 *
 * 形态对齐 iOS 26 系统 Tab Bar：
 *   - **悬浮胶囊**：离开屏幕左右与底边（21px / 约 22px），内容从它底下通铺到
 *     屏幕物理底边；页面滚到底时的让位由 .scroll-safe 在滚动内容末尾垫出，
 *     外壳主区不整体让位（与全站「内容从 Home 指示条下穿过」的约定一致）；
 *   - **选中态滑动**：一颗高亮胶囊在页签间弹性滑动（纯 transform，可被打断）；
 *   - **下滑收缩**：内容向下滚时收成只剩当前页签图标的圆钮，反向滚、回到顶部
 *     或点它即展开（对应 tabBarMinimizeBehavior = .onScrollDown）；
 *   - **搜索独立**：搜索不占页签，作为尾端单独的圆钮（对应 Tab(role: .search)），
 *     点开沿用全站 SearchCommand 面板。
 *
 * 页签：发现 / 媒体库 / 订阅 / 更多。「订阅」按 canSubscribe 显隐；「更多」
 * 落到 /my（主题 pages.my 坑位，基础实现 = components/more-page.tsx），收纳
 * 活动、设置、AI 会话、切换账号等低频入口；「新会话」不占页签，入口在顶栏
 * 右侧的撰写键（外壳的 ComposeSheet）。
 *
 * 玻璃走 CSS backdrop-filter 而不是 vendor/liquid-glass 的 WebGL：后者只能
 * 折射一张静态背景图，底栏浮在滚动的海报墙上必须对真实内容实时取样；Safari
 * 也不支持 SVG 位移折射，边缘透镜感用 CSS 渐变近似（样式见 globals.css 的
 * .glass-tabbar 组）。
 */

const DISCOVER_TAB = { id: "discover", label: "发现", href: "/discover/movie", Icon: CompassIcon } as const;
const LIBRARY_TAB = { id: "library", label: "媒体库", href: "/library", Icon: LibraryIcon } as const;
const SUBSCRIPTION_TAB = {
  id: "subscriptions",
  label: "订阅",
  href: "/subscriptions",
  Icon: BookmarkIcon,
} as const;
const MORE_TAB = { id: "more", label: "更多", href: "/my", Icon: MoreIcon } as const;

/** pathname → 当前页签 id（详情等子页落在所属的顶层页签上；无归属返回空串） */
function activeTabId(pathname: string): string {
  if (pathname.startsWith("/discover") || pathname.startsWith("/media")) return "discover";
  if (pathname.startsWith("/library")) return "library";
  if (pathname.startsWith("/subscriptions")) return "subscriptions";
  // 「更多」里的二级页面（设置、活动）保持父页签高亮——iOS 惯例，进二级页后
  // 页签全部熄灭会让用户失去「我在哪」的位置感
  if (
    pathname === "/my" ||
    pathname.startsWith("/settings") ||
    pathname.startsWith("/activity") ||
    pathname.startsWith("/tasks")
  ) {
    return "more";
  }
  return "";
}

/** 下滑多少才收缩：离顶太近时收起会让首屏显得局促 */
const MINIMIZE_MIN_SCROLL = 48;

export function GlassTabBar() {
  const pathname = usePathname();
  const chrome = usePageChrome();
  const { canSubscribe, canSearch } = usePermissions();
  const tabs = canSubscribe
    ? [DISCOVER_TAB, LIBRARY_TAB, SUBSCRIPTION_TAB, MORE_TAB]
    : [DISCOVER_TAB, LIBRARY_TAB, MORE_TAB];
  const activeIndex = tabs.findIndex((tab) => tab.id === activeTabId(pathname));
  const ActiveIcon = activeIndex >= 0 ? tabs[activeIndex].Icon : null;

  const [minimized, setMinimized] = useState(false);
  // 换页即展开：新页面从顶部开始，收着的底栏会让用户找不到导航
  useEffect(() => setMinimized(false), [pathname]);

  /**
   * 滚动方向监听。全站页面是「外层 h-full + 内层 overflow-y-auto」结构，滚动
   * 发生在各页自己的容器上而不是 window——scroll 事件不冒泡但可以在捕获阶段
   * 拦到，于是在 document 上挂一个捕获监听，按事件目标各自记上一次的 scrollTop。
   * 横滑海报行也会触发 scroll，但它们纵向不可滚（scrollTop 恒定），dy 为 0 直接忽略，
   * 否则横滑一次就会把收起的底栏误展开。
   */
  useEffect(() => {
    const lastTop = new WeakMap<Element, number>();
    const onScroll = (event: Event) => {
      const el = event.target instanceof Element ? event.target : document.scrollingElement;
      if (!el || el.scrollHeight <= el.clientHeight) return;
      const top = el.scrollTop;
      const dy = top - (lastTop.get(el) ?? top);
      lastTop.set(el, top);
      if (dy === 0) return;
      if (top <= 8) setMinimized(false);
      else if (dy > 6 && top > MINIMIZE_MIN_SCROLL) setMinimized(true);
      else if (dy < -10) setMinimized(false);
    };
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => document.removeEventListener("scroll", onScroll, { capture: true });
  }, []);

  // 没有归属页签的路由（/new、/search 等）不收缩：收起后只剩「当前页签」的
  // 圆钮，而这里没有当前页签可显示
  const isMinimized = minimized && ActiveIcon !== null;

  return (
    <nav aria-label="主导航" className="glass-tabbar" data-minimized={isMinimized}>
      <div className="glass-tabbar__bar glass-capsule">
        <div
          className="glass-tabbar__tabs"
          style={{ "--tab-count": tabs.length, "--tab-index": activeIndex } as CSSProperties}
          inert={isMinimized}
        >
          {activeIndex >= 0 && <span className="glass-tabbar__indicator" aria-hidden="true" />}
          {tabs.map(({ id, label, href, Icon }, index) => (
            <Link
              key={id}
              href={href as Route}
              aria-current={index === activeIndex ? "page" : undefined}
              className="glass-tabbar__tab"
            >
              <Icon />
              <span>{label}</span>
            </Link>
          ))}
        </div>
        {isMinimized && ActiveIcon && (
          <button
            type="button"
            onClick={() => setMinimized(false)}
            aria-label="展开标签栏"
            className="glass-tabbar__mini"
          >
            <ActiveIcon />
          </button>
        )}
      </div>
      {/* 搜索圆钮：条件渲染而非 CSS 隐藏——SearchCommand 自带全局 ⌘K 监听，
          外壳在挂底栏的形态下不再在顶栏渲染搜索键，全站只此一份 */}
      {canSearch && chrome && (
        <div className="glass-tabbar__search glass-capsule">
          <SearchCommand onSearch={chrome.onSearch} triggerClassName="glass-tabbar__search-btn" />
        </div>
      )}
    </nav>
  );
}
