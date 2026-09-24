"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { ChatIcon, CopyIcon, MoreIcon, PencilIcon, TrashIcon } from "@/components/icons";

/**
 * 会话行尾的「⋯」操作菜单：在新会话中继续 / 复制会话 ID / 重命名 / 删除会话。
 *
 * 从侧栏会话行（sidebar.tsx 的 RunRow）同一套交互抽出来给「更多」页的最近会话
 * 复用：抽屉侧栏在手机上退役后，会话列表搬到了「更多」页，这些操作跟着一起
 * 搬过来，否则手机上就没有改名和删除的入口。菜单 Portal 到 body：所在卡片
 * 有 overflow 裁剪，行内弹层会被切掉。点击外部、Esc、滚动都关闭。
 *
 * 侧栏的 RunRow 仍保留它自己那份内联实现（它的触发键要与行尾时间标签互相让位，
 * 开合状态得留在行里），两处菜单项须保持一致。
 */
export function ConversationMenu({
  onFork,
  onCopyId,
  onRename,
  onDelete,
  triggerClassName = "",
  iconClassName = "size-5",
}: {
  onFork: () => void;
  onCopyId: () => void;
  onRename: () => void;
  onDelete: () => void;
  /** 触发键的定位/尺寸由所在行决定（默认只给基础皮肤） */
  triggerClassName?: string;
  iconClassName?: string;
}) {
  // 菜单打开状态即定位坐标（打开瞬间按触发按钮位置计算一次）
  const [menuPos, setMenuPos] = useState<{ left: number; top: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const open = menuPos != null;

  useEffect(() => {
    if (!open) return;
    const close = () => setMenuPos(null);
    const onPointer = (e: MouseEvent) => {
      const target = e.target as Node;
      if (menuRef.current?.contains(target) || moreRef.current?.contains(target)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    // passive：只做关闭动作、不会 preventDefault，别让浏览器为它放弃滚动快路径
    document.addEventListener("scroll", close, { capture: true, passive: true });
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("scroll", close, { capture: true });
    };
  }, [open]);

  const pick = (action: () => void) => {
    setMenuPos(null);
    action();
  };

  return (
    <>
      <button
        ref={moreRef}
        type="button"
        aria-label="会话操作"
        aria-expanded={open}
        data-active={open}
        onClick={(e) => {
          e.stopPropagation();
          if (open) {
            setMenuPos(null);
            return;
          }
          const rect = e.currentTarget.getBoundingClientRect();
          // 菜单宽 176px（w-44），右缘与触发键右缘对齐；窄屏上不会越出左边
          setMenuPos({ left: Math.max(8, rect.right - 176), top: rect.bottom + 6 });
        }}
        className={`glass-row touch-target justify-center !rounded-md !p-0 ${triggerClassName}`}
      >
        <MoreIcon className={iconClassName} />
      </button>

      {open &&
        createPortal(
          <div
            ref={menuRef}
            className="menu-surface w-44 overflow-hidden p-1.5"
            // z 取菜单档 70：Portal 到 body 后与全站浮层同层比较，须压过 60 档的
            // 全屏面板（撰写面板），否则会被盖住、点 ⋯ 毫无反应
            style={{ position: "fixed", left: menuPos.left, top: menuPos.top, zIndex: 70 }}
          >
            <button
              type="button"
              onClick={() => pick(onFork)}
              className="glass-row px-2.5 py-2 text-ui font-medium max-md:py-2.5"
            >
              <ChatIcon className="size-4 shrink-0 opacity-80 max-md:size-5" />
              <span className="flex-1">在新会话中继续</span>
            </button>
            <button
              type="button"
              onClick={() => pick(onCopyId)}
              className="glass-row px-2.5 py-2 text-ui font-medium max-md:py-2.5"
            >
              <CopyIcon className="size-4 shrink-0 opacity-80 max-md:size-5" />
              <span className="flex-1">复制会话 ID</span>
            </button>
            <button
              type="button"
              onClick={() => pick(onRename)}
              className="glass-row px-2.5 py-2 text-ui font-medium max-md:py-2.5"
            >
              <PencilIcon className="size-4 shrink-0 opacity-80 max-md:size-5" />
              <span className="flex-1">重命名</span>
            </button>
            <button
              type="button"
              onClick={() => pick(onDelete)}
              className="glass-row px-2.5 py-2 text-ui font-medium !text-[var(--danger)] hover:!bg-[rgba(255,107,107,0.12)] max-md:py-2.5"
            >
              <TrashIcon className="size-4 shrink-0 opacity-80 max-md:size-5" />
              <span className="flex-1">删除会话</span>
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}
