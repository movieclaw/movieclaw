/**
 * 主题注册表 —— 多主题 UI 框架的唯一事实源（docs/design/web-themes.md §3.2）。
 *
 * 主题是这个功能里唯一会「全局生效」的东西，注册表刻意保持**无组件依赖的
 * 纯数据**（不 import React / 不 import 组件），便于单测与后续扩展。
 *
 * 生效机制分三层（同文档 §3.3）：
 *   1. token 层：globals.css 里 `html[data-theme="<id>"]` 下的语义变量覆盖组；
 *   2. Tailwind v4 主题变量层：同一覆盖组里的 `--radius-*` 换档（全站圆角）；
 *   3. 结构层：外壳按 `structural` 标记分支渲染（NetflixTopNav / NetflixTabBar）。
 *
 * 新增主题的步骤：
 *   - 这里注册一份 ThemeMeta；
 *   - globals.css 加对应该 id 的变量覆盖组（纯换肤主题只需第 1 层）；
 *   - 设置 → 外观的「主题」卡片自动出现（消费方遍历 THEMES 渲染）。
 */
/**
 * 主题的外壳几何声明（「让位契约」，2026-09-24）。
 *
 * 外壳与全局 CSS 不再为具体主题写让位规则，只消费这里声明、由防闪烁脚本
 * （layout.tsx）与 UiPrefsProvider（lib/ui-prefs.tsx）写到 <html> 上的三个
 * CSS 变量：--mobile-topbar-h / --mobile-tabbar-h / --mobile-tabbar-offset，
 * 以及 data-tabbar-mode 属性。作者优化基础主题的几何只改自己的声明，
 * 不会波及其他主题；全局规则里也不再出现 html:not([data-theme=…]) 这类
 * 反向排除选择器。
 */
export interface ThemeChrome {
  /**
   * 移动端顶栏让位高度（px）：顶栏浮在内容之上（雾层或悬浮胶囊），主区的
   * padding-top = safe-top + 此值。消费方：globals.css 的 .app-shell > main。
   */
  mobileTopBarHeight: number;
  /** 移动端底栏的让位形态与几何。 */
  mobileTabBar: {
    /**
     * docked = 实底停靠栏：main 整层让位（栏高 + safe-bottom），滚动容器
     * 末尾不留白；floating = 悬浮玻璃栏：内容从栏底穿过、一直滚到物理底边，
     * 滚动容器末尾按「offset + height + 12px」留白（.scroll-safe::after）；
     * none = 无底栏。
     */
    mode: "docked" | "floating" | "none";
    /** 底栏自身高度（px）。 */
    height: number;
    /**
     * 悬浮底栏距视口底边的偏移（CSS 长度表达式，可引用 --safe-bottom 等
     * 变量；仅 floating 消费）。
     */
    bottomOffset: string;
  };
}

export interface ThemeMeta {
  id: string;
  /** 设置页展示名 */
  label: string;
  /** 一句话描述 */
  description: string;
  /** 外观选择卡的预览缩略：两个主色即可拼出观感 */
  preview: { bg: string; accent: string };
  /**
   * 浏览器地址栏 / PWA 状态栏颜色。layout.tsx 的防闪烁脚本由注册表序列化出
   * 「主题 id → themeColor」映射，首帧绘制前随 data-theme 一起写入
   * <meta name="theme-color">——新增主题自动被脚本认识，无需改脚本。
   */
  themeColor: string;
  /**
   * 结构级差异标记：true = 该主题会更换应用外壳（顶栏 / 底部标签栏 / 内容首页），
   * AppShell 据此走独立的结构分支；纯换肤主题恒为 false。
   */
  structural: boolean;
  /** 外壳几何声明（让位契约，见上方 ThemeChrome）。新主题必须声明；漏了由
   *  themeChrome() 兜底为默认主题的声明。 */
  chrome: ThemeChrome;
}

/** 银玻璃（默认主题）的悬浮底栏距底偏移：距物理底边约 22px——有 Home 指示条时
 *  = safe-bottom − 12px。只按视口算，不减 --vp-overshoot：#442 真机（iOS 26
 *  PWA）实测视口底边就是屏幕物理底边，减了会把整条底栏推出屏幕、只剩一条边
 *  可见（该补偿量只给铺底层用，与 globals.css 的 --tabbar-bottom 同式）。 */
const FLOATING_TABBAR_BOTTOM_OFFSET = "max(22px, calc(var(--safe-bottom) - 12px))";

export const THEMES: ThemeMeta[] = [
  {
    id: "silver",
    label: "银玻璃",
    description: "液态玻璃 · 冷银高光的控制台质感（默认）",
    preview: { bg: "#10131c", accent: "#cdd6e6" },
    themeColor: "#0a0b10",
    structural: false,
    chrome: {
      mobileTopBarHeight: 52,
      mobileTabBar: { mode: "floating", height: 62, bottomOffset: FLOATING_TABBAR_BOTTOM_OFFSET },
    },
  },
  {
    id: "netflix",
    label: "Netflix",
    description: "纯色平铺 · 品牌红 · 顶栏与横版卡片行的影院浏览形态",
    preview: { bg: "#141414", accent: "#e50914" },
    themeColor: "#000000",
    structural: true,
    chrome: {
      // 雾层顶栏让位 52px；实底标签栏 49px 停靠，main 整层让位（不含 safe-bottom，
      // 消费方自加）——与 NetflixTabBar / tokens.css 的既有几何一致
      mobileTopBarHeight: 52,
      mobileTabBar: { mode: "docked", height: 49, bottomOffset: "0px" },
    },
  },
];

export const DEFAULT_THEME_ID = "silver";

const THEME_ID_SET = new Set(THEMES.map((theme) => theme.id));

/**
 * 未知值兜底为默认主题：偏好可能来自老后端（不认识该字段）、手改的
 * localStorage 缓存或未来被下线的主题 id，消费方拿到的必须是注册表里的
 * 合法 id，绝不能把垃圾值写到 <html> 的 data-theme 上。
 */
export function normalizeThemeId(value: unknown): string {
  return typeof value === "string" && THEME_ID_SET.has(value) ? value : DEFAULT_THEME_ID;
}

/** 是否为注册表里的合法主题 id：按端主题的覆盖字段用——非法值按「未设置」处理
 *  （回落通用主题），而不是像 normalizeThemeId 那样兜成默认主题。 */
export function isValidThemeId(value: unknown): value is string {
  return typeof value === "string" && THEME_ID_SET.has(value);
}

/** 按 id 取主题元数据；未知 id（理论上传入前已 normalize）回落默认主题。 */
export function themeMeta(id: string): ThemeMeta {
  return THEMES.find((theme) => theme.id === id) ?? THEMES[0];
}
