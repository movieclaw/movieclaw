"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { AccountSwitcherDialog } from "@/components/account-switcher-dialog";
import { AppUpdateEntry } from "@/components/app-update-entry";
import { AvatarBadge } from "@/components/avatar-badge";
import { ConversationMenu } from "@/components/conversation-menu";
import { copyText } from "@/components/copy-button";
import { useConfirm, usePrompt, useToast } from "@/components/feedback";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  GearIcon,
  LogoutIcon,
  UserIcon,
} from "@/components/icons";
import { NoticeCenter } from "@/components/notice-center";
import { reloadAfterAccountChange } from "@/lib/account-reload";
import { logout } from "@/lib/api/auth";
import { useAgentConversations } from "@/lib/agent-conversations";
import { accessiblePathFor, roleLabel, usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";
import type { TaskActivityBadge } from "@/lib/task-activity";

/**
 * 「更多」页（路由 /my，主题 pages.my 坑位的基础实现）——银玻璃移动端液态玻璃
 * 底栏最右的头像页签（docs/design/web-themes-mobile/04-iOS-液态玻璃底栏.md §3.1）。
 *
 * 抽屉侧栏在移动端退役后，它承载的低频入口与账号操作都收在这里，版式对齐
 * iOS「更多 / 设置」的分组列表（inset grouped）：
 *   - 用户头：头像 + 昵称 + 角色；
 *   - 常用：个人信息 / 待处理事项 / 设置 / 应用更新（新会话走顶栏右上角的「+」
 *     撰写键，活动已提到底栏页签，这里都不重复放）；
 *   - 账号：切换账号 / 退出登录——紧跟设置之后，不被下面会长的会话列表推到页底；
 *   - 最近会话：AI 会话列表，点击直达会话页。默认只列最近几条，其余收在
 *     「显示全部」一行里就地展开（iOS 设置列表的惯例；卡片内滚动条试过，用户
 *     嫌难看，且没有会话列表页可跳，所以是展开而不是「查看全部」）。
 * 新会话与活动、AI 会话是 Agent 能力，管理员专属——与侧栏的 memberNavItems 同口径
 * （安全边界在后端 require_admin，这里是界面裁剪）。
 *
 * Netflix 主题有自己的「我的」页（themes/netflix/pages/my-page），会覆盖本页。
 */
export function MorePage() {
  const router = useRouter();
  const { session } = useSession();
  const { isAdmin } = usePermissions();
  const { conversations, rename, remove, fork } = useAgentConversations();
  const prompt = usePrompt();
  const confirm = useConfirm();
  const toast = useToast();
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [showAllSessions, setShowAllSessions] = useState(false);
  const hiddenSessions = Math.max(0, conversations.length - RECENT_SESSIONS_LIMIT);
  const visibleSessions =
    showAllSessions || hiddenSessions === 0
      ? conversations
      : conversations.slice(0, RECENT_SESSIONS_LIMIT);

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
    await reloadAfterAccountChange(next ? accessiblePathFor(next, "/") : "/login", next != null);
  };

  // 会话行「⋯」菜单的四个动作：与侧栏会话行同一套语义（sidebar.tsx）
  const handleRename = async (id: string, currentTitle: string) => {
    const input = await prompt({ title: "重命名会话", initialValue: currentTitle, maxLength: 80 });
    if (input == null) return;
    const title = input.trim().slice(0, 80);
    if (!title || title === currentTitle) return;
    void rename(id, title).catch((error) => {
      toast.error(`重命名失败：${(error as Error).message}`);
    });
  };
  const handleDelete = async (id: string, title: string) => {
    const ok = await confirm({
      title: `彻底删除会话「${title}」？`,
      description: "服务器上的完整对话记录将一并删除，此操作不可恢复。",
      confirmLabel: "彻底删除",
      tone: "danger",
    });
    if (!ok) return;
    void remove(id).catch((error) => {
      toast.error(`删除失败：${(error as Error).message}`);
    });
  };
  const handleFork = async (id: string) => {
    try {
      const targetId = await fork(id);
      router.push(`/sessions/${targetId}` as Route);
    } catch (error) {
      toast.error(`创建续接会话失败：${(error as Error).message}`);
    }
  };
  const handleCopyId = async (id: string) => {
    try {
      await copyText(id);
      toast.success("会话 ID 已复制");
    } catch (error) {
      toast.error(`复制失败：${(error as Error).message}`);
    }
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
          {/* 新会话已有顶栏右上角的「+」撰写键（app-shell），这里改放个人信息入口：
              账号头就在上面，点进去改头像 / 昵称 / 密码是最顺的一步 */}
          <MoreRow
            Icon={UserIcon}
            label="个人信息"
            onClick={() => router.push("/settings/profile" as Route)}
          />
          {/* 待处理事项与应用更新：组件自轮询，无事时整行不渲染 */}
          <NoticeCenter collapsed={false} />
          <MoreRow Icon={GearIcon} label="设置" onClick={() => router.push("/settings" as Route)} />
          <AppUpdateEntry collapsed={false} onOpen={() => router.push("/settings/app" as Route)} />
        </MoreGroup>

        <MoreGroup label="账号">
          <MoreRow Icon={UserIcon} label="切换账号" onClick={() => setSwitcherOpen(true)} />
          <MoreRow Icon={LogoutIcon} label="退出登录" danger onClick={() => void handleLogout()} />
        </MoreGroup>

        {isAdmin && (
          <MoreGroup label="最近会话">
            {conversations.length === 0 ? (
              <p className="px-4 py-3 text-caption leading-5 text-[var(--text-faint)]">
                还没有会话，点上方的「新会话」开始。
              </p>
            ) : (
              visibleSessions.map((c) => (
                <MoreRow
                  key={c.id}
                  label={c.title}
                  running={c.running}
                  onClick={() => router.push(`/sessions/${c.id}` as Route)}
                  trailing={
                    <ConversationMenu
                      onFork={() => void handleFork(c.id)}
                      onCopyId={() => void handleCopyId(c.id)}
                      onRename={() => void handleRename(c.id, c.title)}
                      onDelete={() => void handleDelete(c.id, c.title)}
                      triggerClassName="!size-8 text-[var(--text-muted)]"
                    />
                  }
                />
              ))
            )}
            {hiddenSessions > 0 && (
              // 展开/收起行：与列表行同一皮肤，但文字居中、弱化，用向下/向上箭头表达
              // 「还有内容折在这里」（列表行的右缘箭头表达「点进去」，两者不混用）
              <button
                type="button"
                onClick={() => setShowAllSessions((v) => !v)}
                aria-expanded={showAllSessions}
                className="glass-row w-full justify-center px-4 py-2.5 text-ui font-medium !text-[var(--text-muted)]"
              >
                <span>{showAllSessions ? "收起" : `显示全部 ${conversations.length} 个会话`}</span>
                <ChevronDownIcon
                  className={`size-4 shrink-0 transition-transform duration-200 ${
                    showAllSessions ? "rotate-180" : ""
                  }`}
                />
              </button>
            )}
          </MoreGroup>
        )}
      </div>

      <AccountSwitcherDialog open={switcherOpen} onClose={() => setSwitcherOpen(false)} />
    </div>
  );
}

