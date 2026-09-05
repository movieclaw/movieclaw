"use client";

/**
 * 待处理事项（状态化告警中心）的全局入口 + 面板。
 *
 * 产品语义（与后端 system_notice 对齐）：这里只展示"需要用户行动才能推进
 * 的运行时故障"——正常运行时列表为空，入口**整个不渲染**（宁少勿多的
 * 控件密度原则）；一旦亮起就是真问题。问题修好后服务端自动消退，入口
 * 随之消失，用户不需要"清理通知"。
 *
 * 形态：侧栏主导航下方一行红色入口（折叠态为带红点的居中图标），点击
 * 打开居中弹窗列出全部事项；每条可跳转到能修它的页面，或手动忽略。
 * 轮询 30s 一次 + 窗口回焦立即刷一次——查询极轻（active 空表短路）。
 */

import { useCallback, useEffect, useState } from "react";

import { useSession } from "@/lib/session";
import type { Route } from "next";
import { useRouter } from "next/navigation";

import { HandoffButton } from "@/components/handoff-button";
import { BellIcon, ChevronRightIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import {
  dismissNotice,
  fetchActiveNotices,
  noticeHref,
  type SystemNotice,
} from "@/lib/api/notices";
import { formatRelativeTime } from "@/lib/time";

const POLL_MS = 30_000;

/** 严重度圆点颜色：直接取全站状态色，不另立色值 */
const DOT_COLOR: Record<string, string> = {
  error: "var(--danger)",
  warning: "var(--warn)",
};

export function NoticeCenter({ collapsed }: { collapsed: boolean }) {
  // 系统通知是运维告警（管理员专属接口）：成员不渲染入口、也不发起轮询
  const { session } = useSession();
  if (session.role === "member") return null;
  return <NoticeCenterInner collapsed={collapsed} />;
}

function NoticeCenterInner({ collapsed }: { collapsed: boolean }) {
  const [notices, setNotices] = useState<SystemNotice[]>([]);
  const [open, setOpen] = useState(false);
  const router = useRouter();

  const refresh = useCallback(() => {
    fetchActiveNotices()
      .then(setNotices)
      .catch(() => {
        // 拉取失败（离线/后端重启）不打扰：保留上次结果，下轮轮询自愈
      });
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh]);

  // 目录级根因告警（payload.group_key）存在时，被它收编的单种子告警
  // （payload.grouped_under 指向那个 key）折叠不显示：用户看到的是一条
  // "这个目录 movieclaw 看不到"，而不是 16 条"《某剧》无法入库"。
  // 子告警在服务端仍活跃——任务卡片靠它们挂"无法入库"红标，两边不冲突
  const groupKeys = new Set(notices.map((n) => n.payload.group_key).filter(Boolean));
  const visible = notices.filter(
    (n) => !n.payload.grouped_under || !groupKeys.has(n.payload.grouped_under),
  );
  if (visible.length === 0) return null;

  const goto = (notice: SystemNotice) => {
    setOpen(false);
    router.push(noticeHref(notice) as Route);
  };

  const dismiss = (id: number) => {
    // 乐观移除：dismiss 幂等且失败无害（下轮轮询会拉回真实状态）
    setNotices((prev) => prev.filter((n) => n.id !== id));
    void dismissNotice(id).catch(refresh);
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={collapsed ? `待处理（${visible.length}）` : undefined}
        className={`glass-row nav-item py-2 !text-[var(--danger)] max-md:py-2.5 ${
          collapsed ? "justify-center px-0" : "px-3"
        }`}
      >
        <span className="relative shrink-0">
          <BellIcon className="size-[18px] max-md:size-[22px]" />
          {collapsed && (
            <span className="absolute -right-1 -top-1 size-2 rounded-full bg-[var(--danger)]" />
          )}
        </span>
        {!collapsed && (
          <>
            <span className="flex-1 text-ui font-medium">待处理</span>
            <span className="rounded-full bg-[var(--danger-solid)] px-1.5 py-0.5 text-[11px] font-semibold leading-none text-white">
              {visible.length}
            </span>
          </>
        )}
      </button>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        label="待处理事项"
        width="lg"
        // dvh 而非 vh：iOS 浏览器里 vh 按「地址栏收起后」的大视口算，76vh 的
        // 弹层会超出实际可视高度（subscribe-dialog 同款约定）
        panelClassName="flex max-h-[76dvh] flex-col"
      >
        {/* 头部只放标题与件数：右侧原先那句"问题修复后会自动消失"与标题争夺
            同一条视线，且它是整个面板的机制说明、不是某条事项的信息，降到底栏 */}
        <div className="flex items-center gap-2.5 px-5 pb-4 pt-5">
          <BellIcon className="size-5 shrink-0 text-[var(--danger)]" />
          <h2 className="flex-1 text-title-sm font-semibold text-[var(--text)]">待处理事项</h2>
          <span className="shrink-0 rounded-full bg-white/[0.07] px-2 py-0.5 text-caption font-semibold text-[var(--text-muted)]">
            {visible.length}
          </span>
        </div>
        <div className="scroll-thin flex-1 space-y-2.5 overflow-y-auto px-5 pb-4">
          {visible.map((notice) => (
            // 卡片刻意不用 .glass-row：那个类是给"单行可点行"用的，它在
            // globals.css 里是无 @layer 的普通规则，align-items:center 与
            // gap:11px 会压过 Tailwind 工具类——套在竖排卡片上会把每一行都
            // 挤成"内容宽度并居中"（标题与时间黏在一起飘在中间）。
            <div
              key={notice.id}
              className="rounded-xl border border-white/[0.07] bg-white/[0.03] px-3.5 py-3"
            >
              {/* 严重度圆点自成一条左栏，标题与正文对齐在同一条竖线上；
                  时间贴右，与标题基线对齐 */}
              <div className="flex gap-2.5">
                <span
                  className="mt-[7px] size-2 shrink-0 rounded-full"
                  style={{ background: DOT_COLOR[notice.severity] ?? DOT_COLOR.error }}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-3">
                    <h3 className="min-w-0 flex-1 text-ui font-semibold text-[var(--text)]">
                      {notice.title}
                    </h3>
                    <span className="shrink-0 text-caption text-[var(--text-faint)]">
                      {formatRelativeTime(notice.updated_at)}
                    </span>
                  </div>
                  <p className="mt-1 text-sub leading-relaxed text-[var(--text-muted)]">
                    {notice.message}
                  </p>
                </div>
              </div>
              {/* 操作条：一道发丝线与正文分开，主次分明——「去处理」是这条事项
                  真正的出口（实心玻璃按钮），「忽略」是退路（纯文字，最弱） */}
              <div className="mt-3 flex items-center justify-end gap-2 border-t border-white/[0.05] pt-2.5">
                <button
                  type="button"
                  onClick={() => dismiss(notice.id)}
                  className="rounded-lg px-2.5 py-1.5 text-sub text-[var(--text-faint)] transition-colors hover:bg-white/[0.06] hover:text-[var(--text-muted)] max-md:py-2"
                >
                  忽略
                </button>
                {/* 第二出口：不知道怎么办就交给 AI——后端带着现场自检组装工单 */}
                <HandoffButton
                  kind="notice"
                  refId={String(notice.id)}
                  onBeforeNavigate={() => setOpen(false)}
                />
                <button
                  type="button"
                  onClick={() => goto(notice)}
                  className="btn-glass gap-1 px-3 py-1.5 text-sub font-medium text-[var(--text)] max-md:py-2"
                >
                  去处理
                  <ChevronRightIcon className="size-3.5" />
                </button>
              </div>
            </div>
          ))}
        </div>
        {/* 底栏：面板的机制说明——告诉用户这里的东西不需要手动清理 */}
        <div className="border-t border-white/[0.06] px-5 py-3 text-caption text-[var(--text-faint)]">
          这里只列需要你处理的运行时问题，修复后会自动消失。
        </div>
      </Modal>
    </>
  );
}
