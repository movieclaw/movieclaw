"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { NewTask } from "@/components/new-task";
import { useIsMobile } from "@/lib/use-media-query";
import { useResolvedTheme } from "@/themes/registry";

/**
 * 路由 / 按主题分支（docs/design/web-themes.md §0 决策 3）：
 *   - 银玻璃（默认）：AI 新任务输入台——全站唯一不铺蒙版的「氛围页」门面；
 *   - Netflix：**没有首页**（2026-09 修订：内容首页与媒体库合并成页，
 *     Billboard 移入 /library 页顶，见 themes/netflix/components/library-hero.tsx），
 *     / 仅剩老书签与历史链接会到达，replace 到 /library；
 *   - 银玻璃移动端：replace 到底栏首个页签 /discover/movie（2026-09-23，
 *     docs/design/web-themes-mobile/04）——手机上新会话不是高频操作，收进顶栏
 *     撰写键与「更多」，PWA 冷启动（start_url = /）直接落在发现。
 * 主题只存在于登录后的偏好 Context 里（服务端无值），因此这里是客户端组件。
 */
export default function HomePage() {
  const router = useRouter();
  // 主题自带全出血媒体库 Hero（libraryHero 坑位）= 内容首页并入 /library，
  // 「/」仅剩老书签与历史链接会到达，replace 到 /library
  const hasLibraryHero = useResolvedTheme().slots.libraryHero != null;
  const isMobile = useIsMobile();
  const redirectTo = hasLibraryHero ? "/library" : isMobile ? "/discover/movie" : null;

  useEffect(() => {
    if (redirectTo) router.replace(redirectTo);
  }, [redirectTo, router]);

  return redirectTo ? null : <NewTask />;
}
