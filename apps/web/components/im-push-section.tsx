"use client";

/**
 * 「消息推送」设置分区。
 *
 * 两类设定按胶囊标签分开（与「更新与维护」「外观」分区同一交互语言），因为它们
 * 回答的是两个完全不同的问题，动线不该缠在一起：
 *   - 接入通道：我接了哪些账号？——一张跨平台的统一列表 +「新增通道」菜单，
 *     绑定流程收进弹窗（见 channel-bind-dialog）；
 *   - 推送内容：什么事件会推给我？——事件开关 + 一键测试推送。
 *
 * 旧版把微信 / Telegram / Discord 三块绑定表单和推送开关平铺在一页里：
 * 只接了一个通道也要滚过另外两块空态与说明，推送开关被夹在中间；进页面
 * 还会自动弹微信二维码。改版后列表只呈现"已接入"的事实，一次性流程只在
 * 用户主动新增时才出现。
 */

import { useCallback, useEffect, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import {
  CHANNEL_KINDS,
  CHANNEL_META,
  ChannelBindDialog,
  type ChannelKind,
} from "@/components/channel-bind-dialog";
import { useConfirm, useToast } from "@/components/feedback";
import { ChatIcon, PlusIcon } from "@/components/icons";
import { LlmSetupNotice, useLlmConfigured } from "@/components/llm-gate";
import {
  type ChannelPushConfig,
  type ImChannelId,
  getChannelPushConfig,
  listImAccounts,
  listWeixinAccounts,
  sendChannelPushTest,
  unbindImAccount,
  unbindWeixinAccount,
  updateChannelPushConfig,
} from "@/lib/api/channels";
import { useBackdrop } from "@/lib/backdrop";
import { formatRelativeTime } from "@/lib/time";
import { useTabParam } from "@/lib/use-tab-param";
import { LiquidGlassButton } from "@/vendor/liquid-glass";

export function ImPushSection() {
  // ?tab=content 深链直达推送内容，切换写回地址栏（useTabParam，全设置页同一套）
  const [tab, setTab] = useTabParam(["channels", "content"] as const, "channels");
  const tabs = [
    { id: "channels" as const, label: "接入通道" },
    { id: "content" as const, label: "推送内容" },
  ];

  return (
    <div className="space-y-5">
      <div className="flex gap-1.5">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            aria-pressed={t.id === tab}
            onClick={() => setTab(t.id)}
            className={`rounded-full px-3.5 py-1.5 text-sub font-medium transition-colors ${
              t.id === tab
                ? "bg-white/[0.14] text-white"
                : "text-[var(--text-muted)] hover:bg-white/[0.07] hover:text-[var(--text)]"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "channels" ? <ChannelsTab /> : <PushContentTab />}
    </div>
  );
}

/* —— 接入通道：跨平台统一列表 + 新增菜单 —— */

/** 列表行：三个平台的账号归一成同一形状，列表本身不再关心来源差异。 */
interface ChannelAccountRow {
  channel: ChannelKind;
  account_id: string;
  /** 完成绑定的用户 id（白名单，同时是推送目标） */
  bound_user_id: string | null;
  status: "active" | "stale";
  running: boolean;
  last_error: string | null;
  bound_at: string;
}

function ChannelsTab() {
  const llmConfigured = useLlmConfigured();
  const confirm = useConfirm();
  const toast = useToast();
  const [rows, setRows] = useState<ChannelAccountRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 正在绑定的通道（null = 没开弹窗）
  const [binding, setBinding] = useState<ChannelKind | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      // 三个平台同源同后端，任一失败都视作整体失败：与其展示半张列表让用户
      // 误以为"某个通道掉了"，不如明确报错让他重试
      const [weixin, telegram, discord] = await Promise.all([
        listWeixinAccounts(),
        listImAccounts("telegram"),
        listImAccounts("discord"),
      ]);
      setRows([
        ...weixin.map((a) => ({ ...a, channel: "weixin" as const })),
        ...telegram.map((a) => ({ ...a, channel: "telegram" as const })),
        ...discord.map((a) => ({ ...a, channel: "discord" as const })),
      ]);
    } catch (e) {
      setError((e as Error).message);
      setRows([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleUnbind(row: ChannelAccountRow) {
    const label = CHANNEL_META[row.channel].label;
    if (
      !(await confirm({
        title: `解绑该 ${label} 账号？`,
        description:
          row.channel === "weixin"
            ? "解绑后需重新扫码才能使用。"
            : "解绑将删除 bot 凭据并停止通道，需重新配对才能使用。",
        confirmLabel: "解绑",
        tone: "danger",
      }))
    )
      return;
    setBusy(true);
    setError(null);
    try {
      if (row.channel === "weixin") await unbindWeixinAccount(row.account_id);
      else await unbindImAccount(row.channel as ImChannelId, row.account_id);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const loading = rows == null;

  return (
    <div className="space-y-5">
      <p className="text-sub leading-6 text-[var(--text-muted)]">
        接入的通道既是推送目标，也是 AI 助手的对话入口：直接给它发消息即可搜片、订阅、查进度。
        发送
        <span className="mx-1 rounded bg-white/[0.08] px-1.5 py-0.5 text-caption">/reset</span>
        重置会话，
        <span className="mx-1 rounded bg-white/[0.08] px-1.5 py-0.5 text-caption">/stop</span>
        取消正在进行的处理。
      </p>

      {/* 前置门禁：对话完全由模型驱动，未接入模型时隐藏新增入口并引导 */}
      {llmConfigured === false && <LlmSetupNotice feature="通道中的 AI 对话能力" />}

      {error && (
        <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
          {error}
        </div>
      )}

      <div className="flex items-center justify-between gap-4">
        <p className="text-sub text-[var(--text-muted)]">
          {loading
            ? "加载中…"
            : rows.length === 0
              ? "还没有接入任何通道。"
              : `已接入 ${rows.length} 个账号。`}
        </p>
        {llmConfigured !== false && <AddChannelMenu onPick={setBinding} disabled={loading} />}
      </div>

      {loading ? (
        <div className="space-y-2.5">
          <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
          <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
        </div>
      ) : rows.length === 0 ? (
        <div className="css-glass flex flex-col items-center gap-3 !rounded-2xl px-6 py-12 text-center">
          <span className="icon-chip size-12 !rounded-2xl">
            <ChatIcon className="size-6" />
          </span>
          <div>
            <p className="text-body font-medium text-[var(--text)]">还没有接入任何通道</p>
            <p className="mt-1 text-sub text-[var(--text-muted)]">
              点击右上角「新增通道」，支持微信、Telegram 和 Discord。
            </p>
          </div>
        </div>
      ) : (
        <div className="css-glass !rounded-xl">
          {rows.map((row, i) => (
            <ChannelAccountRowView
              key={`${row.channel}:${row.account_id}`}
              row={row}
              divided={i > 0}
              busy={busy}
              onUnbind={() => void handleUnbind(row)}
            />
          ))}
        </div>
      )}

      {binding && (
        <ChannelBindDialog
          channel={binding}
          onClose={() => setBinding(null)}
          onBound={() => {
            const label = CHANNEL_META[binding].label;
            setBinding(null);
            void load();
            toast.success(`${label} 已接入，现在就可以给它发消息试试`);
          }}
        />
      )}
    </div>
  );
}

/** 「新增通道」下拉菜单：平台名 + 一句话说明，点选即开绑定弹窗。 */
function AddChannelMenu({
  onPick,
  disabled,
}: {
  onPick: (channel: ChannelKind) => void;
  disabled: boolean;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          disabled={disabled}
          className="btn-accent flex shrink-0 items-center gap-1 rounded-full py-1.5 pl-2.5 pr-3.5 text-sub font-semibold disabled:opacity-60"
        >
          <PlusIcon className="size-4" />
          新增通道
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[14rem] p-1"
        >
          {CHANNEL_KINDS.map((channel) => {
            const meta = CHANNEL_META[channel];
            return (
              <DropdownMenu.Item
                key={channel}
                onSelect={() => onPick(channel)}
                className="glass-row nav-item cursor-pointer flex-col !items-start gap-0 px-3 py-2 outline-none data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)]"
              >
                <span className="text-sub font-medium">新增 {meta.label} 渠道</span>
                <span className="text-caption text-[var(--text-faint)]">{meta.summary}</span>
              </DropdownMenu.Item>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/** 一行已接入账号：平台名 + 运行状态 + 绑定人 / 失效原因 + 解绑。 */
function ChannelAccountRowView({
  row,
  divided,
  busy,
  onUnbind,
}: {
  row: ChannelAccountRow;
  divided: boolean;
  busy: boolean;
  onUnbind: () => void;
}) {
  const meta =
    row.status === "stale"
      ? { label: "需重新绑定", color: "var(--danger)" }
      : row.running
        ? { label: "运行中", color: "var(--ok)" }
        : { label: "未运行", color: "#c0c4cc" };

  return (
    <div
      className={`flex items-center gap-3.5 p-4 ${divided ? "border-t border-white/[0.06]" : ""}`}
    >
      <span className="icon-chip size-10 !rounded-xl">
        <ChatIcon className="size-5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="truncate text-body font-semibold text-[var(--text)]">
            {CHANNEL_META[row.channel].label}
          </p>
          <span
            className="flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-caption font-medium"
            style={{ background: `color-mix(in oklab, ${meta.color} 12%, transparent)`, color: meta.color }}
          >
            <span className="size-1.5 rounded-full" style={{ background: meta.color }} />
            {meta.label}
          </span>
        </div>
        <p className="mt-0.5 truncate text-caption text-[var(--text-faint)]">
          {row.status === "stale"
            ? (row.last_error ?? "凭据已失效，请重新绑定")
            : `${row.bound_user_id ?? row.account_id} · 绑定于 ${formatRelativeTime(row.bound_at)}`}
        </p>
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={onUnbind}
        className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium !text-[#ff6b6b] disabled:opacity-40"
      >
        解绑
      </button>
    </div>
  );
}

/* —— 推送内容：事件开关 + 测试推送 —— */

function PushContentTab() {
  const { backdrop } = useBackdrop();
  const toast = useToast();
  const [config, setConfig] = useState<ChannelPushConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pushBusy, setPushBusy] = useState(false);

  useEffect(() => {
    getChannelPushConfig()
      .then(setConfig)
      .catch((e) => setError((e as Error).message));
  }, []);

  async function handleToggle(key: keyof ChannelPushConfig, value: boolean) {
    if (config == null) return;
    const next = { ...config, [key]: value };
    setConfig(next); // 乐观更新，失败回滚
    setError(null);
    try {
      await updateChannelPushConfig(next);
    } catch (e) {
      setConfig(config);
      setError((e as Error).message);
    }
  }

  async function handleTestPush() {
    setPushBusy(true);
    try {
      const { sent } = await sendChannelPushTest();
      toast.success(`已推送到 ${sent} 个账号，去客户端看看吧`);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setPushBusy(false);
    }
  }

  const rows: { key: keyof ChannelPushConfig; label: string; desc: string }[] = [
    { key: "push_dispatch", label: "开始下载", desc: "订阅命中资源并提交下载器时" },
    { key: "push_imported", label: "入库完成", desc: "下载完成整理进媒体库时" },
  ];

  return (
    <div className="space-y-5">
      <p className="text-sub leading-6 text-[var(--text-muted)]">
        这里的开关对所有已接入通道统一生效——关掉某个事件，任何通道都不会再收到它。
      </p>

      {error && (
        <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
          {error}
        </div>
      )}

      {config == null ? (
        <div className="h-[104px] animate-pulse rounded-xl bg-white/[0.04]" />
      ) : (
        <div className="css-glass !rounded-xl">
          {rows.map((row, i) => (
            <div
              key={row.key}
              className={`flex items-center gap-3.5 p-4 ${i > 0 ? "border-t border-white/[0.06]" : ""}`}
            >
              <div className="min-w-0 flex-1">
                <p className="text-body font-medium text-[var(--text)]">{row.label}</p>
                <p className="mt-0.5 text-caption text-[var(--text-faint)]">{row.desc}</p>
              </div>
              {/* 与网络设置同款的受控液态玻璃开关 */}
              <LiquidGlassButton
                backgroundImage={backdrop}
                variant="dark"
                checked={config[row.key]}
                aria-label={`${row.label}推送`}
                onCheckedChange={(v: boolean) => void handleToggle(row.key, v)}
                className="!min-h-0 !w-auto !gap-0 !bg-transparent !p-0"
              >
                <span className="sr-only">{config[row.key] ? "已开启" : "已关闭"}</span>
              </LiquidGlassButton>
            </div>
          ))}
        </div>
      )}

      {/* 测试推送：验证已接入通道确实能收到系统事件 */}
      <div className="css-glass !rounded-xl p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-body font-medium text-[var(--text)]">测试推送</p>
            <p className="mt-0.5 text-caption text-[var(--text-faint)]">
              向所有已接入通道发送一条测试消息
            </p>
          </div>
          <button
            type="button"
            disabled={pushBusy}
            onClick={() => void handleTestPush()}
            className="btn-glass shrink-0 rounded-full px-4 py-1.5 text-sub font-medium disabled:opacity-40"
          >
            发送测试
          </button>
        </div>
      </div>
    </div>
  );
}
