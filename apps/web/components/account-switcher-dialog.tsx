"use client";

import { useEffect, useState } from "react";

import { AvatarBadge } from "@/components/avatar-badge";
import { useConfirm } from "@/components/feedback";
import { CheckIcon, PlusIcon, XIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
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
import { accessiblePathFor } from "@/lib/permissions";
import { HttpError } from "@/lib/http";

/**
 * 「切换账号」弹窗（docs/design/account-switching.md §4）。
 *
 * 常规产品的形态：一个弹窗列出本浏览器登录过的全部账号，当前账号打勾，
 * 点其他账号即切换（不用再输密码），底部是「添加账号」与「退出全部账号」。
 * 用户菜单本身只保留一个「切换账号」入口，不在菜单里堆列表。
 *
 * 凭证全在 HttpOnly Cookie 里，这里只拿列表。切换 / 退出后一律整页跳转，
 * 让工作台各 Store、AuthGate 的会话缓存、背景图与界面偏好缓存随页面清零，
 * 绝不串到上一个账号的数据。
 */
export function AccountSwitcherDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const confirm = useConfirm();
  const [accounts, setAccounts] = useState<AccountView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 切换 / 移除 / 退出进行中：禁用全部操作，避免连点发出两次
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    listAccounts()
      .then((list) => {
        if (!cancelled) setAccounts(list);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof HttpError ? e.message : "账号列表加载失败，请稍后重试");
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  /** 整页跳转到目标页，先清掉按账号缓存的前端状态。 */
  const reloadTo = (href: string) => {
    clearBackdropCache();
    clearUiPrefsCache();
    window.location.href = href;
  };

  /** 切换到另一个账号：后端换激活 Cookie，随后整页进该身份能进的页面。 */
  const handleSwitch = async (account: AccountView) => {
    if (busy || account.active) return;
    setBusy(true);
    setError(null);
    try {
      const next = await switchAccount(account.username);
      reloadTo(accessiblePathFor(next, "/"));
    } catch (e) {
      // 多半是该账号登录态已过期 / 被停用（后端已把它移出列表）：刷新列表并提示
      setBusy(false);
      setError(e instanceof HttpError ? e.message : "切换失败，请稍后重试");
      listAccounts().then(setAccounts).catch(() => {});
    }
  };

  /** 从本浏览器移除一个账号（不是停用账号，只是这台设备不再记住它的登录态）。
   *  移除后要重新输密码才能回来，所以先二次确认。 */
  const handleRemove = async (account: AccountView) => {
    if (busy) return;
    const ok = await confirm({
      title: `退出「${account.nickname}」？`,
      description: account.active
        ? "这是当前账号。退出后本浏览器不再保留它的登录状态，会自动切到其他账号；再回来需要重新输入密码。"
        : "本浏览器将不再保留它的登录状态，再回来需要重新输入密码。账号本身不受影响。",
      confirmLabel: "退出",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      const next = await removeAccount(account.username);
      if (account.active) {
        // 移除的是当前账号：后端已切到下一个（或全部退出），整页刷新
        reloadTo(next ? accessiblePathFor(next, "/") : "/login");
        return;
      }
      setAccounts((list) => (list ? list.filter((a) => a.username !== account.username) : list));
    } catch (e) {
      setError(e instanceof HttpError ? e.message : "移除失败，请稍后重试");
      listAccounts().then(setAccounts).catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  const handleLogoutAll = async () => {
    if (busy) return;
    const count = accounts?.length ?? 0;
    const ok = await confirm({
      title: "退出全部账号？",
      description: `本浏览器里的 ${count} 个账号都会退出登录，再回来需要逐个重新输入密码。共用设备时建议这样做。`,
      confirmLabel: "全部退出",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await logout(true);
    } catch {
      // 请求失败也照常去登录页；会话在后端仍会自然过期
    }
    reloadTo("/login");
  };

  const canAdd = (accounts?.length ?? 0) < MAX_SAVED_ACCOUNTS;

  return (
    <Modal open={open} onClose={onClose} label="切换账号">
      <div className="p-6 max-md:p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-title font-bold text-white">切换账号</h2>
            <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">
              本浏览器已登录的账号，点击即可切换，不用再输密码。
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="shrink-0 rounded-full p-1.5 text-[var(--text-faint)] hover:bg-white/10 hover:text-[var(--text)]"
          >
            <XIcon className="size-4" />
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
            {error}
          </div>
        )}

        <ul className="mt-5 space-y-1.5">
          {accounts === null && !error && (
            <li className="px-3 py-2 text-sub text-[var(--text-muted)]">正在加载…</li>
          )}
          {accounts?.map((account) => (
            <li key={account.username}>
              <AccountRow
                account={account}
                disabled={busy}
                onSwitch={() => handleSwitch(account)}
                onRemove={() => handleRemove(account)}
              />
            </li>
          ))}
        </ul>

        <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
          {canAdd ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => reloadTo("/login?add=1")}
              className="btn-glass flex items-center gap-2 px-3.5 py-2 text-ui font-medium disabled:opacity-50"
            >
              <PlusIcon className="size-4" />
              添加账号
            </button>
          ) : (
            <span className="text-caption text-[var(--text-faint)]">
              最多同时保存 {MAX_SAVED_ACCOUNTS} 个账号，移除一个后可再添加
            </span>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={handleLogoutAll}
            className="px-3.5 py-2 text-ui font-medium text-[var(--danger)] hover:underline disabled:opacity-50"
          >
            退出全部账号
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 弹窗里的一行账号：当前账号打勾且不可点；其他账号整行点击切换，行尾 × 移除。 */
function AccountRow({
  account,
  disabled,
  onSwitch,
  onRemove,
}: {
  account: AccountView;
  disabled: boolean;
  onSwitch: () => void;
  onRemove: () => void;
}) {
  const role = account.role === "admin" ? "超级管理员" : "成员";
  return (
    <div
      className={`flex items-center gap-3 rounded-xl border px-3 py-2.5 ${
        account.active
          ? "border-[var(--accent)]/40 bg-[var(--accent)]/10"
          : "border-white/[0.06] bg-white/[0.03] hover:bg-white/[0.07]"
      }`}
    >
      <button
        type="button"
        disabled={disabled || account.active}
        onClick={onSwitch}
        title={account.active ? undefined : `切换到 ${account.nickname}`}
        className="flex min-w-0 flex-1 items-center gap-3 text-left disabled:cursor-default"
      >
        <AvatarBadge nickname={account.nickname} avatarUrl={account.avatar_url} className="size-9 text-ui" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-ui font-semibold text-[var(--text)]">{account.nickname}</span>
          <span className="block truncate text-caption text-[var(--text-muted)]">
            @{account.username} · {role}
          </span>
        </span>
        {account.active && (
          <span className="flex shrink-0 items-center gap-1 text-caption font-medium text-[var(--accent)]">
            <CheckIcon className="size-3.5" />
            当前
          </span>
        )}
      </button>
      <button
        type="button"
        onClick={onRemove}
        disabled={disabled}
        aria-label={`从本浏览器移除 ${account.nickname}`}
        title="从本浏览器移除"
        className="shrink-0 rounded-full p-1 text-[var(--text-faint)] hover:bg-white/10 hover:text-[var(--text)] disabled:opacity-50"
      >
        <XIcon className="size-3.5" />
      </button>
    </div>
  );
}
