"use client";

import Image from "next/image";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import {
  ActivityIcon,
  BookmarkIcon,
  ChatIcon,
  ClockIcon,
  CopyIcon,
  LayersIcon,
  MoreIcon,
  PanelLeftIcon,
  PencilIcon,
  PlusIcon,
  TrashIcon,
} from "@/components/icons";
import { AppUpdateEntry } from "@/components/app-update-entry";
import { copyText } from "@/components/copy-button";
import { useConfirm, usePrompt, useToast } from "@/components/feedback";
import { NoticeCenter } from "@/components/notice-center";
import { JobCenter } from "@/components/job-center";
import { UserMenu } from "@/components/user-menu";
import { useAgentConversations } from "@/lib/agent-conversations";
import { useBackdrop } from "@/lib/backdrop";
import { sidebarGlass } from "@/lib/glass";
import { applyNavOrder } from "@/lib/sidebar-nav";
import { formatRelativeTime } from "@/lib/time";
import { useUiPrefs } from "@/lib/ui-prefs";
import { useIsMobile } from "@/lib/use-media-query";
import { useSession } from "@/lib/session";
import { usePermissions } from "@/lib/permissions";
import { exploreItems } from "@/lib/mock-data";
import { GlassPanel } from "@/components/glass-panel";
import { SearchCommand, type SearchSubmitOptions } from "@/components/search-command";
import type { SearchScope } from "@/lib/categories";

/**
 * 工作台侧边栏。结构（自上而下，对齐 ChatGPT Codex 侧栏的极简版式）：
 *   品牌头部（右侧搜索图标，⌘K）→ 扁平主导航 → 「最近会话」分组 → 左下角用户信息。
 * 面板本体用真实 WebGL 液态玻璃（GlassPanel, dark 预设）承载，透出并折射背景大图，
 * 是整屏的视觉主角。
 *
 * 折叠形态（collapsed）：只留图标的窄玻璃条——品牌徽标 / 开关 / 搜索竖排在头部，
 * 主导航仅显示居中图标（title 提示文案），「最近会话」整组隐藏，左下角只留头像。
 * 宽度动画由外层 app-shell 的 aside 承担，本组件只负责两种版式的内容切换。
 */
export interface SidebarProps {
  activeNav: string;
  onSelect: (id: string) => void;
  onSearch: (keyword: string, scope: SearchScope, options?: SearchSubmitOptions) => void;
  onOpenSettings: (sectionId?: string) => void;
  /** 是否处于折叠（图标窄栏）形态 */
  collapsed: boolean;
  /** 点击头部开关按钮：在展开 / 折叠间切换 */
  onToggleCollapse: () => void;
  /** 实色形态：沉浸页（Agent 对话）用——不渲染 WebGL 玻璃、不折射背景大图 */
  flat?: boolean;
}

/** 主导航：新会话 / 媒体库 / 探索项 / 订阅 / 活动，合并成一列扁平列表。
 *  「新会话」是 Agent 入口（管理员专属，成员侧隐藏——后端也会 403）。
 *
 *  这个数组的次序就是**内置默认顺序**：用户在「设置 → 外观 → 导航顺序」里排过
 *  的项按个人顺序在前，没排过的（包括版本升级新增的入口）按这里的次序追加在后
 *  （合并规则见 lib/sidebar-nav.ts）。设置页复用同一份清单渲染排序列表，
 *  两边的图标与文案不会各写一份。 */
export const SIDEBAR_NAV_ITEMS = [
  { id: "new", label: "新会话", icon: PlusIcon },
  { id: "library", label: "媒体库", icon: LayersIcon },
  ...exploreItems,
  { id: "subscriptions", label: "我的订阅", icon: BookmarkIcon },
  // 「活动」（管理员专属）在这里占一位，只为**参与排序**：它带角标、落点还随
  // 角标变，因此渲染仍交给 JobCenter（见下方 navItems.map 的分支），这里登记的
  // label/icon 只给设置页的排序列表用。id 与 app-shell 的 navIdFromPath 对齐
  // （/activity → tasks），不另立一套 id。
  { id: "tasks", label: "活动", icon: ActivityIcon },
];

const memberNavItems = SIDEBAR_NAV_ITEMS.filter((item) => item.id !== "new");

