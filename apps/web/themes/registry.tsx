import { GlassTabBar } from "@/components/glass-tab-bar";
import { MobileSettingsNav } from "@/components/mobile-settings-nav";
import { MorePage } from "@/components/more-page";
import { PageNav } from "@/components/page-nav";
import { SettingsIndex } from "@/components/settings-index";
import { SettingsSidebar } from "@/components/settings-view";
import { DEFAULT_THEME_ID, normalizeThemeId, themeMeta } from "@/lib/themes";
import { useTheme } from "@/lib/ui-prefs";

import type {
  DetailNavProps,
  ResolvedSlots,
  ResolvedTheme,
  ThemeDefinition,
  ThemePages,
  ThemeSlots,
} from "./types";
import netflixTheme from "./netflix/theme";

/**
 * 主题坑位注册表（docs/design/theme-framework/01–02）。
 *
 * 主题知识在全站的唯一合法出口：业务组件消费 `useResolvedTheme()` 拿坑位与能力，
 * 不允许再出现 `theme.id === "<主题名>"` 的私下判断。依赖方向：
 *
 *   themes/<id>/** ──注册──▶ registry ──解析──▶ 业务组件
 *                                    （业务组件与主题目录互不 import）
 *
 * 「下载主题」预留：DEFINITIONS 本期静态 import（打包进 bundle）；落地下载时在
 * 这里增加动态 import 段，getResolvedTheme 的同步签名与兜底路径不变
 * （下载未就绪 / 加载失败 → 回落基础实现，见下方解析语义）。
 */

/**
 * 基础坑位缺省：银玻璃（默认主题）的实现就是「基础实现」本身。
 * 有基础实现的坑位才登记；没有基础实现的坑位（如 desktopTopNav）不在表里，
 * 解析结果为 undefined，消费方按「没有就不渲染」处理。
 *
 * 移动端三件套（底栏 / 设置返回条 / 设置分区列表页，见 BASE_PAGES）2026-09-23
 * 起有了基础实现：银玻璃移动端从「☰ 抽屉」改为 iOS 26 液态玻璃底栏
 * （docs/design/web-themes-mobile/04），Netflix 覆盖底栏、沿用后两者。
 */
const BASE_SLOTS: Partial<ThemeSlots> = {
  settingsNav: SettingsSidebar,
  detailNav: PageNavAdapter,
  mobileTabBar: GlassTabBar,
  mobileSettingsNav: MobileSettingsNav,
};

/** 基础整页：/my =「更多」页（底栏末位页签），/settings 移动端 = 分区列表页 */
const BASE_PAGES: ThemePages = {
  my: MorePage,
  settingsIndex: SettingsIndex,
};

/** 基础详情页返回导航：整条 PageNav 工具条（消费 title/fallback，忽略浮动键语义） */
function PageNavAdapter({ title, fallback }: DetailNavProps) {
  return <PageNav title={title} fallback={fallback} />;
}

/**
 * 主题定义注册表。约定：键 = 主题 id，值 = 主题目录的 default export。
 * dev 模式下对未知坑位名做断言（拼写错误在开发期暴露，不带进生产）。
 */
const DEFINITIONS: Record<string, ThemeDefinition> = {
  netflix: netflixTheme,
};

const KNOWN_SLOT_NAMES = new Set([
  "desktopTopNav",
  "mobileTabBar",
  "mobileSettingsNav",
  "settingsNav",
  "detailNav",
  "pageActions",
  "libraryHero",
]);

if (process.env.NODE_ENV !== "production") {
  for (const def of Object.values(DEFINITIONS)) {
    for (const name of Object.keys(def.slots)) {
      if (!KNOWN_SLOT_NAMES.has(name)) {
        console.error(`[themes] 主题「${def.meta.id}」注册了未知坑位「${name}」——检查拼写或先在 types.ts 立户`);
      }
    }
  }
}

/**
 * 解析主题：capabilities / slots / pages 全部归并基础缺省。
 * - 未知主题 id → normalizeThemeId 兜底为默认主题（缓存被改坏也不崩）；
 * - 缺失坑位 / 整页 → BASE_SLOTS / BASE_PAGES 基础实现；
 * - 无定义的主题（如下载未就绪）→ 纯基础实现，页面照常渲染。
 */
export function getResolvedTheme(id: string): ResolvedTheme {
  const def = DEFINITIONS[normalizeThemeId(id)];
  return {
    meta: def?.meta ?? themeMeta(DEFAULT_THEME_ID),
    capabilities: { glass: def?.capabilities.glass ?? true },
    slots: { ...BASE_SLOTS, ...def?.slots } as ResolvedSlots,
    pages: { ...BASE_PAGES, ...def?.pages },
  };
}

/** 便捷钩子：当前主题的解析结果（组件层统一从这里拿坑位与能力） */
export function useResolvedTheme(): ResolvedTheme {
  return getResolvedTheme(useTheme().id);
}
