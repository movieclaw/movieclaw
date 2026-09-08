"use client";

import { useCallback, useLayoutEffect, useState } from "react";

/**
 * 列表页的滚动位置只在当前浏览会话内保留，不写入 localStorage：
 * 位置属于一次导航上下文，刷新页面后重新从列表顶部开始更符合预期。
 */
interface ScrollAnchor {
  id: string;
  /** 锚点顶边相对滚动容器可视区顶边的偏移。 */
  offset: number;
}

interface ScrollPosition {
  top: number;
  anchor?: ScrollAnchor;
}

const positions = new Map<string, ScrollPosition>();
const MAX_SCROLL_POSITIONS = 100;
const MAX_RESTORE_MS = 5000;
const MAX_ANCHOR_SETTLE_MS = 800;

function rememberScrollPosition(key: string, position: ScrollPosition) {
  // 位置只服务于当前浏览会话，限制条目数避免用户在大量列表间导航时无限增长。
  positions.delete(key);
  positions.set(key, position);
  while (positions.size > MAX_SCROLL_POSITIONS) {
    const oldest = positions.keys().next().value;
    if (oldest === undefined) break;
    positions.delete(oldest);
  }
}

function isEditableTarget(target: EventTarget | null) {
  if (!(target instanceof Element)) return false;
  return (
    target.matches("input, textarea, select") ||
    target.closest("input, textarea, select, [contenteditable='true']") !== null
  );
}

interface ScrollRestorationOptions {
  /**
   * 列表项的稳定标识属性。传入后优先恢复首个可见项，而不是单纯恢复像素位置。
   * 海报图片与分页窗口会让同一像素位置对应到不同条目，条目锚点才能保持视觉位置。
   */
  anchorAttribute?: string;
  /**
   * 这次挂载要不要自动回位，默认要。
   *
   * 给「回到上次浏览的位置」的胶囊让路：久别回归时那一屏改由胶囊来问
   *（lib/library-wall-recall.ts），此时若还自动回位，人已经在原处了，
   * 胶囊就成了指着脚下的废话。关掉的只是这一次回位，位置照记不误。
   */
  restore?: boolean;
}

/**
 * 记住一个内部滚动容器的位置，并在返回该列表时恢复。
 *
 * 列表数据可能在挂载后异步到达，第一次设置 scrollTop 可能被浏览器
 * 截断到当前内容高度。因此恢复过程会在 DOM 变化和下一帧继续尝试，
 * 直到内容足够或超过短暂的等待上限。
 */
