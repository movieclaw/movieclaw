"use client";

/**
 * 「接入通道」绑定弹窗——微信 / Telegram / Discord 共用一层壳。
 *
 * 为什么收进弹窗：绑定是一次性动作（拿到二维码 / 配对码 → 去客户端确认 → 完成），
 * 而设置页的常态是「看看已经接好了哪些」。旧版把三块绑定表单常驻在列表页里，
 * 页面被一次性流程撑长，还会在进页面时自动弹出微信二维码——纯浏览也触发副作用。
 * 现在只有点「新增通道」才进入这层，绑定成功即关闭并刷新列表。
 *
 * 三种流程的差异只落在 body 上，外壳（标题 / 指引 / 关闭）完全一致：
 *   - 微信：进弹窗即请求二维码 → 扫码 →（可能）填手机上的配对码 → 完成；
 *   - Telegram / Discord：先填 bot token → 拿 6 位配对码 → 私聊 bot 发码 → 完成。
 *   - 飞书：粘贴群机器人 Webhook 地址 → 服务端发欢迎消息验真 → 即绑即用（无轮询）。
 * 前两者靠 2 秒一次的状态轮询推进（后端只读内存快照，毫秒级返回）。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { Modal } from "@/components/modal";
import {
  type ImBinding,
  type ImChannelId,
  type WeixinBindingSnapshot,
  getImBindingStatus,
  getWeixinBindingStatus,
  startFeishuBinding,
  startImBinding,
  startWeixinBinding,
  submitWeixinVerifyCode,
} from "@/lib/api/channels";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/** 全部可接入的通道；微信走扫码，Telegram/Discord 走 bot token + 配对码，飞书贴 Webhook。 */
export type ChannelKind = "weixin" | ImChannelId;

/** 「新增通道」菜单与统一列表的展示顺序。 */
export const CHANNEL_KINDS: ChannelKind[] = ["weixin", "telegram", "discord", "feishu"];

/**
 * 平台文案——本模块唯一的平台差异落点。
 * summary 给「新增通道」菜单用（一句话说清这个通道是什么），
 * howTo 给绑定弹窗顶部用（说清接下来要做什么），
 * tokenHint 只有需要填 bot token 的通道才有。
 */
export const CHANNEL_META: Record<
  ChannelKind,
  { label: string; summary: string; howTo: string; tokenHint?: string }
> = {
  weixin: {
    label: "微信",
    summary: "手机扫码绑定，无需自建机器人",
    howTo: "打开手机微信扫描下方二维码。扫码人即唯一可对话的用户，同时也是推送目标。",
  },
  telegram: {
    label: "Telegram",
    summary: "接入你用 @BotFather 创建的 bot",
    howTo:
      "在 Telegram 搜索 @BotFather 创建 bot 并复制 token；国内网络请先在「设置 → 网络」为 Telegram 开启代理。",
    tokenHint: "从 @BotFather 创建 bot 后获得的 token",
  },
  discord: {
    label: "Discord",
    summary: "接入你在开发者后台创建的 bot",
    howTo:
      "在 discord.com/developers 创建应用 → Bot → Reset Token 复制；国内网络请先在「设置 → 网络」为 Discord 开启代理。",
    tokenHint: "Discord 开发者后台 Bot 页面的 token",
  },
  feishu: {
    label: "飞书",
    summary: "粘贴群机器人 Webhook 地址，即绑即用",
    howTo:
      "在飞书群聊「设置 → 群机器人 → 添加机器人」里添加「自定义机器人」，把复制的 Webhook 地址粘贴到下面即可。安全设置建议选「签名校验」，并把密钥一并填入。",
  },
};

const INPUT_CLASS =
  "w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2 text-ui " +
  "text-[var(--text)] outline-none focus:border-[var(--accent)]/60";

export interface ChannelBindDialogProps {
  channel: ChannelKind;
  onClose: () => void;
  /** 绑定完成：由调用方负责关闭弹窗并刷新通道列表 */
  onBound: () => void;
}

