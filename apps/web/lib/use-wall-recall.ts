"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  onReturnFromLongAbsence,
  readWallRecall,
  RECALL_MIN_OFFSET,
  writeWallRecall,
} from "@/lib/library-wall-recall";

/** 记录节流：滚动中最多这么久写一次（用户滑得再快也只写十几次） */
const RECORD_THROTTLE_MS = 500;
/** 停下来这么久再补记一次，落定的位置才是「上次看到哪」 */
const RECORD_SETTLE_MS = 300;
/** 胶囊自动消失的滚动距离下限：不足一屏的窄视口也得滑够这么多 */
const DISMISS_MIN_DISTANCE_PX = 400;
/**
 * 胶囊刚出现后的这段时间里，滚动不计入「用户滑走了」。
 * 复位到墙首本身就会产生一次滚动，不豁免的话胶囊刚亮就被自己滑没了。
 */
const DISMISS_ARM_MS = 500;

interface WallRecallOptions {
  /** 记录键，见 wallRecallScope */
  scope: string;
  /** 当前墙的形态 + 排序口径，见 WallRecall.view */
  view: string;
  /** 真正的滚动容器 */
  scroller: HTMLElement | null;
  /** 数据到齐、排序稳定时才开工；false 时既不提示也不记录 */
  enabled: boolean;
  /**
   * 这次挂载算不算「重新进入」。会话内从详情页返回由滚动恢复自动回位
   * （lib/use-scroll-restoration.ts），那种情况不该再弹胶囊问一遍。
   */
  offer: boolean;
  /** 读当前首个可见条目在整份排序里的绝对位置；量不到给 null */
  offsetAt: () => number | null;
  /**
   * 久别回归时把这一屏复位（滚回墙首）。
   *
   * 只有这条路径需要它：页面没重新加载、组件没重挂，人还停在离开时的位置上，
   * 不复位的话胶囊指着的就是脚下这一格，点了等于没点。
   */
  onReenter?: () => void;
}

/**
 * 「回到上次浏览的位置」的记录与提示。
 *
 * 一句话规则：**胶囊在，就不记新位置**——否则用户刚进页面（在墙首）的那几次
 * 滚动会立刻把上次的位置覆盖成 0，胶囊按下去等于原地不动。所以：
 *
 *   1. 进入时读一次记录，够深且形态对得上就把 offset 交给调用方弹胶囊；
 *   2. 胶囊在期间只数滚动距离，滑够一屏就自己消失（用户用行动表示不需要）；
 *   3. 胶囊消失或被点掉之后，才开始把新位置写回去。
 *
 * 「进入」有两条路：组件重新挂载（冷启动 / 刷新 / 换库），以及**挂后台很久
 * 之后回到前台**——iOS PWA 恢复应用不会重新加载页面，只有后一条路走得通
 * （见 library-wall-recall.ts 的 LONG_ABSENCE_MS）。
 */
export function useWallRecall({
  scope,
  view,
  scroller,
  enabled,
  offer,
  offsetAt,
  onReenter,
}: WallRecallOptions) {
  const [recallOffset, setRecallOffset] = useState<number | null>(null);
  // 滚动回调里要读最新的取值函数与胶囊状态，但重建监听器会丢掉已累计的
  // 滚动距离，因此都走 ref
  const offsetAtRef = useRef(offsetAt);
  offsetAtRef.current = offsetAt;
  const onReenterRef = useRef(onReenter);
  onReenterRef.current = onReenter;
  const pendingRef = useRef<number | null>(null);
  pendingRef.current = recallOffset;
  /** 胶囊出现后累计的滚动距离，滑够一屏它就让位 */
  const travelled = useRef(0);
  /** 在这个时刻之前的滚动不计数（复位与恢复造成的那一下） */
  const armAt = useRef(0);

  /** 亮起胶囊，并把「滑走了」的计数归零重新开始 */
  const offerRecall = useCallback((offset: number | null) => {
    travelled.current = 0;
    armAt.current = Date.now() + DISMISS_ARM_MS;
    setRecallOffset(offset);
  }, []);

  // 同一面墙只问一次：数据分几批到达会让 enabled 反复变 true。换了形态
  //（切图床浏览）算另一面墙，先撤掉上一枚胶囊——它的 offset 是按上一种排序
  // 记的，留着按下去会跳到别处
  const asked = useRef<string | null>(null);
  useEffect(() => {
    const token = `${scope}|${view}`;
    if (asked.current === token) return;
    setRecallOffset(null);
    if (!enabled) return; // 数据还没到齐，等这面墙画出来再问
    asked.current = token;
    if (offer) offerRecall(readWallRecall(scope, view)?.offset ?? null);
  }, [enabled, offer, offerRecall, scope, view]);

  const dismissRecall = useCallback(() => setRecallOffset(null), []);

  // 久别回归（iOS PWA 恢复应用走的就是这条）：人还停在离开时那一屏上，
  // 先记下他停在哪，再把墙复位到顶部，然后拿这个位置弹胶囊
  useEffect(() => {
    if (!enabled) return;
    return onReturnFromLongAbsence(() => {
      const offset = offsetAtRef.current();
      if (offset === null || offset < RECALL_MIN_OFFSET) return; // 本来就在墙首附近，没什么可回的
      writeWallRecall(scope, view, offset);
      onReenterRef.current?.();
      offerRecall(offset);
    });
  }, [enabled, offerRecall, scope, view]);

  useEffect(() => {
    if (!scroller || !enabled) return;

    let lastTop = scroller.scrollTop;
    let lastWrite = 0;
    let settle: ReturnType<typeof setTimeout> | undefined;
    const record = () => {
      const offset = offsetAtRef.current();
      if (offset === null) return; // 墙还没画出来，等下一次滚动
      lastWrite = Date.now();
      writeWallRecall(scope, view, offset);
    };
    const onScroll = () => {
      const top = scroller.scrollTop;
      const moved = Math.abs(top - lastTop);
      lastTop = top;
      if (Date.now() < armAt.current) {
        // 复位/恢复那一下不算用户在滑
        travelled.current = 0;
        return;
      }
      travelled.current += moved;
      if (pendingRef.current !== null) {
        // 胶囊还在：用户滑过一屏就当他自己找位置去了，胶囊让位、记录接管
        if (travelled.current >= Math.max(DISMISS_MIN_DISTANCE_PX, scroller.clientHeight)) {
          setRecallOffset(null);
        }
        return;
      }
      // 滚动中按节流写一次（用户可能随时关掉页面，不能只等停下来那一下），
      // 停下来再补一次落定位置
      if (Date.now() - lastWrite >= RECORD_THROTTLE_MS) record();
      clearTimeout(settle);
      settle = setTimeout(record, RECORD_SETTLE_MS);
    };

    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      clearTimeout(settle);
      scroller.removeEventListener("scroll", onScroll);
    };
  }, [enabled, scope, scroller, view]);

  return { recallOffset, dismissRecall };
}
