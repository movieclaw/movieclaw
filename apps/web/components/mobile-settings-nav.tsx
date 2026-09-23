"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useLayoutEffect } from "react";

import { ChevronLeftIcon } from "@/components/icons";
import { PAGE_NAV_BUTTON_CLASS } from "@/components/page-nav";
import { SearchCommand } from "@/components/search-command";
import { usePageChrome } from "@/lib/page-chrome";

/**
 * 移动端设置页的导航条：左侧返回键 + 页面标题 + 右侧搜索键（基础实现，两个
 * 主题共用）。原为 Netflix 主题专属——银玻璃的分区列表装在抽屉侧栏里；抽屉随
 * 液态玻璃底栏退役后（docs/design/web-themes-mobile/04），两个主题都由本条承接
 * 设置页的页顶导航。挂在页面内容顶部（外壳在 settings 路由下渲染）。
 *
 * **2026-09 修订**：原实现把分区选择做成标题旁的下拉浮层——分区一多
 * （管理员 19 个）浮层高过视口又不能滚，长列表在触屏上滑不动；且换分区的
 * 入口藏在二级交互里。分区选择改为独立路由页：/settings 列出全部分区
 * （components/settings-index.tsx），点行进 /settings/[section]。
 * 本条退化为纯导航：列表页显示「设置」、返回「我的」；分区页显示分区名、
 * 返回列表页（两级返回链由外壳按 pathname 计算）。
 *
 * 顶栏认领：挂载即 registerPageNav，让外壳撤掉全局顶栏（MobileTopBar）——
 * 否则设置页顶上摞两条顶栏（全局 52px + 本条），违背「窄屏永远只有一条
 * 顶栏」的收口原则（lib/page-chrome.tsx）。认领后 safe-top 由本条自己
 * 让出；左右安全区仍由 main 的内边距承担（globals.css 的
 * .app-shell[data-topbar="false"] 只清 padding-top），本条只补基础间距，
 * 不重复让位——与 PageNav 的做法同型。
 */
export function MobileSettingsNav({ title, backHref }: { title: string; backHref: Route }) {
  const router = useRouter();
  const chrome = usePageChrome();
  // 认领移动端顶栏那一行（注销函数即 effect 清理）。必须用 useLayoutEffect
  // 而不是 useEffect：登记要赶在浏览器绘制之前生效，否则外壳的全局顶栏会
  // 先画出一帧再被撤掉（PageNav 对同一机制记录过这个坑，见 components/page-nav.tsx）
  useLayoutEffect(() => chrome?.registerPageNav(), [chrome]);
  // 返回键语义是「回上级页」而不是「历史后退」：用户可能在分区间连续切换
  // （历史里堆着一串 /settings/*），按后退语义要逐级回退每个分区才能离开
  // 设置，与 iOS 设置页的返回心智不符。固定 replace 到上级，一次到位且不
  // 额外堆积历史。
  const back = () => router.replace(backHref);

  return (
    <div className="relative z-30 shrink-0 border-b border-[var(--line)] bg-[var(--bg)] py-2 px-2 pt-[calc(var(--safe-top)+0.5rem)]">
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={back}
          aria-label="返回"
          // 返回键与右侧搜索键同一套规格（PAGE_NAV_BUTTON_CLASS：size-9 /
          // pointer-coarse:size-11，按指针能力分档）：全站返回键同图标
          // （ChevronLeft）同尺寸档，不再用 !important 强制 44px 单档
          className={PAGE_NAV_BUTTON_CLASS}
        >
          <ChevronLeftIcon className="size-[18px] max-md:size-[22px]" />
        </button>
        <h1 className="min-w-0 flex-1 truncate px-1 text-title font-semibold text-[var(--text)]">
          {title}
        </h1>
        {/* 本条认领顶栏后，全局顶栏（含搜索键）被撤掉——搜索是其中唯一
            无处安放的入口，在这里补一颗（PageNav 对同一局面的既定做法）；
            底栏已带搜索圆钮时不补。
            必须条件渲染而不是 CSS 隐藏：SearchCommand 自带全局 ⌘K 监听，
            再挂一份会让一次快捷键把面板开了又关。 */}
        {chrome && !chrome.searchInTabBar && (
          <SearchCommand
            onSearch={chrome.onSearch}
            triggerClassName={`${PAGE_NAV_BUTTON_CLASS} ml-auto`}
          />
        )}
      </div>
    </div>
  );
}