export function ChannelBindDialog({ channel, onClose, onBound }: ChannelBindDialogProps) {
  const meta = CHANNEL_META[channel];
  return (
    <Modal open onClose={onClose} label={`接入 ${meta.label}`}>
      <div className="scroll-thin max-h-[76dvh] overflow-y-auto p-6 max-md:p-5">
        <h2 className="text-title font-bold text-white">接入 {meta.label}</h2>
        <p className="mt-1 text-sub leading-6 text-[var(--text-muted)]">{meta.howTo}</p>

        <div className="mt-5">
          {channel === "weixin" ? (
            <WeixinBindBody onBound={onBound} />
          ) : channel === "feishu" ? (
            <FeishuBindBody onBound={onBound} />
          ) : (
            <ImBindBody channel={channel} onBound={onBound} />
          )}
        </div>

        <div className="mt-6 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="btn-glass px-3.5 py-2 text-ui font-medium"
          >
            关闭
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 错误条：三种 body 共用。 */
function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
      {message}
    </div>
  );
}

/* —— 微信：扫码 + 可选配对码 —— */

function WeixinBindBody({ onBound }: { onBound: () => void }) {
  const [binding, setBinding] = useState<WeixinBindingSnapshot | null>(null);
  const [verifyCode, setVerifyCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const begin = useCallback(async () => {
    setError(null);
    setVerifyCode("");
    setBusy(true);
    try {
      const start = await startWeixinBinding();
      setBinding({
        challenge_id: start.challenge_id,
        status: "pending",
        message: start.message,
        qrcode_url: start.qrcode_url,
        qrcode_image: start.qrcode_image,
        account: null,
      });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  // 进弹窗即出码：点「新增微信」的意图就是要二维码，不再多一次点击。
  // 只自动发起一次，失败 / 过期后由用户点按钮重来，不无限重试。
  const autoStartedRef = useRef(false);
  useEffect(() => {
    if (autoStartedRef.current) return;
    autoStartedRef.current = true;
    void begin();
  }, [begin]);

  const polling =
    binding != null &&
    (binding.status === "pending" ||
      binding.status === "scanned" ||
      binding.status === "need_verify_code");
  useVisiblePolling(
    () => {
      if (binding == null) return;
      void getWeixinBindingStatus(binding.challenge_id)
        .then((snap) => {
          if (snap.status === "confirmed" || snap.status === "already_bound") {
            onBound();
            return;
          }
          setBinding(snap);
        })
        .catch(() => {
          /* 轮询失败静默重试（challenge 过期由状态自己表达） */
        });
    },
    polling ? 2000 : null,
  );

  async function handleVerifySubmit() {
    if (binding == null || !verifyCode.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await submitWeixinVerifyCode(binding.challenge_id, verifyCode.trim());
      setVerifyCode("");
      // 提交后先把本地状态推回 scanned，等下一次轮询的服务端判定
      setBinding({ ...binding, status: "scanned", message: "正在校验配对码…" });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const terminal = binding?.status === "expired" || binding?.status === "failed";

  return (
    <div className="space-y-4">
      {error && <ErrorBanner message={error} />}

      {binding == null ? (
        busy ? (
          <div className="h-[240px] animate-pulse rounded-2xl bg-white/[0.04]" />
        ) : (
          // 发起失败（网关不可达等）：留一个手动重试入口
          <div className="flex flex-col items-center gap-3 rounded-2xl bg-white/[0.03] px-6 py-10 text-center">
            <p className="text-body font-medium text-[var(--text)]">二维码没能生成</p>
            <button
              type="button"
              onClick={() => void begin()}
              className="btn-accent rounded-full px-4 py-1.5 text-sub font-semibold"
            >
              重试
            </button>
          </div>
        )
      ) : (
        <div className="flex flex-col items-center gap-4 rounded-2xl bg-white/[0.03] px-6 py-6 text-center">
          {!terminal && (
            <div className="rounded-2xl bg-white p-3">
              {/* 服务端渲染的 SVG data URL，用原生 img 展示内联数据 */}
              <img src={binding.qrcode_image} alt="微信绑定二维码" className="size-44" />
            </div>
          )}
          <div>
            <p className="text-body font-medium text-[var(--text)]">
              {binding.status === "scanned" ? "已扫码" : terminal ? "绑定未完成" : "等待扫码"}
            </p>
            <p className="mt-1 text-sub text-[var(--text-muted)]">{binding.message}</p>
          </div>
          {terminal && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void begin()}
              className="btn-accent rounded-full px-4 py-1.5 text-sub font-semibold disabled:opacity-40"
            >
              重新生成二维码
            </button>
          )}
        </div>
      )}

      {/* 微信要求补填手机上显示的配对码时才出现 */}
      {binding?.status === "need_verify_code" && (
        <div className="flex gap-2">
          <input
            value={verifyCode}
            onChange={(e) => setVerifyCode(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void handleVerifySubmit()}
            inputMode="numeric"
            autoFocus
            placeholder="手机微信上显示的数字"
            className={INPUT_CLASS}
          />
          <button
            type="button"
            disabled={busy || !verifyCode.trim()}
            onClick={() => void handleVerifySubmit()}
            className="btn-accent shrink-0 rounded-xl px-4 py-2 text-sub font-semibold disabled:opacity-40"
          >
            确认
          </button>
        </div>
      )}
    </div>
  );
}

/* —— 飞书：Webhook 地址即绑即用 —— */

function FeishuBindBody({ onBound }: { onBound: () => void }) {
  const [webhookUrl, setWebhookUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleBind() {
    if (!webhookUrl.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await startFeishuBinding(webhookUrl.trim(), secret.trim());
      onBound();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      {error && <ErrorBanner message={error} />}
      <input
        value={webhookUrl}
        onChange={(e) => setWebhookUrl(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && void handleBind()}
        autoFocus
        placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…"
        className={INPUT_CLASS}
      />
      <input
        value={secret}
        onChange={(e) => setSecret(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && void handleBind()}
        placeholder="签名密钥（未开启签名校验可留空）"
        className={INPUT_CLASS}
      />
      <p className="text-caption leading-5 text-[var(--text-faint)]">
        接入成功会立即向群里发一条欢迎消息，收到即说明通道可用。安全设置若选了
        「自定义关键词」，推送文案需包含该关键词才会送达，建议改用「签名校验」。
      </p>
      <button
        type="button"
        disabled={busy || !webhookUrl.trim()}
        onClick={() => void handleBind()}
        className="btn-accent w-full rounded-xl py-2 text-ui font-semibold disabled:opacity-40"
      >
        {busy ? "接入中…" : "完成接入"}
      </button>
    </div>
  );
}

/* —— Telegram / Discord：bot token + 配对码 —— */

function ImBindBody({ channel, onBound }: { channel: ImChannelId; onBound: () => void }) {
  const meta = CHANNEL_META[channel];
  const [token, setToken] = useState("");
  const [binding, setBinding] = useState<ImBinding | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleStart() {
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setBinding(await startImBinding(channel, token.trim()));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  useVisiblePolling(
    () => {
      if (binding == null) return;
      void getImBindingStatus(channel, binding.challenge_id)
        .then((snap) => {
          if (snap.status === "confirmed") {
            onBound();
            return;
          }
          setBinding(snap);
        })
        .catch(() => {
          /* 轮询失败静默重试（过期由状态自己表达） */
        });
    },
    binding?.status === "pending" ? 2000 : null,
  );

  if (binding == null) {
    return (
      <div className="space-y-3">
        {error && <ErrorBanner message={error} />}
        <input
          value={token}
          onChange={(e) => setToken(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void handleStart()}
          autoFocus
          placeholder={meta.tokenHint}
          className={INPUT_CLASS}
        />
        <button
          type="button"
          disabled={busy || !token.trim()}
          onClick={() => void handleStart()}
          className="btn-accent w-full rounded-xl py-2 text-ui font-semibold disabled:opacity-40"
        >
          {busy ? "校验中…" : "获取配对码"}
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {error && <ErrorBanner message={error} />}
      <div className="flex flex-col items-center gap-3 rounded-2xl bg-white/[0.03] px-6 py-8 text-center">
        {binding.status === "pending" ? (
          <>
            <p className="text-body font-medium text-[var(--text)]">
              私聊 @{binding.bot_name} 发送配对码
            </p>
            <p className="select-all font-mono text-[32px] font-bold tracking-[0.3em] text-[var(--accent)]">
              {binding.pair_code}
            </p>
            <p className="text-caption text-[var(--text-faint)]">
              10 分钟内有效 · 发码人将成为唯一可对话的用户与推送目标
            </p>
          </>
        ) : (
          <>
            <p className="text-body font-medium text-[var(--text)]">绑定未完成</p>
            <p className="text-sub text-[var(--text-muted)]">{binding.message}</p>
            <button
              type="button"
              onClick={() => setBinding(null)}
              className="btn-accent rounded-full px-4 py-1.5 text-sub font-semibold"
            >
              重新发起
            </button>
          </>
        )}
      </div>
    </div>
  );
}