/**
 * 当前用户实际可见的主导航项（未排序）。侧栏与设置页的排序列表必须用同一套
 * 可见性判定，否则会出现"设置页能排、侧栏没有"这种对不上的项。
 */
export function useVisibleNavItems() {
  const { session } = useSession();
  const { isAdmin, canSubscribe } = usePermissions();
  const items = session.role === "member" ? memberNavItems : SIDEBAR_NAV_ITEMS;
  return items.filter((item) => {
    if (item.id === "subscriptions") return canSubscribe;
    // 活动页与任务数据都是管理员专属（JobCenter 自身也有这道判断，成员侧渲染为空），
    // 这里必须同样挡掉，否则设置页会列出一条侧栏根本没有的可排序项
    if (item.id === "tasks") return isAdmin;
    return true;
  });
}

export function Sidebar({
  activeNav,
  onSelect,
  onSearch,
  onOpenSettings,
  collapsed,
  onToggleCollapse,
  flat = false,
}: SidebarProps) {
  const { backdrop } = useBackdrop();
  // 移动端的搜索入口在全局顶栏上（常驻可见），侧栏里这颗就是重复的。
  // 必须条件渲染而不是 CSS 隐藏：SearchCommand 自带全局 ⌘K 监听，
  // 挂两份会让一次快捷键把面板开了又关。
  const isMobile = useIsMobile();
  // 透明度/明暗/厚度来自「设置 → 外观」的用户偏好（ui.preferences.sidebar），
  // 基底为 LiquidGlassCard 同款材质；设置页拖动滑杆时经预览草稿实时生效。
  const { prefs } = useUiPrefs();
  const glass = sidebarGlass(prefs.sidebar);
  // 成员形态做减法：隐藏 Agent 入口（新会话）与「最近会话」组；
  // 这是界面裁剪，安全边界在后端 require_admin
  const { session } = useSession();
  const { canSearch } = usePermissions();
  const isMember = session.role === "member";
  // 个人排序：prefs 已含设置页的未保存草稿，因此在设置页拖动时这里即时跟随
  const visibleNavItems = useVisibleNavItems();
  const navItems = applyNavOrder(visibleNavItems, prefs.nav.order);
  const body = (
    <>
      {/* 品牌头部。展开：完整字标 + 开合/搜索图标横排；折叠：独立徽标、开合、搜索竖排居中。
          开合按钮与搜索共用同一套图标按钮样式（⌘K 在两种形态下均可唤起搜索）。
          两种形态的 logo 都是「回首页」入口，等同于点击导航里的「新会话」。 */}
      {collapsed ? (
        <div className="flex flex-col items-center gap-2 px-3 pb-3 pt-5">
          <BrandHome onSelect={onSelect} homeId={isMember ? "library" : "new"}>
            <Image
              src="/movieclaw-logo-mark-rotor.png"
              alt="MovieClaw"
              width={525}
              height={525}
              priority
              className="size-7 object-contain"
            />
          </BrandHome>
          <CollapseToggle collapsed onClick={onToggleCollapse} />
          {canSearch && <SearchCommand onSearch={onSearch} />}
        </div>
      ) : (
        <div className="flex items-center justify-between px-4 pb-3 pt-4">
          <BrandHome onSelect={onSelect} homeId={isMember ? "library" : "new"}>
            <Image
              src="/movieclaw-logo-rotor.png"
              alt="MovieClaw"
              width={1920}
              height={525}
              priority
              className="h-8 w-auto max-w-[120px] object-contain"
            />
          </BrandHome>
          <div className="flex items-center gap-1">
            {!isMobile && canSearch && <SearchCommand onSearch={onSearch} />}
            <CollapseToggle collapsed={false} onClick={onToggleCollapse} />
          </div>
        </div>
      )}

      {/* 导航区。两段式：主导航固定在上方，「最近会话」占满剩余高度并自带
          滚动条——会话可以有几百条，与主导航共用一条滚动条时翻会话会把
          导航推出视野。min-h-0 是 flex 子项能真正收缩、把滚动交给内层的前提。 */}
      <nav className="flex min-h-0 flex-1 flex-col px-3 pb-2">
        {/* 主导航：无分组标题、无图标底片的扁平列表（对齐 Codex 侧栏）；
            折叠时只留居中图标，文案降级为 title 悬浮提示。
            自带 overflow 是矮窗口下的安全阀：空间不够时它自己滚，
            而不是把「最近会话」挤没。 */}
        <div className="scroll-thin space-y-0.5 overflow-y-auto">
          {navItems.map((item) => {
            // 「活动」自带角标与动态落点，交给 JobCenter 渲染；它同样参与个人排序，
            // 所以位置由这里的 map 决定，而不再被钉在主导航末尾
            if (item.id === "tasks") {
              return (
                <JobCenter key={item.id} collapsed={collapsed} active={activeNav === "tasks"} />
              );
            }
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                type="button"
                data-active={activeNav === item.id}
                onClick={() => onSelect(item.id)}
                title={collapsed ? item.label : undefined}
                className={`glass-row nav-item py-2 max-md:py-2.5 ${collapsed ? "justify-center px-0" : "px-3"}`}
              >
                {/* 图标移动端提到 22px：与顶栏图标键同标准（iOS 列表行图标的惯用比例） */}
                <Icon className="size-[18px] shrink-0 max-md:size-[22px]" />
                {/* 字号写在 span 上而非 button 上：globals.css 的 `button { font: inherit }`
                    是无 @layer 的普通规则，会压过 Tailwind 的 @layer utilities——写在
                    button 上的 text-ui 不生效，会退回 body 的 14px。 */}
                {!collapsed && (
                  <span className="flex-1 text-ui font-medium">{item.label}</span>
                )}
              </button>
            );
          })}
          {/* 待处理事项：常态零渲染，有"需要用户行动"的运行时故障才亮起 */}
          <NoticeCenter collapsed={collapsed} />
          {/* 更新入口：同样常态零渲染。刻意与待处理事项分开——"有新版可用"
              不是故障，不该混进告警队列（理由见 app-update-entry 模块注释） */}
          <AppUpdateEntry collapsed={collapsed} onOpen={() => onOpenSettings("app")} />
        </div>

        {/* 分组：最近会话（真实 Agent 会话，按最近更新排序；折叠或成员形态时整组隐藏） */}
        {!collapsed && !isMember && <RecentSessions activeNav={activeNav} onSelect={onSelect} />}
      </nav>

      {/* 左下角：用户信息（无分割线，靠间距区隔）；折叠时只留头像 */}
      <div className="p-2.5 pt-1.5">
        <UserMenu onOpenSettings={onOpenSettings} collapsed={collapsed} />
      </div>
    </>
  );

  if (flat) {
    // 沉浸页的实色侧栏：只保留浮起卡片的形状语言，不透玻璃
    return <div className="panel--sidebar-flat flex h-full flex-col">{body}</div>;
  }
  return (
    <GlassPanel
      backgroundImage={backdrop}
      variant={glass.variant}
      className="panel--sidebar h-full"
      contentClassName="flex h-full flex-col"
      sampleBackground={glass.sampleBackground}
      settings={glass.settings}
      hairlineAlpha={glass.hairlineAlpha}
      fallbackAlpha={glass.fallbackAlpha}
    >
      {body}
    </GlassPanel>
  );
}

