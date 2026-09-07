"use client";

import { useCallback, useEffect, useState } from "react";

import { ArrowLeftIcon } from "@/components/icons";
import { EndpointForm } from "@/components/mcp/endpoint-form";
import { ToolCatalog } from "@/components/mcp/tool-catalog";
import { Badge, CodeBlock, CopyField, INPUT_CLASS, MetaRow, StatusDot, Switch, formatBytes } from "@/components/mcp/ui";
import {
  type McpEndpoint,
  type McpEndpointPayload,
  type McpPreview,
  type McpSelfCheck,
  type McpService,
  checkMcpEndpoint,
  previewMcpTools,
} from "@/lib/api/mcp";
import { relativeTime } from "@/lib/devices-display";

type Tab = "overview" | "tools" | "connect" | "settings";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "概览" },
  { id: "tools", label: "工具" },
  { id: "connect", label: "接入" },
  { id: "settings", label: "设置" },
];

/**
 * 端点详情：一个端点的全部真相都在这一屏，按「读 → 用 → 改 → 删」分四栏。
 *
 * 为什么是详情页而不是列表里的展开区：接入一个端点要同时看地址、令牌提示、工具面
 * 和示例命令，这些东西挤在列表行里谁也看不清；而且详情有自己的地址（?endpoint=slug），
 * 刷新、收藏、发给同事都能落到同一处。
 */
