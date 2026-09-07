"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { MinusIcon, PlusIcon, XIcon } from "@/components/icons";

/**
 * 可缩放的全屏灯箱内核（docs/design/library-photo-kind.md 3.3）。
 *
 * 图片库的灯箱（PhotoLightbox）与影视库图廊的灯箱（VideoGalleryLightbox）
 * 看的是两种数据（台账条目的原图 / 作品的海报剧照章节图），但舞台上的交互
 * 必须一模一样：渐进加载、缩放、手势、翻页、缩略条。这些全在这里，两个
 * 灯箱只负责把各自的数据折成 ``slides``、往顶栏塞各自的按钮（下载 / 信息、
 * 播放 / 详情）：
 *   - 渐进多级：先显示墙上的缩略图（模糊放大）→ 屏幕适配图 → 有 ``fullUrl``
 *     的只在放大到 1:1 时才拉原图。相邻两张预加载的也是屏幕适配图；
 *   - **控件不常驻**（照播放器）：顶栏、缩放控件、缩略条合起来是一层浮在画面
 *     上的「chrome」，闲置 3 秒自动淡出，鼠标一动 / 按任意键 / 点一下画面即
 *     回来，再点一下收起。收起后画面独占整个视口，什么都不叠——这是这个
 *     灯箱的常态，控件是临时召唤出来的；
 *   - 缩放：滚轮 / 触控板捏合以鼠标位置为锚，双击 / 双击屏幕在该点放大到 2.5×，
 *     两指捏合以两指中点为锚，最大 5×；放大后拖拽平移；键盘 +/- 缩放、`0` 复位。
 *     底栏那组「− 比例 +」控件**只给有鼠标的设备**：触屏上捏合与双击就是
 *     全部手势（iOS 相册也没有缩放按钮），摆一排按钮既多余又占画面。
 *     手势一律走 Pointer Events 自己判定（双击、捏合都不靠浏览器事件）：
 *     iOS 不派发 dblclick，舞台又必须 touch-action:none 挡住系统的整页缩放，
 *     交给浏览器的话手机上放大缩小就全无反应；
 *   - 翻页：未放大时左右拖拽 / 滑动，画面跟手移动，松手超过阈值翻页、不够弹回，
 *     鼠标与手指同一套；触控板横向两指滑同样翻页；键盘 ←→。舞台两侧不放
 *     箭头按钮——常驻的控件叠在画面上打破沉浸感，翻页动作本身已经够直觉。
 *     翻到已加载列表末尾且服务端还有下一页时向外要一页（onReachEnd），
 *     拿到后继续翻；
 *   - 缩略条只渲染当前位置前后各 30 张：万张库不铺满 DOM。
 *
 * 舞台上的浮层（``overlay``，如信息面板）自己挡掉指针事件并标上
 * ``data-lightbox-panel``：滚轮落在它上面是面板滚动，不当缩放；它开着的时候
 * 控件常显不自动收起——顶栏都没了、只剩一块信息面板浮在画面上很怪。
 * Portal 到 body，与 ImageLightbox 同一层叠约定。
 */
const STRIP_WINDOW = 30;
const MIN_ZOOM = 1;
const MAX_ZOOM = 5;
/** 放大 / 缩小按钮与 +/- 键每次的倍率 */
const ZOOM_STEP = 1.5;
/** 双击 / 双击屏幕放大到的倍率 */
const DOUBLE_TAP_ZOOM = 2.5;
/** 两次点按间隔与位移在此以内算双击（触屏没有 dblclick，自己判） */
const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_SLOP = 30;
/** 按下到抬起位移超过它就不算点按（是拖拽或滑动） */
const TAP_SLOP = 10;
/** 横向滑动松手时位移超过它就翻页，不够则弹回 */
const SWIPE_THRESHOLD = 70;
/** 触控板横向滚动累计超过它翻一页；翻过之后冷却一段时间，惯性滚动不会连翻几页 */
const WHEEL_SWIPE_THRESHOLD = 120;
const WHEEL_SWIPE_COOLDOWN_MS = 500;
/** 闲置多久自动收起控件（与主流播放器一致） */
const CHROME_IDLE_MS = 3000;
/** 手动收起后的静默期：这段时间内鼠标动了也不把控件叫回来。
 *  点一下收起，手离开鼠标时的一点点抖动就会立刻把它唤回来——收起等于没生效 */
