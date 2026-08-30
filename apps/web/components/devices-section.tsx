"use client";

import { useCallback, useEffect, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { useConfirm } from "@/components/feedback";
import { CheckIcon, InfoIcon, PlusIcon, TerminalIcon, XIcon } from "@/components/icons";
import { getAppConfig } from "@/lib/api/app";
import {
  type DeviceRequestView,
  type DeviceTokenView,
  approveDeviceRequest,
  createDeviceToken,
  denyDeviceRequest,
  listDeviceRequests,
  listDevices,
  revokeDevice,
} from "@/lib/api/devices";
import {
  clientTypeLabel,
  envSnippet,
  grantBadge,
  grantSummary,
  isLive,
  manualGrantSummary,
  relativeTime,
  resolveServerAddress,
} from "@/lib/devices-display";

/**
 * 「设备」设置分区（docs/design/device-auth.md §7）。
 *
 * 这一页承担两件在设计里被反复强调的事：
 *
 * 1. **批准是防钓鱼的唯一一道人工闸**。用户在这里做的决定，依据全部来自
 *    审批卡上的四项——名称、类型、来源 IP、配对码。「将获得」那一行必须写
 *    大白话而不是内部权限名：v1 除 Worker 外的令牌都是完全权限，用户的知情
 *    就是唯一的闸（§4.5）。
 * 2. **吊销是唯一的事后止损手段**（§8：改密不连坐、令牌不过期）。所以列表
 *    要好用——最近活跃时间要准、一台一行、一键吊销。
 *
 * 3. **手工令牌是设备流够不到的那部分环境的唯一入口**。NAS 的定时任务、CI、
 *    无界面容器里没人能按批准，配对流在那里必然挂到超时；这些场景改为在这里
 *    创建令牌，用环境变量注入给 mclaw（§6.1 非 TTY 分支指向的就是这里）。
 *
 * 待批准请求只活在服务端内存里（未获批准的请求不落库），所以这里轮询而不是
 * 只在挂载时拉一次：用户往往是先在 Mac 上点了「请求接入」，再切到浏览器。
 */
const REQUEST_POLL_MS = 3000;

export function DevicesSection() {
  const confirm = useConfirm();
  const [requests, setRequests] = useState<DeviceRequestView[]>([]);
  const [devices, setDevices] = useState<DeviceTokenView[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadDevices = useCallback(async () => {
    setDevices(await listDevices());
  }, []);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [pending, granted] = await Promise.all([listDeviceRequests(), listDevices()]);
      setRequests(pending);
      setDevices(granted);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 待批准请求独立轮询：用户通常先在设备上发起、再切到浏览器，页面得自己长出来。
  // 拉取失败不弹错——这是附属刷新，不该盖掉用户正在读的审批卡。
  useEffect(() => {
    let alive = true;
    const timer = setInterval(() => {
      void listDeviceRequests()
        .then((next) => {
          if (alive) setRequests(next);
        })
        .catch(() => undefined);
    }, REQUEST_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  const handleApprove = async (req: DeviceRequestView) => {
    setBusy(req.user_code);
    setError(null);
    try {
      await approveDeviceRequest(req.user_code);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const handleDeny = async (req: DeviceRequestView) => {
    setBusy(req.user_code);
    setError(null);
    try {
      await denyDeviceRequest(req.user_code);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const handleRevoke = async (device: DeviceTokenView) => {
    const ok = await confirm({
      title: `吊销「${device.name}」？`,
      description:
        "这台设备会立即失去访问权限，需要重新配对才能再次接入。其他设备不受影响。",
      confirmLabel: "吊销",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(device.id);
    setError(null);
    try {
      await revokeDevice(device.id);
      await loadDevices();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-8">
      {error && (
        <p className="rounded-xl border border-[var(--danger)]/30 bg-[var(--danger)]/10 px-4 py-2.5 text-sub text-[var(--danger)]">
          {error}
        </p>
      )}

      {requests.length > 0 && (
        <section className="space-y-3">
          <h2 className="px-1 text-caption font-semibold uppercase tracking-wider text-[var(--text-faint)]">
            待批准的接入请求
          </h2>
          {requests.map((req) => (
            <ApprovalCard
              key={req.user_code}
              request={req}
              busy={busy === req.user_code}
              onApprove={() => void handleApprove(req)}
              onDeny={() => void handleDeny(req)}
            />
          ))}
        </section>
      )}

      <section className="space-y-3">
        <h2 className="px-1 text-caption font-semibold uppercase tracking-wider text-[var(--text-faint)]">
          已连接的设备
        </h2>
        {loading ? (
          <p className="px-1 text-sub text-[var(--text-faint)]">加载中…</p>
        ) : devices.length === 0 ? (
          <EmptyState hasPending={requests.length > 0} />
        ) : (
          <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
            {devices.map((device) => (
              <DeviceRow
                key={device.id}
                device={device}
                busy={busy === device.id}
                onRevoke={() => void handleRevoke(device)}
              />
            ))}
          </div>
        )}
      </section>

      <ManualTokenSection onCreated={() => void loadDevices()} />
    </div>
  );
}

/**
 * 审批卡：用户做决定的全部依据都在这张卡上。
 *
 * 配对码用大号等宽字并加字距——它要被拿去和设备屏幕上的字符逐个比对，
 * 这是防钓鱼的实际动作，字号小了就没人会真的比。
 */
function ApprovalCard({
  request,
  busy,
  onApprove,
  onDeny,
}: {
  request: DeviceRequestView;
  busy: boolean;
  onApprove: () => void;
  onDeny: () => void;
}) {
  const grant = grantSummary(request.client_type);
  return (
    <div className="css-glass space-y-4 !rounded-2xl border-[var(--accent)]/25 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="text-body font-semibold text-[var(--text)]">{request.client_name}</p>
        <span className="font-mono text-[22px] font-semibold tracking-[0.16em] text-[var(--accent)]">
          {request.user_code}
        </span>
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 text-sub">
        <dt className="text-[var(--text-faint)]">类型</dt>
        <dd className="text-[var(--text-muted)]">{clientTypeLabel(request.client_type)}</dd>
        <dt className="text-[var(--text-faint)]">来源</dt>
        {request.source_ip ? (
          <dd className="font-mono text-[var(--text-muted)]">{request.source_ip}</dd>
        ) : (
          /* 服务端判定这个地址认不出设备时会返回空串（api/client_address.py）：
             桥接网络的容器看到的源地址是网桥网关，全网设备长得一模一样。
             与其摆一个「172.17.0.1」让人以为那是对方的地址，不如直说看不到，
             并把判断依据推回配对码——那本来就是这张卡真正的安全控制。 */
          <dd className="text-[var(--text-faint)]">
            无法确定
            <span className="ml-1.5 text-caption">
              容器网络改写了源地址，请以配对码为准
            </span>
          </dd>
        )}
      </dl>

      <div className="rounded-xl border border-[var(--accent)]/20 bg-[var(--accent-soft)] px-4 py-3">
        <p className="text-sub font-semibold text-[var(--accent)]">{grant.title}</p>
        <p className="mt-1 text-sub leading-relaxed text-[var(--text-muted)]">{grant.body}</p>
      </div>

      <p className="text-caption leading-relaxed text-[var(--text-faint)]">
        请确认上面的配对码与设备上显示的完全一致。如果这不是你刚发起的操作，选择拒绝。
      </p>

      <div className="flex items-center gap-2.5">
        <button
          type="button"
          disabled={busy}
          onClick={onApprove}
          className="btn-accent flex items-center gap-1.5 rounded-full px-4.5 py-2 text-sub font-semibold disabled:opacity-40"
        >
          <CheckIcon className="size-4" />
          批准接入
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onDeny}
          className="btn-glass flex items-center gap-1.5 px-3.5 py-2 text-sub font-medium text-[var(--danger)] disabled:opacity-40"
        >
          <XIcon className="size-4" />
          拒绝
        </button>
      </div>
    </div>
  );
}

/** 已连接设备的一行：状态点 + 名称 + 一句话身份 + 吊销。 */
function DeviceRow({
  device,
  busy,
  onRevoke,
}: {
  device: DeviceTokenView;
  busy: boolean;
  onRevoke: () => void;
}) {
  const live = isLive(device.last_used_at);
  return (
    <div className="flex items-center gap-4 px-5 py-4 first:rounded-t-2xl last:rounded-b-2xl">
      <span
        aria-hidden
        className={`size-2 shrink-0 rounded-full ${
          live ? "bg-[var(--ok,#4ade80)] shadow-[0_0_8px_rgba(74,222,128,0.55)]" : "bg-white/25"
        }`}
      />
      <div className="min-w-0 flex-1">
        <p className="truncate text-body font-medium text-[var(--text)]">{device.name}</p>
        <p className="mt-0.5 text-caption text-[var(--text-faint)]">
          {clientTypeLabel(device.client_type)} · {grantBadge(device.client_type)} ·{" "}
          {relativeTime(device.last_used_at)}
        </p>
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={onRevoke}
        className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium text-[var(--text-muted)] hover:text-[var(--danger)] disabled:opacity-40"
      >
        吊销
      </button>
    </div>
  );
}

/** 空态：直接告诉用户下一步在哪做，而不是只说「暂无数据」。 */
function EmptyState({ hasPending }: { hasPending: boolean }) {
  return (
    <div className="css-glass flex flex-col items-center gap-3 !rounded-2xl px-6 py-10 text-center">
      <span className="icon-chip size-11 !rounded-2xl">
        <TerminalIcon className="size-5" />
      </span>
      <p className="text-body font-medium text-[var(--text)]">还没有设备接入</p>
      <p className="max-w-sm text-sub leading-relaxed text-[var(--text-muted)]">
        {hasPending
          ? "上面有一条待批准的请求，核对配对码后即可批准。"
          : "在 Mac 转码 Worker 里点「在局域网中查找」或填好地址，或在终端运行 mclaw login，设备会显示一段配对码，回到这里批准即可。没人能按批准的环境（定时任务、CI、无界面容器）改用下面的手工令牌。"}
      </p>
    </div>
  );
}

/**
 * 「手工创建令牌」分区（docs/design/device-auth.md §6.1、§7）。
 *
 * 存在的理由只有一条：**配对流要求有人在浏览器里按批准，而有些环境根本没有
 * 那个人**——NAS 上的定时任务、CI、无界面容器。在那里跑 `mclaw login` 只会挂到
 * 超时，CLI 因此在非 TTY 下直接以用法错误退出，并把用户指到这里。
 *
 * 三个刻意的取舍：
 *
 * 1. **做成次要入口，并主动劝退**。手工令牌是全权且不过期的，没有配对流那道
 *    「核对配对码」的人工闸；能开浏览器的机器就该走 `mclaw login`。所以收起态
 *    第一段话就写明「不必走这里」，而不是把两条路并列摆着让用户挑。
 * 2. **给两行环境变量，不是一个裸令牌**。用户接下来要做的事是「让那台机器连上
 *    这台 movieclaw」，地址和令牌缺一不可；只给令牌等于把找地址这一步留给用户，
 *    而地址恰恰是自部署里最容易填错的东西。
 * 3. **明文只在创建响应里出现一次**，服务端只存哈希。所以这张卡必须让用户当场
 *    存走：关闭前有确认，关闭后只能吊销重建。
 */
function ManualTokenSection({ onCreated }: { onCreated: () => void }) {
  const confirm = useConfirm();
  const [stage, setStage] = useState<"idle" | "form">("idle");
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<{ name: string; token: string } | null>(null);
  // 对外访问地址：进入这个分区就先拉一次，等按下创建再拉会让那一下多等一个往返
  const [externalUrl, setExternalUrl] = useState("");
  const manualGrant = manualGrantSummary();

  useEffect(() => {
    void getAppConfig()
      .then((config) => setExternalUrl(config.external_url))
      .catch(() => undefined); // 拿不到就回落当前地址，不该挡住创建
  }, []);

  const handleCreate = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setNameError("先给它起个名字，否则日后没法在列表里认出是哪台机器。");
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const token = await createDeviceToken(trimmed);
      setCreated({ name: token.name, token: token.token });
      setStage("idle");
      setName("");
      setNameError(null);
      onCreated();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setCreating(false);
    }
  };

  // 关闭一次性凭据卡要过确认：明文关掉就再也读不到，误点的代价是吊销重建
  const handleDismiss = async () => {
    const ok = await confirm({
      title: "关闭后就看不到这枚令牌了？",
      description:
        "令牌明文只显示这一次。确认你已经把它存进目标机器，或者复制到了安全的地方。",
      confirmLabel: "我已保存",
    });
    if (ok) setCreated(null);
  };

  return (
    <section className="space-y-3">
      <div className="flex items-baseline justify-between gap-3 px-1">
        <h2 className="text-caption font-semibold uppercase tracking-wider text-[var(--text-faint)]">
          手工创建令牌
        </h2>
        {stage === "idle" && !created && (
          <button
            type="button"
            onClick={() => setStage("form")}
            className="btn-glass flex items-center gap-1.5 px-3 py-1.5 text-sub font-medium"
          >
            <PlusIcon className="size-3.5" />
            创建令牌
          </button>
        )}
      </div>

      {error && (
        <p className="rounded-xl border border-[var(--danger)]/30 bg-[var(--danger)]/10 px-4 py-2.5 text-sub text-[var(--danger)]">
          {error}
        </p>
      )}

      {created ? (
        <CreatedTokenCard
          name={created.name}
          token={created.token}
          externalUrl={externalUrl}
          onDismiss={() => void handleDismiss()}
        />
      ) : stage === "form" ? (
        <div className="css-glass space-y-4 !rounded-2xl p-5">
          <div className="space-y-1.5">
            <label htmlFor="manual-token-name" className="text-sub font-medium text-[var(--text-muted)]">
              名字
            </label>
            <input
              id="manual-token-name"
              type="text"
              autoFocus
              maxLength={64}
              value={name}
              placeholder="nas-cron"
              onChange={(e) => {
                setName(e.target.value);
                if (nameError) setNameError(null);
              }}
              onKeyDown={(e) => e.key === "Enter" && void handleCreate()}
              className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-body text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] focus:border-[var(--accent)]/50"
            />
            <p
              className={`text-caption ${
                nameError ? "text-[var(--danger)]" : "text-[var(--text-faint)]"
              }`}
            >
              {nameError ?? "日后在上面的设备列表里就靠它认出这枚令牌、决定要不要吊销。"}
            </p>
          </div>

          {/* 与审批卡同一段文案：手工令牌和批准出来的 CLI 令牌同权，
              没有理由在这里说得更轻 */}
          <div className="rounded-xl border border-[var(--accent)]/20 bg-[var(--accent-soft)] px-4 py-3">
            <p className="text-sub font-semibold text-[var(--accent)]">{manualGrant.title}</p>
            <p className="mt-1 text-sub leading-relaxed text-[var(--text-muted)]">
              {manualGrant.body}
            </p>
          </div>

          <div className="flex items-center gap-2.5">
            <button
              type="button"
              disabled={creating}
              onClick={() => void handleCreate()}
              className="btn-accent rounded-full px-4.5 py-2 text-sub font-semibold disabled:opacity-40"
            >
              {creating ? "创建中…" : "创建令牌"}
            </button>
            <button
              type="button"
              disabled={creating}
              onClick={() => {
                setStage("idle");
                setName("");
                setNameError(null);
              }}
              className="btn-glass px-3.5 py-2 text-sub font-medium text-[var(--text-muted)] disabled:opacity-40"
            >
              取消
            </button>
          </div>
        </div>
      ) : (
        <div className="css-glass !rounded-2xl p-5">
          <p className="text-sub leading-relaxed text-[var(--text-muted)]">
            没法在浏览器里按下批准的环境——NAS 上的定时任务、CI、无界面容器——在这里创建一枚令牌，用{" "}
            <code className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[0.92em] text-[var(--text)]">
              MOVIECLAW_SERVER
            </code>{" "}
            和{" "}
            <code className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[0.92em] text-[var(--text)]">
              MOVIECLAW_TOKEN
            </code>{" "}
            两个环境变量注入给 mclaw。能打开浏览器的机器请直接运行 mclaw login 配对，不必走这里。
          </p>
        </div>
      )}
    </section>
  );
}

/**
 * 一次性凭据卡：全站唯一一处「现在不存就永远没了」的地方。
 *
 * 边框用 --warn 而不是 --accent 或 --danger：--danger 的语义是「失败了，要你
 * 处理」，这里没有任何东西失败；需要的是一个独有的、能让人停下来的信号。
 */
function CreatedTokenCard({
  name,
  token,
  externalUrl,
  onDismiss,
}: {
  name: string;
  token: string;
  externalUrl: string;
  onDismiss: () => void;
}) {
  const address = resolveServerAddress(
    externalUrl,
    typeof window === "undefined" ? "" : window.location.origin,
  );
  const snippet = envSnippet(address.url, token);

  return (
    <div className="css-glass space-y-4 !rounded-2xl border-[var(--warn)]/35 p-5">
      <div className="flex items-start gap-3">
        <CheckIcon className="mt-0.5 size-[18px] shrink-0 text-[var(--ok)]" />
        <div className="min-w-0">
          <p className="text-body font-semibold text-[var(--text)]">已创建「{name}」</p>
          <p className="mt-0.5 text-sub leading-relaxed text-[var(--warn)]">
            令牌明文只显示这一次。关掉这张卡就再也读不到，只能吊销后重建。
          </p>
        </div>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between gap-3">
          <span className="text-sub font-medium text-[var(--text-muted)]">粘贴到目标环境</span>
          <CopyButton
            text={snippet}
            label="复制两行"
            className="btn-glass px-3 py-1.5 text-sub font-medium text-[var(--text-muted)]"
          />
        </div>
        {/* 令牌那半用 --warn 上色：一眼分得出哪一行是秘密、不能贴进工单和聊天 */}
        <pre className="overflow-x-auto rounded-xl border border-white/[0.08] bg-black/[0.28] px-4 py-3.5 font-mono text-sub leading-relaxed">
          <span className="text-[var(--accent-2)]">MOVIECLAW_SERVER=</span>
          <span className="text-[var(--text)]">{address.url}</span>
          {"\n"}
          <span className="text-[var(--accent-2)]">MOVIECLAW_TOKEN=</span>
          <span className="text-[var(--warn)]">{token}</span>
        </pre>
      </div>

      {address.configured ? (
        <p className="text-caption leading-relaxed text-[var(--text-faint)]">
          地址取自「设置 → 网络与维护」里填写的对外访问地址。
        </p>
      ) : (
        /* 没配对外地址时给的是浏览器地址栏那个值——它未必是目标机器连得到的地址。
           直接给一个可能不通的值，用户只会看到 mclaw 连接超时而查不到原因，
           所以这里说破，并指向真正的修法。 */
        <div className="flex gap-2.5 rounded-xl border border-[var(--warn)]/28 bg-[var(--warn)]/[0.09] px-3.5 py-3">
          <InfoIcon className="mt-0.5 size-4 shrink-0 text-[var(--warn)]" />
          <p className="text-sub leading-relaxed text-[var(--text-muted)]">
            上面这行地址取自你现在浏览器的地址栏，只是猜测——目标机器不一定连得到。
            请到「设置 → 网络与维护」填写对外访问地址，之后这里会直接给出正确的一行。
          </p>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <CopyButton
          text={token}
          label="仅复制令牌"
          className="btn-glass px-3.5 py-2 text-sub font-medium text-[var(--text-muted)]"
        />
        <button
          type="button"
          onClick={onDismiss}
          className="btn-accent rounded-full px-4.5 py-2 text-sub font-semibold"
        >
          我已保存，关闭
        </button>
      </div>
    </div>
  );
}
