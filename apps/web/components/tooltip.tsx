"use client";

import { cloneElement, isValidElement, useEffect, useRef, useState, type Ref } from "react";
import {
  arrow,
  autoUpdate,
  flip,
  FloatingArrow,
  FloatingPortal,
  offset,
  safePolygon,
  shift,
  useDismiss,
  useFloating,
  useClick,
  useFocus,
  useHover,
  useInteractions,
  useMergeRefs,
  useRole,
  useTransitionStyles,
  type Placement,
} from "@floating-ui/react";

/**
 * 通用悬浮提示（Floating UI）：替代原生 title 的富内容 tooltip。
 *
 * 设计取舍：
 * - 触发器用 cloneElement 注入 ref/事件，不额外包一层 DOM——组头按钮是 w-full
 *   布局，包一层 span 会破坏宽度；子元素必须能接收 ref（原生元素即可）。
 * - 悬停 250ms 延迟出现（扫过列表不闪提示），聚焦立即出现（键盘用户无需等待）；
 *   safePolygon 让指针能移进浮层复制内容（沿三角安全区移动不判定为离开）。
 * - FloatingPortal 渲染到 body：不受列表容器 overflow/backdrop-filter 裁剪。
 * - flip/shift 保证靠近视口边缘时自动换边、收敛在可视区内。
 */
export function Tooltip({
  content,
  children,
  placement = "top",
  maxWidth = 380,
  openOnClick = false,
  disabled = false,
  dismissOnReferencePress = false,
}: {
  content: React.ReactNode;
  /** 触发元素：必须是可接收 ref 的单个元素（原生标签即可） */
  children: React.ReactElement<Record<string, unknown>>;
  placement?: Placement;
  maxWidth?: number;
  /** 触屏或需显式点按的提示可开启；默认仍只响应悬停与键盘聚焦。 */
  openOnClick?: boolean;
  /** 临时停用提示；用于详情弹窗打开时保证两种浮层互斥。 */
  disabled?: boolean;
  /** 按下触发器就关闭提示，避免可点击按钮同时保留 Tooltip。 */
  dismissOnReferencePress?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const arrowRef = useRef<SVGSVGElement | null>(null);
  const effectiveOpen = open && !disabled;

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const { refs, floatingStyles, context } = useFloating({
    open: effectiveOpen,
    onOpenChange: (nextOpen) => setOpen(disabled ? false : nextOpen),
    placement,
    whileElementsMounted: autoUpdate,
    middleware: [offset(8), flip({ padding: 8 }), shift({ padding: 8 }), arrow({ element: arrowRef })],
  });

  const { getReferenceProps, getFloatingProps } = useInteractions([
    useHover(context, {
      enabled: !disabled,
      move: false,
      delay: { open: 250 },
      handleClose: safePolygon(),
    }),
    useClick(context, { enabled: !disabled && openOnClick }),
    useFocus(context, { enabled: !disabled }),
    useDismiss(context, {
      enabled: !disabled,
      referencePress: dismissOnReferencePress,
      referencePressEvent: "pointerdown",
    }),
    useRole(context, { enabled: !disabled, role: "tooltip" }),
  ]);

  const { isMounted, styles: transitionStyles } = useTransitionStyles(context, {
    duration: { open: 150, close: 100 },
    initial: { opacity: 0, transform: "translateY(3px) scale(0.98)" },
  });

  const childRef = isValidElement(children)
    ? (children.props as { ref?: Ref<Element> }).ref
    : undefined;
  const referenceRef = useMergeRefs<Element>([refs.setReference, childRef]);
  if (!isValidElement(children)) return children;

  return (
    <>
      {cloneElement(children, getReferenceProps({ ...children.props, ref: referenceRef }))}
      {isMounted && (
        <FloatingPortal>
          <div
            ref={refs.setFloating}
            style={{ ...floatingStyles, maxWidth, zIndex: 60 }}
            {...getFloatingProps()}
          >
            <div
              style={transitionStyles}
              // tooltip-bubble：主题钩子——Netflix 主题在 globals.css 里换成
              // 实色 #181818 气泡（毛玻璃是银玻璃语言，见 docs/design/web-themes.md §5.7）。
              className="tooltip-bubble select-text rounded-xl border border-white/[0.12] bg-[rgba(16,18,26,0.97)] px-3.5 py-2.5 text-sub leading-relaxed text-[var(--text)] shadow-2xl backdrop-blur-2xl"
            >
              <FloatingArrow
                ref={arrowRef}
                context={context}
                width={12}
                height={6}
                tipRadius={2}
                fill="rgba(16,18,26,0.97)"
                strokeWidth={1}
                stroke="rgba(243,245,249,0.12)"
              />
              {content}
            </div>
          </div>
        </FloatingPortal>
      )}
    </>
  );
}
