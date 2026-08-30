"use client";

/**
 * 设置分区的异常/待办角标：数据源 hook + 小圆点 + 「分区 → 角标」映射。
 *
 * 产品语义（新手体验第三期）：管理员在任一设置分区，都能从侧栏一眼看到
 * 哪个分区有事——**异常**（站点验证失败 / 下载器连接失败 / 推送通道需
 * 重新绑定）用告警红点，**待办**（设备在等接入审批）用提示蓝点。两类
 * 绝不混色：待办不是故障，混进红色会制造告警疲劳（与 app-update-entry
 * 更新蓝点的取舍同一逻辑——那颗点保持现状，不归本模块管）。
 *
 * 数据是服务端一次聚合下发（GET /settings/health，判定与各分区页面同源），
 * 前端不逐分区轮询；轮询节奏沿用 usePendingUpdate 的模式：10 分钟 +
 * 窗口聚焦刷新——用户修完配置切回窗口，角标当场熄灭。
 *
 * 侧栏（settings-view 的 SettingsSidebar）与概览分区的异常聚合
 * （settings-overview-section）都吃这同一份数据、同一套映射，
 * 不存在「侧栏亮点、概览却说没事」的口径漂移。
 */

import { useCallback, useEffect, useState } from "react";

import { getSettingsHealth, type SettingsHealth } from "@/lib/api/settings";

/** 轮询间隔：与更新入口同款的低频节奏（10 分钟），聚焦刷新兜底及时性 */
const POLL_MS = 600_000;

/**
 * 读取设置健康聚合。返回 null 表示"没有数据"（未启用、首拉未完成、
 * 接口不可用）——不确定时一律按"无角标"处理，绝不误亮。
 */
export function useSettingsHealth(enabled: boolean = true): SettingsHealth | null {
  const [health, setHealth] = useState<SettingsHealth | null>(null);

  const refresh = useCallback(() => {
    getSettingsHealth()
      .then(setHealth)
      .catch(() => {
        // 拉取失败（离线/后端重启）不打扰：保留上次结果，下轮轮询自愈
      });
  }, []);

  useEffect(() => {
    // 成员会话不轮询：该接口是管理员专属，成员打过去只会收获 403
    if (!enabled) return;
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh, enabled]);

  return enabled ? health : null;
}

export type SettingsBadgeTone = "danger" | "info";

/**
 * 分区 id → 角标色调；null = 不点。侧栏与概览共用这一份映射，
 * 保证两处对"哪个分区有事"的回答永远一致。
 */
export function sectionBadgeTone(
  health: SettingsHealth | null,
  sectionId: string,
): SettingsBadgeTone | null {
  if (!health) return null;
  if (sectionId === "sites" && health.sites_failed > 0) return "danger";
  if (sectionId === "downloaders" && health.downloaders_failed > 0) return "danger";
  if (sectionId === "im-push" && health.im_push_need_rebind > 0) return "danger";
  if (sectionId === "devices" && health.device_requests_pending > 0) return "info";
  return null;
}

/** 分区行右侧的小圆点：异常红（--danger）/待办蓝（--info），与 AppUpdateDot 同款尺寸。 */
export function SettingsBadgeDot({ tone }: { tone: SettingsBadgeTone }) {
  return (
    <span
      aria-label={tone === "danger" ? "有异常需要处理" : "有请求等待操作"}
      className="size-1.5 shrink-0 rounded-full"
      style={{ background: tone === "danger" ? "var(--danger)" : "var(--info)" }}
    />
  );
}
