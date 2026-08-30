"use client";

import Link from "next/link";

import { usePendingUpdate } from "@/components/app-update-entry";
import {
  ChatIcon,
  ChevronRightIcon,
  DeviceIcon,
  DownloadIcon,
  ServerIcon,
  UpgradeIcon,
} from "@/components/icons";
import { useSettingsHealth, type SettingsBadgeTone } from "@/components/settings-health";
import { PipelineHealthPanel } from "@/components/subscription-settings-section";
import type { PendingUpdateView } from "@/lib/api/app";
import type { SettingsHealth } from "@/lib/api/settings";

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
 * 异常/待办聚合卡与设置侧栏的分区角标吃同一份数据（useSettingsHealth，
 * 服务端 GET /settings/health 一次聚合下发）：侧栏红点指向哪个分区，
 * 这里就有一张说明卡讲清楚是什么事、点击直达修复处——两处永远一致。
 *
 * 更新提示卡与侧栏更新入口同一份快照数据（usePendingUpdate）：从别的
 * 入口进了设置也不漏看新版本；无更新时整卡不渲染，页面保持安静。
 */
export function SettingsOverviewSection() {
  // 本分区只对管理员渲染（成员的分区清单里没有 overview），轮询无需按角色关
  const pending = usePendingUpdate();
  const health = useSettingsHealth();

  return (
    <div className="space-y-10">
      <SectionHealthNotices health={health} />
      {pending && <UpdateNoticeCard pending={pending} />}
      <PipelineHealthPanel />
    </div>
  );
}

/**
 * 分区异常/待办聚合卡：每条对应一颗侧栏角标，红色是异常（要修）、
 * 蓝色是待办（有人在等你操作），点击直达对应分区。全部为零时整块不渲染。
 */
function SectionHealthNotices({ health }: { health: SettingsHealth | null }) {
  if (!health) return null;
  const notices = [
    {
      count: health.sites_failed,
      tone: "danger" as SettingsBadgeTone,
      icon: ServerIcon,
      label: `${health.sites_failed} 个资源站点验证失败`,
      hint: "订阅在这些站点搜不到资源，去「资源站点」修复登录态（如更新 Cookie）",
      href: "/settings/sites",
    },
    {
      count: health.downloaders_failed,
      tone: "danger" as SettingsBadgeTone,
      icon: DownloadIcon,
      label: `${health.downloaders_failed} 个下载器连接失败`,
      hint: "无法向这些下载器投递任务，去「下载器」检查地址与凭据",
      href: "/settings/downloaders",
    },
    {
      count: health.im_push_need_rebind,
      tone: "danger" as SettingsBadgeTone,
      icon: ChatIcon,
      label: `${health.im_push_need_rebind} 个推送通道需重新绑定`,
      hint: "凭据已失效，推送与对话都收不到，去「消息推送」重新绑定",
      href: "/settings/im-push",
    },
    {
      count: health.device_requests_pending,
      tone: "info" as SettingsBadgeTone,
      icon: DeviceIcon,
      label: `${health.device_requests_pending} 台设备在等待接入审批`,
      hint: "有设备发起了配对请求，去「设备」核对配对码后批准或拒绝",
      href: "/settings/devices",
    },
  ].filter((n) => n.count > 0);
  if (notices.length === 0) return null;

  return (
    <div className="space-y-3">
      {notices.map((notice) => {
        const Icon = notice.icon;
        return (
          <Link
            key={notice.href}
            href={notice.href as never}
            className="css-glass flex items-center gap-3 !rounded-2xl px-5 py-3.5 transition-colors hover:bg-white/[0.06]"
            style={{ color: notice.tone === "danger" ? "var(--danger)" : "var(--info)" }}
          >
            <Icon className="size-[18px] shrink-0" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-body font-medium">{notice.label}</span>
              <span className="mt-0.5 block text-caption text-[var(--text-muted)]">
                {notice.hint}
              </span>
            </span>
            <ChevronRightIcon className="size-4 shrink-0 opacity-70" />
          </Link>
        );
      })}
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
