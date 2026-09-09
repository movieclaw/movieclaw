"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";
import Link from "next/link";

import { MultiFilterMenu } from "@/components/filter-menu";
import type { Collection } from "@/lib/api/collections";
import { CheckIcon, ChevronDownIcon } from "@/components/icons";
import {
  type FacetValue,
  type LibraryFacets,
  type LibraryFilter,
  type LibraryItemSort,
  type LibraryRelax,
  type WatchFilter,
  getLibraryFacets,
  getLibraryRelax,
  filterCount,
  isFilterEmpty,
} from "@/lib/api/libraries";
import { filterKey, rulesToFilter } from "@/lib/library-filter";
import { useIsMobile } from "@/lib/use-media-query";

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
  collections,
  onSaveAsCollection,
  sortControl,
  className,
}: {
  libraryId: number;
  filter: LibraryFilter;
  onFilterChange: (next: LibraryFilter) => void;
  /** 本库的合集：以 chip 形式排在同一行左侧（合集本来就是存好的筛选） */
  collections?: Collection[];
  /** 「存为合集」——有条件时才给；不给则不显示该入口 */
  onSaveAsCollection?: () => void;
  /** 排序控件本身由调用方渲染（它是墙的偏好，不属于筛选） */
  sortControl?: React.ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  // 「更多筛选」是第二层展开：一级四维已经能答大多数问题，二级是它答不了
  // 的时候才要的。它一打开就要多算十几条 COUNT（tier=all），所以按需请求
  const [more, setMore] = useState(false);
  const [facets, setFacets] = useState<LibraryFacets | null>(null);
  const mobile = useIsMobile();
  const empty = isFilterEmpty(filter);
  const selectedCount = filterCount(filter);

  // 计数跟着当前条件走：每次条件变化都重取，因为"还剩几部"本来就是相对
  // 当前条件而言的。面板没打开且一个条件都没有时不请求——静止态不该
  // 为一个用户还没表达的意图付网络开销
  useEffect(() => {
    if (!open && empty) return;
    let alive = true;
    getLibraryFacets(libraryId, filter, more ? "all" : "primary")
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
  }, [libraryId, filter, open, empty, more]);

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

  /**
   * 当前这面墙正好等于哪个合集——**推导**出来的，不存状态。
   *
   * 用户改动任意一条，键不再相等，标记自然消失；他就从"在看一个合集"变成了
   * "在自己筛"，两种状态之间没有断层，也不需要谁记得去清掉一个 activeCollection。
   */
  const currentKey = filterKey(filter);
  const activeCollectionId =
    currentKey === ""
      ? null
      : ((collections ?? []).find(
          (row) => row.rule_driven && filterKey(rulesToFilter(row.rules)) === currentKey,
        )?.id ?? null);
  const activeCollection = (collections ?? []).find((row) => row.id === activeCollectionId);

  /** 点合集 chip 不是跳页，是**把当前这面墙筛成它**。再点一次就退回全库。 */
  const applyCollection = useCallback(
    (collection: Collection) => {
      onFilterChange(
        collection.id === activeCollectionId ? {} : rulesToFilter(collection.rules),
      );
    },
    [activeCollectionId, onFilterChange],
  );

  return (
    <div className={className}>
      {/* —— 静止态那一行：左侧合集 chip，右侧排序与筛选 —— */}
      <div className="flex flex-wrap items-center gap-2">
        <CollectionChips
          collections={collections ?? []}
          libraryId={libraryId}
          activeId={activeCollectionId}
          onApply={applyCollection}
        />
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
        <div className="scroll-thin mt-2.5 flex flex-wrap items-center gap-2 max-md:flex-nowrap max-md:overflow-x-auto max-md:pb-1">
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
          <button
            type="button"
            aria-expanded={more}
            onClick={() => setMore((v) => !v)}
            className={`glass-row flex h-8 !w-auto shrink-0 items-center gap-1.5 rounded-full !px-3 text-caption ${
              more ? "!bg-[var(--glass-fill-active)] !text-[var(--text)]" : "text-white/70"
            }`}
          >
            更多筛选
          </button>
        </div>
      )}

      {open && more && !mobile && (
        <MoreFiltersPanel facets={facets} filter={filter} onFilterChange={onFilterChange} />
      )}
      {open && more && mobile && (
        <MoreFiltersSheet
          facets={facets}
          filter={filter}
          onFilterChange={onFilterChange}
          onClose={() => setMore(false)}
        />
      )}

      {/* —— 条件行：与面板开合无关，只要有条件就在 —— */}
      {!empty && (
        <ConditionRow
          filter={filter}
          facets={facets}
          onFilterChange={onFilterChange}
          onClear={clearAll}
          collectionName={activeCollection?.name}
          onSaveAsCollection={onSaveAsCollection}
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
  collectionName,
  onSaveAsCollection,
}: {
  filter: LibraryFilter;
  facets: LibraryFacets | null;
  onFilterChange: (next: LibraryFilter) => void;
  onClear: () => void;
  /** 当前条件正好等于这个合集时的合集名；改动任意一条即为 undefined */
  collectionName?: string;
  onSaveAsCollection?: () => void;
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
      {/* 「＝ 合集「X」」：告诉用户这面墙此刻就是那个合集。它是推导出来的，
          用户一改条件就没了——那正是他从"在看合集"切换到"在自己筛"的时刻 */}
      {collectionName && (
        <span className="flex h-7 items-center gap-1 rounded-lg bg-white/[0.10] px-2 text-caption text-white/70">
          ＝ 合集
          <span className="font-semibold text-white">「{collectionName}」</span>
        </span>
      )}
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
      {/* 已经等于某个合集时不再提「存为合集」——那只会存出一个重名的孪生体 */}
      {onSaveAsCollection && !collectionName && (
        <button
          type="button"
          onClick={onSaveAsCollection}
          className="rounded-full px-2.5 py-1 text-caption text-white/50 hover:bg-white/10 hover:text-white"
        >
          存为合集
        </button>
      )}
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

/** 一组固定档位的胶囊。0 的置灰不可点——「永不空货架」的第一道闸。 */
function PillGroup({
  label,
  hint,
  options,
  selected,
  onToggle,
  tone,
}: {
  label: string;
  hint?: string;
  options: readonly FacetValue[];
  selected: readonly string[];
  onToggle: (value: string) => void;
  /** 语义色：库存状态里「要处理的事」和「不用处理的事」得一眼分得开 */
  tone?: Record<string, string>;
}) {
  if (options.length === 0) return null;
  return (
    <div className="mb-3.5">
      <p className="mb-1.5 flex items-baseline gap-1.5 text-caption text-[var(--text-faint)]">
        {label}
        {hint && <span className="text-white/25">{hint}</span>}
      </p>
      <div className="flex flex-wrap gap-1.5">
        {options.map((option) => {
          const on = selected.includes(option.value);
          const dead = option.count === 0 && !on;
          return (
            <button
              key={option.value}
              type="button"
              disabled={dead}
              onClick={() => onToggle(option.value)}
              className={`rounded-full border px-2.5 py-0.5 text-caption transition-colors disabled:pointer-events-none disabled:opacity-30 ${
                on
                  ? "border-white/40 bg-white/[0.14] font-semibold text-white"
                  : `border-white/[0.14] hover:bg-white/[0.08] ${tone?.[option.value] ?? "text-white/65"}`
              }`}
            >
              {option.label}
              <span className="ml-1.5 font-mono text-[10px] tabular-nums text-white/35">
                {option.count}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/**
 * 「更多筛选」双栏面板：左「找片」、右「查库」。
 *
 * 分栏依据是**作用对象不同**：左栏问「作品是什么样的」（来自刮削档案），
 * 右栏问「文件是什么规格」（来自库存台账）。这是两种人格——"周五晚上想看
 * 点什么"和"我那些 4K 都在哪、哪些片缺集"——混在一行 chips 里会让两边都
 * 难用（docs/design/library-filtering.md 铁律 4）。
 *
 * 库存状态给语义色：要处理的事和不用处理的事，扫一眼就分得开。
 */
function MoreFiltersPanel({
  facets,
  filter,
  onFilterChange,
}: {
  facets: LibraryFacets | null;
  filter: LibraryFilter;
  onFilterChange: (next: LibraryFilter) => void;
}) {
  const toggleList = (key: "runtimes" | "languages" | "resolutions" | "stock", value: string) => {
    const current = filter[key] ?? [];
    onFilterChange({
      ...filter,
      [key]: current.includes(value) ? current.filter((v) => v !== value) : [...current, value],
    });
  };
  return (
    <div className="mt-2.5 grid grid-cols-2 gap-x-6 rounded-2xl border border-white/[0.1] bg-black/20 p-4 max-md:grid-cols-1 max-md:gap-y-2">
      <div>
        <p className="mb-3 border-b border-white/[0.08] pb-2 text-sub font-semibold text-white">
          找片
          <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
            作品是什么样的 · 来自刮削档案
          </span>
        </p>
        <PillGroup
          label="评分"
          options={facets?.ratings ?? []}
          selected={filter.ratingGte != null ? [String(filter.ratingGte)] : []}
          onToggle={(v) =>
            onFilterChange({
              ...filter,
              // 评分是阈值不是集合：再点一次同一档就是取消
              ratingGte: filter.ratingGte === Number(v) ? null : Number(v),
            })
          }
        />
        <PillGroup
          label="片长"
          options={facets?.runtimes ?? []}
          selected={filter.runtimes ?? []}
          onToggle={(v) => toggleList("runtimes", v)}
        />
        <PillGroup
          label="原始语言"
          options={facets?.languages ?? []}
          selected={filter.languages ?? []}
          onToggle={(v) => toggleList("languages", v)}
        />
      </div>
      <div className="border-l border-white/[0.08] pl-6 max-md:border-l-0 max-md:border-t max-md:pl-0 max-md:pt-3">
        <p className="mb-3 border-b border-white/[0.08] pb-2 text-sub font-semibold text-white">
          查库
          <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
            文件是什么规格 · 来自库存台账
          </span>
        </p>
        <PillGroup
          label="分辨率"
          options={facets?.resolutions ?? []}
          selected={filter.resolutions ?? []}
          onToggle={(v) => toggleList("resolutions", v)}
        />
        <PillGroup
          label="动态范围"
          options={facets?.hdr ?? []}
          selected={filter.hdr == null ? [] : [filter.hdr ? "1" : "0"]}
          onToggle={(v) => {
            const next = v === "1";
            onFilterChange({ ...filter, hdr: filter.hdr === next ? null : next });
          }}
        />
        <PillGroup
          label="库存状态"
          hint="要处理的事"
          options={facets?.stock ?? []}
          selected={filter.stock ?? []}
          onToggle={(v) => toggleList("stock", v)}
          tone={{ missing: "text-[var(--danger)]", unscraped: "text-[var(--warn)]" }}
        />
      </div>
    </div>
  );
}


/**
 * 合集 chip 行（docs/design/library-filtering.md 4.3 第 1 条）。
 *
 * **不是封面卡横滚**：横滚行装的是内容（一排作品），不是容器。合集本来就是
 * "存好的筛选"，那它就该长得像筛选 chip、和筛选条同属一套系统——点一下不是
 * 跳页，是把当前这面墙筛成它。
 *
 * 装不下就**换行**：换行会暴露数量，横滚只会掩盖数量。
 *
 * 名单驱动的合集是例外：它没有规则，表达不成一组筛选条件，所以那几个 chip
 * 只能跳到详情页。让它假装能筛比诚实地跳页更糟。
 */
function CollectionChips({
  collections,
  libraryId,
  activeId,
  onApply,
}: {
  collections: Collection[];
  libraryId: number;
  activeId: number | null;
  onApply: (collection: Collection) => void;
}) {
  if (collections.length === 0) return null;
  const shown = collections.slice(0, CHIP_LIMIT);
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {shown.map((collection) =>
        collection.rule_driven ? (
          <button
            key={collection.id}
            type="button"
            aria-pressed={collection.id === activeId}
            onClick={() => onApply(collection)}
            className={`flex h-7 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-caption transition ${
              collection.id === activeId
                ? "bg-white/[0.16] text-white"
                : "bg-white/[0.05] text-white/70 hover:bg-white/[0.10] hover:text-white"
            }`}
          >
            {collection.name}
            <span className="font-mono tabular-nums text-white/40">{collection.item_count}</span>
          </button>
        ) : (
          <Link
            key={collection.id}
            href={`/library/${libraryId}/c/${collection.id}` as Route}
            className="flex h-7 shrink-0 items-center gap-1.5 rounded-full bg-white/[0.05] px-2.5 text-caption text-white/70 transition hover:bg-white/[0.10] hover:text-white"
          >
            {collection.name}
            <span className="font-mono tabular-nums text-white/40">{collection.item_count}</span>
          </Link>
        ),
      )}
      {collections.length > shown.length && (
        <Link
          href={`/library/${libraryId}?view=collections` as Route}
          className="flex h-7 shrink-0 items-center rounded-full px-2 text-caption text-white/50 hover:text-white"
        >
          全部合集 ›
        </Link>
      )}
    </div>
  );
}

/** chip 行最多摆几个：再多就该去「合集」视图一次看全，而不是让一行占掉半屏。 */
const CHIP_LIMIT = 8;


/**
 * 「更多筛选」的移动端形态：底部抽屉（docs/design/library-filtering.md 5.2）。
 *
 * **非全屏**是要点：上方留出一截墙，用户看得见条件在实时影响什么。每次勾选
 * 立即生效，底部主按钮上的数字实时跳——不做"确定"式提交。做成"确定"式的话，
 * 用户得先盲选一遍条件再看结果，选错了还要重来；而这里筛选本来就是个来回
 * 试探的过程。
 *
 * 底部那颗 `查看 N 部` 因此不是"提交"，只是"我看完了，把抽屉收起来"。
 */
function MoreFiltersSheet({
  facets,
  filter,
  onFilterChange,
  onClose,
}: {
  facets: LibraryFacets | null;
  filter: LibraryFilter;
  onFilterChange: (next: LibraryFilter) => void;
  onClose: () => void;
}) {
  // Esc / 点上方留白都收起（与全站弹层一致）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (typeof document === "undefined") return null;
  // 不走 Modal：它的遮罩是 bg-black/60 + 模糊，墙会被盖住——那恰好废掉本抽屉
  // 存在的理由。视口的两处硬伤照抄它的解法（globals.css 里的两个变量）：
  // --vp-overshoot 让面板贴住**物理**底边（iOS 独立 App 的视口比屏幕矮一截），
  // --safe-bottom 让最后一颗按钮不压在 Home 指示条上。这里没有输入框，
  // Modal 那套键盘避让用不上
  return createPortal(
    <div className="fixed inset-x-0 top-0 z-50 flex flex-col justify-end [bottom:calc(-1*var(--vp-overshoot))]">
      {/* 上半截只压一层很淡的幕：压黑了就等于全屏，看不见墙在变 */}
      <button
        type="button"
        aria-label="收起更多筛选"
        onClick={onClose}
        className="flex-1 cursor-default bg-black/25"
      />
      <div className="flex max-h-[62dvh] flex-col rounded-t-2xl border-t border-white/10 bg-[rgba(16,18,26,0.92)] shadow-[0_-12px_40px_rgba(0,0,0,0.5)] backdrop-blur-2xl">
        <div className="flex shrink-0 items-center justify-center py-2">
          <span className="h-1 w-9 rounded-full bg-white/25" />
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4">
          <MoreFiltersPanel facets={facets} filter={filter} onFilterChange={onFilterChange} />
        </div>
        <div className="flex shrink-0 items-center gap-2 px-4 pb-[calc(var(--safe-bottom)+var(--vp-overshoot)+12px)] pt-3">
          <button
            type="button"
            onClick={() => onFilterChange({})}
            className="h-10 shrink-0 rounded-full px-4 text-ui text-white/60 hover:bg-white/10"
          >
            清空
          </button>
          {/* 不是「确定」：条件早就生效了，这颗只是把抽屉收起来看结果 */}
          <button
            type="button"
            onClick={onClose}
            className="btn-glass h-10 flex-1 text-ui font-medium"
          >
            {facets ? `查看 ${facets.total} 部` : "查看结果"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
