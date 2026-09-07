"use client";

import { useEffect, useMemo, useState } from "react";

import { XIcon } from "@/components/icons";
import { INPUT_CLASS, formatBytes } from "@/components/mcp/ui";
import type { McpEndpointPayload, McpService, McpToolPreview } from "@/lib/api/mcp";
import { previewMcpTools } from "@/lib/api/mcp";

/** 展开模式超过这个数就建议改折叠（业界观察到的模型退化下沿）。只建议，不拦。 */
const TOOL_HINT_THRESHOLD = 30;

/**
 * 新建 / 编辑端点。
 *
 * 旧版是把 26 个服务双列铺开的长表单，滚三屏、没有搜索，滚下去就看不见自己选了
 * 什么。这一版按「配置 ↔ 后果」的因果关系重排：
 *
 * - **左栏配置，右栏实时后果**。右边始终显示这套配置真实产出的工具面（数量、
 *   上下文体积、前若干个工具名），改左边右边立刻变——不用等创建完才知道。
 * - **服务选择器可搜索、已选置顶**。26 个服务里找 3 个，靠的是搜索不是滚动；
 *   每条两行（域名 / 说明），说明是判断该不该勾的唯一依据，不能被截断。
 * - **工具模式从复选框升格成两张对比卡**，把差异（工具数、体积、参数形态）直接写
 *   在卡面上——这是决定端点形态的选择，不该长得像个附属开关。
 */
/** 与后端 settings/mcp.py 的 SLUG_PATTERN 同口径：小写字母数字与连字符，首尾不为连字符。 */
const SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$/;

