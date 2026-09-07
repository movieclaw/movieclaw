"use client";

import { useMemo, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { Badge } from "@/components/mcp/ui";
import type { McpToolPreview } from "@/lib/api/mcp";

/**
 * 工具目录：这个端点到底把什么交给了模型。
 *
 * 旧版是一个塞满截断文本的小滚动框——扫不了、搜不了、也看不到参数。开发者在这
 * 里真正要回答的问题只有三个，所以界面就按这三个问题组织：
 *
 * 1. **有没有我要的那个工具** → 顶部搜索，工具名与说明一起匹配；
 * 2. **这个工具属于哪个服务** → 按服务分组，组头带数量，能整组折叠；
 * 3. **它要什么参数** → 点开一行就是参数表（名称/类型/必填/落点/说明），
 *    参数名可直接复制。这是判断「模型能不能用对」的唯一依据。
 */
export function ToolCatalog({ tools }: { tools: McpToolPreview[] }) {
  const [query, setQuery] = useState("");
  const [openTool, setOpenTool] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const groups = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    const matched = keyword
      ? tools.filter(
          (t) =>
            t.name.toLowerCase().includes(keyword) ||
            t.description.toLowerCase().includes(keyword),
        )
      : tools;
    const byService = new Map<string, McpToolPreview[]>();
    for (const tool of matched) {
      const list = byService.get(tool.service) ?? [];
      list.push(tool);
      byService.set(tool.service, list);
    }
    return [...byService.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [tools, query]);

  const shown = groups.reduce((sum, [, list]) => sum + list.length, 0);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索工具名或说明…"
          className="field-shell w-full max-w-sm rounded-lg border border-white/[0.08] bg-black/25 px-3 py-1.5 text-sub outline-none placeholder:text-[var(--text-faint)]"
        />
        <span className="shrink-0 text-caption tabular-nums text-[var(--text-faint)]">
          {query ? `${shown} / ${tools.length}` : `${tools.length}`} 个工具
        </span>
      </div>

      {shown === 0 && (
        <p className="py-6 text-center text-sub text-[var(--text-muted)]">
          没有匹配「{query}」的工具
        </p>
      )}

      {groups.map(([service, list]) => {
        const folded = collapsed.has(service);
        return (
          <section key={service} className="overflow-hidden rounded-xl border border-white/[0.07]">
            <button
              type="button"
              onClick={() =>
                setCollapsed((prev) => {
                  const next = new Set(prev);
                  if (next.has(service)) next.delete(service);
                  else next.add(service);
                  return next;
                })
              }
              className="flex w-full items-center gap-2 bg-white/[0.03] px-3 py-2 text-left hover:bg-white/[0.05]"
            >
              <span className="text-[var(--text-faint)]">{folded ? "▸" : "▾"}</span>
              <span className="font-mono text-sub text-[var(--text)]">{service}</span>
              <span className="text-caption text-[var(--text-faint)]">{list.length} 个工具</span>
            </button>

            {!folded && (
              <ul className="divide-y divide-white/[0.05]">
                {list.map((tool) => {
                  const open = openTool === tool.name;
                  return (
                    <li key={tool.name}>
                      <button
                        type="button"
                        onClick={() => setOpenTool(open ? null : tool.name)}
                        className="flex w-full items-baseline gap-2 px-3 py-2 text-left hover:bg-white/[0.03]"
                      >
                        <span className="shrink-0 font-mono text-sub text-[var(--accent)]">
                          {tool.name}
                        </span>
                        {tool.read_only && <Badge>只读</Badge>}
                        {tool.destructive && <Badge tone="danger">破坏性</Badge>}
                        <span className="min-w-0 flex-1 truncate text-caption text-[var(--text-muted)]">
                          {tool.summary || tool.description}
                        </span>
                        <span className="shrink-0 text-caption tabular-nums text-[var(--text-faint)]">
                          {tool.parameters.length} 参数
                        </span>
                      </button>

                      {open && (
                        <div className="space-y-3 border-t border-white/[0.05] bg-black/20 px-3 py-3">
                          <p className="text-caption leading-relaxed text-[var(--text-muted)]">
                            {tool.description}
                          </p>
                          {tool.parameters.length === 0 ? (
                            <p className="text-caption text-[var(--text-faint)]">这个工具不需要参数。</p>
                          ) : (
                            <table className="w-full text-caption">
                              <thead>
                                <tr className="text-left text-[var(--text-faint)]">
                                  <th className="pb-1 font-normal">参数</th>
                                  <th className="pb-1 font-normal">类型</th>
                                  <th className="pb-1 font-normal">位置</th>
                                  <th className="pb-1 font-normal">说明</th>
                                </tr>
                              </thead>
                              <tbody className="align-top">
                                {tool.parameters.map((param) => (
                                  <tr key={param.name} className="border-t border-white/[0.05]">
                                    <td className="py-1.5 pr-3">
                                      <span className="font-mono text-[var(--text)]">{param.name}</span>
                                      {param.required && (
                                        <span className="ml-1 text-[var(--danger)]" title="必填">*</span>
                                      )}
                                    </td>
                                    <td className="py-1.5 pr-3 font-mono text-[var(--text-muted)]">
                                      {param.type}
                                    </td>
                                    <td className="py-1.5 pr-3 text-[var(--text-faint)]">
                                      {param.location}
                                    </td>
                                    <td className="py-1.5 text-[var(--text-muted)]">
                                      {param.description || "—"}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                          <div className="flex items-center gap-2">
                            <CopyButton
                              text={tool.name}
                              label="复制工具名"
                              className="btn-glass px-2.5 py-1 text-caption"
                            />
                            <CopyButton
                              text={JSON.stringify(
                                {
                                  name: tool.name,
                                  arguments: Object.fromEntries(
                                    tool.parameters
                                      .filter((p) => p.required)
                                      .map((p) => [p.name, `<${p.type}>`]),
                                  ),
                                },
                                null,
                                2,
                              )}
                              label="复制调用样例"
                              className="btn-glass px-2.5 py-1 text-caption"
                            />
                          </div>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        );
      })}
    </div>
  );
}
