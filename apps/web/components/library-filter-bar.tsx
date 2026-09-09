"use client";

import { useCallback, useEffect, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { MultiFilterMenu } from "@/components/filter-menu";
import { CheckIcon, ChevronDownIcon } from "@/components/icons";
import {
  type LibraryFacets,
  type LibraryFilter,
  type LibraryItemSort,
  type LibraryRelax,
  type WatchFilter,
  getLibraryFacets,
  getLibraryRelax,
  isFilterEmpty,
} from "@/lib/api/libraries";

/**
 * 单库墙的筛选条（docs/design/library-filtering.md 5.1）。
 *
 * 静止态只留信息，不留控件——四个都写着「全部」的下拉不承载任何信息，
 * 它们只是把「什么都没选」这件事说了四遍。所以同一块地方有三种状态，
 * 永远只占一行的量：
 *
 *   静止态：排序（显值、无边框）+ 筛选（唯一一个带框的按钮）
 *   点开后：多出一行四个维度下拉，用完可收回
 *   有条件：条件本身顶替控件出现，且**与面板开合无关**——收起面板不影响
 *           条件可见，否则用户会以为收起就等于取消了筛选
 *
 * 已选条件把「或」和「且」画出来：同一维度的多个值用「或」连在一个容器里，
 * 维度之间用「且」隔开。多选语义是这类筛选器最大的困惑源，画出来就没人会问。
 */
export function LibraryFilterBar({
  libraryId,
  filter,
  onFilterChange,
  sortControl,
  className,
}: {
  libraryId: number;
  filter: LibraryFilter;
  onFilterChange: (next: LibraryFilter) => void;
  /** 排序控件本身由调用方渲染（它是墙的偏好，不属于筛选） */
  sortControl?: React.ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [facets, setFacets] = useState<LibraryFacets | null>(null);
  const empty = isFilterEmpty(filter);
  const selectedCount =
    (filter.genres?.length ?? 0) +
    (filter.countries?.length ?? 0) +
    (filter.decades?.length ?? 0) +
    (filter.watch ? 1 : 0);

  // 计数跟着当前条件走：每次条件变化都重取，因为"还剩几部"本来就是相对
  // 当前条件而言的。面板没打开且一个条件都没有时不请求——静止态不该
  // 为一个用户还没表达的意图付网络开销
  useEffect(() => {
    if (!open && empty) return;
    let alive = true;
    getLibraryFacets(libraryId, filter)
      .then((data) => {
        if (alive) setFacets(data);
      })
      .catch(() => {
        // 计数拿不到就不显示数字（下拉仍可用）：数字缺席比数字错了好
        if (alive) setFacets(null);
      });
    return () => {
      alive = false;
    };
  }, [libraryId, filter, open, empty]);

  const toggle = useCallback(
    (dim: "genres" | "countries" | "decades", value: string) => {
      const current = (filter[dim] ?? []) as (string | number)[];
      const typed = dim === "genres" ? Number(value) : value;
      const next = current.includes(typed)
        ? current.filter((v) => v !== typed)
        : [...current, typed];
      onFilterChange({ ...filter, [dim]: next });
    },
    [filter, onFilterChange],
  );

  const toggleWatch = useCallback(
    (value: string) => {
      // 观看状态是单选：再点一次同一个值就是取消
      onFilterChange({
        ...filter,
        watch: filter.watch === value ? null : (value as WatchFilter),
      });
    },
    [filter, onFilterChange],
  );

  const clearAll = useCallback(() => onFilterChange({}), [onFilterChange]);

  return (
    <div className={className}>
      {/* —— 静止态那一行 —— */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {sortControl}
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className={`glass-row flex h-8 !w-auto shrink-0 items-center gap-1.5 rounded-full !px-3 text-caption ${
              open || !empty ? "!bg-[var(--glass-fill-active)] !text-[var(--text)]" : "text-white/70"
            }`}
          >
            筛选
            {selectedCount > 0 && (
              <span className="font-mono text-caption font-semibold tabular-nums text-white/85">
                {selectedCount}
              </span>
            )}
          </button>
        </div>
      </div>

      {/* —— 点开才有的四个维度 —— */}
      {open && (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <MultiFilterMenu
            label="类型"
            hint="可多选 · 维度内是「或」"
            selected={(filter.genres ?? []).map(String)}
            options={facets?.genres ?? []}
            onToggle={(v) => toggle("genres", v)}
          />
          <MultiFilterMenu
            label="年代"
            hint="按时间排下来，底条就是分布"
            selected={filter.decades ?? []}
            options={facets?.decades ?? []}
            onToggle={(v) => toggle("decades", v)}
          />
          <MultiFilterMenu
            label="地区"
            hint="可多选 · 维度内是「或」"
            selected={filter.countries ?? []}
            options={facets?.countries ?? []}
            onToggle={(v) => toggle("countries", v)}
          />
          <MultiFilterMenu
            label="观看"
            hint="单选"
            selected={filter.watch ? [filter.watch] : []}
            options={facets?.watch ?? []}
            onToggle={toggleWatch}
          />
        </div>
      )}

      {/* —— 条件行：与面板开合无关，只要有条件就在 —— */}
      {!empty && (
        <ConditionRow
          filter={filter}
          facets={facets}
          onFilterChange={onFilterChange}
          onClear={clearAll}
        />
      )}
    </div>
  );
}

/** 维度名 → 该维度在条件行里的显示顺序与标签。 */
const DIMS = [
  { key: "genres", label: "类型" },
  { key: "decades", label: "年代" },
  { key: "countries", label: "地区" },
  { key: "watch", label: "观看" },
] as const;

function ConditionRow({
  filter,
  facets,
  onFilterChange,
  onClear,
}: {
  filter: LibraryFilter;
  facets: LibraryFacets | null;
  onFilterChange: (next: LibraryFilter) => void;
  onClear: () => void;
}) {
  /** 取值 → 展示名：优先用 facet 带回来的 label（类型/地区靠它翻中文名）。 */
  const labelOf = (dim: string, value: string): string => {
    const pool =
      dim === "genres"
        ? facets?.genres
        : dim === "countries"
          ? facets?.countries
          : dim === "decades"
            ? facets?.decades
            : facets?.watch;
    return pool?.find((row) => row.value === value)?.label ?? value;
  };

  const drop = (dim: string, value: string) => {
    if (dim === "watch") {
      onFilterChange({ ...filter, watch: null });
      return;
    }
    const key = dim as "genres" | "countries" | "decades";
    const current = (filter[key] ?? []) as (string | number)[];
    const typed = dim === "genres" ? Number(value) : value;
    onFilterChange({ ...filter, [key]: current.filter((v) => v !== typed) });
  };

  const groups = DIMS.map(({ key, label }) => {
    const values =
      key === "watch"
        ? filter.watch
          ? [filter.watch]
          : []
        : ((filter[key] ?? []) as (string | number)[]).map(String);
    return { key, label, values };
  }).filter((group) => group.values.length > 0);

  return (
    <div className="mt-2.5 flex flex-wrap items-center gap-2">
      {groups.map((group, index) => (
        <div key={group.key} className="flex items-center gap-2">
          {/* 维度之间是「且」——语义画出来，不写在脚注里 */}
          {index > 0 && <span className="text-caption tracking-wide text-white/30">且</span>}
          <span className="glass-row flex h-7 !w-auto items-center gap-1.5 rounded-lg !bg-[var(--glass-fill-active)] !px-2 py-0">
            <span className="rounded bg-black/25 px-1.5 py-0.5 text-caption text-white/40">
              {group.label}
            </span>
            {group.values.map((value, i) => (
              <span key={value} className="flex items-center gap-1.5">
                {/* 同一维度内的多个值是「或」 */}
                {i > 0 && <span className="text-caption text-white/30">或</span>}
                <span className="flex items-center gap-0.5 text-caption font-semibold text-white">
                  {labelOf(group.key, value)}
                  <button
                    type="button"
                    aria-label={`取消 ${group.label} ${labelOf(group.key, value)}`}
                    onClick={() => drop(group.key, value)}
                    className="rounded-full px-1 text-white/40 hover:bg-white/15 hover:text-white"
                  >
                    ✕
                  </button>
                </span>
              </span>
            ))}
          </span>
        </div>
      ))}
      <button
        type="button"
        onClick={onClear}
        className="rounded-full px-2.5 py-1 text-caption text-white/50 hover:bg-white/10 hover:text-white"
      >
        清空
      </button>
      {facets && (
        <span className="ml-auto text-caption tabular-nums text-white/60">
          筛出 <span className="font-mono font-semibold text-white">{facets.total}</span> 部
        </span>
      )}
    </div>
  );
}

/** 排序档位 → 展示值。默认档在不同墙上叫法不同，由调用方传入。 */
export const SORT_LABELS: Record<LibraryItemSort, string> = {
  title: "按标题",
  added_at: "最近添加",
  release_date: "按时间",
  probing: "待补探优先",
  rating: "按评分",
  runtime: "按片长",
  size: "按体积",
  last_played: "最近观看",
};

/**
 * 墙的排序控件——**显值、无边框**的文字下拉。
 *
 * 为什么它长这样，而「筛选」是唯一一个带框的按钮：
 * **能从内容本身看出来的状态，控件可以无字；看不出来的，控件必须把当前值
 * 显示出来。** 海报墙和瀑布流长得完全不同，看一眼墙就知道自己在哪个形态，
 * 所以形态键只要一个图标；但「按什么排」看墙是看不出来的（「按标题」和
 * 「最近添加」在一屏之内都只是"某种顺序"），所以排序必须把当前值挂在外面。
 *
 * 这也是它从 ⋯ 菜单里被提出来的理由：埋在菜单里，用户根本不知道自己
 * 正按什么排（docs/design/library-filtering.md 5.1.1）。
 */
export function WallSortControl<T extends string>({
  value,
  options,
  onChange,
  disabled,
}: {
  value: T;
  options: readonly (readonly [T, string])[];
  onChange: (next: T) => void;
  /** 扫描补探那几分钟排序被临时接管：如实置灰，不给按了没反应的控件 */
  disabled?: boolean;
}) {
  const current = options.find(([key]) => key === value)?.[1] ?? value;
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label="排序"
          className="flex h-8 shrink-0 items-center gap-1 rounded-full px-2.5 text-caption text-white/55 hover:bg-white/[0.08] hover:text-white disabled:pointer-events-none disabled:opacity-40 data-[state=open]:bg-white/[0.12] data-[state=open]:text-white"
        >
          <span className="font-semibold text-white/85">{current}</span>
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
          <DropdownMenu.Label className="px-3 pb-1 pt-1.5 text-caption text-[var(--text-faint)]">
            索引条跟着换口径
          </DropdownMenu.Label>
          <DropdownMenu.RadioGroup value={value} onValueChange={(next) => onChange(next as T)}>
            {options.map(([key, label]) => (
              <DropdownMenu.RadioItem
                key={key}
                value={key}
                className="glass-row nav-item flex cursor-pointer items-center justify-between px-3 py-2 text-sub outline-none data-[highlighted]:!bg-[var(--glass-fill-hover)]"
              >
                {label}
                <DropdownMenu.ItemIndicator>
                  <CheckIcon className="size-3.5 text-[var(--info)]" />
                </DropdownMenu.ItemIndicator>
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/**
 * 筛空之后的出路（铁律 2：永不空货架）。
 *
 * 不渲染空墙——空墙什么也没说，用户只能自己一条条试。这里直接告诉他
 * 「放宽哪一条能救回多少部」，一点就生效。服务端**只返回救得回内容的条件**，
 * 所以这里不需要再过滤：拿到空表就说明这几条两两之间就没有交集，
 * 那时只留「清空全部条件」。
 */
export function FilterEmptyState({
  libraryId,
  filter,
  onFilterChange,
}: {
  libraryId: number;
  filter: LibraryFilter;
  onFilterChange: (next: LibraryFilter) => void;
}) {
  const [relax, setRelax] = useState<LibraryRelax | null>(null);

  useEffect(() => {
    let alive = true;
    getLibraryRelax(libraryId, filter)
      .then((data) => alive && setRelax(data))
      .catch(() => alive && setRelax(null));
    return () => {
      alive = false;
    };
  }, [libraryId, filter]);

  const count =
    (filter.genres?.length ?? 0) +
    (filter.countries?.length ?? 0) +
    (filter.decades?.length ?? 0) +
    (filter.watch ? 1 : 0);

  const drop = (dim: string, value: string) => {
    if (dim === "watch") {
      onFilterChange({ ...filter, watch: null });
      return;
    }
    const key = dim as "genres" | "countries" | "decades";
    const current = (filter[key] ?? []) as (string | number)[];
    const typed = dim === "genres" ? Number(value) : value;
    onFilterChange({ ...filter, [key]: current.filter((v) => v !== typed) });
  };

  const suggestions = relax?.suggestions ?? [];
  return (
    <div className="mx-6 mt-8 max-w-[34rem] rounded-2xl border border-dashed border-white/[0.14] p-6 max-md:mx-4 max-md:mt-6 max-md:p-4">
      <h3 className="text-body-lg font-semibold text-white">
        没有同时满足这 {count} 个条件的作品
      </h3>
      <p className="mt-1 text-sub text-[var(--text-muted)]">
        {suggestions.length > 0
          ? "放宽一条就能找回内容。"
          : "这几个条件两两之间就没有交集，去掉任意一条也救不回来。"}
      </p>
      <div className="mt-4 flex flex-col gap-1.5">
        {suggestions.map((row) => (
          <button
            key={`${row.dim}:${row.value}`}
            type="button"
            onClick={() => drop(row.dim, row.value)}
            className="glass-row flex items-center gap-3 rounded-xl px-3 py-2 text-left text-sub text-white/80 hover:!bg-[var(--glass-fill-hover)] hover:text-white"
          >
            <span className="min-w-0 flex-1">
              去掉「
              <span className="font-semibold text-white">
                {row.dim_label} = {row.label}
              </span>
              」
            </span>
            <span className="shrink-0 font-mono text-caption tabular-nums text-[var(--info)]">
              → {row.count} 部
            </span>
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={() => onFilterChange({})}
        className="mt-3 rounded-full px-3 py-1.5 text-caption text-white/50 hover:bg-white/10 hover:text-white"
      >
        清空全部条件
      </button>
    </div>
  );
}