/** 「最近会话」默认露出的条数；再多的折进「显示全部」行里。 */
const RECENT_SESSIONS_LIMIT = 5;

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

/**
 * 列表行：glass-row 皮肤 + 右缘 chevron 表达「点进去」的可点性。
 * ``trailing``：行尾叠一个独立控件（会话行的「⋯」菜单）——它不能嵌在主体
 * 按钮里（button 不能套 button），所以绝对定位盖在行尾、主体按钮右侧留出位置，
 * 有它时不再画 chevron（一行只表达一种可点性）。
 */
function MoreRow({
  Icon,
  label,
  onClick,
  danger = false,
  running = false,
  badge,
  trailing,
}: {
  Icon?: React.ComponentType<React.SVGProps<SVGSVGElement>>;
  label: string;
  onClick: () => void;
  danger?: boolean;
  running?: boolean;
  /** 右缘状态角标（活动行的任务计数：alert 红 / 否则提示蓝） */
  badge?: TaskActivityBadge;
  trailing?: ReactNode;
}) {
  const row = (
    <button
      type="button"
      onClick={onClick}
      title={badge?.hint}
      className={`glass-row w-full px-4 py-3 text-body font-medium ${
        danger ? "!text-[var(--danger)]" : "!text-[var(--text)]"
      } ${trailing ? "pr-14" : ""}`}
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
      {!trailing && <ChevronRightIcon className="size-4 shrink-0 text-[var(--text-faint)]" />}
    </button>
  );
  if (!trailing) return row;
  return (
    <div className="relative">
      {row}
      <div className="absolute right-3 top-1/2 -translate-y-1/2">{trailing}</div>
    </div>
  );
}
