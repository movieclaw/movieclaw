"use client";

import { useCallback, useEffect, useState } from "react";

import { ArrowLeftIcon } from "@/components/icons";
import { EndpointForm } from "@/components/mcp/endpoint-form";
import { ToolCatalog } from "@/components/mcp/tool-catalog";
import {
  CodeBlock,
  CopyField,
  formatBytes,
  INPUT_CLASS,
  MetaRow,
  ServiceChips,
  StatusDot,
} from "@/components/mcp/ui";
import {
  type McpEndpoint,
  type McpEndpointPayload,
  type McpPreview,
  type McpSelfCheck,
  type McpService,
  checkMcpEndpoint,
  previewMcpTools,
} from "@/lib/api/mcp";
import { useBackdrop } from "@/lib/backdrop";
import { relativeTime } from "@/lib/devices-display";
import { LiquidGlassButton } from "@/vendor/liquid-glass";

type Tab = "overview" | "tools" | "settings";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "概览" },
  { id: "tools", label: "工具" },
  { id: "settings", label: "设置" },
];

/**
 * 端点详情：一个端点的全部真相都在这一屏，按「读 → 用 → 改 → 删」分三栏。
 *
 * 为什么是详情页而不是列表里的展开区：接一个端点要同时看地址、认证头、令牌提示
 * 和工具面，这些东西挤在列表行里谁也看不清；而且详情有自己的地址（?endpoint=slug），
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
  const { backdrop } = useBackdrop();
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
      {/* 头部：标题只放名字，其余降一级。
          此前是「状态点 + 名字 + 形态徽章 + 工具数」全挤在标题行上——一个徽章
          紧贴 20px 的标题，读起来既不像标题也不像标签。现在分三层：
          返回 → 标题 + 启停 → 一行淡色元信息（状态 / 地址 / 形态）。
          工具数挪到「工具」页签上，数字就长在它对应的入口旁边。 */}
      <div>
        <button
          type="button"
          onClick={onBack}
          className="btn-glass mb-3 px-2.5 py-1 text-caption font-medium text-[var(--text-muted)]"
        >
          <ArrowLeftIcon className="size-3.5" />
          全部端点
        </button>

        <div className="flex items-start justify-between gap-4">
          <h2 className="min-w-0 truncate text-xl font-medium tracking-tight">{endpoint.name}</h2>
          <div className="flex shrink-0 items-center gap-2.5">
            <span className="text-caption text-[var(--text-muted)]">
              {endpoint.enabled ? "已启用" : "已停用"}
            </span>
            <LiquidGlassButton
              backgroundImage={backdrop}
              variant="dark"
              checked={endpoint.enabled}
              aria-label={`启用 ${endpoint.name}`}
              onCheckedChange={(next: boolean) => {
                if (!busy) onToggleEnabled(next);
              }}
              className="!min-h-0 !w-auto !gap-0 !bg-transparent !p-0"
            >
              <span className="sr-only">{endpoint.enabled ? "已开启" : "已关闭"}</span>
            </LiquidGlassButton>
          </div>
        </div>

        <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-[var(--text-muted)]">
          <span className="flex items-center gap-1.5">
            <StatusDot on={endpoint.enabled} title={endpoint.enabled ? "运行中" : "已停用"} />
            {endpoint.enabled ? "运行中" : "已停用"}
          </span>
          <span className="text-[var(--text-faint)]">·</span>
          <span className="font-mono">/mcp/{endpoint.slug}</span>
          <span className="text-[var(--text-faint)]">·</span>
          <span>{endpoint.expand_tools ? "一命令一工具" : "一服务一工具"}</span>
        </div>
      </div>

      <nav className="flex gap-1 border-b border-white/[0.07]">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => onTab(item.id)}
            className={`-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sub transition-colors ${
              tab === item.id
                ? "border-[var(--accent)] text-[var(--text)]"
                : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]"
            }`}
          >
            {item.label}
            {item.id === "tools" && (
              <span className="tabular-nums text-caption text-[var(--text-faint)]">
                {endpoint.tool_count}
              </span>
            )}
          </button>
        ))}
      </nav>

      {tab === "overview" && (
        <div className="space-y-5">
          {/* 接进一个客户端要填的全部东西，就这两行。原先它们独占一个「接入」页签，
              而那个页签在去掉各端示例后只剩这两样，还和概览重复了一遍端点地址——
              一个页签换一次点击、看到的是别处已经有的内容，不如并进来。 */}
          <div>
            <p className="mb-1.5 text-caption text-[var(--text-muted)]">端点地址</p>
            <CopyField value={fullUrl} label="复制地址" />
            {!endpoint.url.startsWith("http") && (
              <p className="mt-1.5 text-caption text-[var(--warn,#f5c451)]">
                还没配置外部访问地址，这里只有相对路径。外部客户端要连上，先去「设置 → 网络」填对外地址。
              </p>
            )}
          </div>

          <div>
            <p className="mb-1.5 text-caption text-[var(--text-muted)]">认证请求头</p>
            <CodeBlock code="Authorization: Bearer <你的端点令牌>" lang="http" />
            <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-muted)]">
              令牌明文只在创建和轮换时显示一次（服务端只存哈希），忘了就在下面轮换一枚新的。
              传输是 Streamable HTTP，客户端里选「HTTP」而不是 SSE；claude.ai 网页版的自定义
              连接器只支持 OAuth，暂时接不进来，Claude Code、Cursor、Cline 等本地客户端都可以。
            </p>
          </div>

          {/* 自检：配完之后最想问的那句「它现在能用吗」，就地给答案 */}
          <div className="rounded-xl border border-white/[0.07] p-4">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sub font-medium">连通性自检</p>
                <p className="mt-0.5 text-caption text-[var(--text-muted)]">
                  跑一遍真实协议，只试调只读工具，不改任何状态。
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
              {/* 详情页不设上限：这一屏就是要看全「到底开放了什么」 */}
              <ServiceChips services={endpoint.services} max={endpoint.services.length} />
            </MetaRow>
            {/* 只留这几行：工具体积、令牌、最近调用。
                工具数在页签上、工具形态在标题下的元信息行里、超时在「设置」页签里可改，
                创建时间答不了任何问题——重复一遍只是把真正要看的三行冲淡。 */}
            <MetaRow label="工具">
              {preview ? `定义约 ${formatBytes(preview.approx_bytes)}` : "计算中…"}
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
            <MetaRow label="最近调用">
              {endpoint.last_used_at ? relativeTime(endpoint.last_used_at) : "从未调用"}
            </MetaRow>
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
