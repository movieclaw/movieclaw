"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useResolvedTheme } from "@/themes/registry";

/**
 * 「我的 / 更多」页视图：页面内容由主题坑位提供（基础实现 = components/more-page，
 * 银玻璃底栏的「更多」；Netflix = themes/netflix/pages/my-page，含主题自守卫）。
 * 解析结果恒有基础实现，下方的回工作台分支只在坑位被显式置空时兜底。
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
