"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useResolvedTheme } from "@/themes/registry";
import { settingsSections } from "@/lib/mock-data";
import { useIsMobile } from "@/lib/use-media-query";
import { useThemeState } from "@/lib/ui-prefs";

/**
 * /settings 裸地址按形态分支：
 *   - 移动端（两个主题）：设置分区列表页（components/settings-index.tsx；原为
 *     Netflix 专属，银玻璃抽屉侧栏随液态玻璃底栏退役后上移为基础实现）；
 *   - 桌面端：重定向到首个分区，保证设置页
 *     始终有明确的分区地址。首个分区即「概览」落地页——管理员进设置先看到
 *     配置状态与下一步；成员没有概览（见 MEMBER_SECTION_IDS），SettingsPanel
 *     会兜底到个人信息。桌面端分区菜单在常驻侧栏，列表页反而多一跳。
 *
 * 主题只存在于客户端偏好 Context（服务端无值），因此这里是客户端组件。
 * **重定向必须等偏好落定**（useThemeState 的 loading）：首帧主题取自
 * localStorage 缓存，冷缓存时它是 silver，不等就会把 Netflix 移动端用户
 * 直接 replace 到 /settings/overview——分区列表页整个被跳过，而返回链
 * （/settings/[x] → /settings → /my）又正好落在这一页上，等于回退路径
 * 也一起断了。useIsMobile 首帧读的是真实 matchMedia，无需等待。
 */
export default function SettingsIndexPage() {
  const router = useRouter();
  const { loading } = useThemeState();
  const isMobile = useIsMobile();
  const { pages } = useResolvedTheme();
  const SettingsIndex = pages.settingsIndex;
  const isIndexForm = isMobile && SettingsIndex != null;

  useEffect(() => {
    if (!loading && !isIndexForm) router.replace(`/settings/${settingsSections[0].id}`);
  }, [loading, isIndexForm, router]);

  return isIndexForm && !loading && SettingsIndex ? <SettingsIndex /> : null;
}
