"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { Route } from "next";

import { AuthError, AuthField, AuthScreen } from "@/components/auth-screen";
import { getBootstrapStatus, getSession, login } from "@/lib/api/auth";
import { clearBackdropCache } from "@/lib/backdrop-cache";
import { clearUiPrefsCache } from "@/lib/ui-prefs-cache";
import { usePageTitle } from "@/lib/use-page-title";
import { HttpError } from "@/lib/http";
import { accessiblePathFor } from "@/lib/permissions";
import type { SessionView } from "@/lib/api/auth";

/**
 * 登录成功 / 已登录后要跳回的目标地址：取自 ?next= 参数（会话过期时由 http.ts 写入）。
 * 只接受站内相对路径（以单个 / 开头），拒绝 //host、http(s):// 等外站地址，防开放重定向；
 * 缺失或非法时回落到首页。
 */
function resolveNext(session?: SessionView): string {
  if (typeof window === "undefined") return "/";
  const raw = new URLSearchParams(window.location.search).get("next");
  if (!raw) return session ? accessiblePathFor(session, "/") : "/";
  const next = decodeURIComponent(raw);
  if (next.startsWith("/") && !next.startsWith("//")) {
    return session ? accessiblePathFor(session, next) : next;
  }
  return session ? accessiblePathFor(session, "/") : "/";
}

/** 是否处于"添加账号"形态（用户菜单里点「添加账号」带 ?add=1 进来）。 */
function isAddingAccount(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("add") === "1";
}

/**
 * 登录页。挂载时做两个跳转判断：
 * 1. 系统尚未初始化 → 转 /setup 引导页（首次部署的入口）；
 * 2. 已持有效会话 → 直接回 next 目标（默认首页），不重复登录。
 * 安全性完全由后端保证，这里的跳转只是导航体验。
 *
 * "添加账号"形态（?add=1，docs/design/account-switching.md §4）：已登录也不跳走，
 * 登录成功后后端自动把新账号并入本浏览器的账号列表，前端不需要传任何额外参数。
 */
export default function LoginPage() {
  // 挂载后再读 URL：服务端渲染没有 window，初值若按 URL 算会造成水合不一致
  const [adding, setAdding] = useState(false);
  useEffect(() => {
    setAdding(isAddingAccount());
  }, []);
  usePageTitle(adding ? "添加账号" : "登录");
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await getBootstrapStatus();
        if (cancelled) return;
        if (!status.initialized) {
          router.replace("/setup");
          return;
        }
        // 添加账号：已登录也留在本页。直接读 URL 而不用 adding 状态——
        // 状态要等首个 effect 才更新，这里不能抢在它前面把人跳走
        if (isAddingAccount()) return;
        const session = await getSession(); // 已登录则不抛错
        if (!cancelled) router.replace(resolveNext(session) as Route);
      } catch {
        // 未登录（401）或后端暂不可达：留在登录页即可
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const session = await login(username.trim(), password, remember);
      // 整页跳转而非路由跳转：让 AppShell 及全部数据在已登录态下重新初始化。
      // 回到 next 指向的页面（会话过期前所在处），默认首页。
      clearBackdropCache();
      clearUiPrefsCache();
      window.location.href = resolveNext(session);
    } catch (err) {
      setError(err instanceof HttpError ? err.message : "网络异常，请稍后重试");
      setBusy(false);
    }
  };

  return (
    <AuthScreen
      title={adding ? "添加账号" : "登录"}
      subtitle={
        adding
          ? "登录另一个账号；之后可在用户菜单里一键切换，不用再输密码。"
          : "使用你的 MovieClaw 账号进入。"
      }
    >
      <form onSubmit={submit} className="space-y-4">
        <AuthField
          label="用户名"
          type="text"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          autoFocus
        />
        <AuthField
          label="密码"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
        <label className="flex cursor-pointer items-center gap-2 text-sub text-[var(--text-muted)]">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            className="size-3.5 accent-[var(--accent)]"
          />
          30 天内记住我
        </label>
        <AuthError message={error} />
        <button
          type="submit"
          disabled={busy || !username.trim() || !password}
          className="btn-accent w-full rounded-full px-4.5 py-2.5 text-ui font-semibold disabled:opacity-40"
        >
          {busy ? "登录中…" : adding ? "添加并切换" : "登录"}
        </button>
        {adding && (
          <button
            type="button"
            onClick={() => router.replace("/")}
            className="w-full text-center text-sub text-[var(--text-muted)] hover:text-[var(--text)]"
          >
            取消，回到当前账号
          </button>
        )}
      </form>
    </AuthScreen>
  );
}
