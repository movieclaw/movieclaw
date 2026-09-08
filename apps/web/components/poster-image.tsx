"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

/**
 * 海报图片底座：全站所有海报/封面 <img> 的统一实现。
 *
 * 统一收口三件事，调用方不必各自重复：
 *   1. loading="lazy" —— 海报墙一页几十张图，懒加载是默认约定；
 *   2. referrerPolicy="no-referrer" —— 豆瓣等图床按 Referer 拒绝外链，不带即可正常加载；
 *   3. 加载失败回退 —— 防盗链 / 图床失效时渲染深色占位（或调用方自定义的 fallback），
 *      卡片不塌陷、不出裂图图标；
 *   4. 等待态占位（``pulseWhileLoading``，按需开）—— 图没到之前盖一层脉冲，
 *      大图墙上滑到哪儿黑一块的观感由它兜住。
 *
 * 定位、圆角、hover 缩放等布局差异全部通过 className 由调用方传入；
 * 占位符会套用同一份 className，保证与图片占据完全相同的盒子。
 * 组件只负责「一张海报图」本身，卡片语义（徽章、渐变信息层、点击行为）留给上层。
 */

/**
 * 懒加载失灵兜底：全站共用的一台「快进视口了」观察器。
 *
 * 海报常被包在 ``content-visibility:auto`` 的格子 / 行里（单库海报墙、首页横滚行、
 * 搜索结果行）。Chromium 对「被跳过渲染的子树」里的 ``<img loading="lazy">`` 不做
 * 视口相交判定，而页面打开瞬间所有格子都是跳过态，首屏海报可能永远不发请求
 * ——表现为海报空着、鼠标划过强制重算样式才加载。所以要有人替它判一次。
 *
 * 判的方式很要紧。原先是每张图各自双 rAF 之后读一次 ``getBoundingClientRect()``，
 * 但在跳过态的子树里读几何量会逼浏览器把那棵子树重新布局一遍——**每张图一次
 * 全量布局**。实测 3000 张图触发 2993 次布局、挂载 3.4 秒；换成这里的
 * IntersectionObserver 是 2 次布局、1.5 秒。IO 的相交判定跟着渲染流水线走，
 * 不在脚本里同步求值，因此不逼布局；而且它是持续的，滑到哪儿补到哪儿，
 * 不像 rAF 探测只在挂载时问一次。
 *
 * 提前量 400px 与原先一致。命中即 unobserve——图取过就不必再管。
 */
const NEAR_MARGIN = "400px";
const nearCallbacks = new WeakMap<Element, () => void>();
let nearObserver: IntersectionObserver | null = null;

function observeNearViewport(el: Element, onNear: () => void): (() => void) | undefined {
  if (typeof IntersectionObserver === "undefined") {
    // 没有 IO 的老浏览器：直接取图。它们也没有 content-visibility，不存在这条缺陷路径
    onNear();
    return;
  }
  nearObserver ??= new IntersectionObserver(
    (records) => {
      for (const record of records) {
        if (!record.isIntersecting) continue;
        nearObserver?.unobserve(record.target);
        const callback = nearCallbacks.get(record.target);
        nearCallbacks.delete(record.target);
        callback?.();
      }
    },
    { rootMargin: NEAR_MARGIN },
  );
  nearCallbacks.set(el, onNear);
  nearObserver.observe(el);
  return () => {
    nearObserver?.unobserve(el);
    nearCallbacks.delete(el);
  };
}
export function PosterImage({
  src,
  alt,
  className = "",
  fallback,
  pulseWhileLoading = false,
  preload,
}: {
  /** 图片地址；为空时直接渲染占位 */
  src?: string | null;
  alt: string;
  /** 应用在 <img> 与默认占位上的布局类（定位 / 尺寸 / 过渡等） */
  className?: string;
  /** 自定义占位内容；不传则渲染深色渐变底 */
  fallback?: ReactNode;
  /**
   * 图片就位前盖一层脉冲占位（默认不开）。大图墙需要它：瀑布流的瓦片底色是
   * 深色，滑到哪儿黑一块，看着像坏了而不是在加载。占位与图片是重叠的两层，
   * 只有 className 本身是绝对定位（absolute inset-0 这类）时才对得上。
   */
  pulseWhileLoading?: boolean;
  /**
   * 取图时机由调用方接管：``true`` 立刻取，``false`` 先不取。
   *
   * **传了它（不论真假），本组件就不再自己判视口**——瀑布流的墙自己就知道
   * 哪几块该挂（photo-wall.tsx 的 useTileWindow 按算好的坐标切窗口），不需要
   * 再逐张观察一遍，连那台共享观察器都省了。
   *
   * 不传（海报墙、横滚行等还带 content-visibility 的调用方）就交给上面那台共享
   * 的 IntersectionObserver：快进视口就翻 eager。
   */
  preload?: boolean;
}) {
  const [broken, setBroken] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [eager, setEager] = useState(false);
  // 换图时复位「已就位」：缓存直出的图 load 事件可能早于本组件挂载，
  // 那种情况下 img.complete 已经是 true，不补这一下占位就撤不掉
  useEffect(() => {
    setLoaded(imgRef.current?.complete ?? false);
  }, [src]);
  useEffect(() => {
    // 调用方接管了取图时机：不必再探测（见 preload 的说明）
    if (preload !== undefined) return;
    const img = imgRef.current;
    if (!img || img.complete) return;
    return observeNearViewport(img, () => setEager(true));
  }, [src, preload]);
  if (!src || broken) {
    return (
      fallback ?? (
        <div
          aria-hidden="true"
          className={`bg-gradient-to-b from-white/[0.05] to-[#141824] ${className}`}
        />
      )
    );
  }
  return (
    <>
      <img
        ref={imgRef}
        src={src}
        alt={alt}
        loading={eager || preload ? "eager" : "lazy"}
        // 必须同步解码：async 解码的「完成→重绘」通知在 content-visibility 格子里
        // 会被 Chromium 丢弃，海报停在占位直到 hover 强制重排（实测定位的根因）。
        // sync 让任何一次绘制都当场解码带图，不依赖那条会丢的通知；单张海报解码
        // 只有几毫秒，且只有真正被绘制的格子才会解码，无首屏卡顿之虞。
        decoding="sync"
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
        onLoad={() => setLoaded(true)}
        className={`bg-[#141824] object-cover ${className}`}
      />
      {/* 脉冲占位盖在图片**之上**：<img> 自带不透明深色底，垫在下面看不见 */}
      {pulseWhileLoading && !loaded && (
        <span
          aria-hidden="true"
          className={`pointer-events-none animate-pulse bg-white/[0.07] motion-reduce:animate-none ${className}`}
        />
      )}
    </>
  );
}
