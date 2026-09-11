"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";
import Link from "next/link";

import { MultiFilterMenu } from "@/components/filter-menu";
import type { Collection } from "@/lib/api/collections";
import { ArrowDownIcon, CheckIcon, ChevronDownIcon, FilterIcon } from "@/components/icons";
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
 *   点开后：宽屏多出一行四个维度下拉，用完可收回；窄屏直接拉起底部抽屉，
 *           一二级维度全部平铺成胶囊（见 FilterSheet）
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
  // 二级维度的展示名只在 tier=all 里才有（"gt120" → "> 120′"）。所以只要**条件里
  // 已经有二级维度**就得取全份，不能只在面板展开时取：分享进来的链接、点开的
  // 合集都可能带着二级条件，那时面板是关的，条件行只好把裸值印出来
  // 窄屏的筛选抽屉一打开就把一二级维度都摆出来，所以直接取全份
  const needsAllFacets = more || (mobile && open) || hasSecondary(filter);
  // 全份 facet 到了没有。没到就别把「找片 / 查库」两个小标题孤零零地摆出来——
  // 面板会先是个空壳、随后内容弹进来把它撑高，在移动端就是拇指底下抖一下
  const secondaryReady = Boolean(
    facets && (facets.ratings.length > 0 || facets.runtimes.length > 0),
  );

  useEffect(() => {
    if (!open && empty) return;
    let alive = true;
    getLibraryFacets(libraryId, filter, needsAllFacets ? "all" : "primary")
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
  }, [libraryId, filter, open, empty, needsAllFacets]);

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
      {/* —— 静止态那一行：筛选 · 排序 · 合集 chip，**一律靠左**，排不下换行 ——
          曾经是「左侧合集 chip、右侧排序与筛选」。库里没有自建合集时（系列合集不进
          chip 行）左边整块空着，两个控件孤零零挂在右边，像飘在海报上方。页面上
          库名、库信息都是左对齐的，控件跟着从左边起，读起来是一条线往下走。
          筛选在前：它是这一行唯一带框的按钮，也是最常用的那个 */}
      <div className="flex flex-wrap items-center gap-2">
        {/* 静止态也要看得出是个按钮：glass-row 默认透明无框，「筛选」此前只是一行
            70% 的字，比旁边显值的排序还弱，主次倒挂。补一圈极淡描边 + 最浅一档玻璃底
            + 图标，文字提到 90% 中等字重——认得出，但不跳。有条件 / 展开时仍是高亮底 */}
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className={`glass-row flex h-8 !w-auto shrink-0 items-center gap-1.5 rounded-full !px-3 text-caption font-medium ${
            open || !empty
              ? "!border-white/[0.16] !bg-[var(--glass-fill-active)] !text-[var(--text)]"
              : "!border-white/[0.12] !bg-[var(--glass-fill)] text-white/90 hover:!bg-[var(--glass-fill-hover)]"
          }`}
        >
          <FilterIcon className="size-3.5 opacity-70" />
          筛选
          {selectedCount > 0 && (
            <span className="font-mono text-caption font-semibold tabular-nums text-white/85">
              {selectedCount}
            </span>
          )}
        </button>
        {sortControl}
        <CollectionChips
          collections={collections ?? []}
          libraryId={libraryId}
          activeId={activeCollectionId}
          onApply={applyCollection}
        />
      </div>

      {/* —— 点开才有的四个维度（宽屏） ——
          窄屏不走这里，点「筛选」直接拉起 FilterSheet：一排下拉在窄屏怎么摆都别扭——
          横滚会在滑动中误触弹出，两列铺开又占掉半屏、还得再点一层才看得到取值 */}
      {open && !mobile && (
        <div className="mt-2.5 flex items-center gap-2">
        <div className="flex flex-1 flex-wrap items-center gap-2">
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
        <MoreFiltersPanel
          facets={facets}
          loading={!secondaryReady}
          filter={filter}
          onFilterChange={onFilterChange}
        />
      )}
      {open && mobile && (
        <FilterSheet
          facets={facets}
          loading={!secondaryReady}
          filter={filter}
          onFilterChange={onFilterChange}
          onToggle={toggle}
          onToggleWatch={toggleWatch}
          onClose={() => setOpen(false)}
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
/**
 * 条件行里的维度顺序与叫法。
 *
 * **十个维度一个都不能少。** 条件行的职责是「收起面板之后，用户仍然看得见
 * 自己筛了什么」；只画一级四维的话，用 4K 筛完再收起面板，界面上只剩一个
 * 「筛选 1」的角标和「筛出 8 部」——筛的是什么、怎么取消，都无从得知。
 */
const DIMS = [
  { key: "genres", label: "类型" },
  { key: "decades", label: "年代" },
  { key: "countries", label: "地区" },
  { key: "watch", label: "观看" },
  { key: "ratingGte", label: "评分" },
  { key: "runtimes", label: "片长" },
  { key: "languages", label: "语言" },
  { key: "resolutions", label: "画质" },
  { key: "hdr", label: "动态范围" },
  { key: "stock", label: "库存" },
] as const;

/**
 * 取值本身就是人话的维度：facet 里它们的 label 等于 value（"2160p"、"ja"）。
 * 这类维度在展示名还没到的时候直接印取值就行，不必等。
 */
const SELF_LABELLING = new Set(["resolutions", "languages"]);

/**
 * 展示名查不到时印什么。
 *
 * **不能印裸值。** 类型存的是 TMDB id、地区存的是国家码、片长存的是档位键，
 * 界面上冒出「878」「JP」「gt120」比空着更糟——用户不知道那是什么，也就无法
 * 判断自己筛的对不对。查不到只有一种情况：展示名还在路上（服务端保证选中的
 * 取值一定在 facet 里），所以给个省略号占位，到了自然就补上。
 */
function labelFallback(dim: string, value: string): string {
  return SELF_LABELLING.has(dim) ? value : "…";
}

/** 条件里有没有二级维度（决定要不要取全份 facet：二级的展示名只在全份里）。 */
function hasSecondary(filter: LibraryFilter): boolean {
  return Boolean(
    filter.ratingGte != null ||
      filter.runtimes?.length ||
      filter.languages?.length ||
      filter.resolutions?.length ||
      filter.hdr != null ||
      filter.stock?.length,
  );
}

/** 多值维度（单值的 watch / ratingGte / hdr 走清成 null 那条路）。 */
type MultiDim = "genres" | "countries" | "decades" | "runtimes" | "languages" | "resolutions" | "stock";

/** 维度 → 服务端 facet 里对应的候选表（取展示名用）。 */
function poolOf(key: string, facets: LibraryFacets | null): FacetValue[] | undefined {
  switch (key) {
    case "genres":
      return facets?.genres;
    case "countries":
      return facets?.countries;
    case "decades":
      return facets?.decades;
    case "watch":
      return facets?.watch;
    case "ratingGte":
      return facets?.ratings;
    case "runtimes":
      return facets?.runtimes;
    case "languages":
      return facets?.languages;
    case "resolutions":
      return facets?.resolutions;
    case "hdr":
      return facets?.hdr;
    case "stock":
      return facets?.stock;
    default:
      return undefined;
  }
}

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
  /** 取值 → 展示名。查不到就给省略号，**绝不把裸值印出来**（见 labelFallback）。 */
  const labelOf = (dim: string, value: string): string =>
    poolOf(dim, facets)?.find((row) => row.value === value)?.label ?? labelFallback(dim, value);

  /** 摘掉一个取值。单值维度清成 null，多值维度只去掉这一个。 */
  const drop = (dim: string, value: string) => {
    if (dim === "watch" || dim === "ratingGte" || dim === "hdr") {
      onFilterChange({ ...filter, [dim]: null });
      return;
    }
    const key = dim as MultiDim;
    const current = (filter[key] ?? []) as (string | number)[];
    const typed = dim === "genres" ? Number(value) : value;
    onFilterChange({ ...filter, [key]: current.filter((v) => v !== typed) });
  };

  const groups = DIMS.map(({ key, label }) => {
    let values: string[] = [];
    if (key === "watch") {
      values = filter.watch ? [filter.watch] : [];
    } else if (key === "ratingGte") {
      values = filter.ratingGte == null ? [] : [String(filter.ratingGte)];
    } else if (key === "hdr") {
      // facet 里 HDR/SDR 的取值是 "1"/"0"，与筛选条件的布尔值对上
      values = filter.hdr == null ? [] : [filter.hdr ? "1" : "0"];
    } else {
      values = ((filter[key] ?? []) as (string | number)[]).map(String);
    }
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
  direction,
}: {
  value: T;
  options: readonly (readonly [T, string])[];
  onChange: (next: T) => void;
  /** 扫描补探那几分钟排序被临时接管：如实置灰，不给按了没反应的控件 */
  disabled?: boolean;
  /**
   * 正倒序（不给就不显示方向、也不能切）。按钮上一枚箭头说方向，菜单里当前档
   * 后面用人话写（「短→长」）——光看 ↑ 要想一下它是"从小到大"还是"大的在上"。
   * 再点一次当前档就反转，菜单不关，看得见方向在变
   */
  direction?: {
    ascending: boolean;
    label: string;
    onToggle: () => void;
  };
}) {
  // 当前档不在这组选项里（图廊只有两档，偏好却是"按评分"）时显示第一档，不把键名裸露出来
  const current = options.find(([key]) => key === value)?.[1] ?? options[0]?.[1] ?? value;
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label="排序"
          title={direction ? `${current} · ${direction.label}` : undefined}
          className="flex h-8 shrink-0 items-center gap-1 rounded-full px-2.5 text-caption text-white/55 hover:bg-white/[0.08] hover:text-white disabled:pointer-events-none disabled:opacity-40 data-[state=open]:bg-white/[0.12] data-[state=open]:text-white"
        >
          <span className="font-semibold text-white/85">{current}</span>
          {direction && (
            <ArrowDownIcon
              className={`size-3 text-white/60 transition-transform ${direction.ascending ? "rotate-180" : ""}`}
            />
          )}
          <ChevronDownIcon className="size-3 text-white/40" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        {/* 靠左弹出：排序控件现在排在工具行左侧（筛选之后），右对齐会整片悬到按钮左边外 */}
        <DropdownMenu.Content
          align="start"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[12rem] p-1"
        >
          {/* 只在真的换了档时才 onChange：Radix 的 RadioItem 每次选中都会调 onValueChange，
              **点已选中的那一档也照调**（onSelect 与它串联且 checkForDefaultPrevented: false）。
              不拦的话「再点一次反转」刚翻过来，就被同值的 onChange 把方向重置回去 */}
          <DropdownMenu.RadioGroup
            value={value}
            onValueChange={(next) => {
              if (next !== value) onChange(next as T);
            }}
          >
            {options.map(([key, label]) => {
              const selected = key === value;
              return (
                <DropdownMenu.RadioItem
                  key={key}
                  value={key}
                  // 再点一次当前档 = 反转方向（同值的 onValueChange 已在 RadioGroup 上拦掉）；
                  // preventDefault 让菜单留着，方向文案当场翻过来
                  onSelect={(event) => {
                    if (!selected || !direction) return;
                    event.preventDefault();
                    direction.onToggle();
                  }}
                  aria-label={
                    selected && direction ? `${label}，${direction.label}，再点一次反转顺序` : undefined
                  }
                  className="glass-row nav-item flex cursor-pointer items-center justify-between gap-3 px-3 py-2 text-sub outline-none data-[highlighted]:!bg-[var(--glass-fill-hover)]"
                >
                  {label}
                  <span className="flex items-center gap-1.5 text-caption text-[var(--info)]">
                    {selected && direction && (
                      <>
                        {direction.label}
                        <ArrowDownIcon
                          className={`size-3 ${direction.ascending ? "rotate-180" : ""}`}
                        />
                      </>
                    )}
                    <DropdownMenu.ItemIndicator>
                      <CheckIcon className="size-3.5 text-[var(--info)]" />
                    </DropdownMenu.ItemIndicator>
                  </span>
                </DropdownMenu.RadioItem>
              );
            })}
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

  // 数的是**全部**条件，不只是一级四维：只数一级的话，用「4K + 评分≥9」筛空
  // 时标题会写「没有同时满足这 0 个条件的作品」
  const count = filterCount(filter);

  /** 单值维度清成 null，多值维度只摘掉这一个取值。dim 名与筛选字段同名。 */
  const drop = (dim: string, value: string) => {
    if (dim === "watch" || dim === "rating_gte" || dim === "hdr") {
      onFilterChange({ ...filter, [dim === "rating_gte" ? "ratingGte" : dim]: null });
      return;
    }
    const key = dim as MultiDim;
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
              // 窄屏加高：这里是抽屉里唯一的点按目标，py-0.5 的胶囊拇指很难点准
              className={`rounded-full border px-2.5 py-0.5 text-caption transition-colors disabled:pointer-events-none disabled:opacity-30 max-md:px-3 max-md:py-1.5 ${
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
  loading,
  filter,
  onFilterChange,
}: {
  facets: LibraryFacets | null;
  /** 全份 facet 还没到：先给一句话，别摆两个空标题等着内容弹进来 */
  loading?: boolean;
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
  // 窄屏（max-md）下这块只出现在 FilterSheet 里，排在一级四维下面：卡片外壳
  // 在抽屉里就成了"抽屉里又套一张卡"，与上面平铺的胶囊两种长相。所以窄屏拆掉
  // 边框、底色与内边距，分区只靠一道细线与小标题，整个抽屉一种排法。
  // 宽屏的「更多筛选」面板是在页面里展开的，卡片形态照旧
  if (loading) {
    return (
      <div className="mt-2.5 rounded-2xl border border-white/[0.1] bg-black/20 px-4 py-6 text-center text-sub text-[var(--text-faint)] max-md:mt-0 max-md:rounded-none max-md:border-x-0 max-md:border-b-0 max-md:border-white/[0.08] max-md:bg-transparent max-md:px-0 max-md:py-4">
        正在数各档位还剩多少部…
      </div>
    );
  }
  return (
    <div className="mt-2.5 grid grid-cols-2 gap-x-6 rounded-2xl border border-white/[0.1] bg-black/20 p-4 max-md:mt-0 max-md:grid-cols-1 max-md:rounded-none max-md:border-0 max-md:bg-transparent max-md:p-0">
      <div className="max-md:border-t max-md:border-white/[0.08] max-md:pt-3">
        <p className="mb-3 border-b border-white/[0.08] pb-2 text-sub font-semibold text-white max-md:mb-2.5 max-md:border-b-0 max-md:pb-0">
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
        <p className="mb-3 border-b border-white/[0.08] pb-2 text-sub font-semibold text-white max-md:mb-2.5 max-md:border-b-0 max-md:pb-0">
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
  // **chip 行只放自建的**：一个 300 部的库可能有 40+ 个自动生成的系列，
  // 混进来的话用户自己存的那三五个就被挤没了——而 chip 行的全部价值就是
  // "把我常用的那几组条件放在手边"。系列在合集视图里有自己的分组
  const mine = collections.filter((row) => row.kind !== "series");
  if (mine.length === 0) return null;
  const shown = mine.slice(0, CHIP_LIMIT);
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
      {mine.length > shown.length && (
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
 * 窄屏的筛选抽屉：点「筛选」直接拉起，一二级维度全在里面
 * （docs/design/library-filtering.md 5.2）。
 *
 * **每个维度直接平铺成胶囊**，不是一排下拉：点一下就是一个取值，取值和各自
 * 还剩几部一眼看全，没有「菜单里再弹菜单」。窄屏先后试过两种下拉摆法都别扭——
 * 横滚，滑动中手指一停就误触弹出；两列铺开，占掉半屏还要再点一层才看得到取值。
 * 一级四维在上、二级「找片 / 查库」在下，同一个抽屉往下滑就是，所以窄屏没有
 * 「更多筛选」这扇门。
 *
 * **非全屏**是要点：上方留出一截墙，用户看得见条件在实时影响什么。每次勾选
 * 立即生效，底部主按钮上的数字实时跳——不做"确定"式提交。做成"确定"式的话，
 * 用户得先盲选一遍条件再看结果，选错了还要重来；而这里筛选本来就是个来回
 * 试探的过程。
 *
 * 底部那颗 `查看 N 部` 因此不是"提交"，只是"我看完了，把抽屉收起来"。
 */
function FilterSheet({
  facets,
  loading,
  filter,
  onFilterChange,
  onToggle,
  onToggleWatch,
  onClose,
}: {
  facets: LibraryFacets | null;
  /** 全份 facet 还没到（二级维度的档位要等它） */
  loading?: boolean;
  filter: LibraryFilter;
  onFilterChange: (next: LibraryFilter) => void;
  onToggle: (dim: "genres" | "countries" | "decades", value: string) => void;
  onToggleWatch: (value: string) => void;
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
        aria-label="收起筛选"
        onClick={onClose}
        className="flex-1 cursor-default bg-black/25"
      />
      {/* 装下一二级全部维度，比原先只装二级时高一些，但仍给上方留出一截墙 */}
      <div className="flex max-h-[70dvh] flex-col rounded-t-2xl border-t border-white/10 bg-[rgba(16,18,26,0.92)] shadow-[0_-12px_40px_rgba(0,0,0,0.5)] backdrop-blur-2xl">
        <div className="flex shrink-0 items-center justify-center py-2">
          <span className="h-1 w-9 rounded-full bg-white/25" />
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4">
          {/* 一级四维：与宽屏那排下拉同一份取值与计数，只是换成平铺 */}
          <PillGroup
            label="类型"
            hint="可多选 · 维度内是「或」"
            options={facets?.genres ?? []}
            selected={(filter.genres ?? []).map(String)}
            onToggle={(v) => onToggle("genres", v)}
          />
          <PillGroup
            label="年代"
            options={facets?.decades ?? []}
            selected={filter.decades ?? []}
            onToggle={(v) => onToggle("decades", v)}
          />
          <PillGroup
            label="地区"
            hint="可多选 · 维度内是「或」"
            options={facets?.countries ?? []}
            selected={filter.countries ?? []}
            onToggle={(v) => onToggle("countries", v)}
          />
          <PillGroup
            label="观看"
            hint="单选"
            options={facets?.watch ?? []}
            selected={filter.watch ? [filter.watch] : []}
            onToggle={onToggleWatch}
          />
          <MoreFiltersPanel
            facets={facets}
            loading={loading}
            filter={filter}
            onFilterChange={onFilterChange}
          />
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
