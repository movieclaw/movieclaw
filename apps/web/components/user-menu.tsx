"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { AvatarBadge } from "@/components/avatar-badge";
import { GearIcon, LogoutIcon, PlusIcon, XIcon } from "@/components/icons";
import {
  MAX_SAVED_ACCOUNTS,
  listAccounts,
  logout,
  removeAccount,
  switchAccount,
  type AccountView,
} from "@/lib/api/auth";
import { clearBackdropCache } from "@/lib/backdrop-cache";
import { clearUiPrefsCache } from "@/lib/ui-prefs-cache";
import { roleLabel } from "@/lib/permissions";
import { useSession } from "@/lib/session";

/**
 * 左下角的用户信息入口。
 * 点击后向上弹出菜单（CSS 玻璃，不占 WebGL 上下文）：其他已登录账号（点击
 * 即切换，不用再输密码）/ 添加账号 / 设置 / 退出登录。「设置」切换到设置模式。
 *
 * 多账号（docs/design/account-switching.md）：凭证全在 HttpOnly Cookie 里，这里
 * 只在菜单打开时拉一次账号列表。切换 / 退出后一律整页跳转，让工作台各 Store、
 * AuthGate 的会话缓存、背景图与界面偏好缓存随页面清零，绝不串到上一个账号的数据。
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
  // 本浏览器已登录的全部账号（激活账号排第一）；菜单打开时拉取，关闭不清空以免闪烁
  const [accounts, setAccounts] = useState<AccountView[]>([]);
  // 切换 / 移除进行中：禁用账号行，避免连点发出两次切换
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    listAccounts()
      .then((list) => {
        if (!cancelled) setAccounts(list);
      })
      .catch(() => {
        // 列表拉不到只是少了切换入口，菜单其余功能照常
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

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

  /** 整页跳转到目标页，先清掉按账号缓存的前端状态。 */
  const reloadTo = (href: string) => {
    clearBackdropCache();
    clearUiPrefsCache();
    window.location.href = href;
  };

  /**
   * 退出登录：只退当前账号。浏览器里还有别的账号时后端自动切过去，整页回首页；
   * 没有了才去登录页。all=true 时退出全部账号。
   */
  const handleLogout = async (all = false) => {
    setOpen(false);
    let next: Awaited<ReturnType<typeof logout>> = null;
    try {
      next = await logout(all);
    } catch {
      // 即使请求失败（如网络断开），也照常跳登录页；会话在后端仍会自然过期
    }
    reloadTo(next ? "/" : "/login");
  };

  /** 切换到另一个已登录账号：后端换激活 Cookie，随后整页回首页。 */
  const handleSwitch = async (account: AccountView) => {
    if (switching) return;
    setSwitching(true);
    try {
      await switchAccount(account.username);
    } catch {
      // 多半是该账号登录态已过期 / 被停用（后端已把它移出列表）：引导重新登录该账号
      reloadTo("/login?add=1");
      return;
    }
    reloadTo("/");
  };

  /** 从本浏览器移除一个非当前账号（不影响当前会话）。 */
  const handleRemove = async (e: React.MouseEvent, account: AccountView) => {
    e.stopPropagation();
    if (switching) return;
    setSwitching(true);
    try {
      await removeAccount(account.username);
      setAccounts((list) => list.filter((a) => a.username !== account.username));
    } catch {
      // 移除失败（多半已自然失效）也刷新一次列表，保持与后端一致
      listAccounts().then(setAccounts).catch(() => {});
    } finally {
      setSwitching(false);
    }
  };

  const otherAccounts = accounts.filter((a) => !a.active);
  const canAddAccount = accounts.length < MAX_SAVED_ACCOUNTS;

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
      {otherAccounts.map((account) => (
        <AccountRow
          key={account.username}
          account={account}
          disabled={switching}
          onSwitch={() => handleSwitch(account)}
          onRemove={(e) => handleRemove(e, account)}
        />
      ))}
      {canAddAccount && (
        <MenuItem
          icon={<PlusIcon className="size-[18px] max-md:size-[22px]" />}
          label="添加账号"
          onClick={() => {
            setOpen(false);
            reloadTo("/login?add=1");
          }}
        />
      )}
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
        onClick={() => handleLogout(false)}
      />
      {otherAccounts.length > 0 && (
        <MenuItem
          icon={<LogoutIcon className="size-[18px] max-md:size-[22px]" />}
          label="退出全部账号"
          danger
          onClick={() => handleLogout(true)}
        />
      )}
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

/**
 * 菜单里的一个"其他账号"行：整行点击切换，行尾的 × 只从本浏览器移除该账号
 * （不是停用账号，只是这台设备不再记住它的登录态）。
 */
function AccountRow({
  account,
  disabled,
  onSwitch,
  onRemove,
}: {
  account: AccountView;
  disabled: boolean;
  onSwitch: () => void;
  onRemove: (e: React.MouseEvent) => void;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      aria-disabled={disabled}
      onClick={() => !disabled && onSwitch()}
      onKeyDown={(e) => {
        if (!disabled && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          onSwitch();
        }
      }}
      title={`切换到 ${account.nickname}`}
      className={`glass-row cursor-pointer px-2.5 py-2 text-ui ${disabled ? "opacity-50" : ""}`}
    >
      <AvatarBadge
        nickname={account.nickname}
        avatarUrl={account.avatar_url}
        className="size-7 text-caption"
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium text-[var(--text)]">{account.nickname}</span>
        <span className="block truncate text-caption text-[var(--text-muted)]">
          {account.role === "admin" ? "超级管理员" : "成员"}
        </span>
      </span>
      <button
        type="button"
        onClick={onRemove}
        disabled={disabled}
        aria-label={`从本浏览器移除 ${account.nickname}`}
        title="从本浏览器移除"
        className="shrink-0 rounded-full p-1 text-[var(--text-faint)] hover:bg-white/10 hover:text-[var(--text)]"
      >
        <XIcon className="size-3.5" />
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
