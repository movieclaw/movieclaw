"use client";

import Link from "next/link";
import type { Route } from "next";
import { usePathname, useRouter } from "next/navigation";
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { BookmarkIcon, CompassIcon, LibraryIcon, MoreIcon } from "@/components/icons";
import { SearchCommand } from "@/components/search-command";
import {
  liquidKeyframes,
  liquidTransform,
  sampleAt,
  simulateSpring,
  type SpringFrame,
} from "@/lib/liquid-spring";
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
 *     底栏下方再垫一层滚动边缘效果（.glass-tabbar-edge），内容滚到底栏底下时
 *     渐暗渐糊，把玻璃「托起来」；
 *   - **液态选中胶囊**：在页签间按弹簧滑动，速度越快拉得越长、行进中微微抬起
 *     （lib/liquid-spring.ts，运动模型移植自 vendor LiquidGlassTabBar）；按住底栏
 *     左右拖动，胶囊跟手变成放大的「气泡」，松手吸附到最近页签并跳转；
 *   - **按压辉光**：手指落点处玻璃由内发亮再缓缓熄灭（HIG：从触点照亮）；
 *   - **下滑收缩**：内容向下滚时收成只剩当前页签图标的圆钮，反向滚、回到顶部
 *     或点它即展开（对应 tabBarMinimizeBehavior = .onScrollDown）。收缩时页签向
 *     当前页签收拢、糊掉淡出，圆钮在左端弹出；展开时页签自左向右依次浮现；
 *   - **搜索独立**：搜索不占页签，作为尾端单独的圆钮（对应 Tab(role: .search)），
 *     点开沿用全站 SearchCommand 面板。
 *
 * 页签：发现 / 媒体库 / 订阅 / 更多。「订阅」按 canSubscribe 显隐；「更多」
 * 落到 /my（主题 pages.my 坑位，基础实现 = components/more-page.tsx），收纳
 * 活动、设置、AI 会话、切换账号等低频入口；「新会话」不占页签，入口在顶栏
 * 右侧的撰写键（外壳的 ComposeSheet）。
 *
 * 玻璃走 CSS backdrop-filter 而不是 vendor/liquid-glass 的 WebGL：后者只能
 * 折射一张静态背景图，底栏浮在滚动的海报墙上必须对真实内容实时取样——
 * 2026-09-23 实测对比（docs/design/web-themes-mobile/04 §3.5）：直接用 vendor
 * LiquidGlassTabBar 在海报上是一块不透明黑胶囊，CSS 模糊 + WebGL 边光的混合版
 * 与纯 CSS 几乎看不出差别却多占两个 WebGL 上下文。Safari 也不支持 SVG 位移
 * 折射，边缘透镜感用 CSS 渐变近似（样式见 globals.css 的 .glass-tabbar 组）。
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
/** 手指横移超过这个距离才算「拖动擦选」，否则仍按点击处理 */
const DRAG_SLOP = 8;

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * 按压辉光：在触点处点亮玻璃（写 --gx/--gy 与 data-pressed，CSS 的 ::after
 * 负责径向高光的亮起与熄灭）。挂在胶囊的 pointerdown 上，抬起 / 取消 / 移出时熄灭。
 */
function glowAt(event: ReactPointerEvent<HTMLElement>) {
  const el = event.currentTarget;
  const rect = el.getBoundingClientRect();
  el.style.setProperty("--gx", `${event.clientX - rect.left}px`);
  el.style.setProperty("--gy", `${event.clientY - rect.top}px`);
  el.dataset.pressed = "true";
}
function glowOff(event: ReactPointerEvent<HTMLElement>) {
  delete event.currentTarget.dataset.pressed;
}