const CHROME_HIDE_GRACE_MS = 900;

interface Point {
  x: number;
  y: number;
}

/** 缩放态：倍率 + 平移，一起更新才能保证锚点不漂 */
interface View {
  zoom: number;
  pan: Point;
}

const FIT_VIEW: View = { zoom: MIN_ZOOM, pan: { x: 0, y: 0 } };

function clampZoom(zoom: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

/**
 * 以舞台上的某点为锚缩放：锚点底下的那块画面在缩放前后停在原地。
 *
 * 图片元素居中于舞台、transform-origin 在自身中心，`anchor` 取相对舞台中心的
 * 坐标；锚点对应的画面坐标是 (anchor - pan) / zoom，要它不动，新的平移就是
 * anchor - (anchor - pan) × (新倍率 / 旧倍率)。缩回 1× 时一并归零平移，
 * 否则「适应屏幕」会停在偏离中心的位置。
 */
function zoomAt(anchor: Point, view: View, nextZoom: number): View {
  const zoom = clampZoom(nextZoom);
  if (zoom === MIN_ZOOM) return FIT_VIEW;
  const ratio = zoom / view.zoom;
  return {
    zoom,
    pan: {
      x: anchor.x - (anchor.x - view.pan.x) * ratio,
      y: anchor.y - (anchor.y - view.pan.y) * ratio,
    },
  };
}

function distance(a: Point, b: Point): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function midpoint(a: Point, b: Point): Point {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

/** 灯箱里的一张：三级图片地址 + 缩略条上的比例 */
export interface ZoomLightboxSlide {
  key: string | number;
  title: string;
  /** 墙上那张缩略图：先模糊铺底，屏幕适配图到达前看到大致颜色 */
  thumbUrl: string;
  /** 屏幕适配图（主图）；空串 = 没有可显示的图 */
  screenUrl: string;
  /** 放大到超过屏幕适配图分辨率时才拉的原图；不给则只有一级 */
  fullUrl?: string;
  /** 缩略条上这张的宽高比 */
  aspect: number;
}

export function ZoomLightbox({
  label,
  slides,
  index,
  hasMore,
  onIndexChange,
  onReachEnd,
  onClose,
  actions,
  overlay,
  note,
  onKey,
}: {
  /** 对话框的无障碍名 */
  label: string;
  /** 已加载的全部张（与墙同一列表、同一顺序） */
  slides: ZoomLightboxSlide[];
  index: number;
  /** 服务端还有下一页：翻到末尾时向外要 */
  hasMore: boolean;
  onIndexChange: (index: number) => void;
  onReachEnd: () => void;
  onClose: () => void;
  /** 顶栏右侧、关闭键左边的工具按钮 */
  actions?: ReactNode;
  /** 压在舞台上的浮层（信息面板等）；自己挡掉指针事件并标 data-lightbox-panel */
  overlay?: ReactNode;
  /** 舞台底部的提示文案（下载进度等）；不给时显示加载状态 */
  note?: string | null;
  /** 额外的快捷键：返回 true 表示已处理 */
  onKey?: (key: string) => boolean;
}) {
  const slide = slides[index];
  const [view, setView] = useState<View>(FIT_VIEW);
  const { zoom, pan } = view;
  // 屏幕适配图 / 原图各自的就绪态；原图只在放大后才开始加载
  const [screenReady, setScreenReady] = useState(false);
  const [wantOriginal, setWantOriginal] = useState(false);
  const [originalReady, setOriginalReady] = useState(false);
  const [broken, setBroken] = useState(false);
  const stageRef = useRef<HTMLDivElement>(null);
  // —— 手势状态（都是 ref：手势中每帧更新，不该触发重渲染）——
  /** 当前按在舞台上的所有指针（鼠标 / 手指），捏合靠它凑齐两根手指 */
  const pointers = useRef(new Map<number, Point>());
  /** 拖拽平移：指针位置减去平移量的偏移 */
  const drag = useRef<Point | null>(null);
  /** 捏合：起手时的两指距离与倍率、上一帧的两指中点（中点移动也跟着平移） */
  const pinch = useRef<{ dist: number; zoom: number; mid: Point } | null>(null);
  /** 本次按下的起点：抬起时判断是不是「原地点按」。``blank`` 记按下时是不是
   *  落在画面之外的空白上——指针一旦被舞台捕获，抬起事件的 target 会被重定向
   *  到舞台，那时已分不出点的是画面还是空白 */
  const tapStart = useRef<{ id: number; t: number; x: number; y: number; blank: boolean } | null>(
    null,
  );
  /** 上一次点按：与本次凑成双击 */
  const lastTap = useRef<{ t: number; x: number; y: number } | null>(null);
  /** 横向滑动翻页：起手的指针位置；null = 没在滑 */
  const swipe = useRef<{ id: number; x: number; y: number } | null>(null);
  /** 本次按下已经滑出了距离：松手后的 click 不当「点空白关闭」 */
  const swiped = useRef(false);
  /** 触控板横向滚动的累计量与上次翻页时刻 */
  const wheelSwipe = useRef({ acc: 0, last: 0 });
  /** 跟手的横向位移（px）：滑动中画面跟着指针走，松手归零 */
  const [swipeOffset, setSwipeOffset] = useState(0);
  // 翻到末尾要下一页的闸门：一页没回来之前不重复要
  const waitingMore = useRef(false);
  /** 单击的待执行动作：等 DOUBLE_TAP_MS 确认不是双击（双击是缩放）再落地 */
  const singleTap = useRef(0);
  // —— 控件显隐（chrome）——
  const [chromeShown, setChromeShown] = useState(true);
  const hideTimer = useRef(0);
  /** 手动收起后的静默期截止时刻：这之前鼠标移动不唤回控件 */
  const hideGraceUntil = useRef(0);
  /** 信息面板开着时钉住控件，不自动收起 */
  const pinned = Boolean(overlay);
  const pinnedRef = useRef(pinned);
  pinnedRef.current = pinned;

  const thumbUrl = slide?.thumbUrl ?? "";
  const screenUrl = slide?.screenUrl ?? "";
  const fullUrl = slide?.fullUrl ?? "";
  const fullReady = screenReady || originalReady;

  const resetZoom = useCallback(() => setView(FIT_VIEW), []);
  /** 按钮与键盘的缩放：以舞台中心为锚 */
  const zoomBy = useCallback(
    (factor: number) => setView((current) => zoomAt({ x: 0, y: 0 }, current, current.zoom * factor)),
    [],
  );

  /** 重新计时：到点自动收起（钉住时不计时） */
  const restartHide = useCallback(() => {
    window.clearTimeout(hideTimer.current);
    if (pinnedRef.current) return;
    hideTimer.current = window.setTimeout(() => setChromeShown(false), CHROME_IDLE_MS);
  }, []);
  /** 有动静（鼠标移动、按键）就把控件叫回来并重新计时 */
  const revealChrome = useCallback(() => {
    setChromeShown(true);
    restartHide();
  }, [restartHide]);
  /** 点画面：收放控件。手动收起的那一下起算一段静默期（见常量注释） */
  const toggleChrome = useCallback(() => {
    setChromeShown((shown) => {
      if (shown) hideGraceUntil.current = Date.now() + CHROME_HIDE_GRACE_MS;
      return !shown;
    });
  }, []);

  // 控件露出后开始倒计时；钉住时清掉计时并保持露出
  useEffect(() => {
    if (pinned) {
      window.clearTimeout(hideTimer.current);
      setChromeShown(true);
      return;
    }
    if (!chromeShown) return;
    restartHide();
    return () => window.clearTimeout(hideTimer.current);
  }, [chromeShown, pinned, restartHide]);

  // 卸载时把两个计时器都收掉
  useEffect(
    () => () => {
      window.clearTimeout(hideTimer.current);
      window.clearTimeout(singleTap.current);
    },
    [],
  );

  /** 视口坐标 → 相对舞台中心的坐标（zoomAt 的锚点约定） */
  const stageAnchor = useCallback((clientX: number, clientY: number): Point => {
    const rect = stageRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return { x: clientX - rect.left - rect.width / 2, y: clientY - rect.top - rect.height / 2 };
  }, []);

  // 换图：复位缩放与加载态
  useEffect(() => {
    resetZoom();
    setScreenReady(false);
    setWantOriginal(false);
    setOriginalReady(false);
    setBroken(false);
    setSwipeOffset(0);
  }, [index, resetZoom]);

  // 放大到超过屏幕适配图的分辨率时才拉原图（第三级）
  useEffect(() => {
    if (zoom > 1) setWantOriginal(true);
  }, [zoom]);

  // 列表变长了（要的下一页到了）：闸门放开
  useEffect(() => {
    waitingMore.current = false;
  }, [slides.length]);

  const step = useCallback(
    (delta: number) => {
      const next = index + delta;
      if (next < 0) return;
      if (next >= slides.length) {
        if (hasMore && !waitingMore.current) {
          waitingMore.current = true;
          onReachEnd();
        }
        return;
      }
      onIndexChange(next);
    },
    [hasMore, index, slides.length, onIndexChange, onReachEnd],
  );

  // 原生 wheel 监听只挂一次（依赖里没有 step / zoom），通过 ref 读最新值
  const stepRef = useRef(step);
  stepRef.current = step;
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;

  // 快翻到末尾前提前要下一页，翻到最后一张时通常已经到了
  useEffect(() => {
    if (hasMore && index >= slides.length - 5 && !waitingMore.current) {
      waitingMore.current = true;
      onReachEnd();
    }
  }, [hasMore, index, slides.length, onReachEnd]);

  // 键盘：Esc 关闭，←/→ 翻页，+/- 缩放，0 复位缩放；其余交给调用方
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // 键盘用户看不到「鼠标一动就回来」，任何按键都当作有动静
      revealChrome();
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") step(-1);
      else if (e.key === "ArrowRight") step(1);
      else if (e.key === "+" || e.key === "=") zoomBy(ZOOM_STEP);
      else if (e.key === "-") zoomBy(1 / ZOOM_STEP);
      else if (e.key === "0") resetZoom();
      else onKey?.(e.key);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, onKey, resetZoom, revealChrome, step, zoomBy]);

  // 锁住身后页面的滚动
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  // 滚轮 / 触控板捏合缩放，以鼠标位置为锚。
  // 必须用原生监听并声明 passive:false：React 把 wheel 挂成 passive 监听，
  // 合成事件里 preventDefault 无效（控制台报 "Unable to preventDefault inside
  // passive event listener"），触控板捏合会被浏览器拿去缩放整页。
  // 缩放幅度随滚动量走：鼠标滚轮一格（约 100）≈ 1.2×，触控板的细碎事件各自
  // 只走一点点，一次大幅滚动最多翻倍——比固定步长更跟手。
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (e: WheelEvent) => {
      // 浮层（信息面板）自己要滚动，不当缩放
      if ((e.target as Element).closest("[data-lightbox-panel]")) return;
      e.preventDefault();
      // 横向为主的滚动（触控板两指左右滑）在未放大时是翻页，不是缩放
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        if (zoomRef.current > 1) return;
        const state = wheelSwipe.current;
        if (e.timeStamp - state.last < WHEEL_SWIPE_COOLDOWN_MS) return;
        state.acc += e.deltaX;
        if (Math.abs(state.acc) >= WHEEL_SWIPE_THRESHOLD) {
          stepRef.current(state.acc > 0 ? 1 : -1);
          state.acc = 0;
          state.last = e.timeStamp;
        }
        return;
      }
      const factor = Math.min(2, Math.max(0.5, Math.exp(-e.deltaY * 0.002)));
      const anchor = stageAnchor(e.clientX, e.clientY);
      setView((current) => zoomAt(anchor, current, current.zoom * factor));
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => stage.removeEventListener("wheel", onWheel);
  }, [stageAnchor]);

  // 预加载相邻两张的屏幕适配图：翻过去不用再等（不预拉原图，几 MB 一张太贵）
  useEffect(() => {
    for (const neighbor of [index - 1, index + 1]) {
      const url = slides[neighbor]?.screenUrl;
      if (url) {
        const img = new Image();
        img.src = url;
      }
    }
  }, [index, slides]);

  // —— 舞台手势：拖拽平移、两指捏合、双击 / 双击屏幕 ——
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    // 翻页键、缩放控件上的按下交给按钮自己：舞台一旦捕获指针，click 会被
    // 重定向到舞台，按钮就点不响了
    if ((e.target as Element).closest("button")) return;
    const point = { x: e.clientX, y: e.clientY };
    pointers.current.set(e.pointerId, point);
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { dist: distance(a, b), zoom, mid: midpoint(a, b) };
      drag.current = null;
      tapStart.current = null;
      // 第二根手指落下即取消滑动翻页：这是捏合
      swipe.current = null;
      setSwipeOffset(0);
      return;
    }
    if (pointers.current.size > 2) return;
    tapStart.current = {
      id: e.pointerId,
      t: e.timeStamp,
      ...point,
      blank: e.target === e.currentTarget,
    };
    swiped.current = false;
    if (zoom > 1) {
      drag.current = { x: e.clientX - pan.x, y: e.clientY - pan.y };
    } else {
      // 未放大：这一按可能是横向滑动翻页，起手位置先记下，移动超过阈值才算
      swipe.current = { id: e.pointerId, ...point };
    }
    // 捕获指针：鼠标拖出舞台（甚至窗口）仍能收到移动与抬起
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const active = pinch.current;
    if (active && pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()];
      const mid = midpoint(a, b);
      const nextZoom = (active.zoom * distance(a, b)) / active.dist;
      const anchor = stageAnchor(mid.x, mid.y);
      const shift = { x: mid.x - active.mid.x, y: mid.y - active.mid.y };
      active.mid = mid;
      setView((current) => {
        const next = zoomAt(anchor, current, nextZoom);
        if (next.zoom === MIN_ZOOM) return next;
        // 两指整体移动时画面跟着走（捏合的同时也能平移）
        return { zoom: next.zoom, pan: { x: next.pan.x + shift.x, y: next.pan.y + shift.y } };
      });
      return;
    }
    if (drag.current) {
      const offset = drag.current;
      setView((current) => ({ ...current, pan: { x: e.clientX - offset.x, y: e.clientY - offset.y } }));
      return;
    }
    const start = swipe.current;
    if (start && start.id === e.pointerId && pointers.current.size === 1) {
      const dx = e.clientX - start.x;
      if (!swiped.current && Math.abs(dx) <= TAP_SLOP) return;
      swiped.current = true;
      // 到头了（前面没有 / 后面没有也不会再来）：阻尼跟手，提示这是边界
      const blocked = dx > 0 ? index === 0 : index >= slides.length - 1 && !hasMore;
      setSwipeOffset(blocked ? dx / 3 : dx);
    }
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(e.pointerId);
    drag.current = null;
    if (pointers.current.size < 2) pinch.current = null;
    const swipeStart = swipe.current;
    if (swipeStart && swipeStart.id === e.pointerId) {
      swipe.current = null;
      if (swiped.current) {
        const dx = e.clientX - swipeStart.x;
        setSwipeOffset(0);
        if (Math.abs(dx) >= SWIPE_THRESHOLD) step(dx < 0 ? 1 : -1);
        return;
      }
    }
    // 双击判定：本次是原地点按，且与上一次点按足够近、足够快。
    // 鼠标也走这条（不用 dblclick）：Android Chrome 双击会同时派发 dblclick，
    // 两条路各放大一次就抵消了，统一只认这一条
    const start = tapStart.current;
    tapStart.current = null;
    if (!start || start.id !== e.pointerId || pointers.current.size > 0) return;
    if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > TAP_SLOP) return;
    const previous = lastTap.current;
    const tap = { t: e.timeStamp, x: e.clientX, y: e.clientY };
    if (
      previous &&
      tap.t - previous.t < DOUBLE_TAP_MS &&
      Math.hypot(tap.x - previous.x, tap.y - previous.y) < DOUBLE_TAP_SLOP
    ) {
      lastTap.current = null;
      // 双击落定：取消上一次点按排队的单击动作，只做缩放
      window.clearTimeout(singleTap.current);
      const anchor = stageAnchor(tap.x, tap.y);
      setView((current) =>
        current.zoom > 1 ? FIT_VIEW : zoomAt(anchor, current, DOUBLE_TAP_ZOOM),
      );
      return;
    }
    lastTap.current = tap;
    // 单击：点画面切换控件显隐，点画面外的空白（未放大时）关闭。等一个双击
    // 判定窗口再落地——否则双击的第一下会先把控件闪一下
    const { blank } = start;
    window.clearTimeout(singleTap.current);
    singleTap.current = window.setTimeout(() => {
      if (blank && zoomRef.current === 1) onClose();
      else toggleChrome();
    }, DOUBLE_TAP_MS);
  };

  const onPointerCancel = (e: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(e.pointerId);
    drag.current = null;
    pinch.current = null;
    tapStart.current = null;
    swipe.current = null;
    setSwipeOffset(0);
  };

  const stripRange = useMemo(() => {
    const start = Math.max(0, index - STRIP_WINDOW);
    const end = Math.min(slides.length, index + STRIP_WINDOW + 1);
    return { start, end };
  }, [index, slides.length]);
  const activeThumb = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    activeThumb.current?.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
  }, [index]);

  if (!slide) return null;

  const gesturing = drag.current !== null || pinch.current !== null || swipe.current !== null;
  // 「正在加载原图」只在真有原图可等时显示：图廊这类只有一级图的，放大后
  // 没有第三级，不能挂着一条永远消不掉的提示
  const loadingNote =
    !broken && screenUrl && (!fullReady || (fullUrl && wantOriginal && !originalReady))
      ? fullReady
        ? "正在加载原图"
        : "正在加载"
      : null;
  const stageNote = note ?? loadingNote;
  // 控件浮在画面之上，舞台独占整个视口：图能用满屏幕（只留一圈 12px 呼吸），
  // 收起控件之后就是一张纯粹的图
  const imageClass =
    "max-h-[calc(100dvh-1.5rem)] max-w-full rounded-lg object-contain shadow-[0_24px_80px_rgba(0,0,0,0.8)]";
  // 一层控件的共同显隐。收起用 visibility 而不是只靠 pointer-events：外层要
  // pointer-events-none 让渐变垫层不挡住画面上的点击，内层交互元素又各自开回
  // auto，只有 visibility 能连着子元素一起关掉。transition 带上 visibility，
  // 淡出走完 300ms 才真正消失，淡入则立刻可见
  const chromeClass = `transition-[opacity,visibility] duration-300 motion-reduce:transition-none ${
    chromeShown || pinned ? "visible opacity-100" : "invisible opacity-0"
  }`;

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={label}
      // 鼠标一动就把控件叫回来；手势进行中不算——拖着翻页时不该冒出一层控件
      onPointerMove={() => {
        if (drag.current || pinch.current || swipe.current) return;
        if (Date.now() < hideGraceUntil.current) return;
        revealChrome();
      }}
      className="fixed inset-0 z-[70] overflow-hidden bg-[rgba(4,5,9,0.94)] backdrop-blur-md [bottom:calc(-1*var(--vp-overshoot))]"
    >
      {/* 舞台：铺满整个对话框（控件浮在它上面）。点画面收放控件、点画面外的
          空白关闭（未缩放时），滚轮 / 捏合 / 双击缩放，放大后拖拽平移、
          未放大时左右拖拽翻页。
          touch-none：系统的整页捏合与双击缩放交给自己判定的手势 */}
      <div
        ref={stageRef}
        className={`absolute inset-0 flex touch-none items-center justify-center overflow-hidden p-3 ${
          zoom > 1
            ? drag.current
              ? "cursor-grabbing"
              : "cursor-grab"
            : swipeOffset !== 0
              ? "cursor-grabbing"
              : "cursor-zoom-in"
        }`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
      >
        {broken || !screenUrl ? (
          <div className="rounded-2xl border border-white/[0.12] bg-white/[0.04] px-8 py-10 text-center text-ui text-white/60">
            图片加载失败
            <span className="mt-1 block text-caption text-white/40">
              文件可能已被移动或删除，重新扫描后会更新
            </span>
          </div>
        ) : (
          <div
            className="relative max-h-full max-w-full select-none"
            style={{
              transform: `translate(${pan.x + swipeOffset}px, ${pan.y}px) scale(${zoom})`,
              transition: gesturing ? "none" : "transform 160ms ease-out",
            }}
          >
            {/* 三级渐进：缩略图垫底（模糊）→ 屏幕适配图盖上 → 放大后原图再盖上 */}
            {thumbUrl && (
              <img
                src={thumbUrl}
                alt=""
                aria-hidden="true"
                draggable={false}
                className={`${imageClass} transition-[filter] duration-300 ${
                  fullReady ? "invisible absolute inset-0" : "blur-[6px] brightness-90"
                }`}
              />
            )}
            <img
              key={screenUrl}
              src={screenUrl}
              alt={slide.title}
              data-stage="screen"
              draggable={false}
              onLoad={() => setScreenReady(true)}
              onError={() => {
                // 派生失败（格式 Pillow 不认等）：有原图就直接退到原图
                if (fullUrl) setWantOriginal(true);
                else setBroken(true);
              }}
              className={`${imageClass} ${
                screenReady && !originalReady ? "" : "absolute inset-0 opacity-0"
              }`}
            />
            {fullUrl && wantOriginal && (
              <img
                key={fullUrl}
                src={fullUrl}
                alt={slide.title}
                data-stage="original"
                draggable={false}
                onLoad={() => setOriginalReady(true)}
                onError={() => setBroken(true)}
                className={`${imageClass} ${originalReady ? "" : "absolute inset-0 opacity-0"}`}
              />
            )}
          </div>
        )}

        {/* 加载 / 下载提示：放画面顶部而不是底部——底部整条归控件，
            而这条提示在控件收起时也要能看见 */}
        {stageNote && (
          <span className="pointer-events-none absolute left-1/2 top-[calc(4.25rem+var(--safe-top))] -translate-x-1/2 whitespace-nowrap rounded-full bg-black/50 px-2.5 py-0.5 text-micro tracking-wide text-white/50">
            {stageNote}
          </span>
        )}

        {overlay}
      </div>

      {/* 顶栏：计数 + 标题 + 工具。渐变垫底让白字压在亮图上也读得清；
          容器不吃指针事件，只有右侧那组按钮吃 */}
      <div
        className={`pointer-events-none absolute inset-x-0 top-0 z-20 flex items-center gap-3 bg-gradient-to-b from-[rgba(4,5,9,0.85)] via-[rgba(4,5,9,0.4)] to-transparent px-4 pb-8 text-white/85 [padding-top:calc(0.625rem+var(--safe-top))] max-md:gap-2 max-md:px-3 ${chromeClass}`}
      >
        <span className="tnum shrink-0 rounded-full bg-white/[0.1] px-2.5 py-0.5 text-sub">
          {index + 1} / {hasMore ? `${slides.length}+` : slides.length}
        </span>
        <p className="min-w-0 flex-1 truncate text-center text-ui text-white/70">{slide.title}</p>
        <div className="pointer-events-auto flex shrink-0 items-center gap-1">
          {actions}
          <button
            type="button"
            aria-label="关闭 (Esc)"
            onClick={onClose}
            className="rounded-full p-2 text-white/70 transition-colors hover:bg-white/[0.12] hover:text-white"
          >
            <XIcon className="size-5" />
          </button>
        </div>
      </div>

      {/* 底栏：缩放控件 + 缩略条，与顶栏同进同退 */}
      <div
        className={`pointer-events-none absolute inset-x-0 bottom-0 z-20 bg-gradient-to-t from-[rgba(4,5,9,0.9)] via-[rgba(4,5,9,0.5)] to-transparent pt-12 ${chromeClass}`}
      >
        {/* 缩放控件：比例以适应屏幕为 100%，点比例复位。触屏上整组不出现——
            捏合放大、双击放大 / 复位已经覆盖了它的全部功能 */}
        {!broken && screenUrl && (
          <div className="pointer-events-auto mx-auto mb-3 flex w-max items-center gap-0.5 rounded-full bg-black/70 p-1 text-white/85 [@media(hover:none)]:hidden">
            <button
              type="button"
              title="缩小 (-)"
              aria-label="缩小"
              disabled={zoom <= MIN_ZOOM}
              onClick={() => zoomBy(1 / ZOOM_STEP)}
              className="grid size-7 place-items-center rounded-full transition-colors hover:bg-white/[0.12] hover:text-white disabled:opacity-35 disabled:hover:bg-transparent"
            >
              <MinusIcon className="size-4" />
            </button>
            <button
              type="button"
              title="适应屏幕 (0)"
              aria-label={`当前 ${Math.round(zoom * 100)}%，点击适应屏幕`}
              onClick={resetZoom}
              className="tnum min-w-[52px] rounded-full px-1.5 py-1 text-caption transition-colors hover:bg-white/[0.12] hover:text-white"
            >
              {Math.round(zoom * 100)}%
            </button>
            <button
              type="button"
              title="放大 (+)"
              aria-label="放大"
              disabled={zoom >= MAX_ZOOM}
              onClick={() => zoomBy(ZOOM_STEP)}
              className="grid size-7 place-items-center rounded-full transition-colors hover:bg-white/[0.12] hover:text-white disabled:opacity-35 disabled:hover:bg-transparent"
            >
              <PlusIcon className="size-4" />
            </button>
          </div>
        )}

        {/* 缩略条：只渲染当前位置前后各 30 张 */}
        <div className="scroll-none pointer-events-auto overflow-x-auto px-4 pb-2.5 [padding-bottom:calc(0.625rem+var(--safe-bottom)+var(--vp-overshoot))] max-md:px-3">
          <div className="mx-auto flex w-max gap-1.5">
            {slides.slice(stripRange.start, stripRange.end).map((entry, offset) => {
              const i = stripRange.start + offset;
              const active = i === index;
              return (
                <button
                  key={entry.key}
                  ref={active ? activeThumb : undefined}
                  type="button"
                  aria-label={`查看 ${entry.title}`}
                  aria-current={active}
                  onClick={() => onIndexChange(i)}
                  className={`h-12 shrink-0 overflow-hidden rounded-md transition ${
                    active ? "ring-2 ring-[var(--accent)]" : "opacity-45 hover:opacity-90"
                  }`}
                  style={{ width: Math.round(48 * Math.min(2, Math.max(0.5, entry.aspect))) }}
                >
                  <img
                    src={entry.thumbUrl}
                    alt=""
                    loading="lazy"
                    className="h-full w-full bg-white/[0.05] object-cover"
                  />
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
