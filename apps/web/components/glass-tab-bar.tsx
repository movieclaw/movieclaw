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

import { usePendingUpdate } from "@/components/app-update-entry";
import { AvatarBadge } from "@/components/avatar-badge";
import { ActivityIcon, BookmarkIcon, HomeIcon, LibraryStackIcon } from "@/components/icons";
import { mediaLiveCount, useMediaActivity } from "@/components/media-activity-section";
import { GlassRim, type RimTarget } from "@/lib/glass-rim";
import {
  liquidKeyframes,
  liquidTransform,
  sampleAt,
  simulateSpring,
  type SpringFrame,
} from "@/lib/liquid-spring";
import { usePageChrome } from "@/lib/page-chrome";
import { usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";
import { taskActivityBadge, useTaskActivity, type TaskActivityBadge } from "@/lib/task-activity";

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
 *     当前页签收拢、糊掉淡出，圆钮在左端弹出；展开时页签自左向右依次浮现。
 *
 * 页签（纯图标，参照 Instagram iOS 底栏）：发现 / 媒体库 / 订阅 / 活动 / 头像。
 * 「订阅」按 canSubscribe 显隐，「活动」仅管理员；最右的头像是当前用户头像，
 * 落到 /my「更多」页（主题 pages.my 坑位，基础实现 = components/more-page.tsx），
 * 收纳个人信息、设置、切换账号、AI 会话等低频入口，/settings 下的页面也算在它名下。
 * 与原生 App 同一形态（2026-09-26 用户要求两端一致）：头像原先在顶栏左上角、点开
 * 半屏面板，搜索原先是底栏尾端的独立圆钮（Tab(role: .search)），现在搜索挪到了
 * 顶栏右上角（app-shell 的 MobileTopBar / 详情页的 PageNav），底栏只剩页签。
 *
 * 玻璃分两层（docs/design/web-themes-mobile/04 §3.7）：
 *   - 胶囊中心是清透的 CSS 层（不模糊不染色），透出真实的滚动内容（WebGL 读不到网页内容，
 *     2026-09-23 直接用 vendor LiquidGlassTabBar 在海报上是一块黑胶囊，§3.5）；
 *   - 胶囊厚边一圈由 lib/glass-rim.ts 用 WebGL 画折射 / 色散 / 菲涅尔 / 高光
 *     （光学模型移植自 liquid-glass-studio）：每帧把底栏下方的背景图与海报重建成
 *     场景纹理来折射，所以边上折进去的是真实滚过的海报。每个胶囊里一块 canvas
 *     （.glass-rim）承接渲染结果；WebGL2 不可用或渲染出错时胶囊不打 data-rim，
 *     保持原来的纯 CSS 暗玻璃。
 */

const DISCOVER_TAB = { id: "discover", label: "发现", href: "/discover/movie", Icon: HomeIcon } as const;
const LIBRARY_TAB = { id: "library", label: "媒体库", href: "/library", Icon: LibraryStackIcon } as const;
const SUBSCRIPTION_TAB = {
  id: "subscriptions",
  label: "订阅",
  href: "/subscriptions",
  Icon: BookmarkIcon,
} as const;
/** 活动（任务中心）：高频入口，从「更多」页提到底栏（2026-09-24 用户要求）；
 *  Agent 能力，管理员专属——与侧栏 memberNavItems 同口径。iOS 标签栏上限 5 个，
 *  管理员加上头像正好 5 格；搜索挪到顶栏后胶囊铺满整宽，375pt 机型上每格约 65pt。 */
const ACTIVITY_TAB = { id: "activity", label: "活动", href: "/activity", Icon: ActivityIcon } as const;

/** 「更多」页签的图标：当前用户头像（没上传过头像时是昵称首字徽标） */
function MoreTabAvatar() {
  const { session } = useSession();
  return (
    <AvatarBadge
      nickname={session.nickname}
      avatarUrl={session.avatar_url}
      className="glass-tabbar__avatar text-[10px]"
    />
  );
}

const MORE_TAB = { id: "more", label: "更多", href: "/my", Icon: MoreTabAvatar } as const;

/** pathname → 当前页签 id（详情等子页落在所属的顶层页签上；无归属返回空串） */
function activeTabId(pathname: string): string {
  if (pathname.startsWith("/discover") || pathname.startsWith("/media")) return "discover";
  if (pathname.startsWith("/library")) return "library";
  if (pathname.startsWith("/subscriptions")) return "subscriptions";
  // 活动有自己的页签；非管理员看不到该页签时这里返回的 id 匹配不到任何格，
  // 页签全灭（他们本来也进不了活动页）
  if (pathname.startsWith("/activity") || pathname.startsWith("/tasks")) return "activity";
  // 设置从「更多」页进，归在头像页签名下（原生 App 的设置栈同样在「更多」页签里）
  if (pathname.startsWith("/my") || pathname.startsWith("/settings")) return "more";
  return "";
}

/**
 * 活动页签圆点的取舍：任务（需要处理 / 进行中）与观看（有人在播）可能同时
 * 成立，但页签上只放一颗点，按「最需要提醒」取一个：
 *   告警红（有任务要处理，需要动手）> 观看绿（此刻有人在播）> 进行中蓝（例行进度）。
 * 颜色与活动页一级切换器上的提示逐一对应；被压下去的状态进活动页都看得到。
 */
function pickActivityDot(
  task: TaskActivityBadge,
  liveCount: number,
): { tone: "alert" | "live" | "active"; hint: string } | null {
  if (task.alert && task.count > 0) return { tone: "alert", hint: task.hint };
  if (liveCount > 0) return { tone: "live", hint: "有人正在观看" };
  if (task.count > 0) return { tone: "active", hint: task.hint };
  return null;
}

/** 下滑多少才收缩：iOS 26 的 onScrollDown 是向下滑一小段就收（相册实测），
 *  但离顶太近时收起会让首屏显得局促，留一点余量 */
const MINIMIZE_MIN_SCROLL = 24;
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
  const accessory = chrome?.tabBarAccessory ?? null;
  const { canSubscribe, isAdmin } = usePermissions();
  const { session } = useSession();
  const tabs = [
    DISCOVER_TAB,
    LIBRARY_TAB,
    ...(canSubscribe ? [SUBSCRIPTION_TAB] : []),
    ...(isAdmin ? [ACTIVITY_TAB] : []),
    MORE_TAB,
  ];
  // 活动页签的任务圆点（iOS 页签红点惯例）：告警红 / 否则提示蓝；数据来自全站 Provider
  const activityBadge = taskActivityBadge(useTaskActivity());
  // 观看状态：此刻有人在播。只有管理员有活动页签，也只有管理员能读媒体活动
  // 接口，按同一个权限门控轮询，成员不会打出 403
  const liveCount = mediaLiveCount(useMediaActivity(isAdmin).snapshot);
  const activityDot = pickActivityDot(activityBadge, liveCount);
  // 头像页签的蓝点：有待安装的新版本 / 模型（原先画在顶栏左上角的头像上；成员不查更新）
  const pendingUpdate = usePendingUpdate(session.role !== "member");
  const count = tabs.length;
  const activeIndex = tabs.findIndex((tab) => tab.id === activeTabId(pathname));
  const ActiveIcon = activeIndex >= 0 ? tabs[activeIndex].Icon : null;

  const [minimized, setMinimized] = useState(false);
  // 有底部附件的页面（发现页）：页面在顶部时正常展开主菜单（附件隐藏）；向下滑
  // 一小段就收成圆钮、附件在圆钮与搜索圆钮之间露出并常驻；往回滑**不**展开，
  // 只有滚回顶部或点圆钮才展开；选完页签 / 再点当前页签也收回。这是用户拍板的
  // 形态：附件不浮到底栏上方去挤内容，进页面第一眼看到的仍是完整菜单。
  const hasAccessory = accessory != null;
  const [expandedOverAccessory, setExpandedOverAccessory] = useState(true);
  const hasAccessoryRef = useRef(hasAccessory);
  hasAccessoryRef.current = hasAccessory;
  // 换页即回默认态（新页面从顶部开始）：两种模式都展开
  useEffect(() => {
    setMinimized(false);
    setExpandedOverAccessory(true);
  }, [pathname]);

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
      if (hasAccessoryRef.current) {
        // 附件模式：回到顶部展开；向下滑一小段收起露出附件；往回滑不展开
        if (top <= 8) setExpandedOverAccessory(true);
        else if (dy > 6 && top > MINIMIZE_MIN_SCROLL) setExpandedOverAccessory(false);
        return;
      }
      if (top <= 8) setMinimized(false);
      else if (dy > 6 && top > MINIMIZE_MIN_SCROLL) setMinimized(true);
      else if (dy < -10) setMinimized(false);
    };
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => document.removeEventListener("scroll", onScroll, { capture: true });
  }, []);

  // 没有归属页签的路由（/search 等）不收缩：收起后只剩「当前页签」的
  // 圆钮，而这里没有当前页签可显示
  const isMinimized = (hasAccessory ? !expandedOverAccessory : minimized) && ActiveIcon !== null;

  // ———— WebGL 玻璃厚边 ————
  const barRef = useRef<HTMLDivElement>(null);
  const accessoryRef = useRef<HTMLDivElement>(null);
  const rimRef = useRef<GlassRim | null>(null);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-transparency: reduce)").matches) return;
    let rim: GlassRim;
    try {
      rim = new GlassRim();
    } catch (error) {
      console.warn(String(error instanceof Error ? error.message : error));
      return;
    }
    rimRef.current = rim;
    rim.start(() =>
      [barRef, accessoryRef].flatMap((ref): RimTarget[] => {
        const capsule = ref.current;
        const canvas = capsule?.querySelector<HTMLCanvasElement>(":scope > canvas.glass-rim");
        return capsule && canvas ? [{ capsule, canvas }] : [];
      }),
    );
    return () => {
      rim.dispose();
      rimRef.current = null;
    };
  }, []);
  // 换页、收缩 / 展开、附件显隐：重新出几帧（新页面的海报、胶囊新形状）
  useEffect(() => {
    rimRef.current?.wake();
  }, [pathname, isMinimized, hasAccessory]);

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
      {/* 底部附件（iOS 26 tab bar bottom accessory）：常驻在收起圆钮右侧，
          主菜单展开时隐藏（样式见 .glass-tabbar-accessory） */}
      {accessory != null && (
        <div
          ref={accessoryRef}
          className="glass-tabbar-accessory glass-capsule"
          data-minimized={isMinimized}
          onPointerDown={glowAt}
          onPointerUp={glowOff}
          onPointerCancel={glowOff}
          onPointerLeave={glowOff}
        >
          <canvas className="glass-rim" aria-hidden="true" />
          {accessory}
        </div>
      )}
      <nav ref={navRef} aria-label="主导航" className="glass-tabbar" data-minimized={isMinimized}>
        <div
          ref={barRef}
          className="glass-tabbar__bar glass-capsule"
          onPointerDown={glowAt}
          onPointerUp={glowOff}
          onPointerCancel={glowOff}
          onPointerLeave={glowOff}
        >
          <canvas className="glass-rim" aria-hidden="true" />
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
                // 纯图标页签（2026-09-25 用户要求去掉文字，参照 Instagram 底栏）：名字只给读屏
                aria-label={label}
                className="glass-tabbar__tab"
                style={{ "--i": index } as CSSProperties}
                // 点击即起跳，不等路由真正切换完（新页面渲染可能要几百毫秒）
                onClick={(e) => {
                  // 附件模式下再点当前页签 = 收回主菜单（不导航）
                  if (hasAccessory && index === activeIndex) {
                    e.preventDefault();
                    setExpandedOverAccessory(false);
                    return;
                  }
                  slideTo(index * cellWidth());
                }}
                draggable={false}
              >
                <Icon />
                {/* 小圆点而不是数字：iOS 标签栏的「有新动态」惯例，数字角标在 26px
                    图标旁太重（用户看过实机截图直接否掉）；具体数量进活动页看 */}
                {id === "activity" && activityDot && (
                  <span
                    className="glass-tabbar__badge"
                    data-alert={activityDot.tone === "alert"}
                    data-live={activityDot.tone === "live"}
                    title={activityDot.hint}
                    aria-label={activityDot.hint}
                  />
                )}
                {id === "more" && pendingUpdate && (
                  <span className="glass-tabbar__badge" title="有可用更新" aria-label="有可用更新" />
                )}
              </Link>
            ))}
          </div>
          {/* 收起态圆钮常驻 DOM（只切换可见性），收起 / 展开两个方向都能做过渡；
              钉在胶囊左端而不是居中，胶囊收窄时图标原地不动 */}
          <button
            type="button"
            onClick={() => (hasAccessory ? setExpandedOverAccessory(true) : setMinimized(false))}
            aria-label="展开标签栏"
            aria-hidden={!isMinimized}
            tabIndex={isMinimized ? 0 : -1}
            className="glass-tabbar__mini"
          >
            {ActiveIcon && <ActiveIcon />}
          </button>
        </div>
      </nav>
    </>
  );
}

/**
 * 底部附件里的分段切换（相册「年 / 月 / 全部」的形态）：撑满附件胶囊、各段等宽，
 * 选中段一枚白系药丸。页面用 chrome.setTabBarAccessory 挂进来。
 */
export function AccessorySegmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="glass-tabbar-accessory__seg"
      style={{ "--seg-count": options.length } as CSSProperties}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          data-active={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
