"use client";

import type { SessionView } from "@/lib/api/auth";
import { useSession } from "@/lib/session";

/**
 * 前端只消费业务权限，不在各页面散落角色判断。这里负责把管理员角色和成员
 * 能力快照收敛为稳定语义；后端鉴权仍是最终安全边界。
 */
export interface AppPermissions {
  isAdmin: boolean;
  canSubscribe: boolean;
  canSearch: boolean;
  canDirectDownload: boolean;
  canManageLibraries: boolean;
  canManageSubscriptions: boolean;
}

export function permissionsFor(session: SessionView): AppPermissions {
  const isAdmin = session.role === "admin";
  return {
    isAdmin,
    canSubscribe: isAdmin || session.capabilities.allow_subscribe,
    canSearch: isAdmin || session.capabilities.allow_search,
    canDirectDownload: isAdmin || session.capabilities.allow_direct_download,
    canManageLibraries: isAdmin,
    canManageSubscriptions: isAdmin,
  };
}

/** 当前会话的语义权限；权限变化后随 SessionProvider 快照立即更新。 */
export function usePermissions(): AppPermissions {
  return permissionsFor(useSession().session);
}

export function roleLabel(session: SessionView): string {
  return session.role === "admin" ? "超级管理员" : "成员";
}

/**
 * 把登录后的目标地址收敛到当前身份可进入的页面。除了登录落点，AuthGate
 * 也复用它拦截手输 URL，避免成员短暂看到 Agent 页面再收到后端 403。
 */
export function accessiblePathFor(session: SessionView, requestedPath: string): string {
  if (session.role === "member") {
    // Agent 入口（首页输入台 / 直达页 / 会话页）一律挡在成员之外：界面上已经
    // 不给入口，手输 URL 也不该看到一个后端全 403 的空壳页
    if (
      requestedPath === "/" ||
      requestedPath === "/new" ||
      requestedPath.startsWith("/sessions/")
    ) {
      return "/library";
    }
    if (!session.capabilities.allow_subscribe && requestedPath.startsWith("/subscriptions")) {
      return "/library";
    }
    if (!session.capabilities.allow_search && requestedPath.startsWith("/search")) {
      return "/library";
    }
  }
  return requestedPath;
}
