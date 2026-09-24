import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";

import { ViewportKeyboard } from "@/components/viewport-keyboard";
import { appleStartupImages } from "@/lib/apple-splash";
import { publicEnv } from "@/lib/env";
import { DEFAULT_THEME_ID, THEMES } from "@/lib/themes";

// 先引入液态玻璃组件自带的样式，再引入本项目的全局深色主题（后者可覆盖前者）。
import "@/vendor/liquid-glass/styles.css";
import "./globals.css";

// 拉丁字符/数字用 Inter（更克制、专业）；中文由 PingFang SC 等系统字体承接。
// 自托管可变字体（官方 InterVariable.woff2）：构建不再联网拉 Google Fonts，
// 避免代理不稳时构建长时间僵死。
const inter = localFont({
  src: "./fonts/InterVariable.woff2",
  variable: "--font-inter",
  display: "swap",
});

/**
 * 全站标题规范：主页（新任务）就是品牌名本身；子页面用「{页面名} · 品牌名」。
 * 静态子页在各自 page.tsx 里 export metadata（走这里的 template）；
 * 数据驱动的子页（库名/片名/会话名/搜索词）由 lib/use-page-title.ts 在
 * 数据就绪后写 document.title，格式与 template 保持一致。
 */
export const metadata: Metadata = {
  title: {
    default: publicEnv.appName,
    template: `%s · ${publicEnv.appName}`,
  },
  description: "Movieclaw 控制台 —— 液态玻璃风格的影视追踪工作台。",
  /**
   * iOS「添加到主屏幕」的 App 化配置（PWA 清单见 app/manifest.ts）：
   * - capable: 以独立 App 形态运行，去掉 Safari 地址栏与底部工具条；
   * - black-translucent: 状态栏完全透明、页面画到状态栏底下，配合
   *   viewport-fit=cover 与 --safe-top 让顶栏与系统状态栏无缝沉浸；
   * - title: 主屏图标下的名字（不写则取网页 title，可能带后缀）。
   */
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: publicEnv.appName,
    // 启动屏：iOS 不用 manifest 合成，必须逐设备给静态图（见 lib/apple-splash.ts）
    startupImage: appleStartupImages,
  },
  // iOS 会把「长得像电话号码」的数字自动变成蓝色 tel 链接，
  // 媒体条目页满屏都是年份/时长/容量数字，关掉自动识别
  formatDetection: {
    telephone: false,
  },
  // iOS 不读 manifest 里的 icons，主屏图标只认 apple-touch-icon
  icons: {
    apple: "/apple-touch-icon.png",
  },
  // Next 的 appleWebApp.capable 只输出新标准 mobile-web-app-capable；
  // 旧版 iOS（< 16.4，不读 manifest 的 display）只认 apple- 前缀，手动补上。
  other: {
    "apple-mobile-web-app-capable": "yes",
  },
};

/**
 * 视口规范（移动端适配的地基）：
 * - viewportFit: "cover" —— 让页面铺满刘海屏的整块屏幕，随后由各处的
 *   env(safe-area-inset-*) 把内容从刘海/胶囊/底部指示条里让开（见 globals.css
 *   的 --safe-* 变量）。不写它则 iOS 会自动留出黑边，背景大图铺不满。
 * - 不锁死缩放（不设 maximumScale）：捏合放大是无障碍的基本能力。iOS 上
 *   「聚焦小字号输入框自动放大页面」的问题另行解决——globals.css 在移动端
 *   把输入控件的字号提到 16px，从根上不触发那次自动缩放。
 */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#0a0b10",
};

/**
 * 防背景闪烁（FOUC）：--backdrop-image 正常由 BackdropProvider 在「挂载 +
 * GET /appearance 返回」之后写入，强刷时首帧会先画出内置默认图再切换、闪一下。
 * 这段内联脚本在首帧绘制前从 localStorage 恢复上次的背景变量（缓存由
 * lib/backdrop.tsx 在每次换图时写入；图片 URL 带版本号且强缓存，恢复是瞬时的）。
 * 只接受站内相对路径，缓存被篡改也注入不了外部地址。
 *
 * 沉浸路由（/sessions/[id]，Agent 对话页）：首帧绘制前给 <html> 打 immersive-route
 * 标记——背景大图的伪元素整个不渲染（也就不会发起图片请求），纯色层免淡入，
 * 强刷时不会闪出背景图。此时也无需恢复背景变量。客户端路由切换后的同步由
 * AppShell 的 effect 负责（见 components/app-shell.tsx）。
 */
