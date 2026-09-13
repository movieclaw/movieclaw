"use client";

import type { Route } from "next";
import Link from "next/link";

const TABS = [
  { key: "home", label: "首页", href: "/library" as Route },
  { key: "collections", label: "合集", href: "/library/collections" as Route },
] as const;

/**
 * 媒体库分区的视角切换：首页 ⇄ 合集。
 *
 * **形态照抄发现页的 TMDB / 豆瓣 分段控件**（`discover-view.tsx` 的 `SourceSwitcher`），
 * 一个像素都没改：全站的「视角切换」只有这一副长相，不新造交互词汇
 * （docs/design/activity.md 把这条写成了规矩）。
 *
 * **窄屏位置也照搬发现页**：媒体库首页与合集总览都是分区级的页面、没有 PageNav，
 * 切换器若在窄屏自己占一行，就会和全局顶栏摞成两排 header。所以移动端由调用方
 * 用 `setTopBarActions` 挂进全局顶栏那一行（字标与搜索之间本来就空着），桌面端
 * 留在页头右上角。两处调用的写法见 `library-view.tsx` 与 `all-collections-view.tsx`。
 *
 * **两个视角是两条真路由**，所以用 `<Link>` 而不是按钮：能预取、能中键新开、
 * 能复制地址。当前视角用 `aria-current="page"` 标注——这是导航，不是 tablist，
 * 别照着库页那个「作品 / 合集」去加 `role="tab"`，那边确实是同一页内换视图。
 *
 * 顺带交代一句：这已经是站内第五份同样的分段控件标记（另四份在 discover、
 * activity、subscriptions、library-detail）。没有顺手把五处统一成一个公共组件——
 * 那四处各自长出了变体（数量徽标、紧凑档、tablist 语义、桌面端另一套皮肤），
 * 归一是独立的一件事，不该夹在这次改动里搭车。
 */
export function LibrarySectionSwitch({
  current,
  className = "",
}: {
  current: "home" | "collections";
  /** 调用方用来对齐页头那一行（如 `mt-1`）；不要拿它改控件自身的长相 */
  className?: string;
}) {
  return (
    <div
      className={`flex shrink-0 rounded-full border border-white/10 bg-black/35 p-1 backdrop-blur-xl ${className}`}
    >
      {TABS.map((tab) => (
        <Link
          key={tab.key}
          href={tab.href}
          aria-current={current === tab.key ? "page" : undefined}
          className={`rounded-full px-4 py-1.5 text-sub font-semibold transition ${
            current === tab.key
              ? "bg-white/15 text-white shadow-sm"
              : "text-[var(--text-muted)] hover:text-white"
          }`}
        >
          {tab.label}
        </Link>
      ))}
    </div>
  );
}
