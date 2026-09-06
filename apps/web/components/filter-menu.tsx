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
