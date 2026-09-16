"use client";

import { AppUpdateDot, usePendingUpdate } from "@/components/app-update-entry";
import { ArrowLeftIcon } from "@/components/icons";
import { settingsSectionGroupsFor } from "@/lib/mock-data";
import { useSession } from "@/lib/session";

/**
 * Netflix 主题设置模式的左栏（桌面端专用，移动端走 /settings 分区列表页）。
 *
 * 银玻璃的 SettingsSidebar 是「玻璃面板 + 胶囊行」的 SaaS 菜单；Netflix 的
 * 画布是纯色平铺，再叠一块卡片面板就与整站语言脱节——Netflix 账户页的左侧
 * 导航是黑底上的纯文字列表。本组件照这个语言重排：未激活 #b3b3b3、悬停变白
 * 并衬一抹白色行底、激活白字加粗并在左缘挂一条品牌红指示条（红只用于激活
 * 指示，§4 纪律）。行内小图标保留（十几个分区纯文字难扫读），随文字同色、
 * 不设图标底座。分区清单与选中态语义与 SettingsSidebar 完全同源，只换皮。
 */
export function NetflixSettingsSidebar({
  active,
  onSelect,
  onBack,
}: {
  active: string;
  onSelect: (id: string) => void;
  onBack: () => void;
}) {
  // 成员只看到「账号」组（个人信息/外观）；管理分区后端一律 403，前端不给入口
  const { session } = useSession();
  const sectionGroups = settingsSectionGroupsFor(session.role);
  // 有可用更新时给「更新与维护」分区行点一颗小蓝点：与银玻璃侧栏同一份快照数据
  const pendingUpdate = usePendingUpdate(session.role !== "member");

  return (
    <div className="flex h-full flex-col pr-3">
      {/* 顶部返回：纯文字链接形态（Netflix 帮助中心的「‹ 返回」语言，
          不再是银玻璃的玻璃胶囊按钮） */}
      <div className="px-2 pb-2 pt-6">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-1.5 rounded-[4px] px-1 py-1 text-ui font-medium text-[var(--text-muted)] transition-colors hover:text-white"
        >
          <ArrowLeftIcon className="size-4" />
          <span>返回工作台</span>
        </button>
      </div>

      <nav className="scroll-thin flex-1 overflow-y-auto px-2 pb-6">
        {sectionGroups.map((group) => (
          // 概览组不设标题（label 为空串），空标题不渲染小节头；key 落到首个分区 id
          <div key={group.label || group.items[0]?.id} className="mt-5 first:mt-3">
            {group.label && <h3 className="group-label mb-1.5 px-2.5">{group.label}</h3>}
            <div className="space-y-0.5">
              {group.items.map((section) => {
                const Icon = section.icon;
                const isActive = active === section.id;
                return (
                  <button
                    key={section.id}
                    type="button"
                    data-active={isActive}
                    onClick={() => onSelect(section.id)}
                    className={`relative flex w-full items-center gap-2.5 rounded-[4px] px-2.5 py-[7px] text-left text-ui transition-colors hover:bg-white/[0.06] hover:text-white ${
                      isActive ? "font-bold text-white" : "font-medium text-[var(--text-muted)]"
                    }`}
                  >
                    {/* 激活指示：左缘品牌红短条（整站唯一直接用红的导航态） */}
                    {isActive && (
                      <span
                        aria-hidden="true"
                        className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-full bg-[var(--accent)]"
                      />
                    )}
                    <Icon className="size-4 shrink-0" />
                    <span className="min-w-0 flex-1 truncate">{section.label}</span>
                    {section.id === "app" && pendingUpdate && <AppUpdateDot />}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </nav>
    </div>
  );
}
