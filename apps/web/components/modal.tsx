"use client";

/**
 * 通用居中弹窗基座——全局唯一的模态骨架，所有居中卡片弹窗基于它封装。
 *
 * 统一吸收三件容易漏抄/踩坑的事：
 * 1. **createPortal 挂到 document.body**：液态玻璃 UI 里列表行/卡片普遍带
 *    backdrop-filter，而带 backdrop-filter 的祖先会成为 position:fixed 的
 *    包含块——就地渲染的弹窗会被困在触发元素内、被相邻元素遮挡
 *    （搜索结果下载弹窗踩过的坑）。portal 从结构上杜绝这一类 bug。
 * 2. **遮罩 + Esc 关闭 + aria 模态语义**：一处实现，处处一致。
 * 3. **玻璃面板视觉**（圆角/描边/阴影/毛玻璃）收敛为一份，改一处全局生效。
 *
 * 页面弹窗在外层包业务壳（如 download-target-dialog、subscribe-dialog），
 * 需要定制时用 width 换宽度档位、panelClassName 追加面板类（如换限高档位）。
 *
 * **面板高度与滚动由基座兜底**（见下方 SCROLL_CLS）：面板恒为「限高的 flex
 * 列」，children 落在一个自动滚动区里。调用方什么都不做，内容再长也滚得到
 * 底部按钮；想要「头部与底栏常驻、只有中间滚」，把 children 写成三个兄弟
 * 节点、中间那个加 "min-h-0 flex-1 overflow-y-auto" 即可（本仓库既有的
 * reidentify-dialog / notice-center 就是这个写法）。
 *
 * 嵌套弹窗（弹窗内再开弹窗，如表单里的目录选择器）：上层置 raised 抬高
 * z 层级；上层若需拦截 Esc（如输入态只退输入不关弹窗），自行在 capture
 * 阶段监听并 stopPropagation，本组件的冒泡阶段监听即不会触发。
 * 置 topmost 的弹窗（压在别的弹窗或灯箱之上）本组件自己就在 capture 阶段
 * 监听并掐断传播——一次 Esc 只关最上面那层，身下那层留在原地。
 */

import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { softKeyboardPossible } from "@/lib/soft-keyboard";

/**
 * 软键盘对视口底部的遮挡高度（px）。
 *
 * iOS（尤其 PWA standalone）弹出键盘时**布局视口不变、可视视口变矮**：
 * fixed 定位仍按布局视口排，贴底的 bottom sheet 和居中弹窗的输入框会沉到
 * 键盘底下。VisualViewport API 给出真实可见区域，据此算出底部被遮挡量，
 * 弹窗容器把 bottom 抬高同样的距离即可始终落在可见区内。
 * 桌面端与键盘收起时恒为 0，行为不变；不支持该 API 的环境静默退化。
 *
 * 只在焦点确实落在可输入元素上时才认这份遮挡（见 lib/soft-keyboard.ts）：
 * iOS 收起键盘后可视视口常停在变矮的状态不恢复，照单全收会把弹窗凭空抬起
 * ——底部抽屉比容器还高，从屏幕上沿溢出，标题与关闭按钮跑到状态栏外面。
 * focusout 同样重算一次，弹窗里的输入框一失焦，抽屉立刻落回屏幕底边。
 */
function useKeyboardInset(active: boolean): number {
  const [inset, setInset] = useState(0);
  useEffect(() => {
    if (!active) return;
    const vv = window.visualViewport;
    if (!vv) return;
    const update = () => {
      const occluded = softKeyboardPossible() ? window.innerHeight - vv.height - vv.offsetTop : 0;
      setInset(Math.max(0, Math.round(occluded)));
    };
    // 推迟一拍等焦点落定：focusout 触发时 activeElement 还没换过去
    const onFocusOut = () => window.setTimeout(update, 0);
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    window.addEventListener("focusout", onFocusOut);
    return () => {
      setInset(0);
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
      window.removeEventListener("focusout", onFocusOut);
    };
  }, [active]);
  return active ? inset : 0;
}

/** 面板宽度档位：表单类弹窗 md；内容较多的 lg；预览/清单类 2xl；
 *  full 为沉浸查看类（日志全屏等）铺满视口，高度由调用方经 panelClassName 撑满。 */
const WIDTH_CLS = {
  md: "max-w-md",
  lg: "max-w-lg",
  "2xl": "max-w-2xl",
  full: "max-w-none",
} as const;

/**
 * children 的容器：一个「限高的 flex 列 + 自动滚动」的中间层。
 *
 * 它同时伺候两种 children，靠的是 flex 列里 min-height:auto 的默认行为：
 * - **不分段的弹窗**（一个 `<div class="space-y-4 p-6">` 包住全部内容）：
 *   该 div 是本容器唯一的 flex 项，自动最小尺寸 = 内容高，压不扁，于是
 *   超长部分由本容器滚出滚动条——不会再被面板 overflow-hidden 裁掉、
 *   底部按钮也不会消失在屏幕外（移动端 items-end 时溢出方向朝**上**，
 *   裁掉的正是头部与底部按钮，用户既看不到也滚不动）。
 * - **头/身/底三段的弹窗**：中间那段自己带 overflow-y-auto（自动最小尺寸
 *   随之归零、可被压缩），头尾按内容高占位，本容器就永远不需要滚动——
 *   等于结构透明，头部与底栏照旧常驻。
 *
 * overscroll-contain：滚到尽头不把滚动传给身后的页面（iOS 上尤其明显）。
 */