const RESTORE_BACKDROP_SCRIPT = `try{if(location.pathname.indexOf("/sessions/")===0){document.documentElement.classList.add("immersive-route")}else{var u=localStorage.getItem("movieclaw.backdrop");if(u&&u.charAt(0)==="/")document.documentElement.style.setProperty("--backdrop-image",'url("'+u+'")')}}catch(e){}`;

/**
 * 主题防闪烁（FOUC）：与 next-themes 的标准做法同构——首帧绘制前读 ui.prefs
 * 的 localStorage 首帧缓存（lib/ui-prefs-cache.ts），把主题 id 写上 <html> 的
 * data-theme 属性。token 层（globals.css 的变量覆盖组）与 Tailwind 圆角换档
 * 都挂在 html[data-theme] 作用域上，属性就位即全站换肤，强刷不会先画银玻璃
 * 再跳成 Netflix。白名单由主题注册表生成（DEFAULT_THEME_ID 除外——默认主题 =
 * 无属性，与 ui-prefs 的写入口径一致），缓存被改坏也注入不了任意属性值；
 * 新增主题自动被脚本认识，无需改这里。登录后服务端偏好拉回、以及设置页
 * 切换主题时的后续同步由 lib/ui-prefs.tsx 的 effect 负责（AppShell 只在登录后渲染）。
 *
 * /play/*、/s/[slug] 不套 AppShell，同样吃到这段脚本与 token 层（结构层除外）。
 * Netflix 命中时顺带把 <meta name="theme-color"> 改成纯黑——强刷后浏览器地址
 * 栏 / PWA 状态栏不先闪一段银玻璃色（meta 由 viewport 导出注入，head 同步
 * 解析、脚本执行时已在；找不到时静默跳过。默认主题不命中映射，meta 维持
 * layout.tsx viewport 导出的原值，无需恢复动作）。
 *
 * 映射表从主题注册表序列化而来（ THEMES → 「id → themeColor」JSON），本组件是
 * 服务端组件、注册表是纯数据，拼接发生在 SSR 输出里，客户端零开销。
 */
const THEME_COLOR_MAP = JSON.stringify(
  Object.fromEntries(
    THEMES.filter((theme) => theme.id !== DEFAULT_THEME_ID).map((theme) => [theme.id, theme.themeColor]),
  ),
);
/**
 * 外壳让位契约的序列化（ThemeMeta.chrome →「id → 几何」JSON）：首帧绘制前按
 * 当前主题把 --mobile-topbar-h / --mobile-tabbar-h / --mobile-tabbar-offset 与
 * data-tabbar-mode 写到 <html> 上——globals.css 的让位规则只消费变量，主题
 * 不再自带全局布局规则（见 lib/themes.ts 的 ThemeChrome）。压缩成短键以省
 * 内联体积：t=顶栏让位高、m=底栏形态、b=底栏高、o=距底偏移表达式。
 */
const THEME_CHROME_MAP = JSON.stringify(
  Object.fromEntries(
    THEMES.map((theme) => [
      theme.id,
      {
        t: theme.chrome.mobileTopBarHeight,
        m: theme.chrome.mobileTabBar.mode,
        b: theme.chrome.mobileTabBar.height,
        o: theme.chrome.mobileTabBar.bottomOffset,
      },
    ]),
  ),
);
/**
 * 首帧主题与外壳几何的落位（同一份脚本，避免两个脚本的先后歧义）：
 * - 主题按**版式**解析：移动端（<768px）优先取 theme_mobile 覆盖、桌面端优先
 *   theme_desktop，都没有（老账号 / 未单独设置）回落 theme——与
 *   lib/ui-prefs.tsx 的 resolveThemeId 同一份语义，改一处要看另一处；
 * - 几何按解析出的主题查 chrome 映射，未知 id（缓存被改坏）回落银玻璃；
 * - data-theme / theme-color 只在非默认主题写（默认 = 无属性，与 ui-prefs
 *   的写入口径一致）。
 */
