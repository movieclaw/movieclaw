"use client";

import { useCallback, useEffect, useState } from "react";

import { useConfirm } from "@/components/feedback";
import { CheckIcon, TerminalIcon, XIcon } from "@/components/icons";
import {
  type DeviceRequestView,
  type DeviceTokenView,
  approveDeviceRequest,
  denyDeviceRequest,
  listDeviceRequests,
  listDevices,
  revokeDevice,
} from "@/lib/api/devices";
import {
  clientTypeLabel,
  grantBadge,
  grantSummary,
  isLive,
  relativeTime,
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
          : "在 Mac 转码 Worker 里点「在局域网中查找」或填好地址，或在终端运行 mclaw login，设备会显示一段配对码，回到这里批准即可。"}
      </p>
    </div>
  );
}
