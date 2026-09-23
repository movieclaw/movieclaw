"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { BookmarkIcon, CompassIcon, LibraryIcon, UserIcon } from "@/components/icons";
import { usePermissions } from "@/lib/permissions";

/**
 * Netflix 主题的移动端底部标签栏（<768px，docs/design/web-themes.md §5.2）。
 *
 * 参照 Netflix App 的内容消费动线映射为 4 个**路由**页签：发现 / 媒体库 /
 * 订阅 / 我的（2026-09 修订：移除「首页」——内容首页与媒体库合并，/ 改由
 * 顶栏字标直达，底栏让位给高频的内容入口；「订阅」对齐桌面顶栏的「我的订阅」）。
 * 「订阅」按 canSubscribe 显隐，无权限时退化为 3 页签。
 * 栏高 49px + 底部安全区、激活白、未激活 --text-faint、图标 24px——这些数值
 * 无官方出处，按 iOS 惯例取值（设计文档标注的自家设计决策）。
 */

/** 页签基础清单（订阅由权限过滤补充，「我的」固定在末位）。 */
const BASE_TABS = [
  { id: "discover", label: "发现", href: "/discover/movie", Icon: CompassIcon },
  { id: "library", label: "媒体库", href: "/library", Icon: LibraryIcon },
] as const;

const SUBSCRIPTION_TAB = {
  id: "subscriptions",
  label: "订阅",
  href: "/subscriptions",
  Icon: BookmarkIcon,
} as const;

const MY_TAB = { id: "my", label: "我的", href: "/my", Icon: UserIcon } as const;

/** pathname → 当前页签 id（详情等子页落在所属的顶层页签上）。 */
function activeTabId(pathname: string): string {
  if (pathname.startsWith("/discover") || pathname.startsWith("/media")) return "discover";
  // / 是 /library 的别名（Netflix 主题下 replace 过去），高亮随媒体库
  if (pathname === "/" || pathname.startsWith("/library")) return "library";
  if (pathname.startsWith("/subscriptions")) return "subscriptions";
  // 设置是「我的」的二级页面（返回链固定 /settings/[x] → /settings → /my）：
  // iOS 惯例是二级页保持父页签高亮，进设置后四个页签全部熄灭会让用户失去
  // 「我在哪」的位置感
  if (pathname.startsWith("/settings") || pathname === "/my") return "my";
  return "";
}

export function NetflixTabBar() {
  const pathname = usePathname();
  const { canSubscribe } = usePermissions();
  const active = activeTabId(pathname);
  // 订阅页签按权限插在媒体库与我的之间；tab 数组重建的代价可忽略（4 个字面量）
  const tabs = canSubscribe ? [...BASE_TABS, SUBSCRIPTION_TAB, MY_TAB] : [...BASE_TABS, MY_TAB];

  return (
    <nav
      aria-label="主导航"
      className="nf-tabbar fixed inset-x-0 bottom-0 z-40 flex h-[calc(49px+var(--safe-bottom))] items-stretch border-t border-white/[0.06] pb-[var(--safe-bottom)]"
    >
      {tabs.map(({ id, label, href, Icon }) => (
        <Link
          key={id}
          href={href}
          aria-current={active === id ? "page" : undefined}
          className={`flex flex-1 flex-col items-center justify-center gap-0.5 ${
            active === id ? "text-white" : "text-[var(--text-faint)]"
          }`}
        >
          <Icon className="size-6" />
          <span className="text-micro font-medium leading-none">{label}</span>
        </Link>
      ))}
    </nav>
  );
}
