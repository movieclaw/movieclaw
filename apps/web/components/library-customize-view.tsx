"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import {
  ArrowLeftIcon,
  ChevronDownIcon,
  GripIcon,
  HeartIcon,
  LibraryIcon,
  PlayIcon,
  TrashIcon,
} from "@/components/icons";
import { type Collection, listCollections } from "@/lib/api/collections";
import { type MediaLibrary, listLibraries } from "@/lib/api/libraries";
import {
  COLLECTION_SORTS,
  FAVORITES_SORT_PRESETS,
  type FavoritesSort,
  type HomeRow,
  type HomeRowSort,
  SORT_PRESETS,
  buildHomeRows,
  moveRow,
  newCollectionRow,
  newLibraryRow,
  rowMeta,
  rowTitle,
  rowsToPrefs,
  sortPresetsFor,
} from "@/lib/home-rows";
import { useUiPrefs } from "@/lib/ui-prefs";

/** 改动到落库的去抖：连点几下箭头只 PUT 一次；名字逐字键入也不会一字一请求。 */
const SAVE_DELAY_MS = 400;

/**
 * 自定义媒体库首页（/library/customize）。
 *
 * 首页 = 一份有序的行清单，每一行 = 来源 × 排序 × 名字
 * （docs/design/library-home-perspective.md）。本页是它唯一的配置面：首页上没有任何
 * 排序细节与行菜单，调整全部收在这里。
 *
 * - **独立页面，不是抽屉**：桌面与移动端同一份实现；纯列表、不放海报，效果回首页看。
 * - **没有保存键**：每次改动即时落库（去抖），只影响自己的首页（成员各存各的）。
 * - **点一行就地展开**它的排序单选、只看没看过的、名字；内置的「接下来继续」
 *   「我的媒体库」没有可改的，点了不展开。
 * - 隐藏的行留在原位压暗，不挪到底部；「恢复默认」是页面里唯一带确认的动作。
 *
 * 顺序改动走本地草稿立即呈现，落库成功后草稿让位给已保存值；落库失败保留草稿
 * 并提示，用户改一下就会再试。
 */
