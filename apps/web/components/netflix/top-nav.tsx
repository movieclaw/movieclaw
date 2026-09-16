"use client";

import Link from "next/link";
import type { Route } from "next";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { AccountSwitcherDialog } from "@/components/account-switcher-dialog";
import { AvatarBadge } from "@/components/avatar-badge";
import { MovieclawWordmark } from "@/components/netflix/brand";
import { ChevronDownIcon, LogoutIcon, UserIcon } from "@/components/icons";
import { AppUpdateEntry } from "@/components/app-update-entry";
import { NoticeCenter } from "@/components/notice-center";
import { SearchCommand, type SearchSubmitOptions } from "@/components/search-command";
import { logout } from "@/lib/api/auth";
import { clearBackdropCache } from "@/lib/backdrop-cache";
import type { SearchScope } from "@/lib/categories";
import { useAgentConversations } from "@/lib/agent-conversations";
import { accessiblePathFor, usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";
import { taskActivityBadge, useTaskActivity, type TaskActivityBadge } from "@/lib/task-activity";
import { clearUiPrefsCache } from "@/lib/ui-prefs-cache";

/**
 * Netflix 主题的桌面顶栏（≥768px，docs/design/web-themes.md §5.1）。
 *
 * - 高 68px、fixed，页面顶端透明（压一层向下渐隐的黑雾保证字标可读），
 *   任一滚动容器滚过阈值后过渡为 #141414 实底——Netflix 顶栏是实底不是毛玻璃，
 *   不引入 backdrop-filter。
 * - 左：品牌字标（回媒体库）+ 导航链接（激活 = 品牌红加粗——红只用于品牌
 *   标识与激活指示的 §4 纪律，未激活 = #e5e5e5、悬停变白）；
 *   <1100px 收敛为「浏览 ▾」下拉（Netflix 同款断点）。
 * - 右：「＋ 新任务」（白底黑字，AI 是本站差异能力，给一个 Netflix 没有但
 *   不破坏画面的入口）· 搜索（复用 SearchCommand 命令面板）· 通知铃 · 头像下拉。
 * - 侧栏的 nav.order 个人排序不作用于顶栏（记录在案的取舍）：顶栏链接是
 *   全局固定五项，权限过滤对齐侧栏的可见性判定（useVisibleNavItems 同口径）。
 */

/** 顶栏导航链接的全局固定清单（权限过滤对齐侧栏可见性判定）。
 *  无「首页」——内容首页已与媒体库合并（2026-09 修订），字标直达 /library。 */
const NAV_LINKS: { id: string; label: string; href: Route }[] = [
  { id: "movies", label: "电影", href: "/discover/movie" as Route },
  { id: "tv", label: "剧集", href: "/discover/tv" as Route },
  { id: "library", label: "媒体库", href: "/library" as Route },
  { id: "subscriptions", label: "我的订阅", href: "/subscriptions" as Route },
];

/** pathname → 顶栏激活项 id（与 NAV_LINKS 对齐；未命中返回空串，无高亮）。 */
function activeNavId(pathname: string): string {
  if (pathname.startsWith("/discover/movie")) return "movies";
  if (pathname.startsWith("/discover/tv")) return "tv";
  // / 是 /library 的别名（Netflix 主题下 replace 过去），高亮随媒体库
  if (pathname === "/" || pathname.startsWith("/library")) return "library";
  if (pathname.startsWith("/subscriptions")) return "subscriptions";
  return "";
}

export function NetflixTopNav({
  onSearch,
  onOpenSettings,
}: {
  onSearch: (keyword: string, scope: SearchScope, options?: SearchSubmitOptions) => void;
  onOpenSettings: (sectionId?: string) => void;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const { canSearch, canSubscribe, isAdmin } = usePermissions();
  const active = activeNavId(pathname);

  // 顶栏透明 → 实底的滚动判定：全站页面都是「外壳固定 + 内部容器滚动」，
  // window 本身不滚，用 capture 监听才能收到各页面滚动容器的事件。
  // 多个滚动容器并存时以最后滚动者为准——实际场景每页只有一条主滚动容器，
  // 且任何一条滚过阈值都意味着顶栏该落底了，判定足够稳。
  // 过渡不做成阈值翻转（bg 在 transparent/black 间跳变，观感生硬），而是把
  // 「滚入深度 0→1」连续写进 CSS 变量 --nf-nav-dim（24px 内保持全透明、
  // 24~404px 线性加深），黑底层按它连续淡入——滚动过程零 React 重渲染。
  // 变量已注册为 <number> 类型（globals.css 的 @property），.nf-topnav 上还
  // 挂了 450ms 的变量过渡：滚轮一次甩几百像素时黑底层也是缓动跟随，不会跳变。
  const rootRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const onScroll = (e: Event) => {
      const target = e.target;
      const top =
        target instanceof Element ? target.scrollTop : (target as Window | null)?.scrollY ?? 0;
      const progress = Math.min(1, Math.max(0, (top - 24) / 380));
      root.style.setProperty("--nf-nav-dim", progress.toFixed(3));
    };
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => document.removeEventListener("scroll", onScroll, { capture: true });
  }, []);
  // 顶栏常驻外壳、不随路由重建，滚动深度是跨页面残留的：上个页面滚过后跳到
  // 详情页，新页面停在顶部却没有滚动事件来纠正，顶栏会一直保持实底黑。路由
  // 切换即复位为页面顶端状态。复位与随后的「落位纠正」都要瞬时完成：媒体库
  // 等页面带着恢复的滚动位置回来时（滚动恢复在首帧绘制前写 scrollTop），纠正
  // 若参与 450ms 渐变，顶栏底色会在已经滚到深处的新页面上重放一遍
  // 「透明 → 黑」，观感即到达后闪一下。整个短窗口内压掉变量过渡（过渡属性
  // 挂 inline），等窗口过期再交还类上的缓动；在窗口内做过样式计算后再恢复
  // 才安全，靠墙钟 timeout 而非 rAF——rAF 在渲染节流（后台标签）下可能与
  // 写入挤进同一次样式计算，瞬时写入又被渐变接管（实测踩过）。
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    root.style.transition = "none";
    root.style.setProperty("--nf-nav-dim", "0");
    const resume = window.setTimeout(() => {
      root.style.transition = "";
    }, 250);
    return () => window.clearTimeout(resume);
  }, [pathname]);

  const visibleLinks = NAV_LINKS.filter((link) => link.id !== "subscriptions" || canSubscribe);

  return (
    <header ref={rootRef} className="nf-topnav fixed inset-x-0 top-0 z-40">
      {/* 两层背景随 --nf-nav-dim 连续叠合：底部永远铺「向下渐隐的黑雾」
          （页面顶端时黑字标在亮图上可读），黑实底层按滚动进度淡入盖过它。
          变量已注册（@property <number>），过渡写在变量自身上（.nf-topnav），
          直接给 opacity 加 transition 反而会被每帧写入的变量卡住（同
          PageNav --nav-reveal 的既知结论）。 */}
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-[linear-gradient(180deg,rgba(0,0,0,0.72),rgba(0,0,0,0)_100%)]"
      />
      <div aria-hidden="true" className="absolute inset-0 bg-black" style={{ opacity: "var(--nf-nav-dim, 0)" }} />
      <div className="relative flex h-[68px] items-center gap-6 px-[4vw]">
        {/* 品牌字标：回媒体库（Netflix 主题没有首页——内容首页与媒体库合并；
            内容可点区拉满高度，与导航链接同标准） */}
        <Link
          href="/library"
          aria-label="回到媒体库"
          className="flex shrink-0 items-center transition-opacity hover:opacity-80"
        >
          <MovieclawWordmark className="h-7 w-auto" />
        </Link>

        {/* 导航链接：≥1100px 全量展开 */}
        <nav className="hidden min-w-0 items-center gap-5 min-[1100px]:flex">
          {visibleLinks.map((link) => (
            <Link
              key={link.id}
              href={link.href}
              className={`shrink-0 text-[14px] transition-colors ${
                active === link.id
                  ? "font-bold text-[var(--accent)]"
                  : "font-normal text-[#e5e5e5] hover:text-white"
              }`}
            >
              {link.label}
            </Link>
          ))}
        </nav>

        {/* <1100px：链接收敛为「浏览 ▾」下拉 */}
        <BrowseDropdown links={visibleLinks} active={active} />

        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          {/* ＋ 新任务：品牌红实底主操作（Netflix 品牌语言：红只在主 CTA /
              进度条等少数位置出现，顶栏按钮是全站最醒目的一处） */}
          <button
            type="button"
            onClick={() => router.push("/new")}
            className="flex h-9 items-center gap-1.5 rounded-[4px] bg-[var(--accent)] px-3 text-[14px] font-semibold text-white transition-colors hover:bg-[var(--accent-strong)]"
          >
            <PlusGlyph />
            新任务
          </button>
          {canSearch && <SearchCommand onSearch={onSearch} triggerClassName="nf-icon-btn" />}
          <NoticeCenter collapsed variant="bell" />
          <NetflixAvatarMenu onOpenSettings={onOpenSettings} isAdmin={isAdmin} />
        </div>
      </div>
    </header>
  );
}

