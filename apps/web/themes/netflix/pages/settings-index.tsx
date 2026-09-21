"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";

import { ChevronRightIcon } from "@/components/icons";
import { settingsSectionGroupsFor } from "@/lib/mock-data";
import { useSession } from "@/lib/session";

/**
 * Netflix 主题移动端的设置分区列表页（路由 /settings，2026-09 修订）。
 *
 * 原实现是 NetflixSettingsNav 页顶的分区下拉浮层——分区一多（管理员 19 个）
 * 浮层高过视口又不能滚，长列表在触屏上滑不动；换分区的入口也藏在二级交互
 * 里。改为独立页面后：本页按组列出全部分区（glass-row 行，与 /my 同一套
 * 皮肤），点行进 /settings/[section]，页顶返回键回本页（见 NetflixSettingsNav）。
 * 桌面端不用本页（分区菜单在常驻侧栏）；银玻璃走重定向到首个分区，同样到不了
 * 这里。
 */
export function NetflixSettingsIndex() {
  const router = useRouter();
  const { session } = useSession();
  // 分区清单按角色过滤：成员只看到通用组，管理分区没有入口（后端 403 兜底）
  const groups = settingsSectionGroupsFor(session.role);

  return (
    <div className="scroll-thin scroll-safe h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-2xl px-4 pb-16 pt-5">
        {groups.map((group) => (
          <nav
            key={group.label || group.items[0]?.id}
            aria-label={group.label || "设置分区"}
            className="mt-6 space-y-0.5 first:mt-0"
          >
            {/* 概览组不设标题（label 为空串），空标题不渲染小节头 */}
            {group.label && <p className="group-label px-3 pb-1.5">{group.label}</p>}
            {group.items.map((section) => {
              const Icon = section.icon;
              return (
                <button
                  key={section.id}
                  type="button"
                  onClick={() => router.push(`/settings/${section.id}` as Route)}
                  className="glass-row w-full px-3 py-2 max-md:py-2.5 text-ui font-medium"
                >
                  {Icon && <Icon className="size-[18px] max-md:size-[22px] shrink-0" />}
                  <span className="min-w-0 flex-1 truncate text-left">{section.label}</span>
                  <ChevronRightIcon className="size-4 shrink-0 text-[var(--text-faint)]" />
                </button>
              );
            })}
          </nav>
        ))}
      </div>
    </div>
  );
}
