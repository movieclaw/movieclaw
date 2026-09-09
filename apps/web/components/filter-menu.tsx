"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { CheckIcon, ChevronDownIcon } from "@/components/icons";

export interface FilterMenuOption<T extends string | number> {
  value: T;
  label: string;
  /** 选项下方的一句说明（可选） */
  hint?: string;
}

/**
 * 工具栏里的筛选下拉：「标签 · 当前值 ▾」。
 *
 * 与卡片右上角的操作菜单（TaskActionsMenu）同一套 Radix 菜单与 menu-surface
 * 皮肤，只是触发器带标签、菜单项带选中态。筛选条件都站在内容之上的同一行，
 * 用户一眼能看到当前看的是哪一片、按什么口径——把它们挂在某个分区标题上会
 * 让人以为只管那一段。
 */
export function FilterMenu<T extends string | number>({
  label,
  value,
  options,
  onChange,
  ariaLabel,
}: {
  label: string;
  value: T;
  options: readonly FilterMenuOption<T>[];
  onChange: (value: T) => void;
  ariaLabel?: string;
}) {
  const current = options.find((option) => option.value === value) ?? options[0];
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={ariaLabel ?? label}
          className="glass-row flex h-8 !w-auto shrink-0 items-center gap-1.5 rounded-full !px-3 text-caption text-white/70 data-[state=open]:!bg-[var(--glass-fill-active)] data-[state=open]:!text-[var(--text)]"
        >
          <span className="text-white/40">{label}</span>
          <span className="font-semibold text-white/85">{current?.label}</span>
          <ChevronDownIcon className="size-3 text-white/40" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[11rem] p-1"
        >
          {options.map((option) => (
            <DropdownMenu.Item
              key={String(option.value)}
              onSelect={() => onChange(option.value)}
              className="glass-row nav-item flex cursor-pointer items-start gap-2 px-3 py-2 text-sub outline-none data-[highlighted]:!bg-[var(--glass-fill-hover)]"
            >
              <span className="flex size-4 shrink-0 items-center justify-center pt-0.5">
                {option.value === value && <CheckIcon className="size-3.5 text-[var(--info)]" />}
              </span>
              <span className="min-w-0">
                <span
                  className={`block font-medium ${
                    option.value === value ? "text-white" : "text-white/75"
                  }`}
                >
                  {option.label}
                </span>
                {option.hint && (
                  <span className="block text-caption leading-4 text-white/40">{option.hint}</span>
                )}
              </span>
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/** 多选筛选菜单里的一个候选值。 */
export interface MultiFilterOption {
  value: string;
  label: string;
  /** 在**其他维度**已选条件下勾上本值还剩几部；0 置灰不可点 */
  count: number;
}

/**
 * 多选筛选下拉：在 FilterMenu 的基础上只扩两处——**点击不关闭**、
 * 选项行带计数与一条与数量成正比的**底条**。
 *
 * 底条贴在行底、只有 2px：满高色块会被读成"这一行被选中了"，和选中态抢
 * 同一个视觉通道。同一个机制用在所有维度上，年代档按序排下来自然就是
 * 一张库存分布图——你是从形状里挑，不是从一列文字里挑
 * （docs/design/library-filtering.md 3.3）。
 *
 * 计数为 0 的候选值**照常渲染**（置灰不可点），不是隐藏：选项凭空消失比
 * 置灰更让人困惑，这也是"永不空货架"的第一道闸。
 */
export function MultiFilterMenu({
  label,
  selected,
  options,
  onToggle,
  hint,
}: {
  label: string;
  selected: readonly string[];
  options: readonly MultiFilterOption[];
  onToggle: (value: string) => void;
  /** 菜单顶部的一句说明（"可多选 · 维度内是「或」"之类） */
  hint?: string;
}) {
  const current =
    selected.length === 0
      ? "全部"
      : selected.length === 1
        ? (options.find((o) => o.value === selected[0])?.label ?? selected[0])
        : `${options.find((o) => o.value === selected[0])?.label ?? selected[0]} +${selected.length - 1}`;
  const max = Math.max(1, ...options.map((o) => o.count));
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={label}
          className={`glass-row flex h-8 !w-auto shrink-0 items-center gap-1.5 rounded-full !px-3 text-caption data-[state=open]:!bg-[var(--glass-fill-active)] data-[state=open]:!text-[var(--text)] ${
            selected.length > 0 ? "!text-[var(--text)]" : "text-white/70"
          }`}
        >
          <span className="text-white/40">{label}</span>
          <span className="font-semibold text-white/85">{current}</span>
          <ChevronDownIcon className="size-3 text-white/40" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 max-h-[20rem] min-w-[14rem] overflow-auto p-1"
        >
          {hint && (
            <DropdownMenu.Label className="px-3 pb-1 pt-1.5 text-caption text-[var(--text-faint)]">
              {hint}
              {selected.length > 0 && ` · 已选 ${selected.length}`}
            </DropdownMenu.Label>
          )}
          {options.map((option) => {
            const on = selected.includes(option.value);
            const dead = option.count === 0 && !on;
            return (
              <DropdownMenu.Item
                key={option.value}
                disabled={dead}
                // 多选：点一下切一个值，菜单不关——关了就得重新点开才能勾第二个
                onSelect={(event) => {
                  event.preventDefault();
                  if (!dead) onToggle(option.value);
                }}
                className={`glass-row nav-item relative flex cursor-pointer items-center gap-2 overflow-hidden px-3 py-2 text-sub outline-none data-[highlighted]:!bg-[var(--glass-fill-hover)] ${
                  dead ? "pointer-events-none opacity-30" : ""
                }`}
              >
                {/* 与数量成正比的底条：数字要读，形状可以扫 */}
                <span
                  aria-hidden
                  className="absolute bottom-[3px] left-3 h-[2px] rounded-full"
                  style={{
                    width: `calc(${Math.round((option.count / max) * 100)}% - 1.5rem)`,
                    background: on ? "var(--info)" : "rgba(255,255,255,.28)",
                  }}
                />
                <span className="flex size-4 shrink-0 items-center justify-center">
                  {on && <CheckIcon className="size-3.5 text-[var(--info)]" />}
                </span>
                <span className={`min-w-0 flex-1 ${on ? "font-medium text-white" : "text-white/75"}`}>
                  {option.label}
                </span>
                <span className="shrink-0 font-mono text-caption tabular-nums text-white/40">
                  {option.count}
                </span>
              </DropdownMenu.Item>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