export function EndpointDetail({
  endpoint,
  services,
  baseUrl,
  busy,
  tab,
  onTab,
  onBack,
  onToggleEnabled,
  onSave,
  onRotate,
  onDelete,
}: {
  endpoint: McpEndpoint;
  services: McpService[];
  baseUrl: string;
  busy: boolean;
  tab: Tab;
  onTab: (next: Tab) => void;
  onBack: () => void;
  onToggleEnabled: (next: boolean) => void;
  onSave: (payload: McpEndpointPayload) => void;
  onRotate: () => void;
  onDelete: () => void;
}) {
  const [preview, setPreview] = useState<McpPreview | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const [check, setCheck] = useState<McpSelfCheck | null>(null);
  const [checking, setChecking] = useState(false);

  /** 自检：跑一次真实的协议往返 + 一次只读调用，回答「现在能不能用」。 */
  const runCheck = async () => {
    setChecking(true);
    try {
      setCheck(await checkMcpEndpoint(endpoint.id));
    } catch (e) {
      // 请求本身失败（网络断、接口不存在）也要显示成一次「未通过」，而不是把异常
      // 抛给 React——自检的全部意义就是给出结论，静默失败是最坏的结果
      setCheck({
        ok: false,
        message: `自检请求失败：${(e as Error).message}`,
        protocol_version: "",
        tool_count: 0,
        elapsed_ms: 0,
        probe_tool: "",
        probe_ok: false,
        probe_message: "",
        warnings: [],
      });
    } finally {
      setChecking(false);
    }
  };

  const loadPreview = useCallback(async () => {
    setPreview(await previewMcpTools(endpoint.services, endpoint.expand_tools));
  }, [endpoint.services, endpoint.expand_tools]);

  useEffect(() => {
    setPreview(null);
    void loadPreview();
  }, [loadPreview]);

  const fullUrl = endpoint.url.startsWith("http")
    ? endpoint.url
    : `${baseUrl || "http://<你的地址>"}${endpoint.url}`;

  return (
    <div className="space-y-5">
      {/* 头部：返回 + 名称 + 状态 + 启停。启停放在这里而不是设置栏——它是最高频的开关 */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <button
            type="button"
            onClick={onBack}
            className="btn-glass mb-2.5 px-2.5 py-1 text-caption font-medium text-[var(--text-muted)]"
          >
            <ArrowLeftIcon className="size-3.5" />
            全部端点
          </button>
          <div className="flex flex-wrap items-center gap-2">
            <StatusDot on={endpoint.enabled} title={endpoint.enabled ? "运行中" : "已停用"} />
            <h2 className="text-xl font-medium tracking-tight">{endpoint.name}</h2>
            <Badge tone="accent">{endpoint.expand_tools ? "展开" : "折叠"}</Badge>
            <span className="text-caption tabular-nums text-[var(--text-muted)]">
              {endpoint.tool_count} 个工具
            </span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          <span className="text-caption text-[var(--text-muted)]">
            {endpoint.enabled ? "已启用" : "已停用"}
          </span>
          <Switch
            checked={endpoint.enabled}
            disabled={busy}
            label={`启用 ${endpoint.name}`}
            onChange={onToggleEnabled}
          />
        </div>
      </div>

      <nav className="flex gap-1 border-b border-white/[0.07]">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => onTab(item.id)}
            className={`-mb-px border-b-2 px-3 py-2 text-sub transition-colors ${
              tab === item.id
                ? "border-[var(--accent)] text-[var(--text)]"
                : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]"
            }`}
          >
            {item.label}
          </button>
        ))}
      </nav>

      {tab === "overview" && (
        <div className="space-y-5">
          <div>
            <p className="mb-1.5 text-caption text-[var(--text-muted)]">端点地址</p>
            <CopyField value={fullUrl} label="复制地址" />
            {!endpoint.url.startsWith("http") && (
              <p className="mt-1.5 text-caption text-[var(--warn,#f5c451)]">
                还没配置外部访问地址，这里只有相对路径。外部客户端要连上，先去「设置 → 网络」填对外地址。
              </p>
            )}
          </div>

          {/* 自检：配完之后最想问的那句「它现在能用吗」，就地给答案 */}
          <div className="rounded-xl border border-white/[0.07] p-4">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sub font-medium">连通性自检</p>
                <p className="mt-0.5 text-caption text-[var(--text-muted)]">
                  跑一次真实的协议握手、列一遍工具，再挑个只读工具实际调一次。
                </p>
              </div>
              <button
                type="button"
                onClick={() => void runCheck()}
                disabled={checking}
                className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium disabled:opacity-50"
              >
                {checking ? "自检中…" : check ? "重新自检" : "运行自检"}
              </button>
            </div>

            {check && (
              <div className="mt-3 space-y-2 border-t border-white/[0.06] pt-3">
                <p className="flex items-center gap-2 text-sub">
                  <StatusDot on={check.ok} title={check.ok ? "通过" : "未通过"} />
                  <span className={check.ok ? "" : "text-[var(--danger)]"}>{check.message}</span>
                  <span className="text-caption tabular-nums text-[var(--text-faint)]">
                    {check.elapsed_ms} ms
                  </span>
                </p>
                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-caption">
                  <dt className="text-[var(--text-faint)]">协议</dt>
                  <dd className="font-mono">{check.protocol_version || "—"}</dd>
                  <dt className="text-[var(--text-faint)]">工具</dt>
                  <dd className="tabular-nums">{check.tool_count} 个</dd>
                  {check.probe_tool && (
                    <>
                      <dt className="text-[var(--text-faint)]">试调</dt>
                      <dd className="min-w-0">
                        <span className="font-mono text-[var(--accent)]">{check.probe_tool}</span>
                        <span className={check.probe_ok ? "ml-2" : "ml-2 text-[var(--danger)]"}>
                          {check.probe_ok ? "成功" : "失败"}
                        </span>
                        {check.probe_message && (
                          <span className="ml-2 text-[var(--text-faint)]">
                            {check.probe_message.slice(0, 60)}
                          </span>
                        )}
                      </dd>
                    </>
                  )}
                </dl>
                {check.warnings.map((warning) => (
                  <p
                    key={warning}
                    className="rounded-lg border border-[var(--warn,#f5c451)]/30 bg-[var(--warn,#f5c451)]/10 px-2.5 py-1.5 text-caption"
                  >
                    {warning}
                  </p>
                ))}
              </div>
            )}
          </div>

          <div className="rounded-xl border border-white/[0.07] px-4 py-2">
            <MetaRow label="服务">
              <span className="font-mono">{endpoint.services.join("、")}</span>
            </MetaRow>
            <MetaRow label="工具">
              {endpoint.tool_count} 个（{endpoint.expand_tools ? "展开：一命令一工具" : "折叠：一服务一工具"}）
              {preview && ` · 定义约 ${formatBytes(preview.approx_bytes)}`}
            </MetaRow>
            <MetaRow label="令牌">
              <span className="font-mono">{endpoint.token_hint}</span>
              <button
                type="button"
                onClick={onRotate}
                disabled={busy}
                className="btn-glass ml-2 px-2.5 py-0.5 text-caption"
              >
                轮换
              </button>
            </MetaRow>
            <MetaRow label="超时">{endpoint.timeout_seconds} 秒</MetaRow>
            <MetaRow label="最近调用">
              {endpoint.last_used_at ? relativeTime(endpoint.last_used_at) : "从未调用"}
            </MetaRow>
            <MetaRow label="创建于">{endpoint.created_at.slice(0, 16).replace("T", " ")}</MetaRow>
          </div>

          {endpoint.missing_services.length > 0 && (
            <p className="rounded-lg border border-[var(--warn,#f5c451)]/35 bg-[var(--warn,#f5c451)]/10 px-3 py-2 text-caption">
              配置里有 {endpoint.missing_services.length} 个服务在当前版本已不存在，已忽略：
              <span className="font-mono">{endpoint.missing_services.join("、")}</span>
            </p>
          )}
        </div>
      )}

      {tab === "tools" &&
        (preview ? (
          <ToolCatalog tools={preview.tools} />
        ) : (
          <p className="text-sub text-[var(--text-muted)]">正在计算工具目录…</p>
        ))}

      {tab === "connect" && <ConnectGuide url={fullUrl} hint={endpoint.token_hint} />}

      {tab === "settings" && (
        <div className="space-y-8">
          <EndpointForm
            initial={{
              name: endpoint.name,
              slug: endpoint.slug,
              services: endpoint.services,
              description: endpoint.description,
              expand_tools: endpoint.expand_tools,
              timeout_seconds: endpoint.timeout_seconds,
            }}
            services={services}
            baseUrl={baseUrl}
            busy={busy}
            slugEditable={false}
            submitLabel="保存修改"
            onSubmit={onSave}
            onCancel={onBack}
          />

          {/* 危险区：沉到最底，删除要打字确认——和 Stripe/GitHub 的处理一致，
              因为端点一删，接入它的客户端立刻全断，而且不可恢复 */}
          <section className="rounded-xl border border-[var(--danger)]/30 p-4">
            <h3 className="text-sub font-medium text-[var(--danger)]">危险操作</h3>
            <p className="mt-1 text-caption leading-relaxed text-[var(--text-muted)]">
              删除后地址与令牌一并作废，不可恢复；已接入的客户端会立刻失败。
              确认请输入端点标识 <span className="font-mono text-[var(--text)]">{endpoint.slug}</span>。
            </p>
            <div className="mt-3 flex items-center gap-2">
              <input
                type="text"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                placeholder={endpoint.slug}
                className={`${INPUT_CLASS} max-w-[220px] font-mono`}
              />
              <button
                type="button"
                disabled={busy || confirmText !== endpoint.slug}
                onClick={onDelete}
                className="btn-glass px-3.5 py-1.5 text-sub font-medium !text-[#ff6b6b] disabled:opacity-40"
              >
                删除这个端点
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

/**
 * 接入指引：把「拿到地址和令牌之后要做什么」讲完整。
 *
 * 令牌明文只在创建/轮换那一刻存在，所以这里的片段用占位符，并明确写出该把哪个词
 * 替换掉——用户回头再看这一页时，需要的是步骤而不是密钥。
 */
function ConnectGuide({ url, hint }: { url: string; hint: string }) {
  const [client, setClient] = useState<"claude" | "json" | "curl">("claude");
  const token = "<你的端点令牌>";
  const snippets = {
    claude: {
      lang: "bash",
      code:
        `claude mcp add --transport http movieclaw \\\n` +
        `  ${url} \\\n` +
        `  --header "Authorization: Bearer ${token}"`,
      note: "在你要用的机器上执行。加完用 /mcp 确认 movieclaw 已连接。",
    },
    json: {
      lang: "json",
      code: JSON.stringify(
        {
          mcpServers: {
            movieclaw: { type: "http", url, headers: { Authorization: `Bearer ${token}` } },
          },
        },
        null,
        2,
      ),
      note: "Cursor、Cline 等把 MCP 服务器写在配置文件里的客户端用这段。",
    },
    curl: {
      lang: "bash",
      code:
        `curl -sS ${url} \\\n` +
        `  -H "Authorization: Bearer ${token}" \\\n` +
        `  -H "Content-Type: application/json" \\\n` +
        `  -H "MCP-Protocol-Version: 2026-07-28" \\\n` +
        `  -H "Mcp-Method: tools/list" \\\n` +
        `  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{\n` +
        `       "io.modelcontextprotocol/protocolVersion":"2026-07-28",\n` +
        `       "io.modelcontextprotocol/clientCapabilities":{}}}}'`,
      note: "接不通时先跑这条：200 且返回工具清单说明端点没问题，问题在客户端配置。",
    },
  } as const;

  return (
    <div className="space-y-4">
      <div className="flex gap-1.5">
        {(
          [
            ["claude", "Claude Code"],
            ["json", "配置文件"],
            ["curl", "cURL 自检"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setClient(id)}
            className={`rounded-full border px-3 py-1 text-caption transition-colors ${
              client === id
                ? "border-[var(--accent)] bg-white/[0.06] text-[var(--text)]"
                : "border-white/[0.12] text-[var(--text-muted)] hover:text-[var(--text)]"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <CodeBlock code={snippets[client].code} lang={snippets[client].lang} />
      <p className="text-caption text-[var(--text-muted)]">{snippets[client].note}</p>

      <div className="rounded-xl border border-white/[0.07] px-4 py-3 text-caption leading-relaxed text-[var(--text-muted)]">
        <p className="mb-1.5 text-sub font-medium text-[var(--text)]">把 {token} 换成什么</p>
        <p>
          令牌明文只在创建和轮换时显示一次，服务端只存哈希。当前这枚的指纹是
          <span className="mx-1 font-mono text-[var(--text)]">{hint}</span>；
          忘了就在「概览」里轮换一枚新的（旧的立即失效）。
        </p>
        <p className="mt-2">
          claude.ai 网页版的自定义连接器只支持 OAuth，暂时接不进来；
          Claude Code、Cursor、Cline 等本地客户端都可以。
        </p>
      </div>
    </div>
  );
}