const SCROLL_CLS = "flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain scroll-thin";

export function Modal({
  open,
  onClose,
  label,
  width = "md",
  raised = false,
  topmost = false,
  panelClassName = "",
  children,
}: {
  /** false 时不渲染任何内容 */
  open: boolean;
  /** 点遮罩 / 按 Esc 触发 */
  onClose: () => void;
  /** 弹窗的无障碍名称（aria-label） */
  label: string;
  width?: keyof typeof WIDTH_CLS;
  /** 叠在其他弹窗之上时置 true（z-60 > 普通弹窗的 z-50） */
  raised?: boolean;
  /** 全站最顶层（z-90 > 搜索面板的 z-80）：仅供确认/输入弹窗（feedback.tsx）——
   *  它们可能从任何弹层（含搜索面板）内被触发，必须压过一切 */
  topmost?: boolean;
  /** 追加到玻璃面板容器的类（定制布局，如 flex 限高列布局） */
  panelClassName?: string;
  children: ReactNode;
}) {
  // Esc 关闭（冒泡阶段，可被上层弹窗的 capture 监听拦截，见文件头注释）。
  // topmost 弹窗压在别的浮层之上（弹窗里的二次确认、灯箱里点开的保存位置弹窗），
  // 它就该独占这一下 Esc：改在 capture 阶段监听并掐断传播，否则身下那层
  // （同样在 window 上听 Esc 的弹窗或灯箱）会被同一次按键一并关掉。
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (topmost) e.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, topmost);
    return () => window.removeEventListener("keydown", onKey, topmost);
  }, [open, onClose, topmost]);

  // 软键盘遮挡高度：>0 时把容器 bottom 抬到键盘之上（iOS PWA 输入弹窗的救命绳）
  const keyboardInset = useKeyboardInset(open);

  // 桌面端默认限高到容器（= 视口减去外层 p-6），超出部分交给滚动区。
  // 调用方自带 max-h/h 档位时让位：同为 max-height 的两个类谁生效取决于
  // 生成 CSS 的先后，不确定；干脆不叠加，调用方的意图优先。
  const defaultMaxH = /(^|[\s!:])(max-)?h-/.test(panelClassName) ? "" : "max-h-full";

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    // 移动端改为「底部抽屉（bottom sheet）」：面板贴住屏幕下沿、左右满宽、
    // 只有上方两角圆。这是手机上模态的既定语言——出现位置靠近拇指、
    // 内容宽度不被 max-w 白白挤掉，长表单也不会被吊在屏幕正中上下都留空。
    // 底部内边距吃安全区，最后一颗按钮不会压在 Home 指示条上。
    // bottom 越出视口 --vp-overshoot：iOS 独立 App 的视口比屏幕矮一截（见
    // globals.css），不越出的话遮罩在屏幕底部留一条没压暗的缝、移动端 bottom
    // sheet 也会悬在物理底边上方。面板内容用加大的 pb 留在视口内（见下方）。
    // 面板另有 max-md:!max-h-full 夹住高度：键盘抬起容器底边后容器会变矮，
    // 调用方按视口给的 max-h-[N vh] 就可能超过容器，而 items-end 的溢出方向
    // 是**上**，头部与关闭按钮会被顶出屏幕且无法滚回（! 压过调用方的 max-h）。
    <div
      className={`fixed inset-0 [bottom:calc(-1*var(--vp-overshoot))] ${topmost ? "z-[90]" : raised ? "z-[60]" : "z-50"} flex items-center justify-center p-6 max-md:items-end max-md:p-0`}
      // 键盘弹出时容器底边抬到键盘上沿（覆盖 className 里的 overshoot 负值）：
      // bottom sheet 随之整体上移、居中弹窗在剩余可见区内重新居中，输入框不再被遮
      style={keyboardInset > 0 ? { bottom: keyboardInset } : undefined}
      role="dialog"
      aria-modal="true"
      aria-label={label}
      // portal 后 React 合成事件仍沿组件树冒泡——弹窗常由列表行内的按钮触发，
      // 拦掉点击以免误触发触发元素自身的点击行为
      onClick={(e) => e.stopPropagation()}
    >
      <button
        type="button"
        aria-label="关闭"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-black/60 backdrop-blur-sm"
      />
      <div
        className={`relative flex w-full flex-col ${WIDTH_CLS[width]} ${defaultMaxH} overflow-hidden rounded-2xl border border-white/10 bg-[rgba(16,18,26,0.92)] shadow-[0_32px_90px_rgba(0,0,0,0.7)] backdrop-blur-2xl max-md:!max-h-full max-md:!max-w-none max-md:rounded-b-none max-md:border-x-0 max-md:border-b-0 max-md:pb-[calc(var(--safe-bottom)+var(--vp-overshoot))] ${panelClassName}`}
      >
        <div className={SCROLL_CLS}>{children}</div>
      </div>
    </div>,
    document.body,
  );
}
