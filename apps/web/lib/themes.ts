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
}

export const THEMES: ThemeMeta[] = [
  {
    id: "silver",
    label: "银玻璃",
    description: "液态玻璃 · 冷银高光的控制台质感（默认）",
    preview: { bg: "#10131c", accent: "#cdd6e6" },
    themeColor: "#0a0b10",
    structural: false,
  },
  {
    id: "netflix",
    label: "Netflix",
    description: "纯色平铺 · 品牌红 · 顶栏与横版卡片行的影院浏览形态",
    preview: { bg: "#141414", accent: "#e50914" },
    themeColor: "#000000",
    structural: true,
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

/** 按 id 取主题元数据；未知 id（理论上传入前已 normalize）回落默认主题。 */
export function themeMeta(id: string): ThemeMeta {
  return THEMES.find((theme) => theme.id === id) ?? THEMES[0];
}
