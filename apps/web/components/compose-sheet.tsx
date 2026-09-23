"use client";

import { useEffect, useState } from "react";

import { NewTask } from "@/components/new-task";

/**
 * 移动端「新会话」撰写面板：从底部升起的模态 sheet，内容就是 NewTask 输入台
 * （docs/design/web-themes-mobile/04-iOS-液态玻璃底栏.md §3.1）。
 *
 * 新会话在手机上不是高频操作，不占底栏页签；入口是顶栏右侧的撰写键与「更多」
 * 里的一行——对齐 iOS 信息 / 邮件的 compose 惯例：点开是盖住底栏的模态面板，
 * 「取消」或点暗处收回。发起任务后 NewTask 会跳 /sessions/[id]，外壳在路由
 * 变化时收起面板。
 *
 * 首次打开后保持挂载（关闭只做位移）：用户写了一半收起、再打开时草稿还在，
 * 与 iOS 撰写面板下滑暂存草稿的行为一致。
 *
 * 挂在 .app-shell 之外（外壳负责）：外壳在命令面板打开时有缩放变换，fixed
 * 元素挂在里面会被一起缩放、定位基准也会变。层级沿用原移动端抽屉的档位
 * （遮罩 55 / 面板 60），低于菜单浮层 70——Composer 的模型、技能菜单能正常弹出。
 */
export function ComposeSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [mounted, setMounted] = useState(open);
  useEffect(() => {
    if (open) setMounted(true);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

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
        role="dialog"
        aria-modal="true"
        aria-label="新会话"
        className="compose-sheet"
        data-open={open}
        inert={!open}
      >
        <div className="mx-auto mt-2 h-[5px] w-9 shrink-0 rounded-full bg-white/25" aria-hidden="true" />
        <div className="grid shrink-0 grid-cols-[1fr_auto_1fr] items-center px-4 pt-1.5">
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
        <div className="min-h-0 flex-1">{mounted && <NewTask />}</div>
      </section>
    </>
  );
}