export function GlassTabBar() {
  const pathname = usePathname();
  const router = useRouter();
  const chrome = usePageChrome();
  const { canSubscribe, canSearch } = usePermissions();
  const tabs = canSubscribe
    ? [DISCOVER_TAB, LIBRARY_TAB, SUBSCRIPTION_TAB, MORE_TAB]
    : [DISCOVER_TAB, LIBRARY_TAB, MORE_TAB];
  const count = tabs.length;
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

  // ———— 液态选中胶囊 ————
  const navRef = useRef<HTMLElement>(null);
  const tabsRef = useRef<HTMLDivElement>(null);
  const indicatorRef = useRef<HTMLSpanElement>(null);
  /**
   * 指示器运动状态：anim 为正在播放的 WAAPI 动画，frames 是它的弹簧轨迹（打断时
   * 按已播放时长查当前位置与速度），x 是「已经决定要去」的位置——动画中 = 目标，
   * 静止 = 当前位置。拖动时由手指直接写 x。
   */
  const motion = useRef<{ anim: Animation | null; frames: SpringFrame[]; x: number }>({
    anim: null,
    frames: [],
    x: 0,
  });
  const drag = useRef({ id: -1, startX: 0, active: false, lastX: 0, lastT: 0, v: 0, suppressClick: false });

  const cellWidth = () => (tabsRef.current?.clientWidth ?? 0) / count;

  /** 当前的真实位置与速度：动画进行中按已播放时长插值，否则就是静止位置 */
  const currentState = () => {
    const m = motion.current;
    if (m.anim && m.anim.playState === "running") {
      return sampleAt(m.frames, Number(m.anim.currentTime ?? 0));
    }
    return { t: 0, x: m.x, v: 0 };
  };

  /** 立即摆到 x（无动画）：首帧定位、尺寸变化、减弱动态效果 */
  const placeAt = (x: number, v = 0, lift = 0) => {
    const el = indicatorRef.current;
    if (!el) return;
    motion.current.anim?.cancel();
    motion.current.anim = null;
    motion.current.x = x;
    el.style.transform = liquidTransform(x, v, lift);
  };

  /** 从当前状态（含速度）按弹簧滑到 x；可被下一次调用打断并接续 */
  const slideTo = (x: number, from = currentState()) => {
    const el = indicatorRef.current;
    if (!el) return;
    if (prefersReducedMotion() || (Math.abs(from.x - x) < 0.5 && Math.abs(from.v) < 1)) {
      placeAt(x);
      return;
    }
    motion.current.anim?.cancel();
    const frames = simulateSpring(from.x, x, from.v);
    // 先把静止终态写进内联样式，动画结束（或被取消）后元素停在正确的位置
    el.style.transform = liquidTransform(x, 0, 0);
    const anim = el.animate(liquidKeyframes(frames), {
      duration: frames[frames.length - 1].t,
      easing: "linear",
    });
    motion.current = { anim, frames, x };
    el.dataset.moving = "true";
    anim.onfinish = () => {
      if (motion.current.anim !== anim) return;
      motion.current.anim = null;
      delete el.dataset.moving;
    };
  };

  // 选中页签变化（点击、路由跳转、拖动松手后的跳转）→ 滑过去。
  // 首次挂载直接摆好不播动画；已经在去往该页签途中（点击时已提前起跳）则不重播。
  const placedRef = useRef(false);
  useLayoutEffect(() => {
    if (activeIndex < 0 || drag.current.active) return;
    const x = activeIndex * cellWidth();
    if (!placedRef.current) {
      placedRef.current = true;
      placeAt(x);
      return;
    }
    if (Math.abs(motion.current.x - x) < 0.5) return;
    slideTo(x);
    // cellWidth / placeAt / slideTo 只读 ref，不需要进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeIndex, count]);

  // 尺寸变化（转屏、订阅页签显隐）：格宽变了，按当前页签重新摆位
  useEffect(() => {
    const el = tabsRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => {
      if (activeIndex >= 0 && !drag.current.active) placeAt(activeIndex * cellWidth());
    });
    observer.observe(el);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeIndex, count]);

  /** 拖动中标记手指下方的页签（图标提亮），直接改 DOM 属性，避免逐帧重渲染 */
  const markHover = (index: number | null) => {
    tabsRef.current?.querySelectorAll<HTMLElement>(".glass-tabbar__tab").forEach((tab, i) => {
      tab.toggleAttribute("data-hover", i === index);
    });
  };

  const onTabsPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (isMinimized || (event.pointerType === "mouse" && event.button !== 0)) return;
    drag.current = {
      id: event.pointerId,
      startX: event.clientX,
      active: false,
      lastX: event.clientX,
      lastT: event.timeStamp,
      v: 0,
      suppressClick: false,
    };
  };

  const onTabsPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    const tabsEl = tabsRef.current;
    if (event.pointerId !== d.id || !tabsEl || activeIndex < 0) return;
    if (!d.active) {
      if (Math.abs(event.clientX - d.startX) < DRAG_SLOP) return;
      d.active = true;
      tabsEl.setPointerCapture(event.pointerId);
      navRef.current?.setAttribute("data-dragging", "true");
    }
    const rect = tabsEl.getBoundingClientRect();
    const cw = rect.width / count;
    const max = (count - 1) * cw;
    let x = event.clientX - rect.left - cw / 2;
    // 越过两端时带橡皮筋阻尼，而不是硬停：手感上「还能再拉一点，但拉不动了」
    if (x < 0) x *= 0.25;
    else if (x > max) x = max + (x - max) * 0.25;
    const dt = Math.max(1, event.timeStamp - d.lastT);
    d.v = d.v * 0.6 + ((event.clientX - d.lastX) / dt) * 1000 * 0.4;
    d.lastX = event.clientX;
    d.lastT = event.timeStamp;
    placeAt(x, d.v, 1);
    markHover(Math.max(0, Math.min(count - 1, Math.round(x / cw))));
  };

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>, commit: boolean) => {
    const d = drag.current;
    if (event.pointerId !== d.id) return;
    d.id = -1;
    if (!d.active) return;
    d.active = false;
    d.suppressClick = commit;
    navRef.current?.removeAttribute("data-dragging");
    markHover(null);
    const cw = cellWidth();
    const target = commit
      ? Math.max(0, Math.min(count - 1, Math.round(motion.current.x / cw)))
      : activeIndex;
    // 带着松手时的速度弹向落点：甩得快会越过一点再回来，像液滴落定
    slideTo(target * cw, { t: 0, x: motion.current.x, v: d.v });
    if (commit && target !== activeIndex) router.push(tabs[target].href as Route);
  };

  return (
    <>
      {/* 滚动边缘效果：内容滚到底栏下方时渐暗渐糊，把玻璃托起来（iOS 26
          scroll edge effect）。属于内容层之上、玻璃之下，不与玻璃叠玻璃 */}
      <div className="glass-tabbar-edge" data-minimized={isMinimized} aria-hidden="true" />
      <nav ref={navRef} aria-label="主导航" className="glass-tabbar" data-minimized={isMinimized}>
        <div
          className="glass-tabbar__bar glass-capsule"
          onPointerDown={glowAt}
          onPointerUp={glowOff}
          onPointerCancel={glowOff}
          onPointerLeave={glowOff}
        >
          <div
            ref={tabsRef}
            className="glass-tabbar__tabs"
            style={
              {
                "--tab-count": count,
                // 收缩时页签向当前页签收拢
                "--tab-origin": `${((Math.max(activeIndex, 0) + 0.5) / count) * 100}%`,
              } as CSSProperties
            }
            inert={isMinimized}
            onPointerDown={onTabsPointerDown}
            onPointerMove={onTabsPointerMove}
            onPointerUp={(e) => endDrag(e, true)}
            onPointerCancel={(e) => endDrag(e, false)}
            onClickCapture={(e) => {
              // 拖动擦选松手后浏览器仍会派发一次 click：已由 endDrag 处理跳转，这里吞掉
              if (!drag.current.suppressClick) return;
              drag.current.suppressClick = false;
              e.preventDefault();
              e.stopPropagation();
            }}
          >
            <span
              ref={indicatorRef}
              className="glass-tabbar__indicator"
              data-hidden={activeIndex < 0}
              aria-hidden="true"
            />
            {tabs.map(({ id, label, href, Icon }, index) => (
              <Link
                key={id}
                href={href as Route}
                aria-current={index === activeIndex ? "page" : undefined}
                className="glass-tabbar__tab"
                style={{ "--i": index } as CSSProperties}
                // 点击即起跳，不等路由真正切换完（新页面渲染可能要几百毫秒）
                onClick={() => slideTo(index * cellWidth())}
                draggable={false}
              >
                <Icon />
                <span>{label}</span>
              </Link>
            ))}
          </div>
          {/* 收起态圆钮常驻 DOM（只切换可见性），收起 / 展开两个方向都能做过渡；
              钉在胶囊左端而不是居中，胶囊收窄时图标原地不动 */}
          <button
            type="button"
            onClick={() => setMinimized(false)}
            aria-label="展开标签栏"
            aria-hidden={!isMinimized}
            tabIndex={isMinimized ? 0 : -1}
            className="glass-tabbar__mini"
          >
            {ActiveIcon && <ActiveIcon />}
          </button>
        </div>
        {/* 搜索圆钮：条件渲染而非 CSS 隐藏——SearchCommand 自带全局 ⌘K 监听，
            外壳在挂底栏的形态下不再在顶栏渲染搜索键，全站只此一份 */}
        {canSearch && chrome && (
          <div
            className="glass-tabbar__search glass-capsule"
            onPointerDown={glowAt}
            onPointerUp={glowOff}
            onPointerCancel={glowOff}
            onPointerLeave={glowOff}
          >
            <SearchCommand onSearch={chrome.onSearch} triggerClassName="glass-tabbar__search-btn" />
          </div>
        )}
      </nav>
    </>
  );
}
