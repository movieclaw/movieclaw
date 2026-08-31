"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { AvatarBadge } from "@/components/avatar-badge";
import { GearIcon, LogoutIcon } from "@/components/icons";
import { logout } from "@/lib/api/auth";
import { clearBackdropCache } from "@/lib/backdrop-cache";
import { clearUiPrefsCache } from "@/lib/ui-prefs-cache";
import { roleLabel } from "@/lib/permissions";
import { useSession } from "@/lib/session";

/**
 * 左下角的用户信息入口。
 * 点击后向上弹出菜单（CSS 玻璃，不占 WebGL 上下文）：设置 / 退出登录。
 * 「设置」切换到设置模式。
 */
export interface UserMenuProps {
  onOpenSettings: (sectionId?: string) => void;
  /** 侧栏折叠形态：触发按钮只留头像；弹出菜单比窄栏宽，须 Portal 到 body 展示 */
  collapsed?: boolean;
}

export function UserMenu({ onOpenSettings, collapsed = false }: UserMenuProps) {
  const { session } = useSession();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  // 折叠态菜单的 fixed 定位（打开瞬间按触发按钮位置计算一次）。
  // 之所以 Portal + fixed：菜单(240px)比折叠窄栏宽，留在面板内会被玻璃面板的
  // overflow:hidden 裁掉、且会压在主区面板的层叠上下文之下。
  const [menuPos, setMenuPos] = useState<{ left: number; bottom: number } | null>(null);

  // 点击外部或按 Esc 关闭菜单（Portal 出去的菜单不在 rootRef 内，需单独判断）
  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      const t = e.target as Node;
      if (rootRef.current?.contains(t) || menuRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const go = (sectionId?: string) => {
    setOpen(false);
    onOpenSettings(sectionId);
  };

  /** 退出登录：请求后端清 Cookie，随后整页跳转登录页（重置全部前端状态）。 */
  const handleLogout = async () => {
    setOpen(false);
    try {
      await logout();
    } catch {
      // 即使请求失败（如网络断开），也照常跳登录页；会话在后端仍会自然过期
    }
    clearBackdropCache();
    clearUiPrefsCache();
    window.location.href = "/login";
  };

  /** 打开菜单；折叠态下先按触发按钮的当前位置算好 fixed 坐标 */
  const toggleOpen = () => {
    if (!open && collapsed && rootRef.current) {
      const rect = rootRef.current.getBoundingClientRect();
      setMenuPos({ left: rect.left, bottom: window.innerHeight - rect.top + 10 });
    }
    setOpen((v) => !v);
  };

  const menu = open && (
    <div
      ref={menuRef}
      className={`menu-surface origin-bottom overflow-hidden p-1.5 ${
        collapsed ? "w-60" : "absolute bottom-[calc(100%+10px)] left-0 right-0 z-30"
      }`}
      style={
        // 全部内联：.menu-surface 自带 position:relative，须整体覆盖掉
        collapsed && menuPos
          ? { position: "fixed", left: menuPos.left, bottom: menuPos.bottom, zIndex: 50 }
          : undefined
      }
    >
      <div className="flex items-center gap-3 px-2.5 pb-2.5 pt-2">
        <AvatarBadge
          nickname={session.nickname}
          avatarUrl={session.avatar_url}
          className="size-9 text-ui"
        />
        <div className="min-w-0">
          <p className="truncate text-ui font-semibold text-[var(--text)]">{session.nickname}</p>
          <p className="truncate text-caption text-[var(--text-muted)]">@{session.username}</p>
        </div>
      </div>
      <div className="my-1" />
      <MenuItem
        icon={<GearIcon className="size-[18px] max-md:size-[22px]" />}
        label="设置"
        onClick={() => go()}
      />
      <div className="my-1" />
      <MenuItem
        icon={<LogoutIcon className="size-[18px] max-md:size-[22px]" />}
        label="退出登录"
        danger
        onClick={handleLogout}
      />
    </div>
  );

  return (
    <div ref={rootRef} className="relative">
      {/* 向上弹出的菜单：展开态在面板内绝对定位；折叠态 Portal 到 body（见 menuPos 注释） */}
      {menu && (collapsed ? createPortal(menu, document.body) : menu)}

      {/* 用户信息触发按钮 */}
      <button
        type="button"
        onClick={toggleOpen}
        data-active={open}
        title={collapsed ? session.nickname : undefined}
        className={`glass-row py-2 ${collapsed ? "justify-center px-0" : "px-2"}`}
      >
        <AvatarBadge
          nickname={session.nickname}
          avatarUrl={session.avatar_url}
          className="size-9 text-ui"
        />
        {!collapsed && (
          <>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-ui font-semibold text-[var(--text)]">
                {session.nickname}
              </span>
              <span className="block truncate text-caption text-[var(--text-muted)]">
                {roleLabel(session)}
              </span>
            </span>
            <svg
              viewBox="0 0 20 20"
              className={`size-4 shrink-0 text-[var(--text-faint)] transition-transform ${open ? "rotate-180" : ""}`}
              fill="none"
              stroke="currentColor"
              strokeWidth={1.8}
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="m6 8 4-4 4 4M6 12l4 4 4-4" />
            </svg>
          </>
        )}
      </button>
    </div>
  );
}

function MenuItem({
  icon,
  label,
  onClick,
  danger = false,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`glass-row px-2.5 py-2 text-ui font-medium max-md:py-2.5 ${
        danger ? "!text-[var(--danger)] hover:!bg-[rgba(255,107,107,0.12)]" : ""
      }`}
    >
      <span className="shrink-0 opacity-80">{icon}</span>
      <span className="flex-1">{label}</span>
    </button>
  );
}
