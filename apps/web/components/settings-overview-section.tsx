"use client";

import Link from "next/link";

import { usePendingUpdate } from "@/components/app-update-entry";
import { ChevronRightIcon, UpgradeIcon } from "@/components/icons";
import { PipelineHealthPanel } from "@/components/subscription-settings-section";
import type { PendingUpdateView } from "@/lib/api/app";

/**
 * 「概览」分区：管理员进设置的落地页（/settings 重定向到这里）。
 *
 * 设计动机（新手体验）：设置有十几个分区，但新手真正的问题只有三个——
 * 哪些必须配、现在有什么问题、下一步做什么。概览用同一块面板回答全部：
 *   - 必要件（站点/下载器/媒体库）没配齐时，链路体检自动呈现为开局清单
 *     （三步引导 + 一键跳转），明确告诉新手其余分区都可以先不碰；
 *   - 配齐后变成逐库的链路体检：红黄项聚合成修复卡，每张卡给修复去处，
 *     问题主动找人，而不是让用户在分区里翻。
 *
 * 体检面板复用订阅规则分区的 PipelineHealthPanel（数据与判定同源，
 * 见 subscription-settings-section.tsx）；体检只读，修复动作全部跳回
 * 原配置页，概览绝不成为第二个配置入口。
 *
 * 更新提示卡与侧栏更新入口同一份快照数据（usePendingUpdate）：从别的
 * 入口进了设置也不漏看新版本；无更新时整卡不渲染，页面保持安静。
 */
export function SettingsOverviewSection() {
  // 本分区只对管理员渲染（成员的分区清单里没有 overview），轮询无需按角色关
  const pending = usePendingUpdate();

  return (
    <div className="space-y-10">
      {pending && <UpdateNoticeCard pending={pending} />}
      <PipelineHealthPanel />
    </div>
  );
}

/**
 * 更新提示卡：一行式的安静入口，点击直达「更新与维护」（那一页进去就已把
 * 新版本卡片摆出来）。用全站「提示性」的 --info 色而非告警色——有新版
 * 是"有新东西"，不是"出事了"（与 app-update-entry 的产品语义一致）。
 */
function UpdateNoticeCard({ pending }: { pending: PendingUpdateView }) {
  // 应用与模型可能同时有更新：只说更要紧的那个（应用版本），与侧栏入口同款
  const label = pending.app_version
    ? `新版本 v${pending.app_version} 可用`
    : `新识别模型 ${pending.model_tag} 可用`;
  return (
    <Link
      href="/settings/app"
      className="css-glass flex items-center gap-3 !rounded-2xl px-5 py-3.5 transition-colors hover:bg-white/[0.06]"
      style={{ color: "var(--info)" }}
    >
      <UpgradeIcon className="size-[18px] shrink-0" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body font-medium">{label}</span>
        <span className="mt-0.5 block text-caption text-[var(--text-muted)]">
          去「更新与维护」查看更新内容并一键升级
        </span>
      </span>
      <ChevronRightIcon className="size-4 shrink-0 opacity-70" />
    </Link>
  );
}
