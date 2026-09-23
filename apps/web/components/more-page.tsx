"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { AccountSwitcherDialog } from "@/components/account-switcher-dialog";
import { AppUpdateEntry } from "@/components/app-update-entry";
import { AvatarBadge } from "@/components/avatar-badge";
import {
  ActivityIcon,
  ChevronRightIcon,
  GearIcon,
  LogoutIcon,
  PencilIcon,
  UserIcon,
} from "@/components/icons";
import { NoticeCenter } from "@/components/notice-center";
import { logout } from "@/lib/api/auth";
import { useAgentConversations } from "@/lib/agent-conversations";
import { clearBackdropCache } from "@/lib/backdrop-cache";
import { usePageChrome } from "@/lib/page-chrome";
import { accessiblePathFor, roleLabel, usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";
import { taskActivityBadge, useTaskActivity, type TaskActivityBadge } from "@/lib/task-activity";
import { clearUiPrefsCache } from "@/lib/ui-prefs-cache";

/**
 * 「更多」页（路由 /my，主题 pages.my 坑位的基础实现）——银玻璃移动端液态玻璃
 * 底栏的末位页签（docs/design/web-themes-mobile/04-iOS-液态玻璃底栏.md §3.1）。
 *
 * 抽屉侧栏在移动端退役后，它承载的低频入口与账号操作都收在这里，版式对齐
 * iOS「更多 / 设置」的分组列表（inset grouped）：
 *   - 用户头：头像 + 昵称 + 角色；
 *   - 常用：新会话（打开外壳的撰写面板）/ 待处理事项 / 活动（任务角标）/ 设置 /
 *     应用更新；
 *   - 最近会话：AI 会话列表，点击直达会话页；
 *   - 账号：切换账号 / 退出登录。
 * 新会话与活动、AI 会话是 Agent 能力，管理员专属——与侧栏的 memberNavItems 同口径
 * （安全边界在后端 require_admin，这里是界面裁剪）。
 *
 * Netflix 主题有自己的「我的」页（themes/netflix/pages/my-page），会覆盖本页。
 */
export function MorePage() {
  const router = useRouter();
  const chrome = usePageChrome();
  const { session } = useSession();
  const { isAdmin } = usePermissions();
  const { conversations } = useAgentConversations();
  const [switcherOpen, setSwitcherOpen] = useState(false);

  /**
   * 退出登录：只退当前账号，本浏览器还有别的账号时后端自动切过去，
   * 没有了才去登录页；整页跳转重置全部前端状态（与侧栏用户菜单同一套流程）。
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

  return (
    <div className="scroll-thin scroll-safe h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-2xl px-4 pb-10 pt-4 md:px-6 md:pt-10">
        <header className="flex items-center gap-4 px-1 pb-5">
          <AvatarBadge
            nickname={session.nickname}
            avatarUrl={session.avatar_url}
            className="size-14 text-title-lg"
          />
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-title-lg font-bold tracking-[-0.01em] text-[var(--text)]">
              {session.nickname}
            </h1>
            <p className="mt-0.5 truncate text-ui text-[var(--text-muted)]">
              @{session.username} · {roleLabel(session)}
            </p>
          </div>
        </header>

        <MoreGroup label="常用">
          {isAdmin && chrome && (
            <MoreRow Icon={PencilIcon} label="新会话" onClick={chrome.openCompose} />
          )}
          {/* 待处理事项与应用更新：组件自轮询，无事时整行不渲染 */}
          <NoticeCenter collapsed={false} />
          {isAdmin && <ActivityRow />}
          <MoreRow Icon={GearIcon} label="设置" onClick={() => router.push("/settings" as Route)} />
          <AppUpdateEntry collapsed={false} onOpen={() => router.push("/settings/app" as Route)} />
        </MoreGroup>

        {isAdmin && (
          <MoreGroup label="最近会话">
            {conversations.length === 0 ? (
              <p className="px-4 py-3 text-caption leading-5 text-[var(--text-faint)]">
                还没有会话，点右上角的撰写键开始。
              </p>
            ) : (
              conversations.map((c) => (
                <MoreRow
                  key={c.id}
                  label={c.title}
                  running={c.running}
                  onClick={() => router.push(`/sessions/${c.id}` as Route)}
                />
              ))
            )}
          </MoreGroup>
        )}

        <MoreGroup label="账号">
          <MoreRow Icon={UserIcon} label="切换账号" onClick={() => setSwitcherOpen(true)} />
          <MoreRow Icon={LogoutIcon} label="退出登录" danger onClick={() => void handleLogout()} />
        </MoreGroup>
      </div>

      <AccountSwitcherDialog open={switcherOpen} onClose={() => setSwitcherOpen(false)} />
    </div>
  );
}

/** 分组卡片：小节标题 + 圆角卡片，行间细分隔线（iOS inset grouped 列表） */
function MoreGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <nav aria-label={label} className="mt-5 first:mt-0">
      <p className="group-label px-4 pb-1.5">{label}</p>
      <div className="divide-y divide-[var(--line)] overflow-hidden rounded-2xl bg-[var(--glass-fill)] ring-1 ring-inset ring-[var(--line)] [&_.glass-row]:rounded-none">
        {children}
      </div>
    </nav>
  );
}

/** 活动入口行：拆成独立组件，任务快照变化时只重渲染这一行；数据来自全站 Provider。 */
function ActivityRow() {
  const router = useRouter();
  const badge = taskActivityBadge(useTaskActivity());
  return (
    <MoreRow
      Icon={ActivityIcon}
      label="活动"
      badge={badge.count > 0 ? badge : undefined}
      onClick={() => router.push(badge.href as Route)}
    />
  );
}

/** 列表行：glass-row 皮肤 + 右缘 chevron 表达「点进去」的可点性。 */
function MoreRow({
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
      className={`glass-row w-full px-4 py-3 text-body font-medium ${
        danger ? "!text-[var(--danger)]" : "!text-[var(--text)]"
      }`}
    >
      {running && (
        <span aria-hidden="true" className="size-1.5 shrink-0 animate-pulse rounded-full bg-[var(--info)]" />
      )}
      {Icon && <Icon className="size-[22px] shrink-0 text-[var(--text-muted)]" />}
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