export function useScrollRestoration(key: string, options: ScrollRestorationOptions = {}) {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const ref = useCallback((node: HTMLDivElement | null) => setElement(node), []);
  const anchorAttribute = options.anchorAttribute;
  const shouldRestore = options.restore ?? true;

  useLayoutEffect(() => {
    if (!element) return;

    let frame = 0;
    // 新容器挂载时，Next 可能先触发一次归零滚动。恢复目标必须在此刻
    // 固定下来，不能再从 positions 读取，否则这次初始化事件会把目标覆盖成 0。
    let pendingPosition = shouldRestore ? positions.get(key) : undefined;
    let anchorSettleDeadline = 0;
    const deadline = performance.now() + MAX_RESTORE_MS;
    const capturePosition = (): ScrollPosition => {
      const position: ScrollPosition = { top: element.scrollTop };
      if (!anchorAttribute) return position;

      const containerRect = element.getBoundingClientRect();
      const anchor = Array.from(
        element.querySelectorAll<HTMLElement>(`[${anchorAttribute}]`),
      ).find((candidate) => {
        const rect = candidate.getBoundingClientRect();
        return rect.bottom > containerRect.top && rect.top < containerRect.bottom;
      });
      const id = anchor?.getAttribute(anchorAttribute);
      if (anchor && id) {
        position.anchor = {
          id,
          offset: anchor.getBoundingClientRect().top - containerRect.top,
        };
      }
      return position;
    };
    const restore = () => {
      frame = 0;
      const saved = pendingPosition;
      if (saved == null) return;
      const savedAnchor = saved.anchor;
      if (savedAnchor && anchorAttribute) {
        const anchor = Array.from(
          element.querySelectorAll<HTMLElement>(`[${anchorAttribute}]`),
        ).find((candidate) => candidate.getAttribute(anchorAttribute) === savedAnchor.id);
        if (anchor) {
          const containerTop = element.getBoundingClientRect().top;
          element.scrollTop += anchor.getBoundingClientRect().top - containerTop - savedAnchor.offset;
          // content-visibility 与图片尺寸会在挂载后的短暂窗口内补齐。持续复核
          // 锚点，防止高度修正把用户带到相邻的一行海报。
          if (!anchorSettleDeadline) {
            anchorSettleDeadline = performance.now() + MAX_ANCHOR_SETTLE_MS;
          }
          if (performance.now() < anchorSettleDeadline) {
            frame = requestAnimationFrame(restore);
          } else {
            pendingPosition = undefined;
          }
          return;
        }
      }
      // 内容还没撑到目标高度时先不写入，避免浏览器把目标截断后触发
      // scroll 监听，误把截断值保存成新的位置。
      const maxScrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
      if (saved.top > maxScrollTop) {
        if (performance.now() < deadline) {
          frame = requestAnimationFrame(restore);
        } else {
          // 数据最终不足以撑到旧位置时，放弃旧值，让之后的真实滚动继续记忆。
          pendingPosition = undefined;
          rememberScrollPosition(key, capturePosition());
        }
        return;
      }
      element.scrollTop = saved.top;
      pendingPosition = undefined;
    };
    const scheduleRestore = () => {
      if (!frame) frame = requestAnimationFrame(restore);
    };
    const cancelRestore = () => {
      if (pendingPosition == null) return;
      pendingPosition = undefined;
      anchorSettleDeadline = 0;
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
    };
    const remember = () => {
      // 等待异步列表数据期间，忽略框架初始化的归零滚动，保住本次返回目标。
      if (pendingPosition != null) return;
      rememberScrollPosition(key, capturePosition());
    };
    const resizes =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleRestore);
    const observeResizedChildren = () => {
      if (!resizes) return;
      resizes.observe(element);
      for (const child of element.children) resizes.observe(child);
    };
    const mutations = new MutationObserver((records) => {
      if (resizes) {
        for (const record of records) {
          for (const node of record.addedNodes) {
            if (node instanceof Element && node.parentElement === element) {
              resizes.observe(node);
            }
          }
        }
      }
      scheduleRestore();
    });
    const handleKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target)) return;
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) {
        cancelRestore();
      }
    };

    element.addEventListener("scroll", remember, { passive: true });
    element.addEventListener("wheel", cancelRestore, { passive: true });
    element.addEventListener("touchstart", cancelRestore, { passive: true });
    element.addEventListener("pointerdown", cancelRestore, { passive: true });
    window.addEventListener("keydown", handleKeyDown, true);
    mutations.observe(element, { childList: true, subtree: true });
    observeResizedChildren();
    scheduleRestore();

    return () => {
      if (frame) cancelAnimationFrame(frame);
      element.removeEventListener("scroll", remember);
      element.removeEventListener("wheel", cancelRestore);
      element.removeEventListener("touchstart", cancelRestore);
      element.removeEventListener("pointerdown", cancelRestore);
      window.removeEventListener("keydown", handleKeyDown, true);
      mutations.disconnect();
      resizes?.disconnect();
      // 路由跳转期间框架可能先把当前容器复位到顶部，再卸载本页；这里若再
      // 读取 scrollTop 会把已由 scroll 事件保存的真实位置覆盖成 0。
    };
  }, [anchorAttribute, element, key, shouldRestore]);

  return ref;
}
