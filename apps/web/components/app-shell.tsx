"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Image from "next/image";
import type { Route } from "next";
import { usePathname, useRouter } from "next/navigation";

/** 侧栏折叠态的本地持久化 key（设备级偏好，不进后端 ui.preferences） */
const SIDEBAR_COLLAPSED_KEY = "movieclaw.sidebar-collapsed";
/** 进设置前所在的工作台地址（含查询串），设置页「返回工作台」按原样回跳 */
const SETTINGS_RETURN_KEY = "movieclaw.settings-return";

import { FeedbackProvider } from "@/components/feedback";
import { MenuIcon } from "@/components/icons";
import { PAGE_NAV_BUTTON_CLASS } from "@/components/page-nav";
import { SearchCommand, type SearchSubmitOptions } from "@/components/search-command";
import { SettingsSidebar } from "@/components/settings-view";
import { Sidebar } from "@/components/sidebar";
import { SubscribeEntryProvider } from "@/components/subscribe-entry";
import { AgentConversationsProvider } from "@/lib/agent-conversations";
import { useAppNavigationTracking } from "@/lib/back-navigation";
import { BackdropProvider } from "@/lib/backdrop";
import type { SearchScope } from "@/lib/categories";
import { PageChromeProvider } from "@/lib/page-chrome";
import { SearchPrefsProvider } from "@/lib/search-prefs";
import { buildSearchPath } from "@/lib/search-url";
import { UiPrefsProvider } from "@/lib/ui-prefs";
import { useIsMobile } from "@/lib/use-media-query";
import { settingsSectionGroupsFor, settingsSections } from "@/lib/mock-data";
import { usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";

/**
 * 应用外壳：全站骨架布局（左栏 + 右区），所有导航态由 URL 驱动。
 *
 * 每个页面都是真实路由——刷新保留、可分享、前进后退可用：
 *   /                    新任务（氛围首页）
 *   /discover/movie|tv   发现电影 / 剧集
 *   /subscriptions       我的订阅
 *   /activity            活动（观看 / 任务）
 *   /media/[type]/[id]   影片详情
 *   /search?q=…          跨站搜索结果（范围/快照都在查询参数里）
 *   /sessions/[id]       AI 会话
 *   /settings/[section]  设置各分区
 *
 * 布局参考 Codex / Claude Code：两种模式共用「左栏 + 右区」两栏结构：
 *   - workspace（工作台）：左 = 常规侧边栏，右 = 当前路由页面
 *   - settings（设置）  ：左 = 设置分区菜单（含返回按钮），右 = 分区内容
 * 设置不是弹窗，而是整体替换左栏内容；模式由 pathname 是否在 /settings 下推导。
 */

/** pathname → 侧栏选中项 id（找不到对应项时返回空串，侧栏无高亮） */
function navIdFromPath(pathname: string): string {
  if (pathname === "/") return "new";
  if (pathname.startsWith("/library")) return "library";
  if (pathname.startsWith("/subscriptions")) return "subscriptions";
  if (pathname.startsWith("/activity")) return "tasks";
  if (pathname.startsWith("/discover/movie")) return "explore-movies";
  if (pathname.startsWith("/discover/tv")) return "explore-tv";
  const session = /^\/sessions\/([^/]+)$/.exec(pathname);
  return session ? session[1] : "";
}

/** 侧栏项 id → 路由地址（与 navIdFromPath 互逆） */
function pathOfNavId(id: string): Route {
  switch (id) {
    case "new":
      return "/";
    case "library":
      return "/library" as Route;
    case "subscriptions":
      return "/subscriptions";
    case "tasks":
      return "/activity" as Route;
    case "explore-movies":
      return "/discover/movie" as Route;
    case "explore-tv":
      return "/discover/tv" as Route;
    default:
      return `/sessions/${id}` as Route;
  }
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  // PageNav 用这份会话标记在 Safari 判断「真实上一页是否仍在站内」；Chromium
  // 会直接读取 Navigation API，不依赖该兼容层。
  useAppNavigationTracking(pathname);
  // 移动端（< 768px）走另一套骨架：单栏 + 顶栏 + 抽屉式侧栏，见文件末尾的分支渲染
  const isMobile = useIsMobile();
  // 抽屉开合（仅移动端有意义）
  const [drawerOpen, setDrawerOpen] = useState(false);
  // 本页是否自带顶栏（详情类页面的 PageNav 会自登记，见 lib/page-chrome.tsx）。
  // 计数而非布尔：路由切换时新旧页面短暂共存，先卸载的那个不能把状态清零。
  const [pageNavCount, setPageNavCount] = useState(0);
  const registerPageNav = useCallback(() => {
    setPageNavCount((n) => n + 1);
    return () => setPageNavCount((n) => n - 1);
  }, []);
  // 顶层页面挂进全局顶栏的页面级控件（如发现页的数据源切换），同样是为了
  // 不让它们自己吸一条顶栏、在窄屏上摞成两排。撤销时先比对身份，避免路由
  // 切换期间旧页面的清理把新页面刚挂上的控件顺手抹掉。
  const [topBarActions, setTopBarActionsState] = useState<React.ReactNode>(null);
  const setTopBarActions = useCallback((node: React.ReactNode) => {
    setTopBarActionsState(node);
    return () => setTopBarActionsState((current) => (current === node ? null : current));
  }, []);
  // 页面标题顶替顶栏字标（如会话页），见 lib/page-chrome.tsx 的 setTopBarTitle。
  // 存 token 对象而非裸字符串：撤销时按引用比对，新旧页面短暂共存且标题恰好
  // 相同时，先卸载那个的清理不会误清新页面刚挂上的标题。
  const [topBarTitle, setTopBarTitleState] = useState<{ text: string } | null>(null);
  const setTopBarTitle = useCallback((text: string) => {
    const token = { text };
    setTopBarTitleState(token);
    return () => setTopBarTitleState((current) => (current === token ? null : current));
  }, []);

  const isSettings = pathname.startsWith("/settings");
  const activeNav = navIdFromPath(pathname);
  // 设置分区从路径推导：/settings/appearance → appearance；/settings 兜底到首个分区
  const activeSettings = isSettings
    ? (pathname.split("/")[2] ?? settingsSections[0].id)
    : settingsSections[0].id;

  // 侧栏折叠态：收起后只留图标窄栏，主区铺开、更沉浸。存 localStorage 记住设备偏好；
  // AuthGate 确认登录后才在客户端渲染本组件，故初始化时可直接读取、无 SSR 水合问题。
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
    } catch {
      return false; // localStorage 不可用（隐私模式等）时仅失去记忆，功能不受影响
    }
  });

  const toggleSidebar = () => {
    setSidebarCollapsed((v) => {
      const next = !v;
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // 忽略写入失败
      }
      return next;
    });
  };

  /** 选中侧栏导航项：跳对应路由（离开搜索结果/详情页由路由切换自然完成）。 */
  const handleSelect = (id: string) => {
    router.push(pathOfNavId(id));
  };

  /**
   * 提交搜索：关键词 + 范围（标签换算而来）编码进 /search 的查询参数。
   * options.vertical 决定落地垂直（媒体/站点资源，见 URL 的 tab 参数）；
   * options.snapshotId 非空 = 预览某条历史的结果快照（结果页顶部有提示条与「重新搜索」）。
   */
  // useCallback：这个回调要放进 PageChrome 上下文，身份不稳定会让每次外壳
  // 重渲染都把上下文当作新值推给消费者
  const handleSearch = useCallback(
    (keyword: string, scope: SearchScope, options?: SearchSubmitOptions) => {
      router.push(
        buildSearchPath(
          { keyword, scope, snapshotId: options?.snapshotId },
          options?.vertical,
        ) as Route,
      );
    },
    [router],
  );

  /** 从用户菜单进入设置：记下当前工作台地址（含查询串），返回时原样回跳 */
  // 设置入口的默认分区按角色取可见清单的第一项：管理员落「概览」，
  // 成员的清单里没有概览，落「个人信息」——避免把成员送进一个 403 分区
  const { session } = useSession();
  const defaultSettingsSection =
    settingsSectionGroupsFor(session.role)[0]?.items[0]?.id ?? settingsSections[0].id;
  const openSettings = (sectionId: string = defaultSettingsSection) => {
    try {
      sessionStorage.setItem(
        SETTINGS_RETURN_KEY,
        window.location.pathname + window.location.search,
      );
    } catch {
      // 记不住就退回首页，不影响进入设置
    }
    router.push(`/settings/${sectionId}` as Route);
  };

  const backToWorkspace = () => {
    let target = "/";
    try {
      target = sessionStorage.getItem(SETTINGS_RETURN_KEY) || "/";
    } catch {
      // 读取失败回首页
    }
    // 这是退出设置模式而不是打开一个新页面；replace 避免浏览器后退后又回到设置。
    router.replace(target as Route);
  };

  // 全站布局规范：默认所有页面都铺一层模糊蒙版（.page-scrim）压住背景大图、
  // 突出页面主题内容；例外是「氛围页」——「新任务」首页（路由 /）与两个影片
  // 详情页（媒体库条目 /library/x/item/y 与发现页条目 /media/...）。两类详情都用
  // 页面内部的有限高度剧照，并由自身渐变保证内容可读。新增路由无需登记，
  // 自动继承蒙版。
  const isHome =
    pathname === "/" ||
    /^\/library\/\d+\/item\/\d+/.test(pathname) ||
    pathname.startsWith("/media/");
  // Agent 对话页走沉浸模式：蒙版换成完全不透明的 .page-solid，整页盖掉
  // 背景大图（密集文本页不允许透图）；侧栏切换为实色形态。
  const isImmersive = pathname.startsWith("/sessions/");

  // 沉浸路由标记：强刷时由 layout.tsx 的内联脚本在首帧绘制前打上（避免闪出
  // 背景大图），这里负责客户端路由切换时的双向同步——进入 /sessions/[id] 关掉背景
  // 大图伪元素，离开时恢复其他页面的大图。
  useEffect(() => {
    document.documentElement.classList.toggle("immersive-route", isImmersive);
  }, [isImmersive]);

  // 移动端顶栏归属：详情类页面自带 PageNav（返回 + 标题 + 页面操作），
  // 全局顶栏再叠一条就成了两层顶栏，于是把这一行让给页面自己（见 lib/page-chrome.tsx）。
  const showMobileTopBar = isMobile && pageNavCount === 0;
  const openDrawer = useCallback(() => setDrawerOpen(true), []);
  const pageChrome = useMemo(
    () => ({ registerPageNav, onSearch: handleSearch, openDrawer, setTopBarActions, setTopBarTitle }),
    [registerPageNav, handleSearch, openDrawer, setTopBarActions, setTopBarTitle],
  );

  // 移动端抽屉：切换路由即自动收起（点导航项跳走后抽屉不该还盖着新页面），
  // 回到桌面版式时也一并复位，避免再切回窄屏时抽屉莫名其妙已经开着。
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname, isMobile]);

  // 抽屉打开时按 Esc 关闭（外接键盘 / 平板场景）
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  // 侧栏本体：桌面常驻左栏、移动端装进抽屉，两处共用同一份实例。
  // 必须只渲染一份——面板是真实 WebGL 液态玻璃，多一份就多吃一个 WebGL 上下文。
  const sidebarNode = isSettings ? (
    <SettingsSidebar
      active={activeSettings}
      onSelect={(id) => router.push(`/settings/${id}` as Route)}
      onBack={backToWorkspace}
    />
  ) : (
    <Sidebar
      activeNav={activeNav}
      onSelect={handleSelect}
      onSearch={handleSearch}
      onOpenSettings={openSettings}
      // 移动端抽屉里没有「折叠成图标窄条」的意义（抽屉本身就是收起态）
      collapsed={!isMobile && sidebarCollapsed}
      onToggleCollapse={isMobile ? () => setDrawerOpen(false) : toggleSidebar}
      flat={isImmersive}
    />
  );

  return (
    // BackdropProvider 提供全站唯一的背景图数据源（CSS 大图 + 玻璃折射纹理），
    // 让「外观」设置里上传的图能同步作用到 body::before 与所有玻璃面板。
    <BackdropProvider>
    {/* FeedbackProvider：Toast 回执与确认/输入弹窗的全站唯一挂载点
      （见 components/feedback.tsx），取代原生 alert/confirm/prompt。 */}
    <FeedbackProvider>
    {/* SearchPrefsProvider / UiPrefsProvider：搜索偏好与界面样式的全站唯一数据源，
      应用启动各拉取一次、Context 共享，设置页的改动即时同步到所有消费页面。 */}
    <SearchPrefsProvider>
    <UiPrefsProvider>
    {/* AgentConversationsProvider：AI 会话的全站状态（侧栏最近会话 +
      /sessions/[id] 会话页共用），刷新后按会话编号自动回放未完成任务。 */}
    <AgentConversationsProvider>
    {/* SubscribeEntryProvider：海报卡片「订阅影片」按钮的全站入口，
      订阅弹层只在这里挂一份（见 components/subscribe-entry.tsx）。 */}
    <SubscribeEntryProvider>
    {/* PageChromeProvider：移动端顶栏的归属协商——页面自带 PageNav 时由它接管
      这一行，外壳撤掉自己的全局顶栏（见 lib/page-chrome.tsx）。 */}
    <PageChromeProvider value={pageChrome}>
    {/* 全屏背景蒙版（.page-scrim）：作为 .app-shell 的兄弟节点、
      z 介于 body::before(0) 与 app-shell(10) 之间：压住背景大图、托住内容。
      全站统一一档，模糊度/暗度由「设置 → 外观 → 界面质感」的滑杆驱动
      （--scrim-blur / --scrim-dark，见 lib/ui-prefs.tsx）；唯一例外是
      「新任务」首页——大图直出的氛围页门面，不铺蒙版（见 isHome）。 */}
    {!isHome && (
      <div className={isImmersive ? "page-solid" : "page-scrim"} aria-hidden="true" />
    )}
    {isMobile ? (
      /* —— 移动端骨架：单栏 + 顶栏，侧栏收进覆盖式抽屉 ——
         高度用 100dvh 而非 100vh：移动浏览器的地址栏会收放，100vh 取的是
         「地址栏收起后」的大视口，底部输入区会被推到屏幕外；dvh 跟随实际
         可视高度，是移动端唯一正确的满屏单位。
         再减 --keyboard-inset：dvh 不认软键盘（iOS 弹键盘不改布局视口），
         外壳得自己按键盘占高收缩，否则贴底的输入行落在键盘底下
         （见 components/viewport-keyboard.tsx）。无键盘时该值为 0。 */
      /* data-topbar：主区要不要为全局顶栏空出那 52px，取决于这一行有没有被
         页面自己的 PageNav 认领（对应 globals.css 的 .app-shell[data-topbar] 规则）。 */
      <div
        className="app-shell viewport-app-height relative z-10 w-full"
        data-topbar={showMobileTopBar}
      >
        {showMobileTopBar && (
          <MobileTopBar
            onMenu={openDrawer}
            onSearch={handleSearch}
            actions={topBarActions}
            title={topBarTitle?.text}
          />
        )}
        {/* 主区铺满外壳（absolute 而非 flex 子项）：全站页面清一色是
            「外层 h-full + 内层 overflow-y-auto」，h-full 要能解析就必须有一个
            确定的父高度——absolute inset-0 给的是确定值，flex-1 得来的高度在
            百分比解析上是灰色地带。让位顶栏与安全区的内边距由 globals.css
            的 .app-shell > main 统一提供。 */}
        <main className="absolute inset-0">{children}</main>
      </div>
    ) : (
      /* 浮起圆角卡片布局（对齐参考站 liquid-glass-oss）：外层留 padding、两栏留间隙，
        背景大图在卡片四周与中缝透出，面板作为浮于图上的玻璃卡片。
        app-shell 类名是给命令面板的「主界面后推」纵深效果用的锚点（见 globals.css 的
        body.cmdk-open .app-shell）：面板打开时整个外壳轻微缩放后退，浮层则漂在其上。
        高度同样减 --keyboard-inset：iPad 竖屏走的就是这一支布局，弹起软键盘时
        一样要把外壳收缩到键盘之上（桌面端该值恒为 0）。 */
      <div className="app-shell viewport-app-height relative z-10 flex w-full gap-3.5 p-3.5">
        {/* —— 左栏：浮起的玻璃侧栏卡片 ——
          宽度随折叠态动画（仅工作台可折叠；设置模式的分区菜单始终全宽）。 */}
        <aside
          className={`h-full shrink-0 transition-[width] duration-300 ease-[cubic-bezier(0.2,0.8,0.2,1)] ${
            !isSettings && sidebarCollapsed ? "w-[68px]" : "w-[300px]"
          }`}
        >
          {sidebarNode}
        </aside>

        {/* —— 右区：当前路由页面 —— */}
        <main className="h-full min-w-0 flex-1">{children}</main>
      </div>
    )}

    {/* —— 移动端抽屉 ——
      作为 .app-shell 的兄弟节点固定定位：外壳有缩放变换与内容裁剪，
      抽屉挂在里面会被一起缩放/裁掉。关闭时保持挂载（只做位移），
      侧栏的 WebGL 玻璃与最近会话列表因此不必反复销毁重建。 */}
    {isMobile && (
      <>
        {drawerOpen && (
          <button
            type="button"
            aria-label="关闭侧边栏"
            onClick={() => setDrawerOpen(false)}
            className="mobile-drawer-scrim cursor-default"
          />
        )}
        <div
          className="mobile-drawer"
          data-open={drawerOpen}
          aria-hidden={!drawerOpen}
          inert={!drawerOpen}
        >
          {sidebarNode}
        </div>
      </>
    )}
    </PageChromeProvider>
    </SubscribeEntryProvider>
    </AgentConversationsProvider>
    </UiPrefsProvider>
    </SearchPrefsProvider>
    </FeedbackProvider>
    </BackdropProvider>
  );
}