const RESTORE_THEME_SCRIPT = `try{var p=JSON.parse(localStorage.getItem("movieclaw.ui-prefs")||"null");var c=${THEME_COLOR_MAP};var k=${THEME_CHROME_MAP};var mobile=window.matchMedia&&matchMedia("(max-width:767px)").matches;var t=p?(mobile?(p.theme_mobile||p.theme):(p.theme_desktop||p.theme)):null;var ch=(t&&k[t])||k.silver;var d=document.documentElement;var s=d.style;s.setProperty("--mobile-topbar-h",ch.t+"px");s.setProperty("--mobile-tabbar-h",ch.b+"px");s.setProperty("--mobile-tabbar-offset",ch.o);if(ch.m==="docked")d.setAttribute("data-tabbar-mode","docked");else d.removeAttribute("data-tabbar-mode");if(t&&c[t]){d.setAttribute("data-theme",t);var m=document.querySelector('meta[name="theme-color"]');if(m)m.setAttribute("content",c[t])}}catch(e){}`;

/**
 * 视口超铺补偿的实测校准（--vp-overshoot，globals.css 该变量处的长注释）。
 * CSS 媒体查询按「视口比屏幕矮一个 safe-top」的老几何把它猜成 safe-top；iOS 26
 * 起 standalone 视口已铺满整屏，再减这一截会把贴底固定元素（液态玻璃底栏）推到
 * 物理底边之外——PWA 里切到银玻璃主题后底栏被屏幕下边界裁掉一半即是此因。
 * 改为实测「布局视口底边到屏幕物理底边的真实距离」（screen.height −
 * innerHeight），并以 safe-top 为上限（iPad 分屏等形态下 innerHeight 远小于
 * 屏幕，差值不是超铺量，退回 CSS 猜测值）：老系统实测差值 == safe-top，行为
 * 不变；新系统为 0，自动归位。必须在首帧绘制前执行——CSS 猜测值先就位、这里
 * 立刻覆写成内联样式，用户看不到跳动。旋转后的重测见 viewport-keyboard.tsx
 * 的 syncViewportOvershoot（与本段是同一份逻辑的两处副本，改一处要看另一处）。
 */
const SYNC_VIEWPORT_OVERSHOOT_SCRIPT = `try{if(window.matchMedia("(display-mode: standalone)").matches&&window.CSS&&CSS.supports("(-webkit-touch-callout: none)")){var d=document.documentElement,s=parseFloat(getComputedStyle(d).getPropertyValue("--safe-top"));if(isFinite(s)){var g=Math.min(Math.max(screen.height-window.innerHeight,0),s);d.style.setProperty("--vp-overshoot",g+"px")}}}catch(e){}`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // suppressHydrationWarning：body 最前的内联脚本会在水合前就给 <html> 写上
    // --backdrop-image 内联样式与 data-theme 属性（见上方两个 RESTORE_* 脚本），
    // 服务端首帧 HTML 里没有这些，两边必然不一致。这是「首帧防闪烁」的固有代价，
    // 用它抑制这一处预期内的告警（只作用于 <html> 自身属性，不影响子树里真正的
    // 水合问题被暴露）。
    <html lang="zh-CN" className={inter.variable} suppressHydrationWarning>
      <body>
        {/* 必须是 body 最前的同步内联脚本：解析即执行，赶在首帧绘制之前 */}
        <script dangerouslySetInnerHTML={{ __html: RESTORE_BACKDROP_SCRIPT }} />
        <script dangerouslySetInnerHTML={{ __html: RESTORE_THEME_SCRIPT }} />
        <script dangerouslySetInnerHTML={{ __html: SYNC_VIEWPORT_OVERSHOOT_SCRIPT }} />
        {/* 挂在根布局：登录页等 AppShell 之外的页面也有输入框，同样需要键盘适配 */}
        <ViewportKeyboard />
        {children}
      </body>
    </html>
  );
}