/** 头部的侧栏开合按钮：与搜索触发器同款的方形图标按钮 */
/**
 * 品牌 logo 的「回首页」外壳：点击等同于选中导航里的「新会话」（id: new），
 * 展开态包字标、折叠态包徽标，两处共用同一交互与 hover 反馈。
 */
function BrandHome({
  onSelect,
  homeId,
  children,
}: {
  onSelect: (id: string) => void;
  /** 「回首页」的落点：管理员是新会话页，成员是媒体库（成员没有 Agent 入口） */
  homeId: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(homeId)}
      aria-label="回到首页"
      title="回到首页"
      className="shrink-0 cursor-pointer transition-opacity hover:opacity-75"
    >
      {children}
    </button>
  );
}

function CollapseToggle({ collapsed, onClick }: { collapsed: boolean; onClick: () => void }) {
  const label = collapsed ? "展开侧栏" : "收起侧栏";
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      // 移动端 44px：这颗键在抽屉里就是「收起抽屉」，iOS HIG 的最小可点目标；
      // 桌面保持 32px 紧凑图标键
      className="glass-row !size-8 shrink-0 justify-center !p-0 max-md:!size-11"
    >
      <PanelLeftIcon className="size-[18px] max-md:size-[22px]" />
    </button>
  );
}

/** 触底预加载的提前量：距底部还有这么多像素就取下一页，滚到底时数据已就位。 */
const LOAD_MORE_THRESHOLD_PX = 120;

