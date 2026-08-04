"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

/**
 * 海报图片底座：全站所有海报/封面 <img> 的统一实现。
 *
 * 统一收口三件事，调用方不必各自重复：
 *   1. loading="lazy" —— 海报墙一页几十张图，懒加载是默认约定；
 *   2. referrerPolicy="no-referrer" —— 豆瓣等图床按 Referer 拒绝外链，不带即可正常加载；
 *   3. 加载失败回退 —— 防盗链 / 图床失效时渲染深色占位（或调用方自定义的 fallback），
 *      卡片不塌陷、不出裂图图标。
 *
 * 定位、圆角、hover 缩放等布局差异全部通过 className 由调用方传入；
 * 占位符会套用同一份 className，保证与图片占据完全相同的盒子。
 * 组件只负责「一张海报图」本身，卡片语义（徽章、渐变信息层、点击行为）留给上层。
 */
export function PosterImage({
  src,
  alt,
  className = "",
  fallback,
}: {
  /** 图片地址；为空时直接渲染占位 */
  src?: string | null;
  alt: string;
  /** 应用在 <img> 与默认占位上的布局类（定位 / 尺寸 / 过渡等） */
  className?: string;
  /** 自定义占位内容；不传则渲染深色渐变底 */
  fallback?: ReactNode;
}) {
  const [broken, setBroken] = useState(false);
  // 懒加载失灵兜底：海报常被包在 content-visibility:auto 的格子/行里（海报墙、
  // 横滚行），Chromium 对「被跳过渲染的子树」里的懒加载图片不做视口相交判定，
  // 而页面打开瞬间所有格子都处于跳过态，首屏图片可能永远不发请求——表现为
  // 海报空着、鼠标划过强制重算样式才加载。对策：确实在视口附近的图片把
  // loading 翻成 eager 强制加载；屏外图片不受影响，仍走原生懒加载。
  //
  // 翻 eager 必须等格子先解除跳过态（双 rAF 延后到第 2 帧，仍在跳过态就再等）：
  // 实测图片若在所在子树被跳过渲染期间加载完成，加载触发的重绘失效会被
  // Chromium 丢弃，格子恢复渲染后画的仍是占位，事后无法从脚本补救（重设 src、
  // 临时关 content-visibility 都救不回，只有 hover 这类逐格强制重算才行）；
  // 局域网/缓存取图往往快过首帧，挂载时就发请求必然有一批撞上这个窗口。
  // 先等格子渲染出来再取图，加载完成就走普通的失效→绘制路径。rAF 在后台
  // 标签页天然挂起，也顺带保证了「后台打开、切回前台才开始取图」的正确时序。
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [eager, setEager] = useState(false);
  useEffect(() => {
    let raf = 0;
    const kickOrRetry = () => {
      const img = imgRef.current;
      if (!img || img.complete) return;
      const rect = img.getBoundingClientRect();
      const margin = 400;
      if (
        rect.width <= 0 ||
        rect.top >= window.innerHeight + margin ||
        rect.bottom <= -margin ||
        rect.left >= window.innerWidth + margin ||
        rect.right <= -margin
      ) {
        return;
      }
      try {
        if (!img.checkVisibility({ contentVisibilityAuto: true })) {
          schedule(); // 格子还在跳过态，等下一个双帧再看
          return;
        }
      } catch {
        // 老浏览器没有 checkVisibility：直接翻 eager，它们也没有这条缺陷路径
      }
      setEager(true);
    };
    const schedule = () => {
      raf = requestAnimationFrame(() => {
        raf = requestAnimationFrame(kickOrRetry);
      });
    };
    schedule();
    return () => cancelAnimationFrame(raf);
  }, [src]);
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
    <img
      ref={imgRef}
      src={src}
      alt={alt}
      loading={eager ? "eager" : "lazy"}
      // 必须同步解码：async 解码的「完成→重绘」通知在 content-visibility 格子里
      // 会被 Chromium 丢弃，海报停在占位直到 hover 强制重排（实测定位的根因）。
      // sync 让任何一次绘制都当场解码带图，不依赖那条会丢的通知；单张海报解码
      // 只有几毫秒，且只有真正被绘制的格子才会解码，无首屏卡顿之虞。
      decoding="sync"
      referrerPolicy="no-referrer"
      onError={() => setBroken(true)}
      className={`bg-[#141824] object-cover ${className}`}
    />
  );
}
