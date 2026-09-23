"use client";

import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import { NewTask } from "@/components/new-task";

/**
 * 移动端「新会话」撰写面板：从底部升起的模态 sheet，内容就是 NewTask 输入台
 * （docs/design/web-themes-mobile/04-iOS-液态玻璃底栏.md §3.1）。
 *
 * 新会话在手机上不是高频操作，不占底栏页签；入口是顶栏右侧的撰写键与「更多」
 * 里的一行——对齐 iOS 信息 / 邮件的 compose 惯例：点开是盖住底栏的模态面板，
 * 「取消」、点暗处或按住把手往下拖都能收回。发起任务后 NewTask 会跳
 * /sessions/[id]，外壳在路由变化时收起面板。
 *
 * 动效（样式见 globals.css 的 .compose-sheet 组）：
 *   - 打开时给 <html> 打 data-compose-open，底下整个应用退成一张缩小下沉的
 *     圆角卡片（iOS 页面 sheet 的纵深）；
 *   - 拖动：手指位移写进 --sheet-offset（面板跟手）与 --sheet-drag（0~1 的进度，
 *     遮罩与底下卡片按它同步恢复），拖动期间 data-sheet-dragging 关掉过渡；
 *     松手时拖过面板高度的 1/4、或向下甩出（> 0.5px/ms）就收回，否则弹回。
 *     往上拖只给 1/5 的阻尼位移，表达「已经到顶了」。
 *
 * 首次打开后保持挂载（关闭只做位移）：用户写了一半收起、再打开时草稿还在，
 * 与 iOS 撰写面板下滑暂存草稿的行为一致。
 *
 * 挂在 .app-shell 之外（外壳负责）：外壳自己会被缩放后推，fixed 元素挂在里面会被
 * 一起缩放、定位基准也会变。层级沿用原移动端抽屉的档位（遮罩 55 / 面板 60），
 * 低于菜单浮层 70——Composer 的模型、技能菜单能正常弹出。
 */
export function ComposeSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [mounted, setMounted] = useState(open);
  const sheetRef = useRef<HTMLElement>(null);
  const drag = useRef({ id: -1, startY: 0, lastY: 0, lastT: 0, v: 0, offset: 0 });

  useEffect(() => {
    if (open) setMounted(true);
  }, [open]);

  // 底下应用退成卡片的开关挂在 <html> 上：.app-shell 与本组件是兄弟节点，只能靠根属性联动
  useEffect(() => {
    const root = document.documentElement;
    root.toggleAttribute("data-compose-open", open);
    // 重新打开时清掉上一次拖动残留的位移（拖动关闭后 600ms 内再打开的情形）
    if (open) {
      root.style.removeProperty("--sheet-offset");
      root.style.removeProperty("--sheet-drag");
    }
    return () => root.removeAttribute("data-compose-open");
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  /** 写拖动进度：面板位移 px + 0~1 的进度（遮罩与底下卡片据此恢复） */
  const setDrag = (offset: number) => {
    const root = document.documentElement;
    const height = sheetRef.current?.clientHeight || 1;
    drag.current.offset = offset;
    root.style.setProperty("--sheet-offset", `${offset}px`);
    root.style.setProperty("--sheet-drag", String(Math.max(0, Math.min(1, offset / height))));
  };
  const clearDrag = () => {
    const root = document.documentElement;
    root.removeAttribute("data-sheet-dragging");
    root.style.removeProperty("--sheet-offset");
    root.style.removeProperty("--sheet-drag");
  };

  const onHandleDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    // 把手区里的「取消」按钮照常点击，不当作拖动起点
    if ((event.target as HTMLElement).closest("button")) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = {
      id: event.pointerId,
      startY: event.clientY,
      lastY: event.clientY,
      lastT: event.timeStamp,
      v: 0,
      offset: 0,
    };
    document.documentElement.setAttribute("data-sheet-dragging", "");
  };

  const onHandleMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (event.pointerId !== d.id) return;
    const dy = event.clientY - d.startY;
    const dt = Math.max(1, event.timeStamp - d.lastT);
    d.v = d.v * 0.6 + ((event.clientY - d.lastY) / dt) * 0.4;
    d.lastY = event.clientY;
    d.lastT = event.timeStamp;
    setDrag(dy >= 0 ? dy : dy * 0.2);
  };

  const onHandleUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (event.pointerId !== d.id) return;
    d.id = -1;
    const height = sheetRef.current?.clientHeight || 1;
    const dismiss = d.offset > height / 4 || d.v > 0.5;
    // 先恢复过渡，再改目标：面板从手指松开的位置接着动，而不是瞬移
    document.documentElement.removeAttribute("data-sheet-dragging");
    if (dismiss) {
      onClose();
      // 收起过渡从当前位移出发；进度变量在过渡开始后再清，避免卡片先跳回
      window.setTimeout(clearDrag, 600);
    } else {
      setDrag(0);
      window.setTimeout(clearDrag, 600);
    }
  };

  return (
    <>
      <button
        type="button"
        aria-label="关闭新会话"
        tabIndex={-1}
        onClick={onClose}
        className="compose-scrim cursor-default"
        data-open={open}
      />
      <section
        ref={sheetRef}
        role="dialog"
        aria-modal="true"
        aria-label="新会话"
        className="compose-sheet"
        data-open={open}
        inert={!open}
      >
        <div
          className="compose-sheet__handle shrink-0 pb-1"
          onPointerDown={onHandleDown}
          onPointerMove={onHandleMove}
          onPointerUp={onHandleUp}
          onPointerCancel={onHandleUp}
        >
          <div className="mx-auto mt-2 h-[5px] w-9 rounded-full bg-white/25" aria-hidden="true" />
          <div className="grid grid-cols-[1fr_auto_1fr] items-center px-4 pt-1.5">
            <button
              type="button"
              onClick={onClose}
              className="justify-self-start py-2 text-body text-[var(--accent)] active:opacity-60"
            >
              取消
            </button>
            <h2 className="text-body font-semibold text-[var(--text)]">新会话</h2>
            <span />
          </div>
        </div>
        <div className="min-h-0 flex-1">{mounted && <NewTask />}</div>
      </section>
    </>
  );
}
