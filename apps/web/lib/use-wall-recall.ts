"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { readWallRecall, writeWallRecall } from "@/lib/library-wall-recall";

/** 记录节流：滚动中最多这么久写一次（用户滑得再快也只写十几次） */
const RECORD_THROTTLE_MS = 500;
/** 停下来这么久再补记一次，落定的位置才是「上次看到哪」 */
const RECORD_SETTLE_MS = 300;
/** 胶囊自动消失的滚动距离下限：不足一屏的窄视口也得滑够这么多 */
const DISMISS_MIN_DISTANCE_PX = 400;

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
   * 这次是不是「重新进入」。会话内从详情页返回由滚动恢复自动回位
   * （lib/use-scroll-restoration.ts），那种情况不该再弹胶囊问一遍。
   */
  offer: boolean;
  /** 读当前首个可见条目在整份排序里的绝对位置；量不到给 null */
  offsetAt: () => number | null;
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
 */
export function useWallRecall({
  scope,
  view,
  scroller,
  enabled,
  offer,
  offsetAt,
}: WallRecallOptions) {
  const [recallOffset, setRecallOffset] = useState<number | null>(null);
  // 滚动回调里要读最新的取值函数与胶囊状态，但重建监听器会丢掉已累计的
  // 滚动距离，因此都走 ref
  const offsetAtRef = useRef(offsetAt);
  offsetAtRef.current = offsetAt;
  const pendingRef = useRef<number | null>(null);
  pendingRef.current = recallOffset;

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
    if (offer) setRecallOffset(readWallRecall(scope, view)?.offset ?? null);
  }, [enabled, offer, scope, view]);

  const dismissRecall = useCallback(() => setRecallOffset(null), []);

  useEffect(() => {
    if (!scroller || !enabled) return;

    let lastTop = scroller.scrollTop;
    let travelled = 0;
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
      travelled += Math.abs(top - lastTop);
      lastTop = top;
      if (pendingRef.current !== null) {
        // 胶囊还在：用户滑过一屏就当他自己找位置去了，胶囊让位、记录接管
        if (travelled >= Math.max(DISMISS_MIN_DISTANCE_PX, scroller.clientHeight)) {
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