export function LibraryCustomizeView() {
  const { prefs, savePrefs } = useUiPrefs();
  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(null);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  // 本地草稿：null = 跟随已存偏好。改动先进草稿立即呈现，去抖后整体 PUT
  const [draft, setDraft] = useState<HomeRow[] | null>(null);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const editSeq = useRef(0);
  // savePrefs 闭包里的 prefs 会过期：PUT 的是整个界面偏好，得基于**最新**的其他分组
  const prefsRef = useRef(prefs);
  prefsRef.current = prefs;

  useEffect(() => {
    let cancelled = false;
    Promise.all([listLibraries(), listCollections()])
      .then(([libs, cols]) => {
        if (cancelled) return;
        setLibraries(libs);
        setCollections(cols);
        setLoadFailed(false);
      })
      .catch(() => !cancelled && setLoadFailed(true));
    return () => {
      cancelled = true;
    };
  }, []);

  const savedRows = useMemo(
    () => buildHomeRows(prefs.home, libraries ?? [], collections),
    [prefs.home, libraries, collections],
  );
  const rows = draft ?? savedRows;

  const commit = useCallback(
    (next: HomeRow[]) => {
      const seq = ++editSeq.current;
      setDraft(next);
      setSaveError(null);
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        savePrefs({ ...prefsRef.current, home: { rows: rowsToPrefs(next) } })
          .then(() => {
            // 期间没有新的改动才让草稿让位，否则会把用户刚改的那一下吞掉
            if (editSeq.current === seq) setDraft(null);
          })
          .catch((err: unknown) => {
            setSaveError(
              err instanceof Error ? err.message : "保存失败，请稍后再试",
            );
          });
      }, SAVE_DELAY_MS);
    },
    [savePrefs],
  );
  useEffect(
    () => () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    },
    [],
  );

  const update = (id: string, patch: (row: HomeRow) => HomeRow) =>
    commit(rows.map((row) => (row.id === id ? patch(row) : row)));
  const move = (index: number, direction: -1 | 1) =>
    commit(moveRow(rows, index, direction));
  const remove = (id: string) => {
    if (expanded === id) setExpanded(null);
    commit(rows.filter((row) => row.id !== id));
  };
  const add = (row: HomeRow) => {
    commit([...rows, row]);
    setExpanded(row.id);
  };
  const restoreDefaults = () => {
    if (!window.confirm("恢复默认布局？你自己加的行会被移除。")) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    editSeq.current += 1;
    setExpanded(null);
    setDraft(null);
    // 空清单 = 出厂布局，与侧栏导航同一约定
    savePrefs({ ...prefsRef.current, home: { rows: [] } }).catch(
      (err: unknown) => {
        setSaveError(
          err instanceof Error ? err.message : "保存失败，请稍后再试",
        );
      },
    );
  };

  const visibleLibraries = (libraries ?? []).filter(
    (library) => library.viewer_access,
  );
  const collectionsOnHome = new Set(
    rows
      .filter((row) => row.kind === "collection")
      .map((row) => row.collection.id),
  );
  const shownCount = rows.filter((row) => !row.hidden).length;

  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <div className="mx-auto w-full max-w-[640px] px-6 pt-6 max-md:px-4 max-md:pt-4">
        <Link
          href={"/library" as Route}
          className="inline-flex items-center gap-1 text-sub text-[var(--text-faint)] transition hover:text-[var(--text)]"
        >
          <ArrowLeftIcon className="size-4" />
          媒体库
        </Link>
        <div className="mt-2 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="text-on-image text-[24px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
              自定义首页
            </h2>
            <p className="text-on-image mt-1 text-sub text-[var(--text-muted)]">
              {libraries === null
                ? "正在读取…"
                : `${shownCount} 行显示 · ${rows.length - shownCount} 行隐藏 · 改动即时生效`}
            </p>
          </div>
          <button
            type="button"
            onClick={restoreDefaults}
            className="btn-glass mt-1 h-8 shrink-0 px-3 text-sub font-medium"
          >
            恢复默认
          </button>
        </div>

        {loadFailed && (
          <p className="mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub text-amber-200">
            读取媒体库与合集失败，行清单可能不完整；刷新页面重试。
          </p>
        )}
        {saveError && (
          <p className="mt-4 text-sub text-[var(--danger)]" role="alert">
            {saveError}
          </p>
        )}

        {/* 库与合集没回来之前不画列表：先画三条内置行、再蹦出库行，看着像列表在抖 */}
        {libraries !== null && (
          <ul className="mt-4 space-y-1.5" data-testid="home-rows">
            {rows.map((row, index) => (
              <RowItem
                key={row.id}
                row={row}
                index={index}
                total={rows.length}
                expanded={expanded === row.id}
                dragging={dragIndex === index}
                onToggle={() =>
                  setExpanded((current) => (current === row.id ? null : row.id))
                }
                onMove={(direction) => move(index, direction)}
                onHide={() =>
                  update(row.id, (r) => ({ ...r, hidden: !r.hidden }))
                }
                onChange={(patch) => update(row.id, patch)}
                onRemove={() => remove(row.id)}
                onDragStart={() => setDragIndex(index)}
                onDragEnd={() => setDragIndex(null)}
                onDragEnter={() => {
                  // 拖到哪就换到哪（跟手实时重排），与设置页导航顺序同一手感
                  if (dragIndex == null || dragIndex === index) return;
                  const next = rows.slice();
                  const [moved] = next.splice(dragIndex, 1);
                  next.splice(index, 0, moved);
                  commit(next);
                  setDragIndex(index);
                }}
              />
            ))}
          </ul>
        )}

        {/* 添加一行只问一个问题：从哪来。选一个库得到「最近添加的 X」，选一个合集得到
            它本身；排序和名字点开那一行再改。已在首页的合集置灰 */}
        <div className="mt-4 rounded-xl border border-dashed border-white/15 px-4 py-3">
          <p className="text-caption text-[var(--text-faint)]">
            ＋ 添加一行 · 从哪来？
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {visibleLibraries.map((library) => (
              <button
                key={library.id}
                type="button"
                onClick={() => add(newLibraryRow(library))}
                className="rounded-full border border-white/15 px-3 py-1 text-sub text-[var(--text-muted)] transition hover:bg-white/[0.07] hover:text-[var(--text)]"
              >
                {library.name}库
              </button>
            ))}
          </div>
          {collections.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {collections.map((collection) => {
                const onHome = collectionsOnHome.has(collection.id);
                return (
                  <button
                    key={collection.id}
                    type="button"
                    disabled={onHome}
                    onClick={() => add(newCollectionRow(collection))}
                    className="rounded-full border border-white/15 px-3 py-1 text-sub text-[var(--text-muted)] transition hover:bg-white/[0.07] hover:text-[var(--text)] disabled:opacity-35 disabled:hover:bg-transparent"
                  >
                    {collection.name}
                    <span className="ml-1.5 text-caption text-[var(--text-faint)]">
                      {onHome ? "已在首页" : `${collection.item_count} 部`}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
        <p className="mt-3 text-caption text-[var(--text-faint)]">
          改动即时生效，只影响你自己的首页。点一行展开它的排序和名字，效果回首页看。
        </p>
      </div>
    </div>
  );
}

function editable(row: HomeRow): boolean {
  return row.kind !== "up-next" && row.kind !== "libraries";
}

function RowIcon({ row }: { row: HomeRow }) {
  const cls = "size-4";
  switch (row.kind) {
    case "up-next":
      return <PlayIcon className={cls} />;
    case "favorites":
      return <HeartIcon className={cls} />;
    case "libraries":
      return <LibraryIcon className={cls} />;
    case "collection":
      return <CollectionMark />;
    default:
      return <span className="text-[13px] leading-none">≡</span>;
  }
}

/** 迷你叠卡记号：一眼分清「这是一组」和「这是一个库」。 */
function CollectionMark() {
  return (
    <span className="relative block h-3 w-2.5" aria-hidden>
      <i className="absolute inset-0 translate-x-[3px] scale-y-[0.84] rounded-[2px] border border-current opacity-30" />
      <i className="absolute inset-0 rounded-[2px] border border-current opacity-70" />
    </span>
  );
}

function RowItem({
  row,
  index,
  total,
  expanded,
  dragging,
  onToggle,
  onMove,
  onHide,
  onChange,
  onRemove,
  onDragStart,
  onDragEnd,
  onDragEnter,
}: {
  row: HomeRow;
  index: number;
  total: number;
  expanded: boolean;
  dragging: boolean;
  onToggle: () => void;
  onMove: (direction: -1 | 1) => void;
  onHide: () => void;
  onChange: (patch: (row: HomeRow) => HomeRow) => void;
  onRemove: () => void;
  onDragStart: () => void;
  onDragEnd: () => void;
  onDragEnter: () => void;
}) {
  const canEdit = editable(row);
  const title = rowTitle(row);
  return (
    <li
      draggable
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onDragEnter={onDragEnter}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => e.preventDefault()}
      data-testid={`home-row-item-${row.id}`}
      data-hidden={row.hidden ? "true" : undefined}
      className={`rounded-xl border transition ${
        dragging
          ? "border-white/20 bg-white/[0.1] opacity-60"
          : expanded
            ? "border-white/[0.16] bg-white/[0.045]"
            : "border-white/[0.08] bg-white/[0.03]"
      }`}
    >
      {/* 行头：整个行头是点击区（可改的行才展开）。右侧只有上下移与眼睛：
          手机上箭头叠成一列，名字与小字不被挤断 */}
      <div
        role={canEdit ? "button" : undefined}
        tabIndex={canEdit ? 0 : undefined}
        onClick={canEdit ? onToggle : undefined}
        onKeyDown={(e) => {
          if (!canEdit) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
        className={`flex items-center gap-2.5 py-2 pl-2 pr-2.5 max-md:pl-2.5 ${canEdit ? "cursor-pointer" : ""}`}
      >
        <GripIcon className="size-4 shrink-0 cursor-grab text-[var(--text-faint)] max-md:hidden" />
        <span
          className={`grid size-6 shrink-0 place-items-center rounded-md text-[var(--text-muted)] max-md:size-5 ${
            row.kind === "collection"
              ? "bg-[rgba(127,176,255,0.14)] text-[#7fb0ff]"
              : "bg-white/[0.06]"
          } ${row.hidden ? "opacity-40" : ""}`}
        >
          <RowIcon row={row} />
        </span>
        <div className={`min-w-0 flex-1 ${row.hidden ? "opacity-40" : ""}`}>
          <div className="truncate text-ui font-semibold text-[var(--text)] max-md:text-sub">
            {title}
          </div>
          <div className="truncate text-caption text-[var(--text-faint)]">
            {rowMeta(row)}
          </div>
        </div>
        <div
          className="flex shrink-0 items-center gap-1 max-md:gap-1.5"
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
          role="presentation"
        >
          <div className="flex items-center gap-0.5 max-md:flex-col max-md:gap-0">
            <MoveButton
              label={`把「${title}」上移`}
              up
              disabled={index === 0}
              onClick={() => onMove(-1)}
            />
            <MoveButton
              label={`把「${title}」下移`}
              disabled={index === total - 1}
              onClick={() => onMove(1)}
            />
          </div>
          <button
            type="button"
            aria-label={row.hidden ? `显示「${title}」` : `隐藏「${title}」`}
            aria-pressed={!row.hidden}
            title={row.hidden ? "显示这一行" : "隐藏这一行"}
            onClick={onHide}
            className="grid size-8 place-items-center rounded-md text-[var(--text-muted)] transition hover:bg-white/[0.08] hover:text-[var(--text)] max-md:size-9"
            data-testid="row-visibility"
          >
            <EyeIcon off={row.hidden} />
          </button>
        </div>
        <span className="w-3 shrink-0 text-center text-[10px] text-[var(--text-faint)] max-md:hidden">
          {canEdit && (
            <ChevronDownIcon
              className={`size-3 transition ${expanded ? "rotate-180" : ""}`}
            />
          )}
        </span>
      </div>

      {expanded && canEdit && (
        <RowEditor row={row} onChange={onChange} onRemove={onRemove} />
      )}
    </li>
  );
}

/**
 * 就地展开的编辑区，只有三样：排序单选（每个选项就是推荐名，右侧小字是规则）、
 * 「只显示我没看过的」开关（仅库行）、名字输入框（占位文字实时显示推荐）。
 * 合集行只有排序单选，加一个「打开合集」；自加行多一个「删除这一行」。
 * 来源不在这里改：换库等于另一行，删了重加。
 */
function RowEditor({
  row,
  onChange,
  onRemove,
}: {
  row: HomeRow;
  onChange: (patch: (row: HomeRow) => HomeRow) => void;
  onRemove: () => void;
}) {
  if (row.kind === "favorites") {
    return (
      <div className="border-t border-white/[0.08] px-3 pb-3 pt-2.5 md:pl-[52px]">
        <p className="text-caption text-[var(--text-faint)]">排序</p>
        <SortRadios
          options={(Object.keys(FAVORITES_SORT_PRESETS) as FavoritesSort[]).map(
            (key) => ({
              key,
              label: FAVORITES_SORT_PRESETS[key].name,
              hint: FAVORITES_SORT_PRESETS[key].hint,
            }),
          )}
          value={row.sort}
          onChange={(sort) =>
            onChange((r) =>
              r.kind === "favorites"
                ? { ...r, sort: sort as FavoritesSort }
                : r,
            )
          }
        />
      </div>
    );
  }
  if (row.kind === "collection") {
    const href = (
      row.collection.library_id === null
        ? `/library/c/${row.collection.id}`
        : `/library/${row.collection.library_id}/c/${row.collection.id}`
    ) as Route;
    return (
      <div className="border-t border-white/[0.08] px-3 pb-3 pt-2.5 md:pl-[52px]">
        <p className="text-caption text-[var(--text-faint)]">排序</p>
        <SortRadios
          options={COLLECTION_SORTS.map((key) => ({
            key,
            label: SORT_PRESETS[key].name(""),
            hint: SORT_PRESETS[key].hint,
          }))}
          value={row.sort}
          onChange={(sort) =>
            onChange((r) =>
              r.kind === "collection" ? { ...r, sort: sort as HomeRowSort } : r,
            )
          }
        />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-caption text-[var(--text-faint)]">
            名字跟合集走，规则在合集页改
          </span>
          <span className="flex-1" />
          <Link
            href={href}
            className="btn-glass px-3 py-1 text-sub font-medium"
          >
            打开合集 ›
          </Link>
          <button
            type="button"
            onClick={onRemove}
            className="inline-flex items-center gap-1 rounded-full px-3 py-1 text-sub text-[var(--danger)] transition hover:bg-white/[0.06]"
          >
            <TrashIcon className="size-3.5" />
            删除这一行
          </button>
        </div>
      </div>
    );
  }
  if (row.kind !== "library") return null;
  const libraryName = row.library.name;
  const suggested = SORT_PRESETS[row.sort].name(libraryName);
  return (
    <div className="border-t border-white/[0.08] px-3 pb-3 pt-2.5 md:pl-[52px]">
      <p className="text-caption text-[var(--text-faint)]">排序</p>
      <SortRadios
        options={sortPresetsFor(row.library.kind).map((key) => ({
          key,
          label: SORT_PRESETS[key].name(libraryName),
          hint: SORT_PRESETS[key].hint,
        }))}
        value={row.sort}
        onChange={(sort) =>
          onChange((r) =>
            r.kind === "library" ? { ...r, sort: sort as HomeRowSort } : r,
          )
        }
      />
      <label className="mt-3 flex items-center gap-2.5 text-sub text-[var(--text)]">
        <Switch
          checked={row.unwatched}
          label="只显示我没看过的"
          onChange={(unwatched) =>
            onChange((r) => (r.kind === "library" ? { ...r, unwatched } : r))
          }
        />
        只显示我没看过的
        <span className="ml-auto text-caption text-[var(--text-faint)] max-md:hidden">
          按评分排时，没有它这一行永远是同 20 部
        </span>
      </label>
      <p className="mt-3 text-caption text-[var(--text-faint)]">名字</p>
      <input
        value={row.name}
        placeholder={suggested}
        maxLength={40}
        aria-label="这一行的名字"
        onChange={(e) => {
          const name = e.target.value;
          onChange((r) => (r.kind === "library" ? { ...r, name } : r));
        }}
        onBlur={() =>
          onChange((r) =>
            r.kind === "library" ? { ...r, name: r.name.trim() } : r,
          )
        }
        className="mt-1.5 w-full rounded-lg border border-white/15 bg-white/[0.05] px-3 py-1.5 text-sub text-[var(--text)] outline-none placeholder:text-[var(--text-faint)] focus:border-white/30"
        data-testid="row-name"
      />
      <p className="mt-1.5 text-caption text-[var(--text-faint)]">
        {row.name ? (
          <>
            已手动命名。清空则回到推荐：
            <span className="text-[var(--text-muted)]">{suggested}</span>
          </>
        ) : (
          "留空跟随排序：换一个排序，名字自动变"
        )}
      </p>
      {!row.builtin && (
        <div className="mt-3 flex justify-end">
          <button
            type="button"
            onClick={onRemove}
            className="inline-flex items-center gap-1 rounded-full px-3 py-1 text-sub text-[var(--danger)] transition hover:bg-white/[0.06]"
          >
            <TrashIcon className="size-3.5" />
            删除这一行
          </button>
        </div>
      )}
    </div>
  );
}

function SortRadios({
  options,
  value,
  onChange,
}: {
  options: { key: string; label: string; hint: string }[];
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <div
      role="radiogroup"
      className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-0.5 max-md:grid-cols-1"
    >
      {options.map((option) => {
        const on = option.key === value;
        return (
          <button
            key={option.key}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => onChange(option.key)}
            className={`flex items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sub transition hover:bg-white/[0.045] ${
              on ? "text-[var(--text)]" : "text-[var(--text-muted)]"
            }`}
            data-testid={`row-sort-${option.key}`}
          >
            <span
              className={`grid size-3.5 shrink-0 place-items-center rounded-full border ${
                on ? "border-[#7fb0ff]" : "border-white/25"
              }`}
            >
              {on && <span className="size-[7px] rounded-full bg-[#7fb0ff]" />}
            </span>
            <span className="truncate">{option.label}</span>
            <span className="ml-auto shrink-0 text-caption text-[var(--text-faint)] max-md:hidden">
              {option.hint}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function Switch({
  checked,
  label,
  onChange,
}: {
  checked: boolean;
  label: string;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative h-[18px] w-[30px] shrink-0 rounded-full transition ${
        checked ? "bg-[#7fb0ff]" : "bg-white/[0.16]"
      }`}
      data-testid="row-unwatched"
    >
      <span
        className={`absolute left-[2px] top-[2px] size-[14px] rounded-full bg-white transition ${
          checked ? "translate-x-3" : ""
        }`}
      />
    </button>
  );
}

function MoveButton({
  label,
  up = false,
  disabled,
  onClick,
}: {
  label: string;
  up?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      // 触屏上 HTML5 拖放根本不触发，这两颗键是触屏用户改序的唯一入口；
      // 手机上两颗叠成一列，宽度只占一颗，名字与小字不被挤断
      className="grid size-7 place-items-center rounded-md text-[var(--text-muted)] transition hover:bg-white/[0.08] hover:text-[var(--text)] disabled:opacity-25 disabled:hover:bg-transparent max-md:size-5"
    >
      <ChevronDownIcon className={`size-3.5 ${up ? "rotate-180" : ""}`} />
    </button>
  );
}

function EyeIcon({ off }: { off: boolean }) {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      className="size-4"
    >
      <path
        d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"
        opacity={off ? 0.45 : 1}
      />
      {off ? <path d="M3 13L13 3" /> : <circle cx="8" cy="8" r="2" />}
    </svg>
  );
}
