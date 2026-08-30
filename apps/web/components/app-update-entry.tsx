"use client";

/**
 * 侧栏的更新入口（常驻环境徽标）。
 *
 * 产品语义：**更新不是「待处理事项」**。待处理事项的语义是"运行时故障，修好
 * 才消失"，而"有新版可用"既不是故障、也不该被用户"处理掉"——混进告警面板会
 * 让红色入口天天亮着，真故障反而被淹没（告警疲劳）。
 *
 * 形态对齐主流桌面产品的更新交互（Chrome 菜单圆点 / VS Code 齿轮角标 /
 * macOS 系统设置徽标），三条共同点：
 *   1. **不打断**：没有弹窗、没有全屏拦截，只是界面边缘多了一个安静的入口；
 *   2. **常驻**：不提供"忽略"——装了新版它自然消失，用户不需要做清理动作，
 *      也就不会因为误关而永远错过更新（这正是弹窗式提醒的老毛病）；
 *   3. **一跳直达**：点击直接落到能完成更新的那一页，且那一页进去就已经把
 *      新版本摆出来了（见 app-update-section 的 pending 预填），不需要用户
 *      再去找一颗"检查更新"按钮。
 *
 * 数据源是服务端的待更新快照（GET /app/update/pending，读库不触网），由每小时
 * 的定时检查与用户手动检查共同维护，因此这里可以放心地低频轮询。
 */

import { useCallback, useEffect, useState } from "react";

import { ChevronRightIcon, UpgradeIcon } from "@/components/icons";
import { getPendingUpdate, type PendingUpdateView } from "@/lib/api/app";
import { useSession } from "@/lib/session";

/** 轮询间隔：后端每小时才检查一次，前端跟得再紧也没有新信息，10 分钟足够 */
const POLL_MS = 600_000;

/** 更新提示色：用全站「正在进行 / 提示性」的 --info——是"有新东西"而非"出事了"，
 *  绝不能用告警的红/琥珀，否则又变回一条看起来像故障的提醒 */
const UPDATE_COLOR = "var(--info)";

/**
 * 读取待更新快照。返回 null 表示"无可用更新"（含尚未检查、非 Docker 部署、
 * 接口不可用）——一切不确定的情况都按"没有更新"处理，绝不误报。
 */
export function usePendingUpdate(enabled: boolean = true): PendingUpdateView | null {
  const [pending, setPending] = useState<PendingUpdateView | null>(null);

  const refresh = useCallback(() => {
    getPendingUpdate()
      .then((view) => setPending(view.app_version || view.model_tag ? view : null))
      .catch(() => {
        // 拉取失败（离线/后端重启）不打扰：保留上次结果，下轮轮询自愈
      });
  }, []);

  useEffect(() => {
    // 成员会话不轮询：更新接口是管理员专属，成员打过去只会收获一排 403
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

  return enabled ? pending : null;
}

/**
 * 侧栏主导航下方的一行更新入口：无更新时整行不渲染（与待处理事项同款的
 * "宁少勿多"控件密度原则）。折叠态退化为带蓝点的居中图标。
 */
export function AppUpdateEntry({
  collapsed,
  onOpen,
}: {
  collapsed: boolean;
  /** 跳到「设置 → 更新与维护」，那一页进去就会直接摆出新版本卡片 */
  onOpen: () => void;
}) {
  // 更新是管理员的事：成员看不到入口，也不发起轮询（接口是管理员专属）
  const { session } = useSession();
  const pending = usePendingUpdate(session.role !== "member");
  if (!pending) return null;

  // 应用与模型可能同时有更新：标题只说更要紧的那个（应用版本），
  // 模型单独有更新时才退化为模型文案——一行入口不塞两件事
  const label = pending.app_version
    ? `新版本 v${pending.app_version}`
    : `新识别模型 ${pending.model_tag}`;

  return (
    <button
      type="button"
      onClick={onOpen}
      title={collapsed ? label : undefined}
      className={`glass-row nav-item py-2 max-md:py-2.5 ${
        collapsed ? "justify-center px-0" : "px-3"
      }`}
      style={{ color: UPDATE_COLOR }}
    >
      <span className="relative shrink-0">
        <UpgradeIcon className="size-[18px] max-md:size-[22px]" />
        {collapsed && (
          <span
            className="absolute -right-1 -top-1 size-2 rounded-full"
            style={{ background: UPDATE_COLOR }}
          />
        )}
      </span>
      {!collapsed && (
        <>
          <span className="flex-1 truncate text-ui font-medium">{label}</span>
          <ChevronRightIcon className="size-4 shrink-0 opacity-70" />
        </>
      )}
    </button>
  );
}

/** 「设置 → 更新与维护」分区行右侧的小圆点：进了设置后仍能一眼找到更新在哪一栏。 */
export function AppUpdateDot() {
  return (
    <span
      aria-label="有可用更新"
      className="size-1.5 shrink-0 rounded-full"
      style={{ background: UPDATE_COLOR }}
    />
  );
}