/** 「＋」字符字形：与文本基线对齐的一次性加号，不为此引入图标档位 */
function PlusGlyph() {
  return (
    <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth={2.4} aria-hidden="true">
      <path d="M12 5v14M5 12h14" strokeLinecap="round" />
    </svg>
  );
}

/** 「浏览 ▾」下拉：<1100px 的导航收敛形态（Netflix 同款交互）。 */
function BrowseDropdown({
  links,
  active,
}: {
  links: readonly { id: string; label: string; href: Route }[];
  active: string;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
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

  const currentLabel = links.find((link) => link.id === active)?.label ?? "浏览";

  return (
    <div ref={rootRef} className="relative min-[1100px]:hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex h-9 items-center gap-1 rounded-[4px] px-2 text-[14px] font-medium text-[#e5e5e5] transition-colors hover:text-white"
      >
        {currentLabel}
        <ChevronDownIcon className={`size-4 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div
          className="menu-surface absolute left-0 top-full z-50 mt-2 w-44 p-1.5"
          // .menu-surface 的无层 CSS position:relative 会压过 absolute 工具类
          // （无层规则优先于 @layer utilities），菜单因此脱离按钮锚点、被顶栏
          // flex 居中推出窗口——内联覆盖才能赢（同 composer.tsx / user-menu.tsx）
          style={{ position: "absolute" }}
        >
          {links.map((link) => (
            <Link
              key={link.id}
              href={link.href}
              onClick={() => setOpen(false)}
              className={`glass-row px-2.5 py-2 text-ui font-medium ${
                active === link.id ? "font-bold text-[var(--accent)]" : ""
              }`}
            >
              <span className="flex-1">{link.label}</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * 方角小头像下拉：设置 / 活动 / AI 会话 / 切换账号 / 退出。
 *
 * Netflix 原版头像菜单没有「设置」项——这是有意的偏离（功能需要，见设计
 * 文档 §2.5）；「AI 会话」是侧栏「最近会话」列表的收拢：顶栏放不下，收敛
 * 进头像下拉的一节（滚动列表而非真正的二级浮层，触达路径更短）。
 */
function NetflixAvatarMenu({
  onOpenSettings,
  isAdmin,
}: {
  onOpenSettings: (sectionId?: string) => void;
  isAdmin: boolean;
}) {
  const router = useRouter();
  const { session } = useSession();
  const { conversations } = useAgentConversations();
  const [open, setOpen] = useState(false);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

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

  /**
   * 退出登录：与侧栏 UserMenu 同一套语义——只退当前账号，本浏览器还有别的
   * 账号时后端自动切过去，没有了才去登录页；整页跳转重置全部前端状态。
   */
  const handleLogout = async () => {
    setOpen(false);
    let next: Awaited<ReturnType<typeof logout>> = null;
    try {
      next = await logout();
    } catch {
      // 即使请求失败（如网络断开），也照常跳登录页；会话在后端仍会自然过期
    }
    clearBackdropCache();
    clearUiPrefsCache();
    window.location.href = next ? accessiblePathFor(next, "/") : "/login";
  };

  return (
    <div ref={rootRef} className="relative">
      {open && (
        <div
          ref={menuRef}
          className="menu-surface absolute right-0 top-full z-50 mt-2 w-64 overflow-hidden p-1.5"
          // 同 BrowseDropdown：内联覆盖 .menu-surface 的无层 position:relative，
          // 否则菜单脱离按钮锚点、被顶栏 flex 布局推出窗口顶
          style={{ position: "absolute" }}
        >
          <div className="flex items-center gap-3 px-2.5 pb-2.5 pt-2">
            <AvatarBadge
              nickname={session.nickname}
              avatarUrl={session.avatar_url}
              className="size-9 text-ui"
            />
            <div className="min-w-0">
              <p className="truncate text-ui font-semibold text-[var(--text)]">
                {session.nickname}
              </p>
              <p className="truncate text-caption text-[var(--text-muted)]">@{session.username}</p>
            </div>
          </div>
          <div className="my-1 h-px bg-white/[0.08]" />
          {/* 更新入口：侧栏在 Netflix 主题下退役，常驻更新徽标由头像菜单承接
              （组件自轮询自鉴权，无更新时整行不渲染），一跳直达更新分区 */}
          <AppUpdateEntry
            collapsed={false}
            onOpen={() => {
              setOpen(false);
              onOpenSettings("app");
            }}
          />
          <MenuRow label="设置" onClick={() => { setOpen(false); onOpenSettings(); }} />
          {isAdmin && (
            <AvatarActivityRow
              onGo={(href) => {
                setOpen(false);
                router.push(href as Route);
              }}
            />
          )}
          {isAdmin && conversations.length > 0 && (
            <>
              <p className="group-label px-3 pb-1 pt-2.5">AI 会话</p>
              <div className="scroll-thin max-h-56 overflow-y-auto">
                {conversations.map((c) => (
                  <MenuRow
                    key={c.id}
                    label={c.title}
                    running={c.running}
                    onClick={() => {
                      setOpen(false);
                      router.push(`/sessions/${c.id}`);
                    }}
                  />
                ))}
              </div>
            </>
          )}
          <div className="my-1 h-px bg-white/[0.08]" />
          <MenuRow
            label="切换账号"
            icon={<UserIcon className="size-[18px]" />}
            onClick={() => {
              setOpen(false);
              setSwitcherOpen(true);
            }}
          />
          <MenuRow
            label="退出登录"
            danger
            icon={<LogoutIcon className="size-[18px]" />}
            onClick={() => void handleLogout()}
          />
        </div>
      )}
      <AccountSwitcherDialog open={switcherOpen} onClose={() => setSwitcherOpen(false)} />

      {/* 触发按钮：方角小头像（Netflix 头像是圆角方形，不做全圆） */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="账号菜单"
        className="nf-icon-btn"
      >
        <AvatarBadge
          nickname={session.nickname}
          avatarUrl={session.avatar_url}
          className="size-8 rounded-[4px] text-ui"
        />
      </button>
    </div>
  );
}

/** 头像菜单里的活动行：带任务角标（alert 红 / 进行中蓝）与动态落点——
 *  侧栏 JobCenter 的同一套徽标逻辑（taskActivityBadge）。拆成独立组件让
 *  任务快照变化只重渲染这一行；数据来自全站 Provider，本组件不发起请求。 */
function AvatarActivityRow({ onGo }: { onGo: (href: string) => void }) {
  const badge = taskActivityBadge(useTaskActivity());
  return (
    <MenuRow
      label="活动"
      badge={badge.count > 0 ? badge : undefined}
      onClick={() => onGo(badge.href)}
    />
  );
}

/** 头像下拉里的一行（glass-row 皮肤在 Netflix 主题下自动跟随 token）。 */
function MenuRow({
  label,
  onClick,
  icon,
  danger = false,
  running = false,
  badge,
}: {
  label: string;
  onClick: () => void;
  icon?: React.ReactNode;
  danger?: boolean;
  running?: boolean;
  /** 右缘状态角标（活动行的任务计数：alert 红 / 否则提示蓝） */
  badge?: TaskActivityBadge;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={badge?.hint ?? label}
      className={`glass-row px-3 py-2 text-ui font-medium max-md:py-2.5 ${
        danger ? "!text-[var(--danger)] hover:!bg-[rgba(255,107,107,0.12)]" : ""
      }`}
    >
      {running && (
        <span aria-hidden="true" className="size-1.5 shrink-0 animate-pulse rounded-full bg-[var(--info)]" />
      )}
      {icon && <span className="shrink-0 opacity-80">{icon}</span>}
      <span className="min-w-0 flex-1 truncate">{label}</span>
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
    </button>
  );
}