/**
 * 移动端顶栏：汉堡（唤起抽屉）+ 品牌字标（回首页）+ 搜索。
 *
 * 为什么是「浮在内容之上」而不是「占一行把内容推下去」：全站有一半页面是
 * 大图氛围页与 Hero 大剧照，顶栏若占位会在画面顶端切出一条硬边。这里做成
 * absolute 的渐隐雾层（同 PageNav 的处理）。内容的让位收口在 globals.css 的
 * 一条 `.app-shell > main` 规则里（安全区 + --mobile-topbar-h），
 * 各页面不必各写各的 padding，新增路由自动继承。
 *
 * 只放三个入口是有意为之：手机上顶栏的每一个图标都在跟内容抢宽度，
 * 导航（抽屉里全都有）、设置（抽屉底部用户菜单里）都不该在这里再占一格。
 */
function MobileTopBar({
  onMenu,
  onSearch,
  actions,
  title,
}: {
  onMenu: () => void;
  onSearch: (keyword: string, scope: SearchScope, options?: SearchSubmitOptions) => void;
  /** 当前页面挂上来的页面级控件（见 lib/page-chrome.tsx 的 setTopBarActions） */
  actions?: React.ReactNode;
  /** 当前页面挂上来的标题：有则顶替品牌字标（见 setTopBarTitle） */
  title?: string;
}) {
  const router = useRouter();
  const { canSearch } = usePermissions();
  return (
    <header className="mobile-topbar pointer-events-none absolute inset-x-0 top-0 z-40">
      <div className="pointer-events-auto flex h-[52px] items-center gap-2 px-3">
        {/* 复用子页面 PageNav 的圆形玻璃键（移动端 44px，iOS HIG 最小可点目标）：
            全局顶栏与详情页顶栏是同一层级的导航条，控件必须同一副长相。
            这里刻意不用真实 WebGL 液态玻璃（LiquidGlassIconButton）：每颗都是独立
            WebGL 上下文（移动 Safari 上限个位数，超限静默丢弃最老的上下文），且它
            只会折射静态背景大图——顶栏浮在滚动的海报墙上，CSS backdrop-blur 对真实
            内容实时取样反而更接近真玻璃（2026-07 实测对比后的结论）。 */}
        <button
          type="button"
          onClick={onMenu}
          aria-label="打开侧边栏"
          className={PAGE_NAV_BUTTON_CLASS}
        >
          <MenuIcon className="size-[22px]" />
        </button>
        {title ? (
          // 页面标题顶替字标：min-w-0 + truncate 让超长标题在汉堡与右侧控件
          // 之间安全截断成省略号，绝不把搜索键挤出屏幕或撑破顶栏
          <h1 className="min-w-0 flex-1 truncate text-body font-semibold tracking-[-0.01em] text-[var(--text)]">
            {title}
          </h1>
        ) : (
          /* 字标可点区拉到 44px 高（与图标键同标准）——图片本身保持 h-7 的视觉
             大小，命中区靠按钮撑起，否则 28px 高的字标在触屏上很难点中 */
          <button
            type="button"
            onClick={() => router.push("/")}
            aria-label="回到首页"
            className="flex h-11 shrink-0 items-center transition-opacity active:opacity-60"
          >
            <Image
              src={actions ? "/movieclaw-logo-mark-rotor.png" : "/movieclaw-logo-rotor.png"}
              alt="MovieClaw"
              width={actions ? 525 : 1920}
              height={525}
              priority
              className={actions ? "size-7 object-contain" : "h-7 w-auto max-w-[104px] object-contain"}
            />
          </button>
        )}
        {/* 页面级控件塞在字标与搜索之间——那段本来就空着，够放一个分段控件；
            min-w-0 让它在窄屏上自己收缩，而不是把搜索挤出屏幕 */}
        <div className="ml-auto flex min-w-0 shrink items-center gap-2">
          {actions}
          {canSearch && (
            <div className="shrink-0">
              <SearchCommand onSearch={onSearch} triggerClassName={PAGE_NAV_BUTTON_CLASS} />
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
