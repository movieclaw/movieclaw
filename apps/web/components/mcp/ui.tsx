"use client";

/**
 * MCP 分区的公共小件。
 *
 * 这一页面向的是「要把端点接进 Claude Code 的开发者」，不是来改偏好设置的人。
 * 所以基调按开发者控制台来定，三条贯穿全页：
 *
 * 1. **标识符一律等宽字体，且永远配一个复制按钮**——地址、令牌、工具名、参数名
 *    都是要粘到别处去的东西，不是给人读的散文；
 * 2. **信息密度优先于留白**：列表是表格不是卡片墙，一屏能扫完比好看重要；
 * 3. **破坏性操作与日常操作分层**：日常操作在行内，破坏性的沉到详情页底部的
 *    危险区，且要打字确认。
 */

import { CopyButton } from "@/components/copy-button";

/** 状态点：绿=在跑，灰=停用。比一个「已停用」文字标签更省横向空间，也更好扫。 */
export function StatusDot({ on, title }: { on: boolean; title: string }) {
  return (
    <span
      title={title}
      aria-label={title}
      className={`inline-block size-1.5 shrink-0 rounded-full ${
        on ? "bg-[var(--ok,#5fd39b)]" : "bg-white/25"
      }`}
    />
  );
}

export function Badge({
  children,
  tone = "muted",
}: {
  children: React.ReactNode;
  tone?: "muted" | "accent" | "danger";
}) {
  const tones = {
    muted: "border-white/[0.12] text-[var(--text-muted)]",
    accent: "border-[var(--accent)]/40 text-[var(--accent)]",
    danger: "border-[var(--danger)]/40 text-[var(--danger)]",
  };
  return (
    <span
      className={`shrink-0 rounded border px-1.5 py-px font-mono text-[11px] leading-[18px] ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export function Switch({
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

/**
 * 只读的标识符字段：等宽、可选中、右侧常驻复制按钮。
 * 端点地址、令牌、命令片段全用它——开发者对这类字段的唯一诉求就是「原样拿走」。
 */
export function CopyField({
  value,
  label,
  mono = true,
  className = "",
}: {
  value: string;
  label: string;
  mono?: boolean;
  className?: string;
}) {
  return (
    <div
      className={`field-shell flex items-center gap-2 rounded-lg border border-white/[0.08] bg-black/25 py-1.5 pl-3 pr-1.5 ${className}`}
    >
      <span
        className={`min-w-0 flex-1 select-all truncate text-sub ${mono ? "font-mono" : ""}`}
        title={value}
      >
        {value}
      </span>
      <CopyButton text={value} label={label} className="btn-glass shrink-0 px-2.5 py-1 text-caption" />
    </div>
  );
}

/** 带语言标签与复制按钮的代码块。接入指引里的每段命令都用它。 */
export function CodeBlock({ code, lang }: { code: string; lang: string }) {
  return (
    <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-black/35">
      <div className="flex items-center justify-between border-b border-white/[0.06] px-3 py-1.5">
        <span className="font-mono text-[11px] uppercase tracking-wider text-[var(--text-faint)]">
          {lang}
        </span>
        <CopyButton text={code} label="复制" className="btn-glass px-2.5 py-1 text-caption" />
      </div>
      <pre className="scroll-thin overflow-x-auto px-3 py-2.5 font-mono text-caption leading-relaxed text-[var(--text)]">
        {code}
      </pre>
    </div>
  );
}

/** 键值行：详情页「概览」里成对出现的元信息。左标签定宽，右值可长可短。 */
export function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-3 py-1.5">
      <span className="w-20 shrink-0 text-caption text-[var(--text-faint)]">{label}</span>
      <span className="min-w-0 flex-1 text-sub">{children}</span>
    </div>
  );
}

export const INPUT_CLASS =
  "w-full rounded-lg border border-white/[0.08] bg-black/25 px-3 py-2 text-sub " +
  "text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-faint)] " +
  "focus:border-[var(--accent)]/50";

export function formatBytes(bytes: number): string {
  return bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`;
}
