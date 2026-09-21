import type { ComponentType, ReactNode } from "react";
import type { Route } from "next";
import type { ThemeMeta } from "@/lib/themes";
import type { SettingsSidebarProps } from "@/components/settings-view";

/**
 * 主题坑位与定义的类型契约（docs/design/theme-framework/02）。
 *
 * 坑位（slot）= 主题可以替换的组件位。全部可选：解析时缺失的坑位回落基础实现
 * （registry.ts 的 BASE_SLOTS），没有基础实现的坑位解析为 undefined，
 * 消费方按「没有就不渲染」处理——与银玻璃现状语义一致。
 *
 * 纪律：本文件只做类型，不 import 组件实现（type-only 除外）；
 * 主题目录与业务组件都只经由 registry.ts 消费这份契约。
 */

/**
 * 主题能力声明：主题向框架声明自己用什么机制，框架据此配置底层。
 * 当前只有液态玻璃开关——vendor 不允许自己判断主题名（边界反转）。
 */
export interface ThemeCapabilities {
  /** 是否启用液态玻璃（WebGL 画布）。纯色平铺主题 = false */
  glass: boolean;
}

/** 桌面顶栏 props（结构级主题替换银玻璃的「侧栏布局」时消费） */
export interface DesktopTopNavProps {
  onSearch: () => void;
  onOpenSettings: () => void;
}

/** 详情页返回导航 props。基础实现消费 title/fallback，Netflix 实现消费 onBack/onPhoto */
export interface DetailNavProps {
  title: string;
  /** 无站内历史时的结构父级兜底（与 PageNav 的 fallback 同形） */
  fallback: { label: string; href: Route };
  onBack: () => void;
  /** 落在亮色画面上时用实底圆盘保底对比度（Netflix 实现消费） */
  onPhoto?: boolean;
}

/** 页面右上悬浮操作簇容器 props */
export interface PageActionsProps {
  children: ReactNode;
}

/**
 * 结构坑位清单。新增坑位的流程：消费点先写 `slots.xxx ?? 基础实现`，
 * 再在这里补类型——本接口就是坑位总账，不允许绕过注册表的私下替换。
 */
export interface ThemeSlots {
  /** 桌面顶栏。银玻璃 = undefined（内建侧栏布局，无全局顶栏） */
  desktopTopNav?: ComponentType<DesktopTopNavProps>;
  /** 移动端底部标签栏。银玻璃 = undefined（抽屉 + 全局顶栏，无底栏） */
  mobileTabBar?: ComponentType;
  /** 设置分区菜单。基础实现 = SettingsSidebar（玻璃面板 SaaS 菜单） */
  settingsNav?: ComponentType<SettingsSidebarProps>;
  /** 详情页返回导航。基础实现 = PageNav 工具条；Netflix = 浮动返回键 */
  detailNav?: ComponentType<DetailNavProps>;
  /** 页面右上悬浮操作簇容器。基础实现 = 页面内建吸顶工具条 */
  pageActions?: ComponentType<PageActionsProps>;
  /** 媒体库页顶 Hero。银玻璃 = undefined（LibraryView 内建页头） */
  libraryHero?: ComponentType;
}

/** 主题专属整页（路由壳在 app/(app)/——App Router 路由是构建期静态的） */
export interface ThemePages {
  my?: ComponentType;
  settingsIndex?: ComponentType;
  subscriptions?: ComponentType;
}

/** 一个主题的完整定义：主题目录的唯一出口（themes/<id>/theme.ts default export） */
export interface ThemeDefinition {
  meta: ThemeMeta;
  capabilities: ThemeCapabilities;
  slots: ThemeSlots;
  pages: ThemePages;
}

/** 解析结果：定义归并基础缺省后的产物，消费方只看这个 */
export interface ResolvedTheme {
  meta: ThemeMeta;
  capabilities: ThemeCapabilities;
  slots: ThemeSlots;
  pages: ThemePages;
}
