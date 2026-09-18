"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { AccountSwitcherDialog } from "@/components/account-switcher-dialog";
import { AvatarBadge } from "@/components/avatar-badge";
import { MovieclawMark } from "@/components/netflix/brand";
import {
  ActivityIcon,
  ChevronRightIcon,
  GearIcon,
  LogoutIcon,
  PlusIcon,
  BookmarkIcon,
  UserIcon,
} from "@/components/icons";
import { AppUpdateEntry } from "@/components/app-update-entry";
import { NoticeCenter } from "@/components/notice-center";
import { logout } from "@/lib/api/auth";
import { clearBackdropCache } from "@/lib/backdrop-cache";
import { useAgentConversations } from "@/lib/agent-conversations";
import { accessiblePathFor, usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";
import { taskActivityBadge, useTaskActivity, type TaskActivityBadge } from "@/lib/task-activity";
import { clearUiPrefsCache } from "@/lib/ui-prefs-cache";
import { useThemeState } from "@/lib/ui-prefs";

/**
 * Netflix 主题的「我的」页面（路由 /my，2026-09 修订）。
 *
 * 原实现是右侧滑出的 NetflixMySheet 面板——浮层形态承载不了还在生长的
 * 账号/设置动线（面板里点设置又要跳路由），且与「每个入口都是真实路由、
 * 可刷新可分享」的全站导航原则相悖。改为独立页面后：
 *   - 底栏「我的」页签直接路由到 /my（不再开关面板）；
 *   - 设置成为本页的二级页面（/settings 分区列表 → /settings/[section]，
 *     页顶返回键逐级回来，见 NetflixSettingsNav 与 settings-index.tsx）；
 *   - 银玻璃主题不使用本页（它的同等入口在抽屉侧栏），直达时跳回首页。
 *
 * 内容分区对齐 Netflix App 的 My Netflix：用户头 + 快捷入口 + AI 会话 +
 * 账号操作。行皮肤用 glass-row（Netflix 主题下自动换实色卡 + 4px 方角）。
 */
export function NetflixMyPage() {
  const router = useRouter();
  // loading 期间**不做任何跳转判断**：首帧主题读的是 localStorage 缓存，冷缓存
  // 时它是 silver，直接 replace 会把 Netflix 用户从「我的」弹回媒体库
  // （新设备首次登录 / 无痕窗口 / 清过站点数据都会踩到，见 useThemeState）
  const { theme, loading } = useThemeState();
  const { session } = useSession();
  const { isAdmin, canSubscribe } = usePermissions();
  const { conversations } = useAgentConversations();
  const [switcherOpen, setSwitcherOpen] = useState(false);

  // 银玻璃主题的同等入口在抽屉侧栏里，本页只在 Netflix 结构下存在
  useEffect(() => {
    if (!loading && theme.id !== "netflix") router.replace("/");
  }, [loading, theme.id, router]);

  /**
   * 退出登录：只退当前账号，本浏览器还有别的账号时后端自动切过去，
   * 没有了才去登录页；整页跳转重置全部前端状态。
   */
  const handleLogout = async () => {
    let next: Awaited<ReturnType<typeof logout>> = null;
    try {
      next = await logout();
    } catch {
      // 即使请求失败（网络断开），也照常跳登录页；会话在后端仍会自然过期
    }
    clearBackdropCache();
    clearUiPrefsCache();
    window.location.href = next ? accessiblePathFor(next, "/") : "/login";
  };

  // 偏好未落定时先占位（而不是 null）：本页是底栏页签的落点，渲染 null 会让
  // 页面在这一两帧里空掉、底栏浮在纯黑上闪一下
  if (loading) return <div className="h-full" aria-busy="true" />;
  if (theme.id !== "netflix") return null;

  return (
    <div className="scroll-thin scroll-safe h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-2xl px-4 pb-16 pt-6 md:px-6 md:pt-10">
        {/* 用户头：头像 + 昵称 + 用户名（My Netflix 的门面） */}
        <header className="flex items-center gap-4 px-1">
          {/* 圆角走 style（见 AvatarBadge 说明）：className 里的 rounded-[4px]
              压不过组件内部的 rounded-full，实测渲染成正圆 */}
          <AvatarBadge
            nickname={session.nickname}
            avatarUrl={session.avatar_url}
            className="size-14 text-title-lg"
            style={{ borderRadius: 4 }}
          />
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-title-lg font-bold tracking-[-0.01em] text-[var(--text)]">
              {session.nickname}
            </h1>
            <p className="mt-0.5 truncate text-ui text-[var(--text-muted)]">
              @{session.username}
            </p>
          </div>
          <MovieclawMark className="h-5 w-auto shrink-0 opacity-90" aria-hidden="true" />
        </header>

        {/* 快捷入口 */}
        <nav aria-label="我的入口" className="mt-6 space-y-0.5">
          {/* 待处理事项：Netflix 主题没有侧栏，银玻璃侧栏里的告警入口由本行
              承接（组件自轮询自鉴权，无事时整行不渲染） */}
          <NoticeCenter collapsed={false} />
          {/* 新任务是 Agent 入口，管理员专属——银玻璃侧栏用 memberNavItems 把
              「新会话」整条摘掉，这里必须同口径，否则成员点进去是一个后端
              全 403 的页面（安全边界在后端 require_admin，这里是界面裁剪） */}
          {isAdmin && (
            <MyRow Icon={PlusIcon} label="新任务" onClick={() => router.push("/new" as Route)} />
          )}
          {canSubscribe && (
            <MyRow
              Icon={BookmarkIcon}
              label="我的订阅"
              onClick={() => router.push("/subscriptions" as Route)}
            />
          )}
          {/* 活动：带任务角标（下载失败红 / 进行中蓝）与动态落点——侧栏
              JobCenter 的同一套徽标逻辑，Netflix 入口不再是无声的裸链接 */}
          {isAdmin && <MyActivityRow />}
          <MyRow
            Icon={GearIcon}
            label="设置"
            onClick={() => router.push("/settings" as Route)}
          />
          {/* 应用内更新的常驻入口（组件自轮询，无更新时整行不渲染）：
              Netflix 主题不再渲染侧栏，侧栏里的更新徽标改由本行承接 */}
          <AppUpdateEntry
            collapsed={false}
            onOpen={() => router.push("/settings/app" as Route)}
          />
        </nav>

        {/* AI 会话：与原「我的」面板同源（管理员可见），点击直达会话页 */}
        {isAdmin && (
          <nav aria-label="AI 会话" className="mt-6 space-y-0.5">
            <p className="group-label px-3 pb-1.5">AI 会话</p>
            {conversations.length === 0 ? (
              <p className="px-3 py-1.5 text-caption leading-5 text-[var(--text-faint)]">
                还没有会话，从「新任务」开始。
              </p>
            ) : (
              conversations.map((c) => (
                <MyRow
                  key={c.id}
                  label={c.title}
                  running={c.running}
                  onClick={() => router.push(`/sessions/${c.id}` as Route)}
                />
              ))
            )}
          </nav>
        )}

        {/* 账号操作 */}
        <nav aria-label="账号操作" className="mt-6 space-y-0.5">
          <MyRow Icon={UserIcon} label="切换账号" onClick={() => setSwitcherOpen(true)} />
          <MyRow Icon={LogoutIcon} label="退出登录" danger onClick={() => void handleLogout()} />
        </nav>
      </div>

      <AccountSwitcherDialog open={switcherOpen} onClose={() => setSwitcherOpen(false)} />
    </div>
  );
}

/** 活动入口行：拆成独立组件，任务快照变化时只重渲染这一行、不牵动整页；
 *  数据来自全站 Provider（本组件不发起请求）。 */
function MyActivityRow() {
  const router = useRouter();
  const badge = taskActivityBadge(useTaskActivity());
  return (
    <MyRow
      Icon={ActivityIcon}
      label="活动"
      badge={badge.count > 0 ? badge : undefined}
      onClick={() => router.push(badge.href as Route)}
    />
  );
}

/** 页面行：glass-row 皮肤 + 右缘 chevron 表达「点进去」的可点性。 */
function MyRow({
  Icon,
  label,
  onClick,
  danger = false,
  running = false,
  badge,
}: {
  Icon?: React.ComponentType<React.SVGProps<SVGSVGElement>>;
  label: string;
  onClick: () => void;
  danger?: boolean;
  running?: boolean;
  /** 右缘状态角标（活动行的任务计数：alert 红 / 否则提示蓝） */
  badge?: TaskActivityBadge;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={badge?.hint}
      className={`glass-row w-full px-3 py-2 max-md:py-2.5 text-ui font-medium ${
        danger ? "!text-[var(--danger)] hover:!bg-[rgba(255,107,107,0.12)]" : ""
      }`}
    >
      {running && (
        <span aria-hidden="true" className="size-1.5 shrink-0 animate-pulse rounded-full bg-[var(--info)]" />
      )}
      {Icon && <Icon className="size-[18px] max-md:size-[22px] shrink-0" />}
      <span className="min-w-0 flex-1 truncate text-left">{label}</span>
      {badge && badge.count > 0 && (
        <span
          className={`shrink-0 rounded-full px-1.5 py-0.5 text-micro font-semibold leading-none ${
            badge.alert
              ? "bg-[var(--danger-solid)] text-white"
              : "bg-[var(--info)]/20 text-[var(--info)]"
          }`}
        >
          {badge.count}
        </span>
      )}
      <ChevronRightIcon className="size-4 shrink-0 text-[var(--text-faint)]" />
    </button>
  );
}
