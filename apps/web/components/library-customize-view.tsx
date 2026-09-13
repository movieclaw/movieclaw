"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import {
  ArrowLeftIcon,
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
  moveRowTo,
  newCollectionRow,
  newLibraryRow,
  rowMeta,
  rowTitle,
  rowsToPrefs,
  sortPresetsFor,
} from "@/lib/home-rows";
import { useUiPrefs } from "@/lib/ui-prefs";

/** 改动到落库的去抖：拖拽过程中每换一次位都是一次改动，松手后只 PUT 一次；名字逐字键入也不会一字一请求。 */
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
 * - **不展开**：每行就是一条——名字直接在行上改（库行），排序是一个下拉框，
 *   「只看没看过的」是一颗小开关，眼睛是显隐，自加行多一个删除。
 * - **拖拽排序**：左侧把手是唯一的换位方式（指针事件实现，触屏也能拖；键盘 Alt+↑/↓）。
 * - 隐藏的行留在原位压暗，不挪到底部；「恢复默认」是页面里唯一带确认的动作。
 *
 * 改动走本地草稿立即呈现，落库成功后草稿让位给已保存值；落库失败保留草稿
 * 并提示，用户改一下就会再试。
 */
export function LibraryCustomizeView() {
  const { prefs, savePrefs } = useUiPrefs();
  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(null);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
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
  // 拖拽期间 pointermove 连发，读最新的行要走 ref，不能靠闭包
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

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
    commit(rowsRef.current.map((row) => (row.id === id ? patch(row) : row)));
  const remove = (id: string) =>
    commit(rowsRef.current.filter((row) => row.id !== id));
  const add = (row: HomeRow) => commit([...rowsRef.current, row]);
  const restoreDefaults = () => {
    if (!window.confirm("恢复默认布局？你自己加的行会被移除。")) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    editSeq.current += 1;
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

  // —— 拖拽排序：指针事件而不是 HTML5 DnD——后者触屏根本不触发，而手机上没有别的换位入口。
  //    把手 pointerdown 捕获指针，之后 move 事件都送到把手上（行重排后 DOM 节点不变，
  //    捕获不丢）；落点 = 指针所在的那一行的中线之上/之下，跟手实时重排
  const listRef = useRef<HTMLUListElement>(null);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const dragPointer = useRef<number | null>(null);
  const onGripPointerDown = (
    e: React.PointerEvent<HTMLElement>,
    id: string,
  ) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragPointer.current = e.pointerId;
    setDraggingId(id);
  };
  const onGripPointerMove = (
    e: React.PointerEvent<HTMLElement>,
    id: string,
  ) => {
    if (dragPointer.current !== e.pointerId || !listRef.current) return;
    const items = Array.from(
      listRef.current.querySelectorAll<HTMLLIElement>("li[data-row-id]"),
    );
    let target = items.length - 1;
    for (let index = 0; index < items.length; index += 1) {
      const box = items[index].getBoundingClientRect();
      if (e.clientY < box.top + box.height / 2) {
        target = index;
        break;
      }
    }
    const from = rowsRef.current.findIndex((row) => row.id === id);
    if (from === -1 || from === target) return;
    commit(moveRowTo(rowsRef.current, from, target));
  };
  const onGripPointerUp = () => {
    dragPointer.current = null;
    setDraggingId(null);
  };
  const moveByKey = (id: string, offset: -1 | 1) => {
    const from = rowsRef.current.findIndex((row) => row.id === id);
    commit(moveRowTo(rowsRef.current, from, from + offset));
  };

  const visibleLibraries = (libraries ?? []).filter(
    (library) => library.viewer_access,
  );
  const pickableCollections = collections.filter(
    (collection) => collection.kind !== "builtin",
  );
  const collectionsOnHome = new Set(
    rows
      .filter((row) => row.kind === "collection")
      .map((row) => row.collection.id),
  );
  const shownCount = rows.filter((row) => !row.hidden).length;

  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <div className="mx-auto w-full max-w-[680px] px-6 pt-6 max-md:px-4 max-md:pt-4">
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
                : `${shownCount} 行显示 · ${rows.length - shownCount} 行隐藏 · 拖动左侧把手调整顺序，改动即时生效`}
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
          <ul
            ref={listRef}
            className="mt-4 space-y-1.5"
            data-testid="home-rows"
          >
            {rows.map((row) => (
              <RowItem
                key={row.id}
                row={row}
                dragging={draggingId === row.id}
                onChange={(patch) => update(row.id, patch)}
                onRemove={() => remove(row.id)}
                onGripPointerDown={(e) => onGripPointerDown(e, row.id)}
                onGripPointerMove={(e) => onGripPointerMove(e, row.id)}
                onGripPointerUp={onGripPointerUp}
                onMoveKey={(offset) => moveByKey(row.id, offset)}
              />
            ))}
          </ul>
        )}

        {/* 添加一行只问一个问题：从哪来。选一个库得到「最近添加的 X」，选一个合集得到
            它本身；排序和名字在行上直接改。已在首页的合集置灰；内置的「我的收藏」合集
            不进候选（首页已经有「我的收藏」这一行） */}
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
          {pickableCollections.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {pickableCollections.map((collection) => {
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
          改动即时生效，只影响你自己的首页。库行的名字留空就跟随排序推荐；效果回首页看。
        </p>
      </div>
    </div>
  );
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

/** 这一行可选的排序档：内置的「接下来继续」「我的媒体库」没有；其余各自一张短表。 */
function sortOptions(row: HomeRow): { key: string; label: string }[] | null {
  switch (row.kind) {
    case "favorites":
      return (Object.keys(FAVORITES_SORT_PRESETS) as FavoritesSort[]).map(
        (key) => ({
          key,
          label: FAVORITES_SORT_PRESETS[key].name,
        }),
      );
    case "collection":
      return COLLECTION_SORTS.map((key) => ({
        key,
        label: SORT_PRESETS[key].short,
      }));
    case "library":
      return sortPresetsFor(row.library.kind).map((key) => ({
        key,
        label: SORT_PRESETS[key].name(row.library.name),
      }));
    default:
      return null;
  }
}

/**
 * 一行：把手 · 类型 · 名字（库行直接可改）+ 小字 · 排序下拉 · 只看没看过的（库行）·
 * 眼睛 · 删除（自加行）。没有展开态。手机上控件换到第二行。
 */
function RowItem({
  row,
  dragging,
  onChange,
  onRemove,
  onGripPointerDown,
  onGripPointerMove,
  onGripPointerUp,
  onMoveKey,
}: {
  row: HomeRow;
  dragging: boolean;
  onChange: (patch: (row: HomeRow) => HomeRow) => void;
  onRemove: () => void;
  onGripPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
  onGripPointerMove: (e: React.PointerEvent<HTMLElement>) => void;
  onGripPointerUp: () => void;
  onMoveKey: (offset: -1 | 1) => void;
}) {
  const title = rowTitle(row);
  const options = sortOptions(row);
  const sort =
    row.kind === "favorites" ||
    row.kind === "library" ||
    row.kind === "collection"
      ? row.sort
      : null;
  const removable =
    row.kind === "collection" || (row.kind === "library" && !row.builtin);
  return (
    <li
      data-row-id={row.id}
      data-testid={`home-row-item-${row.id}`}
      data-hidden={row.hidden ? "true" : undefined}
      className={`flex items-center gap-2.5 rounded-xl border py-2 pl-1.5 pr-2.5 transition max-md:flex-wrap ${
        dragging
          ? "border-white/25 bg-white/[0.1] shadow-lg"
          : "border-white/[0.08] bg-white/[0.03]"
      }`}
    >
      {/* 把手：唯一的换位入口。touch-none 让触屏拖动时页面不跟着滚 */}
      <button
        type="button"
        aria-label={`拖动调整「${title}」的顺序（或按 Alt + 上下方向键）`}
        title="拖动调整顺序"
        onPointerDown={onGripPointerDown}
        onPointerMove={onGripPointerMove}
        onPointerUp={onGripPointerUp}
        onPointerCancel={onGripPointerUp}
        onKeyDown={(e) => {
          if (!e.altKey) return;
          if (e.key === "ArrowUp") {
            e.preventDefault();
            onMoveKey(-1);
          } else if (e.key === "ArrowDown") {
            e.preventDefault();
            onMoveKey(1);
          }
        }}
        className="grid size-8 shrink-0 cursor-grab touch-none place-items-center rounded-md text-[var(--text-faint)] outline-none transition hover:bg-white/[0.06] hover:text-[var(--text)] focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] active:cursor-grabbing"
        data-testid="row-grip"
      >
        <GripIcon className="size-4" />
      </button>
      <span
        className={`grid size-6 shrink-0 place-items-center rounded-md text-[var(--text-muted)] ${
          row.kind === "collection"
            ? "bg-[rgba(127,176,255,0.14)] text-[#7fb0ff]"
            : "bg-white/[0.06]"
        } ${row.hidden ? "opacity-40" : ""}`}
      >
        <RowIcon row={row} />
      </span>
      <div className={`min-w-0 flex-1 ${row.hidden ? "opacity-40" : ""}`}>
        {row.kind === "library" ? (
          // 名字直接在行上改：占位文字就是推荐名，留空即跟随排序
          <input
            value={row.name}
            placeholder={SORT_PRESETS[row.sort].name(row.library.name)}
            maxLength={40}
            aria-label={`「${title}」的名字`}
            title="点击改名；留空跟随排序推荐"
            onChange={(e) => {
              const name = e.target.value;
              onChange((r) => (r.kind === "library" ? { ...r, name } : r));
            }}
            onBlur={() =>
              onChange((r) =>
                r.kind === "library" ? { ...r, name: r.name.trim() } : r,
              )
            }
            className="-mx-1.5 w-full max-w-full rounded-md border border-transparent bg-transparent px-1.5 py-0.5 text-ui font-semibold text-[var(--text)] outline-none transition placeholder:text-[var(--text)] hover:border-white/15 focus:border-white/30 focus:bg-white/[0.05] focus:placeholder:text-[var(--text-faint)] max-md:text-sub"
            data-testid="row-name"
          />
        ) : (
          <div className="truncate py-0.5 text-ui font-semibold text-[var(--text)] max-md:text-sub">
            {title}
          </div>
        )}
        <div className="truncate text-caption text-[var(--text-faint)]">
          {rowMeta(row)}
        </div>
      </div>
      {/* 排序与「未看」在手机上换到第二行（flex-wrap + basis-full + order），眼睛与删除
          留在第一行——没有排序的内置行因此仍是一行高 */}
      {options && sort !== null && (
        <div className="flex shrink-0 items-center gap-1.5 max-md:order-last max-md:basis-full max-md:justify-end max-md:pl-[46px]">
          <select
            value={sort}
            aria-label={`「${title}」的排序`}
            onChange={(e) => {
              const next = e.target.value;
              onChange((r) => {
                if (r.kind === "favorites")
                  return { ...r, sort: next as FavoritesSort };
                if (r.kind === "library" || r.kind === "collection")
                  return { ...r, sort: next as HomeRowSort };
                return r;
              });
            }}
            className="h-8 max-w-[168px] rounded-md border border-white/15 bg-white/[0.05] px-2 text-sub text-[var(--text)] outline-none focus:border-white/30"
            data-testid="row-sort"
          >
            {options.map((option) => (
              <option
                key={option.key}
                value={option.key}
                className="bg-[#161923] text-white"
              >
                {option.label}
              </option>
            ))}
          </select>
          {row.kind === "library" && (
            <button
              type="button"
              aria-pressed={row.unwatched}
              title="只显示我没看过的：按评分排时，没有它这一行永远是同 20 部"
              onClick={() =>
                onChange((r) =>
                  r.kind === "library" ? { ...r, unwatched: !r.unwatched } : r,
                )
              }
              className={`h-8 rounded-md border px-2 text-sub transition ${
                row.unwatched
                  ? "border-[#7fb0ff]/60 bg-[rgba(127,176,255,0.14)] text-[var(--text)]"
                  : "border-white/15 text-[var(--text-muted)] hover:bg-white/[0.06]"
              }`}
              data-testid="row-unwatched"
            >
              未看
            </button>
          )}
        </div>
      )}
      <div className="flex shrink-0 items-center gap-0.5">
        <button
          type="button"
          aria-label={row.hidden ? `显示「${title}」` : `隐藏「${title}」`}
          aria-pressed={!row.hidden}
          title={row.hidden ? "显示这一行" : "隐藏这一行"}
          onClick={() => onChange((r) => ({ ...r, hidden: !r.hidden }))}
          className="grid size-8 place-items-center rounded-md text-[var(--text-muted)] transition hover:bg-white/[0.08] hover:text-[var(--text)]"
          data-testid="row-visibility"
        >
          <EyeIcon off={row.hidden} />
        </button>
        {removable && (
          <button
            type="button"
            aria-label={`删除「${title}」`}
            title="删除这一行"
            onClick={onRemove}
            className="grid size-8 place-items-center rounded-md text-[var(--text-faint)] transition hover:bg-white/[0.08] hover:text-[var(--danger)]"
            data-testid="row-remove"
          >
            <TrashIcon className="size-4" />
          </button>
        )}
      </div>
    </li>
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
