"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useResolvedTheme } from "@/themes/registry";

/**
 * 「我的」页视图：页面内容由主题坑位提供（Netflix = themes/netflix/pages/my-page，
 * 含主题自守卫）；未提供该页的主题（银玻璃）没有此入口的价值承载，直达时回工作台。
 */
export function MyPageView() {
  const router = useRouter();
  const { pages } = useResolvedTheme();
  const MyPageContent = pages.my;

  useEffect(() => {
    if (!MyPageContent) router.replace("/");
  }, [MyPageContent, router]);

  return MyPageContent ? <MyPageContent /> : null;
}