/**
 * 「最近会话」分组：独立滚动 + 触底增量加载 + 行尾相对时间。
 *
 * 单独成组件而不是写在 Sidebar 里，是为了把会话 store 的订阅限制在这棵子树：
 * 流式生成时 store 每 80ms 就更新一次，订阅写在 Sidebar 上会连带 WebGL
 * 玻璃面板一起重渲染。
 *
 * 分页有两个触发口：滚动触底，以及「列表没撑满容器」——大屏下首页 20 条
 * 可能撑不出滚动条，用户永远滚不到底，会误以为只有这些会话。
 */
function RecentSessions({
  activeNav,
  onSelect,
}: {
  activeNav: string;
  onSelect: (id: string) => void;
}) {
  const { conversations, hasMore, loadingMore, loadMore, fork, rename, remove } =
    useAgentConversations();
  const toast = useToast();
  const confirm = useConfirm();
  const prompt = usePrompt();
  const listRef = useRef<HTMLDivElement>(null);

  // 行尾展示的是「x 分钟前」，不重渲染就会一直停在打开页面那一刻。
  // 每分钟空转一次，代价只有这棵子树的一次 diff。
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  // 列表没撑满容器时主动补页（首屏数据少 / 高分屏侧栏很长 / 删到只剩几条）
  useEffect(() => {
    const el = listRef.current;
    if (!el || !hasMore || loadingMore) return;
    if (el.scrollHeight <= el.clientHeight) loadMore();
  }, [conversations.length, hasMore, loadingMore, loadMore]);

  const handleScroll = () => {
    const el = listRef.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight <= LOAD_MORE_THRESHOLD_PX) {
      loadMore();
    }
  };

  /** 重命名会话：空输入或未改动直接放弃。 */
  const handleRename = async (id: string, currentTitle: string) => {
    const input = await prompt({
      title: "重命名会话",
      initialValue: currentTitle,
      maxLength: 80,
    });
    if (input == null) return;
    const title = input.trim().slice(0, 80);
    if (!title || title === currentTitle) return;
    void rename(id, title).catch((error) => {
      toast.error(`重命名失败：${(error as Error).message}`);
    });
  };

  /** 把源会话上下文快照进独立新会话，成功后直接切到新会话页面。 */
  const handleFork = async (id: string) => {
    try {
      const targetId = await fork(id);
      onSelect(targetId);
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

  /** 彻底删除会话（二次确认）；删的是当前打开的会话时回到新会话页。 */
  const handleDelete = async (id: string, title: string) => {
    const ok = await confirm({
      title: `彻底删除会话「${title}」？`,
      description: "服务器上的完整对话记录将一并删除，此操作不可恢复。",
      confirmLabel: "彻底删除",
      tone: "danger",
    });
    if (!ok) return;
    void remove(id)
      .then(() => {
        if (activeNav === id) onSelect("new");
      })
      .catch((error) => {
        toast.error(`删除失败：${(error as Error).message}`);
      });
  };

  return (
    <div className="mt-6 flex min-h-0 flex-1 flex-col">
      <Section label="最近会话" icon={<ClockIcon className="size-3.5" />}>
        <div
          ref={listRef}
          onScroll={handleScroll}
          className="scroll-thin min-h-0 flex-1 space-y-0.5 overflow-y-auto"
        >
          {conversations.length === 0 ? (
            <p className="px-2.5 py-1 text-caption leading-5 text-[var(--text-faint)]">
              还没有会话，从「新会话」开始。
            </p>
          ) : (
            conversations.map((c) => (
              <RunRow
                key={c.id}
                title={c.title}
                running={c.running}
                time={formatRelativeTime(new Date(c.updatedAt).toISOString())}
                active={activeNav === c.id}
                onClick={() => onSelect(c.id)}
                onFork={() => void handleFork(c.id)}
                onCopyId={() => void handleCopyId(c.id)}
                onRename={() => void handleRename(c.id, c.title)}
                onDelete={() => void handleDelete(c.id, c.title)}
              />
            ))
          )}
          {loadingMore && (
            <p className="px-2.5 py-1.5 text-caption text-[var(--text-faint)]">加载中…</p>
          )}
        </div>
      </Section>
    </div>
  );
}

/** 分组外壳：标题固定，内容区（通常是可滚动列表）占满剩余高度。 */
function Section({
  label,
  icon,
  children,
}: {
  label: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1.5">
      <div className="flex shrink-0 items-center gap-1.5 px-2.5 pb-1">
        {icon && <span className="text-[var(--text-faint)]">{icon}</span>}
        <span className="group-label">{label}</span>
      </div>
      {children}
    </div>
  );
}

/**
 * 会话行：主体是跳转按钮；行尾叠一个「更多操作」按钮（悬停或菜单打开时可见，
 * 覆盖在时间标签的位置上）。操作菜单 Portal 到 body 展示——侧栏面板有
 * overflow 裁剪，且玻璃面板的层叠上下文会压住行内弹层。
 */
function RunRow({
  title,
  running,
  time,
  active,
  onClick,
  onFork,
  onCopyId,
  onRename,
  onDelete,
}: {
  title: string;
  running: boolean;
  time: string;
  active: boolean;
  onClick: () => void;
  onFork: () => void;
  onCopyId: () => void;
  onRename: () => void;
  onDelete: () => void;
}) {
  // 菜单打开状态即定位坐标（打开瞬间按触发按钮位置计算一次）
  const [menuPos, setMenuPos] = useState<{ left: number; top: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const open = menuPos != null;

  // 点击外部、按 Esc 或滚动时关闭（菜单在 body 里，需手动判定归属）
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
    <div className="group/run relative">
      <button
        type="button"
        data-active={active}
        onClick={onClick}
        title={`更新于 ${time}`}
        className="glass-row nav-item items-center gap-2.5 px-2.5 py-1 max-md:py-2"
      >
        {/* 状态点：仿 ChatGPT 的极简指示。容器始终占位（size-[7px]）以保证所有标题左对齐，
            但只有「运行中」才在其中画出小圆点 + 向外扩散的 ping 光晕；历史会话留空占位。 */}
        <span className="relative flex size-[7px] shrink-0 items-center justify-center">
          {running && (
            <>
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-[#6aa7ff] opacity-70" />
              <span className="relative size-[7px] rounded-full bg-[#6aa7ff]" />
            </>
          )}
        </span>
        {/* 标题占满剩余宽度，右端渐变透明淡出（无省略号）——用 mask 而非渐变色
            遮罩：侧栏底是 WebGL 玻璃，没有可匹配的实色。 */}
        <span className="flex-1 overflow-hidden whitespace-nowrap text-ui font-medium text-[var(--text)] [mask-image:linear-gradient(to_right,#000_calc(100%_-_16px),transparent)]">
          {title}
        </span>
        {/* 行尾相对时间。桌面端悬停/菜单打开时让位给 ⋯ 按钮（两者占同一块地方）；
            移动端 ⋯ 恒定可见（.touch-reveal），改用右外边距给它留出位置。 */}
        <span
          className={`shrink-0 text-caption text-[var(--text-faint)] transition-opacity duration-200 max-md:mr-10 ${
            open ? "opacity-0" : "group-hover/run:opacity-0"
          }`}
        >
          {time}
        </span>
      </button>

      <button
        ref={moreRef}
        type="button"
        aria-label="会话操作"
        data-active={open}
        onClick={(e) => {
          if (open) {
            setMenuPos(null);
            return;
          }
          const rect = e.currentTarget.getBoundingClientRect();
          setMenuPos({ left: rect.right - 176, top: rect.bottom + 6 });
        }}
        // 移动端：视觉放大一档（32px）+ .touch-target 把命中区撑到 44px（iOS HIG）
        className={`glass-row touch-target !absolute right-1.5 top-1/2 !size-6 -translate-y-1/2 justify-center !rounded-md !p-0 transition-opacity duration-200 max-md:!size-8 ${
          open ? "opacity-100" : "touch-reveal opacity-0 group-hover/run:opacity-100"
        }`}
      >
        <MoreIcon className="size-4 max-md:size-5" />
      </button>

      {open &&
        createPortal(
          <div
            ref={menuRef}
            className="menu-surface w-44 overflow-hidden p-1.5"
            // z 必须压过移动端抽屉（.mobile-drawer 是 60）：侧栏在窄屏上装进抽屉，
            // 菜单虽 Portal 到 body，z 不够会被抽屉盖住——表现为点 ⋯ 毫无反应
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
    </div>
  );
}