export function EndpointForm({
  initial,
  services,
  baseUrl,
  busy,
  slugEditable,
  takenSlugs = [],
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial: McpEndpointPayload;
  services: McpService[];
  baseUrl: string;
  busy: boolean;
  slugEditable: boolean;
  /** 已被占用的地址标识：重名要在输入时就说，而不是等提交换回一个 409 */
  takenSlugs?: string[];
  submitLabel: string;
  onSubmit: (payload: McpEndpointPayload) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState<McpToolPreview[] | null>(null);
  const expand = draft.expand_tools ?? true;
  const picked = useMemo(() => new Set(draft.services), [draft.services]);

  /** 地址标识的即时校验。空值不报错（还没开始填），有值才判。 */
  const slugError = useMemo(() => {
    const slug = draft.slug.trim();
    if (!slug || !slugEditable) return "";
    if (!SLUG_PATTERN.test(slug)) return "只能用小写字母、数字和连字符，且不能以连字符开头或结尾";
    if (takenSlugs.includes(slug)) return "这个标识已被其他端点占用";
    return "";
  }, [draft.slug, slugEditable, takenSlugs]);

  /** 右栏预览：服务或模式一变就重算。请求很轻（纯内存渲染），不做防抖也不卡。 */
  useEffect(() => {
    let alive = true;
    if (draft.services.length === 0) {
      setPreview([]);
      return;
    }
    void previewMcpTools(draft.services, expand).then((result) => {
      if (alive) setPreview(result.tools);
    });
    return () => {
      alive = false;
    };
  }, [draft.services, expand]);

  const totals = useMemo(() => {
    const chosen = services.filter((s) => picked.has(s.domain));
    return {
      services: chosen.length,
      commands: chosen.reduce((sum, s) => sum + s.command_count, 0),
      expandedBytes: chosen.reduce((sum, s) => sum + s.expanded_bytes, 0),
      collapsedBytes: chosen.reduce((sum, s) => sum + s.collapsed_bytes, 0),
    };
  }, [services, picked]);
  const toolCount = expand ? totals.commands : totals.services;
  const bytes = expand ? totals.expandedBytes : totals.collapsedBytes;

  const visible = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    const rows = keyword
      ? services.filter(
          (s) =>
            s.domain.toLowerCase().includes(keyword) ||
            s.description.toLowerCase().includes(keyword),
        )
      : services;
    // 已选的置顶：滚到哪儿都看得见自己选了什么
    return [...rows].sort((a, b) => Number(picked.has(b.domain)) - Number(picked.has(a.domain)));
  }, [services, query, picked]);

  const toggle = (domain: string) =>
    setDraft((d) => ({
      ...d,
      services: picked.has(domain)
        ? d.services.filter((s) => s !== domain)
        : [...d.services, domain],
    }));

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-medium tracking-tight">{submitLabel}</h2>
        <button type="button" onClick={onCancel} aria-label="取消" className="btn-glass p-1.5">
          <XIcon className="size-4" />
        </button>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        {/* ── 左栏：配置 ───────────────────────────────────────── */}
        <div className="space-y-5">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block">
              <span className="mb-1.5 block text-caption text-[var(--text-muted)]">端点名称</span>
              <input
                type="text"
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                placeholder="家庭影音助理"
                className={INPUT_CLASS}
              />
            </label>
            <label className="block">
              <span className="mb-1.5 block text-caption text-[var(--text-muted)]">
                地址标识{!slugEditable && "（建成后不可改）"}
              </span>
              <input
                type="text"
                value={draft.slug}
                disabled={!slugEditable}
                onChange={(e) => setDraft({ ...draft, slug: e.target.value })}
                placeholder="home-assistant"
                aria-invalid={Boolean(slugError)}
                className={`${INPUT_CLASS} font-mono disabled:opacity-50 ${
                  slugError ? "border-[var(--danger)]/60" : ""
                }`}
              />
            </label>
          </div>
          {slugError ? (
            <p className="-mt-2 text-caption text-[var(--danger)]">{slugError}</p>
          ) : (
            <p className="-mt-2 font-mono text-caption text-[var(--text-faint)]">
              {baseUrl}/mcp/{draft.slug || "<地址标识>"}
            </p>
          )}

          {/* 工具模式：两张对比卡，差异写在卡面上 */}
          <div>
            <p className="mb-2 text-caption text-[var(--text-muted)]">工具形态</p>
            <div className="grid gap-2 sm:grid-cols-2">
              {[
                {
                  on: expand,
                  title: "展开",
                  sample: "subscriptions_update",
                  desc: "一条命令一个工具，参数带类型，模型照 schema 填",
                  count: totals.commands,
                  size: totals.expandedBytes,
                },
                {
                  on: !expand,
                  title: "折叠",
                  sample: "subscriptions(command, params)",
                  desc: "一个服务一个工具，工具少、占的上下文小",
                  count: totals.services,
                  size: totals.collapsedBytes,
                },
              ].map((mode) => (
                <button
                  key={mode.title}
                  type="button"
                  onClick={() => setDraft({ ...draft, expand_tools: mode.title === "展开" })}
                  className={`rounded-xl border px-3 py-2.5 text-left transition-colors ${
                    mode.on
                      ? "border-[var(--accent)]/60 bg-[var(--accent-soft)]"
                      : "border-white/[0.08] bg-white/[0.02] hover:border-white/[0.18]"
                  }`}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-sub font-medium">{mode.title}</span>
                    <span className="text-caption tabular-nums text-[var(--text-muted)]">
                      {totals.services > 0 ? `${mode.count} 个工具 · ${formatBytes(mode.size)}` : "—"}
                    </span>
                  </div>
                  <p className="mt-1 font-mono text-[11px] text-[var(--accent)]">{mode.sample}</p>
                  <p className="mt-1 text-caption leading-snug text-[var(--text-muted)]">{mode.desc}</p>
                </button>
              ))}
            </div>
          </div>

          {/* 服务选择器：搜索 + 单行 + 已选置顶 */}
          <div>
            <div className="mb-2 flex items-center justify-between gap-3">
              <p className="text-caption text-[var(--text-muted)]">
                开放的服务 · 已选 {totals.services} / {services.length}
              </p>
              <div className="flex items-center gap-2">
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="搜索服务…"
                  className="field-shell w-44 rounded-lg border border-white/[0.08] bg-black/25 px-2.5 py-1 text-caption outline-none placeholder:text-[var(--text-faint)]"
                />
                {totals.services > 0 && (
                  <button
                    type="button"
                    onClick={() => setDraft({ ...draft, services: [] })}
                    className="btn-glass px-2.5 py-1 text-caption"
                  >
                    清空
                  </button>
                )}
              </div>
            </div>

            <div className="scroll-thin max-h-[340px] overflow-y-auto rounded-xl border border-white/[0.07] divide-y divide-white/[0.05]">
              {visible.map((service) => {
                const on = picked.has(service.domain);
                return (
                  <label
                    key={service.domain}
                    className={`flex cursor-pointer items-start gap-2.5 px-3 py-2 transition-colors ${
                      on ? "bg-[var(--accent-soft)]" : "hover:bg-white/[0.03]"
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => toggle(service.domain)}
                      className="mt-1"
                    />
                    {/* 两行：域名与说明各一行。勾一个服务等于把它的全部命令交给模型，
                        而说明正是判断该不该勾的依据——截成半句话就等于没写 */}
                    <div className="min-w-0 flex-1 space-y-0.5">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-sub">{service.domain}</span>
                        <span className="text-caption tabular-nums text-[var(--text-faint)]">
                          {service.command_count} 条命令
                        </span>
                      </div>
                      <p className="text-caption leading-relaxed text-[var(--text-muted)]">
                        {service.description}
                      </p>
                    </div>
                  </label>
                );
              })}
            </div>
            <p className="mt-2 text-caption leading-relaxed text-[var(--text-faint)]">
              勾中的服务，接进来的模型就能全部执行，和你自己在命令行上能做的一样——
              勾了「library」它就可以删除磁盘上的媒体文件。
            </p>
          </div>

          <label className="block max-w-[200px]">
            <span className="mb-1.5 block text-caption text-[var(--text-muted)]">单次调用超时（秒）</span>
            <input
              type="number"
              min={5}
              max={900}
              value={draft.timeout_seconds ?? 300}
              onChange={(e) => setDraft({ ...draft, timeout_seconds: Number(e.target.value) })}
              className={`${INPUT_CLASS} tabular-nums`}
            />
          </label>
        </div>

        {/* ── 右栏：实时后果 ───────────────────────────────────── */}
        <aside className="lg:sticky lg:top-4 lg:self-start">
          <div className="rounded-xl border border-white/[0.08] bg-black/20 p-4">
            <p className="text-caption text-[var(--text-faint)]">客户端将看到</p>
            <p className="mt-1 text-2xl font-medium tabular-nums tracking-tight">
              {toolCount} <span className="text-base font-normal text-[var(--text-muted)]">个工具</span>
            </p>
            <p className="mt-0.5 text-caption text-[var(--text-muted)]">
              {totals.services} 个服务 · 覆盖 {totals.commands} 条命令 · 定义约 {formatBytes(bytes)}
            </p>

            {expand && toolCount > TOOL_HINT_THRESHOLD && (
              <p className="mt-3 rounded-lg border border-[var(--warn,#f5c451)]/35 bg-[var(--warn,#f5c451)]/10 px-2.5 py-2 text-caption leading-relaxed">
                超过 {TOOL_HINT_THRESHOLD} 个工具后模型选择准确率会下降。
                改用<b>折叠</b>可降到 {totals.services} 个工具、约 {formatBytes(totals.collapsedBytes)}。
              </p>
            )}

            <div className="mt-3 border-t border-white/[0.06] pt-3">
              {preview === null ? (
                <p className="text-caption text-[var(--text-faint)]">正在计算…</p>
              ) : preview.length === 0 ? (
                <p className="text-caption text-[var(--text-faint)]">选中服务后这里会列出工具</p>
              ) : (
                <ul className="scroll-thin max-h-64 space-y-1 overflow-y-auto">
                  {preview.slice(0, 60).map((tool) => (
                    <li key={tool.name} className="truncate font-mono text-caption text-[var(--accent)]">
                      {tool.name}
                    </li>
                  ))}
                  {preview.length > 60 && (
                    <li className="text-caption text-[var(--text-faint)]">
                      …… 还有 {preview.length - 60} 个
                    </li>
                  )}
                </ul>
              )}
            </div>
          </div>
        </aside>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-white/[0.06] pt-4">
        <button type="button" onClick={onCancel} className="btn-glass px-3.5 py-1.5 text-sub font-medium">
          取消
        </button>
        <button
          type="button"
          onClick={() => onSubmit(draft)}
          disabled={
            busy || !draft.name.trim() || !draft.slug.trim() || totals.services === 0 || Boolean(slugError)
          }
          className="btn-glass px-3.5 py-1.5 text-sub font-medium disabled:opacity-40"
        >
          {submitLabel}
        </button>
      </div>
    </div>
  );
}
