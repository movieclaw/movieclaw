"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { useConfirm } from "@/components/feedback";
import { PlusIcon, XIcon } from "@/components/icons";
import {
  type McpEndpoint,
  type McpEndpointPayload,
  type McpPreview,
  type McpStatus,
  createMcpEndpoint,
  deleteMcpEndpoint,
  getMcpStatus,
  previewMcpTools,
  rotateMcpToken,
  setMcpEnabled,
  updateMcpEndpoint,
} from "@/lib/api/mcp";
import { relativeTime } from "@/lib/devices-display";

const INPUT_CLASS =
  "w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-sub " +
  "text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] " +
  "focus:border-[var(--accent)]/50";

/**
 * 展开模式下超过这个数就建议改折叠：业界观察到模型在 30~40 个工具以上开始
 * 明显退化（选错工具、编造不存在的工具）。**只建议不拦截**——用户执意要一个
 * 全能端点是他自己的判断（docs/design/mcp-server.md §4.1）。
 */
const TOOL_HINT_THRESHOLD = 30;

function formatBytes(bytes: number): string {
  return bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`;
}

/**
 * 「MCP 服务」设置分区（docs/design/mcp-server.md §7）。
 *
 * 这一页要回答三个问题：
 * 1. **这个端点开放了什么** —— 勾中的服务就是它的全部能力面，工具数与上下文体积
 *    实时算给用户看；
 * 2. **怎么接进去** —— 令牌明文只出现一次，所以那一刻要把三种客户端的配置片段
 *    一并给全，别让用户回头再去查 MCP 文档；
 * 3. **怎么收回** —— 停用、轮换令牌、删除，三个入口都在端点卡上。
 */
export function McpSection() {
  const confirm = useConfirm();
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<McpEndpointPayload | null>(null);
  /** 一次性令牌：关掉就永远看不到了 */
  const [revealed, setRevealed] = useState<{ name: string; url: string; token: string } | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [preview, setPreview] = useState<McpPreview | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await getMcpStatus());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run<T>(action: () => Promise<T>): Promise<T | null> {
    setBusy(true);
    setError(null);
    try {
      const result = await action();
      await load();
      return result;
    } catch (e) {
      setError((e as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  if (!status) {
    return <p className="text-sub text-[var(--text-muted)]">{error ?? "加载中…"}</p>;
  }

  return (
    <div className="space-y-6">
      {error && <p className="text-sub text-[var(--danger)]">{error}</p>}

      {/* 总开关：关掉后所有端点一律 404，对外完全隐身 */}
      <div className="css-glass flex items-center justify-between gap-4 !rounded-xl p-4">
        <div className="min-w-0">
          <p className="text-ui font-medium">启用 MCP 服务</p>
          <p className="mt-0.5 text-caption text-[var(--text-muted)]">
            让 Claude Code、Cursor 等 AI 客户端通过 MCP 协议使用 movieclaw。
            关闭后所有端点立即返回 404。
          </p>
        </div>
        <Switch
          checked={status.enabled}
          disabled={busy}
          label="启用 MCP 服务"
          onChange={(next) => void run(() => setMcpEnabled(next))}
        />
      </div>

      {status.enabled && !status.external_url_configured && (
        <p className="rounded-xl border border-[var(--warn,#f5c451)]/35 bg-[var(--warn,#f5c451)]/10 px-3.5 py-2.5 text-caption text-[var(--text-muted)]">
          还没配置外部访问地址，下面的端点地址只有相对路径。外部客户端要连上，
          请先到「设置 → 网络」填写对外地址。
        </p>
      )}

      {revealed && (
        <TokenReveal detail={revealed} onClose={() => setRevealed(null)} />
      )}

      <section>
        <h3 className="group-label mb-2.5 px-1">端点</h3>
        {status.endpoints.length === 0 && !draft ? (
          <div className="css-glass flex flex-col items-center gap-3 !rounded-2xl px-6 py-12 text-center">
            <p className="text-ui font-medium">还没有 MCP 端点</p>
            <p className="max-w-md text-caption text-[var(--text-muted)]">
              建一个端点、勾选要开放的服务，AI 客户端填上地址和令牌就能查库存、
              搜资源、管订阅。每个端点的工具目录相互独立，给不同用途建不同端点即可。
            </p>
            <button
              type="button"
              onClick={() => setDraft(emptyDraft())}
              className="btn-glass mt-1 px-4 py-1.5 text-sub font-medium"
            >
              <PlusIcon className="size-4" />
              新建端点
            </button>
          </div>
        ) : (
          <div className="space-y-2.5">
            {status.endpoints.map((endpoint) => (
              <EndpointCard
                key={endpoint.id}
                endpoint={endpoint}
                busy={busy}
                expanded={expanded === endpoint.id}
                preview={expanded === endpoint.id ? preview : null}
                onToggleDetail={async () => {
                  if (expanded === endpoint.id) {
                    setExpanded(null);
                    return;
                  }
                  setExpanded(endpoint.id);
                  setPreview(null);
                  setPreview(await previewMcpTools(endpoint.services, endpoint.expand_tools));
                }}
                onToggleEnabled={(next) =>
                  void run(() => updateMcpEndpoint(endpoint.id, { enabled: next }))
                }
                onToggleExpand={(next) =>
                  void run(() => updateMcpEndpoint(endpoint.id, { expand_tools: next }))
                }
                onRotate={async () => {
                  const okToRotate = await confirm({
                    title: "轮换令牌？",
                    description:
                      "会生成一枚新令牌，旧令牌立即失效。已接入的客户端都要更新配置，" +
                      "否则会开始报 401。新令牌同样只显示一次。",
                    confirmLabel: "生成新令牌",
                  });
                  if (!okToRotate) return;
                  const created = await run(() => rotateMcpToken(endpoint.id));
                  if (created) {
                    setRevealed({
                      name: created.endpoint.name,
                      url: created.endpoint.url,
                      token: created.token,
                    });
                  }
                }}
                onDelete={async () => {
                  const okToDelete = await confirm({
                    title: `删除「${endpoint.name}」？`,
                    description:
                      endpoint.last_used_at
                        ? `端点地址与令牌一并作废，不可恢复。它最近一次被调用是${relativeTime(endpoint.last_used_at)}，删除后那个客户端会立刻失败。`
                        : "端点地址与令牌一并作废，不可恢复。它还没有被调用过。",
                    confirmLabel: "删除端点",
                    tone: "danger",
                  });
                  if (okToDelete) void run(() => deleteMcpEndpoint(endpoint.id));
                }}
              />
            ))}
            {!draft && (
              <button
                type="button"
                onClick={() => setDraft(emptyDraft())}
                className="btn-glass w-full justify-center border-dashed py-2.5 text-sub font-medium"
              >
                <PlusIcon className="size-4" />
                新建端点
              </button>
            )}
          </div>
        )}
      </section>

      {draft && (
        <EndpointForm
          draft={draft}
          services={status.services}
          baseUrl={status.base_url}
          busy={busy}
          onChange={setDraft}
          onCancel={() => setDraft(null)}
          onSubmit={async () => {
            const created = await run(() => createMcpEndpoint(draft));
            if (created) {
              setDraft(null);
              setRevealed({
                name: created.endpoint.name,
                url: created.endpoint.url,
                token: created.token,
              });
            }
          }}
        />
      )}
    </div>
  );
}

function emptyDraft(): McpEndpointPayload {
  return {
    name: "",
    slug: "",
    services: [],
    description: "",
    expand_tools: true,
    timeout_seconds: 300,
  };
}

function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="relative h-[22px] w-[38px] shrink-0 rounded-full bg-white/20 transition-colors disabled:opacity-40 aria-checked:bg-[var(--ok,#5fd39b)]"
    >
      <span
        className={`absolute left-[3px] top-[3px] size-4 rounded-full bg-white transition-transform ${
          checked ? "translate-x-4" : ""
        }`}
      />
    </button>
  );
}

function Badge({ children, tone }: { children: React.ReactNode; tone?: "danger" | "muted" }) {
  const color =
    tone === "danger"
      ? "text-[var(--danger)] border-[var(--danger)]/40"
      : "text-[var(--text-muted)] border-white/[0.14]";
  return (
    <span className={`rounded-full border px-2 py-0.5 text-caption ${color}`}>{children}</span>
  );
}

function EndpointCard({
  endpoint,
  busy,
  expanded,
  preview,
  onToggleDetail,
  onToggleEnabled,
  onToggleExpand,
  onRotate,
  onDelete,
}: {
  endpoint: McpEndpoint;
  busy: boolean;
  expanded: boolean;
  preview: McpPreview | null;
  onToggleDetail: () => void;
  onToggleEnabled: (next: boolean) => void;
  onToggleExpand: (next: boolean) => void;
  onRotate: () => void;
  onDelete: () => void;
}) {
  return (
    <div className={`css-glass !rounded-xl p-4 ${endpoint.enabled ? "" : "opacity-60"}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-ui font-medium">{endpoint.name}</p>
            <Badge>{endpoint.expand_tools ? "展开" : "折叠"}</Badge>
            {!endpoint.enabled && <Badge>已停用</Badge>}
          </div>
          <p className="mt-1 truncate font-mono text-caption text-[var(--text-muted)]">
            {endpoint.url}
          </p>
          <p className="mt-1 text-caption text-[var(--text-muted)]">
            {endpoint.tool_count} 个工具 · {endpoint.services.join("、")}
          </p>
          <p className="mt-0.5 text-caption text-[var(--text-faint)]">
            令牌 {endpoint.token_hint} ·{" "}
            {endpoint.last_used_at ? `最近调用 ${relativeTime(endpoint.last_used_at)}` : "从未调用"}
          </p>
          {endpoint.missing_services.length > 0 && (
            <p className="mt-1 text-caption text-[var(--warn,#f5c451)]">
              有 {endpoint.missing_services.length} 个服务在当前版本已不存在，已忽略：
              {endpoint.missing_services.join("、")}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <CopyButton
            text={endpoint.url}
            label="复制地址"
            className="btn-glass px-3 py-1.5 text-sub"
          />
          <Switch
            checked={endpoint.enabled}
            disabled={busy}
            label={`启用 ${endpoint.name}`}
            onChange={onToggleEnabled}
          />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/[0.06] pt-3">
        <button type="button" onClick={onToggleDetail} className="btn-glass px-3 py-1.5 text-sub font-medium">
          {expanded ? "收起工具目录" : "查看工具目录"}
        </button>
        <label className="ml-1 flex items-center gap-2 text-caption text-[var(--text-muted)]">
          <input
            type="checkbox"
            checked={endpoint.expand_tools}
            disabled={busy}
            onChange={(e) => onToggleExpand(e.target.checked)}
          />
          展开工具（关闭则一个服务一个工具）
        </label>
        <span className="flex-1" />
        <button type="button" onClick={onRotate} disabled={busy} className="btn-glass px-3 py-1.5 text-sub font-medium">
          轮换令牌
        </button>
        <button
          type="button"
          onClick={onDelete}
          disabled={busy}
          className="btn-glass px-3 py-1.5 text-sub font-medium !text-[#ff6b6b]"
        >
          删除
        </button>
      </div>

      {expanded && (
        <div className="mt-3 rounded-xl border border-white/[0.06] bg-white/[0.02] p-3">
          {!preview ? (
            <p className="text-caption text-[var(--text-muted)]">正在计算工具目录…</p>
          ) : (
            <>
              <p className="mb-2 text-caption text-[var(--text-muted)]">
                {preview.tool_count} 个工具 · 覆盖 {preview.command_count} 条命令 ·
                工具定义约 {formatBytes(preview.approx_bytes)}
              </p>
              <ul className="max-h-72 space-y-1.5 overflow-y-auto">
                {preview.tools.map((tool) => (
                  <li key={tool.name} className="text-caption">
                    <span className="font-mono text-[var(--accent)]">{tool.name}</span>
                    {tool.read_only && <span className="ml-1.5 text-[var(--text-faint)]">只读</span>}
                    {tool.destructive && <span className="ml-1.5 text-[var(--danger)]">破坏性</span>}
                    <span className="ml-1.5 text-[var(--text-muted)]">
                      {tool.description.split("\n")[0].slice(0, 80)}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function EndpointForm({
  draft,
  services,
  baseUrl,
  busy,
  onChange,
  onCancel,
  onSubmit,
}: {
  draft: McpEndpointPayload;
  services: McpStatus["services"];
  baseUrl: string;
  busy: boolean;
  onChange: (next: McpEndpointPayload) => void;
  onCancel: () => void;
  onSubmit: () => void;
}) {
  const expand = draft.expand_tools ?? true;
  const picked = useMemo(() => new Set(draft.services), [draft.services]);

  /** 实时汇总：工具数与上下文体积按当前模式算，只报数字不评判。 */
  const totals = useMemo(() => {
    const chosen = services.filter((s) => picked.has(s.domain));
    const commands = chosen.reduce((sum, s) => sum + s.command_count, 0);
    const bytes = chosen.reduce(
      (sum, s) => sum + (expand ? s.expanded_bytes : s.collapsed_bytes),
      0,
    );
    const collapsedBytes = chosen.reduce((sum, s) => sum + s.collapsed_bytes, 0);
    return {
      commands,
      bytes,
      collapsedBytes,
      tools: expand ? commands : chosen.length,
      services: chosen.length,
    };
  }, [services, picked, expand]);

  const overThreshold = expand && totals.tools > TOOL_HINT_THRESHOLD;

  return (
    <section className="css-glass space-y-4 !rounded-xl p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-ui font-medium">新建端点</h3>
        <button type="button" onClick={onCancel} aria-label="取消" className="btn-glass p-1.5">
          <XIcon className="size-4" />
        </button>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <label className="block">
          <span className="mb-1.5 block text-caption text-[var(--text-muted)]">端点名称</span>
          <input
            type="text"
            value={draft.name}
            onChange={(e) => onChange({ ...draft, name: e.target.value })}
            placeholder="如 家庭影音助理"
            className={INPUT_CLASS}
          />
        </label>
        <label className="block">
          <span className="mb-1.5 block text-caption text-[var(--text-muted)]">
            地址标识（小写字母、数字与连字符）
          </span>
          <input
            type="text"
            value={draft.slug}
            onChange={(e) => onChange({ ...draft, slug: e.target.value })}
            placeholder="home-assistant"
            className={INPUT_CLASS}
          />
        </label>
      </div>
      <p className="-mt-1 font-mono text-caption text-[var(--text-faint)]">
        {baseUrl || ""}/mcp/{draft.slug || "<地址标识>"}
      </p>

      <div className="flex items-center justify-between gap-4 rounded-xl border border-white/[0.06] bg-white/[0.02] px-3.5 py-3">
        <div className="min-w-0">
          <p className="text-sub font-medium">展开工具</p>
          <p className="mt-0.5 text-caption text-[var(--text-muted)]">
            开：一条命令一个工具（<span className="font-mono">subscriptions_update</span>），参数带类型；
            关：一个服务一个工具（<span className="font-mono">subscriptions</span>），工具少得多。
            不同客户端对工具面的适配不一样，两种都能用。
          </p>
        </div>
        <Switch
          checked={expand}
          label="展开工具"
          onChange={(next) => onChange({ ...draft, expand_tools: next })}
        />
      </div>

      <div>
        <p className="mb-2 text-caption text-[var(--text-muted)]">
          选择服务——勾中的服务，接进来的模型就能全部执行，和你自己在命令行上能做的一样。
          比如勾了「本地媒体库」，它就可以删除磁盘上的媒体文件。
        </p>
        <div className="grid gap-2 sm:grid-cols-2">
          {services.map((service) => {
            const on = picked.has(service.domain);
            return (
              <button
                key={service.domain}
                type="button"
                onClick={() =>
                  onChange({
                    ...draft,
                    services: on
                      ? draft.services.filter((s) => s !== service.domain)
                      : [...draft.services, service.domain],
                  })
                }
                className={`flex items-start gap-2.5 rounded-xl border px-3 py-2.5 text-left transition-colors ${
                  on
                    ? "border-[var(--accent)]/60 bg-[var(--accent-soft)]"
                    : "border-white/[0.08] bg-white/[0.02] hover:border-white/[0.18]"
                }`}
              >
                <input type="checkbox" checked={on} readOnly tabIndex={-1} className="mt-0.5" />
                <span className="min-w-0">
                  <span className="block text-sub font-medium">
                    {service.domain}
                    <span className="ml-1.5 text-caption font-normal text-[var(--text-faint)]">
                      {service.command_count} 条命令
                    </span>
                  </span>
                  <span className="mt-0.5 block text-caption text-[var(--text-muted)]">
                    {service.description}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <label className="block max-w-xs">
        <span className="mb-1.5 block text-caption text-[var(--text-muted)]">单次调用超时（秒）</span>
        <input
          type="number"
          min={5}
          max={900}
          value={draft.timeout_seconds ?? 300}
          onChange={(e) => onChange({ ...draft, timeout_seconds: Number(e.target.value) })}
          className={INPUT_CLASS}
        />
      </label>

      {overThreshold && (
        <p className="rounded-xl border border-[var(--warn,#f5c451)]/35 bg-[var(--warn,#f5c451)]/10 px-3.5 py-2.5 text-caption text-[var(--text-muted)]">
          工具偏多（{totals.tools} 个），模型选择准确率会下降。建议关掉「展开工具」——
          同样这 {totals.services} 个服务会变成 {totals.services} 个工具，
          上下文从约 {formatBytes(totals.bytes)} 降到约 {formatBytes(totals.collapsedBytes)}。
        </p>
      )}

      <div className="flex items-center justify-between gap-3 border-t border-white/[0.06] pt-3">
        <p className="text-caption text-[var(--text-muted)]">
          {totals.services === 0 ? (
            "至少选择一个服务才能创建端点"
          ) : (
            <>
              <span className="font-medium text-[var(--text)]">{totals.tools} 个工具</span>
              {" · "}
              {totals.services} 个服务 · 覆盖 {totals.commands} 条命令 · 工具定义约{" "}
              {formatBytes(totals.bytes)}
            </>
          )}
        </p>
        <div className="flex gap-2">
          <button type="button" onClick={onCancel} className="btn-glass px-3.5 py-1.5 text-sub font-medium">
            取消
          </button>
          <button
            type="button"
            onClick={onSubmit}
            disabled={busy || totals.services === 0 || !draft.name || !draft.slug}
            className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-40"
          >
            创建端点
          </button>
        </div>
      </div>
    </section>
  );
}

/**
 * 令牌只出现这一次（服务端只存哈希），所以这一屏要把接入需要的一切都给全：
 * 地址、令牌，以及三种客户端的可复制片段。
 */
function TokenReveal({
  detail,
  onClose,
}: {
  detail: { name: string; url: string; token: string };
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"claude" | "json" | "curl">("claude");
  const snippets: Record<typeof tab, string> = {
    claude:
      `claude mcp add --transport http movieclaw \\\n` +
      `  ${detail.url} \\\n` +
      `  --header "Authorization: Bearer ${detail.token}"`,
    json: JSON.stringify(
      {
        mcpServers: {
          movieclaw: {
            type: "http",
            url: detail.url,
            headers: { Authorization: `Bearer ${detail.token}` },
          },
        },
      },
      null,
      2,
    ),
    curl:
      `curl -sS ${detail.url} \\\n` +
      `  -H "Authorization: Bearer ${detail.token}" \\\n` +
      `  -H "Content-Type: application/json" \\\n` +
      `  -H "MCP-Protocol-Version: 2026-07-28" \\\n` +
      `  -H "Mcp-Method: tools/list" \\\n` +
      `  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'`,
  };

  return (
    <section className="css-glass space-y-3 !rounded-xl border border-[var(--accent)]/30 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-ui font-medium">「{detail.name}」已就绪</p>
          <p className="mt-0.5 text-caption text-[var(--danger)]">
            令牌只显示这一次。关掉就再也看不到明文了——服务端只保存哈希。
            丢了不要紧，随时可以轮换出一枚新的。
          </p>
        </div>
        <button type="button" onClick={onClose} aria-label="我已保存" className="btn-glass p-1.5">
          <XIcon className="size-4" />
        </button>
      </div>

      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 truncate rounded-lg bg-white/[0.05] px-3 py-2 font-mono text-caption">
          {detail.token}
        </code>
        <CopyButton text={detail.token} label="复制令牌" className="btn-glass shrink-0 px-3 py-1.5 text-sub" />
      </div>

      <div className="flex gap-1.5">
        {(
          [
            ["claude", "Claude Code"],
            ["json", "配置文件"],
            ["curl", "cURL 自检"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`rounded-full border px-3 py-1 text-caption ${
              tab === key
                ? "border-[var(--accent)] bg-white/[0.06] text-[var(--text)]"
                : "border-white/[0.14] text-[var(--text-muted)]"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="flex items-start gap-2">
        <pre className="scroll-thin min-w-0 flex-1 overflow-x-auto rounded-lg bg-black/40 px-3 py-2.5 font-mono text-caption leading-relaxed">
          {snippets[tab]}
        </pre>
        <CopyButton text={snippets[tab]} label="复制" className="btn-glass shrink-0 px-3 py-1.5 text-sub" />
      </div>

      <p className="text-caption text-[var(--text-faint)]">
        claude.ai 网页版的自定义连接器只支持 OAuth，暂时接不进来；
        Claude Code、Cursor、Cline 等本地客户端都可以正常使用。
      </p>
    </section>
  );
}
