"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { ChevronLeftIcon, ChevronRightIcon } from "@/components/icons";

/**
 * 横滚容器（Netflix 式行），发现页海报行与媒体库卡片行共用。
 *
 * 交互设计：
 *   - 隐藏原生滚动条（.scroll-none），还能往某个方向滚时对应的玻璃圆钮
 *     **常驻显示**（静息略收敛、行 hover 提到全亮），点击按约 85% 可视宽度
 *     平滑翻页；触控板/滚轮横扫仍然可用。曾经是 hover 才浮现——不悬停就
 *     完全隐形，等于没告诉用户「右边还有内容」，纯滚动条隐藏反而藏掉了
 *     唯一的可滑线索。
 *   - 触屏上不渲染（hover:none 媒体查询）：那边的可滑线索是刻意露出的半张
 *     卡（见 library-home-recently-watched.md §5），常驻圆钮只会压住卡片
 *     还挨误触。
 *   - 到达边缘时对应方向的按钮隐藏（用 onScroll 实时追踪滚动位置）；
 *     内容不足一屏时两侧都不出现，视觉上与普通一行无异。
 *   - 间距/内边距由调用方通过 className 决定，容器只管滚动与翻页。
 */
export function HScroller({
  children,
  className = "",
}: {
  children: ReactNode;
  /** 追加到滚动容器的类名（gap / padding 等排版由调用方决定） */
  className?: string;
}) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [canLeft, setCanLeft] = useState(false);
  const [canRight, setCanRight] = useState(false);
  const edgeFrame = useRef(0);

  /** 根据当前滚动位置更新两侧按钮的可用性（含 1px 容差，避免亚像素误差）。
   *  测量合并进下一帧统一执行：scroll 事件每帧可触发多次，事件回调里直接读
   *  scrollLeft/clientWidth 会强制同步布局；发现页一屏多个横滚行叠加起来，
   *  滑动时的布局抖动很可感。rAF 内读取则每帧至多量一次、且在布局干净时执行 */
  const updateEdges = useCallback(() => {
    if (edgeFrame.current) return;
    edgeFrame.current = window.requestAnimationFrame(() => {
      edgeFrame.current = 0;
      const el = scrollerRef.current;
      if (!el) return;
      setCanLeft(el.scrollLeft > 1);
      setCanRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 1);
    });
  }, []);

  // 无依赖数组：子项异步加载（如媒体库列表）后内容宽度会变，每次渲染后都重量
  // 一次最省心；两个 state 未变时 React 自行短路，不会引起额外渲染
  useEffect(updateEdges);

  // 视口尺寸变化会改变可视宽度，跟着重算；卸载时取消未执行的测量帧
  useEffect(() => {
    window.addEventListener("resize", updateEdges);
    return () => {
      window.removeEventListener("resize", updateEdges);
      window.cancelAnimationFrame(edgeFrame.current);
    };
  }, [updateEdges]);

  const page = (dir: -1 | 1) => {
    const el = scrollerRef.current;
    el?.scrollBy({ left: dir * el.clientWidth * 0.85, behavior: "smooth" });
  };

  return (
    // onPointerEnter 重测：行被 content-visibility 跳过渲染时挂载测量读到的
    // 尺寸全是 0，滚入视口浏览器恢复渲染但不会触发 React 重渲染——翻页钮
    // 只在悬停时浮现，进场先重测一次即可保证钮的可用性正确；测量已并帧，
    // 重复触发无额外开销
    <div className="group/hscroll relative" onPointerEnter={updateEdges}>
      <div
        ref={scrollerRef}
        onScroll={updateEdges}
        // 注意不能加 scroll-snap：snap 的回吸会和 scrollBy 的平滑动画互相抵消，导致箭头点击无效
        // overscroll-x-contain：横滑到行的尽头后不把剩余动量传给外层纵向滚动区，
        // 否则手机上「滑到头」会顺势带动整页跳一下，手感失控也更容易误触
        className={`scroll-none flex overflow-x-auto overscroll-x-contain ${className}`}
      >
        {children}
      </div>

      {/* 左右翻页钮：能滚就常驻；到边缘后隐藏 */}
      <ScrollArrow dir={-1} visible={canLeft} onClick={() => page(-1)} />
      <ScrollArrow dir={1} visible={canRight} onClick={() => page(1)} />
    </div>
  );
}

function ScrollArrow({
  dir,
  visible,
  onClick,
}: {
  dir: -1 | 1;
  visible: boolean;
  onClick: () => void;
}) {
  const Icon = dir === -1 ? ChevronLeftIcon : ChevronRightIcon;
  return (
    <button
      type="button"
      aria-label={dir === -1 ? "向左滚动" : "向右滚动"}
      onClick={onClick}
      // !absolute：.surface-raised 自带 position:relative 且声明在工具类之后，
      // 会盖掉普通 absolute，导致按钮掉出定位流、堆到行底部
      className={`surface-raised !absolute top-[38%] z-10 flex size-9 -translate-y-1/2 items-center justify-center !rounded-full text-[var(--text)] transition-all duration-200 hover:scale-110 [@media(hover:none)]:hidden ${
        dir === -1 ? "left-2" : "right-2"
      } ${
        visible
          ? "pointer-events-auto opacity-80 group-hover/hscroll:opacity-100"
          : "pointer-events-none opacity-0"
      }`}
    >
      <Icon className="size-4" />
    </button>
  );
}
